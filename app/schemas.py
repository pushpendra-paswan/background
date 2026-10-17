import re
import uuid
from datetime import date, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Country codes a customer row is allowed to use
ALLOWED_COUNTRY_CODES = [
    "IN", "US", "GB", "CA", "AU", "DE", "FR", "ES", "IT", "NL",
    "SG", "AE", "JP", "CN", "BR", "MX", "ZA", "SE", "CH", "NZ",
]


class ImportJobCreatedResponse(BaseModel):
    job_id: uuid.UUID


class ImportJobResponse(BaseModel):
    # Lets Pydantic read values straight from a SQLAlchemy ImportJob object
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: str
    total_rows: int
    valid_rows: int
    invalid_rows: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    notify_email: str | None
    email_status: str


class CustomerRow(BaseModel):
    # One row of the uploaded CSV. A missing field is reported by Pydantic as "missing"
    name: str = Field(max_length=100)
    email: str
    phone: str
    date_of_birth: date
    country: str

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        # Check the format only (no DNS lookup), then store it lowercased
        try:
            validate_email(value, check_deliverability=False)
        except EmailNotValidError:
            raise ValueError("invalid email format")
        return value.lower()

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str) -> str:
        # Remove spaces, dashes and parentheses, then expect an optional "+" and 7 to 15 digits
        cleaned_phone = re.sub(r"[ \-()]", "", value)
        if not re.fullmatch(r"\+?[0-9]{7,15}", cleaned_phone):
            raise ValueError("invalid phone number")
        return cleaned_phone

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def check_date_of_birth(cls, value: str) -> date:
        # The format must be exactly YYYY-MM-DD
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("invalid date format, use YYYY-MM-DD")

        # The date must exist (for example, not 2020-02-30)
        try:
            date_of_birth = date.fromisoformat(value)
        except ValueError:
            raise ValueError("date does not exist")

        # Not in the future, and not more than about 120 years ago
        today = date.today()
        if date_of_birth > today:
            raise ValueError("date is in the future")
        if date_of_birth < today - timedelta(days=43830):
            raise ValueError("date is more than 120 years ago")
        return date_of_birth

    @field_validator("country")
    @classmethod
    def check_country(cls, value: str) -> str:
        # Uppercase first, then it must be one of the allowed codes
        country_code = value.upper()
        if country_code not in ALLOWED_COUNTRY_CODES:
            raise ValueError("not an allowed country code")
        return country_code
