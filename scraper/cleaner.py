"""
Content cleaner and preprocessor for scraped data
"""
import re
from typing import Dict, Any, List
from bs4 import BeautifulSoup
import html2text
from config import settings
from utils import (
    clean_text,
    extract_metadata_from_html,
    chunk_text,
    generate_summary,
    normalize_url,
    is_document_url,
    fetch_document_preview,
    logger,
)


class ContentCleaner:
    """
    Clean and preprocess scraped content
    """
    
    def __init__(self):
        self.html2text = html2text.HTML2Text()
        self.html2text.ignore_links = False
        self.html2text.ignore_images = False
        self.html2text.ignore_emphasis = False
        self.html2text.body_width = 0  # Don't wrap text
        
    def clean_html_content(self, html: str, base_url: str | None = None) -> Dict[str, Any]:
        """
        Clean and extract content from HTML
        
        Args:
            html: Raw HTML content
            
        Returns:
            Dictionary with cleaned content
        """
        try:
            soup = BeautifulSoup(html, 'lxml')
            
            # Keep a copy for metadata extraction (before removing elements)
            soup_copy_for_metadata = BeautifulSoup(html, 'lxml')
            
            # Remove unwanted elements
            self._remove_unwanted_elements(soup)
            
            # Extract metadata from full page (includes Story Stats)
            metadata = extract_metadata_from_html(soup_copy_for_metadata)
            
            # Add ForexFactory-specific metadata from Story Stats
            ff_metadata = self._extract_forexfactory_metadata(soup_copy_for_metadata)
            metadata.update(ff_metadata)
            
            # Extract main content
            main_content = self._extract_main_content(soup)
            
            # Extract text in different formats (from main_content, not entire soup!)
            text_content = self._extract_text(main_content)
            markdown_content = self._convert_to_markdown(main_content)
            
            # Extract structured data (from main_content)
            headings = self._extract_headings(main_content)
            links = self._extract_links(main_content, base_url)
            images = self._extract_images(main_content, base_url)
            tables = self._extract_tables(main_content)
            documents = self._extract_documents(links)
            
            text_chunks = chunk_text(
                text_content,
                settings.MAX_TEXT_SECTION_LENGTH if hasattr(settings, 'MAX_TEXT_SECTION_LENGTH') else 2000
            )

            result = {
                'sections': {
                    'metadata': metadata,
                    'content': {
                        'summary': generate_summary(text_content, getattr(settings, 'SUMMARY_SENTENCE_COUNT', 3)),
                        'text_chunks': text_chunks,
                        'markdown': markdown_content
                    },
                    'structure': {
                        'headings': headings,
                        'tables': tables
                    },
                    'resources': {
                        'links': links,
                        'images': images,
                        'documents': documents
                    }
                },
                'stats': {
                    'word_count': len(text_content.split()),
                    'char_count': len(text_content),
                    'chunk_count': len(text_chunks)
                }
            }
            
            logger.info(
                f"Content cleaned successfully. Word count: {result['stats'].get('word_count', 'unknown')}"
            )
            return result
            
        except Exception as e:
            logger.error(f"Error cleaning HTML content: {e}")
            raise
    
    def _remove_unwanted_elements(self, soup: BeautifulSoup):
        """Remove scripts, styles, and other unwanted elements"""
        unwanted_tags = [
            'script', 'style', 'noscript', 'iframe', 'svg',
            'header', 'footer', 'nav', 'aside', 'form'
        ]
        
        for tag in unwanted_tags:
            for element in soup.find_all(tag):
                element.decompose()
        
        # Remove comments
        for comment in soup.findAll(text=lambda text: isinstance(text, str) and text.strip().startswith('<!--')):
            comment.extract()
    
    def _extract_main_content(self, soup: BeautifulSoup) -> BeautifulSoup:
        """
        Try to extract the main content area
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            BeautifulSoup object with main content
        """
        # PRIORITY 1: ForexFactory-specific extraction
        # Extract from <li class="news__article"> to avoid comments/ads
        ff_article = soup.select_one('li.news__article')
        if ff_article:
            # Handle tweet articles - extract all tweet headlines from data-index sections
            if 'news__article--tweet' in ff_article.get('class', []):
                logger.info("Extracting tweet article content")
                tweet_texts = []
                
                # Find the parent news__story container
                news_story = soup.select_one('div.flexBox.noflex.news__story')
                if news_story:
                    # Extract all flexBox__body sections (data-index="main", "0", "1", etc.)
                    body_sections = news_story.find_all('ul', class_='flexBox__body')
                    logger.info(f"Found {len(body_sections)} tweet sections")
                    
                    for section in body_sections:
                        data_index = section.get('data-index', 'unknown')
                        # Find h1 in tweet articles within this section
                        tweet_articles = section.find_all('li', class_='news__article--tweet')
                        for article in tweet_articles:
                            h1 = article.find('h1')
                            if h1:
                                headline = h1.get_text(strip=True)
                                if headline:
                                    tweet_texts.append(f"[Tweet {data_index}]: {headline}")
                                    logger.info(f"Extracted tweet from data-index={data_index}: {headline[:80]}...")
                            
                            # Also try to get source from the <a> tag
                            source_link = article.find('a', {'data-story-source': True})
                            if source_link:
                                source = source_link.get('data-story-source', '')
                                if source and tweet_texts:
                                    tweet_texts[-1] = f"[Tweet {data_index} from {source}]: {headline}"
                
                # Create combined content from all tweets
                if tweet_texts:
                    combined_text = '\n\n'.join(tweet_texts)
                    logger.info(f"Extracted {len(tweet_texts)} tweet(s): {combined_text[:150]}...")
                    # Return a simple div with the tweet texts
                    new_soup = BeautifulSoup(f'<div class="tweet-content">{combined_text}</div>', 'lxml')
                    return new_soup
                else:
                    logger.warning("No tweet headlines found, returning full article")
                    return ff_article
            
            logger.info("Found ForexFactory article container: li.news__article")
            
            # For video articles: extract from news__video-caption
            if 'news__article--video' in ff_article.get('class', []):
                video_caption = ff_article.select_one('div.news__video-caption')
                if video_caption:
                    logger.info("Extracting video article caption")
                    return video_caption
            
            # For external articles: extract from news__copy
            news_copy = ff_article.select_one('p.news__copy')
            if news_copy:
                logger.info("Extracting external article content")
                return news_copy
            
            # Fallback: return entire article container
            return ff_article
        
        # PRIORITY 2: Common content containers
        main_selectors = [
            'main',
            'article',
            '[role="main"]',
            '.main-content',
            '#main-content',
            '.post-content',
            '.article-content',
            '.entry-content'
        ]
        
        # Try to find main content
        for selector in main_selectors:
            main = soup.select_one(selector)
            if main:
                logger.info(f"Found main content with selector: {selector}")
                return main
        
        # Fallback to body
        body = soup.find('body')
        return body if body else soup
    
    def _extract_forexfactory_metadata(self, soup: BeautifulSoup) -> Dict[str, str]:
        """
        Extract ForexFactory-specific metadata from Story Stats sidebar and article headline.
        
        Args:
            soup: BeautifulSoup object of full page
            
        Returns:
            Dictionary with FF metadata (posted_date, category, headline, etc.)
        """
        metadata = {}
        
        try:
            # Extract article headline from h1 tag
            h1_tag = soup.select_one('li.news__article h1')
            if h1_tag:
                headline = clean_text(h1_tag.get_text())
                if headline:
                    metadata['headline'] = headline
                    logger.info(f"Extracted headline: {headline}")
            
            # Find Story Stats section
            stats_section = soup.select_one('div.news-stats')
            if not stats_section:
                return metadata
            
            # Extract Posted date - it's in a <strong> tag within news-stats__info--title
            # Structure: <div class="news-stats__info--title">Posted: <strong>Jan 2, 2026 10:50pm</strong></div>
            for row in stats_section.select('li.news-stats__row'):
                # Find Posted date
                title_div = row.select_one('div.news-stats__info--title')
                if title_div and 'Posted:' in title_div.get_text():
                    strong_tag = title_div.find('strong')
                    if strong_tag:
                        metadata['posted_date'] = strong_tag.get_text(strip=True)
                
                # Find Category  
                # Structure: <div class="news-stats__info--category">Category: Technical Analysis</div>
                category_div = row.select_one('div.news-stats__info--category')
                if category_div:
                    category_text = category_div.get_text(strip=True)
                    # Remove "Category:" prefix
                    category_text = category_text.replace('Category:', '').strip()
                    # Normalize whitespace (ForexFactory sometimes has extra spaces)
                    category_text = re.sub(r'\s+', ' ', category_text)
                    metadata['category'] = category_text
            
            logger.info(f"Extracted FF metadata: {metadata}")
            
        except Exception as e:
            logger.warning(f"Error extracting FF metadata: {e}")
        
        return metadata
    
    def _extract_text(self, soup: BeautifulSoup) -> str:
        """
        Extract clean text from HTML
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            Cleaned text
        """
        text = soup.get_text(separator=' ', strip=True)
        
        # Clean whitespace
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        
        return text
    
    def _convert_to_markdown(self, soup: BeautifulSoup) -> str:
        """
        Convert HTML to Markdown
        
        Args:
            soup: BeautifulSoup object
            
        Returns:
            Markdown text
        """
        try:
            html_str = str(soup)
            markdown = self.html2text.handle(html_str)
            
            # Clean up excessive newlines
            markdown = re.sub(r'\n{3,}', '\n\n', markdown)
            
            return markdown.strip()
        except Exception as e:
            logger.warning(f"Error converting to markdown: {e}")
            return ""
    
    def _extract_headings(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """Extract all headings with their levels"""
        headings = []
        
        for level in range(1, 7):
            for heading in soup.find_all(f'h{level}'):
                text = clean_text(heading.get_text())
                if text:
                    headings.append({
                        'level': level,
                        'text': text
                    })
        
        return headings
    
    def _extract_links(self, soup: BeautifulSoup, base_url: str | None) -> List[Dict[str, str]]:
        """Extract all links"""
        links = []
        
        for link in soup.find_all('a', href=True):
            href = link.get('href', '').strip()
            text = clean_text(link.get_text())
            
            normalized = normalize_url(href, base_url) if href else None
            if normalized:
                links.append({
                    'url': normalized,
                    'text': text,
                    'title': link.get('title', '')
                })
        
        return links
    
    def _extract_images(self, soup: BeautifulSoup, base_url: str | None) -> List[Dict[str, str]]:
        """Extract all images"""
        images = []
        
        for img in soup.find_all('img'):
            src = img.get('src', '').strip()
            normalized = normalize_url(src, base_url) if src else None
            if normalized:
                images.append({
                    'src': normalized,
                    'alt': img.get('alt', ''),
                    'title': img.get('title', '')
                })
        
        return images

    def _extract_documents(self, links: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """Fetch previews for document links (e.g. PDFs) within configured limits."""
        if not settings.FETCH_DOCUMENT_LINKS:
            return []

        seen = set()
        document_links = []
        for link in links:
            url = link.get('url')
            if not url or url in seen:
                continue
            if is_document_url(url):
                seen.add(url)
                document_links.append(link)

        limited_links = document_links[: settings.DOCUMENT_LINK_LIMIT]
        previews: List[Dict[str, Any]] = []

        for link in limited_links:
            url = link.get('url')
            preview = fetch_document_preview(url)
            preview['link_text'] = link.get('text', '')
            previews.append(preview)

        return previews
    
    def _extract_tables(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extract tables with their data"""
        tables = []
        
        for table in soup.find_all('table'):
            rows = []
            
            # Extract headers
            headers = []
            header_row = table.find('thead')
            if header_row:
                for th in header_row.find_all(['th', 'td']):
                    headers.append(clean_text(th.get_text()))
            
            # Extract rows
            tbody = table.find('tbody') or table
            for tr in tbody.find_all('tr'):
                cells = []
                for td in tr.find_all(['td', 'th']):
                    cells.append(clean_text(td.get_text()))
                if cells:
                    rows.append(cells)
            
            if rows:
                tables.append({
                    'headers': headers,
                    'rows': rows,
                    'row_count': len(rows)
                })
        
        return tables
    
    def clean_and_format(self, html: str, base_url: str | None = None, format: str = 'all') -> Dict[str, Any]:
        """
        Main method to clean and format content
        
        Args:
            html: Raw HTML content
            format: Output format ('text', 'markdown', 'structured', 'all')
            
        Returns:
            Cleaned and formatted content
        """
        cleaned_data = self.clean_html_content(html, base_url=base_url)
        
        if format == 'text':
            return {
                'sections': {
                    'metadata': cleaned_data['sections']['metadata'],
                    'content': {
                        'summary': cleaned_data['sections']['content']['summary'],
                        'text_chunks': cleaned_data['sections']['content']['text_chunks']
                    }
                },
                'stats': cleaned_data['stats']
            }
        elif format == 'markdown':
            return {
                'sections': {
                    'metadata': cleaned_data['sections']['metadata'],
                    'content': {
                        'summary': cleaned_data['sections']['content']['summary'],
                        'markdown': cleaned_data['sections']['content']['markdown']
                    }
                },
                'stats': cleaned_data['stats']
            }
        elif format == 'structured':
            return {
                'sections': {
                    'metadata': cleaned_data['sections']['metadata'],
                    'structure': cleaned_data['sections']['structure']
                },
                'stats': cleaned_data['stats']
            }
        else:  # 'all'
            return cleaned_data
