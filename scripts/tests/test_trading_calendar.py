#!/usr/bin/env python3
"""
TEST 1 — Trading Calendar: Timestamp Truth Table
Pure unit test - no APIs, no ingestion
Tests timestamp validation in isolation
"""

import sys
import os
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from api.app.trading_calendar import validate_timestamp, MetadataHealth

def test_timestamp_validation():
    """Test timestamp validation with fixed UTC timestamps"""
    
    print("=" * 80)
    print("TEST 1 — Trading Calendar: Timestamp Truth Table")
    print("=" * 80)
    
    # Test cases: (timestamp, expected_valid, description)
    test_cases = [
        (datetime(2025, 12, 27, 10, 0, tzinfo=timezone.utc), False, "Saturday 10:00"),
        (datetime(2025, 12, 28, 21, 59, tzinfo=timezone.utc), False, "Sunday 21:59"),
        (datetime(2025, 12, 28, 22, 0, tzinfo=timezone.utc), True, "Sunday 22:00 (market opens)"),
        (datetime(2025, 12, 29, 9, 0, tzinfo=timezone.utc), True, "Monday 09:00"),
        (datetime(2025, 12, 26, 21, 59, tzinfo=timezone.utc), True, "Friday 21:59"),
        (datetime(2025, 12, 26, 22, 0, tzinfo=timezone.utc), False, "Friday 22:00 (market closes)"),
    ]
    
    passed = 0
    failed = 0
    
    for timestamp, expected_valid, description in test_cases:
        # Validate timestamp (no holiday metadata - OFFLINE mode)
        result = validate_timestamp(
            timestamp=timestamp,
            holidays=None,
            holidays_cached_at=None,
            holiday_ttl_seconds=345600
        )
        
        # Check result
        success = result.is_valid == expected_valid
        status = "✅ PASS" if success else "❌ FAIL"
        
        print(f"\n{status} | {description}")
        print(f"  Timestamp: {timestamp}")
        print(f"  Expected: {'VALID' if expected_valid else 'INVALID'}")
        print(f"  Got: {'VALID' if result.is_valid else 'INVALID'}")
        print(f"  Reason: {result.reason}")
        print(f"  Confidence: {result.confidence_level}")
        print(f"  Scope: {result.validation_scope}")
        print(f"  Metadata Health: {result.metadata_health}")
        
        if success:
            passed += 1
        else:
            failed += 1
    
    print("\n" + "=" * 80)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 80)
    
    if failed == 0:
        print("✅ TEST 1 PASSED: Calendar correctness proven")
        return True
    else:
        print("❌ TEST 1 FAILED: Calendar validation incorrect")
        return False


if __name__ == "__main__":
    success = test_timestamp_validation()
    sys.exit(0 if success else 1)
