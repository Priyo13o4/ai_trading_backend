import pandas as pd
from typing import Dict, Any, Callable
from backtest_engine.simulation.indicators import (
    calculate_atr, is_bearish_engulfing, is_bullish_engulfing, 
    is_doji, is_hammer, is_pin_bar, is_liquidity_sweep
)

def check_confirmation_pattern(df: pd.DataFrame, shift: int, confirmation: str, direction: str, entry_level: float) -> bool:
    if confirmation == "none" or not confirmation:
        return True
    
    if len(df) <= shift:
        return False
        
    c = df.iloc[-(shift + 1)]
        
    if confirmation == "bearish_engulfing":
        return is_bearish_engulfing(df, shift)
    elif confirmation == "bullish_engulfing":
        return is_bullish_engulfing(df, shift)
    elif confirmation == "doji":
        return is_doji(df, shift)
    elif confirmation == "hammer":
        return is_hammer(df, shift)
    elif confirmation == "pin_bar":
        return is_pin_bar(df, shift)
    elif confirmation == "liquidity_sweep":
        return is_liquidity_sweep(df, shift, direction, entry_level)
    elif confirmation == "close_above":
        return c['close'] > entry_level
    elif confirmation == "close_below":
        return c['close'] < entry_level
    
    return False


def is_zone_valid(trigger_zone: list) -> bool:
    if not trigger_zone or len(trigger_zone) < 2:
        return False
    return trigger_zone[0] > 0 and trigger_zone[1] > 0 and trigger_zone[0] < trigger_zone[1]


def signal_level(signal: dict) -> float:
    return float(signal.get("level") or signal.get("entry_level") or 0.0)


def signal_direction(signal: dict) -> str:
    return str(signal.get("direction") or "").lower()


# Entry conditions

def check_immediate(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return True

def check_breakout_close(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2: return False
    close = df.iloc[-2]['close'] # shift 1
    direction = signal_direction(signal)
    level = signal_level(signal)
    if level <= 0: return False
    if direction == "long":
        return (close > level) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level)
    else:
        return (close < level) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level)

def check_zone_retest(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    zone = signal.get('trigger_zone', [])
    if not is_zone_valid(zone): return False
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * point * 10.0
    return (zone[0] - spread_buffer) <= current_price <= (zone[1] + spread_buffer)
    # No confirmation check for zone_retest as per PRD / EA parity

def check_pullback_entry(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    zone = signal.get('trigger_zone', [])
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * point * 10.0
    if is_zone_valid(zone):
        return (zone[0] - spread_buffer) <= current_price <= (zone[1] + spread_buffer)
    
    tolerance = (signal.get('max_distance_pips', 100) or 100) * (point * 10.0)
    level = signal_level(signal)
    return level > 0 and abs(current_price - level) <= (tolerance + spread_buffer)

def check_break_and_retest(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_pullback_entry(df, current_price, signal, point, atr)

def check_momentum_spike(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2 or pd.isna(atr): return False
    c = df.iloc[-2]
    range_ = c['high'] - c['low']
    return range_ > atr * 1.5

def check_range_breakout(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2: return False
    close = df.iloc[-2]['close']
    zone = signal.get('trigger_zone', [0, 0])
    if not is_zone_valid(zone): return False
    
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * point * 10.0
    direction = signal_direction(signal)
    level = signal_level(signal)
    if direction == "long":
        return (close > (zone[1] + spread_buffer)) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level)
    else:
        return (close < (zone[0] - spread_buffer)) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level)

def check_liquidity_grab(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2: return False
    direction = signal_direction(signal)
    level = signal_level(signal)
    return is_liquidity_sweep(df, 1, direction, level) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level)

def check_news_spike_reversal(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2 or pd.isna(atr) or atr <= 0: return False
    c = df.iloc[-2]
    range_ = c['high'] - c['low']
    
    return (
        range_ >= atr * 1.2 and 
        is_liquidity_sweep(df, 1, signal_direction(signal), signal_level(signal)) and 
        check_confirmation_pattern(df, 1, signal.get('confirmation'), signal_direction(signal), signal_level(signal))
    )

def check_volatility_contraction(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2 or pd.isna(atr) or atr <= 0: return False
    c = df.iloc[-2]
    range_ = c['high'] - c['low']
    contracted = range_ <= atr * 0.8
    if not contracted: return False
    
    direction = signal_direction(signal)
    level = signal_level(signal)
    if direction == "long":
        return c['close'] > level and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level)
    return c['close'] < level and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level)

def check_vwap_bounce(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_zone_retest(df, current_price, signal, point, atr) and check_confirmation_pattern(df, 1, signal.get('confirmation'), signal_direction(signal), signal_level(signal))

def check_price_rejection(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2: return False
    
    zone = signal.get('trigger_zone', [])
    if not is_zone_valid(zone): return False
    
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * point * 10.0
    c = df.iloc[-2] # shift 1
    touched_zone = (c['low'] <= (zone[1] + spread_buffer)) and (c['high'] >= (zone[0] - spread_buffer))
    if not touched_zone:
        return False
        
    max_dist_pips = signal.get('max_distance_pips', 0)
    if max_dist_pips > 0:
        max_dist = max_dist_pips * (point * 10.0)
    else:
        zone_width = abs(zone[1] - zone[0])
        max_dist = zone_width * 0.5
        
    direction = signal_direction(signal)
    if direction == "long":
        dist_from_zone = abs(current_price - zone[0])
    else:
        dist_from_zone = abs(current_price - zone[1])
        
    if dist_from_zone > (max_dist + spread_buffer):
        return False
        
    return check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, signal_level(signal))


ENTRY_CONDITIONS: Dict[str, Callable] = {
    "immediate": check_immediate,
    "breakout_close": check_breakout_close,
    "zone_retest": check_zone_retest,
    "pullback_entry": check_pullback_entry,
    "break_and_retest": check_break_and_retest,
    "momentum_spike": check_momentum_spike,
    "range_breakout": check_range_breakout,
    "liquidity_grab": check_liquidity_grab,
    "news_spike_reversal": check_news_spike_reversal,
    "volatility_contraction": check_volatility_contraction,
    "vwap_bounce": check_vwap_bounce,
    "price_rejection": check_price_rejection
}

def check_for_invalidation(current_price: float, signal: dict, point: float, invalidation_points: float, zone_touched: bool) -> tuple[bool, bool]:
    ctype = signal.get('condition_type')
    if ctype not in ["pullback_entry", "price_rejection", "zone_retest"]:
        return False, zone_touched
        
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * point * 10.0
    zone = signal.get('trigger_zone', [])
    if is_zone_valid(zone):
        entry_bottom = zone[0] - spread_buffer
        entry_top = zone[1] + spread_buffer
    else:
        tolerance = (signal.get('max_distance_pips', 100) or 100) * (point * 10.0)
        level = signal_level(signal)
        entry_bottom = level - (tolerance + spread_buffer)
        entry_top = level + (tolerance + spread_buffer)
        
    if entry_bottom <= current_price <= entry_top:
        return False, True
        
    if zone_touched:
        inval_dist = invalidation_points * point
        if current_price > entry_top:
            dist = current_price - entry_top
        else:
            dist = entry_bottom - current_price
            
        if dist > inval_dist:
            return True, zone_touched
            
    return False, zone_touched
