# PROJECT_CONTEXT.md

## Project summary

CSV Customer Import: a learning project. An API accepts a CSV of customer records. A background worker validates each row, inserts the valid ones into Postgres, writes a report of invalid rows, and (from Milestone 4.1) sends an email when done. The job queue is hand-built on Redis, because the point is to learn how job systems work (claiming, acknowledgement, retries, dead letter queue, crash recovery). See `CLAUDE.md` for the tech stack, coding style and rules.

## Current phase and milestone

- **Phase 1: Foundation and Upload API**: Milestone 1.1 DONE
- **Phase 2: Redis Queue and Worker**: Milestone 2.1 DONE
- **Phase 3: CSV Processing Pipeline**: Milestone 3.1 DONE
- **Phase 4: Email Notification Job**: Milestone 4.1 DONE
- **Phase 5: Reliability**: Milestone 5.1 DONE (retries, backoff, dead letter queue). Milestone 5.2 DONE (crash recovery: heartbeats, reaper, job timeout, graceful shutdown). Milestone 5.3 DONE (checkpoint and resume). Next: 6.1

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

## Files

| File | Purpose |
|---|---|
| `requirements.txt` | Python dependencies (pinned). 3.1 added `email-validator` and `faker` |
| `.env.example` | Template for `.env` (database URL, Redis URL, upload folder, max upload size, Postgres credentials, the 4.1 email settings, the 5.1 retry settings and the 5.2 crash recovery settings) |
| `.gitignore` | Ignores `.env`, caches, uploaded files and generated reports |
| `Dockerfile` | Python 3.12 image for the API |
| `docker-compose.yml` | Runs api, worker, postgres, redis, mailpit. Mounts `./uploads` and `./reports` into both the api and worker containers (so the api can serve files the worker wrote). The worker has `restart: unless-stopped`, and 5.1 passes `MAX_ATTEMPTS`, `RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS` to it (`${VAR:-default}`, so a value in `.env` or in the shell wins). 5.2 passes `HEARTBEAT_INTERVAL_SECONDS`, `HEARTBEAT_TTL_SECONDS`, `REAPER_INTERVAL_SECONDS` and `JOB_TIMEOUT_SECONDS` the same way and sets `stop_grace_period: 30s` on the worker |
| `alembic.ini` | Alembic config (the database URL comes from `app/config.py`) |
| `alembic/env.py` | Alembic runtime: connects using `settings.database_url`, uses `Base.metadata` |
| `alembic/script.py.mako` | Template Alembic uses for new migrations |
| `alembic/versions/0001_create_customers_and_import_jobs.py` | Initial migration (hand-written) |
| `alembic/versions/0002_add_email_columns.py` | 4.1: adds `notify_email`, `email_status`, `email_error` to `import_jobs` |
| `alembic/versions/0003_add_customer_import_job_id.py` | 5.1: adds `customers.import_job_id` |
| `alembic/versions/0004_add_report_bytes.py` | 5.3: adds `import_jobs.report_bytes` (hand-written) |
| `app/config.py` | Settings from environment variables (`redis_url`, the five email settings since 4.1, and `max_attempts`, `retry_base_seconds`, `retry_max_seconds` since 5.1, and the four crash recovery settings since 5.2) |
| `app/database.py` | Engine, session factory, `Base`, and the `get_db` dependency |
| `app/models.py` | `Customer` (has `import_job_id` since 5.1) and `ImportJob` models (`ImportJob` has the three email columns since 4.1 and `report_bytes` since 5.3) |
| `app/schemas.py` | Pydantic response models (`ImportJobResponse` (now with `notify_email` and `email_status`), `ImportJobCreatedResponse`), the `CustomerRow` row validator and the `ALLOWED_COUNTRY_CODES` constant |
| `app/services/imports.py` | `save_upload`: all upload validation and saving in one function |
| `app/routes/imports.py` | `POST /imports` (validates `notify_email`, enqueues the job), `GET /imports/{id}`, `GET /imports`, `GET /imports/{id}/errors` |
| `app/main.py` | Creates the FastAPI app and includes the router |
| `app/queue/__init__.py` | Empty, marks the package |
| `app/queue/redis_queue.py` | Redis connection (`redis_client`), the key names and prefixes, `enqueue`, the Lua script and `promote_due_jobs` (5.1), and the recovery Lua script `recover_job_script` (5.2) |
| `app/queue/worker.py` | The worker loop (`run_worker`), run with `python -m app.queue.worker`. Has the retry / dead letter logic since 5.1. Since 5.2 it also has the worker id, the heartbeat thread function (`send_heartbeats`), the reaper (`reap_dead_workers`), `JobTimeoutError` with its SIGALRM handler, and the shutdown signal handler |
| `app/handlers/__init__.py` | The handler registry dictionary `HANDLERS` (`process_csv`, `send_email`, `sleep_job`, `always_fail`, `flaky_job`) |
| `app/handlers/process_csv.py` | The real CSV pipeline, one function (`BATCH_SIZE = 1000`). Enqueues `send_email` at the end (4.1). Since 5.3 it saves a checkpoint after every batch and resumes from it when it can; otherwise it starts from scratch |
| `app/handlers/send_email.py` | 4.1: the `send_email` handler, one function |
| `app/handlers/test_handlers.py` | `sleep_job`, `always_fail` and `flaky_job` (5.1), for testing the worker |
| `scripts/enqueue_test_job.py` | Enqueues a job by type without uploading a file. `--import-job-id <id>` makes the payload `{"import_job_id": ...}`, to resend an email (4.1) or to run `process_csv` again. 5.1 added `flaky_job` with `--name` and `--fail-times` |
| `scripts/generate_test_csv.py` | Generates a test customers CSV with deliberately bad rows and prints the expected counts |
| `uploads/.gitkeep` | Keeps the uploads folder in the project (uploaded files are git-ignored) |
| `reports/.gitkeep` | Keeps the reports folder in the project (error reports are git-ignored) |

