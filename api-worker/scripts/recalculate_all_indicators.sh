#!/bin/bash
# Recalculate all indicators using corrected formulas from technical.py
# Run this script inside the API container

set -e

echo "==========================================="
echo "🔄 INDICATOR RECALCULATION"
echo "==========================================="
echo "Using corrected formulas from technical.py"
echo "==========================================="
echo ""

# Run indicator calculation for all symbols and timeframes
echo "📊 Starting indicator calculation (v2.0 - DST-safe)..."
python /app/scripts/calculate_recent_indicators_v2.py

echo ""
echo "==========================================="
echo "✅ RECALCULATION COMPLETE"
echo "==========================================="
echo ""
echo "📊 Verifying database..."
docker exec -i n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data -c "
SELECT 
    COUNT(*) as total_rows,
    COUNT(DISTINCT symbol) as symbols,
    COUNT(DISTINCT timeframe) as timeframes,
    MIN(time) as oldest_data,
    MAX(time) as newest_data
FROM technical_indicators;
"

echo ""
echo "🎯 Sample check (XAUUSD H1 latest indicator):"
docker exec -i n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data -c "
SELECT 
    time,
    ema_9, ema_21,
    rsi,
    macd_main,
    atr, atr_percentile,
    bb_width_percentile,
    ema_momentum_slope
FROM technical_indicators
WHERE symbol = 'XAUUSD' AND timeframe = 'H1'
ORDER BY time DESC
LIMIT 1;
"
