"""Add mae_pips and mfe_pips to signals

Revision ID: f5dc2b47812b
Revises: 34ed5be3cc23
Create Date: 2026-06-09 07:40:34.287430
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



revision: str = 'f5dc2b47812b'
down_revision: Union[str, Sequence[str], None] = '34ed5be3cc23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('signals', sa.Column('mae_pips', sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column('signals', sa.Column('mfe_pips', sa.Numeric(precision=10, scale=2), nullable=True))


def downgrade() -> None:
    op.drop_column('signals', 'mfe_pips')
    op.drop_column('signals', 'mae_pips')
