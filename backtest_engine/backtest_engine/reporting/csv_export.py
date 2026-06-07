import csv
from pathlib import Path
from typing import List
from backtest_engine.models import BacktestResult

def export_results_to_csv(results: List[BacktestResult], filepath: str):
    """Export backtest results to CSV."""
    with open(filepath, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'result_id', 'run_id', 'strategy_id', 'symbol', 'direction', 'condition_type',
            'timeframe', 'confirmation', 'strategy_timestamp', 'strategy_expiry_time',
            'outcome', 'outcome_reason', 'entry_time', 'entry_price', 'exit_time', 
            'exit_price', 'take_profit', 'stop_loss', 'initial_stop_loss', 'final_stop_loss',
            'lot_size', 'partial_close_executed', 'break_even_moved', 'hit_tp', 'hit_sl',
            'gross_pnl', 'commission', 'swap', 'net_pnl', 'pnl_pips', 'r_multiple',
            'balance_before', 'balance_after', 'equity_high_watermark', 'drawdown_after',
            'bars_scanned'
        ])
        
        for r in results:
            writer.writerow([
                r.result_id, r.run_id, r.strategy_id, r.symbol, r.direction, r.condition_type,
                r.timeframe, r.confirmation, 
                r.strategy_timestamp.isoformat() if r.strategy_timestamp else "",
                r.strategy_expiry_time.isoformat() if r.strategy_expiry_time else "",
                r.outcome, r.outcome_reason,
                r.entry_time.isoformat() if r.entry_time else "",
                r.entry_price,
                r.exit_time.isoformat() if r.exit_time else "",
                r.exit_price, r.take_profit, r.stop_loss, r.initial_stop_loss, r.final_stop_loss,
                r.lot_size, r.partial_close_executed, r.break_even_moved, r.hit_tp, r.hit_sl,
                r.gross_pnl, r.commission, r.swap, r.net_pnl, r.pnl_pips, r.r_multiple,
                r.balance_before, r.balance_after, r.equity_high_watermark, r.drawdown_after,
                r.bars_scanned
            ])


def export_frame_to_csv(df, filepath: str | Path):
    """Export a pandas DataFrame summary to CSV."""
    df.to_csv(filepath, index=False)
