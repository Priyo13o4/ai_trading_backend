"""Allow neutral direction

Revision ID: 34ed5be3cc23
Revises: b128c9e13dbe
Create Date: 2026-06-08 23:40:59.027372
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



revision: str = '34ed5be3cc23'
down_revision: Union[str, Sequence[str], None] = 'b128c9e13dbe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop existing constraint
    op.drop_constraint('strategies_direction_check', 'strategies', type_='check')
    op.drop_constraint('signals_direction_check', 'signals', type_='check')
    
    # Add new constraint allowing 'neutral'
    op.create_check_constraint(
        'strategies_direction_check', 
        'strategies', 
        "direction IN ('long', 'short', 'neutral')"
    )
    op.create_check_constraint(
        'signals_direction_check', 
        'signals', 
        "direction IN ('long', 'short', 'neutral')"
    )


def downgrade() -> None:
    op.drop_constraint('strategies_direction_check', 'strategies', type_='check')
    op.drop_constraint('signals_direction_check', 'signals', type_='check')
    
    op.create_check_constraint(
        'strategies_direction_check', 
        'strategies', 
        "direction IN ('long', 'short')"
    )
    op.create_check_constraint(
        'signals_direction_check', 
        'signals', 
        "direction IN ('long', 'short')"
    )
