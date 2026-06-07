from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest_engine.broker_specs import BrokerSymbolSpec
from backtest_engine.reporting import advanced_insights as advanced_insights_module
from backtest_engine.reporting.advanced_insights import CandlePack, build_advanced_trade_insights, generate_advanced_trade_insights


def _spec(symbol: str) -> BrokerSymbolSpec:
    return BrokerSymbolSpec(
        symbol=symbol,
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
        currency_base=symbol[:3],
        currency_profit=symbol[3:],
        currency_margin=symbol[:3],
        swap_mode=1,
        swap_long=0.0,
        swap_short=0.0,
        margin_initial=0.0,
        margin_maintenance=0.0,
        commission_per_lot_round_turn_assumption=11.0,
    )


def _run() -> SimpleNamespace:
    started_at = datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        run_id=uuid.uuid4(),
        run_name="advanced-insights-test",
        profile_name="default",
        profile_version="1.0",
        engine_version="1.0",
        source_database_name="backtest_lab",
        source_database_fingerprint={"name": "backtest_lab"},
        strategy_filter={"mode": "all"},
        ea_config={"atr_period": 2},
        ea_config_hash="ea-hash",
        broker_specs_hash="broker-hash",
        trade_executor_hash="trade-hash",
        fill_model="m1_ohlc_conservative",
        started_at=started_at,
        finished_at=started_at + timedelta(hours=1),
        status="completed",
    )


def _cagg_frame(base_price: float) -> pd.DataFrame:
    idx = pd.to_datetime(
        [
            "2026-05-01T11:30:00Z",
            "2026-05-01T11:45:00Z",
            "2026-05-01T12:00:00Z",
            "2026-05-01T12:15:00Z",
        ]
    )
    return pd.DataFrame(
        {
            "open": [base_price - 0.0004, base_price - 0.0002, base_price, base_price + 0.0001],
            "high": [base_price + 0.0001, base_price + 0.0002, base_price + 0.0003, base_price + 0.0004],
            "low": [base_price - 0.0005, base_price - 0.0004, base_price - 0.0002, base_price - 0.0001],
            "close": [base_price - 0.0002, base_price, base_price + 0.0001, base_price + 0.0002],
            "volume": [100, 100, 100, 100],
        },
        index=idx,
    )


def _eurusd_pack() -> CandlePack:
    idx = pd.to_datetime(
        [
            "2026-05-01T12:00:00Z",
            "2026-05-01T12:01:00Z",
            "2026-05-01T12:02:00Z",
            "2026-05-01T12:03:00Z",
            "2026-05-01T12:04:00Z",
            "2026-05-01T12:05:00Z",
            "2026-05-01T12:06:00Z",
        ]
    )
    m1 = pd.DataFrame(
        {
            "open": [1.1010, 1.1012, 1.1013, 1.1008, 1.1008, 1.1006, 1.1000],
            "high": [1.1013, 1.1014, 1.1015, 1.1009, 1.1015, 1.1012, 1.1025],
            "low": [1.1011, 1.1012, 1.1013, 1.1004, 1.1008, 1.1006, 1.0989],
            "close": [1.1012, 1.1013, 1.1014, 1.1008, 1.1012, 1.1009, 1.0990],
            "volume": [10, 10, 10, 10, 10, 10, 10],
        },
        index=idx,
    )
    return CandlePack(m1=m1, cagg_by_timeframe={"M15": _cagg_frame(1.1000)})


def _gbpusd_pack() -> CandlePack:
    idx = pd.to_datetime(
        [
            "2026-05-01T12:00:00Z",
            "2026-05-01T12:01:00Z",
            "2026-05-01T12:02:00Z",
            "2026-05-01T12:03:00Z",
            "2026-05-01T12:04:00Z",
            "2026-05-01T12:05:00Z",
            "2026-05-01T12:06:00Z",
        ]
    )
    m1 = pd.DataFrame(
        {
            "open": [1.3000, 1.3002, 1.3002, 1.2998, 1.2995, 1.2990, 1.2988],
            "high": [1.3004, 1.30035, 1.3003, 1.3001, 1.2999, 1.2983, 1.3012],
            "low": [1.3001, 1.30025, 1.3002, 1.2995, 1.2990, 1.2979, 1.2988],
            "close": [1.30035, 1.30030, 1.30025, 1.2998, 1.2992, 1.2981, 1.3009],
            "volume": [10, 10, 10, 10, 10, 10, 10],
        },
        index=idx,
    )
    return CandlePack(m1=m1, cagg_by_timeframe={"M15": _cagg_frame(1.3000)})


