"""Structured transcode specification and its translation to HandBrakeCLI args.

Design goals
------------
* The client **never** sends raw CLI flags. It sends a typed, structured spec.
* Only fields defined here are accepted; unknown fields are rejected, not
  silently dropped.
* Every value is validated against an explicit allow-list or numeric range, so
  a malicious payload cannot inject an arbitrary flag (no ``-o`` override, no
  ``--preset`` smuggling, no shell metacharacters — arguments are passed as an
  argv list, never through a shell).

The tabs mirror HandBrake's own UI: Summary, Dimensions, Filters, Video,
Audio, Subtitles, Chapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .engine import HARDWARE_ENCODER_PREFIXES


class SpecError(ValueError):
    """Raised when a spec contains an unknown field or an invalid value."""


# -- allow-lists -----------------------------------------------------------

#: Software video encoders HandBrake exposes (identifier -> human label).
VIDEO_ENCODERS: dict[str, str] = {
    "x264": "H.264 (x264)",
    "x265": "H.265 (x265)",
    "x264_10bit": "H.264 10-bit (x264)",
    "x265_10bit": "H.265 10-bit (x265)",
    "x265_12bit": "H.265 12-bit (x265)",
    "mpeg4": "MPEG-4",
    "mpeg2": "MPEG-2",
    "VP9": "VP9",
    "av1": "AV1 (SVT-AV1)",
    "theora": "Theora",
}

#: Hardware encoders, keyed by identifier. Presence here only means "the spec
#: layer will pass it through"; the engine must still report it as available
#: before a job is queued.
VIDEO_HW_ENCODERS: dict[str, str] = {
    "nvenc_h264": "H.264 (NVENC)",
    "nvenc_h265": "H.265 (NVENC)",
    "nvenc_h265_10bit": "H.265 10-bit (NVENC)",
    "qsv_h264": "H.264 (Intel QSV)",
    "qsv_h265": "H.265 (Intel QSV)",
    "qsv_h265_10bit": "H.265 10-bit (Intel QSV)",
    "vce_h264": "H.264 (AMD VCE)",
    "vce_h265": "H.265 (AMD VCE)",
    "vt_h264": "H.264 (VideoToolbox)",
    "vt_h265": "H.265 (VideoToolbox)",
}

VIDEO_ENCODERS_ALL = {**VIDEO_ENCODERS, **VIDEO_HW_ENCODERS}

VIDEO_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
}

VIDEO_TUNES = {"none", "film", "animation", "grain", "stillimage", "psnr", "ssim", "fastdecode", "zerolatency"}

VIDEO_PROFILES = {"auto", "baseline", "main", "high", "high10", "high422", "high444"}

#: x264/x265 level identifiers accepted by ``--level``.
VIDEO_LEVELS = {
    "auto", "1.0", "1.1", "1.2", "1.3", "2.0", "2.1", "2.2", "3.0", "3.1",
    "3.2", "4.0", "4.1", "4.2", "5.0", "5.1", "5.2", "6.0", "6.1", "6.2",
}

FRAMERATES = {"auto", "5", "10", "12", "15", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"}

VIDEO_QUALITY_TYPES = {"rf", "cqp", "vbr", "abr", "lossless", "constant", "crf"}

DEINTERLACE = {"off", "fast", "slow", "slower", "bob", "custom"}
DENOISE = {"off", "nlmeans", "hqdn3d", "custom"}
DETELECINE = {"off", "default", "custom"}
ROTATION = {"off", "90", "180", "270"}

#: Audio encoder tokens accepted for ``-E`` / ``--audio-fallback``, after the
#: alias resolution in :func:`cutecat.encoders.cli_encoder` (``aac`` -> ``av_aac``,
#: ``flac`` -> ``flac16``, ``lpcm`` -> ``pcm16``).
#:
#: Three values deliberately stay out, verified against HandBrakeCLI 1.11.0 with
#: a real audio track present:
#:
#: * ``auto``   — rejected: ``Invalid audio encoder (auto)``. It is a HandBrake
#:                *GUI* convenience, never a valid CLI token.
#: * ``dts``    — accepted but means "pass through DTS, else fall back"; it does
#:                not encode DTS and silently yielded AAC. Not an encoder.
#: * ``dtshd``  — rejected: ``Invalid audio encoder (dtshd)``; no build ships a
#:                DTS-HD encoder, only ``copy:dtshd``.
#:
#: They are still *explained* in the UI via ``encoders.AUDIO_CATALOG_ONLY`` so a
#: user can see why they are unavailable, without being submittable.
AUDIO_ENCODERS = {
    "none", "copy", "aac", "ac3", "eac3", "truehd",
    "flac", "mp3", "opus", "vorbis", "lpcm",
}

#: Codec *names* accepted by ``--audio-copy-mask``, taken from the engine's own
#: help text: ``aac/ac3/eac3/truehd/dts/dtshd/mp2/mp3/opus/vorbis/flac/alac/pcm``.
#: These are copy-mask names, not ``-E`` encoder tokens — ``av_aac`` is rejected
#: here even though it is the correct ``-E`` spelling.
AUDIO_COPY_MASK_CODECS = {
    "aac", "ac3", "eac3", "truehd", "dts", "dtshd", "mp2", "mp3", "opus",
    "vorbis", "flac", "alac", "pcm",
}

AUDIO_MIXDOWNS = {
    "auto", "mono", "stereo", "dpl1", "dpl2", "5point1", "6point1", "7point1",
}

AUDIO_SAMPLERATES = {"auto", "22050", "24000", "32000", "44100", "48000", "96000", "192000"}

AUDIO_BITRATES = {
    "auto", "32", "48", "64", "80", "96", "112", "128", "160", "192", "224",
    "256", "320", "384", "448", "512", "576", "640", "768", "960", "1536",
}

SUBTITLE_BEHAVIORS = {"none", "auto", "burn", "default", "foreign", "add", "add-first"}

CHROME_SMOOTH = {"auto", "on", "off"}
COLOR_MATRIX = {"auto", "bt709", "bt601", "bt2020", "fcc", "smpte240m", "custom"}
COLOR_RANGE = {"auto", "limited", "full"}
COLOR_PRIMARIES = {"auto", "bt709", "bt601", "bt2020", "smpte240m", "custom"}
COLOR_TRANSFER = {"auto", "bt709", "bt2020", "smpte2084", "bt2100", "custom"}

VIDEO_CONTAINERS = {"auto", "mp4", "mkv", "webm", "av_mp4", "av_mkv"}


def _require_choice(name: str, value: Any, allowed: set[str] | dict) -> str:
    if value is None:
        raise SpecError(f"{name} is required")
    text = str(value)
    if text not in allowed:
        raise SpecError(f"{name}={text!r} is not allowed")
    return text


def _optional_choice(name: str, value: Any, allowed: set[str] | dict) -> str | None:
    if value in (None, "", "auto") and name not in ("framerate",):
        if value == "auto":
            return "auto"
        return None
    return _require_choice(name, value, allowed)


def _bounded_float(name: str, value: Any, low: float, high: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{name} must be a number") from exc
    if not (low <= number <= high):
        raise SpecError(f"{name} must be between {low} and {high}")
    return number


def _bounded_int(name: str, value: Any, low: int, high: int) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{name} must be an integer") from exc
    if not (low <= number <= high):
        raise SpecError(f"{name} must be between {low} and {high}")
    return number


# -- spec sections ---------------------------------------------------------


@dataclass
class VideoSpec:
    encoder: str = "x264"
    quality_type: str = "rf"
    quality: float | None = 22.0
    bitrate_kbps: int | None = None
    two_pass: bool = False
    turbo: bool = False
    preset: str | None = "medium"
    tune: str | None = None
    profile: str | None = None
    level: str | None = None
    framerate: str = "auto"
    peak_framerate: bool = False
    vfr: bool = False
    cfr: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "VideoSpec":
        _reject_unknown("video", data, {
            "encoder", "quality_type", "quality", "bitrate_kbps", "two_pass",
            "turbo", "preset", "tune", "profile", "level", "framerate",
            "peak_framerate", "vfr", "cfr",
        })
        spec = cls()
        if "encoder" in data:
            spec.encoder = _require_choice("video.encoder", data["encoder"], VIDEO_ENCODERS_ALL)
        if "quality_type" in data:
            spec.quality_type = _require_choice("video.quality_type", data["quality_type"], VIDEO_QUALITY_TYPES)
        if "quality" in data:
            spec.quality = _bounded_float("video.quality", data["quality"], 0, 63)
        if "bitrate_kbps" in data:
            spec.bitrate_kbps = _bounded_int("video.bitrate_kbps", data["bitrate_kbps"], 1, 500_000)
        if "two_pass" in data:
            spec.two_pass = _as_bool(data["two_pass"])
        if "turbo" in data:
            spec.turbo = _as_bool(data["turbo"])
        if "preset" in data and data["preset"] not in (None, ""):
            spec.preset = _require_choice("video.preset", data["preset"], VIDEO_PRESETS)
        if "tune" in data and data["tune"] not in (None, "", "none"):
            spec.tune = _require_choice("video.tune", data["tune"], VIDEO_TUNES)
        if "profile" in data and data["profile"] not in (None, "", "auto"):
            spec.profile = _require_choice("video.profile", data["profile"], VIDEO_PROFILES)
        if "level" in data and data["level"] not in (None, "", "auto"):
            spec.level = _require_choice("video.level", data["level"], VIDEO_LEVELS)
        if "framerate" in data:
            spec.framerate = _require_choice("video.framerate", data["framerate"] or "auto", FRAMERATES)
        if "peak_framerate" in data:
            spec.peak_framerate = _as_bool(data["peak_framerate"])
        if "vfr" in data:
            spec.vfr = _as_bool(data["vfr"])
        if "cfr" in data:
            spec.cfr = _as_bool(data["cfr"])
        if spec.vfr and spec.cfr:
            raise SpecError("video.vfr and video.cfr are mutually exclusive")
        return spec


@dataclass
class AudioTrackSpec:
    track: int | str = 1
    encoder: str = "aac"
    mixdown: str | None = None
    samplerate: str | None = None
    bitrate: str | None = None
    gain: float | None = None
    drc: float | None = None
    name: str | None = None
    language: str | None = None
    default_track: bool = False
    #: Which source track to select: "1", "2", ..., or "auto".
    source: str = "auto"

    @classmethod
    def from_dict(cls, data: dict) -> "AudioTrackSpec":
        _reject_unknown("audio track", data, {
            "track", "encoder", "mixdown", "samplerate", "bitrate", "gain",
            "drc", "name", "language", "default_track", "source",
        })
        spec = cls()
        if "track" in data:
            spec.track = _bounded_int("audio.track", data["track"], 1, 64) or 1
        if "encoder" in data:
            spec.encoder = _require_choice("audio.encoder", data["encoder"], AUDIO_ENCODERS)
        if "mixdown" in data and data["mixdown"] not in (None, "", "auto"):
            spec.mixdown = _require_choice("audio.mixdown", data["mixdown"], AUDIO_MIXDOWNS)
        if "samplerate" in data and data["samplerate"] not in (None, "", "auto"):
            spec.samplerate = _require_choice("audio.samplerate", data["samplerate"], AUDIO_SAMPLERATES)
        if "bitrate" in data and data["bitrate"] not in (None, "", "auto"):
            spec.bitrate = _require_choice("audio.bitrate", data["bitrate"], AUDIO_BITRATES)
        if "gain" in data:
            spec.gain = _bounded_float("audio.gain", data["gain"], -20, 20)
        if "drc" in data:
            spec.drc = _bounded_float("audio.drc", data["drc"], 0, 4)
        if "name" in data and data["name"]:
            spec.name = _safe_label("audio.name", data["name"])
        if "language" in data and data["language"]:
            spec.language = _safe_label("audio.language", data["language"])
        if "default_track" in data:
            spec.default_track = _as_bool(data["default_track"])
        if "source" in data:
            source = str(data["source"])
            if source != "auto":
                source = str(_bounded_int("audio.source", source, 1, 64))
            spec.source = source
        return spec


@dataclass
class AudioSpec:
    tracks: list[AudioTrackSpec] = field(default_factory=list)
    fallback_encoder: str = "aac"
    copy_mask: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "AudioSpec":
        _reject_unknown("audio", data, {"tracks", "fallback_encoder", "copy_mask"})
        spec = cls()
        if "fallback_encoder" in data:
            spec.fallback_encoder = _require_choice("audio.fallback_encoder", data["fallback_encoder"], AUDIO_ENCODERS)
        if "copy_mask" in data and data["copy_mask"]:
            # ``--audio-copy-mask`` takes comma-separated codec *names*, not a
            # bitmask: ``aac,ac3`` succeeds while ``1,2,3`` fails with
            # "Invalid audio codec in autopassthru copy mask". The allowed set is
            # the one named in the engine's own help text.
            mask = str(data["copy_mask"])
            names = [part.strip() for part in mask.split(",")]
            if not names or any(not name for name in names):
                raise SpecError("audio.copy_mask must be a comma-separated codec list")
            unknown = [name for name in names if name not in AUDIO_COPY_MASK_CODECS]
            if unknown:
                raise SpecError(
                    f"audio.copy_mask codec(s) not allowed: {', '.join(sorted(unknown))}"
                )
            spec.copy_mask = ",".join(names)
        tracks = data.get("tracks") or []
        if not isinstance(tracks, list):
            raise SpecError("audio.tracks must be a list")
        spec.tracks = [AudioTrackSpec.from_dict(t) for t in tracks]
        return spec


@dataclass
class SubtitleSpec:
    behavior: str = "none"
    tracks: list[dict] = field(default_factory=list)
    burn_track: int | None = None
    srt_file: str | None = None
    srt_codeset: str | None = None
    default_track: int | None = None
    forced_only: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "SubtitleSpec":
        _reject_unknown("subtitles", data, {
            "behavior", "tracks", "burn_track", "srt_file", "srt_codeset",
            "default_track", "forced_only",
        })
        spec = cls()
        if "behavior" in data:
            spec.behavior = _require_choice("subtitles.behavior", data["behavior"], SUBTITLE_BEHAVIORS)
        if "burn_track" in data:
            spec.burn_track = _bounded_int("subtitles.burn_track", data["burn_track"], 1, 64)
        if "default_track" in data:
            spec.default_track = _bounded_int("subtitles.default_track", data["default_track"], 1, 64)
        if "forced_only" in data:
            spec.forced_only = _as_bool(data["forced_only"])
        if "srt_file" in data and data["srt_file"]:
            # A subtitle file must be a *relative* path inside a storage root;
            # the worker resolves it. Never an absolute host path.
            spec.srt_file = _safe_relative_file("subtitles.srt_file", data["srt_file"])
        if "srt_codeset" in data and data["srt_codeset"]:
            spec.srt_codeset = _safe_label("subtitles.srt_codeset", data["srt_codeset"])
        tracks = data.get("tracks") or []
        if not isinstance(tracks, list):
            raise SpecError("subtitles.tracks must be a list")
        for track in tracks:
            _reject_unknown("subtitle track", track, {"track", "burn", "default", "forced", "name"})
            entry: dict = {}
            if "track" in track:
                entry["track"] = _bounded_int("subtitles.track.track", track["track"], 1, 64)
            for flag in ("burn", "default", "forced"):
                if flag in track:
                    entry[flag] = _as_bool(track[flag])
            if "name" in track and track["name"]:
                entry["name"] = _safe_label("subtitles.track.name", track["name"])
            spec.tracks.append(entry)
        return spec


@dataclass
class ChapterSpec:
    mode: str = "auto"  # auto | none | markers
    markers: list[dict] = field(default_factory=list)
    marker_file: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ChapterSpec":
        _reject_unknown("chapters", data, {"mode", "markers", "marker_file"})
        spec = cls()
        if "mode" in data:
            spec.mode = _require_choice("chapters.mode", data["mode"], {"auto", "none", "markers"})
        if "marker_file" in data and data["marker_file"]:
            spec.marker_file = _safe_relative_file("chapters.marker_file", data["marker_file"])
        markers = data.get("markers") or []
        if not isinstance(markers, list):
            raise SpecError("chapters.markers must be a list")
        for marker in markers:
            _reject_unknown("chapter marker", marker, {"name", "start", "end"})
            if "start" not in marker:
                raise SpecError("chapter marker requires a start value")
            entry = {
                "start": _timecode("chapter.start", marker["start"]),
                "end": _timecode("chapter.end", marker.get("end")) if marker.get("end") else None,
                "name": _safe_label("chapter.name", marker.get("name") or "Chapter"),
            }
            spec.markers.append(entry)
        return spec


@dataclass
class DimensionSpec:
    width: int | None = None
    height: int | None = None
    crop_mode: str = "auto"  # auto | none | custom
    crop_top: int | None = None
    crop_bottom: int | None = None
    crop_left: int | None = None
    crop_right: int | None = None
    anamorphic: str = "auto"  # auto | none | strict | loose | custom
    modulus: int | None = None
    keep_aspect: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "DimensionSpec":
        _reject_unknown("dimensions", data, {
            "width", "height", "crop_mode", "crop_top", "crop_bottom",
            "crop_left", "crop_right", "anamorphic", "modulus", "keep_aspect",
        })
        spec = cls()
        spec.width = _bounded_int("dimensions.width", data.get("width"), 16, 8192)
        spec.height = _bounded_int("dimensions.height", data.get("height"), 16, 8192)
        if "crop_mode" in data:
            spec.crop_mode = _require_choice("dimensions.crop_mode", data["crop_mode"], {"auto", "none", "custom"})
        for side in ("top", "bottom", "left", "right"):
            key = f"crop_{side}"
            if key in data and data[key] not in (None, ""):
                setattr(spec, key, _bounded_int(f"dimensions.{key}", data[key], 0, 4096))
        if spec.crop_mode == "custom":
            if all(getattr(spec, f"crop_{s}") is None for s in ("top", "bottom", "left", "right")):
                raise SpecError("custom crop requires at least one crop value")
        if "anamorphic" in data:
            spec.anamorphic = _require_choice(
                "dimensions.anamorphic", data["anamorphic"],
                {"auto", "none", "strict", "loose", "custom"},
            )
        if "modulus" in data and data["modulus"] not in (None, ""):
            modulus = int(data["modulus"])
            if modulus not in (2, 4, 8, 16, 32):
                raise SpecError("dimensions.modulus must be 2, 4, 8, 16 or 32")
            spec.modulus = modulus
        if "keep_aspect" in data:
            spec.keep_aspect = _as_bool(data["keep_aspect"])
        return spec


@dataclass
class FilterSpec:
    deinterlace: str = "off"
    deinterlace_custom: str | None = None
    denoise: str = "off"
    denoise_custom: str | None = None
    detelecine: str = "off"
    detelecine_custom: str | None = None
    deblock: int | None = None
    rotate: str = "off"
    grayscale: bool = False
    hflip: bool = False
    #: ``--colorspace``-style overrides.
    color_matrix: str | None = None
    color_range: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    chroma_smooth: bool = False
    lapsharp: bool = False
    unsharp: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "FilterSpec":
        _reject_unknown("filters", data, {
            "deinterlace", "deinterlace_custom", "denoise", "denoise_custom",
            "detelecine", "detelecine_custom", "deblock", "rotate", "grayscale",
            "hflip", "color_matrix", "color_range", "color_primaries",
            "color_transfer", "chroma_smooth", "lapsharp", "unsharp",
        })
        spec = cls()
        if "deinterlace" in data:
            spec.deinterlace = _require_choice("filters.deinterlace", data["deinterlace"], DEINTERLACE)
        if "deinterlace_custom" in data and data["deinterlace_custom"]:
            spec.deinterlace_custom = _custom_filter("filters.deinterlace_custom", data["deinterlace_custom"])
        if "denoise" in data:
            spec.denoise = _require_choice("filters.denoise", data["denoise"], DENOISE)
        if "denoise_custom" in data and data["denoise_custom"]:
            spec.denoise_custom = _custom_filter("filters.denoise_custom", data["denoise_custom"])
        if "detelecine" in data:
            spec.detelecine = _require_choice("filters.detelecine", data["detelecine"], DETELECINE)
        if "detelecine_custom" in data and data["detelecine_custom"]:
            spec.detelecine_custom = _custom_filter("filters.detelecine_custom", data["detelecine_custom"])
        if "deblock" in data and data["deblock"] not in (None, ""):
            spec.deblock = _bounded_int("filters.deblock", data["deblock"], 0, 10)
        if "rotate" in data:
            spec.rotate = _require_choice("filters.rotate", data["rotate"], ROTATION)
        for flag in ("grayscale", "hflip", "chroma_smooth", "lapsharp", "unsharp"):
            if flag in data:
                setattr(spec, flag, _as_bool(data[flag]))
        for key, allowed in (
            ("color_matrix", COLOR_MATRIX),
            ("color_range", COLOR_RANGE),
            ("color_primaries", COLOR_PRIMARIES),
            ("color_transfer", COLOR_TRANSFER),
        ):
            if key in data and data[key] not in (None, "", "auto"):
                setattr(spec, key, _require_choice(f"filters.{key}", data[key], allowed))
        return spec


@dataclass
class TranscodeSpec:
    """The complete, validated request the engine will execute."""

    version: int = 1
    preset: str | None = None
    container: str = "auto"
    title: int = 1
    video: VideoSpec = field(default_factory=VideoSpec)
    audio: AudioSpec = field(default_factory=AudioSpec)
    subtitles: SubtitleSpec = field(default_factory=SubtitleSpec)
    chapters: ChapterSpec = field(default_factory=ChapterSpec)
    dimensions: DimensionSpec = field(default_factory=DimensionSpec)
    filters: FilterSpec = field(default_factory=FilterSpec)
    #: Optional metadata the client wants stamped on the output.
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "TranscodeSpec":
        if not isinstance(data, dict):
            raise SpecError("spec must be an object")
        _reject_unknown("spec", data, {
            "version", "preset", "container", "title", "video", "audio",
            "subtitles", "chapters", "dimensions", "filters", "metadata",
        })
        spec = cls()
        if "version" in data:
            spec.version = _bounded_int("version", data["version"], 1, 1) or 1
        if "preset" in data and data["preset"]:
            spec.preset = _safe_preset_name(data["preset"])
        if "container" in data:
            spec.container = _require_choice("container", data["container"], VIDEO_CONTAINERS)
        if "title" in data:
            spec.title = _bounded_int("title", data["title"], 1, 9999) or 1
        if "video" in data:
            spec.video = VideoSpec.from_dict(data["video"] or {})
        if "audio" in data:
            spec.audio = AudioSpec.from_dict(data["audio"] or {})
        if "subtitles" in data:
            spec.subtitles = SubtitleSpec.from_dict(data["subtitles"] or {})
        if "chapters" in data:
            spec.chapters = ChapterSpec.from_dict(data["chapters"] or {})
        if "dimensions" in data:
            spec.dimensions = DimensionSpec.from_dict(data["dimensions"] or {})
        if "filters" in data:
            spec.filters = FilterSpec.from_dict(data["filters"] or {})
        if "metadata" in data:
            spec.metadata = _safe_metadata(data["metadata"])
        return spec

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return {
            "version": self.version,
            "preset": self.preset,
            "container": self.container,
            "title": self.title,
            "video": asdict(self.video),
            "audio": asdict(self.audio),
            "subtitles": asdict(self.subtitles),
            "chapters": asdict(self.chapters),
            "dimensions": asdict(self.dimensions),
            "filters": asdict(self.filters),
            "metadata": self.metadata,
        }


# -- spec -> CLI arguments -------------------------------------------------


def build_engine_args(spec: TranscodeSpec) -> list[str]:
    """Translate a validated spec into HandBrakeCLI arguments.

    Only a fixed vocabulary of flags is emitted, each with a validated value.
    The caller (worker) prepends ``-i``/``-o``; those are never spec-controlled.
    """

    args: list[str] = []

    # Preset must come first so explicit flags can override it.
    if spec.preset:
        args.extend(["--preset", spec.preset])

    if spec.title:
        args.extend(["--title", str(spec.title)])

    if spec.container != "auto":
        args.extend(["--format", spec.container])

    # -- Video -------------------------------------------------------------
    video = spec.video
    args.extend(["-e", video.encoder])

    if video.quality_type == "lossless":
        args.append("--lossless")
    elif video.quality is not None:
        if video.quality_type in ("rf", "crf"):
            args.extend(["-q", _fmt(video.quality)])
        elif video.quality_type == "cqp":
            args.extend(["--cqp", _fmt(video.quality)])
        elif video.quality_type in ("vbr", "abr"):
            if video.bitrate_kbps:
                args.extend(["-b", str(video.bitrate_kbps)])
        else:
            args.extend(["-q", _fmt(video.quality)])

    if video.bitrate_kbps and video.quality_type in ("vbr", "abr"):
        args.extend(["-b", str(video.bitrate_kbps)])
    if video.two_pass:
        args.append("--two-pass")
    if video.turbo:
        args.append("--turbo")
    if video.preset:
        args.extend(["--encoder-preset", video.preset])
    if video.tune:
        args.extend(["--encoder-tune", video.tune])
    if video.profile:
        args.extend(["--encoder-profile", video.profile])
    if video.level:
        args.extend(["--encoder-level", video.level])
    if video.framerate and video.framerate != "auto":
        args.extend(["-r", video.framerate])
    if video.peak_framerate:
        args.append("--pfr")
    if video.vfr:
        args.append("--vfr")
    if video.cfr:
        args.append("--cfr")

    # -- Dimensions --------------------------------------------------------
    dim = spec.dimensions
    if dim.width:
        args.extend(["-w", str(dim.width)])
    if dim.height:
        args.extend(["-l", str(dim.height)])
    if dim.crop_mode == "none":
        args.extend(["--crop-mode", "none"])
    elif dim.crop_mode == "custom":
        args.extend([
            "--crop",
            "{top}:{bottom}:{left}:{right}".format(
                top=dim.crop_top or 0,
                bottom=dim.crop_bottom or 0,
                left=dim.crop_left or 0,
                right=dim.crop_right or 0,
            ),
        ])
    if dim.anamorphic != "auto":
        args.extend(["--anamorphic", dim.anamorphic])
    if dim.modulus:
        args.extend(["--modulus", str(dim.modulus)])

    # -- Filters -----------------------------------------------------------
    filters = spec.filters
    if filters.deinterlace != "off":
        if filters.deinterlace == "custom" and filters.deinterlace_custom:
            args.extend(["--deinterlace", filters.deinterlace_custom])
        else:
            args.extend(["--deinterlace", filters.deinterlace])
    if filters.denoise != "off":
        if filters.denoise == "custom" and filters.denoise_custom:
            args.extend(["--denoise", filters.denoise_custom])
        else:
            args.extend(["--denoise", filters.denoise])
    if filters.detelecine != "off":
        if filters.detelecine == "custom" and filters.detelecine_custom:
            args.extend(["--detelecine", filters.detelecine_custom])
        else:
            args.extend(["--detelecine", filters.detelecine])
    if filters.deblock is not None:
        args.extend(["--deblock", str(filters.deblock)])
    if filters.rotate != "off":
        args.extend(["--rotate", filters.rotate])
    if filters.grayscale:
        args.append("--grayscale")
    if filters.hflip:
        args.append("--hflip")
    if filters.chroma_smooth:
        args.append("--chroma-smooth")
    if filters.lapsharp:
        args.append("--lapsharp")
    if filters.unsharp:
        args.append("--unsharp")
    for key, flag in (
        ("color_matrix", "--color-matrix"),
        ("color_range", "--color-range"),
        ("color_primaries", "--color-primaries"),
        ("color_transfer", "--color-transfer"),
    ):
        value = getattr(filters, key)
        if value:
            args.extend([flag, value])

    # -- Audio -------------------------------------------------------------
    audio = spec.audio
    if audio.copy_mask:
        args.extend(["--audio-copy-mask", audio.copy_mask])
    if audio.fallback_encoder:
        args.extend(["--audio-fallback", audio.fallback_encoder])
    if audio.tracks:
        args.extend(["--audio", ",".join(str(t.track) for t in audio.tracks)])
        encoders = ",".join(t.encoder for t in audio.tracks)
        args.extend(["-E", encoders])
        mixdowns = ",".join((t.mixdown or "auto") for t in audio.tracks)
        args.extend(["-6", mixdowns])
        bitrates = ",".join((t.bitrate or "auto") for t in audio.tracks)
        args.extend(["-B", bitrates])
        samplerates = ",".join((t.samplerate or "auto") for t in audio.tracks)
        args.extend(["--ar", samplerates])
        gains = ",".join(_fmt(t.gain) if t.gain is not None else "0" for t in audio.tracks)
        if any(t.gain is not None for t in audio.tracks):
            args.extend(["--gain", gains])
        drcs = ",".join(_fmt(t.drc) if t.drc is not None else "0" for t in audio.tracks)
        if any(t.drc is not None for t in audio.tracks):
            args.extend(["--drc", drcs])
        names = ",".join(t.name or "" for t in audio.tracks)
        if any(t.name for t in audio.tracks):
            args.extend(["--audio-track-names", names])
        if any(t.default_track for t in audio.tracks):
            default_index = next(
                (str(i + 1) for i, t in enumerate(audio.tracks) if t.default_track), "1"
            )
            args.extend(["--audio-default", default_index])

    # -- Subtitles ---------------------------------------------------------
    subs = spec.subtitles
    if subs.behavior != "none":
        args.extend(["--subtitle", subs.behavior])
    if subs.tracks:
        args.extend(["--subtitle-tracks", ",".join(str(t["track"]) for t in subs.tracks if t.get("track"))])
    if subs.burn_track:
        args.extend(["--subtitle-burn", str(subs.burn_track)])
    if subs.default_track:
        args.extend(["--subtitle-default", str(subs.default_track)])
    if subs.forced_only:
        args.append("--subtitle-forced")
    if subs.srt_file:
        args.extend(["--srt-file", subs.srt_file])
    if subs.srt_codeset:
        args.extend(["--srt-codeset", subs.srt_codeset])

    # -- Chapters ----------------------------------------------------------
    chapters = spec.chapters
    if chapters.mode == "none":
        args.append("--no-chapters")
    elif chapters.mode == "markers" and chapters.marker_file:
        args.extend(["--chapters-file", chapters.marker_file])

    return args


# -- helpers ---------------------------------------------------------------


def _fmt(value: float) -> str:
    """Format a float without a trailing ``.0`` for integer-valued numbers."""

    if float(value).is_integer():
        return str(int(value))
    return ("%g" % value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _reject_unknown(section: str, data: dict, allowed: set[str]) -> None:
    if not isinstance(data, dict):
        raise SpecError(f"{section} must be an object")
    unknown = set(data) - allowed
    if unknown:
        raise SpecError(f"unknown {section} field(s): {', '.join(sorted(unknown))}")


_SAFE_LABEL_RE = __import__("re").compile(r"^[\w .:+/\-]{1,120}$", __import__("re").UNICODE)


def _safe_label(name: str, value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise SpecError(f"{name} cannot be empty")
    if len(text) > 120:
        raise SpecError(f"{name} is too long")
    if any(ch in text for ch in ("\x00", "\n", "\r")):
        raise SpecError(f"{name} contains control characters")
    return text


def _safe_relative_file(name: str, value: Any) -> str:
    text = _safe_label(name, value)
    if text.startswith("/") or ".." in text.replace("\\", "/").split("/"):
        raise SpecError(f"{name} must be a relative path without '..'")
    if "\\" in text:
        text = text.replace("\\", "/")
    return text


def _safe_preset_name(value: Any) -> str:
    text = str(value).strip()
    if not text or len(text) > 200:
        raise SpecError("preset name is invalid")
    if any(ch in text for ch in ("\x00", "\n", "\r")):
        raise SpecError("preset name contains control characters")
    return text


def _custom_filter(name: str, value: Any) -> str:
    """Validate a HandBrake custom filter string (e.g. ``yadif=1:-1:0``)."""

    text = str(value).strip()
    if len(text) > 200:
        raise SpecError(f"{name} is too long")
    if not __import__("re").fullmatch(r"[A-Za-z0-9_.,:=+\- ]+", text):
        raise SpecError(f"{name} contains unsupported characters")
    return text


def _timecode(name: str, value: Any) -> str:
    text = str(value).strip()
    if not __import__("re").fullmatch(r"\d{1,2}:\d{2}:\d{2}(\.\d{1,3})?", text):
        raise SpecError(f"{name} must be HH:MM:SS[.mmm]")
    return text


def _safe_metadata(data: Any) -> dict:
    if not isinstance(data, dict):
        raise SpecError("metadata must be an object")
    allowed = {"title", "artist", "album", "comment", "genre", "year", "description"}
    out: dict = {}
    for key, value in data.items():
        if key not in allowed:
            raise SpecError(f"metadata field {key!r} is not allowed")
        out[key] = _safe_label(f"metadata.{key}", value)
    return out
