import json
import hashlib
from typing import Dict
from dataclasses import dataclass
from pathlib import Path

@dataclass
class BrokerSymbolSpec:
    symbol: str
    exists: bool
    selected: bool
    digits: int
    point: float
    spread_points: int
    spread_float: bool
    trade_mode: int
    trade_calc_mode: int
    contract_size: float
    tick_size: float
    tick_value: float
    tick_value_profit: float
    tick_value_loss: float
    volume_min: float
    volume_max: float
    volume_step: float
    volume_limit: float
    stops_level: int
    freeze_level: int
    currency_base: str
    currency_profit: str
    currency_margin: str
    swap_mode: int
    swap_long: float
    swap_short: float
    margin_initial: float
    margin_maintenance: float
    commission_per_lot_round_turn_assumption: float


def load_broker_specs(filepath: str | Path) -> Dict[str, BrokerSymbolSpec]:
    specs = {}
    with open(filepath, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            spec = BrokerSymbolSpec(**data)
            specs[spec.symbol] = spec
    return specs


def get_broker_specs_hash(filepath: str | Path) -> str:
    """Generate deterministic hash of broker specs."""
    specs = load_broker_specs(filepath)
    # Serialize deterministically by sorting keys
    serialized = []
    for sym in sorted(specs.keys()):
        serialized.append(specs[sym].__dict__)
    
    s = json.dumps(serialized, sort_keys=True)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def apply_commission_overrides(
    specs: Dict[str, BrokerSymbolSpec],
    ea_config: dict,
) -> Dict[str, BrokerSymbolSpec]:
    """Apply symbol-level round-turn commission assumptions for a run.

    MT5 does not expose broker commission via SymbolInfo*, so the exporter can
    only carry assumptions. Backtests should make those assumptions explicit and
    per symbol.
    """
    default = ea_config.get("default_commission_per_lot_round_turn")
    overrides = ea_config.get("commission_per_lot_round_turn_by_symbol") or {}

    for symbol, spec in specs.items():
        if symbol in overrides:
            spec.commission_per_lot_round_turn_assumption = float(overrides[symbol])
        elif default is not None:
            spec.commission_per_lot_round_turn_assumption = float(default)

    return specs
