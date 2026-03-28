import hashlib
import ipaddress
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_PROJECT_URL")
SUPABASE_ADMIN_KEY = os.getenv("SUPABASE_SECRET_KEY")


class TrialPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrialPolicyOutcome:
    trial_allowed: bool
    reason: str
    device_id_hash: str | None
    had_active_trial: bool



def extract_device_id(*, body: dict[str, object], header_value: str | None) -> str:
    header_device = (header_value or "").strip()
    if header_device:
        return header_device

    raw = body.get("device_id")
    if isinstance(raw, str):
        return raw.strip()

    return ""



def hash_device_id(device_id: str) -> str:
    normalized = (device_id or "").strip().lower()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()



def _ua_hash(user_agent: str) -> str:
    normalized = (user_agent or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()



def _ip_prefix(ip_address: str) -> str:
    raw = (ip_address or "").strip()
    if not raw:
        return ""

    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return ""

    if ip.version == 4:
        parts = str(ip).split(".")
        if len(parts) != 4:
            return ""
        return ".".join(parts[:3])

    return ":".join(ip.exploded.split(":")[:4])



def _base_headers(*, prefer: str | None = None) -> dict[str, str]:
    if not SUPABASE_URL:
        raise TrialPolicyError("SUPABASE_URL is not set")
    if not SUPABASE_ADMIN_KEY:
        raise TrialPolicyError("SUPABASE_SECRET_KEY is not set")

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
        raise TrialPolicyError("SUPABASE_URL is not set")
    return SUPABASE_URL.rstrip("/") + path


async def _has_active_trial_subscription(user_id: str) -> bool:
    now_iso = datetime.now(timezone.utc).isoformat()
    url = _rest_url(
        "/rest/v1/user_subscriptions"
        f"?select=id&user_id=eq.{user_id}&status=eq.trial&expires_at=gt.{now_iso}&limit=1"
    )

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, headers=_base_headers())
    except httpx.RequestError as exc:
        raise TrialPolicyError("active trial lookup unavailable") from exc

    if resp.status_code >= 400:
        raise TrialPolicyError(f"active trial lookup failed ({resp.status_code})")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise TrialPolicyError("active trial lookup malformed response") from exc

    return isinstance(payload, list) and len(payload) > 0


async def _mark_device_trial_first_use(
    *,
    device_id_hash: str,
    user_id: str,
    user_agent: str,
    ip_address: str,
) -> bool:
    url = _rest_url("/rest/v1/device_trials?on_conflict=device_id_hash")
    headers = _base_headers(prefer="resolution=ignore-duplicates,return=representation")
    payload = [
        {
            "device_id_hash": device_id_hash,
            "first_user_id": user_id,
            "trial_used": True,
            "metadata": {
                "ip_prefix": _ip_prefix(ip_address) or None,
                "ua_hash": _ua_hash(user_agent) or None,
            },
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.RequestError as exc:
        raise TrialPolicyError("device trial insert unavailable") from exc

    if resp.status_code >= 400:
        raise TrialPolicyError(f"device trial insert failed ({resp.status_code})")

    try:
        result = resp.json()
    except ValueError:
        return False

    return isinstance(result, list) and len(result) > 0


async def _disable_trial_entitlement(user_id: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    url = _rest_url(f"/rest/v1/user_subscriptions?user_id=eq.{user_id}&status=eq.trial")
    payload = {
        "status": "expired",
        "expires_at": now_iso,
        "trial_ends_at": now_iso,
    }

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.patch(url, headers=_base_headers(), json=payload)
    except httpx.RequestError as exc:
        raise TrialPolicyError("trial disable unavailable") from exc

    if resp.status_code >= 400:
        raise TrialPolicyError(f"trial disable failed ({resp.status_code})")


async def apply_trial_policy_for_exchange(
    *,
    user_id: str,
    device_id: str,
    user_agent: str,
    ip_address: str,
) -> TrialPolicyOutcome:
    device_hash = hash_device_id(device_id)
    if not device_hash:
        return TrialPolicyOutcome(
            trial_allowed=True,
            reason="skip_no_device_id",
            device_id_hash=None,
            had_active_trial=False,
        )

    had_active_trial = await _has_active_trial_subscription(user_id)
    if not had_active_trial:
        return TrialPolicyOutcome(
            trial_allowed=True,
            reason="skip_no_active_trial",
            device_id_hash=device_hash,
            had_active_trial=False,
        )

    first_use = await _mark_device_trial_first_use(
        device_id_hash=device_hash,
        user_id=user_id,
        user_agent=user_agent,
        ip_address=ip_address,
    )

    if first_use:
        return TrialPolicyOutcome(
            trial_allowed=True,
            reason="allow_first_device_trial",
            device_id_hash=device_hash,
            had_active_trial=True,
        )

    await _disable_trial_entitlement(user_id)
    logger.info(
        "auth.trial_policy outcome=disable_trial user_id=%s reason=same_device",
        user_id,
    )
    return TrialPolicyOutcome(
        trial_allowed=False,
        reason="deny_same_device",
        device_id_hash=device_hash,
        had_active_trial=True,
    )
