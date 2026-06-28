import math
from backtest_engine.broker_specs import BrokerSymbolSpec

def normalize_volume(
    volume: float,
    spec: BrokerSymbolSpec,
    min_lot_size: float = 0.01,
    max_lot_size: float = 0.1
) -> float:
    # EA-18 mirror (entry path): if the risk-derived volume is below the broker minimum,
    # this clamps it UP to the minimum (`max(volume, min_allowed)`) rather than aborting the
    # trade. That matches NormalizeVolumeForSymbol(..., allowMinClamp=true) in
    # TradeExecutor(updated).mq5 — high-conviction metal/news signals whose risk-based lot
    # falls under the broker floor (e.g. XAUUSD ~0.003) still enter at the broker minimum,
    # accepting the higher-than-target risk (state.py flags lot_floor_violation /
    # risk_exceeded_due_to_min_lot but does NOT reject). The partial-close path keeps the
    # EA's allowMinClamp=false behaviour: check_partial_close() returns size 0 (skip) when the
    # close volume is below the broker minimum, so partials are never clamped up.
    # Parity note: the EA clamps to SYMBOL_VOLUME_MIN; here min_allowed = max(config min_lot,
    # broker_min), so keep config min_lot_size <= the broker minimum to stay faithful.
    if volume <= 0:
        return 0.0

    broker_min = spec.volume_min
    broker_max = spec.volume_max
    broker_step = spec.volume_step
    
    min_allowed = max(min_lot_size, broker_min) if broker_min > 0 else min_lot_size
    max_allowed = min(max_lot_size, broker_max) if broker_max > 0 else max_lot_size
    
    if max_allowed < min_allowed:
        return 0.0
        
    lot_size = max(volume, min_allowed)
    lot_size = min(lot_size, max_allowed)
    
    if broker_step > 0:
        lot_size = math.floor((lot_size + 1e-9) / broker_step) * broker_step
        if lot_size < min_allowed:
            lot_size = min_allowed
        if lot_size > max_allowed:
            lot_size = max_allowed
            
    if lot_size < min_allowed:
        return 0.0
        
    return lot_size

def calculate_lot_size(
    balance: float, 
    risk_percent: float, 
    entry_price: float, 
    stop_loss: float, 
    spec: BrokerSymbolSpec,
    confidence: str = "Medium",
    risk_reward_ratio: float = 0.0,
    base_lot_size: float = 0.02,
    use_dynamic_sizing: bool = True,
    high_confidence_multiplier: float = 1.5,
    medium_confidence_multiplier: float = 1.0,
    low_confidence_multiplier: float = 0.7,
    min_lot_size: float = 0.01,
    max_lot_size: float = 0.1
) -> float:
    """Calculate lot size exactly as MT5 does based on RiskPercent."""
    lot_size = base_lot_size
    
    if not use_dynamic_sizing:
        return normalize_volume(lot_size, spec, min_lot_size, max_lot_size)
        
    if confidence == "High":
        lot_size *= high_confidence_multiplier
    elif confidence == "Medium":
        lot_size *= medium_confidence_multiplier
    elif confidence == "Low":
        lot_size *= low_confidence_multiplier
        
    if risk_reward_ratio > 0:
        if risk_reward_ratio < 1.5:
            lot_size *= 1.2
        elif risk_reward_ratio > 2.5:
            lot_size *= 0.8
            
    risk_amount = balance * (risk_percent / 100.0)
    
    stop_distance = abs(entry_price - stop_loss)
    tick_size = spec.tick_size
    tick_value = getattr(spec, 'tick_value_loss', spec.tick_value) if hasattr(spec, 'tick_value_loss') else spec.tick_value
    
    if tick_size > 0 and tick_value > 0:
        risk_in_money_per_lot = (stop_distance / tick_size) * tick_value
        commission_assumption = getattr(spec, 'commission_per_lot_round_turn_assumption', 0.0)
        risk_in_money_per_lot += commission_assumption
        if risk_in_money_per_lot > 0:
            max_lot_by_risk = risk_amount / risk_in_money_per_lot
            lot_size = max_lot_by_risk
            
    return normalize_volume(lot_size, spec, min_lot_size, max_lot_size)
