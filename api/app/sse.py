"""
Server-Sent Events (SSE) endpoints for real-time updates
Handles streaming of candles, news, and strategies
"""

import asyncio
import json
import logging
from typing import AsyncGenerator
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from .cache import redis_client, PubSubManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


async def event_generator(channel: str, request: Request) -> AsyncGenerator[str, None]:
    """
    Generate SSE events from Redis pub/sub channel
    
    Args:
        channel: Redis channel to subscribe to
        request: FastAPI request object (for disconnect detection)
    
    Yields:
        SSE formatted messages
    """
    pubsub = redis_client.pubsub()
    
    try:
        # Subscribe to channel
        pubsub.subscribe(channel)
        logger.info(f"Client subscribed to {channel}")
        
        # Send initial connection message
        yield f"data: {json.dumps({'type': 'connected', 'channel': channel})}\n\n"
        
        # Listen for messages
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                logger.info(f"Client disconnected from {channel}")
                break
            
            # Get message with timeout
            message = pubsub.get_message(timeout=1.0)
            
            if message and message['type'] == 'message':
                # Forward the message to client
                data = message['data']
                yield f"data: {data}\n\n"
            
            # Small sleep to prevent CPU spinning
            await asyncio.sleep(0.1)
    
    except asyncio.CancelledError:
        logger.info(f"Client stream cancelled for {channel}")
    except Exception as e:
        logger.error(f"Error in event generator for {channel}: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    finally:
        # Cleanup
        pubsub.unsubscribe(channel)
        pubsub.close()
        logger.info(f"Client unsubscribed from {channel}")


@router.get("/candles/{symbol}/{timeframe}")
async def stream_candles(symbol: str, timeframe: str, request: Request):
    """
    Stream real-time candle updates for a specific symbol and timeframe
    
    Example:
        GET /api/stream/candles/XAUUSD/M5
        
    Returns SSE stream with format:
        data: {"type":"candle_update","symbol":"XAUUSD","timeframe":"M5","candle":{...}}
    """
    # Validate inputs
    symbol = symbol.upper()
    timeframe = timeframe.upper()
    
    logger.info(f"Starting candle stream for {symbol} {timeframe}")
    
    # Create filtered event generator
    async def filtered_generator():
        async for event in event_generator(PubSubManager.CHANNELS['candles'], request):
            # Filter by symbol and timeframe
            if 'data: ' in event and event.strip() != 'data:':
                try:
                    data = json.loads(event.replace('data: ', '').strip())
                    # Only send if matches requested symbol/timeframe
                    if (data.get('type') == 'connected' or 
                        (data.get('symbol') == symbol and data.get('timeframe') == timeframe)):
                        yield event
                except json.JSONDecodeError:
                    yield event
            else:
                yield event
    
    return StreamingResponse(
        filtered_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )


@router.get("/candles")
async def stream_all_candles(request: Request):
    """
    Stream real-time candle updates for ALL symbols and timeframes
    
    Example:
        GET /api/stream/candles
        
    Returns SSE stream with all candle updates
    """
    logger.info("Starting candle stream for all pairs")
    
    return StreamingResponse(
        event_generator(PubSubManager.CHANNELS['candles'], request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/news")
async def stream_news(request: Request):
    """
    Stream real-time news updates
    
    Example:
        GET /api/stream/news
        
    Returns SSE stream with format:
        data: {"type":"news_update","news":{...}}
    """
    logger.info("Starting news stream")
    
    return StreamingResponse(
        event_generator(PubSubManager.CHANNELS['news'], request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/strategies")
async def stream_strategies(request: Request):
    """
    Stream real-time strategy updates
    
    Example:
        GET /api/stream/strategies
        
    Returns SSE stream with format:
        data: {"type":"strategy_update","strategy":{...}}
    """
    logger.info("Starting strategies stream")
    
    return StreamingResponse(
        event_generator(PubSubManager.CHANNELS['strategies'], request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/health")
async def stream_health():
    """Health check for SSE endpoints"""
    try:
        redis_client.ping()
        return {
            "status": "healthy",
            "redis": "connected",
            "channels": list(PubSubManager.CHANNELS.values())
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "redis": "disconnected",
            "error": str(e)
        }
