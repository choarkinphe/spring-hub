"""Trusted-admin rffmpeg configuration and explicit, bounded readiness checks."""
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from .pathsafe import normalise_relative
from .ffmpeg_engine import FFmpegEngine
from .engine import EngineError

REMOTE_FIELDS = ("rffmpeg_bin", "rffprobe_bin", "remote_probe_dir")


def remote_values(values):
    return {key: values[key] for key in REMOTE_FIELDS}


def validate_remote(values, roots):
    for key in REMOTE_FIELDS:
        value = values[key]
        if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"{key} 必须是有效路径字符串，不能包含控制字符")
        if value != value.strip():
            raise ValueError(f"{key} 不能包含首尾空格")
    if not any(values[key] for key in REMOTE_FIELDS):
        return
    if not all(values[key] for key in REMOTE_FIELDS):
        raise ValueError("rffmpeg 配置需填写两个程序入口和共享探测目录，或同时清空三项以停用")
    for key in REMOTE_FIELDS[:2]:
        value = values[key]
        if value.startswith("/"):
            normalise_relative(value[1:])
        elif not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
            raise ValueError(f"{key} 仅支持绝对 Linux 路径或单个 PATH 程序名，不接受命令参数")
    path = Path(values["remote_probe_dir"])
    if not path.is_absolute() or ".." in path.parts or "\\" in str(path):
        raise ValueError("共享探测目录必须是绝对 Linux 路径，不能包含 .. 或反斜杠")
    # Configuration saving checks containment only, not mount availability, and
    # never creates directories or invokes a wrapper. Execution rechecks mounts.
    for root in roots:
        if root.read_only:
            continue
        base = Path(root.path)
        try:
            path.relative_to(base)
            path.resolve().relative_to(base.resolve())
            return
        except ValueError:
            pass
    raise ValueError("共享探测目录必须位于已配置的可写存储根内，不能通过软链接逃逸")


class RemoteCheck:
    def __init__(self):
        self._lock = threading.Lock()

    def run(self, config, roots):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("rffmpeg 配置检查正在进行，请稍后重试")
        try:
            return self._check(config, roots)
        finally:
            self._lock.release()

    @staticmethod
    def _check(config, roots):
        fields = {key: getattr(config, key) for key in REMOTE_FIELDS}
        if not any(fields.values()):
            return {"status": "disabled", "checks": [], "message": "rffmpeg 已停用；填写并保存三个配置项后再检查。"}
        checks = []
        try:
            validate_remote(fields, roots)
        except ValueError as exc:
            return {"status": "failed", "checks": [], "message": str(exc)}
        engine = FFmpegEngine(config, remote=True, roots=roots)
        try:
            directory = engine._probe_directory()
            writable = os.access(directory, os.W_OK | os.X_OK)
            checks.append({"id": "directory", "ok": writable, "message": "共享目录存在、挂载有效且权限可写（未执行写入测试）。" if writable else "共享目录缺少写入或访问权限。"})
        except EngineError:
            checks.append({"id": "directory", "ok": False, "message": "共享目录不可用，请检查目录存在性、可写存储根和挂载标记。"})
        for key, expected in (("rffmpeg_bin", "ffmpeg"), ("rffprobe_bin", "ffprobe")):
            binary = shutil.which(fields[key])
            if not binary:
                checks.append({"id": key, "ok": False, "message": f"未找到可执行的 {expected} 兼容入口；程序必须在服务/容器内可用。"})
                continue
            # Do not contact remote hosts if local shared-path prerequisites fail.
            if not checks[0]["ok"]:
                checks.append({"id": key, "ok": False, "message": f"{expected} 入口存在；共享目录检查失败，未执行版本检查。"})
                continue
            try:
                output = engine._run([binary, "-version"], timeout=5)
                first = output.splitlines()[0] if output else ""
                matched = re.match(rf"^{expected} version ([A-Za-z0-9_.+~:-]{{1,100}})(?:\s|$)", first)
                checks.append({"id": key, "ok": bool(matched), "message": f"{expected} 兼容版本响应正常：{matched[1]}。" if matched else f"入口未返回可识别的 {expected} 版本信息。"})
            except EngineError:
                checks.append({"id": key, "ok": False, "message": f"{expected} 版本调用失败或超时；请检查管理员侧 wrapper/SSH 日志。"})
                if key == "rffmpeg_bin":
                    checks.append({"id": "rffprobe_bin", "ok": False, "message": "为避免重复联系可能未结束的远程调用，已跳过 ffprobe 版本检查。"})
                    break
        return {"status": "passed" if all(c["ok"] for c in checks) else "failed", "checks": checks,
            "message": "检查仅验证本机目录与兼容版本入口；不证明远端身份、共享路径一致或实际编码可用。未创建任务或编码产物。"}
