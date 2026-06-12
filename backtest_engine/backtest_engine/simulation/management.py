import math

def normalize_stop_level(
    desired_sl: float,
    current_price: float,
    direction: str,
    stop_level_points: float = 0.0,
    point: float = 0.0,
    entry_price: float = 0.0
) -> float:
    # Enforce at minimum 20 points (2 pips) stop distance to simulate real broker constraints
    broker_min_points = max(stop_level_points, 20.0)
    min_distance_price = broker_min_points * point
    if direction == "long":
        max_allowed_sl = current_price - min_distance_price
        if desired_sl > max_allowed_sl:
            valid_sl = max_allowed_sl
            if entry_price > 0 and valid_sl < entry_price:
                return 0.0
            return valid_sl
    else:
        min_allowed_sl = current_price + min_distance_price
        if desired_sl < min_allowed_sl:
            valid_sl = min_allowed_sl
            if entry_price > 0 and valid_sl > entry_price:
                return 0.0
            return valid_sl
    return desired_sl

def check_break_even(
    current_price: float,
    entry_price: float,
    stop_loss: float,
    direction: str,
    original_sl_distance: float,
    total_tp_distance: float,
    break_even_percent: float,
    break_even_moved: bool,
    lots: float,
    commission_per_lot: float,
    tick_value: float,
    tick_size: float,
    point: float,
    spread_buffer_points: float = 0.5,
    swap_cost: float = 0.0,
    threshold_distance: float | None = None,
) -> tuple[bool, float]:
    """Check if Break Even should be triggered using true-cost offset."""
    if break_even_percent <= 0 or break_even_moved or original_sl_distance <= 0:
        return False, stop_loss
        
    profit_distance = (current_price - entry_price) if direction == "long" else (entry_price - current_price)
    threshold_dist = threshold_distance
    if threshold_dist is None:
        threshold_dist = original_sl_distance * (break_even_percent / 100.0)
        
    force_be = (total_tp_distance > 0 and profit_distance >= 0.8 * total_tp_distance)
    
    if profit_distance >= threshold_dist or force_be:
        commission_cost = lots * commission_per_lot
        value_per_price_unit = (tick_value / tick_size) * lots if tick_size > 0 else 0
        
        cost_offset_price = 0.0
        if value_per_price_unit > 0:
            cost_offset_price = ((commission_cost + abs(swap_cost)) / value_per_price_unit)
            
        cost_offset_price += (spread_buffer_points * point)
        
        if direction == "long":
            new_sl = entry_price + cost_offset_price
            new_sl = normalize_stop_level(new_sl, current_price, direction, 0, point, entry_price)
            if (stop_loss == 0 or new_sl > stop_loss) and new_sl > 0:
                return True, new_sl
        else:
            new_sl = entry_price - cost_offset_price
            new_sl = normalize_stop_level(new_sl, current_price, direction, 0, point, entry_price)
            if (stop_loss == 0 or new_sl < stop_loss) and new_sl > 0:
                return True, new_sl
                
    return False, stop_loss


