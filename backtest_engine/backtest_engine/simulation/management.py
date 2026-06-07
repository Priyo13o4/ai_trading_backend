import math

def check_break_even(
    current_price: float,
    entry_price: float,
    stop_loss: float,
    direction: str,
    original_sl_distance: float,
    break_even_percent: float,
    break_even_moved: bool,
    lots: float,
    commission_per_lot: float,
    tick_value: float,
    tick_size: float,
    point: float,
    spread_buffer_points: float = 0.5,
    threshold_distance: float | None = None,
) -> tuple[bool, float]:
    """Check if Break Even should be triggered using true-cost offset."""
    if break_even_percent <= 0 or break_even_moved or original_sl_distance <= 0:
        return False, stop_loss
        
    profit_distance = (current_price - entry_price) if direction == "long" else (entry_price - current_price)
    threshold_dist = threshold_distance
    if threshold_dist is None:
        threshold_dist = original_sl_distance * (break_even_percent / 100.0)
    
    if profit_distance >= threshold_dist:
        commission_cost = lots * commission_per_lot
        value_per_point = (tick_value / tick_size) * lots if tick_size > 0 else 0
        
        total_cost_points = 0
        if value_per_point > 0:
            total_cost_points = (commission_cost / value_per_point) + spread_buffer_points
            
        cost_offset_price = total_cost_points * point
        
        if direction == "long":
            new_sl = entry_price + cost_offset_price
            if stop_loss == 0 or new_sl > stop_loss:
                return True, new_sl
        else:
            new_sl = entry_price - cost_offset_price
            if stop_loss == 0 or new_sl < stop_loss:
                return True, new_sl
                
    return False, stop_loss


def check_trailing_stop(
    current_price: float,
    current_sl: float,
    direction: str,
    original_sl_distance: float,
    trailing_start_percent: float,
    trailing_step_percent: float
) -> float:
    """Calculate new SL based on trailing stop parameters matching MT5 Stage B."""
    if trailing_start_percent <= 0 or original_sl_distance <= 0:
        return current_sl
        
    trail_distance = original_sl_distance * (trailing_start_percent / 100.0)
    step_distance = original_sl_distance * (trailing_step_percent / 100.0)
    
    if direction == "long":
        potential_new_sl = current_price - trail_distance
        if potential_new_sl > current_sl:
            distance_diff = potential_new_sl - current_sl
            if distance_diff >= step_distance:
                return potential_new_sl
    else:
        potential_new_sl = current_price + trail_distance
        if current_sl == 0 or potential_new_sl < current_sl:
            distance_diff = step_distance if current_sl == 0 else (current_sl - potential_new_sl)
            if distance_diff >= step_distance:
                return potential_new_sl
                
    return current_sl


def check_partial_close(
    current_price: float,
    entry_price: float,
    direction: str,
    original_sl_distance: float,
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
    
    if profit_distance >= original_sl_distance:
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
    ma_value: float,
    ma_trail_min_step_percent: float,
    break_even_percent: float
) -> float:
    """Manage MA-Based Trailing Stop matching MT5."""
    profit_distance = (current_price - entry_price) if direction == "long" else (entry_price - current_price)
    
    if profit_distance < (original_sl_distance * (break_even_percent / 100.0)):
        return current_sl
        
    if ma_value <= 0:
        return current_sl
        
    min_step_distance = original_sl_distance * (ma_trail_min_step_percent / 100.0)
    proposed_sl = 0.0
    
    if direction == "long":
        if ma_value > entry_price and (current_sl == 0 or ma_value > current_sl):
            distance_diff = ma_value - current_sl if current_sl != 0 else ma_value - entry_price
            if distance_diff >= min_step_distance:
                proposed_sl = ma_value
    else:
        if ma_value < entry_price and (current_sl == 0 or ma_value < current_sl):
            distance_diff = current_sl - ma_value if current_sl != 0 else entry_price - ma_value
            if distance_diff >= min_step_distance:
                proposed_sl = ma_value
                
    if proposed_sl != 0.0:
        return proposed_sl
        
    return current_sl


def moving_average_value(df, period: int, method: str = "ema") -> float:
    """Return the latest closed-candle MA value for the management timeframe."""
    if period <= 0 or len(df) < period:
        return 0.0
    close = df["close"]
    if method.lower() == "sma":
        return float(close.rolling(window=period).mean().iloc[-1])
    return float(close.ewm(span=period, adjust=False).mean().iloc[-1])
