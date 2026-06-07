"""Add execution_status to strategies

Revision ID: 452e55e95079
Revises: 4db8c464a9db
Create Date: 2026-05-29 21:51:52.043499
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



revision: str = '452e55e95079'
down_revision: Union[str, Sequence[str], None] = '4db8c464a9db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('strategies', sa.Column('execution_status', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('strategies', 'execution_status')
