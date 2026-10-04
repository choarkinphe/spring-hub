#!/usr/bin/env python3
"""A faithful *interface* mock of HandBrakeCLI, used only for wiring tests.

This is **not** HandBrake and proves nothing about real transcoding quality or
real CLI compatibility. It reproduces the parts of the command-line contract
that Cute Cat depends on, so the queue/scan/preset/progress code paths can be
exercised without a GPU or a media file:

    HandBrakeCLI --version
    HandBrakeCLI --help
    HandBrakeCLI --preset-list
    HandBrakeCLI --preset-import-file <file> [-z]
    HandBrakeCLI -i <input> --scan --json
    HandBrakeCLI -i <in> -o <out> [flags...] --json

Behaviour switches (for tests):
    --mock-fail        exit non-zero with an error on stderr
    --mock-slow        emit progress over several seconds (cancel testing)
    --mock-titles N    number of titles reported by --scan
    --mock-blocked E   comma-separated encoders this build contains but cannot
                       run (no hardware); they instantiate and then fail.
                       Defaults to BLOCKED_ENCODERS, mirroring the real build.
    --mock-absent E    comma-separated encoders this build does not contain at
                       all; they never reach the job configuration. Additive:
                       anything outside BUILD_ENCODERS is absent anyway.

The mock deliberately reproduces the real build's most misleading behaviour:
its ``--help`` lists FEWER encoders than it actually contains (see
``HELP_ENCODERS`` vs ``BUILD_ENCODERS``), which is what made the old
help-derived detection call present encoders "not installed".
"""

from __future__ import annotations

import json
import os
import sys
import time

#: What ``--help`` advertises. Note this omits nvenc_h265 / qsv_*, exactly like
#: the real Ubuntu 1.11.0 build.
HELP_ENCODERS = (
    "x264", "x265", "x264_10bit", "x265_10bit", "x265_12bit", "mpeg4",
    "mpeg2", "VP9", "svt_av1", "theora", "nvenc_h264",
)

#: What the build really contains: the help list plus encoders the help text
#: forgot. ``vce_*`` and ``vt_*`` are absent from both, like the real build.
BUILD_ENCODERS = HELP_ENCODERS + (
    "nvenc_h265", "nvenc_h265_10bit",
    "qsv_h264", "qsv_h265", "qsv_h265_10bit",
)

#: Encoders the build contains that this machine cannot run, because there is
#: no usable hardware backend. They instantiate, then fail.
BLOCKED_ENCODERS = (
    "nvenc_h264", "nvenc_h265", "nvenc_h265_10bit",
    "qsv_h264", "qsv_h265", "qsv_h265_10bit",
)

#: Human-readable names HandBrake prints into its job configuration.
ENCODER_DISPLAY = {
    "x264": "H.264 (libx264)", "x264_10bit": "H.264 10-bit (libx264)",
    "x265": "H.265 (libx265)", "x265_10bit": "H.265 10-bit (libx265)",
    "x265_12bit": "H.265 12-bit (libx265)",
    "mpeg4": "MPEG-4 (libavcodec)", "mpeg2": "MPEG-2 (libavcodec)",
    "VP9": "VP9 (libvpx)", "svt_av1": "AV1 (SVT)", "theora": "Theora",
    "nvenc_h264": "H.264 (NVEnc)", "nvenc_h265": "H.265 (NVEnc)",
    "nvenc_h265_10bit": "H.265 10-bit (NVEnc)",
    "qsv_h264": "H.264 (Intel QSV)", "qsv_h265": "H.265 (Intel QSV)",
    "qsv_h265_10bit": "H.265 10-bit (Intel QSV)",
}


def _emit_version() -> int:
    print("HandBrake 1.7.3")
    print("https://handbrake.fr")
    print("Mock build for wiring tests — NOT a real HandBrake binary.")
    return 0


