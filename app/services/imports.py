import csv
import os
import uuid

from fastapi import HTTPException, UploadFile

from app.config import settings

REQUIRED_COLUMNS = ["name", "email", "phone", "date_of_birth", "country"]
CHUNK_SIZE_BYTES = 1024 * 1024


def save_upload(file: UploadFile, job_id: uuid.UUID) -> str:
    # Reject files that do not end in .csv
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must have a .csv extension")

    # Make sure the upload folder exists and decide where the file will be saved
    os.makedirs(settings.upload_folder, exist_ok=True)
    file_path = os.path.join(settings.upload_folder, f"{job_id}.csv")
    max_upload_bytes = settings.max_upload_size_mb * 1024 * 1024

    # Write the upload to disk in chunks, counting bytes as we go
    total_bytes = 0
    too_large = False
    with open(file_path, "wb") as saved_file:
        while True:
            chunk = file.file.read(CHUNK_SIZE_BYTES)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_upload_bytes:
                too_large = True
                break
            saved_file.write(chunk)

    # If the file went over the size limit, delete the partial file and reject
    if too_large:
        os.remove(file_path)
        raise HTTPException(
            status_code=413,
            detail=f"File is larger than {settings.max_upload_size_mb} MB",
        )

    # Reject empty files
    if total_bytes == 0:
        os.remove(file_path)
        raise HTTPException(status_code=400, detail="File is empty")

    # Read only the first row (the header). utf-8-sig removes a BOM if Excel added one
    try:
        with open(file_path, newline="", encoding="utf-8-sig") as saved_file:
            header_row = next(csv.reader(saved_file), None)
    except (UnicodeDecodeError, csv.Error):
        os.remove(file_path)
        raise HTTPException(status_code=400, detail="File is not a readable CSV")

    # Reject files with no header row
    if header_row is None or all(column.strip() == "" for column in header_row):
        os.remove(file_path)
        raise HTTPException(status_code=400, detail="File has no header row")

    # Reject files that are missing any required column (extra columns are allowed)
    header_names = [column.strip() for column in header_row]
    missing_columns = [name for name in REQUIRED_COLUMNS if name not in header_names]
    if missing_columns:
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail=f"Missing required columns: {', '.join(missing_columns)}",
        )

    return file_path
