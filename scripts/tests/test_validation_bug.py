#!/usr/bin/env python3
"""
TEST 2 — Validation Bug Regression Test
Confirms window.is_open fix works (no AttributeError)
"""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from api.app.trading_calendar import validate_timestamp

def test_no_crash():
    """Verify validation doesn't crash with AttributeError"""
    
    print("=" * 80)
    print("TEST 2 — Validation Bug Regression Test")
    print("=" * 80)
    
    try:
        # Try various timestamps
        test_timestamps = [
            datetime(2025, 12, 27, 10, 0, tzinfo=timezone.utc),  # Saturday
            datetime(2025, 12, 29, 15, 0, tzinfo=timezone.utc),  # Monday
            datetime(2025, 12, 26, 23, 0, tzinfo=timezone.utc),  # Friday night
        ]
        
        for ts in test_timestamps:
            result = validate_timestamp(
                timestamp=ts,
                holidays=None,
                holidays_cached_at=None,
                holiday_ttl_seconds=345600
            )
            
            print(f"\n✅ No crash for {ts}")
            print(f"   Result: {result.is_valid} | {result.reason}")
        
        print("\n" + "=" * 80)
        print("✅ TEST 2 PASSED: No AttributeError, validation works")
        print("=" * 80)
        return True
        
    except AttributeError as e:
        print("\n" + "=" * 80)
        print(f"❌ TEST 2 FAILED: AttributeError occurred")
        print(f"   Error: {e}")
        print("   BUG STILL EXISTS: window.is_valid should be window.is_open")
        print("=" * 80)
        return False
    except Exception as e:
        print("\n" + "=" * 80)
        print(f"❌ TEST 2 FAILED: Unexpected error")
        print(f"   Error: {e}")
        print("=" * 80)
        return False


if __name__ == "__main__":
    success = test_no_crash()
    sys.exit(0 if success else 1)