def _emit_help() -> int:
    print("Usage: HandBrakeCLI [options] -i <source> -o <destination>")
    print()
    print("### Video Options ----------------------------------------------------")
    # Deliberately under-reports: HELP_ENCODERS is a strict subset of what the
    # build actually contains. A probe that trusts this list will wrongly call
    # nvenc_h265 / qsv_* "not installed".
    print("   -e, --encoder <string>   Set video encoder (" + ", ".join(HELP_ENCODERS[:4]) + ",")
    print("                            " + ", ".join(HELP_ENCODERS[4:8]) + ",")
    print("                            " + ", ".join(HELP_ENCODERS[8:11]) + ")")
    print("   -q, --quality <float>    Set video quality")
    print("   -b, --vb <int>           Set video bitrate (kbps)")
    print("       --encoder-preset     x264/x265 preset (ultrafast..placebo)")
    print("       --encoder-tune       x264/x265 tune")
    print("       --encoder-profile    H.264/H.265 profile")
    print("       --encoder-level      H.264/H.265 level")
    print()
    print("### Audio Options ----------------------------------------------------")
    # Mirrors the real 1.11 per-line enumeration. Note the real spellings:
    # av_aac (not "aac"), flac16/flac24 (not "flac"), pcm16/pcm24 (not "lpcm"),
    # and copy:<type> entries with no bare "dts"/"dtshd"/"auto" encoder.
    print("   -E, --aencoder <string> Select audio encoder(s):")
    for name in ("none", "av_aac", "copy:aac", "ac3", "copy:ac3", "eac3",
                 "copy:eac3", "truehd", "copy:truehd", "copy:dts", "copy:dtshd",
                 "copy:mp2", "mp3", "copy:mp3", "opus", "copy:opus", "vorbis",
                 "copy:vorbis", "flac16", "flac24", "copy:flac", "alac16",
                 "alac24", "copy:alac", "pcm16", "pcm24", "copy:pcm", "copy"):
        print(f"                               {name}")
    print("   -6, --mixdown <string>   Set mixdown (mono, stereo, dpl2, 5point1)")
    print("   -B, --ab <int>           Set audio bitrate")
    print("       --audio-fallback <string>")
    print("                           Set audio codec to use when copy is not possible.")
    print("       --audio-copy-mask <string>")
    print("                           Set audio codecs permitted when 'copy' is used")
    print("                           (aac/ac3/eac3/truehd/dts/dtshd/mp2/mp3/opus/")
    print("                           vorbis/flac/alac/pcm)")
    print()
    print("### Destination Options ----------------------------------------------")
    print("   -f, --format <string>    Set output container (mp4, mkv, webm)")
    return 0


def _emit_presets() -> int:
    print("General/Fast 1080p30")
    print("General/HQ 1080p30 Surround")
    print("General/Super HQ 1080p30 Surround")
    print("Matroska/H.265 MKV 1080p30")
    print("Matroska/H.265 MKV 2160p60 4K")
    print("Web/Gmail Large 3 Minutes 720p30")
    return 0


def _import_preset(path: str) -> int:
    if not os.path.isfile(path):
        sys.stderr.write(f"preset import failed: no such file: {path}\n")
        return 1
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"preset import failed: invalid JSON: {exc}\n")
        return 1
    if not isinstance(doc, dict) or "PresetList" not in doc:
        sys.stderr.write("preset import failed: missing PresetList\n")
        return 1
    print(f"Imported {len(doc['PresetList'])} preset(s) from {os.path.basename(path)}")
    return 0


def _scan(source: str, titles: int) -> int:
    if not os.path.isfile(source):
        sys.stderr.write(f"scan failed: cannot open {source}\n")
        return 1
    title_list = []
    for index in range(1, titles + 1):
        title_list.append(
            {
                "Index": index,
                "Name": f"Title {index}",
                "Duration": {"Hours": 0, "Minutes": 1 + index, "Seconds": 30},
                "Format": "AVC",
                "VideoCodec": "h264",
                "FrameRate": 23.976,
                "Resolution": {"Width": 1920, "Height": 1080},
            }
        )
    print(json.dumps({"Version": "1.7.3", "TitleList": title_list}))
    return 0


def _csv_option(argv: list[str], flag: str) -> set[str]:
    if flag not in argv:
        return set()
    index = argv.index(flag)
    if index + 1 >= len(argv):
        return set()
    return {part.strip() for part in argv[index + 1].split(",") if part.strip()}


def _encoder_name(argv: list[str]) -> str | None:
    if "-e" not in argv:
        return None
    index = argv.index("-e")
    return argv[index + 1] if index + 1 < len(argv) else None


def _emit_job_configuration(encoder: str, dest: str, source: str) -> None:
    """Print the job configuration, like the real CLI does before encoding.

    The encoder line is the presence proof: HandBrake resolves the encoder name
    here *before* it touches any hardware, so this is printed even when the
    encode goes on to fail for lack of a GPU.
    """

    sys.stderr.write("[12:00:00] job configuration:\n")
    sys.stderr.write("[12:00:00]  * source\n")
    sys.stderr.write(f"[12:00:00]    + {source}\n")
    sys.stderr.write("[12:00:00]  * destination\n")
    sys.stderr.write(f"[12:00:00]    + {dest}\n")
    sys.stderr.write("[12:00:00]  * video track\n")
    sys.stderr.write(f"[12:00:00]    + encoder: {ENCODER_DISPLAY.get(encoder, encoder)}\n")
    sys.stderr.write("[12:00:00]  * audio track 1\n")
    # Six-space indent: a probe that scans the whole log would pick this up and
    # conclude every encoder is present.
    sys.stderr.write("[12:00:00]    + encoder: AAC (libavcodec)\n")


