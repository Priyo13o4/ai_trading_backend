#!/bin/bash
# Run all architecture verification tests

echo "================================================================================================="
echo "ARCHITECTURE VERIFICATION TEST SUITE"
echo "Testing timestamp-first, calendar-driven ingestion"
echo "================================================================================================="

cd "$(dirname "$0")"

FAILED=0

# TEST 1: Trading Calendar Truth Table
echo -e "\n\n"
python3 test_trading_calendar.py
if [ $? -ne 0 ]; then
    FAILED=$((FAILED + 1))
fi

# TEST 2: Validation Bug Regression
echo -e "\n\n"
python3 test_validation_bug.py
if [ $? -ne 0 ]; then
    FAILED=$((FAILED + 1))
fi

# TEST 3: Window Decomposition
echo -e "\n\n"
python3 test_window_decomposition.py
if [ $? -ne 0 ]; then
    FAILED=$((FAILED + 1))
fi

# TEST 7: Database Audit (requires Docker container for psycopg3 and DB access)
echo -e "\n\n"
cd ../../
docker compose exec -T api python /app/scripts/tests/test_db_audit.py
if [ $? -ne 0 ]; then
    FAILED=$((FAILED + 1))
fi
cd scripts/tests

# Final summary
echo -e "\n\n"
echo "================================================================================================="
if [ $FAILED -eq 0 ]; then
    echo "✅ ALL TESTS PASSED"
    echo "Architecture verified: timestamp-first, calendar-driven ingestion fully implemented"
else
    echo "❌ $FAILED TEST(S) FAILED"
    echo "Architecture incomplete or incorrect"
fi
echo "================================================================================================="

exit $FAILED
