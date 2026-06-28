"""Tests for the EA-mirror behaviours added to the backtest engine.

Covers EA-08 (range_breakout off level), EA-18 (min-lot clamp / partial skip), EA-01 (max
entry-distance guard, via a real simulate_strategy run) and the portfolio model (EA-04
opposite-conflict, concurrency cap, EA-16 equity drawdown sizing, PnL scaling, equity curve).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest_engine.broker_specs import BrokerSymbolSpec
from backtest_engine.simulation.entries import check_range_breakout
from backtest_engine.simulation.risk import normalize_volume
from backtest_engine.simulation.management import check_partial_close
from backtest_engine.simulation.portfolio import simulate_portfolio, PortfolioTradeCtx
from backtest_engine.simulation.state import simulate_strategy


UTC = timezone.utc


def _spec(symbol="TEST", point=0.001, tick_size=0.001, tick_value=1.0,
          volume_min=0.01, volume_max=100.0, volume_step=0.01, commission=0.0):
    return BrokerSymbolSpec(
        symbol=symbol, exists=True, selected=True, digits=3, point=point, spread_points=0,
        spread_float=False, trade_mode=4, trade_calc_mode=0, contract_size=100000.0,
        tick_size=tick_size, tick_value=tick_value, tick_value_profit=tick_value,
        tick_value_loss=tick_value, volume_min=volume_min, volume_max=volume_max,
        volume_step=volume_step, volume_limit=0.0, stops_level=0, freeze_level=0,
        currency_base="EUR", currency_profit="USD", currency_margin="EUR", swap_mode=0,
        swap_long=0.0, swap_short=0.0, margin_initial=0.0, margin_maintenance=0.0,
        commission_per_lot_round_turn_assumption=commission,
    )


# --------------------------------------------------------------------------- EA-08
def test_ea08_range_breakout_fires_off_level_not_zone():
    point = 0.001
    # A long signal whose synthesized zone sits well above the level. The old code keyed off
    # zone[1]; EA-08 keys off level + buffer, so price just above level must trigger.
    signal = {
        "direction": "long", "level": 100.0, "entry_level": 100.0,
        "trigger_zone": [100.0, 105.0], "confirmation": "none",
        "entry_spread_buffer_pips": 0.0, "condition_type": "range_breakout",
    }
    df = pd.DataFrame({"open": [100, 100], "high": [101, 101],
                       "low": [99, 99], "close": [100, 100]})
    # price above level (100.5) but BELOW old zone top (105): old logic = False, EA-08 = True
    assert check_range_breakout(df, 100.5, signal, point, atr=1.0) is True
    # price below level: no breakout
    assert check_range_breakout(df, 99.5, signal, point, atr=1.0) is False


def test_ea08_short_breakout_off_level():
    point = 0.001
    signal = {
        "direction": "short", "level": 100.0, "entry_level": 100.0,
        "trigger_zone": [95.0, 100.0], "confirmation": "none",
        "entry_spread_buffer_pips": 0.0, "condition_type": "range_breakout",
    }
    df = pd.DataFrame({"open": [100, 100], "high": [101, 101],
                       "low": [99, 99], "close": [100, 100]})
    assert check_range_breakout(df, 99.5, signal, point, atr=1.0) is True
    assert check_range_breakout(df, 100.5, signal, point, atr=1.0) is False


# --------------------------------------------------------------------------- EA-18
def test_ea18_clamps_sub_min_lot_up_to_broker_min():
    spec = _spec(volume_min=0.01, volume_step=0.01)
    # risk-derived volume below broker min must clamp UP, not abort
    assert normalize_volume(0.003, spec, min_lot_size=0.01, max_lot_size=0.1) == pytest.approx(0.01)


def test_ea18_partial_close_skips_below_broker_min():
    # close volume below broker min -> marked executed but size 0 (skip), never clamped up
    triggered, vol = check_partial_close(
        current_price=101.0, entry_price=100.0, direction="long",
        original_sl_distance=1.0, total_tp_distance=2.0, original_lot=0.01,
        partial_close_percent=50.0, partial_closed=False,
        broker_min_volume=0.01, broker_volume_step=0.01,
    )
    assert triggered is True and vol == 0.0


# --------------------------------------------------------------------------- portfolio helpers
def _result(strategy_id, symbol, direction, entry_t, exit_t, lot, net_pnl,
            entry_price=1.10, sl=1.09, confidence="Medium", rr=0.0):
    r = SimpleNamespace(
        strategy_id=strategy_id, symbol=symbol, direction=direction,
        entry_time=entry_t, exit_time=exit_t, lot_size=lot, net_pnl=net_pnl,
        entry_price=entry_price, initial_stop_loss=sl,
        equity_high_watermark=None, drawdown_after=None,
        pf_ctx=PortfolioTradeCtx(confidence=confidence, risk_reward_ratio=rr,
                                 mark_times=[], mark_prices=[]),
    )
    return r


def _eurusd():
    # 5-digit FX-like spec where risk sizing yields lots above the floor (tick_value 0.1)
    return _spec(symbol="EURUSD", point=0.00001, tick_size=0.00001, tick_value=0.1,
                 volume_min=0.01, volume_step=0.01, volume_max=100.0)


def _t(minute):
    return datetime(2026, 1, 1, 0, minute, tzinfo=UTC)


# --------------------------------------------------------------------------- EA-04
def test_ea04_opposite_open_position_is_rejected():
    specs = {"EURUSD": _eurusd()}
    cfg = {"block_opposite_open_positions": True, "max_concurrent_trades": 20,
           "max_total_risk_percent": 95.0, "use_drawdown_protection": False}
    r1 = _result(1, "EURUSD", "long", _t(0), _t(10), 0.1, 5.0)
    r2 = _result(2, "EURUSD", "short", _t(5), _t(15), 0.1, 5.0)  # opens while r1 still open
    out = simulate_portfolio([r1, r2], cfg, specs, starting_balance=500.0)
    assert out.num_admitted == 1
    assert out.reject_reasons.get("directional_conflict") == 1


def test_ea04_same_direction_overlap_is_allowed():
    specs = {"EURUSD": _eurusd()}
    cfg = {"block_opposite_open_positions": True, "max_concurrent_trades": 20,
           "max_total_risk_percent": 95.0, "use_drawdown_protection": False}
    r1 = _result(1, "EURUSD", "long", _t(0), _t(10), 0.1, 5.0)
    r2 = _result(2, "EURUSD", "long", _t(5), _t(15), 0.1, 5.0)
    out = simulate_portfolio([r1, r2], cfg, specs, starting_balance=500.0)
    assert out.num_admitted == 2
    assert not out.reject_reasons


# --------------------------------------------------------------------------- concurrency cap
def test_concurrency_cap_rejects_second_overlap():
    specs = {"EURUSD": _eurusd(), "GBPUSD": _spec(symbol="GBPUSD", point=0.00001,
             tick_size=0.00001, tick_value=0.1)}
    cfg = {"block_opposite_open_positions": True, "max_concurrent_trades": 1,
           "max_total_risk_percent": 95.0, "use_drawdown_protection": False}
    r1 = _result(1, "EURUSD", "long", _t(0), _t(10), 0.1, 5.0)
    r2 = _result(2, "GBPUSD", "long", _t(5), _t(15), 0.1, 5.0)
    out = simulate_portfolio([r1, r2], cfg, specs, starting_balance=500.0)
    assert out.num_admitted == 1
    assert out.reject_reasons.get("max_concurrent") == 1


# --------------------------------------------------------------------------- EA-16
def test_ea16_drawdown_reduces_lot_after_loss():
    specs = {"EURUSD": _eurusd()}
    cfg = {"block_opposite_open_positions": True, "max_concurrent_trades": 20,
           "max_total_risk_percent": 95.0, "use_drawdown_protection": True,
           "drawdown_threshold": 10.0, "drawdown_reduction_factor": 0.5,
           "max_risk_percent_per_trade": 2.0}
    # r1 sizes to 0.1 lot at equity 500 (ratio 1 with independent lot 0.1); a -100 loss drops
    # balance to 400 (20% drawdown). r2 then enters in drawdown -> sized off equity & halved.
    r1 = _result(1, "EURUSD", "long", _t(0), _t(5), 0.1, -100.0)
    r2 = _result(2, "EURUSD", "long", _t(10), _t(20), 0.1, 10.0)
    out = simulate_portfolio([r1, r2], cfg, specs, starting_balance=500.0)
    t2 = next(t for t in out.trades if t.strategy_id == 2)
    assert t2.admitted
    assert t2.drawdown_sizing_applied is True
    assert t2.portfolio_lot == pytest.approx(0.04, abs=1e-6)  # 0.08 sized * 0.5 reduction

    # control: no loss -> no drawdown -> no reduction, larger lot
    cfg_ctrl = dict(cfg)
    r1c = _result(1, "EURUSD", "long", _t(0), _t(5), 0.1, 0.0)
    r2c = _result(2, "EURUSD", "long", _t(10), _t(20), 0.1, 10.0)
    out_c = simulate_portfolio([r1c, r2c], cfg_ctrl, specs, starting_balance=500.0)
    t2c = next(t for t in out_c.trades if t.strategy_id == 2)
    assert t2c.drawdown_sizing_applied is False
    assert t2c.portfolio_lot > t2.portfolio_lot


# --------------------------------------------------------------------------- PnL scaling + curve
def test_portfolio_scales_pnl_to_chosen_lot_and_builds_equity_curve():
    specs = {"EURUSD": _eurusd()}
    cfg = {"block_opposite_open_positions": True, "max_concurrent_trades": 20,
           "max_total_risk_percent": 95.0, "use_drawdown_protection": False,
           "max_risk_percent_per_trade": 2.0}
    # independent lot 0.05 with +25 net -> +500/lot. Portfolio sizes 0.1 -> scaled +50.
    r = _result(1, "EURUSD", "long", _t(0), _t(10), 0.05, 25.0)
    out = simulate_portfolio([r], cfg, specs, starting_balance=500.0)
    assert out.num_admitted == 1
    t = out.trades[0]
    assert t.portfolio_lot == pytest.approx(0.1, abs=1e-6)
    assert t.net_pnl == pytest.approx(50.0, abs=1e-6)
    assert out.final_balance == pytest.approx(550.0, abs=1e-6)
    assert len(out.equity_curve) >= 2  # at least the seed point + one event


# --------------------------------------------------------------------------- EA-01 (integration)
def _candles(start, n, step_minutes, base=100.0, rng=0.5):
    idx = [start + timedelta(minutes=step_minutes * i) for i in range(n)]
    return pd.DataFrame({
        "open": [base] * n, "high": [base + rng / 2] * n,
        "low": [base - rng / 2] * n, "close": [base] * n, "volume": [100] * n,
    }, index=pd.DatetimeIndex(idx))


def _ea01_config():
    return {
        "atr_period": 14, "use_atr_filter": False, "entry_spread_buffer_pips": 0.0,
        "max_entry_distance_atr": 1.5, "invalidation_points": 0,
        "max_risk_percent_per_trade": 2.0, "base_lot_size": 0.02, "use_dynamic_sizing": True,
        "high_confidence_multiplier": 1.5, "medium_confidence_multiplier": 1.0,
        "low_confidence_multiplier": 0.7, "min_lot_size": 0.01, "max_lot_size": 0.1,
        "use_break_even": False, "use_trailing_stop": False, "use_ma_trailing_stop": False,
        "use_partial_closing": False, "record_mark_path": False, "zone_buffer_atr_multiplier": 0.5,
    }


def _ea01_strategy(first_m1_open, first_m1_high):
    cagg_start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cagg_df = _candles(cagg_start, 30, 15, base=100.0, rng=0.5)  # M15, ATR ~0.5
    m1_start = cagg_start + timedelta(minutes=30 * 15)
    # first M1 bar carries the breakout tick; keep two bars so the scan has room
    m1_df = pd.DataFrame({
        "open": [first_m1_open, first_m1_high], "high": [first_m1_high, first_m1_high],
        "low": [first_m1_open, first_m1_high], "close": [first_m1_high, first_m1_high],
        "volume": [100, 100],
    }, index=pd.DatetimeIndex([m1_start, m1_start + timedelta(minutes=1)]))
    strategy = SimpleNamespace(
        strategy_id=1, symbol="TEST", direction="long", timestamp=m1_start,
        expiry_time=m1_start + timedelta(minutes=100),
        entry_signal={"condition_type": "breakout_close", "level": 100.0,
                      "timeframe": "M15", "confirmation": "none"},
        take_profit=110.0, stop_loss=95.0, confidence="Medium", risk_reward_ratio=0.0,
        execution_allowed=True, trade_recommended=True, risk_level=None, trade_mode=None,
        strategy_name="EA01 test",
    )
    return strategy, cagg_df, m1_df


def test_ea01_blocks_entry_far_from_level():
    strategy, cagg_df, m1_df = _ea01_strategy(first_m1_open=100.0, first_m1_high=130.0)
    res = asyncio.run(simulate_strategy(
        strategy=strategy, cagg_df=cagg_df, m1_df=m1_df, ea_config=_ea01_config(),
        broker_spec=_spec(symbol="TEST"), run_id="t", current_balance=500.0,
    ))
    assert res.outcome == "blocked_max_distance"
    assert res.entry_time is None


def test_ea01_allows_entry_within_distance():
    strategy, cagg_df, m1_df = _ea01_strategy(first_m1_open=100.0, first_m1_high=100.3)
    res = asyncio.run(simulate_strategy(
        strategy=strategy, cagg_df=cagg_df, m1_df=m1_df, ea_config=_ea01_config(),
        broker_spec=_spec(symbol="TEST"), run_id="t", current_balance=500.0,
    ))
    assert res.outcome != "blocked_max_distance"
    assert res.entry_time is not None
