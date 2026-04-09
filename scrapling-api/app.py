import atexit
import json
import logging
import os
import re
import signal
import threading
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher, StealthySession
from escalation import (
    is_acceptable_result,
)

_SESSION_LOCK = threading.Lock()
_SESSION_FETCH_LOCK = threading.Lock()
_STEALTHY_SESSION = None
_STEALTHY_SESSION_CFG = None
logger = logging.getLogger("scrapling_api")


def _repair_text_encoding(text):
    if text is None:
        return ""

    value = str(text)

    # Best-effort pass for common UTF-8/Latin-1 mojibake.
    if "\u00e2\u0080" in value or "\u00c3" in value or "\u00c2" in value:
        try:
            repaired = value.encode("latin-1").decode("utf-8")
            if repaired:
                value = repaired
        except Exception:
            pass

    replacements = {
        "\u00e2\u0080\u0099": "'",
        "\u00e2\u0080\u0098": "'",
        "\u00e2\u0080\u009c": '"',
        "\u00e2\u0080\u009d": '"',
        "\u00e2\u0080\u0093": "-",
        "\u00e2\u0080\u0094": "-",
        "\u00c2\u00a0": " ",
    }
    for bad, good in replacements.items():
        value = value.replace(bad, good)

    return value


def _norm(text):
    return re.sub(r"\s+", " ", _repair_text_encoding(text or "")).strip()


def _posted_at_to_utc_iso(posted_at_text):
    if not posted_at_text:
        return None

    raw = _norm(posted_at_text)
    now_local = datetime.now().astimezone()

    formats = [
        ("%b %d, %Y %I:%M%p", True),
        ("%b %d, %I:%M%p", False),
    ]

    for fmt, has_year in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            if has_year:
                local_dt = parsed.replace(tzinfo=now_local.tzinfo)
            else:
                local_dt = parsed.replace(year=now_local.year, tzinfo=now_local.tzinfo)
                # ForexFactory story timestamps are recent. If the guessed date is too far
                # in the future, roll back to previous year.
                if local_dt - now_local > timedelta(days=45):
                    local_dt = local_dt.replace(year=local_dt.year - 1)
            return local_dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
        except Exception:
            return None

    return None


def _dedupe_keep_order(items):
    out = []
    seen = set()
    for item in items:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _clean_chunks(chunks):
    cleaned = []
    for c in chunks:
        t = _norm(c)
        if not t:
            continue
        # Drop obvious JS/UI noise.
        if any(bad in t for bad in ["window.", "function(", "document.", "gtag(", "_google-"]):
            continue
        cleaned.append(t)
    return _dedupe_keep_order(cleaned)


def _extract_title(page):
    selectors = [
        "li.news__article h1::text",
        "article h1::text",
        "h1::text",
        "title::text",
    ]
    for sel in selectors:
        try:
            value = _norm(page.css(sel).get() or "")
            if value:
                return value
        except Exception:
            pass
    return ""


