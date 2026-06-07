import json
import pandas as pd
import numpy as np
import sys

def main():
    file_path = "backtest_engine/reports/sweeps/sweep-20260601104249/atr_on_partial_on_ma_trailing/summaries/advanced_insights/advanced_insights.json"
    
    with open(file_path, "r") as f:
        data = json.load(f)
        
    df = pd.DataFrame(data["mfe"])
    
    # Filter for Forex pairs (length 6, no USD in front unless it's a major, typically fiat pairs)
    # Simple check: length is 6 and does not contain BTC, ETH, XAU, XAG
    forex_mask = df['symbol'].str.len() == 6
    crypto_gold_mask = df['symbol'].str.contains('BTC|ETH|XAU|XAG')
    forex_df = df[forex_mask & ~crypto_gold_mask]
    
    # All rows in mfe are executed trades
    executed_df = forex_df

    
    # Trades that ended up in loss (closed_sl)
    loss_df = executed_df[executed_df['analysis_bucket'] == 'closed_sl']
    
    print(f"Total Forex Trades Executed: {len(executed_df)}")
    print(f"Forex Trades ending in SL: {len(loss_df)}")
    
    if len(loss_df) > 0:
        # Analyze Max Favorable Excursion (MFE) for losing trades
        print("\n--- MFE (Max Favorable Excursion) for Losing Forex Trades ---")
        mfe_pips = loss_df['mfe_pips'].dropna()
        
        print(f"Mean MFE before hitting SL: {mfe_pips.mean():.2f} pips")
        print(f"Median MFE before hitting SL: {mfe_pips.median():.2f} pips")
        print(f"75th percentile MFE: {np.percentile(mfe_pips, 75):.2f} pips")
        print(f"90th percentile MFE: {np.percentile(mfe_pips, 90):.2f} pips")
        
        # How many went into profit at all?
        went_into_profit = (mfe_pips > 0).sum()
        print(f"\nTrades that went into profit (>0 pips) but still hit SL: {went_into_profit} ({went_into_profit/len(mfe_pips)*100:.1f}%)")
        
        # How many went more than 10 pips into profit?
        over_10 = (mfe_pips > 10).sum()
        print(f"Trades that went >10 pips into profit but still hit SL: {over_10} ({over_10/len(mfe_pips)*100:.1f}%)")

        # How many went more than 20 pips into profit?
        over_20 = (mfe_pips > 20).sum()
        print(f"Trades that went >20 pips into profit but still hit SL: {over_20} ({over_20/len(mfe_pips)*100:.1f}%)")
        
        # How close did they get to TP?
        if 'mfe_pct_of_tp_distance' in loss_df.columns:
            closest_tp = loss_df['mfe_pct_of_tp_distance'].dropna() / 100.0
            print("\n--- Distance to TP for Losing Forex Trades ---")
            print(f"Average closest approach to TP: {closest_tp.mean()*100:.1f}%")
            
            over_50_pct = (closest_tp > 0.50).sum()
            print(f"Trades that reached halfway (>50%) to TP before reversing to SL: {over_50_pct} ({over_50_pct/len(closest_tp)*100:.1f}%)")
            
            over_80_pct = (closest_tp > 0.80).sum()
            print(f"Trades that reached >80% to TP before reversing to SL: {over_80_pct} ({over_80_pct/len(closest_tp)*100:.1f}%)")

if __name__ == '__main__':
    main()
