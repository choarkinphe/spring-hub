"""Local FFmpeg and administrator-owned rffmpeg compatible entry points."""
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from .engine import EngineCapabilities, EngineError, _build_probe_clip
from .ffmpeg_spec import VIDEO, AUDIO
from .admission import AdmissionError
from .process import run_process


class InvocationUnknown(EngineError):
    """Timeout or transport failure is not evidence of a blocked codec."""


class FFmpegEngine:
    def __init__(self, config, *, remote=False, roots=()):
        self.config, self.remote, self.roots = config, remote, roots
        self.name = "rffmpeg" if remote else "ffmpeg"
        self._cache = None
        self.decoder_items = []
        self._lock = threading.Lock()

    def binary_path(self):
        return shutil.which(self.config.rffmpeg_bin if self.remote else self.config.ffmpeg_bin)

    def ffprobe_path(self):
        return shutil.which(self.config.rffprobe_bin if self.remote else self.config.ffprobe_bin)

    def _run(self, cmd, timeout=30):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InvocationUnknown(f"{self.name} invocation unavailable") from exc
        if proc.returncode:
            error = InvocationUnknown if self.remote else EngineError
            raise error(f"{self.name} invocation failed (exit {proc.returncode})")
        return proc.stdout

    @staticmethod
    def _inventory(text):
        return {m[1]: m[0][0] for m in re.findall(r"^\s*([VAS][A-Z.]{5})\s+(\S+)\s", text, re.M)}

    def _probe_directory(self):
        if not self.remote:
            return None
        from .pathsafe import resolve_request, PathSafetyError
        path = Path(self.config.remote_probe_dir)
        if not self.config.remote_probe_dir or not path.is_absolute():
            raise EngineError("rffmpeg requires an absolute shared remote_probe_dir")
        for root in self.roots:
            if root.read_only:
                continue
            try:
                rel = path.relative_to(Path(root.path)).as_posix()
                resolved = resolve_request(list(self.roots), root.id, "" if rel == "." else rel, require_exists=True)
                if resolved.is_dir and resolved.absolute == path.resolve():
                    return str(resolved.absolute)
            except (ValueError, PathSafetyError):
                pass
        raise EngineError("remote_probe_dir must exist inside an available writable storage root")

    @contextmanager
    def _probe_scratch(self, directory, verdicts):
        work = tempfile.mkdtemp(prefix=".springhub-codec-", dir=directory)
        completed = False
        try:
            yield work
            completed = True
        finally:
            if not self.remote or (completed and not any(v in ("blocked", "unknown") for v in verdicts.values())):
                shutil.rmtree(work, ignore_errors=True)

    def probe(self, *, refresh=False):
        with self._lock:
            if not refresh and self._cache and time.monotonic() - self._cache[0] < 300:
                return self._cache[1]
            caps = EngineCapabilities(False, self.binary_path(), None, None, False, False)
            if not caps.binary or not self.ffprobe_path():
                caps.notes = [f"{self.name} requires executable ffmpeg and ffprobe compatible entry points"]
                return caps
            try:
                version = self._run([caps.binary, "-version"])
                if not version.startswith("ffmpeg version"):
                    raise EngineError("entry point did not report FFmpeg version")
                caps.available = True
                # Only persist the first version line, not wrapper SSH diagnostics.
                caps.version_string = version.splitlines()[0]
                caps.version = version.split()[2]
                names = self._inventory(self._run([caps.binary, "-hide_banner", "-encoders"]))
                caps.encoders = sorted(n for n, kind in names.items() if kind == "V")
                caps.audio_encoders = sorted(n for n, kind in names.items() if kind == "A")
                caps.video_encoders_known = caps.audio_encoders_known = bool(names)
                caps.help_available = True
                muxers = self._run([caps.binary, "-hide_banner", "-muxers"])
                caps.muxers = re.findall(r"^\s*E\s+(\S+)\s", muxers, re.M)
                if not self.remote:
                    decoder_text = self._run([caps.binary, "-hide_banner", "-decoders"])
                    self.decoder_items = [{"name": n, "kind": {"V":"video", "A":"audio", "S":"subtitle"}[kind]} for n, kind in self._inventory(decoder_text).items()]
                else:
                    # Do not create another remote call merely to enumerate decoders.
                    self.decoder_items = []
                verdicts = {}
                directory = self._probe_directory()
                # Probes use real input paths too: remote lavfi alone would not test shared storage.
                with self._probe_scratch(directory, verdicts) as work:
                    clip = Path(work) / "probe.avi"
                    _build_probe_clip(str(clip))
                    for token, encoder in VIDEO.items():
                        if encoder not in caps.encoders:
                            verdicts[token] = "absent" if names else "unknown"
                            continue
                        output = Path(work) / (token + ".mkv")
                        depth = "yuv420p12le" if "12bit" in token else "yuv420p10le" if "10bit" in token else "yuv420p"
                        command = [caps.binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(clip),
                                   "-map", "0:v:0", "-frames:v", "1", "-an", "-c:v", encoder,
                                   "-pix_fmt", depth, "-threads", "1", "-f", "matroska", str(output)]
                        if encoder == "libsvtav1":
                            command[-1:-1] = ["-svtav1-params", "lp=1"]
                        try:
                            self._run(command, timeout=20)
                            verdicts[token] = "works" if output.is_file() and output.stat().st_size else "blocked"
                        except EngineError as exc:
                            verdicts[token] = "unknown" if isinstance(exc, InvocationUnknown) else "blocked"
                            if self.remote:
                                # A disconnected wrapper may have left a remote encoder running.
                                # Do not start a fresh host call or reuse its scratch files.
                                for remaining in VIDEO:
                                    verdicts.setdefault(remaining, "unknown")
                                break
                    # A failed remote call may leave a writer: preserve its private scratch directory.
                    if self.remote and any(v in ("blocked", "unknown") for v in verdicts.values()):
                        caps.probe_notes.append("Remote probe failure: isolated scratch retained; administrator must confirm remote exit before cleanup.")
                caps.video_encoder_probe = verdicts
                caps.hardware_encoders = [t for t in VIDEO if t.startswith(("nvenc", "qsv", "vce", "vt"))]
                if self.remote:
                    caps.notes.append("rffmpeg compatible wrapper; shared-path probes passed only where marked works. Remote host identity/fallback is managed by administrator; running pause unsupported.")
            except EngineError as exc:
                caps.notes.append(str(exc))
            self._cache = (time.monotonic(), caps)
            return caps

    def admit(self, spec, *, refresh=False):
        if self.remote:
            self._probe_directory()
        caps = self.probe(refresh=refresh)
        if not caps.available:
            raise AdmissionError(f"{self.name} unavailable", status=503)
        verdict = (caps.video_encoder_probe or {}).get(spec.video.encoder)
        if verdict != "works":
            raise AdmissionError(f"{self.name} encoder {spec.video.encoder} readiness: {verdict or 'unknown'}",
                                 status=503 if verdict in (None, "unknown") else 409)
        for track in spec.audio.tracks:
            if track.encoder not in ("none", "copy") and AUDIO[track.encoder] not in caps.audio_encoders:
                raise AdmissionError(f"{self.name} audio encoder {track.encoder} absent")

    def scan(self, input_path, *, timeout=120):
        binary = self.ffprobe_path()
        if not binary:
            raise EngineError(f"{self.name} ffprobe unavailable")
        text = self._run([binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", input_path], timeout)
        try:
            data = json.loads(text)
            streams = data.get("streams", [])
            duration = float(data.get("format", {}).get("duration", 0))
            if not math.isfinite(duration) or duration <= 0 or not any(s.get("codec_type") == "video" for s in streams):
                raise ValueError("no video with positive duration")
            video = next(s for s in streams if s.get("codec_type") == "video")
            return {"titles": [{"Duration": duration, "VideoCodec": video.get("codec_name"),
                "Width": video.get("width"), "Height": video.get("height"), "streams": streams}],
                "title_count": 1, "engine_version": self.name}
        except (ValueError, TypeError, KeyError) as exc:
            raise EngineError("ffprobe produced no usable video metadata") from exc

    def build_encode_command(self, *, input_path, output_path, args):
        return [self.binary_path() or "__missing_ffmpeg__", "-nostdin", "-hide_banner", "-loglevel", "warning",
                "-n", "-i", input_path, *args, "-progress", "pipe:1", "-nostats", output_path]

    def run_encode(self, *, input_path, output_path, args, on_event=None, should_cancel=None,
                   should_pause=None, on_pause=None, timeout=0):
        if not self.binary_path():
            raise EngineError(f"{self.name} unavailable")
        duration = self.scan(input_path)["titles"][0]["Duration"]
        two_pass = "__two_pass__" in args
        args = [x for x in args if x != "__two_pass__"]
        count = 2 if two_pass else 1
        remaining = timeout
        passlog = str(Path(output_path).parent / "passlog")
        for index in range(count):
            values = {}
            def line(text):
                if "=" not in text:
                    return
                key, value = text.split("=", 1)
                values[key] = value
                if key == "progress" and on_event:
                    try:
                        seconds = float(values.get("out_time_us", 0)) / 1000000
                        fraction = min(.99, seconds / duration)
                        speed = float(values.get("speed", "0x").rstrip("x"))
                        on_event({"progress": (index + fraction) * 100 / count, "rate": speed,
                                  "eta_seconds": int(max(0, duration - seconds) / speed) if speed > 0 else None})
                    except ValueError:
                        pass
            current = list(args)
            if two_pass:
                current += ["-pass", str(index + 1), "-passlogfile", passlog]
            cmd = self.build_encode_command(input_path=input_path, output_path=output_path, args=current)
            if two_pass and index == 0:
                # Same video mapping/filter chain; no intermediate media publication.
                cmd[-1:] = ["-an", "-sn", "-f", "null", "-"]
            elapsed = run_process(cmd, on_line=line, should_cancel=should_cancel, should_pause=should_pause,
                                  on_pause=on_pause, timeout=remaining, remote=self.remote)
            if timeout:
                remaining -= elapsed
                if remaining <= 0:
                    raise EngineError(f"encode timed out after {timeout} seconds")
        return 0

    def catalog(self, caps):
        from .spec import VIDEO_ENCODERS, VIDEO_HW_ENCODERS
        result = {"video": [], "audio": []}
        for kind, vocabulary in (("video", {**VIDEO_ENCODERS, **VIDEO_HW_ENCODERS}), ("audio", {**{k: k.upper() for k in AUDIO}, "none": "不输出"})):
            for token, label in vocabulary.items():
                encoder = (VIDEO if kind == "video" else AUDIO).get(token, token)
                verdict = (caps.video_encoder_probe or {}).get(token) if kind == "video" else ("works" if token in ("none", "copy") or encoder in caps.audio_encoders else "absent" if caps.audio_encoders_known else "unknown")
                installed = verdict != "absent" if verdict not in (None, "unknown") else None
                status = "available" if verdict == "works" else "no_hardware" if verdict == "blocked" else "not_installed" if verdict == "absent" else "unknown"
                result[kind].append({"id": token, "name": label, "cli_name": encoder, "installed": installed,
                    "status": status, "selectable": caps.available and verdict == "works", "device": "passthrough" if token in ("none", "copy") else "gpu" if token in VIDEO_HW_ENCODERS else "cpu",
                    "backend": self.name, "description": f"{self.name} / {encoder}",
                    "reason": "微型编码检测通过；不保证所有素材兼容。" if verdict == "works" and kind == "video" else "引擎清单报告，不是实际音轨编码保证。" if verdict == "works" else "构建缺少、实例化失败或检测未知；不自动回退。"})
        return result
