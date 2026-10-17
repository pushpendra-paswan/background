# PROJECT_CONTEXT.md

## Project summary

CSV Customer Import: a learning project. An API accepts a CSV of customer records. A background worker validates each row, inserts the valid ones into Postgres, writes a report of invalid rows, and (from Milestone 4.1) sends an email when done. The job queue is hand-built on Redis, because the point is to learn how job systems work (claiming, acknowledgement, retries, dead letter queue, crash recovery). See `CLAUDE.md` for the tech stack, coding style and rules.

## Current phase and milestone

- **Phase 1: Foundation and Upload API**: Milestone 1.1 DONE
- **Phase 2: Redis Queue and Worker**: Milestone 2.1 DONE
- **Phase 3: CSV Processing Pipeline**: Milestone 3.1 DONE
- **Phase 4: Email Notification Job**: Milestone 4.1 DONE
- **Phase 5: Reliability**: Milestone 5.1 DONE (retries, backoff, dead letter queue). Milestone 5.2 DONE (crash recovery: heartbeats, reaper, job timeout, graceful shutdown). Milestone 5.3 DONE (checkpoint and resume)
- **Phase 6: Scale and Visibility**: Milestone 6.1 DONE (multiple workers, progress). Milestone 6.2 DONE (cancel). Milestone 6.3 DONE (stats, dead letter queue endpoints, structured logging, large-file test). All planned milestones are done

## What was built

- Docker Compose stack: `api`, `worker`, `postgres`, `redis`, `mailpit`. Mailpit receives the summary emails (4.1).
- FastAPI app with three endpoints (below).
- SQLAlchemy 2.0 (sync) models for `customers` and `import_jobs`, with UUID primary keys.
- Alembic with one initial migration that creates both tables.
- Upload handling: extension check, chunked write to disk with a size limit, empty-file check, header check.
- **Milestone 2.1:** a hand-built Redis job queue, a separate worker process, and a handler registry. `POST /imports` now enqueues a `process_csv` job, and the worker picks it up and updates the status in Postgres. `process_csv` was a placeholder at that point.
- **Milestone 3.1:** `process_csv` is now the real pipeline: it streams the CSV, validates every row, bulk inserts the valid ones, writes an error report for the invalid ones, and keeps the counters up to date. New endpoint `GET /imports/{id}/errors` and a test data script. See "CSV processing (Milestone 3.1)" below.
- **Milestone 4.1:** when `process_csv` finishes, it enqueues a separate `send_email` job that sends a summary email through Mailpit. The worker now only touches `import_jobs` for `process_csv` jobs. See "Email notification (Milestone 4.1)" below.
- **Milestone 5.1:** failed jobs are retried (default 3 attempts) with exponential backoff and jitter, and then moved to a dead letter queue. `process_csv` is now safe to run more than once. See "Retries and dead letter queue (Milestone 5.1)" below.
- **Milestone 5.2:** a job left behind by a dead worker is found and recovered (each worker has its own processing list and a heartbeat key, and every worker runs a reaper). A job that runs too long is stopped (timeout). A worker told to stop (SIGTERM / SIGINT) finishes its current job and exits cleanly. See "Crash recovery (Milestone 5.2)" below.
- **Milestone 5.3:** `process_csv` saves a checkpoint after every batch (counters, `last_processed_row` and the size of the error report, all committed together with the insert). A retry, a timeout retry or a job recovered by the reaper continues from the checkpoint instead of re-reading everything. See "Checkpoint and resume (Milestone 5.3)" below.
- **Milestone 6.1:** several workers can run at the same time (`docker compose up -d --scale worker=3`) and several imports can run in parallel with exact counters, even when they contain the same emails. `GET /imports/{id}` shows `rows_in_file` and `progress_percent`. See "Multiple workers and progress (Milestone 6.1)" below.
- **Milestone 6.2:** `POST /imports/{id}/cancel`. A queued or retrying import is cancelled at once; a running import goes to `cancelling` and stops at the next batch boundary. Every status change made by a worker is now a conditional update, so a cancel is never overwritten. See "Cancel (Milestone 6.2)" below.
- **Milestone 6.3:** every log line is one JSON object, and every line about a job carries its `job_id` (`app/logging_config.py`). `GET /stats` shows queue sizes, workers, imports by status and average durations. `GET /dead-jobs` lists the dead letter queue and `POST /dead-jobs/{job_id}/replay` moves a dead job back to the queue (a failed import then resumes from its checkpoint). A 500,000-row import was run with memory measured. See "Observability (Milestone 6.3)" and "Large-file test (Milestone 6.3)" below.

## Files

| File | Purpose |
|---|---|
| `requirements.txt` | Python dependencies (pinned). 3.1 added `email-validator` and `faker` |
| `.env.example` | Template for `.env` (database URL, Redis URL, upload folder, max upload size, Postgres credentials, the 4.1 email settings, the 5.1 retry settings, the 5.2 crash recovery settings and the 6.3 `LOG_LEVEL`) |
| `.gitignore` | Ignores `.env`, caches, uploaded files and generated reports |
| `Dockerfile` | Python 3.12 image for the API |
| `docker-compose.yml` | Runs api, worker, postgres, redis, mailpit. Mounts `./uploads` and `./reports` into both the api and worker containers (so the api can serve files the worker wrote). The worker has `restart: unless-stopped`, and 5.1 passes `MAX_ATTEMPTS`, `RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS` to it (`${VAR:-default}`, so a value in `.env` or in the shell wins). The worker service has no `container_name` and no published ports, so `--scale worker=3` works (6.1). 5.2 passes `HEARTBEAT_INTERVAL_SECONDS`, `HEARTBEAT_TTL_SECONDS`, `REAPER_INTERVAL_SECONDS` and `JOB_TIMEOUT_SECONDS` the same way and sets `stop_grace_period: 30s` on the worker |
| `alembic.ini` | Alembic config (the database URL comes from `app/config.py`) |
| `alembic/env.py` | Alembic runtime: connects using `settings.database_url`, uses `Base.metadata` |
| `alembic/script.py.mako` | Template Alembic uses for new migrations |
| `alembic/versions/0001_create_customers_and_import_jobs.py` | Initial migration (hand-written) |
| `alembic/versions/0002_add_email_columns.py` | 4.1: adds `notify_email`, `email_status`, `email_error` to `import_jobs` |
| `alembic/versions/0003_add_customer_import_job_id.py` | 5.1: adds `customers.import_job_id` |
| `alembic/versions/0004_add_report_bytes.py` | 5.3: adds `import_jobs.report_bytes` (hand-written) |
| `alembic/versions/0005_add_rows_in_file.py` | 6.1: adds `import_jobs.rows_in_file` (hand-written) |
| `app/config.py` | Settings from environment variables (`redis_url`, the five email settings since 4.1, and `max_attempts`, `retry_base_seconds`, `retry_max_seconds` since 5.1, the four crash recovery settings since 5.2, and `log_level` since 6.3, default `INFO`) |
| `app/database.py` | Engine, session factory, `Base`, and the `get_db` dependency |
| `app/models.py` | `Customer` (has `import_job_id` since 5.1) and `ImportJob` models (`ImportJob` has the three email columns since 4.1, `report_bytes` since 5.3 and `rows_in_file` since 6.1) |
| `app/schemas.py` | Pydantic response models (`ImportJobResponse` (now with `notify_email` and `email_status`, and since 6.1 `rows_in_file` and the computed field `progress_percent`), `ImportJobCreatedResponse`), the `CustomerRow` row validator and the `ALLOWED_COUNTRY_CODES` constant. Since 6.3 also `JOB_STATUSES` (the eight statuses) and the models of `GET /stats` (`StatsResponse` with `QueueStats`, `WorkerStats`, `ImportStats`, `DurationStats`) and of the dead job endpoints (`DeadJobResponse`, `ReplayedJobResponse`) |
| `app/services/imports.py` | `save_upload`: all upload validation and saving in one function |
| `app/routes/imports.py` | `POST /imports` (validates `notify_email`, enqueues the job), `GET /imports/{id}`, `GET /imports`, `GET /imports/{id}/errors`, and since 6.2 `POST /imports/{id}/cancel`. Since 6.3 the upload and the cancel log a line (`import uploaded`, `cancel requested`) |
| `app/main.py` | Creates the FastAPI app, calls `setup_logging("api")` when the module is loaded (6.3) and includes the `imports` and `admin` routers |
| `app/logging_config.py` | 6.3: JSON logging. The `job_context` ContextVar, `JobContextFilter`, `JsonFormatter` and `setup_logging(service, worker_id=None)`. Standard library only |
| `app/routes/admin.py` | 6.3: `GET /stats`, `GET /dead-jobs` and `POST /dead-jobs/{job_id}/replay` |
| `app/queue/__init__.py` | Empty, marks the package |
| `app/queue/redis_queue.py` | Redis connection (`redis_client`), the key names and prefixes, `enqueue`, the Lua script and `promote_due_jobs` (5.1), the recovery Lua script `recover_job_script` (5.2) and the replay Lua script `replay_dead_job_script` (6.3) |
| `app/queue/worker.py` | The worker loop (`run_worker`), run with `python -m app.queue.worker`. Has the retry / dead letter logic since 5.1. Since 5.2 it also has the worker id (6.1: the claim and the reaper update `import_jobs` with one atomic `UPDATE` each, see "Multiple workers and progress"), the heartbeat thread function (`send_heartbeats`), the reaper (`reap_dead_workers`), `JobTimeoutError` with its SIGALRM handler, and the shutdown signal handler. Since 6.2 it also has `save_failure_status` (the one shared function that records `retrying` / `failed` without overwriting a cancel, used by the failure path and the reaper) and the conditional start step with the skip path for cancelled imports. Since 6.3 it calls `setup_logging("worker", worker_id)` at startup, sets `job_context` for every claimed job (reset in a `finally` at the end of the iteration), and adds fields (`extra=`) to the retry, dead and reaper log lines |
| `app/handlers/__init__.py` | The handler registry dictionary `HANDLERS` (`process_csv`, `send_email`, `sleep_job`, `always_fail`, `flaky_job`, `tracked_job`) |
| `app/handlers/process_csv.py` | The real CSV pipeline, one function (`BATCH_SIZE = 1000`). Since 6.1 it counts the rows of the file at the start of every attempt and inserts each batch sorted by email with `RETURNING`. Enqueues `send_email` at the end (4.1). Since 5.3 it saves a checkpoint after every batch and resumes from it when it can; otherwise it starts from scratch. Since 6.2 it checks for a cancel after every batch commit, finishes with a conditional `UPDATE`, and has the cancel ending. Since 6.3 the progress line has `peak_rss_mb`, and a summary line `import <id> finished` is logged at both endings |
| `app/handlers/send_email.py` | 4.1: the `send_email` handler, one function |
| `app/handlers/test_handlers.py` | `sleep_job`, `always_fail` and `flaky_job` (5.1) and `tracked_job` (6.1), for testing the worker |
| `scripts/enqueue_test_job.py` | Enqueues a job by type without uploading a file. `--import-job-id <id>` makes the payload `{"import_job_id": ...}`, to resend an email (4.1) or to run `process_csv` again. 5.1 added `flaky_job` with `--name` and `--fail-times` |
| `scripts/generate_test_csv.py` | Generates a test customers CSV with deliberately bad rows and prints the expected counts. 6.1 added `--email-prefix` |
| `scripts/check_no_double_claim.py` | 6.1: enqueues `tracked_job` jobs and checks that every one ran exactly once, and how many each worker ran |
| `uploads/.gitkeep` | Keeps the uploads folder in the project (uploaded files are git-ignored) |
| `reports/.gitkeep` | Keeps the reports folder in the project (error reports are git-ignored) |

