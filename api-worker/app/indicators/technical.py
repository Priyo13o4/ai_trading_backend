"""
Technical Indicators Module
Implements technical indicators using pandas_ta (matching MT5 script logic)
"""

import pandas as pd
import numpy as np
import pandas_ta as ta
from typing import Optional, Dict
import logging

logger = logging.getLogger(__name__)


def _calculate_percentile(current_value: float, historical_series: pd.Series) -> Optional[float]:
    """Calculate percentile rank of current value against historical values (NaN-safe)."""
    if historical_series is None or historical_series.empty or pd.isna(current_value):
        return None

    clean_historical = historical_series.dropna()
    if clean_historical.empty:
        return None

    rank = (clean_historical < current_value).sum()
    return round((rank / len(clean_historical)) * 100, 1)


def calculate_emas(df: pd.DataFrame, periods: list) -> Dict[str, Optional[float]]:
    """Calculate EMAs for given periods"""
    emas = {}
    for period in periods:
        try:
            ema_series = ta.ema(df['close'], length=period)
            if ema_series is not None and not ema_series.empty and not pd.isna(ema_series.iloc[-1]):
                emas[f'EMA_{period}'] = round(float(ema_series.iloc[-1]), 5)
            else:
                emas[f'EMA_{period}'] = None
        except Exception as e:
            logger.warning(f"EMA_{period} calculation failed: {e}")
            emas[f'EMA_{period}'] = None
    return emas


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Calculate RSI"""
    try:
        rsi_series = ta.rsi(df['close'], length=period)
        if rsi_series is not None and not rsi_series.empty and not pd.isna(rsi_series.iloc[-1]):
            return round(float(rsi_series.iloc[-1]), 2)
        return None
    except Exception as e:
        logger.warning(f"RSI calculation failed: {e}")
        return None


def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, Optional[float]]:
    """Calculate MACD"""
    try:
        macd_data = ta.macd(df['close'], fast=fast, slow=slow, signal=signal)
        if macd_data is not None and not macd_data.empty:
            macd_main_col = next((c for c in macd_data.columns if c.startswith('MACD_')), None)
            macd_signal_col = next((c for c in macd_data.columns if c.startswith('MACDs_')), None)
            macd_hist_col = next((c for c in macd_data.columns if c.startswith('MACDh_')), None)

            macd_main = macd_data[macd_main_col].iloc[-1] if macd_main_col is not None else (macd_data.iloc[-1, 0] if macd_data.shape[1] > 0 else np.nan)
            macd_signal = macd_data[macd_signal_col].iloc[-1] if macd_signal_col is not None else (macd_data.iloc[-1, 1] if macd_data.shape[1] > 1 else np.nan)
            macd_hist = macd_data[macd_hist_col].iloc[-1] if macd_hist_col is not None else (macd_data.iloc[-1, 2] if macd_data.shape[1] > 2 else np.nan)

            return {
                'macd_main': round(float(macd_main), 5) if not pd.isna(macd_main) else None,
                'macd_signal': round(float(macd_signal), 5) if not pd.isna(macd_signal) else None,
                'macd_histogram': round(float(macd_hist), 5) if not pd.isna(macd_hist) else None
            }
        return {'macd_main': None, 'macd_signal': None, 'macd_histogram': None}
    except Exception as e:
        logger.warning(f"MACD calculation failed: {e}")
        return {'macd_main': None, 'macd_signal': None, 'macd_histogram': None}


def calculate_atr(df: pd.DataFrame, period: int = 14, volatility_lookback: int = 50) -> Dict[str, Optional[float]]:
    """Calculate ATR and ATR percentile"""
    try:
        atr_series = ta.atr(df['high'], df['low'], df['close'], length=period)
        if atr_series is not None and not atr_series.empty and not pd.isna(atr_series.iloc[-1]):
            current_atr = float(atr_series.iloc[-1])
            atr_value = round(current_atr, 5)

            # Calculate percentile
            atr_percentile = None
            if len(atr_series) > volatility_lookback:
                historical_atr = atr_series.iloc[-(volatility_lookback + 1):-1]
                atr_percentile = _calculate_percentile(current_atr, historical_atr)

            return {'atr': atr_value, 'atr_percentile': atr_percentile}
        return {'atr': None, 'atr_percentile': None}
    except Exception as e:
        logger.warning(f"ATR calculation failed: {e}")
        return {'atr': None, 'atr_percentile': None}


def calculate_bollinger_bands(
    df: pd.DataFrame, 
    period: int = 20, 
    std: float = 2.0, 
    volatility_lookback: int = 50
) -> Dict[str, Optional[float]]:
    """Calculate Bollinger Bands with squeeze ratio and width percentile"""
    try:
        bb_data = ta.bbands(df['close'], length=period, std=std)
        if bb_data is not None and not bb_data.empty:
            bb_upper_col = next((c for c in bb_data.columns if c.startswith('BBU_')), None)
            bb_middle_col = next((c for c in bb_data.columns if c.startswith('BBM_')), None)
            bb_lower_col = next((c for c in bb_data.columns if c.startswith('BBL_')), None)

            bb_upper_raw = bb_data[bb_upper_col].iloc[-1] if bb_upper_col is not None else (bb_data.iloc[-1, 2] if bb_data.shape[1] > 2 else np.nan)
            bb_middle_raw = bb_data[bb_middle_col].iloc[-1] if bb_middle_col is not None else (bb_data.iloc[-1, 1] if bb_data.shape[1] > 1 else np.nan)
            bb_lower_raw = bb_data[bb_lower_col].iloc[-1] if bb_lower_col is not None else (bb_data.iloc[-1, 0] if bb_data.shape[1] > 0 else np.nan)

            bb_upper = round(float(bb_upper_raw), 5) if not pd.isna(bb_upper_raw) else None
            bb_middle = round(float(bb_middle_raw), 5) if not pd.isna(bb_middle_raw) else None
            bb_lower = round(float(bb_lower_raw), 5) if not pd.isna(bb_lower_raw) else None
            
            # Calculate squeeze ratio
            bb_squeeze_ratio = None
            if bb_upper is not None and bb_lower is not None and bb_middle is not None and bb_middle != 0:
                bb_squeeze_ratio = round((bb_upper - bb_lower) / bb_middle, 5)
            
            # Calculate BB width percentile
            bb_width_percentile = None
            bb_upper_series = bb_data[bb_upper_col] if bb_upper_col is not None else (bb_data.iloc[:, 2] if bb_data.shape[1] > 2 else pd.Series(dtype=float))
            bb_middle_series = bb_data[bb_middle_col] if bb_middle_col is not None else (bb_data.iloc[:, 1] if bb_data.shape[1] > 1 else pd.Series(dtype=float))
            bb_lower_series = bb_data[bb_lower_col] if bb_lower_col is not None else (bb_data.iloc[:, 0] if bb_data.shape[1] > 0 else pd.Series(dtype=float))

            bbw_series = (bb_upper_series - bb_lower_series).abs() / bb_middle_series.abs().replace(0, np.nan)
            if not bbw_series.empty and len(bbw_series) > volatility_lookback and not pd.isna(bbw_series.iloc[-1]):
                current_bbw = float(bbw_series.iloc[-1])
                historical_bbw = bbw_series.iloc[-(volatility_lookback + 1):-1]
                bb_width_percentile = _calculate_percentile(current_bbw, historical_bbw)
            
            return {
                'bb_upper': bb_upper,
                'bb_middle': bb_middle,
                'bb_lower': bb_lower,
                'bb_squeeze_ratio': bb_squeeze_ratio,
                'bb_width_percentile': bb_width_percentile
            }
        return {
            'bb_upper': None, 'bb_middle': None, 'bb_lower': None,
            'bb_squeeze_ratio': None, 'bb_width_percentile': None
        }
    except Exception as e:
        logger.warning(f"Bollinger Bands calculation failed: {e}")
        return {
            'bb_upper': None, 'bb_middle': None, 'bb_lower': None,
            'bb_squeeze_ratio': None, 'bb_width_percentile': None
        }


def calculate_roc(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Calculate Rate of Change"""
    try:
        roc_series = ta.roc(df['close'], length=period)
        if roc_series is not None and not roc_series.empty and not pd.isna(roc_series.iloc[-1]):
            return round(float(roc_series.iloc[-1]), 4)
        return None
    except Exception as e:
        logger.warning(f"ROC calculation failed: {e}")
        return None


