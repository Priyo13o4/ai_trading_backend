from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backtest_engine.broker_specs import BrokerSymbolSpec
from backtest_engine.models import BacktestArtifact, BacktestResult, BacktestRun
from backtest_engine.simulation.candles import load_cagg_candles, load_m1_candles, load_raw_broker_candles
from backtest_engine.simulation.entries import ENTRY_CONDITIONS, is_zone_valid
from backtest_engine.simulation.indicators import calculate_atr
from backtest_engine.simulation.state import _closed_history_with_forming_placeholder, _timeframe_delta

NO_ENTRY_OUTCOMES = {"expired_without_entry", "invalidated_without_entry"}
MFE_OUTCOMES = {"closed_sl", "closed_trailing_sl"}

ZONE_CONDITION_TYPES = {
    "zone_retest",
    "pullback_entry",
    "break_and_retest",
    "price_rejection",
    "range_breakout",
    "vwap_bounce",
}

RAW_BROKER_TIMEFRAMES = {"D1", "W1", "MN1"}

DETAIL_COLUMNS = [
    "analysis_type",
    "analysis_bucket",
    "analysis_note",
    "result_id",
    "run_id",
    "run_name",
    "profile_name",
    "symbol",
    "direction",
    "condition_type",
    "timeframe",
    "confirmation",
    "strategy_timestamp",
    "strategy_expiry_time",
    "entry_time",
    "exit_time",
    "outcome",
    "outcome_reason",
    "reference_kind",
    "reference_low",
    "reference_high",
    "reference_value",
    "trigger_zone_source",
    "signal_debug_present",
    "closest_approach_price",
    "closest_approach_pips",
    "crossed_trigger",
    "first_touch_time",
    "bars_to_closest_approach",
    "confirmation_required",
    "confirmation_passed",
    "no_trade_reason",
    "entry_price",
    "exit_price",
    "take_profit",
    "stop_loss",
    "initial_stop_loss",
    "final_stop_loss",
    "best_favorable_price",
    "worst_adverse_price",
    "mfe_pips",
    "max_adverse_excursion_pips",
    "initial_risk_pips",
    "tp_distance_pips",
    "mfe_pct_of_initial_risk",
    "mfe_pct_of_tp_distance",
    "reached_80pct_of_tp",
    "breakeven_moved",
    "partial_close_executed",
    "stop_type",
]


@dataclass(frozen=True)
class CandlePack:
    m1: pd.DataFrame
    cagg_by_timeframe: dict[str, pd.DataFrame]


@dataclass(frozen=True)
class AdvancedTradeInsightBundle:
    run_id: str
    metadata: dict[str, Any]
    detail_frame: pd.DataFrame
    missed_entry_frame: pd.DataFrame
    mfe_frame: pd.DataFrame
    output_paths: list[Path]


