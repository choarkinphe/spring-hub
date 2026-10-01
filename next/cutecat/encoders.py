"""Encoder presentation and explicit, equivalent CLI naming adaptations."""
from .engine import EngineCapabilities

#: The backend token to look for in a source build's ``./configure --help``.
#: Deliberately *not* a hardcoded flag: HandBrake's configure option names vary
#: between releases, and this environment has no upstream source to check them
#: against, so the guidance tells the operator to read the flag off configure
#: rather than guessing one that might silently do nothing.
CONFIGURE_HINT = {
    "nvenc": "nvenc",
    "qsv": "qsv",
    "vce": "vce",
    "vaapi": "vaapi",
    "amf": "amf",
}

#: Backends that exist on exactly one platform, so "install a different build"
#: is the wrong advice and "this cannot be enabled here" is the right one.
PLATFORM_ONLY_BACKENDS = {
    "videotoolbox": "Apple VideoToolbox 是 macOS 上的系统框架，Linux 构建不提供它。",
}

VENDORS = {
    "nvenc": "NVIDIA NVENC",
    "qsv": "Intel Quick Sync",
    "vce": "AMD VCE / VCN",
    "videotoolbox": "Apple VideoToolbox",
    "vaapi": "VA-API",
    "amf": "AMD AMF",
}

VIDEO_DESCRIPTIONS = {
    "x264": "H.264 软件编码，兼容性广，适合日常播放与分享。",
    "x264_10bit": "10 位 H.264，改善渐变表现；播放设备需支持 10 位解码。",
    "x265": "H.265 软件编码，压缩效率高于 H.264，通常需要更多 CPU 时间。",
    "x265_10bit": "10 位 H.265，适合高质量渐变与 HDR 素材；HDR 保留还取决于其它设置。",
    "x265_12bit": "12 位 H.265，适合专业流程，设备兼容性较有限。",
    "mpeg4": "传统 MPEG-4 Part 2，主要用于旧设备兼容。",
    "mpeg2": "传统 MPEG-2，适用于 DVD 等旧格式工作流。",
    "VP9": "VP9 开放格式，适合 WebM 与网络视频，软件编码较耗 CPU。",
    "av1": "SVT-AV1 软件编码，压缩效率高，编码耗时和播放支持需考虑。",
    "theora": "传统开放视频格式，压缩效率和现代设备支持较有限。",
}
AUDIO_DESCRIPTIONS = {
    "none": "不输出该音轨。",
    "auto": "由 HandBrake 按输出容器选择默认音频编码器。",
    "copy": "直通源音频，不重新压缩；是否能直通取决于源格式与输出容器。",
    "aac": "AAC 有损音频，兼容性广，适合 MP4 和日常播放。",
    "ac3": "Dolby Digital 有损音频，常用于家庭影院多声道。",
    "eac3": "Dolby Digital Plus，有损多声道音频。",
    "dts": "DTS 多声道音频；当前引擎需实际提供编码能力，不等同于 DTS 直通。",
    "dtshd": "DTS-HD 音频；直通支持并不代表提供 DTS-HD 编码器。",
    "truehd": "Dolby TrueHD 无损音频，主要用于家庭影院。",
    "flac": "FLAC 无损压缩，保留解码后的音频数据，体积大于有损格式。",
    "mp3": "MP3 有损音频，兼容旧设备和音乐播放器。",
    "opus": "Opus 有损音频，低码率效率高，常配合 WebM/MKV。",
    "vorbis": "Vorbis 开放有损音频，常配合 WebM/MKV。",
    "lpcm": "线性 PCM 未压缩音频，文件较大。",
}
ALIASES = {"video": {"av1": ("svt_av1",)}, "audio": {
    "aac": ("av_aac",), "flac": ("flac16",), "lpcm": ("pcm16",),
}}

#: Formats HandBrake only ever passes through. No engine build ships an encoder
#: for these, so they must never be presented as "not installed" (installable).
AUDIO_PASSTHROUGH_ONLY = {"dts": "DTS", "dtshd": "DTS-HD"}

#: Audio values worth *explaining* in the UI but never valid to submit. They are
#: absent from :data:`cutecat.spec.AUDIO_ENCODERS` on purpose; the catalog shows
#: them so the reason is visible instead of the option silently vanishing.
#:
#: Verified against HandBrakeCLI 1.11.0 with a real audio track present:
#: ``-E auto`` and ``-E dtshd`` fail with "Invalid audio encoder"; ``-E dts`` is
#: accepted but is a passthrough request that silently produced AAC.
AUDIO_CATALOG_ONLY = frozenset({"auto", "dts", "dtshd"})


def cli_encoder(kind: str, value: str, reported: list[str]) -> str | None:
    if value in reported:
        return value
    return next((name for name in ALIASES[kind].get(value, ()) if name in reported), None)


def encoder_family(value: str, hardware: dict) -> str:
    """The hardware backend a value belongs to, or ``""`` for software."""

    if value not in hardware:
        return ""
    family = value.split("_")[0]
    return "videotoolbox" if family == "vt" else family


