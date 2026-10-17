import csv
import logging
import os
import resource
import time
import uuid
from datetime import datetime, timezone

import redis
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.database import SessionLocal
from app.models import Customer, ImportJob
from app.queue.redis_queue import enqueue
from app.schemas import CustomerRow

logger = logging.getLogger("process_csv")

BATCH_SIZE = 1000
REPORTS_FOLDER = "reports"
CUSTOMER_FIELDS = ["name", "email", "phone", "date_of_birth", "country"]


def process_csv(payload: dict) -> None:
    import_job_id = uuid.UUID(payload["import_job_id"])

    # Remember when this attempt started, for the "finished" log line
    attempt_started_at = time.monotonic()

    # Counters for the whole file, and the lowercased emails seen so far (for duplicate checks)
    total_rows = 0
    valid_rows = 0
    invalid_rows = 0
    seen_emails: set[str] = set()

    # Make sure the reports folder exists
    os.makedirs(REPORTS_FOLDER, exist_ok=True)
    report_path = f"{REPORTS_FOLDER}/errors_{import_job_id}.csv"

    with SessionLocal() as db:
        # Load the import job row (the worker already set it to "running")
        import_job = db.get(ImportJob, import_job_id)

        # Decide: resume from the checkpoint, or start from scratch. Resume only if an earlier attempt
        # committed at least one batch, never finished, and its error report is still there and complete
        resume = False
        fresh_start_fields = {"line": 0, "rows_done": 0}
        if import_job.last_processed_row > 0 and import_job.finished_at is None:
            if not os.path.exists(report_path):
                logger.warning("import %s: the checkpoint cannot be trusted, the error report file is missing", import_job_id)
                fresh_start_fields["reason"] = "error report file is missing"
            elif os.path.getsize(report_path) < import_job.report_bytes:
                logger.warning(
                    "import %s: the checkpoint cannot be trusted, the error report file has %s bytes but %s were saved",
                    import_job_id,
                    os.path.getsize(report_path),
                    import_job.report_bytes,
                )
                fresh_start_fields["reason"] = "error report file is shorter than saved"
            else:
                resume = True

        if resume:
            # Continue with the totals saved in the last committed batch
            logger.info(
                "resuming import %s from line %s (%s rows already processed)",
                import_job_id,
                import_job.last_processed_row,
                import_job.total_rows,
                extra={"line": import_job.last_processed_row, "rows_done": import_job.total_rows},
            )
            total_rows = import_job.total_rows
            valid_rows = import_job.valid_rows
            invalid_rows = import_job.invalid_rows

            # Cut the report back to its size at the last commit. This removes rows written by a
            # batch that was never committed
            os.truncate(report_path, import_job.report_bytes)

            # Rebuild the seen emails from the customers this import already inserted, so a later row
            # that repeats one of them is still reported as a duplicate. Streamed, not loaded at once
            emails_of_this_import = select(Customer.email).where(Customer.import_job_id == import_job_id)
            for email in db.scalars(emails_of_this_import.execution_options(yield_per=BATCH_SIZE)):
                seen_emails.add(email)
        else:
            # Reset the counters and the checkpoint
            logger.info("starting import %s from scratch", import_job_id, extra=fresh_start_fields)
            import_job.total_rows = 0
            import_job.valid_rows = 0
            import_job.invalid_rows = 0
            import_job.last_processed_row = 0
            import_job.report_bytes = 0
            import_job.error_report_path = None
            import_job.finished_at = None

            # Create a new error report with its header, replacing any old one, and commit the reset
            with open(report_path, "w", encoding="utf-8", newline="") as new_report_file:
                csv.writer(new_report_file).writerow(["row_number", *CUSTOMER_FIELDS, "errors"])
            db.commit()

        # Count the data rows with a plain pass over the file (constant memory), for the progress percentage.
        # Same settings as the main pass. Empty rows are not counted, because DictReader skips them too
        rows_in_file = 0
        with open(import_job.file_path, encoding="utf-8-sig", newline="") as count_file:
            count_reader = csv.reader(count_file)
            next(count_reader, None)
            for counted_row in count_reader:
                if counted_row:
                    rows_in_file += 1
        import_job.rows_in_file = rows_in_file
        db.commit()
        logger.info("import %s: %s data rows in the file", import_job_id, rows_in_file)

        # Rows up to this line were handled by an earlier attempt and are skipped (0 on a fresh start).
        # Copied into a local variable because the checkpoint on import_job moves forward with every batch
        skip_until_line = import_job.last_processed_row
        rows_at_attempt_start = total_rows
        batches_done = 0
        cancel_requested = False

        # Open the CSV and the error report (append mode: it already has its header). A missing or
        # undecodable file raises here (or while reading), so the worker retries and finally marks the job "failed"
        with open(import_job.file_path, encoding="utf-8-sig", newline="") as csv_file, open(
            report_path, "a", encoding="utf-8", newline=""
        ) as report_file:
            reader = csv.DictReader(csv_file)
            report_writer = csv.writer(report_file)

            # Trim the column names, the same way the upload check does
            reader.fieldnames = [column_name.strip() for column_name in reader.fieldnames]

            while True:
                # Read the next batch (at most BATCH_SIZE rows), keeping each row's line number
                batch_rows = []
                for raw_row in reader:
                    # Skip rows that an earlier attempt already committed
                    if reader.line_num <= skip_until_line:
                        continue
                    batch_rows.append((reader.line_num, raw_row))
                    if len(batch_rows) == BATCH_SIZE:
                        break

                # No rows left, so the file is finished
                if len(batch_rows) == 0:
                    break

                valid_in_batch = []
                invalid_in_batch = []

                for line_number, raw_row in batch_rows:
                    # Strip whitespace. Missing and empty cells are left out, so Pydantic reports them as missing
                    cleaned_row = {}
                    for field_name in CUSTOMER_FIELDS:
                        value = (raw_row.get(field_name) or "").strip()
                        if value != "":
                            cleaned_row[field_name] = value

                    # Validate the row and collect ALL of its errors
                    error_messages = []
                    customer = None
                    try:
                        customer = CustomerRow(**cleaned_row)
                    except ValidationError as validation_error:
                        for item in validation_error.errors():
                            if item["type"] == "missing":
                                reason = "is required"
                            else:
                                reason = item["msg"].removeprefix("Value error, ")
                            error_messages.append(f"{item['loc'][0]}: {reason}")

                    # A valid row must not repeat an email from earlier in the file
                    if customer is not None:
                        if customer.email in seen_emails:
                            error_messages.append("email: duplicate email in file")
                        else:
                            seen_emails.add(customer.email)

                    # Sort the row into valid or invalid
                    if len(error_messages) > 0:
                        invalid_in_batch.append((line_number, raw_row, "; ".join(error_messages)))
                    else:
                        valid_in_batch.append((line_number, raw_row, customer))

                # Emails in this batch whose customer exists because of this import
                emails_of_this_import: set[str] = set()

                if len(valid_in_batch) > 0:
                    # Sort by email. Two imports with overlapping emails then take their row locks in the
                    # same order, so Postgres cannot deadlock them
                    valid_in_batch.sort(key=lambda valid_row: valid_row[2].email)

                    # Insert all valid rows with one statement. A conflict on email is skipped, and RETURNING
                    # tells us which rows were really inserted
                    rows_to_insert = [{**customer.model_dump(), "import_job_id": import_job_id} for _, _, customer in valid_in_batch]
                    insert_statement = insert(Customer).values(rows_to_insert)
                    insert_statement = insert_statement.on_conflict_do_nothing(index_elements=["email"]).returning(Customer.email)
                    emails_of_this_import.update(db.scalars(insert_statement))

                    # Some emails were skipped. One query: which of them belong to this import? Those were
                    # inserted by an earlier run of this same import, so they stay valid
                    skipped_emails = [customer.email for _, _, customer in valid_in_batch if customer.email not in emails_of_this_import]
                    if len(skipped_emails) > 0:
                        emails_of_this_import.update(
                            db.scalars(
                                select(Customer.email).where(
                                    Customer.email.in_(skipped_emails),
                                    Customer.import_job_id == import_job_id,
                                )
                            )
                        )

                # Count the valid rows. Any other skipped row has an email that belongs to a different import
                # (finished earlier, or inserting at the same moment), so it becomes invalid
                valid_in_batch_count = 0
                for line_number, raw_row, customer in valid_in_batch:
                    if customer.email in emails_of_this_import:
                        valid_in_batch_count += 1
                    else:
                        invalid_in_batch.append((line_number, raw_row, "email: email already exists"))

                # Write the invalid rows (now including the "already exists" ones) to the error report in line order,
                # using the original values
                invalid_in_batch.sort(key=lambda invalid_row: invalid_row[0])
                for line_number, raw_row, error_text in invalid_in_batch:
                    original_values = [raw_row.get(field_name) or "" for field_name in CUSTOMER_FIELDS]
                    report_writer.writerow([line_number, *original_values, error_text])
                report_file.flush()

                # Read the size of the report file after this batch
                report_size = os.fstat(report_file.fileno()).st_size

                # Save the checkpoint: the counters, the last line and the report size, in the same
                # transaction as the insert, then commit once
                total_rows += len(batch_rows)
                valid_rows += valid_in_batch_count
                invalid_rows += len(invalid_in_batch)
                import_job.total_rows = total_rows
                import_job.valid_rows = valid_rows
                import_job.invalid_rows = invalid_rows
                import_job.last_processed_row = reader.line_num
                import_job.report_bytes = report_size
                db.commit()

                # Log the progress after every 20th batch
                batches_done += 1
                if batches_done % 20 == 0:
                    logger.info(
                        "import %s: processed up to line %s",
                        import_job_id,
                        reader.line_num,
                        extra={
                            "line": reader.line_num,
                            "total_rows": total_rows,
                            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                        },
                    )

                # Was a cancel requested? A fresh query for the status, only after the commit, so the
                # checkpoint always matches the database. If so, stop reading rows
                current_status = db.scalar(select(ImportJob.status).where(ImportJob.id == import_job_id))
                if current_status == "cancelling":
                    cancel_requested = True
                    break

        # With no invalid rows there is no report. Otherwise remember where it is
        if invalid_rows == 0:
            os.remove(report_path)
        else:
            import_job.error_report_path = report_path

        # Finish the job with one conditional UPDATE: only if the status is still "running".
        # If no row changed, the import was cancelled in the meantime (a cancel that arrived too late
        # for this UPDATE does not undo a normal completion)
        if not cancel_requested:
            if invalid_rows == 0:
                final_status = "completed"
            else:
                final_status = "completed_with_errors"
            finished = db.execute(
                update(ImportJob)
                .where(ImportJob.id == import_job_id, ImportJob.status == "running")
                .values(status=final_status, finished_at=datetime.now(timezone.utc))
            )
            db.commit()
            if finished.rowcount == 0:
                cancel_requested = True

        # Cancelled: set the final cancelled status. No email is sent for a cancelled import.
        # Customers already inserted and the report written so far stay (no rollback)
        if cancel_requested:
            cancelled = db.execute(
                update(ImportJob)
                .where(ImportJob.id == import_job_id, ImportJob.status.in_(["running", "cancelling"]))
                .values(status="cancelled", finished_at=datetime.now(timezone.utc), error_message="cancelled by user")
            )
            db.commit()
            if cancelled.rowcount == 0:
                # Someone else changed the status (for example the reaper recovered this job). Leave it as it is
                logger.warning("import %s: the status changed while stopping, leaving it as it is", import_job_id)
            else:
                logger.info(
                    "import %s cancelled at line %s after %s rows",
                    import_job_id,
                    import_job.last_processed_row,
                    import_job.total_rows,
                    extra={"line": import_job.last_processed_row, "total_rows": import_job.total_rows},
                )

                # Summary of this attempt. rows_per_second counts only the rows processed in this attempt
                duration_seconds = time.monotonic() - attempt_started_at
                logger.info(
                    "import %s finished",
                    import_job_id,
                    extra={
                        "status": "cancelled",
                        "total_rows": total_rows,
                        "valid_rows": valid_rows,
                        "invalid_rows": invalid_rows,
                        "duration_seconds": round(duration_seconds, 1),
                        "rows_per_second": round((total_rows - rows_at_attempt_start) / max(duration_seconds, 0.001)),
                        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                    },
                )
            return

        # Summary of this attempt, after the final status is committed. rows_per_second counts only the rows
        # processed in this attempt (a resumed import skips the rows an earlier attempt did)
        duration_seconds = time.monotonic() - attempt_started_at
        logger.info(
            "import %s finished",
            import_job_id,
            extra={
                "status": final_status,
                "total_rows": total_rows,
                "valid_rows": valid_rows,
                "invalid_rows": invalid_rows,
                "duration_seconds": round(duration_seconds, 1),
                "rows_per_second": round((total_rows - rows_at_attempt_start) / max(duration_seconds, 0.001)),
                "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            },
        )

        # Chain the email job. If the queue is down, record it and finish normally: the import itself is done
        try:
            enqueue("send_email", {"import_job_id": str(import_job_id)})
        except redis.exceptions.RedisError as error:
            import_job.email_status = "failed"
            import_job.email_error = str(error)
            db.commit()