def check_trailing_stop(
    current_price: float,
    current_sl: float,
    direction: str,
    original_sl_distance: float,
    total_tp_distance: float,
    trailing_start_percent: float,
    trailing_step_percent: float,
    entry_price: float,
    point: float = 0.0
) -> float:
    """Calculate new SL based on trailing stop parameters matching MT5 Stage B."""
    profit_distance = (current_price - entry_price) if direction == "long" else (entry_price - current_price)
    force_trail = (total_tp_distance > 0 and profit_distance >= 0.8 * total_tp_distance)
    
    if trailing_start_percent <= 0 or original_sl_distance <= 0:
        return current_sl  # Cannot trail without a valid SL distance — force_trail cannot override this
        
    if profit_distance < (original_sl_distance * (trailing_start_percent / 100.0)) and not force_trail:
        return current_sl
    
    trail_distance = original_sl_distance * (trailing_start_percent / 100.0)
    step_distance = original_sl_distance * (trailing_step_percent / 100.0)
    
    proposed_sl = current_sl
    
    if direction == "long":
        potential_new_sl = current_price - trail_distance
        if potential_new_sl > current_sl:
            distance_diff = potential_new_sl - current_sl
            if distance_diff >= step_distance or force_trail:
                proposed_sl = potential_new_sl
    else:
        potential_new_sl = current_price + trail_distance
        if current_sl == 0 or potential_new_sl < current_sl:
            distance_diff = step_distance if current_sl == 0 else (current_sl - potential_new_sl)
            if distance_diff >= step_distance or force_trail:
                proposed_sl = potential_new_sl
                
    if proposed_sl != current_sl:
        valid_sl = normalize_stop_level(proposed_sl, current_price, direction, 0, point, entry_price)
        # Check backward trailing prevention again after normalization
        if direction == "long" and valid_sl > current_sl and valid_sl > 0:
            return valid_sl
        if direction == "short" and (current_sl == 0 or valid_sl < current_sl) and valid_sl > 0:
            return valid_sl
            
    return current_sl


def check_partial_close(
    current_price: float,
    entry_price: float,
    direction: str,
    original_sl_distance: float,
    total_tp_distance: float,
    original_lot: float,
    partial_close_percent: float,
    partial_closed: bool,
    broker_min_volume: float,
    broker_volume_step: float
) -> tuple[bool, float]:
    """Check if partial close should be triggered matching CheckPartialClose from MT5."""
    if partial_closed or original_sl_distance <= 0 or partial_close_percent <= 0:
        return False, 0.0
        
    profit_distance = (current_price - entry_price) if direction == "long" else (entry_price - current_price)
    force_partial = (total_tp_distance > 0 and profit_distance >= 0.8 * total_tp_distance)
    
    if profit_distance >= original_sl_distance or force_partial:
        close_volume = original_lot * (partial_close_percent / 100.0)
        
        if broker_volume_step > 0:
            close_volume = math.floor((close_volume + 1e-9) / broker_volume_step) * broker_volume_step
            
        if close_volume < broker_min_volume or close_volume <= 0:
            return True, 0.0 # mark as executed but size 0 (skipped) per EA logic
            
        return True, close_volume
        
    return False, 0.0


def check_ma_trailing_stop(
    current_price: float,
    entry_price: float,
    current_sl: float,
    direction: str,
    original_sl_distance: float,
    total_tp_distance: float,
    ma_value: float,
    ma_trail_min_step_percent: float,
    trailing_start_percent: float,
    point: float = 0.0
) -> float:
    """Manage MA-Based Trailing Stop matching MT5."""
    profit_distance = (current_price - entry_price) if direction == "long" else (entry_price - current_price)
    force_trail = (total_tp_distance > 0 and profit_distance >= 0.8 * total_tp_distance)
    
    if profit_distance < (original_sl_distance * (trailing_start_percent / 100.0)) and not force_trail:
        return current_sl
        
    if ma_value <= 0:
        return current_sl
        
    min_step_distance = original_sl_distance * (ma_trail_min_step_percent / 100.0)
    proposed_sl = 0.0
    
    if direction == "long":
        # Allow MA to trail SL even if below entry (risk reduction mode)
        # Only requirement: MA must be above current SL (improving the stop)
        if current_sl == 0 or ma_value > current_sl:
            # When sl==0 (first trigger), measure movement from entry; use abs() to handle
            # risk-reduction case where MA is below entry (valid improvement from 0).
            distance_diff = (ma_value - current_sl) if current_sl != 0 else abs(ma_value - entry_price)
            if distance_diff >= min_step_distance:
                proposed_sl = ma_value
    else:
        # For short: MA must be below current SL (improving the stop)
        if current_sl == 0 or ma_value < current_sl:
            distance_diff = (current_sl - ma_value) if current_sl != 0 else abs(entry_price - ma_value)
            if distance_diff >= min_step_distance:
                proposed_sl = ma_value
                
    if proposed_sl != 0.0:
        valid_sl = normalize_stop_level(proposed_sl, current_price, direction, 0, point, entry_price)
        if direction == "long" and valid_sl > current_sl and valid_sl > 0:
            return valid_sl
        if direction == "short" and (current_sl == 0 or valid_sl < current_sl) and valid_sl > 0:
            return valid_sl
            
    return current_sl


