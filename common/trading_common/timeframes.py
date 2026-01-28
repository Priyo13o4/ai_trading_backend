"""Timeframe policy (authoritative).

Hybrid source-of-truth (LOCKED):
- Broker (MT5) provides raw candles: M1, D1, W1, MN1
- Timescale continuous aggregates provide derived candles: M5, M15, M30, H1, H4

Invariants:
- D1/W1/MN1 must NEVER be queried from CAGGs.
"""

from __future__ import annotations

from datetime import timedelta


BROKER_TIMEFRAMES: set[str] = {"M1", "D1", "W1", "MN1"}
DERIVED_CAGG_TIMEFRAMES: set[str] = {"M5", "M15", "M30", "H1", "H4"}


class TimeframePolicyError(RuntimeError):
    pass


TF_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,
    "MN1": 43200,
}


def normalize_timeframe(timeframe: str) -> str:
    return (timeframe or "").strip().upper()


def is_broker_timeframe(timeframe: str) -> bool:
    return normalize_timeframe(timeframe) in BROKER_TIMEFRAMES


def is_derived_cagg_timeframe(timeframe: str) -> bool:
    return normalize_timeframe(timeframe) in DERIVED_CAGG_TIMEFRAMES


def timeframe_minutes(timeframe: str) -> int:
    tf = normalize_timeframe(timeframe)
    if tf not in TF_MINUTES:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return int(TF_MINUTES[tf])


def timeframe_timedelta(timeframe: str) -> timedelta:
    return timedelta(minutes=timeframe_minutes(timeframe))


def cagg_relation_for_timeframe(timeframe: str) -> str:
    tf = normalize_timeframe(timeframe)
    mapping = {
        "M5": "candlesticks_m5",
        "M15": "candlesticks_m15",
        "M30": "candlesticks_m30",
        "H1": "candlesticks_h1",
        "H4": "candlesticks_h4",
    }
    if tf not in mapping:
        raise ValueError(f"Timeframe is not a derived CAGG timeframe: {tf}")
    return mapping[tf]


def assert_timeframe_policy(timeframe: str, source: str) -> str:
    """Fail-fast guardrail for the hybrid truth model.

    source:
      - "cagg": the timeframe must be a derived CAGG TF (M5–H4)
      - "broker_raw" / "candlesticks": the timeframe must be broker-provided (M1/D1/W1/MN1)
    """

    tf = normalize_timeframe(timeframe)
    src = (source or "").strip().lower()

    if src in {"cagg", "timescale", "cagg_view"}:
        if tf not in DERIVED_CAGG_TIMEFRAMES:
            raise TimeframePolicyError(f"Timeframe policy violation: {tf} is not allowed from CAGGs")
        return tf

    if src in {"broker", "broker_raw", "candlesticks", "base_table"}:
        if tf not in BROKER_TIMEFRAMES:
            raise TimeframePolicyError(f"Timeframe policy violation: {tf} is not allowed from broker/base table")
        return tf

    raise ValueError(f"Unknown timeframe policy source: {source}")
