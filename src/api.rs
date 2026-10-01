use crate::spec::TranscodeSpec;
use crate::{
    config::AppConfig,
    db::{Database, NewJob},
    media::MediaTools,
    storage::Storage,
};
use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Json},
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tower_http::{cors::CorsLayer, services::ServeDir, trace::TraceLayer};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<AppConfig>,
    pub db: Database,
    pub storage: Storage,
    pub media: MediaTools,
    pub installer: crate::installer::Installer,
}

#[derive(Debug, Serialize)]
struct ApiError {
    error: String,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        (StatusCode::BAD_REQUEST, Json(self)).into_response()
    }
}

type ApiResult<T> = Result<Json<T>, ApiError>;

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health/live", get(live))
        .route("/health/ready", get(ready))
        .route("/api/v1/capabilities", get(capabilities))
        .route("/api/v1/tools", get(tool_status).post(install_tools))
        .route("/api/v1/presets", get(presets))
        .route("/api/v1/storage-roots", get(storage_roots))
        .route("/api/v1/storage-roots/{id}/entries", get(entries))
        .route("/api/v1/probe", post(probe))
        .route("/api/v1/spec/validate", post(validate_spec))
        .route("/api/v1/jobs", get(list_jobs).post(create_job))
        .route("/api/v1/jobs/{id}", get(get_job))
        .route("/api/v1/jobs/{id}/cancel", post(cancel_job))
        .fallback_service(ServeDir::new("web").append_index_html_on_directories(true))
        .layer(CorsLayer::very_permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn live() -> Json<serde_json::Value> {
    Json(serde_json::json!({"status":"ok"}))
}

async fn ready(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, ApiError> {
    auth(&state, &headers)?;
    let caps = state.media.capabilities().await;
    if !caps.ffmpeg_available || !caps.ffprobe_available {
        return Err(ApiError {
            error: "ffmpeg and ffprobe are required".into(),
        });
    }
    Ok(Json(
        serde_json::json!({"status":"ready", "capabilities": caps}),
    ))
}

async fn capabilities(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> ApiResult<crate::media::Capabilities> {
    auth(&state, &headers)?;
    Ok(Json(state.media.capabilities().await))
}

async fn validate_spec(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(spec): Json<TranscodeSpec>,
) -> ApiResult<serde_json::Value> {
    auth(&state, &headers)?;
    spec.validate().map_err(api_error)?;
    Ok(Json(serde_json::json!({"valid": true, "spec": spec})))
}

#[derive(Serialize)]
struct Preset {
    id: &'static str,
    label: &'static str,
    description: &'static str,
    extension: &'static str,
}
async fn tool_status(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> ApiResult<crate::installer::InstallStatus> {
    auth(&state, &headers)?;
    Ok(Json(state.installer.status()))
}

async fn install_tools(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> ApiResult<crate::installer::InstallStatus> {
    auth(&state, &headers)?;
    let installer = state.installer.clone();
    let status = installer.status();
    if matches!(status.state, crate::installer::InstallState::Ready) {
        return Ok(Json(status));
    }
    tokio::spawn(async move {
        let _ = installer.install().await;
    });
    Ok(Json(state.installer.status()))
}

async fn presets(State(state): State<AppState>, headers: HeaderMap) -> ApiResult<Vec<Preset>> {
    auth(&state, &headers)?;
    Ok(Json(vec![
        Preset {
            id: "h264-balanced",
            label: "H.264 · 均衡",
            description: "兼容性优先，适合大多数设备",
            extension: "mp4",
        },
        Preset {
            id: "h265-quality",
            label: "H.265 · 质量",
            description: "更小体积，编码耗时更长",
            extension: "mp4",
        },
        Preset {
            id: "h264-small",
            label: "H.264 · 体积",
            description: "尽量降低文件体积",
            extension: "mp4",
        },
    ]))
}

#[derive(Serialize)]
struct StorageRoot {
    id: String,
    label: String,
    path: String,
    read_only: bool,
    available: bool,
    marker_ok: bool,
}
async fn storage_roots(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> ApiResult<Vec<StorageRoot>> {
    auth(&state, &headers)?;
    let roots = state
        .storage
        .roots()
        .iter()
        .map(|root| {
            let available = root.path.is_dir();
            let marker_ok = root
                .mount_marker
                .as_ref()
                .map(|marker| root.path.join(marker).is_file())
                .unwrap_or(true);
            StorageRoot {
                id: root.id.clone(),
                label: root.label.clone(),
                path: root.path.display().to_string(),
                read_only: root.read_only,
                available,
                marker_ok,
            }
        })
        .collect();
    Ok(Json(roots))
}

#[derive(Deserialize)]
struct EntryQuery {
    #[serde(default)]
    path: String,
}
async fn entries(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(query): Query<EntryQuery>,
) -> ApiResult<Vec<crate::storage::DirectoryEntry>> {
    auth(&state, &headers)?;
    state
        .storage
        .list_dir(&id, &query.path)
        .map(Json)
        .map_err(api_error)
}

#[derive(Deserialize)]
struct ProbeRequest {
    root_id: String,
    path: String,
}
async fn probe(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<ProbeRequest>,
) -> ApiResult<crate::media::ProbeResult> {
    auth(&state, &headers)?;
    let path = state
        .storage
        .resolve_existing(&request.root_id, &request.path)
        .map_err(api_error)?;
    state.media.probe(&path).await.map(Json).map_err(api_error)
}

async fn list_jobs(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> ApiResult<Vec<crate::db::Job>> {
    auth(&state, &headers)?;
    state.db.list_jobs().await.map(Json).map_err(api_error)
}

async fn get_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> ApiResult<crate::db::Job> {
    auth(&state, &headers)?;
    state
        .db
        .get_job(&id)
        .await
        .map_err(api_error)?
        .map(Json)
        .ok_or_else(|| ApiError {
            error: "job not found".into(),
        })
}

async fn create_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<NewJob>,
) -> ApiResult<crate::db::Job> {
    auth(&state, &headers)?;
    if !["h264-balanced", "h265-quality", "h264-small"].contains(&request.preset_id.as_str()) {
        return Err(api_error("unknown preset"));
    }
    if let Some(spec) = &request.spec {
        spec.validate().map_err(api_error)?;
    }
    let input = state
        .storage
        .resolve_existing(&request.input_root, &request.input_path)
        .map_err(api_error)?;
    let output = state
        .storage
        .resolve_output(&request.output_root, &request.output_path)
        .map_err(api_error)?;
    if input == output {
        return Err(api_error("input and output must be different"));
    }
    if output.exists() {
        return Err(api_error("output already exists"));
    }
    state
        .db
        .create_job(&request)
        .await
        .map(Json)
        .map_err(api_error)
}

async fn cancel_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> ApiResult<serde_json::Value> {
    auth(&state, &headers)?;
    let canceled = state.db.cancel(&id).await.map_err(api_error)?;
    Ok(Json(serde_json::json!({"canceled": canceled})))
}

fn auth(state: &AppState, headers: &HeaderMap) -> Result<(), ApiError> {
    if state.config.security.api_token.is_empty() {
        return Ok(());
    }
    let expected = format!("Bearer {}", state.config.security.api_token);
    if headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        == Some(expected.as_str())
    {
        Ok(())
    } else {
        Err(ApiError {
            error: "authentication required".into(),
        })
    }
}

fn api_error(error: impl std::fmt::Display) -> ApiError {
    ApiError {
        error: error.to_string(),
    }
}
