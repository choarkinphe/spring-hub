use anyhow::{Context, Result};
use serde::Deserialize;
use std::{net::SocketAddr, path::PathBuf};

#[derive(Debug, Clone, Deserialize)]
pub struct AppConfig {
    #[serde(default)]
    pub server: ServerConfig,
    #[serde(default)]
    pub security: SecurityConfig,
    #[serde(default)]
    pub storage_roots: Vec<StorageRootConfig>,
    #[serde(default)]
    pub transcoding: TranscodingConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ServerConfig {
    #[serde(default = "default_listen")]
    pub listen: String,
    #[serde(default = "default_database_url")]
    pub database_url: String,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct SecurityConfig {
    #[serde(default)]
    pub api_token: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct StorageRootConfig {
    pub id: String,
    pub label: String,
    pub path: PathBuf,
    #[serde(default)]
    pub read_only: bool,
    #[serde(default)]
    pub mount_marker: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TranscodingConfig {
    #[serde(default = "default_ffmpeg")]
    pub ffmpeg_bin: String,
    #[serde(default = "default_ffprobe")]
    pub ffprobe_bin: String,
    #[serde(default = "default_concurrency")]
    pub max_concurrent_jobs: usize,
    #[serde(default)]
    pub installer: InstallerConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct InstallerConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_install_dir")]
    pub install_dir: PathBuf,
    #[serde(default = "default_source_dir")]
    pub source_dir: PathBuf,
    #[serde(default = "default_bundle_version")]
    pub version: Option<String>,
}

fn default_listen() -> String {
    "0.0.0.0:8080".to_owned()
}
fn default_database_url() -> String {
    "sqlite:///data/cute-cat.db".to_owned()
}
fn default_ffmpeg() -> String {
    "ffmpeg".to_owned()
}
fn default_ffprobe() -> String {
    "ffprobe".to_owned()
}
fn default_concurrency() -> usize {
    1
}
fn default_install_dir() -> PathBuf {
    PathBuf::from("/data/cute-cat-tools")
}
fn default_source_dir() -> PathBuf {
    PathBuf::from("/opt/cute-cat/ffmpeg-bundle")
}
fn default_bundle_version() -> Option<String> {
    Some("bundled".to_owned())
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            listen: default_listen(),
            database_url: default_database_url(),
        }
    }
}

impl Default for TranscodingConfig {
    fn default() -> Self {
        Self {
            ffmpeg_bin: default_ffmpeg(),
            ffprobe_bin: default_ffprobe(),
            max_concurrent_jobs: default_concurrency(),
            installer: InstallerConfig::default(),
        }
    }
}

impl Default for InstallerConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            install_dir: default_install_dir(),
            source_dir: default_source_dir(),
            version: default_bundle_version(),
        }
    }
}

impl AppConfig {
    pub fn load() -> Result<Self> {
        let path = std::env::var("CUTE_CAT_CONFIG").unwrap_or_else(|_| "config.toml".to_owned());
        let raw = std::fs::read_to_string(&path).with_context(|| format!("read config {path}"))?;
        let config: Self = toml::from_str(&raw).context("parse config")?;
        config.validate()?;
        Ok(config)
    }

    pub fn listen_addr(&self) -> Result<SocketAddr> {
        self.server.listen.parse().context("invalid server.listen")
    }

    fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            !self.storage_roots.is_empty(),
            "at least one storage root is required"
        );
        anyhow::ensure!(
            self.transcoding.max_concurrent_jobs > 0,
            "max_concurrent_jobs must be positive"
        );
        if self.transcoding.installer.enabled {
            anyhow::ensure!(
                self.transcoding.installer.install_dir.is_absolute(),
                "installer install_dir must be absolute"
            );
            for root in &self.storage_roots {
                anyhow::ensure!(
                    self.transcoding.installer.install_dir != root.path
                        && !self
                            .transcoding
                            .installer
                            .install_dir
                            .starts_with(&root.path)
                        && !root
                            .path
                            .starts_with(&self.transcoding.installer.install_dir),
                    "installer directory must be separate from storage roots"
                );
            }
        }
        let mut ids = std::collections::HashSet::new();
        for root in &self.storage_roots {
            anyhow::ensure!(
                ids.insert(&root.id),
                "duplicate storage root id: {}",
                root.id
            );
            anyhow::ensure!(!root.id.is_empty(), "storage root id cannot be empty");
        }
        Ok(())
    }
}
