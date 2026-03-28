"""
FastAPI Web Scraper & Crawler API
Professional web scraping service with anti-bot detection bypass
"""
import time
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, List, Dict, Any
import traceback
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool

from config import settings
from scraper import WebScraper
from crawler import WebCrawler
from cleaner import ContentCleaner
from utils import is_valid_url, logger, normalize_url
from duckduckgo_search import DDGS


# Request Models
class ScrapeRequest(BaseModel):
    """Request model for scraping endpoint"""
    url: str = Field(..., description="URL to scrape")
    force_selenium: bool = Field(True, description="Force using Selenium for JavaScript rendering (default: True)")
    auto_detect_js: bool = Field(True, description="Automatically detect if JavaScript rendering is needed")
    output_format: str = Field("all", description="Output format: 'text', 'markdown', 'structured', or 'all'")
    
    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://www.forexfactory.com/news/1354758-the-july-2025-senior-loan-officer-opinion-survey",
                "force_selenium": False,
                "auto_detect_js": True,
                "output_format": "all"
            }
        }


class CrawlRequest(BaseModel):
    """Request model for crawling endpoint"""
    url: str = Field(..., description="URL to crawl for links")
    max_links: int = Field(100, description="Maximum number of links to return per category")
    force_selenium: bool = Field(True, description="Force using Selenium (default: True)")
    depth: int = Field(1, description="Crawl depth (multi-level crawling)", ge=1, le=settings.MAX_CRAWL_DEPTH)
    
    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://www.forexfactory.com/news/1354758-the-july-2025-senior-loan-officer-opinion-survey",
                "max_links": 100,
                "force_selenium": False,
                "depth": 2
            }
        }


class BatchScrapeRequest(BaseModel):
    """Request model for batch scraping"""
    urls: List[str] = Field(..., description="List of URLs to scrape")
    force_selenium: bool = Field(False, description="Force using Selenium")
    output_format: str = Field("text", description="Output format")


class SearchRequest(BaseModel):
    """Request model for search endpoint"""
    query: str = Field(..., min_length=2, description="Search query")
    max_results: int = Field(5, ge=1, le=25, description="Maximum results to return")
    region: Optional[str] = Field(None, description="DuckDuckGo region code, e.g. 'wt-wt'")
    safesearch: Optional[str] = Field(None, description="DuckDuckGo safesearch level: 'off', 'moderate', 'strict'")
    preview_count: int = Field(0, ge=0, le=settings.SEARCH_MAX_PREVIEW, description="Number of top results to pre-fetch and summarize")
    force_selenium: bool = Field(False, description="Force Selenium when building previews")


# Response Models
class ScrapeResponse(BaseModel):
    """Response model for scraping endpoint"""
    success: bool
    url: str
    sections: Dict[str, Any]
    stats: Dict[str, Any]
    method: str
    meta: Dict[str, Any]
    error: Optional[str] = None


class CrawlResponse(BaseModel):
    """Response model for crawling endpoint"""
    success: bool
    url: str
    sections: Dict[str, Any]
    stats: Dict[str, Any]
    error: Optional[str] = None


class SearchResponse(BaseModel):
    """Response model for search endpoint"""
    query: str
    results: List[Dict[str, Any]]
    took_ms: float
    meta: Dict[str, Any]


# Lifespan context manager
scraper_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    global scraper_instance
    logger.info("Starting up API server...")
    scraper_instance = WebScraper()
    yield
    logger.info("Shutting down API server...")
    if scraper_instance:
        scraper_instance.close()


# Initialize FastAPI app
app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    description=settings.API_DESCRIPTION,
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API Endpoints

@app.get("/", tags=["Health"])
async def root():
    """Root endpoint - API information"""
    return {
        "name": settings.API_TITLE,
        "version": settings.API_VERSION,
        "status": "operational",
        "endpoints": {
            "health": "/health",
            "scrape": "/api/v1/scrape",
            "crawl": "/api/v1/crawl",
            "batch_scrape": "/api/v1/batch-scrape",
            "search": "/api/v1/search",
            "docs": "/docs"
        }
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": settings.API_VERSION
    }