def _mfe_leak_pack() -> CandlePack:
    idx = pd.to_datetime(
        [
            "2026-05-01T12:00:00Z",
            "2026-05-01T12:01:00Z",
            "2026-05-01T12:02:00Z",
            "2026-05-01T12:03:00Z",
        ]
    )
    m1 = pd.DataFrame(
        {
            "open": [1.1000, 1.1002, 1.1003, 1.1004],
            "high": [1.1015, 1.1006, 1.1008, 1.1009],
            "low": [1.0998, 1.1001, 1.1002, 1.1003],
            "close": [1.1002, 1.1004, 1.1005, 1.1006],
            "volume": [10, 10, 10, 10],
        },
        index=idx,
    )
    return CandlePack(m1=m1, cagg_by_timeframe={"M15": _cagg_frame(1.1000)})


def _result(**overrides):
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    base = {
        "result_id": 1,
        "strategy_id": 1,
        "run_id": _run().run_id,
        "symbol": "EURUSD",
        "direction": "long",
        "condition_type": "breakout_close",
        "timeframe": "M15",
        "confirmation": "none",
        "strategy_timestamp": ts,
        "strategy_expiry_time": ts + timedelta(minutes=5),
        "entry_time": None,
        "exit_time": None,
        "outcome": "expired_without_entry",
        "outcome_reason": "Did not trigger",
        "entry_price": None,
        "exit_price": None,
        "take_profit": None,
        "stop_loss": None,
        "initial_stop_loss": None,
        "final_stop_loss": None,
        "debug": {
            "signal": {
                "direction": "long",
                "condition_type": "breakout_close",
                "timeframe": "M15",
                "confirmation": "none",
                "entry_level": 1.1010,
                "trigger_zone_source": "level_fallback",
                "confirmation_required": False,
            }
        },
        "break_even_moved": False,
        "partial_close_executed": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_advanced_trade_insights_separates_buckets_and_writes_artifacts(tmp_path):
    run = _run()
    results = [
        _result(
            result_id=1,
            run_id=run.run_id,
            symbol="EURUSD",
            condition_type="breakout_close",
            strategy_timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            strategy_expiry_time=datetime(2026, 5, 1, 12, 2, tzinfo=timezone.utc),
            outcome="expired_without_entry",
            outcome_reason="Did not trigger",
            debug={
                "signal": {
                    "direction": "long",
                    "condition_type": "breakout_close",
                    "timeframe": "M15",
                    "confirmation": "none",
                    "entry_level": 1.1010,
                    "trigger_zone_source": "level_fallback",
                    "confirmation_required": False,
                }
            },
        ),
        _result(
            result_id=2,
            run_id=run.run_id,
            symbol="GBPUSD",
            direction="long",
            condition_type="zone_retest",
            strategy_timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            strategy_expiry_time=datetime(2026, 5, 1, 12, 2, tzinfo=timezone.utc),
            outcome="invalidated_without_entry",
            outcome_reason="Touched zone and moved away",
            debug={
                "signal": {
                    "direction": "long",
                    "condition_type": "zone_retest",
                    "timeframe": "M15",
                    "confirmation": "none",
                    "entry_level": 1.3000,
                    "trigger_zone": [1.3000, 1.3002],
                    "trigger_zone_source": "entry_signal",
                    "confirmation_required": False,
                }
            },
        ),
        _result(
            result_id=3,
            run_id=run.run_id,
            symbol="EURUSD",
            direction="long",
            condition_type="breakout_close",
            strategy_timestamp=datetime(2026, 5, 1, 12, 3, tzinfo=timezone.utc),
            strategy_expiry_time=datetime(2026, 5, 1, 12, 7, tzinfo=timezone.utc),
            entry_time=datetime(2026, 5, 1, 12, 3, tzinfo=timezone.utc),
            exit_time=datetime(2026, 5, 1, 12, 6, tzinfo=timezone.utc),
            outcome="closed_sl",
            outcome_reason="Stop loss hit",
            entry_price=1.1000,
            exit_price=1.0989,
            take_profit=1.1020,
            stop_loss=1.0990,
            initial_stop_loss=1.0990,
            final_stop_loss=1.0990,
            debug={
                "signal": {
                    "direction": "long",
                    "condition_type": "breakout_close",
                    "timeframe": "M15",
                    "confirmation": "none",
                    "entry_level": 1.1000,
                    "trigger_zone_source": "level_fallback",
                    "confirmation_required": False,
                }
            },
        ),
        _result(
            result_id=4,
            run_id=run.run_id,
            symbol="GBPUSD",
            direction="short",
            condition_type="zone_retest",
            strategy_timestamp=datetime(2026, 5, 1, 12, 3, tzinfo=timezone.utc),
            strategy_expiry_time=datetime(2026, 5, 1, 12, 7, tzinfo=timezone.utc),
            entry_time=datetime(2026, 5, 1, 12, 3, tzinfo=timezone.utc),
            exit_time=datetime(2026, 5, 1, 12, 6, tzinfo=timezone.utc),
            outcome="closed_trailing_sl",
            outcome_reason="Trailing stop hit",
            entry_price=1.3000,
            exit_price=1.3009,
            take_profit=1.2980,
            stop_loss=1.3010,
            initial_stop_loss=1.3010,
            final_stop_loss=1.2992,
            break_even_moved=True,
            partial_close_executed=True,
            debug={
                "signal": {
                    "direction": "short",
                    "condition_type": "zone_retest",
                    "timeframe": "M15",
                    "confirmation": "none",
                    "entry_level": 1.3000,
                    "trigger_zone": [1.2998, 1.3002],
                    "trigger_zone_source": "entry_signal",
                    "confirmation_required": False,
                }
            },
        ),
    ]

    bundles = {
        "EURUSD": _eurusd_pack(),
        "GBPUSD": _gbpusd_pack(),
    }

    bundle = build_advanced_trade_insights(
        run=run,
        results=results,
        candle_packs_by_symbol=bundles,
        broker_specs={"EURUSD": _spec("EURUSD"), "GBPUSD": _spec("GBPUSD")},
        output_dir=tmp_path / "advanced_insights",
    )

    assert bundle.detail_frame.shape[0] == 4
    assert set(bundle.missed_entry_frame["analysis_bucket"]) == {"expired_without_entry", "invalidated_without_entry"}
    assert bundle.missed_entry_frame.loc[bundle.missed_entry_frame["analysis_bucket"] == "expired_without_entry", "closest_approach_pips"].iloc[0] == pytest.approx(1.0)
    invalidated_row = bundle.missed_entry_frame.loc[bundle.missed_entry_frame["analysis_bucket"] == "invalidated_without_entry"].iloc[0]
    assert bool(invalidated_row["crossed_trigger"]) is True
    assert invalidated_row["closest_approach_price"] == pytest.approx(1.3002)
    assert invalidated_row["first_touch_time"] == "2026-05-01T12:00:00+00:00"
    assert set(bundle.mfe_frame["stop_type"]) == {"closed_sl", "closed_trailing_sl"}

    closed_sl_row = bundle.mfe_frame.loc[bundle.mfe_frame["stop_type"] == "closed_sl"].iloc[0]
    assert closed_sl_row["mfe_pips"] == pytest.approx(15.0)
    assert closed_sl_row["max_adverse_excursion_pips"] == pytest.approx(11.0)
    assert closed_sl_row["stop_loss"] == pytest.approx(1.0990)
    assert bool(closed_sl_row["reached_80pct_of_tp"]) is False

    trailing_row = bundle.mfe_frame.loc[bundle.mfe_frame["stop_type"] == "closed_trailing_sl"].iloc[0]
    assert trailing_row["mfe_pips"] == pytest.approx(21.0)
    assert trailing_row["stop_loss"] == pytest.approx(1.3010)
    assert bool(trailing_row["reached_80pct_of_tp"]) is True
    assert bool(trailing_row["breakeven_moved"]) is True
    assert bool(trailing_row["partial_close_executed"]) is True

    summary_path = tmp_path / "advanced_insights" / "summary.md"
    assert summary_path.exists()
    assert (tmp_path / "advanced_insights" / "advanced_insights.csv").exists()
    assert (tmp_path / "advanced_insights" / "advanced_insights.json").exists()
    assert (tmp_path / "advanced_insights" / "missed_entry_histogram.csv").exists()
    assert (tmp_path / "advanced_insights" / "mfe_histogram.csv").exists()


def test_mfe_excludes_entry_candle_from_favorable_excursion(tmp_path):
    run = _run()
    result = _result(
        result_id=5,
        run_id=run.run_id,
        symbol="EURUSD",
        direction="long",
        condition_type="breakout_close",
        strategy_timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        strategy_expiry_time=datetime(2026, 5, 1, 12, 4, tzinfo=timezone.utc),
        entry_time=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        exit_time=datetime(2026, 5, 1, 12, 3, tzinfo=timezone.utc),
        outcome="closed_sl",
        outcome_reason="Stop loss hit",
        entry_price=1.1000,
        exit_price=1.0990,
        take_profit=1.1020,
        stop_loss=1.0990,
        initial_stop_loss=1.0990,
        final_stop_loss=1.0990,
        debug=None,
    )

    bundle = build_advanced_trade_insights(
        run=run,
        results=[result],
        candle_packs_by_symbol={"EURUSD": _mfe_leak_pack()},
        broker_specs={"EURUSD": _spec("EURUSD")},
        output_dir=tmp_path / "advanced_insights",
    )

    mfe_row = bundle.mfe_frame.iloc[0]
    assert mfe_row["best_favorable_price"] == pytest.approx(1.1008)
    assert mfe_row["mfe_pips"] == pytest.approx(8.0)


def test_advanced_insights_json_export_is_strict_json(tmp_path):
    run = _run()
    result = _result(
        result_id=6,
        run_id=run.run_id,
        symbol="EURUSD",
        strategy_timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        strategy_expiry_time=datetime(2026, 5, 1, 12, 2, tzinfo=timezone.utc),
        outcome="expired_without_entry",
        outcome_reason="Did not trigger",
        debug=None,
    )

    build_advanced_trade_insights(
        run=run,
        results=[result],
        candle_packs_by_symbol={},
        broker_specs={},
        output_dir=tmp_path / "advanced_insights",
    )

    json_text = (tmp_path / "advanced_insights" / "advanced_insights.json").read_text()
    assert "NaN" not in json_text
    assert "Infinity" not in json_text

    payload = json.loads(json_text)
    assert payload["missed_entry_histogram"][0]["avg_closest_approach_pips"] is None


def test_generate_advanced_trade_insights_degrades_sparse_rows_without_source_strategy_fallback(tmp_path, monkeypatch):
    run = _run()
    result = _result(
        result_id=7,
        run_id=run.run_id,
        symbol="EURUSD",
        direction="long",
        condition_type="breakout_close",
        timeframe=None,
        strategy_timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        strategy_expiry_time=datetime(2026, 5, 1, 12, 2, tzinfo=timezone.utc),
        outcome="expired_without_entry",
        outcome_reason="Did not trigger",
        debug=None,
    )
    load_timeframes: list[str] = []

    async def fake_load_m1_candles(session, symbol, start_time, end_time):
        return _mfe_leak_pack().m1

    async def fake_load_cagg_candles(session, symbol, timeframe, start_time, end_time):
        load_timeframes.append(timeframe)
        return _cagg_frame(1.1000)

    class FakeQueryResult:
        def __init__(self, *, scalar=None, rows=None):
            self._scalar = scalar
            self._rows = rows or []

        def scalar_one(self):
            return self._scalar

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class FakeDestSession:
        def __init__(self):
            self.calls = 0

        async def execute(self, statement):
            self.calls += 1
            if self.calls == 1:
                return FakeQueryResult(scalar=run)
            if self.calls == 2:
                return FakeQueryResult(rows=[result])
            raise AssertionError(f"Unexpected dest execute call: {self.calls}")

    class FakeSourceSession:
        async def execute(self, statement):
            raise AssertionError(f"Unexpected source session execute call: {statement!r}")

    monkeypatch.setattr(advanced_insights_module, "load_m1_candles", fake_load_m1_candles)
    monkeypatch.setattr(advanced_insights_module, "load_cagg_candles", fake_load_cagg_candles)

    bundle = asyncio.run(
        generate_advanced_trade_insights(
            dest_session=FakeDestSession(),
            source_session=FakeSourceSession(),
            run_id=run.run_id,
            broker_specs={"EURUSD": _spec("EURUSD")},
            output_dir=tmp_path / "advanced_insights",
        )
    )

    assert load_timeframes == []
    missed_row = bundle.missed_entry_frame.iloc[0]
    assert missed_row["analysis_note"] == "missing_debug_signal"
    assert bool(missed_row["signal_debug_present"]) is False
    assert pd.isna(missed_row["closest_approach_pips"])
    assert pd.isna(missed_row["timeframe"])