"""Background worker: a persistent, cancelable, logged job queue.

One worker thread per configured concurrency slot. Each job:

1. is claimed atomically from SQLite (``queued`` -> ``probing``);
2. has its input/output paths re-validated against storage roots;
3. is probed with ``--scan`` (title count / duration captured);
4. is encoded with the real engine, with ``--json`` progress parsed into the
   job row and every stderr line appended to the job log;
5. is finalized (``succeeded`` / ``failed`` / ``canceled``).

Cancellation is cooperative: :meth:`Store.request_cancel` flips a flag that the
engine watcher observes to signal the process group.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .config import AppConfig
from .engine import EngineError, HandBrakeEngine, parse_progress_line
from .pathsafe import PathSafetyError, resolve_request, assert_writable
from .spec import TranscodeSpec, build_engine_args
from .store import Store


class Worker:
    def __init__(self, config: AppConfig, store: Store, engine: HandBrakeEngine):
        self.config = config
        self.store = store
        self.engine = engine
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for index in range(self.config.engine.max_concurrent_jobs):
            thread = threading.Thread(
                target=self._loop, name=f"worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    # -- main loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = None
            try:
                job = self.store.claim_next_queued()
            except Exception as exc:  # pragma: no cover - db failure
                self.store.add_log("__system__", "error", f"claim failed: {exc}")
                time.sleep(2)
                continue
            if job is None:
                self._stop.wait(1.0)
                continue
            try:
                self._run_job(job.id)
            except Exception as exc:  # pragma: no cover - defensive
                self.store.add_log(job.id, "error", f"worker crashed: {exc}")
                self.store.set_status(job.id, "failed", error=str(exc))

    # -- single job --------------------------------------------------------

    def _run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return

        self.store.add_log(job_id, "info", "job claimed")

        # 1. Re-validate paths (defence in depth: they were checked at submit).
        try:
            input_path = resolve_request(
                list(self.config.storage_roots), job.input_root, job.input_path,
                require_exists=True,
            )
            if not input_path.is_file:
                raise PathSafetyError("input is not a file")
            output_path = resolve_request(
                list(self.config.storage_roots), job.output_root, job.output_path,
            )
            assert_writable(output_path)
        except PathSafetyError as exc:
            self.store.add_log(job_id, "error", f"path rejected: {exc}")
            self.store.set_status(job_id, "failed", error=f"path rejected: {exc}")
            return

        if self.config.engine.refuse_overwrite and output_path.absolute.exists():
            self.store.add_log(job_id, "error", "output already exists")
            self.store.set_status(job_id, "failed", error="output already exists")
            return

        output_path.absolute.parent.mkdir(parents=True, exist_ok=True)

        spec = TranscodeSpec.from_dict(job.spec)
        args = build_engine_args(spec)

        # 2. Probe (scan) — captures title count / duration. Non-fatal.
        try:
            self.store.add_log(job_id, "info", "scanning source")
            scan = self.engine.scan(str(input_path.absolute))
            titles = scan.get("titles") or []
            duration = _first_duration(scan)
            self.store.update_job(
                job_id,
                title_count=len(titles),
                duration_seconds=duration,
            )
            self.store.add_log(
                job_id, "info",
                f"scan complete: {len(titles)} title(s)"
                + (f", duration {duration:.1f}s" if duration else ""),
            )
        except EngineError as exc:
            self.store.add_log(job_id, "warn", f"scan unavailable: {exc}")
        except Exception as exc:  # pragma: no cover
            self.store.add_log(job_id, "warn", f"scan failed: {exc}")

        if self.store.cancel_requested(job_id):
            self.store.set_status(job_id, "canceled", error="canceled before encode")
            self.store.add_log(job_id, "info", "canceled before encode")
            return

        # 3. Encode.
        self.store.set_status(job_id, "running")
        self.store.update_job(job_id, args_json=__import__("json").dumps(args))
        self.store.add_log(
            job_id, "info",
            "encoding: " + " ".join(self.engine.build_encode_command(
                input_path=str(input_path.absolute),
                output_path=str(output_path.absolute),
                args=args,
            )),
        )

        def on_event(event: dict) -> None:
            progress = event.get("progress")
            if progress is not None:
                self.store.update_job(
                    job_id,
                    progress=max(0.0, min(1.0, float(progress) / 100.0)),
                    speed=str(event.get("rate")) if event.get("rate") is not None else None,
                    eta_seconds=event.get("eta_seconds"),
                )

        try:
            self.engine.run_encode(
                input_path=str(input_path.absolute),
                output_path=str(output_path.absolute),
                args=args,
                on_event=on_event,
                should_cancel=lambda: self.store.cancel_requested(job_id),
                timeout=self.config.engine.job_timeout_seconds,
            )
        except EngineError as exc:
            message = str(exc)
            if message == "__canceled__":
                self.store.set_status(job_id, "canceled", error="canceled by user")
                self.store.add_log(job_id, "info", "encode canceled")
            else:
                self.store.set_status(job_id, "failed", error=message)
                self.store.add_log(job_id, "error", message)
            _cleanup_partial(output_path.absolute)
            return
        except Exception as exc:  # pragma: no cover
            self.store.set_status(job_id, "failed", error=str(exc))
            self.store.add_log(job_id, "error", str(exc))
            _cleanup_partial(output_path.absolute)
            return

        self.store.set_status(job_id, "succeeded")
        self.store.add_log(job_id, "info", "encode finished")


def _first_duration(scan: dict) -> float | None:
    titles = scan.get("titles") or []
    for title in titles:
        if isinstance(title, dict) and "Duration" in title:
            try:
                value = title["Duration"]
                # HandBrake reports duration as {"Hours":..,"Minutes":..,"Seconds":..}
                if isinstance(value, dict):
                    return (
                        int(value.get("Hours", 0)) * 3600
                        + int(value.get("Minutes", 0)) * 60
                        + int(value.get("Seconds", 0))
                    )
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _cleanup_partial(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:  # pragma: no cover
        pass
