import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def get_missing_core_tables(
    db: AsyncSession,
    required_tables: list[str] | None = None,
) -> list[str]:
    """Return required core API tables that are currently missing in public schema."""
    tables = required_tables or [
        "strategies",
        "email_news_analysis",
        "weekly_macro_playbook",
        "economic_event_analysis",
    ]
    try:
        missing: list[str] = []
        for table_name in tables:
            res = await db.execute(
                text("SELECT to_regclass(:rel) AS regclass"),
                {"rel": f"public.{table_name}"},
            )
            row = res.mappings().fetchone()
            if not row or row.get("regclass") is None:
                missing.append(table_name)
        return missing
    except Exception as e:
        logger.error("[DB ERROR] get_missing_core_tables: %s", str(e), exc_info=True)
        raise
