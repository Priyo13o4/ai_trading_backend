"""
Server-Sent Events (SSE) endpoints for real-time updates
Handles streaming of candles, news, and strategies
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import AsyncGenerator
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from .auth import auth_context
from .cache import NewsCache, StrategyCache, redis_client, PubSubManager, get_last_candle_update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("SSE_HEARTBEAT_INTERVAL_SECONDS", "15"))


def _server_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_pair_from_payload(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None

    value = payload.get("symbol") or payload.get("pair") or payload.get("trading_pair")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _strategy_matches_pair(payload: dict, normalized_pair: str | None) -> bool:
    if not normalized_pair:
        return True

    strategy_pair = _strategy_pair_from_payload(payload)
    return strategy_pair == normalized_pair


async def event_generator(
    channel: str,
    request: Request,
    *,
    initial_payloads: list[dict] | None = None,
    send_connected: bool = True,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncGenerator[str, None]:
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
        if send_connected:
            yield f"data: {json.dumps({'type': 'connected', 'channel': channel})}\n\n"

        if initial_payloads:
            for payload in initial_payloads:
                yield f"data: {json.dumps(payload)}\n\n"
        
        # Listen for messages
        last_heartbeat = time.monotonic()
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

            now = time.monotonic()
            if heartbeat_interval_s > 0 and now - last_heartbeat >= heartbeat_interval_s:
                heartbeat = {"type": "heartbeat", "server_ts": _server_timestamp()}
                yield f"data: {json.dumps(heartbeat)}\n\n"
                last_heartbeat = now
            
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
async def stream_candles(symbol: str, timeframe: str, request: Request, _ctx=Depends(auth_context)):
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

    # Replay the latest cached candle so the UI can render immediately on page load.
    # Prefer forming (ephemeral) if present, otherwise fall back to last closed candle.
    snapshot = get_last_candle_update(symbol, timeframe, prefer_forming=True)
    if snapshot is not None:
        snapshot = dict(snapshot)
        snapshot["is_snapshot"] = True
    
    # Create filtered event generator
    async def filtered_generator():
        async for event in event_generator(
            PubSubManager.CHANNELS['candles'],
            request,
            initial_payloads=[snapshot] if snapshot else None,
        ):
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
async def stream_all_candles(request: Request, _ctx=Depends(auth_context)):
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
async def stream_news(request: Request, _ctx=Depends(auth_context)):
    """
    Stream real-time news updates
    
    Example:
        GET /api/stream/news
        
    Returns SSE stream with format:
        data: {"type":"news_update","news":{...}}
    """
    logger.info("Starting news stream")
    
    snapshot = NewsCache.get("all") or []
    initial_payloads = None
    if snapshot:
        initial_payloads = [
            {"type": "news_snapshot", "news": snapshot, "server_ts": _server_timestamp()}
        ]

    return StreamingResponse(
        event_generator(
            PubSubManager.CHANNELS['news'],
            request,
            initial_payloads=initial_payloads,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/strategies")
async def stream_strategies(request: Request, pair: str | None = None, _ctx=Depends(auth_context)):
    """
    Stream real-time strategy updates
    
    Example:
        GET /api/stream/strategies
        
    Returns SSE stream with format:
        data: {"type":"strategy_update","strategy":{...}}
    """
    logger.info("Starting strategies stream")
    
    normalized_pair = pair.upper() if pair else None
    snapshot = StrategyCache.get(normalized_pair or "all") or []
    if normalized_pair:
        snapshot = [
            item for item in snapshot
            if _strategy_matches_pair(item, normalized_pair)
        ]

    initial_payloads = None
    if snapshot:
        initial_payloads = [
            {
                "type": "strategies_snapshot",
                "strategies": snapshot,
                "server_ts": _server_timestamp(),
            }
        ]

    async def filtered_generator():
        async for event in event_generator(
            PubSubManager.CHANNELS['strategies'],
            request,
            initial_payloads=initial_payloads,
        ):
            if not normalized_pair:
                yield event
                continue

            if 'data: ' not in event or event.strip() == 'data:':
                yield event
                continue

            try:
                data = json.loads(event.replace('data: ', '').strip())
            except json.JSONDecodeError:
                yield event
                continue

            event_type = data.get('type')
            if event_type in {'connected', 'heartbeat', 'error'}:
                yield event
                continue

            if event_type == 'strategy_update':
                strategy_payload = data.get('strategy') if isinstance(data.get('strategy'), dict) else data
                if _strategy_matches_pair(strategy_payload, normalized_pair):
                    yield event
                continue

            if event_type == 'strategies_snapshot':
                strategies_payload = data.get('strategies')
                if isinstance(strategies_payload, list):
                    filtered = [
                        item for item in strategies_payload
                        if _strategy_matches_pair(item, normalized_pair)
                    ]
                    if filtered:
                        data['strategies'] = filtered
                        yield f"data: {json.dumps(data)}\n\n"
                continue

            if _strategy_matches_pair(data, normalized_pair):
                yield event

    return StreamingResponse(
        filtered_generator(),
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
