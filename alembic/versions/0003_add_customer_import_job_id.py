"""add import_job_id to customers

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Which import inserted the customer (null for customers that were not created by an import)
    op.add_column("customers", sa.Column("import_job_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "import_job_id")
