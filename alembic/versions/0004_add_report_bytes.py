"""add report_bytes to import_jobs

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Size of the error report file as of the last committed batch (used to resume)
    op.add_column(
        "import_jobs",
        sa.Column("report_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("import_jobs", "report_bytes")
