"""Add signal source and widen execution status

Revision ID: 8f7e2d4a9c10
Revises: 452e55e95079
Create Date: 2026-05-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8f7e2d4a9c10"
down_revision: Union[str, Sequence[str], None] = "452e55e95079"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return inspector.has_table(table_name, schema="public") or inspector.has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name, schema="public"))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name, schema="public"))


def upgrade() -> None:
    if _has_column("strategies", "execution_status"):
        op.alter_column(
            "strategies",
            "execution_status",
            existing_type=sa.String(length=20),
            type_=sa.String(length=50),
            existing_nullable=True,
        )

    if _has_table("signals") and not _has_column("signals", "source"):
        op.add_column("signals", sa.Column("source", sa.String(length=50), nullable=True))

    if _has_column("signals", "source") and not _has_index("signals", "idx_signals_source"):
        op.create_index("idx_signals_source", "signals", ["source"], unique=False)

    if _has_column("signals", "source") and not _has_index("signals", "idx_signals_source_pair_status_entry_time"):
        op.create_index(
            "idx_signals_source_pair_status_entry_time",
            "signals",
            ["source", "trading_pair", "status", "entry_time"],
            unique=False,
        )


def downgrade() -> None:
    if _has_index("signals", "idx_signals_source_pair_status_entry_time"):
        op.drop_index("idx_signals_source_pair_status_entry_time", table_name="signals")

    if _has_index("signals", "idx_signals_source"):
        op.drop_index("idx_signals_source", table_name="signals")

    if _has_column("signals", "source"):
        op.drop_column("signals", "source")

    if _has_column("strategies", "execution_status"):
        op.alter_column(
            "strategies",
            "execution_status",
            existing_type=sa.String(length=50),
            type_=sa.String(length=20),
            existing_nullable=True,
        )
