"""Referral reward pause/resume state machine orchestration.

This module powers Scope E for referral rewards that grant free months by pausing
and later resuming Razorpay subscriptions.

Flow summary:
1. Seed cycle-1 rows for rewards in `claimed` status.
2. Promote rewards into pause phase and pause at provider level.
3. Resume paused cycles whose `free_access_until` has elapsed.
4. Extend active pause windows for stacked rewards instead of chaining new
    provider pauses.

The worker is designed to be idempotent and retry-safe:
- Pause failures map to `pause_failed` (retryable).
- Resume failures map to `resume_failed` (retryable).
- Writes are guarded by transactions and row-level status checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import requests
from psycopg.rows import dict_row

from app.db import POSTGRES_DSN

logger = logging.getLogger(__name__)

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
_DEFAULT_BATCH_SIZE = 100


def _is_debug_enabled() -> bool:
    return (os.getenv("AUTHDBG_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y", "on"}


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
            raise RuntimeError("Razorpay credentials are not configured")

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
            raise RuntimeError(f"pause_failed status={response.status_code} body={response.text[:500]}")

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
            raise RuntimeError(f"resume_failed status={response.status_code} body={response.text[:500]}")

        body = response.json() if response.content else {}
        return {
            "status": str(body.get("status") or "active"),
            "raw": body,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _seed_pending_cycles(conn: psycopg.Connection[Any]) -> int:
    """Create cycle-1 reward_pending rows for newly claimed rewards.

    The query is idempotent because `ON CONFLICT DO NOTHING` is keyed on
    `(reward_id, cycle_number)`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest_subscriptions AS (
                SELECT DISTINCT ON (us.user_id)
                    us.user_id,
                                        us.external_subscription_id,
                                        us.last_payment_date AS last_charge_at,
                                        COALESCE(us.next_billing_date, us.expires_at) AS next_charge_at
                FROM public.user_subscriptions us
                WHERE us.payment_provider = 'razorpay'
                  AND us.external_subscription_id IS NOT NULL
                ORDER BY us.user_id, us.updated_at DESC, us.created_at DESC
            )
            INSERT INTO public.referral_reward_pause_cycles (
                reward_id,
                cycle_number,
                status,
                razorpay_subscription_id,
                last_charge_at,
                next_charge_at,
                created_at,
                updated_at
            )
            SELECT
                rr.referral_id,
                1,
                'reward_pending'::public.referral_pause_cycle_status,
                ls.external_subscription_id,
                ls.last_charge_at,
                ls.next_charge_at,
                NOW(),
                NOW()
            FROM public.referral_rewards rr
            INNER JOIN latest_subscriptions ls
                ON ls.user_id = rr.user_id
            WHERE rr.status = 'claimed'
            ON CONFLICT (reward_id, cycle_number) DO NOTHING
            """
        )
        return cur.rowcount or 0


def _claim_pending_cycles(conn: psycopg.Connection[Any], *, batch_size: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH claimable AS (
                SELECT
                    reward_id,
                    cycle_number
                FROM public.referral_reward_pause_cycles
                WHERE status IN ('reward_pending', 'pause_pending', 'pause_failed')
                  AND (pause_deferred_until IS NULL OR pause_deferred_until <= NOW())
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE public.referral_reward_pause_cycles c
            SET status = 'pause_pending',
                updated_at = NOW()
            FROM claimable q
            WHERE c.reward_id = q.reward_id
              AND c.cycle_number = q.cycle_number
            RETURNING
                reward_id,
                cycle_number,
                razorpay_subscription_id,
                last_charge_at,
                next_charge_at,
                created_at
            """,
            (batch_size,),
        )
        return [dict(row) for row in (cur.fetchall() or [])]


