"""
Async Scrapling API Wrapper - Concurrent live implementation.
Runs the same extraction payload contract as sync using shared extraction helpers.
"""
import json
import logging
import os
import asyncio
import traceback
import concurrent.futures
from datetime import datetime
from aiohttp import web

from scrapling.fetchers import Fetcher, StealthyFetcher
from escalation import is_acceptable_result
from app import _extract_article_payload, _extract_links, _extract_title

logger = logging.getLogger("scrapling_api_async")

# Thread pool for running blocking scrapling calls (max_workers=8 for normal load + backlog scenarios)
THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8)
ESCALATION_STEP_TIMEOUT_SECONDS = 40
REQUEST_TIMEOUT_SECONDS = 300


async def async_fetch_with_escalation(url, payload):
    """
    Async escalation with best-effort handling.
    Steps: HTTP -> Stealthy no CF -> Stealthy with CF
    Timeout: 40s per step (worst-case 120s across three steps)
    
    If all escalation steps fail, returns best-effort result with degraded_mode flag
    instead of raising an exception (best-effort approach for reliability).
    
    Returns tuple: (page_object, escalation_step_used, source_status_code)
    """
    loop = asyncio.get_event_loop()
    status_code = 200
    last_successful_page = None
    last_successful_article = None
    last_escalation_step = None
    
    # Step 1: Try HTTP first (fastest, 7% CPU, 50MB RAM).
    try:
        logger.info(
            f"Escalation Step 1: Trying HTTP mode for {url} "
            f"({ESCALATION_STEP_TIMEOUT_SECONDS}s timeout)"
        )
        page = await asyncio.wait_for(
            loop.run_in_executor(THREAD_POOL, lambda: Fetcher.get(url)),
            timeout=ESCALATION_STEP_TIMEOUT_SECONDS
        )
        status_code = int(getattr(page, "status", 200) or 200)
        article = _extract_article_payload(page)
        text_length = len(article.get("text", ""))
        
        # Track as potential best-effort result
        last_successful_page = page
        last_successful_article = article
        last_escalation_step = "http"
        
        # If HTTP succeeds with good content, use it and stop escalation
        if is_acceptable_result(status_code, text_length):
            logger.info(f"HTTP succeeded with {text_length} chars of content")
            return page, "http", status_code
        
        logger.info(f"HTTP insufficient ({text_length} chars) or error ({status_code}), escalating...")
    except asyncio.TimeoutError:
        logger.warning(f"HTTP fetch timeout (40s), escalating to Stealthy...")
    except Exception as e:
        logger.warning(f"HTTP fetch failed: {e}, escalating to Stealthy...")
    
    # Step 2: Escalate to Stealthy without Cloudflare solving.
    try:
        logger.info(
            f"Escalation Step 2: Trying Stealthy (no CF) mode for {url} "
            f"({ESCALATION_STEP_TIMEOUT_SECONDS}s timeout)"
        )
        options = {
            "headless": bool(payload.get("headless", True)),
            "solve_cloudflare": False,
        }
        
        page = await asyncio.wait_for(
            loop.run_in_executor(
                THREAD_POOL,
                lambda: StealthyFetcher.fetch(url, **options)
            ),
            timeout=ESCALATION_STEP_TIMEOUT_SECONDS
        )
        status_code = int(getattr(page, "status", 200) or 200)
        article = _extract_article_payload(page)
        text_length = len(article.get("text", ""))
        
        # Track as potential best-effort result
        last_successful_page = page
        last_successful_article = article
        last_escalation_step = "stealthy_no_cf"
        
        # If Stealthy (no CF) succeeds with good content, use it
        if is_acceptable_result(status_code, text_length):
            logger.info(f"Stealthy (no CF) succeeded with {text_length} chars of content")
            return page, "stealthy_no_cf", status_code
        
        logger.info(f"Stealthy (no CF) insufficient ({text_length} chars), escalating to full power...")
    except asyncio.TimeoutError:
        logger.warning(f"Stealthy (no CF) timeout (40s), escalating to full Stealthy+CF...")
    except Exception as e:
        logger.warning(f"Stealthy (no CF) failed: {e}, escalating to full Stealthy+CF...")
    
    # Step 3: Last resort - full power with Cloudflare solving.
    try:
        logger.info(
            f"Escalation Step 3: Trying Stealthy (with CF) mode for {url} "
            f"({ESCALATION_STEP_TIMEOUT_SECONDS}s timeout)"
        )
        options = {
            "headless": bool(payload.get("headless", True)),
            "solve_cloudflare": True,
        }
        
        page = await asyncio.wait_for(
            loop.run_in_executor(
                THREAD_POOL,
                lambda: StealthyFetcher.fetch(url, **options)
            ),
            timeout=ESCALATION_STEP_TIMEOUT_SECONDS
        )
        status_code = int(getattr(page, "status", 200) or 200)
        article = _extract_article_payload(page)
        text_length = len(article.get("text", ""))
        
        # Track as potential best-effort result
        last_successful_page = page
        last_successful_article = article
        last_escalation_step = "stealthy_with_cf"
        
        logger.info(f"Stealthy (with CF) completed with {text_length} chars")
        return page, "stealthy_with_cf", status_code
    except asyncio.TimeoutError:
        logger.error(f"Stealthy (with CF) timeout (40s)")
    except Exception as e:
        logger.error(f"Stealthy (with CF) failed: {e}")
    
    # All escalation steps exhausted - return best-effort result instead of raising
    logger.error(f"All escalation steps failed for {url}. Returning best-effort result.")
    
    if last_successful_article and last_successful_page:
        logger.info(f"Returning best-effort result from {last_escalation_step}")
        return last_successful_page, last_escalation_step, status_code
    else:
        # Absolutely nothing available - create minimal fallback
        logger.warning(f"No successful content available for {url}, returning minimal fallback")
        # Create a minimal page-like object with empty content
        class MinimalPage:
            def __init__(self):
                self.status = 500
                self.text = ""
                self.html = ""
        
        return MinimalPage(), "complete_failure", 500


