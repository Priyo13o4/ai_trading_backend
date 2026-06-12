import os
import sys
import argparse
import uuid
import json
from datetime import datetime, timezone
from collections import defaultdict
import numpy as np
import pandas as pd
from sqlalchemy import text

# Add parent directory to path so we can import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_engine.db import AsyncSessionLocal
from backtest_engine.source_db import SourceAsyncSessionLocal

def determine_parameters(desc):
    # e.g. "EURUSD_BE1.5_MATrail14" -> symbol="EURUSD", BE=1.5, trail="MATrail14"
    parts = desc.split('_')
    symbol = parts[0]
    be = float(parts[1].replace('BE', '')) if len(parts) > 1 else 2.0
    trail = parts[2] if len(parts) > 2 else "NoMATrail"
    return symbol, be, trail

async def main(sweep_id: str, output_path: str):
    print(f"Exporting sweep: {sweep_id} to {output_path}")
    
    async with AsyncSessionLocal() as dest_session:
        # 1. Fetch all runs for the sweep
        if sweep_id == "latest":
            # Find the latest sweep id from runs
            run_query = text("""
                SELECT DISTINCT strategy_filter->>'sweep_id' as sweep_id
                FROM backtest_runs
                WHERE strategy_filter->>'sweep_id' IS NOT NULL
                ORDER BY sweep_id DESC
                LIMIT 1
            """)
            res = await dest_session.execute(run_query)
            row = res.fetchone()
            if not row or not row[0]:
                print("No sweeps found in database.")
                return
            sweep_id = row[0]
            
        print(f"Loading runs for Sweep ID: {sweep_id}")
        runs_query = text("""
            SELECT run_id, run_name, ea_config, strategy_filter, started_at, management_family
            FROM backtest_runs
            WHERE strategy_filter->>'sweep_id' = :sweep_id
            ORDER BY started_at ASC
        """)
        res = await dest_session.execute(runs_query, {"sweep_id": sweep_id})
        runs = res.fetchall()
        
        if not runs:
            print(f"No runs found for sweep: {sweep_id}")
            return
            
        sweep_data = {
            "sweep_id": sweep_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "variants": []
        }
        
        for run in runs:
            run_uuid = run.run_id
            strategy_filter = run.strategy_filter or {}
            variant_id = strategy_filter.get("variant_id", "unknown")
            overrides = strategy_filter.get("variant_overrides", {})
            
            # Fetch results
            res_query = text("""
                SELECT strategy_id, symbol, direction, condition_type, timeframe, confirmation,
                       strategy_timestamp, strategy_expiry_time, outcome, outcome_reason,
                       entry_time, entry_price, exit_time, exit_price, take_profit, stop_loss,
                       initial_stop_loss, final_stop_loss, lot_size, net_pnl, pnl_pips,
                       mae_pips, mfe_pips, exit_efficiency, balance_before, balance_after,
                       r_multiple, management_family, regime_type, news_state, session,
                       theoretical_fixed_tp_net_pnl, theoretical_fixed_tp_win, opportunity_cost_flag,
                       lot_floor_violation, risk_exceeded_due_to_min_lot
                FROM backtest_results
                WHERE run_id = :run_id
            """)
            res_trades = await dest_session.execute(res_query, {"run_id": run_uuid})
            trades = [dict(t._mapping) for t in res_trades.fetchall()]
            
            if not trades:
                continue
                
            # Filter entered vs missed
            entered_trades = [t for t in trades if t['entry_time'] is not None]
            missed_trades = [t for t in trades if t['entry_time'] is None]
            
            # Calculate summary stats
            total_trades = len(entered_trades)
            wins = [t for t in entered_trades if t['net_pnl'] is not None and float(t['net_pnl']) > 0]
            losses = [t for t in entered_trades if t['net_pnl'] is not None and float(t['net_pnl']) < 0]
            
            win_rate = len(wins) / total_trades if total_trades > 0 else 0
            
            sum_win_pnl = sum(float(t['net_pnl']) for t in wins)
            sum_loss_pnl = sum(float(t['net_pnl']) for t in losses)
            profit_factor = sum_win_pnl / abs(sum_loss_pnl) if sum_loss_pnl != 0 else float('nan')
            
            total_net_pnl = sum(float(t['net_pnl'] or 0.0) for t in entered_trades)
            
            # MAE/MFE averages
            avg_mae = np.mean([float(t['mae_pips'] or 0.0) for t in entered_trades]) if total_trades > 0 else 0
            avg_mfe = np.mean([float(t['mfe_pips'] or 0.0) for t in entered_trades]) if total_trades > 0 else 0
            
            # Risk Violations
            violations = []
            for t in entered_trades:
                if t['net_pnl'] is not None and t['balance_before'] is not None:
                    pnl = float(t['net_pnl'])
                    bal = float(t['balance_before'])
                    if bal > 0 and pnl < 0:
                        risk_pct = -pnl / bal
                        if risk_pct > 0.022:
                            violations.append(risk_pct)
            
            # Session breakdown
            session_breakdown = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
            for t in entered_trades:
                sess = t['session'] or "Unknown"
                pnl = float(t['net_pnl'] or 0.0)
                session_breakdown[sess]['trades'] += 1
                session_breakdown[sess]['pnl'] += pnl
                if pnl > 0:
                    session_breakdown[sess]['wins'] += 1
                    
            # Entry breakdown
            entry_breakdown = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
            for t in entered_trades:
                ctype = t['condition_type'] or "unknown"
                pnl = float(t['net_pnl'] or 0.0)
                entry_breakdown[ctype]['trades'] += 1
                entry_breakdown[ctype]['pnl'] += pnl
                if pnl > 0:
                    entry_breakdown[ctype]['wins'] += 1
                    
            # Regime breakdown
            regime_breakdown = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
            for t in entered_trades:
                regime = t['regime_type'] or "Ranging"
                pnl = float(t['net_pnl'] or 0.0)
                regime_breakdown[regime]['trades'] += 1
                regime_breakdown[regime]['pnl'] += pnl
                if pnl > 0:
                    regime_breakdown[regime]['wins'] += 1
                    
            # News breakdown
            news_breakdown = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
            for t in entered_trades:
                news = t['news_state'] or "quiet"
                pnl = float(t['net_pnl'] or 0.0)
                news_breakdown[news]['trades'] += 1
                news_breakdown[news]['pnl'] += pnl
                if pnl > 0:
                    news_breakdown[news]['wins'] += 1
                    
            # Missed trade forward testing analysis
            # calculate opportunity cost
            opp_wins = 0
            opp_cost_pnl = 0.0
            opp_trades_triggered = 0
            for t in missed_trades:
                if t['opportunity_cost_flag'] is True:
                    opp_trades_triggered += 1
                    opp_wins += 1
                    # Assume $10 risk (2%) and 1.5 RR profit = $15 profit
                    opp_cost_pnl += 15.0
                elif t['opportunity_cost_flag'] is False:
                    # If it touched zone but didn't hit TP (so it would have hit SL)
                    # We can assume a loss of $10
                    opp_trades_triggered += 1
                    opp_cost_pnl -= 10.0
                    
            # Format trades list for react JSON serialization
            serialized_trades = []
            for t in entered_trades:
                serialized_trades.append({
                    "strategy_id": t["strategy_id"],
                    "symbol": t["symbol"],
                    "direction": t["direction"],
                    "condition_type": t["condition_type"],
                    "timeframe": t["timeframe"],
                    "confirmation": t["confirmation"],
                    "strategy_timestamp": t["strategy_timestamp"].isoformat() if t["strategy_timestamp"] else None,
                    "entry_time": t["entry_time"].isoformat() if t["entry_time"] else None,
                    "exit_time": t["exit_time"].isoformat() if t["exit_time"] else None,
                    "entry_price": float(t["entry_price"]) if t["entry_price"] else None,
                    "exit_price": float(t["exit_price"]) if t["exit_price"] else None,
                    "net_pnl": float(t["net_pnl"]) if t["net_pnl"] else 0.0,
                    "pnl_pips": float(t["pnl_pips"]) if t["pnl_pips"] else 0.0,
                    "mae_pips": float(t["mae_pips"]) if t["mae_pips"] is not None else 0.0,
                    "mfe_pips": float(t["mfe_pips"]) if t["mfe_pips"] is not None else 0.0,
                    "exit_efficiency": float(t["exit_efficiency"]) if t["exit_efficiency"] is not None else 0.0,
                    "initial_sl": float(t["initial_stop_loss"]) if t["initial_stop_loss"] else None,
                    "initial_tp": float(t["take_profit"]) if t["take_profit"] else None,
                    "outcome": t["outcome"],
                    "outcome_reason": t["outcome_reason"],
                    "lot_size": float(t["lot_size"]) if t["lot_size"] else 0.01,
                    "regime_type": t["regime_type"] or "Ranging",
                    "news_state": t["news_state"] or "quiet",
                    "session": t["session"] or "Unknown",
                    "theo_net_pnl": float(t["theoretical_fixed_tp_net_pnl"]) if t["theoretical_fixed_tp_net_pnl"] is not None else 0.0,
                    "theo_win": bool(t["theoretical_fixed_tp_win"]),
                    "lot_floor_violation": bool(t.get("lot_floor_violation", False)),
                    "risk_exceeded_due_to_min_lot": bool(t.get("risk_exceeded_due_to_min_lot", False))
                })
                
            # Parse description from runs overrides
            symbol = overrides.get("symbol", "unknown")
            be_mult = overrides.get("break_even_atr_multiplier", 0.0)
            trail_type = "NoMATrail"
            if overrides.get("use_ma_trailing_stop"):
                trail_type = f"MATrail{overrides.get('ma_trail_period', 21)}"
            elif overrides.get("use_trailing_stop"):
                trail_type = "ATRTrail"
                
            variant_desc = getattr(run, "run_name", "") or str(run_uuid)
            
            sweep_data["variants"].append({
                "variant_id": variant_id,
                "run_id": str(run_uuid),
                "variant_description": variant_desc,
                "symbol": symbol,
                "be_multiplier": be_mult,
                "trail_type": trail_type,
                "management_family": getattr(run, "management_family", "unknown"),
                "total_trades": total_trades,
                "win_rate": win_rate,
                "net_pnl": total_net_pnl,
                "profit_factor": profit_factor if not np.isnan(profit_factor) else None,
                "avg_mae": float(avg_mae),
                "avg_mfe": float(avg_mfe),
                "risk_violations_count": len(violations),
                "max_risk_violation": float(max(violations)) if len(violations) > 0 else 0.0,
                "missed_trades_count": len(missed_trades),
                "missed_trades_triggered_count": opp_trades_triggered,
                "missed_trades_tp_count": opp_wins,
                "opportunity_cost_pnl": opp_cost_pnl,
                "session_breakdown": dict(session_breakdown),
                "entry_breakdown": dict(entry_breakdown),
                "regime_breakdown": dict(regime_breakdown),
                "news_breakdown": dict(news_breakdown),
                "trades": serialized_trades
            })
            
        # 3. Save JSON output
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(sweep_data, f, indent=2)
        print(f"Successfully exported data to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep-id", type=str, default="latest", help="Sweep ID to export or 'latest'")
    parser.add_argument("--output", type=str, default="../ai_trading_backtest_dashboard/public/sweep_data.json", help="Path to save sweep_data.json")
    args = parser.parse_args()
    
    import asyncio
    asyncio.run(main(args.sweep_id, args.output))
