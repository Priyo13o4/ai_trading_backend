from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from urllib.parse import urlparse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
BACKTEST_DB = os.getenv("BACKTEST_DB", "backtest_lab")

if "BACKTEST_DATABASE_URL" in os.environ:
    DATABASE_URL = os.environ["BACKTEST_DATABASE_URL"]
elif BACKTEST_DB:
    DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{BACKTEST_DB}"
else:
    raise RuntimeError("BACKTEST_DATABASE_URL or BACKTEST_DB is required for backtest writes")


def _normalized_db_identity(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    return (parsed.hostname or "localhost", parsed.port or 5432, parsed.path.lstrip("/"))


def assert_backtest_db_is_isolated(source_url: str) -> None:
    if _normalized_db_identity(DATABASE_URL) == _normalized_db_identity(source_url):
        raise RuntimeError(
            "Backtest destination DB matches source DB. Set BACKTEST_DATABASE_URL to an isolated database."
        )

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
