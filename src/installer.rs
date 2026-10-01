use crate::config::InstallerConfig;
use anyhow::{Context, Result};
use serde::Serialize;
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};
use tokio::{fs, process::Command, sync::Mutex};

#[derive(Clone)]
pub struct Installer {
    config: InstallerConfig,
    lock: Arc<Mutex<()>>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallState {
    Disabled,
    Misconfigured,
    NotInstalled,
    Installing,
    Ready,
    Failed,
}

#[derive(Debug, Clone, Serialize)]
pub struct InstallStatus {
    pub enabled: bool,
    pub state: InstallState,
    pub progress: Option<u8>,
    pub version: Option<String>,
    pub error: Option<String>,
    pub install_dir: Option<String>,
}

impl Installer {
    pub fn new(config: InstallerConfig) -> Self {
        Self {
            config,
            lock: Arc::new(Mutex::new(())),
        }
    }

    pub fn status(&self) -> InstallStatus {
        if !self.config.enabled {
            return self.status_with(InstallState::Disabled, None, None);
        }
        if self.config.source_dir.as_os_str().is_empty()
            || self.config.install_dir.as_os_str().is_empty()
        {
            return self.status_with(
                InstallState::Misconfigured,
                None,
                Some("安装源或私有目录未配置".into()),
            );
        }
        let ffmpeg = self.active_path("ffmpeg");
        let ffprobe = self.active_path("ffprobe");
        if ffmpeg.is_file() && ffprobe.is_file() {
            self.status_with(InstallState::Ready, Some(100), None)
        } else {
            self.status_with(InstallState::NotInstalled, None, None)
        }
    }

    fn status_with(
        &self,
        state: InstallState,
        progress: Option<u8>,
        error: Option<String>,
    ) -> InstallStatus {
        InstallStatus {
            enabled: self.config.enabled,
            state,
            progress,
            version: self.config.version.clone(),
            error,
            install_dir: self
                .config
                .enabled
                .then(|| self.config.install_dir.display().to_string()),
        }
    }

    fn active_path(&self, name: &str) -> PathBuf {
        self.config
            .install_dir
            .join("current")
            .join("bin")
            .join(name)
    }
    pub fn ffmpeg_path(&self) -> Option<PathBuf> {
        self.status().enabled.then(|| self.active_path("ffmpeg"))
    }
    pub fn ffprobe_path(&self) -> Option<PathBuf> {
        self.status().enabled.then(|| self.active_path("ffprobe"))
    }

    pub async fn install(&self) -> Result<InstallStatus> {
        let _guard = self
            .lock
            .try_lock()
            .map_err(|_| anyhow::anyhow!("安装已在进行中"))?;
        anyhow::ensure!(self.config.enabled, "安装器未启用");
        let source = &self.config.source_dir;
        let source_ffmpeg = source.join("bin").join("ffmpeg");
        let source_ffprobe = source.join("bin").join("ffprobe");
        anyhow::ensure!(
            source_ffmpeg.is_file() && source_ffprobe.is_file(),
            "固定 FFmpeg bundle 不完整"
        );
        let staging = self.config.install_dir.join(".staging");
        let version_dir = self
            .config
            .install_dir
            .join("versions")
            .join(self.config.version.as_deref().unwrap_or("bundled"));
        if version_dir.exists() {
            fs::remove_dir_all(&version_dir)
                .await
                .context("清理旧 bundle")?;
        }
        fs::create_dir_all(staging.join("bin"))
            .await
            .context("创建安装目录")?;
        fs::copy(&source_ffmpeg, staging.join("bin/ffmpeg"))
            .await
            .context("复制 ffmpeg")?;
        fs::copy(&source_ffprobe, staging.join("bin/ffprobe"))
            .await
            .context("复制 ffprobe")?;
        validate_binary(&staging.join("bin/ffmpeg")).await?;
        validate_binary(&staging.join("bin/ffprobe")).await?;
        set_executable(&staging.join("bin/ffmpeg")).await?;
        set_executable(&staging.join("bin/ffprobe")).await?;
        fs::create_dir_all(self.config.install_dir.join("versions")).await?;
        fs::rename(&staging, &version_dir)
            .await
            .context("激活 FFmpeg bundle")?;
        let current = self.config.install_dir.join("current");
        if current.exists() {
            fs::remove_dir_all(&current).await.ok();
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(&version_dir, &current).context("激活 current")?;
        #[cfg(not(unix))]
        fs::copy(&version_dir.join("bin/ffmpeg"), current.join("bin/ffmpeg")).await?;
        Ok(self.status_with(InstallState::Ready, Some(100), None))
    }
}

async fn validate_binary(path: &Path) -> Result<()> {
    let output = Command::new(path)
        .arg("-version")
        .output()
        .await
        .context("校验 FFmpeg bundle")?;
    anyhow::ensure!(output.status.success(), "FFmpeg bundle 可执行校验失败");
    Ok(())
}

#[cfg(unix)]
async fn set_executable(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = fs::metadata(path).await?.permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions).await?;
    Ok(())
}
#[cfg(not(unix))]
async fn set_executable(_path: &Path) -> Result<()> {
    Ok(())
}
