"""Validated, portable task-template bundles. No executable argv or host secrets."""
import copy
import json
import uuid
from .spec import TranscodeSpec, prepare_job_preset
from .presets import resolve_job_preset, validate_preset_document, PresetError
from .builtin_templates import list_templates
from .backends import engine_name, build_args

FIELDS = {"name", "description", "spec", "preset_id", "form_spec", "baseline", "version", "engine"}


def validate_template(payload):
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise ValueError("unknown template field")
    if type(payload.get("version", 1)) is not int or payload.get("version", 1) != 1:
        raise ValueError("unsupported template version")
    engine_name(payload.get("engine"))
    name, description = payload.get("name"), payload.get("description", "")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not isinstance(description, str) or len(description) > 500:
        raise ValueError("invalid template name/description")
    if "form_spec" not in payload:
        raise ValueError("template form_spec is required")
    for key in ("spec", "form_spec", "baseline"):
        if key == "baseline" and payload.get(key) is None:
            continue
        TranscodeSpec.from_dict(payload.get(key, {}))
    return {**copy.deepcopy(payload), "name": name.strip(), "description": description, "version": 1}


class TemplateBundles:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine

    def export(self, payload):
        if set(payload) != {"ids"} or not isinstance(payload["ids"], list) or not 1 <= len(payload["ids"]) <= 100:
            raise ValueError("export requires 1..100 template ids")
        known = {t["id"]: t for t in list_templates(self.store)}
        result = []
        for identity in payload["ids"]:
            if not isinstance(identity, str) or identity not in known:
                raise ValueError("unknown template id")
            template = {k: v for k, v in known[identity].items() if k in FIELDS}
            template = validate_template(template)
            document = None
            if template["spec"].get("preset"):
                if engine_name(template.get("engine")) != "handbrake":
                    raise ValueError("HandBrake preset cannot be exported for a different engine")
                _, _, document = resolve_job_preset(self.store, self.engine, template.get("preset_id"), template["spec"]["preset"])
                presets = validate_preset_document(document)
                if len(presets) != 1:
                    raise ValueError("preset dependency must contain one selectable preset")
                document = copy.deepcopy(presets[0].raw)
                document["PresetList"][0]["PresetName"] = template["spec"]["preset"]
            # Server-local identities are deliberately not portable.
            template.pop("preset_id", None)
            result.append({"template": template, "preset_document": document})
        bundle = {"format": "cute-cat-task-templates", "version": 1, "templates": result}
        if len(json.dumps(bundle).encode()) > 900000:
            raise ValueError("export bundle is too large")
        return bundle

    def import_bundle(self, bundle, *, preview=False):
        if set(bundle) != {"format", "version", "templates"} or bundle["format"] != "cute-cat-task-templates" or type(bundle["version"]) is not int or bundle["version"] != 1:
            raise ValueError("unsupported template bundle")
        entries = bundle["templates"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("bundle must contain 1..100 templates")
        prepared = []
        for index, entry in enumerate(entries):
            try:
                if not isinstance(entry, dict) or set(entry) != {"template", "preset_document"}:
                    raise ValueError("invalid bundle entry")
                template = validate_template(entry["template"])
                if "preset_id" in template:
                    raise ValueError("portable templates must not include server preset ids")
                document = entry["preset_document"]
                name = template["spec"].get("preset")
                if name:
                    if engine_name(template.get("engine")) != "handbrake":
                        raise ValueError("HandBrake preset dependency requires handbrake engine")
                    presets = validate_preset_document(document)
                    if name not in {p.name for p in presets}:
                        raise PresetError("preset dependency does not match template")
                    # Validate using the real engine, not caller-provided argv.
                    if len(presets) != 1:
                        raise ValueError("preset dependency must contain one selectable preset")
                    selected = copy.deepcopy(presets[0].raw)
                    selected["PresetList"][0]["PresetName"] = "__cute_cat_import__"
                    resolved = self.engine.export_preset("__cute_cat_import__", selected)
                    validate_preset_document(resolved)
                elif document is not None:
                    raise ValueError("custom template cannot include a preset dependency")
                spec = TranscodeSpec.from_dict(template["spec"])
                effective = prepare_job_preset(document, template["spec"])
                build_args(engine_name(template.get("engine")), spec, overrides=template["spec"] if document else None, preset=effective)
                prepared.append((template, document))
            except (ValueError, PresetError) as exc:
                raise ValueError(f"template {index + 1}: {exc}") from exc
        if preview:
            return {"count": len(prepared), "names": [t["name"] for t, _ in prepared]}
        return {"templates": self.store.import_template_bundle(prepared)}
