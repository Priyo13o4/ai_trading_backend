from __future__ import annotations

from pathlib import Path

import pandas as pd

from backtest_engine.reporting.aggregates import results_to_frame


def _load_pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _style(plt):
    plt.rcParams.update(
        {
            "figure.facecolor": "#0f1115",
            "axes.facecolor": "#151922",
            "axes.edgecolor": "#3a4150",
            "axes.labelcolor": "#e5e7eb",
            "xtick.color": "#cbd5e1",
            "ytick.color": "#cbd5e1",
            "text.color": "#e5e7eb",
            "grid.color": "#2b3240",
            "axes.titleweight": "bold",
        }
    )


def generate_plots(results, output_dir: str | Path, run_id: str) -> list[Path]:
    plt = _load_pyplot()
    _style(plt)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    df = results_to_frame(results)
    if df.empty:
        return []

    artifacts: list[Path] = []
    artifacts.append(_plot_equity_curve(plt, df, output_dir / f"{run_id}_equity_curve.png"))
    artifacts.append(_plot_symbol_pnl(plt, df, output_dir / f"{run_id}_symbol_net_pnl.png"))
    artifacts.append(_plot_outcome_cluster(plt, df, output_dir / f"{run_id}_symbol_outcome_cluster.png"))
    artifacts.append(_plot_daily_pnl(plt, df, output_dir / f"{run_id}_daily_net_pnl.png"))
    return artifacts


def _plot_equity_curve(plt, df: pd.DataFrame, path: Path) -> Path:
    trades = df[df["entered"]].sort_values("period_time").copy()
    if trades.empty:
        trades = df.sort_values("period_time").copy()
    trades["equity"] = 500.0 + trades["net_pnl"].cumsum()

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(trades["period_time"], trades["equity"], color="#38bdf8", linewidth=1.8)
    ax.set_title("Balance Curve")
    ax.set_ylabel("Balance")
    ax.grid(True, alpha=0.45)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_symbol_pnl(plt, df: pd.DataFrame, path: Path) -> Path:
    pnl = df.groupby("symbol")["net_pnl"].sum().sort_values()
    colors = ["#f87171" if value < 0 else "#34d399" for value in pnl]

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.barh(pnl.index, pnl.values, color=colors)
    ax.axvline(0, color="#e5e7eb", linewidth=0.8)
    ax.set_title("Net PnL By Symbol")
    ax.set_xlabel("Net PnL")
    ax.grid(True, axis="x", alpha=0.35)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_outcome_cluster(plt, df: pd.DataFrame, path: Path) -> Path:
    pivot = pd.crosstab(df["symbol"], df["outcome"])
    preferred = [
        "closed_tp",
        "closed_sl",
        "closed_trailing_sl",
        "open_at_data_end",
        "expired_without_entry",
        "invalidated_without_entry",
        "rejected_execution_not_allowed",
    ]
    cols = [col for col in preferred if col in pivot.columns] + [
        col for col in pivot.columns if col not in preferred
    ]
    pivot = pivot[cols]

    fig, ax = plt.subplots(figsize=(13, 6.5))
    image = ax.imshow(pivot.values, aspect="auto", cmap="viridis")
    ax.set_title("Symbol / Outcome Cluster")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, rotation=35, ha="right")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    for y in range(len(pivot.index)):
        for x in range(len(pivot.columns)):
            value = pivot.iat[y, x]
            if value:
                ax.text(x, y, str(value), ha="center", va="center", fontsize=8, color="#f8fafc")
    fig.colorbar(image, ax=ax, label="Count")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _plot_daily_pnl(plt, df: pd.DataFrame, path: Path) -> Path:
    daily = df.copy()
    daily["day"] = daily["period_time"].dt.floor("D")
    pnl = daily.groupby("day")["net_pnl"].sum()
    colors = ["#f87171" if value < 0 else "#34d399" for value in pnl]

    fig, ax = plt.subplots(figsize=(13, 5))
    ax.bar(pnl.index, pnl.values, color=colors, width=0.8)
    ax.axhline(0, color="#e5e7eb", linewidth=0.8)
    ax.set_title("Daily Net PnL")
    ax.set_ylabel("Net PnL")
    ax.grid(True, axis="y", alpha=0.35)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path
