"""add email columns to import_jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Where the summary email goes, how sending went, and the error if it failed
    op.add_column("import_jobs", sa.Column("notify_email", sa.String(255), nullable=True))
    op.add_column(
        "import_jobs",
        sa.Column("email_status", sa.String(20), nullable=False, server_default="pending"),
    )
    op.add_column("import_jobs", sa.Column("email_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("import_jobs", "email_error")
    op.drop_column("import_jobs", "email_status")
    op.drop_column("import_jobs", "notify_email")
