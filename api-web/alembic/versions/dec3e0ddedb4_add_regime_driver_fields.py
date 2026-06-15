"""add_regime_driver_fields

Revision ID: dec3e0ddedb4
Revises: f5dc2b47812b
Create Date: 2026-06-13 15:12:46.090895
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



revision: str = 'dec3e0ddedb4'
down_revision: Union[str, Sequence[str], None] = 'f5dc2b47812b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('regime_data', sa.Column('dominant_driver', sa.String(length=50), nullable=True))
    op.add_column('regime_data', sa.Column('macro_counterforce_active', sa.Boolean(), nullable=True))
    op.add_column('regime_data', sa.Column('macro_counterforce_note', sa.Text(), nullable=True))
    op.add_column('regime_data', sa.Column('regime_fragility', sa.String(length=20), nullable=True))
    op.add_column('regime_data', sa.Column('regime_break_probability', sa.Integer(), nullable=True))

    op.create_check_constraint(
        'check_dominant_driver',
        'regime_data',
        "dominant_driver IN ('technical', 'macro', 'risk_sentiment', 'mixed')"
    )
    op.create_check_constraint(
        'check_regime_fragility',
        'regime_data',
        "regime_fragility IN ('low', 'medium', 'high')"
    )
    op.create_check_constraint(
        'check_regime_break_probability',
        'regime_data',
        "regime_break_probability >= 0 AND regime_break_probability <= 100"
    )


def downgrade() -> None:
    op.drop_constraint('check_regime_break_probability', 'regime_data')
    op.drop_constraint('check_regime_fragility', 'regime_data')
    op.drop_constraint('check_dominant_driver', 'regime_data')

    op.drop_column('regime_data', 'regime_break_probability')
    op.drop_column('regime_data', 'regime_fragility')
    op.drop_column('regime_data', 'macro_counterforce_note')
    op.drop_column('regime_data', 'macro_counterforce_active')
    op.drop_column('regime_data', 'dominant_driver')
