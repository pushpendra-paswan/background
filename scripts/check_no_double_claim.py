# Usage (inside the api container, with several workers running):
#   python -m scripts.check_no_double_claim --jobs 200
# Enqueues tracked_job jobs, waits until the workers are done, and checks that every job ran exactly once.
import argparse
import time
from datetime import datetime

from app.queue.redis_queue import DELAYED_KEY, PROCESSING_KEY_PREFIX, QUEUE_KEY, enqueue, redis_client

# Read the command line arguments
parser = argparse.ArgumentParser(description="Check that no job is run twice or lost when several workers run")
parser.add_argument("--jobs", type=int, default=200)
parser.add_argument("--timeout", type=int, default=180)
arguments = parser.parse_args()

# The run name makes the Redis keys of this run unique
run_name = datetime.now().strftime("%Y%m%d%H%M%S")
ran_key = f"ran:{run_name}"
workers_key = f"workers:{run_name}"

# Put all the jobs on the queue
for job_number in range(arguments.jobs):
    enqueue("tracked_job", {"run": run_name, "n": job_number})
print(f"Run {run_name}: enqueued {arguments.jobs} jobs, waiting for the workers")

# Wait until the queue is empty, no worker has a job in its list, and nothing has changed for 3 seconds
started_waiting = time.time()
last_state = None
unchanged_since = time.time()
while time.time() - started_waiting < arguments.timeout:
    jobs_in_processing_lists = 0
    for processing_key in redis_client.scan_iter(match=PROCESSING_KEY_PREFIX + "*"):
        jobs_in_processing_lists += redis_client.llen(processing_key)
    runs_so_far = sum(int(count) for count in redis_client.hvals(ran_key))
    state = (redis_client.llen(QUEUE_KEY), redis_client.zcard(DELAYED_KEY), jobs_in_processing_lists, runs_so_far)

    # Remember when the state last changed
    if state != last_state:
        last_state = state
        unchanged_since = time.time()

    # Finished: nothing waiting, nothing running, and nothing changed for 3 seconds
    if state[0] == 0 and state[1] == 0 and state[2] == 0 and time.time() - unchanged_since >= 3:
        break
    time.sleep(1)
else:
    print(f"Timeout after {arguments.timeout} seconds, the jobs did not all finish")

# Read how often each job ran, and find the ones that ran 0 times or more than once
run_counts = redis_client.hgetall(ran_key)
wrong_jobs = []
for job_number in range(arguments.jobs):
    if int(run_counts.get(str(job_number), 0)) != 1:
        wrong_jobs.append(job_number)

if len(wrong_jobs) == 0:
    print(f"PASS: all {arguments.jobs} jobs ran exactly once")
else:
    print(f"FAIL: {len(wrong_jobs)} jobs did not run exactly once (job number: times run)")
    for job_number in wrong_jobs:
        print(f"  {job_number}: {run_counts.get(str(job_number), 0)}")

# Show how many jobs each worker (container hostname) ran
jobs_per_worker = redis_client.hgetall(workers_key)
print("Jobs per worker:")
for hostname, count in sorted(jobs_per_worker.items()):
    print(f"  {hostname}: {count}")
if len(jobs_per_worker) < 2:
    print("WARNING: fewer than two workers ran jobs. Start more with: docker compose up -d --scale worker=3")
