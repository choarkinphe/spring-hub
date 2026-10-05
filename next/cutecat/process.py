"""Cancelable argv execution for FFmpeg-compatible programs (no shell)."""
import os
import signal
import subprocess
import threading
import time
import codecs
import selectors
from .engine import EngineError, _terminate


def run_process(cmd, *, on_line=None, should_cancel=None, should_pause=None, on_pause=None, timeout=0, remote=False):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            bufsize=1, start_new_session=True)
    tail, errors = [], []
    canceled, timed_out = threading.Event(), threading.Event()
    start = time.monotonic()
    paused_seconds = [0.0]

    def read(pipe, progress):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        exited_at = None
        def deliver(line):
            if progress and on_line:
                on_line(line.strip())
            else:
                tail.append(line.strip())
                del tail[:-30]
        try:
            # A dead wrapper can leave inherited pipes open in descendants.
            # Bounded nonblocking drain prevents cancellation/shutdown hanging forever.
            os.set_blocking(pipe.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(pipe, selectors.EVENT_READ)
                while True:
                    if proc.poll() is not None:
                        exited_at = exited_at or time.monotonic()
                        if time.monotonic() - exited_at > 2:
                            errors.append("child pipe remained open after process exit")
                            break
                    if not selector.select(.1):
                        continue
                    try:
                        chunk = os.read(pipe.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        pending += decoder.decode(b"", final=True)
                        if pending:
                            deliver(pending)
                        break
                    pending += decoder.decode(chunk)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        deliver(line)
                    if len(pending) > 65536:
                        deliver(pending[:65536])
                        pending = ""
        except Exception as exc:
            errors.append(str(exc))
            _terminate(proc)

    def control():
        paused_at = None
        try:
            while proc.poll() is None:
                if should_cancel and should_cancel():
                    canceled.set()
                    _terminate(proc)
                    return
                pause = bool(not remote and should_pause and should_pause())
                if pause and paused_at is None:
                    os.killpg(proc.pid, signal.SIGSTOP)
                    paused_at = time.monotonic()
                    if on_pause:
                        on_pause(True)
                elif not pause and paused_at is not None:
                    os.killpg(proc.pid, signal.SIGCONT)
                    paused_seconds[0] += time.monotonic() - paused_at
                    paused_at = None
                    if on_pause:
                        on_pause(False)
                elapsed = time.monotonic() - start - paused_seconds[0] - (time.monotonic() - paused_at if paused_at else 0)
                if timeout and elapsed > timeout:
                    timed_out.set()
                    _terminate(proc)
                    return
                threading.Event().wait(.1)
        except ProcessLookupError:
            pass
        except Exception as exc:
            errors.append(str(exc))
            _terminate(proc)

    readers = [threading.Thread(target=read, args=(proc.stdout, True)), threading.Thread(target=read, args=(proc.stderr, False))]
    watcher = threading.Thread(target=control)
    for thread in readers + [watcher]:
        thread.start()
    try:
        code = proc.wait()
        for thread in readers + [watcher]:
            thread.join()
    finally:
        if proc.poll() is None:
            _terminate(proc)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
    if canceled.is_set():
        raise EngineError("__canceled__")
    if timed_out.is_set():
        raise EngineError(f"encode timed out after {timeout} seconds")
    if errors:
        raise EngineError("process control/read failed: " + errors[0])
    if code:
        # Remote diagnostics may contain host/SSH data: keep them out of persisted logs.
        detail = "remote wrapper failed; inspect administrator-side logs" if remote else "\n".join(tail)
        raise EngineError(f"encode failed (exit {code}): {detail}")
    return max(0.0, time.monotonic() - start - paused_seconds[0])
