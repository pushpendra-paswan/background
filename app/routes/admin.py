import logging
import uuid
from datetime import datetime, timezone

import redis
from fastapi import APIRouter, Depends, HTTPException
from kombu.exceptions import OperationalError
from sqlalchemy import extract, func, select, update
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import get_db
from app.handlers.process_csv import process_csv
from app.handlers.send_email import send_email
from app.models import ImportJob
from app.schemas import (
    JOB_STATUSES,
    DurationStats,
    EmailStats,
    ImportJobResponse,
    ImportStats,
    QueueStats,
    StatsResponse,
    WorkerStats,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

# Used to read the two queue numbers that Celery's inspect cannot give
redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


@router.get("/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)) -> StatsResponse:
    # Tasks waiting in the queue, and tasks that a worker took but has not acknowledged yet
    waiting_count = redis_client.llen("celery")
    in_flight_count = redis_client.hlen("unacked")

    # Ask the workers who is alive. This waits up to 1 second for the answers. No answer means no workers
    ping_replies = celery_app.control.inspect(timeout=1.0).ping() or {}
    worker_names = sorted(ping_replies.keys())

    # Ask the workers that answered what they are doing. Naming them lets Celery stop waiting as soon as they all answer
    active_count = 0
    scheduled_count = 0
    if len(worker_names) > 0:
        worker_inspector = celery_app.control.inspect(destination=worker_names, timeout=1.0)
        for tasks in (worker_inspector.active() or {}).values():
            active_count += len(tasks)
        for tasks in (worker_inspector.scheduled() or {}).values():
            scheduled_count += len(tasks)

    # Imports by status, from one GROUP BY query. A status with no imports counts 0
    import_counts = {status: 0 for status in JOB_STATUSES}
    for status, status_count in db.execute(select(ImportJob.status, func.count()).group_by(ImportJob.status)).all():
        import_counts[status] = status_count

    # Emails that could not be sent
    failed_email_count = db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.email_status == "failed"))

    # Durations of the finished imports, from one query. The average is empty (None) when there are none
    finished_count, average_run, average_total = db.execute(
        select(
            func.count(),
            func.avg(extract("epoch", ImportJob.finished_at - ImportJob.started_at)),
            func.avg(extract("epoch", ImportJob.finished_at - ImportJob.created_at)),
        ).where(ImportJob.status.in_(["completed", "completed_with_errors"]))
    ).one()
    if average_run is not None:
        average_run = round(float(average_run), 2)
    if average_total is not None:
        average_total = round(float(average_total), 2)

    # Build the answer
    return StatsResponse(
        queue=QueueStats(
            waiting=waiting_count, in_flight=in_flight_count, active=active_count, scheduled=scheduled_count
        ),
        workers=WorkerStats(alive=len(worker_names), names=worker_names),
        imports=ImportStats(total=sum(import_counts.values()), by_status=import_counts),
        emails=EmailStats(failed=failed_email_count),
        durations=DurationStats(
            finished_imports=finished_count, avg_run_seconds=average_run, avg_total_seconds=average_total
        ),
    )


@router.post("/imports/{job_id}/retry", status_code=202, response_model=ImportJobResponse)
def retry_import(job_id: uuid.UUID, db: Session = Depends(get_db)) -> ImportJob:
    # The import must exist
    if db.get(ImportJob, job_id) is None:
        raise HTTPException(status_code=404, detail="Import job not found")

    # Only a failed import can be retried. One conditional UPDATE, so two requests cannot both win.
    # The checkpoint and the report are kept, so the import resumes where it stopped
    retried = db.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.status == "failed")
        .values(status="queued", error_message=None, finished_at=None, attempts=0)
    )
    db.commit()
    if retried.rowcount == 0:
        raise HTTPException(status_code=409, detail="only failed imports can be retried")

    # Put a process_csv task on the queue. If that fails, the import goes back to "failed" and the caller gets 503
    try:
        process_csv.delay(str(job_id))
    except (OperationalError, redis.exceptions.RedisError):
        db.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id)
            .values(
                status="failed",
                error_message="Could not add the job to the queue (Redis is unavailable)",
                finished_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        raise HTTPException(status_code=503, detail="Job queue is unavailable, try again later")

    logger.info("import retried", extra={"import_job_id": str(job_id)})
    return db.get(ImportJob, job_id)


@router.post("/imports/{job_id}/resend-email", status_code=202, response_model=ImportJobResponse)
def resend_email(job_id: uuid.UUID, db: Session = Depends(get_db)) -> ImportJob:
    # The import must exist
    if db.get(ImportJob, job_id) is None:
        raise HTTPException(status_code=404, detail="Import job not found")

    # Only a failed email of a finished import can be sent again. One conditional UPDATE
    resend = db.execute(
        update(ImportJob)
        .where(
            ImportJob.id == job_id,
            ImportJob.email_status == "failed",
            ImportJob.status.in_(["completed", "completed_with_errors"]),
        )
        .values(email_status="pending", email_error=None)
    )
    db.commit()
    if resend.rowcount == 0:
        raise HTTPException(status_code=409, detail="email is not in a failed state")

    # Put a send_email task on the queue
    send_email.delay(str(job_id))

    logger.info("email resend requested", extra={"import_job_id": str(job_id)})
    return db.get(ImportJob, job_id)
