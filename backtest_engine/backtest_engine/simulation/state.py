import logging
import pandas as pd
from typing import Any

from backtest_engine.models import BacktestResult
from backtest_engine.broker_specs import BrokerSymbolSpec
from backtest_engine.simulation.entries import ENTRY_CONDITIONS, check_for_invalidation
from backtest_engine.simulation.fills import evaluate_bar_exits
from backtest_engine.simulation.management import (
    check_break_even,
    check_trailing_stop,
    check_partial_close,
    check_ma_trailing_stop,
    moving_average_value,
)
from backtest_engine.simulation.pnl import calculate_pnl, calculate_commission, calculate_r_multiple
from backtest_engine.simulation.risk import calculate_lot_size
from backtest_engine.simulation.indicators import calculate_atr
from backtest_engine.simulation.strategy_context import build_signal_context

logger = logging.getLogger(__name__)


def _signal_debug_payload(signal: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "strategy_id",
        "strategy_name",
        "symbol",
        "direction",
        "condition_type",
        "timeframe",
        "confirmation",
        "entry_level",
        "entry_reference_price",
        "trigger_zone",
        "trigger_zone_source",
        "confirmation_required",
        "max_distance_pips",
        "confidence",
        "risk_reward_ratio",
        "execution_allowed",
        "trade_recommended",
        "risk_level",
        "trade_mode",
        "take_profit",
        "stop_loss",
    ):
        if key in signal and signal[key] is not None:
            value = signal[key]
            if key == "trigger_zone" and isinstance(value, list) and len(value) >= 2:
                payload[key] = [float(value[0]), float(value[1])]
            else:
                payload[key] = value
    return {"signal": payload}


def _timeframe_delta(timeframe: str | None) -> Any:
    mapping = {
        "M1": pd.Timedelta(minutes=1),
        "M5": pd.Timedelta(minutes=5),
        "M15": pd.Timedelta(minutes=15),
        "M30": pd.Timedelta(minutes=30),
        "H1": pd.Timedelta(hours=1),
        "H4": pd.Timedelta(hours=4),
        "D1": pd.DateOffset(days=1),
        "W1": pd.Timedelta(weeks=1),
        "MN1": pd.DateOffset(months=1),
    }
    return mapping.get((timeframe or "M15").upper(), pd.Timedelta(minutes=15))


def _closed_history_with_forming_placeholder(
    cagg_df: pd.DataFrame,
    *,
    closed_idx: int,
    current_ts: Any,
) -> pd.DataFrame:
    """Return closed HTF history plus a harmless forming placeholder row.

    The entry helpers mirror MQL's `shift=1` indexing by reading `df.iloc[-2]`.
    In MT5, index 0 is the current forming candle and index 1 is the previous
    closed candle. Our DB only has completed CAGG candles, so we append a
    duplicate placeholder after the last closed candle. Entry helpers never use
    its OHLC values; it only preserves the same indexing contract without
    peeking into a future completed CAGG bucket.
    """
    closed = cagg_df.iloc[: closed_idx + 1]
    if closed.empty:
        return closed
    placeholder = closed.iloc[[-1]].copy()
    placeholder.index = pd.DatetimeIndex([current_ts])
    return pd.concat([closed, placeholder])


def _passes_atr_filter(
    strategy: Any,
    atr_series: pd.Series,
    cagg_idx: int,
    ea_config: dict,
) -> bool:
    if not ea_config.get("use_atr_filter", False):
        return True

    strategy_name = str(getattr(strategy, "strategy_name", "") or "")
    if any(token in strategy_name for token in ("Mean Reversion", "Fade", "Rejection")):
        return True

    lookback = int(ea_config.get("atr_lookback", 20) or 20)
    if lookback <= 0 or cagg_idx < lookback - 1:
        return False

    window = atr_series.iloc[cagg_idx - lookback + 1 : cagg_idx + 1].dropna()
    if len(window) < lookback:
        return False

    current_atr = float(window.iloc[-1])
    average_atr = float(window.mean())
    return current_atr >= average_atr * float(ea_config.get("min_atr_multiplier", 0.8))

