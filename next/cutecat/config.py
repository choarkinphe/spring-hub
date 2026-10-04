"""Configuration loading (TOML, via the standard-library ``tomllib``).

A single TOML file drives the service. Environment variables can override the
few operational knobs that matter for containers:

* ``SPRINGHUB_CONFIG``  — path to the TOML file (default ``config.toml``).
* ``SPRINGHUB_LISTEN``  — override ``server.listen``.
* ``SPRINGHUB_DATABASE``— override ``server.database``.
* ``SPRINGHUB_ENGINE``  — override ``engine.handbrake_bin``.

The historical ``CUTE_CAT_*`` prefix is accepted when the new key is absent.

Secrets are **never** read from the config file into the image; the optional
API token uses ``SPRINGHUB_API_TOKEN`` (legacy ``CUTE_CAT_API_TOKEN`` accepted).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StorageRoot:
    id: str
    label: str
    path: str
    read_only: bool = False
    mount_marker: str | None = None

    def marker_present(self) -> bool | None:
        """Return marker state: True/False, or None when no marker is configured."""

        if not self.mount_marker:
            return None
        return (Path(self.path) / self.mount_marker).exists()


@dataclass(frozen=True)
class EngineConfig:
    handbrake_bin: str = "HandBrakeCLI"
    ffprobe_bin: str = "ffprobe"
    max_concurrent_jobs: int = 1
    #: Extra fixed arguments appended to every encode, *before* the structured
    #: whitelist. Kept empty by default; never sourced from a request.
    extra_args: tuple[str, ...] = ()
    #: Hard ceiling for a single job's wall-clock runtime in seconds (0 = none).
    job_timeout_seconds: int = 0
    #: When true, refuse to run a job whose output already exists.
    refuse_overwrite: bool = True

    def as_dict(self) -> dict:
        return {
            "handbrake_bin": self.handbrake_bin,
            "ffprobe_bin": self.ffprobe_bin,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "job_timeout_seconds": self.job_timeout_seconds,
            "refuse_overwrite": self.refuse_overwrite,
        }


@dataclass(frozen=True)
class SecurityConfig:
    #: Empty string means "no auth" (local development / trusted admin only).
    api_token: str = ""
    #: Bind address; Compose defaults to loopback.
    listen: str = "0.0.0.0:8080"


@dataclass(frozen=True)
class AppConfig:
    listen: str = "0.0.0.0:8080"
    database: str = "/data/cute-cat.db"
    media_root: str = "/media"
    api_token: str = ""
    storage_roots: tuple[StorageRoot, ...] = ()
    engine: EngineConfig = field(default_factory=EngineConfig)
    #: Directory the built-in web assets are served from (resolved at startup).
    web_dir: str = ""

    @property
    def host(self) -> str:
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])

    def as_public_dict(self) -> dict:
        """Config safe to expose over the API (no token)."""

        return {
            "listen": self.listen,
            "database": self.database,
            "storage_roots": [
                {
                    "id": r.id,
                    "label": r.label,
                    "path": r.path,
                    "read_only": r.read_only,
                    "mount_marker": r.mount_marker,
                    "marker_present": r.marker_present(),
                }
                for r in self.storage_roots
            ],
            "engine": self.engine.as_dict(),
            "auth_required": bool(self.api_token),
        }


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def environment(name: str, default=None, *, allow_empty=False):
    """New public prefix wins; legacy variables remain supported.

    Empty path/address variables use defaults, but an explicitly empty token
    remains meaningful and must not inherit a legacy token accidentally.
    """
    for prefix in ("SPRINGHUB_", "CUTE_CAT_"):
        key = prefix + name
        if key in os.environ:
            value = os.environ[key]
            return value if value or allow_empty else default
    return default


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load configuration from TOML, applying environment overrides."""

    config_path = Path(
        path
        or environment("CONFIG")
        or "config.toml"
    )
    raw: dict = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    elif path is not None:
        # An explicitly requested file that does not exist is an error; the
        # implicit default simply falls back to built-in defaults.
        raise ConfigError(f"config file not found: {config_path}")

    server = raw.get("server", {}) or {}
    engine_raw = raw.get("engine", {}) or {}
    security = raw.get("security", {}) or {}

    listen = environment("LISTEN") or server.get("listen") or "0.0.0.0:8080"
    database = environment("DATABASE") or server.get("database") or "/data/cute-cat.db"
    media_root = environment("MEDIA_ROOT") or server.get("media_root") or "/media"

    roots_raw = raw.get("storage_roots", []) or []
    roots: list[StorageRoot] = []
    seen: set[str] = set()
    for entry in roots_raw:
        root_id = str(entry.get("id", "")).strip()
        if not root_id:
            raise ConfigError("storage root id cannot be empty")
        if root_id in seen:
            raise ConfigError(f"duplicate storage root id: {root_id}")
        seen.add(root_id)
        roots.append(
            StorageRoot(
                id=root_id,
                label=str(entry.get("label", root_id)),
                path=str(entry.get("path", "")),
                read_only=_as_bool(entry.get("read_only"), False),
                mount_marker=entry.get("mount_marker"),
            )
        )

    if not roots:
        # Sensible single-root default so the service still boots in dev.
        roots.append(StorageRoot(id="media", label="媒体目录", path=media_root, read_only=False,
                                 mount_marker=environment("MOUNT_MARKER")))

    extra_args_raw = engine_raw.get("extra_args", []) or []
    if isinstance(extra_args_raw, str):
        extra_args = tuple(extra_args_raw.split())
    else:
        extra_args = tuple(str(a) for a in extra_args_raw)

    engine = EngineConfig(
        handbrake_bin=environment("ENGINE") or engine_raw.get("handbrake_bin") or "HandBrakeCLI",
        ffprobe_bin=engine_raw.get("ffprobe_bin") or "ffprobe",
        max_concurrent_jobs=max(1, int(environment("MAX_JOBS") or engine_raw.get("max_concurrent_jobs", 1) or 1)),
        extra_args=extra_args,
        job_timeout_seconds=int(engine_raw.get("job_timeout_seconds", 0) or 0),
        refuse_overwrite=_as_bool(engine_raw.get("refuse_overwrite"), True),
    )

    api_token = environment("API_TOKEN", security.get("api_token", "") or "", allow_empty=True)

    web_dir = str(raw.get("web", {}).get("dir", "") or "")

    return AppConfig(
        listen=listen,
        database=database,
        media_root=media_root,
        api_token=api_token,
        storage_roots=tuple(roots),
        engine=engine,
        web_dir=web_dir,
    )
