import os
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from typing import Any, AsyncIterator, Callable, TypeVar
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

DB_TIMEOUT_RETRY_AFTER_SECONDS = (os.getenv("DB_TIMEOUT_RETRY_AFTER_SECONDS") or "5").strip() or "5"

# Max 20 concurrent DB calls per worker to prevent overload and unbounded thread explosion
_db_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="db_pool")

T = TypeVar("T")


async def async_db(func: Callable[[], T], timeout: float = 5.0) -> T:
    """
    Execute a synchronous Supabase call non-blocking with strict timeout.

    This wrapper prevents blocking the async event loop when calling
    synchronous Supabase .execute() methods. It runs the blocking call
    in a bounded thread pool with a strict timeout.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_db_executor, func),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.error("DB call exceeded %s second timeout", timeout)
        raise HTTPException(
            status_code=503,
            detail="Database timeout",
            headers={"Retry-After": DB_TIMEOUT_RETRY_AFTER_SECONDS},
        )
    except Exception as e:
        logger.error("DB call failed: %s", str(e)[:200])
        raise


supabase_db = async_db


def shutdown_db_executor():
    """Shutdown the thread pool executor. Call on application shutdown."""
    _db_executor.shutdown(wait=True, cancel_futures=True)


# ============================================================================
# DATABASES & ENGINES
# ============================================================================

_db_name = os.getenv('TRADING_BOT_DB') or os.getenv('POSTGRES_DB')
POSTGRES_DSN = f"host={os.getenv('POSTGRES_HOST')} port={os.getenv('POSTGRES_PORT')} dbname={_db_name} user={os.getenv('POSTGRES_USER')} password={os.getenv('POSTGRES_PASSWORD')}"

POSTGRES_ASYNC_DSN = (
    os.getenv("DATABASE_URL")
    or f"postgresql+psycopg://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD', '')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}:{os.getenv('POSTGRES_PORT', '5432')}/{_db_name}"
)
if POSTGRES_ASYNC_DSN.startswith("postgresql://"):
    POSTGRES_ASYNC_DSN = POSTGRES_ASYNC_DSN.replace("postgresql://", "postgresql+psycopg://", 1)

async_engine = create_async_engine(
    POSTGRES_ASYNC_DSN,
    pool_pre_ping=True,
    connect_args={"prepared_statement_cache_size": 0},
)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


def _use_timescale_caggs() -> bool:
    return (os.getenv("USE_TIMESCALE_CAGGS") or "").strip().lower() in {"1", "true", "yes", "y"}


_supabase_client = None


def get_supabase_project_host() -> str | None:
    url = (os.getenv("SUPABASE_PROJECT_URL") or "").strip()
    if not url:
        return None
    return urlparse(url).hostname


def reset_supabase_client() -> None:
    global _supabase_client
    _supabase_client = None


def get_supabase_client():
    from supabase import create_client
    global _supabase_client
    if _supabase_client is None:
        url = os.getenv("SUPABASE_PROJECT_URL")
        key = os.getenv("SUPABASE_SECRET_KEY")
        if not url or not key:
            raise Exception("Missing Supabase credentials (SUPABASE_PROJECT_URL and SUPABASE_SECRET_KEY) in environment")
        _supabase_client = create_client(url, key)
    return _supabase_client
