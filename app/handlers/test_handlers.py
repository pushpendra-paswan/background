import logging
import socket
import time

import redis

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)

redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name="sleep_job")
def sleep_job(seconds: float) -> None:
    # Sleep for the given number of seconds
    logger.info("sleep_job sleeping for %s seconds", seconds)
    time.sleep(seconds)
    logger.info("sleep_job woke up")


@celery_app.task(
    name="always_fail",
    autoretry_for=(Exception,),
    retry_backoff=settings.retry_base_seconds,
    retry_backoff_max=settings.retry_max_seconds,
    retry_jitter=True,
    max_retries=settings.max_attempts - 1,
)
def always_fail() -> None:
    # Always raise an error, to test retries
    raise RuntimeError("always_fail job failed on purpose")


@celery_app.task(
    name="flaky_job",
    autoretry_for=(Exception,),
    retry_backoff=settings.retry_base_seconds,
    retry_backoff_max=settings.retry_max_seconds,
    retry_jitter=True,
    max_retries=settings.max_attempts - 1,
)
def flaky_job(name: str, fail_times: int) -> None:
    # Count the calls in Redis. Fail while the count is at most fail_times, then succeed
    call_count = redis_client.incr(f"flaky:{name}")
    if call_count <= fail_times:
        raise RuntimeError(f"flaky_job failed on purpose (call {call_count} of {fail_times} failures)")
    logger.info("flaky_job succeeded on call %s", call_count)


@celery_app.task(name="tracked_job")
def tracked_job(run: str, n: int, seconds: float = 0.2) -> None:
    # Pretend to work, then record the run in Redis: "ran:<run>" counts how often job n ran,
    # "workers:<run>" counts how many jobs each container (hostname) ran
    time.sleep(seconds)
    redis_client.hincrby(f"ran:{run}", str(n), 1)
    redis_client.hincrby(f"workers:{run}", socket.gethostname(), 1)
