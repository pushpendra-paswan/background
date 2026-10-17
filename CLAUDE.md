# CLAUDE.md

## Project

**CSV Customer Import: Background Job Processing System** (learning project).

An API accepts a CSV file of customer records. A background worker then:

1. Reads the CSV file.
2. Validates each record.
3. Inserts valid records into the database.
4. Generates a report of invalid records.
5. Sends an email when processing finishes.

Jobs run as **Celery tasks**, with Redis as the broker. (The project started with a queue hand-built on Redis to learn how job systems work. Milestone 7.1 replaced it with Celery.)

## Tech Stack

- Python 3.12
- FastAPI (API)
- Pydantic v2 (row validation and API schemas)
- PostgreSQL (customers and job tracking)
- SQLAlchemy 2.0 (synchronous) + Alembic (migrations)
- Celery (`celery[redis]==5.6.3`) as the job queue, with Redis as the broker (no result backend)
- The `redis` Python client, for queue sizes in `/stats` and for the test scripts
- Python stdlib `csv` for reading and writing CSV (no pandas)
- `smtplib` + Mailpit (local fake SMTP server with a web inbox)
- Local disk for uploaded files and error reports
- Faker for test data generation
- pytest for tests
- Docker Compose to run `api`, `worker`, `postgres`, `redis`, `mailpit`

## Do NOT use

- RQ, Dramatiq, Arq or any other job/queue library besides Celery
- RabbitMQ, Flower, Celery Beat, a Celery result backend, Celery canvas (chain/group/chord)
- pandas
- S3 or any object storage
- A frontend. This is an API-only project.
- Kubernetes, microservices, WebSockets, or deployment setup
- Anything not listed in the tech stack above without asking first

## Coding Style (important)

Keep the code simple and plain, the way a beginner would read and follow it.

- **Plain sequential code.** A function reads top to bottom, step by step.
- **A short comment above each step** saying what it does.
- **No unnecessary helper functions.** If something is used once, write it inline.
- **No helper calling another helper.** No chains of tiny functions.
- **No unnecessary abstractions:** no base classes, factories, decorators, or design patterns unless there is a real need.
- **No unnecessary splitting of code across files.** Do not create a new file without a reason. Related code stays together.
- **Basic project structure only:** routes, services, models, schemas. Add a repository layer only if I ask for it.
- **Readable names.** Prefer clear, longer names over short clever ones.
- **No extra features.** Build only what the current milestone asks for.
- **No over-engineering.** No premature optimization, no speculative config options.
- **Type hints on function signatures.** No complex generics.
- **Keep error handling simple.** Catch specific errors where a failure is expected (bad row, SMTP down). Do not wrap everything in try/except.
- **No unnecessary tests.** Add tests only when the milestone says so (for example, retry and crash recovery behavior).

## Project Structure

```
.
├── CLAUDE.md
├── PROJECT_CONTEXT.md
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── alembic/
├── alembic.ini
├── app/
│   ├── main.py            # FastAPI app
│   ├── config.py          # settings from env vars
│   ├── database.py        # engine and session
│   ├── logging_config.py  # JSON log formatter, job_context, setup_logging
│   ├── models.py          # SQLAlchemy models
│   ├── schemas.py         # Pydantic schemas
│   ├── celery_app.py      # Celery app, ImportTask failure hooks, logging signals
│   ├── routes/            # API endpoints (imports.py, admin.py: /stats, retry, resend-email)
│   ├── services/          # business logic used by routes
│   └── handlers/          # Celery tasks (process_csv, send_email, test_handlers)
├── scripts/               # test CSV generator and similar
├── uploads/               # uploaded CSV files
├── reports/               # generated error report CSVs
└── tests/
```

Treat this as a starting layout. Do not add folders or files that no milestone needs yet.

## Domain

- **customers**: id, name, email (unique), phone, date_of_birth, country, created_at
- **import_jobs**: id, filename, file_path, status, total_rows, valid_rows, invalid_rows, last_processed_row, error_report_path, attempts, error_message, created_at, started_at, finished_at
- **Job statuses:** `queued`, `running`, `retrying`, `cancelling`, `cancelled`, `completed`, `completed_with_errors`, `failed`
- Use UUID primary keys.
- **Invalid rows are not job failures.** A job fails only for system problems (corrupt file, database outage, and so on).

## Key Design Rules

