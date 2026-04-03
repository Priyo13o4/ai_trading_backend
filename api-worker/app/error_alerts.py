import logging
import os
import time
import uuid
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import requests

logger = logging.getLogger(__name__)
_ALERT_DEDUP_CACHE: dict[str, float] = {}
_ALERT_RATE_LIMIT_WINDOW: list[float] = []
_ALERT_CIRCUIT_OPEN_UNTIL: float = 0.0
_ALERT_CIRCUIT_CONSECUTIVE_FAILURES: int = 0

_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9._-]+\.[a-zA-Z0-9._-]+\b")
_LONG_HEX_RE = re.compile(r"\b[a-fA-F0-9]{32,}\b")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([^\s,;]+)")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|token|authorization)\b\s*[:=]\s*([^\s,;]+)"
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return value


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except Exception:
            value = default

    if value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _runtime_environment_name() -> str:
    for env_name in ("APP_ENV", "ENVIRONMENT", "FASTAPI_ENV", "ENV"):
        raw = (os.getenv(env_name) or "").strip()
        if raw:
            return raw.lower()
    return "production"


def _runtime_service_name() -> str:
    return (os.getenv("SERVICE_NAME") or "api-worker").strip() or "api-worker"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_suffix(path_suffix: str) -> str:
    normalized = (path_suffix or "").strip()
    if not normalized:
        return "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _compose_alert_url(base_url: str, path_suffix: str) -> str:
    normalized_suffix = _normalize_suffix(path_suffix)
    kind = normalized_suffix.lstrip("/")
    trimmed_base = (base_url or "").strip().rstrip("/")
    if ":kind" in trimmed_base:
        return trimmed_base.replace(":kind", kind)
    return trimmed_base + normalized_suffix


def _dedup_window_seconds() -> float:
    return max(0.0, _env_float("N8N_ERROR_ALERT_DEDUP_WINDOW_SECONDS", 90.0))


def _rate_limit_window_seconds() -> float:
    return max(0.0, _env_float("N8N_ERROR_ALERT_RATE_LIMIT_WINDOW_SECONDS", 60.0))


def _rate_limit_max_events() -> int:
    return _env_int("N8N_ERROR_ALERT_RATE_LIMIT_MAX_EVENTS", 30, minimum=1, maximum=10000)


def _max_retries() -> int:
    return _env_int("N8N_ERROR_ALERT_MAX_RETRIES", 2, minimum=0, maximum=8)


def _retry_backoff_base_seconds() -> float:
    return _env_float("N8N_ERROR_ALERT_BACKOFF_BASE_SECONDS", 0.5)


def _retry_backoff_max_seconds() -> float:
    return _env_float("N8N_ERROR_ALERT_BACKOFF_MAX_SECONDS", 6.0)


def _circuit_failure_threshold() -> int:
    return _env_int("N8N_ERROR_ALERT_CIRCUIT_FAIL_THRESHOLD", 5, minimum=1, maximum=100)


def _circuit_open_seconds() -> float:
    return _env_float("N8N_ERROR_ALERT_CIRCUIT_OPEN_SECONDS", 60.0)