The `__init__.py` files in `app/`, `app/routes/` and `app/services/` are empty and only mark the packages.

## Endpoints

| Endpoint | Behavior |
|---|---|
| `POST /imports` | Multipart upload, field `file`, optional form field `notify_email` (blank or missing means `DEFAULT_NOTIFY_EMAIL`; an invalid one returns `400` before anything is saved). Saves to `uploads/<job_id>.csv`, creates an `import_jobs` row with status `queued`, calls `enqueue("process_csv", {"import_job_id": "<id>"})`, returns `202 {"job_id": "..."}`. Errors: `400` (invalid `notify_email`, not .csv, empty, no header, missing required columns), `413` (over size limit), `503` (Redis is down: the row is set to `failed` with an `error_message` and `finished_at`) |
| `GET /imports/{id}` | Returns id, filename, status, rows_in_file, progress_percent, total_rows, valid_rows, invalid_rows, error_message, created_at, started_at, finished_at, notify_email, email_status. `404` if unknown (`422` if the id is not a valid UUID) |
| `GET /imports` | All jobs, newest first, with the same fields as `GET /imports/{id}`. No pagination or filtering |
| `GET /imports/{id}/errors` | Downloads the invalid-rows report (`FileResponse`, `text/csv`, filename `errors_<id>.csv`). `404` if the job does not exist or has no error report (for example, a job with 0 invalid rows) |
| `POST /imports/{id}/cancel` | 6.2. Cancels an import and returns the same body as `GET /imports/{id}`. `200` if it is now `cancelled` (it was `queued` or `retrying`) or already `cancelling` / `cancelled` (idempotent), `202` if it was `running` and is now `cancelling`, `409` `import already finished` for `completed`, `completed_with_errors` and `failed`, `404` if unknown, `422` for an invalid UUID. See "Cancel (Milestone 6.2)" |
| `GET /stats` | 6.3. Queue sizes, live workers, imports by status and average durations, see "Observability (Milestone 6.3)" |
| `GET /dead-jobs?limit=100` | 6.3. The dead letter queue, newest first. `limit` is 1 to 1000 (`422` otherwise). Each item: `job_id`, `job_type`, `payload`, `attempts`, `max_attempts`, `last_error`, `failed_at` |
| `POST /dead-jobs/{job_id}/replay` | 6.3. Moves a dead job back to `queue` with `attempts` 0 and returns `200` with the new message and `"queued": true`. `404` `dead job not found` if there is no such dead job (or another request replayed it first) |

Required CSV columns: `name`, `email`, `phone`, `date_of_birth`, `country`. Extra columns are allowed.

## Tables

**customers**: `id` UUID PK, `name` varchar(255), `email` varchar(255) unique, `phone` varchar(50), `date_of_birth` date nullable, `country` varchar(100), `created_at` timestamptz (default now).

**import_jobs**: `id` UUID PK, `filename` varchar(255), `file_path` varchar(500), `status` varchar(30) (default `queued`), `rows_in_file` integer nullable (6.1: data rows in the CSV, null until the first attempt has counted them), `total_rows`, `valid_rows`, `invalid_rows`, `last_processed_row`, `attempts` (integers, default 0), `report_bytes` bigint (default 0, since 5.3: size in bytes of the error report file as of the last committed batch), `error_report_path` varchar(500) nullable, `error_message` text nullable, `created_at` timestamptz (default now), `started_at` and `finished_at` timestamptz nullable, and since 4.1: `notify_email` varchar(255) nullable (only old rows are null), `email_status` varchar(20) (default `pending`), `email_error` text nullable.

**customers** also has, since 5.1, `import_job_id` UUID nullable (no foreign key): the import that inserted the customer. It is null for customers that were not created by an import.

`email_status` values: `pending` (no email sent yet), `sent`, `failed` (SMTP error, or the `send_email` job could not be enqueued). The import `status` and `email_status` are separate: an email problem never changes the import status.

Job statuses (from CLAUDE.md): `queued`, `running`, `retrying`, `cancelling`, `cancelled`, `completed`, `completed_with_errors`, `failed`. All eight are used now (`completed_with_errors` since 3.1, `retrying` since 5.1, `cancelling` and `cancelled` since 6.2). `retrying` means the last attempt failed and another one is scheduled; `failed` now means every attempt failed. `cancelling` means a cancel was requested while a worker is running the import (it has not stopped yet); `cancelled` is final. The finished states are `completed`, `completed_with_errors`, `failed` and `cancelled`; all of them have `finished_at` set.

## Queue design (Milestone 2.1)

**Redis keys** (`delayed` and `dead` since 5.1, `processing:<worker_id>` and `worker:<worker_id>` since 5.2):

| Key | Type | Meaning |
|---|---|---|
| `queue` | list | Jobs waiting to run |
| `processing:<worker_id>` | list | Jobs claimed by that one worker and not yet finished. One list per worker. Redis deletes the key by itself when the list becomes empty |
| `worker:<worker_id>` | string, with TTL | The worker's heartbeat: the current Unix time, with a TTL of `HEARTBEAT_TTL_SECONDS`. If the key is gone, the worker is dead or stopped |
| `delayed` | sorted set | Jobs waiting to be retried. Member = the message JSON, score = Unix time when the job may run again |
| `dead` | list | The dead letter queue: jobs that used up all attempts, or have an unknown type |

**Job message** (one JSON string per list item):

```json
{"job_id": "<uuid>", "job_type": "<string>", "payload": {}, "attempts": 0, "max_attempts": 3}
```

`attempts` is the number of times the job has **already failed**, so the current run is attempt number `attempts + 1`. `enqueue(job_type, payload)` makes a new `job_id` (not the same as `import_job_id`), `LPUSH`es the JSON onto `queue`, and returns the `job_id`. When a job is retried or dead, the worker builds a **new** message from the old one with `attempts + 1` and `last_error` set to the error text. A dead message also has `failed_at` (ISO timestamp). Example of a dead message: `{"job_id": "...", "job_type": "always_fail", "payload": {...}, "attempts": 3, "max_attempts": 3, "last_error": "always_fail job failed on purpose", "failed_at": "2026-10-16T06:18:57+00:00"}`.