def _claim_due_resumes(conn: psycopg.Connection[Any], *, batch_size: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH claimable AS (
                SELECT
                    reward_id,
                    cycle_number
                FROM public.referral_reward_pause_cycles
                WHERE status IN ('paused', 'resume_pending', 'resume_failed')
                  AND pause_confirmed = TRUE
                  AND COALESCE(free_access_until, pause_end_time) IS NOT NULL
                  AND COALESCE(free_access_until, pause_end_time) <= NOW()
                ORDER BY COALESCE(free_access_until, pause_end_time) ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE public.referral_reward_pause_cycles c
            SET status = 'resume_pending',
                updated_at = NOW()
            FROM claimable q
            WHERE c.reward_id = q.reward_id
              AND c.cycle_number = q.cycle_number
            RETURNING
                reward_id,
                cycle_number,
                razorpay_pause_id,
                razorpay_subscription_id,
                pause_confirmed,
                free_access_until,
                pause_end_time
            """,
            (batch_size,),
        )
        return [dict(row) for row in (cur.fetchall() or [])]


def _mark_pause_success(
    conn: psycopg.Connection[Any],
    *,
    reward_id: str,
    cycle_number: int,
    pause_id: str | None,
    pause_start_time: datetime,
    pause_end_time: datetime,
    cycle_duration_seconds: int,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.referral_reward_pause_cycles
            SET status = 'paused',
                razorpay_pause_id = COALESCE(%s, razorpay_pause_id),
                pause_start_time = %s,
                pause_end_time = %s,
                pause_confirmed = TRUE,
                free_access_until = %s,
                cycle_duration_seconds = %s,
                pause_deferred_until = NULL,
                updated_at = NOW()
            WHERE reward_id = %s
              AND cycle_number = %s
                            AND status = 'pause_pending'
            """,
            (
                pause_id,
                pause_start_time,
                pause_end_time,
                pause_end_time,
                cycle_duration_seconds,
                reward_id,
                cycle_number,
            ),
        )
        return (cur.rowcount or 0) > 0


def _mark_pause_deferred(
    conn: psycopg.Connection[Any],
    *,
    reward_id: str,
    cycle_number: int,
    defer_until: datetime,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.referral_reward_pause_cycles
            SET pause_deferred_until = %s,
                updated_at = NOW()
            WHERE reward_id = %s
              AND cycle_number = %s
                            AND status = 'pause_pending'
            """,
            (defer_until, reward_id, cycle_number),
        )
        return (cur.rowcount or 0) > 0


def _mark_resume_success(conn: psycopg.Connection[Any], *, reward_id: str, cycle_number: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.referral_reward_pause_cycles
            SET status = 'resumed',
                                pause_confirmed = FALSE,
                                free_access_until = NULL,
                updated_at = NOW()
            WHERE reward_id = %s
              AND cycle_number = %s
                            AND status = 'resume_pending'
            """,
            (reward_id, cycle_number),
        )
        return (cur.rowcount or 0) > 0


def _mark_pause_failed(conn: psycopg.Connection[Any], *, reward_id: str, cycle_number: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.referral_reward_pause_cycles
                        SET status = 'pause_failed',
                updated_at = NOW()
            WHERE reward_id = %s
              AND cycle_number = %s
                            AND status = 'pause_pending'
            """,
            (reward_id, cycle_number),
        )
        return (cur.rowcount or 0) > 0


def _mark_resume_failed(conn: psycopg.Connection[Any], *, reward_id: str, cycle_number: int) -> bool:
        with conn.cursor() as cur:
                cur.execute(
                        """
                        UPDATE public.referral_reward_pause_cycles
                        SET status = 'resume_failed',
                                updated_at = NOW()
                        WHERE reward_id = %s
                            AND cycle_number = %s
                            AND status = 'resume_pending'
                        """,
                        (reward_id, cycle_number),
                )
                return (cur.rowcount or 0) > 0


def _find_open_pause_window(
    conn: psycopg.Connection[Any],
    *,
    subscription_id: str,
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                reward_id,
                cycle_number,
                COALESCE(free_access_until, pause_end_time) AS resume_time
            FROM public.referral_reward_pause_cycles
            WHERE razorpay_subscription_id = %s
              AND status = 'paused'
              AND pause_confirmed = TRUE
              AND COALESCE(free_access_until, pause_end_time) > NOW()
            ORDER BY COALESCE(free_access_until, pause_end_time) DESC
            LIMIT 1
            FOR UPDATE
            """,
            (subscription_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _extend_pause_window(
    conn: psycopg.Connection[Any],
    *,
    reward_id: str,
    cycle_number: int,
    extension_seconds: int,
) -> datetime | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE public.referral_reward_pause_cycles
            SET pause_end_time = COALESCE(free_access_until, pause_end_time) + (%s * INTERVAL '1 second'),
                free_access_until = COALESCE(free_access_until, pause_end_time) + (%s * INTERVAL '1 second'),
                updated_at = NOW()
            WHERE reward_id = %s
              AND cycle_number = %s
              AND status = 'paused'
              AND pause_confirmed = TRUE
            RETURNING COALESCE(free_access_until, pause_end_time) AS resume_time
            """,
            (extension_seconds, extension_seconds, reward_id, cycle_number),
        )
        row = cur.fetchone()

    if not row:
        return None
    return _parse_timestamp(row.get("resume_time"))


def _mark_reward_consumed_by_extension(
    conn: psycopg.Connection[Any],
    *,
    reward_id: str,
    cycle_number: int,
    resume_time: datetime,
    cycle_duration_seconds: int,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.referral_reward_pause_cycles
            SET status = 'resumed',
                pause_confirmed = TRUE,
                pause_start_time = NOW(),
                pause_end_time = %s,
                free_access_until = %s,
                cycle_duration_seconds = %s,
                updated_at = NOW()
            WHERE reward_id = %s
              AND cycle_number = %s
                            AND status = 'pause_pending'
            """,
            (resume_time, resume_time, cycle_duration_seconds, reward_id, cycle_number),
        )
        return (cur.rowcount or 0) > 0


def run_referral_pause_resume_cycle() -> RunStats:
    """Run one end-to-end pause/resume reconciliation cycle.

    Returns:
        RunStats summarizing seeded, paused, and resumed work.
    """
    batch_size = max(1, _env_int("REFERRAL_PAUSE_RESUME_BATCH_SIZE", _DEFAULT_BATCH_SIZE))
    pause_lead_seconds = max(0, _env_int("REFERRAL_PAUSE_RESUME_LEAD_SECONDS", 120))

    client = RazorpayPauseResumeClient()
    seeded = 0
    paused_ok = 0
    resumed_ok = 0
    failed_marked = 0
    deferred_pending = 0
    extended_rewards = 0

    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        conn.autocommit = False

        try:
            seeded = _seed_pending_cycles(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        pending_rows = _claim_pending_cycles(conn, batch_size=batch_size)
        conn.commit()

        for row in pending_rows:
            reward_id = str(row.get("reward_id") or "")
            cycle_number = int(row.get("cycle_number") or 0)
            subscription_id = str(row.get("razorpay_subscription_id") or "").strip()

            if not reward_id or cycle_number <= 0 or not subscription_id:
                failed = _mark_pause_failed(conn, reward_id=reward_id, cycle_number=cycle_number)
                conn.commit()
                failed_marked += 1 if failed else 0
                _log_event(
                    logging.ERROR,
                    "referral_pause_attempt",
                    outcome="pause_failed_data_error",
                    reward_id=reward_id,
                    cycle_number=cycle_number,
                    subscription_id=subscription_id or "none",
                )
                continue

            now_utc = _utc_now()
            cycle_duration_seconds = _derive_cycle_duration_seconds(
                row.get("last_charge_at"),
                row.get("next_charge_at"),
            )
            if cycle_duration_seconds is None:
                _log_event(
                    logging.WARNING,
                    "referral_pause_attempt",
                    outcome="retry_pending_missing_cycle_duration",
                    reward_id=reward_id,
                    cycle_number=cycle_number,
                    subscription_id=subscription_id,
                )
                continue

            total_extension_seconds = cycle_duration_seconds * _reward_cycle_target()

            open_pause = _find_open_pause_window(conn, subscription_id=subscription_id)
            if open_pause:
                open_reward_id = str(open_pause.get("reward_id") or "")
                open_cycle_number = int(open_pause.get("cycle_number") or 0)
                if open_reward_id and open_cycle_number > 0:
                    extended_until = _extend_pause_window(
                        conn,
                        reward_id=open_reward_id,
                        cycle_number=open_cycle_number,
                        extension_seconds=total_extension_seconds,
                    )
                    if extended_until:
                        consumed = _mark_reward_consumed_by_extension(
                            conn,
                            reward_id=reward_id,
                            cycle_number=cycle_number,
                            resume_time=extended_until,
                            cycle_duration_seconds=cycle_duration_seconds,
                        )
                        conn.commit()
                        if consumed:
                            extended_rewards += 1
                        _log_event(
                            logging.INFO,
                            "referral_pause_attempt",
                            outcome="extended_existing_pause" if consumed else "stale",
                            reward_id=reward_id,
                            cycle_number=cycle_number,
                            subscription_id=subscription_id,
                            extended_until=extended_until.isoformat(),
                        )
                        continue
                conn.rollback()

            if _should_defer_pause(row.get("next_charge_at"), now_utc):
                next_charge = _parse_timestamp(row.get("next_charge_at"))
                if next_charge:
                    deferred = _mark_pause_deferred(
                        conn,
                        reward_id=reward_id,
                        cycle_number=cycle_number,
                        defer_until=next_charge + timedelta(minutes=5),
                    )
                    conn.commit()
                    if deferred:
                        deferred_pending += 1
                    _log_event(
                        logging.INFO,
                        "referral_pause_attempt",
                        outcome="deferred_near_renewal",
                        reward_id=reward_id,
                        cycle_number=cycle_number,
                        subscription_id=subscription_id,
                        next_charge_at=next_charge.isoformat(),
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
                    conn,
                    reward_id=reward_id,
                    cycle_number=cycle_number,
                    pause_id=str(pause_id) if pause_id else None,
                    pause_start_time=now_utc,
                    pause_end_time=pause_end,
                    cycle_duration_seconds=cycle_duration_seconds,
                )
                conn.commit()

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
                    pause_end_time=pause_end.isoformat(),
                )
            except Exception as exc:
                conn.rollback()
                failed = _mark_pause_failed(conn, reward_id=reward_id, cycle_number=cycle_number)
                conn.commit()
                failed_marked += 1 if failed else 0
                _log_event(
                    logging.ERROR,
                    "referral_pause_attempt_failed",
                    outcome="retry_pause_failed",
                    reward_id=reward_id,
                    cycle_number=cycle_number,
                    subscription_id=subscription_id,
                    error=str(exc)[:500],
                )

        paused_rows = _claim_due_resumes(conn, batch_size=batch_size)
        conn.commit()

        for row in paused_rows:
            reward_id = str(row.get("reward_id") or "")
            cycle_number = int(row.get("cycle_number") or 0)
            subscription_id = str(row.get("razorpay_subscription_id") or "").strip()
            pause_id = row.get("razorpay_pause_id")
            idempotency_key = _build_idempotency_key("resume", reward_id, cycle_number)

            if not reward_id or cycle_number <= 0 or not subscription_id:
                failed = _mark_resume_failed(conn, reward_id=reward_id, cycle_number=cycle_number)
                conn.commit()
                failed_marked += 1 if failed else 0
                _log_event(
                    logging.ERROR,
                    "referral_resume_attempt",
                    outcome="resume_failed_data_error",
                    reward_id=reward_id,
                    cycle_number=cycle_number,
                    subscription_id=subscription_id or "none",
                )
                continue

            try:
                result = client.resume_subscription(
                    subscription_id,
                    str(pause_id) if pause_id else None,
                    idempotency_key=idempotency_key,
                )
                updated = _mark_resume_success(conn, reward_id=reward_id, cycle_number=cycle_number)
                conn.commit()

                if updated:
                    resumed_ok += 1
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
                conn.rollback()
                failed = _mark_resume_failed(conn, reward_id=reward_id, cycle_number=cycle_number)
                conn.commit()
                failed_marked += 1 if failed else 0
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
