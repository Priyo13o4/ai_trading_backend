"""
Trading calendar utilities for forex/gold.

Provides deterministic open/close evaluation that does not depend on
live API calls, with explicit metadata health state to avoid silent
degradation.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import List, Optional, Tuple


class MetadataHealth(str, Enum):
    FULL = "full"          # Holiday metadata fresh
    DEGRADED = "degraded"  # Holiday metadata present but stale
    OFFLINE = "offline"    # No holiday metadata available


@dataclass
class MarketWindow:
    is_open: bool
    reason: str
    health: MetadataHealth
    window_start: datetime
    window_end: datetime
    holiday: Optional[str] = None
    metadata_age_seconds: Optional[float] = None


@dataclass
class TimestampValidation:
    """Result of timestamp validation with explicit confidence and scope."""
    is_valid: bool
    reason: str
    confidence_level: str  # "high" (FULL metadata), "medium" (DEGRADED), "low" (OFFLINE)
    validation_scope: str  # Description of what was validated
    metadata_health: MetadataHealth
    timestamp: datetime


def _fx_session_bounds(now: datetime) -> Tuple[datetime, datetime]:
    """Return current weekly forex session bounds in UTC.

    Forex session opens Sunday 22:00 UTC and closes Friday 22:00 UTC.
    
    DST Handling:
    - Uses fixed 22:00 UTC regardless of US DST (aligns with Twelve Data API)
    - New York local time varies: 17:00 EST (winter) or 17:00 EDT (summer)
    - During EDT (Mar-Nov), NY 17:00 = 21:00 UTC, but canonical forex hours start at 22:00 UTC
    - Some MT5 brokers may include 21:00-22:00 UTC hour during EDT - we filter this out
    - Our implementation matches major data providers (Twelve Data) using 22:00 UTC year-round
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Find most recent Sunday
    days_since_sunday = (now.weekday() - 6) % 7
    session_start_date = (now - timedelta(days=days_since_sunday)).date()
    session_start = datetime.combine(session_start_date, time(hour=22), tzinfo=timezone.utc)
    session_end = session_start + timedelta(days=5)
    return session_start, session_end


def _classify_metadata_health(now: datetime, cached_at: Optional[datetime], metadata_present: bool, ttl_seconds: int) -> Tuple[MetadataHealth, Optional[float]]:
    if not metadata_present:
        return MetadataHealth.OFFLINE, None

    if cached_at is None:
        return MetadataHealth.DEGRADED, None

    # Ensure both datetimes are timezone-aware for comparison
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    
    age = (now - cached_at).total_seconds()
    if age <= ttl_seconds:
        return MetadataHealth.FULL, age

    return MetadataHealth.DEGRADED, age


def _holiday_hit(holidays: List[dict], today: datetime) -> Optional[str]:
    today_str = today.date().isoformat()
    for holiday in holidays:
        if holiday.get("date") == today_str:
            name = holiday.get("name") or "Market Holiday"
            exchange = holiday.get("exchange")
            if exchange:
                return f"{name} ({exchange})"
            return name
    return None


