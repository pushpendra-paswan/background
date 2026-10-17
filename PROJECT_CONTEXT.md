# PROJECT_CONTEXT.md

## Project summary

CSV Customer Import: a learning project. An API accepts a CSV of customer records. A background worker validates each row, inserts the valid ones into Postgres, writes a report of invalid rows, and sends an email when done. Jobs run as **Celery tasks** with Redis as the broker. (Until Milestone 7.1 the queue was hand-built on Redis, to learn how job systems work. 7.1 replaced it with Celery, and the section "Hand-built queue vs Celery" compares the two.) See `CLAUDE.md` for the tech stack, coding style and rules.

## Current phase and milestone

- **Phase 1: Foundation and Upload API**: Milestone 1.1 DONE
- **Phase 2: Redis Queue and Worker**: Milestone 2.1 DONE (replaced in 7.1)
- **Phase 3: CSV Processing Pipeline**: Milestone 3.1 DONE
- **Phase 4: Email Notification Job**: Milestone 4.1 DONE
- **Phase 5: Reliability**: Milestones 5.1 (retries), 5.2 (crash recovery, timeout, graceful shutdown) DONE and re-done on Celery in 7.1. Milestone 5.3 (checkpoint and resume) DONE
- **Phase 6: Scale and Visibility**: Milestone 6.1 (multiple workers, progress), 6.2 (cancel), 6.3 (stats, structured logging, large-file test) DONE
- **Phase 7: Celery migration**: Milestone 7.1 DONE. All planned milestones are done

## What was built

- Docker Compose stack: `api`, `worker`, `postgres`, `redis`, `mailpit`.
- FastAPI app, SQLAlchemy 2.0 (sync) models for `customers` and `import_jobs` with UUID primary keys, Alembic migrations, upload handling (extension check, chunked write with a size limit, empty-file check, header check).
- **3.1:** `process_csv` streams the CSV, validates every row, bulk inserts the valid ones, writes an error report for the invalid ones and keeps the counters up to date. `GET /imports/{id}/errors` and a test data script.
- **4.1:** when `process_csv` finishes, it chains a separate `send_email` task that sends a summary email through Mailpit.
- **5.1 / 5.2 / 7.1:** a failing task is retried (3 attempts by default) with exponential backoff and jitter. A task that runs too long gets a soft time limit error and is retried. A task whose worker died is delivered again and resumes. A worker told to stop finishes its task first. Since 7.1 all of this is done by Celery (autoretry, `soft_time_limit`, late acknowledgement and the visibility timeout, warm shutdown) plus three small additions of ours: a Postgres advisory lock per import, a crash-loop guard on `import_jobs.attempts`, and the `ImportTask` hooks that keep `import_jobs.status` right.
- **5.3:** `process_csv` saves a checkpoint after every batch (counters, `last_processed_row` and the size of the error report, all committed together with the insert). A retry, a timeout retry or a redelivered task continues from the checkpoint.
- **6.1:** several workers (`docker compose up -d --scale worker=3`) and several imports in parallel with exact counters, even when they contain the same emails. `GET /imports/{id}` shows `rows_in_file` and `progress_percent`.
- **6.2:** `POST /imports/{id}/cancel`. Every status change made by a task is conditional, so a cancel is never overwritten.
- **6.3:** every log line is one JSON object and every line about a job carries `job_id`. `GET /stats`. A 500,000-row import was run with memory measured.
- **7.1:** the hand-built queue, worker, Lua scripts, heartbeats, reaper and dead letter queue are gone. `POST /imports/{id}/retry` (replaces dead job replay), `POST /imports/{id}/resend-email`, and a new `GET /stats`. See "Celery (Milestone 7.1)" below.

## Files

