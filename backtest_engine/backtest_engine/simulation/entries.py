import pandas as pd
from typing import Dict, Any, Callable
from backtest_engine.simulation.indicators import (
    calculate_atr, is_bearish_engulfing, is_bullish_engulfing, 
    is_doji, is_hammer, is_shooting_star, is_pin_bar, is_bullish_pin_bar, is_bearish_pin_bar, is_liquidity_sweep
)

def check_confirmation_pattern(df: pd.DataFrame, shift: int, confirmation: str, direction: str, entry_level: float, current_price: float = None) -> bool:
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
        if direction == "long":
            return is_hammer(df, shift)
        else:
            return is_shooting_star(df, shift)
    elif confirmation == "shooting_star":
        if direction == "long":
            return is_hammer(df, shift)
        else:
            return is_shooting_star(df, shift)
    elif confirmation == "pin_bar":
        if direction == "long":
            return is_bullish_pin_bar(df, shift)
        else:
            return is_bearish_pin_bar(df, shift)
    elif confirmation == "liquidity_sweep":
        return is_liquidity_sweep(df, shift, direction, entry_level)
    elif confirmation == "close_above":
        # Always use the last definitively CLOSED candle (df.iloc[-2]) regardless of shift
        # to prevent look-ahead bias from the forming candle's partial close price.
        if len(df) < 2:
            return False
        return df['close'].iloc[-2] > entry_level
    elif confirmation == "close_below":
        # Always use the last definitively CLOSED candle (df.iloc[-2]) regardless of shift
        # to prevent look-ahead bias from the forming candle's partial close price.
        if len(df) < 2:
            return False
        return df['close'].iloc[-2] < entry_level
    elif confirmation == "rsi_divergence":
        return is_rsi_divergence(df, shift, direction)
    elif confirmation == "macd_cross":
        return is_macd_cross(df, shift, direction)
    elif confirmation == "volume_spike":
        return is_volume_spike(df, shift)
    elif confirmation == "engulfing":
        if direction == "long":
            return is_bullish_engulfing(df, shift)
        else:
            return is_bearish_engulfing(df, shift)
    
    return False

def is_rsi_divergence(df: pd.DataFrame, shift: int, direction: str) -> bool:
    if len(df) < shift + 14 + 10:
        return False
    # calculate RSI(14) using Wilder's RMA (ewm alpha=1/14) to match MT5 iRSI exactly
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    
    rsi_slice = rsi.iloc[-(shift + 10):-(shift) if shift > 0 else None][::-1]
    close_slice = df['close'].iloc[-(shift + 10):-(shift) if shift > 0 else None][::-1]
    if len(rsi_slice) < 10: return False
    
    if direction == "long":
        recent_price_slice = close_slice.iloc[0:5]
        past_price_slice = close_slice.iloc[5:10]
        
        recent_price_low_idx = recent_price_slice.argmin()
        past_price_low_idx = 5 + past_price_slice.argmin()
        
        recent_price_low = close_slice.iloc[recent_price_low_idx]
        past_price_low = close_slice.iloc[past_price_low_idx]
        
        recent_rsi_low = rsi_slice.iloc[recent_price_low_idx]
        past_rsi_low = rsi_slice.iloc[past_price_low_idx]
            
        return (recent_price_low < past_price_low) and (recent_rsi_low > past_rsi_low)
    else:
        recent_price_slice = close_slice.iloc[0:5]
        past_price_slice = close_slice.iloc[5:10]
        
        recent_price_high_idx = recent_price_slice.argmax()
        past_price_high_idx = 5 + past_price_slice.argmax()
        
        recent_price_high = close_slice.iloc[recent_price_high_idx]
        past_price_high = close_slice.iloc[past_price_high_idx]
        
        recent_rsi_high = rsi_slice.iloc[recent_price_high_idx]
        past_rsi_high = rsi_slice.iloc[past_price_high_idx]
            
        return (recent_price_high > past_price_high) and (recent_rsi_high < past_rsi_high)

def is_macd_cross(df: pd.DataFrame, shift: int, direction: str) -> bool:
    if len(df) < shift + 26 + 9 + 2: return False
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd_main = ema12 - ema26
    macd_signal = macd_main.rolling(window=9).mean()  # MT5 iMACD uses SMA not EMA for signal line
    
    idx0 = -(shift + 1)
    idx1 = -(shift + 2)
    
    if direction == "long":
        return (macd_main.iloc[idx0] > macd_signal.iloc[idx0]) and (macd_main.iloc[idx1] <= macd_signal.iloc[idx1])
    else:
        return (macd_main.iloc[idx0] < macd_signal.iloc[idx0]) and (macd_main.iloc[idx1] >= macd_signal.iloc[idx1])

