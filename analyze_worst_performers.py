import csv
import sys
from collections import defaultdict
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_worst_performers.py <path_to_results.csv>")
        return
        
    raw_results_path = Path(sys.argv[1])
    
    if not raw_results_path.exists():
        print(f"File not found: {raw_results_path}")
        return

    # structure: dict mapping (symbol, condition_type, direction) -> {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl': 0.0}
    stats = defaultdict(lambda: {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl': 0.0})
    
    with open(raw_results_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get('symbol', 'UNKNOWN')
            ctype = row.get('condition_type', 'UNKNOWN')
            direction = row.get('direction', 'UNKNOWN')
            outcome = row.get('outcome', '')
            
            if outcome not in ['closed_tp', 'closed_sl', 'closed_trailing_sl']:
                continue
                
            try:
                pnl = float(row.get('net_pnl', 0.0))
            except ValueError:
                pnl = 0.0
                
            key = (sym, ctype, direction)
            stats[key]['total_trades'] += 1
            stats[key]['net_pnl'] += pnl
            
            if outcome == 'closed_tp':
                stats[key]['wins'] += 1
            elif outcome == 'closed_sl':
                stats[key]['losses'] += 1
                
    # Format and sort results
    results = []
    for (sym, ctype, direction), s in stats.items():
        total = s['total_trades']
        win_rate = (s['wins'] / total * 100) if total > 0 else 0
        loss_rate = (s['losses'] / total * 100) if total > 0 else 0
        
        results.append({
            'symbol': sym,
            'condition_type': ctype,
            'direction': direction,
            'total_trades': total,
            'net_pnl': s['net_pnl'],
            'win_rate': round(win_rate, 1),
            'loss_rate': round(loss_rate, 1)
        })
        
    worst_pnl = sorted(results, key=lambda x: x['net_pnl'])[:15]
    
    print("=== TOP 15 WORST PERFORMING (SYMBOL + STRATEGY + DIRECTION) BY NET PNL ===")
    print(f"{'SYMBOL':<10} | {'STRATEGY':<30} | {'DIR':<5} | {'TRADES':<6} | {'WIN%':<6} | {'LOSS%':<6} | {'NET PNL':<10}")
    print("-" * 85)
    for r in worst_pnl:
        print(f"{r['symbol']:<10} | {r['condition_type']:<30} | {r['direction']:<5} | {r['total_trades']:<6} | {r['win_rate']:<6} | {r['loss_rate']:<6} | {r['net_pnl']:<10.2f}")
        
    print("\n")
    
    # Worst loss rate with minimum 5 trades
    sig_trades = [r for r in results if r['total_trades'] >= 5]
    worst_loss_rate = sorted(sig_trades, key=lambda x: (-x['loss_rate'], x['net_pnl']))[:15]
    
    print("=== TOP 15 WORST PERFORMING BY LOSS RATE (Min 5 Trades) ===")
    print(f"{'SYMBOL':<10} | {'STRATEGY':<30} | {'DIR':<5} | {'TRADES':<6} | {'WIN%':<6} | {'LOSS%':<6} | {'NET PNL':<10}")
    print("-" * 85)
    for r in worst_loss_rate:
        print(f"{r['symbol']:<10} | {r['condition_type']:<30} | {r['direction']:<5} | {r['total_trades']:<6} | {r['win_rate']:<6} | {r['loss_rate']:<6} | {r['net_pnl']:<10.2f}")

if __name__ == "__main__":
    main()
