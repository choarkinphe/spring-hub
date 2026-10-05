"""HTTP API + static file server (standard library only).

Routes
------
Pages
    GET  /                      -> web workbench
    GET  /assets/<file>         -> static assets (web/ directory)

Meta
    GET  /health/live
    GET  /health/ready
    GET  /api/v1/capabilities   -> engine + config + feature matrix
    GET  /api/v1/config         -> public config (never the token)
    GET|POST /api/v1/settings   -> persistent runtime defaults
    GET|POST /api/v1/task-templates -> reusable structured encoding settings
    POST /api/v1/output-name    -> safe relative naming expansion
    GET  /api/v1/system/status  -> read-only service-visible resource metrics

Storage
    GET  /api/v1/storage-roots
    GET  /api/v1/storage-roots/<id>/entries?path=<rel>

Presets
    GET  /api/v1/presets                 -> official + imported
    POST /api/v1/presets/import          -> import a preset JSON from a root

Probe
    POST /api/v1/probe                   -> scan a file, return titles
    POST /api/v1/spec/validate           -> non-persistent spec/preset prevalidation

Jobs
    GET  /api/v1/jobs
    POST /api/v1/jobs
    GET  /api/v1/jobs/<id>
    POST /api/v1/jobs/<id>/cancel
    GET  /api/v1/jobs/<id>/logs

Auth: when ``security.api_token`` is set, every ``/api/v1/*`` request must send
``Authorization: Bearer <token>`` or ``X-API-Token: <token>``.
"""

from __future__ import annotations

import json
import mimetypes
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig
from .engine import EngineError, HandBrakeEngine
from .admission import AdmissionError
from .pathsafe import PathSafetyError, resolve_request, resolve_spec_files, validate_job_paths, assert_writable
from .presets import (
    PresetError,
    discover_preset_files,
    load_imported_presets,
    resolve_job_preset,
)
from .encoders import AUDIO_CATALOG_ONLY
from .spec import (
    AUDIO_ENCODERS,
    AUDIO_MIXDOWNS,
    DENOISE,
    DEINTERLACE,
    FRAMERATES,
    SpecError,
    SUBTITLE_BEHAVIORS,
    TranscodeSpec,
    VIDEO_CONTAINERS,
    VIDEO_ENCODERS,
    VIDEO_HW_ENCODERS,
    VIDEO_LEVELS,
    VIDEO_PRESETS,
    VIDEO_PROFILES,
    VIDEO_QUALITY_TYPES,
    VIDEO_TUNES,
)
from .store import Store
from .system_status import SystemStatus
from .decoders import decoder_inventory
from .runtime import RuntimeSettings, SettingsConflict, output_name
from .outputs import allocate_output, pending_paths, OutputConflict
from .maintenance import Maintenance
from .templates import TemplateBundles, validate_template
from .builtin_templates import list_templates, find_template
from .backends import Backends, engine_name, admit
from .preparation import request_spec, prepare
from .remote_settings import RemoteCheck

MAX_BODY_BYTES = 1_000_000


