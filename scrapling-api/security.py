import os
import hmac
from aiohttp import web

def verify_api_key(request, *env_var_names: str) -> str:
    """
    Verifies that the request contains an X-API-Key header matching one of the expected environment variables.
    Uses hmac.compare_digest to prevent timing attacks.
    """
    provided_key = (request.headers.get("X-API-Key") or "").strip()
    if not provided_key:
        raise web.HTTPUnauthorized(reason="Missing X-API-Key")

    for env_name in env_var_names:
        expected_key = (os.getenv(env_name) or "").strip()
        if expected_key and hmac.compare_digest(provided_key, expected_key):
            return env_name

    raise web.HTTPUnauthorized(reason="Unauthorized API Key")