def is_volume_spike(df: pd.DataFrame, shift: int) -> bool:
    if len(df) < shift + 11: return False
    vol_slice = df['tick_volume'].iloc[-(shift + 11):-(shift) if shift > 0 else None][::-1]
    if len(vol_slice) < 11: return False
    
    current_volume = vol_slice.iloc[0]
    avg_volume = vol_slice.iloc[1:11].mean()
    
    return current_volume > avg_volume * 1.5


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
    direction = signal_direction(signal)
    level = signal_level(signal)
    if level <= 0: return False
    if direction == "long":
        return (current_price > level) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level, current_price)
    else:
        return (current_price < level) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level, current_price)

def check_zone_retest(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    zone = signal.get('trigger_zone', [])
    if not is_zone_valid(zone): return False
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * 10.0 * point
    return (zone[0] - spread_buffer) <= current_price <= (zone[1] + spread_buffer)
    # No confirmation check for zone_retest as per PRD / EA parity

def check_pullback_entry(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    zone = signal.get('trigger_zone', [])
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * 10.0 * point
    if is_zone_valid(zone):
        return (zone[0] - spread_buffer) <= current_price <= (zone[1] + spread_buffer)
    
    mdp = signal.get('max_distance_pips')
    tolerance = (mdp if mdp is not None else 100) * 10.0 * point
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
    zone = signal.get('trigger_zone', [0, 0])
    if not is_zone_valid(zone): return False
    
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * 10.0 * point
    direction = signal_direction(signal)
    level = signal_level(signal)
    if direction == "long":
        return (current_price > (zone[1] + spread_buffer)) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level, current_price)
    else:
        return (current_price < (zone[0] - spread_buffer)) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level, current_price)

def check_liquidity_grab(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2: return False
    direction = signal_direction(signal)
    level = signal_level(signal)
    return is_liquidity_sweep(df, 1, direction, level) and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level, current_price)