def _json_ready(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_ready(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(inner) for inner in value]
    try:
        if not math.isfinite(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _signal_from_result(result: Any) -> dict[str, Any] | None:
    debug = getattr(result, "debug", None)
    if isinstance(debug, dict):
        signal = debug.get("signal")
        if isinstance(signal, dict):
            return signal
    return None


def _resolve_missed_entry_signal(
    result: BacktestResult,
    strategy_snapshot_lookup: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    signal = dict(_signal_from_result(result) or {})
    if strategy_snapshot_lookup is not None:
        snapshot_signal = _signal_from_strategy_snapshot(strategy_snapshot_lookup.get(int(result.strategy_id)))
        for key, value in snapshot_signal.items():
            if signal.get(key) is None and value is not None:
                signal[key] = value

    if not signal:
        return None

    signal["timeframe"] = str(signal.get("timeframe") or getattr(result, "timeframe", None) or "M15").upper()
    if signal.get("level") is None and signal.get("entry_level") is not None:
        signal["level"] = signal["entry_level"]
    if signal.get("entry_level") is None and signal.get("level") is not None:
        signal["entry_level"] = signal["level"]
    return signal


def _is_zone_condition(condition_type: str | None) -> bool:
    return str(condition_type or "") in ZONE_CONDITION_TYPES


def _pip_size(spec: BrokerSymbolSpec) -> float:
    point = float(getattr(spec, "point", 0.0) or 0.0)
    tick_size = float(getattr(spec, "tick_size", 0.0) or 0.0)
    if point > 0:
        return point * 10.0
    if tick_size > 0:
        return tick_size
    return 1.0


def _price_to_pips(delta: float | None, spec: BrokerSymbolSpec) -> float | None:
    if delta is None:
        return None
    pip_size = _pip_size(spec)
    if pip_size <= 0:
        return None
    return abs(float(delta)) / pip_size


def _reference_from_signal(signal: Mapping[str, Any], condition_type: str | None) -> tuple[str, float | None, float | None, float | None]:
    level = _as_float(signal.get("entry_level") or signal.get("level"), None)
    zone = signal.get("trigger_zone") or []

    if _is_zone_condition(condition_type) and is_zone_valid(zone):
        low = _as_float(zone[0], None)
        high = _as_float(zone[1], None)
        if low is not None and high is not None:
            return "zone", low, high, None

    if level is not None:
        return "level", level, level, level

    if is_zone_valid(zone):
        low = _as_float(zone[0], None)
        high = _as_float(zone[1], None)
        if low is not None and high is not None:
            return "zone", low, high, None

    return "unknown", None, None, None


def _touches_reference(bar: pd.Series, direction: str, reference_kind: str, low: float | None, high: float | None, value: float | None) -> bool:
    if reference_kind == "zone" and low is not None and high is not None:
        return float(bar["low"]) <= high and float(bar["high"]) >= low

    if value is None:
        return False

    if direction == "long":
        return float(bar["low"]) <= value
    return float(bar["high"]) >= value


def _distance_to_reference(bar: pd.Series, direction: str, reference_kind: str, low: float | None, high: float | None, value: float | None) -> float | None:
    if reference_kind == "zone" and low is not None and high is not None:
        if direction == "long":
            return max(0.0, float(bar["low"]) - high)
        return max(0.0, low - float(bar["high"]))

    if value is None:
        return None

    if direction == "long":
        return max(0.0, float(bar["low"]) - value)
    return max(0.0, value - float(bar["high"]))


def _closest_approach_price(
    bar: pd.Series,
    direction: str,
    reference_kind: str,
    low: float | None,
    high: float | None,
    value: float | None,
    touched: bool,
) -> float:
    if touched:
        if reference_kind == "zone" and low is not None and high is not None:
            return float(high if direction == "long" else low)
        if value is not None:
            return float(value)
    return float(bar["low"] if direction == "long" else bar["high"])


def _write_atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _detail_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        return pd.DataFrame(columns=DETAIL_COLUMNS)
    return frame.reindex(columns=DETAIL_COLUMNS)


def _run_metadata(run: BacktestRun) -> dict[str, Any]:
    return {
        "run_id": str(run.run_id),
        "run_name": run.run_name,
        "profile_name": run.profile_name,
        "profile_version": run.profile_version,
        "engine_version": run.engine_version,
        "source_database_name": run.source_database_name,
        "source_database_fingerprint": _json_ready(run.source_database_fingerprint),
        "strategy_filter": _json_ready(run.strategy_filter),
        "ea_config": _json_ready(run.ea_config),
        "ea_config_hash": run.ea_config_hash,
        "broker_specs_hash": run.broker_specs_hash,
        "trade_executor_hash": run.trade_executor_hash,
        "fill_model": run.fill_model,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "status": run.status,
    }


def _signal_from_strategy_snapshot(strategy_snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(strategy_snapshot, Mapping):
        return {}

    signal: dict[str, Any] = {}
    entry_signal = strategy_snapshot.get("entry_signal")
    if isinstance(entry_signal, Mapping):
        signal.update(entry_signal)

    for key in (
        "direction",
        "condition_type",
        "timeframe",
        "confirmation",
        "entry_level",
        "level",
        "trigger_zone",
        "trigger_zone_source",
        "confirmation_required",
        "stop_loss",
        "take_profit",
        "symbol",
        "strategy_name",
    ):
        value = strategy_snapshot.get(key)
        if value is not None and signal.get(key) is None:
            signal[key] = value

    if signal.get("level") is None and signal.get("entry_level") is not None:
        signal["level"] = signal["entry_level"]
    if signal.get("entry_level") is None and signal.get("level") is not None:
        signal["entry_level"] = signal["level"]
    return signal


async def _load_strategy_snapshot_lookup(
    dest_session: AsyncSession,
    run_uuid: uuid.UUID,
    snapshot_path: str | Path | None = None,
) -> dict[int, dict[str, Any]]:
    candidate_paths: list[Path] = []
    if snapshot_path is not None:
        candidate_paths.append(Path(snapshot_path))

    artifact_result = await dest_session.execute(
        select(BacktestArtifact)
        .where(BacktestArtifact.run_id == run_uuid)
        .order_by(BacktestArtifact.artifact_id.asc())
    )
    for artifact in artifact_result.scalars().all():
        artifact_path = Path(str(artifact.path))
        if artifact_path.name == "selected_strategies_snapshot.json":
            candidate_paths.append(artifact_path)

    payload: Any = None
    for candidate in candidate_paths:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        break

    if payload is None:
        return {}

    if isinstance(payload, list):
        selected_strategies = payload
    elif isinstance(payload, dict):
        selected_strategies = payload.get("selected_strategies") or payload.get("strategies") or []
    else:
        return {}

    lookup: dict[int, dict[str, Any]] = {}
    if not isinstance(selected_strategies, list):
        return lookup

    for strategy_snapshot in selected_strategies:
        if not isinstance(strategy_snapshot, dict):
            continue
        strategy_id = strategy_snapshot.get("strategy_id")
        try:
            strategy_key = int(strategy_id)
        except (TypeError, ValueError):
            continue
        lookup[strategy_key] = strategy_snapshot

    return lookup


async def _load_timeframe_candles(
    session: AsyncSession,
    symbol: str,
    timeframe: str | None,
    start_time: datetime,
    end_time: datetime | None,
) -> pd.DataFrame:
    normalized_timeframe = str(timeframe or "M15").upper()
    if normalized_timeframe == "M1":
        return await load_m1_candles(session, symbol, start_time, end_time)
    if normalized_timeframe in RAW_BROKER_TIMEFRAMES:
        return await load_raw_broker_candles(session, symbol, normalized_timeframe, start_time, end_time)
    return await load_cagg_candles(session, symbol, normalized_timeframe, start_time, end_time)


def _missed_entry_record(
    result: BacktestResult,
    pack: CandlePack | None,
    broker_spec: BrokerSymbolSpec | None,
    run: BacktestRun,
    signal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    debug_signal = _signal_from_result(result)
    signal = dict(signal or debug_signal or {})
    record: dict[str, Any] = {
        "analysis_type": "missed_entry",
        "analysis_bucket": result.outcome,
        "analysis_note": None,
        "result_id": result.result_id,
        "run_id": str(result.run_id),
        "run_name": run.run_name,
        "profile_name": run.profile_name,
        "symbol": result.symbol,
        "direction": result.direction,
        "condition_type": result.condition_type,
        "timeframe": result.timeframe,
        "confirmation": result.confirmation,
        "strategy_timestamp": _iso(result.strategy_timestamp),
        "strategy_expiry_time": _iso(result.strategy_expiry_time),
        "entry_time": None,
        "exit_time": None,
        "outcome": result.outcome,
        "outcome_reason": result.outcome_reason,
        "reference_kind": None,
        "reference_low": None,
        "reference_high": None,
        "reference_value": None,
        "trigger_zone_source": None,
        "signal_debug_present": debug_signal is not None,
        "closest_approach_price": None,
        "closest_approach_pips": None,
        "crossed_trigger": False,
        "first_touch_time": None,
        "bars_to_closest_approach": None,
        "confirmation_required": None,
        "confirmation_passed": None,
        "no_trade_reason": result.outcome_reason,
        "entry_price": None,
        "exit_price": None,
        "take_profit": None,
        "stop_loss": _as_float(signal.get("stop_loss"), None) if signal else None,
        "initial_stop_loss": None,
        "final_stop_loss": None,
        "best_favorable_price": None,
        "worst_adverse_price": None,
        "mfe_pips": None,
        "max_adverse_excursion_pips": None,
        "initial_risk_pips": None,
        "tp_distance_pips": None,
        "mfe_pct_of_initial_risk": None,
        "mfe_pct_of_tp_distance": None,
        "reached_80pct_of_tp": None,
        "breakeven_moved": None,
        "partial_close_executed": None,
        "stop_type": None,
    }

    if not signal:
        record["analysis_note"] = "missing_debug_signal"
        return record

    if pack is None or broker_spec is None:
        record["analysis_note"] = "missing_candle_pack_or_broker_spec"
        return record

    direction = str(result.direction or signal.get("direction") or "").lower()
    condition_type = str(result.condition_type or signal.get("condition_type") or "")
    timeframe = str(result.timeframe or signal.get("timeframe") or "M15").upper()
    reference_kind, reference_low, reference_high, reference_value = _reference_from_signal(signal, condition_type)
    record.update(
        {
            "direction": direction,
            "condition_type": condition_type,
            "timeframe": timeframe,
            "confirmation": signal.get("confirmation", result.confirmation),
            "reference_kind": reference_kind,
            "reference_low": reference_low,
            "reference_high": reference_high,
            "reference_value": reference_value,
            "trigger_zone_source": signal.get("trigger_zone_source"),
            "confirmation_required": bool(signal.get("confirmation_required", str(signal.get("confirmation") or "none").lower() != "none")),
            "no_trade_reason": result.outcome_reason,
            "stop_type": None,
        }
    )

    m1_df = pack.m1
    if m1_df.empty:
        record["analysis_note"] = "missing_m1_history"
        return record

    start_time = pd.to_datetime(result.strategy_timestamp, utc=True)
    expiry_time = pd.to_datetime(result.strategy_expiry_time, utc=True)
    window = m1_df[(m1_df.index >= start_time) & (m1_df.index <= expiry_time)]
    if window.empty:
        record["analysis_note"] = "no_m1_candles_in_window"
        return record

    cagg_df = pack.cagg_by_timeframe.get(timeframe, pd.DataFrame())
    if cagg_df.empty:
        record["analysis_note"] = "missing_timeframe_candles"
        return record

    atr_series = calculate_atr(cagg_df, period=int((run.ea_config or {}).get("atr_period", 14) or 14)) if not cagg_df.empty else pd.Series(dtype=float)
    entry_func = ENTRY_CONDITIONS.get(condition_type)
    tf_delta = _timeframe_delta(timeframe)
    cagg_idx = -1
    cagg_times = cagg_df.index
    cagg_len = len(cagg_df)

    best_distance: float | None = None
    best_distance_price: float | None = None
    bars_to_closest_approach: int | None = None
    first_touch_time: datetime | None = None
    confirmation_passed = False

    for bars_seen, (ts, bar) in enumerate(window.iterrows(), start=1):
        while cagg_idx < cagg_len - 1 and cagg_times[cagg_idx + 1] + tf_delta <= ts:
            cagg_idx += 1

        if cagg_idx >= 0 and cagg_idx < cagg_len:
            hist_df = _closed_history_with_forming_placeholder(cagg_df, closed_idx=cagg_idx, current_ts=ts)
            atr_val = atr_series.iloc[cagg_idx] if cagg_idx < len(atr_series) else float("nan")
        else:
            hist_df = pd.DataFrame()
            atr_val = float("nan")

        touched = _touches_reference(bar, direction, reference_kind, reference_low, reference_high, reference_value)
        distance = _distance_to_reference(bar, direction, reference_kind, reference_low, reference_high, reference_value)

        if distance is not None and (best_distance is None or distance < best_distance):
            best_distance = distance
            best_distance_price = _closest_approach_price(bar, direction, reference_kind, reference_low, reference_high, reference_value, touched)
            bars_to_closest_approach = bars_seen

        if touched and first_touch_time is None:
            first_touch_time = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts
            if entry_func is not None:
                confirmation_passed = bool(entry_func(hist_df, float(bar["close"]), signal, broker_spec.point, atr_val))

    record.update(
        {
            "closest_approach_price": best_distance_price,
            "closest_approach_pips": _price_to_pips(best_distance, broker_spec),
            "crossed_trigger": first_touch_time is not None,
            "first_touch_time": _iso(first_touch_time),
            "bars_to_closest_approach": bars_to_closest_approach,
            "confirmation_passed": confirmation_passed,
            "analysis_note": None,
        }
    )
    return record


def _mfe_record(result: BacktestResult, pack: CandlePack | None, broker_spec: BrokerSymbolSpec | None, run: BacktestRun) -> dict[str, Any]:
    record: dict[str, Any] = {
        "analysis_type": "mfe",
        "analysis_bucket": result.outcome,
        "analysis_note": None,
        "result_id": result.result_id,
        "run_id": str(result.run_id),
        "run_name": run.run_name,
        "profile_name": run.profile_name,
        "symbol": result.symbol,
        "direction": result.direction,
        "condition_type": result.condition_type,
        "timeframe": result.timeframe,
        "confirmation": result.confirmation,
        "strategy_timestamp": _iso(result.strategy_timestamp),
        "strategy_expiry_time": _iso(result.strategy_expiry_time),
        "entry_time": _iso(result.entry_time),
        "exit_time": _iso(result.exit_time),
        "outcome": result.outcome,
        "outcome_reason": result.outcome_reason,
        "reference_kind": None,
        "reference_low": None,
        "reference_high": None,
        "reference_value": None,
        "trigger_zone_source": None,
        "signal_debug_present": _signal_from_result(result) is not None,
        "closest_approach_price": None,
        "closest_approach_pips": None,
        "crossed_trigger": None,
        "first_touch_time": None,
        "bars_to_closest_approach": None,
        "confirmation_required": None,
        "confirmation_passed": None,
        "no_trade_reason": None,
        "entry_price": _as_float(result.entry_price, None),
        "exit_price": _as_float(result.exit_price, None),
        "take_profit": _as_float(result.take_profit, None),
        "stop_loss": _first_not_none(
            _as_float(result.initial_stop_loss, None),
            _as_float(result.final_stop_loss, None),
            _as_float(result.stop_loss, None),
        ),
        "initial_stop_loss": _as_float(result.initial_stop_loss, None),
        "final_stop_loss": _as_float(result.final_stop_loss, None),
        "best_favorable_price": None,
        "worst_adverse_price": None,
        "mfe_pips": None,
        "max_adverse_excursion_pips": None,
        "initial_risk_pips": None,
        "tp_distance_pips": None,
        "mfe_pct_of_initial_risk": None,
        "mfe_pct_of_tp_distance": None,
        "reached_80pct_of_tp": None,
        "breakeven_moved": bool(result.break_even_moved),
        "partial_close_executed": bool(result.partial_close_executed),
        "stop_type": result.outcome,
    }

    if pack is None or broker_spec is None:
        record["analysis_note"] = "missing_candle_pack_or_broker_spec"
        return record

    m1_df = pack.m1
    if m1_df.empty or result.entry_time is None or result.exit_time is None or result.entry_price is None:
        record["analysis_note"] = "missing_mfe_inputs"
        return record

    entry_time = pd.to_datetime(result.entry_time, utc=True)
    exit_time = pd.to_datetime(result.exit_time, utc=True)
    window_before_exit = m1_df[(m1_df.index > entry_time) & (m1_df.index < exit_time)]
    exit_candle = m1_df[(m1_df.index == exit_time)]
    adverse_window = pd.concat([window_before_exit, exit_candle]) if not exit_candle.empty else window_before_exit

    direction = str(result.direction or "").lower()
    entry_price = float(result.entry_price)
    stop_price = _first_not_none(
        _as_float(result.initial_stop_loss, None),
        _as_float(result.final_stop_loss, None),
        _as_float(result.stop_loss, None),
    )
    take_profit = _as_float(result.take_profit, None)

    if direction == "long":
        best_favorable_price = float(adverse_window["high"].max()) if not adverse_window.empty else entry_price
        worst_adverse_price = float(adverse_window["low"].min()) if not adverse_window.empty else entry_price
        mfe_delta = best_favorable_price - entry_price
        mae_delta = entry_price - worst_adverse_price
        tp_distance = abs(take_profit - entry_price) if take_profit is not None else None
        risk_distance = abs(entry_price - stop_price) if stop_price is not None else None
    else:
        best_favorable_price = float(adverse_window["low"].min()) if not adverse_window.empty else entry_price
        worst_adverse_price = float(adverse_window["high"].max()) if not adverse_window.empty else entry_price
        mfe_delta = entry_price - best_favorable_price
        mae_delta = worst_adverse_price - entry_price
        tp_distance = abs(entry_price - take_profit) if take_profit is not None else None
        risk_distance = abs(stop_price - entry_price) if stop_price is not None else None

    mfe_pips = _price_to_pips(mfe_delta, broker_spec)
    mae_pips = _price_to_pips(mae_delta, broker_spec)
    tp_distance_pips = _price_to_pips(tp_distance, broker_spec) if tp_distance is not None else None
    risk_pips = _price_to_pips(risk_distance, broker_spec) if risk_distance is not None else None

    record.update(
        {
            "best_favorable_price": best_favorable_price,
            "worst_adverse_price": worst_adverse_price,
            "mfe_pips": mfe_pips,
            "max_adverse_excursion_pips": mae_pips,
            "initial_risk_pips": risk_pips,
            "tp_distance_pips": tp_distance_pips,
            "mfe_pct_of_initial_risk": (mfe_pips / risk_pips * 100.0) if mfe_pips is not None and risk_pips else None,
            "mfe_pct_of_tp_distance": (mfe_pips / tp_distance_pips * 100.0) if mfe_pips is not None and tp_distance_pips else None,
            "reached_80pct_of_tp": bool(mfe_pips is not None and tp_distance_pips is not None and mfe_pips >= (tp_distance_pips * 0.8)),
            "analysis_note": None,
        }
    )
    return record


def _missed_entry_histogram(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["analysis_bucket", "symbol", "condition_type", "timeframe", "confirmation", "trigger_zone_source", "outcome_reason"]
    if frame.empty:
        return pd.DataFrame(columns=columns + ["count", "avg_closest_approach_pips", "median_closest_approach_pips", "crossed_trigger_rate", "confirmation_passed_rate"])

    grouped = frame.groupby(columns, dropna=False)
    return grouped.agg(
        count=("result_id", "size"),
        avg_closest_approach_pips=("closest_approach_pips", "mean"),
        median_closest_approach_pips=("closest_approach_pips", "median"),
        crossed_trigger_rate=("crossed_trigger", "mean"),
        confirmation_passed_rate=("confirmation_passed", "mean"),
    ).reset_index()


def _mfe_histogram(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["stop_type", "symbol", "condition_type", "timeframe", "confirmation", "outcome_reason"]
    if frame.empty:
        return pd.DataFrame(columns=columns + ["count", "avg_mfe_pips", "median_mfe_pips", "avg_max_adverse_excursion_pips", "reached_80pct_of_tp_rate", "breakeven_moved_rate", "partial_close_executed_rate"])

    grouped = frame.groupby(columns, dropna=False)
    return grouped.agg(
        count=("result_id", "size"),
        avg_mfe_pips=("mfe_pips", "mean"),
        median_mfe_pips=("mfe_pips", "median"),
        avg_max_adverse_excursion_pips=("max_adverse_excursion_pips", "mean"),
        reached_80pct_of_tp_rate=("reached_80pct_of_tp", "mean"),
        breakeven_moved_rate=("breakeven_moved", "mean"),
        partial_close_executed_rate=("partial_close_executed", "mean"),
    ).reset_index()


def _render_markdown_table(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    if frame.empty:
        return ["No rows."]

    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for _, row in frame.iterrows():
        values: list[str] = []
        for column in columns:
            value = row[column]
            if pd.isna(value):
                values.append("-")
            elif column.endswith("rate") or column.endswith("_pct"):
                values.append(f"{float(value) * 100.0:.1f}%")
            elif isinstance(value, float):
                values.append(f"{value:.2f}")
            else:
                values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return lines


def _render_summary_md(run: BacktestRun, missed_hist: pd.DataFrame, mfe_hist: pd.DataFrame, detail_frame: pd.DataFrame) -> str:
    lines = [
        "# Advanced Trade Insights",
        "",
        f"Run ID: `{run.run_id}`",
        f"Run Name: `{run.run_name}`",
        f"Profile: `{run.profile_name}`",
        f"Source DB: `{run.source_database_name}`",
        "",
        "## Coverage",
        f"- Missed-entry rows: {int((detail_frame['analysis_type'] == 'missed_entry').sum()) if not detail_frame.empty else 0}",
        f"- MFE rows: {int((detail_frame['analysis_type'] == 'mfe').sum()) if not detail_frame.empty else 0}",
        "",
        "## Missed Entry Summary",
    ]

    missed_table = missed_hist[["analysis_bucket", "count", "avg_closest_approach_pips", "median_closest_approach_pips", "crossed_trigger_rate", "confirmation_passed_rate"]] if not missed_hist.empty else pd.DataFrame()
    lines.extend(_render_markdown_table(missed_table, ["analysis_bucket", "count", "avg_closest_approach_pips", "median_closest_approach_pips", "crossed_trigger_rate", "confirmation_passed_rate"]))
    lines.extend([
        "",
        "## MFE Summary",
    ])
    mfe_table = mfe_hist[["stop_type", "count", "avg_mfe_pips", "median_mfe_pips", "avg_max_adverse_excursion_pips", "reached_80pct_of_tp_rate", "breakeven_moved_rate", "partial_close_executed_rate"]] if not mfe_hist.empty else pd.DataFrame()
    lines.extend(_render_markdown_table(mfe_table, ["stop_type", "count", "avg_mfe_pips", "median_mfe_pips", "avg_max_adverse_excursion_pips", "reached_80pct_of_tp_rate", "breakeven_moved_rate", "partial_close_executed_rate"]))
    return "\n".join(lines) + "\n"


def _build_bundle(run: BacktestRun, rows: list[dict[str, Any]], output_dir: Path) -> AdvancedTradeInsightBundle:
    detail_frame = _detail_frame(rows)
    missed_entry_frame = detail_frame[detail_frame["analysis_type"] == "missed_entry"].copy() if not detail_frame.empty else pd.DataFrame(columns=DETAIL_COLUMNS)
    mfe_frame = detail_frame[detail_frame["analysis_type"] == "mfe"].copy() if not detail_frame.empty else pd.DataFrame(columns=DETAIL_COLUMNS)

    if not detail_frame.empty:
        sort_cols = [col for col in ["analysis_type", "symbol", "strategy_timestamp", "result_id"] if col in detail_frame.columns]
        detail_frame = detail_frame.sort_values(sort_cols, na_position="last")

    missed_hist = _missed_entry_histogram(missed_entry_frame)
    mfe_hist = _mfe_histogram(mfe_frame)

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.md"
    detail_csv_path = output_dir / "advanced_insights.csv"
    json_path = output_dir / "advanced_insights.json"
    missed_hist_path = output_dir / "missed_entry_histogram.csv"
    mfe_hist_path = output_dir / "mfe_histogram.csv"

    metadata = _json_ready({
        "run": _run_metadata(run),
        "missed_entry": _json_ready(missed_entry_frame.to_dict(orient="records")),
        "mfe": _json_ready(mfe_frame.to_dict(orient="records")),
        "missed_entry_histogram": _json_ready(missed_hist.to_dict(orient="records")),
        "mfe_histogram": _json_ready(mfe_hist.to_dict(orient="records")),
    })

    _write_atomic_text(summary_path, _render_summary_md(run, missed_hist, mfe_hist, detail_frame))
    _write_atomic_text(detail_csv_path, detail_frame.to_csv(index=False))
    _write_atomic_text(json_path, json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False))
    _write_atomic_text(missed_hist_path, missed_hist.to_csv(index=False))
    _write_atomic_text(mfe_hist_path, mfe_hist.to_csv(index=False))

    return AdvancedTradeInsightBundle(
        run_id=str(run.run_id),
        metadata=metadata,
        detail_frame=detail_frame,
        missed_entry_frame=missed_entry_frame,
        mfe_frame=mfe_frame,
        output_paths=[summary_path, detail_csv_path, json_path, missed_hist_path, mfe_hist_path],
    )


def build_advanced_trade_insights(
    run: BacktestRun,
    results: list[Any],
    candle_packs_by_symbol: Mapping[str, CandlePack],
    broker_specs: Mapping[str, BrokerSymbolSpec],
    output_dir: str | Path,
    strategy_snapshot_lookup: Mapping[int, Mapping[str, Any]] | None = None,
) -> AdvancedTradeInsightBundle:
    relevant_results = [result for result in results if result.outcome in NO_ENTRY_OUTCOMES or result.outcome in MFE_OUTCOMES]
    detail_rows: list[dict[str, Any]] = []
    for result in relevant_results:
        pack = candle_packs_by_symbol.get(result.symbol)
        broker_spec = broker_specs.get(result.symbol)
        if result.outcome in NO_ENTRY_OUTCOMES:
            detail_rows.append(_missed_entry_record(result, pack, broker_spec, run, signal=_resolve_missed_entry_signal(result, strategy_snapshot_lookup)))
        else:
            detail_rows.append(_mfe_record(result, pack, broker_spec, run))

    return _build_bundle(run, detail_rows, Path(output_dir))


async def generate_advanced_trade_insights(
    dest_session: AsyncSession,
    source_session: AsyncSession,
    run_id: str | uuid.UUID,
    broker_specs: Mapping[str, BrokerSymbolSpec],
    output_dir: str | Path,
) -> AdvancedTradeInsightBundle:
    run_uuid = uuid.UUID(str(run_id))
    run_result = await dest_session.execute(select(BacktestRun).where(BacktestRun.run_id == run_uuid))
    run = run_result.scalar_one()

    results_result = await dest_session.execute(
        select(BacktestResult)
        .where(BacktestResult.run_id == run_uuid)
        .order_by(BacktestResult.result_id.asc())
    )
    results = list(results_result.scalars().all())

    relevant_results = [result for result in results if result.outcome in NO_ENTRY_OUTCOMES or result.outcome in MFE_OUTCOMES]
    if not relevant_results:
        return _build_bundle(run, [], Path(output_dir))

    strategy_snapshot_lookup = await _load_strategy_snapshot_lookup(dest_session, run_uuid)

    window_bounds: dict[str, dict[str, datetime]] = {}
    timeframes_by_symbol: dict[str, set[str]] = {}
    for result in relevant_results:
        symbol = result.symbol
        start_time = result.strategy_timestamp if result.outcome in NO_ENTRY_OUTCOMES else result.entry_time or result.strategy_timestamp
        end_time = result.strategy_expiry_time if result.outcome in NO_ENTRY_OUTCOMES else result.exit_time or result.strategy_expiry_time
        if symbol not in window_bounds:
            window_bounds[symbol] = {"start": start_time, "end": end_time}
        else:
            window_bounds[symbol]["start"] = min(window_bounds[symbol]["start"], start_time)
            window_bounds[symbol]["end"] = max(window_bounds[symbol]["end"], end_time)
        if result.outcome in NO_ENTRY_OUTCOMES:
            resolved_signal = _resolve_missed_entry_signal(result, strategy_snapshot_lookup)
            if resolved_signal is not None:
                timeframes_by_symbol.setdefault(symbol, set()).add(str(resolved_signal.get("timeframe") or result.timeframe or "M15").upper())

    candle_packs: dict[str, CandlePack] = {}
    for symbol, bounds in window_bounds.items():
        m1_df = await load_m1_candles(source_session, symbol, bounds["start"], bounds["end"])
        cagg_by_timeframe: dict[str, pd.DataFrame] = {}
        for timeframe in sorted(timeframes_by_symbol.get(symbol, set())):
            cagg_by_timeframe[timeframe] = await _load_timeframe_candles(source_session, symbol, timeframe, bounds["start"], bounds["end"])
        candle_packs[symbol] = CandlePack(m1=m1_df, cagg_by_timeframe=cagg_by_timeframe)

    return build_advanced_trade_insights(
        run,
        results=relevant_results,
        candle_packs_by_symbol=candle_packs,
        broker_specs=broker_specs,
        output_dir=output_dir,
        strategy_snapshot_lookup=strategy_snapshot_lookup,
    )
