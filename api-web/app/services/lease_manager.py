"""
LeaseManager: Renewable distributed lease system with fencing tokens.

Provides safe leadership coordination for janitor workers with:
- Fencing tokens to prevent split-brain scenarios
- Automatic heartbeat renewal
- Safe release with token verification
- Atomic operations via Lua scripts
"""

import asyncio
import logging
import socket
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from app.redis_cache import CACHE_REDIS

logger = logging.getLogger(__name__)

# Lua script for atomic token-based SET (acquire/renew)
# Returns 1 if successful, 0 if failed (token mismatch)
LUA_SET_WITH_TOKEN_CHECK = """
local key = KEYS[1]
local new_value = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local expected_token = ARGV[3]

if expected_token == "" then
    -- Initial acquire: only set if key doesn't exist
    local result = redis.call('SET', key, new_value, 'NX', 'EX', ttl_seconds)
    if result then
        return 1
    else
        return 0
    end
else
    -- Renewal: only set if current value contains expected token
    local current = redis.call('GET', key)
    if current and string.find(current, expected_token, 1, true) then
        redis.call('SET', key, new_value, 'EX', ttl_seconds)
        return 1
    else
        return 0
    end
end
"""

# Lua script for atomic token-based DELETE (release)
# Returns 1 if deleted, 0 if token mismatch or key doesn't exist
LUA_DELETE_WITH_TOKEN_CHECK = """
local key = KEYS[1]
local expected_token = ARGV[1]

local current = redis.call('GET', key)
if current and string.find(current, expected_token, 1, true) then
    redis.call('DEL', key)
    return 1
else
    return 0
end
"""