- `POST /imports` saves the file, creates the `import_jobs` row, enqueues the job, and returns `202` with the `job_id` immediately. It never processes the file itself.
- Workers are separate processes from the API.
- **Late acknowledgement:** tasks are acknowledged after they run (`task_acks_late`), so a task whose worker dies is delivered again.
- **At-least-once delivery.** A task can be delivered more than once, so tasks must be **idempotent**. Customer inserts use `INSERT ... ON CONFLICT (email) DO NOTHING`.
- **One run per import:** `process_csv` holds a Postgres advisory lock per import. A duplicate delivery logs `import already running elsewhere` and returns.
- **Retries** use Celery autoretry with backoff and jitter (`MAX_ATTEMPTS`, `RETRY_BASE_SECONDS`, `RETRY_MAX_SECONDS`). The job timeout is a soft time limit; there is no hard time limit.
- **The visibility timeout must be more than the job timeout plus 30 seconds** (`VISIBILITY_TIMEOUT_SECONDS` > `JOB_TIMEOUT_SECONDS` + 30). `celery_app.py` refuses to start otherwise.
- **A crash counts as an attempt** through `import_jobs.attempts` (incremented at every start). Celery does not count a crash as a retry, so `process_csv` gives up when `attempts` is greater than `MAX_ATTEMPTS`.
- **Stream the CSV** with the `csv` module and process in batches of 1,000 rows. Never load a whole file into memory.
- **Checkpoint and resume:** `process_csv` saves a checkpoint after every batch (counters, `last_processed_row` and the report size, in the same transaction as the insert) and resumes from it on retry.
- The email is a **separate job** chained after processing, so an SMTP failure never causes the CSV to be reprocessed.
- Workers run in parallel (`docker compose up --scale worker=3`), so tasks must be safe when several of them run at the same time.
- **Logging:** log only with the `logging` module, never `print`; one JSON object per line; every log line about a job carries `job_id`; keep messages short and put ids and numbers in fields (`extra=`).
- **Cancel is cooperative:** a running import stops at the next batch boundary, and no code may overwrite a cancel; all status changes made by workers (and by the `ImportTask` retry and failure hooks) are conditional updates or row-locked checks. Cancel never uses Celery revoke or terminate.

## API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /imports` | Upload CSV, return `202` with `job_id` |
| `GET /imports/{id}` | Status, progress, counts |
| `GET /imports` | List past imports |
| `GET /imports/{id}/errors` | Download the invalid-records report |
| `POST /imports/{id}/cancel` | Cancel an import (`200` cancelled, `202` cancelling, `409` already finished) |
| `POST /imports/{id}/retry` | Retry a `failed` import; it resumes from its checkpoint (`202`, `404` unknown, `409` not failed, `503` queue down) |
| `POST /imports/{id}/resend-email` | Send the email again when `email_status` is `failed` (`202`, `404` unknown, `409` not in a failed state) |
| `GET /stats` | What the system is doing now: queue sizes (waiting, in flight, active, scheduled), workers, imports by status, failed emails, average durations |

## Phases

1. **Foundation and Upload API:** Docker Compose, FastAPI, Alembic, tables, upload endpoint, status endpoints.
2. **Redis Queue and Worker:** enqueue, worker, claiming and acknowledgement (built by hand first, replaced by Celery in phase 7).
3. **CSV Processing Pipeline:** streaming, validation, bulk insert, error report, counters.
4. **Email Notification Job:** chained `send_email` job via Mailpit.
5. **Reliability:** retries with backoff and jitter, job timeout, checkpoint and resume, crash recovery, graceful shutdown (hand-built dead letter queue, heartbeats and reaper were replaced in phase 7).
6. **Scale and Visibility:** multiple workers, progress, cancel, stats endpoint, structured logging.
7. **Celery migration:** the hand-built queue is replaced by Celery (Milestone 7.1). Failed imports are retried with `POST /imports/{id}/retry`.

## Workflow

- I give you **one milestone prompt at a time**. Do only that milestone.
- At the start of each milestone, read `CLAUDE.md` and `PROJECT_CONTEXT.md`.
- Do not wait for my approval before writing code. Briefly state your plan (files you will create or change), then start building right away.
- After finishing each milestone, **update `PROJECT_CONTEXT.md`** with what was built, the files created or changed, decisions made, and what comes next.
- If something is unclear or the milestone conflicts with this file, ask instead of guessing.
- Run the app through Docker Compose. Configuration comes from environment variables, with a `.env.example` file.

## Commands

```
docker compose up --build        # run everything
docker compose exec api alembic upgrade head
docker compose up -d --scale worker=3   # run 3 workers in parallel
docker compose logs -f worker
docker compose logs --no-log-prefix worker | grep <import id>   # follow one import through the JSON logs
docker compose exec worker celery -A app.celery_app inspect ping   # which workers answer
pytest
```

Mailpit inbox: http://localhost:8025