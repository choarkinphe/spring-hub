"""Opt-in, unprivileged installation of a pinned FFmpeg/ffprobe bundle."""
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Monthly builds are retained upstream; never follow the mutable latest tag.
RELEASE = "autobuild-2026-09-30-13-08"
PACKAGES = {
    "x86_64": ("ffmpeg-n8.1.3-9-g29e619e767-linux64-gpl-8.1.tar.xz", 150360944,
               "97ce978979194b5cf7e06a5e68020dbdaa7a4f3294c5452b6a1bc347100dbb79"),
    "aarch64": ("ffmpeg-n8.1.3-9-g29e619e767-linuxarm64-gpl-8.1.tar.xz", 126468728,
                "10523d1e0be6b3ce62a9708cc50815c4b5eb5f36ae147fbe2afeecda6f2d6845"),
}
BUSY = {"starting", "downloading", "verifying", "extracting", "checking"}
MAX_EXTRACTED = 1024 * 1024 * 1024


def package():
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine().lower())
    return PACKAGES.get(machine) if platform.system() == "Linux" else None


def install_root(database):
    return Path(database).resolve().parent / "tools" / "ffmpeg"


def managed_paths(database):
    """Resolve only our pinned bundle; never accept a user-provided executable path."""
    item = package()
    if not item:
        return None
    root = install_root(database)
    directory = root / item[2]
    marker = directory / "installed.json"
    if root.is_symlink() or root.parent.is_symlink() or directory.is_symlink() or marker.is_symlink():
        return None
    try:
        if marker.stat().st_size > 4096 or json.loads(marker.read_text()) != {"sha256": item[2], "release": RELEASE}:
            return None
        paths = {"ffmpeg_bin": str(directory / "ffmpeg"), "ffprobe_bin": str(directory / "ffprobe")}
        if any(Path(p).is_symlink() or not Path(p).is_file() or not os.access(p, os.X_OK) for p in paths.values()):
            return None
        return paths
    except (OSError, ValueError):
        return None


