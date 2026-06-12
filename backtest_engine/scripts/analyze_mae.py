import asyncio
import os
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from backtest_engine.models import BacktestRun, BacktestResult

BACKTEST_DATABASE_URL = os.getenv("BACKTEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/backtest_lab")

async def analyze_latest_sweep():
    engine = create_async_engine(BACKTEST_DATABASE_URL, echo=False)
    Session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    
    async with Session() as session:
        stmt = select(BacktestRun).order_by(BacktestRun.started_at.desc()).limit(12)
        result = await session.execute(stmt)
        runs = result.scalars().all()
        
        if not runs:
            print("No backtest runs found!")
            return
            
        sweep_id = runs[0].strategy_filter.get("sweep_id")
        print(f"Analyzing Sweep: {sweep_id}")
        
        run_ids = [r.run_id for r in runs if r.strategy_filter.get("sweep_id") == sweep_id]
        
        stmt = select(BacktestResult).where(BacktestResult.run_id.in_(run_ids))
        result = await session.execute(stmt)
        all_results = result.scalars().all()
        
        data = []
        for res in all_results:
            variant_id = next((r.strategy_filter.get("variant_id") for r in runs if r.run_id == res.run_id), "unknown")
            data.append({
                "variant": variant_id,
                "symbol": res.symbol,
                "outcome": res.outcome,
                "net_pnl": float(res.net_pnl),
                "mae_pips": float(res.mae_pips) if res.mae_pips is not None else 0.0,
                "mfe_pips": float(res.mfe_pips) if res.mfe_pips is not None else 0.0
            })
            
        df = pd.DataFrame(data)
        
        # Calculate summary per variant and symbol
        summary = df.groupby(["variant", "symbol"]).agg(
            trades=("outcome", "count"),
            win_rate=("outcome", lambda x: (x == "closed_tp").sum() / len(x) * 100),
            avg_mae=("mae_pips", "mean"),
            avg_mfe=("mfe_pips", "mean"),
            mae_count=("mae_pips", lambda x: (x > 0).sum()),
            mfe_count=("mfe_pips", lambda x: (x > 0).sum()),
            net_pnl=("net_pnl", "sum")
        ).reset_index()
        
        print("\n--- BEST MA CONFIGURATION PER SYMBOL ---")
        best_per_symbol = []
        for symbol in summary['symbol'].unique():
            symbol_data = summary[summary['symbol'] == symbol]
            # Find variant with highest net_pnl for this symbol
            best = symbol_data.loc[symbol_data['net_pnl'].idxmax()]
            best_per_symbol.append(best)
            
        best_df = pd.DataFrame(best_per_symbol)
        best_df = best_df.sort_values("symbol")
        # Format the output table nicely
        best_df['win_rate'] = best_df['win_rate'].round(1).astype(str) + "%"
        best_df['avg_mae'] = best_df['avg_mae'].round(1)
        best_df['avg_mfe'] = best_df['avg_mfe'].round(1)
        best_df['net_pnl'] = best_df['net_pnl'].round(2)
        
        print(best_df.to_string(index=False))
        
        print("\n--- OVERALL TOP VARIANTS ---")
        overall = df.groupby("variant").agg(
            trades=("outcome", "count"),
            net_pnl=("net_pnl", "sum"),
            avg_mae=("mae_pips", "mean"),
            avg_mfe=("mfe_pips", "mean"),
            mae_count=("mae_pips", lambda x: (x > 0).sum()),
            mfe_count=("mfe_pips", lambda x: (x > 0).sum()),
        ).reset_index().sort_values("net_pnl", ascending=False)
        overall['avg_mae'] = overall['avg_mae'].round(1)
        overall['avg_mfe'] = overall['avg_mfe'].round(1)
        overall['net_pnl'] = overall['net_pnl'].round(2)
        print(overall.to_string(index=False))

if __name__ == "__main__":
    asyncio.run(analyze_latest_sweep())