class LeaseManager:
    """
    Distributed lease manager with renewable leases and fencing tokens.

    Features:
    - Atomic acquire with fencing tokens
    - Automatic heartbeat renewal (1/3 of TTL)
    - Safe release with token verification
    - Context manager support
    - Graceful degradation on Redis failures

    Example:
        ```python
        lease_manager = LeaseManager()

        # Manual usage
        token = await lease_manager.acquire_lease("my_lock", ttl_seconds=30)
        if token:
            try:
                # Do work
                if not await lease_manager.check_lease_valid("my_lock", token):
                    # Lost leadership, abort
                    return
            finally:
                await lease_manager.release_lease("my_lock", token)

        # Context manager with auto-renewal
        async with lease_manager.lease("my_lock", ttl_seconds=30) as token:
            if token:
                # Do work, heartbeat runs automatically
                pass
        ```
    """

    def __init__(self, redis_client=None):
        """
        Initialize LeaseManager.

        Args:
            redis_client: Optional Redis client (defaults to CACHE_REDIS)
        """
        self.redis = redis_client or CACHE_REDIS
        self.hostname = socket.gethostname()
        self._heartbeat_tasks = {}  # token -> asyncio.Task

    def _build_lease_key(self, lock_name: str) -> str:
        """Build Redis key for lease."""
        return f"lease:manager:{lock_name}"

    def _build_lease_value(self, token: str, timestamp: float) -> str:
        """
        Build lease value with fencing token, timestamp, and hostname.

        Format: {token}:{timestamp}:{hostname}
        """
        return f"{token}:{timestamp}:{self.hostname}"

    def _parse_lease_value(self, value: str) -> Optional[dict]:
        """Parse lease value into components."""
        try:
            parts = value.split(":", 2)
            if len(parts) >= 2:
                return {
                    "token": parts[0],
                    "timestamp": float(parts[1]),
                    "hostname": parts[2] if len(parts) > 2 else "unknown",
                }
        except Exception:
            pass
        return None

    async def acquire_lease(
        self,
        lock_name: str,
        ttl_seconds: int,
        auto_renew: bool = False,
    ) -> Optional[str]:
        """
        Acquire a distributed lease with fencing token.

        Args:
            lock_name: Name of the lock/lease
            ttl_seconds: Time-to-live in seconds
            auto_renew: If True, start automatic heartbeat renewal

        Returns:
            Fencing token if acquired, None if failed
        """
        key = self._build_lease_key(lock_name)
        token = uuid.uuid4().hex
        timestamp = time.time()
        value = self._build_lease_value(token, timestamp)

        try:
            # Use Lua script for atomic acquire (SET NX)
            result = await self.redis.eval(
                LUA_SET_WITH_TOKEN_CHECK,
                1,
                key,
                value,
                str(ttl_seconds),
                "",  # empty expected_token means initial acquire
            )

            if result == 1:
                logger.info(
                    "[LeaseManager] Acquired lease: lock=%s token=%s ttl=%ds",
                    lock_name,
                    token[:8],
                    ttl_seconds,
                )

                if auto_renew:
                    await self._start_heartbeat(lock_name, token, ttl_seconds)

                return token
            else:
                logger.debug(
                    "[LeaseManager] Failed to acquire lease: lock=%s (already held)",
                    lock_name,
                )
                return None

        except Exception as exc:
            logger.warning(
                "[LeaseManager] Redis error during acquire: lock=%s error=%s",
                lock_name,
                exc,
            )
            return None

    async def renew_lease(
        self,
        lock_name: str,
        token: str,
        ttl_seconds: int,
    ) -> bool:
        """
        Renew an existing lease (heartbeat).

        Args:
            lock_name: Name of the lock/lease
            token: Fencing token from acquire
            ttl_seconds: New TTL in seconds

        Returns:
            True if renewed, False if lost (token mismatch or expired)
        """
        key = self._build_lease_key(lock_name)
        timestamp = time.time()
        value = self._build_lease_value(token, timestamp)

        try:
            # Use Lua script for atomic renewal with token check
            result = await self.redis.eval(
                LUA_SET_WITH_TOKEN_CHECK,
                1,
                key,
                value,
                str(ttl_seconds),
                token,  # must match existing token
            )

            if result == 1:
                logger.debug(
                    "[LeaseManager] Renewed lease: lock=%s token=%s ttl=%ds",
                    lock_name,
                    token[:8],
                    ttl_seconds,
                )
                return True
            else:
                logger.warning(
                    "[LeaseManager] Failed to renew lease: lock=%s token=%s (lost leadership)",
                    lock_name,
                    token[:8],
                )
                return False

        except Exception as exc:
            logger.warning(
                "[LeaseManager] Redis error during renewal: lock=%s token=%s error=%s",
                lock_name,
                token[:8],
                exc,
            )
            return False

    async def release_lease(self, lock_name: str, token: str) -> bool:
        """
        Release a lease safely with token verification.

        Args:
            lock_name: Name of the lock/lease
            token: Fencing token from acquire

        Returns:
            True if released, False if token mismatch (already lost)
        """
        # Stop heartbeat if running
        await self._stop_heartbeat(token)

        key = self._build_lease_key(lock_name)

        try:
            # Use Lua script for atomic token-verified delete
            result = await self.redis.eval(
                LUA_DELETE_WITH_TOKEN_CHECK,
                1,
                key,
                token,
            )

            if result == 1:
                logger.info(
                    "[LeaseManager] Released lease: lock=%s token=%s",
                    lock_name,
                    token[:8],
                )
                return True
            else:
                logger.warning(
                    "[LeaseManager] Failed to release lease: lock=%s token=%s (token mismatch)",
                    lock_name,
                    token[:8],
                )
                return False

        except Exception as exc:
            logger.warning(
                "[LeaseManager] Redis error during release: lock=%s token=%s error=%s",
                lock_name,
                token[:8],
                exc,
            )
            return False

    async def check_lease_valid(self, lock_name: str, token: str) -> bool:
        """
        Check if a lease is still valid (fencing check).

        Args:
            lock_name: Name of the lock/lease
            token: Fencing token from acquire

        Returns:
            True if still holding lease, False if lost
        """
        key = self._build_lease_key(lock_name)

        try:
            current_value = await self.redis.get(key)

            if not current_value:
                logger.warning(
                    "[LeaseManager] Lease check failed: lock=%s token=%s (expired)",
                    lock_name,
                    token[:8],
                )
                return False

            lease_data = self._parse_lease_value(current_value)
            if lease_data and lease_data["token"] == token:
                return True
            else:
                logger.warning(
                    "[LeaseManager] Lease check failed: lock=%s token=%s (token mismatch)",
                    lock_name,
                    token[:8],
                )
                return False

        except Exception as exc:
            logger.warning(
                "[LeaseManager] Redis error during lease check: lock=%s token=%s error=%s",
                lock_name,
                token[:8],
                exc,
            )
            return False

    async def _start_heartbeat(
        self,
        lock_name: str,
        token: str,
        ttl_seconds: int,
    ) -> None:
        """Start automatic heartbeat renewal in background."""
        heartbeat_interval = ttl_seconds / 3  # Renew at 1/3 of TTL

        async def heartbeat_loop():
            try:
                while True:
                    await asyncio.sleep(heartbeat_interval)

                    renewed = await self.renew_lease(lock_name, token, ttl_seconds)
                    if not renewed:
                        logger.error(
                            "[LeaseManager] Heartbeat renewal failed: lock=%s token=%s (lost leadership)",
                            lock_name,
                            token[:8],
                        )
                        break

            except asyncio.CancelledError:
                logger.debug(
                    "[LeaseManager] Heartbeat stopped: lock=%s token=%s",
                    lock_name,
                    token[:8],
                )
                raise
            except Exception as exc:
                logger.error(
                    "[LeaseManager] Heartbeat error: lock=%s token=%s error=%s",
                    lock_name,
                    token[:8],
                    exc,
                )

        task = asyncio.create_task(heartbeat_loop())
        self._heartbeat_tasks[token] = task

        logger.info(
            "[LeaseManager] Started heartbeat: lock=%s token=%s interval=%.1fs",
            lock_name,
            token[:8],
            heartbeat_interval,
        )

    async def _stop_heartbeat(self, token: str) -> None:
        """Stop automatic heartbeat renewal."""
        task = self._heartbeat_tasks.pop(token, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @asynccontextmanager
    async def lease(
        self,
        lock_name: str,
        ttl_seconds: int = 30,
        auto_renew: bool = True,
    ):
        """
        Context manager for automatic lease management.

        Args:
            lock_name: Name of the lock/lease
            ttl_seconds: Time-to-live in seconds
            auto_renew: Enable automatic heartbeat renewal

        Yields:
            Fencing token if acquired, None if failed

        Example:
            ```python
            async with lease_manager.lease("my_lock", ttl_seconds=30) as token:
                if token:
                    # Do work, lease is automatically renewed
                    if not await lease_manager.check_lease_valid("my_lock", token):
                        # Lost leadership mid-work
                        return
                else:
                    # Failed to acquire lease
                    pass
            ```
        """
        token = await self.acquire_lease(lock_name, ttl_seconds, auto_renew=auto_renew)
        try:
            yield token
        finally:
            if token:
                await self.release_lease(lock_name, token)

    async def get_lease_info(self, lock_name: str) -> Optional[dict]:
        """
        Get information about current lease holder (debugging).

        Args:
            lock_name: Name of the lock/lease

        Returns:
            Lease info dict or None if not held
        """
        key = self._build_lease_key(lock_name)

        try:
            current_value = await self.redis.get(key)
            if not current_value:
                return None

            lease_data = self._parse_lease_value(current_value)
            if lease_data:
                ttl = await self.redis.ttl(key)
                lease_data["ttl_seconds"] = ttl
                lease_data["age_seconds"] = time.time() - lease_data["timestamp"]
                return lease_data
            return None

        except Exception as exc:
            logger.warning(
                "[LeaseManager] Error getting lease info: lock=%s error=%s",
                lock_name,
                exc,
            )
            return None
