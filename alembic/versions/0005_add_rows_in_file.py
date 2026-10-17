"""add rows_in_file to import_jobs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Number of data rows in the CSV (null until the first attempt has counted them)
    op.add_column("import_jobs", sa.Column("rows_in_file", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("import_jobs", "rows_in_file")
