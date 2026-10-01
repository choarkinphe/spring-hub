mod api;
mod config;
mod db;
mod ffmpeg;
#[allow(dead_code)]
mod installer;
mod media;
mod spec;
mod storage;
mod worker;

use anyhow::Result;
use api::{router, AppState};
use config::AppConfig;
use db::Database;
use installer::Installer;
use media::MediaTools;
use std::sync::Arc;
use storage::Storage;
use tracing::info;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            std::env::var("RUST_LOG").unwrap_or_else(|_| "cute_cat=info,tower_http=info".into()),
        )
        .json()
        .init();
    let config = Arc::new(AppConfig::load()?);
    let db = Database::connect(&config.server.database_url).await?;
    db.recover_running().await?;
    let storage = Storage::new(config.storage_roots.clone());
    let installer = Installer::new(config.transcoding.installer.clone());
    let media = MediaTools {
        ffmpeg: installer
            .ffmpeg_path()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|| config.transcoding.ffmpeg_bin.clone()),
        ffprobe: installer
            .ffprobe_path()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|| config.transcoding.ffprobe_bin.clone()),
    };
    let state = AppState {
        config: config.clone(),
        db: db.clone(),
        storage: storage.clone(),
        media: media.clone(),
        installer: installer.clone(),
    };
    tokio::spawn(worker::run_worker(
        db,
        storage,
        media,
        config.transcoding.max_concurrent_jobs,
    ));
    let listener = tokio::net::TcpListener::bind(config.listen_addr()?).await?;
    info!(address = %listener.local_addr()?, "cute-cat is ready");
    axum::serve(listener, router(state)).await?;
    Ok(())
}