@app.post("/api/v1/scrape", response_model=ScrapeResponse, tags=["Scraping"])
async def scrape_url(request: ScrapeRequest):
    """
    Scrape content from a URL
    
    This endpoint scrapes web pages and returns cleaned, preprocessed content.
    It automatically handles JavaScript-rendered content and bypasses anti-bot protection.
    
    **Features:**
    - Dual scraping engines (BeautifulSoup + Selenium)
    - Automatic JavaScript detection
    - Anti-bot protection bypass
    - Structured JSON sections for AI workflows
    - Content cleaning and preprocessing
    
    **Parameters:**
    - **url**: The URL to scrape
    - **force_selenium**: Force using Selenium (slower but handles complex JS)
    - **auto_detect_js**: Automatically switch to Selenium if JS is detected
    - **output_format**: Format of the output (text, markdown, structured, all)
    
    **Example Response:**
    ```json
    {
        "success": true,
        "url": "https://example.com",
        "sections": {
            "metadata": {"title": "Page Title", "description": "..."},
            "content": {
                "summary": "First sentences...",
                "text_chunks": ["chunk 1", "chunk 2"],
                "markdown": "# Heading\n..."
            },
            "structure": {
                "headings": [{"level": 1, "text": "Heading"}],
                "tables": [{"headers": [...], "rows": [...]}]
            },
            "resources": {
                "links": [{"url": "https://...", "text": "..."}],
                "images": [{"src": "https://...", "alt": "..."}]
            }
        },
        "stats": {"word_count": 1200, "char_count": 6500},
        "method": "selenium",
        "meta": {"html_size": 45231}
    }
    ```
    """
    try:
        # Validate URL
        if not is_valid_url(request.url):
            raise HTTPException(status_code=400, detail="Invalid URL format")
        
        logger.info(f"Scraping request for: {request.url}")
        
        # Scrape the page
        # IMPORTANT: Selenium + requests scraping is blocking. Offload to a threadpool
        # so the async server can handle concurrent requests (enables driver pooling).
        scrape_result = await run_in_threadpool(
            scraper_instance.scrape,
            url=request.url,
            force_selenium=request.force_selenium,
            auto_detect_js=request.auto_detect_js,
        )
        
        if not scrape_result.get('success'):
            # Keep a 200 with success=false so downstream services can act on
            # sentinel errors (e.g., "cloudflare_unsolved") and decide whether to stop.
            return {
                "success": False,
                "url": request.url,
                "sections": {},
                "stats": {},
                "method": scrape_result.get('method') or "",
                "meta": {
                    "status_code": scrape_result.get('status_code'),
                    "html_size": len(scrape_result.get('html') or "")
                },
                "error": scrape_result.get('error') or "Scraping failed",
            }
        
        # Clean and format content
        cleaner = ContentCleaner()
        cleaned_content = await run_in_threadpool(
            cleaner.clean_and_format,
            html=scrape_result['html'],
            base_url=request.url,
            format=request.output_format,
        )
        
        # Build response
        response = {
            "success": True,
            "url": request.url,
            "sections": cleaned_content.get('sections', {}),
            "stats": cleaned_content.get('stats', {}),
            "method": scrape_result['method'],
            "meta": {
                "status_code": scrape_result.get('status_code'),
                "html_size": len(scrape_result['html'])
            }
        }
        
        logger.info(f"Successfully scraped: {request.url}")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error scraping {request.url}: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/crawl", response_model=CrawlResponse, tags=["Crawling"])
async def crawl_url(request: CrawlRequest):
    """
    Crawl a URL to discover links and pages
    
    This endpoint performs multi-level crawling and returns structured sections:
    - Per-depth breakdown of discovered pages
    - Aggregated link categories
    - Visited page list
    
    **Features:**
    - Multi-depth BFS crawling
    - Smart link categorization
    - Aggregated insights for AI agents
    - Statistics about discovered links
    
    **Parameters:**
    - **url**: The URL to crawl
    - **max_links**: Maximum links per category
    - **force_selenium**: Use Selenium for scraping
    - **depth**: Crawl depth (1..MAX)
    
    **Example Response:**
    ```json
    {
        "success": true,
        "url": "https://example.com",
        "sections": {
            "levels": {
                "0": [{"page": "https://example.com", "links": {...}}],
                "1": [{"page": "https://example.com/about", "links": {...}}]
            },
            "aggregated_links": {
                "internal": [...],
                "external": [...],
                "media": [...]
            },
            "visited_pages": ["https://example.com", "https://example.com/about"]
        },
        "stats": {
            "total_pages": 7,
            "max_depth_reached": 2,
            "total_internal_links": 32,
            "total_external_links": 11
        }
    }
    ```
    """
    try:
        # Validate URL
        if not is_valid_url(request.url):
            raise HTTPException(status_code=400, detail="Invalid URL format")
        
        logger.info(f"Crawling request for: {request.url}")
        
        # Scrape the page first
        scrape_result = await run_in_threadpool(
            scraper_instance.scrape,
            url=request.url,
            force_selenium=request.force_selenium,
        )
        
        if not scrape_result['success']:
            raise HTTPException(status_code=500, detail=scrape_result.get('error', 'Scraping failed'))
        
        # Crawl for links with multi-level support
        crawler = WebCrawler(base_url=request.url)
        crawl_result = crawler.crawl_multilevel(
            initial_html=scrape_result['html'],
            scraper=scraper_instance,
            depth=request.depth,
            max_links=request.max_links
        )

        response = {
            "success": True,
            "url": request.url,
            "sections": {
                "levels": crawl_result.get('levels', {}),
                "aggregated_links": crawl_result.get('aggregated_links', {}),
                "visited_pages": crawl_result.get('visited_pages', [])
            },
            "stats": crawl_result.get('statistics', {})
        }
        
        logger.info(f"Successfully crawled: {request.url}")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error crawling {request.url}: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/batch-scrape", tags=["Scraping"])
