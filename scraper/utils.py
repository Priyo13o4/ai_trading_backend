"""Utility functions for the web scraper and discovery stack."""
import io
import logging
import random
import re
import time
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, urljoin

try:
    import brotli  # Optional dependency for Brotli decoding
except ImportError:  # pragma: no cover - optional path
    brotli = None

from config import settings
from requests import Response
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


DOCUMENT_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv', '.zip', '.rar'
}


def get_random_user_agent() -> str:
    """Get a random user agent from the pool"""
    return random.choice(settings.USER_AGENTS)


_STABLE_USER_AGENT: Optional[str] = None


def get_stable_user_agent() -> str:
    """Return a stable User-Agent for this process.

    Rotating UAs per request can look suspicious when cookies/TLS fingerprints
    remain stable. A consistent UA usually blends in better.
    """
    global _STABLE_USER_AGENT
    if _STABLE_USER_AGENT is None:
        _STABLE_USER_AGENT = get_random_user_agent()
    return _STABLE_USER_AGENT



def is_valid_url(url: str) -> bool:
    """
    Validate if a URL is properly formatted
    
    Args:
        url: URL string to validate
        
    Returns:
        bool: True if valid, False otherwise
    """
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False


def normalize_url(url: str, base_url: str = None) -> Optional[str]:
    """
    Normalize and clean a URL
    
    Args:
        url: URL to normalize
        base_url: Base URL for relative paths
        
    Returns:
        Normalized URL or None if invalid
    """
    try:
        # Remove whitespace
        url = url.strip()
        
        # Handle relative URLs
        if base_url and not url.startswith(('http://', 'https://', '//')):
            url = urljoin(base_url, url)
        elif url.startswith('//'):
            url = f"https:{url}"
        
        # Remove fragments
        url = url.split('#')[0]
        
        # Validate
        if is_valid_url(url):
            return url
        return None
    except Exception as e:
        logger.error(f"Error normalizing URL {url}: {e}")
        return None


def get_domain(url: str) -> Optional[str]:
    """
    Extract domain from URL
    
    Args:
        url: URL string
        
    Returns:
        Domain name or None
    """
    try:
        parsed = urlparse(url)
        return parsed.netloc
    except Exception:
        return None


def is_same_domain(url1: str, url2: str) -> bool:
    """
    Check if two URLs belong to the same domain
    
    Args:
        url1: First URL
        url2: Second URL
        
    Returns:
        bool: True if same domain
    """
    return get_domain(url1) == get_domain(url2)


def clean_text(text: str) -> str:
    """
    Clean and normalize text content
    
    Args:
        text: Raw text
        
    Returns:
        Cleaned text
    """
    if not text:
        return ""
    
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    
    # Remove leading/trailing whitespace
    text = text.strip()
    
    return text


def decode_response_content(response: Response) -> str:
    """Decode HTTP response content, handling Brotli when available."""
    encoding_header = response.headers.get('Content-Encoding', '').lower()

    if 'br' in encoding_header:
        if not brotli:
            logger.warning("Received Brotli content but 'brotli' package is not installed; using raw text fallback")
            return response.text

        try:
            decoded_bytes = brotli.decompress(response.content)
            charset = response.encoding or response.apparent_encoding or 'utf-8'
            return decoded_bytes.decode(charset, errors='replace')
        except Exception as exc:  # pragma: no cover - safety net
            logger.warning(f"Failed to decode Brotli content: {exc}; falling back to response.text")
            return response.text

    return response.text


def chunk_text(text: str, max_length: int) -> List[str]:
    """Split text into manageable chunks for downstream processing"""
    if not text:
        return []

    chunks = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + max_length, length)

        # Try to cut at sentence boundary if possible
        chunk = text[start:end]
        last_period = chunk.rfind('.')
        if last_period != -1 and end < length:
            end = start + last_period + 1
            chunk = text[start:end]

        chunks.append(chunk.strip())
        start = end

    return [c for c in chunks if c]


def generate_summary(text: str, max_sentences: int) -> str:
    """Generate a naive summary using the first N sentences"""
    if not text:
        return ""

    sentences = re.split(r'(?<=[.!?]) +', text)
    summary = ' '.join(sentences[:max_sentences])
    return summary.strip()


