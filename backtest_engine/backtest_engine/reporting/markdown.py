import pandas as pd
from typing import List
from backtest_engine.models import BacktestRun, BacktestResult
from backtest_engine.reporting.aggregates import (
    results_to_frame,
    summarize_by_period,
    summarize_by_symbol,
    summarize_by_symbol_period,
)


def _summary_table(df: pd.DataFrame, title_cols: list[str]) -> list[str]:
    if df.empty:
        return ["No rows."]

    cols = title_cols + [
        "strategies",
        "entered",
        "closed",
        "open",
        "tp",
        "sl",
        "trailing_sl",
        "partials",
        "be_moves",
        "net_pnl",
        "avg_r",
        "entry_rate",
        "close_win_rate",
    ]
    lines = [
        "| " + " | ".join(cols) + " |",
        "|" + "|".join(["---"] * len(cols)) + "|",
    ]
    for _, row in df.iterrows():
        values = []
        for col in cols:
            value = row[col]
            if col in {"net_pnl", "avg_r"}:
                values.append(f"{value:.2f}" if pd.notna(value) else "-")
            elif col in {"entry_rate", "close_win_rate"}:
                values.append(f"{value * 100:.1f}%")
            else:
                values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return lines

def generate_markdown_report(run: BacktestRun, results: List[BacktestResult]) -> str:
    """Generate a Markdown summary of the backtest run."""
    
    total = run.total_strategies
    executed = run.executed_trades
    
    if executed > 0:
        win_rate = sum(1 for r in results if r.net_pnl and r.net_pnl > 0) / executed * 100
    else:
        win_rate = 0.0
        
    df = results_to_frame(results)
    strategy_start = df["strategy_timestamp"].min() if not df.empty else None
    strategy_end = df["strategy_timestamp"].max() if not df.empty else None
    entered = int(df["entered"].sum()) if not df.empty else 0
    open_trades = int(df["open"].sum()) if not df.empty else 0
    net_pnl = float(df["net_pnl"].sum()) if not df.empty else 0.0

    lines = [
        f"# Backtest Report: {run.profile_name}",
        f"**Run ID**: `{run.run_id}`",
        f"**Run Time**: {run.started_at} to {run.finished_at or 'N/A'}",
        f"**Strategy Time Range**: {strategy_start or 'N/A'} to {strategy_end or 'N/A'}",
        "",
        "## Summary",
        f"- **Strategies Processed**: {run.processed_strategies} / {total}",
        f"- **Entered Trades**: {entered}",
        f"- **Closed Trades**: {executed}",
        f"- **Open At Data End**: {open_trades}",
        f"- **Win Rate**: {win_rate:.1f}%",
        f"- **Net PnL**: {net_pnl:.2f}",
        f"- **No Trade Count**: {run.no_trade_count}",
        f"- **Unsupported Count**: {run.unsupported_count}",
        "",
        "## Per Symbol",
        *_summary_table(summarize_by_symbol(df), ["symbol"]),
        "",
        "## Strategy Results",
    ]
    
    # Table header
    lines.append("| ID | Symbol | Dir | Outcome | Net PnL | R-Mult | Entry | Exit |")
    lines.append("|---|---|---|---|---|---|---|---|")
    
    for r in results:
        pnl = f"{r.net_pnl:.2f}" if r.net_pnl is not None else "0.00"
        rmult = f"{r.r_multiple:.2f}" if r.r_multiple is not None else "-"
        entry = r.entry_time.strftime("%m-%d %H:%M") if r.entry_time else "-"
        exit_ = r.exit_time.strftime("%m-%d %H:%M") if r.exit_time else "-"
        lines.append(f"| {r.strategy_id} | {r.symbol} | {r.direction} | {r.outcome} | {pnl} | {rmult} | {entry} | {exit_} |")
        
    return "\n".join(lines)


def generate_summary_report(title: str, summary: pd.DataFrame, group_cols: list[str]) -> str:
    lines = [f"# {title}", ""]
    lines.extend(_summary_table(summary, group_cols))
    return "\n".join(lines) + "\n"


def build_standard_summaries(results: List[BacktestResult]) -> dict[str, pd.DataFrame]:
    df = results_to_frame(results)
    return {
        "per_symbol": summarize_by_symbol(df),
        "daily": summarize_by_period(df, "D"),
        "weekly": summarize_by_period(df, "W"),
        "monthly": summarize_by_period(df, "M"),
        "symbol_daily": summarize_by_symbol_period(df, "D"),
        "symbol_weekly": summarize_by_symbol_period(df, "W"),
        "symbol_monthly": summarize_by_symbol_period(df, "M"),
    }
