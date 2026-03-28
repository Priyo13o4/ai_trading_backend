import logging
import os
from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

JWKS_URL = os.getenv("SUPABASE_JWKS_URL")
AUD = os.getenv("SUPABASE_AUDIENCE", "authenticated")

_jwks_client: PyJWKClient | None = None
if JWKS_URL:
    try:
        _jwks_client = PyJWKClient(JWKS_URL)
    except Exception as err:
        logger.error("Failed to init JWKS client: %s", err)
        _jwks_client = None


async def verify_supabase_access_token(token: str) -> dict[str, Any]:
    if not _jwks_client:
        raise HTTPException(status_code=500, detail="JWKS client not configured")

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=AUD,
        )
        return claims
    except HTTPException:
        raise
    except Exception as err:
        logger.info("JWKS verification failed: %s", err)
        raise HTTPException(status_code=401, detail="Invalid token")
