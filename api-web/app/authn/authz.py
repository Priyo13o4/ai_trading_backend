import logging
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def require_permission(ctx: dict[str, Any], permission: str) -> None:
    perms = ctx.get("permissions") or []
    if permission not in perms:
        logger.info("Permission denied user=%s perm=%s", ctx.get("user_id"), permission)
        raise HTTPException(status_code=403, detail="Forbidden")