def calculate_ema_momentum_slope(df: pd.DataFrame, period: int = 21) -> Optional[float]:
    """Calculate EMA momentum slope (linear regression on last 5 EMA values)"""
    try:
        ema_series = ta.ema(df['close'], length=period)
        if ema_series is not None and not ema_series.empty and len(ema_series) > 5 and not pd.isna(ema_series.iloc[-1]):
            y = ema_series.tail(5).values
            x = np.arange(len(y))
            slope, _ = np.polyfit(x, y, 1)
            return round(slope, 5)
        return None
    except Exception as e:
        logger.warning(f"EMA momentum slope calculation failed: {e}")
        return None


def calculate_adx(df: pd.DataFrame, period: int = 14) -> Dict[str, Optional[float]]:
    """Calculate ADX and Directional Movement Indicators"""
    try:
        adx_data = ta.adx(df['high'], df['low'], df['close'], length=period)
        if adx_data is not None and not adx_data.empty:
            return {
                'adx': round(float(adx_data.iloc[-1, 0]), 2) if not pd.isna(adx_data.iloc[-1, 0]) else None,
                'dmp': round(float(adx_data.iloc[-1, 1]), 2) if not pd.isna(adx_data.iloc[-1, 1]) else None,
                'dmn': round(float(adx_data.iloc[-1, 2]), 2) if not pd.isna(adx_data.iloc[-1, 2]) else None
            }
        return {'adx': None, 'dmp': None, 'dmn': None}
    except Exception as e:
        logger.warning(f"ADX calculation failed: {e}")
        return {'adx': None, 'dmp': None, 'dmn': None}


