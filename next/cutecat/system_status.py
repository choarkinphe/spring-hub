"""Read-only, best-effort metrics for the service's visible Linux environment."""
from __future__ import annotations

import csv
import io
import math
import os
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _number(value, scale=1):
    try:
        n = float(value) * scale
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None


def parse_cpu_stat(text):
    line = next(line for line in text.splitlines() if line.startswith("cpu "))
    values = [int(n) for n in line.split()[1:9]]  # guest is already counted in user/nice
    if len(values) < 4 or any(n < 0 for n in values):
        raise ValueError("invalid CPU counters")
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def parse_memory(text):
    values = {}
    for line in text.splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2:
            values[parts[0]] = _number(parts[1], 1024)
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None or available > total:
        raise ValueError("MemTotal/MemAvailable unavailable")
    return {"total_bytes": int(total), "available_bytes": int(available),
            "used_bytes": int(total - available), "used_percent": (total - available) / total * 100}


def parse_nvidia(text):
    devices = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 5 or not row[0].strip():
            continue
        total, used, utilization = _number(row[2], 1024 * 1024), _number(row[3], 1024 * 1024), _number(row[4])
        devices.append({"name": row[0].strip(), "driver": row[1].strip(),
                        "memory_total_bytes": total, "memory_used_bytes": used,
                        "utilization_percent": utilization if utilization is not None and utilization <= 100 else None,
                        "source": "nvidia-smi"})
    if not devices:
        raise ValueError("no usable NVIDIA rows")
    return devices


def _run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=2, check=False)
    if result.returncode:
        raise ValueError("device query failed")
    if len(result.stdout) > 256_000:
        raise ValueError("device response too large")
    return result.stdout


class SystemStatus:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0
        self._cpu_previous = None
        self._gpu_cached = None
        self._gpu_at = 0

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached_at < 2:
                return self._cached
            release = platform.release()
            scope = "WSL2 服务环境" if "microsoft" in release.lower() else "Linux 服务可见环境"
            result = {"sampled_at": datetime.now(timezone.utc).isoformat(), "scope": scope,
                      "platform": platform.system(), "note": "不是 Windows 宿主机完整监控；容器内指标按各数据源口径显示。"}
            for key, collector in (("cpu", self._cpu), ("memory", self._memory),
                                   ("gpu", self._gpu), ("disks", self._disks)):
                try:
                    result[key] = collector()
                except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as exc:
                    result[key] = {"status": "unknown", "note": f"无法读取：{type(exc).__name__}"}
            self._cached, self._cached_at = result, now
            return result

    def _cpu(self):
        current = parse_cpu_stat(_text("/proc/stat"))
        previous, self._cpu_previous = self._cpu_previous, current
        usage = None
        if previous:
            total, idle = current[0] - previous[0], current[1] - previous[1]
            if total > 0 and 0 <= idle <= total:
                usage = (total - idle) / total * 100
        model = next((line.split(":", 1)[1].strip() for line in _text("/proc/cpuinfo").splitlines()
                      if line.startswith("model name") or line.startswith("Hardware")), platform.machine())
        try:
            affinity = len(os.sched_getaffinity(0))
        except AttributeError:
            affinity = None
        quota = None
        try:
            limit, period = _text("/sys/fs/cgroup/cpu.max").split()
            if limit != "max" and int(period) > 0:
                quota = int(limit) / int(period)
        except (OSError, ValueError):
            pass
        return {"status": "ok", "model": model, "logical_cpus": os.cpu_count(), "affinity_cpus": affinity,
                "quota_cpus": quota, "utilization_percent": usage, "source": "/proc/stat, /proc/cpuinfo",
                "note": "CPU 使用率为服务环境可见 CPU 的采样间隔平均值；首次等待第二次采样，不是任务 CPU 占用。"}

    def _memory(self):
        result = {"status": "ok", "source": "/proc/meminfo", **parse_memory(_text("/proc/meminfo"))}
        try:
            limit = _text("/sys/fs/cgroup/memory.max").strip()
            if limit != "max":
                result["container_limit_bytes"] = int(limit)
                result["container_used_bytes"] = int(_text("/sys/fs/cgroup/memory.current"))
        except (OSError, ValueError):
            pass
        return result

    def _gpu(self):
        now = time.monotonic()
        if self._gpu_cached is not None and now - self._gpu_at < 20:
            return self._gpu_cached
        query = shutil.which("nvidia-smi")
        if not query and Path("/usr/lib/wsl/lib/nvidia-smi").is_file():
            query = "/usr/lib/wsl/lib/nvidia-smi"
        note = ""
        devices = []
        if query:
            try:
                devices = parse_nvidia(_run([query, "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                                             "--format=csv,noheader,nounits"]))
            except (OSError, ValueError, subprocess.SubprocessError):
                note = "NVIDIA 查询失败或超时；尝试可见设备目录。"
        if not devices:
            devices = self._sysfs_gpus()
        bridge = Path("/dev/dxg").exists()
        result = {"status": "ok" if devices else "unknown", "devices": devices,
                  "wsl_bridge": bridge, "note": note + (" WSL GPU 桥接可见。" if bridge else "") +
                  " 未报告的利用率/显存显示未知；检测到 GPU 不代表可硬件编码。", "sampled_at": datetime.now(timezone.utc).isoformat()}
        self._gpu_cached, self._gpu_at = result, now
        return result

    @staticmethod
    def _sysfs_gpus():
        devices = []
        paths = set(Path("/sys/bus/pci/devices").glob("*"))
        paths.update((card / "device").resolve() for card in Path("/sys/class/drm").glob("card[0-9]*") if (card / "device").exists())
        for path in sorted(paths):
            try:
                if not _text(path / "class").strip().startswith("0x03"):
                    continue
                vendor, device = _text(path / "vendor").strip(), _text(path / "device").strip()
                driver = (path / "driver").resolve().name if (path / "driver").exists() else None
                utilization = None
                total = used = None
                try:
                    utilization = _number(_text(path / "gpu_busy_percent").strip())
                    total = _number(_text(path / "mem_info_vram_total").strip())
                    used = _number(_text(path / "mem_info_vram_used").strip())
                except OSError:
                    pass
                devices.append({"name": f"PCI GPU {vendor}:{device}", "driver": driver,
                                "utilization_percent": utilization if utilization is not None and utilization <= 100 else None,
                                "memory_total_bytes": total, "memory_used_bytes": used, "source": "sysfs PCI"})
            except OSError:
                continue
        return devices

    def _disks(self):
        paths = [("系统盘", "/", None), ("服务数据", str(Path(self.config.database).parent), None)]
        paths.extend((root.label, root.path, root) for root in self.config.storage_roots)
        rows = []
        for label, path, root in paths:
            row = {"label": label, "path": path, "status": "unknown"}
            try:
                if root and (not Path(path).is_dir() or (root.mount_marker and not root.marker_present())):
                    row["note"] = "存储根不可用或挂载标记缺失；不读取底层本地盘容量。"
                else:
                    usage = shutil.disk_usage(path)
                    stat = os.stat(path)
                    row.update(status="ok", total_bytes=usage.total, used_bytes=usage.used,
                               available_bytes=usage.free, filesystem_id=str(stat.st_dev),
                               note="同一文件系统的多个路径共享容量，不应相加；可用容量为当前用户口径。")
            except OSError:
                row["note"] = "目录不存在、无权限或文件系统读取失败。"
            rows.append(row)
        return {"status": "ok", "source": "statvfs", "items": rows}
