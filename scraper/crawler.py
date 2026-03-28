"""
Web Crawler to discover and categorize links from web pages
"""
from typing import List, Dict, Any, Set, Tuple
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from utils import (
    is_valid_url, 
    normalize_url, 
    get_domain, 
    is_same_domain,
    logger
)
from config import settings


class WebCrawler:
    """
    Web crawler to extract and categorize links from pages
    """
    
    def __init__(self, base_url: str):
        """
        Initialize crawler
        
        Args:
            base_url: The base URL for the crawl
        """
        self.base_url = base_url
        self.base_domain = get_domain(base_url)
        
    def crawl(self, html: str, max_links: int = 100) -> Dict[str, Any]:
        """
        Crawl and extract links from HTML content
        
        Args:
            html: HTML content to crawl
            max_links: Maximum number of links to return per category
            
        Returns:
            Dictionary with categorized links
        """
        try:
            soup = BeautifulSoup(html, 'lxml')
            
            # Extract all links
            all_links = self._extract_all_links(soup)
            
            # Categorize links
            categorized = self._categorize_links(all_links)
            
            # Limit results
            for category in categorized:
                if isinstance(categorized[category], list):
                    categorized[category] = categorized[category][:max_links]
            
            # Add statistics
            categorized['statistics'] = self._generate_statistics(categorized)
            
            logger.info(f"Crawled {categorized['statistics']['total_links']} links from {self.base_url}")
            return categorized
            
        except Exception as e:
            logger.error(f"Error crawling page: {e}")
            raise

    def crawl_multilevel(
        self,
        initial_html: str,
        scraper,
        depth: int = 1,
        max_links: int = 100
    ) -> Dict[str, Any]:
        """
        Perform multi-level crawling using BFS strategy
        
        Args:
            initial_html: HTML content of the starting page
            scraper: WebScraper instance for fetching additional pages
            depth: Maximum crawl depth
            max_links: Maximum links per page
        
        Returns:
            Aggregated crawl results with per-level breakdown
        """

        max_depth = min(depth, settings.MAX_CRAWL_DEPTH)
        visited: Set[str] = set()
        queue: List[Tuple[str, int, str]] = [(self.base_url, 0, initial_html)]
        visited.add(self.base_url)

        level_results: Dict[int, List[Dict[str, Any]]] = {}
        aggregate_links: Dict[str, List[Dict[str, str]]] = {
            'internal': [],
            'external': [],
            'media': [],
            'documents': [],
            'social': [],
            'navigation': []
        }

        while queue:
            current_url, current_depth, html_content = queue.pop(0)
            self.base_url = current_url

            try:
                crawl_data = self.crawl(html_content, max_links=max_links)
            except Exception as e:
                logger.error(f"Crawl failed for {current_url}: {e}")
                continue

            level_results.setdefault(current_depth, []).append({
                'page': current_url,
                'links': crawl_data
            })

            # Aggregate links
            for category in aggregate_links.keys():
                aggregate_links[category].extend(crawl_data.get(category, []))

            # Discover next level
            if current_depth < max_depth:
                next_depth = current_depth + 1
                internal_links = crawl_data.get('internal', []) + crawl_data.get('navigation', [])

                for link in internal_links:
                    child_url = link['url']
                    if child_url in visited:
                        continue

                    visited.add(child_url)

                    try:
                        scrape_result = scraper.scrape(child_url)
                        if scrape_result['success']:
                            queue.append((child_url, next_depth, scrape_result['html']))
                        else:
                            logger.warning(f"Skipping {child_url}: scrape unsuccessful")
                    except Exception as scrape_error:
                        logger.warning(f"Skipping {child_url}: {scrape_error}")

        # Deduplicate aggregated links while preserving order
        for category, links in aggregate_links.items():
            seen = set()
            unique_links = []
            for link in links:
                key = link['url']
                if key not in seen:
                    seen.add(key)
                    unique_links.append(link)
            aggregate_links[category] = unique_links[:max_links]

        return {
            'levels': level_results,
            'aggregated_links': aggregate_links,
            'visited_pages': list(visited),
            'statistics': {
                'total_pages': len(visited),
                'max_depth_reached': max(level_results.keys()) if level_results else 0,
                'total_internal_links': len(aggregate_links['internal']),
                'total_external_links': len(aggregate_links['external'])
            }
        }
    
    def _extract_all_links(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """
        Extract all links from the page
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            List of link dictionaries
        """
        links = []
        seen_urls = set()
        
        for link in soup.find_all('a', href=True):
            href = link.get('href', '').strip()
            
            # Normalize URL
            normalized = normalize_url(href, self.base_url)
            
            if normalized and normalized not in seen_urls:
                seen_urls.add(normalized)
                
                links.append({
                    'url': normalized,
                    'text': link.get_text(strip=True),
                    'title': link.get('title', ''),
                    'rel': link.get('rel', [])
                })
        
        return links
    
    def _categorize_links(self, links: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
        """
        Categorize links into internal, external, and special types
        
        Args:
            links: List of link dictionaries
            
        Returns:
            Categorized links dictionary
        """
        internal_links = []
        external_links = []
        media_links = []
        document_links = []
        social_links = []
        navigation_links = []
        
        # Media and document extensions
        media_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.mp4', '.mp3', '.avi', '.mov'}
        document_extensions = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv', '.zip', '.rar'}
        
        # Social media domains
        social_domains = {'facebook.com', 'twitter.com', 'linkedin.com', 'instagram.com', 'youtube.com', 
                         'tiktok.com', 'reddit.com', 'pinterest.com', 'x.com'}
        
        for link in links:
            url = link['url']
            url_lower = url.lower()
            parsed = urlparse(url)
            
            # Check if same domain
            is_internal = is_same_domain(url, self.base_url)
            
            # Check link type
            is_media = any(url_lower.endswith(ext) for ext in media_extensions)
            is_document = any(url_lower.endswith(ext) for ext in document_extensions)
            is_social = any(social in parsed.netloc for social in social_domains)
            
            # Categorize
            if is_media:
                media_links.append(link)
            elif is_document:
                document_links.append(link)
            elif is_social:
                social_links.append(link)
            elif is_internal:
                # Further categorize internal links
                if self._is_navigation_link(link):
                    navigation_links.append(link)
                else:
                    internal_links.append(link)
            else:
                external_links.append(link)
        
        return {
            'internal': internal_links,
            'external': external_links,
            'media': media_links,
            'documents': document_links,
            'social': social_links,
            'navigation': navigation_links
        }
    
    def _is_navigation_link(self, link: Dict[str, str]) -> bool:
        """
        Determine if a link is likely a navigation element
        
        Args:
            link: Link dictionary
            
        Returns:
            True if navigation link
        """
        nav_keywords = {'home', 'about', 'contact', 'menu', 'nav', 'login', 'signup', 
                       'register', 'search', 'cart', 'account', 'profile'}
        
        text_lower = link['text'].lower()
        url_lower = link['url'].lower()
        
        # Check if link text or URL contains navigation keywords
        for keyword in nav_keywords:
            if keyword in text_lower or keyword in url_lower:
                return True
        
        return False
    
    def _generate_statistics(self, categorized: Dict[str, List]) -> Dict[str, int]:
        """
        Generate statistics about crawled links
        
        Args:
            categorized: Categorized links dictionary
            
        Returns:
            Statistics dictionary
        """
        return {
            'total_links': sum(len(v) for k, v in categorized.items() if isinstance(v, list)),
            'internal_count': len(categorized.get('internal', [])),
            'external_count': len(categorized.get('external', [])),
            'media_count': len(categorized.get('media', [])),
            'document_count': len(categorized.get('documents', [])),
            'social_count': len(categorized.get('social', [])),
            'navigation_count': len(categorized.get('navigation', []))
        }
    
    def get_sitemap_urls(self) -> List[str]:
        """
        Try to find and parse sitemap.xml
        
        Returns:
            List of URLs from sitemap
        """
        sitemap_urls = [
            urljoin(self.base_url, '/sitemap.xml'),
            urljoin(self.base_url, '/sitemap_index.xml'),
            urljoin(self.base_url, '/sitemap-index.xml'),
        ]
        
        # This would require additional implementation to actually fetch and parse
        # For now, return the potential sitemap URLs
        return sitemap_urls
    
    def discover_pages(self, html: str, depth: int = 1) -> List[str]:
        """
        Discover all accessible pages from the current page
        
        Args:
            html: HTML content
            depth: Crawl depth (1 = current page only)
            
        Returns:
            List of discovered page URLs
        """
        crawl_result = self.crawl(html)
        
        # Combine internal and navigation links
        pages = []
        for link in crawl_result.get('internal', []) + crawl_result.get('navigation', []):
            url = link['url']
            # Filter out non-page URLs (anchors, etc.)
            if '#' not in url.split('/')[-1]:  # Avoid fragment-only URLs
                pages.append(url)
        
        # Remove duplicates
        pages = list(dict.fromkeys(pages))
        
        logger.info(f"Discovered {len(pages)} pages")
        return pages
