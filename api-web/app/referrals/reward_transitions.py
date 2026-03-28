"""Referral rewards transition logic - Scope C implementation.

Provides functions to:
1. Transition on_hold -> available when hold_expires_at <= now (UTC)
2. Transition available -> applied

All functions are idempotent and safe to call repeatedly.
Debug logging is tied to AUTHDBG_ENABLED environment variable.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.db import async_db, get_supabase_client

logger = logging.getLogger(__name__)


def _is_debug_enabled() -> bool:
    """Check if AUTHDBG_ENABLED is set."""
    return os.getenv("AUTHDBG_ENABLED", "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _debug_log(message: str) -> None:
    """Log info message if AUTHDBG_ENABLED is set."""
    if _is_debug_enabled():
        logger.info("[REFERRAL_REWARDS] %s", message)


@dataclass(frozen=True)
class TransitionResult:
    """Result of a referral reward state transition."""
    outcome: str  # success, error_controlled, feature_disabled
    transitioned_count: int = 0
    error_message: str | None = None


async def transition_rewards_on_hold_to_available() -> TransitionResult:
    """
    Batch transition referral_rewards from on_hold to available.
    
    Candidates: status='on_hold' AND hold_expires_at <= NOW() (UTC)
    
    Returns:
        TransitionResult with outcome and transitioned_count
        
    Idempotent: Safe to call repeatedly. Only updates qualifying rows.
    """
    _debug_log("transition_rewards_on_hold_to_available() starting")
    
    supabase = get_supabase_client()
    
    try:
        response = await async_db(
            lambda: supabase.rpc('transition_rewards_on_hold_to_available').execute()
        )
        
        data = getattr(response, 'data', None)
        if isinstance(data, list) and data:
            row = data[0] if isinstance(data[0], dict) else None
        elif isinstance(data, dict):
            row = data
        else:
            row = None
        
        if not row:
            _debug_log("transition_rewards_on_hold_to_available() returned empty result")
            return TransitionResult(outcome='error_controlled', transitioned_count=0)
        
        result_code = str(row.get('result_code', '')).strip()
        transitioned_count = int(row.get('transitioned_count', 0))
        
        if result_code == 'success':
            _debug_log(f"transition_rewards_on_hold_to_available() transitioned {transitioned_count} rewards")
            return TransitionResult(
                outcome='success',
                transitioned_count=transitioned_count,
            )
        else:
            _debug_log(f"transition_rewards_on_hold_to_available() unexpected result_code: {result_code}")
            return TransitionResult(outcome='error_controlled', transitioned_count=0)
            
    except Exception as e:
        logger.exception("transition_rewards_on_hold_to_available() failed")
        _debug_log(f"transition_rewards_on_hold_to_available() exception: {type(e).__name__}")
        return TransitionResult(
            outcome='error_controlled',
            transitioned_count=0,
            error_message=str(e),
        )


async def apply_available_rewards() -> TransitionResult:
    """
    Batch transition referral_rewards from available to applied.
    
    Candidates: status='available'
    
    Returns:
        TransitionResult with outcome and transitioned_count
        
    Idempotent: Safe to call repeatedly. Only updates qualifying rows.
    No-op if no available rewards exist.
    """
    _debug_log("apply_available_rewards() starting")
    
    supabase = get_supabase_client()
    
    try:
        response = await async_db(
            lambda: supabase.rpc('apply_available_rewards').execute()
        )
        
        data = getattr(response, 'data', None)
        if isinstance(data, list) and data:
            row = data[0] if isinstance(data[0], dict) else None
        elif isinstance(data, dict):
            row = data
        else:
            row = None
        
        if not row:
            _debug_log("apply_available_rewards() returned empty result")
            return TransitionResult(outcome='error_controlled', transitioned_count=0)
        
        result_code = str(row.get('result_code', '')).strip()
        applied_count = int(row.get('applied_count', 0))
        
        if result_code == 'success':
            _debug_log(f"apply_available_rewards() applied {applied_count} rewards")
            return TransitionResult(
                outcome='success',
                transitioned_count=applied_count,
            )
        else:
            _debug_log(f"apply_available_rewards() unexpected result_code: {result_code}")
            return TransitionResult(outcome='error_controlled', transitioned_count=0)
            
    except Exception as e:
        logger.exception("apply_available_rewards() failed")
        _debug_log(f"apply_available_rewards() exception: {type(e).__name__}")
        return TransitionResult(
            outcome='error_controlled',
            transitioned_count=0,
            error_message=str(e),
        )