def check_news_spike_reversal(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2 or pd.isna(atr) or atr <= 0: return False
    c = df.iloc[-2]
    range_ = c['high'] - c['low']
    
    return (
        range_ >= atr * 1.2 and 
        is_liquidity_sweep(df, 1, signal_direction(signal), signal_level(signal)) and 
        check_confirmation_pattern(df, 1, signal.get('confirmation'), signal_direction(signal), signal_level(signal), current_price)
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
        return current_price > level and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level, current_price)
    return current_price < level and check_confirmation_pattern(df, 1, signal.get('confirmation'), direction, level, current_price)

def check_vwap_bounce(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_zone_retest(df, current_price, signal, point, atr) and check_confirmation_pattern(df, 1, signal.get('confirmation'), signal_direction(signal), signal_level(signal), current_price)

def check_price_rejection(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 2: return False
    
    zone = signal.get('trigger_zone', [])
    if not is_zone_valid(zone): return False
    
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * 10.0 * point
    c_prev = df.iloc[-2]  # shift 1: last closed candle
    # Check if the last closed candle touched the zone
    touched_zone_prev = (c_prev['low'] <= (zone[1] + spread_buffer)) and (c_prev['high'] >= (zone[0] - spread_buffer))
    # Also check if current tick price is inside the zone
    touched_zone_current = (zone[0] - spread_buffer) <= current_price <= (zone[1] + spread_buffer)
    
    # Determine which candle actually touched the zone so confirmation is on the right candle
    if touched_zone_prev:
        # The last completed candle (shift=1) touched — confirm using that same candle
        confirmation_shift = 1
        touched_zone = True
    elif touched_zone_current:
        # Current tick is inside zone — use the current (forming) candle (shift=0)
        confirmation_shift = 0
        touched_zone = True
    else:
        return False
        
    max_dist_pips = signal.get('max_distance_pips', 0)
    if max_dist_pips > 0:
        max_dist = max_dist_pips * 10.0 * point
    else:
        zone_width = abs(zone[1] - zone[0])
        max_dist = zone_width * 0.5
        
    direction = signal_direction(signal)
    if zone[0] <= current_price <= zone[1]:
        dist_from_zone = 0.0
    elif direction == "long":
        dist_from_zone = abs(current_price - zone[1])
    else:
        dist_from_zone = abs(current_price - zone[0])
        
    if dist_from_zone > (max_dist + spread_buffer):
        return False
        
    # Check confirmation on the SAME candle that touched the zone
    return check_confirmation_pattern(df, confirmation_shift, signal.get('confirmation'), direction, signal_level(signal))


def check_mean_reversion(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_zone_retest(df, current_price, signal, point, atr) and check_confirmation_pattern(df, 1, signal.get('confirmation'), signal_direction(signal), signal_level(signal), current_price)

def check_session_open_surge(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) == 0: return False
    last_time = df.index[-1]
    is_session_open = False
    if last_time.hour == 8 and last_time.minute < 30: is_session_open = True
    if last_time.hour == 13 and last_time.minute < 30: is_session_open = True
    if last_time.hour == 0 and last_time.minute < 30: is_session_open = True
    
    if not is_session_open: return False
    return check_zone_retest(df, current_price, signal, point, atr)

def check_trendline_bounce(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_zone_retest(df, current_price, signal, point, atr) and check_confirmation_pattern(df, 1, signal.get('confirmation'), signal_direction(signal), signal_level(signal), current_price)

def check_fibonacci_rejection(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_price_rejection(df, current_price, signal, point, atr)

def check_order_block_tap(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_zone_retest(df, current_price, signal, point, atr) and check_confirmation_pattern(df, 1, signal.get('confirmation'), signal_direction(signal), signal_level(signal), current_price)

def check_fvg_fill(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    return check_pullback_entry(df, current_price, signal, point, atr)

def check_indicator_cross(df: pd.DataFrame, current_price: float, signal: dict, point: float, atr: float) -> bool:
    if len(df) < 3: return False
    ema9 = df['close'].ewm(span=9, adjust=False).mean()
    ema21 = df['close'].ewm(span=21, adjust=False).mean()
    
    idx0 = -2
    idx1 = -3
    
    direction = signal_direction(signal)
    if direction == "long":
        return (ema9.iloc[idx0] > ema21.iloc[idx0]) and (ema9.iloc[idx1] <= ema21.iloc[idx1])
    else:
        return (ema9.iloc[idx0] < ema21.iloc[idx0]) and (ema9.iloc[idx1] >= ema21.iloc[idx1])


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
    "price_rejection": check_price_rejection,
    "mean_reversion": check_mean_reversion,
    "session_open_surge": check_session_open_surge,
    "trendline_bounce": check_trendline_bounce,
    "fibonacci_rejection": check_fibonacci_rejection,
    "order_block_tap": check_order_block_tap,
    "fvg_fill": check_fvg_fill,
    "indicator_cross": check_indicator_cross
}

def check_for_invalidation(current_price: float, signal: dict, point: float, invalidation_points: float, zone_touched: bool) -> tuple[bool, bool]:
    ctype = signal.get('condition_type')
    if ctype not in ["pullback_entry", "price_rejection", "zone_retest"]:
        return False, zone_touched
        
    spread_buffer = float(signal.get('entry_spread_buffer_pips', 0.0)) * 10.0 * point
    zone = signal.get('trigger_zone', [])
    if is_zone_valid(zone):
        entry_bottom = zone[0] - spread_buffer
        entry_top = zone[1] + spread_buffer
    else:
        mdp = signal.get('max_distance_pips')
        tolerance = (mdp if mdp is not None else 100) * 10.0 * point
        level = signal_level(signal)
        entry_bottom = level - (tolerance + spread_buffer)
        entry_top = level + (tolerance + spread_buffer)
        
    if entry_bottom <= current_price <= entry_top:
        return False, True

    # --- Distance-based invalidation ---
    # If price has moved too far from the zone/level, the setup is invalidated.
    if invalidation_points > 0 and point > 0:
        # invalidation_points is in raw broker points (e.g. 50 points = 5 pips on 5-digit broker)
        invalidation_distance = invalidation_points * point
        # Measure distance from the nearest boundary of the entry zone
        if is_zone_valid(zone):
            if current_price < entry_bottom:
                distance_from_zone = entry_bottom - current_price
            elif current_price > entry_top:
                distance_from_zone = current_price - entry_top
            else:
                distance_from_zone = 0.0
        else:
            level = signal_level(signal)
            distance_from_zone = abs(current_price - level)
        if distance_from_zone > invalidation_distance:
            return True, zone_touched
        
    pre_entry_rule = signal.get('pre_entry_rule')
    if pre_entry_rule:
        import json
        if isinstance(pre_entry_rule, str):
            try:
                pre_entry_rule = json.loads(pre_entry_rule)
            except json.JSONDecodeError:
                pre_entry_rule = {}
                
        rule_type = pre_entry_rule.get('rule_type')
        params = pre_entry_rule.get('params', {})
        if 'level' in params:
            level_val = float(params['level'])
            if rule_type == 'close_above' and current_price > level_val:
                return True, zone_touched
            if rule_type == 'close_below' and current_price < level_val:
                return True, zone_touched
            
    return False, zone_touched

