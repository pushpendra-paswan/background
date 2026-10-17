import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

from app.config import settings

# Fields about the job that is running right now. The worker sets it for every job and resets it at the end.
# Nobody changes the dict itself, so the shared default is safe
job_context: contextvars.ContextVar[dict] = contextvars.ContextVar("job_context", default={})

# The attributes every LogRecord has. Anything else on a record came from extra= (or from job_context)
STANDARD_RECORD_ATTRIBUTES = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JobContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Copy the job fields onto the record. A value passed with extra= wins
        for field_name, field_value in job_context.get().items():
            if not hasattr(record, field_name):
                setattr(record, field_name, field_value)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, worker_id: str | None) -> None:
        super().__init__()
        self.service = service
        self.worker_id = worker_id

    def format(self, record: logging.LogRecord) -> str:
        # The fixed fields come first
        log_line = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
        }
        if self.worker_id is not None:
            log_line["worker_id"] = self.worker_id
        log_line["logger"] = record.name
        log_line["message"] = record.getMessage()

        # Then every extra attribute of the record
        for attribute_name, attribute_value in record.__dict__.items():
            if attribute_name not in STANDARD_RECORD_ATTRIBUTES:
                log_line[attribute_name] = attribute_value

        # The traceback goes into one field, so the log line stays one line
        if record.exc_info:
            log_line["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_line, default=str)


def setup_logging(service: str, worker_id: str | None = None) -> None:
    # One handler on the root logger: JSON lines to stdout
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, worker_id))
    handler.addFilter(JobContextFilter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(settings.log_level.upper())

    # Uvicorn sets up its own handlers. Remove them so its lines go through the JSON handler too
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
