"""
Cross-Service Retry Matrix
Defines timeout, retries, backoff, idempotency key, and dead-letter for each boundary.

This module provides consistent retry policies for all integration boundaries:
- Provider webhook callbacks (Razorpay, Plisio)
- Internal service calls
- n8n workflow HTTP nodes

Usage:
    from app.config.retry_policies import get_retry_policy, RetryPolicy

    policy = get_retry_policy("razorpay_webhook")
    # Use policy.timeout_seconds, policy.max_retries, etc.
"""
from dataclasses import dataclass
from typing import Optional
import random


@dataclass
class RetryPolicy:
    """
    Configuration for retry behavior at an integration boundary.

    Attributes:
        timeout_seconds: Maximum time to wait for a single attempt
        max_retries: Maximum number of retry attempts (total attempts = max_retries + 1)
        backoff_base_seconds: Base duration for exponential backoff
        backoff_max_seconds: Maximum backoff duration cap
        jitter_factor: Randomization factor (0.0 to 1.0) to prevent thundering herd
        idempotency_key_source: Where to find the idempotency key
            - "payload:field_name" - Extract from request payload
            - "header:Header-Name" - Extract from request headers
            - "generated_uuid" - Generate a new UUID
        dead_letter_target: Where to send failed events after exhausting retries
            - "webhook_events.last_error" - Mark in database
            - "slack" - Send to Slack webhook
            - "logs_only" - Only log, no dead-letter queue
    """
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    jitter_factor: float
    idempotency_key_source: str
    dead_letter_target: str

    def calculate_backoff(self, attempt: int) -> float:
        """
        Calculate backoff duration for a given attempt number.

        Uses exponential backoff with jitter and a maximum cap.

        Args:
            attempt: Current attempt number (0-indexed)

        Returns:
            Backoff duration in seconds
        """
        # Exponential backoff: base * 2^attempt
        backoff = self.backoff_base_seconds * (2 ** attempt)

        # Apply maximum cap
        backoff = min(backoff, self.backoff_max_seconds)

        # Apply jitter to prevent thundering herd
        if self.jitter_factor > 0:
            jitter = backoff * self.jitter_factor * random.random()
            backoff = backoff + jitter

        return backoff


# Provider callback policies
RETRY_POLICIES = {
    # Razorpay webhook callbacks
    "razorpay_webhook": RetryPolicy(
        timeout_seconds=5.0,
        max_retries=5,
        backoff_base_seconds=120,  # 2 minutes
        backoff_max_seconds=1920,  # 32 minutes
        jitter_factor=0.2,
        idempotency_key_source="payload:id",
        dead_letter_target="webhook_events.last_error"
    ),

    # Plisio callback notifications
    "plisio_callback": RetryPolicy(
        timeout_seconds=5.0,
        max_retries=5,
        backoff_base_seconds=120,
        backoff_max_seconds=1920,
        jitter_factor=0.2,
        idempotency_key_source="payload:txn_id",
        dead_letter_target="webhook_events.last_error"
    ),

    # n8n internal webhook hops
    "n8n_internal_webhook": RetryPolicy(
        timeout_seconds=10.0,
        max_retries=3,
        backoff_base_seconds=5,
        backoff_max_seconds=60,
        jitter_factor=0.3,
        idempotency_key_source="header:X-N8N-Execution-Id",
        dead_letter_target="n8n_dead_letter_queue"
    ),

    # Internal service-to-service calls
    "internal_service_call": RetryPolicy(
        timeout_seconds=3.0,
        max_retries=2,
        backoff_base_seconds=1,
        backoff_max_seconds=5,
        jitter_factor=0.5,
        idempotency_key_source="generated_uuid",
        dead_letter_target="logs_only"
    ),

    # SSE reconnection policy
    "sse_reconnect": RetryPolicy(
        timeout_seconds=30.0,
        max_retries=10,
        backoff_base_seconds=1,
        backoff_max_seconds=30,
        jitter_factor=0.3,
        idempotency_key_source="header:Last-Event-ID",
        dead_letter_target="logs_only"
    ),

    # Background janitor tasks
    "janitor_task": RetryPolicy(
        timeout_seconds=30.0,
        max_retries=3,
        backoff_base_seconds=60,
        backoff_max_seconds=300,
        jitter_factor=0.25,
        idempotency_key_source="generated_uuid",
        dead_letter_target="logs_only"
    ),
}


def get_retry_policy(boundary: str) -> RetryPolicy:
    """
    Get the retry policy for a given integration boundary.

    Args:
        boundary: Name of the boundary (e.g., "razorpay_webhook", "internal_service_call")

    Returns:
        RetryPolicy for the boundary, or default internal_service_call policy if not found
    """
    return RETRY_POLICIES.get(boundary, RETRY_POLICIES["internal_service_call"])


def get_provider_webhook_policy(provider_name: str) -> RetryPolicy:
    """Return the retry policy for a payment provider webhook."""
    normalized = str(provider_name or "").strip().lower()
    if normalized == "razorpay":
        return get_retry_policy("razorpay_webhook")
    if normalized == "plisio":
        return get_retry_policy("plisio_callback")
    return get_retry_policy("internal_service_call")


def list_boundaries() -> list:
    """Return list of all defined boundary names."""
    return list(RETRY_POLICIES.keys())
