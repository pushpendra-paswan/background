from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import settings
from app.database import Base
import app.models  # noqa: F401  (imported so the tables are registered on Base)

config = context.config
fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Run the migrations against the real database
engine = create_engine(settings.database_url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
