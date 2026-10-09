"""Persistent queue with isolated output staging and cancelable encoding."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path

from .config import AppConfig
from .engine import EngineError, HandBrakeEngine
from .presets import resolve_job_preset, validate_preset_document
from .pathsafe import (
    resolve_request, resolve_spec_files, validate_job_paths,
)
from .spec import TranscodeSpec, prepare_job_preset
from .store import Store
from .runtime import RuntimeSettings
from .outputs import allocate_output, pending_paths
from .backends import Backends, engine_name, build_args, admit
from contextlib import contextmanager
import shutil


class Worker:
    def __init__(self, config: AppConfig, store: Store, engine: HandBrakeEngine, runtime=None):
        self.config = config
        self.store = store
        self.engine = engine
        self.runtime = runtime or RuntimeSettings(config, store)
        self.backends = Backends(config, engine, self.runtime)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for index in range(8):
            thread = threading.Thread(target=self._loop, name=f"worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = None
            try:
                job = self.runtime.claim()
            except Exception as exc:  # pragma: no cover - db failure
                self.store.add_log("__system__", "error", f"claim failed: {exc}")
                self._stop.wait(2)
                continue
            if job is None:
                self._stop.wait(1.0)
                continue
            try:
                self._run_job(job.id)
            except Exception as exc:  # pragma: no cover - defensive
                self.store.add_log(job.id, "error", f"worker crashed: {exc}")
                self.store.set_status(job.id, "failed", error=str(exc))
            finally:
                self.runtime.release()

    def _check_cancel(self, job_id: str) -> None:
        if self._stop.is_set() or self.store.cancel_requested(job_id):
            raise EngineError("__canceled__")
        while self.store.pause_requested(job_id):
            self.store.acknowledge_pause(job_id, True)
            if self._stop.wait(0.1) or self.store.cancel_requested(job_id):
                raise EngineError("__canceled__")
        self.store.acknowledge_pause(job_id, False)

    def _run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None or job.status in ("waiting", "succeeded", "failed", "canceled", "interrupted") or (job.status == "paused" and not job.execution_active):
            return
        name = engine_name((job.execution or {}).get("engine"))
        engine = self.backends.get(name, remote_config=(job.execution or {}).get("remote_config"))
        self.store.add_log(job_id, "info", f"job claimed (engine={name})")
        settings = (job.execution or {}).get("settings")
        timeout = settings["job_timeout_seconds"] if settings else self.runtime.read()["job_timeout_seconds"]
        refuse_overwrite = True if settings else self.config.engine.refuse_overwrite
        try:
            roots = list(self.config.storage_roots)
            input_path = resolve_request(roots, job.input_root, job.input_path, require_exists=True)
            output_path = resolve_request(roots, job.output_root, job.output_path)
            validate_job_paths(input_path, output_path)
            spec = TranscodeSpec.from_dict(job.spec)
            files = resolve_spec_files(spec, roots, job.input_root)
            execution = job.execution
            if execution is None:
                # Legacy jobs never had an immutable snapshot. Resolve explicitly,
                # rather than silently treating an imported name as an official one.
                _, _, document = resolve_job_preset(self.store, self.engine, job.preset_id, job.preset_name or spec.preset)
                overrides = job.spec
            else:
                document = execution.get("preset")
                overrides = execution.get("overrides") or {}
            if document:
                validate_preset_document(document)
            document = prepare_job_preset(document, overrides)
            args = build_args(name, spec, overrides=overrides if document else None, preset=document)
            if execution and execution.get("source_policy"):
                from .source_preserve import resolve_source
                policy = TranscodeSpec.from_dict(execution["source_policy"])
                checked, args, snapshot = resolve_source(policy, engine.scan(str(input_path.absolute)))
                if snapshot != execution.get("source_snapshot") or checked.to_dict() != spec.to_dict():
                    raise EngineError("源视频参数在创建后发生变化，请重新创建任务")
            for flag, absolute in files.items():
                if flag == "--markers":
                    index = next(i for i, arg in enumerate(args) if arg.startswith("--markers="))
                    args[index] = "--markers=" + absolute
                else:
                    args[args.index(flag) + 1] = absolute
            self._check_cancel(job_id)

            # Locks live beside the local database, not on an SMB/NFS share.
            # Keep lock files: unlinking one would let waiters lock different inodes.
            lock_dir = Path(self.store.path).resolve().parent / "output-locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(os.fsencode(output_path.absolute)).hexdigest()
            with (lock_dir / key).open("a") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        self._check_cancel(job_id)
                        self._stop.wait(0.1)
                try:
                    self._check_cancel(job_id)
                    if refuse_overwrite and output_path.exists and not (settings and settings["output_collision_policy"] == "rename"):
                        raise EngineError("output already exists")
                    output_path.absolute.parent.mkdir(parents=True, exist_ok=True)
                    # Recheck after mkdir; a changed symlink must not move the staging area.
                    current = resolve_request(roots, job.output_root, job.output_path)
                    validate_job_paths(input_path, current)
                    if current.absolute != output_path.absolute:
                        raise EngineError("output path changed")
                    with self._staging(job, output_path.absolute.parent, remote=name == "rffmpeg") as staging:
                        partial = Path(staging) / output_path.absolute.name
                        # Re-probe after waiting for the output lock: queued tasks
                        # must not reuse submission-time hardware readiness.
                        if execution and execution.get("source_policy"):
                            _, _, snapshot = resolve_source(policy, engine.scan(str(input_path.absolute)))
                            if snapshot != execution.get("source_snapshot"):
                                raise EngineError("等待输出锁期间源参数已改变，请重新创建任务")
                        admit(engine, name, spec, preset=document, overrides=overrides, refresh=True)
                        self._check_cancel(job_id)
                        with tempfile.TemporaryDirectory(prefix="cute-cat-job-preset-") as preset_dir:
                            if document:
                                snapshot = Path(preset_dir) / "job.json"
                                snapshot.write_text(json.dumps(document), encoding="utf-8")
                                if "--preset" not in args:
                                    args[:0] = ["--preset", "__cute_cat_job__"]
                                index = args.index("--preset")
                                args[index + 1] = "__cute_cat_job__"
                                args[index:index] = ["--preset-import-file", str(snapshot)]
                            self._encode_job(job, input_path.absolute, partial, args, timeout, engine=engine)
                        self._check_cancel(job_id)
                        self.store.set_status(job_id, "finalizing")
                        self.store.add_log(job_id, "info", "validating staged output")
                        if not partial.is_file() or partial.stat().st_size == 0:
                            raise EngineError("output validation failed: empty or missing file")
                        scan = engine.scan(str(partial))
                        duration = _first_duration(scan)
                        if not scan.get("titles") or duration is None or duration <= 0:
                            raise EngineError("output validation failed: no playable title with positive duration")
                        self._check_cancel(job_id)
                        current = resolve_request(roots, job.output_root, job.output_path)
                        source = resolve_request(roots, job.input_root, job.input_path, require_exists=True)
                        validate_job_paths(source, current)
                        if current.absolute != output_path.absolute:
                            raise EngineError("output path changed before publication")
                        # Publication and terminal status share the cancellation lock.
                        while True:
                            self._check_cancel(job_id)
                            current = resolve_request(roots, job.output_root, job.output_path)
                            source = resolve_request(roots, job.input_root, job.input_path, require_exists=True)
                            validate_job_paths(source, current)
                            if current.absolute != output_path.absolute:
                                raise EngineError("output path changed before publication")
                            def publish():
                                if not settings:
                                    self._publish(partial, current.absolute)
                                    return
                                def choose(reserved):
                                    pending = pending_paths(self.store, roots, exclude=job_id)
                                    return allocate_output(roots, source, job.output_root, settings["requested_output"], reserved | pending, settings["output_collision_policy"])
                                # Retry only a destination race. Other filesystem
                                # errors must not turn into overwrites or retries.
                                for attempt in range(1000):
                                    try:
                                        target = self.store.publish_allocated(job_id, choose, lambda path: os.link(partial, path.absolute))
                                        return
                                    except FileExistsError:
                                        if settings["output_collision_policy"] != "rename":
                                            raise
                                raise EngineError("output publication collision limit reached")
                            if self.store.complete_job(job_id, publish):
                                break
                        self.store.add_log(job_id, "info", "encode finished")
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception as exc:
            message = str(exc)
            if message == "__canceled__":
                self.store.set_status(job_id, "canceled", error="canceled before publication")
                self.store.add_log(job_id, "info", "encode canceled")
            else:
                self.store.set_status(job_id, "failed", error=message)
                self.store.add_log(job_id, "error", message)
        finally:
            self.store.finish_execution(job_id)

    def _publish(self, partial: Path, output: Path) -> None:
        if self.config.engine.refuse_overwrite:
            # link is atomic and fails if *any* entry appeared at the destination.
            # Fail closed on filesystems without hard links; never fall back to rename.
            os.link(partial, output)
        else:
            os.replace(partial, output)

    @contextmanager
    def _staging(self, job, parent, *, remote=False):
        directory = tempfile.mkdtemp(prefix=f".cute-cat-{job.id}-", dir=parent)
        try:
            yield directory
        finally:
            current = self.store.get_job(job.id)
            if remote and (current is None or current.status != "succeeded"):
                self.store.add_log(job.id, "warn", f"remote exit not confirmed; isolated staging retained: {directory}. Administrator must confirm remote process exit before cleanup.")
            else:
                shutil.rmtree(directory, ignore_errors=True)

    def _encode_job(self, job, input_path: Path, partial: Path, args: list[str], timeout: int, *, engine=None) -> None:
        engine = engine or self.engine
        self.store.set_status(job.id, "probing")
        try:
            scan = engine.scan(str(input_path))
            self.store.update_job(
                job.id, title_count=len(scan.get("titles") or []), duration_seconds=_first_duration(scan),
            )
            self.store.add_log(job.id, "info", "source scan complete")
        except EngineError as exc:
            self.store.add_log(job.id, "warn", f"scan unavailable: {exc}")
        self._check_cancel(job.id)
        self.store.set_status(job.id, "running")
        self._check_cancel(job.id)
        self.store.update_job(job.id, args_json=json.dumps(args))
        self.store.add_log(job.id, "info", "encoding: " + " ".join(engine.build_encode_command(
            input_path=str(input_path), output_path=str(partial), args=args,
        )))

        def on_event(event: dict) -> None:
            if event.get("progress") is not None:
                self.store.update_job(
                    job.id, progress=max(0.0, min(0.99, float(event["progress"]) / 100.0)),
                    speed=str(event["rate"]) if event.get("rate") is not None else None,
                    eta_seconds=event.get("eta_seconds"),
                )

        engine.run_encode(
            input_path=str(input_path), output_path=str(partial), args=args,
            on_event=on_event,
            should_cancel=lambda: self._stop.is_set() or self.store.cancel_requested(job.id),
            timeout=timeout,
            should_pause=lambda: self.store.pause_requested(job.id),
            on_pause=lambda paused: self.store.acknowledge_pause(job.id, paused),
        )


def _first_duration(scan: dict) -> float | None:
    for title in scan.get("titles") or []:
        if not isinstance(title, dict) or "Duration" not in title:
            continue
        try:
            value = title["Duration"]
            if isinstance(value, dict):
                duration = (int(value.get("Hours", 0)) * 3600
                            + int(value.get("Minutes", 0)) * 60 + float(value.get("Seconds", 0)))
            else:
                duration = float(value)
            if math.isfinite(duration):
                return duration
        except (TypeError, ValueError):
            continue
    return None
