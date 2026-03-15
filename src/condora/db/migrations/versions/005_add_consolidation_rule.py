"""Add consolidation_rule_id to processing_blocks

Revision ID: 005
Revises: 004
Create Date: 2026-03-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "processing_blocks",
        sa.Column("consolidation_rule_id", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processing_blocks", "consolidation_rule_id")
