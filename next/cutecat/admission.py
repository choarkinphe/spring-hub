"""Fail-closed encoder admission shared by submission and execution."""

from .encoders import ALIASES, cli_encoder
from .engine import EngineCapabilities


class AdmissionError(ValueError):
    def __init__(self, message: str, *, status: int = 409):
        super().__init__(message)
        self.status = status


def validate_encoders(spec, caps: EngineCapabilities, *, preset: dict | None = None,
                      overrides: dict | None = None) -> None:
    if not caps.available:
        raise AdmissionError("HandBrakeCLI is unavailable", status=503)
    entry = preset["PresetList"][0] if preset else {}
    video_override = not preset or "encoder" in ((overrides or {}).get("video") or {})
    video = spec.video.encoder if video_override else entry.get("VideoEncoder")
    candidates = (video, *ALIASES["video"].get(video, ()))
    verdicts = caps.video_encoder_probe or {}
    verdict = next((verdicts[name] for name in candidates if name in verdicts), None)
    if verdict == "absent":
        raise AdmissionError(f"video encoder {video!r} is absent from this engine build")
    if verdict == "blocked":
        raise AdmissionError(f"video encoder {video!r} cannot encode on this host; check hardware/drivers")
    if verdict != "works":
        raise AdmissionError(f"video encoder {video!r} readiness is unknown; refresh detection", status=503)

    audio_overrides = (overrides or {}).get("audio") or {}
    tracks = spec.audio.tracks if not preset or "tracks" in audio_overrides else None
    audio = [track.encoder for track in tracks] if tracks is not None else [
        track.get("AudioEncoder") for track in entry.get("AudioList", [])
    ]
    fallback = (spec.audio.fallback_encoder if not preset or "fallback_encoder" in audio_overrides
                else entry.get("AudioEncoderFallback"))
    if fallback:
        audio.append(fallback)
    for encoder in audio:
        if encoder in ("none", "copy"):
            continue
        if not caps.audio_encoders_known:
            raise AdmissionError("audio encoder detection is unknown; refresh detection", status=503)
        if not encoder or not cli_encoder("audio", encoder, caps.audio_encoders):
            raise AdmissionError(f"audio encoder {encoder!r} is absent from this engine build")
