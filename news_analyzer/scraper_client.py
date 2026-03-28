"""
Scraper Client for fetching article content
Integrates with the existing scraper container.
"""
import requests
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone
import time
from bs4 import BeautifulSoup
import re
import pytz

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
        payload = {
            "url": url,
            "force_selenium": force_selenium,
            "auto_detect_js": True
        }
        
        try:
            logger.debug(f"Scraping article: {url}")
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # Check if scraping was successful
                if not data.get('success', False):
                    err = (data.get('error') or 'Unknown error').strip()
                    if 'cloudflare_unsolved' in err.lower():
                        raise CloudflareChallengeUnsolvedError(err)
                    logger.error(f"Scraper returned success=false for {url}: {err}")
                    return None
                
                # Handle the actual scraper response structure per USAGE_GUIDE
                sections = data.get('sections', {})
                content_section = sections.get('content', {})
                metadata = sections.get('metadata', {})
                
                # Get text chunks and join them - THIS IS THE KEY
                text_chunks = content_section.get('text_chunks', [])
                full_content = ' '.join(text_chunks) if text_chunks else ''
                
                # Extract metadata from the scraped content
                published_date = self._extract_published_date_from_metadata(metadata, full_content)
                
                # Extract ForexFactory category from metadata (preferred) or content
                ff_category = metadata.get('category') or self._extract_forexfactory_category(full_content)
                
                # Validate ForexFactory content
                is_valid, validation_reason = self._validate_forexfactory_content(full_content, url)
                
                result = {
                    'content': full_content,
                    'summary': content_section.get('summary', ''),
                    'html': '',  # Not using HTML anymore
                    'status_code': data.get('meta', {}).get('status_code', 200),
                    'metadata': metadata,
                    'published_date': published_date,
                    'forexfactory_category': ff_category,
                    'is_valid': is_valid,
                    'validation_reason': validation_reason,
                    'url': url,
                    'stats': data.get('stats', {}),
                    'method': data.get('method'),
                    'word_count': data.get('stats', {}).get('word_count', 0)
                }
                
                logger.debug(f"Successfully scraped {url} ({len(result['content'])} chars, {result['word_count']} words, method: {result['method']})")
                return result
            else:
                logger.error(f"Scraper returned status {response.status_code} for {url}")
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
        Extract the published date from metadata or content.
        
        ForexFactory Story Stats format: "Jan 2, 2026 10:50pm" or "Jan 4, 9:11pm(18 hr ago)"
        Note: ForexFactory times appear to be in IST (UTC+5:30), need to convert to UTC
        
        Args:
            metadata: Metadata dict from scraper
            content: Cleaned text content
        
        Returns:
            datetime object in UTC or None
        """
        try:
            # PRIORITY 1: Extract from metadata['posted_date'] (from cleaner.py FF extraction)
            posted_date = metadata.get('posted_date')
            if posted_date:
                # Clean up - remove "(X hr ago)" suffix if present
                posted_date = re.sub(r'\([^)]+\)$', '', posted_date).strip()
                
                # Try parsing "Jan 2, 2026 10:50pm" format (with year)
                try:
                    parsed_date = datetime.strptime(posted_date, "%b %d, %Y %I:%M%p")
                    
                    # Convert from IST to UTC
                    ist = pytz.timezone('Asia/Kolkata')
                    parsed_date_ist = ist.localize(parsed_date)
                    parsed_date_utc = parsed_date_ist.astimezone(pytz.UTC)
                    
                    logger.debug(f"Date: {posted_date} IST -> {parsed_date_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    return parsed_date_utc.replace(tzinfo=None)  # Return naive UTC datetime
                except ValueError:
                    pass
                
                # Try parsing without year (add current year)
                try:
                    current_year = datetime.now().year
                    posted_date_with_year = f"{posted_date} {current_year}"
                    parsed_date = datetime.strptime(posted_date_with_year, "%b %d, %I:%M%p %Y")
                    
                    # Convert from IST to UTC
                    ist = pytz.timezone('Asia/Kolkata')
                    parsed_date_ist = ist.localize(parsed_date)
                    parsed_date_utc = parsed_date_ist.astimezone(pytz.UTC)
                    
                    logger.debug(f"Date: {posted_date} IST -> {parsed_date_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    return parsed_date_utc.replace(tzinfo=None)
                except ValueError as e:
                    logger.debug(f"Failed to parse metadata posted_date: {posted_date}, error: {e}")
            
            # PRIORITY 2: Extract from content (fallback for older code path)
            if content:
                # Try with year first
                posted_pattern = r'Posted:\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}(?:am|pm))'
                matches = re.search(posted_pattern, content, re.IGNORECASE)
                
                if matches:
                    date_str = matches.group(1)
                    try:
                        # Parse "Jan 2, 2026 10:50pm" format
                        parsed_date = datetime.strptime(date_str, "%b %d, %Y %I:%M%p")
                        
                        # ForexFactory times are in IST (UTC+5:30), convert to UTC
                        ist = pytz.timezone('Asia/Kolkata')
                        parsed_date_ist = ist.localize(parsed_date)
                        parsed_date_utc = parsed_date_ist.astimezone(pytz.UTC)
                        
                        logger.info(f"Extracted date from Story Stats: {date_str} IST -> {parsed_date_utc} UTC")
                        return parsed_date_utc.replace(tzinfo=None)  # Return naive UTC datetime
                    except ValueError as e:
                        logger.debug(f"Failed to parse Story Stats date with year: {date_str}, error: {e}")
                
                # Fallback: Try without year (add current year)
                posted_pattern_no_year = r'Posted:\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{1,2}:\d{2}(?:am|pm))'
                matches = re.search(posted_pattern_no_year, content, re.IGNORECASE)
                
                if matches:
                    date_str = matches.group(1)
                    current_year = datetime.now().year
                    date_str_with_year = f"{date_str} {current_year}"
                    
                    try:
                        parsed_date = datetime.strptime(date_str_with_year, "%b %d, %I:%M%p %Y")
                        
                        # Convert from IST to UTC
                        ist = pytz.timezone('Asia/Kolkata')
                        parsed_date_ist = ist.localize(parsed_date)
                        parsed_date_utc = parsed_date_ist.astimezone(pytz.UTC)
                        
                        logger.info(f"Extracted date from Story Stats (no year): {date_str} IST -> {parsed_date_utc} UTC")
                        return parsed_date_utc.replace(tzinfo=None)  # Return naive UTC datetime
                    except ValueError as e:
                        logger.debug(f"Failed to parse Story Stats date without year: {date_str}, error: {e}")
            
            # PRIORITY 2: Try common metadata fields
            if not metadata:
                logger.warning("No metadata and couldn't extract from content")
                return None
            
            date_fields = [
                'publish_date',
                'date',
                'published',
                'created',
                'pubdate'
            ]
            
            for field in date_fields:
                date_str = metadata.get(field)
                if date_str:
                    parsed_date = self._parse_date_string(date_str)
                    if parsed_date:
                        logger.info(f"Extracted date from metadata field '{field}': {parsed_date}")
                        return parsed_date
            
            # Try parsing description if it contains date patterns
            description = metadata.get('description', '')
            if description:
                date_patterns = [
                    r'(\w{3,9}\s+\d{1,2},\s+\d{4})',  # "January 1, 2026"
                    r'(\d{1,2}\s+\w{3,9}\s+\d{4})',     # "1 January 2026"
                    r'(\d{4}-\d{2}-\d{2})',              # "2026-01-01"
                ]
                
                for pattern in date_patterns:
                    matches = re.findall(pattern, description)
                    if matches:
                        for match in matches:
                            parsed_date = self._parse_date_string(match)
                            if parsed_date:
                                logger.info(f"Extracted date from description: {parsed_date}")
                                return parsed_date
            
            # Fallback: try first 500 chars of content
            if content:
                date_patterns = [
                    r'(\w{3,9}\s+\d{1,2},\s+\d{4})',
                    r'(\d{1,2}\s+\w{3,9}\s+\d{4})',
                    r'(\d{4}-\d{2}-\d{2})',
                ]
                
                for pattern in date_patterns:
                    matches = re.findall(pattern, content[:500])
                    if matches:
                        for match in matches:
                            parsed_date = self._parse_date_string(match)
                            if parsed_date:
                                logger.info(f"Extracted date from content: {parsed_date}")
                                return parsed_date
            
            logger.warning("Could not extract published date")
            return None
            
        except Exception as e:
            logger.error(f"Error extracting published date: {e}")
            return None
    
    def _extract_published_date(self, html: str, content: str) -> Optional[datetime]:
        """
        Extract the published date from article HTML or content.
        
        ForexFactory typically includes dates in various formats.
        We'll try multiple extraction methods.
        
        Args:
            html: Raw HTML content
            content: Cleaned text content
        
        Returns:
            datetime object or None
        """
        if not html:
            return None
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Method 1: Look for meta tags (Open Graph, Twitter Cards, etc.)
            meta_tags = [
                ('meta', {'property': 'article:published_time'}),
                ('meta', {'name': 'publish-date'}),
                ('meta', {'name': 'date'}),
                ('meta', {'property': 'og:published_time'}),
                ('time', {'datetime': True}),
            ]
            
            for tag_name, attrs in meta_tags:
                tag = soup.find(tag_name, attrs)
                if tag:
                    # Extract datetime from content or datetime attribute
                    date_str = tag.get('content') or tag.get('datetime')
                    if date_str:
                        parsed_date = self._parse_date_string(date_str)
                        if parsed_date:
                            logger.info(f"Extracted date from {tag_name}: {parsed_date}")
                            return parsed_date
            
            # Method 2: Look for common date patterns in HTML classes/ids
            date_elements = soup.find_all(['span', 'div', 'p', 'time'], 
                                         class_=re.compile(r'date|time|publish', re.I))
            
            for elem in date_elements:
                date_str = elem.get_text(strip=True)
                if date_str:
                    parsed_date = self._parse_date_string(date_str)
                    if parsed_date:
                        logger.info(f"Extracted date from element: {parsed_date}")
                        return parsed_date
            
            # Method 3: Look for date patterns in content text
            # Common patterns: "Jan 01, 2026", "January 1, 2026", "2026-01-01", etc.
            date_patterns = [
                r'(\w{3,9}\s+\d{1,2},\s+\d{4})',  # "January 1, 2026"
                r'(\d{1,2}\s+\w{3,9}\s+\d{4})',     # "1 January 2026"
                r'(\d{4}-\d{2}-\d{2})',              # "2026-01-01"
                r'(\d{2}/\d{2}/\d{4})',              # "01/01/2026"
            ]
            
            for pattern in date_patterns:
                matches = re.findall(pattern, content[:500])  # Check first 500 chars
                if matches:
                    for match in matches:
                        parsed_date = self._parse_date_string(match)
                        if parsed_date:
                            logger.info(f"Extracted date from content pattern: {parsed_date}")
                            return parsed_date
            
            logger.warning("Could not extract published date from article")
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
        formats = [
            '%Y-%m-%dT%H:%M:%S%z',  # ISO 8601 with timezone
            '%Y-%m-%dT%H:%M:%S',     # ISO 8601 without timezone
            '%Y-%m-%d',               # YYYY-MM-DD
            '%B %d, %Y',              # January 01, 2026
            '%b %d, %Y',              # Jan 01, 2026
            '%d %B %Y',               # 01 January 2026
            '%d %b %Y',               # 01 Jan 2026
            '%m/%d/%Y',               # 01/01/2026
            '%d/%m/%Y',               # 01/01/2026 (European)
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
        
        return None
    
    def test_connection(self) -> bool:
        """Test if the scraper service is reachable"""
        health_url = f"{self.base_url}/health"
        # Keep this check fast: the scraper can be busy (Selenium) and may not respond
        # quickly even if it's otherwise functioning.
        timeout = 2

        for attempt in range(1, 3):
            try:
                response = self.session.get(health_url, timeout=timeout)
                if response.status_code == 200:
                    logger.info("Scraper service is healthy")
                    return True
                logger.warning(f"Scraper service returned status {response.status_code}")
                return False
            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Scraper health check failed (attempt {attempt}/2): {e}")
                    time.sleep(1)
                    continue
                logger.error(f"Cannot reach scraper service: {e}")
                return False
    
    def _extract_forexfactory_category(self, content: str) -> Optional[str]:
        """
        Extract ForexFactory category from content.
        Categories like "Low Impact Breaking News", "Fundamental Analysis", etc.
        
        Args:
            content: Full scraped content
        
        Returns:
            Category string or None
        """
        if not content:
            return None
        
        # Look for "Category: <category name>" pattern
        # Common in ForexFactory's Story Stats section
        category_pattern = r'Category:\s*([^\n]+?)(?:\s+Comments:|$)'
        matches = re.search(category_pattern, content, re.IGNORECASE)
        
        if matches:
            category = matches.group(1).strip()
            logger.info(f"Extracted ForexFactory category: {category}")
            return category
        
        return None
    
    def _validate_forexfactory_content(self, content: str, url: str) -> tuple[bool, str]:
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
        if not content:
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
        
        # Check for very short content (likely an error)
        if len(content.strip()) < 100:
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