def is_document_url(url: str) -> bool:
    """Check whether a URL points to a supported document type."""
    if not url:
        return False
    lowered = url.split('?', 1)[0].lower()
    return any(lowered.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


def fetch_document_preview(url: str) -> Dict[str, Any]:
    """Fetch a lightweight preview for supported document links."""
    result: Dict[str, Any] = {
        'url': url,
        'success': False,
        'error': None,
        'content_type': None,
        'size_bytes': None,
        'summary': '',
        'text_preview': ''
    }

    try:
        headers = {
            'User-Agent': get_stable_user_agent(),
            'Accept': '*/*'
        }
        with requests.get(url, headers=headers, timeout=settings.DEFAULT_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').lower()
            content_length_header = response.headers.get('Content-Length')
            size_limit = settings.DOCUMENT_MAX_BYTES

            if content_length_header and int(content_length_header) > size_limit:
                result['error'] = 'document_too_large'
                result['size_bytes'] = int(content_length_header)
                return result

            content = response.content
            size_bytes = len(content)
            if size_bytes > size_limit:
                result['error'] = 'document_too_large'
                result['size_bytes'] = size_bytes
                return result

        result['size_bytes'] = size_bytes
        result['content_type'] = content_type or 'application/octet-stream'

        if 'pdf' in result['content_type'] or url.lower().endswith('.pdf'):
            try:
                from pdfminer.high_level import extract_text

                start_time = time.perf_counter()
                text = extract_text(io.BytesIO(content), maxpages=3)
                elapsed = time.perf_counter() - start_time
                logger.info(f"Extracted PDF preview from {url} in {elapsed:.2f}s")

                clean_text_value = clean_text(text)[:2000]
                result['text_preview'] = clean_text_value
                result['summary'] = generate_summary(clean_text_value, min(2, settings.SUMMARY_SENTENCE_COUNT))
                result['success'] = True
                return result
            except Exception as exc:  # pragma: no cover - pdf fallback path
                logger.warning(f"Failed to parse PDF {url}: {exc}")
                result['error'] = f'pdf_parse_error: {exc}'
                return result

        # Unsupported document type for preview; surface metadata only
        result['error'] = 'unsupported_document_type'
        return result

    except Exception as exc:
        logger.warning(f"Failed to fetch document preview for {url}: {exc}")
        result['error'] = str(exc)
        return result


def extract_metadata_from_html(soup) -> Dict[str, Any]:
    """
    Extract metadata from HTML soup object
    
    Args:
        soup: BeautifulSoup object
        
    Returns:
        Dictionary of metadata
    """
    metadata = {
        'title': '',
        'description': '',
        'keywords': '',
        'author': '',
        'og_title': '',
        'og_description': '',
        'og_image': ''
    }
    
    try:
        # Title
        title_tag = soup.find('title')
        if title_tag:
            metadata['title'] = clean_text(title_tag.get_text())
        
        # Meta tags
        meta_tags = soup.find_all('meta')
        for tag in meta_tags:
            name = tag.get('name', '').lower()
            property = tag.get('property', '').lower()
            content = tag.get('content', '')
            
            if name == 'description':
                metadata['description'] = clean_text(content)
            elif name == 'keywords':
                metadata['keywords'] = clean_text(content)
            elif name == 'author':
                metadata['author'] = clean_text(content)
            elif property == 'og:title':
                metadata['og_title'] = clean_text(content)
            elif property == 'og:description':
                metadata['og_description'] = clean_text(content)
            elif property == 'og:image':
                metadata['og_image'] = content
                
    except Exception as e:
        logger.error(f"Error extracting metadata: {e}")
    
    return metadata


def format_error_response(error: Exception, url: str) -> Dict[str, Any]:
    """
    Format error response for API
    
    Args:
        error: Exception object
        url: URL that caused the error
        
    Returns:
        Formatted error dictionary
    """
    return {
        'success': False,
        'error': str(error),
        'error_type': type(error).__name__,
        'url': url
    }


def estimate_js_requirement(html_content: str) -> bool:
    """
    Estimate if a page requires JavaScript rendering
    
    Args:
        html_content: Raw HTML content
        
    Returns:
        bool: True if likely needs JS rendering
    """
    # Check for common indicators
    js_indicators = [
        'react',
        'angular',
        'vue',
        '__NEXT_DATA__',
        'nuxt',
        'gatsby',
        'webpack',
        'application/json',
        'window.__INITIAL',
    ]
    
    html_lower = html_content.lower()
    
    # Count indicators
    indicator_count = sum(1 for indicator in js_indicators if indicator in html_lower)
    
    # If multiple indicators found, likely needs JS
    return indicator_count >= 2
