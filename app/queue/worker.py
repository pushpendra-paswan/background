import json
import logging
import random
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone

import redis

from app.config import settings
from app.database import SessionLocal
from app.handlers import HANDLERS
from app.models import ImportJob
from app.queue.redis_queue import (
    DEAD_KEY,
    DELAYED_KEY,
    PROCESSING_KEY_PREFIX,
    QUEUE_KEY,
    WORKER_KEY_PREFIX,
    promote_due_jobs,
    recover_job_script,
    redis_client,
)

logger = logging.getLogger("worker")

# Set to True by the SIGTERM / SIGINT handler. The worker loop stops when it is True
shutdown_requested = False


class JobTimeoutError(Exception):
    # Raised inside a running handler when it takes longer than JOB_TIMEOUT_SECONDS
    pass


def raise_job_timeout(signal_number: int, frame) -> None:
    # Called by Python when the SIGALRM timer fires. The error comes out of whatever line the handler is running
    raise JobTimeoutError(f"job timed out after {settings.job_timeout_seconds} seconds")


def request_shutdown(signal_number: int, frame) -> None:
    # Called by Python on SIGTERM / SIGINT. Only set the flag: the worker loop does the stopping
    global shutdown_requested
    shutdown_requested = True
    logger.info("shutdown requested")


def send_heartbeats(worker_id: str, stop_event: threading.Event) -> None:
    # Runs in a background thread. Refresh the heartbeat key until the main thread sets stop_event.
    # stop_event.wait() sleeps for the interval, but wakes up at once when the event is set
    while not stop_event.wait(settings.heartbeat_interval_seconds):
        try:
            redis_client.set(WORKER_KEY_PREFIX + worker_id, time.time(), ex=settings.heartbeat_ttl_seconds)
        except redis.exceptions.RedisError:
            # Keep the thread alive. The next beat will try again
            logger.exception("Could not refresh the heartbeat")


def reap_dead_workers() -> None:
    # Find every per-worker processing list. SCAN is used instead of KEYS because it does not block Redis
    for processing_key in redis_client.scan_iter(match=PROCESSING_KEY_PREFIX + "*"):
        dead_worker_id = processing_key[len(PROCESSING_KEY_PREFIX):]

        # If the heartbeat key still exists, that worker is alive: leave its jobs alone
        if redis_client.exists(WORKER_KEY_PREFIX + dead_worker_id):
            continue

        # The heartbeat expired, so the worker is dead. Handle every job it left behind
        for message in redis_client.lrange(processing_key, 0, -1):
            job = json.loads(message)

            # A crash counts as a failed attempt, so a job that kills its worker cannot loop forever
            new_job = {
                **job,
                "attempts": job["attempts"] + 1,
                "last_error": f"worker died while running this job (worker {dead_worker_id})",
            }

            # Retries left: back to "queue" (no backoff). Otherwise: to "dead"
            if new_job["attempts"] < job["max_attempts"]:
                destination_key = QUEUE_KEY
                new_status = "retrying"
            else:
                destination_key = DEAD_KEY
                new_job["failed_at"] = datetime.now(timezone.utc).isoformat()
                new_status = "failed"

            # Move the job in one atomic Lua script. It returns 0 if another worker already moved it
            moved = recover_job_script(
                keys=[processing_key, destination_key], args=[message, json.dumps(new_job)]
            )
            if moved != 1:
                continue
            logger.warning(
                "Recovered job %s (%s) from dead worker %s, moved to %r (attempt %s of %s)",
                job["job_id"], job["job_type"], dead_worker_id, destination_key, new_job["attempts"], job["max_attempts"],
            )

            # Record it on the import (process_csv only). The job is already safe in Redis,
            # so if this update fails, log it and carry on
            if job["job_type"] == "process_csv":
                try:
                    with SessionLocal() as db:
                        import_job = db.get(ImportJob, uuid.UUID(job["payload"]["import_job_id"]))
                        if import_job is not None:
                            import_job.status = new_status
                            import_job.error_message = "worker died while running this job"
                            if new_status == "failed":
                                import_job.finished_at = datetime.now(timezone.utc)
                            db.commit()
                except Exception:
                    logger.exception("Could not save the %s status for job %s", new_status, job["job_id"])


