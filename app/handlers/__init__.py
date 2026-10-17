from app.handlers.process_csv import process_csv
from app.handlers.send_email import send_email
from app.handlers.test_handlers import always_fail, flaky_job, sleep_job

# Maps a job_type to the function that runs it. Every handler takes the payload dict
HANDLERS = {
    "process_csv": process_csv,
    "send_email": send_email,
    "sleep_job": sleep_job,
    "always_fail": always_fail,
    "flaky_job": flaky_job,
}