def _acquisition(kind: str, value: str, family: str, *, is_gpu: bool) -> dict | None:
    """How to obtain an engine build that actually contains this encoder.

    HandBrake encoders are **compiled into the binary**, not separate packages:
    there is no ``install nvenc_h265``. So the only honest instruction is "get a
    different engine build", and the operator runs it on the host. Cute Cat
    never executes any of this — it has no package manager, no Docker socket and
    no privilege, by design.
    """

    if family in PLATFORM_ONLY_BACKENDS:
        return None

    options: list[dict] = []
    if is_gpu:
        token = CONFIGURE_HINT.get(family, family)
        options.append({
            "label": "从源码构建（唯一能保证带上硬件编码器的方式）",
            "detail": f"发行版自带的 handbrake-cli 通常不带硬件编码器。"
                      f"先在源码目录用 ./configure --help 查出与 {token} 相关的开关"
                      "（各版本名称不同），构建后再让引擎指向新二进制。",
            "command": "./configure --help | grep -i " + token,
        })
    options.append({
        "label": "发行版软件包",
        "detail": "最快，但发行版构建可能不含全部编码器；安装后回到本页点“重新检测”确认。",
        "command": "sudo apt-get install handbrake-cli",
    })
    if is_gpu:
        options.append({
            "label": "在容器中使用本项目的镜像",
            "detail": "镜像里的 HandBrake 来自发行版，同样可能不含硬件编码器；"
                      "还需要把设备节点映射进容器，否则硬件依旧不可用。",
            "command": "docker compose -f compose.handbrake.yaml up --build",
        })
    options.append({
        "label": "换成你已有的引擎",
        "detail": "把 engine.handbrake_bin 指向新构建的 HandBrakeCLI，然后点“重新检测”。",
        "command": "handbrake_bin = \"/usr/local/bin/HandBrakeCLI\"",
    })

    return {
        "headline": "编码器编译在 HandBrake 引擎里，不能单独安装；换一个包含它的引擎构建即可。",
        "note": "以下命令由你在宿主机执行。本程序不会执行安装，也不需要任何特权。",
        "reference": "README.handbrake.md · Building a hardware-enabled engine",
        "options": options,
    }


