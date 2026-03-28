# Scraper Service - Usage Guide

## 🎯 Quick Start

### From Docker Network (inside another container)
```
URL: http://tradingbot-scraper:8000
```

### From Host Machine
```
URL: http://localhost:8000
```

---

## 📝 Basic Examples

### 1. Scrape a News Article (Recommended for news sites)
```bash
curl -X POST http://tradingbot-scraper:8000/api/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.bbc.com/news/business",
    "force_selenium": true,
    "output_format": "all"
  }'
```

### 2. Lightweight Scrape (Smaller response)
```bash
curl -X POST http://tradingbot-scraper:8000/api/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "force_selenium": false,
    "output_format": "text"
  }'
```

### 3. Batch Scrape Multiple URLs
```bash
curl -X POST http://tradingbot-scraper:8000/api/v1/batch-scrape \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://example1.com",
      "https://example2.com"
    ],
    "force_selenium": false,
    "output_format": "text"
  }'
```

### 4. Discover Links on a Page
```bash
curl -X POST http://tradingbot-scraper:8000/api/v1/crawl \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "max_links": 50,
    "force_selenium": false,
    "depth": 2
  }'
```

### 5. Web Search
```bash
curl -X POST http://tradingbot-scraper:8000/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Federal Reserve interest rates",
    "max_results": 10,
    "preview_count": 3,
    "force_selenium": false
  }'
```

---

## 🐍 Python Examples

### Simple Scrape
```python
import requests

response = requests.post(
    'http://tradingbot-scraper:8000/api/v1/scrape',
    json={
        'url': 'https://www.bbc.com/news',
        'force_selenium': True,
        'output_format': 'all'
    },
    timeout=120
)

data = response.json()
print(f"Title: {data['sections']['metadata']['title']}")
print(f"Words: {data['stats']['word_count']}")
print(f"Content: {data['sections']['content']['text_chunks'][0][:200]}")
```

### Extract Key Data
```python
import requests

response = requests.post(
    'http://tradingbot-scraper:8000/api/v1/scrape',
    json={
        'url': 'https://news.example.com/article',
        'force_selenium': True,
        'output_format': 'all'
    },
    timeout=120
)

data = response.json()

# Extract what you need
title = data['sections']['metadata']['title']
summary = data['sections']['content']['summary']
full_text = ' '.join(data['sections']['content']['text_chunks'])
links = data['sections']['resources']['links']
images = data['sections']['resources']['images']
word_count = data['stats']['word_count']

print(f"Title: {title}")
print(f"Summary: {summary}")
print(f"Found {len(links)} links")
print(f"Found {len(images)} images")
```

### With Error Handling
```python
import requests
from requests.exceptions import Timeout, ConnectionError

def scrape_url(url, use_selenium=True):
    try:
        response = requests.post(
            'http://tradingbot-scraper:8000/api/v1/scrape',
            json={
                'url': url,
                'force_selenium': use_selenium,
                'output_format': 'all'
            },
            timeout=120
        )
        response.raise_for_status()
        
        data = response.json()
        
        if not data['success']:
            print(f"Scrape failed: {data['error']}")
            return None
        
        return {
            'title': data['sections']['metadata']['title'],
            'content': ' '.join(data['sections']['content']['text_chunks']),
            'word_count': data['stats']['word_count'],
            'links': data['sections']['resources']['links'],
            'method': data['method']
        }
        
    except Timeout:
        print(f"Request timed out for {url}")
        return None
    except ConnectionError:
        print("Cannot connect to scraper service")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

# Usage
article = scrape_url('https://www.reuters.com/business/')
if article:
    print(f"Scraped: {article['title']}")
    print(f"Words: {article['word_count']}")
```

---

## 📋 API Parameters

### `/api/v1/scrape` - Scrape a single URL

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | The URL to scrape |
| `force_selenium` | boolean | false | Force Selenium for all sites |
| `auto_detect_js` | boolean | true | Auto-switch to Selenium if JS detected |
| `output_format` | string | "all" | Response format (all/text/markdown/structured) |

### `/api/v1/batch-scrape` - Scrape multiple URLs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `urls` | array | required | List of URLs to scrape |
| `force_selenium` | boolean | false | Force Selenium for all URLs |
| `output_format` | string | "text" | Response format |

### `/api/v1/crawl` - Discover links on a page

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | Base URL to crawl |
| `max_links` | integer | 100 | Max links per category |
| `force_selenium` | boolean | false | Use Selenium for rendering |
| `depth` | integer | 1 | Crawl depth (1-3) |

### `/api/v1/search` - Web search with previews

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Search query |
| `max_results` | integer | 5 | Number of results |
| `preview_count` | integer | 0 | Number of previews to fetch |
| `force_selenium` | boolean | false | Use Selenium for previews |

---

## 📊 Response Structure

