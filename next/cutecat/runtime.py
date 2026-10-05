"""Validated persistent runtime defaults and safe output naming."""
import json
import re
import string
import threading
from pathlib import PurePosixPath
from .pathsafe import normalise_relative
from .builtin_templates import list_templates


class SettingsConflict(ValueError):
    pass


class RuntimeSettings:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = threading.RLock()
        self.active = 0
        self.defaults = {
            "max_concurrent_jobs": min(8, max(1, config.engine.max_concurrent_jobs)),
            "auto_start": True, "job_timeout_seconds": config.engine.job_timeout_seconds,
            "default_output_root": next((r.id for r in config.storage_roots if not r.read_only), ""),
            "output_name_template": "converted/{source}.{ext}", "default_task_template_id": None,
            "output_collision_policy": "reject",
        }
        self.read()

    def read(self):
        with self.lock:
            raw = self.store.get_setting("runtime_settings")
            return {**self.defaults, **(json.loads(raw) if raw else {})}

    def save(self, payload):
        if not isinstance(payload, dict) or set(payload) - set(self.defaults) - {"expected_revision"}:
            raise ValueError("unknown runtime setting")
        with self.lock:
            revision = int(self.store.get_setting("runtime_revision", "0"))
            expected = payload.get("expected_revision", revision)
            if type(expected) is not int or expected != revision:
                raise SettingsConflict("设置已被其他页面修改，请重新读取后保存")
            values = {**self.read(), **{k: v for k, v in payload.items() if k in self.defaults}}
            if values["output_collision_policy"] not in ("reject", "rename"):
                raise ValueError("output collision policy must be reject or rename")
            for key, low, high in (("max_concurrent_jobs", 1, 8), ("job_timeout_seconds", 0, 86400)):
                n = values[key]
                if type(n) is not int or not low <= n <= high:
                    raise ValueError(f"{key} must be {low}..{high}")
            if type(values["auto_start"]) is not bool:
                raise ValueError("auto_start must be boolean")
            roots = {r.id for r in self.config.storage_roots if not r.read_only}
            if values["default_output_root"] not in roots:
                raise ValueError("default output root must be writable")
            validate_name_template(values["output_name_template"])
            identity = values["default_task_template_id"]
            if identity is not None and identity not in {t["id"] for t in list_templates(self.store)}:
                raise ValueError("unknown default task template")
            self.store.set_settings({"runtime_settings": json.dumps(values), "runtime_revision": str(revision + 1)})
            return values

    def claim(self):
        with self.lock:
            if self.active >= self.read()["max_concurrent_jobs"]:
                return None
            job = self.store.claim_next_queued()
            if job:
                self.active += 1
            return job

    def release(self):
        with self.lock:
            self.active = max(0, self.active - 1)

    def report(self):
        with self.lock:
            return {"values": self.read(), "defaults": dict(self.defaults), "active_slots": self.active,
                    "revision": int(self.store.get_setting("runtime_revision", "0")),
                    "source": "SQLite settings override TOML defaults", "has_saved_settings": self.store.get_setting("runtime_settings") is not None}


def validate_name_template(template):
    if not isinstance(template, str) or not template or len(template) > 240:
        raise ValueError("output name template must be 1..240 characters")
    for literal, field, spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in {"source", "encoder", "preset", "ext"} or spec or conversion):
            raise ValueError("unsupported output name placeholder")
    if not normalise_relative(template.format(source="source", encoder="encoder", preset="preset", ext="mp4")):
        raise ValueError("output name must be a file path")


def output_name(template, source, encoder, preset, container):
    validate_name_template(template)
    def safe(value):
        return re.sub(r"[^\w .-]", "_", str(value))[:100].strip(" .") or "custom"
    extension = "mkv" if container in ("mkv", "av_mkv") else "webm" if container == "webm" else "mp4"
    values = {"source": safe(PurePosixPath(str(source).replace("\\", "/")).stem),
              "encoder": safe(encoder), "preset": safe(preset or "custom"), "ext": extension}
    return normalise_relative(template.format(**values))