The `__init__.py` files in `app/`, `app/routes/` and `app/services/` are empty and only mark the packages.

## Endpoints

| Endpoint | Behavior |
|---|---|
| `POST /imports` | Multipart upload, field `file`, optional form field `notify_email` (blank or missing means `DEFAULT_NOTIFY_EMAIL`; an invalid one returns `400` before anything is saved). Saves to `uploads/<job_id>.csv`, creates an `import_jobs` row with status `queued`, calls `enqueue("process_csv", {"import_job_id": "<id>"})`, returns `202 {"job_id": "..."}`. Errors: `400` (invalid `notify_email`, not .csv, empty, no header, missing required columns), `413` (over size limit), `503` (Redis is down: the row is set to `failed` with an `error_message` and `finished_at`) |
| `GET /imports/{id}` | Returns id, filename, status, total_rows, valid_rows, invalid_rows, error_message, created_at, started_at, finished_at, notify_email, email_status. `404` if unknown (`422` if the id is not a valid UUID) |
| `GET /imports` | All jobs, newest first. No pagination or filtering |
| `GET /imports/{id}/errors` | Downloads the invalid-rows report (`FileResponse`, `text/csv`, filename `errors_<id>.csv`). `404` if the job does not exist or has no error report (for example, a job with 0 invalid rows) |

Required CSV columns: `name`, `email`, `phone`, `date_of_birth`, `country`. Extra columns are allowed.

## Tables

**customers**: `id` UUID PK, `name` varchar(255), `email` varchar(255) unique, `phone` varchar(50), `date_of_birth` date nullable, `country` varchar(100), `created_at` timestamptz (default now).

