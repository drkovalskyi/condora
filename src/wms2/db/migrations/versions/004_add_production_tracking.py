"""Add production tracking columns to workflows

Revision ID: 004
Revises: 003
Create Date: 2026-02-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("events_produced", sa.BigInteger, server_default="0", nullable=False),
    )
    op.add_column(
        "workflows",
        sa.Column("target_events", sa.BigInteger, server_default="0", nullable=False),
    )
    op.add_column(
        "workflows",
        sa.Column("files_processed", sa.Integer, server_default="0", nullable=False),
    )
    op.add_column(
        "workflows",
        sa.Column("total_input_files", sa.Integer, server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("workflows", "total_input_files")
    op.drop_column("workflows", "files_processed")
    op.drop_column("workflows", "target_events")
    op.drop_column("workflows", "events_produced")