def _extract_story_stats(page):
    def _first(selector):
        try:
            return _norm(page.css(selector).get() or "")
        except Exception:
            return ""

    try:
        stats_text = _norm(" ".join(page.css("div.news-stats *::text").getall()))
    except Exception:
        stats_text = ""

    posted_at = _first("div.news-stats .news-stats__date strong::text") or None
    posted_ago = _first("div.news-stats .news-stats__date em::text") or None
    if posted_ago:
        posted_ago = posted_ago.lstrip("|").strip() or None

    posted_by = _first("div.news-stats .news-stats__poster .username::text") or None
    category_text = _norm(" ".join(page.css("div.news-stats .news-stats__detail--category *::text").getall()))

    impact = None
    news_type = None
    linked_events = []

    if category_text:
        m_impact = re.search(r"\b(Low|Medium|High)\s+Impact\b", category_text, re.IGNORECASE)
        if m_impact:
            impact = m_impact.group(1).capitalize()

        news_type_clean = re.sub(r"\b(?:Low|Medium|High)\s+Impact\b", "", category_text, flags=re.IGNORECASE)
        news_type_clean = _norm(news_type_clean)
        if news_type_clean:
            news_type = news_type_clean

    try:
        names = [_norm(v) for v in page.css("div.news-stats .news-stats__section--linked-events a.news-stats__linked-event span::text").getall()]
        hrefs = page.css("div.news-stats .news-stats__section--linked-events a.news-stats__linked-event::attr(href)").getall()
        icon_classes = page.css("div.news-stats .news-stats__section--linked-events a.news-stats__linked-event img::attr(class)").getall()

        for idx, name in enumerate(names):
            if not name:
                continue
            href = hrefs[idx] if idx < len(hrefs) else None
            if href and href.startswith("/"):
                href = f"https://www.forexfactory.com{href}"

            event_impact = None
            if idx < len(icon_classes):
                m_icon_impact = re.search(r"ff-(low|medium|high)", icon_classes[idx], re.IGNORECASE)
                if m_icon_impact:
                    event_impact = m_icon_impact.group(1).capitalize()

            linked_events.append(
                {
                    "name": name,
                    "url": href,
                    "impact": event_impact,
                }
            )
    except Exception:
        linked_events = []

    if stats_text:
        if not posted_at:
            m_posted = re.search(r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{1,2}:\d{2}(?:am|pm))", stats_text)
            if m_posted:
                posted_at = m_posted.group(1)

        if not posted_by:
            m_by = re.search(r"Posted by\s+([A-Za-z0-9_\-]+)", stats_text)
            if m_by:
                posted_by = m_by.group(1)

        if not impact:
            m_impact = re.search(r"\b(Low|Medium|High)\s+Impact\b", stats_text, re.IGNORECASE)
            if m_impact:
                impact = m_impact.group(1).capitalize()

        if not news_type:
            m_type = re.search(
                r"\b(?:Low|Medium|High)\s+Impact\s+([A-Za-z][A-Za-z\-/ ]{1,40})",
                stats_text,
                re.IGNORECASE,
            )
            if m_type:
                news_type = _norm(m_type.group(1))

    server_tz = datetime.now().astimezone().tzname()
    posted_at_utc = _posted_at_to_utc_iso(posted_at)

    return {
        "date_time": posted_at,
        "date_time_utc": posted_at_utc,
        "posted_at_text": posted_at,
        "posted_ago_text": posted_ago,
        "posted_by": posted_by,
        "impact": impact,
        "news_type": news_type,
        "linked_events": linked_events,
        "server_timezone": server_tz,
        "story_stats_text": stats_text,
    }


def _extract_article_chunks(page):
    selectors = [
        "li.news__article p.news__copy::text",
        "li.news__article .news__story p::text",
        "li.news__article p::text",
        "article p::text",
        "main article p::text",
        "main p::text",
    ]

    collected = []
    for sel in selectors:
        try:
            vals = page.css(sel).getall()
        except Exception:
            vals = []
        if vals:
            collected.extend(vals)
            # Stop early once we have clear article content.
            if len(collected) >= 6:
                break

    cleaned = _clean_chunks(collected)
    return cleaned


def _extract_embed_chunks(page):
    """
    Extract text from social embeds that often appear inside ForexFactory stories.

    Twitter/X embeds are usually blockquote.twitter-tweet. Truth Social embeds on
    ForexFactory are often generic blockquotes with rich text nodes but no <p> tags.
    """
    selectors = [
        "li.news__article blockquote.twitter-tweet p::text",
        "li.news__article blockquote.twitter-tweet *::text",
        "div.news__story li.news__article--tweet h1::text",
        "div.news__story li.news__article--tweet p::text",
        "li.news__article--tweet h1::text",
        "li.news__article--tweet p::text",
        "li.news__article .news__story blockquote *::text",
        "li.news__article blockquote *::text",
        "article blockquote.twitter-tweet p::text",
        "article blockquote.twitter-tweet *::text",
        "article blockquote *::text",
    ]

    collected = []
    for sel in selectors:
        try:
            vals = page.css(sel).getall()
        except Exception:
            vals = []
        if vals:
            collected.extend(vals)

    cleaned = _clean_chunks(collected)

    filtered = []
    for chunk in cleaned:
        c = _norm(chunk)
        if not c:
            continue

        low = c.lower()
        if low in {"show more", "view on x", "view on twitter"}:
            continue
        if re.fullmatch(r"\d+\s*(?:s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)\s+ago", low):
            continue
        if low in {"@", "-", "|", "..."}:
            continue

        filtered.append(c)

    return _dedupe_keep_order(filtered)


