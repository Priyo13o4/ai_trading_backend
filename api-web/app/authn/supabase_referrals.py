import logging
import os
import re
import time
import hashlib
import ipaddress
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .supabase_admin import SupabaseAdminError, admin_get_user

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_PROJECT_URL")
SUPABASE_ADMIN_KEY = os.getenv("SUPABASE_SECRET_KEY")
REFERRAL_CODE_PATTERN = re.compile(r"^[A-Z0-9]{6,20}$")
REFERRAL_CAPTURE_FRESHNESS_SECONDS = int(os.getenv("REFERRAL_CAPTURE_FRESHNESS_SECONDS", "900"))


class SupabaseReferralError(RuntimeError):
    pass


def _base_headers(*, prefer: str | None = None) -> dict[str, str]:
    if not SUPABASE_URL:
        raise SupabaseReferralError("SUPABASE_URL is not set")
    if not SUPABASE_ADMIN_KEY:
        raise SupabaseReferralError("SUPABASE_SECRET_KEY is not set")

    headers = {
        "apikey": SUPABASE_ADMIN_KEY,
        "authorization": f"Bearer {SUPABASE_ADMIN_KEY}",
        "content-type": "application/json",
    }
    if prefer:
        headers["prefer"] = prefer
    return headers


def _rest_url(path: str) -> str:
    if not SUPABASE_URL:
        raise SupabaseReferralError("SUPABASE_URL is not set")
    return SUPABASE_URL.rstrip("/") + path


def normalize_referral_code(raw_code: Any) -> str | None:
    if not isinstance(raw_code, str):
        return None
    normalized = raw_code.strip().upper()
    if not normalized:
        return None
    if not REFERRAL_CODE_PATTERN.match(normalized):
        return None
    return normalized


