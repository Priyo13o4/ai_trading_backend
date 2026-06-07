from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
SOURCE_DB = os.getenv("POSTGRES_DB", os.getenv("TRADING_BOT_DB", "ai_trading_bot_data"))

SOURCE_DATABASE_URL = (
    os.getenv("SOURCE_DATABASE_URL")
    or f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{SOURCE_DB}"
)

source_engine = create_async_engine(SOURCE_DATABASE_URL, echo=False)
SourceAsyncSessionLocal = async_sessionmaker(
    bind=source_engine, class_=AsyncSession, expire_on_commit=False
)

async def get_source_db() -> AsyncGenerator[AsyncSession, None]:
    async with SourceAsyncSessionLocal() as session:
        yield session
