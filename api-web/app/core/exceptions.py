import uuid
import asyncio
import logging
from typing import Optional, Any
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from ..notifications.error_alerts import notify_runtime_error_event
from .request import (
    _request_id_from_request,
    _request_user_id_from_request,
    _request_latency_ms_from_request,
)

logger = logging.getLogger(__name__)

def _runtime_error_context_from_request(request: Request) -> dict[str, Any]:
    client_ip = request.client.host if request.client else ""
    context = {
        "request_id": _request_id_from_request(request),
        "query": request.url.query,
        "client_ip": client_ip,
        "user_agent": (request.headers.get("user-agent") or "")[:200],
        "referer": (request.headers.get("referer") or "")[:200],
    }
    latency_ms = _request_latency_ms_from_request(request)
    if latency_ms is not None:
        context["latency_ms"] = latency_ms
    return context

async def _emit_runtime_error_alert(
    *,
    request: Request,
    error_id: str,
    request_id: str,
    status_code: int,
    message_safe: str,
    message_internal: str,
    extra_context: Optional[dict[str, Any]] = None,
) -> None:
    context = _runtime_error_context_from_request(request)
    if extra_context:
        context.update(extra_context)

    latency_ms = _request_latency_ms_from_request(request)

    try:
        await notify_runtime_error_event(
            error_id=error_id,
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            status_code=status_code,
            user_id=_request_user_id_from_request(request),
            message_safe=message_safe,
            message_internal=message_internal,
            latency_ms=latency_ms,
            context=context,
        )
    except Exception as alert_exc:
        logger.error(
            "Runtime error alert dispatch failed error_id=%s request_id=%s reason=%s",
            error_id,
            request_id,
            alert_exc,
        )

def _http_exception_message(detail: Any, *, fallback: str) -> str:
    if isinstance(detail, str):
        text = detail.strip()
        if text:
            return text[:220]

    if isinstance(detail, dict):
        candidate = detail.get("message")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:220]

    return fallback

async def global_http_exception_handler(request: Request, exc: HTTPException):
    status_code = int(getattr(exc, "status_code", 500) or 500)
    request_id = _request_id_from_request(request)

    if status_code < 500:
        message = _http_exception_message(exc.detail, fallback="Request could not be completed")
        return JSONResponse(
            status_code=status_code,
            content={
                "message": message,
                "detail": exc.detail,
            },
            headers=getattr(exc, "headers", None),
        )

    error_id = f"runtime-{uuid.uuid4().hex[:20]}"
    detail_text = str(getattr(exc, "detail", ""))[:800]
    message = "Internal server error"

    logger.warning(
        "runtime.http_exception.expected error_id=%s request_id=%s method=%s path=%s status=%s detail=%s",
        error_id,
        request_id,
        request.method,
        request.url.path,
        status_code,
        detail_text,
    )

    return JSONResponse(
        status_code=status_code,
        content={
            "message": message,
            "detail": message,
            "error_id": error_id,
            "request_id": request_id,
        },
        headers=getattr(exc, "headers", None),
    )

async def global_unhandled_exception_handler(request: Request, exc: Exception):
    request_id = _request_id_from_request(request)
    error_id = f"runtime-{uuid.uuid4().hex[:20]}"
    message_internal = f"{exc.__class__.__name__}: {str(exc)[:800]}"
    message = "Internal server error"

    logger.error(
        "runtime.unhandled_exception error_id=%s request_id=%s method=%s path=%s",
        error_id,
        request_id,
        request.method,
        request.url.path,
        exc_info=True,
    )

    asyncio.create_task(
        _emit_runtime_error_alert(
            request=request,
            error_id=error_id,
            request_id=request_id,
            status_code=500,
            message_safe="Internal server error",
            message_internal=message_internal,
            extra_context={"exception_type": exc.__class__.__name__},
        )
    )

    return JSONResponse(
        status_code=500,
        content={
            "message": message,
            "detail": message,
            "error_id": error_id,
            "request_id": request_id,
        },
    )
