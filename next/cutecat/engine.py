"""HandBrakeCLI integration: capability probe, scan parsing, and encoding.

Everything here shells out to the real ``HandBrakeCLI`` binary. The separate
``ffmpeg_engine`` module executes FFmpeg/rffmpeg tasks; it does not emulate
HandBrake presets. ``ffprobe`` remains optional for HandBrake tasks and required
for FFmpeg-compatible tasks.

The exact CLI surface used (stable across HandBrake 1.x):

* ``HandBrakeCLI --version``
* ``HandBrakeCLI --help``
* ``HandBrakeCLI --preset-list``
* ``HandBrakeCLI --preset-import-file <f> [-z]``
* ``HandBrakeCLI -i <input> --scan --json``   (scan / title metadata)
* ``HandBrakeCLI -i <input> -o <out> --preset "..." ...``  (encode)
* ``--json`` emits newline-delimited JSON events on **stderr** during encode.

One extra call is made, and only to answer "does this build really contain
that encoder?" — a one-frame encode against a generated clip, described in
:meth:`HandBrakeEngine._probe_encoder_instantiation`. ``--help`` under-reports
HandBrake's own encoder table, so it cannot be trusted on its own.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import EngineConfig

VERSION_RE = re.compile(r"HandBrake\s+([0-9][0-9A-Za-z.\-]*)")


class EngineError(RuntimeError):
    """A HandBrakeCLI invocation failed or produced unusable output."""


@dataclass
class EngineCapabilities:
    available: bool
    binary: str | None
    version: str | None
    version_string: str | None
    help_available: bool
    presets_available: bool
    encoders: list[str] = field(default_factory=list)
    muxers: list[str] = field(default_factory=list)
    hardware_encoders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    audio_encoders: list[str] = field(default_factory=list)
    video_encoders_known: bool = False
    audio_encoders_known: bool = False
    hardware_status: dict[str, str] = field(default_factory=dict)
    #: Per-encoder verdicts from actually instantiating each one (see
    #: :meth:`HandBrakeEngine._probe_encoder_instantiation`), keyed by the CLI's
    #: own name and valued ``"works"`` / ``"blocked"`` / ``"absent"``. ``None``
    #: means the probe did not run or could not be trusted, so "absent from
    #: ``--help``" must never be read as "absent from the build".
    video_encoder_probe: dict[str, str] | None = None
    #: Human-readable reasons the instantiation probe could not be trusted.
    #: Kept separate from ``notes`` so the UI can say "detection was partial"
    #: without implying the engine is broken.
    probe_notes: list[str] = field(default_factory=list)
    decoder_backends: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "binary": self.binary,
            "version": self.version,
            "version_string": self.version_string,
            "help_available": self.help_available,
            "presets_available": self.presets_available,
            "encoders": self.encoders,
            "muxers": self.muxers,
            "hardware_encoders": self.hardware_encoders,
            "notes": self.notes,
            "audio_encoders": self.audio_encoders,
            "video_encoders_known": self.video_encoders_known,
            "audio_encoders_known": self.audio_encoders_known,
            "hardware_status": self.hardware_status,
            "video_encoder_probe": self.video_encoder_probe,
            "probe_notes": self.probe_notes,
            "decoder_backends": self.decoder_backends,
        }


#: Encoder identifiers that HandBrake reports as hardware-accelerated. These
#: are HandBrake's own names — deliberately *not* borrowed from FFmpeg's
#: ``vaapi``/``amf`` naming. Support is only ever reported when the real CLI
#: lists it; nothing is inferred.
HARDWARE_ENCODER_PREFIXES = (
    "nvenc",       # NVIDIA NVENC (h264_nvenc / hevc_nvenc ...)
    "qsv",         # Intel Quick Sync
    "vce",         # AMD VCE / VCN
    "videotoolbox",
    "vt_",
    "vaapi",
    "amf",
)


def _run(cmd: Sequence[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


# -- encoder-presence probing ---------------------------------------------
#
# ``HandBrakeCLI --help`` is *not* a reliable encoder inventory: the Ubuntu
# 1.11.0 build prints 16 names while actually containing 23 (``nvenc_h265``,
# ``nvenc_h265_10bit`` and every QSV encoder are missing from the list but do
# instantiate). Deriving "not installed" from ``--help`` therefore produced
# false "install this" advice for encoders that are already compiled in.
#
# Two candidate oracles were measured and rejected:
#
# * ``-i <missing> -e <enc>`` — the input error masks everything, so even
#   ``bogus_xyz`` looks fine.
# * ``--encoder-preset-list <enc>`` — answers from a static registry and names
#   ``vt_h264`` (Apple VideoToolbox, impossible on Linux) with a real preset
#   list, so it cannot separate "compiled in" from "compiled out".
#
# The only reliable signal is a real job: once HandBrake accepts the encoder it
# prints the resolved encoder into the job configuration before it fails at
# hardware init. That line is the presence proof.

#: HandBrake indents its structured log lines (``[hh:mm:ss]    + encoder: …``),
#: and the timestamp is optional because a build can omit it. Both the timestamp
#: and the indent are optional here so the same pattern reads a bare, unindented
#: ``+ encoder:`` line too.
_LOG_PREFIX = r"(?:\s*\[[0-9:]+\])?\s*"

#: HandBrake writes this once the video track is actually muxed. It is the real
#: "the encode worked" signal: the process exit code is not, because a
#: successful x265 encode exits 4 while a failed NVENC one exits 3.
MUX_VIDEO_TRACK_RE = re.compile(r"^" + _LOG_PREFIX + r"mux: track 0", re.MULTILINE)

#: The job configuration lists the tracks it is about to build. Reading the
#: video encoder only from inside the video-track block keeps the audio
#: encoder — which every run has — from being mistaken for a presence proof.
VIDEO_TRACK_MARKER_RE = re.compile(r"^" + _LOG_PREFIX + r"\*\s*video track\b", re.MULTILINE)
AUDIO_TRACK_MARKER_RE = re.compile(r"^" + _LOG_PREFIX + r"\*\s*audio track\b", re.MULTILINE)
TRACK_ENCODER_RE = re.compile(r"^" + _LOG_PREFIX + r"\+\s*encoder:\s*(\S.*?)\s*$", re.MULTILINE)

#: Encoders that every HandBrake build ships. One of these is used as a control
#: run: if the instantiation probe cannot confirm an encoder that is certainly
#: present, the probe itself is untrustworthy and its silence must never be read
#: as "absent from the build".
INSTANTIATION_CONTROL_ENCODERS = ("x264", "x265", "mpeg4", "mpeg2", "VP9", "theora")

#: Scratch clip used for the instantiation probe. 320x240 because x265 refuses
#: anything under one CTU, which would fail for reasons unrelated to presence.
PROBE_CLIP_NAME = "cute-cat-probe.avi"
PROBE_CLIP_WIDTH = 320
PROBE_CLIP_HEIGHT = 240

#: Container for the probe encode. Matroska accepts every encoder HandBrake
#: ships; MP4 does not, and using it made VP8/Theora look absent (their jobs
#: were refused for container reasons, before the encoder was ever named) and
#: made x265 exit non-zero. Measured with ``-f av_mp4`` vs ``-f av_mkv``.
PROBE_CONTAINER = "av_mkv"

#: Per-encoder ceiling for the instantiation probe. Measured cost on a 12-core
#: box is 0.6-1.2 s; anything near this limit means the engine is wedged.
PROBE_ENCODER_TIMEOUT = 20

#: How many instantiation probes run at once. Nine at once finished in ~1.1 s
#: but would fight a CPU-limited container; four keeps the cold cost near 2.5 s.
PROBE_CONCURRENCY = 4

#: How long a capability probe is reused. The probe now costs a real (if tiny)
#: encode per unlisted encoder, so it must not run on every HTTP request.
PROBE_CACHE_SECONDS = 300


def _build_probe_clip(path: str, *, width: int = PROBE_CLIP_WIDTH,
                      height: int = PROBE_CLIP_HEIGHT) -> None:
    """Write a one-frame AVI that HandBrakeCLI will open and scan.

    Raw BGR24 video plus a mono 16-bit PCM track, so the clip needs no external
    codec and cannot fail for an encoder-independent reason.
    """

    fps, frames = 1, 1
    sample_rate, channels, sample_bytes = 8000, 1, 2
    frame = bytes([80, 120, 160]) * (width * height)
    samples = sample_rate // fps
    audio_bytes = samples * channels * sample_bytes

    def chunk(tag: bytes, data: bytes) -> bytes:
        padding = b"\x00" if len(data) % 2 else b""
        return tag + struct.pack("<I", len(data)) + data + padding

    def list_chunk(kind: bytes, data: bytes) -> bytes:
        return b"LIST" + struct.pack("<I", len(data) + 4) + kind + data

    avih = struct.pack(
        "<IIIIIIIIII4I", 1000000 // fps, 0, 0, 0x10, frames, 0, 1,
        width * height * 3, width, height, 0, 0, 0, 0,
    )
    vstrh = struct.pack(
        "<4s4sIHHIIIIIIIIhhhh", b"vids", b"DIB ", 0, 0, 0, 0, 1, fps, 0,
        frames, width * height * 3, 0xFFFFFFFF, 0, 0, 0, width, height,
    )
    vstrf = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24, 0, width * height * 3, 0, 0, 0, 0,
    )
    astrh = struct.pack(
        "<4s4sIHHIIIIIIIIhhhh", b"auds", b"\x00\x00\x00\x00", 0, 0, 0, 0, 1,
        sample_rate * channels * sample_bytes, 0, audio_bytes,
        frames * audio_bytes, 0xFFFFFFFF, channels * sample_bytes, 0, 0, 0, 0,
    )
    astrf = struct.pack(
        "<HHIIHH", 1, channels, sample_rate,
        sample_rate * channels * sample_bytes, channels * sample_bytes, 16,
    )

    header = list_chunk(
        b"hdrl",
        chunk(b"avih", avih)
        + list_chunk(b"strl", chunk(b"strh", vstrh) + chunk(b"strf", vstrf))
        + list_chunk(b"strl", chunk(b"strh", astrh) + chunk(b"strf", astrf)),
    )

    movi, index, offset = b"", b"", 4
    for _ in range(frames):
        video = chunk(b"00db", frame)
        movi += video
        index += chunk(b"00db", struct.pack("<IIII", 0x10, offset, len(frame), 0))
        offset += len(video)
        audio = chunk(b"01wb", b"\x00" * audio_bytes)
        movi += audio
        index += chunk(b"01wb", struct.pack("<IIII", 0x10, offset, len(audio) - 8, 0))
        offset += len(audio)

    body = header + list_chunk(b"movi", movi) + chunk(b"idx1", index)
    with open(path, "wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", len(body) + 4) + b"AVI " + body)


class HandBrakeEngine:
    """Thin, well-defined wrapper around a real HandBrakeCLI binary."""

    def __init__(self, config: EngineConfig):
        self.config = config
        self._probe_lock = threading.Lock()
        self._probe_cache: tuple[float, str, tuple[str, ...], EngineCapabilities] | None = None

    # -- discovery ---------------------------------------------------------

    def binary_path(self) -> str | None:
        """Return the resolved path to HandBrakeCLI, or None if missing."""

        candidate = self.config.handbrake_bin
        if not candidate:
            return None
        found = shutil.which(candidate)
        if found:
            return found
        # Allow an explicit path even when not on PATH.
        if os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            return candidate
        return None

    def ffprobe_path(self) -> str | None:
        if not self.config.ffprobe_bin:
            return None
        return shutil.which(self.config.ffprobe_bin)

    def probe(self, *, refresh: bool = False) -> EngineCapabilities:
        """Detect the engine and its reported encoders/muxers.

        Results are cached for :data:`PROBE_CACHE_SECONDS` because a full probe
        now runs a tiny encode per unlisted encoder. Pass ``refresh=True`` (the
        UI's "重新检测") to bypass the cache.
        """

        binary = self.binary_path()
        if binary is None:
            with self._probe_lock:
                self._probe_cache = None
            return EngineCapabilities(
                available=False,
                binary=None,
                version=None,
                version_string=None,
                help_available=False,
                presets_available=False,
                notes=[
                    f"HandBrakeCLI not found (looked for {self.config.handbrake_bin!r}). "
                    "Set engine.handbrake_bin or install the engine. "
                    "Encoding is disabled until a real binary is present.",
                ],
            )

        cache_key = (binary, self.config.ffprobe_bin)
        with self._probe_lock:
            cached = self._probe_cache
        if (
            not refresh
            and cached is not None
            and cached[1:3] == cache_key
            and (time.monotonic() - cached[0]) < PROBE_CACHE_SECONDS
        ):
            return cached[3]

        caps = self._probe_uncached(binary)
        with self._probe_lock:
            self._probe_cache = (time.monotonic(), cache_key[0], cache_key[1], caps)
        return caps

    def _probe_uncached(self, binary: str) -> EngineCapabilities:
        version_string = None
        version = None
        notes: list[str] = []

        version_string = None
        version = None
        notes: list[str] = []
        try:
            proc = _run([binary, "--version"], timeout=20)
            version_string = (proc.stdout or proc.stderr or "").strip() or None
            if version_string:
                match = VERSION_RE.search(version_string)
                version = match.group(1) if match else None
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
            notes.append(f"failed to run --version: {exc}")

        help_text = ""
        help_available = False
        try:
            proc = _run([binary, "--help"], timeout=20)
            help_text = (proc.stdout or "") + (proc.stderr or "")
            help_available = proc.returncode == 0 and bool(help_text.strip())
            if not help_available:
                notes.append("--help did not complete successfully")
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            notes.append(f"failed to run --help: {exc}")

        encoders = self._parse_encoders(help_text) if help_available else []
        audio_encoders = self._parse_encoder_section(help_text, "--aencoder") if help_available else []
        muxers = self._parse_muxers(help_text)
        hardware = sorted(
            {e for e in encoders if any(
                e.lower().startswith(p.rstrip("_") + "_") or e.lower().endswith("_" + p.rstrip("_"))
                for p in HARDWARE_ENCODER_PREFIXES
            )}
        )

        presets_available = False
        try:
            presets_available = bool(self.list_presets())
        except (EngineError, OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            notes.append(f"failed to run --preset-list: {exc}")

        # ``--help`` under-reports the encoder table on real builds, so it is
        # only the *candidate* list. What the build really contains is settled
        # by instantiating each candidate.
        instantiable, probe_notes = self._probe_encoder_instantiation(
            binary, encoders, hardware_encoders=hardware,
        )

        return EngineCapabilities(
            available=True,
            binary=binary,
            version=version,
            version_string=version_string,
            help_available=help_available,
            presets_available=presets_available,
            encoders=encoders,
            muxers=muxers,
            hardware_encoders=hardware,
            notes=notes,
            audio_encoders=audio_encoders,
            video_encoders_known=bool(encoders),
            audio_encoders_known=bool(audio_encoders),
            hardware_status=self._parse_hardware_status(help_text),
            video_encoder_probe=instantiable,
            probe_notes=probe_notes,
            decoder_backends=self._parse_decoder_backends(help_text),
        )

    def _probe_encoder_instantiation(
        self,
        binary: str,
        candidates: Sequence[str],
        *,
        hardware_encoders: Sequence[str] = (),
    ) -> tuple[dict[str, str] | None, list[str]]:
        """Classify every candidate encoder by actually instantiating it.

        HandBrake prints the resolved encoder into its job configuration before
        it touches the hardware, so a *failed* encode still proves presence:

        ``works``   the encoder instantiated and the tiny encode completed —
                    the build has it and this machine can run it.
        ``blocked`` the encoder instantiated but the encode failed — the build
                    has it, this machine cannot run it (no GPU, no driver, no
                    device node in the container). Nothing to install.
        ``absent``  the encoder never reached the job configuration — the build
                    really does not contain it.

        ``blocked`` vs ``absent`` is exactly the distinction ``--help`` cannot
        make, and getting it wrong told users to install encoders they already
        had. Returns ``(None, notes)`` when the probe cannot be trusted, so a
        failed probe never becomes "not installed".
        """

        # Probe the spec's own vocabulary, not the help text's: ``--help`` is
        # the very list that under-reports, and the spec's names are what the
        # catalog actually shows. ``candidates`` and ``hardware_encoders`` are
        # unioned in so a build advertising an encoder the spec omits is still
        # measured.
        wanted = set(self._spec_video_encoder_names())
        wanted.update(name for name in candidates if name)
        wanted.update(hardware_encoders)
        if not wanted:
            return None, ["未能确定要检测的视频编码器清单，已跳过实例化检测。"]

        workdir = tempfile.mkdtemp(prefix="cute-cat-probe-")
        clip = os.path.join(workdir, PROBE_CLIP_NAME)
        try:
            _build_probe_clip(clip)
        except OSError as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            return None, [f"无法生成检测用片段，已跳过实例化检测：{exc}"]

        notes: list[str] = []
        try:
            ordered = sorted(wanted)
            with ThreadPoolExecutor(max_workers=PROBE_CONCURRENCY) as pool:
                outcomes = dict(
                    pool.map(
                        lambda name: (
                            name,
                            self._instantiate_encoder(binary, clip, workdir, name),
                        ),
                        ordered,
                    )
                )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        present = {name for name, verdict in outcomes.items() if verdict in ("works", "blocked")}

        control = next((name for name in INSTANTIATION_CONTROL_ENCODERS if name in wanted), None)
        if control is not None and outcomes.get(control) != "works":
            notes.append(
                f"实例化检测未能确认必然存在的 {control}，结果不可信，已忽略；"
                "编码器“未安装”的结论可能不准确。"
            )
            return None, notes

        if not present:
            notes.append("实例化检测没有确认任何视频编码器，结果不可信，已忽略。")
            return None, notes

        return outcomes, notes

    def _instantiate_encoder(self, binary: str, clip: str, workdir: str, encoder: str) -> str:
        """Report ``works`` / ``blocked`` / ``absent`` / ``unknown`` (probe failure)."""

        # ``encoder`` is already a CLI name, so the suffix is only for the file.
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", encoder)
        output = os.path.join(workdir, f"out-{safe}.mkv")
        cmd = [
            binary,
            "-i", clip,
            "-o", output,
            "-f", PROBE_CONTAINER,
            "-e", encoder,
            # One preview, not written to disk: the run stays a single frame
            # while still walking the full "build the job" path that prints the
            # encoder name.
            "--previews", "1:0",
        ]
        try:
            proc = _run(cmd, timeout=PROBE_ENCODER_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        finally:
            # A successful probe writes a real file; never leave it behind.
            try:
                os.unlink(output)
            except OSError:
                pass
        text = (proc.stdout or "") + (proc.stderr or "")
        if not self._parse_job_video_encoder(text):
            return "absent" if re.search(r"Unknown video (?:codec|encoder)", text, re.I) else "unknown"
        # A muxed video track means the encoder ran to completion. Do not use
        # the exit code: a successful x265 encode exits 4.
        return "works" if MUX_VIDEO_TRACK_RE.search(text) else "blocked"

    @classmethod
    def _parse_job_video_encoder(cls, log_text: str) -> str | None:
        """Extract the video encoder from HandBrake's printed job configuration.

        Only the ``* video track`` block is read. The audio block always names
        its own encoder (``AAC (libavcodec)``), and counting that would mark
        every encoder as present.
        """

        video = VIDEO_TRACK_MARKER_RE.search(log_text)
        if video is None:
            return None
        audio = AUDIO_TRACK_MARKER_RE.search(log_text, video.end())
        block = log_text[video.end(): audio.start() if audio else len(log_text)]
        match = TRACK_ENCODER_RE.search(block)
        return match.group(1) if match else None

    @staticmethod
    def _spec_video_encoder_names() -> tuple[str, ...]:
        """Every *CLI* video encoder name the spec layer can emit.

        ``spec`` and ``encoders`` both import this module, so these are imported
        lazily to avoid a circular import. Alias *sources* are dropped and their
        targets kept: the catalog offers ``av1`` but the CLI's encoder is
        ``svt_av1``, and probing the spec's own spelling would report a present
        encoder as absent.
        """

        from .encoders import ALIASES
        from .spec import VIDEO_ENCODERS, VIDEO_HW_ENCODERS

        names = set(VIDEO_ENCODERS) | set(VIDEO_HW_ENCODERS)
        for source, aliases in ALIASES["video"].items():
            names.discard(source)
            names.update(aliases)
        return tuple(sorted(names))

    @staticmethod
    def _parse_encoder_section(help_text: str, flag: str) -> list[str]:
        """Read only the option's enum, never names in prose or diagnostics."""
        lines = help_text.splitlines()
        for index, line in enumerate(lines):
            if not re.search(re.escape(flag) + r"\s+<", line):
                continue
            block = [line]
            for following in lines[index + 1:]:
                if not following.strip() or re.match(r"\s*-", following):
                    break
                block.append(following)
            text = " ".join(block)
            parenthesized = next((group for group in re.findall(r"\(([^()]*)\)", text) if group.strip() != "s"), None)
            if parenthesized:
                tokens = re.split(r"[,\s]+", parenthesized.strip())
            else:
                tokens = []
                for entry in block[1:]:
                    value = entry.strip()
                    if not re.fullmatch(r"[A-Za-z0-9_]+(?::[A-Za-z0-9_]+)?", value):
                        break
                    tokens.append(value)
            return list(dict.fromkeys(t for t in tokens if re.fullmatch(r"[A-Za-z0-9_]+(?::[A-Za-z0-9_]+)?", t)))
        return []

    @classmethod
    def _parse_encoders(cls, help_text: str) -> list[str]:
        return cls._parse_encoder_section(help_text, "--encoder")

    @staticmethod
    def _parse_hardware_status(text: str) -> dict[str, str]:
        status = {}
        for line in text.splitlines():
            match = re.search(r"\b(nvenc|qsv|vce|videotoolbox):\s*(.*)", line, re.IGNORECASE)
            if not match:
                continue
            family, detail = match.group(1).lower(), match.group(2).lower()
            if "not available" in detail or "not compiled" in detail:
                status[family] = "unavailable"
            elif "is available" in detail:
                status[family] = "detected"
        return status

    @staticmethod
    def _parse_decoder_backends(text: str) -> dict[str, dict]:
        result = {}
        for line in text.splitlines():
            match = re.search(r"\b(nvdec|qsv|videotoolbox):\s*(.*)", line, re.I)
            if not match:
                continue
            name, detail = match.group(1).lower(), match.group(2).strip()
            lower = detail.lower()
            status = "unknown"
            if "not compiled" in lower:
                status = "not_compiled"
            elif "not available" in lower:
                status = "unavailable"
            elif re.search(r"\bis available\b", lower):
                status = "reported"
            result[name] = {"status": status, "reason": detail, "source": "HandBrakeCLI diagnostics",
                            "note": "后端诊断，不是逐格式解码实测。"}
        return result

    @staticmethod
    def _parse_muxers(help_text: str) -> list[str]:
        muxers: list[str] = []
        for match in re.finditer(r"(?:^|\s)([a-z0-9_]+)(?=\s*[,)])", help_text):
            token = match.group(1)
            if token in ("mp4", "mkv", "webm", "av_mp4", "av_mkv", "mov", "m4v"):
                muxers.append(token)
        seen: set[str] = set()
        ordered: list[str] = []
        for mux in muxers:
            if mux not in seen:
                seen.add(mux)
                ordered.append(mux)
        return ordered

    # -- presets -----------------------------------------------------------

    @staticmethod
    def _parse_presets(text: str) -> list[dict]:
        """Parse folders, preset names and wrapped descriptions by indentation.

        Real HandBrake emits this tree on stderr, alongside diagnostic lines.
        Also accept slash-qualified names emitted without a tree by older
        adapters. Never treat diagnostics or descriptions as selectable names.
        """

        presets: list[dict] = []
        folders: list[tuple[int, str]] = []
        current: dict | None = None
        preset_indent = -1
        for line in text.splitlines():
            expanded = line.expandtabs(4)
            value = expanded.strip()
            if not value or value.startswith("[") or value == "HandBrake has exited.":
                continue
            indent = len(expanded) - len(expanded.lstrip())
            if value.endswith("/"):
                while folders and folders[-1][0] >= indent:
                    folders.pop()
                folders.append((indent, value[:-1]))
                current = None
                continue
            if current is not None and indent > preset_indent:
                current["description"] = (current["description"] + " " + value).strip()
                continue
            while folders and folders[-1][0] >= indent:
                folders.pop()
            if folders:
                category = "/".join(folder for _, folder in folders)
                name = f"{category}/{value}"
            elif indent == 0 and "/" in value:
                category, _, _ = value.rpartition("/")
                name = value
            else:
                current = None
                continue
            current = {
                "name": name, "source": "official", "raw": value,
                "category": category, "description": "",
            }
            preset_indent = indent
            presets.append(current)
        return presets

    def list_presets(self) -> list[dict]:
        """Return selectable official presets from either CLI output stream."""

        binary = self.binary_path()
        if binary is None:
            raise EngineError("HandBrakeCLI is not available")
        try:
            proc = _run([binary, "--preset-list"], timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise EngineError(f"--preset-list failed: {exc}") from exc
        if proc.returncode != 0:
            raise EngineError(f"--preset-list failed: {(proc.stderr or proc.stdout).strip()}")
        presets: list[dict] = []
        seen: set[str] = set()
        for stream in (proc.stdout or "", proc.stderr or ""):
            for preset in self._parse_presets(stream):
                if preset["name"] not in seen:
                    seen.add(preset["name"])
                    presets.append(preset)
        return presets

    def import_preset_file(self, path: str, *, import_all: bool = False) -> str:
        """Validate a preset JSON file by importing it with the real engine.

        Returns the engine's combined output. Raises :class:`EngineError` when
        the engine rejects the file.
        """

        binary = self.binary_path()
        if binary is None:
            raise EngineError("HandBrakeCLI is not available")
        cmd = [binary, "--preset-import-file", path]
        if import_all:
            cmd.append("-z")
        proc = _run(cmd, timeout=60)
        if proc.returncode != 0:
            raise EngineError((proc.stderr or proc.stdout or "preset import failed").strip())
        return (proc.stdout or "") + (proc.stderr or "")

    def export_preset(self, name: str, document: dict | None = None) -> dict:
        """Resolve a preset with the engine; never rely on a previous CLI process."""
        binary = self.binary_path()
        if binary is None:
            raise EngineError("HandBrakeCLI is not available")
        with tempfile.TemporaryDirectory(prefix="cute-cat-preset-") as directory:
            cmd = [binary]
            if document is not None:
                imported = Path(directory) / "import.json"
                imported.write_text(json.dumps(document), encoding="utf-8")
                cmd.extend(["--preset-import-file", str(imported)])
            cmd.extend(["--preset", name, "--preset-export", "__cute_cat_job__"])
            try:
                proc = _run(cmd, timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                raise EngineError(f"preset resolution failed: {exc}") from exc
            if proc.returncode != 0:
                raise EngineError(f"preset resolution failed: {(proc.stderr or proc.stdout).strip()}")
            for doc in _extract_json_documents(proc.stdout or ""):
                if (isinstance(doc, dict) and isinstance(doc.get("PresetList"), list)
                        and len(doc["PresetList"]) == 1
                        and isinstance(doc["PresetList"][0], dict)
                        and doc["PresetList"][0].get("PresetName") == "__cute_cat_job__"
                        and not doc["PresetList"][0].get("Folder")):
                    return doc
            raise EngineError("preset resolution produced no preset JSON")

    # -- scan --------------------------------------------------------------

    def scan(self, input_path: str, *, timeout: int = 120) -> dict:
        """Run ``--scan --json`` and return parsed title metadata.

        The real CLI emits JSON on stdout for ``--scan --json`` in HandBrake
        1.x. We parse defensively: whole-document JSON first, then
        newline-delimited JSON objects, then raise.
        """

        binary = self.binary_path()
        if binary is None:
            raise EngineError("HandBrakeCLI is not available")
        proc = _run([binary, "-i", input_path, "--scan", "--json"], timeout=timeout)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0 and not stdout.strip():
            raise EngineError(f"scan failed: {stderr.strip() or 'unknown error'}")
        return parse_scan_output(stdout, stderr)

    # -- encode ------------------------------------------------------------

    def build_encode_command(
        self,
        *,
        input_path: str,
        output_path: str,
        args: Sequence[str],
    ) -> list[str]:
        """Assemble the full encode argv from whitelisted ``args``.

        ``args`` must already have been produced by
        :func:`cutecat.spec.build_engine_args`; nothing client-supplied reaches
        this method directly.
        """

        binary = self.binary_path() or self.config.handbrake_bin
        cmd = [binary, "-i", input_path, "-o", output_path]
        cmd.extend(self.config.extra_args)
        from .encoders import cli_encoder

        adapted = list(args)
        if any(flag in adapted for flag in ("-e", "-E", "--audio-fallback")):
            caps = self.probe()
            video_names = list(set(caps.encoders) | set(caps.video_encoder_probe or {}))
            for flag, kind, reported in (("-e", "video", video_names), ("-E", "audio", caps.audio_encoders)):
                if flag in adapted:
                    index = adapted.index(flag) + 1
                    adapted[index] = ",".join(cli_encoder(kind, value, reported) or value for value in adapted[index].split(","))
            # ``--audio-fallback`` takes a single encoder name and is subject to
            # the same naming (``flac`` -> ``flac16``); the CLI rejects the short
            # form here just as it does for ``-E``.
            if "--audio-fallback" in adapted:
                index = adapted.index("--audio-fallback") + 1
                value = adapted[index]
                adapted[index] = cli_encoder("audio", value, caps.audio_encoders) or value
        cmd.extend(adapted)
        return cmd

    def run_encode(
        self,
        *,
        input_path: str,
        output_path: str,
        args: Sequence[str],
        on_event: Callable[[dict], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        on_pause: Callable[[bool], None] | None = None,
        timeout: int = 0,
    ) -> int:
        """Run an encode, streaming ``--json`` progress events to ``on_event``.

        Progress events arrive on stderr as newline-delimited JSON. The process
        is started in its own session so cancellation can signal the whole tree.
        """

        import os
        import signal

        binary = self.binary_path()
        if binary is None:
            raise EngineError("HandBrakeCLI is not available")

        cmd = self.build_encode_command(
            input_path=input_path, output_path=output_path, args=args
        )
        # ``--json`` makes the engine emit machine-readable progress on stderr.
        if "--json" not in cmd:
            cmd.append("--json")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        canceled = threading.Event()
        timed_out = threading.Event()
        control_errors: list[str] = []
        _START_TS[proc.pid] = __import__("time").monotonic()
        tail: list[str] = []

        def _reader() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.rstrip("\n")
                if not line:
                    continue
                tail.append(line)
                if len(tail) > 200:
                    del tail[0]
                event = parse_progress_line(line)
                if event and on_event is not None:
                    try:
                        on_event(event)
                    except Exception:  # pragma: no cover - never let UI break the job
                        pass

        reader = threading.Thread(target=_reader, name="hb-stderr", daemon=True)
        reader.start()

        def _watch_control() -> None:
            paused_at = None
            paused_seconds = 0.0
            while proc.poll() is None:
                if should_cancel is not None and should_cancel():
                    canceled.set()
                    _terminate(proc)
                    return
                pause = bool(should_pause and should_pause())
                try:
                    if pause and paused_at is None:
                        os.killpg(proc.pid, signal.SIGSTOP)
                        paused_at = time.monotonic()
                        if on_pause:
                            on_pause(True)
                    elif not pause and paused_at is not None:
                        os.killpg(proc.pid, signal.SIGCONT)
                        paused_seconds += time.monotonic() - paused_at
                        paused_at = None
                        if on_pause:
                            on_pause(False)
                except ProcessLookupError:
                    return
                elapsed = _elapsed(proc) - paused_seconds - (time.monotonic() - paused_at if paused_at is not None else 0)
                if timeout and elapsed > timeout:
                    timed_out.set()
                    _terminate(proc)
                    return
                threading.Event().wait(0.1)

        def _watch_cancel() -> None:
            try:
                _watch_control()
            except Exception as exc:
                control_errors.append(f"process control failed: {exc}")
                _terminate(proc)

        watcher = threading.Thread(target=_watch_cancel, name="hb-cancel", daemon=True)
        watcher.start()

        try:
            # Drain stdout so the child never blocks on a full pipe.
            if proc.stdout is not None:
                for _ in proc.stdout:
                    pass
            returncode = proc.wait()
            reader.join(timeout=5)
            watcher.join(timeout=1)
        finally:
            # Popen owns both pipe wrappers; close them on every exit path.
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            reader.join(timeout=1)
            watcher.join(timeout=1)
            _START_TS.pop(proc.pid, None)

        if control_errors:
            raise EngineError(control_errors[0])
        if timed_out.is_set():
            raise EngineError(f"encode timed out after {timeout} seconds")
        if canceled.is_set():
            raise EngineError("__canceled__")
        if returncode != 0:
            raise EngineError(
                "encode failed (exit {}): {}".format(
                    returncode, "\n".join(tail[-20:]) or "no output"
                )
            )
        return returncode


_START_TS: dict[int, float] = {}


def _elapsed(proc: subprocess.Popen) -> float:
    import time

    start = _START_TS.setdefault(proc.pid, time.monotonic())
    return time.monotonic() - start


def _terminate(proc: subprocess.Popen) -> None:
    import os
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGCONT)
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


# -- scan output parsing ---------------------------------------------------


def parse_scan_output(stdout: str, stderr: str = "") -> dict:
    """Parse ``--scan --json`` output defensively.

    Accepts either a single JSON document or newline-delimited JSON objects.
    Returns a normalised structure with a ``titles`` list.
    """

    documents = _extract_json_documents(stdout) + _extract_json_documents(stderr)
    if not documents:
        raise EngineError("scan produced no parseable JSON output")

    merged: dict = {"titles": [], "source": {}, "raw_events": documents}
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        if "TitleList" in doc and isinstance(doc["TitleList"], list):
            merged["titles"].extend(doc["TitleList"])
        if "MainFeature" in doc:
            merged["main_feature"] = doc["MainFeature"]
        if "Source" in doc:
            merged["source"] = doc["Source"]
        if "Version" in doc:
            merged["engine_version"] = doc["Version"]
        for key in ("Duration", "Format", "Size", "FrameRate"):
            if key in doc and key not in merged:
                merged[key] = doc[key]

    merged["title_count"] = len(merged["titles"])
    return merged


def _extract_json_documents(text: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    # Whole-document parse first.
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    # Newline-delimited JSON.
    docs: list = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            docs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if docs:
        return docs
    # Brace-matching fallback for pretty-printed concatenated documents.
    return list(_iter_json_objects(text))


def _iter_json_objects(text: str) -> Iterable:
    depth = 0
    start = None
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start : index + 1]
                try:
                    yield json.loads(chunk)
                except json.JSONDecodeError:
                    pass
                start = None


# -- progress parsing ------------------------------------------------------


def parse_progress_line(line: str) -> dict | None:
    """Parse a single ``--json`` progress line from HandBrakeCLI.

    HandBrake emits objects such as::

        {"State":"WORKING","Progress":12.34,"Rate":45.6,"ETA":123,
         "Hours":0,"Minutes":1,"Seconds":2}

    Returns a normalised dict or None if the line is not a progress event.
    """

    line = (line or "").strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    state = obj.get("State") or obj.get("state")
    if state is None and "Progress" not in obj and "progress" not in obj:
        return None

    progress = obj.get("Progress", obj.get("progress"))
    try:
        progress_value = float(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress_value = None

    normalized = {
        "state": state,
        "progress": progress_value,
        "rate": _maybe_float(obj.get("Rate", obj.get("rate"))),
        "eta_seconds": _eta_seconds(obj),
        "raw": obj,
    }
    return normalized


def _maybe_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _eta_seconds(obj: dict) -> int | None:
    if "ETA" in obj:
        try:
            return int(float(obj["ETA"]))
        except (TypeError, ValueError):
            return None
    parts = []
    for key in ("Hours", "Minutes", "Seconds"):
        if key in obj:
            try:
                parts.append(int(obj[key]))
            except (TypeError, ValueError):
                return None
    if parts:
        hours, minutes, seconds = (parts + [0, 0, 0])[:3]
        return hours * 3600 + minutes * 60 + seconds
    return None