def _extract_referral_code_from_user(user_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(user_payload, dict):
        return None

    raw_meta = user_payload.get("raw_user_meta_data") or user_payload.get("user_metadata")
    if isinstance(raw_meta, dict):
        normalized = normalize_referral_code(raw_meta.get("referral_code"))
        if normalized:
            return normalized

    return None


def _parse_user_created_at_epoch(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        epoch = int(value)
        return epoch if epoch > 0 else None
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    if raw.isdigit():
        epoch = int(raw)
        return epoch if epoch > 0 else None

    normalized = raw
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return int(parsed.timestamp())


def _extract_user_created_at_epoch(user_payload: dict[str, Any] | None) -> int | None:
    if not isinstance(user_payload, dict):
        return None
    return _parse_user_created_at_epoch(user_payload.get("created_at"))


def _is_fresh_signup(created_at_epoch: int | None) -> bool:
    if created_at_epoch is None:
        return False
    age = int(time.time()) - created_at_epoch
    # Allow 300 seconds (5 mins) buffer for clock drift between Supabase cloud and local container/server
    return -300 <= age <= REFERRAL_CAPTURE_FRESHNESS_SECONDS


def _hash_user_agent(user_agent: str | None) -> str:
    """Return a stable SHA256 hex digest for a User-Agent string."""
    normalized = (user_agent or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _derive_ip_prefix(ip_address: str | None) -> str:
    """Return an anonymized network prefix from a raw client IP.

    IPv4: first 3 octets (e.g. 203.0.113)
    IPv6: first 64 bits (first 4 hextets)
    """
    raw = (ip_address or "").strip()
    if not raw:
        return ""

    try:
        ip_obj = ipaddress.ip_address(raw)
    except ValueError:
        return ""

    if ip_obj.version == 4:
        octets = str(ip_obj).split(".")
        if len(octets) != 4:
            return ""
        return ".".join(octets[:3])

    hextets = ip_obj.exploded.split(":")
    return ":".join(hextets[:4])


async def _resolve_active_referral_code(code: str) -> dict[str, str] | None:
    safe_code = quote(code, safe="")
    url = _rest_url(
        "/rest/v1/referral_codes"
        f"?select=id,user_id,code&code=eq.{safe_code}&is_active=is.true&limit=1"
    )
    headers = _base_headers()

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise SupabaseReferralError("referral code lookup unavailable") from exc

    if resp.status_code >= 400:
        raise SupabaseReferralError(f"referral code lookup failed ({resp.status_code})")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise SupabaseReferralError("referral code lookup malformed response") from exc

    if not isinstance(payload, list) or not payload:
        return None

    first = payload[0]
    if not isinstance(first, dict):
        return None

    referral_code_id = first.get("id")
    code_owner_user_id = first.get("user_id")
    if not isinstance(referral_code_id, str) or not isinstance(code_owner_user_id, str):
        return None

    return {
        "referral_code_id": referral_code_id,
        "referrer_id": code_owner_user_id,
    }


async def _insert_referral_tracking_row(
    *,
    referrer_id: str,
    referred_id: str,
    referral_code_id: str,
    ua_hash: str,
    ip_prefix: str,
) -> bool:
    url = _rest_url("/rest/v1/referral_tracking?on_conflict=referred_id")
    headers = _base_headers(prefer="resolution=ignore-duplicates,return=representation")

    metadata: dict[str, Any] = {
        "attribution_source": "auth_exchange",
    }
    audit_metadata: dict[str, Any] = {
        "attribution_security": {
            "ip_prefix": ip_prefix or None,
            "ua_hash": ua_hash or None,
        }
    }

    payload = [
        {
            "referrer_id": referrer_id,
            "referred_id": referred_id,
            "referral_code_id": referral_code_id,
            "status": "pending",
            "metadata": metadata,
            "audit_metadata": audit_metadata,
            "registration_ua_hash": ua_hash or None,
            "registration_ip_prefix": ip_prefix or None,
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.RequestError as exc:
        raise SupabaseReferralError("referral tracking insert unavailable") from exc

    if resp.status_code >= 400:
        raise SupabaseReferralError(f"referral tracking insert failed ({resp.status_code})")

    try:
        response_payload = resp.json()
    except ValueError:
        return False

    return isinstance(response_payload, list) and len(response_payload) > 0


async def capture_referral_attribution_from_exchange(
    *,
    referred_user_id: str,
    claims: dict[str, Any],
    user_agent: str,
    ip_address: str,
) -> str:
    """Capture referral attribution for fresh signups.

    Security fingerprint values are computed here to enforce consistent
    attribution-time hashing behavior across all callers.
    """
    referral_code = _extract_referral_code_from_user(claims)
    claims_created_at = _extract_user_created_at_epoch(claims)
    admin_user: dict[str, Any] | None = None
    admin_created_at: int | None = None
    ua_hash = _hash_user_agent(user_agent)
    ip_prefix = _derive_ip_prefix(ip_address)

    try:
        admin_user = await admin_get_user(referred_user_id)
    except SupabaseAdminError as exc:
        if not referral_code:
            raise SupabaseReferralError("referral metadata fetch failed") from exc
    else:
        admin_created_at = _extract_user_created_at_epoch(admin_user)

    created_at_for_freshness = admin_created_at if admin_created_at is not None else claims_created_at
    if not _is_fresh_signup(created_at_for_freshness):
        return "skip:stale_signup_for_capture"

    if not referral_code:
        if admin_user is None:
            try:
                admin_user = await admin_get_user(referred_user_id)
            except SupabaseAdminError as exc:
                raise SupabaseReferralError("referral metadata fetch failed") from exc

        referral_code = _extract_referral_code_from_user(admin_user)

    if not referral_code:
        return "skip:no_referral_code"

    resolved = await _resolve_active_referral_code(referral_code)
    if not resolved:
        return "skip:code_not_found"

    referrer_id = resolved["referrer_id"]
    if referrer_id == referred_user_id:
        return "skip:self_referral"

    inserted = await _insert_referral_tracking_row(
        referrer_id=referrer_id,
        referred_id=referred_user_id,
        referral_code_id=resolved["referral_code_id"],
        ua_hash=ua_hash,
        ip_prefix=ip_prefix,
    )
    if inserted:
        return "success:captured"

    return "skip:already_attributed"