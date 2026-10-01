"""Preset handling: official presets and imported preset JSON.

Two sources of presets:

1. **Official** — read from the real engine via ``--preset-list``. Names are
   used verbatim with ``--preset``.
2. **Imported** — a preset JSON file that already exists inside a configured
   storage root. The file is validated before use:
   * it must be valid JSON with a ``PresetList`` array (HandBrake's schema);
   * every path-like field inside it must be a *relative* path with no ``..``
     and no leading slash (see :mod:`cutecat.pathsafe`);
   * the real engine is asked to import it (``--preset-import-file``) so a
     malformed file is rejected by the engine, not just by us.

Nothing is ever downloaded; presets come from the local filesystem or the
local engine only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig, StorageRoot
from .engine import EngineError, HandBrakeEngine
from .pathsafe import check_preset_path_field, resolve_in_root


class PresetError(ValueError):
    pass


@dataclass
class Preset:
    name: str
    source: str  # "official" | "imported"
    description: str | None = None
    category: str | None = None
    file: str | None = None  # relative path within a storage root
    root: str | None = None
    raw: dict | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "category": self.category,
            "file": self.file,
            "root": self.root,
        }


def _walk_json(value, path: str = "$"):
    """Yield ``(field_name, field_value, json_path)`` for every dict entry."""

    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item, f"{path}.{key}"
            yield from _walk_json(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_json(item, f"{path}[{index}]")


def validate_preset_document(document: object) -> list[Preset]:
    """Validate a parsed preset JSON document and return its presets.

    Rejects the document if any path-like field escapes the sandbox.
    """

    if not isinstance(document, dict):
        raise PresetError("preset file must contain a JSON object")
    preset_list = document.get("PresetList")
    if preset_list is None:
        raise PresetError("preset file is missing the 'PresetList' array")
    if not isinstance(preset_list, list):
        raise PresetError("'PresetList' must be an array")

    # Path safety: scan the whole document, not only the preset names.
    for field_name, value, json_path in _walk_json(document):
        problem = check_preset_path_field(field_name, value)
        if problem:
            raise PresetError(f"{problem} (at {json_path})")

    presets: list[Preset] = []
    for entry in preset_list:
        if not isinstance(entry, dict):
            raise PresetError("each preset must be an object")
        name = entry.get("PresetName") or entry.get("Name")
        if not name or not isinstance(name, str):
            raise PresetError("preset is missing 'PresetName'")
        category = entry.get("Category") or entry.get("Type")
        presets.append(
            Preset(
                name=name,
                source="imported",
                description=entry.get("Description"),
                category=category if isinstance(category, str) else None,
                raw=entry,
            )
        )
    return presets


def load_imported_presets(config: AppConfig, engine: HandBrakeEngine, root: StorageRoot, relative: str) -> list[Preset]:
    """Load and validate a preset JSON file that lives inside ``root``."""

    resolved = resolve_in_root(root, relative, require_exists=True)
    if not resolved.is_file:
        raise PresetError(f"preset path is not a file: {relative}")
    try:
        raw_text = resolved.absolute.read_text(encoding="utf-8")
    except OSError as exc:
        raise PresetError(f"cannot read preset file: {exc}") from exc

    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PresetError(f"preset file is not valid JSON: {exc}") from exc

    presets = validate_preset_document(document)

    # Ask the real engine to import it too, so the engine's own schema check
    # runs. If the engine is unavailable this is skipped (and noted).
    try:
        engine.import_preset_file(str(resolved.absolute), import_all=False)
    except EngineError as exc:
        raise PresetError(f"engine rejected the preset file: {exc}") from exc

    for preset in presets:
        preset.file = resolved.relative
        preset.root = root.id
    return presets


def discover_preset_files(roots: tuple[StorageRoot, ...]) -> list[dict]:
    """List candidate ``*.json`` preset files inside storage roots (read-only)."""

    found: list[dict] = []
    for root in roots:
        base = Path(root.path)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            # Never follow symlinks out of the root.
            try:
                if path.is_symlink():
                    continue
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            found.append({"root": root.id, "relative": rel, "name": path.name})
    return found
