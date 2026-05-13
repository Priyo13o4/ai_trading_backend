import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

# We will patch sys.path so we can import the worker script without pain.
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "api-worker"))
sys.path.insert(0, APP_ROOT)

from scripts.worker.mt5_staleness_watchdog import check_staleness, STALENESS_MINUTES

@patch('scripts.worker.mt5_staleness_watchdog.get_latest_candles')
@patch('scripts.worker.mt5_staleness_watchdog.report_runtime_error')
def test_watchdog_no_alert_when_fresh(mock_report, mock_get_candles):
    # Setup mock data (fresh)
    now = datetime.now(timezone.utc)
    mock_get_candles.return_value = [
        {'symbol': 'BTCUSD', 'last_candle_time': now - timedelta(minutes=1)},
        {'symbol': 'EURUSD', 'last_candle_time': now - timedelta(minutes=1)}
    ]
    
    check_staleness()
    
    mock_report.assert_not_called()

@patch('scripts.worker.mt5_staleness_watchdog.get_latest_candles')
@patch('scripts.worker.mt5_staleness_watchdog.report_runtime_error')
@patch('scripts.worker.mt5_staleness_watchdog.last_alert_time', None)
def test_watchdog_alerts_when_stale(mock_report, mock_get_candles):
    # Setup mock data (stale)
    now = datetime.now(timezone.utc)
    mock_get_candles.return_value = [
        {'symbol': 'BTCUSD', 'last_candle_time': now - timedelta(minutes=STALENESS_MINUTES + 1)},
        {'symbol': 'EURUSD', 'last_candle_time': now - timedelta(minutes=1)}
    ]
    
    check_staleness()
    
    mock_report.assert_called_once()
    args, kwargs = mock_report.call_args
    assert kwargs['severity'] == 'critical'
    assert 'BTCUSD' in kwargs['context']['last_known_candles_ist']
