import json
import os

LOCAL_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]
LOCAL_ALLOWED_ORIGIN_REGEX = r"^https?://(localhost|127[.]0[.]0[.]1)(:[0-9]{1,5})?$"
LOCAL_ENV_VALUES = {"local", "development", "dev", "test", "testing"}


def _runtime_environment_name() -> str:
    for env_name in ("APP_ENV", "AUTH_ENV", "ENVIRONMENT", "FASTAPI_ENV", "ENV"):
        raw = (os.getenv(env_name) or "").strip()
        if raw:
            return raw.lower()
    return "production"


def _is_local_environment() -> bool:
    return _runtime_environment_name() in LOCAL_ENV_VALUES


def parse_cors_origins_from_env() -> list[str]:
    raw = (os.getenv("ALLOWED_ORIGINS") or "").strip()
    if not raw:
        if _is_local_environment():
            return list(LOCAL_ALLOWED_ORIGINS)
        raise RuntimeError(
            "ALLOWED_ORIGINS must be set for non-local environments"
        )

    try:
        if raw.startswith("["):
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
    except Exception:
        pass

    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if origins:
        return origins
    if _is_local_environment():
        return list(LOCAL_ALLOWED_ORIGINS)
    raise RuntimeError(
        "ALLOWED_ORIGINS resolved to an empty list for non-local environment"
    )


def cors_origin_regex_from_env() -> str:
    raw = (os.getenv("ALLOWED_ORIGIN_REGEX") or "").strip()
    if raw:
        return raw
    if _is_local_environment():
        return LOCAL_ALLOWED_ORIGIN_REGEX
    raise RuntimeError(
        "ALLOWED_ORIGIN_REGEX must be set for non-local environments"
    )