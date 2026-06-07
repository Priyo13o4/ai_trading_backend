import csv
from collections import defaultdict
from pathlib import Path
from datetime import datetime

def get_session(hour):
    if 23 <= hour or hour < 8:
        return 'Asian (23-08)'
    elif 8 <= hour < 12:
        return 'London (08-12)'
    elif 12 <= hour < 16:
        return 'Overlap (12-16)'
    elif 16 <= hour < 20:
        return 'NY Aft (16-20)'
    else:
        return 'DeadZone (20-23)'

def main():
    raw_results_path = Path("backtest_engine/reports/sweeps/sweep-20260602165549/be_10pct_atr_0.5/raw/results.csv")
    
    if not raw_results_path.exists():
        print(f"File not found: {raw_results_path}")
        return

    # stats[session][symbol][strategy][direction] = ...
    stats = defaultdict(lambda: {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl': 0.0})
    
    with open(raw_results_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            outcome = row.get('outcome', '')
            if outcome not in ['closed_tp', 'closed_sl', 'closed_trailing_sl']:
                continue
                
            entry_time_str = row.get('entry_time', '')
            if not entry_time_str:
                continue
                
            # Parse ISO 8601 time string e.g., "2026-05-12T01:16:00+00:00"
            try:
                # slice up to the +00:00
                dt_str = entry_time_str.split('+')[0]
                dt = datetime.fromisoformat(dt_str)
                session = get_session(dt.hour)
            except Exception as e:
                print(f"Error parsing date {entry_time_str}: {e}")
                continue

            sym = row.get('symbol', 'UNKNOWN')
            ctype = row.get('condition_type', 'UNKNOWN')
            direction = row.get('direction', 'UNKNOWN')
            
            try:
                pnl = float(row.get('net_pnl', 0.0))
            except ValueError:
                pnl = 0.0
                
            key = (session, sym, ctype, direction)
            stats[key]['total_trades'] += 1
            stats[key]['net_pnl'] += pnl
            
            if outcome == 'closed_tp':
                stats[key]['wins'] += 1
            elif outcome == 'closed_sl':
                stats[key]['losses'] += 1
                
    results = []
    for (session, sym, ctype, direction), s in stats.items():
        total = s['total_trades']
        if total < 3: # Ignore completely statistically insignificant data
            continue
            
        win_rate = (s['wins'] / total * 100) if total > 0 else 0
        loss_rate = (s['losses'] / total * 100) if total > 0 else 0
        
        results.append({
            'session': session,
            'symbol': sym,
            'condition_type': ctype,
            'direction': direction,
            'total_trades': total,
            'net_pnl': s['net_pnl'],
            'win_rate': round(win_rate, 1),
            'loss_rate': round(loss_rate, 1)
        })
        
    worst_pnl = sorted(results, key=lambda x: x['net_pnl'])[:15]
    best_pnl = sorted(results, key=lambda x: -x['net_pnl'])[:15]
    
    print("=== TOP 15 WORST PERFORMING BY SESSION (Min 3 Trades) ===")
    print(f"{'SESSION':<16} | {'SYMBOL':<8} | {'STRATEGY':<20} | {'DIR':<5} | {'TRADES':<6} | {'WIN%':<5} | {'LOSS%':<5} | {'NET PNL':<10}")
    print("-" * 95)
    for r in worst_pnl:
        print(f"{r['session']:<16} | {r['symbol']:<8} | {r['condition_type']:<20} | {r['direction']:<5} | {r['total_trades']:<6} | {r['win_rate']:<5} | {r['loss_rate']:<5} | {r['net_pnl']:<10.2f}")
        
    print("\n=== TOP 15 BEST PERFORMING BY SESSION (Min 3 Trades) ===")
    print(f"{'SESSION':<16} | {'SYMBOL':<8} | {'STRATEGY':<20} | {'DIR':<5} | {'TRADES':<6} | {'WIN%':<5} | {'LOSS%':<5} | {'NET PNL':<10}")
    print("-" * 95)
    for r in best_pnl:
        print(f"{r['session']:<16} | {r['symbol']:<8} | {r['condition_type']:<20} | {r['direction']:<5} | {r['total_trades']:<6} | {r['win_rate']:<5} | {r['loss_rate']:<5} | {r['net_pnl']:<10.2f}")
        
    # Analyze pure sessions (aggregation)
    session_stats = defaultdict(lambda: {'total':0, 'pnl':0.0, 'wins':0, 'losses':0})
    for (session, sym, ctype, direction), s in stats.items():
        session_stats[session]['total'] += s['total_trades']
        session_stats[session]['pnl'] += s['net_pnl']
        session_stats[session]['wins'] += s['wins']
        session_stats[session]['losses'] += s['losses']
        
    print("\n=== OVERALL PERFORMANCE BY SESSION ===")
    print(f"{'SESSION':<16} | {'TRADES':<6} | {'WIN%':<5} | {'LOSS%':<5} | {'NET PNL':<10}")
    print("-" * 60)
    for session, s in sorted(session_stats.items(), key=lambda x: -x[1]['pnl']):
        total = s['total']
        win_rate = (s['wins'] / total * 100) if total > 0 else 0
        loss_rate = (s['losses'] / total * 100) if total > 0 else 0
        print(f"{session:<16} | {total:<6} | {win_rate:<5.1f} | {loss_rate:<5.1f} | {s['pnl']:<10.2f}")

if __name__ == "__main__":
    main()
