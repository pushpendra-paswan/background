import uuid
from datetime import datetime, timezone

import redis
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ImportJob
from app.queue.redis_queue import enqueue
from app.schemas import ImportJobCreatedResponse, ImportJobResponse
from app.services.imports import save_upload

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

    # Put a process_csv job on the queue. If Redis is down, mark the import as failed and return 503
    try:
        enqueue("process_csv", {"import_job_id": str(job_id)})
    except redis.exceptions.RedisError:
        import_job.status = "failed"
        import_job.error_message = "Could not add the job to the queue (Redis is unavailable)"
        import_job.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=503, detail="Job queue is unavailable, try again later")

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
