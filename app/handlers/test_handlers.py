import logging
import socket
import time

from app.queue.redis_queue import redis_client

logger = logging.getLogger(__name__)


def sleep_job(payload: dict) -> None:
    # Sleep for the number of seconds given in the payload
    logger.info("sleep_job sleeping for %s seconds", payload["seconds"])
    time.sleep(payload["seconds"])
    logger.info("sleep_job woke up")


def always_fail(payload: dict) -> None:
    # Always raise an error, to test how the worker handles a failed job
    raise RuntimeError("always_fail job failed on purpose")


def flaky_job(payload: dict) -> None:
    # Count the calls in Redis. Fail while the count is at most fail_times, then succeed
    call_count = redis_client.incr(f"flaky:{payload['name']}")
    if call_count <= payload["fail_times"]:
        raise RuntimeError(f"flaky_job failed on purpose (call {call_count} of {payload['fail_times']} failures)")
    logger.info("flaky_job succeeded on call %s", call_count)


def tracked_job(payload: dict) -> None:
    # Pretend to work, then record the run in Redis: "ran:<run>" counts how often job n ran,
    # "workers:<run>" counts how many jobs each container (hostname) ran
    time.sleep(payload.get("seconds", 0.2))
    redis_client.hincrby(f"ran:{payload['run']}", str(payload["n"]), 1)
    redis_client.hincrby(f"workers:{payload['run']}", socket.gethostname(), 1)
