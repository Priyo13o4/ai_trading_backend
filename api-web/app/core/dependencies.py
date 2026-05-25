import os
import hmac
from fastapi import Request, HTTPException, Depends

from app.authn.deps import require_session
from app.authn.authz import require_permission

def _require_internal_api_key(request: Request, *env_var_names: str) -> str:
    provided = (request.headers.get("X-API-Key") or "").strip()
    if not provided:
        raise HTTPException(401, "Missing X-API-Key")

    for env_name in env_var_names:
        expected = (os.getenv(env_name) or "").strip()
        if expected and hmac.compare_digest(provided, expected):
            return env_name

    raise HTTPException(401, "Unauthorized")

async def require_signals_context(ctx=Depends(require_session)):
    require_permission(ctx, "signals")
    return ctx