**import_jobs**: `id` UUID PK, `filename` varchar(255), `file_path` varchar(500), `status` varchar(30) (default `queued`), `total_rows`, `valid_rows`, `invalid_rows`, `last_processed_row`, `attempts` (integers, default 0), `report_bytes` bigint (default 0, since 5.3: size in bytes of the error report file as of the last committed batch), `error_report_path` varchar(500) nullable, `error_message` text nullable, `created_at` timestamptz (default now), `started_at` and `finished_at` timestamptz nullable, and since 4.1: `notify_email` varchar(255) nullable (only old rows are null), `email_status` varchar(20) (default `pending`), `email_error` text nullable.

**customers** also has, since 5.1, `import_job_id` UUID nullable (no foreign key): the import that inserted the customer. It is null for customers that were not created by an import.

`email_status` values: `pending` (no email sent yet), `sent`, `failed` (SMTP error, or the `send_email` job could not be enqueued). The import `status` and `email_status` are separate: an email problem never changes the import status.

Job statuses (from CLAUDE.md): `queued`, `running`, `retrying`, `completed`, `completed_with_errors`, `failed`. All six are used now (`completed_with_errors` since 3.1, `retrying` since 5.1). `retrying` means the last attempt failed and another one is scheduled; `failed` now means every attempt failed.

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

**Handler registry.** `HANDLERS` in `app/handlers/__init__.py` is a plain dictionary `job_type -> function`. Each function takes the payload dict. Registered: `process_csv`, `send_email`, `sleep_job` (payload `{"seconds": n}`), `always_fail`, `flaky_job` (payload `{"name": "...", "fail_times": n}`).

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
2. Open the CSV (`utf-8-sig`, `newline=""`) with `csv.DictReader`, and the report in append mode.
3. Read up to 1,000 rows (with their `reader.line_num`). On a resume, rows up to `last_processed_row` are read and thrown away first. Nothing else from the file is in memory.
4. Strip and validate each row, check for duplicates in the file.
5. One query `WHERE email IN (...)` for the valid emails of the batch. Rows already in `customers` become invalid with `email: email already exists`.
6. One bulk `INSERT ... ON CONFLICT (email) DO NOTHING` for the rest.
7. Write the batch's invalid rows to the error report, sorted by line number, then flush. Read the report size from the file descriptor (`os.fstat`).
8. Update the counters, `last_processed_row` and `report_bytes` on `import_jobs` and `COMMIT`, in the same transaction as the insert. One commit per batch. Log `import <id>: processed up to line <n>` after every 20th batch.
9. At the end: `completed` if `invalid_rows == 0`, else `completed_with_errors`; set `finished_at`; then enqueue `send_email`.

The counters are assigned from local totals (not incremented in SQL). The local totals start at 0 on a fresh start and at the committed values on a resume, so running the handler again does not double count them.

**What each counter means** (`import_jobs`):
- `total_rows`: data rows processed so far (blank lines are skipped by the csv module and not counted).
- `valid_rows`: rows sent to the insert (the customers that were added).
- `invalid_rows`: rows that failed validation, were duplicates in the file, or already existed in the database.
- `last_processed_row`: the CSV line number (`reader.line_num`) of the last row of the last committed batch. The header is line 1. Since 5.3 it is the checkpoint: a resume skips every row up to this line. For a quoted field that spans several lines it is the last line of the record.

**Error report** (`reports/errors_<import_job_id>.csv`). Columns: `row_number, name, email, phone, date_of_birth, country, errors`. `row_number` is the CSV line number (first data row is 2). Values are the original ones from the file (not stripped or lowercased); a missing cell is empty. `errors` joins every reason with `"; "`, each as `field: reason`, for example `email: invalid email format; country: not an allowed country code`. The file is created when a fresh start begins (an existing one is replaced), and on a resume the existing file is kept and appended to. With 0 invalid rows it is deleted and `error_report_path` stays empty. Otherwise `error_report_path` is set. `error_report_path` is not in the `GET /imports/{id}` response (check it with SQL).

