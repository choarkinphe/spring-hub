"""Engine selection and shared mapping/admission, preserving legacy defaults."""
from .engine import HandBrakeEngine
from .ffmpeg_engine import FFmpegEngine
from .ffmpeg_spec import build_ffmpeg_args
from .spec import build_engine_args, SpecError
from .admission import validate_encoders

NAMES = ("handbrake", "ffmpeg", "rffmpeg")


def engine_name(value=None):
    value = "handbrake" if value is None else value
    if not isinstance(value, str) or value not in NAMES:
        raise SpecError("engine must be handbrake, ffmpeg or rffmpeg")
    return value


class Backends:
    def __init__(self, config, handbrake=None):
        self.engines = {"handbrake": handbrake or HandBrakeEngine(config.engine),
            "ffmpeg": FFmpegEngine(config.engine),
            "rffmpeg": FFmpegEngine(config.engine, remote=True, roots=config.storage_roots)}

    def get(self, name=None):
        return self.engines[engine_name(name)]

    def report(self, refresh=False):
        result = []
        for name, engine in self.engines.items():
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
