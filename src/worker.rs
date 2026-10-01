use crate::{
    db::{Database, Job},
    media::MediaTools,
    storage::Storage,
};
use anyhow::{Context, Result};
use std::{sync::Arc, time::Duration};
use tokio::sync::Semaphore;
use tracing::{error, info};

pub async fn run_worker(db: Database, storage: Storage, media: MediaTools, concurrency: usize) {
    let slots = Arc::new(Semaphore::new(concurrency.max(1)));
    loop {
        match slots.clone().try_acquire_owned() {
            Ok(permit) => match db.claim_next().await {
                Ok(Some(job)) => {
                    let db = db.clone();
                    let storage = storage.clone();
                    let media = media.clone();
                    tokio::spawn(async move {
                        if let Err(error) = execute_job(&db, &storage, &media, &job).await {
                            error!(job_id = %job.id, %error, "transcode job failed");
                            let _ = db
                                .update_status(&job.id, "failed", Some(&error.to_string()))
                                .await;
                        }
                        drop(permit);
                    });
                }
                Ok(None) => {
                    drop(permit);
                    tokio::time::sleep(Duration::from_millis(500)).await;
                }
                Err(error) => {
                    drop(permit);
                    error!(%error, "job poll failed");
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            },
            Err(_) => tokio::time::sleep(Duration::from_millis(100)).await,
        }
    }
}

async fn execute_job(
    db: &Database,
    storage: &Storage,
    media: &MediaTools,
    job: &Job,
) -> Result<()> {
    db.update_status(&job.id, "probing", None).await?;
    let input = storage.resolve_existing(&job.input_root, &job.input_path)?;
    let output = storage.resolve_output(&job.output_root, &job.output_path)?;
    anyhow::ensure!(input != output, "input and output must be different");
    let probe = media.probe(&input).await?;
    let duration = probe.duration_seconds.unwrap_or_default();
    let temp_name = format!(".cute-cat-{}.part", job.id);
    let temp = output
        .parent()
        .context("output parent missing")?
        .join(temp_name);
    db.update_status(&job.id, "running", None).await?;
    let progress_db = db.clone();
    let progress_id = job.id.clone();
    let spec: crate::spec::TranscodeSpec =
        serde_json::from_str(&job.spec_json).context("parse saved transcode spec")?;
    spec.validate()?;
    media
        .transcode(
            &input,
            &temp,
            &spec,
            probe.duration_seconds,
            move |progress, speed| {
                let progress_db = progress_db.clone();
                let progress_id = progress_id.clone();
                let speed = speed.map(ToOwned::to_owned);
                tokio::spawn(async move {
                    let _ = progress_db
                        .update_progress(&progress_id, progress, speed.as_deref())
                        .await;
                });
            },
        )
        .await?;
    db.update_status(&job.id, "finalizing", None).await?;
    let final_probe = media.probe(&temp).await?;
    anyhow::ensure!(
        final_probe.duration_seconds.unwrap_or_default() > 0.0 || duration == 0.0,
        "output validation failed"
    );
    if output.exists() {
        tokio::fs::remove_file(&temp).await.ok();
        anyhow::bail!("output already exists");
    }
    tokio::fs::rename(&temp, &output)
        .await
        .context("publish output")?;
    db.update_progress(&job.id, 1.0, None).await?;
    db.update_status(&job.id, "succeeded", None).await?;
    info!(job_id = %job.id, path = %output.display(), "transcode job completed");
    Ok(())
}