**Failures.** A bad row is never a failure. A missing file or a file that cannot be decoded raises, and the worker retries the job and finally marks it `failed` (the upload check only decodes the start of the file, so a bad byte later in a large file is found here). Batches committed before the error stay in the database, and the report stays on disk as it is: the next attempt resumes from the checkpoint (since 5.3) and continues the same report. Nothing is deleted or reset when a handler fails.

**Known limits (until Phase 5):**
- **Re-uploading the same file** as a *new* import marks every row that was inserted earlier as invalid with `email: email already exists`, and inserts nothing new. (Running the *same* import job again is safe since 5.1, see below.)
- If two workers insert the same email at the same moment, `ON CONFLICT DO NOTHING` skips the second row silently, and `valid_rows` is one too high. Not handled yet (Phase 6, multiple workers).
- The set of seen emails grows with the number of distinct emails in the file.

**Test data script.** `scripts/generate_test_csv.py --rows 10000 --invalid-percent 10 --output test_customers.csv --seed 42` (these are the defaults). Names and dates of birth come from Faker; emails are `user<row>@example.com`, so they are unique. The exact number of bad rows is `round(rows * percent / 100)`, at random positions, and the six kinds take turns: missing name, bad email format, bad phone, bad date of birth (wrong format or future), country not allowed, duplicate email in file (copies the email of an earlier valid row). Valid phones come in three styles (`+digits`, `+1 (123) 456-7890`, plain digits). It prints the expected total, valid and invalid counts and the count per kind. The expected counts assume none of those emails are in `customers` yet. Run it in the api container; use an output path under `uploads/` so the file appears on the host and can be uploaded with curl:

```
docker compose exec api python scripts/generate_test_csv.py --rows 10000 --invalid-percent 10 --output uploads/test_10k.csv
curl -F file=@uploads/test_10k.csv localhost:8000/imports
```

## Email notification (Milestone 4.1)

**Job chain.** `process_csv` then `send_email`. The email is its own job so that an SMTP problem can never reprocess the CSV.

**Chaining** (end of `process_csv`). After the final status is committed, it calls `enqueue("send_email", {"import_job_id": "<id>"})`. If that raises a `RedisError`, it does not raise: it sets `email_status = "failed"` and `email_error` to the error text, and the import stays `completed` or `completed_with_errors`. If `process_csv` fails (status `failed`), no email is sent. Failure emails come later.

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
queue --BLMOVE--> processing:<worker_id> --handler OK--> LREM (done)
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
4. For `process_csv` only: status `running`, `started_at` set, `attempts + 1`, `error_message` cleared. Then run the handler, with the job timeout timer set around it (5.2). Everything in step 4 is inside one `try`, so an error from the status update counts as a failed attempt too.
5. Success: `LREM` on the worker's own list.
6. Failure: build the new message (`attempts + 1`, `last_error`). If `attempts + 1 < max_attempts`, retry (transaction: `ZADD delayed` + `LREM` on the worker's own list). Otherwise dead (transaction: `LPUSH dead` + `LREM` on the worker's own list, plus `failed_at`). Every retry and every dead job is logged with job id, type, attempt number, delay and error.
7. For `process_csv` only, after the Redis move: status `retrying` (error_message set), or `failed` (error_message and `finished_at` set). If this DB update itself fails it is logged and the worker keeps running; the job is already safe in Redis.

`send_email` jobs are not touched by the worker in `import_jobs`; the handler sets `email_status` (`failed` on error, `sent` on success), so a retried email that succeeds ends as `sent`.

**Backoff.** `delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** failures_before_this_one)`, so with the defaults (2, 60) the waits are 2, 4, 8, ... capped at 60. Then jitter: `final = delay / 2 + random.uniform(0, delay / 2)`, so the real wait is between half the delay and the full delay. **Why jitter:** without it, jobs that failed together (for example when SMTP or Postgres went down) all retry at exactly the same moment and hit the recovering service as one spike. Randomizing the wait spreads them out. With 3 attempts there are only two waits (after attempt 1 and 2); after attempt 3 the job is dead.

