import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_PROJECT_URL")
SUPABASE_ADMIN_KEY = os.getenv("SUPABASE_SECRET_KEY")


class SupabaseRpcError(RuntimeError):
    pass


async def rpc_get_active_subscription(user_id: str) -> Optional[dict[str, Any]]:
    if not SUPABASE_URL:
        raise SupabaseRpcError("SUPABASE_URL is not set")
    if not SUPABASE_ADMIN_KEY:
        raise SupabaseRpcError("SUPABASE_SECRET_KEY is not set")

    url = SUPABASE_URL.rstrip("/") + "/rest/v1/rpc/get_active_subscription"
    headers = {
        "apikey": SUPABASE_ADMIN_KEY,
        "content-type": "application/json",
    }

    payload = {"p_user_id": user_id}

    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code >= 400:
        logger.warning("Supabase RPC get_active_subscription failed: %s %s", resp.status_code, resp.text)
        raise SupabaseRpcError(f"Supabase RPC error ({resp.status_code})")

    data = resp.json()
    if not data:
        return None
    if isinstance(data, list):
        return data[0]
    if isinstance(data, dict):
        return data
    return None
