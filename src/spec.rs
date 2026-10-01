use anyhow::{bail, ensure, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscodeSpec {
    #[serde(default = "default_version")]
    pub version: u32,
    #[serde(default)]
    pub container: Container,
    #[serde(default)]
    pub video: VideoSpec,
    #[serde(default)]
    pub geometry: GeometrySpec,
    #[serde(default)]
    pub filters: FilterSpec,
    #[serde(default)]
    pub audio: Vec<AudioTrackSpec>,
    #[serde(default)]
    pub subtitles: Vec<SubtitleTrackSpec>,
    #[serde(default)]
    pub chapters: ChapterSpec,
    #[serde(default)]
    pub metadata: MetadataSpec,
    #[serde(default)]
    pub range: RangeSpec,
}

fn default_version() -> u32 {
    1
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum Container {
    #[default]
    Mp4,
    Mkv,
    Webm,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VideoSpec {
    #[serde(default = "default_encoder")]
    pub encoder: VideoEncoder,
    #[serde(default)]
    pub quality: QualityMode,
    #[serde(default = "default_quality")]
    pub quality_value: f32,
    #[serde(default = "default_speed")]
    pub speed: SpeedPreset,
    #[serde(default)]
    pub tune: Option<VideoTune>,
    #[serde(default)]
    pub profile: Option<String>,
    #[serde(default)]
    pub level: Option<String>,
    #[serde(default = "default_framerate")]
    pub framerate: Option<f32>,
    #[serde(default)]
    pub framerate_mode: FramerateMode,
    #[serde(default)]
    pub pixel_format: Option<String>,
    #[serde(default)]
    pub two_pass: bool,
}
fn default_encoder() -> VideoEncoder {
    VideoEncoder::H264
}
fn default_quality() -> f32 {
    22.0
}
fn default_speed() -> SpeedPreset {
    SpeedPreset::Medium
}
fn default_framerate() -> Option<f32> {
    None
}
impl Default for VideoSpec {
    fn default() -> Self {
        Self {
            encoder: default_encoder(),
            quality: QualityMode::Crf,
            quality_value: default_quality(),
            speed: default_speed(),
            tune: None,
            profile: None,
            level: None,
            framerate: None,
            framerate_mode: FramerateMode::Vfr,
            pixel_format: None,
            two_pass: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum VideoEncoder {
    H264,
    H265,
    Vp9,
    Av1,
    H264Nvenc,
    H265Nvenc,
    H264Vaapi,
    H265Vaapi,
    H264Qsv,
    H265Qsv,
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum QualityMode {
    #[default]
    Crf,
    Bitrate,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SpeedPreset {
    Ultrafast,
    Fast,
    Medium,
    Slow,
    Slower,
    Veryslow,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VideoTune {
    Film,
    Animation,
    Grain,
    Stillimage,
    Fastdecode,
    Zerolatency,
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum FramerateMode {
    #[default]
    Vfr,
    Cfr,
    Pfr,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct GeometrySpec {
    #[serde(default)]
    pub width: Option<u32>,
    #[serde(default)]
    pub height: Option<u32>,
    #[serde(default = "default_true")]
    pub keep_aspect: bool,
    #[serde(default)]
    pub crop: CropSpec,
    #[serde(default)]
    pub rotate: Option<u16>,
}
fn default_true() -> bool {
    true
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CropSpec {
    pub top: u32,
    pub bottom: u32,
    pub left: u32,
    pub right: u32,
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct FilterSpec {
    #[serde(default)]
    pub deinterlace: bool,
    #[serde(default)]
    pub denoise: Option<DenoiseMode>,
    #[serde(default)]
    pub sharpen: bool,
    #[serde(default)]
    pub grayscale: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DenoiseMode {
    Hqdn3d,
    Nlmeans,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioTrackSpec {
    pub source: u32,
    #[serde(default)]
    pub encoder: AudioEncoder,
    #[serde(default = "default_audio_bitrate")]
    pub bitrate_kbps: u32,
    #[serde(default)]
    pub mixdown: Mixdown,
    #[serde(default)]
    pub gain_db: f32,
    #[serde(default)]
    pub delay_ms: i32,
}
fn default_audio_bitrate() -> u32 {
    160
}
impl Default for AudioTrackSpec {
    fn default() -> Self {
        Self {
            source: 0,
            encoder: AudioEncoder::Aac,
            bitrate_kbps: default_audio_bitrate(),
            mixdown: Mixdown::Stereo,
            gain_db: 0.0,
            delay_ms: 0,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum AudioEncoder {
    #[default]
    Aac,
    Opus,
    Vorbis,
    Ac3,
    Eac3,
    Flac,
    Copy,
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Mixdown {
    Mono,
    #[default]
    Stereo,
    Surround,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubtitleTrackSpec {
    pub source: u32,
    #[serde(default)]
    pub mode: SubtitleMode,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub forced: bool,
    #[serde(default)]
    pub default: bool,
    #[serde(default)]
    pub external_path: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "kebab-case")]
pub enum SubtitleMode {
    #[default]
    Soft,
    Burn,
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ChapterSpec {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub names: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct MetadataSpec {
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub comment: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RangeSpec {
    #[serde(default)]
    pub start_seconds: Option<f64>,
    #[serde(default)]
    pub end_seconds: Option<f64>,
}

impl TranscodeSpec {
    pub fn validate(&self) -> Result<()> {
        ensure!(self.version == 1, "unsupported spec version");
        if let Some(width) = self.geometry.width {
            ensure!(
                (16..=16384).contains(&width),
                "width must be between 16 and 16384"
            );
        }
        if let Some(height) = self.geometry.height {
            ensure!(
                (16..=16384).contains(&height),
                "height must be between 16 and 16384"
            );
        }
        ensure!(
            self.geometry.crop.left + self.geometry.crop.right
                < self.geometry.width.unwrap_or(u32::MAX),
            "horizontal crop is too large"
        );
        ensure!(
            self.geometry.crop.top + self.geometry.crop.bottom
                < self.geometry.height.unwrap_or(u32::MAX),
            "vertical crop is too large"
        );
        ensure!(
            (0.0..=100.0).contains(&self.video.quality_value),
            "quality must be between 0 and 100"
        );
        if let Some(rate) = self.video.framerate {
            ensure!(
                (1.0..=240.0).contains(&rate),
                "framerate must be between 1 and 240"
            );
        }
        if self.video.two_pass {
            ensure!(
                matches!(self.video.quality, QualityMode::Bitrate),
                "two-pass requires bitrate mode"
            );
        }
        if let (Some(start), Some(end)) = (self.range.start_seconds, self.range.end_seconds) {
            ensure!(start >= 0.0 && end > start, "invalid range");
        }
        for track in &self.audio {
            ensure!(track.bitrate_kbps <= 1536, "audio bitrate is too high");
            ensure!(track.gain_db.abs() <= 30.0, "audio gain is out of range");
        }
        for subtitle in &self.subtitles {
            if subtitle.external_path.is_some() && subtitle.mode == SubtitleMode::Burn {
                bail!("external subtitle burn-in requires a validated subtitle path");
            }
        }
        Ok(())
    }
}

impl Default for TranscodeSpec {
    fn default() -> Self {
        Self {
            version: 1,
            container: Container::default(),
            video: VideoSpec::default(),
            geometry: GeometrySpec::default(),
            filters: FilterSpec::default(),
            audio: vec![AudioTrackSpec::default()],
            subtitles: Vec::new(),
            chapters: ChapterSpec::default(),
            metadata: MetadataSpec::default(),
            range: RangeSpec::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_are_valid() {
        assert!(TranscodeSpec {
            ..Default::default()
        }
        .validate()
        .is_ok());
    }
    #[test]
    fn rejects_two_pass_crf() {
        let mut spec = TranscodeSpec::default();
        spec.video.two_pass = true;
        assert!(spec.validate().is_err());
    }
}