async def scrape_handler(request):
    """
    Async HTTP handler for scrape requests with escalation system.
    Endpoint: POST /api/v1/scrape
    """
    try:
        payload = await request.json()
        url = payload.get("url", "").strip()

        if not url:
            return web.json_response(
                {"success": False, "error": "Missing 'url' parameter"},
                status=400
            )

        escalation_step = None
        source_status_code = 200
        
        try:
            # Always use escalation system
            result, escalation_step, source_status_code = await asyncio.wait_for(
                async_fetch_with_escalation(url, payload),
                timeout=REQUEST_TIMEOUT_SECONDS
            )

            if not result:
                return web.json_response(
                    {"success": False, "error": "Empty result from fetcher"},
                    status=500
                )

            source_status_code = int(
                getattr(result, "status", source_status_code) or source_status_code
            )
            article = _extract_article_payload(result)
            text = article["text"]
            title = article["title"]
            links = _extract_links(result)

            # Detect if result is degraded (returned due to escalation failure)
            is_degraded = escalation_step in ["complete_failure"] or (source_status_code >= 500)

            return web.json_response(
                {
                    "success": True,
                    "url": url,
                    "title": title,
                    "text": text,
                    "word_count": len(text.split()) if text else 0,
                    "links": links,
                    "article": {
                        "extraction_method": article["method"],
                        "paragraph_count": article["paragraph_count"],
                        "date_time": article["stats"].get("date_time"),
                        "date_time_utc": article["stats"].get("date_time_utc"),
                        "posted_at_text": article["stats"].get("posted_at_text"),
                        "posted_ago_text": article["stats"].get("posted_ago_text"),
                        "posted_by": article["stats"].get("posted_by"),
                        "news_type": article["stats"].get("news_type"),
                        "impact": article["stats"].get("impact"),
                        "linked_events": article["stats"].get("linked_events") or [],
                        "server_timezone": article["stats"].get("server_timezone"),
                    },
                    "meta": {
                        "status_code": source_status_code,
                        "escalation_step": escalation_step,
                        "fallback_mode_used": None,
                        "degraded_mode": is_degraded,
                    },
                    "error": None,
                }
            )

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching {url}")
            return web.json_response(
                {"success": False, "error": f"Fetch timeout ({REQUEST_TIMEOUT_SECONDS}s)"},
                status=504
            )

    except json.JSONDecodeError:
        return web.json_response(
            {"success": False, "error": "Invalid JSON in request body"},
            status=400
        )
    except Exception as e:
        logger.error(f"Scrape handler error: {e}\n{traceback.format_exc()}")
        return web.json_response(
            {"success": False, "error": str(e)},
            status=500
        )


