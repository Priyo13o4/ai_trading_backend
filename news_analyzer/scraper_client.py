"""
Scraper Client for fetching article content
Integrates with the existing scraper container.
"""
import requests
import logging
from typing import Dict, Optional
from datetime import datetime, timezone, timedelta
import re

from config import Config

logger = logging.getLogger(__name__)


class CloudflareChallengeUnsolvedError(RuntimeError):
    """Raised when the headful scraper could not clear Cloudflare in time."""


class ScraperClient:
    """Client for interacting with the scraper service"""
    
    def __init__(self, base_url: str = None, timeout: int = None):
        self.base_url = base_url or Config.SCRAPER_BASE_URL
        self.timeout = timeout or Config.SCRAPER_TIMEOUT
        self.session = requests.Session()

        # Give the session enough pooled connections for threaded usage.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=50,
            pool_maxsize=50,
            max_retries=0,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def scrape_article(self, url: str, force_selenium: bool = True) -> Optional[Dict]:
        """
        Scrape a news article using the scraper service.
        
        Args:
            url: The article URL to scrape
            force_selenium: Force use of Selenium for JS-heavy sites
        
        Returns:
            Dict with 'content', 'html', 'status_code', 'metadata'
            None if scraping failed
        """
        endpoint = f"{self.base_url}/api/v1/scrape"
        preflight_payload = {
            "url": url,
            "mode": "http",
        }

        stealth_payloads = [
            {
                "url": url,
                "mode": "stealthy",
                "headless": True,
                "solve_cloudflare": False,
                "network_idle": False,
            },
            {
                "url": url,
                "mode": "stealthy",
                "headless": True,
                "solve_cloudflare": True,
                "google_search": True,
                "network_idle": False,
                "session_reset": True,
                "reuse_session": False,
            },
        ]
        
        try:
            logger.debug(f"Scraping article: {url}")
            data = None

            # Fast preflight via plain HTTP mode. This quickly identifies hard source
            # failures (e.g., 400/404) and avoids expensive browser startup.
            try:
                preflight_response = self.session.post(
                    endpoint,
                    json=preflight_payload,
                    timeout=min(self.timeout, 30),
                )
                if preflight_response.status_code == 200:
                    preflight_data = preflight_response.json()
                    if preflight_data.get('success', False):
                        preflight_source_status = int(
                            preflight_data.get('meta', {}).get('status_code', 200) or 200
                        )
                        if preflight_source_status >= 400:
                            logger.debug(
                                f"HTTP preflight returned source status {preflight_source_status} for {url}; "
                                "skipping stealthy fetch"
                            )
                            data = preflight_data
            except Exception:
                # Preflight is opportunistic; fall through to stealthy fetches.
                data = None

            if data is None:
                for attempt_idx, payload in enumerate(stealth_payloads):
                    response = self.session.post(
                        endpoint,
                        json=payload,
                        timeout=self.timeout
                    )

                    if response.status_code != 200:
                        err_payload = None
                        try:
                            err_payload = response.json()
                        except Exception:
                            err_payload = None

                        if isinstance(err_payload, dict):
                            err_msg = err_payload.get('error') or err_payload.get('message') or 'unknown error'
                            logger.error(
                                f"Scraper returned status {response.status_code} for {url}: {err_msg}"
                            )
                        else:
                            logger.error(f"Scraper returned status {response.status_code} for {url}")
                        return None

                    candidate = response.json()
                    if not candidate.get('success', False):
                        err = (candidate.get('error') or 'Unknown error').strip()
                        if 'cloudflare_unsolved' in err.lower():
                            if attempt_idx == 0:
                                logger.info(
                                    f"Cloudflare gate suspected for {url}; retrying with solver enabled"
                                )
                                continue
                            raise CloudflareChallengeUnsolvedError(err)
                        logger.error(f"Scraper returned success=false for {url}: {err}")
                        return None

                    source_status_code = int(candidate.get('meta', {}).get('status_code', 200) or 200)
                    if attempt_idx == 0 and source_status_code in {403, 429, 503}:
                        logger.info(
                            f"Source HTTP {source_status_code} for {url}; retrying with Cloudflare solver"
                        )
                        continue

                    data = candidate
                    break

            if data is not None:

                article = data.get('article', {}) or {}
                sections = data.get('sections', {}) or {}
                section_meta = sections.get('metadata', {}) or {}
                section_content = sections.get('content', {}) or {}

                full_content = (data.get('text') or '').strip()
                if not full_content:
                    chunks = section_content.get('text_chunks') or []
                    if isinstance(chunks, list):
                        full_content = ' '.join(
                            chunk.strip() for chunk in chunks if isinstance(chunk, str) and chunk.strip()
                        ).strip()

                title = (
                    data.get('title')
                    or article.get('title')
                    or section_meta.get('headline')
                    or section_meta.get('title')
                    or ''
                )

                news_type = article.get('news_type') or section_meta.get('news_type')
                impact = article.get('impact') or section_meta.get('impact')
                date_time_utc = article.get('date_time_utc') or section_meta.get('date_time_utc')
                date_time = article.get('date_time') or section_meta.get('date_time')
                links = data.get('links') or []

                if not isinstance(links, list):
                    links = []

                method = data.get('mode') or data.get('method')

                word_count = data.get('word_count')
                if not isinstance(word_count, int):
                    word_count = len(full_content.split()) if full_content else 0

                metadata = {
                    'title': title,
                    'headline': title,
                    'posted_date': date_time_utc or date_time or '',
                    'publish_date': date_time_utc or '',
                    'posted_date_utc': date_time_utc or '',
                    'publish_date_utc': date_time_utc or '',
                    'posted_at_text': article.get('posted_at_text') or section_meta.get('posted_at_text') or '',
                    'category': self._build_category(news_type, impact),
                    'impact': impact,
                    'news_type': news_type,
                    'posted_by': article.get('posted_by') or section_meta.get('posted_by'),
                    'posted_ago_text': article.get('posted_ago_text') or section_meta.get('posted_ago_text'),
                    'linked_events': article.get('linked_events') or section_meta.get('linked_events') or [],
                    'links': links,
                }

                source_status_code = int(data.get('meta', {}).get('status_code', 200) or 200)
                
                # Extract metadata from the scraped content
                published_date = self._extract_published_date_from_metadata(metadata, full_content)
                
                # Category now comes directly from scraper metadata.
                ff_category = metadata.get('category')
                
                # Validate ForexFactory content.
                if source_status_code >= 400:
                    is_valid, validation_reason = False, f"Source HTTP {source_status_code}"
                else:
                    is_valid, validation_reason = self._validate_forexfactory_content(
                        full_content,
                        url,
                        title=metadata.get('title'),
                        links=links,
                    )
                
                result = {
                    'content': full_content,
                    'summary': full_content[:320],
                    'html': '',  # Not using HTML anymore
                    'status_code': source_status_code,
                    'metadata': metadata,
                    'published_date': published_date,
                    'forexfactory_category': ff_category,
                    'is_valid': is_valid,
                    'validation_reason': validation_reason,
                    'url': url,
                    'stats': {
                        'word_count': word_count,
                        'char_count': len(full_content),
                    },
                    'method': method,
                    'word_count': word_count
                }
                
                logger.debug(f"Successfully scraped {url} ({len(result['content'])} chars, {result['word_count']} words, method: {result['method']})")
                return result
            else:
                logger.error(f"Scraper did not return usable data for {url}")
                return None
                
        except requests.exceptions.Timeout:
            logger.error(f"Timeout scraping {url}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error scraping {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error scraping {url}: {e}")
            return None
    
    def _extract_published_date_from_metadata(self, metadata: dict, content: str) -> Optional[datetime]:
        """
        Extract published date using explicit scraper metadata fields.
        
        Args:
            metadata: Metadata dict from scraper
            content: Cleaned text content
        
        Returns:
            datetime object in UTC or None
        """
        try:
            if not metadata:
                return None

            date_fields = [
                'publish_date',
                'posted_date',
                'publish_date_utc',
                'posted_date_utc',
            ]

            for field in date_fields:
                date_str = metadata.get(field)
                if not date_str:
                    continue

                parsed_date = self._parse_date_string(str(date_str))
                if not parsed_date:
                    continue

                if parsed_date.tzinfo is not None:
                    parsed_date = parsed_date.astimezone(timezone.utc).replace(tzinfo=None)
                return parsed_date

            return None
            
        except Exception as e:
            logger.error(f"Error extracting published date: {e}")
            return None
    
    def _parse_date_string(self, date_str: str) -> Optional[datetime]:
        """
        Try to parse a date string using multiple formats.
        
        Args:
            date_str: Date string to parse
        
        Returns:
            datetime object or None
        """
        if not date_str or len(date_str) < 8:
            return None
        
        # Common date formats
        normalized = date_str.strip()
        if normalized.endswith('Z'):
            normalized = normalized[:-1] + '+00:00'

        formats = [
            ('%Y-%m-%dT%H:%M:%S.%f%z', True),  # ISO 8601 with milliseconds and timezone
            ('%Y-%m-%dT%H:%M:%S%z', True),     # ISO 8601 with timezone
            ('%Y-%m-%dT%H:%M:%S', True),       # ISO 8601 without timezone
            ('%Y-%m-%d', True),                # YYYY-MM-DD
            ('%b %d, %Y %I:%M%p', True),       # Apr 05, 2026 06:30am
            ('%b %d, %I:%M%p', False),         # Apr 05, 6:30am (no year)
            ('%B %d, %Y', True),               # January 01, 2026
            ('%b %d, %Y', True),               # Jan 01, 2026
            ('%d %B %Y', True),                # 01 January 2026
            ('%d %b %Y', True),                # 01 Jan 2026
            ('%m/%d/%Y', True),                # 01/01/2026
            ('%d/%m/%Y', True),                # 01/01/2026 (European)
        ]

        now_utc = datetime.utcnow()
        for fmt, has_year in formats:
            try:
                parsed = datetime.strptime(normalized, fmt)
                if has_year:
                    return parsed

                guessed = parsed.replace(year=now_utc.year)
                # If guess is too far in the future, prefer previous year.
                if guessed - now_utc > timedelta(days=45):
                    guessed = guessed.replace(year=guessed.year - 1)
                return guessed
            except ValueError:
                continue
        
        return None
    
    def _build_category(self, news_type: Optional[str], impact: Optional[str]) -> Optional[str]:
        clean_type = (news_type or '').strip()
        clean_impact = (impact or '').strip()

        if clean_type.lower() == 'breaking' and clean_impact:
            return f"{clean_impact} Impact Breaking News"
        if clean_type and clean_impact:
            return f"{clean_type} ({clean_impact} Impact)"
        if clean_type:
            return clean_type
        if clean_impact:
            return f"{clean_impact} Impact"
        return None
    
    def _validate_forexfactory_content(
        self,
        content: str,
        url: str,
        title: Optional[str] = None,
        links: Optional[list[str]] = None,
    ) -> tuple[bool, str]:
        """
        Validate ForexFactory content for common errors and edge cases.
        
        Checks for:
        - 404/not found pages
        - Gaps or missing stories
        - Sub-websites (metalmine.com, cryptocraft.io are allowed)
        
        Args:
            content: Scraped article content
            url: Article URL
        
        Returns:
            Tuple of (is_valid: bool, reason: str)
        """
        title_lower = (title or '').strip().lower()

        if not content:
            if 'notice' in title_lower and 'forex factory' in title_lower:
                return False, "Junior Member restriction page"
            return False, "Empty content"
        
        content_lower = content.lower()

        # Cloudflare / bot verification pages (transient).
        cloudflare_indicators = [
            "verify you are human",
            "needs to review the security of your connection",
            "just a moment",
            "/cdn-cgi/challenge-platform",
            "__cf_chl_",
        ]
        if any(indicator in content_lower for indicator in cloudflare_indicators):
            return False, "Cloudflare challenge page"
        
        # Check for specific ForexFactory error page patterns
        # 1. Story Not Found page
        if 'story not found' in content_lower and "sorry, you've requested an invalid page" in content_lower:
            return False, "Story Not Found page"
        
        # 2. Junior Member restriction page
        if 'junior member' in content_lower and 'you cannot perform this action' in content_lower:
            return False, "Junior Member restriction page"
        
        # 3. Generic error indicators (more specific patterns)
        error_indicators = [
            ('flexbox noflex error', 'ForexFactory error page'),
            ('404', '404 error'),
            ('page not found', 'page not found'),
            ('page does not exist', 'page does not exist')
        ]
        
        for indicator, label in error_indicators:
            if indicator in content_lower:
                return False, f"Error page detected: {label}"
        
        # Social-embed stories can be concise even when valid.
        social_hosts = ('twitter.com', 'x.com', 'truthsocial.com')
        has_social_embed_link = any(
            any(host in (link or '').lower() for host in social_hosts)
            for link in (links or [])
            if isinstance(link, str)
        )

        min_len = 30 if has_social_embed_link else 100
        if len(content.strip()) < min_len:
            return False, f"Content too short ({len(content)} chars)"
        
        # Check for allowed ForexFactory domains and sub-sites
        # ForexFactory redirects some content to these domains
        allowed_domains = [
            'forexfactory.com',
            'metalsmine.com',     # ForexFactory's metals sub-site (corrected spelling)
            'cryptocraft.com'     # ForexFactory's crypto sub-site (corrected domain)
        ]
        
        # Extract domain from URL
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        
        # Allow any allowed domain
        is_allowed_domain = any(allowed in domain for allowed in allowed_domains)
        
        if not is_allowed_domain:
            return False, f"Domain not in allowed list: {domain}"
        
        # All checks passed
        return True, "Valid"


    def close(self):
        """Close the session"""
        self.session.close()
