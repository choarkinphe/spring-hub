"""One spec/template preparation path for validation, submission and execution."""
import copy
from .spec import TranscodeSpec, SpecError, prepare_job_preset
from .presets import resolve_job_preset
from .pathsafe import resolve_spec_files
from .builtin_templates import find_template
from .backends import engine_name, build_args


def merge_spec(base, override):
    if not isinstance(override, dict):
        raise SpecError("spec overrides must be an object")
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_spec(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def request_spec(store, payload):
    template = None
    if payload.get("template_id") is not None:
        template = find_template(store, payload["template_id"])
        if template is None:
            raise SpecError("unknown task template")
    name = engine_name(payload.get("engine", template.get("engine") if template else None))
    if template and not template.get("builtin") and name != engine_name(template.get("engine")):
        raise SpecError("user template belongs to a different engine; copy and validate it first")
    mode = payload.get("spec_mode", "merge")
    if mode not in ("merge", "replace"):
        raise SpecError("spec_mode must be merge or replace")
    raw = merge_spec(template["spec"], payload.get("spec", {})) if template and mode == "merge" else payload.get("spec", {})
    identity = payload.get("preset_id", template.get("preset_id") if template else None)
    return name, raw, identity, template


def prepare(config, store, backends, raw, preset_id=None, *, name="handbrake", input_root=None):
    name = engine_name(name)
    spec = TranscodeSpec.from_dict(raw)
    if input_root is not None:
        resolve_spec_files(spec, list(config.storage_roots), input_root)
    if name == "handbrake":
        identity, preset_name, document = resolve_job_preset(store, backends.get(name), preset_id, spec.preset)
        document = prepare_job_preset(document, raw)
        spec.preset = preset_name
    else:
        if spec.preset or preset_id not in (None, "custom"):
            raise SpecError("HandBrake preset cannot be used with FFmpeg/rffmpeg")
        identity, preset_name, document = "custom", None, None
    args = build_args(name, spec, overrides=raw if document else None, preset=document)
    return spec, identity, preset_name, document, args