def encoder_catalog(caps: EngineCapabilities, video: dict, hardware: dict, audio) -> dict:
    """Build the presentation catalog.

    ``status`` separates the ways an encoder can be unusable:

    ``no_hardware``   the engine build *has* the encoder, but this machine
                      cannot run it (no GPU, no driver, no device node in the
                      container) — nothing to install.
    ``not_installed`` the engine build really does not contain the encoder, so
                      a different build would fix it — the card shows how to get
                      one.
    ``unsupported``   no build provides it on this platform at all.

    Whether the build contains an encoder comes from
    :attr:`EngineCapabilities.video_encoder_probe` (real instantiation), because
    ``HandBrakeCLI --help`` under-reports the encoder table and using it alone
    told users to install encoders they already had.
    """

    result = {"video": [], "audio": []}
    probe = caps.video_encoder_probe
    for kind, names in (("video", {**video, **hardware}), ("audio", {name: name.upper() for name in sorted(audio)})):
        reported = caps.encoders if kind == "video" else caps.audio_encoders
        known = caps.video_encoders_known if kind == "video" else caps.audio_encoders_known
        for value, name in names.items():
            is_gpu = kind == "video" and value in hardware
            device = "gpu" if is_gpu else "cpu"
            family = encoder_family(value, hardware)
            vendor = VENDORS.get(family, "CPU 软件编码")
            resolved = cli_encoder(kind, value, reported)
            special = kind == "audio" and value == "none"
            catalog_only = kind == "audio" and value in AUDIO_CATALOG_ONLY
            hardware_missing = False
            backend = caps.hardware_status.get(family) if is_gpu else None
            acquisition = None

            # What the instantiation probe says about this exact encoder.
            verdict = None
            if kind == "video" and probe:
                probed_name = value if value in probe else next(
                    (alias for alias in ALIASES["video"].get(value, ()) if alias in probe), None,
                )
                verdict = probe.get(probed_name) if probed_name else None

            if not caps.available:
                installed: bool | None = False
            elif verdict is not None:
                installed = verdict != "absent"
            elif not known:
                installed = None
            else:
                installed = bool(resolved or special)

            selectable = installed is True

            if not caps.available:
                status, reason = "missing", "未安装 HandBrakeCLI，或配置的引擎不可执行。"
            elif catalog_only:
                # Explained, but never submittable: these are absent from the spec
                # whitelist because the CLI rejects or reinterprets them.
                selectable = False
                if value in AUDIO_PASSTHROUGH_ONLY:
                    label = AUDIO_PASSTHROUGH_ONLY[value]
                    status = "passthrough_only"
                    reason = (
                        f"HandBrake 只提供 {label} 直通，不提供 {label} 编码器，任何构建都无法编码为 {label}；"
                        f"若要保留源 {label} 音轨，请选择“直通（copy）”。"
                    )
                else:
                    status = "unsupported"
                    reason = "auto 不是 HandBrake 命令行接受的音频编码器取值，任何引擎构建都不会提供；请选择具体编码器，或使用直通。"
            elif verdict == "absent":
                # Genuinely missing from this build — the only case where a
                # different engine build would help.
                if family in PLATFORM_ONLY_BACKENDS:
                    status = "unsupported"
                    reason = PLATFORM_ONLY_BACKENDS[family]
                    selectable = False
                else:
                    status = "not_installed"
                    selectable = False
                    acquisition = _acquisition(kind, value, family, is_gpu=is_gpu)
                    if is_gpu:
                        reason = (
                            f"实例化检测确认当前 HandBrake 构建不含 {vendor} 的此编码器；"
                            "更换包含它的构建即可使用。"
                        )
                    else:
                        reason = "实例化检测确认当前 HandBrake 构建不含此编码器；更换包含它的引擎构建即可使用。"
            elif verdict == "blocked":
                # In the build, but this machine cannot run it. Measured, not
                # inferred — the probe reached the encoder and the encode failed.
                status = "no_hardware"
                hardware_missing = True
                selectable = False
                if backend == "unavailable":
                    reason = (
                        f"引擎包含此 {vendor} 编码器，但报告该后端不可用，实例化后编码失败；"
                        "请检查硬件、驱动或容器内的设备映射。"
                    )
                else:
                    reason = (
                        f"引擎包含此 {vendor} 编码器，但实例化后编码失败；"
                        "通常是缺少可用硬件、驱动或容器内的设备节点，与是否安装引擎无关。"
                    )
            elif not known and verdict is None:
                status, reason = "unknown", "编码器清单检测失败或格式无法识别，请重新检测。"
            elif not installed:
                # No instantiation verdict for this encoder. Fall back to the
                # help-derived answer, and say plainly which weaker source it
                # came from — a probe that never ran is not the same as one that
                # ran and simply did not cover this encoder.
                fallback = (
                    "（实例化检测未执行，此为 --help 清单结论。）" if probe is None
                    else "（实例化检测未覆盖此编码器，此为 --help 清单结论。）"
                )
                if is_gpu and backend != "detected":
                    hardware_missing = True
                    status = "no_hardware"
                    if backend == "unavailable":
                        reason = f"引擎报告 {vendor} 后端不可用；此编码器需要对应硬件与驱动，请检查设备、驱动或容器映射。"
                    else:
                        reason = f"未检测到 {vendor} 硬件后端；此编码器需要对应硬件与驱动，无法仅靠安装引擎启用。"
                else:
                    status = "not_installed"
                    acquisition = _acquisition(kind, value, family, is_gpu=is_gpu)
                    if is_gpu:
                        reason = f"{vendor} 硬件已检测到，但当前 HandBrake 构建未包含此编码器；更换包含它的构建即可使用。"
                    else:
                        reason = "当前 HandBrake 构建未包含此编码器；更换包含它的引擎构建即可使用。"
                    reason += fallback
            elif is_gpu:
                if verdict == "works":
                    status = "unverified"
                    reason = "实例化检测中该编码器完成了一次极小尺寸的测试编码；实际素材的 GPU 转码仍需以转码结果为准。"
                elif backend == "unavailable":
                    status, reason, selectable = "no_hardware", "引擎列出了此编码器，却报告该硬件后端不可用；请检查设备、驱动或容器映射。", False
                    hardware_missing = True
                else:
                    status, reason = "unverified", "引擎已列出此编码器；实际 GPU 转码尚未验证。"
                    if backend == "detected":
                        reason = "引擎检测到硬件后端；实际 GPU 转码尚未验证。"
            else:
                status, reason = "available", "当前引擎已提供；具体素材与参数兼容性以转码结果为准。"

            if kind == "audio" and value in ("none", "auto", "copy"):
                device, vendor = "passthrough", "不重新编码" if value != "auto" else "按容器自动选择"
            elif status == "passthrough_only":
                device, vendor = "passthrough", "仅直通（无法编码）"
            description = (VIDEO_DESCRIPTIONS if kind == "video" else AUDIO_DESCRIPTIONS).get(value)
            if is_gpu:
                codec = "H.264" if "h264" in value else "H.265"
                depth = "10 位 " if "10bit" in value else ""
                description = f"{vendor} 专用硬件编码 {depth}{codec}，通常速度快、CPU 编码负担低；需要兼容硬件与驱动。"
            result[kind].append({
                "id": value, "name": name, "description": description,
                "device": device, "backend": vendor, "installed": installed,
                "status": status, "reason": reason, "selectable": selectable,
                "cli_name": resolved, "hardware_missing": hardware_missing,
                "acquisition": acquisition,
            })
    return result
