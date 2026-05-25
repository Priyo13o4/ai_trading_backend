import os
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..observability.debug import debug_log
from ..cors_env import cors_origin_regex_from_env, parse_cors_origins_from_env

logger = logging.getLogger(__name__)

def _cors_allow_headers() -> list[str]:
    required_headers = {
        "Content-Type",
        "X-CSRF-Token",
        "X-Request-ID",
        "X-Correlation-ID",
        "Authorization",
        "X-Requested-With",
        "X-API-Key",
    }

    raw = (os.getenv("ALLOWED_CORS_HEADERS") or "").strip()
    if raw:
        extras = {h.strip() for h in raw.split(",") if h.strip()}
        return sorted(required_headers | extras)

    # Keep this list tight to known frontend/internal usage.
    return sorted(required_headers)

def _authdbg(message: str, *args) -> None:
    debug_log(logger, "cors", message, *args)

def setup_cors(app: FastAPI) -> None:
    CORS_ALLOW_ORIGINS = parse_cors_origins_from_env()
    CORS_ALLOW_ORIGIN_REGEX = cors_origin_regex_from_env()
    CORS_ALLOW_HEADERS = _cors_allow_headers()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=CORS_ALLOW_HEADERS,
    )
