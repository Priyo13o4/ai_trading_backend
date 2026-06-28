"""Add usd_context to weekly_macro_playbook

Revision ID: c1e4f7a0b9d2
Revises: a3f8c2e91d05
Create Date: 2026-06-28 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'c1e4f7a0b9d2'
down_revision: Union[str, Sequence[str], None] = 'a3f8c2e91d05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'weekly_macro_playbook',
        sa.Column('usd_context', JSONB, nullable=True)
    )


def downgrade() -> None:
    op.drop_column('weekly_macro_playbook', 'usd_context')
