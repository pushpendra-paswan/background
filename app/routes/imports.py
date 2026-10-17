import logging
import uuid
from datetime import datetime, timezone

import redis
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from kombu.exceptions import OperationalError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.handlers.process_csv import process_csv
from app.models import ImportJob
from app.schemas import ImportJobCreatedResponse, ImportJobResponse
from app.services.imports import save_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("", status_code=202, response_model=ImportJobCreatedResponse)
def create_import(
    file: UploadFile,
    notify_email: str | None = Form(None),
    db: Session = Depends(get_db),
) -> ImportJobCreatedResponse:
    # Use the default address when notify_email is missing or blank. Otherwise it must be a valid email (nothing is created if not)
    if notify_email is None or notify_email.strip() == "":
        notify_email = settings.default_notify_email
    else:
        notify_email = notify_email.strip()
        try:
            validate_email(notify_email, check_deliverability=False)
        except EmailNotValidError:
            raise HTTPException(status_code=400, detail="notify_email is not a valid email address")

    # Create the job id first, because the saved file is named after it
    job_id = uuid.uuid4()

    # Validate and save the file (raises 400 or 413 and cleans up if it is rejected)
    file_path = save_upload(file, job_id)

    # Create the import job row with status "queued"
    import_job = ImportJob(
        id=job_id,
        filename=file.filename,
        file_path=file_path,
        status="queued",
        notify_email=notify_email,
    )
    db.add(import_job)
    db.commit()

    # Put a process_csv task on the queue. If Redis is down, mark the import as failed and return 503
    try:
        queue_job_id = process_csv.delay(str(job_id)).id
    except (OperationalError, redis.exceptions.RedisError):
        import_job.status = "failed"
        import_job.error_message = "Could not add the job to the queue (Redis is unavailable)"
        import_job.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=503, detail="Job queue is unavailable, try again later")

    # Log it with both ids: the import id and the Celery task id
    logger.info("import uploaded", extra={"import_job_id": str(job_id), "job_id": queue_job_id})

    # Return immediately. The worker does the processing
    return ImportJobCreatedResponse(job_id=job_id)


@router.get("/{job_id}", response_model=ImportJobResponse)
def get_import(job_id: uuid.UUID, db: Session = Depends(get_db)) -> ImportJob:
    # Look up the job by id
    import_job = db.get(ImportJob, job_id)

    # Return 404 if it does not exist
    if import_job is None:
        raise HTTPException(status_code=404, detail="Import job not found")

    return import_job


@router.get("", response_model=list[ImportJobResponse])
def list_imports(db: Session = Depends(get_db)) -> list[ImportJob]:
    # Return all import jobs, newest first
    query = select(ImportJob).order_by(ImportJob.created_at.desc())
    return list(db.scalars(query).all())


@router.post("/{job_id}/cancel", response_model=ImportJobResponse)
def cancel_import(job_id: uuid.UUID, response: Response, db: Session = Depends(get_db)) -> ImportJob:
    # Every change is one conditional UPDATE (never "read the status, then write"), so a cancel
    # cannot race with a worker. Try twice: a worker may change the status between our steps
    for _ in range(2):
        # Step 1: queued or retrying: no worker is running it, so it is cancelled at once
        cancelled_now = db.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id, ImportJob.status.in_(["queued", "retrying"]))
            .values(status="cancelled", finished_at=datetime.now(timezone.utc), error_message="cancelled by user")
        )
        db.commit()
        if cancelled_now.rowcount == 1:
            logger.info("cancel requested", extra={"import_job_id": str(job_id), "result": "cancelled"})
            return db.get(ImportJob, job_id)

        # Step 2: running: ask the worker to stop. It stops at the next batch boundary
        cancelling_now = db.execute(
            update(ImportJob).where(ImportJob.id == job_id, ImportJob.status == "running").values(status="cancelling")
        )
        db.commit()
        if cancelling_now.rowcount == 1:
            response.status_code = 202
            logger.info("cancel requested", extra={"import_job_id": str(job_id), "result": "cancelling"})
            return db.get(ImportJob, job_id)

        # Step 3: nothing changed, so read the row to see why
        import_job = db.get(ImportJob, job_id)
        if import_job is None:
            raise HTTPException(status_code=404, detail="Import job not found")

        # Already cancelling or cancelled: cancel is idempotent, so this is fine
        if import_job.status in ("cancelling", "cancelled"):
            logger.info("cancel requested", extra={"import_job_id": str(job_id), "result": import_job.status})
            return import_job

        # Already finished: it cannot be cancelled
        if import_job.status in ("completed", "completed_with_errors", "failed"):
            logger.info("cancel requested", extra={"import_job_id": str(job_id), "result": import_job.status})
            raise HTTPException(status_code=409, detail="import already finished")

    # A worker kept changing the status under us. Ask the caller to try again
    raise HTTPException(status_code=409, detail="import changed while cancelling, try again")


@router.get("/{job_id}/errors")
def download_errors(job_id: uuid.UUID, db: Session = Depends(get_db)) -> FileResponse:
    # Look up the job. It must exist and have an error report
    import_job = db.get(ImportJob, job_id)
    if import_job is None or import_job.error_report_path is None:
        raise HTTPException(status_code=404, detail="No error report for this import job")

    # Send the report file as a download
    return FileResponse(
        import_job.error_report_path,
        media_type="text/csv",
        filename=f"errors_{job_id}.csv",
    )
