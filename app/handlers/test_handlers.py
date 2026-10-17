import logging
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
