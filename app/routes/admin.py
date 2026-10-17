import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import extract, func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ImportJob
from app.queue.redis_queue import (
    DEAD_KEY,
    DELAYED_KEY,
    PROCESSING_KEY_PREFIX,
    QUEUE_KEY,
    WORKER_KEY_PREFIX,
    redis_client,
    replay_dead_job_script,
)
from app.schemas import (
    JOB_STATUSES,
    DeadJobResponse,
    DurationStats,
    ImportStats,
    QueueStats,
    ReplayedJobResponse,
    StatsResponse,
    WorkerStats,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


@router.get("/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)) -> StatsResponse:
    # Queue sizes: the waiting list, the retry set and the dead letter queue
    waiting_count = redis_client.llen(QUEUE_KEY)
    delayed_count = redis_client.zcard(DELAYED_KEY)
    dead_count = redis_client.llen(DEAD_KEY)

    # Jobs being processed: add up the lists of all workers. SCAN is used instead of KEYS because it does not block Redis
    processing_count = 0
    for processing_key in redis_client.scan_iter(match=PROCESSING_KEY_PREFIX + "*"):
        processing_count += redis_client.llen(processing_key)

    # Workers that are alive: one heartbeat key per worker
    worker_ids = []
    for worker_key in redis_client.scan_iter(match=WORKER_KEY_PREFIX + "*"):
        worker_ids.append(worker_key[len(WORKER_KEY_PREFIX):])
    worker_ids.sort()

    # Imports by status, from one GROUP BY query. A status with no imports counts 0
    import_counts = {status: 0 for status in JOB_STATUSES}
    for status, status_count in db.execute(select(ImportJob.status, func.count()).group_by(ImportJob.status)).all():
        import_counts[status] = status_count

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
        queue=QueueStats(waiting=waiting_count, delayed=delayed_count, processing=processing_count, dead=dead_count),
        workers=WorkerStats(alive=len(worker_ids), ids=worker_ids),
        imports=ImportStats(total=sum(import_counts.values()), by_status=import_counts),
        durations=DurationStats(
            finished_imports=finished_count, avg_run_seconds=average_run, avg_total_seconds=average_total
        ),
    )


@router.get("/dead-jobs", response_model=list[DeadJobResponse])
def list_dead_jobs(limit: int = Query(100, ge=1, le=1000)) -> list[DeadJobResponse]:
    # Read the newest dead messages (they are pushed on the left, so index 0 is the newest)
    dead_jobs = []
    for message in redis_client.lrange(DEAD_KEY, 0, limit - 1):
        # A message that is not valid JSON is skipped
        try:
            job = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("skipping a dead message that is not valid JSON", extra={"message_start": message[:100]})
            continue

        dead_jobs.append(
            DeadJobResponse(
                job_id=job["job_id"],
                job_type=job["job_type"],
                payload=job["payload"],
                attempts=job["attempts"],
                max_attempts=job["max_attempts"],
                last_error=job.get("last_error"),
                failed_at=job.get("failed_at"),
            )
        )
    return dead_jobs


@router.post("/dead-jobs/{job_id}/replay", response_model=ReplayedJobResponse)
def replay_dead_job(job_id: str, db: Session = Depends(get_db)) -> ReplayedJobResponse:
    # Find the dead message with this job id: read the whole list and compare
    original_message = None
    for message in redis_client.lrange(DEAD_KEY, 0, -1):
        try:
            job = json.loads(message)
        except json.JSONDecodeError:
            continue
        if job["job_id"] == job_id:
            original_message = message
            break
    if original_message is None:
        raise HTTPException(status_code=404, detail="dead job not found")

    # Build the new message: same job, attempts back to 0, no error and no failed_at
    new_job = {
        "job_id": job["job_id"],
        "job_type": job["job_type"],
        "payload": job["payload"],
        "attempts": 0,
        "max_attempts": settings.max_attempts,
    }

    # Move it from "dead" to "queue" in one Lua script. It returns 0 if another request replayed it first
    moved = replay_dead_job_script(keys=[DEAD_KEY, QUEUE_KEY], args=[original_message, json.dumps(new_job)])
    if moved != 1:
        raise HTTPException(status_code=404, detail="dead job not found")

    # A failed import goes back to "queued". One conditional UPDATE, so any other status is left alone.
    # The checkpoint and the report are still there, so the next attempt resumes from them
    if new_job["job_type"] == "process_csv":
        db.execute(
            update(ImportJob)
            .where(ImportJob.id == uuid.UUID(new_job["payload"]["import_job_id"]), ImportJob.status == "failed")
            .values(status="queued", error_message=None, finished_at=None)
        )
        db.commit()

    # Log it, with the import id when the job has one
    log_fields = {"job_id": new_job["job_id"], "job_type": new_job["job_type"]}
    if "import_job_id" in new_job["payload"]:
        log_fields["import_job_id"] = new_job["payload"]["import_job_id"]
    logger.info("dead job replayed", extra=log_fields)

    return ReplayedJobResponse(**new_job, queued=True)
