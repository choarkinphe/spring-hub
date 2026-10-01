"""Path safety helpers.

Every filesystem access made on behalf of a client must go through these
helpers. The rules are deliberately strict:

* A client only ever supplies a storage-root id plus a *relative* path.
* Absolute paths, ``..`` segments, drive letters and NUL bytes are rejected
  before any syscall.
* The final path is resolved with :func:`os.path.realpath` and must remain
  inside the resolved storage root. This blocks symlink escapes, including
  symlinks whose *target* is outside the root.

These rules also apply to path-like fields found inside imported preset JSON
(see :mod:`cutecat.presets`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import StorageRoot


class PathSafetyError(ValueError):
    """Raised when a client-supplied path violates the storage-root boundary."""


#: Field names inside imported preset JSON that are treated as paths and must
#: therefore be validated before the preset is accepted.
PRESET_PATH_FIELDS = frozenset(
    {
        "path",
        "file",
        "filename",
        "filepath",
        "file_path",
        "output",
        "output_file",
        "output_path",
        "input",
        "input_path",
        "import_file",
        "preset_import_file",
        "source",
        "destination",
    }
)


@dataclass(frozen=True)
class ResolvedPath:
    """A path that has been proven to live inside a storage root."""

    root: StorageRoot
    relative: str
    absolute: Path

    @property
    def exists(self) -> bool:
        return self.absolute.exists()

    @property
    def is_dir(self) -> bool:
        return self.absolute.is_dir()

    @property
    def is_file(self) -> bool:
        return self.absolute.is_file()

    def as_dict(self) -> dict:
        return {
            "root": self.root.id,
            "relative": self.relative,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "is_file": self.is_file,
        }


def _reject_unsafe_relative(relative: str) -> None:
    if relative is None:
        raise PathSafetyError("path is required")
    if not isinstance(relative, str):
        raise PathSafetyError("path must be a string")
    if "\x00" in relative:
        raise PathSafetyError("path contains a NUL byte")
    # Normalise separators so a Windows-style client cannot smuggle ``\..``.
    normalised = relative.replace("\\", "/")
    if normalised.startswith("/"):
        raise PathSafetyError("absolute paths are not accepted")
    # Reject a Windows drive prefix such as ``C:`` even on POSIX hosts.
    if len(normalised) >= 2 and normalised[1] == ":":
        raise PathSafetyError("drive-qualified paths are not accepted")
    for segment in normalised.split("/"):
        if segment == "..":
            raise PathSafetyError("'..' segments are not accepted")


def normalise_relative(relative: str) -> str:
    """Validate a client relative path and return a canonical POSIX form."""

    _reject_unsafe_relative(relative)
    normalised = relative.replace("\\", "/").strip("/")
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    return "/".join(parts)


def resolve_in_root(root: StorageRoot, relative: str, *, require_exists: bool = False) -> ResolvedPath:
    """Resolve ``relative`` inside ``root``, refusing any escape.

    Symlink escapes are caught because both sides are compared after
    :func:`os.path.realpath`, not after a purely lexical ``normpath``.
    """

    clean = normalise_relative(relative)
    root_real = Path(os.path.realpath(root.path))
    candidate = root_real / clean if clean else root_real
    candidate_real = Path(os.path.realpath(candidate))

    if candidate_real != root_real and root_real not in candidate_real.parents:
        raise PathSafetyError("path escapes the storage root")

    if require_exists and not candidate_real.exists():
        raise PathSafetyError("path does not exist")

    return ResolvedPath(root=root, relative=clean, absolute=candidate_real)


def find_root(roots: list[StorageRoot], root_id: str) -> StorageRoot:
    for root in roots:
        if root.id == root_id:
            return root
    raise PathSafetyError(f"unknown storage root: {root_id!r}")


def resolve_request(roots: list[StorageRoot], root_id: str, relative: str, *, require_exists: bool = False) -> ResolvedPath:
    return resolve_in_root(find_root(roots, root_id), relative, require_exists=require_exists)


def assert_writable(target: ResolvedPath) -> None:
    if target.root.read_only:
        raise PathSafetyError(f"storage root {target.root.id!r} is read-only")


def check_preset_path_field(field_name: str, value: object) -> str | None:
    """Return an error string if a preset path-like field is unsafe.

    Preset JSON is untrusted input. Any field whose name looks like a path is
    required to be a *relative* path with no ``..`` and no leading slash; an
    absolute path would let a crafted preset point the engine at arbitrary
    host files.
    """

    if field_name.lower() not in PRESET_PATH_FIELDS:
        return None
    if value is None:
        return None
    if not isinstance(value, str):
        return f"preset field {field_name!r} must be a string path"
    try:
        _reject_unsafe_relative(value)
    except PathSafetyError as exc:
        return f"preset field {field_name!r} rejected: {exc}"
    return None
