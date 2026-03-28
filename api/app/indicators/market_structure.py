"""
Market Structure Analysis Module
Implements swing analysis, pivot points, and volume profile (matching MT5 script logic)
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


def calculate_pivot_points(high: float, low: float, close: float, open_price: float) -> Dict[str, Dict[str, float]]:
    """
    Calculate all pivot points (Classic, Woodie, Camarilla)
    Based on previous bar's OHLC
    """
    pivots = {}
    
    # Classic Pivot Points
    P = (high + low + close) / 3
    R1 = (2 * P) - low
    S1 = (2 * P) - high
    R2 = P + (high - low)
    S2 = P - (high - low)
    R3 = P + 2 * (high - low)
    S3 = P - 2 * (high - low)
    pivots['classic'] = {
        "R3": round(R3, 4),
        "R2": round(R2, 4),
        "R1": round(R1, 4),
        "P": round(P, 4),
        "S1": round(S1, 4),
        "S2": round(S2, 4),
        "S3": round(S3, 4)
    }
    
    # Woodie Pivot Points
    P_w = (high + low + 2 * close) / 4
    R1_w = (2 * P_w) - low
    S1_w = (2 * P_w) - high
    R2_w = P_w + (high - low)
    S2_w = P_w - (high - low)
    pivots['woodie'] = {
        "R2": round(R2_w, 4),
        "R1": round(R1_w, 4),
        "P": round(P_w, 4),
        "S1": round(S1_w, 4),
        "S2": round(S2_w, 4)
    }
    
    # Camarilla Pivot Points
    Range = high - low
    H4 = close + (Range * 1.1 / 2)
    H3 = close + (Range * 1.1 / 4)
    H2 = close + (Range * 1.1 / 6)
    H1 = close + (Range * 1.1 / 12)
    L1 = close - (Range * 1.1 / 12)
    L2 = close - (Range * 1.1 / 6)
    L3 = close - (Range * 1.1 / 4)
    L4 = close - (Range * 1.1 / 2)
    pivots['camarilla'] = {
        "H4": round(H4, 4),
        "H3": round(H3, 4),
        "H2": round(H2, 4),
        "H1": round(H1, 4),
        "L1": round(L1, 4),
        "L2": round(L2, 4),
        "L3": round(L3, 4),
        "L4": round(L4, 4)
    }
    
    return pivots


def calculate_volume_profile(df: pd.DataFrame) -> Dict[str, float]:
    """
    Calculate volume profile:
    - Point of Control (POC): Price level with highest volume
    - Value Area High (VAH): Upper boundary of 70% volume
    - Value Area Low (VAL): Lower boundary of 70% volume
    """
    if df is None or df.empty:
        return {}
    
    try:
        # Group prices into bins and sum volume
        price_volume = df.groupby(pd.cut(df['close'], bins=100))['volume'].sum()
        
        # Point of Control: price level with max volume
        poc = price_volume.idxmax().mid if not price_volume.empty else 0.0
        
        # Value Area: 70% of total volume
        total_volume = price_volume.sum()
        if total_volume == 0:
            return {
                "point_of_control": round(poc, 4),
                "value_area_high": 0.0,
                "value_area_low": 0.0
            }
        
        sorted_volume = price_volume.sort_values(ascending=False)
        cumulative_volume = sorted_volume.cumsum()
        value_area_series = sorted_volume[cumulative_volume <= total_volume * 0.7]
        
        vah = value_area_series.index.max().right if not value_area_series.empty else 0.0
        val = value_area_series.index.min().left if not value_area_series.empty else 0.0
        
        return {
            "point_of_control": round(poc, 4),
            "value_area_high": round(vah, 4),
            "value_area_low": round(val, 4)
        }
    except Exception as e:
        logger.warning(f"Volume profile calculation failed: {e}")
        return {}


def analyze_swing_structure(df: pd.DataFrame, lookback: int) -> Dict[str, int]:
    """
    Analyze swing highs and lows to determine market structure:
    - Total swing highs/lows
    - Higher highs/lower highs
    - Higher lows/lower lows
    """
    try:
        analysis_df = df.tail(lookback).copy()
        
        # Identify swing highs: high > previous high AND high > next high
        analysis_df['is_swing_high'] = (
            (analysis_df['high'] > analysis_df['high'].shift(1)) & 
            (analysis_df['high'] > analysis_df['high'].shift(-1))
        )
        
        # Identify swing lows: low < previous low AND low < next low
        analysis_df['is_swing_low'] = (
            (analysis_df['low'] < analysis_df['low'].shift(1)) & 
            (analysis_df['low'] < analysis_df['low'].shift(-1))
        )
        
        swing_highs = analysis_df[analysis_df['is_swing_high']]['high']
        swing_lows = analysis_df[analysis_df['is_swing_low']]['low']
        
        structure = {
            "total_swing_highs": len(swing_highs),
            "total_swing_lows": len(swing_lows),
            "higher_highs": int((swing_highs.diff() > 0).sum()),
            "lower_highs": int((swing_highs.diff() < 0).sum()),
            "higher_lows": int((swing_lows.diff() > 0).sum()),
            "lower_lows": int((swing_lows.diff() < 0).sum())
        }
        
        return structure
    except Exception as e:
        logger.warning(f"Swing structure analysis failed: {e}")
        return {
            "total_swing_highs": 0,
            "total_swing_lows": 0,
            "higher_highs": 0,
            "lower_highs": 0,
            "higher_lows": 0,
            "lower_lows": 0
        }


def analyze_market_structure(
    df: pd.DataFrame,
    lookback: int
) -> Dict[str, any]:
    """
    Complete market structure analysis
    """
    if df is None or len(df) < lookback:
        return {}
    
    try:
        analysis_df = df.tail(lookback).copy()
        
        # Recent high/low and range
        recent_high = float(analysis_df['high'].max())
        recent_low = float(analysis_df['low'].min())
        range_percent = round(((recent_high - recent_low) / recent_low) * 100, 2) if recent_low > 0 else 0.0
        
        # Swing analysis
        swing_analysis = analyze_swing_structure(df, lookback)
        
        # Pivot points (from previous bar)
        pivot_data = {}
        if len(df) > 1:
            prev_bar = df.iloc[-2]
            pivot_data = calculate_pivot_points(
                high=prev_bar['high'],
                low=prev_bar['low'],
                close=prev_bar['close'],
                open_price=prev_bar['open']
            )
        
        # Volume profile
        volume_profile_data = calculate_volume_profile(df)
        
        return {
            "recent_high": round(recent_high, 5),
            "recent_low": round(recent_low, 5),
            "range_percent": range_percent,
            "swing_analysis": swing_analysis,
            "price_level_analysis": {
                "pivot_points": pivot_data,
                "volume_profile": volume_profile_data
            }
        }
    except Exception as e:
        logger.error(f"Market structure analysis failed: {e}", exc_info=True)
        return {}
