"""Read-only, stable playback templates; copies remain normal user templates."""
import copy
import uuid
from .spec import TranscodeSpec

SCENARIOS = (
    ("web-1080", "网站标准 · 1080p", 1920, 1080, 22, 5000, "high", "4.0", 128, False),
    ("web-720", "网站轻量 · 720p", 1280, 720, 23, 2500, "main", "3.1", 128, False),
    ("food-detail", "食品 / 商品细节 · 1080p", 1920, 1080, 19, 7000, "high", "4.0", 160, False),
    ("mobile-720", "手机兼容 · 720p", 1280, 720, 23, 2200, "main", "3.1", 128, False),
    ("mobile-480", "手机省流量 · 480p", 854, 480, 25, 1000, "baseline", "3.0", 96, False),
    ("tablet", "平板 · 1080p", 1920, 1080, 21, 5000, "high", "4.0", 160, False),
    ("tv", "电视通用 · 1080p", 1920, 1080, 20, 8000, "high", "4.0", 192, False),
    ("hevc", "现代设备 HEVC · 1080p", 1920, 1080, 24, 4000, "auto", "auto", 128, True),
    ("background", "静音网页背景 · 720p", 1280, 720, 25, 1800, "main", "3.1", 0, False),
)


def builtin_templates():
    result = []
    for slug, name, width, height, quality, maxrate, profile, level, audio, hevc in SCENARIOS:
        raw = {
            "container": "mp4", "video": {"encoder": "x265" if hevc else "x264", "quality": quality,
                "preset": "medium", "profile": profile, "level": level, "framerate": "30", "peak_framerate": True},
            "dimensions": {"width": width, "height": height, "crop_mode": "none", "anamorphic": "none", "modulus": 2},
            "audio": {"tracks": [{"encoder": "aac", "mixdown": "stereo", "samplerate": "48000", "bitrate": str(audio)}] if audio else []},
            "subtitles": {"behavior": "none"}, "chapters": {"mode": "none"},
            "streaming": {"faststart": True, "pixel_format": "yuv420p", "only_downscale": True,
                "maxrate_kbps": maxrate, "buffer_kbps": maxrate * 2, "keyframe_interval": 60},
        }
        spec = TranscodeSpec.from_dict(raw).to_dict()
        note = "MP4 快速起播；保留比例、只缩小、不裁边，最高 30 fps。"
        note += "HEVC 需要播放器支持，不适合所有浏览器。" if hevc else "H.264 8-bit / AAC，面向常见网页和设备。" if audio else "无音轨，适合静音背景；不保证浏览器自动播放。"
        if slug == "food-detail":
            note += "较高质量保留食材纹理；不会自动增艳或锐化。"
        result.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "springhub:playback:v1:" + slug)),
            "builtin": True, "slug": slug, "name": name, "description": note, "version": 1,
            "engine": "handbrake", "supported_engines": ["handbrake", "ffmpeg", "rffmpeg"],
            "spec": spec, "form_spec": copy.deepcopy(spec), "baseline": None, "preset_id": "custom"})
    return result


def list_templates(store):
    return builtin_templates() + store.list_templates()


def find_template(store, identity):
    return next((t for t in list_templates(store) if t["id"] == identity), None)