def compute_market_window(
    now: datetime,
    holidays: Optional[List[dict]],
    holidays_cached_at: Optional[datetime],
    holiday_ttl_seconds: int,
) -> MarketWindow:
    """Compute current market window with explicit metadata health.
    
    Daily Rollover Period (22:00-23:00 UTC):
    - During this hour, brokers perform daily settlement
    - Spreads widen significantly, liquidity drops
    - Data providers may return stale or no data
    - We mark market as closed during rollover
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    session_start, session_end = _fx_session_bounds(now)
    in_weekly_window = session_start <= now < session_end

    # Base weekend/after-hours evaluation
    if not in_weekly_window:
        metadata_present = holidays is not None
        health, age = _classify_metadata_health(now, holidays_cached_at, metadata_present, holiday_ttl_seconds)
        return MarketWindow(
            is_open=False,
            reason="Market Closed: Weekend",
            health=health,
            window_start=session_start,
            window_end=session_end,
            holiday=None,
            metadata_age_seconds=age,
        )

    # Daily rollover check (22:00-23:00 UTC on weekdays Mon-Thu)
    # Friday 22:00 is already weekend closure, Sunday 22:00 is session open
    utc_hour = now.hour
    utc_weekday = now.weekday()  # Mon=0, Sun=6
    
    if utc_hour == 22 and utc_weekday in (0, 1, 2, 3):  # Mon-Thu at 22:xx
        metadata_present = holidays is not None
        health, age = _classify_metadata_health(now, holidays_cached_at, metadata_present, holiday_ttl_seconds)
        return MarketWindow(
            is_open=False,
            reason="Market Closed: Daily Rollover",
            health=health,
            window_start=session_start,
            window_end=session_end,
            holiday=None,
            metadata_age_seconds=age,
        )

    holidays_list = holidays or []
    holiday_name = _holiday_hit(holidays_list, now)
    metadata_present = holidays is not None
    health, age = _classify_metadata_health(now, holidays_cached_at, metadata_present, holiday_ttl_seconds)

    if holiday_name:
        return MarketWindow(
            is_open=False,
            reason=f"Market Holiday: {holiday_name}",
            health=health,
            window_start=session_start,
            window_end=session_end,
            holiday=holiday_name,
            metadata_age_seconds=age,
        )

    return MarketWindow(
        is_open=True,
        reason="Market Open",
        health=health,
        window_start=session_start,
        window_end=session_end,
        holiday=None,
        metadata_age_seconds=age,
    )# This content needs to be appended to trading_calendar.py

def validate_timestamp(
    timestamp: datetime,
    holidays: Optional[List[dict]],
    holidays_cached_at: Optional[datetime],
    holiday_ttl_seconds: int,
) -> TimestampValidation:
    """
    Validate a specific timestamp against trading calendar rules.
    
    This is the authoritative timestamp validation function.
    NEVER store a candle without calling this function.
    
    Args:
        timestamp: The candle timestamp to validate
        holidays: Holiday metadata (None if unavailable)
        holidays_cached_at: When holiday data was cached (None if never)
        holiday_ttl_seconds: TTL for holiday cache
    
    Returns:
        TimestampValidation with explicit confidence and scope
    
    Behavior:
        - Validates against forex session rules (Sun 22:00 - Fri 22:00 UTC)
        - Checks holiday overlay if metadata available
        - Returns explicit confidence based on metadata health
        - OFFLINE mode: validates weekend only, cannot validate holidays
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    
    now = datetime.now(timezone.utc)
    
    # Compute market window for this timestamp
    window = compute_market_window(timestamp, holidays, holidays_cached_at, holiday_ttl_seconds)
    
    # Determine confidence level based on metadata health
    if window.health == MetadataHealth.FULL:
        confidence = "high"
        scope = "Weekend + Holiday validation (fresh metadata)"
    elif window.health == MetadataHealth.DEGRADED:
        confidence = "medium"
        age = window.metadata_age_seconds or 0
        scope = f"Weekend + Holiday validation (stale metadata, {age:.0f}s old)"
    else:  # OFFLINE
        confidence = "low"
        scope = "Weekend-only validation (no holiday metadata)"
    
    return TimestampValidation(
        is_valid=window.is_open,
        reason=window.reason,
        confidence_level=confidence,
        validation_scope=scope,
        metadata_health=window.health,
        timestamp=timestamp,
    )


def split_into_trading_windows(
    start: datetime,
    end: datetime,
    holidays: Optional[List[dict]],
    holidays_cached_at: Optional[datetime],
    holiday_ttl_seconds: int,
) -> List[Tuple[datetime, datetime]]:
    """
    Split a time range into valid trading windows.
    
    Decomposes [start, end] into non-overlapping windows that exclude:
    - Weekends (Fri 22:00 UTC - Sun 22:00 UTC)
    - Known holidays (if metadata available)
    
    Args:
        start: Range start timestamp
        end: Range end timestamp
        holidays: Holiday metadata
        holidays_cached_at: When holiday data was cached
        holiday_ttl_seconds: TTL for holiday cache
    
    Returns:
        List of (window_start, window_end) tuples covering only valid trading periods
    
    Example:
        split_into_trading_windows(
            datetime(2025, 12, 24, 12, 0),  # Tuesday noon
            datetime(2025, 12, 30, 12, 0),  # Monday noon
            holidays=[{"date": "2025-12-25", "name": "Christmas"}],
            ...
        )
        Returns:
        [
            (2025-12-24 12:00, 2025-12-24 22:00),  # Tue noon to Tue close
            # Skip Christmas Wed + weekend
            (2025-12-29 22:00, 2025-12-30 12:00),  # Mon open to Mon noon
        ]
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    
    windows = []
    current = start
    
    # Step through hour by hour to detect transitions
    # (Coarse granularity is acceptable for gap filling)
    while current < end:
        validation = validate_timestamp(current, holidays, holidays_cached_at, holiday_ttl_seconds)
        
        if validation.is_valid:
            # Start of a valid window
            window_start = current
            
            # Find end of this valid window
            probe = current
            while probe < end:
                probe += timedelta(hours=1)
                if probe >= end:
                    # Reached end of range
                    windows.append((window_start, end))
                    current = end
                    break
                
                probe_validation = validate_timestamp(probe, holidays, holidays_cached_at, holiday_ttl_seconds)
                if not probe_validation.is_valid:
                    # Found transition to invalid period
                    windows.append((window_start, probe))
                    current = probe
                    break
        else:
            # Invalid period, skip ahead
            current += timedelta(hours=1)
    
    return windows