def allowed_download(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"} and not parsed.username and not parsed.password and parsed.port in (None, 443)


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_download(newurl):
            raise ValueError("下载重定向超出受信任的 HTTPS 地址范围")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class FFmpegInstall:
    def __init__(self, config, backends):
        self.config, self.backends = config, backends
        self._lock = threading.Lock()
        self._state = {"status": "idle", "message": "尚未安装应用目录版本。", "downloaded": 0}
        self._thread = None

    def status(self):
        engine = self.backends.get("ffmpeg")
        ffmpeg, ffprobe = engine.binary_path(), engine.ffprobe_path()
        installed = bool(ffmpeg and ffprobe)
        with self._lock:
            result = dict(self._state)
        item = package()
        root = install_root(self.config.database)
        parent = root
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        writable = os.access(parent, os.W_OK | os.X_OK)
        managed = managed_paths(self.config.database)
        result.update(installed=installed, supported=bool(item), can_install=bool(item) and writable and not installed and result["status"] not in BUSY,
                      ffmpeg=ffmpeg, ffprobe=ffprobe, directory=str(root), total=item[1] if item else 0,
                      source="应用目录" if managed and ffmpeg == managed["ffmpeg_bin"] else "部署环境",
                      release=RELEASE, provider="BtbN/FFmpeg-Builds", license="GPL-3.0",
                      source_url=f"https://github.com/BtbN/FFmpeg-Builds/releases/tag/{RELEASE}")
        if installed and result["status"] == "idle":
            result["message"] = "使用已安装的 FFmpeg / ffprobe，无需下载或覆盖。"
        if not item:
            result["message"] = "应用目录安装仅支持 Linux x86_64 / ARM64；其他环境请使用部署提供的 FFmpeg。"
        elif not writable and not installed:
            result["message"] = "服务数据目录不可写，请由管理员检查数据卷权限。"
        return result

    def start(self):
        with self._lock:
            if self._state["status"] in BUSY:
                raise RuntimeError("FFmpeg 安装正在进行，请勿重复提交")
        report = self.status()
        if report["installed"]:
            return report
        if not report["can_install"]:
            raise ValueError(report["message"])
        with self._lock:
            if self._state["status"] in BUSY:
                raise RuntimeError("FFmpeg 安装正在进行，请勿重复提交")
            self._state = {"status": "starting", "message": "准备安装到应用数据目录…", "downloaded": 0}
            self._thread = threading.Thread(target=self._run, daemon=True, name="ffmpeg-install")
            self._thread.start()
        return self.status()

    def _update(self, status, message, **values):
        with self._lock:
            self._state.update(status=status, message=message, **values)

    def _download(self, target, item):
        name, size, digest = item
        url = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{RELEASE}/{name}"
        checksum, downloaded = hashlib.sha256(), 0
        deadline = time.monotonic() + 900
        opener = urllib.request.build_opener(DownloadRedirect())
        self._update("downloading", "正在下载固定版本的 FFmpeg / ffprobe…")
        with target.open("xb") as output:
            for attempt in range(5):
                headers = {"User-Agent": "SpringHub-FFmpeg-Installer", "Accept-Encoding": "identity"}
                if downloaded:
                    headers["Range"] = f"bytes={downloaded}-"
                request = urllib.request.Request(url, headers=headers)
                try:
                    with opener.open(request, timeout=30) as response:
                        if not allowed_download(response.geturl()):
                            raise ValueError("下载地址不是受信任的 HTTPS 来源")
                        status = getattr(response, "status", 200)
                        if status == 206:
                            expected = f"bytes {downloaded}-{size - 1}/{size}"
                            if response.headers.get("Content-Range") != expected:
                                raise ValueError("下载分段范围不匹配，未运行程序")
                        elif status == 200:
                            # Some proxies ignore Range: restart, never append a
                            # full response to a partial archive.
                            output.seek(0)
                            output.truncate()
                            checksum, downloaded = hashlib.sha256(), 0
                        else:
                            raise ValueError("下载响应状态无效")
                        while chunk := response.read(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > size or time.monotonic() > deadline:
                                raise ValueError("下载超出大小或时间限制，请重试")
                            checksum.update(chunk)
                            output.write(chunk)
                            self._update("downloading", "正在下载固定版本的 FFmpeg / ffprobe…", downloaded=downloaded)
                except (OSError, TimeoutError):
                    if attempt == 4 or time.monotonic() > deadline:
                        raise ValueError("下载中断且重试失败，请检查网络后重试")
                if downloaded == size:
                    break
                if time.monotonic() > deadline:
                    break
                self._update("downloading", "下载中断，正在重试续传…", downloaded=downloaded)
        self._update("verifying", "正在核对文件大小与固定 SHA-256…")
        if downloaded != size:
            raise ValueError("下载多次中断，文件不完整；未安装或运行程序，请重试")
        if checksum.hexdigest() != digest:
            raise ValueError("下载文件 SHA-256 不匹配，未安装或运行任何程序")

    @staticmethod
    def _extract(archive, directory):
        found, expanded, count = set(), 0, 0
        # Do not extractall: archive paths, links, device nodes and permissions
        # must never control writes outside this private staging directory.
        with tarfile.open(archive, "r:xz") as bundle:
            for member in bundle:
                count += 1
                path = Path(member.name)
                if count > 20000 or path.is_absolute() or ".." in path.parts or "\\" in member.name:
                    raise ValueError("安装包包含不安全的路径或过多文件")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("安装包包含链接或特殊文件，拒绝解包")
                expanded += member.size
                if expanded > MAX_EXTRACTED or member.size < 0:
                    raise ValueError("安装包展开大小超出限制")
                binary = path.name in ("ffmpeg", "ffprobe") and len(path.parts) == 3 and path.parts[-2] == "bin"
                license_file = path.name in ("LICENSE", "LICENSE.txt", "COPYING", "README.txt", "README.md") and len(path.parts) == 2
                if not binary and not license_file:
                    continue
                if path.name in found:
                    raise ValueError("安装包有重复的程序或许可文件")
                found.add(path.name)
                with bundle.extractfile(member) as source, (directory / path.name).open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                (directory / path.name).chmod(0o700 if binary else 0o600)
        if not {"ffmpeg", "ffprobe"}.issubset(found):
            raise ValueError("安装包缺少 FFmpeg 或 ffprobe")

    @staticmethod
    def _check(directory):
        for name in ("ffmpeg", "ffprobe"):
            result = subprocess.run([str(directory / name), "-version"], capture_output=True, timeout=10, text=True)
            if result.returncode or not result.stdout.startswith(name + " version "):
                raise ValueError("FFmpeg / ffprobe 版本验证失败；当前系统可能不兼容此构建")

    def _run(self):
        try:
            item = package()
            root = install_root(self.config.database)
            if root.is_symlink() or root.parent.is_symlink():
                raise ValueError("应用工具目录不能是软链接")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = root / "install.lock"
            with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError("其他服务进程正在安装 FFmpeg，请稍后重试") from exc
                # A stopped service may leave private staging; no other installer
                # owns it while this cross-process lock is held.
                for stale in root.glob(".install-*"):
                    if stale.is_dir() and not stale.is_symlink():
                        shutil.rmtree(stale)
                if managed_paths(self.config.database):
                    self._update("succeeded", "应用目录版本已安装。")
                    return
                destination = root / item[2]
                if destination.exists() or destination.is_symlink():
                    raise ValueError("目标安装目录已存在但验证未通过，请管理员检查；未覆盖文件")
                if shutil.disk_usage(root).free < item[1] + MAX_EXTRACTED:
                    raise ValueError("应用数据目录至少需要约 1.2 GiB 可用空间")
                with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as work:
                    staging = Path(work)
                    archive = staging / "bundle.tar.xz"
                    self._download(archive, item)
                    binaries = staging / "bundle"
                    binaries.mkdir(mode=0o700)
                    self._update("extracting", "校验通过，正在解包程序…")
                    self._extract(archive, binaries)
                    self._update("checking", "正在验证本机 FFmpeg / ffprobe 版本…")
                    self._check(binaries)
                    (binaries / "installed.json").write_text(json.dumps({"sha256": item[2], "release": RELEASE}))
                    (binaries / "SOURCE.txt").write_text(f"BtbN/FFmpeg-Builds\nGPL-3.0\nhttps://github.com/BtbN/FFmpeg-Builds/releases/tag/{RELEASE}\nSHA-256: {item[2]}\n")
                    binaries.rename(destination)
                self._update("succeeded", "FFmpeg / ffprobe 已安装并通过版本验证；现可选择 FFmpeg 本机。")
        except Exception as exc:
            # Never publish subprocess output (which may contain private data).
            message = str(exc) if isinstance(exc, ValueError) else "下载或安装失败，请检查网络、数据卷权限或系统兼容性后重试。"
            self._update("failed", message)
