import asyncio
import argparse
import uuid
import pandas as pd
from datetime import timedelta
from collections import defaultdict
from sqlalchemy import select, text
from backtest_engine.db import AsyncSessionLocal
from backtest_engine.source_db import SourceAsyncSessionLocal
from backtest_engine.models import BacktestRun, BacktestResult

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from common.trading_common.timeframes import cagg_relation_for_timeframe

def get_session_name(dt):
    if dt is None:
        return "Unknown"
    h = dt.hour
    if 0 <= h < 8:
        return "Asian"
    elif 8 <= h < 13:
        return "London"
    elif 13 <= h < 16:
        return "London/NY Overlap"
    elif 16 <= h < 21:
        return "New York"
    else:
        return "Late NY/Sydney"

async def fetch_future_candles(source_session, symbol, start_time, candles_count=20):
    # Fetch next M5 candles
    end_time = start_time + timedelta(minutes=5 * candles_count)
    table_name = cagg_relation_for_timeframe("M5")
    query = text(f"""
        SELECT time, open, high, low, close
        FROM {table_name}
        WHERE symbol = :symbol AND time >= :start_time AND time <= :end_time
        ORDER BY time ASC
        LIMIT :limit
    """)
    res = await source_session.execute(query, {
        "symbol": symbol,
        "start_time": start_time,
        "end_time": end_time,
        "limit": candles_count
    })
    return res.fetchall()

async def analyze_failure_taxonomy(source_session, losing_trades):
    taxonomy = {"liquidity_sweep": 0, "early_entry": 0, "dead_wrong": 0}
    for trade in losing_trades:
        if not trade.exit_time or not trade.stop_loss or not trade.take_profit:
            taxonomy["dead_wrong"] += 1
            continue
            
        candles = await fetch_future_candles(source_session, trade.symbol, trade.exit_time, candles_count=24) # Next 2 hours
        
        if not candles:
            taxonomy["dead_wrong"] += 1
            continue

        hit_tp = False
        chopped = False
        
        for row in candles:
            low, high = row.low, row.high
            # Check if hit TP
            if trade.direction == "long":
                if high >= trade.take_profit:
                    hit_tp = True
                    break
                if low <= trade.stop_loss * 0.999: # Continued lower
                    pass
            else:
                if low <= trade.take_profit:
                    hit_tp = True
                    break
                if high >= trade.stop_loss * 1.001: # Continued higher
                    pass
                    
        if hit_tp:
            taxonomy["liquidity_sweep"] += 1
        else:
            # Simple heuristic for early entry vs dead wrong
            # Did it close near the SL level?
            final_close = candles[-1].close
            if trade.direction == "long":
                if final_close > trade.stop_loss:
                    taxonomy["early_entry"] += 1
                else:
                    taxonomy["dead_wrong"] += 1
            else:
                if final_close < trade.stop_loss:
                    taxonomy["early_entry"] += 1
                else:
                    taxonomy["dead_wrong"] += 1
                    
    return taxonomy

async def analyze_missed_trades(source_session, missed_trades):
    results = {"would_hit_tp": 0, "would_hit_sl": 0, "unknown": 0}
    for trade in missed_trades:
        tp = trade.take_profit
        sl = trade.initial_stop_loss or trade.stop_loss
        
        if not tp or not sl:
            results["unknown"] += 1
            continue
            
        candles = await fetch_future_candles(source_session, trade.symbol, trade.strategy_timestamp, candles_count=48)
        
        outcome = "unknown"
        for row in candles:
            low, high = row.low, row.high
            if trade.direction == "long":
                if low <= sl:
                    outcome = "would_hit_sl"
                    break
                if high >= tp:
                    outcome = "would_hit_tp"
                    break
            else:
                if high >= sl:
                    outcome = "would_hit_sl"
                    break
                if low <= tp:
                    outcome = "would_hit_tp"
                    break
                    
        results[outcome] += 1
    return results

async def main(run_id: str):
    print(f"Loading data for run: {run_id}")
    
    async with SourceAsyncSessionLocal() as source_session:
        async with AsyncSessionLocal() as dest_session:
            run_uuid = uuid.UUID(run_id) if run_id != "latest" else None
            
            if run_id == "latest":
                query = select(BacktestRun).order_by(BacktestRun.started_at.desc()).limit(1)
                res = await dest_session.execute(query)
                run = res.scalar_one_or_none()
                if not run:
                    print("No runs found.")
                    return
                run_uuid = run.run_id
                
            print(f"Analyzing Run UUID: {run_uuid}")
            
            # Load all results
            res_query = select(BacktestResult).where(BacktestResult.run_id == run_uuid)
            res = await dest_session.execute(res_query)
            results = res.scalars().all()
            
            if not results:
                print("No results found for this run.")
                return
                
            # MAE/MFE by variant
            variants = defaultdict(lambda: {"mae_sum": 0, "mfe_sum": 0, "count": 0})
            losing_trades = []
            missed_trades = []
            session_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
            
            for r in results:
                # Group by variant hash or profile hash
                var_id = r.profile_hash
                if r.mae_pips is not None:
                    variants[var_id]["mae_sum"] += float(r.mae_pips)
                    variants[var_id]["mfe_sum"] += float(r.mfe_pips or 0)
                    variants[var_id]["count"] += 1
                    
                if r.outcome == "closed_sl":
                    losing_trades.append(r)
                    
                if r.outcome == "invalidated_before_entry":
                    missed_trades.append(r)
                    
                if r.entry_time:
                    sess = get_session_name(r.entry_time)
                    pnl = float(r.net_pnl or 0)
                    if pnl > 0:
                        session_perf[sess]["wins"] += 1
                    elif pnl < 0:
                        session_perf[sess]["losses"] += 1
                    session_perf[sess]["pnl"] += pnl

            print("\n--- MAE / MFE per Variant ---")
            for var_id, data in variants.items():
                if data["count"] > 0:
                    avg_mae = data["mae_sum"] / data["count"]
                    avg_mfe = data["mfe_sum"] / data["count"]
                    print(f"Variant {var_id[:8]}: Avg MAE = {avg_mae:.2f} pips, Avg MFE = {avg_mfe:.2f} pips")
                    
            print("\n--- Failure Taxonomy ---")
            if losing_trades:
                print(f"Analyzing {len(losing_trades)} losing trades...")
                taxonomy = await analyze_failure_taxonomy(source_session, losing_trades)
                for k, v in taxonomy.items():
                    print(f"  {k}: {v} trades ({v/len(losing_trades)*100:.1f}%)")
            else:
                print("No losing trades found.")
                
            print("\n--- Missed Trade Evaluator ---")
            if missed_trades:
                print(f"Analyzing {len(missed_trades)} missed trades...")
                missed = await analyze_missed_trades(source_session, missed_trades)
                for k, v in missed.items():
                    print(f"  {k}: {v} trades ({v/len(missed_trades)*100:.1f}%)")
            else:
                print("No missed trades found.")
                
            print("\n--- Session Performance ---")
            for sess, data in session_perf.items():
                total = data["wins"] + data["losses"]
                win_rate = data["wins"] / total * 100 if total > 0 else 0
                print(f"{sess:18} | Wins: {data['wins']:3} | Losses: {data['losses']:3} | WinRate: {win_rate:5.1f}% | Net PnL: {data['pnl']:.2f}")



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=str, default="latest", help="Run UUID or 'latest'")
    args = parser.parse_args()
    asyncio.run(main(args.run_id))
