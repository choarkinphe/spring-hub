"""Optional FFmpeg decoder inventory, never used to admit HandBrake jobs."""
import re
import shutil
import subprocess
import threading
import time


_LOCK = threading.Lock()
_CACHE = None
_AT = 0


def parse_decoders(text):
    items = []
    for line in text.splitlines():
        match = re.match(r"\s*([VAS][A-Z.]{5})\s+([A-Za-z0-9_]+)\s+(.+)$", line)
        if match and not match[3].startswith("="):
            items.append({"name": match[2], "kind": {"V": "video", "A": "audio", "S": "subtitle"}[match[1][0]],
                          "description": match[3].strip()})
    return items


def decoder_inventory(refresh=False):
    global _CACHE, _AT
    with _LOCK:
        now = time.monotonic()
        if not refresh and _CACHE is not None and now - _AT < 300:
            return _CACHE
        binary = shutil.which("ffmpeg")
        result = {"provider": "FFmpeg", "binary": binary, "status": "missing" if not binary else "unknown", "items": [],
                  "note": "这是独立 FFmpeg 构建的解码器清单，不代表 HandBrake 内置解码器；不参与任务准入。"}
        if binary:
            try:
                proc = subprocess.run([binary, "-hide_banner", "-decoders"], capture_output=True,
                                      text=True, timeout=3, check=False)
                if proc.returncode == 0 and len(proc.stdout) <= 1_000_000:
                    items = parse_decoders(proc.stdout)
                    if items:
                        result.update(status="reported", items=items)
            except (OSError, subprocess.SubprocessError):
                pass
        _CACHE, _AT = result, now
        return result
