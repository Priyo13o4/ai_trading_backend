"""Shared utilities for the referrals package.

Single source of truth for common helpers previously duplicated across
reward_evaluator.py, reward_revocation.py, manual_activation.py, and routes/referrals.py.
"""

import uuid
from typing import Optional


def validate_uuid(raw: object) -> Optional[str]:
    """Validate and normalize a UUID string. Returns None if invalid."""
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, TypeError, AttributeError):
        return None