def run_worker() -> None:
    # A new random worker id on every start, so a restarted container counts as a new worker
    worker_id = socket.gethostname() + "-" + uuid.uuid4().hex[:8]
    processing_key = PROCESSING_KEY_PREFIX + worker_id
    worker_key = WORKER_KEY_PREFIX + worker_id
    logger.info("Worker %s started, waiting for jobs", worker_id)

    # Set the heartbeat once BEFORE the first claim, so this worker never owns a processing list without a heartbeat
    redis_client.set(worker_key, time.time(), ex=settings.heartbeat_ttl_seconds)

    # Keep the heartbeat fresh from a background thread, because the main thread can be busy inside a handler for minutes
    stop_heartbeat = threading.Event()
    heartbeat_thread = threading.Thread(target=send_heartbeats, args=(worker_id, stop_heartbeat), daemon=True)
    heartbeat_thread.start()

    # Register the signal handlers: SIGALRM is the job timeout, SIGTERM / SIGINT ask the worker to stop
    signal.signal(signal.SIGALRM, raise_job_timeout)
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    last_reaper_run = 0.0

    while not shutdown_requested:
        # Move retry jobs whose time has come from "delayed" to "queue"
        promote_due_jobs()

        # Look for dead workers every REAPER_INTERVAL_SECONDS. A reaper failure must not stop the worker
        if time.time() - last_reaper_run >= settings.reaper_interval_seconds:
            last_reaper_run = time.time()
            try:
                reap_dead_workers()
            except Exception:
                logger.exception("The reaper failed")

        # Atomically move one job from "queue" to this worker's own list (the claim). Wait up to 2 seconds
        message = redis_client.blmove(QUEUE_KEY, processing_key, 2, "RIGHT", "LEFT")

        # Nothing arrived, so loop again (this also promotes delayed jobs that became due)
        if message is None:
            continue

        # Read the job message. "attempts" is how many times the job has already failed
        job = json.loads(message)
        job_id = job["job_id"]
        job_type = job["job_type"]
        payload = job["payload"]
        attempt_number = job["attempts"] + 1
        logger.info(
            "Claimed job %s (%s), attempt %s of %s", job_id, job_type, attempt_number, job["max_attempts"]
        )

        # error_text stays None if the job succeeds
        error_text = None

        # Look up the handler. An unknown type is a failure that is never retried
        handler = HANDLERS.get(job_type)
        if handler is None:
            error_text = "unknown job type"
            logger.error("Unknown job_type %r for job %s", job_type, job_id)
        else:
            try:
                # Only process_csv jobs change the import status. A send_email job must never touch it.
                # Each attempt starts as "running" with the error from the last attempt cleared
                if job_type == "process_csv":
                    with SessionLocal() as db:
                        import_job = db.get(ImportJob, uuid.UUID(payload["import_job_id"]))
                        import_job.status = "running"
                        import_job.started_at = datetime.now(timezone.utc)
                        import_job.attempts += 1
                        import_job.error_message = None
                        db.commit()

                # Run the handler with a timer. If it runs too long, SIGALRM raises JobTimeoutError inside it.
                # The timer is always cancelled right after, so it never fires during the status updates
                signal.alarm(settings.job_timeout_seconds)
                try:
                    handler(payload)
                finally:
                    signal.alarm(0)

            except Exception as error:
                # Any error, including a timeout or an error from the status update above, counts as a failed attempt
                error_text = str(error)
                logger.exception("Job %s (%s) failed on attempt %s", job_id, job_type, attempt_number)

        # Success: remove the job from this worker's list. This is the acknowledgement
        if error_text is None:
            removed_count = redis_client.lrem(processing_key, 1, message)
            if removed_count == 0:
                logger.warning("Job %s (%s) finished, but the reaper had already recovered it", job_id, job_type)
            logger.info("Finished job %s (%s)", job_id, job_type)
            continue

        # Build the new message from the old one, with one more failed attempt and the error text
        new_job = {**job, "attempts": attempt_number, "last_error": error_text}

        if handler is not None and attempt_number < job["max_attempts"]:
            # Retries are left. Exponential backoff: the first failure waits RETRY_BASE_SECONDS, then it doubles
            delay_seconds = min(settings.retry_max_seconds, settings.retry_base_seconds * 2 ** job["attempts"])

            # Jitter: keep half of the delay and randomize the other half, so failed jobs do not all retry together
            delay_seconds = delay_seconds / 2 + random.uniform(0, delay_seconds / 2)

            # In one Redis transaction: schedule the retry in "delayed" and remove the job from this worker's list
            transaction = redis_client.pipeline(transaction=True)
            transaction.zadd(DELAYED_KEY, {json.dumps(new_job): time.time() + delay_seconds})
            transaction.lrem(processing_key, 1, message)
            transaction.execute()
            logger.warning(
                "Retrying job %s (%s) after attempt %s of %s in %.1f seconds. Error: %s",
                job_id, job_type, attempt_number, job["max_attempts"], delay_seconds, error_text,
            )
            new_status = "retrying"
        else:
            # Last attempt (or an unknown job type). In one Redis transaction: add it to "dead" and remove it from this worker's list
            new_job["failed_at"] = datetime.now(timezone.utc).isoformat()
            transaction = redis_client.pipeline(transaction=True)
            transaction.lpush(DEAD_KEY, json.dumps(new_job))
            transaction.lrem(processing_key, 1, message)
            transaction.execute()
            logger.error(
                "Job %s (%s) is dead after %s attempt(s). Error: %s", job_id, job_type, attempt_number, error_text
            )
            new_status = "failed"

        # Record the failure on the import (process_csv only). The job is already safe in Redis,
        # so if this update fails, log it and keep the worker running
        if job_type == "process_csv":
            try:
                with SessionLocal() as db:
                    import_job = db.get(ImportJob, uuid.UUID(payload["import_job_id"]))
                    if import_job is not None:
                        import_job.status = new_status
                        import_job.error_message = error_text
                        if new_status == "failed":
                            import_job.finished_at = datetime.now(timezone.utc)
                        db.commit()
            except Exception:
                logger.exception("Could not save the %s status for job %s", new_status, job_id)

    # The loop ended because shutdown was requested. Stop the heartbeat thread and wait for it to finish
    stop_heartbeat.set()
    heartbeat_thread.join()

    # Delete the heartbeat key, so the worker is clearly gone and not "alive for 15 more seconds"
    redis_client.delete(worker_key)

    # Report whether this worker's own list is empty (it should be: no job is left half done)
    leftover_count = redis_client.llen(processing_key)
    if leftover_count == 0:
        logger.info("Processing list of worker %s is empty", worker_id)
    else:
        logger.warning("Processing list of worker %s still has %s job(s)", worker_id, leftover_count)
    logger.info("Worker %s stopped", worker_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_worker()
