"""Add output_files and site to processing_blocks for Rucio retry

Revision ID: 006
Revises: 005
Create Date: 2026-03-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "processing_blocks",
        sa.Column("output_files", JSONB, nullable=True),
    )
    op.add_column(
        "processing_blocks",
        sa.Column("site", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processing_blocks", "output_files")
    op.drop_column("processing_blocks", "site")
