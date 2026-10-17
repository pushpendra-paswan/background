import logging
import smtplib
import uuid
from email.message import EmailMessage

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models import ImportJob

logger = logging.getLogger("send_email")


@celery_app.task(
    bind=True,
    name="send_email",
    autoretry_for=(OSError,),
    retry_backoff=settings.retry_base_seconds,
    retry_backoff_max=settings.retry_max_seconds,
    retry_jitter=True,
    max_retries=settings.max_attempts - 1,
)
def send_email(self, import_job_id: str) -> None:
    import_job_id = uuid.UUID(import_job_id)

    with SessionLocal() as db:
        # Load the import job row
        import_job = db.get(ImportJob, import_job_id)

        # A repeated job must not send a second email
        if import_job.email_status == "sent":
            logger.info("Email for import %s was already sent, skipping", import_job_id)
            return

        # Build the plain-text body
        body_lines = [
            f"Filename: {import_job.filename}",
            f"Status: {import_job.status}",
            f"Total rows: {import_job.total_rows}",
            f"Inserted rows: {import_job.valid_rows}",
            f"Rejected rows: {import_job.invalid_rows}",
            f"Started at: {import_job.started_at}",
            f"Finished at: {import_job.finished_at}",
        ]

        # Add the report link only when there is an error report
        if import_job.error_report_path is not None:
            body_lines.append(f"Error report: {settings.app_base_url}/imports/{import_job_id}/errors")

        # Build the email. Imports created before notify_email existed use the default address
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = import_job.notify_email or settings.default_notify_email
        message["Subject"] = f"Import finished: {import_job.filename} ({import_job.status})"
        message.set_content("\n".join(body_lines))

        # Send it. No TLS and no login (Mailpit does not need them)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp_server:
                smtp_server.send_message(message)
        except OSError as error:
            # SMTP errors, refused connections and timeouts are all OSError. Save the error, then re-raise so Celery retries the task
            import_job.email_status = "failed"
            import_job.email_error = str(error)
            db.commit()
            raise

        # Success: remember that the email was sent
        import_job.email_status = "sent"
        import_job.email_error = None
        db.commit()
        logger.info("Sent email for import %s to %s", import_job_id, message["To"])
