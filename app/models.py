import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    phone: Mapped[str] = mapped_column(String(50))
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    country: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # The import that inserted this customer (used to make retries safe)
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), server_default="queued")
    total_rows: Mapped[int] = mapped_column(Integer, server_default="0")
    valid_rows: Mapped[int] = mapped_column(Integer, server_default="0")
    invalid_rows: Mapped[int] = mapped_column(Integer, server_default="0")
    last_processed_row: Mapped[int] = mapped_column(Integer, server_default="0")
    # Size in bytes of the error report file as of the last committed batch (used to resume)
    report_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    error_report_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notify_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # One of: pending, sent, failed
    email_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    email_error: Mapped[str | None] = mapped_column(Text, nullable=True)
