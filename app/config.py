from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Read from environment variables (DATABASE_URL, REDIS_URL, UPLOAD_FOLDER, MAX_UPLOAD_SIZE_MB,
    # SMTP_HOST, SMTP_PORT, SMTP_FROM, DEFAULT_NOTIFY_EMAIL, APP_BASE_URL,
    # MAX_ATTEMPTS, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS,
    # JOB_TIMEOUT_SECONDS, VISIBILITY_TIMEOUT_SECONDS, WORKER_CONCURRENCY, LOG_LEVEL)
    database_url: str
    redis_url: str
    upload_folder: str = "uploads"
    max_upload_size_mb: int = 50
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_from: str = "noreply@example.com"
    default_notify_email: str = "admin@example.com"
    app_base_url: str = "http://localhost:8000"
    max_attempts: int = 3
    retry_base_seconds: int = 2
    retry_max_seconds: int = 60
    job_timeout_seconds: int = 600
    visibility_timeout_seconds: int = 900
    worker_concurrency: int = 1
    log_level: str = "INFO"


settings = Settings()