def _has_social_embed_signals(page):
    selectors = [
        "li.news__article blockquote.twitter-tweet",
        "li.news__article .twitter-tweet",
        "li.news__article a[href*='twitter.com']",
        "li.news__article a[href*='x.com']",
        "li.news__article a[href*='truthsocial.com']",
        "div.news__story li.news__article--tweet",
    ]

    for sel in selectors:
        try:
            if page.css(sel).get():
                return True
        except Exception:
            continue
    return False


def _extract_links(page, max_links=200):
    selectors = [
        "li.news__article a::attr(href)",
        "article a::attr(href)",
        "main article a::attr(href)",
    ]

    links = []
    for sel in selectors:
        try:
            vals = page.css(sel).getall()
        except Exception:
            vals = []
        if vals:
            links.extend(vals)
            if len(links) >= max_links:
                break

    if not links:
        try:
            links = page.css("a::attr(href)").getall()
        except Exception:
            links = []

    return _dedupe_keep_order([l for l in links if l])[:max_links]


def _extract_article_payload(page):
    title = _extract_title(page)
    stats = _extract_story_stats(page)
    chunks = _extract_article_chunks(page)

    initial_text = _norm(" ".join(chunks))
    used_embed_fallback = False

    # Always enrich with embed text when social markers exist. This prevents
    # multi-embed stories from keeping only the first extracted block.
    if _has_social_embed_signals(page) or len(initial_text) < 120:
        embed_chunks = _extract_embed_chunks(page)
        if embed_chunks:
            chunks = _dedupe_keep_order(chunks + embed_chunks)
            used_embed_fallback = True

    text = _norm(" ".join(chunks))

    return {
        "title": title,
        "text": text,
        "chunks": chunks,
        "paragraph_count": len(chunks),
        "method": "targeted+embed" if used_embed_fallback else "targeted",
        "stats": stats,
    }


def _session_cfg_from_options(options):
    return (
        bool(options.get("headless", True)),
        bool(options.get("solve_cloudflare", True)),
        bool(options.get("google_search", False)),
        bool(options.get("network_idle", False)),
    )


def _close_session(session):
    if not session:
        return
    try:
        close_fn = getattr(session, "close", None)
        if callable(close_fn):
            close_fn()
            return
    except Exception:
        pass

    try:
        exit_fn = getattr(session, "__exit__", None)
        if callable(exit_fn):
            exit_fn(None, None, None)
    except Exception:
        pass


def _ensure_stealthy_session(options, force_reset=False):
    global _STEALTHY_SESSION, _STEALTHY_SESSION_CFG

    cfg = _session_cfg_from_options(options)
    with _SESSION_LOCK:
        if force_reset and _STEALTHY_SESSION is not None:
            _close_session(_STEALTHY_SESSION)
            _STEALTHY_SESSION = None
            _STEALTHY_SESSION_CFG = None

        if _STEALTHY_SESSION is not None and _STEALTHY_SESSION_CFG == cfg:
            return _STEALTHY_SESSION

        if _STEALTHY_SESSION is not None:
            _close_session(_STEALTHY_SESSION)
            _STEALTHY_SESSION = None
            _STEALTHY_SESSION_CFG = None

        session_kwargs = {
            "headless": cfg[0],
            "solve_cloudflare": cfg[1],
            "google_search": cfg[2],
            "network_idle": cfg[3],
        }
        session = StealthySession(**session_kwargs)

        # Keep a persistent opened browser context.
        enter_fn = getattr(session, "__enter__", None)
        if callable(enter_fn):
            maybe_entered = enter_fn()
            if maybe_entered is not None:
                session = maybe_entered

        _STEALTHY_SESSION = session
        _STEALTHY_SESSION_CFG = cfg
        return _STEALTHY_SESSION


