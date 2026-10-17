from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# One engine for the whole app
engine = create_engine(settings.database_url)

# Each request gets its own session from this factory
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    # Open a session for the request and always close it afterwards
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
