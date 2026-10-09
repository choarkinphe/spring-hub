"""Resolve HEVC source-preserving policy from video-stream metadata, never total bitrate."""
import copy
import hashlib
import json
from fractions import Fraction
from .spec import SpecError, TranscodeSpec, FilterSpec


def validate_policy(spec):
    v, d, s = spec.video, spec.dimensions, spec.streaming
    if spec.preset or spec.title != 1 or spec.container != "mp4" or v.encoder != "x265" or v.quality_type != "source":
        raise SpecError("保持源参数要求自定义 HEVC MP4 与源平均码率模式")
    if v.bitrate_kbps or v.two_pass or v.turbo or v.framerate != "auto" or v.cfr or v.vfr or v.peak_framerate or v.tune or v.profile or v.level:
        raise SpecError("保持源参数不能同时指定固定码率、帧率、档次或多遍参数")
    if d.width or d.height or d.crop_mode != "none" or d.anamorphic != "auto" or d.modulus:
        raise SpecError("保持源参数不能缩放、裁边或更改像素宽高比")
    if spec.filters != FilterSpec() or s.pixel_format or s.maxrate_kbps or s.buffer_kbps or s.keyframe_interval or s.only_downscale:
        raise SpecError("保持源参数不能同时指定滤镜、像素格式或播放限制")
    subs = spec.subtitles
    if spec.audio.tracks or spec.audio.copy_mask or spec.audio.fallback_encoder != "aac" or subs.behavior != "none" or subs.tracks or subs.srt_file or subs.srt_burn or subs.srt_default or subs.burn_track or subs.default_track or subs.forced_only or spec.chapters.mode != "auto" or spec.chapters.marker_file or spec.chapters.markers:
        raise SpecError("保持源参数自动复制全部兼容音轨与字幕、保留章节，不接受单独音轨/字幕设置")


def resolve_source(spec, scan):
    validate_policy(spec)
    try:
        streams = scan["titles"][0]["streams"]
        if not isinstance(streams, list) or len(streams) > 128 or any(not isinstance(s, dict) for s in streams):
            raise ValueError()
        videos = [s for s in streams if s.get("codec_type") == "video"]
        if len(videos) != 1 or videos[0].get("disposition", {}).get("attached_pic"):
            raise SpecError("保持源参数仅支持一个普通视频流，不支持多视频或封面流")
        video = videos[0]
        width, height = int(video["width"]), int(video["height"])
        if not 16 <= width <= 8192 or not 16 <= height <= 8192:
            raise SpecError("源分辨率超出支持范围（16–8192）")
        pixel = video.get("pix_fmt")
        if pixel not in ("yuv420p", "yuv420p10le"):
            raise SpecError("保持源参数仅支持 8/10-bit 4:2:0；不能静默更改源像素格式")
        if video.get("color_transfer") in ("smpte2084", "arib-std-b67") or video.get("color_primaries") == "bt2020" or video.get("side_data_list"):
            raise SpecError("源视频包含 HDR、旋转或其他附属信息，当前模板不能可靠保留；请使用专用转换设置")
        bitrate = video.get("bit_rate")
        if bitrate in (None, "N/A", "0", 0):
            tags = video.get("tags", {})
            bitrate = tags.get("BPS") or tags.get("BPS-eng")
        if isinstance(bitrate, bool) or not str(bitrate).isdigit() or not 1000 <= int(bitrate) <= 500000000:
            raise SpecError("无法读取可靠的源视频平均码率；不能使用容器总码率，请改用手动码率设置")
        rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0")
        if not 0 < Fraction(rate) <= 1000:
            raise SpecError("源帧率无效，无法保持源时间戳")
        sar = str(video.get("sample_aspect_ratio") or "1:1")
        if sar not in ("N/A", "0:1") and not 0 < Fraction(sar.replace(":", "/")) <= 100:
            raise SpecError("源像素宽高比无效")
        audio, subtitles = [], []
        for stream in streams:
            kind, codec = stream.get("codec_type"), stream.get("codec_name")
            if kind == "audio":
                if codec not in {"aac", "mp3", "ac3", "eac3", "alac"}:
                    raise SpecError("源音轨不能直接复制到 MP4：" + str(codec) + "；请改用音频转码设置")
                audio.append(stream)
            elif kind == "subtitle":
                if codec != "mov_text":
                    raise SpecError("源字幕不能直接复制到 MP4：" + str(codec) + "；模板不会丢弃或转换字幕")
                subtitles.append(stream)
            elif kind not in ("video", "audio", "subtitle"):
                raise SpecError("源文件包含无法保留的附属流，请使用自定义转换设置")
        raw = copy.deepcopy(spec.to_dict())
        raw.pop("source_preserve")
        raw["video"].update(encoder="x265_10bit" if pixel == "yuv420p10le" else "x265", quality_type="abr", quality=None,
                            bitrate_kbps=max(1, (int(bitrate) + 500) // 1000))
        raw["streaming"]["pixel_format"] = pixel
        raw["audio"]["tracks"] = [{"encoder": "copy", "source": str(i + 1)} for i in range(len(audio))]
        resolved = TranscodeSpec.from_dict(raw)
        from .ffmpeg_spec import build_ffmpeg_args
        args = build_ffmpeg_args(resolved)
        # Fixed switches only: source timestamps, metadata and supported streams
        # are retained without applying user-provided FFmpeg expressions.
        args[args.index("-map_metadata") + 1] = "0"
        args.remove("-sn")
        if subtitles:
            args += ["-map", "0:s", "-c:s", "copy"]
        else:
            args += ["-sn"]
        args += ["-fps_mode", "passthrough"]
        colors = {"color_primaries": {"bt709", "smpte170m", "bt470bg"},
                  "color_transfer": {"bt709", "smpte170m", "gamma22", "gamma28"},
                  "color_space": {"bt709", "smpte170m", "bt470bg"}, "color_range": {"tv", "pc"}}
        for key, allowed in colors.items():
            value = video.get(key)
            if value in allowed:
                flag = {"color_space": "-colorspace", "color_transfer": "-color_trc"}.get(key, "-" + key)
                args += [flag, value]
            elif value not in (None, "unknown", "unspecified"):
                raise SpecError("源色彩标记不在可保留范围：" + key)
        fingerprint = hashlib.sha256(json.dumps(streams, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return resolved, args, {"fingerprint": fingerprint, "width": width, "height": height, "pixel_format": pixel,
                                "framerate": rate, "sample_aspect_ratio": sar, "video_bitrate_bps": int(bitrate),
                                "target_bitrate_kbps": resolved.video.bitrate_kbps, "audio_tracks": len(audio), "subtitle_tracks": len(subtitles)}
    except SpecError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError) as exc:
        raise SpecError("源视频元信息缺失或无效，不能可靠保持源参数") from exc
