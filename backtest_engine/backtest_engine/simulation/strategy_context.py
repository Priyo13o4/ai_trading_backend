from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from typing import Any

import pandas as pd

from backtest_engine.simulation.entries import is_zone_valid


@dataclass(frozen=True)
class SignalContext:
    """EA-shaped strategy context used by the simulator.

    The production DB stores some fields on the strategy row and some fields
    inside entry_signal JSON. The EA receives one flattened JSON payload, so the
    simulator should also flatten once at the boundary instead of making every
    entry-condition function guess where a field came from.
    """

    data: dict[str, Any]
    error: str | None = None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def _atr_at_or_before(atr_series: pd.Series, timestamp: Any, timeframe_delta: pd.Timedelta | None = None) -> float:
    if atr_series.empty:
        return 0.0
    if timeframe_delta is not None:
        closed = atr_series[atr_series.index + timeframe_delta <= timestamp]
    else:
        closed = atr_series[atr_series.index <= timestamp]
    if closed.empty:
        return 0.0
    value = _as_float(closed.iloc[-1])
    if isnan(value):
        return 0.0
    return value


def build_signal_context(
    strategy: Any,
    *,
    atr_series: pd.Series,
    ea_config: dict[str, Any],
) -> SignalContext:
    entry_signal = dict(strategy.entry_signal or {})
    direction = str(getattr(strategy, "direction", "") or "").strip().lower()
    level = _as_float(entry_signal.get("level"), 0.0)

    if not direction:
        return SignalContext(entry_signal, "missing_direction")
    if level <= 0:
        return SignalContext(entry_signal, "missing_entry_level")

    signal = dict(entry_signal)
    signal["direction"] = direction
    signal["level"] = level
    signal["entry_level"] = level
    signal["symbol"] = getattr(strategy, "symbol", "")
    signal["strategy_id"] = getattr(strategy, "strategy_id", None)
    signal["strategy_name"] = getattr(strategy, "strategy_name", "")
    signal["take_profit"] = _as_float(getattr(strategy, "take_profit", 0.0))
    signal["stop_loss"] = _as_float(getattr(strategy, "stop_loss", 0.0))
    signal["confidence"] = getattr(strategy, "confidence", "Medium")
    signal["risk_reward_ratio"] = _as_float(getattr(strategy, "risk_reward_ratio", 0.0))
    signal["execution_allowed"] = _as_bool(getattr(strategy, "execution_allowed", True), True)
    signal["trade_recommended"] = _as_bool(getattr(strategy, "trade_recommended", True), True)
    signal["risk_level"] = getattr(strategy, "risk_level", None)
    signal["trade_mode"] = getattr(strategy, "trade_mode", None)
    signal["entry_reference_price"] = level
    signal["confirmation_required"] = bool(entry_signal.get("confirmation") not in {None, "", "none"})

    trigger_zone = signal.get("trigger_zone") or []
    if not is_zone_valid(trigger_zone):
        timeframe = str(entry_signal.get("timeframe") or "M15").upper()
        delta_map = {
            "M1": pd.Timedelta(minutes=1),
            "M5": pd.Timedelta(minutes=5),
            "M15": pd.Timedelta(minutes=15),
            "M30": pd.Timedelta(minutes=30),
            "H1": pd.Timedelta(hours=1),
            "H4": pd.Timedelta(hours=4),
            "D1": pd.Timedelta(days=1),
            "W1": pd.Timedelta(weeks=1),
            "MN1": pd.Timedelta(days=30),
        }
        atr = _atr_at_or_before(
            atr_series,
            getattr(strategy, "timestamp", None),
            delta_map.get(timeframe),
        )
        zone_multiplier = _as_float(ea_config.get("zone_buffer_atr_multiplier"), 0.5)
        if atr > 0:
            trigger_zone = [level - (atr * zone_multiplier), level + (atr * zone_multiplier)]
            signal["trigger_zone_source"] = "atr_fallback"
        else:
            trigger_zone = [level, level]
            signal["trigger_zone_source"] = "level_fallback"
        signal["trigger_zone"] = trigger_zone
    else:
        signal["trigger_zone_source"] = "entry_signal"

    return SignalContext(signal)
