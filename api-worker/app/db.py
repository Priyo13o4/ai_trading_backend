"""API Worker - Database configuration.

Creates service-specific database connection using shared utilities.
"""

import os
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from trading_common.db import build_postgres_dsn, build_postgres_dsn_legacy

# Service-specific Postgres DSN (legacy format for psycopg compatibility)
POSTGRES_DSN = build_postgres_dsn_legacy()

POSTGRES_ASYNC_DSN = os.getenv("DATABASE_URL") or build_postgres_dsn()
if POSTGRES_ASYNC_DSN.startswith("postgresql://"):
    POSTGRES_ASYNC_DSN = POSTGRES_ASYNC_DSN.replace("postgresql://", "postgresql+psycopg://", 1)

async_engine = create_async_engine(
    POSTGRES_ASYNC_DSN,
    pool_pre_ping=True,
    connect_args={"prepare_threshold": None},
)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
