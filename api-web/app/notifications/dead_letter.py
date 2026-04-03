"""Dead-letter notification helpers for webhook processing failures."""

import logging
from typing import Any, Dict

from .error_alerts import notify_dead_letter_event

logger = logging.getLogger(__name__)

async def notify_dead_letter(event: Dict[str, Any], error: Exception) -> bool:
    """
    Notify about a dead-lettered webhook event.

    Always logs at CRITICAL level and sends a direct n8n error alert
    to the /dead-letter webhook path when alerts are enabled.

    Args:
        event: The webhook event dict (from webhook_events table)
        error: The exception that caused the final failure

    Returns:
        True if notification was successful, otherwise False.
    """
    event_id = event.get("id", "unknown")
    provider = event.get("provider", "unknown")
    event_type = event.get("event_type", "unknown")
    retry_count = event.get("retry_count", 0)
    received_at = event.get("received_at", "unknown")

    # Always log critically; this should trigger platform monitoring.
    logger.critical(
        "DEAD_LETTER_WEBHOOK event_id=%s provider=%s event_type=%s retries=%d received_at=%s error=%s",
        event_id,
        provider,
        event_type,
        retry_count,
        received_at,
        str(error)[:500]
    )

    return await notify_dead_letter_event(event, error)


async def notify_dead_letter_batch(events: list, errors: list) -> int:
    """
    Notify about multiple dead-lettered webhook events.

    Args:
        events: List of webhook event dicts
        errors: List of corresponding exceptions

    Returns:
        Number of successful notifications
    """
    success_count = 0
    for event, error in zip(events, errors):
        if await notify_dead_letter(event, error):
            success_count += 1
    return success_count
