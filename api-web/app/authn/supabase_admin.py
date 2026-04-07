import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_PROJECT_URL")
SUPABASE_ADMIN_KEY = os.getenv("SUPABASE_SECRET_KEY")


class SupabaseAdminError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def _base_headers() -> dict[str, str]:
    if not SUPABASE_URL:
        raise SupabaseAdminError("SUPABASE_URL is not set")
    if not SUPABASE_ADMIN_KEY:
        raise SupabaseAdminError("SUPABASE_SECRET_KEY is not set")

    return {
        "apikey": SUPABASE_ADMIN_KEY,
        "content-type": "application/json",
    }


def _auth_admin_url(path: str) -> str:
    if not SUPABASE_URL:
        raise SupabaseAdminError("SUPABASE_URL is not set")
    return SUPABASE_URL.rstrip("/") + path


def _is_valid_user_payload(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False

    user_id = candidate.get("id")
    if not isinstance(user_id, str) or not user_id.strip():
        return False

    return True


def _extract_user_from_admin_response(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    nested_user = payload.get("user")
    if _is_valid_user_payload(nested_user):
        return nested_user

    if _is_valid_user_payload(payload):
        return payload

    return None


def _safe_payload_diagnostics(payload: Any) -> dict[str, Any]:
    details: dict[str, Any] = {
        "payload_type": type(payload).__name__,
    }
    if isinstance(payload, dict):
        details["payload_keys"] = sorted(str(key) for key in payload.keys())
        nested_user = payload.get("user")
        details["nested_user_type"] = type(nested_user).__name__
        if isinstance(nested_user, dict):
            details["nested_user_keys"] = sorted(str(key) for key in nested_user.keys())
    return details


async def admin_get_user(user_id: str) -> dict[str, Any]:
    url = _auth_admin_url(f"/auth/v1/admin/users/{user_id}")
    headers = _base_headers()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise SupabaseAdminError("Supabase admin get user unavailable", status_code=503) from exc

    if resp.status_code == 404:
        logger.warning("Supabase admin get user not found: status=%s", resp.status_code)
        raise SupabaseAdminError("User not found", status_code=401)

    if resp.status_code >= 400:
        logger.warning("Supabase admin get user failed: status=%s", resp.status_code)
        status_code = 400 if 400 <= resp.status_code < 500 else 503
        raise SupabaseAdminError("Supabase admin get user failed", status_code=status_code)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise SupabaseAdminError("Supabase admin get user malformed response", status_code=503) from exc

    user = _extract_user_from_admin_response(payload)
    if user is None:
        logger.error("Supabase admin get user parse failed details=%s", _safe_payload_diagnostics(payload))
        raise SupabaseAdminError("Unexpected Supabase admin get user response", status_code=503)

    return user


async def admin_update_user(user_id: str, update_payload: dict[str, Any]) -> dict[str, Any]:
    url = _auth_admin_url(f"/auth/v1/admin/users/{user_id}")
    headers = _base_headers()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.put(url, headers=headers, json=update_payload)
    except httpx.RequestError as exc:
        raise SupabaseAdminError("Supabase admin update unavailable", status_code=503) from exc

    if resp.status_code >= 400:
        logger.warning("Supabase admin update user failed: status=%s", resp.status_code)
        status_code = 400 if 400 <= resp.status_code < 500 else 503
        raise SupabaseAdminError("Supabase admin update failed", status_code=status_code)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise SupabaseAdminError("Supabase admin update malformed response", status_code=503) from exc

    user = _extract_user_from_admin_response(payload)
    if user is None:
        logger.error("Supabase admin update user parse failed details=%s", _safe_payload_diagnostics(payload))
        raise SupabaseAdminError("Unexpected Supabase admin update user response", status_code=503)

    return user