def _sanitize_internal_message(value: Any) -> str:
    text = _truncate_text(value, 800)
    if not text:
        return ""

    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    text = _LONG_HEX_RE.sub("[REDACTED_HEX]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_KV_RE.sub(r"\1=[REDACTED]", text)
    return text


def _sanitize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    safe_payload = dict(payload)
    safe_payload["message_internal"] = _sanitize_internal_message(payload.get("message_internal"))
    return safe_payload


def _alert_fingerprint(payload: Mapping[str, Any]) -> str:
    context = payload.get("context") if isinstance(payload.get("context"), Mapping) else {}
    raw = "|".join(
        [
            str(payload.get("event_type") or ""),
            str(payload.get("service") or ""),
            str(payload.get("path") or ""),
            str(payload.get("method") or ""),
            str(payload.get("status_code") or ""),
            str(payload.get("severity") or ""),
            str(payload.get("environment") or ""),
            str(context.get("exception_type") or ""),
            str(context.get("script") or ""),
            str(context.get("phase") or ""),
            str(context.get("provider") or ""),
            str(context.get("provider_event_type") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _purge_expired_dedup_entries(now: float, window: float) -> None:
    cutoff = now - window
    for key, ts in list(_ALERT_DEDUP_CACHE.items()):
        if ts < cutoff:
            _ALERT_DEDUP_CACHE.pop(key, None)


def _should_suppress_duplicate(fingerprint: str) -> bool:
    window = _dedup_window_seconds()
    if window <= 0:
        return False

    now = time.monotonic()
    _purge_expired_dedup_entries(now, window)
    last_sent = _ALERT_DEDUP_CACHE.get(fingerprint)
    if last_sent is not None and (now - last_sent) < window:
        return True

    return False


def _mark_sent_for_dedup(fingerprint: str) -> None:
    window = _dedup_window_seconds()
    if window <= 0:
        return
    now = time.monotonic()
    _purge_expired_dedup_entries(now, window)
    _ALERT_DEDUP_CACHE[fingerprint] = now


def _allow_dispatch_under_rate_limit() -> bool:
    now = time.monotonic()
    window_seconds = _rate_limit_window_seconds()
    if window_seconds <= 0:
        return True

    cutoff = now - window_seconds
    while _ALERT_RATE_LIMIT_WINDOW and _ALERT_RATE_LIMIT_WINDOW[0] < cutoff:
        _ALERT_RATE_LIMIT_WINDOW.pop(0)

    if len(_ALERT_RATE_LIMIT_WINDOW) >= _rate_limit_max_events():
        return False

    _ALERT_RATE_LIMIT_WINDOW.append(now)
    return True


def _is_circuit_open() -> bool:
    return time.monotonic() < _ALERT_CIRCUIT_OPEN_UNTIL


def _record_circuit_success() -> None:
    global _ALERT_CIRCUIT_CONSECUTIVE_FAILURES, _ALERT_CIRCUIT_OPEN_UNTIL
    _ALERT_CIRCUIT_CONSECUTIVE_FAILURES = 0
    _ALERT_CIRCUIT_OPEN_UNTIL = 0.0


def _record_circuit_failure() -> None:
    global _ALERT_CIRCUIT_CONSECUTIVE_FAILURES, _ALERT_CIRCUIT_OPEN_UNTIL
    _ALERT_CIRCUIT_CONSECUTIVE_FAILURES += 1
    threshold = _circuit_failure_threshold()
    if _ALERT_CIRCUIT_CONSECUTIVE_FAILURES >= threshold:
        _ALERT_CIRCUIT_OPEN_UNTIL = time.monotonic() + _circuit_open_seconds()
        logger.error(
            "Worker error alert circuit opened failures=%s open_seconds=%s",
            _ALERT_CIRCUIT_CONSECUTIVE_FAILURES,
            _circuit_open_seconds(),
        )


def _truncate_text(value: Any, limit: int = 600) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit]


def post_error_alert(path_suffix: str, payload: Mapping[str, Any]) -> bool:
    if not _env_bool("N8N_ERROR_ALERT_ENABLED", False):
        return True

    safe_payload = _sanitize_payload(payload)
    fingerprint = _alert_fingerprint(safe_payload)
    if _should_suppress_duplicate(fingerprint):
        logger.info(
            "Suppressed duplicate worker runtime alert service=%s path=%s",
            safe_payload.get("service"),
            safe_payload.get("path"),
        )
        return True

    if not _allow_dispatch_under_rate_limit():
        logger.warning(
            "Dropped worker alert due to local rate limit event_type=%s service=%s path=%s",
            safe_payload.get("event_type"),
            safe_payload.get("service"),
            safe_payload.get("path"),
        )
        return False

    if _is_circuit_open():
        logger.warning(
            "Dropped worker alert while circuit open event_type=%s service=%s path=%s",
            safe_payload.get("event_type"),
            safe_payload.get("service"),
            safe_payload.get("path"),
        )
        return False

    base_url = (os.getenv("N8N_ERROR_ALERT_BASE_URL") or "").strip()
    if not base_url:
        logger.warning("N8N_ERROR_ALERT_ENABLED=1 but N8N_ERROR_ALERT_BASE_URL is empty")
        return False

    url = _compose_alert_url(base_url, path_suffix)
    timeout_seconds = max(0.1, _env_float("N8N_ERROR_ALERT_TIMEOUT_SECONDS", 5.0))

    headers = {"Content-Type": "application/json"}
    secret = (os.getenv("N8N_ERROR_ALERT_SECRET") or "").strip()
    if secret:
        headers["X-Error-Alert-Secret"] = secret

    retries = _max_retries()
    attempts = retries + 1
    backoff_base = _retry_backoff_base_seconds()
    backoff_max = _retry_backoff_max_seconds()

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=safe_payload, headers=headers, timeout=timeout_seconds)
            if 200 <= response.status_code < 300:
                _record_circuit_success()
                _mark_sent_for_dedup(fingerprint)
                return True

            logger.error(
                "Worker error alert webhook failed attempt=%s/%s status=%s url=%s body=%s",
                attempt,
                attempts,
                response.status_code,
                url,
                _truncate_text(response.text, 300),
            )
        except Exception as exc:
            logger.error(
                "Worker error alert webhook request failed attempt=%s/%s url=%s error=%s",
                attempt,
                attempts,
                url,
                exc,
            )

        if attempt < attempts:
            backoff_seconds = min(backoff_max, backoff_base * (2 ** (attempt - 1)))
            time.sleep(backoff_seconds)

    _record_circuit_failure()
    logger.critical(
        "Fail-safe: dropping worker alert after retries event_type=%s service=%s path=%s error_id=%s request_id=%s",
        safe_payload.get("event_type"),
        safe_payload.get("service"),
        safe_payload.get("path"),
        safe_payload.get("error_id"),
        safe_payload.get("request_id"),
    )
    return False


def report_runtime_error(
    *,
    path: str,
    method: str,
    status_code: int,
    message_safe: str,
    message_internal: str,
    context: Optional[Mapping[str, Any]] = None,
    severity: str = "critical",
    error_id: Optional[str] = None,
    request_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    payload: dict[str, Any] = {
        "event_type": "runtime_error",
        "service": _runtime_service_name(),
        "environment": _runtime_environment_name(),
        "severity": severity,
        "error_id": error_id or f"runtime-{uuid.uuid4().hex[:20]}",
        "request_id": request_id or f"worker-{uuid.uuid4().hex[:12]}",
        "timestamp": _now_iso(),
        "path": path,
        "method": (method or "PROCESS").upper(),
        "status_code": int(status_code),
        "message_safe": _truncate_text(message_safe),
        "message_internal": _sanitize_internal_message(message_internal),
        "context": dict(context or {}),
    }

    if user_id:
        payload["user_id"] = user_id

    return post_error_alert("/runtime-error", payload)