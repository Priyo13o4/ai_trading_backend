import logging
from typing import Any
from sqlalchemy import or_, select, text, func, cast, String
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Strategy

logger = logging.getLogger(__name__)


async def expire_elapsed_strategies_batch(db: AsyncSession, batch_size: int = 1000) -> list[dict[str, Any]]:
    """Expire elapsed active strategies in a lock-safe batch and return affected rows."""
    safe_batch_size = max(1, min(int(batch_size or 1000), 5000))

    try:
        result = await db.execute(
            text(
                """
                WITH candidates AS (
                    SELECT strategy_id, expiry_time
                    FROM public.strategies
                    WHERE status = 'active'
                      AND expiry_time <= NOW()
                    ORDER BY expiry_time ASC, strategy_id ASC
                    LIMIT :batch_size
                    FOR UPDATE SKIP LOCKED
                ),
                updated AS (
                    UPDATE public.strategies s
                    SET status = 'expired'
                    FROM candidates c
                    WHERE s.strategy_id = c.strategy_id
                    RETURNING s.strategy_id, c.expiry_time
                )
                SELECT strategy_id, expiry_time
                FROM updated
                ORDER BY expiry_time ASC, strategy_id ASC
                """
            ),
            {"batch_size": safe_batch_size},
        )
        rows = [dict(row) for row in result.mappings().all()]
        await db.commit()
        if rows:
            logger.info("[DB] Expired %s elapsed strategies in janitor batch", len(rows))
        return rows
    except Exception as e:
        logger.error("[DB ERROR] expire_elapsed_strategies_batch: %s", str(e), exc_info=True)
        raise


async def get_active_strategies(db: AsyncSession, pair: str = None):
    """
    Get all active strategies, optionally filtered by pair
    Uses helper function from database
    """
    logger.info(f"[DB] Fetching active strategies{f' for {pair}' if pair else ''}")
    try:
        stmt = (
            select(Strategy)
            .where(Strategy.status == "active", Strategy.expiry_time > func.now())
            .order_by(Strategy.confidence.desc())
        )
        if pair:
            stmt = stmt.where(Strategy.symbol == pair.upper())

        results = [row.to_dict() for row in (await db.execute(stmt)).scalars().all()]
        logger.info(f"[DB] Found {len(results)} active strategies")
        return results
    except Exception as e:
        logger.error(f"[DB ERROR] get_active_strategies: {str(e)}")
        raise


async def get_strategies_all_from_db(
    db: AsyncSession,
    symbol: str = None,
    direction: str = None,
    status: str = None,
    search: str = None,
    limit: int = 20,
    offset: int = 0,
):
    """Get strategies with optional filters and pagination, including total count."""
    logger.info(
        "[DB] Fetching strategies (symbol=%s, direction=%s, status=%s, search=%s, limit=%s, offset=%s)",
        symbol,
        direction,
        status,
        search,
        limit,
        offset,
    )
    try:
        filters = []

        if symbol:
            filters.append(Strategy.symbol == symbol.upper())
        if direction:
            filters.append(func.lower(Strategy.direction) == direction.lower())
        if status:
            normalized_status = status.lower()
            filters.append(func.lower(Strategy.status) == normalized_status)
            if normalized_status == "active":
                filters.append(Strategy.expiry_time > func.now())
        if search:
            search_pattern = f"%{search.strip()}%"
            filters.append(
                or_(
                    Strategy.strategy_name.ilike(search_pattern),
                    Strategy.symbol.ilike(search_pattern),
                    func.coalesce(Strategy.summary, "").ilike(search_pattern),
                    func.coalesce(Strategy.news_context, "").ilike(search_pattern),
                    func.coalesce(cast(Strategy.detailed_analysis, String), "").ilike(search_pattern),
                )
            )

        row_stmt = (
            select(
                Strategy.strategy_id,
                Strategy.strategy_name,
                Strategy.symbol.label("pair"),
                Strategy.direction,
                Strategy.confidence,
                Strategy.take_profit,
                Strategy.stop_loss,
                Strategy.risk_reward_ratio,
                Strategy.expiry_minutes,
                Strategy.expiry_time,
                Strategy.timestamp.label("created_at"),
                Strategy.detailed_analysis,
                Strategy.entry_signal,
                Strategy.status,
            )
            .where(*filters)
            .order_by(Strategy.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        count_stmt = select(func.count()).select_from(Strategy).where(*filters)

        rows = [dict(row) for row in (await db.execute(row_stmt)).mappings().all()]
        total = int((await db.execute(count_stmt)).scalar_one())
        logger.info("[DB] Found %s strategies (total=%s)", len(rows), total)
        return rows, total
    except Exception as e:
        logger.error(f"[DB ERROR] get_strategies_all_from_db: {str(e)}", exc_info=True)
        raise


async def get_strategy_by_id_from_db(db: AsyncSession, strategy_id: int):
    """Get a single strategy by strategy_id."""
    logger.info("[DB] Fetching strategy by id=%s", strategy_id)
    try:
        stmt = (
            select(
                Strategy.strategy_id,
                Strategy.strategy_name,
                Strategy.symbol.label("pair"),
                Strategy.direction,
                Strategy.confidence,
                Strategy.take_profit,
                Strategy.stop_loss,
                Strategy.risk_reward_ratio,
                Strategy.expiry_minutes,
                Strategy.expiry_time,
                Strategy.timestamp.label("created_at"),
                Strategy.detailed_analysis,
                Strategy.entry_signal,
                Strategy.status,
            )
            .where(
                Strategy.strategy_id == strategy_id,
                Strategy.status == "active",
                Strategy.expiry_time > func.now(),
            )
            .limit(1)
        )
        result = (await db.execute(stmt)).mappings().first()
        row = dict(result) if result else None
        if row:
            logger.info("[DB] Found strategy id=%s", strategy_id)
        else:
            logger.info("[DB] No strategy found for id=%s", strategy_id)
        return row
    except Exception as e:
        logger.error(f"[DB ERROR] get_strategy_by_id_from_db({strategy_id}): {str(e)}", exc_info=True)
        raise
