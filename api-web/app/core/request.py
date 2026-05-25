import time
import uuid
from typing import Optional
from fastapi import Request

def _request_id_from_request(request: Request) -> str:
    from_state = (getattr(request.state, "request_id", "") or "").strip()
    if from_state:
        return from_state

    return (
        (request.headers.get("x-request-id") or "").strip()
        or (request.headers.get("x-correlation-id") or "").strip()
        or uuid.uuid4().hex[:12]
    )

def _request_user_id_from_request(request: Request) -> Optional[str]:
    state_user_id = (getattr(request.state, "user_id", "") or "").strip()
    if state_user_id:
        return state_user_id
    return None

def _request_latency_ms_from_request(request: Request) -> Optional[float]:
    raw_latency = getattr(request.state, "latency_ms", None)
    if raw_latency is not None:
        try:
            return max(0.0, round(float(raw_latency), 2))
        except Exception:
            pass

    start = getattr(request.state, "request_started_monotonic", None)
    if start is None:
        return None

    try:
        return max(0.0, round((time.perf_counter() - float(start)) * 1000.0, 2))
    except Exception:
        return None