def moving_average_value(df, period: int, method: str = "ema") -> float:
    """Return the latest closed-candle MA value for the management timeframe.
    
    Uses iloc[-2] (last confirmed closed candle) to prevent intra-bar MA repainting
    from whipsawing trailing stops on the forming candle.
    """
    if period <= 0 or len(df) < period + 1:  # need at least period+1 rows to have a confirmed closed candle
        return 0.0
    close = df["close"]
    if method.lower() == "sma":
        return float(close.rolling(window=period).mean().iloc[-2])
    return float(close.ewm(span=period, adjust=False).mean().iloc[-2])

def should_early_exit(
    rule: dict,
    minutes_open: int,
    current_price: float,
    entry_price: float,
    tp: float,
    sl: float,
    direction: str,
    m15_atr: float = 0.0,
    h1_atr: float = 0.0,
    is_bearish_pinbar_m15: bool = False,
    is_bullish_pinbar_m15: bool = False,
    is_bearish_pinbar_m5: bool = False,
    is_bullish_pinbar_m5: bool = False,
    current_hour: int = 0
) -> bool:
    if not rule or not rule.get("is_active"):
        return False
        
    rule_type = rule.get("rule_type")
    max_minutes = rule.get("max_minutes", 0)
    min_progress_r = rule.get("min_progress_r", 0.0)
    level = rule.get("level", 0.0)
    session_name = rule.get("session_name", "")
    
    if rule_type == "time_stall":
        if minutes_open >= max_minutes:
            if tp <= 0:
                return False  # No TP set — cannot evaluate progress toward target
            total_dist = abs(tp - entry_price)
            if total_dist == 0:
                return False
            progress = abs(current_price - entry_price) / total_dist
            # Use explicit direction argument — never infer from tp > entry_price (fails when TP=0)
            moving_right = (current_price > entry_price) if direction == "long" else (current_price < entry_price)
            if not moving_right or progress < min_progress_r:
                return True
                
    elif rule_type == "fail_to_reach_1R":
        # Guard: if sl == 0 we cannot compute one_r safely, skip the rule
        if sl == 0:
            return False
        one_r = abs(entry_price - sl)
        progress_from_entry = abs(current_price - entry_price)
        if minutes_open >= max_minutes and progress_from_entry < one_r:
            return True
            
    elif rule_type == "structure_break":
        # Use direction arg instead of inferring from tp
        if direction == "long" and current_price < level: return True
        if direction == "short" and current_price > level: return True
        
    elif rule_type == "session_end":
        if session_name == "london" and current_hour >= 16: return True
        if session_name == "newyork" and current_hour >= 20: return True
        if session_name == "asian" and current_hour >= 8: return True
        
    elif rule_type == "momentum_loss":
        if m15_atr > 0 and h1_atr > 0:
            if m15_atr < h1_atr * min_progress_r:
                return True
                
    elif rule_type == "partial_target_rejection":
        # Guard: if sl == 0 we cannot compute one_r safely
        if sl == 0:
            return False
        one_r = abs(entry_price - sl)
        directional_progress = (current_price - entry_price) if direction == "long" else (entry_price - current_price)
        if directional_progress >= one_r * 0.8:
            if direction == "long":
                if is_bearish_pinbar_m15 or is_bearish_pinbar_m5:
                    return True
            elif direction == "short":
                if is_bullish_pinbar_m15 or is_bullish_pinbar_m5:
                    return True
                
    return False
