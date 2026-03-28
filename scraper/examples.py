"""
Example usage scripts for the Web Scraper API
"""
import requests
import json

# Base URL of the API
BASE_URL = "http://localhost:8000"


def test_health():
    """Test health endpoint"""
    response = requests.get(f"{BASE_URL}/health")
    print("Health Check:", response.json())


def scrape_url(url, force_selenium=False, output_format="all"):
    """
    Scrape a URL
    
    Args:
        url: URL to scrape
        force_selenium: Force using Selenium
        output_format: Output format (text, markdown, structured, all)
    """
    print(f"\n{'='*60}")
    print(f"Scraping: {url}")
    print(f"{'='*60}")
    
    response = requests.post(
        f"{BASE_URL}/api/v1/scrape",
        json={
            "url": url,
            "force_selenium": force_selenium,
            "auto_detect_js": True,
            "output_format": output_format
        },
        timeout=120
    )
    
    if response.status_code == 200:
        data = response.json()
        
        if data['success']:
            print(f"\n✅ Success!")
            print(f"Method: {data['method']}")

            sections = data.get('sections', {})
            metadata = sections.get('metadata', {})
            content = sections.get('content', {})
            stats = data.get('stats', {})

            print(f"\nMetadata:")
            print(f"  Title: {metadata.get('title', 'N/A')}")
            print(f"  Description: {metadata.get('description', 'N/A')}")
            print(f"  Word Count: {stats.get('word_count', 'N/A')}")

            text_chunks = content.get('text_chunks', [])
            if output_format in ['text', 'all'] and text_chunks:
                print(f"\nText Chunk Preview:")
                preview = text_chunks[0]
                print(preview[:500] + ("..." if len(preview) > 500 else ""))

            markdown_text = content.get('markdown')
            if output_format in ['markdown', 'all'] and markdown_text:
                print(f"\nMarkdown Content (first 500 chars):")
                print(markdown_text[:500] + ("..." if len(markdown_text) > 500 else ""))
            
            return data
        else:
            print(f"❌ Failed: {data.get('error', 'Unknown error')}")
    else:
        print(f"❌ HTTP Error {response.status_code}: {response.text}")
    
    return None


def crawl_url(url, max_links=50, depth=1):
    """
    Crawl a URL for links
    
    Args:
        url: URL to crawl
        max_links: Maximum links per category
        depth: Multi-level crawl depth
    """
    print(f"\n{'='*60}")
    print(f"Crawling: {url}")
    print(f"{'='*60}")
    
    response = requests.post(
        f"{BASE_URL}/api/v1/crawl",
        json={
            "url": url,
            "max_links": max_links,
            "force_selenium": False,
            "depth": depth
        },
        timeout=120
    )
    
    if response.status_code == 200:
        data = response.json()
        
        if data['success']:
            print(f"\n✅ Success!")

            stats = data.get('stats', {})
            sections = data.get('sections', {})

            print(f"\nStatistics:")
            for key, value in stats.items():
                print(f"  {key}: {value}")

            aggregated_links = sections.get('aggregated_links', {})
            print(f"\nAggregated Link Categories:")
            for category, links in aggregated_links.items():
                print(f"  {category}: {len(links)} links")
                if links:
                    print(f"    Example: {links[0]['url']}")

            visited_pages = sections.get('visited_pages', [])
            if visited_pages:
                print(f"\nVisited Pages ({len(visited_pages)}):")
                for i, page in enumerate(visited_pages[:10], 1):
                    print(f"  {i}. {page}")
                if len(visited_pages) > 10:
                    print(f"  ... and {len(visited_pages) - 10} more")
            
            return data
        else:
            print(f"❌ Failed: {data.get('error', 'Unknown error')}")
    else:
        print(f"❌ HTTP Error {response.status_code}: {response.text}")
    
    return None


def batch_scrape(urls, output_format="text"):
    """
    Scrape multiple URLs
    
    Args:
        urls: List of URLs to scrape
        output_format: Output format
    """
    print(f"\n{'='*60}")
    print(f"Batch Scraping {len(urls)} URLs")
    print(f"{'='*60}")
    
    response = requests.post(
        f"{BASE_URL}/api/v1/batch-scrape",
        json={
            "urls": urls,
            "force_selenium": False,
            "output_format": output_format
        },
        timeout=300
    )
    
    if response.status_code == 200:
        data = response.json()
        
        print(f"\n✅ Batch Complete!")
        print(f"\nSummary:")
        for key, value in data['summary'].items():
            print(f"  {key}: {value}")
        
        print(f"\nResults:")
        for i, result in enumerate(data['results'], 1):
            status = "✅" if result['success'] else "❌"
            print(f"  {i}. {status} {result['url']}")
            if not result['success']:
                print(f"      Error: {result.get('error', 'Unknown')}")
        
        return data
    else:
        print(f"❌ HTTP Error {response.status_code}: {response.text}")
    
    return None


def test_forex_factory():
    """Test scraping Forex Factory (the demo URL)"""
    url = "https://www.forexfactory.com/news/1354758-the-july-2025-senior-loan-officer-opinion-survey"
    
    print("\n" + "="*60)
    print("Testing Forex Factory Scraping")
    print("="*60)
    
    # This site requires JavaScript, so force Selenium
    result = scrape_url(url, force_selenium=True, output_format="text")
    
    if result:
        print("\n📊 Analysis:")
        sections = result.get('sections', {})
        content = sections.get('content', {})
        stats = result.get('stats', {})
        print(f"  - Successfully scraped JavaScript-protected page")
        text_chunks = content.get('text_chunks', [])
        if text_chunks:
            print(f"  - First chunk length: {len(text_chunks[0])} characters")
        print(f"  - Word count: {stats.get('word_count')}")
        
        # Save to file
        with open('forex_factory_scraped.json', 'w') as f:
            json.dump(result, f, indent=2)
        print(f"  - Saved to: forex_factory_scraped.json")


def main():
    """Run example tests"""
    print("="*60)
    print("Web Scraper API - Example Usage")
    print("="*60)
    
    # Test health
    test_health()
    
    # Test scraping
    print("\n\n1. Testing Basic Scraping (Example.com)")
    scrape_url("https://example.com", output_format="text")
    
    # Test crawling
    print("\n\n2. Testing Link Crawling (Example.com)")
    crawl_url("https://example.com", max_links=20, depth=2)
    
    # Test Forex Factory (the demo URL)
    print("\n\n3. Testing JavaScript-Heavy Site (Forex Factory)")
    test_forex_factory()
    
    # Test batch scraping
    print("\n\n4. Testing Batch Scraping")
    urls = [
        "https://example.com",
        "https://www.example.org",
    ]
    batch_scrape(urls, output_format="text")
    
    print("\n" + "="*60)
    print("All tests complete!")
    print("="*60)


if __name__ == "__main__":
    # Make sure the API is running before executing
    try:
        requests.get(f"{BASE_URL}/health", timeout=5)
        main()
    except requests.exceptions.ConnectionError:
        print("❌ Error: API is not running!")
        print("Please start the API first: python app.py")
    except Exception as e:
        print(f"❌ Error: {e}")
