import logging
from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_common.models import EmailNewsAnalysis, WeeklyMacroPlaybook, EconomicEventAnalysis

logger = logging.getLogger(__name__)


async def get_news_preview_from_db(db: AsyncSession):
    """
    Get the single most recent high-impact or breaking news item for public preview.
    Used on landing page without authentication.
    Returns: dict with id, title, text, timestamp, importance_score, market_impact_prediction, forexfactory_url
    """
    logger.info("[DB] Fetching public news preview")
    try:
        res = await db.execute(
            text("""
              SELECT
                email_id as id,
                headline as title,
                COALESCE(human_takeaway, ai_analysis_summary, original_email_content) as text,
                email_received_at as timestamp,
                importance_score,
                market_impact_prediction,
                volatility_expectation,
                forex_instruments,
                confidence_label,
                forexfactory_urls[1] as forexfactory_url,
                breaking_news
              FROM email_news_analysis
              WHERE forex_relevant = true
              AND (importance_score >= 4 OR breaking_news = true)
              ORDER BY email_received_at DESC
              LIMIT 1
            """)
        )
        row = res.mappings().fetchone()
        if row:
            result = dict(row)
            logger.info("[DB] Found news preview: %s", result.get('title', '')[:60])
        else:
            result = None
            logger.warning("[DB] No high-impact news found for preview")
        return result
    except Exception as e:
        logger.error("[DB ERROR] get_news_preview_from_db: %s", str(e), exc_info=True)
        raise


async def get_latest_news_from_db(db: AsyncSession, limit: int = 50, offset: int = 0):
    """
    Get current/recent forex news from email_news_analysis table.
    Supports pagination with limit/offset.
    """
    logger.info("[DB] Fetching current news (limit=%s, offset=%s)", limit, offset)
    try:
        res = await db.execute(
            text("""
              SELECT
                email_id as id,
                headline as title,
                COALESCE(ai_analysis_summary, original_email_content) as text,
                email_received_at as timestamp,
                importance_score,
                sentiment_score,
                forex_instruments,
                forexfactory_category,
                market_impact_prediction,
                volatility_expectation,
                forexfactory_urls[1] as forexfactory_url,
                human_takeaway,
                attention_score,
                news_state,
                market_pressure,
                attention_window,
                confidence_label,
                expected_followups,
                ai_analysis_summary,
                original_email_content,
                similar_news_context,
                similar_news_ids,
                primary_instrument,
                pricing_state,
                reaction_certainty,
                directional_confidence,
                repricing_type
              FROM email_news_analysis
              WHERE forex_relevant = true
              AND importance_score >= 2
              ORDER BY email_received_at DESC
              LIMIT :lim OFFSET :off
            """),
            {"lim": limit, "off": offset},
        )
        results = [dict(r) for r in res.mappings().fetchall()]
        logger.info("[DB] Found %s current news items", len(results))
        return results
    except Exception as e:
        logger.error("[DB ERROR] get_latest_news_from_db: %s", str(e))
        raise


async def get_news_count(session: AsyncSession):
    """Get total count of forex-relevant news for pagination"""
    logger.info("[DB] Counting news items")
    try:
        stmt = select(func.count()).select_from(EmailNewsAnalysis).where(
            EmailNewsAnalysis.forex_relevant == True,
            EmailNewsAnalysis.importance_score >= 2
        )
        result = await session.execute(stmt)
        return result.scalar()
    except Exception as e:
        logger.error(f"[DB ERROR] get_news_count: {str(e)}")
        raise


async def get_upcoming_news_from_db(session: AsyncSession):
    """
    Get upcoming high-impact news events from the economic calendar.
    Returns events in the next 6 hours (and recent 30 mins) ordered chronologically.
    """
    logger.info("[DB] Fetching upcoming economic calendar events")
    try:
        stmt = (
            select(EconomicEventAnalysis)
            .where(EconomicEventAnalysis.event_time_utc >= text("NOW() - INTERVAL '30 minutes'"))
            .where(EconomicEventAnalysis.event_time_utc <= text("NOW() + INTERVAL '6 hours'"))
            .order_by(EconomicEventAnalysis.event_time_utc.asc())
            .limit(25)
        )
        result = await session.execute(stmt)
        results = [row.to_dict() for row in result.scalars().all()]
        logger.info(f"[DB] Found {len(results)} upcoming calendar events")
        return results
    except Exception as e:
        logger.error(f"[DB ERROR] get_upcoming_news_from_db: {str(e)}")
        raise


async def get_news_by_id_from_db(session: AsyncSession, item_id: int):
    """
    Get a specific news record by its email_id (id)
    """
    logger.info(f"[DB] Fetching news by ID: {item_id}")
    try:
        stmt = select(EmailNewsAnalysis).where(EmailNewsAnalysis.email_id == item_id)
        result = await session.execute(stmt)
        news = result.scalar_one_or_none()
        return news.to_dict() if news else None
    except Exception as e:
        logger.error(f"[DB ERROR] get_news_by_id_from_db: {str(e)}")
        return None


async def get_latest_weekly_macro_playbook_from_db(session: AsyncSession):
    """Get the latest weekly macro playbook row."""
    logger.info("[DB] Fetching latest weekly macro playbook")
    try:
        stmt = (
            select(WeeklyMacroPlaybook)
            .order_by(
                desc(WeeklyMacroPlaybook.target_week_start),
                desc(WeeklyMacroPlaybook.generated_at),
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        playbook = result.scalar_one_or_none()
        return playbook.to_dict() if playbook else None
    except Exception as e:
        logger.error(f"[DB ERROR] get_latest_weekly_macro_playbook_from_db: {str(e)}", exc_info=True)
        raise


async def get_economic_event_analysis_from_db(
    session: AsyncSession,
    limit: int = 20,
    offset: int = 0,
    upcoming_only: bool = False,
):
    """Get economic event analysis rows with optional upcoming-only filter and total count."""
    logger.info(
        "[DB] Fetching economic events (upcoming_only=%s, limit=%s, offset=%s)",
        upcoming_only,
        limit,
        offset,
    )

    try:
        stmt = select(EconomicEventAnalysis)
        count_stmt = select(func.count()).select_from(EconomicEventAnalysis)

        if upcoming_only:
            stmt = stmt.where(EconomicEventAnalysis.event_time_utc >= func.now())
            count_stmt = count_stmt.where(EconomicEventAnalysis.event_time_utc >= func.now())

        stmt = (
            stmt.order_by(
                EconomicEventAnalysis.event_time_utc.asc() if upcoming_only else EconomicEventAnalysis.event_time_utc.desc(),
                desc(EconomicEventAnalysis.created_at),
            )
            .limit(limit)
            .offset(offset)
        )

        result = await session.execute(stmt)
        rows = [row.to_dict() for row in result.scalars().all()]

        count_result = await session.execute(count_stmt)
        total = int(count_result.scalar() or 0)

        logger.info("[DB] Found %s economic events (total=%s)", len(rows), total)
        return {"events": rows, "total": total}
    except Exception as exc:
        logger.error("[DB ERROR] get_economic_event_analysis_from_db: %s", str(exc), exc_info=True)
        raise
