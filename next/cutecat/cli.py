"""Entrypoint: ``python -m cutecat`` or the ``cute-cat-handbrake`` console script.

Boot order:
1. load configuration (TOML + environment overrides);
2. open the SQLite store and mark previously-running jobs as interrupted;
3. probe the real engine (missing binary is reported, not fatal);
4. start the worker pool;
5. serve HTTP until interrupted.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, load_config
from .engine import HandBrakeEngine
from .server import AppState, build_server
from .store import Store
from .worker import Worker


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cutecat", description="Cute Cat (HandBrake) workbench")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--listen", help="override listen address, e.g. 0.0.0.0:8080")
    parser.add_argument("--database", help="override sqlite database path")
    parser.add_argument("--engine", help="override HandBrakeCLI binary path")
    parser.add_argument("--web-dir", help="override web asset directory")
    parser.add_argument("--check", action="store_true", help="print capability report and exit")
    parser.add_argument("--version", action="version", version=f"cute-cat-handbrake {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    # CLI overrides win over both file and environment.
    if args.listen or args.database or args.engine or args.web_dir:
        from dataclasses import replace

        overrides = {}
        if args.listen:
            overrides["listen"] = args.listen
        if args.database:
            overrides["database"] = args.database
        if args.web_dir:
            overrides["web_dir"] = args.web_dir
        if args.engine:
            overrides["engine"] = replace(config.engine, handbrake_bin=args.engine)
        config = replace(config, **overrides)

    engine = HandBrakeEngine(config.engine)

    if args.check:
        caps = engine.probe()
        print(f"cute-cat-handbrake {__version__}")
        print(f"engine available: {caps.available}")
        print(f"binary: {caps.binary}")
        print(f"version: {caps.version_string}")
        print(f"hardware encoders: {', '.join(caps.hardware_encoders) or '(none reported)'}")
        for note in caps.notes:
            print(f"note: {note}")
        return 0 if caps.available else 1

    store = Store(config.database)
    recovered = store.recover_interrupted()
    if recovered:
        print(f"recovered {recovered} interrupted job(s)", file=sys.stderr)

    state = AppState(config, store, engine)
    worker = Worker(config, store, engine, state.runtime)
    worker.start()

    server = build_server(state)
    stop = threading.Event()

    def _shutdown(signum, frame):  # noqa: ANN001
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    caps = engine.probe()
    print(f"cute-cat-handbrake {__version__} listening on http://{config.listen}")
    print(f"database: {config.database}")
    print(f"engine: {caps.binary or 'NOT FOUND'} ({caps.version_string or 'n/a'})")
    if not caps.available:
        print("WARNING: HandBrakeCLI not found — encoding is disabled until it is installed.")

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        worker.stop()
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
