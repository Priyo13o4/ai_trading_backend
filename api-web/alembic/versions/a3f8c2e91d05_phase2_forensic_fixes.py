"""phase2_forensic_fixes

Revision ID: a3f8c2e91d05
Revises: dec3e0ddedb4
Create Date: 2026-06-20 00:00:00.000000

Adds columns and an index identified during Phase 2 forensic investigation:
- signals.be_sl_hit          — distinguishes break-even SL hits from genuine adverse hits
- regime_data.direction_bias — net directional lean from the regime classifier
- regime_data.geopolitical_risk_level  — severity of geopolitical risk at classification time
- regime_data.geopolitical_event_type  — event category for Strategy Selector routing rules
- strategies.price_at_analysis         — mid price snapshot at strategy generation time
- idx_regime_data_analysis_ts          — supports ORDER BY analysis_timestamp DESC lookups
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f8c2e91d05'
down_revision: Union[str, Sequence[str], None] = 'dec3e0ddedb4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # signals: track whether break-even SL was the exit mechanism
    op.add_column('signals', sa.Column(
        'be_sl_hit', sa.Boolean(), nullable=True, server_default=sa.text('false')
    ))

    # regime_data: directional lean
    op.add_column('regime_data', sa.Column(
        'direction_bias', sa.String(length=10), nullable=True
    ))
    op.create_check_constraint(
        'check_direction_bias',
        'regime_data',
        "direction_bias IN ('long', 'short', 'neutral')"
    )

    # regime_data: geopolitical risk severity
    op.add_column('regime_data', sa.Column(
        'geopolitical_risk_level', sa.String(length=10), nullable=True
    ))
    op.create_check_constraint(
        'check_geopolitical_risk_level',
        'regime_data',
        "geopolitical_risk_level IN ('low', 'medium', 'high')"
    )

    # regime_data: event category for Strategy Selector direction rules
    op.add_column('regime_data', sa.Column(
        'geopolitical_event_type', sa.String(length=20), nullable=True
    ))
    op.create_check_constraint(
        'check_geopolitical_event_type',
        'regime_data',
        "geopolitical_event_type IN ('escalation', 'de_escalation', 'pending', 'none')"
    )

    # strategies: mid price at generation time for staleness / slippage analysis
    op.add_column('strategies', sa.Column(
        'price_at_analysis', sa.Numeric(precision=15, scale=5), nullable=True
    ))

    # index: fast ORDER BY analysis_timestamp DESC per-symbol lookups
    op.create_index(
        'idx_regime_data_analysis_ts',
        'regime_data',
        [sa.text('analysis_timestamp DESC')],
    )


def downgrade() -> None:
    op.drop_index('idx_regime_data_analysis_ts', table_name='regime_data')

    op.drop_column('strategies', 'price_at_analysis')

    op.drop_constraint('check_geopolitical_event_type', 'regime_data', type_='check')
    op.drop_column('regime_data', 'geopolitical_event_type')

    op.drop_constraint('check_geopolitical_risk_level', 'regime_data', type_='check')
    op.drop_column('regime_data', 'geopolitical_risk_level')

    op.drop_constraint('check_direction_bias', 'regime_data', type_='check')
    op.drop_column('regime_data', 'direction_bias')

    op.drop_column('signals', 'be_sl_hit')
