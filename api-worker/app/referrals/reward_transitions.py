"""Referral rewards transition logic for worker runtime.

Provides async wrappers to execute DB RPCs that transition referral rewards:
1. on_hold -> available
2. available -> applied
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ADMIN_KEY = os.getenv("SUPABASE_SECRET_KEY")


class ReferralTransitionConfigurationError(RuntimeError):
    """Raised when required Supabase configuration or RPC contract is missing."""


class ReferralTransitionExecutionError(RuntimeError):
    """Raised when an RPC call returns a runtime failure."""


def _is_debug_enabled() -> bool:
    return os.getenv("AUTHDBG_ENABLED", "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _debug_log(message: str) -> None:
    if _is_debug_enabled():
        logger.info("[REFERRAL_REWARDS] %s", message)


@dataclass(frozen=True)
class TransitionResult:
    outcome: str
    transitioned_count: int = 0


def _rpc_call(rpc_name: str) -> object:
    if not SUPABASE_URL:
        raise ReferralTransitionConfigurationError("SUPABASE_URL is not set")
    if not SUPABASE_ADMIN_KEY:
        raise ReferralTransitionConfigurationError("SUPABASE_SECRET_KEY is not set")

    url = SUPABASE_URL.rstrip("/") + f"/rest/v1/rpc/{rpc_name}"
    headers = {
        "apikey": SUPABASE_ADMIN_KEY,
        "authorization": f"Bearer {SUPABASE_ADMIN_KEY}",
        "content-type": "application/json",
    }

    response = requests.post(url, headers=headers, json={}, timeout=10)
    if response.status_code >= 400:
        body = response.text[:500]
        if response.status_code in {401, 403}:
            raise ReferralTransitionConfigurationError(
                f"supabase_auth_failed rpc={rpc_name} status={response.status_code} body={body}"
            )
        if response.status_code == 404:
            raise ReferralTransitionConfigurationError(
                f"required_rpc_missing rpc={rpc_name} status={response.status_code} body={body}"
            )
        raise ReferralTransitionExecutionError(
            f"rpc_failed rpc={rpc_name} status={response.status_code} body={body}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ReferralTransitionExecutionError(f"Supabase RPC {rpc_name} returned invalid JSON") from exc


def _run_transition_rpc(rpc_name: str, count_field: str) -> TransitionResult:
    data = _rpc_call(rpc_name)

    if isinstance(data, list):
        row = data[0] if data and isinstance(data[0], dict) else None
    elif isinstance(data, dict):
        row = data
    else:
        row = None

    if not row:
        raise ReferralTransitionExecutionError(f"rpc_empty_result rpc={rpc_name}")

    result_code = str(row.get("result_code") or "").strip()
    transitioned_count = int(row.get(count_field) or 0)

    if result_code != "success":
        raise ReferralTransitionExecutionError(
            f"rpc_unexpected_result_code rpc={rpc_name} result_code={result_code}"
        )

    _debug_log(f"{rpc_name} transitioned {transitioned_count} rows")
    return TransitionResult(outcome="success", transitioned_count=transitioned_count)


async def transition_rewards_on_hold_to_available() -> TransitionResult:
    return await asyncio.to_thread(
        _run_transition_rpc,
        "transition_rewards_on_hold_to_available",
        "transitioned_count",
    )


async def apply_available_rewards() -> TransitionResult:
    return await asyncio.to_thread(
        _run_transition_rpc,
        "apply_available_rewards",
        "applied_count",
    )
