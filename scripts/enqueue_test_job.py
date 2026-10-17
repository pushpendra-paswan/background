# Usage (inside the api container):
#   python -m scripts.enqueue_test_job sleep_job 3
#   python -m scripts.enqueue_test_job always_fail
#   python -m scripts.enqueue_test_job flaky_job --name test1 --fail-times 2
#   python -m scripts.enqueue_test_job nonsense_type
#   python -m scripts.enqueue_test_job send_email --import-job-id <import id>    (resend an email)
#   python -m scripts.enqueue_test_job process_csv --import-job-id <import id>   (process an import again)
import argparse

from app.queue.redis_queue import enqueue

# Read the job type and the optional arguments
parser = argparse.ArgumentParser()
parser.add_argument("job_type")
parser.add_argument("seconds", nargs="?", type=int, default=1)
parser.add_argument("--import-job-id")
parser.add_argument("--name")
parser.add_argument("--fail-times", type=int, default=2)
arguments = parser.parse_args()

# Pick the payload: an import job id makes it an import payload, flaky_job needs a name, otherwise use seconds
if arguments.import_job_id is not None:
    payload = {"import_job_id": arguments.import_job_id}
elif arguments.job_type == "flaky_job":
    if arguments.name is None:
        parser.error("flaky_job needs --name")
    payload = {"name": arguments.name, "fail_times": arguments.fail_times}
else:
    payload = {"seconds": arguments.seconds}

# Put the job on the queue
job_id = enqueue(arguments.job_type, payload)
print(f"Enqueued {arguments.job_type} job {job_id}")