class ApiError(Exception):
    def __init__(self, status: int, message: str, *, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class AppState:
    def __init__(self, config: AppConfig, store: Store, engine: HandBrakeEngine):
        self.config = config
        self.store = store
        self.engine = engine
        self.runtime = RuntimeSettings(config, store)
        self.backends = Backends(config, engine, self.runtime)
        self.remote_check = RemoteCheck()
        self.maintenance = Maintenance(store)
        self.templates = TemplateBundles(store, engine)
        self.system_status = SystemStatus(config)
        self.web_dir = Path(config.web_dir) if config.web_dir else Path(__file__).resolve().parent.parent / "web"


# -- helpers ---------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not serializable: {type(value)!r}")


def _spec_options() -> dict:
    return {
        "video_encoders": VIDEO_ENCODERS,
        "video_hw_encoders": VIDEO_HW_ENCODERS,
        "video_quality_types": sorted(VIDEO_QUALITY_TYPES),
        "video_presets": sorted(VIDEO_PRESETS),
        "video_tunes": sorted(VIDEO_TUNES),
        "video_profiles": sorted(VIDEO_PROFILES),
        "video_levels": sorted(VIDEO_LEVELS),
        "framerates": sorted(FRAMERATES),
        "containers": sorted(VIDEO_CONTAINERS),
        "audio_encoders": sorted(AUDIO_ENCODERS),
        "audio_mixdowns": sorted(AUDIO_MIXDOWNS),
        "deinterlace": sorted(DEINTERLACE),
        "denoise": sorted(DENOISE),
        "subtitle_behaviors": sorted(SUBTITLE_BEHAVIORS - {"add"}),
        "ui_constraints": {
            "bitrate_quality_types": ["abr", "vbr"],
            "lossless_encoders": ["x264", "x264_10bit", "x265", "x265_10bit", "x265_12bit"],
            "anamorphic": ["auto", "none", "loose"],
            "deinterlace": ["off", "skip-spatial", "default", "bob"],
            "denoise": ["off", "nlmeans", "hqdn3d"],
            "detelecine": ["off", "default"],
            "source_subtitle_limit": 1,
        },
    }


def _feature_matrix() -> list[dict]:
    """Explicit implemented / not-implemented matrix, surfaced in the UI.

    ``status`` is one of: ``implemented``, ``unverified``, ``blocked``,
    ``disabled``. Nothing is marked ``implemented`` unless it is covered by a
    unit/integration test *and* maps to a real engine call; hardware paths are
    ``unverified`` because no GPU was available to test.
    """

    features = [
        {"area": "Service resources + codec report", "status": "implemented",
         "note": "Read-only CPU/memory/GPU/disk metrics for the service-visible environment; unknowns stay unknown. "
                 "Encoder presence reuses the engine probe; decoder diagnostics and optional independent FFmpeg inventory "
                 "are labelled by provider, not an all-format compatibility guarantee."},
        {"area": "Native web UI (no noVNC)", "status": "implemented",
         "note": "Hand-written HTML/CSS/JS workbench served from web/."},
        {"area": "Real HandBrakeCLI invocation", "status": "verified",
         "note": "Real HandBrakeCLI 1.11 CPU tasks verified, including x264/x265 preset encoding; "
                 "not every encoder/parameter combination is verified."},
        {"area": "Scan / title JSON parsing", "status": "implemented",
         "note": "Defensive parser for --scan --json; unit-tested against "
                 "representative payloads."},
        {"area": "Official preset loading (--preset-list)", "status": "verified",
         "note": "Real preset enumeration, normalization and x264 output verified."},
        {"area": "Preset JSON import + path-field safety", "status": "verified",
         "note": "UUID identity and immutable task snapshot, imported in the actual encode process; "
                 "real x265 output and deleted-source behavior verified."},
        {"area": "Encoder admission", "status": "implemented",
         "note": "Submission checks effective encoders; execution refreshes detection. "
                 "Video requires a successful tiny probe; audio uses the engine list. No CPU fallback."},
        {"area": "Structured parameter whitelist", "status": "implemented",
         "note": "Typed spec -> argv; unknown/unsupported values rejected. Real 1.11 CPU contract checks cover "
                 "geometry, rotation, filters, color, x264/x265 lossless, audio sample rates and source/SRT subtitle indexes. "
                 "No visual-quality or all-encoder guarantee."},
        {"area": "Parameter prevalidation + UI constraints", "status": "implemented",
         "note": "Standalone validation without a source; queue submission validates the same captured spec/UUID first. "
                 "Checks structure, preset resolution and CLI mapping only; no paths, source tracks, admission or encoding. "
                 "Preset argv contains overrides, not the full preset command. UI constrains coupled controls; "
                 "preset/tune/profile/level remain protocol choices, not per-encoder compatibility detection."},
        {"area": "Persistent queue / controls / settings", "status": "implemented",
         "note": "Start/retry, process-group pause/resume, record-only delete and dynamic concurrency. "
                 "Paused processes retain slots; restart cannot resume in-memory progress. "
                 "Persistent task templates and safe output naming; no media deletion or automatic overwrite."},
        {"area": "Path safety (root escape / symlink)", "status": "implemented",
         "note": "realpath containment checks; unit-tested."},
        {"area": "Workbench tabs (Summary/Dimensions/Filters/Video/Audio/Subtitles/Chapters)",
         "status": "implemented", "note": "All seven tabs rendered and bound to the spec."},
        {"area": "Live preview / thumbnails", "status": "disabled",
         "note": "No preview renderer is implemented; the tab is disabled in "
                 "the UI rather than faked."},
        {"area": "Hardware encoders (NVENC/QSV/VCE/VideoToolbox/VAAPI/AMF)",
         "status": "unverified",
         "note": "Admission requires a working instantiation probe, not just a help listing. "
                 "No usable GPU here; blocked NVENC returns 409. Actual GPU jobs remain unverified."},
        {"area": "SMB/NFS mount inside container", "status": "disabled",
         "note": "Mounts are performed by the host / Docker local volume; the "
                 "app never mounts network shares."},
        {"area": "Docker image + Compose", "status": "unverified",
         "note": "Files provided; no container runtime was available to build "
                 "or run them."},
        {"area": "Disc menus / multi-node scheduling", "status": "disabled",
         "note": "Out of scope for this version."},
    ]
    chinese = [
        ("resources", "资源与编解码器报告", "只读采集服务可见的处理器、内存、显卡与磁盘信息。未知不填零；编码器提供情况和运行可用性分开，不保证全部素材兼容。"),
        ("web", "原生网页工作台", "原生网页，不依赖远程桌面或前端框架。"),
        ("engine", "真实 HandBrake 引擎", "已验证 HandBrake 1.11 的部分处理器转码和预设任务；未验证所有编码器与参数组合。"),
        ("scan", "源文件扫描", "解析媒体标题与轨道信息，已有代表性数据回归测试。"),
        ("presets", "官方预设", "真实预设列表、规范化与部分转码产物已验证。"),
        ("preset_import", "预设导入与路径保护", "预设身份和任务快照保持独立，已验证导入预设转码；原预设文件变化不影响已创建任务。"),
        ("admission", "编码器准入检查", "提交时检查有效编码器，执行前重新检测；不可用时拒绝，不自动切回软件编码。"),
        ("spec", "结构化参数白名单", "拒绝未知或不支持的参数；部分尺寸、滤镜、颜色、无损和音轨组合已实测，不保证所有组合。"),
        ("validation", "参数预校验与控件联动", "无需选择源文件即可预校验结构、预设和命令映射；不验证文件存在性、轨道、硬件或实际编码。"),
        ("queue", "任务队列、控制与设置", "支持启动、重试、暂停、继续和动态并发；记录清理不删除媒体。设置、模板、输出编号与完成事件已实现。"),
        ("paths", "存储路径安全", "检查真实路径包含关系与软链接逃逸，已有回归测试。"),
        ("tabs", "七标签编码设置", "摘要、尺寸、滤镜、视频、音频、字幕和章节均与结构化参数绑定。"),
        ("preview", "画面预览与缩略图", "尚未实现画面渲染，入口停用，不用模拟画面代替。"),
        ("hardware", "硬件编码", "准入要求微型实例化检测成功。本机缺少可用硬件编码环境，完整显卡转码仍未验证。"),
        ("mounts", "容器内网络共享挂载", "由宿主机或容器部署挂载网络共享，应用不执行挂载。"),
        ("docker", "容器镜像与部署", "已提供部署文件；当前无容器运行环境，未完成镜像构建和运行验证。"),
        ("advanced", "光盘菜单与多节点调度", "本版本不提供这些功能。"),
    ]
    result = [{**feature, "id": identity, "area_zh": area, "note_zh": note}
              for feature, (identity, area, note) in zip(features, chinese)]
    result.append({"id": "playback", "area": "Playback templates and external API/CLI", "area_zh": "播放模板与外部调用",
        "status": "implemented", "note": "Nine read-only MP4 templates; local FFmpeg and administrator-owned rffmpeg compatible entry points. HTTP/CLI share queue and safe publication.",
        "note_zh": "9 类内置 MP4 模板、本机 FFmpeg 和外部 HTTP/CLI 已接入；rffmpeg 使用管理员配置的共享路径 wrapper，运行态暂停不支持，取消不证明远端退出。"})
    return result


# -- request handler -------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"SpringHub/{__version__}"
    state: AppState  # set on the server class

    # -- logging -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Keep the default access log but route it through the standard logger
        # rather than stderr writes that mix with engine output.
        pass

    # -- auth --------------------------------------------------------------

    def _authorized(self) -> bool:
        token = self.state.config.api_token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            if header[7:].strip() == token:
                return True
        if self.headers.get("X-API-Token", "") == token:
            return True
        return False

    # -- verb dispatch -----------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/health/live", "/health/ready"):
                self._send_json(200, {"status": "ok", "engine": self.state.engine.binary_path()})
                return
            if path.startswith("/api/"):
                if not self._authorized():
                    raise ApiError(401, "unauthorized")
                body = self._read_body() if method == "POST" else None
                payload = self._route_api(method, path, query, body)
                self._send_json(200, payload)
                return
            self._route_static(method, path)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message, "detail": exc.detail})
        except BrokenPipeError:  # pragma: no cover
            pass
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._send_json(500, {"error": "internal error", "detail": str(exc)})

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "request body too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "request body must be a JSON object")
        return data

    # -- API routes --------------------------------------------------------

    def _route_api(self, method: str, path: str, query: dict, body: dict | None) -> Any:
        cfg = self.state.config
        store = self.state.store

        # Detection runs a tiny encode per encoder, so it is cached; the UI's
        # "重新检测" passes refresh=1 to force a real re-probe.
        refresh = (query.get("refresh") or [""])[0] in ("1", "true", "yes")

        if path == "/api/v1/settings":
            if method == "GET":
                return self.state.runtime.report()
            if method == "POST":
                try:
                    self.state.runtime.save(body or {})
                except SettingsConflict as exc:
                    raise ApiError(409, str(exc)) from exc
                except (ValueError, PathSafetyError) as exc:
                    raise ApiError(400, str(exc)) from exc
                return self.state.runtime.report()

        if path == "/api/v1/rffmpeg/check" and method == "POST":
            if body:
                raise ApiError(400, "配置检查仅使用已保存配置，不接受程序路径或命令参数")
            values = self.state.backends.remote_settings()
            engine = self.state.backends.get("rffmpeg", remote_config=values)
            try:
                result = self.state.remote_check.run(engine.config, cfg.storage_roots)
            except RuntimeError as exc:
                raise ApiError(409, str(exc)) from exc
            if values != self.state.backends.remote_settings():
                raise ApiError(409, "检查期间 rffmpeg 配置已修改，请重新检查")
            return result

        if path == "/api/v1/output-name" and method == "POST":
            payload = body or {}
            if set(payload) - {"source", "encoder", "preset", "container", "template"}:
                raise ApiError(400, "unknown naming field")
            try:
                return {"path": output_name(payload.get("template", self.state.runtime.read()["output_name_template"]),
                    payload.get("source", "source"), payload.get("encoder", "encoder"), payload.get("preset"), payload.get("container", "auto"))}
            except (ValueError, PathSafetyError) as exc:
                raise ApiError(400, str(exc)) from exc

        if path == "/api/v1/job-events" and method == "GET":
            try:
                after = int(query["after"][0]) if "after" in query else None
                limit = int(query.get("limit", ["100"])[0])
                if after is not None and after < 0 or not 1 <= limit <= 200:
                    raise ValueError("invalid cursor/limit")
            except (ValueError, IndexError) as exc:
                raise ApiError(400, "invalid event cursor/limit") from exc
            return store.event_report(after, limit)

        if path in ("/api/v1/maintenance/preview", "/api/v1/maintenance/cleanup") and method == "POST":
            try:
                return (self.state.maintenance.preview if path.endswith("preview") else self.state.maintenance.cleanup)(body or {})
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc

        if path == "/api/v1/task-templates/export" and method == "POST":
            try:
                return self.state.templates.export(body or {})
            except (ValueError, SpecError, PresetError) as exc:
                raise ApiError(400, str(exc)) from exc
        if path in ("/api/v1/task-templates/import-preview", "/api/v1/task-templates/import") and method == "POST":
            try:
                return self.state.templates.import_bundle(body or {}, preview=path.endswith("import-preview"))
            except (ValueError, SpecError, PresetError) as exc:
                raise ApiError(400, str(exc)) from exc

        match_template = re.fullmatch(r"/api/v1/task-templates(?:/([0-9a-fA-F\-]+))?", path)
        if match_template:
            identity = match_template.group(1)
            templates = list_templates(store)
            if identity and not any(t["id"] == identity for t in templates):
                raise ApiError(404, "task template not found")
            if method == "GET" and not identity:
                return {"templates": templates}
            if identity and find_template(store, identity).get("builtin") and method in ("POST", "DELETE"):
                raise ApiError(409, "built-in templates are read-only; copy to a user template")
            if method == "DELETE" and identity:
                with self.state.runtime.lock:
                    store.delete_template(identity)
                return {"deleted": True}
            if method == "POST":
                try:
                    payload = validate_template(body or {})
                except (ValueError, SpecError) as exc:
                    raise ApiError(400, str(exc)) from exc
                self._prepare_spec(payload.get("spec", {}), payload.get("preset_id", "custom"), name=engine_name(payload.get("engine")))
                return store.save_template(payload, identity)

        if path == "/api/v1/system/status" and method == "GET":
            return self.state.system_status.snapshot()

        if path == "/api/v1/capabilities" and method == "GET":
            caps = self.state.engine.probe(refresh=refresh)
            return {
                "version": __version__,
                "engine": caps.as_dict(),
                "ffprobe": self.state.engine.ffprobe_path(),
                "config": {**cfg.as_public_dict(), "effective_rffmpeg": self.state.backends.remote_settings()},
                "engine_ids": ["handbrake", "ffmpeg", "rffmpeg"],
                "spec_options": _spec_options(),
                "encoder_catalog": self._encoder_catalog(caps),
                "decoder_inventory": decoder_inventory(refresh),
                "features": _feature_matrix(),
            }

        if path == "/api/v1/engines" and method == "GET":
            return self.state.backends.report(refresh)

        if path == "/api/v1/encoders" and method == "GET":
            try:
                name = engine_name((query.get("engine") or [None])[0])
            except SpecError as exc:
                raise ApiError(400, str(exc)) from exc
            engine = self.state.backends.get(name)
            caps = engine.probe(refresh=refresh)
            return {"engine_id": name, "engine": caps.as_dict(), "encoder_catalog": self._encoder_catalog(caps) if name == "handbrake" else engine.catalog(caps),
                    "decoder_inventory": decoder_inventory(refresh) if name == "handbrake" else {"status": "reported" if engine.decoder_items else "unknown", "items": engine.decoder_items, "note": "当前所选引擎的构建清单；源格式支持以实际扫描为准，不代表 HandBrake 内置库。"}}

        if path == "/api/v1/config" and method == "GET":
            return {**cfg.as_public_dict(), "effective_rffmpeg": self.state.backends.remote_settings()}

        if path == "/api/v1/storage-roots" and method == "GET":
            return {
                "roots": [
                    {
                        "id": r.id,
                        "label": r.label,
                        "path": r.path,
                        "read_only": r.read_only,
                        "mount_marker": r.mount_marker,
                        "marker_present": r.marker_present(),
                        "available": Path(r.path).is_dir(),
                    }
                    for r in cfg.storage_roots
                ]
            }

        match = re.fullmatch(r"/api/v1/storage-roots/([^/]+)/entries", path)
        if match and method == "GET":
            return self._list_entries(match.group(1), query)

        if path == "/api/v1/presets" and method == "GET":
            return self._list_presets()

        if path == "/api/v1/presets/import" and method == "POST":
            return self._import_preset(body or {})

        if path == "/api/v1/probe" and method == "POST":
            return self._probe(body or {})

        if path == "/api/v1/jobs" and method == "GET":
            statuses = None
            if query.get("status"):
                statuses = set(query["status"][0].split(","))
            jobs = store.list_jobs(limit=int(query.get("limit", ["100"])[0]), statuses=statuses)
            return {"jobs": [j.as_dict(include_spec=False, include_args=False) for j in jobs],
                    "counts": store.counts()}

        if path == "/api/v1/spec/validate" and method == "POST":
            payload = body or {}
            wrapped = any(key in payload for key in ("spec", "preset_id", "engine", "template_id"))
            if wrapped and set(payload) - {"spec", "preset_id", "engine", "template_id", "spec_mode"}:
                raise ApiError(400, "validation accepts only spec, preset_id, engine and template_id")
            try:
                name, raw, identity, template = request_spec(store, payload if wrapped else {"spec": payload})
            except SpecError as exc:
                raise ApiError(400, str(exc)) from exc
            spec, preset_id, preset_name, document, args = self._prepare_spec(raw, identity, name=name)
            return {
                "valid": True, "engine": name, "template_id": template["id"] if template else None, "spec": spec.to_dict(), "args": args,
                "preset_id": preset_id, "preset_name": preset_name,
                "args_scope": "overrides" if document else "custom",
                "checked": ["structure", "preset", "mapping"],
                "not_checked": ["paths", "source_tracks", "encoder_readiness", "encoding"],
            }

        if path == "/api/v1/jobs" and method == "POST":
            return self._create_job(body or {})

        action_match = re.fullmatch(r"/api/v1/jobs/([0-9a-fA-F\-]+)/(start|pause)", path)
        if action_match and method == "POST":
            identity, action = action_match.groups()
            if store.get_job(identity) is None:
                raise ApiError(404, "job not found")
            if not store.control_job(identity, action):
                raise ApiError(409, f"job cannot {action}")
            return store.get_job(identity).as_dict()

        match = re.fullmatch(r"/api/v1/jobs/([0-9a-fA-F\-]+)", path)
        if match and method == "DELETE":
            identity = match.group(1)
            if store.get_job(identity) is None:
                raise ApiError(404, "job not found")
            if not store.delete_job(identity):
                raise ApiError(409, "active job must finish or be canceled before deletion")
            return {"deleted": True, "media_deleted": False}
        if match and method == "GET":
            job = store.get_job(match.group(1))
            if job is None:
                raise ApiError(404, "job not found")
            return job.as_dict()

        match = re.fullmatch(r"/api/v1/jobs/([0-9a-fA-F\-]+)/cancel", path)
        if match and method == "POST":
            ok = store.request_cancel(match.group(1))
            if not ok:
                raise ApiError(409, "job is not cancelable")
            store.add_log(match.group(1), "info", "cancel requested")
            return {"canceled": True, "id": match.group(1)}

        match = re.fullmatch(r"/api/v1/jobs/([0-9a-fA-F\-]+)/logs", path)
        if match and method == "GET":
            return {"logs": store.get_logs(match.group(1))}

        raise ApiError(404, f"no route for {method} {path}")

    # -- route implementations --------------------------------------------

    @staticmethod
    def _encoder_catalog(caps) -> dict:
        from .encoders import encoder_catalog

        return encoder_catalog(
            caps,
            VIDEO_ENCODERS,
            VIDEO_HW_ENCODERS,
            # Explain the never-submittable values, but keep them out of the
            # spec whitelist so they can never reach the engine.
            AUDIO_ENCODERS | set(AUDIO_CATALOG_ONLY),
        )

    def _list_entries(self, root_id: str, query: dict) -> dict:
        relative = (query.get("path") or [""])[0]
        try:
            resolved = resolve_request(list(self.state.config.storage_roots), root_id, relative)
        except PathSafetyError as exc:
            raise ApiError(400, str(exc)) from exc
        if not resolved.exists:
            raise ApiError(404, "path does not exist")
        if not resolved.is_dir:
            raise ApiError(400, "path is not a directory")
        entries = []
        for child in sorted(resolved.absolute.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "is_symlink": child.is_symlink(),
                "size": stat.st_size if child.is_file() else None,
                "mtime": stat.st_mtime,
            })
        return {"root": root_id, "path": resolved.relative, "entries": entries}

    def _list_presets(self) -> dict:
        official: list[dict] = []
        engine_note = None
        try:
            official = self.state.engine.list_presets()
        except EngineError as exc:
            engine_note = str(exc)
        imported = self.state.store.list_presets(source="imported")
        candidates = discover_preset_files(self.state.config.storage_roots)
        return {
            "official": official,
            "imported": imported,
            "candidate_files": candidates,
            "engine_note": engine_note,
        }

    def _import_preset(self, body: dict) -> dict:
        root_id = body.get("root")
        relative = body.get("path")
        if not root_id or not relative:
            raise ApiError(400, "root and path are required")
        try:
            from .pathsafe import find_root

            root = find_root(list(self.state.config.storage_roots), str(root_id))
            presets = load_imported_presets(self.state.config, self.state.engine, root, str(relative))
        except (PathSafetyError, PresetError) as exc:
            raise ApiError(400, str(exc)) from exc
        stored = []
        for preset in presets:
            preset_id = self.state.store.upsert_preset(
                name=preset.name, source="imported", category=preset.category,
                description=preset.description, root=preset.root, file=preset.file,
                doc=preset.raw,
            )
            stored.append({"id": preset_id, **preset.as_dict()})
        return {"imported": stored, "count": len(stored)}

    def _probe(self, body: dict) -> dict:
        root_id = body.get("root")
        relative = body.get("path")
        if not root_id or not relative:
            raise ApiError(400, "root and path are required")
        try:
            resolved = resolve_request(
                list(self.state.config.storage_roots), str(root_id), str(relative),
                require_exists=True,
            )
        except PathSafetyError as exc:
            raise ApiError(400, str(exc)) from exc
        if not resolved.is_file:
            raise ApiError(400, "path is not a file")
        try:
            name = engine_name(body.get("engine"))
            scan = self.state.backends.get(name).scan(str(resolved.absolute))
        except SpecError as exc:
            raise ApiError(400, str(exc)) from exc
        except EngineError as exc:
            raise ApiError(503, f"engine scan unavailable: {exc}") from exc
        return {"root": root_id, "path": resolved.relative, "scan": scan}

    def _prepare_spec(self, raw: dict, preset_id=None, *, input_root=None, name="handbrake"):
        """Share non-persistent preset/mapping preparation with prevalidation."""
        try:
            return prepare(self.state.config, self.state.store, self.state.backends, raw, preset_id,
                           name=name, input_root=input_root)
        except (SpecError, PresetError, PathSafetyError) as exc:
            raise ApiError(400, f"invalid spec: {exc}") from exc
        except EngineError as exc:
            raise ApiError(503, str(exc)) from exc

    def _create_job(self, body: dict) -> dict:
        input_ref = body.get("input") or {}
        output_ref = body.get("output") or {}
        if not isinstance(input_ref, dict) or not isinstance(output_ref, dict):
            raise ApiError(400, "input and output must be objects")

        try:
            input_path = resolve_request(
                list(self.state.config.storage_roots),
                str(input_ref.get("root", "")),
                str(input_ref.get("path", "")),
                require_exists=True,
            )
            if not input_path.is_file:
                raise PathSafetyError("input is not a file")
            output_path = resolve_request(
                list(self.state.config.storage_roots),
                str(output_ref.get("root", "")),
                str(output_ref.get("path", "")),
            )
            if output_path.is_dir and self.state.runtime.read()["output_collision_policy"] == "rename" and output_path.relative:
                assert_writable(output_path)
            else:
                validate_job_paths(input_path, output_path)
        except PathSafetyError as exc:
            raise ApiError(400, str(exc)) from exc

        try:
            name, overrides, identity, template = request_spec(self.state.store, body)
        except SpecError as exc:
            raise ApiError(400, str(exc)) from exc
        spec, preset_id, preset_name, document, args = self._prepare_spec(
            overrides, identity, input_root=input_path.root.id, name=name,
        )
        remote_config = self.state.backends.remote_settings() if name == "rffmpeg" else None
        try:
            admit(self.state.backends.get(name, remote_config=remote_config), name, spec, preset=document, overrides=overrides)
        except AdmissionError as exc:
            raise ApiError(exc.status, str(exc)) from exc
        except EngineError as exc:
            raise ApiError(503, str(exc)) from exc
        with self.state.runtime.lock:
            if name == "rffmpeg" and remote_config != self.state.backends.remote_settings():
                raise ApiError(409, "准入期间 rffmpeg 配置已修改，请重新提交任务")
            values = self.state.runtime.read()
            execution = {"engine": name,
                **({"remote_config": remote_config} if remote_config is not None else {}), "template_id": template["id"] if template else None,
                "template_name": template["name"] if template else None,
                "preset": document, "overrides": overrides, "settings": {
                "job_timeout_seconds": values["job_timeout_seconds"],
                "output_collision_policy": values["output_collision_policy"],
                "requested_output": output_path.relative,
            }}
            roots = list(self.state.config.storage_roots)
            try:
                job = self.state.store.create_allocated_job(
                    lambda reserved: allocate_output(roots, input_path, output_path.root.id, output_path.relative,
                        reserved | pending_paths(self.state.store, roots), values["output_collision_policy"]),
                    input_root=input_path.root.id, input_path=input_path.relative,
                    output_root=output_path.root.id, output_path=output_path.relative,
                    preset_id=preset_id, preset_name=preset_name, container=spec.container,
                    spec=spec.to_dict(), args=args, execution=execution, auto_start=values["auto_start"],
                )
            except OutputConflict as exc:
                raise ApiError(409, str(exc)) from exc
            except PathSafetyError as exc:
                raise ApiError(400, str(exc)) from exc
        self.state.store.add_log(job.id, "info", f"job queued (preset={preset_id})")
        return job.as_dict()

    # -- static files ------------------------------------------------------

    def _route_static(self, method: str, path: str) -> None:
        if method not in ("GET", "HEAD"):
            raise ApiError(405, "method not allowed")
        web_dir = self.state.web_dir
        if path in ("/", "/index.html"):
            target = web_dir / "index.html"
        elif path in ("/styles.css", "/app.js"):
            target = web_dir / path.lstrip("/")
        elif path.startswith("/assets/"):
            rel = path[len("/assets/"):]
            target = (web_dir / rel).resolve()
            if web_dir.resolve() not in target.parents and target != web_dir.resolve():
                raise ApiError(400, "invalid asset path")
        else:
            # Unknown path: fall back to the SPA entry point.
            target = web_dir / "index.html"

        if not target.is_file():
            raise ApiError(404, "not found")
        content_type, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if method == "GET":
            self.wfile.write(data)

    # -- response helper ---------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def build_server(state: AppState) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer((state.config.host, state.config.port), handler)
    server.daemon_threads = True
    return server
