def calculate_pnl(
    entry_price: float, 
    exit_price: float, 
    volume: float, 
    direction: str, 
    tick_size: float, 
    tick_value: float
) -> float:
    """Calculate PnL based on static broker spec tick values."""
    if tick_size <= 0:
        return 0.0
        
    price_diff = exit_price - entry_price if direction == "long" else entry_price - exit_price
    ticks = price_diff / tick_size
    
    # tick_value is usually for 1 lot, so we multiply by volume
    return ticks * tick_value * volume

def calculate_commission(volume: float, round_turn_assumption: float) -> float:
    """Calculate fixed commission per lot."""
    return volume * round_turn_assumption

def calculate_r_multiple(pnl: float, initial_risk: float) -> float:
    """Calculate R-Multiple of a trade."""
    if initial_risk <= 0:
        return 0.0
    return pnl / initial_risk