| File | Purpose |
|---|---|
| `requirements.txt` | Python dependencies (pinned). 3.1 added `email-validator` and `faker`, 7.1 added `celery[redis]==5.6.3` |
| `.env.example` | Template for `.env`: database URL, Redis URL, upload folder, max upload size, Postgres credentials, the email settings, the retry settings (`MAX_ATTEMPTS`, `RETRY_BASE_SECONDS`, `RETRY_MAX_SECONDS`), `JOB_TIMEOUT_SECONDS`, `VISIBILITY_TIMEOUT_SECONDS`, `WORKER_CONCURRENCY` and `LOG_LEVEL` |
| `.gitignore` | Ignores `.env`, caches, uploaded files and generated reports |
| `Dockerfile` | Python 3.12 image, used by both `api` and `worker` |
| `docker-compose.yml` | Runs api, worker, postgres, redis, mailpit. Mounts `./uploads` and `./reports` into api and worker. The worker command is `celery -A app.celery_app --quiet worker --loglevel=INFO --concurrency=${WORKER_CONCURRENCY:-1}`, with `restart: unless-stopped` and `stop_grace_period: 30s`. It passes `MAX_ATTEMPTS`, `RETRY_BASE_SECONDS`, `RETRY_MAX_SECONDS`, `JOB_TIMEOUT_SECONDS`, `VISIBILITY_TIMEOUT_SECONDS` and `WORKER_CONCURRENCY` (`${VAR:-default}`, so a value in `.env` wins; **a variable exported in your shell wins over `.env`**). No `container_name` and no ports on the worker, so `--scale worker=3` works |
| `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako` | Alembic config and runtime (the database URL comes from `app/config.py`) |
| `alembic/versions/0001` to `0005` | Hand-written migrations: tables (0001), email columns (0002), `customers.import_job_id` (0003), `import_jobs.report_bytes` (0004), `import_jobs.rows_in_file` (0005). 7.1 added none |
| `app/config.py` | Settings from environment variables. Queue-related: `redis_url`, `max_attempts`, `retry_base_seconds`, `retry_max_seconds`, `job_timeout_seconds`, `visibility_timeout_seconds` (900), `worker_concurrency` (1), `log_level` |
| `app/database.py` | Engine, session factory, `Base`, and the `get_db` dependency |
| `app/models.py` | `Customer` and `ImportJob` models |
| `app/schemas.py` | Pydantic response models (`ImportJobResponse` with the computed field `progress_percent`, `ImportJobCreatedResponse`), `CustomerRow` (row validator), `ALLOWED_COUNTRY_CODES`, `JOB_STATUSES` and the models of `GET /stats` |
| `app/celery_app.py` | 7.1: the Celery app and its config, the check that the visibility timeout is long enough, the `ImportTask` base class (`on_retry`, `on_failure`) and the four signal handlers (node name, logging, job context before and after a task) |
| `app/logging_config.py` | JSON logging: the `job_context` ContextVar, `JobContextFilter`, `JsonFormatter` and `setup_logging(service, worker_id=None)`. Standard library only |
| `app/services/imports.py` | `save_upload`: all upload validation and saving in one function |
| `app/routes/imports.py` | `POST /imports` (validates `notify_email`, calls `process_csv.delay`), `GET /imports/{id}`, `GET /imports`, `GET /imports/{id}/errors`, `POST /imports/{id}/cancel` |
| `app/routes/admin.py` | `GET /stats`, `POST /imports/{id}/retry` and `POST /imports/{id}/resend-email` (7.1) |
| `app/main.py` | Creates the FastAPI app, calls `setup_logging("api")` and includes the `imports` and `admin` routers |
| `app/handlers/__init__.py` | Empty. (Before 7.1 it held the handler registry dictionary, which Celery's task registry replaced) |
| `app/handlers/process_csv.py` | The `process_csv` task, one function (`BATCH_SIZE = 1000`): advisory lock, conditional start, crash-loop guard, resume or fresh start, row counting, batch loop with checkpoint and cancel check, final status, `send_email.delay` |
| `app/handlers/send_email.py` | The `send_email` task, one function |
| `app/handlers/test_handlers.py` | The tasks `sleep_job`, `always_fail`, `flaky_job` and `tracked_job`, for testing the worker (they do not use `ImportTask`) |
| `scripts/enqueue_test_job.py` | Sends one of the six tasks with `.delay`: `--import-job-id`, `--seconds`, `--name`, `--fail-times`, `--times` (send the same call n times) and `--keep-attempts` (`process_csv` otherwise sets `attempts` to 0 first) |
| `scripts/check_no_double_claim.py` | Sends `tracked_job` tasks and checks that every one ran exactly once, and how many each worker ran |
| `scripts/generate_test_csv.py` | Generates a test customers CSV with deliberately bad rows and prints the expected counts (`--email-prefix`, `--seed`, `--invalid-percent`, `--rows`, `--output`) |
| `uploads/.gitkeep`, `reports/.gitkeep` | Keep the folders in the project (uploaded files and reports are git-ignored) |

The `__init__.py` files in `app/`, `app/routes/`, `app/services/` and `app/handlers/` are empty and only mark the packages.

## Endpoints

| Endpoint | Behavior |
|---|---|
| `POST /imports` | Multipart upload, field `file`, optional form field `notify_email` (blank or missing means `DEFAULT_NOTIFY_EMAIL`; an invalid one returns `400` before anything is saved). Saves to `uploads/<job_id>.csv`, creates an `import_jobs` row with status `queued`, calls `process_csv.delay(<id>)`, returns `202 {"job_id": "..."}`. Errors: `400` (invalid `notify_email`, not .csv, empty, no header, missing required columns), `413` (over size limit), `503` (Redis is down: the row is set to `failed` with an `error_message` and `finished_at`; the answer comes back in about 30 ms because the publish gives up after one retry) |
| `GET /imports/{id}` | id, filename, status, rows_in_file, progress_percent, total_rows, valid_rows, invalid_rows, error_message, created_at, started_at, finished_at, notify_email, email_status. `404` if unknown (`422` if the id is not a valid UUID) |
| `GET /imports` | All jobs, newest first, same fields. No pagination or filtering |
| `GET /imports/{id}/errors` | Downloads the invalid-rows report (`text/csv`, filename `errors_<id>.csv`). `404` if the job does not exist or has no error report |
| `POST /imports/{id}/cancel` | Cancels an import and returns the same body as `GET /imports/{id}`. `200` if it is now `cancelled` (it was `queued` or `retrying`) or already `cancelling` / `cancelled`, `202` if it was `running` and is now `cancelling`, `409` `import already finished` for `completed`, `completed_with_errors` and `failed`, `404` unknown, `422` invalid UUID. See "Cancel" |
| `POST /imports/{id}/retry` | 7.1. Retries a `failed` import (replaces the old dead job replay). `404` if unknown. One conditional `UPDATE ... SET status='queued', error_message=NULL, finished_at=NULL, attempts=0 WHERE id=... AND status='failed'`; no row changed: `409` `only failed imports can be retried`. Then `process_csv.delay`, `202` with the import. If the enqueue fails the import goes back to `failed` and the answer is `503`. The checkpoint and the report are kept, so the import resumes where it stopped. Logs `import retried` |
| `POST /imports/{id}/resend-email` | 7.1. `404` if unknown. One conditional `UPDATE ... SET email_status='pending', email_error=NULL WHERE id=... AND email_status='failed' AND status IN ('completed','completed_with_errors')`; no row changed: `409` `email is not in a failed state`. Then `send_email.delay`, `202` with the import. Logs `email resend requested` |
| `GET /stats` | What the system is doing now, see "Stats" |

Required CSV columns: `name`, `email`, `phone`, `date_of_birth`, `country`. Extra columns are allowed.

## Tables

**customers**: `id` UUID PK, `name` varchar(255), `email` varchar(255) unique, `phone` varchar(50), `date_of_birth` date nullable, `country` varchar(100), `created_at` timestamptz (default now), `import_job_id` UUID nullable (no foreign key): the import that inserted the customer, null for customers that were not created by an import.

**import_jobs**: `id` UUID PK, `filename` varchar(255), `file_path` varchar(500), `status` varchar(30) (default `queued`), `rows_in_file` integer nullable (data rows in the CSV, null until the first attempt has counted them), `total_rows`, `valid_rows`, `invalid_rows`, `last_processed_row`, `attempts` (integers, default 0), `report_bytes` bigint (default 0: size in bytes of the error report file as of the last committed batch), `error_report_path` varchar(500) nullable, `error_message` text nullable, `created_at` timestamptz (default now), `started_at` and `finished_at` timestamptz nullable, `notify_email` varchar(255) nullable, `email_status` varchar(20) (default `pending`), `email_error` text nullable.

`attempts` counts **starts** of `process_csv` (since 7.1 the conditional start `UPDATE` increments it and returns it). Because a crashed run was also a start, a crash counts as an attempt (see "Crash recovery"). `POST /imports/{id}/retry` and `scripts/enqueue_test_job.py process_csv` set it back to 0.

`email_status` values: `pending` (no email sent yet), `sent`, `failed` (SMTP error after all attempts, or the `send_email` task could not be published). The import `status` and `email_status` are separate: an email problem never changes the import status.

Job statuses (from CLAUDE.md): `queued`, `running`, `retrying`, `cancelling`, `cancelled`, `completed`, `completed_with_errors`, `failed`. `retrying` means the last attempt failed and Celery has scheduled another one; `failed` means every attempt failed (or the crash-loop guard gave up, or the enqueue failed). `cancelling` means a cancel was requested while a worker runs the import; `cancelled` is final. The finished states are `completed`, `completed_with_errors`, `failed` and `cancelled`; all of them have `finished_at` set.

## Celery (Milestone 7.1)

### Setup (`app/celery_app.py`)

`celery_app = Celery("app", broker=settings.redis_url, include=["app.handlers.process_csv", "app.handlers.send_email", "app.handlers.test_handlers"])`. The worker is started with `celery -A app.celery_app --quiet worker --loglevel=INFO --concurrency=<WORKER_CONCURRENCY>` (prefork pool). The API imports the same module, so `process_csv.delay(...)` publishes to the same Redis. Configuration:

| Setting | Value | Why |
|---|---|---|
| `task_ignore_result` | `True` (no result backend) | Nobody reads task results; state lives in `import_jobs` |
| `task_serializer`, `accept_content` | json | Plain JSON arguments (an import id string) |
| `timezone` | UTC | |
| `task_acks_late` | `True` | The message is acknowledged after the task ran, so a task whose worker dies is delivered again |
| `task_reject_on_worker_lost` | `True` | If the pool process is killed (for example out of memory), the message is put back instead of being lost |
| `worker_prefetch_multiplier` | 1 | A worker takes one message at a time |
| `broker_connection_retry_on_startup` | `True` | The worker waits for Redis at startup |
| `worker_hijack_root_logger`, `worker_redirect_stdouts` | `False` | Logging is ours (see "Logging") |
| `broker_transport_options` | `{"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS}` | See "Crash recovery" |
| `task_publish_retry_policy` | `max_retries` 1, intervals 0 / 0.2 / 0.5 s | `POST /imports` fails fast with `503` when Redis is down |

At import time `celery_app.py` raises `ValueError` if `VISIBILITY_TIMEOUT_SECONDS <= JOB_TIMEOUT_SECONDS + 30`. With the Redis broker, a task that runs longer than the visibility timeout is delivered to a second worker while the first is still running it. Both the API and the worker run this check.

**Settings:** `MAX_ATTEMPTS` 3 (runs per task, so Celery's `max_retries` is `MAX_ATTEMPTS - 1`), `RETRY_BASE_SECONDS` 2, `RETRY_MAX_SECONDS` 60, `JOB_TIMEOUT_SECONDS` 600, `VISIBILITY_TIMEOUT_SECONDS` 900, `WORKER_CONCURRENCY` 1, `LOG_LEVEL` INFO. All are in `app/config.py` and `.env.example`.

### Tasks

| Task | Signature | Notes |
|---|---|---|
| `process_csv` | `process_csv(self, import_job_id: str)`, `base=ImportTask`, `autoretry_for=(Exception,)`, `retry_backoff=RETRY_BASE_SECONDS`, `retry_backoff_max=RETRY_MAX_SECONDS`, `retry_jitter=True`, `max_retries=MAX_ATTEMPTS-1`, `soft_time_limit=JOB_TIMEOUT_SECONDS` | The pipeline. See "CSV processing" |
| `send_email` | `send_email(self, import_job_id: str)`, `autoretry_for=(OSError,)`, same backoff and `max_retries` | See "Email notification" |
| `sleep_job` | `sleep_job(seconds)` | Test task |
| `always_fail` | `always_fail()`, `autoretry_for=(Exception,)` | Test task: raises on every run |
| `flaky_job` | `flaky_job(name, fail_times)`, `autoretry_for=(Exception,)` | Test task: counts calls in Redis key `flaky:<name>`, fails while the count is `<= fail_times`. Use a new `--name` for each test |
| `tracked_job` | `tracked_job(run, n, seconds=0.2)` | Test task: sleeps, then `HINCRBY ran:<run> <n> 1` and `HINCRBY workers:<run> <hostname> 1` |

Task names are fixed (`name="process_csv"` and so on), so a message does not depend on the module path.

### `ImportTask` (failure hooks)

`on_retry` and `on_failure` exist for `process_csv` only (the other tasks do not use the base class). Both take the import id from `args[0]` and call one private method, `_save_failure(new_status, error, import_job_id)`, which in one session reads the row with `SELECT ... FOR UPDATE` and then:
- status `cancelled`: changes nothing;
- status `cancelling`: sets `cancelled`, `finished_at` (if not set) and `error_message` `cancelled by user`;
- anything else: sets `retrying` (on_retry) or `failed` (on_failure) with `error_message` (the error text, at most 1000 characters; a soft time limit error is written as `job timed out after <n> seconds`), and `finished_at` for `failed`.

If this database update itself fails, it logs `Could not save the <status> status for import <id>` and carries on (the retry is already scheduled). The hooks then log one line each: `job retry` (fields `attempt`, `max_attempts`, `error`) and `job failed` (fields `attempt`, `error`). `attempt` is `self.request.retries + 1`. The hooks run before `task_postrun`, so these lines carry the job fields. A cancel is never overwritten.

Why a lock: the API and several tasks write the same row. "Read the status, then write" could lose a cancel; the row lock makes the decision and the write one step.

### Logging signals (also in `celery_app.py`)

- `celeryd_init` stores the node name (for example `celery@3f2a9c1b7d4e`; inside Docker the part after `@` is the container id).
- `setup_logging` runs our `setup_logging("worker", <node name>)`. Connecting to this signal is what makes Celery install no log handlers of its own. The `worker_id` field of every worker line is the node name. (This is the "four handlers" detail: the node name needs its own signal, because the logging signal does not carry it.)
- `task_prerun` sets `job_context` to `job_id` = the Celery task id, `job_type` = the task name and, for `process_csv` and `send_email`, `import_job_id` = the first argument.
- `task_postrun` resets it.

The worker's console banner is turned off with the global option `--quiet`, and the `SecurityWarning` Celery prints about running as root is filtered in `celery_app.py` (the container runs as root on purpose, the mounted folders belong to root). Without both, every worker prints about 30 plain-text lines at startup.

## CSV processing

**Validation rules** (`CustomerRow` in `app/schemas.py`). Every value is stripped first. An empty or missing cell (short row) counts as missing, reported as `<field>: is required`. Extra columns are ignored. Column names in the header are trimmed too.

| Field | Rule |
|---|---|
| `name` | required, at most 100 characters |
| `email` | required, valid format (`email-validator`, no DNS lookup). Stored lowercased |
| `phone` | required. Spaces, dashes and parentheses are removed, then it must be an optional `+` and 7 to 15 digits. **The cleaned value is what is stored** |
| `date_of_birth` | required, exactly `YYYY-MM-DD`, a real date, not in the future, not more than 120 years ago (43830 days) |
| `country` | required. Uppercased, then must be in `ALLOWED_COUNTRY_CODES` (20 codes: IN US GB CA AU DE FR ES IT NL SG AE JP CN BR MX ZA SE CH NZ) |

All errors of a row are collected. Then two more checks that Pydantic cannot do:
- **Duplicate in file:** a set of lowercased emails seen so far. The first valid occurrence passes, later ones fail with `email: duplicate email in file`. Only rows that passed Pydantic are checked, and only they add to the set.
- **Already in the database:** see the batch flow.

**Start of the task** (`process_csv`, one function that reads top to bottom; every step is also a short comment in the code):
1. **Mutual exclusion.** A dedicated connection (`engine.connect()`) runs `SELECT pg_try_advisory_lock(:key)`, key = the first 8 bytes of the import id as a signed integer. The connection is committed (so it does not sit "idle in transaction", the lock is per session) and kept for the whole task. If the lock is not free the task logs `import already running elsewhere` and returns, which acknowledges a duplicate delivery. In a `finally` the task runs `pg_advisory_unlock` and closes the connection: the unlock is required, because a pooled connection keeps its lock. If the process dies, Postgres releases the lock when the connection drops.
2. Load the `import_jobs` row. Missing: log an error and return.
3. **Conditional start.** One `UPDATE ... SET status='running', started_at=now(), attempts=attempts+1, error_message=NULL WHERE id=... AND status NOT IN ('cancelling','cancelled') RETURNING attempts`. Zero rows: the import was cancelled; a `cancelling` import is set to `cancelled` (with `finished_at` and `cancelled by user`), the task logs `skipping import <id>: cancelled` and returns. No email, no retry.
4. **Crash-loop guard.** If the returned `attempts` is greater than `MAX_ATTEMPTS`, the import is set to `failed` (conditional on `running`) with `error_message` `gave up after <n> attempts (worker crashed or restarted repeatedly)`, and the task returns without processing. Celery does not count a crash as a retry, so without this a task that kills its worker would loop forever.

**Batch flow** (`BATCH_SIZE = 1000`):
1. Decide: resume from the checkpoint, or start from scratch (see "Checkpoint and resume"). A fresh start resets the counters and the checkpoint and creates `reports/errors_<id>.csv` with its header. A resume truncates the report to `report_bytes` and rebuilds the set of seen emails.
2. Count the data rows of the file (see "Progress"), save `rows_in_file` and commit.
3. Open the CSV (`utf-8-sig`, `newline=""`) with `csv.DictReader`, and the report in append mode.
4. Read up to 1,000 rows (with their `reader.line_num`). On a resume, rows up to `last_processed_row` are read and thrown away first. Nothing else from the file is in memory.
5. Strip and validate each row, check for duplicates in the file.
6. If the batch has valid rows: sort them by email, then one bulk `INSERT ... ON CONFLICT (email) DO NOTHING RETURNING email`. If some valid rows were not returned, one more query finds which of them belong to this import (`import_job_id` = this import): those were inserted by an earlier run of this import and stay valid. Every other valid row that was not returned becomes invalid with `email: email already exists`.
7. Write the batch's invalid rows to the error report, sorted by line number, then flush. Read the report size from the file descriptor (`os.fstat`).
8. Update the counters, `last_processed_row` and `report_bytes` on `import_jobs` and `COMMIT`, in the same transaction as the insert. Log `import <id>: processed up to line <n>` after every 20th batch.
9. Right after the commit: a fresh query for the status. If it is `cancelling`, stop reading rows.
10. At the end: delete the report if `invalid_rows == 0`, else set `error_report_path`. Then one conditional `UPDATE ... SET status = <completed or completed_with_errors>, finished_at = now() WHERE id = ... AND status = 'running'`. If it changed one row, log `import <id> finished` and call `send_email.delay(import_job_id)`. If it raises `kombu.exceptions.OperationalError`, the task does not raise: it sets `email_status` `failed` and `email_error`, and finishes normally. If it changed 0 rows (or step 9 stopped the loop), the import was cancelled: see "Cancel". A cancelled or failed import sends no email.

The counters are assigned from local totals (not incremented in SQL). The local totals start at 0 on a fresh start and at the committed values on a resume.

**What each counter means** (`import_jobs`):
- `rows_in_file`: data rows in the whole file, counted at the start of every attempt. It does not change while the import runs.
- `total_rows`: data rows processed so far (blank lines are skipped by the csv module and not counted).
- `valid_rows`: exactly the rows whose customer exists because of this import. It is exact even when other imports run at the same moment.
- `invalid_rows`: rows that failed validation, were duplicates in the file, or whose email belongs to another import (`email: email already exists`).
- `last_processed_row`: the CSV line number (`reader.line_num`) of the last row of the last committed batch. The header is line 1. It is the checkpoint: a resume skips every row up to this line.

**Error report** (`reports/errors_<import_job_id>.csv`). Columns: `row_number, name, email, phone, date_of_birth, country, errors`. `row_number` is the CSV line number (first data row is 2). Values are the original ones from the file; a missing cell is empty. `errors` joins every reason with `"; "`, each as `field: reason`. The file is created when a fresh start begins, and on a resume the existing file is kept and appended to. With 0 invalid rows it is deleted and `error_report_path` stays empty. `error_report_path` is not in the `GET /imports/{id}` response (check it with SQL).

**Failures.** A bad row is never a failure. A missing file or a file that cannot be decoded raises: Celery retries the task (the import shows `retrying` between attempts) and after the last attempt the `on_failure` hook marks it `failed`. Batches committed before the error stay in the database, the report stays on disk, and the next attempt resumes from the checkpoint. Nothing is deleted or reset when a task fails.

**Test data script.** `scripts/generate_test_csv.py --rows 10000 --invalid-percent 10 --output test_customers.csv --seed 42` (these are the defaults). Emails are `<prefix><row>@example.com` (`--email-prefix`, default `user`), so files with different prefixes do not overlap and files with the same prefix overlap on purpose. The exact number of bad rows is `round(rows * percent / 100)`; six kinds take turns (missing name, bad email format, bad phone, bad date of birth, country not allowed, duplicate email in file). It prints the expected total, valid and invalid counts. The expected counts assume none of those emails are in `customers` yet (`TRUNCATE customers` before a test). Use an output path under `uploads/`:

```
docker compose exec api python scripts/generate_test_csv.py --rows 10000 --invalid-percent 10 --output uploads/test_10k.csv
curl -F file=@uploads/test_10k.csv localhost:8000/imports
```

**Known limits:**
- **Re-uploading the same file** as a *new* import marks every row that was inserted earlier as invalid with `email: email already exists`. (Running the *same* import again is safe.)
- The set of seen emails grows with the number of distinct emails in the file.

## Email notification

**Task chain.** `process_csv` then `send_email`. The email is its own task so that an SMTP problem can never reprocess the CSV. It never touches `import_jobs.status`.

**`send_email`** (`app/handlers/send_email.py`, one function; `autoretry_for=(OSError,)`):
1. Load the `import_jobs` row.
2. If `email_status` is already `sent`: log it and return (no second email).
3. Build a plain-text `EmailMessage`. From `SMTP_FROM`. To `notify_email` (old rows with no `notify_email` use `DEFAULT_NOTIFY_EMAIL`). Subject `Import finished: <filename> (<status>)`. Body lines: filename, status, total rows, inserted rows, rejected rows, started at, finished at, and, if `error_report_path` is set, `Error report: <APP_BASE_URL>/imports/<id>/errors`.
4. Send with `smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)`. No TLS, no login.
5. Success: `email_status = "sent"` and `email_error` cleared.
6. Failure (`OSError`, which covers `SMTPException`, refused connections, DNS errors and timeouts): `email_status = "failed"`, `email_error` = the message, then re-raise, so Celery retries it. After the last attempt the status stays `failed`; `POST /imports/{id}/resend-email` sends it again.

**Settings:** `SMTP_HOST` (`mailpit`), `SMTP_PORT` (`1025`), `SMTP_FROM` (`noreply@example.com`), `DEFAULT_NOTIFY_EMAIL` (`admin@example.com`), `APP_BASE_URL` (`http://localhost:8000`).

**Delivery guarantee.** Emails are at-least-once. If a worker dies after the SMTP server accepted the message but before `email_status = "sent"` is saved, the task can run again and send a second email. The `already sent` check prevents duplicates in the normal case.

## Retries

**Flow.** A task that raises is retried by Celery's autoretry: it publishes a new message with a countdown (an ETA) and acknowledges the old one. `max_retries = MAX_ATTEMPTS - 1`, so there are `MAX_ATTEMPTS` runs. For `process_csv`:

```
run fails -> Celery schedules the retry -> on_retry: status 'retrying', error_message, log "job retry"
          -> retry runs -> conditional start sets 'running' again
last run fails -> Celery re-raises -> on_failure: status 'failed', error_message, finished_at, log "job failed"
```

**Backoff.** `retry_backoff=RETRY_BASE_SECONDS`, `retry_backoff_max=RETRY_MAX_SECONDS`, `retry_jitter=True`: the countdown is `RETRY_BASE_SECONDS * 2 ** retries`, capped at `RETRY_MAX_SECONDS`, and with jitter Celery picks a random whole number of seconds between **0 and** the countdown (full jitter; the hand-built queue used half to full). So the upper bound grows 2, 4, 8, ... but a single wait can be 0 s. Seen in the logs for `always_fail`: `Retry in 1s`, then `Retry in 4s`; `flaky_job`: `Retry in 0s` twice. **Why jitter:** without it, tasks that failed together (for example when SMTP or Postgres went down) all retry at the same moment and hit the recovering service as one spike.

**Where to see it.** For `process_csv` the hooks log `job retry` and `job failed` with `attempt`, `max_attempts` and `error`. The test tasks do not use `ImportTask`, so for `always_fail` and `flaky_job` the evidence is Celery's own lines (`Task always_fail[...] retry: Retry in 4s: RuntimeError(...)`, then `raised unexpected`).

**No dead letter queue.** Celery has no equivalent. A task that used up its attempts is simply logged as failed and gone. For imports the `failed` status is the record, and `POST /imports/{id}/retry` is the way back (the import resumes from its checkpoint, so the file must not have changed). For emails: `email_status` `failed` and `POST /imports/{id}/resend-email`. Test tasks have no record at all.

**Safe retries for `process_csv`.** A task can run more than once, so it must give the same result every time:
- `customers.import_job_id` records which import inserted each customer.
- The normal path for a retry is to **resume** from the checkpoint. The reset (counters, `last_processed_row`, `report_bytes` to 0, `error_report_path` cleared, `finished_at` null, new report file) happens only on a **fresh start**: a first attempt, a job that already finished, or a checkpoint that cannot be trusted.
- A valid row that the bulk insert skipped (`ON CONFLICT (email) DO NOTHING`, not in `RETURNING`) is looked up with `import_job_id` = the current import. So rows inserted by an earlier run are counted as valid again, and no duplicate customer is created. Customers from other imports, or with a null `import_job_id`, count as existing.

## Crash recovery, timeout and shutdown

**Late acknowledgement and the visibility timeout (replaces heartbeats and the reaper).** With `task_acks_late`, a worker acknowledges the message only after the task returned. While the task runs, the message sits in the Redis hash `unacked` (and the sorted set `unacked_index`). If the worker is killed, the message is never acknowledged. After `VISIBILITY_TIMEOUT_SECONDS` since it was delivered, a worker that runs kombu's restore check puts it back on the queue `celery`, another worker (or the restarted one) receives it, and `process_csv` resumes from the checkpoint. `task_reject_on_worker_lost` covers the other case: only the pool process dies (for example killed for memory) while the main Celery process lives; the message is then put back at once.

**Why it is slower than before.** The hand-built reaper noticed a dead worker after 15 s (heartbeat TTL) plus a few seconds. Celery waits for the visibility timeout, which has to be longer than the longest task (here `JOB_TIMEOUT_SECONDS + 30` at least, default 900 s). On top of that, kombu does not check for expired messages continuously: it checks once per 10 poll cycles, and the poll cycle is 10 s, so one worker looks about every 100 s. With several workers the first check after the timeout comes sooner, but it can be tens of seconds late (see "Hand-built queue vs Celery" for the measured number).

**Duplicate delivery and the advisory lock.** If a task runs longer than the visibility timeout, or Redis hands a message to a second worker for another reason, the same import is run twice at the same time. The Postgres advisory lock in `process_csv` makes the second delivery log `import already running elsewhere` and return, which acknowledges it. The lock is per import (the first 8 bytes of the id), is held by a dedicated connection, and is explicitly unlocked in a `finally`.

**A crash counts as an attempt.** `attempts` is incremented by the start `UPDATE` at every start, including a start that later died with its worker. The crash-loop guard (step 4 of the task start) fails an import whose `attempts` exceeds `MAX_ATTEMPTS`. A task that kills its worker every time is therefore failed at its fourth start, with `gave up after 4 attempts (worker crashed or restarted repeatedly)`.

**Job timeout.** `soft_time_limit=JOB_TIMEOUT_SECONDS` on `process_csv`. When it expires, Celery raises `SoftTimeLimitExceeded` inside the task (a normal `Exception`), which goes through the retry path: `on_retry` sets `retrying` with `job timed out after <n> seconds` and the checkpoint is kept. No code in the handlers catches `Exception` (they catch `ValidationError` and `OSError` only), so nothing swallows it. **There is no hard `time_limit` on purpose:** a hard limit kills the pool process without running our hooks, which would leave the import in `running`. The price: a task that hangs and never returns (for example blocked forever in a system call) is never stopped by Celery. In prefork the soft limit is a signal, so a long blocking call ends the task only after it returns.

**Graceful shutdown.** `docker compose stop worker` sends SIGTERM; Celery does a warm shutdown: the running task finishes, no new task is taken, the process exits with code 0. `stop_grace_period: 30s` is how long Docker waits before SIGKILL, so a task that needs longer than 30 s after the stop is killed (exit 137) and recovered through the visibility timeout. An idle worker stops in about 3 s. A chained `send_email` published during the shutdown stays in the queue until a worker starts again. Celery prints one plain-text line at a warm shutdown (`worker: Warm shutdown (MainProcess)`), which is not JSON.

**Multiple workers.** `docker compose up -d --scale worker=3`. Each container is a Celery node (`celery@<container id>`). Redis gives each message to one consumer, with `worker_prefetch_multiplier=1` a worker holds only the message it runs. Checked: `scripts/check_no_double_claim` printed PASS twice with 200 tasks over three workers (67 / 66 / 67 each).

## Checkpoint and resume

**What the checkpoint is.** Four counters already on `import_jobs` (`total_rows`, `valid_rows`, `invalid_rows`, `last_processed_row`) plus `report_bytes`. After every batch they are updated and committed **in the same transaction as the bulk insert**, so the checkpoint always matches what is really in `customers`. A crash before the commit rolls the whole batch back, and the resume does that batch again.

**Resume or start from scratch.** At the start of every attempt (after the lock, the start `UPDATE` and the crash-loop guard) the task resumes only if ALL are true:
1. `last_processed_row > 0`.
2. `finished_at` is null. It is set only when a job ends as `completed`, `completed_with_errors`, `failed` or `cancelled`. **A finished job restarts from scratch** when `process_csv` runs for it again, because the checkpoint of a finished job describes the end of the file. A fresh start sets `finished_at` back to null. (`POST /imports/{id}/retry` clears `finished_at`, so a retried import resumes.)
3. The report file exists and its size is at least `report_bytes`.

If (1) and (2) hold but (3) does not, the task logs why (`the checkpoint cannot be trusted`) and falls back to a fresh start.

**Why `report_bytes` and the truncate.** In a batch the report is written and flushed **before** the commit, and its size is saved **in** the commit. If the worker dies between the flush and the commit, the batch is rolled back but its invalid rows are already in the report file. On resume `os.truncate(report_path, report_bytes)` cuts that part off, so no row appears twice.

**Why the seen-email set is rebuilt from the database.** The duplicate-in-file check uses the set of emails seen so far, and that set is lost when the process dies. On a resume it is rebuilt with one streamed query (`yield_per`) over the customers where `import_job_id` is this import.

**How rows are skipped.** The CSV is opened from the start. A row whose `reader.line_num` is less than or equal to `last_processed_row` is read and dropped: no validation, no database call, no counting.

**Log messages** (logger `process_csv`; the resume and fresh start lines have the fields `line` and `rows_done`, and `reason` when the checkpoint was not trusted): `starting import <id> from scratch`; `resuming import <id> from line <n> (<m> rows already processed)`; a warning `import <id>: the checkpoint cannot be trusted, ...`; `import <id>: processed up to line <n>` after every 20th batch.

**Known limits:**
1. Skipping still parses the already-processed part of the file (cheap compared with validation and database work).
2. The uploaded file must not change between attempts. This is not checked.
3. The seen-email set contains only emails of rows that were inserted. If an earlier row was invalid only because its email already existed from another import, a later duplicate of it is reported as `email: email already exists` instead of `email: duplicate email in file` after a resume. The counters are identical.
4. **The rebuilt seen-email set is only correct if every customer of this import was inserted before the checkpoint.** That holds for a normal retry. It does NOT hold when a fresh start happens for an import that already has customers (running `process_csv` again for a finished import, or the "cannot be trusted" fallback after some batches were inserted) and that fresh run is then interrupted and resumed: later rows are wrongly reported as `email: duplicate email in file`, and `valid_rows` and the report are wrong (the `customers` table itself stays correct). Running the import once more to the end repairs it. A real fix needs a decision (for example delete the import's own customers on a fresh start). Not done.

## Multiple imports at once and progress

**Why the batch is sorted before the insert (deadlocks).** One batch is one `INSERT` statement that takes a row lock for each new email, in the order of the `VALUES`. If import A inserted `x, y` and import B inserted `y, x` at the same moment, Postgres could see a deadlock and abort one of them. If both sort the batch by email, they ask for the common emails in the same order, so one of them simply waits for the other's commit. A batch is one statement and one commit.

**What `RETURNING email` is used for.** `ON CONFLICT DO NOTHING` skips a conflicting row silently. `RETURNING email` gives back only the emails that were really inserted by this statement. Every valid row that is not returned was skipped, and that is one of two kinds: (1) inserted by an earlier run of this same import (found with `WHERE email IN (skipped) AND import_job_id = <this import>`, stays valid); (2) belongs to another import (finished earlier, or inserted a moment ago in a parallel import): invalid with `email: email already exists`, added before the report is written so the report stays sorted by line. Exactly one import wins each email, so `valid_rows` summed over parallel imports equals the number of customers.

**Progress.** At the start of EVERY attempt, after deciding to resume or start from scratch, `process_csv` counts the data rows with a plain `csv.reader` pass (constant memory), saves `rows_in_file`, commits, and logs the number. It matches `total_rows` because `DictReader` skips the same empty rows. `progress_percent` is a Pydantic `computed_field` on `ImportJobResponse`. In this order: `rows_in_file` is null: `null`; status `completed` or `completed_with_errors`: `100.0`; `rows_in_file` is 0: `100.0`; otherwise `round(min(100, total_rows * 100 / rows_in_file), 1)`. A `cancelled` import shows the real percentage. `total_rows` is committed with every batch and keeps its value on a resume, so progress never goes backwards after a crash (checked: during the crash recovery test `total_rows` stayed at 121,000, then went on to 142,000).

**Known limits:**
1. The count pass reads the file one extra time per attempt.
2. Progress counts rows, not time.
3. `progress_percent` is calculated when the response is built and is not stored.
4. An import that waits for another import's open transaction on the same emails waits at most for one batch of the other import.
5. The email is still at-least-once.

## Cancel

**State changes.**

| Status when cancel arrives | Result | HTTP |
|---|---|---|
| `queued` or `retrying` | straight to `cancelled`, `finished_at` set, `error_message` `cancelled by user` | `200` |
| `running` | `cancelling` (a worker is running it and has to stop) | `202` |
| `cancelling` or `cancelled` | nothing changes (idempotent) | `200` |
| `completed`, `completed_with_errors`, `failed` | nothing changes | `409` `import already finished` |
| unknown id / invalid UUID | | `404` / `422` |

**The endpoint** (`cancel_import` in `app/routes/imports.py`) only uses conditional `UPDATE` statements, never "read the status, then write": (1) `... WHERE status IN ('queued','retrying')` to `cancelled`; (2) `... WHERE status = 'running'` to `cancelling`; (3) otherwise re-read the row to answer `200` or `409`; if a worker changed the status between our statements, steps 1 and 2 run once more, and a second miss returns `409` `import changed while cancelling, try again`. Cancel does not delete the uploaded file, the customers already inserted, or the error report. There is no rollback. **Cancel never uses Celery's revoke or terminate**: the queued message stays where it is and is skipped when it runs.

**Where a cancel is respected** (every status write by a task is conditional, because the API and several tasks write the same row):
- **Start of an attempt:** `WHERE status NOT IN ('cancelling','cancelled')`. Zero rows changed means cancelled: a `cancelling` import is set to `cancelled`, the task logs `skipping import <id>: cancelled` and returns (acknowledged, no email, `attempts` does not go up). This covers a message that waited in the queue while all workers were stopped, and a retry that was waiting for its countdown.
- **After every batch commit:** a fresh `SELECT status`. If it is `cancelling`, the loop stops. Then the report is finished like a normal ending and `UPDATE ... SET status='cancelled', finished_at=now(), error_message='cancelled by user' WHERE status IN ('running','cancelling')`. It logs `import <id> cancelled at line <n> after <m> rows` and an `import <id> finished` line with `status` `cancelled`. The counters and `last_processed_row` stay as of the last committed batch.
- **End of the task:** the normal ending is `WHERE status = 'running'`. If it changed 0 rows, a cancel arrived before it and the import is treated as cancelled.
- **`ImportTask` hooks:** `cancelled` is left alone, `cancelling` becomes `cancelled`.

**Why cooperative, and why only after a batch commit.** A task is one process in the middle of a database transaction; killing it would lose the batch it is working on. Looking at the status only after a commit means the checkpoint (counters, `last_processed_row`, `report_bytes`) matches what is really in `customers` and in the report.

**No email** for a cancelled import. `email_status` stays `pending`. To import the file again, upload it again (the new import marks the emails the cancelled one inserted as `email: email already exists`; the customers have `import_job_id`, so they are easy to find).

**Known limits:**
1. A cancel stops the import only at the next batch boundary.
2. A cancelled task's message may stay in the queue (or in a worker's memory, waiting for its retry time) until it runs and is skipped.
3. Cancel does not remove the customers already inserted.
4. Cancel cannot stop a `send_email` task already queued, because a finished import cannot be cancelled.
5. The uploaded file stays on disk.

## Observability

### Log format

Every log line (worker and api) is ONE JSON object, written to stdout by one handler on the root logger (`setup_logging` in `app/logging_config.py`). Fixed fields, in this order: `ts` (UTC, ISO 8601), `level`, `service` (`worker` or `api`), `worker_id` (workers only: the Celery node name), `logger`, `message`. After them come all extra fields of the record (from `extra=` or from `job_context`). A traceback goes into the field `exception` as one string. `LOG_LEVEL` (default `INFO`) sets the level. Uvicorn's lines go through the same handler. Celery's own lines (`celery.worker.strategy`, `celery.app.trace`, ...) also go through it; Celery adds a `data` field to some of them.

Example (the summary line at the end of `process_csv`):

```json
{"ts": "2026-10-17T07:46:54.832062+00:00", "level": "INFO", "service": "worker", "worker_id": "celery@f08b80482687", "logger": "process_csv", "message": "import d3d6c02f-05a4-416a-82cb-8acf70183c95 finished", "status": "completed_with_errors", "total_rows": 2000, "valid_rows": 1800, "invalid_rows": 200, "duration_seconds": 0.7, "rows_per_second": 2696, "peak_rss_mb": 72.8, "job_id": "58cf2e41-cf90-4737-bea6-e384acf511f9", "job_type": "process_csv", "import_job_id": "d3d6c02f-05a4-416a-82cb-8acf70183c95"}
```

Fields on the log lines (message texts are unchanged from 6.3 except where noted):

| Line | Fields |
|---|---|
| `resuming import ...` / `starting import ... from scratch` | `line`, `rows_done` (0 and 0 for a fresh start), and `reason` when a checkpoint was not trusted |
| `import ...: processed up to line ...` (every 20th batch) | `line`, `total_rows`, `peak_rss_mb` |
| `import ... cancelled at line ...` | `line`, `total_rows` |
| `import ... finished` (after the final status is committed and before `send_email.delay`; also at the cancel ending with `status` `cancelled`) | `status`, `total_rows`, `valid_rows`, `invalid_rows`, `duration_seconds` (this attempt), `rows_per_second` (rows handled in this attempt), `peak_rss_mb` |
| `import already running elsewhere` (7.1) | job fields only |
| `job retry` (7.1, replaces `Retrying job ...`) | `attempt`, `max_attempts`, `error` |
| `job failed` (7.1, replaces `Job ... is dead ...`) | `attempt`, `error` |
| `import ...: gave up after <n> attempts ...` (7.1) | `attempt` |
| `import uploaded` (API, after the enqueue) | `import_job_id`, `job_id` (the Celery task id) |
| `cancel requested` (API) | `import_job_id`, `result` |
| `import retried`, `email resend requested` (API, 7.1) | `import_job_id` |

`peak_rss_mb` is `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024` rounded to 1 decimal: the highest memory the task's process (the pool process) has used so far.

### How `job_context` puts the job fields on every line

`job_context` is a `ContextVar` holding a dict. The `task_prerun` signal handler sets it to `{"job_id": <Celery task id>, "job_type": <task name>, "import_job_id": <first argument>}` (`import_job_id` for `process_csv` and `send_email`), and `task_postrun` resets it to an empty dict. `JobContextFilter` is attached to the handler and copies each key of the context onto the record unless the record already has that attribute (a value passed with `extra=` wins). A task just calls `logger.info(...)` and its lines come out with the job fields. The `ImportTask` hooks run before `task_postrun`, so `job retry` and `job failed` carry them too. **Lines written by Celery itself** (`Task ... received` in the main worker process, the startup lines) are outside a task and have no job fields; the `Task ... succeeded` line is written inside the context and does. **The `data` field of Celery's lines holds the task id and arguments**, so `grep <import id>` also finds the `received` line.

### Tracing one import

`docker compose logs --no-log-prefix api worker | grep <import id>` shows the API line `import uploaded` (it has the Celery task id as `job_id`), the Celery `received` line, the lines of the `process_csv` task (all with `job_id` and `job_type`), retry lines, and the lines of the chained `send_email` task (they mention the import id in the message). API access lines for that id also match and have no `job_id`. Grepping the `job_id` from the `import uploaded` line gives the task's own lines, including every retry (a retry keeps the task id).

### Stats

`GET /stats` (`app/routes/admin.py`), built step by step; Redis, Celery and Postgres are read one after the other, not in one snapshot.

```
{
  "queue": {"waiting": <LLEN celery>, "in_flight": <HLEN unacked>, "active": <tasks running right now>, "scheduled": <tasks waiting for a retry time>},
  "workers": {"alive": <n>, "names": [<sorted node names>]},
  "imports": {"total": ..., "by_status": {<all eight statuses, 0 when none>}},
  "emails": {"failed": <count of email_status 'failed'>},
  "durations": {"finished_imports": ..., "avg_run_seconds": ..., "avg_total_seconds": ...}
}
```

| Field | Where it comes from |
|---|---|
| `queue.waiting` | `LLEN celery` (messages nobody has taken yet) |
| `queue.in_flight` | `HLEN unacked` (messages a worker took and has not acknowledged: running tasks, tasks reserved by a worker, and retry messages waiting for their time) |
| `queue.active`, `queue.scheduled` | Celery `inspect().active()` and `.scheduled()`: tasks running now, and tasks held by a worker until their ETA (retries) |
| `workers.alive`, `workers.names` | the keys of `inspect(timeout=1.0).ping()`. No answer (no workers) means 0 and an empty list |
| `imports.by_status`, `imports.total` | one `SELECT status, count(*) ... GROUP BY status`; all eight statuses are present |
| `emails.failed` | count of imports with `email_status = 'failed'` |
| `durations.*` | one query over the imports with status `completed` or `completed_with_errors`: their count, the average of `finished_at - started_at` and of `finished_at - created_at` (2 decimals, `null` if none) |

The ping waits up to one second for answers. The two later calls name the workers that answered (`destination=`), so Celery stops waiting as soon as all of them answered; the whole call takes about one second. `queue.waiting` and `in_flight` come from a Redis client created in `admin.py`.

Checked: idle, three workers: `alive` 3, `waiting` 0, `in_flight` 0, `active` 0, `scheduled` 0 (the call took about 1 s); with three 300,000-row imports running: `in_flight` 3, `active` 3, `running` 3, `waiting` 0; with the workers stopped and two files uploaded: `alive` 0, `waiting` 2, `queued` 2.

### Known limits of the observability

1. The stats read Redis, the workers and Postgres at slightly different moments, so the numbers can disagree for a moment.
2. `avg_run_seconds` uses `started_at`, which every attempt resets, so a retried import shows only its last attempt.
3. There are no durations for `send_email` tasks.
4. `peak_rss_mb` is the peak of the whole pool process lifetime, so restart the worker before a clean measurement.
5. Celery's own log lines have no job fields outside a task.

## Hand-built queue vs Celery

What changed in 7.1, concept by concept. "Hand-built" is the queue of Milestones 2.1 to 6.3 (now deleted).

| Concept | Hand-built | Celery |
|---|---|---|
| Enqueue | `LPUSH queue <json>` with `job_id`, `job_type`, `payload`, `attempts` | `process_csv.delay(id)`: kombu publishes a message to the Redis list `celery`. The task id is the `job_id` |
| Claiming and acknowledgement | `BLMOVE queue processing:<worker_id>`, `LREM` after success | The worker prefetches (1 message, `worker_prefetch_multiplier=1`) and, with `acks_late`, acknowledges after the task. Until then the message is in the Redis hash `unacked` |
| Retries and backoff | The worker built a new message with `attempts + 1` and put it in a sorted set; delay `min(max, base * 2^n)`, half to full jitter | `autoretry_for`, `retry_backoff`, `retry_backoff_max`, `retry_jitter`: delay `min(max, base * 2^retries)`, then a random value between 0 and that (full jitter) |
| Delayed jobs | Sorted set `delayed`, a Lua script moved due jobs to `queue` | A message with an ETA. The **worker holds it in memory** until the time comes (it stays unacknowledged in Redis meanwhile) |
| Dead letter queue | List `dead`, `GET /dead-jobs`, replay endpoint | **No equivalent.** The `failed` status plus `POST /imports/{id}/retry` |
| Heartbeat and reaper | Heartbeat key with TTL per worker, every worker ran a reaper that moved the lists of dead workers | The **visibility timeout**: an unacknowledged message is redelivered after `VISIBILITY_TIMEOUT_SECONDS`. No heartbeat, no reaper |
| Job timeout | `signal.alarm` (SIGALRM) raising `JobTimeoutError` | `soft_time_limit` raising `SoftTimeLimitExceeded`. No hard `time_limit` |
| Graceful shutdown | SIGTERM set a flag, the loop finished the job | Celery's warm shutdown (SIGTERM waits for running tasks); `stop_grace_period: 30s` kept |
| Per-worker lists | `processing:<worker_id>`, so a dead worker's jobs were known | One shared hash `unacked` (delivery tag to message), plus `unacked_index` |
| Atomic moves | Lua scripts (promote, recover, replay) and `MULTI/EXEC` | Done inside kombu. We do not see them and we do not write any |
| Worker identity | hostname plus 8 random hex characters, new on every start | The Celery node name `celery@<hostname>` (inside Docker the container id; a restarted container keeps it) |
| Stats | `LLEN`/`ZCARD`/`SCAN` over our own keys, heartbeat keys for workers | `LLEN celery`, `HLEN unacked` and Celery `inspect` (ping, active, scheduled) |
| Duplicate protection | A job was in exactly one list, so a double run could only follow a reaper mistake | The Postgres advisory lock per import, because Redis can deliver a message twice |
| Crashes counted as attempts | The reaper added `attempts + 1` to the message | Celery does not; `import_jobs.attempts` is incremented at every start and checked by the crash-loop guard |

### Gotchas seen while building it

1. **Recovery speed.** A crashed task is redelivered after the visibility timeout (900 s by default), not after 15 s as with the heartbeat. Smaller timeouts are not an option for long imports: the timeout must be longer than the longest task, otherwise a running task is delivered twice (the startup check enforces `JOB_TIMEOUT_SECONDS + 30`).
2. **Duplicate delivery.** Redis redelivery is at-least-once by design. A task that outlives the visibility timeout, or a restore race, can start the same import on a second worker. That is why `process_csv` takes a Postgres advisory lock: the second run logs `import already running elsewhere` and returns. Without it, two workers would write the same report file.
3. **No hard time limit.** A hard `time_limit` kills the pool process and Celery does not call our `on_failure` hook, so the import would stay in `running`. We use only the soft limit. The price is that a hung task is never stopped.
4. **Crashes are not retries.** Celery's `max_retries` counts only the `retry()` calls a task makes. A task that keeps killing its worker is redelivered forever. `import_jobs.attempts` plus the crash-loop guard stop it.
5. **Retry delays are held by the worker.** A retry message with an ETA is held in the memory of the worker that received it. If that worker dies, the message is recovered only by the visibility timeout. `inspect().scheduled()` is how to see them (`queue.scheduled` in `/stats`).
6. **Full jitter can be 0.** `retry_jitter=True` picks a random second between 0 and the countdown, so a retry can come back at once.
7. **Output that is not logging.** Celery prints a banner and a root-user warning at startup (turned off with `--quiet` and a warnings filter) and `worker: Warm shutdown (MainProcess)` at shutdown (not turned off).
8. **The shell wins over `.env`.** A variable exported in the shell overrides `.env` for `docker compose` substitution. While testing with `JOB_TIMEOUT_SECONDS` and `MAX_ATTEMPTS` changed in `.env`, an exported old value silently kept the old setting.
9. **Hooks get the original error.** `on_retry` receives the original exception (not the `Retry` wrapper); a first version that looked at `exc.exc` raised an error inside the hook and left a wrong `error_message`.

### Measured numbers (7.1, one machine, Docker, 7.2 GiB)

**Crash recovery** (`JOB_TIMEOUT_SECONDS=120`, `VISIBILITY_TIMEOUT_SECONDS=160`, `MAX_ATTEMPTS=3`, three workers, 300,000-row file): `docker kill` of the worker container at 40.3% (121,000 rows done, 28.8 s after the task started). Another worker logged `resuming import ... from line 121001` **155.7 s after the kill, which is 184.5 s after the message was delivered**. The nominal timeout is 160 s; the extra 24.5 s is the interval of kombu's check (it looks once per 10 poll cycles of 10 s, so about every 100 s per worker). The import ended `completed_with_errors` with 300,000 / 270,000 / 30,000 rows, 270,000 distinct customers, a report with 30,000 data rows and no repeated row number, exactly one email (the import finished 44 s after the resume, 200.1 s after the kill). With a single worker the wait after the timeout can be up to about 100 s (from reading kombu's code; only the three-worker case was measured).

**500,000 rows** (one worker restarted first, `TRUNCATE customers`, `--invalid-percent 10 --email-prefix big --seed 77`, a 32,198,927-byte file):

| Measure | 7.1 (Celery) | 6.3 (hand-built, for comparison) |
|---|---|---|
| Result | `completed_with_errors`, 500,000 / 450,000 / 50,000, 450,000 distinct customers, one email | the same |
| Duration | 125.0 s | 138.8 s |
| Rows per second | 3,999 | 3,603 |
| `peak_rss_mb` at the first progress line (line 20001) | 79.8 | 74.6 |
| `peak_rss_mb` at the last progress line (line 500001, 25 progress lines) | 122.2 (42.4 MB above the first; limit 150) | 117.3 |
| Final `peak_rss_mb` | 122.2 (limit 400) | 117.3 |
| Worker container (`docker stats`, main process plus pool process) | 70.8 MiB before, 113 to 151 MiB while running, 137 MiB at the end | 46.3 MiB before, 97 MiB at 2 minutes |
| API container | 77.1 MiB before; 78.8 to 79.3 MiB during a 5.2 s upload at 6 MB/s (+2.3 MB; limit 50) | 71.2 MiB before, +3.8 MB |

The memory growth is the seen-email set (about 85 to 95 bytes per distinct email), as before; the pool process (not the main Celery process) runs the task, so `peak_rss_mb` measures the right process. The container is bigger than before because the Celery main process is also in it.

**Other results:**
- Normal upload, 10,000 rows: `completed_with_errors`, 9,000 valid, 1,000 invalid, report with 1,000 rows, one email.
- `always_fail`: 3 runs, waits `Retry in 1s` and `Retry in 4s`, then `raised unexpected`. `flaky_job --fail-times 2`: succeeded on call 3.
- Import with the file removed: status `retrying` after attempts 1 and 2, `failed` after attempt 3 with `attempts` 3 and `[Errno 2] No such file or directory`; `job retry` x2 and `job failed` x1 in the logs, each on a different worker. After putting the file back, `POST /imports/{id}/retry` returned `202`, the import completed; on a non-failed import it returned `409`.
- Timeout and resume (`JOB_TIMEOUT_SECONDS=15`, `VISIBILITY_TIMEOUT_SECONDS=60`, `MAX_ATTEMPTS=10`, 300,000 rows): 5 timeouts, 6 attempts, resumes from lines 63001, 122001, 178001, 235001 and 292001, progress rose to 100.0 without a drop, exact counts, 270,000 distinct customers, no repeated report row, one email. (With `MAX_ATTEMPTS` still 3 the import correctly failed after the third timeout.)
- Crash-loop guard: a finished import with `attempts` set to 3 and `process_csv --keep-attempts` ended `failed` with `gave up after 4 attempts (worker crashed or restarted repeatedly)`, and the counters did not change.
- Duplicate delivery: `--times 2 --keep-attempts`: one run processed it, the other logged `import already running elsewhere`; 270,000 / 30,000, 270,000 customers, still one email (the `already sent` check).
- Cancel: a running import cancelled at 30.3% returned `202`, ended `cancelled` at line 91001 (customers equal to `valid_rows`, 81,955, unchanged afterwards; report lines equal to `invalid_rows` + header; no email). With the workers stopped, a cancel returned `200` at once and the task was skipped later (`attempts` 0). A cancel during a retry wait returned `200` and the retry was skipped. Calling the failure method directly: `cancelled` stays, `cancelling` becomes `cancelled`, `running` becomes `retrying` / `failed`.
- Parallel: `check_no_double_claim` PASS twice (200 tasks, 67 / 66 / 67 per worker). Three 300,000-row imports with different `--email-prefix` and `--seed` at once: each 300,000 / 270,000 / 30,000, 810,000 distinct customers, progress never dropped. The same 100,000-row file uploaded twice at once, three times: `valid_rows` summed to 90,000 each time (897 / 89,103, 1,815 / 88,185, 2,726 / 87,274), 90,000 distinct customers, no `deadlock` in the logs.
- Email: with Mailpit stopped, `email_status` ended `failed` (`Temporary failure in name resolution`); after starting Mailpit, `resend-email` returned `202` and `email_status` became `sent`; a second resend returned `409`.
- Redis stopped: `POST /imports` returned `503` in 33 ms and the import was `failed` with `Could not add the job to the queue (Redis is unavailable)`.
- Shutdown: `docker compose stop worker` at 26.7% of a 60,000-row import: the task finished (`completed_with_errors`, 54,000 / 6,000), the container exited with code 0 after 14 s; its `send_email` stayed in the queue and was sent when the workers started again. Three idle workers stopped in 3 s, all exit code 0.
- Logs: all `api` and `worker` lines are valid JSON except the empty line and `worker: Warm shutdown (MainProcess)` Celery prints at shutdown.

## Key decisions

- **One upload function.** `save_upload` does the extension check, chunked write, size check, empty check and header check. It raises `HTTPException` directly and deletes the file on any rejection.
- **Row after file.** The `import_jobs` row is created only after the file passes all checks.
- **Size limit.** The upload is copied in 1 MB chunks. When the count passes `MAX_UPLOAD_SIZE_MB`, the partial file is deleted and the API returns 413.
- **Header check.** Only the first row is read, with `utf-8-sig`. Column names are trimmed and matched exactly (case-sensitive).
- **Migrations.** Written by hand. Run manually with `docker compose exec api alembic upgrade head`, not on container start. 7.1 needed none.
- **Ports.** api 8000, redis 6379, mailpit web 8025 and SMTP 1025. Postgres is not published.
- **Uploads and reports volumes** are mounted at `/app/uploads` and `/app/reports` in both api and worker. Paths stored in the database are relative.
- **Email domains.** `email-validator` rejects reserved domains such as `.test` and `.invalid` (use `.com` or `.org` in test data).
- **Worker and API share one image.** The worker service only overrides the command.
- **Short DB sessions.** Tasks and hooks open a new session for each short job instead of holding one across work. The one long-lived connection in `process_csv` is the advisory lock connection.
- **Task arguments are plain JSON** (an import id string). Nothing else is passed; a task loads what it needs from Postgres.
- **Enqueue failure.** `POST /imports` and `POST /imports/{id}/retry` catch `kombu.exceptions.OperationalError` and `redis.exceptions.RedisError`; the uploaded file stays on disk. `process_csv` catches `OperationalError` when it publishes the email.
- **No Celery extras.** No Flower, no Beat, no result backend, no canvas, no revoke or terminate, no custom base class except `ImportTask`.
- **Deviations from the 7.1 prompt, for the record:** `--quiet` was added to the worker command and a `SecurityWarning` filter to `celery_app.py` (otherwise every worker prints about 30 non-JSON lines at startup); a `celeryd_init` handler stores the node name for the logging handler; `/stats` makes a second `inspect` call that names the workers that answered, so it finishes in about one second instead of three.

## How to run

```
cp .env.example .env
docker compose up --build                 # run everything
docker compose exec api alembic upgrade head   # create the tables (first run, in another terminal)
docker compose logs -f api
docker compose logs -f worker
docker compose logs --no-log-prefix worker | grep <import id>   # follow one import (every line is JSON)
docker compose up -d --scale worker=3     # run 3 workers; --scale worker=1 goes back to one
docker compose exec worker celery -A app.celery_app inspect ping   # one reply per worker
docker compose exec worker celery -A app.celery_app inspect active  # what each worker runs
curl localhost:8000/stats
curl -X POST localhost:8000/imports/<id>/retry          # a failed import
curl -X POST localhost:8000/imports/<id>/resend-email   # a failed email
docker compose down                       # stop (add -v to also delete the database)
```

- API docs: http://localhost:8000/docs
- Mailpit inbox: http://localhost:8025
- Test tasks (inside the api container): `docker compose exec api python -m scripts.enqueue_test_job always_fail`, `... flaky_job --name x1 --fail-times 2`, `... process_csv --import-job-id <id> [--times 2 --keep-attempts]`, `docker compose exec api python -m scripts.check_no_double_claim --jobs 200`
- Look at the queue: `docker compose exec redis redis-cli LLEN celery` (waiting), `HLEN unacked` (taken, not acknowledged)
- After changing `.env`, recreate the workers: `docker compose up -d --scale worker=3`. Do not export these settings in your shell, the shell overrides `.env`.
- Check that every log line is JSON: `docker compose logs --no-log-prefix worker api | python3 -c "import sys,json; [json.loads(l) for l in sys.stdin if l.strip()]"` (the `Warm shutdown` line of a stopped worker will fail it, see Known limits)

## Known limits

1. **Recovery waits for the visibility timeout.** A task whose worker died is redelivered after `VISIBILITY_TIMEOUT_SECONDS` plus the interval of kombu's check: measured 184.5 s for a 160 s timeout with three workers; with one worker up to about 100 s extra (the check runs once per 10 poll cycles of 10 s). With the default 900 s, a crashed import waits 15 to 17 minutes.
2. **A task running longer than the visibility timeout can be delivered twice.** The advisory lock makes the second delivery skip it. (The startup check keeps `JOB_TIMEOUT_SECONDS` below the visibility timeout, but a hung task is not stopped.)
3. **A hung task that never returns holds its lock** until its worker restarts (the connection closes then).
4. **No hard time limit**, so Celery never kills a hung task. The soft limit only works when the task returns to Python code.
5. **Retry messages are held in the worker's memory** until their time. If that worker dies, they are recovered only by the visibility timeout. A cancel of an import in that state is still skipped when the retry runs.
6. **Inspect-based stats** (`workers`, `queue.active`, `queue.scheduled`) take up to one second and show nothing when no worker answers (a busy worker may also answer late; none did in testing).
7. **No authentication** on the admin endpoints (`/stats`, retry, resend-email) or any other endpoint.
8. **Email is at-least-once**, and an email whose attempts are used up stays `failed` until `resend-email` is called.
9. **Celery's own log lines have no job fields** outside a task (for example `Task ... received`), and Celery prints one plain-text line (`worker: Warm shutdown (MainProcess)`) at a warm shutdown.
10. **There is no list of failed imports**: `GET /imports` has no status filter, so finding failed imports means listing all of them (or `GET /stats` for the count).
11. The error handling around the advisory lock is minimal: if the database goes away between taking the lock and the first query, the dedicated connection is left to the garbage collector (Postgres has released the lock by then).
12. The limits of earlier milestones that still apply are listed in the sections above (CSV processing, checkpoint and resume, multiple imports and progress, cancel, observability).

## What comes next

**All planned milestones are done.**
