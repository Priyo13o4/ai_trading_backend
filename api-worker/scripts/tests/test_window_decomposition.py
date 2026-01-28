#!/usr/bin/env python3
"""
TEST 3 — Gap Filler Window Decomposition (MOST IMPORTANT)
Proves Invariant #3: Never fetch known-invalid time ranges
"""

import sys
import os
from datetime import datetime, timezone

scripts_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
repo_root = os.path.abspath(os.path.join(scripts_root, '..'))
api_web_root = os.path.join(repo_root, 'api-web')

if os.path.isdir(api_web_root):
    sys.path.insert(0, api_web_root)
else:
    sys.path.insert(0, scripts_root)

from app.trading_calendar import split_into_trading_windows

def test_window_decomposition():
    """Test that weekend/holiday gaps are excluded from fetch windows"""
    
    print("=" * 80)
    print("TEST 3 — Gap Filler Window Decomposition")
    print("=" * 80)
    
    # Simulate gap: Friday 20:00 → Monday 10:00
    # Should split into: [Fri 20:00-22:00] + [Sun 22:00-Mon 10:00]
    start = datetime(2025, 12, 26, 20, 0, tzinfo=timezone.utc)  # Friday 20:00
    end = datetime(2025, 12, 29, 10, 0, tzinfo=timezone.utc)    # Monday 10:00
    
    print(f"\nTest range: {start} → {end}")
    print(f"Span: {(end - start).total_seconds() / 3600:.1f} hours")
    print(f"Includes: Fri evening + entire weekend + Mon morning")
    
    # Call window decomposition (no holiday metadata)
    windows = split_into_trading_windows(
        start=start,
        end=end,
        holidays=None,
        holidays_cached_at=None,
        holiday_ttl_seconds=345600
    )
    
    print(f"\n✅ Computed {len(windows)} trading windows:")
    
    if not windows:
        print("❌ FAIL: No windows returned - decomposition failed")
        return False
    
    total_window_hours = 0
    for i, (ws, we) in enumerate(windows, 1):
        duration = (we - ws).total_seconds() / 3600
        total_window_hours += duration
        print(f"\nWindow {i}:")
        print(f"  Start: {ws} ({ws.strftime('%A %H:%M')})")
        print(f"  End:   {we} ({we.strftime('%A %H:%M')})")
        print(f"  Duration: {duration:.1f} hours")
        
        # Verify window doesn't include weekend core
        if ws.weekday() == 5 or (ws.weekday() == 6 and ws.hour < 22):
            print(f"  ❌ FAIL: Window starts in weekend!")
            return False
        if we.weekday() == 5 or (we.weekday() == 6 and we.hour < 22):
            print(f"  ⚠️  Warning: Window ends in weekend")
    
    total_gap_hours = (end - start).total_seconds() / 3600
    skipped_hours = total_gap_hours - total_window_hours
    
    print(f"\n" + "=" * 80)
    print(f"Total gap: {total_gap_hours:.1f} hours")
    print(f"Valid windows: {total_window_hours:.1f} hours ({total_window_hours/total_gap_hours*100:.1f}%)")
    print(f"Skipped (closed): {skipped_hours:.1f} hours ({skipped_hours/total_gap_hours*100:.1f}%)")
    
    # Verify weekend was skipped
    if skipped_hours < 40:  # Weekend should be ~48 hours
        print("❌ FAIL: Weekend not properly excluded")
        return False
    
    # Verify no single block fetch
    if len(windows) == 1:
        window_start, window_end = windows[0]
        if window_start == start and window_end == end:
            print("❌ FAIL: Single block fetch detected - no decomposition!")
            return False
    
    print("=" * 80)
    print("✅ TEST 3 PASSED: Window decomposition working correctly")
    print("   - Weekend excluded from fetches")
    print("   - Valid trading windows identified")
    print("   - No single-block fetch")
    print("=" * 80)
    
    return True


if __name__ == "__main__":
    success = test_window_decomposition()
    sys.exit(0 if success else 1)
