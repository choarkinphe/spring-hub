"""Engine selection and shared mapping/admission, preserving legacy defaults."""
from .engine import HandBrakeEngine
from .ffmpeg_engine import FFmpegEngine
from .ffmpeg_spec import build_ffmpeg_args
from .spec import build_engine_args, SpecError
from .admission import validate_encoders
from .remote_settings import REMOTE_FIELDS, remote_values
from dataclasses import replace
import threading
import os
import shutil
from .engine import EngineError

NAMES = ("handbrake", "ffmpeg", "rffmpeg")


def engine_name(value=None):
    value = "handbrake" if value is None else value
    if not isinstance(value, str) or value not in NAMES:
        raise SpecError("engine must be handbrake, ffmpeg or rffmpeg")
    return value


class Backends:
    def __init__(self, config, handbrake=None, runtime=None):
        self.config, self.runtime = config, runtime
        self._lock = threading.Lock()
        self.engines = {"handbrake": handbrake or HandBrakeEngine(config.engine),
            "ffmpeg": FFmpegEngine(config.engine),
            "rffmpeg": FFmpegEngine(config.engine, remote=True, roots=config.storage_roots)}

    def remote_settings(self):
        return remote_values(self.runtime.read()) if self.runtime else {key: getattr(self.config.engine, key) for key in REMOTE_FIELDS}

    def get(self, name=None, *, remote_config=None):
        name = engine_name(name)
        if name == "ffmpeg":
            from .ffmpeg_install import managed_paths
            values = {} if shutil.which(self.config.engine.ffmpeg_bin) and shutil.which(self.config.engine.ffprobe_bin) else managed_paths(self.config.database) or {}
            with self._lock:
                current = self.engines[name]
                desired = replace(self.config.engine, **values)
                if current.config.ffmpeg_bin != desired.ffmpeg_bin or current.config.ffprobe_bin != desired.ffprobe_bin:
                    current = FFmpegEngine(desired)
                    self.engines[name] = current
                return current
        if name != "rffmpeg":
            return self.engines[name]
        values = self.remote_settings() if remote_config is None else remote_config
        if not isinstance(values, dict) or set(values) != set(REMOTE_FIELDS) or any(not isinstance(v, str) for v in values.values()):
            raise SpecError("invalid rffmpeg configuration snapshot")
        with self._lock:
            current = self.engines[name]
            if any(getattr(current.config, key) != values[key] for key in REMOTE_FIELDS):
                current = FFmpegEngine(replace(self.config.engine, **values), remote=True, roots=self.config.storage_roots)
                self.engines[name] = current
            return current

    def availability(self):
        """Local prerequisites only: never execute a codec or remote wrapper."""
        result = []
        labels = {"handbrake": "HandBrake", "ffmpeg": "FFmpeg 本机", "rffmpeg": "rffmpeg 远程"}
        for name in NAMES:
            engine = self.get(name)
            status, reason = "available", "程序入口已安装；实际编码能力在选择后及提交时检查。"
            if name == "rffmpeg" and not all(getattr(engine.config, key) for key in REMOTE_FIELDS):
                status, reason = "unconfigured", "请先在系统设置 → FFmpeg 与远程填写并保存三个配置项。"
            elif not engine.binary_path():
                status, reason = "missing", "未找到可执行的 HandBrakeCLI。" if name == "handbrake" else "未找到可执行的 FFmpeg 兼容入口。"
            elif name != "handbrake" and not engine.ffprobe_path():
                status, reason = "missing", "缺少可执行的 ffprobe 兼容入口。"
            elif name == "rffmpeg":
                try:
                    directory = engine._probe_directory()
                    if not os.access(directory, os.W_OK | os.X_OK):
                        raise EngineError("not writable")
                except (EngineError, OSError):
                    status, reason = "unavailable", "共享探测目录不可用，请检查存储根、挂载标记和写入权限。"
            result.append({"id": name, "label": labels[name], "selectable": status == "available",
                           "status": status, "reason": reason})
        return {"engines": result, "scope": "local_prerequisites", "remote_contacted": False}

    def report(self, refresh=False):
        result = []
        for name in NAMES:
            engine = self.get(name)
            caps = engine.probe(refresh=refresh)
            result.append({"id": name, "label": {"handbrake": "HandBrake", "ffmpeg": "FFmpeg 本机", "rffmpeg": "rffmpeg 远程 wrapper"}[name],
                "capabilities": caps.as_dict(), "running_pause": name != "rffmpeg", "remote_exit_confirmed": name != "rffmpeg"})
        return {"engines": result, "default": "handbrake"}


def build_args(name, spec, *, overrides=None, preset=None):
    if engine_name(name) == "handbrake":
        return build_engine_args(spec, overrides=overrides, preset=preset)
    args = build_ffmpeg_args(spec)
    # Private execution marker: never a raw user argument, not sent to FFmpeg.
    return args + (["__two_pass__"] if spec.video.two_pass else [])


def admit(engine, name, spec, *, preset=None, overrides=None, refresh=False):
    if name == "handbrake":
        validate_encoders(spec, engine.probe(refresh=refresh), preset=preset, overrides=overrides)
    else:
        engine.admit(spec, refresh=refresh)
