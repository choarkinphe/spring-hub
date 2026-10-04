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

VIDEO_QUALITY_TYPES = {"rf", "vbr", "abr", "lossless", "constant", "crf"}

DEINTERLACE = {"off", "fast", "slow", "slower", "default", "skip-spatial", "bob", "custom"}
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
        if "preset" in data:
            spec.preset = _require_choice("video.preset", data["preset"], VIDEO_PRESETS) if data["preset"] not in (None, "") else None
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
        if sum((spec.vfr, spec.cfr, spec.peak_framerate)) > 1:
            raise SpecError("video.vfr, video.cfr and video.peak_framerate are mutually exclusive")
        if spec.quality_type in ("abr", "vbr") and not spec.bitrate_kbps:
            raise SpecError("bitrate mode requires video.bitrate_kbps")
        if spec.two_pass and spec.quality_type not in ("abr", "vbr"):
            raise SpecError("two-pass requires bitrate mode")
        if spec.turbo and not spec.two_pass:
            raise SpecError("turbo requires two-pass")
        if spec.quality_type == "lossless" and spec.encoder not in ("x264", "x264_10bit", "x265", "x265_10bit", "x265_12bit"):
            raise SpecError("lossless is implemented only for x264/x265; use the encoder's quality mode otherwise")
        if spec.quality_type in ("rf", "crf", "constant") and spec.quality is None:
            raise SpecError("quality mode requires video.quality")
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
        if spec.language or spec.default_track:
            raise SpecError("audio language/default overrides are not implemented")
        if spec.name and "," in spec.name:
            raise SpecError("audio.name cannot contain commas (CLI track-list separator)")
        if spec.encoder in ("copy", "none") and any(value not in (None, "auto", 0) for value in
                (spec.mixdown, spec.samplerate, spec.bitrate, spec.gain, spec.drc)):
            raise SpecError("audio copy/none cannot apply encoding parameters")
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
    srt_language: str = "und"
    srt_offset_ms: int = 0
    srt_burn: bool = False
    srt_default: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "SubtitleSpec":
        _reject_unknown("subtitles", data, {
            "behavior", "tracks", "burn_track", "srt_file", "srt_codeset",
            "default_track", "forced_only", "srt_language", "srt_offset_ms", "srt_burn", "srt_default",
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
            if "," in spec.srt_file:
                raise SpecError("subtitles.srt_file must name a single file without commas")
        if "srt_codeset" in data and data["srt_codeset"]:
            spec.srt_codeset = _safe_label("subtitles.srt_codeset", data["srt_codeset"])
            if not __import__("re").fullmatch(r"[A-Za-z0-9_.-]+", spec.srt_codeset):
                raise SpecError("subtitles.srt_codeset must name a single character encoding")
        if "srt_language" in data:
            spec.srt_language = str(data["srt_language"])
            if not __import__("re").fullmatch(r"[a-z]{3}", spec.srt_language):
                raise SpecError("subtitles.srt_language must be an ISO 639-2 code")
        if "srt_offset_ms" in data:
            spec.srt_offset_ms = _bounded_int("subtitles.srt_offset_ms", data["srt_offset_ms"], -86400000, 86400000) or 0
        for key in ("srt_burn", "srt_default"):
            if key in data:
                setattr(spec, key, _as_bool(data[key]))
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
            if not entry.get("track"):
                raise SpecError("subtitle track requires a track number")
            if any(entry.get(key) for key in ("burn", "default", "forced", "name")):
                raise SpecError("per-track subtitle overrides are not implemented; use top-level subtitle options")
            spec.tracks.append(entry)
        if spec.behavior == "add":
            raise SpecError("subtitles.behavior=add is not implemented; specify tracks")
        count = len(spec.tracks) or (1 if spec.behavior in ("foreign", "burn", "default", "add-first", "auto") else 0)
        for key in ("burn_track", "default_track"):
            value = getattr(spec, key)
            if value and value > count:
                raise SpecError(f"subtitles.{key} indexes the selected source subtitle list, not the source track number")
        if spec.forced_only and not count:
            raise SpecError("forced_only requires a selected source subtitle")
        if not spec.srt_file and any((spec.srt_codeset, spec.srt_language != "und", spec.srt_offset_ms, spec.srt_burn, spec.srt_default)):
            raise SpecError("SRT options require subtitles.srt_file")
        if spec.srt_burn and (spec.burn_track or spec.behavior == "burn"):
            raise SpecError("only one source or SRT subtitle may be burned")
        if spec.srt_default and (spec.default_track or spec.behavior == "default"):
            raise SpecError("only one source or SRT subtitle may be default")
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
        if spec.markers:
            raise SpecError("inline chapter markers are not implemented; use marker_file")
        if spec.marker_file and spec.mode != "markers":
            raise SpecError("marker_file requires chapters.mode=markers")
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
            modulus = _bounded_int("dimensions.modulus", data["modulus"], 2, 32)
            if modulus not in (2, 4, 8, 16, 32):
                raise SpecError("dimensions.modulus must be 2, 4, 8, 16 or 32")
            spec.modulus = modulus
        if "keep_aspect" in data:
            spec.keep_aspect = _as_bool(data["keep_aspect"])
        if not spec.keep_aspect:
            raise SpecError("dimensions.keep_aspect=false is not implemented")
        if spec.anamorphic in ("strict", "custom"):
            raise SpecError("strict anamorphic is not supported by this CLI; custom requires a pixel-aspect/display-width model")
        if spec.crop_mode != "custom" and any(getattr(spec, "crop_" + side) is not None for side in ("top", "bottom", "left", "right")):
            raise SpecError("crop values require dimensions.crop_mode=custom")
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
        custom_keys = {
            "deinterlace": {"mode", "parity"},
            "denoise": {"y-spatial", "cb-spatial", "cr-spatial", "y-temporal", "cb-temporal", "cr-temporal"},
            "detelecine": {"skip-left", "skip-right", "skip-top", "skip-bottom", "strict-breaks", "plane", "parity", "disable"},
        }
        for key, allowed in custom_keys.items():
            custom = getattr(spec, key + "_custom")
            if getattr(spec, key) == "custom":
                if not custom:
                    raise SpecError(f"filters.{key}=custom requires {key}_custom")
                pairs = custom.split(":")
                seen = set()
                for pair in pairs:
                    parts = pair.split("=")
                    if len(parts) != 2 or not parts[1] or parts[0] not in allowed or parts[0] in seen:
                        raise SpecError(f"filters.{key}_custom requires supported key=value pairs")
                    seen.add(parts[0])
                    limits = {"mode": (0, 7), "parity": (0, 1), "disable": (0, 1), "plane": (0, 2), "strict-breaks": (-1, 1)}
                    low, high = limits.get(parts[0], (0, 100))
                    _bounded_float(f"filters.{key}_custom.{parts[0]}", parts[1], low, high)
                    if key != "denoise" and not __import__("re").fullmatch(r"-?\d+", parts[1]):
                        raise SpecError(f"filters.{key}_custom.{parts[0]} must be an integer")
            elif custom:
                raise SpecError(f"{key}_custom requires filters.{key}=custom")
        if data.get("color_range") == "auto":
            spec.color_range = "auto"
        if spec.lapsharp and spec.unsharp:
            raise SpecError("lapsharp and unsharp are alternative sharpening filters")
        for key, allowed in (("color_matrix", {"bt709", "bt601", "bt2020"}),
                             ("color_primaries", {"bt709", "bt2020", "smpte240m"}),
                             ("color_transfer", {"bt709", "smpte2084"})):
            if getattr(spec, key) and getattr(spec, key) not in allowed:
                raise SpecError(f"filters.{key} has no unambiguous supported CLI mapping")
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
            if spec.metadata:
                raise SpecError("metadata overrides are not implemented")
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


def prepare_job_preset(document: dict | None, overrides: dict) -> dict | None:
    """Reset values without a CLI reset flag in an isolated task snapshot."""
    if not document:
        return None
    from copy import deepcopy

    result = deepcopy(document)
    entry = result["PresetList"][0]
    video = overrides.get("video") or {}
    if video.get("framerate") == "auto":
        entry["VideoFramerate"] = "auto"
    if any(key in video and not _as_bool(video[key]) for key in ("vfr", "cfr", "peak_framerate")):
        entry["VideoFramerateMode"] = "vfr"
    subtitles = overrides.get("subtitles") or {}
    if "forced_only" in subtitles and not _as_bool(subtitles["forced_only"]):
        entry["SubtitleAddForeignAudioSearch"] = False
    if "copy_mask" in (overrides.get("audio") or {}) and not overrides["audio"]["copy_mask"]:
        entry["AudioCopyMask"] = []
    return result


def build_engine_args(spec: TranscodeSpec, *, overrides: dict | None = None, preset: dict | None = None) -> list[str]:
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
        effective_encoder = video.encoder
        if preset and "encoder" not in ((overrides or {}).get("video") or {}):
            effective_encoder = preset["PresetList"][0].get("VideoEncoder", "")
        if not effective_encoder.startswith(("x264", "x265")):
            raise SpecError("lossless is implemented only for x264/x265")
        args.extend(["-q", "0"])
        if effective_encoder.startswith("x265"):
            args.extend(["--encopts", "lossless=1"])
    elif video.quality is not None and video.quality_type not in ("vbr", "abr"):
        args.extend(["-q", _fmt(video.quality)])

    if video.bitrate_kbps and video.quality_type in ("vbr", "abr"):
        args.extend(["-b", str(video.bitrate_kbps)])
    if video.two_pass:
        args.append("--multi-pass")
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
    args.extend(["--crop-mode", dim.crop_mode])
    if dim.crop_mode == "custom":
        args.extend([
            "--crop",
            "{top}:{bottom}:{left}:{right}".format(
                top=dim.crop_top or 0,
                bottom=dim.crop_bottom or 0,
                left=dim.crop_left or 0,
                right=dim.crop_right or 0,
            ),
        ])
    args.append({"auto": "--auto-anamorphic", "none": "--non-anamorphic", "loose": "--loose-anamorphic"}[dim.anamorphic])
    if dim.modulus:
        args.extend(["--modulus", str(dim.modulus)])

    # -- Filters -----------------------------------------------------------
    filters = spec.filters
    if filters.deinterlace != "off":
        args.extend(["--no-comb-detect", "--no-decomb", "--no-bwdif"])
        value = {"fast": "skip-spatial", "slow": "mode=3", "slower": "mode=3", "default": "mode=3"}.get(filters.deinterlace, filters.deinterlace)
        args.append("--deinterlace=" + (filters.deinterlace_custom if filters.deinterlace == "custom" else value))
    if filters.denoise != "off":
        flag = "--nlmeans" if filters.denoise == "nlmeans" else "--hqdn3d"
        args.append("--no-hqdn3d" if flag == "--nlmeans" else "--no-nlmeans")
        args.append(flag + "=" + (filters.denoise_custom if filters.denoise == "custom" else "medium"))
    if filters.detelecine != "off":
        args.append("--detelecine=" + (filters.detelecine_custom if filters.detelecine == "custom" else "default"))
    if filters.deblock is not None:
        args.append("--deblock=strength=strong:thresh=" + str(filters.deblock))
    rotate_fields = (overrides or {}).get("filters") or {}
    if overrides is None or "rotate" in rotate_fields or "hflip" in rotate_fields:
        angle = filters.rotate if filters.rotate != "off" else "0"
        hflip = int(filters.hflip)
        if preset:
            inherited = preset["PresetList"][0].get("PictureRotate", "angle=0:hflip=0")
            settings = dict(part.split("=", 1) for part in str(inherited).split(":") if "=" in part)
            if "rotate" not in rotate_fields:
                angle = settings.get("angle", "0")
            if "hflip" not in rotate_fields:
                hflip = int(settings.get("hflip", "0"))
        args.append(f"--rotate=angle={angle}:hflip={hflip}")
    if filters.grayscale:
        args.append("--grayscale")
    for key in ("chroma_smooth", "lapsharp", "unsharp"):
        if getattr(filters, key):
            args.append("--" + key.replace("_", "-") + "=medium")
    if filters.color_matrix:
        args.extend(["--color-matrix", {"bt709": "709", "bt601": "601", "bt2020": "2020"}[filters.color_matrix]])
    if filters.color_range:
        args.extend(["--color-range", filters.color_range])
    colors = [f"{key}={getattr(filters, 'color_' + key)}" for key in ("primaries", "transfer") if getattr(filters, "color_" + key)]
    if colors:
        args.extend(["--colorspace", ":".join(colors)])

    # -- Audio -------------------------------------------------------------
    audio = spec.audio
    if audio.copy_mask:
        args.extend(["--audio-copy-mask", audio.copy_mask])
    if audio.fallback_encoder:
        args.extend(["--audio-fallback", audio.fallback_encoder])
    if not audio.tracks:
        args.extend(["--audio", "none"])
    if audio.tracks:
        args.extend(["--audio", ",".join(t.source if t.source != "auto" else str(t.track) for t in audio.tracks)])
        encoders = ",".join(t.encoder for t in audio.tracks)
        args.extend(["-E", encoders])
        mixdowns = ",".join((t.mixdown or "auto") for t in audio.tracks)
        args.extend(["-6", mixdowns])
        bitrates = ",".join((t.bitrate or "auto") for t in audio.tracks)
        args.extend(["-B", bitrates])
        samplerates = ",".join((t.samplerate or "auto") for t in audio.tracks)
        args.extend(["--arate", ",".join(_fmt(float(rate) / 1000) if rate != "auto" else rate for rate in samplerates.split(","))])
        gains = ",".join(_fmt(t.gain) if t.gain is not None else "0" for t in audio.tracks)
        if any(t.gain is not None for t in audio.tracks):
            args.extend(["--gain", gains])
        drcs = ",".join(_fmt(t.drc) if t.drc is not None else "0" for t in audio.tracks)
        if any(t.drc is not None for t in audio.tracks):
            args.extend(["--drc", drcs])
        names = ",".join(t.name or "" for t in audio.tracks)
        if any(t.name for t in audio.tracks):
            args.extend(["--aname", names])
        if any(t.default_track for t in audio.tracks):
            default_index = next(
                (str(i + 1) for i, t in enumerate(audio.tracks) if t.default_track), "1"
            )
            args.extend(["--audio-default", default_index])

    # -- Subtitles ---------------------------------------------------------
    subs = spec.subtitles
    if subs.tracks:
        args.extend(["--subtitle", ",".join(str(t["track"]) for t in subs.tracks)])
    elif subs.behavior == "foreign":
        args.extend(["--subtitle", "scan"])
    elif subs.behavior in ("burn", "default", "add-first", "auto"):
        args.extend(["--subtitle", "1"])
    elif subs.behavior == "none":
        args.extend(["--subtitle", "none"])
    if subs.burn_track:
        args.append("--subtitle-burned=" + str(subs.burn_track))
    elif subs.behavior == "burn":
        args.append("--subtitle-burned=1")
    else:
        args.append("--subtitle-burned=none")
    if subs.default_track:
        args.append("--subtitle-default=" + str(subs.default_track))
    elif subs.behavior == "default":
        args.append("--subtitle-default=1")
    else:
        args.append("--subtitle-default=none")
    args.append("--subtitle-forced=1" if subs.forced_only else "--subtitle-forced=none")
    if subs.srt_file:
        args.extend(["--srt-file", subs.srt_file, "--srt-codeset", subs.srt_codeset or "UTF-8",
                     "--srt-lang", subs.srt_language, "--srt-offset", str(subs.srt_offset_ms)])
        if subs.srt_burn:
            args.append("--srt-burn=1")
        if subs.srt_default:
            args.append("--srt-default=1")

    # -- Chapters ----------------------------------------------------------
    chapters = spec.chapters
    if chapters.mode == "none":
        args.append("--no-markers")
    elif chapters.mode == "markers":
        args.append("--markers=" + chapters.marker_file if chapters.marker_file else "--markers")
    elif chapters.mode == "auto":
        args.append("--markers")

    if overrides is not None:
        # A preset is the base. Dataclass defaults must not become implicit
        # overrides of fields the caller never supplied.
        fields = {
            "--format": ("container",),
            "-e": ("video", "encoder"), "-q": ("video", "quality", "quality_type"),
            "-b": ("video", "bitrate_kbps", "quality_type"),
            "--encopts": ("video", "quality_type"),
            "--multi-pass": ("video", "two_pass"), "--turbo": ("video", "turbo"),
            "--encoder-preset": ("video", "preset"), "--encoder-tune": ("video", "tune"),
            "--encoder-profile": ("video", "profile"), "--encoder-level": ("video", "level"),
            "-r": ("video", "framerate"), "--pfr": ("video", "peak_framerate"),
            "--vfr": ("video", "vfr"), "--cfr": ("video", "cfr"),
            "-w": ("dimensions", "width"), "-l": ("dimensions", "height"),
            "--crop": ("dimensions", "crop_mode"), "--crop-mode": ("dimensions", "crop_mode"),
            "--auto-anamorphic": ("dimensions", "anamorphic"), "--non-anamorphic": ("dimensions", "anamorphic"),
            "--loose-anamorphic": ("dimensions", "anamorphic"), "--modulus": ("dimensions", "modulus"),
            "--audio": ("audio", "tracks"), "-E": ("audio", "tracks"),
            "-6": ("audio", "tracks"), "-B": ("audio", "tracks"), "--arate": ("audio", "tracks"),
            "--gain": ("audio", "tracks"), "--drc": ("audio", "tracks"),
            "--aname": ("audio", "tracks"),
            "--audio-fallback": ("audio", "fallback_encoder"), "--audio-copy-mask": ("audio", "copy_mask"),
            "--subtitle": ("subtitles", "behavior", "tracks"),
            "--subtitle-burned": ("subtitles", "behavior", "burn_track", "srt_burn"),
            "--subtitle-default": ("subtitles", "behavior", "default_track", "srt_default"),
            "--subtitle-forced": ("subtitles", "forced_only"),
            "--srt-file": ("subtitles", "srt_file"), "--srt-codeset": ("subtitles", "srt_file", "srt_codeset"),
            "--srt-lang": ("subtitles", "srt_file", "srt_language"), "--srt-offset": ("subtitles", "srt_file", "srt_offset_ms"),
            "--srt-burn": ("subtitles", "srt_burn"), "--srt-default": ("subtitles", "srt_default"),
            "--markers": ("chapters", "mode", "marker_file"), "--no-markers": ("chapters", "mode"),
        }
        for key in ("deinterlace", "denoise", "detelecine", "deblock", "rotate", "grayscale", "hflip",
                    "chroma_smooth", "lapsharp", "unsharp", "color_matrix", "color_range", "color_primaries", "color_transfer"):
            fields["--" + key.replace("_", "-")] = ("filters", key, key + "_custom")
        fields.update({
            "--rotate": ("filters", "rotate", "hflip"),
            "--deinterlace": ("filters", "deinterlace", "deinterlace_custom"),
            "--no-comb-detect": ("filters", "deinterlace"), "--no-decomb": ("filters", "deinterlace"),
            "--no-bwdif": ("filters", "deinterlace"),
            "--hqdn3d": ("filters", "denoise", "denoise_custom"), "--nlmeans": ("filters", "denoise"),
            "--no-hqdn3d": ("filters", "denoise"), "--no-nlmeans": ("filters", "denoise"),
            "--colorspace": ("filters", "color_primaries", "color_transfer"),
        })
        filtered = []
        index = 0
        switches = {"--multi-pass", "--turbo", "--pfr", "--vfr", "--cfr", "--grayscale",
                    "--auto-anamorphic", "--non-anamorphic", "--loose-anamorphic",
                    "--no-comb-detect", "--no-decomb", "--no-bwdif", "--no-hqdn3d", "--no-nlmeans",
                    "--markers", "--no-markers"}
        while index < len(args):
            flag = args[index]
            end = index + 1
            if "=" not in flag and flag not in switches and end < len(args):
                end += 1
            mapping = fields.get(flag.split("=", 1)[0])
            include = mapping is None or (mapping[0] in overrides if len(mapping) == 1 else
                        any(key in (overrides.get(mapping[0]) or {}) for key in mapping[1:]))
            if include:
                filtered.extend(args[index:end])
            index = end
        args = filtered
        video_fields = overrides.get("video") or {}
        for key, flag in (("two_pass", "--no-multi-pass"), ("turbo", "--no-turbo")):
            if key in video_fields and not getattr(video, key):
                args.append(flag)
        filter_fields = overrides.get("filters") or {}
        disabling = {
            "deinterlace": ["--no-comb-detect", "--no-deinterlace", "--no-decomb", "--no-bwdif"],
            "denoise": ["--no-hqdn3d", "--no-nlmeans"], "detelecine": ["--no-detelecine"],
            "deblock": ["--no-deblock"], "grayscale": ["--no-grayscale"],
            "chroma_smooth": ["--no-chroma-smooth"], "lapsharp": ["--no-lapsharp"],
            "unsharp": ["--no-unsharp"],
        }
        for key, flags in disabling.items():
            value = getattr(filters, key)
            if key in filter_fields and (value == "off" or value is False or value is None):
                args.extend(flags)
        if "tune" in video_fields and not video.tune:
            args.extend(["--encoder-tune", ""])
        for key in ("profile", "level"):
            if key in video_fields and not getattr(video, key):
                args.extend(["--encoder-" + key, "auto"])
        # Source-rate reset is made in the private snapshot: CLI -r 0 is invalid.
        # No -r override is needed once VideoFramerate is reset to 'auto'.
        if "preset" in video_fields and video_fields["preset"] in (None, ""):
            raise SpecError("explicit encoder preset reset requires a named preset")
        for key in ("width", "height", "modulus"):
            if key in (overrides.get("dimensions") or {}) and getattr(dim, key) is None:
                raise SpecError(f"explicit dimensions.{key} reset requires a numeric value")
        if any(key in filter_fields and filter_fields[key] in (None, "", "auto") for key in
               ("color_matrix", "color_primaries", "color_transfer")):
            raise SpecError("explicit color signalling/conversion reset is not supported")
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
    from .pathsafe import PathSafetyError, normalise_relative

    text = _safe_label(name, value)
    try:
        relative = normalise_relative(text)
    except PathSafetyError as exc:
        raise SpecError(f"{name}: {exc}") from exc
    if not relative:
        raise SpecError(f"{name} requires a filename")
    return relative


def _safe_preset_name(value: Any) -> str:
    text = str(value).strip()
    if not text or len(text) > 200:
        raise SpecError("preset name is invalid")
    if any(ch in text for ch in ("\x00", "\n", "\r")):
        raise SpecError("preset name contains control characters")
    return text


def _custom_filter(name: str, value: Any) -> str:
    """Validate the syntax of a HandBrake key=value filter string."""

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
