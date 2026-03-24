#!/usr/bin/env python3
"""
Dependency-aware startup gate for api-web, api-sse, and api-worker.

Implements Task 4-E: Startup Gating (dependency checks before app start)
- Checks Redis connectivity with PING + round-trip tests
- Checks Redis pubsub connectivity for api-sse (critical for SSE functionality)
- Checks Postgres connectivity
- Checks Supabase connectivity (optional)
- Retry with exponential backoff
- Clear error messages indicating which dependency failed
- Timeout after configurable period (default 30s)
"""

from __future__ import annotations

import os
import sys
import time
import logging
from typing import Callable

import psycopg
import redis
from urllib import error, parse, request

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [STARTUP_GATE] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

STARTUP_TIMEOUT_SECONDS = int(os.getenv("STARTUP_CHECK_TIMEOUT", "30"))
STARTUP_RETRY_INITIAL_SECONDS = float(os.getenv("STARTUP_CHECK_INITIAL_INTERVAL", "0.5"))
STARTUP_RETRY_MAX_SECONDS = float(os.getenv("STARTUP_CHECK_MAX_INTERVAL", "5.0"))
STARTUP_BACKOFF_MULTIPLIER = float(os.getenv("STARTUP_CHECK_BACKOFF", "1.5"))
STARTUP_ROLE = (os.getenv("STARTUP_CHECK_ROLE") or "api-web").strip().lower()


def _redis_url(env_name: str, prefix: str) -> str | None:
    url = (os.getenv(env_name) or "").strip()
    if url:
        return url

    host = (os.getenv(f"{prefix}_HOST") or "").strip()
    port = (os.getenv(f"{prefix}_PORT") or "6379").strip()
    db = (os.getenv(f"{prefix}_DB") or "0").strip()
    password = os.getenv(f"{prefix}_PASSWORD")

    if not host:
        return None
    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


def _check_redis(name: str, url: str | None) -> tuple[bool, str]:
    """
    Check Redis connectivity with PING and round-trip test.

    This ensures Redis is not just running, but can actually handle
    read/write operations before we start the application.
    """
    if not url:
        return False, f"{name} redis url not configured"
    try:
        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)

        # PING test
        if not client.ping():
            client.close()
            return False, f"{name} redis PING failed"

        # Round-trip test: verify we can write and read
        test_key = f"__startup_gate_test_{STARTUP_ROLE}__"
        test_value = "ready"
        client.set(test_key, test_value, ex=5)  # 5 second expiry
        retrieved = client.get(test_key)
        client.delete(test_key)  # Clean up

        if retrieved != test_value:
            client.close()
            return False, f"{name} redis round-trip failed (expected '{test_value}', got '{retrieved}')"

        client.close()
        logger.info(f"✓ {name} Redis ready (PING + round-trip)")
        return True, f"{name} redis ready"
    except Exception as exc:
        return False, f"{name} redis not ready: {type(exc).__name__}: {str(exc)[:100]}"


def _check_redis_pubsub(name: str, url: str | None) -> tuple[bool, str]:
    """
    Check Redis pubsub connectivity by subscribing to a test channel.

    This is critical for api-sse service which relies on Redis pubsub
    for real-time event delivery.
    """
    if not url:
        return False, f"{name} redis url not configured for pubsub"
    try:
        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
        pubsub = client.pubsub()

        # Subscribe to test channel
        test_channel = f"__startup_gate_pubsub_test_{STARTUP_ROLE}__"
        pubsub.subscribe(test_channel)

        # Verify subscription
        message = pubsub.get_message(timeout=2)
        if message and message.get('type') == 'subscribe':
            pubsub.unsubscribe(test_channel)
            pubsub.close()
            client.close()
            logger.info(f"✓ {name} Redis pubsub connectivity verified")
            return True, f"{name} redis pubsub ready"
        else:
            pubsub.close()
            client.close()
            return False, f"{name} redis pubsub subscription confirmation not received"

    except Exception as exc:
        return False, f"{name} redis pubsub not ready: {type(exc).__name__}: {str(exc)[:100]}"


