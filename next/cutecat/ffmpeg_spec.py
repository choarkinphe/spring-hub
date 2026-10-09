"""Explicit FFmpeg mapping of the shared structured vocabulary, never raw argv."""
from .spec import SpecError

VIDEO = {"x264": "libx264", "x264_10bit": "libx264", "x265": "libx265", "x265_10bit": "libx265",
         "x265_12bit": "libx265", "VP9": "libvpx-vp9", "av1": "libsvtav1", "mpeg4": "mpeg4",
         "mpeg2": "mpeg2video", "theora": "libtheora", "nvenc_h264": "h264_nvenc",
         "nvenc_h265": "hevc_nvenc", "nvenc_h265_10bit": "hevc_nvenc", "qsv_h264": "h264_qsv",
         "qsv_h265": "hevc_qsv", "qsv_h265_10bit": "hevc_qsv", "vce_h264": "h264_amf",
         "vce_h265": "hevc_amf", "vt_h264": "h264_videotoolbox", "vt_h265": "hevc_videotoolbox"}
AUDIO = {"aac": "aac", "ac3": "ac3", "eac3": "eac3", "truehd": "truehd", "flac": "flac",
         "mp3": "libmp3lame", "opus": "libopus", "vorbis": "libvorbis", "lpcm": "pcm_s16le", "copy": "copy"}
SOFTWARE = {"x264", "x264_10bit", "x265", "x265_10bit", "x265_12bit"}


