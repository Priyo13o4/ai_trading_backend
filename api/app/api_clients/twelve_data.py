"""
Twelve Data API Client
Handles fetching OHLCV data and technical indicators from Twelve Data API
Supports batch requests for multiple symbols
"""

import aiohttp
import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging
import os

logger = logging.getLogger(__name__)

# Configuration
TWELVE_DATA_BASE_URL = os.getenv("TWELVE_DATA_BASE_URL", "https://api.twelvedata.com")
TWELVE_DATA_TIMEOUT = int(os.getenv("TWELVE_DATA_TIMEOUT", "30"))


class TwelveDataClient:
    """Client for Twelve Data API with batch request support"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = TWELVE_DATA_BASE_URL
        self.timeout = aiohttp.ClientTimeout(total=TWELVE_DATA_TIMEOUT)
        
    async def _make_request(self, endpoint: str, params: Dict[str, Any]) -> Optional[Dict]:
        """Make async request to Twelve Data API"""
        params['apikey'] = self.api_key
        url = f"{self.base_url}/{endpoint}"
        
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'code' in data and data['code'] != 200:
                            logger.error(f"API Error: {data.get('message', 'Unknown error')}")
                            return None
                        return data
                    else:
                        logger.error(f"HTTP {response.status}: {await response.text()}")
                        return None
        except asyncio.TimeoutError:
            logger.error(f"Request timeout for {endpoint}")
            return None
        except Exception as e:
            logger.error(f"Request failed: {e}")
            return None
    
    async def get_time_series_batch(
        self, 
        symbols: List[str], 
        interval: str, 
        outputsize: int = 250
    ) -> Dict[str, Any]:
        """
        Fetch time series data for multiple symbols in a single API call
        
        Args:
            symbols: List of symbols (e.g., ['XAUUSD', 'EURUSD'])
            interval: Timeframe (5min, 15min, 1h, 4h, 1day)
            outputsize: Number of bars to fetch
            
        Returns:
            Dictionary with symbol as key and OHLCV data as value
        """
        # Convert symbols to Twelve Data format (forex pairs use / separator)
        formatted_symbols = []
        for symbol in symbols:
            if any(base in symbol for base in ['XAU', 'XAG', 'EUR', 'GBP', 'USD', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD']):
                # Forex pair: XAUUSD -> XAU/USD
                if symbol.startswith('XAU'):
                    formatted_symbols.append('XAU/USD')
                elif symbol.startswith('XAG'):
                    formatted_symbols.append('XAG/USD')
                else:
                    # EURUSD -> EUR/USD
                    formatted_symbols.append(f"{symbol[:3]}/{symbol[3:]}")
            else:
                formatted_symbols.append(symbol)
        
        # Batch request: comma-separated symbols
        symbol_string = ','.join(formatted_symbols)
        
        params = {
            'symbol': symbol_string,
            'interval': interval,
            'outputsize': outputsize,
            'format': 'JSON'
        }
        
        logger.info(f"Fetching batch time series: {symbol_string} @ {interval}")
        data = await self._make_request('time_series', params)
        
        if not data:
            return {}
        
        # Parse response - Twelve Data returns data per symbol
        result = {}
        
        # If single symbol, data is directly in response
        if len(formatted_symbols) == 1:
            if 'values' in data:
                result[symbols[0]] = data['values']
        else:
            # Multiple symbols: each symbol is a key
            for original_symbol, formatted_symbol in zip(symbols, formatted_symbols):
                if formatted_symbol in data:
                    symbol_data = data[formatted_symbol]
                    if 'values' in symbol_data:
                        result[original_symbol] = symbol_data['values']
                    else:
                        logger.warning(f"No values for {original_symbol}")
                else:
                    logger.warning(f"Symbol {formatted_symbol} not in response")
        
        return result
    
    async def get_quote_batch(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Get real-time quote for multiple symbols
        
        Args:
            symbols: List of symbols
            
        Returns:
            Dictionary with symbol as key and quote data as value
        """
        # Convert symbols to Twelve Data format
        formatted_symbols = []
        for symbol in symbols:
            if any(base in symbol for base in ['XAU', 'XAG', 'EUR', 'GBP', 'USD', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD']):
                if symbol.startswith('XAU'):
                    formatted_symbols.append('XAU/USD')
                elif symbol.startswith('XAG'):
                    formatted_symbols.append('XAG/USD')
                else:
                    formatted_symbols.append(f"{symbol[:3]}/{symbol[3:]}")
            else:
                formatted_symbols.append(symbol)
        
        symbol_string = ','.join(formatted_symbols)
        
        params = {
            'symbol': symbol_string,
            'format': 'JSON'
        }
        
        logger.info(f"Fetching batch quotes: {symbol_string}")
        data = await self._make_request('quote', params)
        
        if not data:
            return {}
        
        result = {}
        
        # Parse response similar to time_series
        if len(formatted_symbols) == 1:
            result[symbols[0]] = data
        else:
            for original_symbol, formatted_symbol in zip(symbols, formatted_symbols):
                if formatted_symbol in data:
                    result[original_symbol] = data[formatted_symbol]
        
        return result
    
    async def get_technical_indicators_batch(
        self,
        symbols: List[str],
        indicator: str,
        interval: str,
        **indicator_params
    ) -> Dict[str, Any]:
        """
        Fetch technical indicators for multiple symbols
        
        Args:
            symbols: List of symbols
            indicator: Indicator name (ema, rsi, macd, bbands, atr, etc.)
            interval: Timeframe
            **indicator_params: Indicator-specific parameters
            
        Returns:
            Dictionary with symbol as key and indicator data as value
        """
        formatted_symbols = []
        for symbol in symbols:
            if any(base in symbol for base in ['XAU', 'XAG', 'EUR', 'GBP', 'USD', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD']):
                if symbol.startswith('XAU'):
                    formatted_symbols.append('XAU/USD')
                elif symbol.startswith('XAG'):
                    formatted_symbols.append('XAG/USD')
                else:
                    formatted_symbols.append(f"{symbol[:3]}/{symbol[3:]}")
            else:
                formatted_symbols.append(symbol)
        
        symbol_string = ','.join(formatted_symbols)
        
        params = {
            'symbol': symbol_string,
            'interval': interval,
            'format': 'JSON',
            **indicator_params
        }
        
        logger.info(f"Fetching {indicator} for {symbol_string} @ {interval}")
        data = await self._make_request(indicator, params)
        
        if not data:
            return {}
        
        result = {}
        
        if len(formatted_symbols) == 1:
            if 'values' in data:
                result[symbols[0]] = data['values']
        else:
            for original_symbol, formatted_symbol in zip(symbols, formatted_symbols):
                if formatted_symbol in data:
                    symbol_data = data[formatted_symbol]
                    if 'values' in symbol_data:
                        result[original_symbol] = symbol_data['values']
        
        return result
    
    async def test_connection(self) -> bool:
        """Test API connection and key validity"""
        try:
            data = await self._make_request('quote', {'symbol': 'EUR/USD'})
            if data and 'symbol' in data:
                logger.info("✅ Twelve Data API connection successful")
                return True
            else:
                logger.error("❌ Twelve Data API connection failed")
                return False
        except Exception as e:
            logger.error(f"❌ Connection test failed: {e}")
            return False