def calculate_obv_slope(df: pd.DataFrame, period: int = 10) -> Optional[float]:
    """Calculate OBV slope (linear regression on last N OBV values)"""
    try:
        obv_series = ta.obv(df['close'], df['volume'])
        if obv_series is not None and not obv_series.empty and len(obv_series) > period:
            y = obv_series.tail(period).values
            x = np.arange(len(y))
            slope, _ = np.polyfit(x, y, 1)
            return round(slope, 2)
        return None
    except Exception as e:
        logger.warning(f"OBV slope calculation failed: {e}")
        return None


def calculate_all_indicators(
    df: pd.DataFrame,
    ema_periods: list,
    rsi_period: int,
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
    atr_period: int,
    bb_period: int,
    bb_deviation: float,
    roc_period: int,
    adx_period: int,
    obv_slope_period: int,
    momentum_ema: int,
    volatility_lookback: int
) -> Dict[str, any]:
    """Calculate all technical indicators (matching MT5 script)"""
    
    if df is None or df.empty:
        logger.warning("Insufficient data for indicators. Empty dataframe provided")
        return {}

    if len(df) < volatility_lookback:
        logger.warning(
            f"Data shorter than volatility lookback ({len(df)} < {volatility_lookback}); "
            "raw indicators will still be computed, percentile fields may be null"
        )
    
    indicators = {}
    
    # EMAs
    indicators['emas'] = calculate_emas(df, ema_periods)
    
    # RSI
    indicators['rsi'] = calculate_rsi(df, rsi_period)
    
    # MACD
    macd_result = calculate_macd(df, macd_fast, macd_slow, macd_signal)
    indicators.update(macd_result)
    
    # ATR
    atr_result = calculate_atr(df, atr_period, volatility_lookback)
    indicators.update(atr_result)
    
    # Bollinger Bands
    bb_result = calculate_bollinger_bands(df, bb_period, bb_deviation, volatility_lookback)
    indicators.update(bb_result)
    
    # ROC
    indicators['roc_percent'] = calculate_roc(df, roc_period)
    
    # EMA Momentum Slope
    indicators['ema_momentum_slope'] = calculate_ema_momentum_slope(df, momentum_ema)
    
    # ADX/DMI
    adx_result = calculate_adx(df, adx_period)
    indicators.update(adx_result)
    
    # OBV Slope
    indicators['obv_slope'] = calculate_obv_slope(df, obv_slope_period)
    
    return indicators
