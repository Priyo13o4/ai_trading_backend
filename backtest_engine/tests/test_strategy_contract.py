from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from backtest_engine.broker_specs import BrokerSymbolSpec
from backtest_engine.simulation.state import simulate_strategy


def _spec() -> BrokerSymbolSpec:
    return BrokerSymbolSpec(
        symbol="EURUSD",
        exists=True,
        selected=True,
        digits=5,
        point=0.00001,
        spread_points=0,
        spread_float=True,
        trade_mode=4,
        trade_calc_mode=0,
        contract_size=100000.0,
        tick_size=0.00001,
        tick_value=1.0,
        tick_value_profit=1.0,
        tick_value_loss=1.0,
        volume_min=0.01,
        volume_max=200.0,
        volume_step=0.01,
        volume_limit=0.0,
        stops_level=0,
        freeze_level=0,
        currency_base="EUR",
        currency_profit="USD",
        currency_margin="EUR",
        swap_mode=1,
        swap_long=0.0,
        swap_short=0.0,
        margin_initial=0.0,
        margin_maintenance=0.0,
        commission_per_lot_round_turn_assumption=11.0,
    )


def _strategy(**overrides):
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    base = {
        "strategy_id": 1,
        "strategy_name": "Contract Test",
        "symbol": "EURUSD",
        "direction": "long",
        "entry_signal": {
            "condition_type": "immediate",
            "timeframe": "M15",
            "level": 1.1000,
            "confirmation": "none",
        },
        "take_profit": 1.1020,
        "stop_loss": 1.0990,
        "confidence": "Medium",
        "risk_reward_ratio": 2.0,
        "timestamp": ts,
        "expiry_time": ts + timedelta(minutes=30),
        "execution_allowed": True,
        "trade_recommended": True,
        "risk_level": "normal",
        "trade_mode": "protective",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _candles() -> tuple[pd.DataFrame, pd.DataFrame]:
    idx = pd.to_datetime(
        [
            "2026-05-01T11:45:00Z",
            "2026-05-01T12:00:00Z",
            "2026-05-01T12:15:00Z",
            "2026-05-01T12:30:00Z",
        ]
    )
    cagg = pd.DataFrame(
        {
            "open": [1.0990, 1.1000, 1.1005, 1.1010],
            "high": [1.1000, 1.1010, 1.1020, 1.1025],
            "low": [1.0980, 1.0990, 1.1000, 1.1005],
            "close": [1.0995, 1.1005, 1.1010, 1.1015],
            "volume": [100, 100, 100, 100],
        },
        index=idx,
    )
    m1_idx = pd.to_datetime(
        [
            "2026-05-01T12:00:00Z",
            "2026-05-01T12:01:00Z",
            "2026-05-01T12:02:00Z",
        ]
    )
    m1 = pd.DataFrame(
        {
            "open": [1.1000, 1.1004, 1.1008],
            "high": [1.1005, 1.1010, 1.1021],
            "low": [1.0998, 1.1001, 1.1006],
            "close": [1.1004, 1.1008, 1.1019],
            "volume": [10, 10, 10],
        },
        index=m1_idx,
    )
    return cagg, m1


def _run(strategy):
    cagg, m1 = _candles()
    return asyncio.run(
        simulate_strategy(
            strategy=strategy,
            cagg_df=cagg,
            m1_df=m1,
            ea_config={"use_trailing_stop": False, "use_partial_closing": False},
            broker_spec=_spec(),
            run_id=uuid.uuid4(),
            current_balance=500.0,
        )
    )


def test_execution_allowed_is_read_from_strategy_root():
    result = _run(_strategy(execution_allowed=False))
    assert result.outcome == "rejected_execution_not_allowed"


def test_trade_recommended_has_its_own_outcome():
    result = _run(_strategy(trade_recommended=False))
    assert result.outcome == "rejected_not_recommended"


def test_timeframe_is_recorded_from_entry_signal():
    result = _run(_strategy())
    assert result.timeframe == "M15"


def test_debug_payload_captures_signal_context():
    result = _run(_strategy())
    assert result.debug is not None
    signal = result.debug["signal"]
    assert signal["entry_reference_price"] == 1.1
    assert signal["confirmation_required"] is False
    assert signal["trigger_zone_source"] in {"entry_signal", "atr_fallback", "level_fallback"}
    assert signal["trigger_zone"]


def test_immediate_condition_is_supported():
    result = _run(_strategy())
    assert result.outcome != "unsupported_condition_type"
    assert result.entry_time is not None


def test_decimal_strategy_prices_are_normalized_before_math():
    result = _run(
        _strategy(
            take_profit=Decimal("1.1020"),
            stop_loss=Decimal("1.0990"),
            risk_reward_ratio=Decimal("2.0"),
        )
    )
    assert result.outcome != "error"
    assert result.initial_stop_loss == 1.099
    assert result.take_profit == 1.102
