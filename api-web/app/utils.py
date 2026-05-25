import json
from datetime import datetime, timezone
from typing import Optional

def _custom_serializer(obj):
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            return obj.isoformat() + "Z"
        return obj.isoformat()
    return str(obj)

def json_dumps(obj): 
    return json.dumps(obj, default=_custom_serializer)

STRATEGY_CACHE_MAX_TTL_SECONDS = 300
STRATEGY_CACHE_MIN_TTL_SECONDS = 60
PREVIEW_SUPPORTED_PAIRS = {"XAUUSD", "BTCUSD", "EURUSD"}

def _normalize_optional_query_value(value: Optional[str], *, lowercase: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized:
        return None

    return normalized.lower() if lowercase else normalized

def _seconds_to_expiry(expiry_value) -> Optional[int]:
    if not expiry_value:
        return None

    try:
        if isinstance(expiry_value, datetime):
            expiry_dt = expiry_value
        elif isinstance(expiry_value, str):
            parsed = expiry_value.strip().replace("Z", "+00:00")
            expiry_dt = datetime.fromisoformat(parsed)
        else:
            return None

        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)
        return int((expiry_dt - now_utc).total_seconds())
    except Exception:
        return None

def _strategy_cache_ttl(rows: list[dict]) -> int:
    seconds_candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        seconds = _seconds_to_expiry(row.get("expiry_time"))
        if seconds is not None:
            seconds_candidates.append(seconds)

    if not seconds_candidates:
        return STRATEGY_CACHE_MAX_TTL_SECONDS

    seconds_to_earliest_expiry = min(seconds_candidates)
    return min(
        STRATEGY_CACHE_MAX_TTL_SECONDS,
        max(STRATEGY_CACHE_MIN_TTL_SECONDS, seconds_to_earliest_expiry),
    )