def build_ffmpeg_args(spec):
    v, d, f, s, a, stream = spec.video, spec.dimensions, spec.filters, spec.subtitles, spec.audio, spec.streaming
    if spec.source_preserve:
        from .source_preserve import validate_policy
        validate_policy(spec)
        # No fictitious bitrate/CRF: concrete argv requires source metadata.
        return []
    if spec.preset or spec.title != 1:
        raise SpecError("FFmpeg does not accept HandBrake presets or disc titles")
    if d.crop_mode == "auto" or d.anamorphic == "loose":
        raise SpecError("FFmpeg requires crop_mode=none/custom and anamorphic=none/auto; automatic crop is HandBrake-only")
    if any((f.deinterlace_custom, f.denoise_custom, f.detelecine_custom, f.deblock is not None,
            f.chroma_smooth, f.lapsharp, f.color_matrix, f.color_primaries, f.color_transfer)):
        raise SpecError("selected advanced HandBrake filter has no supported FFmpeg mapping")
    if a.copy_mask or a.fallback_encoder != "aac":
        raise SpecError("FFmpeg audio copy-mask/fallback is not supported; select explicit tracks")
    if spec.chapters.mode == "markers":
        raise SpecError("FFmpeg CSV chapter import is not supported")
    if s.srt_file or s.srt_burn or s.burn_track or s.forced_only or s.behavior in ("foreign", "burn"):
        raise SpecError("FFmpeg supports source soft subtitles only; SRT/burn/foreign selection requires HandBrake")
    if v.two_pass and v.encoder not in ("x264", "x264_10bit", "VP9", "mpeg4", "mpeg2", "theora"):
        raise SpecError("FFmpeg two-pass is supported for x264/VP9/MPEG/Theora only")
    if v.turbo:
        raise SpecError("HandBrake turbo has no FFmpeg equivalent; turn turbo off")
    container = {"av_mp4": "mp4", "av_mkv": "mkv"}.get(spec.container, spec.container)
    if stream.faststart and container != "mp4":
        raise SpecError("faststart requires an explicit MP4 container")
    if container == "webm" and (v.encoder not in ("VP9", "av1") or any(t.encoder not in ("none", "opus", "vorbis") for t in a.tracks)):
        raise SpecError("WebM requires VP9/AV1 and Opus/Vorbis")
    if container == "mp4" and v.encoder in ("theora",):
        raise SpecError("selected video encoder cannot be muxed into MP4")
    args = ["-map", "0:v:0", "-c:v", VIDEO[v.encoder]]
    software = v.encoder in SOFTWARE
    if v.quality_type == "lossless":
        args += ["-crf", "0"]
        if v.encoder.startswith("x265"):
            args += ["-x265-params", "lossless=1"]
    elif v.quality_type in ("abr", "vbr"):
        args += ["-b:v", str(v.bitrate_kbps) + "k"]
    elif software or v.encoder in ("VP9", "av1"):
        args += ["-crf", str(v.quality)]
        if v.encoder == "VP9":
            args += ["-b:v", "0"]
    else:
        raise SpecError("this FFmpeg encoder requires bitrate mode, not HandBrake RF")
    if software:
        if v.preset:
            args += ["-preset", v.preset]
        if v.tune:
            args += ["-tune", v.tune]
        if v.profile:
            args += ["-profile:v", v.profile]
        if v.level:
            args += ["-level:v", v.level]
    elif v.tune or v.profile or v.level or v.preset not in (None, "medium"):
        raise SpecError("selected FFmpeg encoder does not accept x264 preset/tune/profile/level controls")
    depth = "yuv420p12le" if "12bit" in v.encoder else "yuv420p10le" if "10bit" in v.encoder else "yuv420p"
    if stream.pixel_format and stream.pixel_format != depth:
        raise SpecError("pixel format must match selected encoder bit depth")
    args += ["-pix_fmt", stream.pixel_format or depth]
    if stream.maxrate_kbps:
        args += ["-maxrate", str(stream.maxrate_kbps) + "k", "-bufsize", str(stream.buffer_kbps) + "k"]
    if stream.keyframe_interval:
        args += ["-g", str(stream.keyframe_interval)]
        if software:
            params = f"keyint={stream.keyframe_interval}:min-keyint={stream.keyframe_interval}:scenecut=0"
            flag = "-x265-params" if v.encoder.startswith("x265") else "-x264-params"
            if flag in args:
                args[args.index(flag) + 1] += ":" + params
            else:
                args += [flag, params]
    if v.encoder.startswith("x265") and container == "mp4":
        args += ["-tag:v", "hvc1"]
    filters = []
    if d.crop_mode == "custom":
        top, bottom, left, right = (getattr(d, "crop_" + k) or 0 for k in ("top", "bottom", "left", "right"))
        filters += [f"crop=iw-{left + right}:ih-{top + bottom}:{left}:{top}"]
    if d.anamorphic == "none":
        # Preserve display aspect when converting non-square source pixels.
        filters += ["scale=w='max(2,trunc(iw*sar/2)*2)':h=ih", "setsar=1"]
    if f.deinterlace != "off":
        mode = "send_field" if f.deinterlace == "bob" else "send_frame"
        filters += [f"yadif=mode={mode}:parity=auto:deint=all"]
    if f.denoise != "off":
        filters += ["nlmeans" if f.denoise == "nlmeans" else "hqdn3d"]
    if f.detelecine != "off":
        filters += ["fieldmatch", "decimate"]
    if f.rotate == "90":
        filters += ["transpose=1"]
    elif f.rotate == "270":
        filters += ["transpose=2"]
    elif f.rotate == "180":
        filters += ["hflip", "vflip"]
    if f.hflip:
        filters += ["hflip"]
    if f.grayscale:
        filters += ["hue=s=0"]
    if f.unsharp:
        filters += ["unsharp"]
    if d.width or d.height:
        width, height = str(d.width or -2), str(d.height or -2)
        if stream.only_downscale:
            width = f"min(iw,{d.width})" if d.width else "iw"
            height = f"min(ih,{d.height})" if d.height else "ih"
        filters += [f"scale=w='{width}':h='{height}':force_original_aspect_ratio=decrease:force_divisible_by={d.modulus or 2}"]
    if d.anamorphic == "none":
        filters += ["setsar=1"]
    if v.framerate != "auto":
        if v.peak_framerate:
            filters += [f"fps=fps='min(source_fps\\,{v.framerate})'"]
        else:
            args += ["-r", v.framerate]
    if filters:
        args += ["-vf", ",".join(filters)]
    if v.cfr:
        args += ["-fps_mode", "cfr"]
    elif v.vfr or v.peak_framerate:
        args += ["-fps_mode", "vfr"]
    if f.color_range:
        args += ["-color_range", {"limited": "tv", "full": "pc", "auto": "unknown"}[f.color_range]]
    output_track = 0
    for t in a.tracks:
        if t.encoder == "none":
            continue
        index = int(t.source if t.source != "auto" else t.track) - 1
        # Default/auto audio is optional for silent clips; explicit tracks are required.
        optional = "?" if t.source == "auto" else ""
        args += ["-map", f"0:a:{index}{optional}", f"-c:a:{output_track}", AUDIO[t.encoder]]
        if t.mixdown:
            if t.mixdown not in ("mono", "stereo", "5point1", "7point1"):
                raise SpecError("FFmpeg supports mono/stereo/5point1/7point1 mixdown only")
            args += [f"-ac:a:{output_track}", {"mono": "1", "stereo": "2", "5point1": "6", "7point1": "8"}[t.mixdown]]
        if t.bitrate:
            args += [f"-b:a:{output_track}", t.bitrate + "k"]
        if t.samplerate:
            args += [f"-ar:a:{output_track}", t.samplerate]
        if t.drc:
            raise SpecError("HandBrake DRC has no FFmpeg equivalent")
        if t.gain:
            args += [f"-filter:a:{output_track}", f"volume={t.gain}dB"]
        if t.name:
            args += [f"-metadata:s:a:{output_track}", "title=" + t.name]
        output_track += 1
    if not output_track:
        args += ["-an"]
    selected = [t["track"] for t in s.tracks] or ([1] if s.behavior != "none" else [])
    for track in selected:
        args += ["-map", f"0:s:{track - 1}"]
    if selected:
        args += ["-c:s", "mov_text" if container == "mp4" else "webvtt" if container == "webm" else "srt"]
        if s.default_track or s.behavior == "default":
            args += [f"-disposition:s:{(s.default_track or 1) - 1}", "default"]
    else:
        args += ["-sn"]
    args += ["-map_chapters", "-1" if spec.chapters.mode == "none" else "0", "-map_metadata", "-1"]
    if stream.faststart:
        args += ["-movflags", "+faststart"]
    if container != "auto":
        args += ["-f", "matroska" if container == "mkv" else container]
    return args
