import logging
import uuid
import warnings
from datetime import datetime, timezone

from celery import Celery, Task, signals
from celery.exceptions import SecurityWarning, SoftTimeLimitExceeded
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.logging_config import job_context, setup_logging
from app.models import ImportJob

logger = logging.getLogger("celery_app")

# The worker container runs as root (the mounted uploads and reports folders belong to root), and Celery prints a
# plain-text warning about it at startup. Every log line must be JSON, so leave this warning out
warnings.filterwarnings("ignore", category=SecurityWarning)

# With the Redis broker, a task that runs longer than the visibility timeout is delivered to a second worker
# while the first one is still running it. So the timeout must be longer than the longest task
if settings.visibility_timeout_seconds <= settings.job_timeout_seconds + 30:
    raise ValueError(
        f"VISIBILITY_TIMEOUT_SECONDS ({settings.visibility_timeout_seconds}) must be more than "
        f"JOB_TIMEOUT_SECONDS ({settings.job_timeout_seconds}) + 30, "
        "otherwise a running task is delivered to a second worker"
    )

celery_app = Celery(
    "app",
    broker=settings.redis_url,
    include=["app.handlers.process_csv", "app.handlers.send_email", "app.handlers.test_handlers"],
)

celery_app.conf.update(
    # No result backend: nobody reads task results
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    # Acknowledge a task after it ran, not when it is received. A task whose worker dies is delivered again
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # A worker takes one task at a time, so a long import does not hold other tasks back
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Logging is set up by our own setup_logging signal handler below
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
    broker_transport_options={"visibility_timeout": settings.visibility_timeout_seconds},
    # Publishing gives up quickly when Redis is down, so POST /imports can answer 503 at once
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
)

# The name of this worker, for example "celery@3f2a9c1b7d4e". Set by the celeryd_init signal
worker_node_name = None


class ImportTask(Task):
    # Base class of process_csv. Saves the retry and failure on the import row. A cancel is never overwritten
    def _save_failure(self, new_status: str, error: BaseException, import_job_id: str) -> str:
        # The text of the error. A soft time limit error has no message of its own
        if isinstance(error, SoftTimeLimitExceeded):
            error_text = f"job timed out after {settings.job_timeout_seconds} seconds"
        else:
            error_text = str(error)[:1000]

        try:
            with SessionLocal() as db:
                # Read the row and lock it, so a cancel request cannot change it between our read and our write
                import_job = db.scalar(select(ImportJob).where(ImportJob.id == uuid.UUID(import_job_id)).with_for_update())

                # Already cancelled: change nothing
                if import_job is None or import_job.status == "cancelled":
                    return error_text

                # Cancel was requested while the task was failing: the cancel wins
                if import_job.status == "cancelling":
                    import_job.status = "cancelled"
                    import_job.error_message = "cancelled by user"
                    if import_job.finished_at is None:
                        import_job.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    return error_text

                # Any other status: record the retry or the failure
                import_job.status = new_status
                import_job.error_message = error_text
                if new_status == "failed":
                    import_job.finished_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            # The retry or failure itself is already decided, so only log this and carry on
            logger.exception("Could not save the %s status for import %s", new_status, import_job_id)
        return error_text

    def on_retry(self, exc, task_id, args, kwargs, einfo) -> None:
        error_text = self._save_failure("retrying", exc, args[0])
        logger.warning(
            "job retry",
            extra={"attempt": self.request.retries + 1, "max_attempts": settings.max_attempts, "error": error_text},
        )

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        error_text = self._save_failure("failed", exc, args[0])
        logger.error("job failed", extra={"attempt": self.request.retries + 1, "error": error_text})


@signals.celeryd_init.connect
def remember_node_name(sender: str, **kwargs) -> None:
    # sender is the name of this worker. The logging setup below needs it
    global worker_node_name
    worker_node_name = sender


@signals.setup_logging.connect
def set_up_worker_logging(**kwargs) -> None:
    # Connecting to this signal tells Celery not to install log handlers of its own
    setup_logging("worker", worker_node_name)


@signals.task_prerun.connect
def set_job_context(task_id: str, task: Task, args: tuple, **kwargs) -> None:
    # From here on every log line of this task carries the job fields
    job_fields = {"job_id": task_id, "job_type": task.name}
    if task.name in ("process_csv", "send_email"):
        job_fields["import_job_id"] = args[0]
    job_context.set(job_fields)


@signals.task_postrun.connect
def reset_job_context(**kwargs) -> None:
    # The retry and failure hooks run before this, so their log lines still have the job fields
    job_context.set({})