async def health_handler(request):
    """Health check endpoint."""
    return web.json_response({
        "status": "healthy",
        "mode": "async-concurrent",
        "implementation": "Thread pool (max_workers=8) with escalation system",
        "timestamp": datetime.utcnow().isoformat(),
    })


async def scrape_no_filter_handler(request):
    """
    Minimal processing endpoint returning raw HTML without extraction.
    Endpoint: POST /api/v1/scrape-no-filter
    """
    try:
        payload = await request.json()
        url = payload.get("url", "").strip()

        if not url:
            return web.json_response(
                {"success": False, "error": "Missing 'url' parameter"},
                status=400
            )

        try:
            # Use escalation system for fetching.
            result, escalation_step, source_status_code = await asyncio.wait_for(
                async_fetch_with_escalation(url, payload),
                timeout=REQUEST_TIMEOUT_SECONDS
            )

            if not result:
                return web.json_response(
                    {"success": False, "error": "Empty result from fetcher"},
                    status=500
                )

            # Minimal processing: extract only title and raw HTML
            title = _extract_title(result)
            html = getattr(result, "html", "") or getattr(result, "text", "") or ""
            
            # Detect if result is degraded
            is_degraded = escalation_step in ["complete_failure"] or (source_status_code >= 500)

            return web.json_response({
                "success": True,
                "url": url,
                "title": title,
                "html": html[:50000],
                "status_code": source_status_code,
                "meta": {
                    "escalation_step": escalation_step,
                    "endpoint": "scrape-no-filter",
                    "processing": "minimal",
                    "degraded_mode": is_degraded,
                },
                "error": None,
            })

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching {url}")
            return web.json_response(
                {"success": False, "error": f"Fetch timeout ({REQUEST_TIMEOUT_SECONDS}s)"},
                status=504
            )

    except json.JSONDecodeError:
        return web.json_response(
            {"success": False, "error": "Invalid JSON in request body"},
            status=400
        )
    except Exception as e:
        logger.error(f"Scrape-no-filter handler error: {e}\n{traceback.format_exc()}")
        return web.json_response(
            {"success": False, "error": str(e)},
            status=500
        )


async def init_app():
    """Initialize aiohttp application."""
    app = web.Application()
    app.router.add_post('/api/v1/scrape', scrape_handler)
    app.router.add_post('/api/v1/scrape-no-filter', scrape_no_filter_handler)
    app.router.add_get('/health', health_handler)
    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    host = os.getenv("SCRAPLING_HOST", "0.0.0.0")
    port = int(os.getenv("SCRAPLING_PORT", "8010"))

    logger.info(f"Starting Async Scrapling API on {host}:{port}...")
    logger.info("Features: HTTP-first escalation, max_workers=8 for concurrent requests")
    logger.info("Endpoints: /api/v1/scrape (full extraction) + /api/v1/scrape-no-filter (raw HTML)")

    app = asyncio.run(init_app())
    web.run_app(app, host=host, port=port, print=logger.info)


if __name__ == '__main__':
    main()
