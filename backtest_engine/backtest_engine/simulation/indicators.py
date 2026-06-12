import pandas as pd
import numpy as np

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range (ATR)."""
    if len(df) < period:
        return pd.Series(index=df.index, dtype=float)
        
    high = df['high']
    low = df['low']
    close_prev = df['close'].shift(1)
    
    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Simple Moving Average for ATR, per standard MT5 implementation (iATR)
    atr = tr.rolling(window=period).mean()
    return atr

def is_bearish_engulfing(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift + 1:
        return False
    # shift is 1-indexed for previous candle (0 is current forming)
    c1 = df.iloc[-(shift+1)]
    c2 = df.iloc[-(shift+2)] # the candle before c1
    
    open1, close1 = c1['open'], c1['close']
    open2, close2 = c2['open'], c2['close']
    
    # c2 is the previous candle, c1 is the candle before it. Wait, in MT5 array indexing:
    # buffer[0] is the current candle. buffer[1] is shift 1.
    # In MT5 IsBearishEngulfing, shift=1 means open[0] is index 1, open[1] is index 2.
    # So open1 = index shift, open2 = index shift+1
    idx1 = -(shift + 1)
    idx2 = -(shift + 2)
    
    open1, close1 = df.iloc[idx1]['open'], df.iloc[idx1]['close']
    open2, close2 = df.iloc[idx2]['open'], df.iloc[idx2]['close']
    
    # IsBearishEngulfing: close2 > open2 (bullish), close1 < open1 (bearish), open1 > close2, close1 < open2
    return (close2 > open2) and (close1 < open1) and (open1 > close2) and (close1 < open2)

def is_bullish_engulfing(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift + 1:
        return False
    idx1 = -(shift + 1)
    idx2 = -(shift + 2)
    
    open1, close1 = df.iloc[idx1]['open'], df.iloc[idx1]['close']
    open2, close2 = df.iloc[idx2]['open'], df.iloc[idx2]['close']
    
    # IsBullishEngulfing: close2 < open2 (bearish), close1 > open1 (bullish), open1 < close2, close1 > open2
    return (close2 < open2) and (close1 > open1) and (open1 < close2) and (close1 > open2)

def is_doji(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift:
        return False
    c = df.iloc[-(shift + 1)]
    body = abs(c['close'] - c['open'])
    range_ = c['high'] - c['low']
    return (range_ > 0) and (body / range_ < 0.1)

def is_hammer(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift:
        return False
    c = df.iloc[-(shift + 1)]
    body = abs(c['close'] - c['open'])
    upper_shadow = c['high'] - max(c['open'], c['close'])
    lower_shadow = min(c['open'], c['close']) - c['low']
    return (body > 0) and (lower_shadow > body * 2) and (upper_shadow < body * 0.5)

def is_shooting_star(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift:
        return False
    c = df.iloc[-(shift + 1)]
    body = abs(c['close'] - c['open'])
    upper_shadow = c['high'] - max(c['open'], c['close'])
    lower_shadow = min(c['open'], c['close']) - c['low']
    return (body > 0) and (upper_shadow > body * 2) and (lower_shadow < body * 0.5)

def is_pin_bar(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift:
        return False
    c = df.iloc[-(shift + 1)]
    body = abs(c['close'] - c['open'])
    range_ = c['high'] - c['low']
    upper_shadow = c['high'] - max(c['open'], c['close'])
    lower_shadow = min(c['open'], c['close']) - c['low']
    
    if range_ <= 0: return False
    return (body / range_ < 0.33) and ((upper_shadow > body * 3) or (lower_shadow > body * 3))

def is_bearish_pin_bar(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift:
        return False
    c = df.iloc[-(shift + 1)]
    body = abs(c['close'] - c['open'])
    range_ = c['high'] - c['low']
    upper_shadow = c['high'] - max(c['open'], c['close'])
    
    if range_ <= 0: return False
    return (body / range_ < 0.33) and (upper_shadow > body * 3)

def is_bullish_pin_bar(df: pd.DataFrame, shift: int) -> bool:
    if len(df) <= shift:
        return False
    c = df.iloc[-(shift + 1)]
    body = abs(c['close'] - c['open'])
    range_ = c['high'] - c['low']
    lower_shadow = min(c['open'], c['close']) - c['low']
    
    if range_ <= 0: return False
    return (body / range_ < 0.33) and (lower_shadow > body * 3)

def is_liquidity_sweep(df: pd.DataFrame, shift: int, direction: str, level: float) -> bool:
    if len(df) <= shift:
        return False
    c = df.iloc[-(shift + 1)]
    if direction == "long":
        return (c['low'] <= level) and (c['close'] > level)
    else:
        return (c['high'] >= level) and (c['close'] < level)
