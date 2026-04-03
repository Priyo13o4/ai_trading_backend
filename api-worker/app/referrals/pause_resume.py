"""Referral reward pause/resume state machine orchestration.

This module powers Scope E for referral rewards that grant free months by pausing
and later resuming Razorpay subscriptions.

Data authority is Supabase only. No local Postgres schema is used by this worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
_DEFAULT_BATCH_SIZE = 100
_SUPABASE_TIMEOUT_SECONDS = 15

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_ADMIN_KEY = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()

_SCHEMA_VALIDATED = False


class ReferralPauseResumeConfigurationError(RuntimeError):
    """Raised when runtime configuration or Supabase contract prerequisites are missing."""


class ReferralPauseResumeExecutionError(RuntimeError):
    """Raised for runtime execution failures."""


def _is_debug_enabled() -> bool:
    return (os.getenv("AUTHDBG_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _razorpay_is_configured() -> bool:
    return bool((os.getenv("RAZORPAY_KEY_ID") or "").strip() and (os.getenv("RAZORPAY_KEY_SECRET") or "").strip())


def _log_event(level: int, event: str, **fields: Any) -> None:
    payload = " ".join(f"{key}={fields[key]}" for key in sorted(fields) if fields[key] is not None)
    logger.log(level, "event=%s %s", event, payload)


@dataclass(frozen=True)
class RunStats:
    seeded_cycles: int = 0
    paused_success: int = 0
    resumed_success: int = 0
    failed_marked: int = 0
    deferred_pending: int = 0
    extended_rewards: int = 0


class RazorpayPauseResumeClient:
    """Minimal Razorpay REST client for subscription pause/resume operations."""

    def __init__(self) -> None:
        self._key_id = (os.getenv("RAZORPAY_KEY_ID") or "").strip()
        self._key_secret = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
        if not self._key_id or not self._key_secret:
            raise ReferralPauseResumeConfigurationError("Razorpay credentials are not configured")

    @staticmethod
    def _extract_pause_id(response_body: dict[str, Any]) -> str | None:
        for key in ("pause_id", "id", "entity_id"):
            value = response_body.get(key)
            if value:
                return str(value)
        pause = response_body.get("pause")
        if isinstance(pause, dict):
            value = pause.get("id")
            if value:
                return str(value)
        return None

    def pause_subscription(
        self,
        subscription_id: str,
        pause_at_timestamp: int,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        url = f"{RAZORPAY_API_BASE}/subscriptions/{subscription_id}/pause"
        headers = {
            "Content-Type": "application/json",
            "X-Razorpay-Idempotency": idempotency_key,
        }
        payload = {"pause_at": pause_at_timestamp}

        response = requests.post(
            url,
            auth=(self._key_id, self._key_secret),
            json=payload,
            headers=headers,
            timeout=15,
        )
        if response.status_code >= 400:
            raise ReferralPauseResumeExecutionError(
                f"pause_failed status={response.status_code} body={response.text[:500]}"
            )

        body = response.json() if response.content else {}
        return {
            "status": str(body.get("status") or "paused"),
            "pause_id": self._extract_pause_id(body),
            "raw": body,
        }

    def resume_subscription(
        self,
        subscription_id: str,
        pause_id: str | None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        url = f"{RAZORPAY_API_BASE}/subscriptions/{subscription_id}/resume"
        headers = {
            "Content-Type": "application/json",
            "X-Razorpay-Idempotency": idempotency_key,
        }
        payload: dict[str, Any] = {"resume_at": "now"}
        if pause_id:
            payload["pause_id"] = pause_id

        response = requests.post(
            url,
            auth=(self._key_id, self._key_secret),
            json=payload,
            headers=headers,
            timeout=15,
        )
        if response.status_code >= 400:
            raise ReferralPauseResumeExecutionError(
                f"resume_failed status={response.status_code} body={response.text[:500]}"
            )

        body = response.json() if response.content else {}
        return {
            "status": str(body.get("status") or "active"),
            "raw": body,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _reward_cycle_target() -> int:
    value = _env_int("REFERRAL_REWARD_FREE_MONTHS_PER_CLAIM", 1)
    return max(1, value)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _derive_cycle_duration_seconds(last_charge_at: Any, next_charge_at: Any) -> int | None:
    last_dt = _parse_timestamp(last_charge_at)
    next_dt = _parse_timestamp(next_charge_at)
    if not last_dt or not next_dt:
        return None

    delta_seconds = int((next_dt - last_dt).total_seconds())
    if delta_seconds <= 0:
        return None
    return delta_seconds


def _should_defer_pause(next_charge_at: Any, now_utc: datetime) -> bool:
    next_dt = _parse_timestamp(next_charge_at)
    if not next_dt:
        return False
    return next_dt <= (now_utc + timedelta(hours=48))


def _build_idempotency_key(prefix: str, reward_id: str, cycle_number: int) -> str:
    base = f"{prefix}:{reward_id}:{cycle_number}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return f"referral-{prefix}-{digest[:48]}"


def _assert_supabase_configured() -> None:
    if not SUPABASE_URL:
        raise ReferralPauseResumeConfigurationError("SUPABASE_URL is not set")
    if not SUPABASE_ADMIN_KEY:
        raise ReferralPauseResumeConfigurationError("SUPABASE_SECRET_KEY is not set")


def _supabase_headers(*, prefer: str | None = None) -> dict[str, str]:
    _assert_supabase_configured()
    headers = {
        "apikey": SUPABASE_ADMIN_KEY,
        "authorization": f"Bearer {SUPABASE_ADMIN_KEY}",
        "content-type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _supabase_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    payload: object | None = None,
    prefer: str | None = None,
) -> object | None:
    url = SUPABASE_URL.rstrip("/") + path
    request_kwargs: dict[str, Any] = {
        "headers": _supabase_headers(prefer=prefer),
        "params": params,
        "timeout": _SUPABASE_TIMEOUT_SECONDS,
    }
    if payload is not None:
        request_kwargs["json"] = payload

    response = requests.request(method=method, url=url, **request_kwargs)

    if response.status_code >= 400:
        message = response.text[:500]
        if response.status_code in {401, 403}:
            raise ReferralPauseResumeConfigurationError(
                f"supabase_auth_failed status={response.status_code} body={message}"
            )
        raise ReferralPauseResumeExecutionError(
            f"supabase_request_failed path={path} status={response.status_code} body={message}"
        )

    if response.status_code == 204 or not response.content:
        return None

    try:
        return response.json()
    except ValueError as exc:
        raise ReferralPauseResumeExecutionError(
            f"supabase_invalid_json path={path} status={response.status_code}"
        ) from exc


def _as_rows(payload: object | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ReferralPauseResumeExecutionError("Unexpected Supabase payload type")


def _table_get(table: str, *, params: dict[str, str]) -> list[dict[str, Any]]:
    return _as_rows(_supabase_request("GET", f"/rest/v1/{table}", params=params))


def _table_patch(
    table: str,
    *,
    params: dict[str, str],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return _as_rows(
        _supabase_request(
            "PATCH",
            f"/rest/v1/{table}",
            params=params,
            payload=payload,
            prefer="return=representation",
        )
    )


def _table_insert(
    table: str,
    *,
    payload: list[dict[str, Any]],
    on_conflict: str | None = None,
    prefer: str = "return=representation",
) -> list[dict[str, Any]]:
    params: dict[str, str] = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    return _as_rows(
        _supabase_request(
            "POST",
            f"/rest/v1/{table}",
            params=params,
            payload=payload,
            prefer=prefer,
        )
    )


def _validate_supabase_schema_contract() -> None:
    checks = [
        (
            "referral_rewards",
            "referral_id,user_id,status,created_at,updated_at",
        ),
        (
            "referral_reward_pause_cycles",
            "reward_id,cycle_number,status,razorpay_subscription_id,razorpay_pause_id,"
            "last_charge_at,next_charge_at,pause_start_time,pause_end_time,pause_confirmed,"
            "free_access_until,pause_deferred_until,cycle_duration_seconds,created_at,updated_at",
        ),
        (
            "user_subscriptions",
            "user_id,payment_provider,external_subscription_id,last_payment_date,next_billing_date,"
            "expires_at,created_at,updated_at",
        ),
    ]

    for table, select_clause in checks:
        try:
            _table_get(table, params={"select": select_clause, "limit": "1"})
        except Exception as exc:
            raise ReferralPauseResumeConfigurationError(
                f"supabase_schema_contract_failed table={table} error={str(exc)[:500]}"
            ) from exc


def _assert_runtime_preconditions() -> None:
    global _SCHEMA_VALIDATED

    _assert_supabase_configured()
    if not _razorpay_is_configured():
        raise ReferralPauseResumeConfigurationError("Razorpay credentials are not configured")
    if not _SCHEMA_VALIDATED:
        _validate_supabase_schema_contract()
        _SCHEMA_VALIDATED = True


def _chunk(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [values]
    return [values[i : i + size] for i in range(0, len(values), size)]


def _in_filter(values: list[str]) -> str:
    return "(" + ",".join(values) + ")"


def _seed_pending_cycles(*, batch_size: int) -> int:
    claimed = _table_get(
        "referral_rewards",
        params={
            "select": "referral_id,user_id,status,created_at",
            "status": "eq.claimed",
            "order": "created_at.asc",
            "limit": str(batch_size),
        },
    )
    if not claimed:
        return 0

    reward_ids = [str(row.get("referral_id") or "").strip() for row in claimed]
    reward_ids = [rid for rid in reward_ids if rid]
    user_ids = sorted({str(row.get("user_id") or "").strip() for row in claimed if row.get("user_id")})

    if not reward_ids or not user_ids:
        return 0

    subscription_rows: list[dict[str, Any]] = []
    for user_id_chunk in _chunk(user_ids, 100):
        subscription_rows.extend(
            _table_get(
                "user_subscriptions",
                params={
                    "select": "user_id,external_subscription_id,last_payment_date,next_billing_date,expires_at,"
                    "created_at,updated_at",
                    "payment_provider": "eq.razorpay",
                    "external_subscription_id": "not.is.null",
                    "user_id": f"in.{_in_filter(user_id_chunk)}",
                    "order": "updated_at.desc,created_at.desc",
                },
            )
        )

    latest_subscription_by_user: dict[str, dict[str, Any]] = {}
    for sub in subscription_rows:
        user_id = str(sub.get("user_id") or "").strip()
        if user_id and user_id not in latest_subscription_by_user:
            latest_subscription_by_user[user_id] = sub

    existing_seeded_ids: set[str] = set()
    for reward_id_chunk in _chunk(reward_ids, 150):
        existing_rows = _table_get(
            "referral_reward_pause_cycles",
            params={
                "select": "reward_id,cycle_number",
                "reward_id": f"in.{_in_filter(reward_id_chunk)}",
                "cycle_number": "eq.1",
            },
        )
        for row in existing_rows:
            reward_id = str(row.get("reward_id") or "").strip()
            if reward_id:
                existing_seeded_ids.add(reward_id)

    now_iso = _to_iso(_utc_now())
    rows_to_insert: list[dict[str, Any]] = []

    for reward_row in claimed:
        reward_id = str(reward_row.get("referral_id") or "").strip()
        user_id = str(reward_row.get("user_id") or "").strip()
        if not reward_id or not user_id:
            continue
        if reward_id in existing_seeded_ids:
            continue

        subscription = latest_subscription_by_user.get(user_id)
        if not subscription:
            _log_event(
                logging.ERROR,
                "referral_pause_seed_failed",
                outcome="missing_razorpay_subscription",
                reward_id=reward_id,
                user_id=user_id,
            )
            continue

        next_charge = subscription.get("next_billing_date") or subscription.get("expires_at")
        row: dict[str, Any] = {
            "reward_id": reward_id,
            "cycle_number": 1,
            "status": "reward_pending",
            "razorpay_subscription_id": subscription.get("external_subscription_id"),
            "last_charge_at": subscription.get("last_payment_date"),
            "next_charge_at": next_charge,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        rows_to_insert.append(row)

    if not rows_to_insert:
        return 0

    inserted = _table_insert(
        "referral_reward_pause_cycles",
        payload=rows_to_insert,
        on_conflict="reward_id,cycle_number",
        prefer="resolution=ignore-duplicates,return=representation",
    )
    return len(inserted)


def _cas_cycle_status(
    *,
    reward_id: str,
    cycle_number: int,
    expected_status: str,
    new_status: str,
    extra_fields: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "status": new_status,
        "updated_at": _to_iso(_utc_now()),
    }
    if extra_fields:
        payload.update(extra_fields)

    rows = _table_patch(
        "referral_reward_pause_cycles",
        params={
            "reward_id": f"eq.{reward_id}",
            "cycle_number": f"eq.{cycle_number}",
            "status": f"eq.{expected_status}",
            "select": "reward_id,cycle_number,status",
        },
        payload=payload,
    )
    return len(rows) > 0


def _update_cycle_fields(
    *,
    reward_id: str,
    cycle_number: int,
    payload: dict[str, Any],
    expected_status: str | None = None,
    require_pause_confirmed: bool | None = None,
) -> bool:
    params: dict[str, str] = {
        "reward_id": f"eq.{reward_id}",
        "cycle_number": f"eq.{cycle_number}",
        "select": "reward_id,cycle_number,status",
    }
    if expected_status is not None:
        params["status"] = f"eq.{expected_status}"
    if require_pause_confirmed is not None:
        params["pause_confirmed"] = f"eq.{str(require_pause_confirmed).lower()}"

    rows = _table_patch("referral_reward_pause_cycles", params=params, payload=payload)
    return len(rows) > 0


def _fetch_pause_candidates(*, batch_size: int) -> list[dict[str, Any]]:
    rows = _table_get(
        "referral_reward_pause_cycles",
        params={
            "select": "reward_id,cycle_number,status,razorpay_subscription_id,last_charge_at,next_charge_at,"
            "pause_deferred_until,created_at",
            "status": "in.(reward_pending,pause_pending,pause_failed)",
            "order": "created_at.asc",
            "limit": str(batch_size),
        },
    )

    now_utc = _utc_now()
    due: list[dict[str, Any]] = []
    for row in rows:
        deferred_until = _parse_timestamp(row.get("pause_deferred_until"))
        if deferred_until and deferred_until > now_utc:
            continue
        due.append(row)
    return due


def _fetch_resume_candidates(*, batch_size: int) -> list[dict[str, Any]]:
    rows = _table_get(
        "referral_reward_pause_cycles",
        params={
            "select": "reward_id,cycle_number,status,razorpay_pause_id,razorpay_subscription_id,"
            "pause_confirmed,free_access_until,pause_end_time,updated_at",
            "status": "in.(paused,resume_pending,resume_failed)",
            "pause_confirmed": "eq.true",
            "order": "updated_at.asc",
            "limit": str(batch_size),
        },
    )

    now_utc = _utc_now()
    due: list[dict[str, Any]] = []
    for row in rows:
        effective_end = _parse_timestamp(row.get("free_access_until")) or _parse_timestamp(row.get("pause_end_time"))
        if not effective_end:
            continue
        if effective_end <= now_utc:
            due.append(row)
    return due


def _promote_pause_pending_if_needed(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip()
    if status == "pause_pending":
        return True
    reward_id = str(row.get("reward_id") or "").strip()
    cycle_number = int(row.get("cycle_number") or 0)
    if not reward_id or cycle_number <= 0:
        return False
    promoted = _cas_cycle_status(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status=status,
        new_status="pause_pending",
    )
    if promoted:
        row["status"] = "pause_pending"
    return promoted


def _promote_resume_pending_if_needed(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip()
    if status == "resume_pending":
        return True
    reward_id = str(row.get("reward_id") or "").strip()
    cycle_number = int(row.get("cycle_number") or 0)
    if not reward_id or cycle_number <= 0:
        return False
    promoted = _cas_cycle_status(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status=status,
        new_status="resume_pending",
    )
    if promoted:
        row["status"] = "resume_pending"
    return promoted


def _mark_pause_success(
    *,
    reward_id: str,
    cycle_number: int,
    pause_id: str | None,
    pause_start_time: datetime,
    pause_end_time: datetime,
    cycle_duration_seconds: int,
) -> bool:
    return _update_cycle_fields(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="pause_pending",
        payload={
            "status": "paused",
            "razorpay_pause_id": pause_id,
            "pause_start_time": _to_iso(pause_start_time),
            "pause_end_time": _to_iso(pause_end_time),
            "pause_confirmed": True,
            "free_access_until": _to_iso(pause_end_time),
            "cycle_duration_seconds": cycle_duration_seconds,
            "pause_deferred_until": None,
            "updated_at": _to_iso(_utc_now()),
        },
    )


def _mark_pause_deferred(
    *,
    reward_id: str,
    cycle_number: int,
    defer_until: datetime,
) -> bool:
    return _update_cycle_fields(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="pause_pending",
        payload={
            "pause_deferred_until": _to_iso(defer_until),
            "updated_at": _to_iso(_utc_now()),
        },
    )


def _mark_pause_failed(*, reward_id: str, cycle_number: int) -> bool:
    return _cas_cycle_status(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="pause_pending",
        new_status="pause_failed",
    )


def _mark_resume_success(*, reward_id: str, cycle_number: int) -> bool:
    now_iso = _to_iso(_utc_now())
    return _update_cycle_fields(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="resume_pending",
        payload={
            "status": "resumed",
            "pause_confirmed": False,
            "free_access_until": None,
            "pause_end_time": now_iso,
            "updated_at": now_iso,
        },
    )


def _mark_resume_failed(*, reward_id: str, cycle_number: int) -> bool:
    return _cas_cycle_status(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="resume_pending",
        new_status="resume_failed",
    )


def _find_open_pause_window(*, subscription_id: str) -> dict[str, Any] | None:
    rows = _table_get(
        "referral_reward_pause_cycles",
        params={
            "select": "reward_id,cycle_number,free_access_until,pause_end_time,updated_at",
            "razorpay_subscription_id": f"eq.{subscription_id}",
            "status": "eq.paused",
            "pause_confirmed": "eq.true",
            "order": "updated_at.desc",
            "limit": "100",
        },
    )

    now_utc = _utc_now()
    selected: dict[str, Any] | None = None
    selected_resume: datetime | None = None
    for row in rows:
        resume_time = _parse_timestamp(row.get("free_access_until")) or _parse_timestamp(row.get("pause_end_time"))
        if not resume_time or resume_time <= now_utc:
            continue
        if selected_resume is None or resume_time > selected_resume:
            selected = row
            selected_resume = resume_time

    if not selected or not selected_resume:
        return None

    return {
        "reward_id": str(selected.get("reward_id") or ""),
        "cycle_number": int(selected.get("cycle_number") or 0),
        "resume_time": selected_resume,
    }


def _extend_pause_window(
    *,
    reward_id: str,
    cycle_number: int,
    extension_seconds: int,
    current_resume_time: datetime,
) -> datetime | None:
    new_resume_time = current_resume_time + timedelta(seconds=extension_seconds)
    updated = _update_cycle_fields(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="paused",
        require_pause_confirmed=True,
        payload={
            "pause_end_time": _to_iso(new_resume_time),
            "free_access_until": _to_iso(new_resume_time),
            "updated_at": _to_iso(_utc_now()),
        },
    )
    if not updated:
        return None
    return new_resume_time


def _mark_reward_consumed_by_extension(
    *,
    reward_id: str,
    cycle_number: int,
    resume_time: datetime,
    cycle_duration_seconds: int,
) -> bool:
    return _update_cycle_fields(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="pause_pending",
        payload={
            "status": "resumed",
            "pause_confirmed": True,
            "pause_start_time": _to_iso(_utc_now()),
            "pause_end_time": _to_iso(resume_time),
            "free_access_until": _to_iso(resume_time),
            "cycle_duration_seconds": cycle_duration_seconds,
            "updated_at": _to_iso(_utc_now()),
        },
    )


def _load_subscription_cycle(subscription_id: str) -> tuple[Any, Any] | None:
    rows = _table_get(
        "user_subscriptions",
        params={
            "select": "last_payment_date,next_billing_date,expires_at,updated_at",
            "payment_provider": "eq.razorpay",
            "external_subscription_id": f"eq.{subscription_id}",
            "order": "updated_at.desc",
            "limit": "1",
        },
    )
    if not rows:
        return None
    row = rows[0]
    return row.get("last_payment_date"), (row.get("next_billing_date") or row.get("expires_at"))


def _refresh_cycle_timestamps_for_pause(
    *,
    reward_id: str,
    cycle_number: int,
    subscription_id: str,
) -> tuple[int | None, datetime | None]:
    cycle_values = _load_subscription_cycle(subscription_id)
    if not cycle_values:
        return None, None

    last_charge_at, next_charge_at = cycle_values
    cycle_duration_seconds = _derive_cycle_duration_seconds(last_charge_at, next_charge_at)

    _update_cycle_fields(
        reward_id=reward_id,
        cycle_number=cycle_number,
        expected_status="pause_pending",
        payload={
            "last_charge_at": last_charge_at,
            "next_charge_at": next_charge_at,
            "updated_at": _to_iso(_utc_now()),
        },
    )

    return cycle_duration_seconds, _parse_timestamp(next_charge_at)


def _mark_parent_reward_applied(*, reward_id: str) -> bool:
    rows = _table_patch(
        "referral_rewards",
        params={
            "referral_id": f"eq.{reward_id}",
            "status": "eq.claimed",
            "select": "referral_id,status",
        },
        payload={
            "status": "applied",
            "updated_at": _to_iso(_utc_now()),
        },
    )
    return len(rows) > 0


def run_referral_pause_resume_cycle() -> RunStats:
    """Run one end-to-end pause/resume reconciliation cycle against Supabase."""
    batch_size = max(1, _env_int("REFERRAL_PAUSE_RESUME_BATCH_SIZE", _DEFAULT_BATCH_SIZE))
    pause_lead_seconds = max(0, _env_int("REFERRAL_PAUSE_RESUME_LEAD_SECONDS", 120))

    _assert_runtime_preconditions()
    client = RazorpayPauseResumeClient()

    seeded = _seed_pending_cycles(batch_size=batch_size)
    paused_ok = 0
    resumed_ok = 0
    failed_marked = 0
    deferred_pending = 0
    extended_rewards = 0

    pending_rows = _fetch_pause_candidates(batch_size=batch_size)
    for row in pending_rows:
        reward_id = str(row.get("reward_id") or "").strip()
        cycle_number = int(row.get("cycle_number") or 0)
        subscription_id = str(row.get("razorpay_subscription_id") or "").strip()

        if not reward_id or cycle_number <= 0 or not subscription_id:
            if reward_id and cycle_number > 0:
                marked = _mark_pause_failed(reward_id=reward_id, cycle_number=cycle_number)
                if marked:
                    failed_marked += 1
            _log_event(
                logging.ERROR,
                "referral_pause_attempt",
                outcome="pause_failed_data_error",
                reward_id=reward_id or "none",
                cycle_number=cycle_number or "none",
                subscription_id=subscription_id or "none",
            )
            continue

        if not _promote_pause_pending_if_needed(row):
            continue

        now_utc = _utc_now()
        cycle_duration_seconds = _derive_cycle_duration_seconds(
            row.get("last_charge_at"),
            row.get("next_charge_at"),
        )
        next_charge_dt = _parse_timestamp(row.get("next_charge_at"))

        if cycle_duration_seconds is None:
            cycle_duration_seconds, refreshed_next_charge = _refresh_cycle_timestamps_for_pause(
                reward_id=reward_id,
                cycle_number=cycle_number,
                subscription_id=subscription_id,
            )
            if refreshed_next_charge:
                next_charge_dt = refreshed_next_charge

        if cycle_duration_seconds is None:
            marked = _mark_pause_failed(reward_id=reward_id, cycle_number=cycle_number)
            if marked:
                failed_marked += 1
            _log_event(
                logging.ERROR,
                "referral_pause_attempt_failed",
                outcome="missing_cycle_duration",
                reward_id=reward_id,
                cycle_number=cycle_number,
                subscription_id=subscription_id,
            )
            continue

        total_extension_seconds = cycle_duration_seconds * _reward_cycle_target()

        open_pause = _find_open_pause_window(subscription_id=subscription_id)
        if open_pause:
            open_reward_id = str(open_pause.get("reward_id") or "")
            open_cycle_number = int(open_pause.get("cycle_number") or 0)
            open_resume_time = open_pause.get("resume_time")
            if open_reward_id and open_cycle_number > 0 and isinstance(open_resume_time, datetime):
                extended_until = _extend_pause_window(
                    reward_id=open_reward_id,
                    cycle_number=open_cycle_number,
                    extension_seconds=total_extension_seconds,
                    current_resume_time=open_resume_time,
                )
                if extended_until:
                    consumed = _mark_reward_consumed_by_extension(
                        reward_id=reward_id,
                        cycle_number=cycle_number,
                        resume_time=extended_until,
                        cycle_duration_seconds=cycle_duration_seconds,
                    )
                    if consumed:
                        extended_rewards += 1
                    _log_event(
                        logging.INFO,
                        "referral_pause_attempt",
                        outcome="extended_existing_pause" if consumed else "stale",
                        reward_id=reward_id,
                        cycle_number=cycle_number,
                        subscription_id=subscription_id,
                        extended_until=_to_iso(extended_until),
                    )
                    continue

        if _should_defer_pause(next_charge_dt or row.get("next_charge_at"), now_utc):
            if next_charge_dt:
                deferred = _mark_pause_deferred(
                    reward_id=reward_id,
                    cycle_number=cycle_number,
                    defer_until=next_charge_dt + timedelta(minutes=5),
                )
                if deferred:
                    deferred_pending += 1
                _log_event(
                    logging.INFO,
                    "referral_pause_attempt",
                    outcome="deferred_near_renewal",
                    reward_id=reward_id,
                    cycle_number=cycle_number,
                    subscription_id=subscription_id,
                    next_charge_at=_to_iso(next_charge_dt),
                )
                continue

        pause_at = int((now_utc + timedelta(seconds=pause_lead_seconds)).timestamp())
        pause_end = now_utc + timedelta(seconds=total_extension_seconds)
        idempotency_key = _build_idempotency_key("pause", reward_id, cycle_number)

        try:
            result = client.pause_subscription(
                subscription_id,
                pause_at,
                idempotency_key=idempotency_key,
            )
            pause_id = result.get("pause_id")

            updated = _mark_pause_success(
                reward_id=reward_id,
                cycle_number=cycle_number,
                pause_id=str(pause_id) if pause_id else None,
                pause_start_time=now_utc,
                pause_end_time=pause_end,
                cycle_duration_seconds=cycle_duration_seconds,
            )
            if updated:
                paused_ok += 1

            _log_event(
                logging.INFO,
                "referral_pause_attempt",
                outcome="success" if updated else "stale",
                reward_id=reward_id,
                cycle_number=cycle_number,
                subscription_id=subscription_id,
                pause_id=pause_id,
                pause_at=pause_at,
                cycle_duration_seconds=cycle_duration_seconds,
                pause_end_time=_to_iso(pause_end),
            )
        except Exception as exc:
            marked = _mark_pause_failed(reward_id=reward_id, cycle_number=cycle_number)
            if marked:
                failed_marked += 1
            _log_event(
                logging.ERROR,
                "referral_pause_attempt_failed",
                outcome="retry_pause_failed",
                reward_id=reward_id,
                cycle_number=cycle_number,
                subscription_id=subscription_id,
                error=str(exc)[:500],
            )

    resume_rows = _fetch_resume_candidates(batch_size=batch_size)
    for row in resume_rows:
        reward_id = str(row.get("reward_id") or "").strip()
        cycle_number = int(row.get("cycle_number") or 0)
        subscription_id = str(row.get("razorpay_subscription_id") or "").strip()
        pause_id = row.get("razorpay_pause_id")

        if not reward_id or cycle_number <= 0 or not subscription_id:
            if reward_id and cycle_number > 0:
                marked = _mark_resume_failed(reward_id=reward_id, cycle_number=cycle_number)
                if marked:
                    failed_marked += 1
            _log_event(
                logging.ERROR,
                "referral_resume_attempt",
                outcome="resume_failed_data_error",
                reward_id=reward_id or "none",
                cycle_number=cycle_number or "none",
                subscription_id=subscription_id or "none",
            )
            continue

        if not _promote_resume_pending_if_needed(row):
            continue

        idempotency_key = _build_idempotency_key("resume", reward_id, cycle_number)

        try:
            result = client.resume_subscription(
                subscription_id,
                str(pause_id) if pause_id else None,
                idempotency_key=idempotency_key,
            )
            updated = _mark_resume_success(reward_id=reward_id, cycle_number=cycle_number)
            if updated:
                resumed_ok += 1
                _mark_parent_reward_applied(reward_id=reward_id)

            _log_event(
                logging.INFO,
                "referral_resume_attempt",
                outcome="success" if updated else "stale",
                reward_id=reward_id,
                cycle_number=cycle_number,
                subscription_id=subscription_id,
                pause_id=pause_id,
                provider_status=result.get("status"),
            )
        except Exception as exc:
            marked = _mark_resume_failed(reward_id=reward_id, cycle_number=cycle_number)
            if marked:
                failed_marked += 1
            _log_event(
                logging.ERROR,
                "referral_resume_attempt_failed",
                outcome="retry_resume_failed",
                reward_id=reward_id,
                cycle_number=cycle_number,
                subscription_id=subscription_id,
                pause_id=pause_id,
                error=str(exc)[:500],
            )

    if _is_debug_enabled():
        logger.info(
            "event=referral_pause_resume_debug summary=%s",
            json.dumps(
                {
                    "seeded": seeded,
                    "paused_success": paused_ok,
                    "resumed_success": resumed_ok,
                    "failed_marked": failed_marked,
                    "deferred_pending": deferred_pending,
                    "extended_rewards": extended_rewards,
                    "batch_size": batch_size,
                },
                sort_keys=True,
            ),
        )

    return RunStats(
        seeded_cycles=seeded,
        paused_success=paused_ok,
        resumed_success=resumed_ok,
        failed_marked=failed_marked,
        deferred_pending=deferred_pending,
        extended_rewards=extended_rewards,
    )
