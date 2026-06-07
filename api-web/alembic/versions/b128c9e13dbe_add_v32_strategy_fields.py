"""add_v32_strategy_fields

Revision ID: b128c9e13dbe
Revises: 8f7e2d4a9c10
Create Date: 2026-06-07 05:41:26.081257
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b128c9e13dbe'
down_revision: Union[str, Sequence[str], None] = '8f7e2d4a9c10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns
    op.add_column('strategies', sa.Column('pre_entry_rule', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('strategies', sa.Column('post_entry_rule', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('strategies', sa.Column('strategy_version', sa.String(length=10), server_default='v1', nullable=True))

    # Update trade_mode CHECK constraint to include 'normal'
    op.execute("ALTER TABLE strategies DROP CONSTRAINT IF EXISTS check_trade_mode")
    op.execute("ALTER TABLE strategies ADD CONSTRAINT check_trade_mode CHECK (trade_mode IN ('normal', 'protective', 'news_opportunistic', 'scalping', 'swing'))")

    # Backfill strategy_version
    op.execute("UPDATE strategies SET strategy_version = 'v1' WHERE strategy_version IS NULL")


def downgrade() -> None:
    op.drop_column('strategies', 'strategy_version')
    op.drop_column('strategies', 'post_entry_rule')
    op.drop_column('strategies', 'pre_entry_rule')
    
    # Revert trade_mode CHECK constraint
    op.execute("ALTER TABLE strategies DROP CONSTRAINT IF EXISTS check_trade_mode")
    op.execute("ALTER TABLE strategies ADD CONSTRAINT check_trade_mode CHECK (trade_mode IN ('protective', 'news_opportunistic', 'scalping', 'swing'))")
