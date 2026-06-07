from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, asc
import logging
from datetime import datetime
from typing import List
from trading_common.models import Strategy

logger = logging.getLogger(__name__)

async def load_strategies(
    session: AsyncSession, 
    limit: int | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    strategy_id: int | None = None,
    universe: str = "expired",
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> List[Strategy]:
    """
    Load strategies ordered by timestamp.
    Only loads expired strategies to simulate after the fact.
    """
    stmt = select(Strategy)

    if universe == "expired":
        stmt = stmt.where(Strategy.status == "expired")
    elif universe == "all":
        pass
    else:
        raise ValueError(f"Unsupported strategy universe: {universe}")
    
    if symbol:
        stmt = stmt.where(Strategy.symbol == symbol)
        
    if timeframe:
        stmt = stmt.where(Strategy.entry_signal['timeframe'].astext == timeframe)
        
    if strategy_id is not None:
        stmt = stmt.where(Strategy.strategy_id == strategy_id)

    if from_date is not None:
        stmt = stmt.where(Strategy.timestamp >= from_date)

    if to_date is not None:
        stmt = stmt.where(Strategy.timestamp <= to_date)
        
    # Process oldest first to simulate correctly in time order
    stmt = stmt.order_by(asc(Strategy.timestamp), asc(Strategy.strategy_id))
    
    if limit is not None:
        stmt = stmt.limit(limit)
        
    result = await session.execute(stmt)
    strategies = result.scalars().all()
    
    logger.info(f"Loaded {len(strategies)} strategies for backtesting.")
    return list(strategies)
