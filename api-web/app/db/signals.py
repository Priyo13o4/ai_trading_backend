import logging
from sqlalchemy import Numeric, String, cast, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Strategy

logger = logging.getLogger(__name__)


async def get_latest_signal_from_db(db: AsyncSession, pair: str):
    """
    Get latest active strategy for a trading pair
    Returns AI-generated trading recommendation from n8n Strategy Selector
    """
    logger.info(f"[DB] Fetching latest strategy for pair: {pair}")
    try:
        stmt = (
            select(
                Strategy.strategy_id,
                Strategy.strategy_name,
                Strategy.symbol.label("pair"),
                case(
                    (Strategy.direction == "long", "BUY"),
                    (Strategy.direction == "short", "SELL"),
                    else_=func.upper(Strategy.direction),
                ).label("direction"),
                Strategy.confidence,
                Strategy.take_profit,
                Strategy.stop_loss,
                Strategy.risk_reward_ratio,
                Strategy.expiry_minutes,
                Strategy.expiry_time,
                Strategy.timestamp.label("created_at"),
                Strategy.detailed_analysis,
                cast(Strategy.entry_signal["level"].astext, Numeric).label("entry_signal"),
                Strategy.status,
            )
            .where(
                Strategy.symbol == pair.upper(),
                Strategy.status == "active",
                Strategy.expiry_time > func.now(),
            )
            .order_by(Strategy.confidence.desc(), Strategy.timestamp.desc())
            .limit(1)
        )
        result = (await db.execute(stmt)).mappings().first()
        row = dict(result) if result else None
        if row:
            logger.info(f"[DB] Found strategy for {pair}: {row.get('strategy_name')} ({row.get('confidence')})")
        else:
            logger.warning(f"[DB] No active strategy found for {pair}")
        return row
    except Exception as e:
        logger.error(f"[DB ERROR] get_latest_signal_from_db({pair}): {str(e)}", exc_info=True)
        raise


async def get_old_signal_from_db(db: AsyncSession, pair: str):
    """
    Get the strategy that is 2 signals behind the latest for preview purposes
    Used on main page to show sample signals without giving real-time data
    """
    logger.info(f"[DB] Fetching preview strategy for pair: {pair}")
    try:
        stmt = (
            select(
                Strategy.strategy_id,
                Strategy.strategy_name,
                Strategy.symbol.label("pair"),
                case(
                    (Strategy.direction == "long", "BUY"),
                    (Strategy.direction == "short", "SELL"),
                    else_=func.upper(Strategy.direction),
                ).label("direction"),
                Strategy.confidence,
                Strategy.take_profit,
                Strategy.stop_loss,
                Strategy.risk_reward_ratio,
                Strategy.expiry_minutes,
                Strategy.timestamp.label("created_at"),
                Strategy.detailed_analysis,
                cast(Strategy.entry_signal["level"].astext, Numeric).label("entry_signal"),
                Strategy.status,
            )
            .where(Strategy.symbol == pair.upper())
            .order_by(Strategy.timestamp.desc())
            .limit(1)
            .offset(1)
        )
        result = (await db.execute(stmt)).mappings().first()
        row = dict(result) if result else None
        if row:
            logger.info(f"[DB] Found preview strategy for {pair}")
        else:
            logger.warning(f"[DB] No preview strategy found for {pair}")
        return row
    except Exception as e:
        logger.error(f"[DB ERROR] get_old_signal_from_db({pair}): {str(e)}", exc_info=True)
        raise


async def insert_trade_outcome(db: AsyncSession, trade_data: dict):
    """
    Insert MT5 trade execution outcome into signals table.
    Called when EA opens a position.
    """
    logger.info("[DB] Inserting trade outcome for ticket: %s", trade_data.get('ticket'))
    try:
        res = await db.execute(
            text("""
              INSERT INTO signals (
                strategy_id,
                mt5_ticket,
                mt5_magic_number,
                trading_pair,
                direction,
                entry_price,
                take_profit,
                stop_loss,
                lot_size,
                entry_time,
                status,
                market_conditions_at_entry
              ) VALUES (
                :strategy_id,
                :ticket,
                :magic_number,
                :pair,
                :direction,
                :entry_price,
                :tp,
                :sl,
                :lot_size,
                :entry_time,
                'open',
                :market_conditions::JSONB
              )
              RETURNING signal_id
            """),
            trade_data,
        )
        await db.commit()
        row = res.mappings().fetchone()
        logger.info("[DB] Trade inserted with signal_id: %s", row['signal_id'] if row else None)
        return dict(row) if row else None
    except Exception as e:
        await db.rollback()
        logger.error("[DB ERROR] insert_trade_outcome: %s", str(e))
        raise


async def update_trade_outcome(db: AsyncSession, ticket: int, outcome_data: dict):
    """
    Update trade when it closes in MT5.
    Records final P/L, exit price, whether TP/SL was hit.
    """
    logger.info("[DB] Updating trade outcome for ticket: %s", ticket)
    try:
        res = await db.execute(
            text("""
              UPDATE signals SET
                exit_price = :exit_price,
                exit_time = :exit_time,
                status = :status,
                pnl = :pnl,
                pnl_pips = :pnl_pips,
                hit_tp = :hit_tp,
                hit_sl = :hit_sl,
                commission = :commission,
                swap = :swap,
                execution_notes = :notes,
                updated_at = NOW()
              WHERE mt5_ticket = :ticket
              RETURNING signal_id
            """),
            {**outcome_data, "ticket": ticket},
        )
        await db.commit()
        row = res.mappings().fetchone()
        if row:
            logger.info("[DB] Trade outcome updated for signal_id: %s", row['signal_id'])
        else:
            logger.warning("[DB] No signal found with ticket: %s", ticket)
        return dict(row) if row else None
    except Exception as e:
        await db.rollback()
        logger.error("[DB ERROR] update_trade_outcome(%s): %s", ticket, str(e))
        raise


async def get_pair_performance(db: AsyncSession, pair: str):
    """
    Get performance metrics for a trading pair.
    Calls the get_pair_performance stored function.
    """
    logger.info("[DB] Fetching performance metrics for %s", pair)
    try:
        res = await db.execute(
            text("SELECT * FROM get_pair_performance(:pair)"),
            {"pair": pair.upper()},
        )
        row = res.mappings().fetchone()
        result = dict(row) if row else None
        if result:
            logger.info(
                "[DB] Performance for %s: %s trades, %s%% win rate",
                pair,
                result.get('total_trades'),
                result.get('win_rate'),
            )
        return result
    except Exception as e:
        logger.error("[DB ERROR] get_pair_performance(%s): %s", pair, str(e))
        raise
