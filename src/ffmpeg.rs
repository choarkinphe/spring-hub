use crate::spec::*;
use anyhow::{ensure, Result};
use std::ffi::OsString;

pub fn build_args(spec: &TranscodeSpec, input: &str, output: &str) -> Result<Vec<OsString>> {
    spec.validate()?;
    let mut args = Vec::new();
    args.extend(
        ["-hide_banner", "-y", "-i", input]
            .into_iter()
            .map(OsString::from),
    );
    if let Some(start) = spec.range.start_seconds {
        args.extend([OsString::from("-ss"), OsString::from(format_seconds(start))]);
    }
    if let Some(end) = spec.range.end_seconds {
        if let Some(start) = spec.range.start_seconds {
            args.extend([
                OsString::from("-t"),
                OsString::from(format_seconds(end - start)),
            ]);
        } else {
            args.extend([OsString::from("-to"), OsString::from(format_seconds(end))]);
        }
    }
    args.extend([OsString::from("-map"), OsString::from("0:v:0?")]);
    if spec.audio.is_empty() {
        args.extend([OsString::from("-an")]);
    } else {
        for track in &spec.audio {
            args.extend([
                OsString::from("-map"),
                OsString::from(format!("0:a:{}?", track.source)),
            ]);
        }
    }
    for subtitle in &spec.subtitles {
        if subtitle.external_path.is_none() {
            args.extend([
                OsString::from("-map"),
                OsString::from(format!("0:s:{}?", subtitle.source)),
            ]);
        }
    }
    let video_codec = match spec.video.encoder {
        VideoEncoder::H264 => "libx264",
        VideoEncoder::H265 => "libx265",
        VideoEncoder::Vp9 => "libvpx-vp9",
        VideoEncoder::Av1 => "libsvtav1",
        VideoEncoder::H264Nvenc => "h264_nvenc",
        VideoEncoder::H265Nvenc => "hevc_nvenc",
        VideoEncoder::H264Vaapi => "h264_vaapi",
        VideoEncoder::H265Vaapi => "hevc_vaapi",
        VideoEncoder::H264Qsv => "h264_qsv",
        VideoEncoder::H265Qsv => "hevc_qsv",
    };
    args.extend([OsString::from("-c:v"), OsString::from(video_codec)]);
    match spec.video.quality {
        QualityMode::Crf => {
            let flag = if matches!(
                spec.video.encoder,
                VideoEncoder::H264Nvenc | VideoEncoder::H265Nvenc
            ) {
                "-cq"
            } else {
                "-crf"
            };
            args.extend([
                OsString::from(flag),
                OsString::from(format_number(spec.video.quality_value)),
            ]);
        }
        QualityMode::Bitrate => {
            args.extend([
                OsString::from("-b:v"),
                OsString::from(format!("{}k", spec.video.quality_value.round())),
            ]);
            if spec.video.two_pass {
                args.extend([OsString::from("-pass"), OsString::from("1")]);
            }
        }
    }
    if !matches!(
        spec.video.encoder,
        VideoEncoder::H264Vaapi | VideoEncoder::H265Vaapi
    ) {
        args.extend([
            OsString::from("-preset"),
            OsString::from(speed_name(&spec.video.speed)),
        ]);
    }
    if let Some(tune) = &spec.video.tune {
        args.extend([OsString::from("-tune"), OsString::from(tune_name(tune))]);
    }
    if let Some(profile) = &spec.video.profile {
        args.extend([OsString::from("-profile:v"), OsString::from(profile)]);
    }
    if let Some(level) = &spec.video.level {
        args.extend([OsString::from("-level:v"), OsString::from(level)]);
    }
    if let Some(rate) = spec.video.framerate {
        args.extend([OsString::from("-r"), OsString::from(format_number(rate))]);
    }
    if let Some(format) = &spec.video.pixel_format {
        ensure!(
            ["yuv420p", "yuv422p", "yuv444p", "nv12", "p010le"].contains(&format.as_str()),
            "unsupported pixel format"
        );
        args.extend([OsString::from("-pix_fmt"), OsString::from(format)]);
    }
    let filter = build_filter(spec);
    if !filter.is_empty() {
        args.extend([OsString::from("-vf"), OsString::from(filter)]);
    }
    for (index, track) in spec.audio.iter().enumerate() {
        args.extend([
            OsString::from(format!("-c:a:{index}")),
            OsString::from(audio_codec(&track.encoder)),
            OsString::from(format!("-b:a:{index}")),
            OsString::from(format!("{}k", track.bitrate_kbps)),
        ]);
        if track.gain_db != 0.0 {
            args.extend([
                OsString::from(format!("-af:{index}")),
                OsString::from(format!("volume={}dB", track.gain_db)),
            ]);
        }
    }
    for (index, subtitle) in spec.subtitles.iter().enumerate() {
        if subtitle.mode == SubtitleMode::Soft {
            args.extend([
                OsString::from(format!("-c:s:{index}")),
                OsString::from("copy"),
            ]);
        }
    }
    if spec.container == Container::Mp4 {
        args.extend([OsString::from("-movflags"), OsString::from("+faststart")]);
    }
    args.extend([
        OsString::from("-progress"),
        OsString::from("pipe:1"),
        OsString::from("-nostats"),
        OsString::from(output),
    ]);
    Ok(args)
}
fn build_filter(spec: &TranscodeSpec) -> String {
    let mut filters = Vec::new();
    let c = &spec.geometry.crop;
    if c.top + c.bottom + c.left + c.right > 0 {
        filters.push(format!(
            "crop=iw-{}-{}:ih-{}-{}:{}:{}",
            c.left, c.right, c.top, c.bottom, c.left, c.top
        ));
    }
    if let (Some(width), Some(height)) = (spec.geometry.width, spec.geometry.height) {
        filters.push(if spec.geometry.keep_aspect {
            format!("scale={width}:{height}:force_original_aspect_ratio=decrease")
        } else {
            format!("scale={width}:{height}")
        });
    }
    if spec.filters.deinterlace {
        filters.push("yadif".into());
    }
    if let Some(mode) = &spec.filters.denoise {
        filters.push(match mode {
            DenoiseMode::Hqdn3d => "hqdn3d".into(),
            DenoiseMode::Nlmeans => "nlmeans".into(),
        });
    }
    if spec.filters.sharpen {
        filters.push("unsharp".into());
    }
    if spec.filters.grayscale {
        filters.push("hue=s=0".into());
    }
    filters.join(",")
}
fn audio_codec(encoder: &AudioEncoder) -> &'static str {
    match encoder {
        AudioEncoder::Aac => "aac",
        AudioEncoder::Opus => "libopus",
        AudioEncoder::Vorbis => "libvorbis",
        AudioEncoder::Ac3 => "ac3",
        AudioEncoder::Eac3 => "eac3",
        AudioEncoder::Flac => "flac",
        AudioEncoder::Copy => "copy",
    }
}
fn speed_name(speed: &SpeedPreset) -> &'static str {
    match speed {
        SpeedPreset::Ultrafast => "ultrafast",
        SpeedPreset::Fast => "fast",
        SpeedPreset::Medium => "medium",
        SpeedPreset::Slow => "slow",
        SpeedPreset::Slower => "slower",
        SpeedPreset::Veryslow => "veryslow",
    }
}
fn tune_name(tune: &VideoTune) -> &'static str {
    match tune {
        VideoTune::Film => "film",
        VideoTune::Animation => "animation",
        VideoTune::Grain => "grain",
        VideoTune::Stillimage => "stillimage",
        VideoTune::Fastdecode => "fastdecode",
        VideoTune::Zerolatency => "zerolatency",
    }
}
fn format_number(value: f32) -> String {
    format!("{value:.2}")
        .trim_end_matches('0')
        .trim_end_matches('.')
        .to_owned()
}
fn format_seconds(value: f64) -> String {
    format!("{value:.3}")
        .trim_end_matches('0')
        .trim_end_matches('.')
        .to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn builds_safe_structured_args() {
        let args = build_args(&TranscodeSpec::default(), "in.mkv", "out.mp4").unwrap();
        let text = args
            .iter()
            .map(|v| v.to_string_lossy())
            .collect::<Vec<_>>()
            .join(" ");
        assert!(text.contains("-c:v libx264"));
        assert!(!text.contains("&&"));
    }
}
