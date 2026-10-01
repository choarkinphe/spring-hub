use anyhow::{Context, Result};
use serde::Serialize;
use std::path::Path;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

#[derive(Clone)]
pub struct MediaTools {
    pub ffmpeg: String,
    pub ffprobe: String,
}

#[derive(Debug, Serialize)]
pub struct Capabilities {
    pub ffmpeg_available: bool,
    pub ffprobe_available: bool,
    pub ffmpeg_version: Option<String>,
    pub encoders: Vec<String>,
    pub decoders: Vec<String>,
    pub hardware_acceleration: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct ProbeResult {
    pub format: Option<String>,
    pub duration_seconds: Option<f64>,
    pub size_bytes: Option<u64>,
    pub streams: Vec<ProbeStream>,
}

#[derive(Debug, Serialize)]
pub struct ProbeStream {
    pub index: Option<u32>,
    pub codec_type: Option<String>,
    pub codec_name: Option<String>,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub channels: Option<u32>,
}

#[derive(Debug, serde::Deserialize)]
struct FfprobeJson {
    format: Option<FfprobeFormat>,
    #[serde(default)]
    streams: Vec<FfprobeStream>,
}
#[derive(Debug, serde::Deserialize)]
struct FfprobeFormat {
    format_name: Option<String>,
    duration: Option<String>,
    size: Option<String>,
}
#[derive(Debug, serde::Deserialize)]
struct FfprobeStream {
    index: Option<u32>,
    codec_type: Option<String>,
    codec_name: Option<String>,
    width: Option<u32>,
    height: Option<u32>,
    channels: Option<u32>,
}

impl MediaTools {
    pub async fn capabilities(&self) -> Capabilities {
        let version = Command::new(&self.ffmpeg)
            .arg("-version")
            .output()
            .await
            .ok()
            .filter(|output| output.status.success())
            .map(|output| {
                String::from_utf8_lossy(&output.stdout)
                    .lines()
                    .next()
                    .unwrap_or_default()
                    .to_owned()
            });
        let encoders = self
            .list_names("-encoders", " [A-Z.]{6}([a-zA-Z0-9_]+)")
            .await;
        let decoders = self
            .list_names("-decoders", " [A-Z.]{6}([a-zA-Z0-9_]+)")
            .await;
        let hardware_acceleration = self.list_names("-hwaccels", "^([a-zA-Z0-9_]+)$").await;
        Capabilities {
            ffmpeg_available: version.is_some(),
            ffprobe_available: self.check_binary(&self.ffprobe).await,
            ffmpeg_version: version,
            encoders,
            decoders,
            hardware_acceleration,
        }
    }

    async fn check_binary(&self, binary: &str) -> bool {
        Command::new(binary)
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await
            .is_ok_and(|status| status.success())
    }

    async fn list_names(&self, arg: &str, _pattern: &str) -> Vec<String> {
        let Ok(output) = Command::new(&self.ffmpeg)
            .arg("-hide_banner")
            .arg(arg)
            .output()
            .await
        else {
            return Vec::new();
        };
        if !output.status.success() {
            return Vec::new();
        }
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .filter_map(|line| {
                let line = line.trim();
                if line.is_empty()
                    || line.starts_with('-')
                    || line.starts_with('=')
                    || line.starts_with("Encoders")
                    || line.starts_with("Decoders")
                    || line.starts_with("Hardware acceleration")
                {
                    return None;
                }
                if arg == "-hwaccels" {
                    return line.split_whitespace().next().map(ToOwned::to_owned);
                }
                let fields = line.split_whitespace().collect::<Vec<_>>();
                (fields.len() >= 2 && fields[0].len() == 6).then(|| fields[1].to_owned())
            })
            .filter(|name| {
                name.chars()
                    .all(|character| character.is_ascii_alphanumeric() || character == '_')
            })
            .collect()
    }

    pub async fn probe(&self, path: &std::path::Path) -> Result<ProbeResult> {
        let output = Command::new(&self.ffprobe)
            .args([
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
            ])
            .arg(path)
            .output()
            .await
            .context("run ffprobe")?;
        anyhow::ensure!(
            output.status.success(),
            "ffprobe failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let parsed: FfprobeJson =
            serde_json::from_slice(&output.stdout).context("parse ffprobe output")?;
        Ok(ProbeResult {
            format: parsed
                .format
                .as_ref()
                .and_then(|format| format.format_name.clone()),
            duration_seconds: parsed
                .format
                .as_ref()
                .and_then(|format| format.duration.as_deref())
                .and_then(|value| value.parse().ok()),
            size_bytes: parsed
                .format
                .as_ref()
                .and_then(|format| format.size.as_deref())
                .and_then(|value| value.parse().ok()),
            streams: parsed
                .streams
                .into_iter()
                .map(|stream| ProbeStream {
                    index: stream.index,
                    codec_type: stream.codec_type,
                    codec_name: stream.codec_name,
                    width: stream.width,
                    height: stream.height,
                    channels: stream.channels,
                })
                .collect(),
        })
    }

    pub async fn transcode(
        &self,
        input: &Path,
        output: &Path,
        spec: &crate::spec::TranscodeSpec,
        duration_seconds: Option<f64>,
        on_progress: impl Fn(f64, Option<&str>) + Send + 'static,
    ) -> Result<()> {
        let args =
            crate::ffmpeg::build_args(spec, &input.to_string_lossy(), &output.to_string_lossy())?;
        let mut child = Command::new(&self.ffmpeg)
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("run ffmpeg")?;
        let stdout = child.stdout.take().context("ffmpeg stdout unavailable")?;
        let mut lines = BufReader::new(stdout).lines();
        let mut progress = 0.0;
        while let Some(line) = lines.next_line().await.context("read ffmpeg progress")? {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            if key == "out_time_us" {
                if let (Ok(microseconds), Some(duration)) = (value.parse::<f64>(), duration_seconds)
                {
                    progress = (microseconds / 1_000_000.0 / duration).clamp(0.0, 0.99);
                    on_progress(progress, None);
                }
            } else if key == "speed" {
                on_progress(progress, Some(value));
            }
        }
        let status = child.wait().await.context("wait for ffmpeg")?;
        anyhow::ensure!(status.success(), "ffmpeg exited with {status}");
        Ok(())
    }
}
