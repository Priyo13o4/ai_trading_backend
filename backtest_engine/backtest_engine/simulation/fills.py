def evaluate_bar_exits(
    open_price: float,
    high: float,
    low: float,
    close: float,
    direction: str,
    take_profit: float,
    stop_loss: float
) -> str | None:
    """
    Conservative exit model for M1 bars.
    If both TP and SL are hit in the same bar, assume SL was hit first (adverse-first).
    Returns 'tp', 'sl', or None.
    """
    if direction == "long":
        hit_tp = high >= take_profit if take_profit > 0 else False
        hit_sl = low <= stop_loss if stop_loss > 0 else False
    else:
        hit_tp = low <= take_profit if take_profit > 0 else False
        hit_sl = high >= stop_loss if stop_loss > 0 else False
        
    if hit_tp and hit_sl:
        # Conservative: assume stop loss was hit first if both triggered in same M1 bar
        return "sl"
    elif hit_sl:
        return "sl"
    elif hit_tp:
        return "tp"
        
    return None