def _check_postgres() -> tuple[bool, str]:
    """Check Postgres connectivity and ability to execute queries."""
    db_name = os.getenv("TRADING_BOT_DB") or os.getenv("POSTGRES_DB")
    host = os.getenv('POSTGRES_HOST')
    port = os.getenv('POSTGRES_PORT')
    dsn = (
        f"host={host} "
        f"port={port} "
        f"dbname={db_name} "
        f"user={os.getenv('POSTGRES_USER')} "
        f"password={os.getenv('POSTGRES_PASSWORD')}"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                result = cur.fetchone()
                if result and result[0] == 1:
                    logger.info(f"✓ Postgres ready at {host}:{port}/{db_name}")
                    return True, "postgres ready"
                else:
                    return False, f"postgres query returned unexpected result: {result}"
    except Exception as exc:
        return False, f"postgres not ready: {type(exc).__name__}: {str(exc)[:100]}"


def _check_supabase() -> tuple[bool, str]:
    """Check Supabase API connectivity (optional - skipped if not configured)."""
    project_url = (os.getenv("SUPABASE_PROJECT_URL") or "").strip()
    service_key = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    if not project_url or not service_key:
        logger.info("✓ Supabase check skipped (not configured)")
        return True, "supabase check skipped"

    target = parse.urljoin(project_url.rstrip("/") + "/", "rest/v1/subscription_plans?select=id&limit=1")
    req = request.Request(
        target,
        headers={
            "apikey": service_key,
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=3) as resp:
            if 200 <= resp.status < 300:
                logger.info(f"✓ Supabase ready at {project_url}")
                return True, "supabase ready"
            return False, f"supabase unexpected status: {resp.status}"
    except error.HTTPError as exc:
        return False, f"supabase http error: {exc.code}"
    except Exception as exc:
        return False, f"supabase not ready: {type(exc).__name__}: {str(exc)[:100]}"


def _checks_for_role(role: str) -> list[Callable[[], tuple[bool, str]]]:
    """
    Return the appropriate checks for the given service role.

    api-sse: Redis (app + session) with pubsub verification
    api-web: Redis (app + session) + Postgres + Supabase
    api-worker: Redis + Postgres
    """
    app_redis_url = _redis_url("APP_REDIS_URL", "REDIS") or _redis_url("CACHE_REDIS_URL", "REDIS")
    session_redis_url = _redis_url("SESSION_REDIS_URL", "SESSION_REDIS")

    if role == "api-sse":
        # SSE service needs Redis with pubsub capability
        return [
            lambda: _check_redis("app", app_redis_url),
            lambda: _check_redis_pubsub("app", app_redis_url),  # Verify pubsub connectivity
            lambda: _check_redis("session", session_redis_url),
        ]

    if role == "api-worker":
        # Worker needs Redis and Postgres
        return [
            lambda: _check_redis("app", app_redis_url),
            _check_postgres,
        ]

    # Default: api-web (full stack)
    return [
        lambda: _check_redis("app", app_redis_url),
        lambda: _check_redis("session", session_redis_url),
        _check_postgres,
        _check_supabase,
    ]


def main() -> int:
    """
    Run startup dependency checks with exponential backoff.

    Returns:
        0 if all checks pass
        1 if checks fail or timeout
    """
    start_time = time.monotonic()
    deadline = start_time + STARTUP_TIMEOUT_SECONDS
    checks = _checks_for_role(STARTUP_ROLE)
    wait_time = STARTUP_RETRY_INITIAL_SECONDS
    attempt = 0

    logger.info("="*60)
    logger.info(f"Starting dependency checks for: {STARTUP_ROLE}")
    logger.info(f"Timeout: {STARTUP_TIMEOUT_SECONDS}s")
    logger.info(f"Backoff: {STARTUP_RETRY_INITIAL_SECONDS}s → {STARTUP_RETRY_MAX_SECONDS}s (×{STARTUP_BACKOFF_MULTIPLIER})")
    logger.info("="*60)
    logger.info("")

    while time.monotonic() < deadline:
        attempt += 1
        elapsed = time.monotonic() - start_time
        logger.info(f"Attempt {attempt} (elapsed: {elapsed:.1f}s)")

        failures: list[str] = []
        for check in checks:
            ok, message = check()
            if not ok:
                failures.append(message)

        if not failures:
            logger.info("")
            logger.info("="*60)
            logger.info(f"✓ All dependencies ready for {STARTUP_ROLE}")
            logger.info(f"✓ Total startup time: {elapsed:.2f}s")
            logger.info("="*60)
            return 0

        # Log failures
        for failure in failures:
            logger.warning(f"  ✗ {failure}")

        # Check if we have time for another retry
        if time.monotonic() + wait_time >= deadline:
            logger.error("")
            logger.error("="*60)
            logger.error(f"✗ Timeout: Dependencies not ready after {elapsed:.1f}s")
            logger.error(f"✗ Failed checks: {len(failures)}")
            logger.error("="*60)
            return 1

        # Wait before retrying (exponential backoff)
        logger.info(f"  Retrying in {wait_time:.1f}s...")
        logger.info("")
        time.sleep(wait_time)
        wait_time = min(wait_time * STARTUP_BACKOFF_MULTIPLIER, STARTUP_RETRY_MAX_SECONDS)

    # Timeout reached
    elapsed = time.monotonic() - start_time
    logger.error("")
    logger.error("="*60)
    logger.error(f"✗ Startup check failed for {STARTUP_ROLE} after {elapsed:.1f}s")
    logger.error("="*60)
    return 1


if __name__ == "__main__":
    sys.exit(main())
