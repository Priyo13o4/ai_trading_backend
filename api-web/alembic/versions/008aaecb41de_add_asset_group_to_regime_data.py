"""Add asset_group to regime_data

Revision ID: 008aaecb41de
Revises: c1e4f7a0b9d2
Create Date: 2026-06-28 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '008aaecb41de'
down_revision: Union[str, Sequence[str], None] = 'c1e4f7a0b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'regime_data',
        sa.Column('asset_group', sa.String(length=30), nullable=True)
    )
    op.create_check_constraint(
        'check_asset_group',
        'regime_data',
        "asset_group IN ('forex', 'crypto', 'metal', 'index', 'commodity')"
    )


def downgrade() -> None:
    op.drop_constraint('check_asset_group', 'regime_data', type_='check')
    op.drop_column('regime_data', 'asset_group')
