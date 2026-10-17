import json
import time
import uuid

import redis

from app.config import settings

# Names of the Redis keys.
# queue: waiting jobs (list).
# delayed: jobs waiting to be retried (sorted set, score = Unix time when the job may run again).
# dead: the dead letter queue (list)
QUEUE_KEY = "queue"
DELAYED_KEY = "delayed"
DEAD_KEY = "dead"

# Key prefixes. Each worker has its own list of claimed jobs, "processing:<worker_id>",
# and a heartbeat key, "worker:<worker_id>", that expires when the worker stops refreshing it
PROCESSING_KEY_PREFIX = "processing:"
WORKER_KEY_PREFIX = "worker:"

# One Redis connection for the process. Replies come back as str instead of bytes
redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)

# A Lua script runs as one atomic step inside Redis. It finds every delayed job whose time has come
# (score <= now), removes it from "delayed" and pushes it onto "queue". Because it is one step,
# a crash cannot lose a job between the remove and the push, and two workers cannot both promote the same job
promote_due_jobs_script = redis_client.register_script(
    """
    local due_messages = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
    for _, message in ipairs(due_messages) do
        redis.call('ZREM', KEYS[1], message)
        redis.call('LPUSH', KEYS[2], message)
    end
    return #due_messages
    """
)

# Another Lua script, used by the reaper to take a job away from a dead worker. It removes the job
# from the dead worker's list and, only if that removed exactly one item, pushes the new message onto the
# destination list and returns 1. Otherwise it returns 0. Because it is one atomic step, two workers
# reaping at the same time cannot both recover the same job, and a crash cannot lose the job between the two steps
recover_job_script = redis_client.register_script(
    """
    local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
    if removed == 1 then
        redis.call('LPUSH', KEYS[2], ARGV[2])
        return 1
    end
    return 0
    """
)

# A third Lua script, used by the replay endpoint to move a dead job back to the queue. Same idea as the one above:
# it removes the original message from "dead" and, only if that removed exactly one item, pushes the new message
# onto "queue" and returns 1. Otherwise it returns 0. So two simultaneous replays of the same job can never queue it twice
replay_dead_job_script = redis_client.register_script(
    """
    local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
    if removed == 1 then
        redis.call('LPUSH', KEYS[2], ARGV[2])
        return 1
    end
    return 0
    """
)


def enqueue(job_type: str, payload: dict) -> str:
    # Create a new job id
    job_id = str(uuid.uuid4())

    # Build the job message as a JSON string. "attempts" is how many times the job has already failed
    message = json.dumps(
        {
            "job_id": job_id,
            "job_type": job_type,
            "payload": payload,
            "attempts": 0,
            "max_attempts": settings.max_attempts,
        }
    )

    # Push it onto the left of the queue list (workers take from the right, so it is first in, first out)
    redis_client.lpush(QUEUE_KEY, message)

    return job_id


def promote_due_jobs() -> int:
    # Move all retry jobs that are due from "delayed" to "queue" in one atomic step. Returns how many moved
    return promote_due_jobs_script(keys=[DELAYED_KEY, QUEUE_KEY], args=[time.time()])
