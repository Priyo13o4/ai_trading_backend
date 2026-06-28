"""Decision-grade dashboard payload for the Trust -> Edge -> Contamination screens.

VALIDATION_DESIGN/06 specifies a metrics API over `backtest_lab`. That would be backend work
and `backtest_lab` does not exist yet, so — staying inside the "backtest engine + its dashboard"
scope — the engine instead emits a single self-describing JSON artifact and the dashboard reads
it (the same static-file pattern the existing sweep terminal already uses).

The payload answers the three decisions, in order:
  1. TRUST  — can I believe these numbers? (config/version parity, dropped strategies, sample
     power, what is and isn't verified).
  2. EDGE   — what works, where? (the 6-D cube from 05, emitted as closed-trade rows plus
     pre-aggregated common cuts; every cell carries n).
  3. CONTAMINATION — are news/regime poisoning outputs? (performance by news_state / regime,
     with honest notes on which Tier-2 detectors need workflow joins not available here).
Plus the PORTFOLIO equity curve / drawdown / reject-reasons from the new portfolio model.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

CLOSED_OUTCOMES = {"closed_tp", "closed_sl", "closed_trailing_sl", "closed_early_exit"}

# Static known-open parity state from VALIDATION_DESIGN/03 drift matrix. The full Layer-A
# bit-exact harness needs an EA decision-trace export from MT5 Strategy Tester, which is not
# available in this environment, so these surfaces are declared as their audited status rather
# than measured. "green" = audited equivalent in shape; "amber" = partially modelled / mirror
# added but not bit-verified; "red" = known divergence.
PARITY_SURFACES = [
    {"surface": "Entry trigger", "status": "green", "note": "same ~19 condition types; no entry lookahead"},
    {"surface": "Exit SL/TP", "status": "amber", "note": "conservative SL-first; not bit-verified vs EA tick path"},
    {"surface": "Max-distance (EA-01)", "status": "amber", "note": "mirror added; not yet trace-verified"},
    {"surface": "Range-breakout off level (EA-08)", "status": "amber", "note": "mirror added"},
    {"surface": "Min-lot clamp (EA-18)", "status": "green", "note": "engine clamps up to broker min; partials skip"},
    {"surface": "Portfolio caps / conflict (EA-04)", "status": "amber", "note": "portfolio model added; not trace-verified"},
    {"surface": "Equity drawdown sizing (EA-16)", "status": "amber", "note": "portfolio model added; floating equity marked at decision points"},
    {"surface": "Break-even path (D1)", "status": "red", "note": "engine ATR-based; EA 35%-of-TP path not modelled"},
    {"surface": "Session/hours filter (D3/D4)", "status": "red", "note": "not enforced; clock not reconciled"},
    {"surface": "Spread/swap economics (D8)", "status": "red", "note": "swap=0, spread not applied to PnL"},
]


def _num(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _confidence_of(result: Any) -> str:
    debug = getattr(result, "debug", None) or {}
    sig = debug.get("signal", {}) if isinstance(debug, dict) else {}
    return str(sig.get("confidence") or "Unknown")


def _ea_file_version(repo_root: Path) -> str:
    ea_path = repo_root / "MT5_stuff" / "TradeExecutor(updated).mq5"
    try:
        head = ea_path.read_text(errors="ignore")[:4000]
        m = re.search(r'#property\s+version\s+"([^"]+)"', head)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"


def _is_closed(result: Any) -> bool:
    return getattr(result, "outcome", "") in CLOSED_OUTCOMES


def _trade_row(result: Any) -> dict:
    return {
        "strategy_id": getattr(result, "strategy_id", None),
        "symbol": getattr(result, "symbol", None),
        "direction": getattr(result, "direction", None),
        "condition_type": getattr(result, "condition_type", None),
        "confirmation": getattr(result, "confirmation", None),
        "timeframe": getattr(result, "timeframe", None),
        "confidence": _confidence_of(result),
        "regime_type": getattr(result, "regime_type", None),
        "news_state": getattr(result, "news_state", None),
        "management_family": getattr(result, "management_family", None),
        "session": getattr(result, "session", None),
        "outcome": getattr(result, "outcome", None),
        "net_pnl": round(_num(getattr(result, "net_pnl", 0.0)), 4),
        "r_multiple": round(_num(getattr(result, "r_multiple", 0.0)), 4),
        "mae_pips": round(_num(getattr(result, "mae_pips", 0.0)), 2),
        "mfe_pips": round(_num(getattr(result, "mfe_pips", 0.0)), 2),
        "exit_efficiency": round(_num(getattr(result, "exit_efficiency", 0.0)), 4),
        "pnl_pips": round(_num(getattr(result, "pnl_pips", 0.0)), 2),
    }


def _aggregate(rows: list[dict], key: str) -> list[dict]:
    buckets: dict[Any, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[r.get(key)].append(r)
    out = []
    for value, items in buckets.items():
        out.append(_cell_metrics({key: value}, items))
    out.sort(key=lambda c: c["n"], reverse=True)
    return out


def _cell_metrics(label: dict, items: list[dict]) -> dict:
    n = len(items)
    wins = [i for i in items if i["net_pnl"] > 0]
    gains = sum(i["net_pnl"] for i in wins)
    losses = sum(-i["net_pnl"] for i in items if i["net_pnl"] < 0)
    rs = [i["r_multiple"] for i in items]
    expectancy = sum(rs) / n if n else 0.0
    se = 0.0
    if n > 1:
        mean = expectancy
        var = sum((x - mean) ** 2 for x in rs) / (n - 1)
        se = math.sqrt(var / n)
    return {
        **label,
        "n": n,
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "expectancy_r": round(expectancy, 4),
        "expectancy_se": round(se, 4),
        "profit_factor": round(gains / losses, 3) if losses > 0 else (None if gains == 0 else 999.0),
        "net_pnl": round(sum(i["net_pnl"] for i in items), 2),
        "avg_mae_pips": round(sum(i["mae_pips"] for i in items) / n, 2) if n else 0.0,
        "avg_mfe_pips": round(sum(i["mfe_pips"] for i in items) / n, 2) if n else 0.0,
        "avg_exit_efficiency": round(sum(i["exit_efficiency"] for i in items) / n, 4) if n else 0.0,
        "low_sample": n < 20,
    }


def build_dashboard_payload(
    run: Any,
    results: list,
    portfolio_outcome: Any,
    ea_config: dict,
    *,
    repo_root: Path,
    n_min: int = 20,
) -> dict:
    closed = [r for r in results if _is_closed(r)]
    entered = [r for r in results if getattr(r, "entry_time", None) is not None]
    closed_rows = [_trade_row(r) for r in closed]

    # ---- TRUST ----
    config_version = str(ea_config.get("ea_version") or "unspecified")
    file_version = _ea_file_version(repo_root)
    outcome_counts: dict[str, int] = defaultdict(int)
    for r in results:
        outcome_counts[getattr(r, "outcome", "unknown")] += 1

    total_strategies = getattr(run, "total_strategies", len(results))
    simulated = len(results)
    errored = sum(1 for r in results if getattr(r, "outcome", "") in {"error", "missing_candles", "missing_broker_specs"})
    distance_blocked = outcome_counts.get("blocked_max_distance", 0)

    trust = {
        "profile": getattr(run, "profile_name", None),
        "ea_version_config": config_version,
        "ea_version_file": file_version,
        "version_match": (config_version == file_version and file_version != "unknown"),
        "parity_surfaces": PARITY_SURFACES,
        "parity_note": "Layer-A bit-exact parity (03) requires an EA Strategy-Tester decision trace, "
                       "not available here. Surfaces are audited status, not trace-measured.",
        "dropped": {
            "loaded": total_strategies,
            "simulated": simulated,
            "errored": errored,
            "reasons": {k: v for k, v in outcome_counts.items()
                        if k in {"error", "missing_candles", "missing_broker_specs",
                                 "unsupported_condition_type"}},
        },
        "counts": {
            "total_strategies": total_strategies,
            "entered": len(entered),
            "closed": len(closed),
            "no_trade": simulated - len(entered),
            "blocked_max_distance": distance_blocked,
            **{k: outcome_counts.get(k, 0) for k in
               ("closed_tp", "closed_sl", "closed_trailing_sl", "closed_early_exit",
                "expired_without_entry", "invalidated_without_entry", "open_at_data_end",
                "rejected_execution_not_allowed", "rejected_lot_size")},
        },
        "realism": {
            "live_signals_available": 14,
            "note": "Only 14 live fills exist; the Layer-B realism band is under-powered. "
                    "Edge numbers are backtest-derived, not live-verified.",
        },
        "sample_power": {"n_min": n_min, "closed_trades": len(closed)},
        "trust_rating": "EXPLORATORY" if not (config_version == file_version) else "AMBER",
        "trust_rating_reason": "Mirrors EA-01/04/08/16/18 added, but full deterministic parity is "
                               "not trace-verified and BE/session/economics surfaces remain open (D1/D3/D8).",
    }

    # ---- EDGE ----
    edge = {
        "n_min": n_min,
        "trades": closed_rows,
        "by_symbol": _aggregate(closed_rows, "symbol"),
        "by_regime": _aggregate(closed_rows, "regime_type"),
        "by_confidence": _aggregate(closed_rows, "confidence"),
        "by_news_state": _aggregate(closed_rows, "news_state"),
        "by_condition_type": _aggregate(closed_rows, "condition_type"),
        "by_management_family": _aggregate(closed_rows, "management_family"),
        "by_session": _aggregate(closed_rows, "session"),
        "leaving_money_on_table": [
            r for r in closed_rows if r["exit_efficiency"] < 0.5 and r["mfe_pips"] > 0
        ],
    }

    # regime sign-flip detector: a condition_type that is +EV in one regime and -EV in another
    cond_regime: dict[tuple, list[dict]] = defaultdict(list)
    for r in closed_rows:
        cond_regime[(r["condition_type"], r["regime_type"])].append(r)
    sign_flips = []
    by_cond: dict[Any, dict[Any, float]] = defaultdict(dict)
    for (cond, regime), items in cond_regime.items():
        exp = sum(i["r_multiple"] for i in items) / len(items)
        by_cond[cond][regime] = exp
    for cond, regimes in by_cond.items():
        vals = list(regimes.values())
        if any(v > 0 for v in vals) and any(v < 0 for v in vals):
            sign_flips.append({"condition_type": cond, "by_regime": {k: round(v, 3) for k, v in regimes.items()}})
    edge["regime_sign_flips"] = sign_flips

    # ---- CONTAMINATION ----
    contamination = {
        "by_news_state": _aggregate(closed_rows, "news_state"),
        "by_regime": _aggregate(closed_rows, "regime_type"),
        "flags": [],
        "notes": "Tier-2 news directional-accuracy / false-positive rate require joins to "
                 "economic_event_analysis + regime independence A/B (F1/F2), which are workflow-"
                 "scope, not engine-scope. Shown here: win-rate / expectancy proxy by news_state "
                 "and regime, plus the regime sign-flip detector under EDGE.",
    }
    news_cells = {c.get("news_state"): c for c in contamination["by_news_state"]}
    active = news_cells.get("active_news_window")
    quiet = news_cells.get("quiet")
    if active and quiet and active["n"] >= 5 and quiet["n"] >= 5:
        delta = round(active["expectancy_r"] - quiet["expectancy_r"], 3)
        contamination["news_window_expectancy_delta_r"] = delta
        if delta < -0.2:
            contamination["flags"].append(
                f"Expectancy drops {delta}R inside active-news windows vs quiet (n_active={active['n']})."
            )
    if sign_flips:
        contamination["flags"].append(
            f"{len(sign_flips)} condition type(s) flip expectancy sign across regimes — possible regime misfit."
        )

    # ---- PORTFOLIO ----
    portfolio = None
    if portfolio_outcome is not None:
        po = portfolio_outcome
        portfolio = {
            "summary": po.to_summary_dict(),
            "equity_curve": [
                {"t": (t.isoformat() if isinstance(t, datetime) else None),
                 "equity": round(eq, 2), "balance": round(bal, 2)}
                for (t, eq, bal) in po.equity_curve
            ],
            "reject_reasons": po.reject_reasons,
            "trades": [
                {
                    "strategy_id": t.strategy_id, "symbol": t.symbol, "direction": t.direction,
                    "entry_time": t.entry_time.isoformat() if isinstance(t.entry_time, datetime) else None,
                    "exit_time": t.exit_time.isoformat() if isinstance(t.exit_time, datetime) else None,
                    "admitted": t.admitted, "reject_reason": t.reject_reason,
                    "independent_lot": round(t.independent_lot, 4), "portfolio_lot": round(t.portfolio_lot, 4),
                    "net_pnl": round(t.net_pnl, 2), "equity_at_entry": round(t.equity_at_entry, 2),
                    "drawdown_pct_at_entry": round(t.drawdown_pct_at_entry, 2),
                    "drawdown_sizing_applied": t.drawdown_sizing_applied,
                    "balance_after": round(t.balance_after, 2), "concurrent_at_entry": t.concurrent_at_entry,
                }
                for t in po.trades
            ],
        }

    date_span = {
        "from": getattr(run, "started_at", None) and _safe_iso(getattr(run, "started_at")),
        "to": getattr(run, "finished_at", None) and _safe_iso(getattr(run, "finished_at")),
    }

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": {
            "run_id": str(getattr(run, "run_id", "")),
            "run_name": getattr(run, "run_name", None),
            "profile_name": getattr(run, "profile_name", None),
            "ea_config_hash": getattr(run, "ea_config_hash", None),
            "source_database_name": getattr(run, "source_database_name", None),
            "fill_model": getattr(run, "fill_model", None),
            "date_span": date_span,
        },
        "trust": trust,
        "edge": edge,
        "contamination": contamination,
        "portfolio": portfolio,
    }


def _safe_iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def write_dashboard_payload(payload: dict, path: Path) -> Path:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