**Settings:** `MAX_ATTEMPTS=3`, `RETRY_BASE_SECONDS=2`, `RETRY_MAX_SECONDS=60` (`app/config.py`, `.env.example`, passed to the worker in `docker-compose.yml`). `max_attempts` is stored in each message when it is enqueued, so changing the setting only affects new jobs.

**Why a Lua script for promoting.** Moving a job from `delayed` to `queue` takes two Redis commands (`ZREM` then `LPUSH`). Done from Python, a crash between them would lose the job, and two workers could both read the same due member and both push it. A Lua script runs as one atomic step inside Redis, so neither can happen. It is registered once with `register_script`.

**Why Redis transactions (`pipeline(transaction=True)`, i.e. MULTI/EXEC).** Moving a failed job needs two writes: put it in `delayed` or `dead`, and remove it from the worker's processing list. In one transaction both happen or neither does. A job is therefore always in exactly one place: it can't be lost between the writes, and can't be in both places. (Each Redis command itself is atomic, but a pair of them is not unless wrapped like this.) The `LREM` uses the exact raw string from `BLMOVE`, since that is what is stored in the list.

**Safe retries for `process_csv`.** A job can run more than once, so the handler must give the same result every time:
- `customers.import_job_id` records which import inserted each customer.
- Since 5.3 the normal path for a retry is to **resume** from the checkpoint (see "Checkpoint and resume (Milestone 5.3)"). The reset (counters, `last_processed_row`, `report_bytes` to 0, `error_report_path` cleared, `finished_at` null, new report file) happens only on a **fresh start**: a first attempt, a job that already finished, or a checkpoint that cannot be trusted.
- The "email already exists" query ignores customers whose `import_job_id` equals the current import (`is_distinct_from`, which also treats a null as different). So when a run starts over, rows inserted by an earlier run are not reported as "already exists". They are counted as valid again, the bulk insert skips them with `ON CONFLICT (email) DO NOTHING`, and no duplicate customer is created. Customers from other imports, or with a null `import_job_id`, still count as existing.
- Result: running the same job twice gives the same counters, one report, and the same number of customers.

**Dead messages.** The `dead` list is only written to for now. There are no DLQ endpoints and no replay (Phase 6). Look at it with `redis-cli LRANGE dead 0 -1`.

**`flaky_job`** (`test_handlers.py`): `INCR flaky:<name>`; raises while the counter is `<= fail_times`, then succeeds. The counter stays in Redis, so use a new `--name` for each test (or `DEL flaky:<name>`).

**Test script options** (`docker compose exec api python -m scripts.enqueue_test_job ...`): `flaky_job --name <name> --fail-times <n>`; `--import-job-id <id>` works for any job type, for example `process_csv --import-job-id <id>` runs an import again.

**Still not handled:**
- No cancel, no DLQ endpoints or replay.
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
5. Move it with the Lua script (below). If it returned 1, log it. For `process_csv` jobs only, set the import to `retrying` (recovered) or `failed` with `finished_at` (dead), with `error_message` `"worker died while running this job"`. `send_email` jobs do not touch `import_jobs`. If that update fails it is logged and the reaper carries on.

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

**New log messages** (logger `worker`): `Worker <id> started, waiting for jobs`; `Recovered job <job id> (<type>) from dead worker <id>, moved to 'queue' | 'dead' (attempt n of m)` (warning); `The reaper failed` (with traceback); `Could not refresh the heartbeat` (with traceback); `Job <id> (<type>) finished, but the reaper had already recovered it` (warning, when the acknowledgement `LREM` removes 0); `shutdown requested`; `Processing list of worker <id> is empty` (or a warning that it still has n jobs); `Worker <id> stopped`. A timeout shows up as the normal failure log with the error `job timed out after <n> seconds`.