def _fetch_with_mode(url, mode, options, reuse_session=True, session_reset=False):
    if mode == "dynamic":
        fetch_fn = DynamicFetcher.fetch
        try:
            return fetch_fn(url, **options)
        except TypeError:
            return fetch_fn(url)

    if mode == "http":
        fetch_fn = Fetcher.get
        try:
            return fetch_fn(url, **options)
        except TypeError:
            return fetch_fn(url)

    # Default stealthy mode.
    if reuse_session:
        try:
            session = _ensure_stealthy_session(options, force_reset=session_reset)
            with _SESSION_FETCH_LOCK:
                return session.fetch(url)
        except Exception:
            # Reset and retry once, then fall back to one-off fetch.
            try:
                session = _ensure_stealthy_session(options, force_reset=True)
                with _SESSION_FETCH_LOCK:
                    return session.fetch(url)
            except Exception:
                pass

    fetch_fn = StealthyFetcher.fetch
    try:
        return fetch_fn(url, **options)
    except TypeError:
        return fetch_fn(url)


def _timeout_handler(signum, frame):
    """Signal handler for escalation step timeout."""
    raise TimeoutError("Fetch operation exceeded time limit")


def _scrape_with_escalation(url, payload):
    """
    Implements HTTP-first fallback escalation system with timeout and best-effort handling.
    Steps: HTTP (40s) → Stealthy no CF (40s) → Stealthy with CF (40s)
    Total timeout: 120 seconds
    
    If all escalation steps fail, returns best-effort result with degraded_mode flag
    instead of raising an exception (best-effort approach for reliability).
    
    Returns tuple: (page_object, escalation_step_used, status_code)
    """
    escalation_step = None
    status_code = 200
    last_successful_page = None
    last_successful_article = None
    last_escalation_step = None
    
    # Step 1: Try HTTP first (fastest, 7% CPU, 50MB RAM) - 40s timeout
    try:
        logger.info(f"Escalation Step 1: Trying HTTP mode for {url} (40s timeout)")
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(40)
        
        try:
            page = _fetch_with_mode(url, "http", {}, reuse_session=False)
            signal.alarm(0)  # Cancel alarm
            
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
        except TimeoutError as te:
            signal.alarm(0)  # Cancel alarm
            logger.warning(f"HTTP fetch timeout (40s): {te}, escalating to Stealthy...")
        except Exception as e:
            signal.alarm(0)  # Cancel alarm
            logger.warning(f"HTTP fetch failed: {e}, escalating to Stealthy...")
    except Exception as e:
        logger.warning(f"HTTP exception handler failed: {e}")
    
    # Step 2: Escalate to Stealthy without Cloudflare solving - 40s timeout
    try:
        logger.info(f"Escalation Step 2: Trying Stealthy (no CF) mode for {url} (40s timeout)")
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(40)
        
        try:
            options = {
                "headless": bool(payload.get("headless", True)),
                "solve_cloudflare": False,
            }
            page = _fetch_with_mode(url, "stealthy", options, reuse_session=True)
            signal.alarm(0)  # Cancel alarm
            
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
        except TimeoutError as te:
            signal.alarm(0)  # Cancel alarm
            logger.warning(f"Stealthy (no CF) timeout (40s): {te}, escalating to full Stealthy+CF...")
        except Exception as e:
            signal.alarm(0)  # Cancel alarm
            logger.warning(f"Stealthy (no CF) failed: {e}, escalating to full Stealthy+CF...")
    except Exception as e:
        logger.warning(f"Stealthy (no CF) exception handler failed: {e}")
    
    # Step 3: Last resort - Full power with Cloudflare solving - 40s timeout
    try:
        logger.info(f"Escalation Step 3: Trying Stealthy (with CF) mode for {url} (40s timeout)")
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(40)
        
        try:
            options = {
                "headless": bool(payload.get("headless", True)),
                "solve_cloudflare": True,
            }
            page = _fetch_with_mode(url, "stealthy", options, reuse_session=True)
            signal.alarm(0)  # Cancel alarm
            
            status_code = int(getattr(page, "status", 200) or 200)
            article = _extract_article_payload(page)
            text_length = len(article.get("text", ""))
            
            # Track as potential best-effort result
            last_successful_page = page
            last_successful_article = article
            last_escalation_step = "stealthy_with_cf"
            
            logger.info(f"Stealthy (with CF) completed with {text_length} chars")
            return page, "stealthy_with_cf", status_code
        except TimeoutError as te:
            signal.alarm(0)  # Cancel alarm
            logger.error(f"Stealthy (with CF) timeout (40s): {te}")
        except Exception as e:
            signal.alarm(0)  # Cancel alarm
            logger.error(f"Stealthy (with CF) failed: {e}")
    except Exception as e:
        logger.error(f"Stealthy (with CF) exception handler failed: {e}")
    
    # All escalation steps exhausted - return best-effort result instead of raising
    logger.error(f"All escalation steps failed for {url}. Returning best-effort result.")
    
    if last_successful_article and last_successful_page:
        logger.info(f"Returning best-effort result from {last_escalation_step}")
        # Mark result as degraded but still usable
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


