# Usage (inside the api container):
#   python -m scripts.enqueue_test_job sleep_job --seconds 3
#   python -m scripts.enqueue_test_job always_fail
#   python -m scripts.enqueue_test_job flaky_job --name test1 --fail-times 2
#   python -m scripts.enqueue_test_job tracked_job --name myrun --seconds 0.2
#   python -m scripts.enqueue_test_job send_email --import-job-id <import id>    (resend an email)
#   python -m scripts.enqueue_test_job process_csv --import-job-id <import id>   (process an import again)
#   python -m scripts.enqueue_test_job process_csv --import-job-id <import id> --times 2 --keep-attempts
import argparse
import uuid

from sqlalchemy import update

from app.database import SessionLocal
from app.handlers.process_csv import process_csv
from app.handlers.send_email import send_email
from app.handlers.test_handlers import always_fail, flaky_job, sleep_job, tracked_job
from app.models import ImportJob

# Read the task name and the optional arguments
parser = argparse.ArgumentParser()
parser.add_argument("task_name", choices=["process_csv", "send_email", "sleep_job", "always_fail", "flaky_job", "tracked_job"])
parser.add_argument("--import-job-id")
parser.add_argument("--seconds", type=float, default=1)
parser.add_argument("--name")
parser.add_argument("--fail-times", type=int, default=2)
parser.add_argument("--times", type=int, default=1, help="how many times to send the same call")
parser.add_argument("--keep-attempts", action="store_true", help="process_csv: do not set attempts back to 0 first")
arguments = parser.parse_args()

# The import tasks need an import id, flaky_job and tracked_job need a name
if arguments.task_name in ("process_csv", "send_email") and arguments.import_job_id is None:
    parser.error(f"{arguments.task_name} needs --import-job-id")
if arguments.task_name in ("flaky_job", "tracked_job") and arguments.name is None:
    parser.error(f"{arguments.task_name} needs --name")

# process_csv counts every start in import_jobs.attempts. Set it back to 0 first, so an old import can run again
if arguments.task_name == "process_csv" and not arguments.keep_attempts:
    with SessionLocal() as db:
        db.execute(update(ImportJob).where(ImportJob.id == uuid.UUID(arguments.import_job_id)).values(attempts=0))
        db.commit()

# Send the same call as many times as asked
for _ in range(arguments.times):
    if arguments.task_name == "process_csv":
        result = process_csv.delay(arguments.import_job_id)
    elif arguments.task_name == "send_email":
        result = send_email.delay(arguments.import_job_id)
    elif arguments.task_name == "sleep_job":
        result = sleep_job.delay(arguments.seconds)
    elif arguments.task_name == "always_fail":
        result = always_fail.delay()
    elif arguments.task_name == "flaky_job":
        result = flaky_job.delay(arguments.name, arguments.fail_times)
    else:
        result = tracked_job.delay(arguments.name, 0, arguments.seconds)
    print(f"Sent {arguments.task_name} task {result.id}")
