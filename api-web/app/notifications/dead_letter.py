"""
Dead Letter Notification Scaffold
Sends notifications when webhooks exceed max retries.
Can be consumed by n8n to route to Telegram/Slack/etc.

Usage:
    from app.notifications.dead_letter import notify_dead_letter
    await notify_dead_letter(event, error)

Environment Variables:
    SLACK_DEAD_LETTER_WEBHOOK_URL: Optional Slack webhook URL
    If not set, only critical logging is performed.
"""
import os
import logging
import httpx
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Slack webhook URL for dead letter notifications
# n8n can receive these and route to Telegram
SLACK_WEBHOOK_URL: Optional[str] = os.getenv("SLACK_DEAD_LETTER_WEBHOOK_URL")


async def notify_dead_letter(event: Dict[str, Any], error: Exception) -> bool:
    """
    Notify about a dead-lettered webhook event.

    Logs with CRITICAL level AND sends to Slack webhook if configured.
    n8n can consume the Slack webhook to route to Telegram.

    Args:
        event: The webhook event dict (from webhook_events table)
        error: The exception that caused the final failure

    Returns:
        True if notification was successful (or just logged), False on Slack failure
    """
    event_id = event.get("id", "unknown")
    provider = event.get("provider", "unknown")
    event_type = event.get("event_type", "unknown")
    retry_count = event.get("retry_count", 0)
    received_at = event.get("received_at", "unknown")

    # Always log critically - this should trigger monitoring alerts
    logger.critical(
        "DEAD_LETTER_WEBHOOK event_id=%s provider=%s event_type=%s retries=%d received_at=%s error=%s",
        event_id,
        provider,
        event_type,
        retry_count,
        received_at,
        str(error)[:500]
    )

    # Send to Slack webhook if configured
    if SLACK_WEBHOOK_URL:
        return await _send_slack_notification(event, error)

    return True


async def _send_slack_notification(event: Dict[str, Any], error: Exception) -> bool:
    """
    Send dead letter notification to Slack.

    The Slack message is formatted for easy n8n processing:
    - Structured blocks for parsing
    - Clear field labels
    - Timestamp in ISO format
    """
    event_id = event.get("id", "unknown")
    provider = event.get("provider", "unknown")
    event_type = event.get("event_type", "unknown")
    retry_count = event.get("retry_count", 0)
    received_at = event.get("received_at", "unknown")

    try:
        payload = {
            "text": f"🚨 Dead Letter Webhook: {provider}:{event_type}",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "🚨 Dead Letter Webhook", "emoji": True}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Provider:*\n{provider}"},
                        {"type": "mrkdwn", "text": f"*Event Type:*\n{event_type}"},
                        {"type": "mrkdwn", "text": f"*Event ID:*\n`{event_id}`"},
                        {"type": "mrkdwn", "text": f"*Retries:*\n{retry_count}"},
                    ]
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Received At:*\n{received_at}"},
                        {"type": "mrkdwn", "text": f"*Failed At:*\n{datetime.now(timezone.utc).isoformat()}"},
                    ]
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Error:*\n```{str(error)[:500]}```"}
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "Requires manual investigation. Check webhook_events table for full payload."}
                    ]
                }
            ],
            # Include raw data for n8n parsing
            "attachments": [
                {
                    "color": "#FF0000",
                    "fields": [
                        {"title": "event_id", "value": str(event_id), "short": True},
                        {"title": "provider", "value": provider, "short": True},
                        {"title": "event_type", "value": event_type, "short": True},
                        {"title": "retry_count", "value": str(retry_count), "short": True},
                    ]
                }
            ]
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                SLACK_WEBHOOK_URL,
                json=payload,
                timeout=5.0
            )

            if response.status_code == 200:
                logger.info("Dead letter notification sent to Slack: %s", event_id)
                return True
            else:
                logger.error(
                    "Slack webhook failed: status=%d body=%s",
                    response.status_code,
                    response.text[:200]
                )
                return False

    except Exception as slack_err:
        logger.error("Failed to send Slack notification: %s", slack_err)
        return False


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
