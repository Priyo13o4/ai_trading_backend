import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text
from backtest_engine.source_db import get_source_db

def is_trading_time(dt: datetime) -> bool:
    day = dt.weekday()
    hour = dt.hour
    if 0 <= day <= 4 and 21 <= hour < 22: return False
    if (day == 4 and hour >= 22) or day == 5 or (day == 6 and hour < 22): return False
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
    return 15.0

async def analyze_false_negatives(session, start_date: datetime, end_date: datetime, label: str):
    # Fetch all events with importance < 4 OR is_priced_in = true
    query = text("""
        SELECT email_id, headline, primary_instrument, forex_instruments, market_impact_prediction,
               attention_score, analysis_confidence, importance_score, pricing_state, email_received_at, created_at, human_takeaway
        FROM email_news_analysis
        WHERE forex_relevant = true
          AND (importance_score < 4 OR importance_score IS NULL OR pricing_state = 'priced_in')
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
            valid_events.append(event)
            
    md = f"# {label}\n\n"
    
    false_negatives_found = 0
    
    for idx, event in enumerate(valid_events):
        dt = event.email_received_at or event.created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
            
        window_start = dt - timedelta(minutes=10)
        window_end = dt + timedelta(minutes=10)
        
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
            
        # Determine if ANY asset moved significantly
        moved_assets = []
        
        for sym, sym_candles in symbol_data.items():
            pre_news_candles = [c for c in sym_candles if c.time <= dt]
            post_news_candles = [c for c in sym_candles if c.time > dt]
            
            if not pre_news_candles or not post_news_candles:
                continue
                
            pre_news_close = pre_news_candles[-1].close
            post_high = max(c.high for c in post_news_candles)
            post_low = min(c.low for c in post_news_candles)
            
            pip_mult = get_pip_multiplier(sym)
            pip_thresh = get_pip_threshold(sym)
            
            max_move_up = (post_high - pre_news_close) * pip_mult
            max_move_down = (pre_news_close - post_low) * pip_mult
            
            if max_move_up < 0: max_move_up = 0
            if max_move_down < 0: max_move_down = 0
            
            if max_move_up >= pip_thresh or max_move_down >= pip_thresh:
                if max_move_up > max_move_down:
                    direction = "Bullish"
                else:
                    direction = "Bearish"
                moved_assets.append((sym, direction, max_move_up, max_move_down))
        
        # If at least one asset moved, this is a False Negative
        if moved_assets:
            false_negatives_found += 1
            md += f"### News: {event.headline}\n"
            desc = event.human_takeaway if event.human_takeaway else event.headline
            md += f"**Description:** {desc}\n"
            md += f"**Primary Asset:** {event.primary_instrument}\n"
            md += f"**Predicted Impact:** {event.market_impact_prediction}\n"
            reason = []
            if event.importance_score is not None and event.importance_score < 4:
                reason.append(f"Importance Score was {event.importance_score}")
            if event.pricing_state == 'priced_in':
                reason.append("Marked as Priced In")
            md += f"**Ignored Because:** {' AND '.join(reason)}\n"
            md += f"**Other Scores:** Attention: {event.attention_score} | Confidence: {event.analysis_confidence}\n\n"
            
            md += "| Asset That Moved | Direction | Pips Up | Pips Down |\n"
            md += "|---|---|---|---|\n"
            for m in moved_assets:
                md += f"| {m[0]} | {m[1]} | {m[2]:.1f} | {m[3]:.1f} |\n"
            md += "\n---\n\n"
            
    if false_negatives_found == 0:
        md += "No False Negatives found in this set!\n\n---\n\n"
        
    return md

async def main():
    db_gen = get_source_db()
    session = await anext(db_gen)
    
    latest_query = text("SELECT MAX(COALESCE(email_received_at, created_at)) FROM email_news_analysis")
    latest_res = await session.execute(latest_query)
    latest_dt = latest_res.scalar()
    
    if not latest_dt:
        return
        
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
        
    year = latest_dt.year
    
    set1_start = datetime(year, 6, 8, tzinfo=timezone.utc)
    set1_end = datetime(year, 6, 11, 18, 0, 0, tzinfo=timezone.utc)
    set2_start = datetime(year, 6, 11, 18, 0, 0, tzinfo=timezone.utc)
    set2_end = latest_dt
    
    md_content = "# False Negatives Impact Analysis\n\n"
    md_content += "> **Definition**: Events where the AI's Importance Score was less than 4 (telling us to ignore), but at least one asset breached its volatility threshold.\n\n"
    
    md_content += await analyze_false_negatives(session, set1_start, set1_end, "Set 1 (Jun 8 - Jun 11 18:00 UTC)")
    md_content += await analyze_false_negatives(session, set2_start, set2_end, "Set 2 (Jun 11 18:00 UTC - Latest)")
    
    await session.close()
    
    out_path = Path(__file__).parent.parent / "reports" / "news_impact" / "false_negatives_breakdown.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(out_path, "w") as f:
        f.write(md_content)
        
    print(f"Report successfully written to {out_path}")

if __name__ == "__main__":
    asyncio.run(main())