async def simulate_strategy(
    strategy: Any,
    cagg_df: pd.DataFrame,
    m1_df: pd.DataFrame,
    ea_config: dict,
    broker_spec: BrokerSymbolSpec,
    run_id: str,
    current_balance: float
) -> BacktestResult:
    """
    Simulate a single strategy against historical data.
    """
    raw_entry_signal = strategy.entry_signal or {}
    ctype = raw_entry_signal.get("condition_type")
    direction = str(getattr(strategy, "direction", "") or "").strip().lower()
    
    res = BacktestResult(
        run_id=run_id,
        strategy_id=strategy.strategy_id,
        strategy_hash="hash_placeholder",
        profile_hash="profile_placeholder",
        symbol=strategy.symbol,
        direction=direction,
        condition_type=ctype,
        timeframe=raw_entry_signal.get("timeframe"),
        confirmation=raw_entry_signal.get("confirmation", "none"),
        strategy_timestamp=strategy.timestamp,
        strategy_expiry_time=strategy.expiry_time,
        outcome="error",
        outcome_reason="unknown",
        gross_pnl=0.0,
        net_pnl=0.0,
        commission=0.0,
        swap=0.0,
        balance_before=current_balance,
        balance_after=current_balance,
        partial_close_executed=False,
        break_even_moved=False,
        hit_tp=False,
        hit_sl=False
    )
    
    # Check config restrictions
    if not broker_spec.exists or not broker_spec.selected:
        res.outcome = "missing_broker_specs"
        res.outcome_reason = "Symbol not selected in broker specs"
        return res
        
    if not strategy.entry_signal:
        res.outcome = "error"
        res.outcome_reason = "No entry signal data"
        return res

    if m1_df.empty or cagg_df.empty:
        res.outcome = "missing_candles"
        res.outcome_reason = "No candle data"
        return res

    atr_series = calculate_atr(cagg_df, period=ea_config.get("atr_period", 14))
    context = build_signal_context(strategy, atr_series=atr_series, ea_config=ea_config)
    if context.error:
        res.outcome = "error"
        res.outcome_reason = context.error
        return res
    sig = context.data
    
    sym = strategy.symbol
    buffer = ea_config.get("entry_spread_buffer_pips", 0.0)
    buffer_by_symbol = ea_config.get("entry_spread_buffer_pips_by_symbol", {})
    if sym in buffer_by_symbol:
        buffer = buffer_by_symbol[sym]
    sig["entry_spread_buffer_pips"] = buffer
    ctype = sig.get("condition_type")
    direction = sig["direction"]
    stop_loss = float(sig.get("stop_loss", 0.0) or 0.0)
    take_profit = float(sig.get("take_profit", 0.0) or 0.0)
    confidence = sig.get("confidence") or "Medium"
    risk_reward_ratio = float(sig.get("risk_reward_ratio", 0.0) or 0.0)
    
    res.condition_type = ctype
    res.direction = direction
    res.timeframe = sig.get("timeframe")
    res.confirmation = sig.get("confirmation", "none")
    res.debug = _signal_debug_payload(sig)

    if not sig.get("execution_allowed", True):
        res.outcome = "rejected_execution_not_allowed"
        res.outcome_reason = "execution_allowed is false"
        return res

    if not sig.get("trade_recommended", True):
        res.outcome = "rejected_not_recommended"
        res.outcome_reason = "trade_recommended is false"
        return res

    if ctype not in ENTRY_CONDITIONS:
        res.outcome = "unsupported_condition_type"
        res.outcome_reason = f"Unsupported condition: {ctype}"
        return res

    entry_func = ENTRY_CONDITIONS[ctype]
    tf_delta = _timeframe_delta(sig.get("timeframe"))
    
    # We step through the M1 timeframe candles for the entry trigger
    # ensuring exact tick-by-tick entry parity
    sim_m1_df = m1_df[m1_df.index >= strategy.timestamp]
    if sim_m1_df.empty:
        res.outcome = "missing_candles"
        res.outcome_reason = "No M1 data after strategy timestamp"
        return res

    entry_time = None
    entry_price = 0.0
    
    triggered = False
    invalidated = False
    
    cagg_idx = -1
    cagg_times = cagg_df.index
    cagg_len = len(cagg_df)
    
    bars_scanned = 0
    zone_touched = False
    
    for ts, bar in sim_m1_df.iterrows():
        bars_scanned += 1
        # Expiry Enforcement
        if ts > strategy.expiry_time:
            break
            
        current_price = bar['close']
        
        # Advance to the latest higher-timeframe candle that is fully closed.
        while cagg_idx < cagg_len - 1 and cagg_times[cagg_idx + 1] + tf_delta <= ts:
            cagg_idx += 1
            
        if cagg_idx < 0:
            continue
            
        hist_df = _closed_history_with_forming_placeholder(cagg_df, closed_idx=cagg_idx, current_ts=ts)
        atr_val = atr_series.iloc[cagg_idx]
        
        # Check invalidation
        invalidated_step, zone_touched = check_for_invalidation(
            current_price, sig, broker_spec.point, ea_config.get("invalidation_points", 50), zone_touched
        )
        if invalidated_step:
            invalidated = True
            break

        if not _passes_atr_filter(strategy, atr_series, cagg_idx, ea_config):
            continue
            
        if entry_func(hist_df, current_price, sig, broker_spec.point, atr_val):
            triggered = True
            entry_time = ts
            entry_price = current_price
            break
            
    if invalidated:
        res.outcome = "invalidated_without_entry"
        res.outcome_reason = "Zone touched and moved away"
        res.bars_scanned = bars_scanned
        return res
        
    if not triggered:
        res.outcome = "expired_without_entry"
        res.outcome_reason = "Did not trigger before expiry"
        res.bars_scanned = bars_scanned
        return res
        
    # --- ENTER TRADE ---
    # Filter M1 df from entry_time onwards
    m1_trade_df = sim_m1_df[sim_m1_df.index > entry_time]
    
    lot_size = calculate_lot_size(
        current_balance, 
        ea_config.get("max_risk_percent_per_trade", ea_config.get("risk_percent", 2.0)),
        entry_price,
        stop_loss,
        broker_spec,
        confidence=confidence,
        risk_reward_ratio=risk_reward_ratio,
        base_lot_size=ea_config.get("base_lot_size", 0.02),
        use_dynamic_sizing=ea_config.get("use_dynamic_sizing", True),
        high_confidence_multiplier=ea_config.get("high_confidence_multiplier", 1.5),
        medium_confidence_multiplier=ea_config.get("medium_confidence_multiplier", 1.0),
        low_confidence_multiplier=ea_config.get("low_confidence_multiplier", 0.7),
        min_lot_size=ea_config.get("min_lot_size", 0.01),
        max_lot_size=ea_config.get("max_lot_size", 0.1),
    )
    
    if lot_size <= 0:
        res.outcome = "rejected_lot_size"
        res.outcome_reason = "Calculated lot size violates broker constraints"
        res.bars_scanned = bars_scanned
        return res
        
    res.entry_time = entry_time
    res.entry_price = entry_price
    res.lot_size = lot_size
    res.initial_stop_loss = stop_loss
    res.take_profit = take_profit
    
    commission = calculate_commission(lot_size, broker_spec.commission_per_lot_round_turn_assumption)
    res.commission = -commission
    
    initial_risk_val = calculate_pnl(entry_price, stop_loss, lot_size, direction, broker_spec.tick_size, broker_spec.tick_value)
    initial_risk_val = abs(initial_risk_val)
    
    current_sl = stop_loss
    tp = take_profit
    break_even_moved = False
    partial_closed = False
    
    max_fav = 0.0
    max_adv = 0.0
    
    # Process M1 bars for exits and management
    exit_type = None
    exit_price = None
    exit_time = None
    
    # Partial close variables
    use_partial_close = ea_config.get("use_partial_closing", True)
    partial_close_percent = ea_config.get("partial_close_percent", 50.0)
    original_lot = lot_size
    current_lot = lot_size
    realized_gross_pnl = 0.0
    original_sl_distance = abs(entry_price - stop_loss)
    
    for ts, bar in m1_trade_df.iterrows():
        bars_scanned += 1

        while cagg_idx < cagg_len - 1 and cagg_times[cagg_idx + 1] + tf_delta <= ts:
            cagg_idx += 1
        
        # Update excursions
        if direction == "long":
            fav = bar['high'] - entry_price
            adv = entry_price - bar['low']
        else:
            fav = entry_price - bar['low']
            adv = bar['high'] - entry_price
            
        if fav > max_fav: max_fav = fav
        if adv > max_adv: max_adv = adv
            
        # Check exits
        exit_reason = evaluate_bar_exits(
            bar['open'], bar['high'], bar['low'], bar['close'],
            direction, tp, current_sl
        )
        
        if exit_reason == "sl":
            exit_type = "sl"
            exit_time = ts
            exit_price = current_sl
            break
        elif exit_reason == "tp":
            exit_type = "tp"
            exit_time = ts
            exit_price = tp
            break
            
        if cagg_idx >= 0:
            management_hist_df = cagg_df.iloc[: cagg_idx + 1]
        else:
            management_hist_df = pd.DataFrame()

        use_ma_trailing = ea_config.get("use_ma_trailing_stop", False)
        use_trailing = ea_config.get("use_trailing_stop", False)
        use_break_even = ea_config.get("use_break_even", True)

        # Match the EA management priority:
        # 1. MA trailing overrides all other SL management.
        # 2. Hybrid BE + fixed trailing moves to profit lock at trailing start,
        #    then only trails on later ticks.
        # 3. Standalone one-time break-even.
        if use_ma_trailing:
            ma_value = moving_average_value(
                management_hist_df,
                ea_config.get("ma_trail_period", 21),
                ea_config.get("ma_trail_method", "ema"),
            )
            new_sl = check_ma_trailing_stop(
                bar['close'],
                entry_price,
                current_sl,
                direction,
                original_sl_distance,
                ma_value,
                ea_config.get("ma_trail_min_step_percent", 5.0),
                ea_config.get("break_even_percent", 20.0),
            )
            if new_sl != current_sl:
                current_sl = new_sl
                break_even_moved = True
                res.break_even_moved = True
        elif use_trailing and use_break_even:
            if not break_even_moved:
                be_moved, current_sl = check_break_even(
                    bar['close'],
                    entry_price,
                    current_sl,
                    direction,
                    original_sl_distance,
                    ea_config.get("trailing_start_percent", 100.0),
                    break_even_moved,
                    current_lot,
                    broker_spec.commission_per_lot_round_turn_assumption,
                    broker_spec.tick_value,
                    broker_spec.tick_size,
                    broker_spec.point,
                    ea_config.get("break_even_spread_buffer_points", 0.5),
                )
                if be_moved:
                    break_even_moved = True
                    res.break_even_moved = True
            else:
                current_sl = check_trailing_stop(
                    bar['close'],
                    current_sl,
                    direction,
                    original_sl_distance,
                    ea_config.get("trailing_start_percent", 100.0),
                    ea_config.get("trailing_step_percent", 50.0),
                )
        elif use_break_even and not break_even_moved:
            atr_threshold = None
            if ea_config.get("break_even_atr_multiplier", 0) and cagg_idx >= 0:
                atr_value = atr_series.iloc[cagg_idx]
                if pd.notna(atr_value) and atr_value > 0:
                    atr_threshold = float(atr_value) * float(ea_config.get("break_even_atr_multiplier", 0))

            be_moved, current_sl = check_break_even(
                bar['close'],
                entry_price,
                current_sl,
                direction,
                original_sl_distance,
                ea_config.get("break_even_percent", 20.0),
                break_even_moved,
                current_lot,
                broker_spec.commission_per_lot_round_turn_assumption,
                broker_spec.tick_value,
                broker_spec.tick_size,
                broker_spec.point,
                ea_config.get("break_even_spread_buffer_points", 0.5),
                threshold_distance=atr_threshold,
            )
            if be_moved:
                break_even_moved = True
                res.break_even_moved = True

        # Partial closing runs independently after SL management, matching EA.
        if use_partial_close and not partial_closed and original_sl_distance > 0:
            pc_triggered, close_volume = check_partial_close(
                bar['close'], entry_price, direction, original_sl_distance,
                original_lot, partial_close_percent, partial_closed,
                broker_spec.volume_min, broker_spec.volume_step
            )
            if pc_triggered:
                partial_closed = True
                res.partial_close_executed = True
                if close_volume > 0 and close_volume < current_lot:
                    partial_pnl = calculate_pnl(entry_price, bar['close'], close_volume, direction, broker_spec.tick_size, broker_spec.tick_value)
                    realized_gross_pnl += partial_pnl
                    current_lot -= close_volume
            
    res.bars_scanned = bars_scanned
    res.final_stop_loss = current_sl
    
    if exit_type:
        res.exit_time = exit_time
        res.exit_price = exit_price
        
        final_pnl = calculate_pnl(entry_price, exit_price, current_lot, direction, broker_spec.tick_size, broker_spec.tick_value)
        total_gross_pnl = realized_gross_pnl + final_pnl
        
        res.gross_pnl = total_gross_pnl
        res.net_pnl = total_gross_pnl + res.commission + res.swap
        
        if exit_type == "sl":
            res.hit_sl = True
            if break_even_moved or (current_sl != stop_loss):
                res.outcome = "closed_trailing_sl"
                res.outcome_reason = "Hit trailing/BE stop loss"
            else:
                res.outcome = "closed_sl"
                res.outcome_reason = "Hit stop loss"
        else:
            res.hit_tp = True
            res.outcome = "closed_tp"
            res.outcome_reason = "Hit take profit"
            
        res.balance_after = res.balance_before + float(res.net_pnl)
        
        # We can approximate pnl_pips
        if direction == "long":
            res.pnl_pips = (exit_price - entry_price) / broker_spec.point / 10
        else:
            res.pnl_pips = (entry_price - exit_price) / broker_spec.point / 10
            
        if initial_risk_val > 0:
            res.r_multiple = float(total_gross_pnl / initial_risk_val)
        else:
            res.r_multiple = 0.0
            
    else:
        res.outcome = "open_at_data_end"
        res.outcome_reason = "End of replay data; unrealized PnL excluded"
        res.net_pnl = res.commission + res.swap
        res.balance_after = res.balance_before + float(res.net_pnl)
        
    return res
