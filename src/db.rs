use anyhow::{Context, Result};
use sqlx::{sqlite::SqlitePoolOptions, FromRow, SqlitePool};

#[derive(Clone)]
pub struct Database {
    pub pool: SqlitePool,
}

#[derive(Debug, Clone, FromRow, serde::Serialize)]
pub struct Job {
    pub id: String,
    pub input_root: String,
    pub input_path: String,
    pub output_root: String,
    pub output_path: String,
    pub preset_id: String,
    pub status: String,
    pub progress: f64,
    pub speed: Option<String>,
    pub duration_seconds: Option<f64>,
    pub error: Option<String>,
    pub created_at: String,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub spec_json: String,
}

#[derive(Debug, serde::Deserialize)]
pub struct NewJob {
    pub input_root: String,
    pub input_path: String,
    pub output_root: String,
    pub output_path: String,
    pub preset_id: String,
    #[serde(default)]
    pub spec: Option<crate::spec::TranscodeSpec>,
}

impl Database {
    pub async fn connect(url: &str) -> Result<Self> {
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect(url)
            .await
            .context("connect sqlite")?;
        sqlx::migrate!("./migrations")
            .run(&pool)
            .await
            .context("run database migrations")?;
        Ok(Self { pool })
    }

    pub async fn create_job(&self, request: &NewJob) -> Result<Job> {
        let id = uuid::Uuid::new_v4().to_string();
        let created_at = chrono::Utc::now().to_rfc3339();
        let spec = request.spec.clone().unwrap_or_else(|| {
            let mut spec = crate::spec::TranscodeSpec::default();
            match request.preset_id.as_str() {
                "h265-quality" => {
                    spec.video.encoder = crate::spec::VideoEncoder::H265;
                    spec.video.quality_value = 24.0;
                }
                "h264-small" => spec.video.quality_value = 28.0,
                _ => {}
            }
            spec
        });
        spec.validate()?;
        let spec_json = serde_json::to_string(&spec)?;
        sqlx::query("INSERT INTO jobs (id,input_root,input_path,output_root,output_path,preset_id,status,spec_json,created_at) VALUES (?,?,?,?,?,?, 'queued', ?, ?)")
            .bind(&id).bind(&request.input_root).bind(&request.input_path).bind(&request.output_root)
            .bind(&request.output_path).bind(&request.preset_id).bind(spec_json).bind(&created_at).execute(&self.pool).await?;
        self.get_job(&id)
            .await?
            .context("created job was not found")
    }

    pub async fn get_job(&self, id: &str) -> Result<Option<Job>> {
        Ok(sqlx::query_as::<_, Job>("SELECT * FROM jobs WHERE id = ?")
            .bind(id)
            .fetch_optional(&self.pool)
            .await?)
    }

    pub async fn list_jobs(&self) -> Result<Vec<Job>> {
        Ok(
            sqlx::query_as::<_, Job>("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")
                .fetch_all(&self.pool)
                .await?,
        )
    }

    pub async fn claim_next(&self) -> Result<Option<Job>> {
        let mut tx = self.pool.begin().await?;
        let job = sqlx::query_as::<_, Job>(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1",
        )
        .fetch_optional(&mut *tx)
        .await?;
        let Some(job) = job else {
            tx.commit().await?;
            return Ok(None);
        };
        let now = chrono::Utc::now().to_rfc3339();
        sqlx::query(
            "UPDATE jobs SET status = 'probing', started_at = ? WHERE id = ? AND status = 'queued'",
        )
        .bind(now)
        .bind(&job.id)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        self.get_job(&job.id).await
    }

    pub async fn update_progress(
        &self,
        id: &str,
        progress: f64,
        speed: Option<&str>,
    ) -> Result<()> {
        sqlx::query("UPDATE jobs SET progress = ?, speed = ? WHERE id = ?")
            .bind(progress.clamp(0.0, 1.0))
            .bind(speed)
            .bind(id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn update_status(&self, id: &str, status: &str, error: Option<&str>) -> Result<()> {
        let finished_at = matches!(status, "succeeded" | "failed" | "canceled" | "interrupted")
            .then(|| chrono::Utc::now().to_rfc3339());
        sqlx::query("UPDATE jobs SET status = ?, error = ?, finished_at = COALESCE(?, finished_at), progress = CASE WHEN ? = 'succeeded' THEN 1 ELSE progress END WHERE id = ?")
            .bind(status).bind(error).bind(finished_at).bind(status).bind(id).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn cancel(&self, id: &str) -> Result<bool> {
        let result = sqlx::query("UPDATE jobs SET status = 'canceled', finished_at = ? WHERE id = ? AND status IN ('queued','probing')")
            .bind(chrono::Utc::now().to_rfc3339()).bind(id).execute(&self.pool).await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn recover_running(&self) -> Result<()> {
        sqlx::query("UPDATE jobs SET status = 'interrupted', error = 'Service restarted while this job was running', finished_at = ? WHERE status IN ('probing','running','finalizing')")
            .bind(chrono::Utc::now().to_rfc3339()).execute(&self.pool).await?;
        Ok(())
    }
}
