from __future__ import annotations

from decimal import Decimal
from typing import Iterable

import pandas as pd


def _value(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def results_to_frame(results: Iterable) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append(
            {
                "strategy_id": r.strategy_id,
                "symbol": r.symbol,
                "direction": r.direction,
                "condition_type": r.condition_type,
                "timeframe": r.timeframe,
                "confirmation": r.confirmation,
                "strategy_timestamp": r.strategy_timestamp,
                "strategy_expiry_time": r.strategy_expiry_time,
                "outcome": r.outcome,
                "outcome_reason": r.outcome_reason,
                "entry_time": r.entry_time,
                "exit_time": r.exit_time,
                "entry_price": _value(r.entry_price),
                "exit_price": _value(r.exit_price),
                "lot_size": _value(r.lot_size),
                "partial_close_executed": bool(r.partial_close_executed),
                "break_even_moved": bool(r.break_even_moved),
                "hit_tp": bool(r.hit_tp),
                "hit_sl": bool(r.hit_sl),
                "gross_pnl": float(_value(r.gross_pnl) or 0.0),
                "commission": float(_value(r.commission) or 0.0),
                "swap": float(_value(r.swap) or 0.0),
                "net_pnl": float(_value(r.net_pnl) or 0.0),
                "pnl_pips": _value(r.pnl_pips),
                "r_multiple": _value(r.r_multiple),
                "bars_scanned": r.bars_scanned or 0,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    for col in ["strategy_timestamp", "strategy_expiry_time", "entry_time", "exit_time"]:
        df[col] = pd.to_datetime(df[col], utc=True)

    df["entered"] = df["entry_time"].notna()
    df["closed"] = df["outcome"].str.startswith("closed_", na=False)
    df["won"] = df["closed"] & (df["net_pnl"] > 0)
    df["open"] = df["outcome"].eq("open_at_data_end")
    df["period_time"] = df["exit_time"].fillna(df["entry_time"]).fillna(df["strategy_timestamp"])
    df["failure_bucket"] = df["outcome"].map(_failure_bucket)
    df["commission_drag"] = df["commission"].abs()
    return df


def _failure_bucket(outcome: str) -> str:
    mapping = {
        "expired_without_entry": "no_entry_expired",
        "invalidated_without_entry": "invalidated_before_entry",
        "rejected_execution_not_allowed": "rejected_execution_not_allowed",
        "rejected_not_recommended": "rejected_not_recommended",
        "unsupported_condition_type": "unsupported_condition",
        "unsupported_confirmation": "unsupported_confirmation",
        "open_at_data_end": "entered_no_exit_before_data_end",
        "closed_sl": "stopped_before_management_saved_trade",
        "closed_trailing_sl": "stopped_after_be_or_trail",
        "closed_tp": "hit_take_profit",
    }
    return mapping.get(outcome or "", "other")


def summarize(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    grouped = df.groupby(group_cols, dropna=False)
    summary = grouped.agg(
        strategies=("strategy_id", "count"),
        entered=("entered", "sum"),
        closed=("closed", "sum"),
        open=("open", "sum"),
        tp=("outcome", lambda s: (s == "closed_tp").sum()),
        sl=("outcome", lambda s: (s == "closed_sl").sum()),
        trailing_sl=("outcome", lambda s: (s == "closed_trailing_sl").sum()),
        expired=("outcome", lambda s: (s == "expired_without_entry").sum()),
        invalidated=("outcome", lambda s: (s == "invalidated_without_entry").sum()),
        rejected=("outcome", lambda s: s.astype(str).str.startswith("rejected_").sum()),
        partials=("partial_close_executed", "sum"),
        be_moves=("break_even_moved", "sum"),
        gross_pnl=("gross_pnl", "sum"),
        commission=("commission", "sum"),
        net_pnl=("net_pnl", "sum"),
        avg_net_pnl=("net_pnl", "mean"),
        avg_r=("r_multiple", "mean"),
        avg_bars_scanned=("bars_scanned", "mean"),
    ).reset_index()

    summary["entry_rate"] = summary["entered"] / summary["strategies"].where(summary["strategies"] != 0, 1)
    summary["close_win_rate"] = summary["tp"] / summary["closed"].where(summary["closed"] != 0, 1)
    return summary


def summarize_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    return summarize(df, ["symbol"])


def summarize_by_period(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    with_period = df.copy()
    with_period["period"] = with_period["period_time"].dt.tz_convert(None).dt.to_period(freq).astype(str)
    return summarize(with_period, ["period"])


def summarize_by_symbol_period(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    with_period = df.copy()
    with_period["period"] = with_period["period_time"].dt.tz_convert(None).dt.to_period(freq).astype(str)
    return summarize(with_period, ["symbol", "period"])