@atexit.register
def _cleanup_session():
    global _STEALTHY_SESSION, _STEALTHY_SESSION_CFG
    with _SESSION_LOCK:
        _close_session(_STEALTHY_SESSION)
        _STEALTHY_SESSION = None
        _STEALTHY_SESSION_CFG = None


class Handler(BaseHTTPRequestHandler):
    server_version = "scrapling-api/1.0"

    def _send_json(self, status_code, payload):
        data = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before reading response.
            logger.debug("Client disconnected before response write")
            return False

    def log_message(self, format, *args):
        # Keep default server logs, but route through logger for uniform formatting.
        logger.info("%s - - %s", self.address_string(), format % args)

    def do_GET(self):
        return self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        # Route to appropriate handler
        if self.path == "/api/v1/scrape":
            return self._handle_scrape()
        elif self.path == "/api/v1/scrape-no-filter":
            return self._handle_scrape_no_filter()
        else:
            return self._send_json(404, {"error": "Not found"})

    def _handle_scrape(self):
        """Full extraction endpoint with comprehensive article parsing."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))

            url = (payload.get("url") or "").strip()
            if not url:
                return self._send_json(400, {"success": False, "error": "'url' is required"})

            escalation_step = None
            source_status_code = 200
            
            # Always use escalation system
            page, escalation_step, source_status_code = _scrape_with_escalation(url, payload)
            
            article = _extract_article_payload(page)
            text = article["text"]
            title = article["title"]
            links = _extract_links(page)
            
            # Detect if result is degraded (returned due to escalation failure)
            is_degraded = escalation_step in ["complete_failure"] or (source_status_code >= 500)

            return self._send_json(
                200,
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
                },
            )
        except Exception as exc:
            logger.error(f"Scrape error: {exc}\n{traceback.format_exc()}")
            return self._send_json(
                500,
                {
                    "success": False,
                    "error": str(exc),
                    "trace": traceback.format_exc(),
                },
            )

    def _handle_scrape_no_filter(self):
        """Raw content endpoint - minimal processing, returns HTML without extraction."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))

            url = (payload.get("url") or "").strip()
            if not url:
                return self._send_json(400, {"success": False, "error": "'url' is required"})

            # Use escalation system for fetching
            page, escalation_step, source_status_code = _scrape_with_escalation(url, payload)
            
            # Minimal processing: extract only title and raw HTML
            title = _extract_title(page)
            html = getattr(page, "html", "") or getattr(page, "text", "") or ""
            
            # Detect if result is degraded
            is_degraded = escalation_step in ["complete_failure"] or (source_status_code >= 500)
            
            return self._send_json(
                200,
                {
                    "success": True,
                    "url": url,
                    "title": title,
                    "html": html[:50000],  # Return raw HTML (truncated for safety)
                    "status_code": source_status_code,
                    "meta": {
                        "escalation_step": escalation_step,
                        "endpoint": "scrape-no-filter",
                        "processing": "minimal",
                        "degraded_mode": is_degraded,
                    },
                    "error": None,
                },
            )
        except Exception as exc:
            logger.error(f"Scrape-no-filter error: {exc}\n{traceback.format_exc()}")
            return self._send_json(
                500,
                {
                    "success": False,
                    "error": str(exc),
                    "trace": traceback.format_exc(),
                },
            )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    host = os.getenv("SCRAPLING_HOST", "0.0.0.0")
    port = int(os.getenv("SCRAPLING_PORT", "8010"))
    logger.info(f"Starting synchronous Scrapling API on {host}:{port}")
    logger.info("Features: HTTP-first escalation, scrape + scrape-no-filter endpoints")
    
    server = HTTPServer((host, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
