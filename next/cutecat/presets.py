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
    def leaves(entries, category=None):
        for entry in entries:
            if not isinstance(entry, dict):
                raise PresetError("each preset must be an object")
            if entry.get("Folder"):
                children = entry.get("ChildrenArray")
                if not isinstance(children, list):
                    raise PresetError("preset folder requires ChildrenArray")
                yield from leaves(children, entry.get("PresetName") or category)
            else:
                yield entry, category

    seen = set()
    for entry, folder in leaves(preset_list):
        if not isinstance(entry, dict):
            raise PresetError("each preset must be an object")
        name = entry.get("PresetName") or entry.get("Name")
        if not name or not isinstance(name, str):
            raise PresetError("preset is missing 'PresetName'")
        if len(name) > 200 or any(ch in name for ch in ("\x00", "\n", "\r")):
            raise PresetError("invalid preset name")
        if name in seen:
            raise PresetError("duplicate preset names in document")
        seen.add(name)
        if entry.get("PresetDisabled"):
            raise PresetError(f"preset is disabled: {name}")
        category = folder or entry.get("Category") or entry.get("Type")
        presets.append(
            Preset(
                name=name,
                source="imported",
                description=entry.get("Description"),
                category=category if isinstance(category, str) else None,
                raw={**document, "PresetList": [entry]},
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

    if not presets:
        raise PresetError("preset document contains no selectable presets")
    # Import a private, fixed-name copy, not the user's filespec (spaces/globs).
    try:
        for preset in presets:
            document = {**preset.raw, "PresetList": [{**preset.raw["PresetList"][0], "PresetName": "__cute_cat_import__"}]}
            engine.export_preset("__cute_cat_import__", document)
    except EngineError as exc:
        raise PresetError(f"engine rejected the preset file: {exc}") from exc

    for preset in presets:
        preset.file = resolved.relative
        preset.root = root.id
    return presets


def resolve_job_preset(store, engine: HandBrakeEngine, preset_id: str | None,
                       name: str | None) -> tuple[str, str | None, dict | None]:
    """Return an unambiguous identity and engine-normalized job snapshot."""
    if preset_id is not None and not isinstance(preset_id, str):
        raise PresetError("preset_id must be a string")
    if not preset_id and not name or preset_id == "custom" and not name:
        return "custom", None, None
    imported = store.get_preset(preset_id) if preset_id else None
    if imported:
        if imported["source"] != "imported" or name and name != imported["name"]:
            raise PresetError("preset id/name mismatch")
        document = imported["doc"]
        # Compatibility with records imported before full documents were stored.
        if isinstance(document, dict) and "PresetList" not in document:
            document = {"PresetList": [document]}
        presets = validate_preset_document(document)
        if not any(p.name == imported["name"] for p in presets):
            raise PresetError("stored preset name is missing from its document")
        selected = next(p.raw for p in presets if p.name == imported["name"])
        # Avoid collisions with built-in names in HandBrake's preset registry.
        selected = {**selected, "PresetList": [{**selected["PresetList"][0], "PresetName": "__cute_cat_import__"}]}
        resolved = engine.export_preset("__cute_cat_import__", selected)
        validate_preset_document(resolved)
        return imported["id"], imported["name"], resolved
    if preset_id == "custom" or name and preset_id and preset_id != name:
        raise PresetError("preset id/name mismatch")
    selected = name or preset_id
    official = engine.list_presets()
    matches = [p for p in official if selected in (p["name"], p.get("raw"))]
    if len(matches) != 1:
        # Name-only imported references are rejected rather than guessing a file.
        raise PresetError("unknown or ambiguous preset; use an imported preset id")
    selected = matches[0]["name"]
    resolved = engine.export_preset(selected)
    validate_preset_document(resolved)
    return selected, selected, resolved


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