def _encode(source: str, dest: str, slow: bool, fail: bool, argv: list[str]) -> int:
    if fail:
        sys.stderr.write('{"State":"WORKING","Progress":5.0}\n')
        sys.stderr.write("ERROR: mock encode failure\n")
        return 1
    if not os.path.isfile(source):
        sys.stderr.write(f"encode failed: cannot open {source}\n")
        return 1

    encoder = _encoder_name(argv)
    absent = _csv_option(argv, "--mock-absent")
    # Default to the real build's behaviour: the NVENC/QSV encoders are compiled
    # in but this machine has no usable backend, so they fail *after* naming
    # themselves. --mock-blocked replaces that default.
    blocked = _csv_option(argv, "--mock-blocked")
    if "--mock-blocked" not in argv:
        blocked = set(BLOCKED_ENCODERS)

    if encoder:
        known = set(BUILD_ENCODERS)
        if encoder in absent or (encoder not in known):
            # Absent from the build: the job never gets as far as naming it.
            sys.stderr.write(f"ERROR: Unknown video codec ({encoder})\n")
            sys.stderr.write("Encode failed (error 3).\n")
            return 3
        _emit_job_configuration(encoder, dest, source)
        if encoder in blocked:
            # Present, but no usable hardware: fails *after* naming itself.
            sys.stderr.write("ERROR: Failure to initialise thread 'FFMPEG encoder'\n")
            sys.stderr.write("Encode failed (error 3).\n")
            return 3

    steps = 5 if slow else 3
    delay = 0.6 if slow else 0.05
    for step in range(1, steps + 1):
        progress = step * (100.0 / steps)
        sys.stderr.write(json.dumps({
            "State": "WORKING",
            "Progress": round(progress, 2),
            "Rate": 42.5,
            "ETA": max(0, (steps - step)),
        }) + "\n")
        sys.stderr.flush()
        time.sleep(delay)
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    with open(dest, "wb") as handle:
        handle.write(b"MOCK-OUTPUT\n")
    # The real CLI prints "mux: track 0, ..." once the video track is written, and
    # the instantiation probe uses that line — not the exit code — as its success
    # signal (a successful real x265 encode exits 4).
    sys.stderr.write(f"[12:00:00] mux: track 0, 1 frames, 1 bytes, 1.00 kbps, {dest}\n")
    sys.stderr.write('{"State":"MUXING","Progress":100.0}\n')
    return 0


def main(argv: list[str]) -> int:
    if "--version" in argv:
        return _emit_version()
    if "--help" in argv or "-h" in argv:
        return _emit_help()
    if "--preset-list" in argv:
        return _emit_presets()
    document = None
    if "--preset-import-file" in argv:
        path = argv[argv.index("--preset-import-file") + 1]
        result = _import_preset(path)
        if result:
            return result
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        if "--preset" not in argv:
            return 0
    if "--preset" in argv:
        name = argv[argv.index("--preset") + 1]
        if document:
            selected = next((entry for entry in document["PresetList"] if entry.get("PresetName") == name), None)
        else:
            official = {"General/Fast 1080p30", "General/HQ 1080p30 Surround",
                        "General/Super HQ 1080p30 Surround", "Matroska/H.265 MKV 1080p30",
                        "Matroska/H.265 MKV 2160p60 4K", "Web/Gmail Large 3 Minutes 720p30"}
            selected = {"PresetName": name, "VideoEncoder": "x265" if name.startswith("Matroska/") else "x264",
                        "VideoPreset": "fast", "FileFormat": "av_mp4", "AudioList": [{"AudioEncoder": "av_aac"}],
                        "AudioEncoderFallback": "av_aac"} if name in official else None
        if not selected:
            sys.stderr.write(f"Unknown preset: {name}\n")
            return 1
        selected = {"VideoEncoder": "x264", "AudioList": [{"AudioEncoder": "av_aac"}], **selected}
        if "--preset-export" in argv:
            selected = {**selected, "PresetName": argv[argv.index("--preset-export") + 1]}
            print(json.dumps({"PresetList": [selected], "VersionMajor": 72, "VersionMinor": 0, "VersionMicro": 0}))
            return 0
        if "-e" not in argv:
            argv = [*argv, "-e", selected["VideoEncoder"]]

    source = None
    dest = None
    for index, token in enumerate(argv):
        if token == "-i" and index + 1 < len(argv):
            source = argv[index + 1]
        elif token == "-o" and index + 1 < len(argv):
            dest = argv[index + 1]

    if "--scan" in argv:
        titles = 2
        if "--mock-titles" in argv:
            titles = int(argv[argv.index("--mock-titles") + 1])
        return _scan(source or "", titles)

    if source and dest:
        return _encode(source, dest, "--mock-slow" in argv, "--mock-fail" in argv, argv)

    sys.stderr.write("mock HandBrakeCLI: no action matched\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
