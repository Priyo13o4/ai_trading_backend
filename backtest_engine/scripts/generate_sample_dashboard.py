"""Generate a representative dashboard_data.json without a live DB run.

This exercises the full export pipeline (simulate_portfolio -> build_dashboard_payload, reading
the REAL EA #property version for the trust version-pin) on synthetic-but-realistic results so
the Trust/Edge/Contamination/Portfolio dashboard can be developed and demoed offline. The trust
card is clearly marked SAMPLE. For a real run use:

    python -m backtest_engine.cli run --portfolio \
        --dashboard-out ../ai_trading_backtest_dashboard/public/

Usage:
    PYTHONPATH=. .venv/bin/python scripts/generate_sample_dashboard.py [out_path]
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from backtest_engine.simulation.portfolio import simulate_portfolio, PortfolioTradeCtx
from backtest_engine.reporting.dashboard_export import build_dashboard_payload, write_dashboard_payload
from backtest_engine.broker_specs import BrokerSymbolSpec

UTC = timezone.utc
random.seed(7)

SYMBOLS = ["EURUSD", "XAUUSD", "GBPUSD", "USDJPY"]
REGIMES = ["Trending", "Ranging", "Volatile"]
NEWS = ["quiet", "active_news_window"]
CONFS = ["High", "Medium", "Low"]
CONDS = ["breakout_close", "zone_retest", "range_breakout", "pullback_entry", "liquidity_grab"]
FAMILIES = ["partial_then_be", "atr_trailing", "break_even"]
SESSIONS = ["London", "New York", "Asian", "London/NY Overlap"]
CLOSED = ["closed_tp", "closed_sl", "closed_trailing_sl", "closed_early_exit"]


def _spec(symbol):
    digits = 3 if symbol == "XAUUSD" else (3 if symbol == "USDJPY" else 5)
    point = 10 ** (-digits)
    return BrokerSymbolSpec(
        symbol=symbol, exists=True, selected=True, digits=digits, point=point, spread_points=10,
        spread_float=False, trade_mode=4, trade_calc_mode=0, contract_size=100000.0,
        tick_size=point, tick_value=0.1 if symbol != "XAUUSD" else 1.0, tick_value_profit=0.1,
        tick_value_loss=0.1, volume_min=0.01, volume_max=100.0, volume_step=0.01, volume_limit=0.0,
        stops_level=0, freeze_level=0, currency_base=symbol[:3], currency_profit=symbol[3:],
        currency_margin=symbol[:3], swap_mode=0, swap_long=0.0, swap_short=0.0, margin_initial=0.0,
        margin_maintenance=0.0, commission_per_lot_round_turn_assumption=5.0,
    )


def _make_results(n=70):
    results = []
    t0 = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
    for i in range(n):
        symbol = random.choice(SYMBOLS)
        direction = random.choice(["long", "short"])
        outcome = random.choices(CLOSED, weights=[0.34, 0.4, 0.18, 0.08])[0]
        # bias gold/active-news toward losses to make contamination/edge signals visible
        news = random.choice(NEWS)
        conf = random.choice(CONFS)
        win = outcome == "closed_tp" or (outcome == "closed_trailing_sl" and random.random() < 0.5)
        if symbol == "XAUUSD" and news == "active_news_window":
            win = random.random() < 0.3
        r_mult = round(random.uniform(0.6, 2.2) if win else random.uniform(-1.1, -0.3), 3)
        net = round(r_mult * random.uniform(8, 22), 2)
        entry_t = t0 + timedelta(hours=i * 5 + random.randint(0, 3))
        exit_t = entry_t + timedelta(hours=random.randint(2, 18))
        entry_price = 1.10 if symbol == "EURUSD" else (2330.0 if symbol == "XAUUSD" else (1.27 if symbol == "GBPUSD" else 156.0))
        sl = entry_price * (0.99 if direction == "long" else 1.01)
        r = SimpleNamespace(
            strategy_id=1000 + i, symbol=symbol, direction=direction,
            condition_type=random.choice(CONDS), confirmation=random.choice(["none", "engulfing", "pin_bar"]),
            timeframe=random.choice(["M15", "H1", "H4"]), regime_type=random.choice(REGIMES),
            news_state=news, management_family=random.choice(FAMILIES), session=random.choice(SESSIONS),
            outcome=outcome, net_pnl=net, r_multiple=r_mult,
            mae_pips=round(abs(random.uniform(3, 40)), 1), mfe_pips=round(abs(random.uniform(5, 60)), 1),
            exit_efficiency=round(random.uniform(0.2, 1.0), 2), pnl_pips=round(random.uniform(-30, 50), 1),
            entry_time=entry_t, exit_time=exit_t, lot_size=round(random.choice([0.05, 0.08, 0.1]), 2),
            entry_price=entry_price, initial_stop_loss=sl,
            equity_high_watermark=None, drawdown_after=None,
            debug={"signal": {"confidence": conf}},
            pf_ctx=PortfolioTradeCtx(confidence=conf, risk_reward_ratio=round(random.uniform(1.0, 3.0), 1),
                                     mark_times=[], mark_prices=[]),
        )
        results.append(r)
    # inject a few opposite-direction overlaps (EA-04) and a losing cluster (EA-16)
    results.sort(key=lambda x: x.entry_time)
    return results


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent.parent / "ai_trading_backtest_dashboard" / "public" / "dashboard_data.json"
    )
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    results = _make_results()
    specs = {s: _spec(s) for s in SYMBOLS}
    ea_config = {
        "ea_version": "3.31", "block_opposite_open_positions": True, "max_concurrent_trades": 20,
        "max_total_risk_percent": 95.0, "use_drawdown_protection": True, "drawdown_threshold": 10.0,
        "drawdown_reduction_factor": 0.5, "max_risk_percent_per_trade": 2.0,
    }
    po = simulate_portfolio(results, ea_config, specs, starting_balance=500.0)

    now = datetime.now(UTC)
    run = SimpleNamespace(
        run_id="sample-0000", run_name="SAMPLE (synthetic — not a real DB run)",
        profile_name="ea_v3_00", ea_config_hash="sampledemohash00",
        source_database_name="(synthetic)", fill_model="m1_ohlc_conservative",
        started_at=now - timedelta(days=20), finished_at=now, total_strategies=len(results),
    )
    payload = build_dashboard_payload(run, results, po, ea_config, repo_root=repo_root)
    payload["trust"]["trust_rating_reason"] = (
        "SAMPLE synthetic data for dashboard development. " + payload["trust"]["trust_rating_reason"]
    )
    write_dashboard_payload(payload, out)
    print(f"Wrote {out}")
    print(f"Portfolio: end ${po.final_balance:.2f} | maxDD {po.max_drawdown_pct:.1f}% | "
          f"PF {po.profit_factor:.2f} | admitted {po.num_admitted}/{po.num_candidates} | {po.reject_reasons}")
    print(f"Trust: version_match={payload['trust']['version_match']} "
          f"(file={payload['trust']['ea_version_file']}, config={payload['trust']['ea_version_config']})")


if __name__ == "__main__":
    main()
