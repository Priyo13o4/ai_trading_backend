"""
Shared escalation logic for scraper.
Centralized escalation decision-making and utilities used by both sync (app.py)
and async (app_async.py) versions to reduce code duplication and ensure consistency.
"""


def should_escalate_from_http(status_code, text_length):
    """
    Determine if HTTP result needs escalation to Stealthy (no CF).
    
    Args:
        status_code: HTTP status code from response
        text_length: Length of extracted text content
    
    Returns:
        bool: True if escalation is needed, False if result is acceptable
    """
    # Escalate if error status or insufficient content
    return status_code >= 400 or text_length < 100


def should_escalate_from_stealthy_nocf(status_code, text_length):
    """
    Determine if Stealthy (no CF) result needs escalation to full Stealthy+CF.
    
    Args:
        status_code: HTTP status code from response
        text_length: Length of extracted text content
    
    Returns:
        bool: True if escalation is needed, False if result is acceptable
    """
    # Same criteria as HTTP - escalate if error status or insufficient content
    return status_code >= 400 or text_length < 100


def is_acceptable_result(status_code, text_length):
    """
    Check if result is acceptable without further escalation.
    
    Args:
        status_code: HTTP status code from response
        text_length: Length of extracted text content
    
    Returns:
        bool: True if result is acceptable, False if escalation needed
    """
    # Acceptable if 2xx status and at least 100 chars of content
    return status_code < 400 and text_length >= 100


def should_return_best_effort(last_successful_result, last_escalation_step):
    """
    Determine if we should return a best-effort result instead of throwing an error.
    
    Args:
        last_successful_result: The last successful page result (or None)
        last_escalation_step: The last escalation step that was attempted (or None)
    
    Returns:
        bool: True if we have any result worth returning
    """
    # We have a best-effort result if we got any successful payload, or if
    # the run reached advanced escalation steps where partial content is usable.
    return (
        last_successful_result is not None
        or (
            last_escalation_step is not None
            and last_escalation_step in ["stealthy_no_cf", "stealthy_with_cf"]
        )
    )


def build_degraded_response(article_payload, escalation_step, status_code, degraded_type):
    """
    Build a best-effort response when all escalation steps fail.
    
    Args:
        article_payload: Partial article extracted (may have limited content)
        escalation_step: Last escalation step that was attempted
        status_code: Last status code received
        degraded_type: Type of degradation ("partial" or "minimal")
    
    Returns:
        dict: Response payload with degraded_mode flag and available content
    """
    return {
        "title": article_payload.get("title", ""),
        "text": article_payload.get("text", ""),
        "word_count": len(article_payload.get("text", "").split()) if article_payload.get("text") else 0,
        "extraction_method": article_payload.get("method", "degraded"),
        "paragraph_count": article_payload.get("paragraph_count", 0),
        "degraded_mode": True,
        "degraded_type": degraded_type,  # "partial" or "minimal"
        "last_escalation_step": escalation_step,
        "last_status_code": status_code,
    }
