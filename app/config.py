from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Read from environment variables (DATABASE_URL, REDIS_URL, UPLOAD_FOLDER, MAX_UPLOAD_SIZE_MB,
    # SMTP_HOST, SMTP_PORT, SMTP_FROM, DEFAULT_NOTIFY_EMAIL, APP_BASE_URL,
    # MAX_ATTEMPTS, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS,
    # HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_TTL_SECONDS, REAPER_INTERVAL_SECONDS, JOB_TIMEOUT_SECONDS)
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
    heartbeat_interval_seconds: int = 5
    heartbeat_ttl_seconds: int = 15
    reaper_interval_seconds: int = 5
    job_timeout_seconds: int = 600


settings = Settings()