**Claiming and acknowledgement.** The worker claims a job with `BLMOVE queue processing:<worker_id> RIGHT LEFT 2` (the worker's own list since 5.2; before that one shared `processing` list; the timeout was 5 seconds before 5.1). This is one atomic Redis command, so a job is always in exactly one of the two lists, and it cannot be lost between "taken from the queue" and "recorded as in progress". Because enqueue pushes on the left and the worker takes from the right, the order is first in, first out. The job stays in the worker's list while it runs. It is removed with `LREM processing:<worker_id> 1 <message>` (using the exact string BLMOVE returned) when it is finished: on success, or in the same Redis transaction that moves a failed job to `delayed` or `dead`. That `LREM` is the acknowledgement. If it removes 0 items on success, the reaper already recovered the job, and the worker logs a warning.

**Worker flow** (`run_worker` in `app/queue/worker.py`, one function, loops until shutdown is requested; the flow is described under "Retries and dead letter queue (Milestone 5.1)" and "Crash recovery (Milestone 5.2)" below).

**Handler registry.** `HANDLERS` in `app/handlers/__init__.py` is a plain dictionary `job_type -> function`. Each function takes the payload dict. Registered: `process_csv`, `send_email`, `sleep_job` (payload `{"seconds": n}`), `always_fail`, `flaky_job` (payload `{"name": "...", "fail_times": n}`), `tracked_job` (6.1, payload `{"run": "...", "n": n, "seconds": 0.2}`).

## CSV processing (Milestone 3.1)

**Validation rules** (`CustomerRow` in `app/schemas.py`). Every value is stripped first. An empty or missing cell (short row) counts as missing, reported as `<field>: is required`. Extra columns are ignored. Column names in the header are trimmed too (the same way the upload check does).

| Field | Rule |
|---|---|
| `name` | required, at most 100 characters |
| `email` | required, valid format (`email-validator`, no DNS lookup). Stored lowercased |
| `phone` | required. Spaces, dashes and parentheses are removed, then it must be an optional `+` and 7 to 15 digits. **The cleaned value is what is stored** |
| `date_of_birth` | required, exactly `YYYY-MM-DD`, a real date, not in the future, not more than 120 years ago (43830 days) |
| `country` | required. Uppercased, then must be in `ALLOWED_COUNTRY_CODES` (20 codes: IN US GB CA AU DE FR ES IT NL SG AE JP CN BR MX ZA SE CH NZ) |

All errors of a row are collected (not just the first). Then two more checks that Pydantic cannot do:
- **Duplicate in file:** a set of lowercased emails seen so far. The first valid occurrence passes, later ones fail with `email: duplicate email in file`. Only rows that passed Pydantic are checked, and only they add to the set. So a row that has other errors is not also reported as a duplicate, and does not "use up" its email.
- **Already in the database:** see the batch flow.

**Batch flow** (`process_csv`, one function, `BATCH_SIZE = 1000`):
1. Load the `import_jobs` row and decide: resume from the checkpoint, or start from scratch (see "Checkpoint and resume (Milestone 5.3)"). A fresh start resets the counters and the checkpoint and creates `reports/errors_<id>.csv` with its header. A resume truncates the report to `report_bytes` and rebuilds the set of seen emails.
2. Count the data rows of the file (6.1, see "Multiple workers and progress"), save `rows_in_file` and commit.
3. Open the CSV (`utf-8-sig`, `newline=""`) with `csv.DictReader`, and the report in append mode.
4. Read up to 1,000 rows (with their `reader.line_num`). On a resume, rows up to `last_processed_row` are read and thrown away first. Nothing else from the file is in memory.
5. Strip and validate each row, check for duplicates in the file.
6. If the batch has valid rows: sort them by email, then one bulk `INSERT ... ON CONFLICT (email) DO NOTHING RETURNING email`. The returned emails are the rows this statement really inserted. If some valid rows were not returned, one more query finds which of them belong to this import (`import_job_id` = this import). Those were inserted by an earlier run of this same import and stay valid. Every other valid row that was not returned becomes invalid with `email: email already exists`. A batch with no valid rows skips the insert.
7. Write the batch's invalid rows (the ones from validation and the "already exists" ones together) to the error report, sorted by line number, then flush. Read the report size from the file descriptor (`os.fstat`).
8. Update the counters, `last_processed_row` and `report_bytes` on `import_jobs` and `COMMIT`, in the same transaction as the insert. One commit per batch. Log `import <id>: processed up to line <n>` after every 20th batch.
9. Right after the commit (6.2): a fresh query for the status of this import. If it is `cancelling`, stop reading rows and go to step 10.
10. At the end: delete the report if `invalid_rows == 0`, else set `error_report_path`. Then one conditional `UPDATE ... SET status = <completed or completed_with_errors>, finished_at = now() WHERE id = ... AND status = 'running'`. If it changed one row, enqueue `send_email`. If it changed 0 rows (or step 9 stopped the loop), the import was cancelled: see "Cancel (Milestone 6.2)".

The counters are assigned from local totals (not incremented in SQL). The local totals start at 0 on a fresh start and at the committed values on a resume, so running the handler again does not double count them.

**What each counter means** (`import_jobs`):
- `rows_in_file`: data rows in the whole file, counted at the start of every attempt (6.1). Not a progress counter: it does not change while the import runs.
- `total_rows`: data rows processed so far (blank lines are skipped by the csv module and not counted).
- `valid_rows`: exactly the rows whose customer exists because of this import (6.1). It is exact even when other imports run at the same moment.
- `invalid_rows`: rows that failed validation, were duplicates in the file, or whose email belongs to another import (`email: email already exists`).
- `last_processed_row`: the CSV line number (`reader.line_num`) of the last row of the last committed batch. The header is line 1. Since 5.3 it is the checkpoint: a resume skips every row up to this line. For a quoted field that spans several lines it is the last line of the record.

**Error report** (`reports/errors_<import_job_id>.csv`). Columns: `row_number, name, email, phone, date_of_birth, country, errors`. `row_number` is the CSV line number (first data row is 2). Values are the original ones from the file (not stripped or lowercased); a missing cell is empty. `errors` joins every reason with `"; "`, each as `field: reason`, for example `email: invalid email format; country: not an allowed country code`. The file is created when a fresh start begins (an existing one is replaced), and on a resume the existing file is kept and appended to. With 0 invalid rows it is deleted and `error_report_path` stays empty. Otherwise `error_report_path` is set. `error_report_path` is not in the `GET /imports/{id}` response (check it with SQL).

**Failures.** A bad row is never a failure. A missing file or a file that cannot be decoded raises, and the worker retries the job and finally marks it `failed` (unless the import was cancelled in the meantime: a cancel is never overwritten, see "Cancel (Milestone 6.2)") (the upload check only decodes the start of the file, so a bad byte later in a large file is found here). Batches committed before the error stay in the database, and the report stays on disk as it is: the next attempt resumes from the checkpoint (since 5.3) and continues the same report. Nothing is deleted or reset when a handler fails.

**Known limits:**
- **Re-uploading the same file** as a *new* import marks every row that was inserted earlier as invalid with `email: email already exists`, and inserts nothing new. (Running the *same* import job again is safe since 5.1, see below.)
- The set of seen emails grows with the number of distinct emails in the file.

**Test data script.** `scripts/generate_test_csv.py --rows 10000 --invalid-percent 10 --output test_customers.csv --seed 42` (these are the defaults). Names and dates of birth come from Faker; emails are `<prefix><row>@example.com` (the prefix is `--email-prefix`, default `user`), so they are unique. Files with different prefixes do not overlap; files with the same prefix overlap on purpose. The exact number of bad rows is `round(rows * percent / 100)`, at random positions, and the six kinds take turns: missing name, bad email format, bad phone, bad date of birth (wrong format or future), country not allowed, duplicate email in file (copies the email of an earlier valid row). Valid phones come in three styles (`+digits`, `+1 (123) 456-7890`, plain digits). It prints the expected total, valid and invalid counts and the count per kind. The expected counts assume none of those emails are in `customers` yet. Run it in the api container; use an output path under `uploads/` so the file appears on the host and can be uploaded with curl:

```
docker compose exec api python scripts/generate_test_csv.py --rows 10000 --invalid-percent 10 --output uploads/test_10k.csv
curl -F file=@uploads/test_10k.csv localhost:8000/imports
```

## Email notification (Milestone 4.1)

**Job chain.** `process_csv` then `send_email`. The email is its own job so that an SMTP problem can never reprocess the CSV.

**Chaining** (end of `process_csv`). After the final status is committed (and only if the import was not cancelled, 6.2), it calls `enqueue("send_email", {"import_job_id": "<id>"})`. If that raises a `RedisError`, it does not raise: it sets `email_status = "failed"` and `email_error` to the error text, and the import stays `completed` or `completed_with_errors`. If `process_csv` fails (status `failed`), no email is sent. Failure emails come later.

**The `send_email` handler** (`app/handlers/send_email.py`, one function):
1. Load the `import_jobs` row.
2. If `email_status` is already `sent`: log it and return (no second email).
3. Build a plain-text `EmailMessage`. From `SMTP_FROM`. To `notify_email` (old rows with no `notify_email` use `DEFAULT_NOTIFY_EMAIL`). Subject `Import finished: <filename> (<status>)`. Body lines: filename, status, total rows, inserted rows (`valid_rows`), rejected rows (`invalid_rows`), started at, finished at. If `error_report_path` is set, one more line: `Error report: <APP_BASE_URL>/imports/<id>/errors`.
4. Send with `smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)`. No TLS, no login.
5. Success: `email_status = "sent"` and `email_error` cleared.
6. Failure (`OSError`, which covers `SMTPException`, refused connections, DNS errors and timeouts): `email_status = "failed"`, `email_error` = the message, then re-raise so the worker logs it.

**Worker change and why.** Before 4.1 the worker set `running` / `started_at` / `attempts` (and `failed` on error) for any job whose payload had `import_job_id`. With `send_email` that would mark a finished import `failed` just because its email failed, and would reset `started_at`. Now the worker does those `import_jobs` updates only when `job_type == "process_csv"`. For `send_email` it only runs the handler, acknowledges the job, and logs errors. A failed email job is retried like any other job (5.1).

**Settings** (`app/config.py`, `.env.example`; the worker and api get them through `env_file: .env`): `SMTP_HOST` (default `mailpit`), `SMTP_PORT` (`1025`), `SMTP_FROM` (`noreply@example.com`), `DEFAULT_NOTIFY_EMAIL` (`admin@example.com`), `APP_BASE_URL` (`http://localhost:8000`). All have defaults, so an existing `.env` keeps working.

**Delivery guarantee.** Emails are at-least-once, like every job. If a worker dies after the SMTP server accepted the message but before `email_status = "sent"` is saved, the job can run again and send a second email. In the normal case the `already sent` check prevents duplicates.

**Resend an email:** `docker compose exec api python -m scripts.enqueue_test_job send_email --import-job-id <import id>`. It only sends if `email_status` is not `sent`. To force a resend, set it back with SQL first.

## Retries and dead letter queue (Milestone 5.1)

**Retry flow**

```
queue --BLMOVE--> processing:<worker_id> --start check: import cancelled?--> yes: LREM (skipped, 6.2)
                      |
                     no: run the handler --handler OK--> LREM (done)
                      |
                handler raises (or unknown type)
                      |
        attempts + 1 < max_attempts ?
          yes: ZADD delayed (score = now + backoff) + LREM own list   (one transaction)
                      |
                 [wait in "delayed"]
                      |
        top of every worker loop: Lua script moves due jobs delayed --> queue
          no:  LPUSH dead + LREM own list                               (one transaction)
```

A handler that raises includes a timeout (`JobTimeoutError`, 5.2). A worker that dies is handled by the reaper instead, see "Crash recovery (Milestone 5.2)".

**Worker loop** (each step is a short commented block in `run_worker`):
1. `promote_due_jobs()` (the Lua script below). Since 5.2, then the reaper if `REAPER_INTERVAL_SECONDS` have passed.
2. `BLMOVE queue processing:<worker_id> RIGHT LEFT 2`. Nothing arrived: loop (which promotes again). The timeout is 2 seconds so delayed jobs are noticed quickly.
3. Parse the message. Unknown `job_type`: no retry, straight to `dead` with `last_error` `"unknown job type"`.
4. For `process_csv` only: one conditional `UPDATE ... WHERE id = ... AND status NOT IN ('cancelling', 'cancelled')` sets status `running`, `started_at`, `attempts + 1` and clears `error_message` (6.2, see "Cancel (Milestone 6.2)"). If it changed 0 rows and the import exists, the import was cancelled: the job is acknowledged and skipped (step 5 is done, nothing else runs, no failure, no email). Otherwise run the handler, with the job timeout timer set around it (5.2). Everything in step 4 is inside one `try`, so an error from the status update counts as a failed attempt too.
5. Success: `LREM` on the worker's own list.
6. Failure: build the new message (`attempts + 1`, `last_error`). If `attempts + 1 < max_attempts`, retry (transaction: `ZADD delayed` + `LREM` on the worker's own list). Otherwise dead (transaction: `LPUSH dead` + `LREM` on the worker's own list, plus `failed_at`). Every retry and every dead job is logged with job id, type, attempt number, delay and error (since 6.3 also as JSON fields: `attempt`, `max_attempts`, `delay_seconds`, `error`).
7. For `process_csv` only, after the Redis move: status `retrying` (error_message set), or `failed` (error_message and `finished_at` set), through `save_failure_status` (6.2): it locks the row, changes nothing if the import is `cancelled`, turns `cancelling` into `cancelled`, and only otherwise sets the status. If this DB update itself fails it is logged and the worker keeps running; the job is already safe in Redis.

`send_email` jobs are not touched by the worker in `import_jobs`; the handler sets `email_status` (`failed` on error, `sent` on success), so a retried email that succeeds ends as `sent`.

**Backoff.** `delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** failures_before_this_one)`, so with the defaults (2, 60) the waits are 2, 4, 8, ... capped at 60. Then jitter: `final = delay / 2 + random.uniform(0, delay / 2)`, so the real wait is between half the delay and the full delay. **Why jitter:** without it, jobs that failed together (for example when SMTP or Postgres went down) all retry at exactly the same moment and hit the recovering service as one spike. Randomizing the wait spreads them out. With 3 attempts there are only two waits (after attempt 1 and 2); after attempt 3 the job is dead.

**Settings:** `MAX_ATTEMPTS=3`, `RETRY_BASE_SECONDS=2`, `RETRY_MAX_SECONDS=60` (`app/config.py`, `.env.example`, passed to the worker in `docker-compose.yml`). `max_attempts` is stored in each message when it is enqueued, so changing the setting only affects new jobs.

**Why a Lua script for promoting.** Moving a job from `delayed` to `queue` takes two Redis commands (`ZREM` then `LPUSH`). Done from Python, a crash between them would lose the job, and two workers could both read the same due member and both push it. A Lua script runs as one atomic step inside Redis, so neither can happen. It is registered once with `register_script`.

**Why Redis transactions (`pipeline(transaction=True)`, i.e. MULTI/EXEC).** Moving a failed job needs two writes: put it in `delayed` or `dead`, and remove it from the worker's processing list. In one transaction both happen or neither does. A job is therefore always in exactly one place: it can't be lost between the writes, and can't be in both places. (Each Redis command itself is atomic, but a pair of them is not unless wrapped like this.) The `LREM` uses the exact raw string from `BLMOVE`, since that is what is stored in the list.

**Safe retries for `process_csv`.** A job can run more than once, so the handler must give the same result every time:
- `customers.import_job_id` records which import inserted each customer.
- Since 5.3 the normal path for a retry is to **resume** from the checkpoint (see "Checkpoint and resume (Milestone 5.3)"). The reset (counters, `last_processed_row`, `report_bytes` to 0, `error_report_path` cleared, `finished_at` null, new report file) happens only on a **fresh start**: a first attempt, a job that already finished, or a checkpoint that cannot be trusted.
- Since 6.1, a valid row that the bulk insert skipped (`ON CONFLICT (email) DO NOTHING`, not in `RETURNING`) is looked up with `import_job_id` = the current import. So when a run starts over, rows inserted by an earlier run are not reported as "already exists". They are counted as valid again, and no duplicate customer is created. Customers from other imports, or with a null `import_job_id`, still count as existing. (Before 6.1 a separate `WHERE email IN (...)` query did this check before the insert.)
- Result: running the same job twice gives the same counters, one report, and the same number of customers.

**Dead messages.** Since 6.3 the `dead` list is read by `GET /dead-jobs` and by `POST /dead-jobs/{job_id}/replay`, which moves a dead job back to `queue` (see "Observability (Milestone 6.3)"). Nothing deletes a dead job except a replay. You can still look at the raw list with `redis-cli LRANGE dead 0 -1`.

**`flaky_job`** (`test_handlers.py`): `INCR flaky:<name>`; raises while the counter is `<= fail_times`, then succeeds. The counter stays in Redis, so use a new `--name` for each test (or `DEL flaky:<name>`).

**Test script options** (`docker compose exec api python -m scripts.enqueue_test_job ...`): `flaky_job --name <name> --fail-times <n>`; `--import-job-id <id>` works for any job type, for example `process_csv --import-job-id <id>` runs an import again.

**Still not handled:**
- ~~No DLQ endpoints or replay.~~ Added in 6.3.
- If a worker dies right after the Redis transaction but before the status update, the import may show the old status until the next attempt starts.

## Crash recovery (Milestone 5.2)

**Worker id.** At startup each worker makes `worker_id = hostname + "-" + 8 random hex characters` and logs it. The random part is new on **every** start. A restarted container keeps its hostname, so without it the new process would have the same heartbeat key and the same processing list as the dead one: the reaper would think the old worker is still alive and never recover its job. With a fresh id, the old id has no heartbeat, so it is clearly dead, and the new worker is a separate worker.

**Per-worker processing lists.** Each worker claims into its own list, `processing:<worker_id>`. With one shared list, Redis cannot tell whose job is whose, so when a worker dies there is no way to know which entries are its orphans and which belong to workers that are alive and busy. With one list per worker, "the list of a dead worker" is exactly "the jobs to recover". The old shared `processing` key is no longer used (it is not migrated or touched).

**Heartbeat.** The key `worker:<worker_id>` holds the current Unix time and has a TTL of `HEARTBEAT_TTL_SECONDS` (15). It is set once at startup **before the first claim** (so a worker never owns a list without a heartbeat), and then a daemon thread (`send_heartbeats`) refreshes it every `HEARTBEAT_INTERVAL_SECONDS` (5), using the same `redis_client`. It is a thread because the main thread can be inside a handler for minutes (a big CSV) and cannot refresh anything meanwhile. A failed refresh is logged and the thread keeps going. The TTL is three times the interval, so two missed beats are tolerated. A key that has expired means the worker is dead (or stopped).

**Reaper** (`reap_dead_workers`). At the top of the worker loop, right after promoting due jobs, if `REAPER_INTERVAL_SECONDS` (5) have passed since this worker last ran it, it runs. Every worker runs it. Steps:
1. `SCAN` for `processing:*` (not `KEYS`, which blocks Redis). The worker id is the key name after the prefix.
2. If `worker:<worker_id>` exists, the worker is alive: skip it. (This includes the worker running the reaper.)
3. Otherwise read all messages in the list. For each, build a new message with `attempts + 1` and `last_error` `"worker died while running this job (worker <worker_id>)"`.
4. If `attempts + 1 < max_attempts`: recover it to `queue` (no backoff). Otherwise move it to `dead` with `failed_at`.
5. Move it with the Lua script (below). If it returned 1, log it. For `process_csv` jobs only, set the import to `retrying` (recovered) or `failed` with `finished_at` (dead), with `error_message` `"worker died while running this job"`. `send_email` jobs do not touch `import_jobs`. Since 6.1 it does nothing if another worker has already claimed the job and started a newer attempt (`attempts` is higher than the dead attempt number). Since 6.2 it is done by `save_failure_status` with that attempts check, so it never overwrites a cancel either. If that update fails it is logged and the reaper carries on.

The whole reaper call is wrapped in a `try` in the worker loop: if it fails (for example a Redis error), the error is logged and the worker keeps working. With a single worker, the recovered job goes back to `queue` and is claimed by the same worker right away, so the `retrying` status only exists for a moment before the next attempt sets `running`.

**Why a crash counts as an attempt.** If it did not, a job that kills its worker every time (for example by running out of memory) would be recovered, kill the next worker, be recovered again, and so on forever. Counting it means such a job is dead after `max_attempts` crashes. Crashing three times in a row on `sleep_job` ends in `dead` with `attempts` 3, and a `process_csv` import ends `failed`.

**Why a Lua script** (`recover_job_script`, registered with `register_script`, like `promote_due_jobs`). Keys: the dead worker's list and the destination list. Args: the original raw message and the new message. The script does `LREM` of the original from the dead list, and **only if that removed exactly 1 item** it `LPUSH`es the new message to the destination and returns 1; otherwise it returns 0. One atomic step means: two workers reaping at the same time cannot both recover the same job (the second `LREM` removes 0 and returns 0), and a crash between "remove" and "push" cannot lose the job (there is no "between").

**Job timeout.** `JOB_TIMEOUT_SECONDS` (600) is the maximum run time of one handler call. It uses the standard `signal` module: at startup a handler for `SIGALRM` is registered that raises `JobTimeoutError` (a normal `Exception` subclass in `worker.py`). Before the handler call the worker calls `signal.alarm(JOB_TIMEOUT_SECONDS)`, which asks the operating system to send `SIGALRM` to this process after that many seconds. When it arrives, Python runs the signal handler in the main thread, between two Python instructions, so the exception comes out of whatever line the job handler was running. `signal.alarm(0)` in a `finally` right after the handler call cancels the timer, so it can never fire during the status updates or the failure handling. A timeout is a normal failure: it goes through the retry / dead logic with `last_error` `"job timed out after <n> seconds"`. No handler swallows it: they only catch `ValidationError`, `RedisError` and `OSError`, none of which is a parent of `JobTimeoutError`. (The handler runs in the worker's main thread, not in a subprocess, which is what makes `signal.alarm` work.)

**Graceful shutdown.** Handlers for `SIGTERM` (what `docker compose stop` sends) and `SIGINT` set the plain module variable `shutdown_requested = True` and log `shutdown requested`. The worker loop is `while not shutdown_requested`, so:
1. A running job finishes normally (the timeout still applies). No new job is claimed once the flag is seen at the top of the loop. (A job that arrives during a BLMOVE that was already waiting when the signal came can still be claimed; it is then run to the end like any other job.)
2. When the loop ends, the main thread sets a `threading.Event`. The heartbeat thread waits on that event instead of sleeping, so it wakes up and ends at once; the main thread `join`s it.
3. Delete `worker:<worker_id>`.
4. Log whether the worker's own processing list is empty, log `Worker <id> stopped`, and return, so the process exits with code 0.

An idle worker stops within about 2 seconds (the BLMOVE timeout). `stop_grace_period: 30s` on the worker service in `docker-compose.yml` is how long Docker waits after SIGTERM before it sends SIGKILL. If a job needs longer than that, Docker kills the worker (exit code 137), the heartbeat expires, and the reaper recovers the job. That is expected.

**Settings** (`app/config.py`, `.env.example`, passed to the worker in `docker-compose.yml`):

| Setting | Default | Meaning |
|---|---|---|
| `HEARTBEAT_INTERVAL_SECONDS` | 5 | How often the heartbeat thread refreshes the key |
| `HEARTBEAT_TTL_SECONDS` | 15 | Lifetime of the heartbeat key; after this without a refresh the worker counts as dead |
| `REAPER_INTERVAL_SECONDS` | 5 | Minimum time between two reaper runs of one worker |
| `JOB_TIMEOUT_SECONDS` | 600 | Maximum run time of one handler call |

**New log messages** (logger `worker`; since 6.3 every log line is a JSON object, the message text below is the `message` field and 6.3 adds fields, see "Observability (Milestone 6.3)"): `Worker <id> started, waiting for jobs`; `Recovered job <job id> (<type>) from dead worker <id>, moved to 'queue' | 'dead' (attempt n of m)` (warning); `The reaper failed` (with traceback); `Could not refresh the heartbeat` (with traceback); `Job <id> (<type>) finished, but the reaper had already recovered it` (warning, when the acknowledgement `LREM` removes 0); `shutdown requested`; `Processing list of worker <id> is empty` (or a warning that it still has n jobs); `Worker <id> stopped`. A timeout shows up as the normal failure log with the error `job timed out after <n> seconds`.

**How to see it.** `docker compose exec redis redis-cli KEYS 'worker:*'` and `KEYS 'processing:*'`; `TTL worker:<id>` keeps resetting (it stays between about 10 and 15); `docker compose kill -s KILL worker` simulates a crash (then `docker compose start worker`).

**Known limits:**
1. A worker that is alive but cut off from Redis for longer than the TTL can have its job recovered while it is still running it, so the job can run twice. This is why handlers must be idempotent.
2. The SIGALRM timeout fires only between Python instructions, so a long blocking call (for example one huge SQL query) is interrupted only after it returns.
3. The old shared `processing` list is no longer used. A leftover `processing` key from before 5.2 is ignored (the reaper only looks at `processing:*`).
4. The failure transaction from 5.1 does not check that the job is still in the worker's list. If the reaper already recovered the job, a late failure can add a second copy to `delayed` or `dead`.
5. ~~The reaper overwrites `running` with `retrying` when another worker claims the recovered job at once.~~ Fixed in 6.1: with several workers this showed up every time (an import that another worker was resuming stayed `retrying` until it finished). The claim and the reaper now each use one atomic `UPDATE`, see "Multiple workers and progress (Milestone 6.1)".

## Checkpoint and resume (Milestone 5.3)

**What the checkpoint is.** Four counters already on `import_jobs` (`total_rows`, `valid_rows`, `invalid_rows`, `last_processed_row`) plus the new `report_bytes`. After every batch they are updated and committed **in the same transaction as the bulk insert**. Because it is one commit, either both the customers of the batch and the checkpoint are saved, or neither is. So the checkpoint always matches what is really in `customers`: it can never say "line 50,000 is done" while the rows of the last batch are missing, or the other way round. A crash before the commit rolls the whole batch back, and the resume simply does that batch again.

**Resume or start from scratch.** At the start of every attempt the handler resumes only if ALL are true:
1. `last_processed_row > 0` (an earlier attempt committed at least one batch).
2. `finished_at` is null. It is set only when a job ends as `completed`, `completed_with_errors` or `failed`, so null means the earlier attempt never finished. **A finished job restarts from scratch** when `process_csv` runs for it again (for example `--import-job-id`), because the checkpoint of a finished job describes the end of the file: resuming would do nothing, and the point of running it again is to redo the import. A fresh start also sets `finished_at` back to null.
3. The report file `reports/errors_<id>.csv` exists and its size is at least `report_bytes`.

If (1) and (2) hold but (3) does not, the checkpoint cannot be trusted (the counters say rows were reported that the file no longer has), so the handler logs why and falls back to a fresh start instead of failing. Otherwise it starts from scratch exactly as in 5.1.

**Why `report_bytes` and the truncate.** In a batch the report is written and flushed **before** the commit, and its size is saved **in** the commit. If the worker dies between the flush and the commit, the batch is rolled back but its invalid rows are already in the report file, so the file is longer than `report_bytes`. On resume `os.truncate(report_path, report_bytes)` cuts that part off, and the batch is redone and written again, so no row appears twice. If the worker dies after the commit, the file size equals `report_bytes` and nothing is cut. The file can never be shorter than `report_bytes` after a crash (that would mean someone deleted or changed it, which is the "cannot be trusted" case above).

**Why the seen-email set is rebuilt from the database.** The duplicate-in-file check uses the set of emails seen so far, and that set is lost when the worker dies. Without it, a row after the checkpoint that repeats an email from before the checkpoint would pass the check, be skipped by `ON CONFLICT DO NOTHING`, and be counted as valid, which gives wrong counters. On a resume the set is rebuilt with one streamed query (`yield_per`) over the customers where `import_job_id` is this import, and then it works exactly as in an uninterrupted run.

**How rows are skipped.** The CSV is opened from the start. Inside the batch-reading loop, a row whose `reader.line_num` is less than or equal to `last_processed_row` (copied into a local `skip_until_line` first, because the value on the row changes with every batch) is read and dropped with `continue`: no validation, no duplicate check, no database call, no counting. On a fresh start `skip_until_line` is 0, so nothing is skipped. If nothing is left after the skipping (the worker died after the last batch was committed but before the final status was set), the first batch is empty, the loop ends, and the handler goes straight to the final status without inserting anything.

**Failures.** When the handler raises (including `JobTimeoutError`) nothing is reset or deleted. Committed batches stay, the uncommitted batch is rolled back, and the report file stays as it is. The worker does not touch `last_processed_row`, the counters, `report_bytes` or `finished_at` when it starts an attempt (it only sets status, `started_at`, `attempts` and `error_message`).

**New log messages** (logger `process_csv`; JSON since 6.3, the resume and fresh start lines also have the fields `line` and `rows_done`, and `reason` when the checkpoint was not trusted): `starting import <id> from scratch`; `resuming import <id> from line <last_processed_row> (<total_rows> rows already processed)`; a warning `import <id>: the checkpoint cannot be trusted, the error report file is missing` (or `... has <n> bytes but <m> were saved`) just before the fresh start; `import <id>: processed up to line <n>` after every 20th batch (6.3: with the fields `line`, `total_rows` and `peak_rss_mb`).

**How it was checked** (300,000 rows, 30,000 invalid): the worker was killed with `SIGKILL` at about a third of the file and resumed from line 101001 with the counters going on from 101,000; with `JOB_TIMEOUT_SECONDS=15` and `MAX_ATTEMPTS=10` an import resumed after 6 timeouts (7 attempts, each further than the last); with the report file deleted after a kill it logged the "cannot be trusted" warning and started over. All ended with exactly 270,000 valid and 30,000 invalid rows, 270,000 distinct customers, a report with 30,000 data rows and no repeated row numbers, and one email. Running `process_csv` again for a finished import started from scratch and gave the same counters, customer count and a byte-identical report. Clearing `finished_at` on a finished import made the next run resume from line 300001, do nothing, and keep the counters.

**Known limits:**
1. Skipping still parses the already-processed part of the file. That is cheap compared with validation and database work. Seeking to a byte offset would avoid it, but is fragile with quoted fields that contain newlines.
2. The uploaded file must not change between attempts. This is not checked.
3. The seen-email set contains only emails of rows that were inserted. If an earlier row was invalid only because its email already existed from another import, its email is not in the set, so a later duplicate of it is reported as `email: email already exists` instead of `email: duplicate email in file` after a resume. The counters are identical.
4. ~~Two workers inserting the same email at the same moment made `valid_rows` too high.~~ Fixed in 6.1 (`RETURNING`).
5. The email is still at-least-once.
6. **The rebuilt seen-email set is only correct if every customer of this import was inserted before the checkpoint.** That holds for a normal retry. It does NOT hold when a fresh start happens for an import that already has customers (running `process_csv` again for a finished import, or the "cannot be trusted" fallback after some batches were inserted) and that fresh run is then interrupted and resumed. The set then also contains the emails of the earlier run beyond the checkpoint, so later rows are wrongly reported as `email: duplicate email in file`, and `valid_rows` and the report are wrong (the `customers` table itself stays correct). Seen once while testing: a re-run of a finished 300k import was interrupted by `docker compose stop worker` (the worker was killed after the 30s grace period), the job resumed, and 175,013 rows were wrongly rejected. Running the import once more to the end (it starts from scratch because it is finished) repairs the counters and the report. A real fix needs a decision: for example delete the import's own customers on a fresh start, or store the line number on each customer. Not done in this milestone.

## Multiple workers and progress (Milestone 6.1)

**How to run several workers.** `docker compose up -d --scale worker=3`. This works because the `worker` service has no `container_name` and no published ports (nothing to clash on). `--scale worker=1` stops the extra ones: each gets SIGTERM, finishes its job, logs `Processing list of worker <id> is empty` and `Worker <id> stopped`, and exits with code 0.

**One job, one worker.** `enqueue` pushes onto `queue`; every worker runs `BLMOVE queue processing:<worker_id>`. That is one atomic Redis command: the job leaves `queue` and lands in the caller's own list in the same step, and Redis serves waiting clients one at a time, so two workers can never get the same message. The job stays in that worker's list until it is acknowledged. Nothing in the queue code had to change for this (it was built for it in 2.1 and 5.2), except the bug below.

**Why heartbeats, the reaper and the worker ids already work with several workers.** The worker id is `hostname + "-" + 8 random hex characters` (checked in the logs: three containers gave three different ids, for example `590cf997f9cc-43077a68`). So each container has its own heartbeat key (`worker:<id>`), its own processing list, and its own reaper loop. A reaper only touches a list whose heartbeat key is gone, never a list of a live worker, and the recovery is the atomic Lua script, so when several reapers find the same dead worker only one of them moves each job. `promote_due_jobs` is a Lua script too, so two workers cannot promote the same delayed job.

**A bug found and fixed in the queue code.** With one worker, a recovered job was claimed by the same worker after the reaper had finished. With several workers, another worker claims it at once, and the reaper's status update (`retrying`) could land after the new worker's `running`. The result: an import that was being resumed and was making progress showed `retrying` until it finished. Two causes: the reaper did read-then-write, and the claim assigned `status = "running"` to a row that already said `running` (SQLAlchemy sends no UPDATE for an unchanged value, so `retrying` was never corrected). Fix, in `app/queue/worker.py`: the claim is one `UPDATE` (`status`, `started_at`, `attempts = attempts + 1`, `error_message`), and the reaper's status change is one `UPDATE ... WHERE id = ... AND attempts <= <the attempt number of the dead run>`. If a new attempt has already started, `attempts` is higher and the reaper's `UPDATE` changes nothing. In the other order the claim's `UPDATE` simply wins. Nothing else in the queue, heartbeat, reaper, retry or shutdown code was changed.

**Why the batch is sorted before the insert (deadlocks).** One batch is one `INSERT` statement that takes a row lock for each new email, in the order of the `VALUES`. If import A inserted `x, y` and import B inserted `y, x` at the same moment, A could hold `x` and wait for `y` while B holds `y` and waits for `x`: Postgres would see the deadlock and abort one transaction. If both sort the batch by email, they ask for the common emails in the same order, so one of them simply waits for the other's commit. A batch is one statement and one commit, so an import never holds locks from an earlier batch while it waits.

**What `RETURNING email` is used for.** `ON CONFLICT DO NOTHING` skips a conflicting row silently. `RETURNING email` gives back only the emails that were really inserted by this statement. Every valid row that is not returned was skipped, and that is one of two kinds:
1. **Inserted by an earlier run of this same import** (the rerun case from 5.1, or a resumed batch): one query `WHERE email IN (skipped) AND import_job_id = <this import>` finds them. They stay valid and are counted in `valid_rows`. The query runs only when something was skipped.
2. **Belongs to another import**: it finished earlier, or it inserted the same email a moment ago in a parallel import (the statement waited for that import's commit, then skipped). The row becomes invalid with `email: email already exists` and is added to the batch's invalid rows BEFORE the report is written, so the report stays sorted by line number.

Together with the rows that were really inserted, that is three kinds of valid rows after the insert: inserted now, skipped but mine, skipped and not mine. Because an email exists only once, exactly one import wins each email, so `valid_rows` summed over parallel imports equals the number of customers. If Postgres still reports an error (a deadlock, a lost connection), it raises as any error does: the worker retries and the import resumes from the checkpoint.

**Progress.** At the start of EVERY attempt, right after deciding to resume or start from scratch, `process_csv` counts the data rows with a plain pass: `csv.reader` over the file (same `utf-8-sig`, `newline=""`), skip the header, count rows that are not an empty list. It keeps nothing but one integer, so memory is constant. It saves `rows_in_file` and commits, and logs the number. **Why it matches `total_rows`:** `csv.DictReader` is built on `csv.reader` and skips rows that are `[]` (blank lines), and `total_rows` counts only the rows `DictReader` returned, so both leave out the same rows. A row that only has commas is not `[]`, so both count it.

`progress_percent` is a Pydantic `computed_field` on `ImportJobResponse` (so also in `GET /imports`). In this order: `rows_in_file` is null: `null`; status `completed` or `completed_with_errors`: `100.0`; `rows_in_file` is 0: `100.0`; otherwise `round(min(100, total_rows * 100 / rows_in_file), 1)`. A `cancelled` import is not forced to 100 (6.2): it shows the real percentage at the time it stopped. `total_rows` is committed with every batch and keeps its value on a resume, so progress never goes backwards after a crash. A fresh start (a re-run of a finished import) resets it to 0, which is right.

**New test handler and scripts.**
- `tracked_job` (`test_handlers.py`, payload `{"run": "<name>", "n": <int>, "seconds": <number, default 0.2>}`): sleeps, then `HINCRBY ran:<run> <n> 1` and `HINCRBY workers:<run> <hostname> 1`.
- `scripts/check_no_double_claim.py --jobs 200 --timeout 180`: enqueues the jobs, waits until `queue` and `delayed` are empty, no `processing:*` list has items and nothing has changed for 3 seconds (or the timeout), then prints `PASS` if every `n` ran exactly once, otherwise `FAIL` with the `n` values that ran 0 or more than 1 times. It prints the jobs per worker (hostname) and warns if fewer than two workers ran jobs. The `ran:<run>` and `workers:<run>` keys are left in Redis.
- `scripts/generate_test_csv.py --email-prefix <prefix>` (default `user`).

```
docker compose up -d --scale worker=3
docker compose exec api python -m scripts.check_no_double_claim --jobs 200
docker compose exec api python scripts/generate_test_csv.py --rows 100000 --email-prefix a --seed 1 --output uploads/a.csv
```

**How it was checked.** 3 workers: 3 different worker ids and 3 `worker:*` keys; `check_no_double_claim` printed PASS twice (about 67 jobs per worker), no `processing:*` keys and `queue` empty afterwards. Three files of 100,000 rows with prefixes a, b, c ran at the same time on three workers: each ended `completed_with_errors` with 90,000 valid and 10,000 invalid, 270,000 customers. The same 100,000-row file uploaded twice at the same moment, three times: each time `valid_rows` summed to 90,000, `COUNT(*)` and `COUNT(DISTINCT email)` were both 90,000, each report had `invalid_rows + 1` lines, and the logs showed no deadlock. The two imports split the emails in a different way each time (for example 21,629 / 68,371 valid). A 300,000-row file showed progress rising from 0.7 to 100.0 with no step down. Another one was running on one worker, which was killed with `docker kill`; the reaper of another worker recovered the job, a third worker resumed it from line 91001, progress went on from 30.3 and the status stayed `running`; it ended with 270,000 valid, 30,000 invalid, 270,000 distinct customers and a report with 30,000 data rows. Scaling from 3 to 1 gave the clean shutdown lines for the two stopped workers and left no `processing:*` keys.

**Known limits:**
1. The count pass reads the file one extra time per attempt (cheap compared with validation and database work).
2. Progress counts rows, not time: rows are not equally expensive, and a resume skips already-processed rows quickly.
3. `progress_percent` is calculated when the response is built and is not stored.
4. An import that waits for another import's open transaction on the same emails (the insert is blocked until that transaction commits) waits at most for one batch of the other import.
5. The uploaded file must not change between attempts (still not checked). That includes the row count: `rows_in_file` is counted again on every attempt, but the checkpoint is not checked against the file.
6. The email is still at-least-once.

## Cancel (Milestone 6.2)

**State changes.**

| Status when cancel arrives | Result | HTTP |
|---|---|---|
| `queued` or `retrying` | straight to `cancelled`, `finished_at` set, `error_message` `cancelled by user` | `200` |
| `running` | `cancelling` (a worker is running it and has to stop) | `202` |
| `cancelling` or `cancelled` | nothing changes (idempotent) | `200` |
| `completed`, `completed_with_errors`, `failed` | nothing changes | `409` `import already finished` |
| unknown id / invalid UUID | | `404` / `422` |

`cancelling` becomes `cancelled` when the worker stops (see below), or when a worker finds it while failing, being recovered or starting the job.

**The endpoint** (`cancel_import` in `app/routes/imports.py`) only uses conditional `UPDATE` statements, never "read the status, then write": (1) `... WHERE status IN ('queued','retrying')` to `cancelled`; if one row changed, return `200`. (2) `... WHERE status = 'running'` to `cancelling`; if one row changed, return `202`. (3) Otherwise re-read the row: `cancelling` / `cancelled` returns `200`, a finished status returns `409`, anything else means a worker changed the status between our statements, so steps 1 and 2 run once more. If the second round also finds nothing, it returns `409` `import changed while cancelling, try again` (not seen in testing). Cancel does not delete the uploaded file, the customers already inserted, or the error report written so far. There is no rollback.

**Why every status change by a worker is a conditional update.** The API and several workers write the same `import_jobs` row at the same time. With "read the status, then write" a worker could read `running`, the API could set `cancelling`, and the worker's write would overwrite it: the cancel is lost and the import is processed to the end. So no worker code writes a status unless the current status allows it, and the condition is part of the `UPDATE` (Postgres checks it again after waiting for a competing writer):
- **Start of an attempt** (worker): `WHERE status NOT IN ('cancelling','cancelled')`.
- **Failure path and reaper** (`save_failure_status`): the row is read and locked in one session (`SELECT ... FOR UPDATE`), then `cancelled` is left alone, `cancelling` becomes `cancelled`, and any other status is set as before. The lock is needed because this is a decision based on the current value.
- **End of `process_csv`**: `WHERE status = 'running'` for a normal completion, `WHERE status IN ('running','cancelling')` for the cancel ending.

**Why cancel is cooperative and checked only after a batch commit.** The worker is one process in the middle of a database transaction; killing it would lose the batch it is working on, and there is no safe way to interrupt a handler from outside. So the API only records the request in the status, and `process_csv` looks at it between batches. It checks only after a commit (a fresh `SELECT status`, not a cached object), because then the checkpoint (counters, `last_processed_row`, `report_bytes`) matches what is really in `customers` and in the report file. A cancelled import therefore stops with exactly the data of its last committed batch.

**Start check (a cancelled job in the queue, in "delayed", or recovered by the reaper).** A worker that claims a `process_csv` job tries the conditional start `UPDATE`. If it changed 0 rows and the import exists, the import was cancelled: if it is `cancelling` it is set to `cancelled` (with `finished_at` if not set and `cancelled by user`), the job is acknowledged (`LREM` from the worker's own list), `skipping import <id>: cancelled` is logged, and the worker goes to the next job. The handler does not run, nothing is retried, no email is sent, and `attempts` does not go up. This covers a job that was cancelled while it sat in `queue`, while it waited in `delayed` for a retry, or while its worker was dead (the reaper moves it to `queue`, a worker claims it and skips it). The message is left in Redis and is skipped when claimed, because removing a message from the middle of Redis by value is not worth the complexity. A job whose import row does not exist behaves as before (the handler runs and fails).

**Stopping a running import.** After every batch commit `process_csv` reads the status. If it is `cancelling` it leaves the loop. Then, for the report, it does the same as at normal completion (delete the file if `invalid_rows == 0`, else set `error_report_path`). Then:
- Normal ending: the conditional `UPDATE ... WHERE status = 'running'`. If it changed one row the import completed, even if the cancel request came a moment later (it then gets `409`). If it changed 0 rows, a cancel arrived before this `UPDATE`, and the import is treated as cancelled.
- Cancel ending: `UPDATE ... SET status='cancelled', finished_at=now(), error_message='cancelled by user' WHERE status IN ('running','cancelling')`. It logs `import <id> cancelled at line <last_processed_row> after <total_rows> rows` and returns normally, so the worker acknowledges the job as a success. If this `UPDATE` changes 0 rows (for example a recovered copy of the job has taken over), it only logs a warning and leaves the status alone. The counters and `last_processed_row` stay as of the last committed batch.

**Why no email.** The email says an import finished and reports its numbers. A cancelled import did not finish, and its user asked for it to stop, so `send_email` is not enqueued and `email_status` stays `pending`.

**Re-running.** `process_csv --import-job-id <id>` for a cancelled import is skipped by the start check. To import the file again, upload it again. (The new import will mark the emails that the cancelled import already inserted as `email: email already exists`, as in the 3.1 re-upload limit. The customers have `import_job_id`, so they are easy to find or delete.)

**Failure path and reaper.** The Redis side did not change: a job that fails while its import is `cancelling` or `cancelled` is still moved to `delayed` or `dead` as before. Only the database update follows the rules above. A retry that comes back later is skipped by the start check.

**Progress.** `progress_percent` keeps the 6.1 rule order. Only `completed` and `completed_with_errors` show `100.0`; a cancelled import shows the real percentage.

**New log messages:** `skipping import <id>: cancelled` (worker); `import <id> cancelled at line <n> after <m> rows` (`process_csv`, 6.3: with the fields `line` and `total_rows`, followed by an `import <id> finished` line with `status` `cancelled`); a warning `import <id>: the status changed while stopping, leaving it as it is`.

**Known limits:**
1. A cancel stops the import only at the next batch boundary, so it can take as long as one batch to take effect. The cancel request returns at once.
2. A cancelled job's message may stay in `queue` or `delayed` until a worker claims and skips it.
3. A job that fails while its import is `cancelling` can still be retried once more in Redis before the start check ends it. The import can show `cancelling` until then, for up to the retry backoff (at most `RETRY_MAX_SECONDS`). (Normally `save_failure_status` turns `cancelling` into `cancelled` at once, as soon as the failure is handled; the wait only applies if that update fails.)
4. Cancel does not remove the customers already inserted by that import (their `import_job_id` makes them easy to find later).
5. Cancel cannot stop a `send_email` job that is already queued, because an import that has finished cannot be cancelled.
6. The uploaded file stays on disk.

**How it was checked** (3 workers; 300,000-row files with 30,000 invalid rows): cancel at 30.3% returned `202` `cancelling`, and a moment later the import was `cancelled` at line 92001 (92,000 rows, progress 30.7), `error_message` `cancelled by user`, `finished_at` set. `COUNT(*)` of `customers` equalled `valid_rows` (82,845) and did not change over the next 30 seconds; the report had `invalid_rows + 1` lines; Mailpit had no email for it. A second cancel returned `200` with nothing changed; cancelling a finished import returned `409`, an unknown id `404`, a bad id `422`. With all workers stopped, an upload was cancelled with `200`; after the workers started the log showed `skipping import <id>: cancelled`, `attempts` stayed 0, `queue` and `processing:*` were empty. With `RETRY_BASE_SECONDS=30` and the file moved away, the import went to `retrying`, was cancelled with `200`, and after the backoff the job was skipped (not in `dead`). Cancelling and then `docker kill` of the worker in one command: the reaper of another worker recovered the job, the import ended `cancelled` with `total_rows` not growing, customers equal to `valid_rows`, nothing in `dead`. 2,000-row imports cancelled at 15 different delays ended either `cancelled` (customers equal to `valid_rows`, and the report data rows equal to `invalid_rows`) or, when the cancel came after the end, `completed_with_errors` with `409`; none stayed `cancelling`. Three 300,000-row imports in parallel, only the middle one cancelled: the other two ended with exactly 270,000 valid and 30,000 invalid rows, and `customers` held 270,000 + 270,000 + the valid rows of the cancelled one.

## Observability (Milestone 6.3)

### Log format

Every log line (worker and api) is ONE JSON object, written to stdout by one handler on the root logger (`setup_logging` in `app/logging_config.py`). Fixed fields, in this order: `ts` (UTC, ISO 8601), `level`, `service` (`worker` or `api`), `worker_id` (workers only), `logger`, `message`. After them come all the extra fields of the record (from `extra=` or from `job_context`). A traceback goes into the field `exception` as one string, so it stays on one line. The message texts did not change in 6.3 (only fields were added), so greps for the old texts still work. `LOG_LEVEL` (default `INFO`) sets the level.

Example (the summary line at the end of `process_csv`):

```json
{"ts": "2026-10-17T07:46:54.832062+00:00", "level": "INFO", "service": "worker", "worker_id": "f08b80482687-5b49df80", "logger": "process_csv", "message": "import d3d6c02f-05a4-416a-82cb-8acf70183c95 finished", "status": "completed_with_errors", "total_rows": 2000, "valid_rows": 1800, "invalid_rows": 200, "duration_seconds": 0.7, "rows_per_second": 2696, "peak_rss_mb": 72.8, "job_id": "58cf2e41-cf90-4737-bea6-e384acf511f9", "job_type": "process_csv", "import_job_id": "d3d6c02f-05a4-416a-82cb-8acf70183c95"}
```

Uvicorn's own lines (start up, and one access line per request) go through the same handler (`setup_logging` removes uvicorn's handlers and lets its loggers propagate). Uvicorn adds a `color_message` field to a few startup lines (the same text with terminal colour codes); it is harmless noise. Only `print()` is gone from `app/` (there was none left to replace); the scripts that print results for a person (`generate_test_csv.py`, `check_no_double_claim.py`, `enqueue_test_job.py`) still print.

Fields added to the existing lines (the message text is unchanged):

| Line | Fields |
|---|---|
| `Claimed job ...` | `attempt`, `max_attempts` |
| `Retrying job ...` | `attempt`, `max_attempts`, `delay_seconds`, `error` |
| `Job ... is dead after ...` | `attempt`, `error` |
| `Recovered job ...` (reaper) | `job_id`, `job_type`, `import_job_id` (if the job has one), `dead_worker_id`, `destination` |
| `resuming import ...` / `starting import ... from scratch` | `line`, `rows_done` (0 and 0 for a fresh start), and `reason` when a checkpoint was not trusted |
| `import ...: processed up to line ...` (every 20th batch) | `line`, `total_rows`, `peak_rss_mb` |
| `import ... cancelled at line ...` | `line`, `total_rows` |
| `import ... finished` (new, at the end of `process_csv`, after the final status is committed and before `send_email` is enqueued; also at the cancel ending with `status` `cancelled`) | `status`, `total_rows`, `valid_rows`, `invalid_rows`, `duration_seconds` (from the start of this attempt), `rows_per_second` (rows handled in this attempt, so a resumed import shows its own speed), `peak_rss_mb` |
| `import uploaded` (new, API, after the enqueue) | `import_job_id`, `job_id` (the queue job id) |
| `cancel requested` (new, API) | `import_job_id`, `result` (the import status returned: `cancelled`, `cancelling`, or the finished status of a `409`). The queue job id is not known in this route, so it has no `job_id` |
| `dead job replayed` (new, API) | `job_id`, `job_type`, `import_job_id` (if any) |

`peak_rss_mb` is `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024` rounded to 1 decimal (Linux reports KB): the highest memory the worker process has used so far.

### How `job_context` puts the job fields on every line

`job_context` is a `ContextVar` holding a dict. Right after the worker parses a claimed message it sets it to `{"job_id", "job_type", "import_job_id"}` (`import_job_id` only when the payload has one) and keeps the token. At the very end of the iteration a `finally` resets it with the token, so the skip path, the success path, the failure / retry / dead path and the timeout path all log INSIDE the context (the `continue` statements also pass through the `finally`). `JobContextFilter` is attached to the handler: for every record it copies each key of the context onto the record unless the record already has that attribute (so a value passed with `extra=` wins). A handler such as `process_csv` just calls `logger.info(...)` and its lines come out with `job_id`, `job_type` and `import_job_id`, without any of that being passed down as arguments. A thread started by the worker does not inherit the context of the main thread, so the heartbeat thread logs without job fields. The `shutdown requested` line is logged from a signal handler that can run in the middle of a job, so it is logged with `contextvars.Context().run(...)`, which uses an empty context.

### Why the reaper passes its fields explicitly

The reaper runs at the top of the worker loop, outside any job, and it recovers jobs that belong to OTHER (dead) workers. `job_context` is empty there, and the job it talks about is not "the current job" anyway. So the reaper passes `job_id`, `job_type`, `import_job_id` and `dead_worker_id` through `extra=` on its line (and on the line for a failed status update). Its scan, heartbeat, startup and shutdown lines are not about a job and carry no `job_id`.

### Tracing one import

1. `docker compose logs --no-log-prefix api worker | grep <import id>`: shows the API line `import uploaded` (it has the queue `job_id`), the worker lines of the `process_csv` job (all with `job_id` and `job_type`), and the lines of the chained `send_email` job (they mention the import id in the message and carry the `send_email` job's own `job_id`). API access lines of requests for that id also match, and have no `job_id`.
2. Take the queue `job_id` from the `import uploaded` line and `grep` it: the same worker lines, including `Claimed`, retries and `Finished job`. This also finds lines that do not mention the import id (for example the `Claimed job` line).

Checked: for an import of 2,000 rows, every worker line that mentions the import id had `job_id` and `job_type`; the queue job id found the same lines; `grep` for the id found no heartbeat or startup line.

### `GET /stats`

`app/routes/admin.py`, built step by step. Redis and Postgres are read one after the other, not in one snapshot.

| Field | Where it comes from |
|---|---|
| `queue.waiting` | `LLEN queue` |
| `queue.delayed` | `ZCARD delayed` |
| `queue.processing` | the sum of `LLEN` over every `processing:*` list (found with `scan_iter`, never `KEYS`) |
| `queue.dead` | `LLEN dead` |
| `workers.alive`, `workers.ids` | the `worker:*` keys (heartbeats, found with `scan_iter`); `ids` is sorted. A worker whose heartbeat expired is not counted |
| `imports.by_status` | one `SELECT status, count(*) ... GROUP BY status`; all eight statuses (`JOB_STATUSES` in `app/schemas.py`) are present, 0 when there is none |
| `imports.total` | the sum of `by_status` |
| `durations.finished_imports` | number of imports with status `completed` or `completed_with_errors` |
| `durations.avg_run_seconds` | average of `finished_at - started_at` of those imports, rounded to 2 decimals, `null` if there are none |
| `durations.avg_total_seconds` | average of `finished_at - created_at` of those imports (so it includes the time in the queue and the retries), rounded to 2 decimals, `null` if none |

The three duration values come from one query. `failed`, `cancelled` and other statuses are not part of the durations.

### Dead job list and replay

`GET /dead-jobs?limit=100`: `LRANGE dead 0 limit-1`. Dead messages are pushed on the left, so the newest come first. A message that is not valid JSON is skipped with a warning log. `limit` must be 1 to 1000 (`422` otherwise).

`POST /dead-jobs/{job_id}/replay`:
1. Read the whole `dead` list, parse each message and find the one with this `job_id` (`404` `dead job not found` if none).
2. Build the new message: same `job_id`, `job_type` and `payload`, `attempts` 0, `max_attempts` from the settings, no `last_error` and no `failed_at`. The job keeps its id, so one `job_id` can be followed through the first run, the death and the replay in the logs.
3. Move it with `replay_dead_job_script` (Lua, in `app/queue/redis_queue.py`): `LREM` of the original message from `dead`, and ONLY IF exactly 1 item was removed, `LPUSH` of the new message to `queue` and return 1; otherwise return 0 (then the API returns `404`).
4. For `process_csv` jobs only: one conditional `UPDATE import_jobs SET status='queued', error_message=NULL, finished_at=NULL WHERE id=... AND status='failed'`. Any other status is left alone. `send_email` and test jobs touch nothing in the database (a replayed `send_email` job sets `email_status` itself, like any other run).
5. Log `dead job replayed` and return `200` with the new message and `"queued": true`.

**Why a Lua script.** Without it the API would do `LREM` and then `LPUSH` as two commands. Two requests for the same dead job could both find the message in their read of the list; both then push, and the job would be queued twice, or a crash between the two commands would lose the job. In one Lua script the `LREM` and the `LPUSH` are one atomic step, and the second request's `LREM` removes 0 items and gets `0`, so it returns `404`. Checked: two simultaneous requests for the same job gave one `200` and one `404`, and one entry in `queue`.

**Why replaying a failed import resumes from its checkpoint.** A failed import keeps its checkpoint (`last_processed_row`, the counters, `report_bytes`) and its report file; nothing is reset when a handler fails (5.3). The replay only clears `finished_at` and sets `queued`. `process_csv` resumes when `last_processed_row > 0`, `finished_at` is null and the report file is intact, so the replayed job continues from the checkpoint. If the import failed before the first batch (no checkpoint), it starts from scratch. The replay does not clear the counters, so a replay never double counts.

**How it was checked.** `always_fail` x2: both dead with `attempts` 3 and `last_error`, listed newest first; `limit=1` gave one item, `limit=0` and `limit=1001` gave `422`. Replaying one: `200`, the dead list went from 2 to 1, the job ran attempts 1 to 3 again with the same `job_id` and was dead again with a new `failed_at`. `flaky_job` with `--fail-times 3`: dead after 3 failures, replay gave `200`, the 4th call succeeded and the job was gone from `dead`. Unknown id: `404`. Two simultaneous replays: one `200`, one `404`, one message in `queue`. A 20,000-row import whose file was moved away failed after 3 attempts (status `failed`, `error_message` `No such file or directory`, 1 dead job); the file was put back and the workers stopped; the replay gave `200` and `GET /imports/<id>` showed `queued`, `error_message` null, `finished_at` null at once; after the workers started the import ended `completed_with_errors` with 18,000 valid and 2,000 invalid rows (the generator's counts), 18,000 customers (18,000 distinct) and one email in Mailpit. With Mailpit stopped, an import completed, its `send_email` job died (`email_status` `failed`, `Temporary failure in name resolution`); after Mailpit started and the replay, one email arrived and `email_status` was `sent`.

### Other checks of 6.3

- All lines of `docker compose logs --no-log-prefix worker api` parsed as JSON (384 lines, 0 bad).
- Failure logging: the retry lines had `attempt`, `max_attempts`, `delay_seconds` and `error`; the dead line had `attempt` and `error`; each traceback was in the `exception` field of one JSON line. A job that timed out (`JOB_TIMEOUT_SECONDS=3`) logged the timeout lines inside the job context.
- `/stats` when idle: `waiting` 0, `delayed` 0, `processing` 0, `alive` 3 with 3 ids; `by_status` matched `SELECT status, count(*) ... GROUP BY status` with all eight statuses present; the durations were not null.
- `/stats` under load (three 300,000-row files, polled every second): `processing` 3, `running` 3, `waiting` 0 for the whole run; afterwards `processing` 0 and `finished_imports` went from 46 to 49.
- `/stats` with no workers: two uploads gave `waiting` 2, `queued` 2, `alive` 0; after the workers started both imports were processed.
- Earlier milestones: a worker killed with `docker kill` in the middle of a 300,000-row import was recovered by another worker's reaper (the log line has `dead_worker_id`, `destination` and `import_job_id`) and the import resumed from line 34001 and ended with exactly 270,000 valid and 30,000 invalid rows and 270,000 customers; a cancel returned `202`, the import ended `cancelled` with `customers` equal to `valid_rows`; a second cancel returned `200`; `check_no_double_claim` printed PASS for 100 jobs. A `docker compose stop worker` during a long import was cut off by the 30 s grace period and the reaper recovered the job, as documented in 5.2.

### Known limits of 6.3

1. The stats read Redis and Postgres at slightly different moments, so the numbers can disagree for a moment (for example `queue.processing` 1 while no import is `running` yet).
2. `processing` includes the lists of dead workers until the reaper clears them.
3. `avg_run_seconds` uses `started_at`, which every attempt resets, so a retried import shows only its last attempt. `avg_total_seconds` includes the queue time and the retries.
4. There are no durations for `send_email` jobs.
5. The admin endpoints (`/stats`, `/dead-jobs`, replay) have no authentication.
6. Listing and replaying read the whole dead list (the replay) or the first `limit` items (the list). This is fine for a small dead list.
7. A replayed job for an import that was cancelled is skipped by the start check (6.2). (Only a `failed` import goes back to `queued`.)
8. A replayed failed import resumes from its checkpoint, so the file must not have changed.
9. `peak_rss_mb` is the peak of the whole worker process lifetime, so restart the worker before a clean measurement.
10. Log lines written by third-party libraries outside a job carry no job fields.

There is no endpoint to delete a dead job and no "replay all" (for tests, `redis-cli DEL dead`).

## Large-file test (Milestone 6.3)

The point: the design streams the file (CSV read row by row, 1,000 rows per batch, customers inserted per batch), so memory must not grow with the file. The numbers below are measured, with the 6.3 code, on one machine (Docker containers, 7.2 GiB available).

**Test 1: one worker, 500,000 rows.** `docker compose up -d --scale worker=1`, `docker compose restart worker` (so `peak_rss_mb` starts clean), `TRUNCATE customers`, file made with `generate_test_csv.py --rows 500000 --invalid-percent 10 --email-prefix big --seed 77`.

| Measure | Value |
|---|---|
| Rows / file size | 500,000 rows, 32,198,927 bytes (30.7 MiB) |
| Expected counts | 450,000 valid, 50,000 invalid |
| Result | `completed_with_errors`, `total_rows` 500,000, `valid_rows` 450,000, `invalid_rows` 50,000, `COUNT(*)` of `customers` 450,000, one email sent |
| Duration | 138.8 s in the `finished` line (2 min 18.8 s from `started_at` to `finished_at`) |
| Speed | 3,603 rows per second |
| `peak_rss_mb` at the first progress line (line 20001) | 74.6 |
| `peak_rss_mb` at the last progress line (line 500001, 25 progress lines) | 117.3 (42.7 MB above the first; limit was 150) |
| Final `peak_rss_mb` | 117.3 (limit was 400) |
| Worker container (`docker stats`) | 46.3 MiB before the upload; 55 MiB at the start, 97 MiB after 2 minutes; 84 MiB at the end |
| API container (`docker stats`) | 71.2 MiB before the upload |
| API container during the upload | The upload through `curl` over localhost took 0.23 s, too short to sample (72.9 to 73.3 MiB in the samples that followed). So the same file was uploaded again at 6 MB/s (5.2 s) with a sample every 2 s: 73.5, 75.0, 74.8, 73.6, 73.6, 73.5, 73.5 MiB. The highest value is 3.8 MiB above the value before (limit was 50). That second import was cancelled (all its emails already existed) |

**Test 2: three workers, three 300,000-row files at once.** `docker compose up -d --scale worker=3`, `docker compose restart worker`, `TRUNCATE customers`, three files (19.0 MB each, prefixes `L1` to `L3`, seeds 1 to 3, 270,000 valid and 30,000 invalid each) uploaded at the same moment.

| Measure | Value |
|---|---|
| Result | each import `completed_with_errors` with exactly 300,000 / 270,000 / 30,000; `customers` 810,000 (810,000 distinct emails), the sum of the three valid counts |
| Deadlocks | none in the logs |
| Duration per import | 92.1 s, 92.6 s, 92.3 s (while running in parallel) |
| Speed per worker | 3,258, 3,240, 3,249 rows per second (about 9,750 rows per second together) |
| `peak_rss_mb` first progress line / last progress line / final, per worker | 74.6 / 98.0 / 98.0, 74.9 / 98.4 / 98.4, 75.2 / 98.4 / 98.4 |

**What the numbers mean.** Memory does not depend on the size of the file. At the first progress line (20,000 rows in) the worker was at about 75 MB: that is the Python interpreter, the libraries (SQLAlchemy, Pydantic, `email-validator`), the batch and the first 20,000 emails. After that the peak grows slowly: 42.7 MB more for 450,000 more valid emails (500,000 rows), and 23 MB more for 270,000 valid emails (300,000 rows), which is about 85 to 95 bytes per email in both tests. That growth is the **seen-email set** (`seen_emails`, the lowercased emails of the valid rows, used to find duplicates inside the file): it grows with the number of DISTINCT emails, not with the number of rows or the size of the file, and it is the only structure that does. Everything else is per batch (1,000 rows, thrown away after the commit), and the file and the report are read and written line by line. The API never holds the upload: it copies it to disk in 1 MB chunks, so its memory stayed flat while a 32 MB file came in. Nothing was holding rows in memory, so no fix was needed in this milestone.

The 5.x "known limit" stays: the seen-email set is the memory cost of the duplicate check. A file with 50 million distinct emails would need a few GB; a different design (for example letting Postgres find the duplicates) would be needed for that.

## Key decisions

- **One upload function.** `save_upload` in `app/services/imports.py` does the extension check, chunked write, size check, empty check and header check. It raises `HTTPException` directly and deletes the file on any rejection.
- **Row after file.** The `import_jobs` row is created only after the file passes all checks, so a rejected upload leaves no row and no file.
- **Size limit.** The upload is copied in 1 MB chunks and bytes are counted. When the count passes `MAX_UPLOAD_SIZE_MB`, the partial file is deleted and the API returns 413. (Starlette itself spools the multipart body to a temp file before the route runs, so nothing is held fully in memory.)
- **Header check.** Only the first row is read, with `utf-8-sig` so an Excel BOM is ignored. Column names are trimmed of whitespace and matched exactly (case-sensitive). A file that is not valid UTF-8 returns 400.
- **Settings.** `pydantic-settings` reads `DATABASE_URL`, `UPLOAD_FOLDER` (default `uploads`) and `MAX_UPLOAD_SIZE_MB` (default 50). The Postgres container reads its credentials from the same `.env`.
- **Migrations.** Written by hand, not autogenerated. Run manually with `docker compose exec api alembic upgrade head`, not on container start.
- **Server defaults.** Counters, `status` and `created_at` use database `server_default`s, so the model and migration match.
- **Ports.** api 8000, redis 6379, mailpit web 8025 and SMTP 1025. Postgres is not published to the host.
- **Uploads and reports volumes.** `./uploads` and `./reports` are mounted at `/app/uploads` and `/app/reports` in both the api and the worker, so files survive restarts and the api can serve what the worker wrote. Paths stored in the database are relative (`reports/errors_<id>.csv`), resolved from `/app`.
- **Reports folder is a constant.** `REPORTS_FOLDER = "reports"` is in `process_csv.py`, not a setting.
- **Email domains.** `email-validator` rejects reserved domains such as `.test` and `.invalid` (use `.com` or `.org` in test data).
- **Worker and API share one image.** The `worker` service builds the same Dockerfile and `.env`, and only overrides the command.
- **Short DB sessions in the worker.** The worker opens a new session for each status update (`with SessionLocal() as db`) instead of holding one open while a handler runs.
- **Redis `decode_responses=True`.** Messages come back as `str`, so the exact string from `BLMOVE` is passed to `LREM`.
- **Enqueue failure.** `POST /imports` catches `redis.exceptions.RedisError` only. The uploaded file is left on disk when this happens.
- **`REDIS_URL` has no default.** Like `DATABASE_URL`, it must be set. `.env.example` has `redis://redis:6379/0`. Copy the line into an existing `.env`.
- **Worker restarts.** If Redis goes away (or Postgres goes away outside a job), the worker raises and exits. Docker restarts it (`restart: unless-stopped`). A job being run at that moment stays in that worker's processing list, and the reaper in another (or the restarted) worker recovers it (5.2).

## How to run

```
cp .env.example .env
docker compose up --build                 # run everything
docker compose exec api alembic upgrade head   # create the tables (first run, in another terminal)
docker compose logs -f api
docker compose logs -f worker
docker compose logs --no-log-prefix worker | grep <import id>   # 6.3: follow one import (every line is JSON)
curl localhost:8000/stats                  # 6.3: what the system is doing now
curl localhost:8000/dead-jobs              # 6.3: the dead letter queue
docker compose up -d --scale worker=3     # run 3 workers (6.1); --scale worker=1 goes back to one
docker compose down                       # stop (add -v to also delete the database)
```

- API docs: http://localhost:8000/docs
- Mailpit inbox: http://localhost:8025
- Read the logs (6.3): every line is one JSON object. `docker compose logs --no-log-prefix worker api | grep <import id>` shows one import from the upload to the end (add `| python3 -m json.tool` to one line to read it, or filter with `jq` if you have it). Check that every line is JSON: `docker compose logs --no-log-prefix worker | python3 -c "import sys,json; [json.loads(l) for l in sys.stdin]"`. Without `--no-log-prefix` each line starts with the container name, for example `worker-2`
- Stats (6.3): `curl localhost:8000/stats`, or http://localhost:8000/docs
- Watch the workers: `docker compose logs -f worker`, `docker compose ps`, `docker compose exec redis redis-cli KEYS 'worker:*'` (one heartbeat key per worker) and `KEYS 'processing:*'` (a list exists only while that worker holds a job)

## What comes next

**All planned milestones are done.**
