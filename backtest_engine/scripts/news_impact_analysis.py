import asyncio
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path so we can import from backtest_engine and common
sys.path.append(str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text
from backtest_engine.source_db import get_source_db

def is_trading_time(dt: datetime) -> bool:
    day = dt.weekday() # Monday=0, Sunday=6
    hour = dt.hour
    
    # Daily break: 9 PM to 10 PM UTC on weekdays (Monday-Friday)
    if 0 <= day <= 4 and 21 <= hour < 22:
        return False
        
    # Weekend closure: Friday 10 PM UTC to Sunday 10 PM UTC
    if (day == 4 and hour >= 22) or day == 5 or (day == 6 and hour < 22):
        return False
        
    return True

def get_pip_multiplier(symbol: str) -> float:
    sym = symbol.upper()
    if "JPY" in sym: return 100.0
    if "XAUUSD" in sym or "GOLD" in sym: return 10.0
    if "XAG" in sym: return 100.0
    if "BTC" in sym or "ETH" in sym: return 1.0
    if "US30" in sym or "SPX" in sym or "NAS100" in sym or "GER30" in sym or "EUSTX50" in sym or "UK100" in sym: return 1.0
    return 10000.0

def get_pip_threshold(symbol: str) -> float:
    sym = symbol.upper()
    if "XAUUSD" in sym or "GOLD" in sym: return 20.0
    if "BTC" in sym or "ETH" in sym: return 50.0
    if "US30" in sym or "SPX" in sym or "NAS100" in sym: return 20.0
    # Standard forex
    return 15.0

async def analyze_news_set(session, start_date: datetime, end_date: datetime, label: str):
    print(f"\n=== Analyzing {label} ===")
    print(f"Date Range: {start_date} to {end_date}")
    
    # Query email_news_analysis
    query = text("""
        SELECT email_id, headline, primary_instrument, forex_instruments, market_impact_prediction,
               attention_score, analysis_confidence, importance_score, email_received_at, created_at, human_takeaway
        FROM email_news_analysis
        WHERE forex_relevant = true
          AND (pricing_state IS NULL OR pricing_state != 'priced_in')
          AND attention_score >= 60
          AND analysis_confidence >= 0.50
          AND importance_score >= 4
          AND COALESCE(email_received_at, created_at) >= :start_date
          AND COALESCE(email_received_at, created_at) <= :end_date
        ORDER BY COALESCE(email_received_at, created_at) ASC
    """)
    res = await session.execute(query, {"start_date": start_date, "end_date": end_date})
    events = res.fetchall()
    
    valid_events = []
    for event in events:
        dt = event.email_received_at or event.created_at
        if dt is None: continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if is_trading_time(dt):
            valid_events.append({
                "email_id": event.email_id,
                "headline": event.headline,
                "primary_instrument": event.primary_instrument,
                "market_impact_prediction": event.market_impact_prediction,
                "attention_score": event.attention_score,
                "analysis_confidence": event.analysis_confidence,
                "importance_score": event.importance_score,
                "time": dt,
                "human_takeaway": event.human_takeaway
            })
            
    print(f"Found {len(valid_events)} valid news events after Trading Hours filter.")
    
    results = []
    
    for idx, event in enumerate(valid_events):
        print(f"[{idx+1}/{len(valid_events)}] {event['primary_instrument']} - {event['market_impact_prediction']} @ {event['time']}")
        
        window_start = event['time'] - timedelta(minutes=10)
        window_end = event['time'] + timedelta(minutes=10)
        
        # We query the hypertable directly for M1 raw data to ensure highest precision
        candles_query = text("""
            SELECT symbol, time, open, high, low, close 
            FROM candlesticks
            WHERE time BETWEEN :start AND :end
            ORDER BY time ASC
        """)
        c_res = await session.execute(candles_query, {"start": window_start, "end": window_end})
        candles = c_res.fetchall()
        
        symbol_data = {}
        for c in candles:
            if c.symbol not in symbol_data:
                symbol_data[c.symbol] = []
            symbol_data[c.symbol].append(c)
            
        all_symbol_impacts = {}
        primary_accurate = False
        
        for sym, sym_candles in symbol_data.items():
            pre_news_candles = [c for c in sym_candles if c.time <= event['time']]
            post_news_candles = [c for c in sym_candles if c.time > event['time']]
            
            if not pre_news_candles or not post_news_candles:
                continue
                
            pre_news_close = pre_news_candles[-1].close
            post_high = max(c.high for c in post_news_candles)
            post_low = min(c.low for c in post_news_candles)
            
            pip_mult = get_pip_multiplier(sym)
            pip_thresh = get_pip_threshold(sym)
            
            max_move_up = (post_high - pre_news_close) * pip_mult
            max_move_down = (pre_news_close - post_low) * pip_mult
            
            direction = "neutral"
            move_pips = 0.0
            
            if max_move_up >= pip_thresh or max_move_down >= pip_thresh:
                if max_move_up > max_move_down:
                    direction = "bullish"
                    move_pips = max_move_up
                elif max_move_down > max_move_up:
                    direction = "bearish"
                    move_pips = -max_move_down
                else:
                    direction = "mixed"
                    move_pips = max(max_move_up, max_move_down)
            else:
                direction = "neutral"
                move_pips = max_move_up if max_move_up > max_move_down else -max_move_down
                
            all_symbol_impacts[sym] = {
                "pre_close": pre_news_close,
                "post_high": post_high,
                "post_low": post_low,
                "max_move_up": max_move_up,
                "max_move_down": max_move_down,
                "direction": direction,
                "move_pips": move_pips
            }
            
            if sym == event['primary_instrument']:
                pred = str(event['market_impact_prediction']).lower()
                if pred == direction:
                    primary_accurate = True
                    
        if event['primary_instrument'] in all_symbol_impacts:
            prim_data = all_symbol_impacts[event['primary_instrument']]
        else:
            prim_data = {"direction": "unknown", "move_pips": 0.0, "max_move_up": 0.0, "max_move_down": 0.0}
            
        results.append({
            "email_id": event['email_id'],
            "time": event['time'].isoformat(),
            "headline": event['headline'],
            "primary_instrument": event['primary_instrument'],
            "predicted_impact": event['market_impact_prediction'],
            "actual_impact_direction": prim_data["direction"],
            "max_move_up_pips": prim_data["max_move_up"],
            "max_move_down_pips": prim_data["max_move_down"],
            "prediction_accurate": primary_accurate,
            "attention_score": event['attention_score'],
            "confidence": event['analysis_confidence'],
            "all_symbol_impacts": all_symbol_impacts
        })
        
    return results

async def main():
    db_gen = get_source_db()
    session = await anext(db_gen)
    
    # Auto-detect year from latest record
    latest_query = text("SELECT MAX(COALESCE(email_received_at, created_at)) FROM email_news_analysis")
    latest_res = await session.execute(latest_query)
    latest_dt = latest_res.scalar()
    
    if not latest_dt:
        print("No records found in email_news_analysis.")
        await session.close()
        return
        
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
        
    # We use the year of the latest record
    year = latest_dt.year
    
    set1_start = datetime(year, 6, 8, tzinfo=timezone.utc)
    set1_end = datetime(year, 6, 11, 18, 0, 0, tzinfo=timezone.utc)
    
    set2_start = datetime(year, 6, 11, 18, 0, 0, tzinfo=timezone.utc)
    set2_end = latest_dt
    
    # Run analysis
    res1 = await analyze_news_set(session, set1_start, set1_end, "Set 1 (Jun 8 - Jun 11 18:00 UTC)")
    res2 = await analyze_news_set(session, set2_start, set2_end, "Set 2 (Jun 11 18:00 UTC - Latest)")
    
    await session.close()
    
    # Write reports
    out_dir = Path(__file__).parent.parent / "reports" / "news_impact"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    def write_csv(results, filepath):
        if not results:
            print(f"No results for {filepath.name}, skipping.")
            return
        fieldnames = ["email_id", "time", "headline", "primary_instrument", "predicted_impact", 
                      "actual_impact_direction", "max_move_up_pips", "max_move_down_pips", 
                      "prediction_accurate", "attention_score", "confidence", "other_affected_symbols"]
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                row = dict(r)
                other = []
                for sym, data in r["all_symbol_impacts"].items():
                    if sym != r["primary_instrument"] and data["direction"] not in ("neutral", "unknown"):
                        other.append(f"{sym}({data['direction']}:{data['move_pips']:.1f})")
                row["other_affected_symbols"] = " | ".join(other)
                del row["all_symbol_impacts"]
                writer.writerow(row)
                
    write_csv(res1, out_dir / "set1_results.csv")
    write_csv(res2, out_dir / "set2_results.csv")
    
    # Write Markdown
    md = "# News Impact Analysis Report\n\n"
    for label, res in [("Set 1 (Jun 8 - Jun 11 18:00 UTC)", res1), ("Set 2 (Jun 11 18:00 UTC - Latest)", res2)]:
        total = len(res)
        accurate = sum(1 for r in res if r["prediction_accurate"])
        acc_rate = (accurate / total * 100) if total > 0 else 0
        md += f"## {label}\n"
        md += f"- **Total Filtered Events**: {total}\n"
        md += f"- **Accurate Predictions**: {accurate}\n"
        md += f"- **Accuracy Rate**: {acc_rate:.2f}%\n\n"
        
    with open(out_dir / "summary.md", "w") as f:
        f.write(md)
        
    print(f"\nReports written to {out_dir}")

if __name__ == "__main__":
    asyncio.run(main())