**How to see it.** `docker compose exec redis redis-cli KEYS 'worker:*'` and `KEYS 'processing:*'`; `TTL worker:<id>` keeps resetting (it stays between about 10 and 15); `docker compose kill -s KILL worker` simulates a crash (then `docker compose start worker`).

**Known limits:**
1. A worker that is alive but cut off from Redis for longer than the TTL can have its job recovered while it is still running it, so the job can run twice. This is why handlers must be idempotent.
2. The SIGALRM timeout fires only between Python instructions, so a long blocking call (for example one huge SQL query) is interrupted only after it returns.
3. The old shared `processing` list is no longer used. A leftover `processing` key from before 5.2 is ignored (the reaper only looks at `processing:*`).
4. The failure transaction from 5.1 does not check that the job is still in the worker's list. If the reaper already recovered the job, a late failure can add a second copy to `delayed` or `dead`.
5. The reaper pushes the job to `queue` first and updates the import status afterwards. With several workers, another worker could claim the job and set `running` in between, and the reaper would then overwrite it with `retrying`. With a single worker this cannot happen.

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

**New log messages** (logger `process_csv`): `starting import <id> from scratch`; `resuming import <id> from line <last_processed_row> (<total_rows> rows already processed)`; a warning `import <id>: the checkpoint cannot be trusted, the error report file is missing` (or `... has <n> bytes but <m> were saved`) just before the fresh start; `import <id>: processed up to line <n>` after every 20th batch.

**How it was checked** (300,000 rows, 30,000 invalid): the worker was killed with `SIGKILL` at about a third of the file and resumed from line 101001 with the counters going on from 101,000; with `JOB_TIMEOUT_SECONDS=15` and `MAX_ATTEMPTS=10` an import resumed after 6 timeouts (7 attempts, each further than the last); with the report file deleted after a kill it logged the "cannot be trusted" warning and started over. All ended with exactly 270,000 valid and 30,000 invalid rows, 270,000 distinct customers, a report with 30,000 data rows and no repeated row numbers, and one email. Running `process_csv` again for a finished import started from scratch and gave the same counters, customer count and a byte-identical report. Clearing `finished_at` on a finished import made the next run resume from line 300001, do nothing, and keep the counters.

**Known limits:**
1. Skipping still parses the already-processed part of the file. That is cheap compared with validation and database work. Seeking to a byte offset would avoid it, but is fragile with quoted fields that contain newlines.
2. The uploaded file must not change between attempts. This is not checked.
3. The seen-email set contains only emails of rows that were inserted. If an earlier row was invalid only because its email already existed from another import, its email is not in the set, so a later duplicate of it is reported as `email: email already exists` instead of `email: duplicate email in file` after a resume. The counters are identical.
4. If two workers ever insert the same email at the same moment, the earlier note about `valid_rows` still applies (Phase 6).
5. The email is still at-least-once.
6. **The rebuilt seen-email set is only correct if every customer of this import was inserted before the checkpoint.** That holds for a normal retry. It does NOT hold when a fresh start happens for an import that already has customers (running `process_csv` again for a finished import, or the "cannot be trusted" fallback after some batches were inserted) and that fresh run is then interrupted and resumed. The set then also contains the emails of the earlier run beyond the checkpoint, so later rows are wrongly reported as `email: duplicate email in file`, and `valid_rows` and the report are wrong (the `customers` table itself stays correct). Seen once while testing: a re-run of a finished 300k import was interrupted by `docker compose stop worker` (the worker was killed after the 30s grace period), the job resumed, and 175,013 rows were wrongly rejected. Running the import once more to the end (it starts from scratch because it is finished) repairs the counters and the report. A real fix needs a decision: for example delete the import's own customers on a fresh start, or store the line number on each customer. Not done in this milestone.

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
docker compose down                       # stop (add -v to also delete the database)
```

- API docs: http://localhost:8000/docs
- Mailpit inbox: http://localhost:8025

## What comes next

**Milestone 6.1: Multiple Workers and Progress.**