### Success Response
```json
{
  "success": true,
  "url": "https://example.com",
  "sections": {
    "metadata": {
      "title": "Page Title",
      "description": "Page description",
      "author": "Author Name",
      "og_title": "OG Title",
      "og_description": "OG Description",
      "og_image": "Image URL"
    },
    "content": {
      "summary": "First few sentences...",
      "text_chunks": ["chunk1", "chunk2", "chunk3"],
      "markdown": "# Heading\n\nContent..."
    },
    "structure": {
      "headings": [
        {"level": 1, "text": "Main Heading"},
        {"level": 2, "text": "Sub Heading"}
      ],
      "tables": [
        {
          "headers": ["Col1", "Col2"],
          "rows": [["data1", "data2"]],
          "row_count": 1
        }
      ]
    },
    "resources": {
      "links": [
        {"url": "https://...", "text": "Link text", "title": "Title"}
      ],
      "images": [
        {"src": "https://...", "alt": "Alt text", "title": "Title"}
      ],
      "documents": [
        {"url": "https://....pdf", "success": true, "text_preview": "..."}
      ]
    }
  },
  "stats": {
    "word_count": 1200,
    "char_count": 6500,
    "chunk_count": 5
  },
  "method": "selenium",
  "meta": {
    "status_code": 200,
    "html_size": 45231
  },
  "error": null
}
```

### Error Response
```json
{
  "success": false,
  "error": "Error message",
  "url": "https://example.com"
}
```

---

## ⏱️ Timing Guide

| Website Type | Method | Time | Recommendation |
|--------------|--------|------|-----------------|
| Static HTML | BeautifulSoup | 2-5s | `force_selenium: false` |
| News Sites | Selenium | 10-30s | `force_selenium: true` |
| React/Vue | Selenium | 15-60s | `force_selenium: true` |
| Documentation | BeautifulSoup | 1-3s | `force_selenium: false` |

**Always use timeout ≥ 120 seconds** in your requests!

---

## 🔧 How BeautifulSoup vs Selenium Works

The scraper automatically decides:

1. **If `force_selenium=true`** → Always uses Selenium
2. **If `force_selenium=false` (default):**
   - Tries BeautifulSoup first (fast, ~2-5s)
   - Analyzes HTML for JS frameworks (React, Angular, Vue, etc.)
   - If 2+ JS indicators found → Switches to Selenium
   - If BeautifulSoup fails → Falls back to Selenium

---

## 💡 Best Practices

### For News Articles
```json
{
  "force_selenium": true,
  "output_format": "all"
}
```
- Most news sites use JavaScript
- Gets full content + metadata + images

### For Simple/Static Content
```json
{
  "force_selenium": false,
  "output_format": "text"
}
```
- Much faster (2-5 seconds)
- Smaller response size

### For Batch Processing
```json
{
  "force_selenium": false,
  "output_format": "text"
}
```
- Use `batch-scrape` endpoint
- Set `force_selenium=false` to save time on simple URLs
- Let auto-detection handle JS sites if needed

### For Link Discovery
```json
{
  "force_selenium": false,
  "depth": 2
}
```
- Use `crawl` endpoint
- Start with depth=1 or 2 to avoid too many requests

---

## 🚀 Production Usage

### From Your AI Trading Bot
```python
# In a service container (your news analyzer, etc.)
import requests
from datetime import datetime

class ScraperClient:
    def __init__(self, base_url='http://tradingbot-scraper:8000'):
        self.base_url = base_url
        
    def scrape_news(self, url, wait_for_js=True):
        """Scrape news article"""
        response = requests.post(
            f'{self.base_url}/api/v1/scrape',
            json={
                'url': url,
                'force_selenium': wait_for_js,
                'output_format': 'all'
            },
            timeout=120
        )
        
        data = response.json()
        
        if not data['success']:
            return None
        
        return {
            'title': data['sections']['metadata']['title'],
            'content': ' '.join(data['sections']['content']['text_chunks']),
            'summary': data['sections']['content']['summary'],
            'links': data['sections']['resources']['links'],
            'images': data['sections']['resources']['images'],
            'word_count': data['stats']['word_count'],
            'scraped_at': datetime.now().isoformat(),
            'method': data['method']
        }

# Usage
scraper = ScraperClient()
article = scraper.scrape_news('https://www.reuters.com/business/')
```

---

## 🐛 Troubleshooting

### "Cannot connect to scraper"
- Make sure scraper container is running: `docker ps | grep scraper`
- Check the container is healthy: `docker ps --format "table {{.Names}}\t{{.Status}}"`
- If using Docker network, use `http://tradingbot-scraper:8000`
- If on host, use `http://localhost:8000`

### "Request timeout"
- Increase timeout in your code (min 120 seconds)
- May indicate Selenium is taking too long
- Check server logs: `docker logs tradingbot-scraper`

### "Low word count on news site"
- Site is likely JavaScript-heavy
- Try with `force_selenium: true`
- Check response method: `data['method']` should be "selenium"

### "Chrome instance closed - memory freed"
- This is normal! Scraper closes Chrome after each request
- Prevents memory bloat with on-demand scraping

---

## 📚 Health & Status Checks

### Health Check
```bash
curl http://tradingbot-scraper:8000/health
```

Response:
```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### API Info
```bash
curl http://tradingbot-scraper:8000/
```

Response:
```json
{
  "name": "Web Scraper & Crawler API",
  "version": "1.0.0",
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
```

### Interactive Docs
```
http://localhost:8000/docs
```
(Swagger UI for testing endpoints)

---

## 📞 Support

- Check logs: `docker logs tradingbot-scraper`
- View metrics: `docker stats tradingbot-scraper`
- Restart service: `docker-compose restart scraper`