async def batch_scrape(request: BatchScrapeRequest):
    """
    Scrape multiple URLs in batch
    
    This endpoint allows scraping multiple URLs in a single request.
    Useful for processing multiple pages from the same domain.
    
    **Note:** For large batches, consider using individual scrape requests
    to avoid timeouts.
    
    **Parameters:**
    - **urls**: List of URLs to scrape
    - **force_selenium**: Use Selenium for all URLs
    - **output_format**: Output format for all URLs

    Each result mirrors the `sections`/`stats` shape from `/api/v1/scrape`,
    with a final `summary` block reporting total, successful, and failed counts.
    """
    try:
        results = []
        successful = 0
        failed = 0
        
        for url in request.urls:
            try:
                if not is_valid_url(url):
                    results.append({
                        "url": url,
                        "success": False,
                        "error": "Invalid URL format"
                    })
                    failed += 1
                    continue
                
                # Scrape
                scrape_result = scraper_instance.scrape(
                    url=url,
                    force_selenium=request.force_selenium
                )
                
                if scrape_result['success']:
                    # Clean content
                    cleaner = ContentCleaner()
                    cleaned_content = cleaner.clean_and_format(
                        html=scrape_result['html'],
                        base_url=url,
                        format=request.output_format
                    )
                    
                    results.append({
                        "url": url,
                        "success": True,
                        "sections": cleaned_content.get('sections', {}),
                        "stats": cleaned_content.get('stats', {}),
                        "method": scrape_result['method']
                    })
                    successful += 1
                else:
                    results.append({
                        "url": url,
                        "success": False,
                        "error": scrape_result.get('error', 'Unknown error')
                    })
                    failed += 1
                    
            except Exception as e:
                logger.error(f"Error scraping {url}: {e}")
                results.append({
                    "url": url,
                    "success": False,
                    "error": str(e)
                })
                failed += 1
        
        return {
            "success": True,
            "results": results,
            "summary": {
                "total": len(request.urls),
                "successful": successful,
                "failed": failed
            }
        }
        
    except Exception as e:
        logger.error(f"Error in batch scrape: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/test", tags=["Testing"])
async def test_scraper(
    url: str = Query(..., description="URL to test scrape"),
    force_selenium: bool = Query(False, description="Force Selenium")
):
    """
    Quick test endpoint for scraping
    
    Simple GET endpoint for quick testing without POST body.
    """
    try:
        request = ScrapeRequest(
            url=url,
            force_selenium=force_selenium,
            output_format="text"
        )
        return await scrape_url(request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/search", response_model=SearchResponse, tags=["Discovery"])
async def search_web(request: SearchRequest):
    """Perform a federated web search and optionally return enriched previews."""
    start_time = time.perf_counter()

    region = request.region or settings.SEARCH_DEFAULT_REGION
    safesearch = request.safesearch or settings.SEARCH_DEFAULT_SAFE
    max_results = request.max_results
    preview_count = min(request.preview_count, max_results, settings.SEARCH_MAX_PREVIEW)

    try:
        results: List[Dict[str, Any]] = []
        seen_urls = set()

        with DDGS() as ddgs:
            for rank, result in enumerate(ddgs.text(
                request.query,
                max_results=max_results,
                region=region,
                safesearch=safesearch,
                backend="lite"
            ), start=1):
                url = normalize_url(result.get('href') or result.get('url', ''), None)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                results.append({
                    'rank': rank,
                    'title': result.get('title'),
                    'url': url,
                    'snippet': result.get('body', ''),
                    'source': result.get('source'),
                    'published': result.get('date')
                })

                if len(results) >= max_results:
                    break

        # Enrich previews for top results by scraping summaries
        if preview_count and results:
            cleaner = ContentCleaner()
            for item in results[:preview_count]:
                try:
                    scrape_result = scraper_instance.scrape(
                        url=item['url'],
                        force_selenium=request.force_selenium,
                        auto_detect_js=True
                    )
                    if not scrape_result['success']:
                        continue

                    cleaned_content = cleaner.clean_and_format(
                        html=scrape_result['html'],
                        base_url=item['url'],
                        format='text'
                    )
                    sections = cleaned_content.get('sections', {})
                    content = sections.get('content', {})
                    item['preview'] = {
                        'summary': content.get('summary'),
                        'top_chunk': (content.get('text_chunks') or [''])[0],
                        'word_count': cleaned_content.get('stats', {}).get('word_count')
                    }
                    item['method'] = scrape_result.get('method')
                except Exception as preview_error:
                    logger.warning(f"Search preview failed for {item['url']}: {preview_error}")

        duration_ms = (time.perf_counter() - start_time) * 1000

        return {
            'query': request.query,
            'results': results,
            'took_ms': round(duration_ms, 2),
            'meta': {
                'region': region,
                'safesearch': safesearch,
                'preview_count': preview_count,
                'total_results': len(results)
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search failed for query '{request.query}': {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD
    )
