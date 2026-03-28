"""
Test script specifically for Forex Factory URL
This demonstrates scraping a JavaScript-heavy page with anti-bot protection
"""
import requests
import json
import time
from datetime import datetime


BASE_URL = "http://localhost:8000"
FOREX_FACTORY_URL = "https://www.forexfactory.com/news/1354758-the-july-2025-senior-loan-officer-opinion-survey"


def print_section(title):
    """Print a formatted section header"""
    print("\n" + "="*70)
    print(f"  {title}")
    print("="*70 + "\n")


def test_api_health():
    """Test if API is running"""
    print_section("1. Testing API Health")
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        if response.status_code == 200:
            print("✅ API is running and healthy")
            print(f"Response: {response.json()}")
            return True
        else:
            print(f"❌ API returned status code: {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to API!")
        print("Please start the API first: python app.py")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def scrape_forex_factory():
    """Scrape the Forex Factory article"""
    print_section("2. Scraping Forex Factory Article")
    
    print(f"URL: {FOREX_FACTORY_URL}")
    print("Method: Using Selenium (JavaScript-enabled)")
    print("\nSending request... (this may take 30-60 seconds)")
    
    start_time = time.time()
    
    try:
        response = requests.post(
            f"{BASE_URL}/api/v1/scrape",
            json={
                "url": FOREX_FACTORY_URL,
                "force_selenium": True,  # Force Selenium for JS-heavy site
                "auto_detect_js": True,
                "output_format": "all"  # Get all available data
            },
            timeout=120  # 2 minute timeout
        )
        
        elapsed_time = time.time() - start_time
        
        if response.status_code == 200:
            data = response.json()
            
            if data['success']:
                print(f"\n✅ Successfully scraped in {elapsed_time:.2f} seconds!")
                print(f"\n📊 Scraping Details:")
                print(f"  Method Used: {data['method']}")
                print(f"  HTML Size: {data['meta'].get('html_size', 'N/A')} bytes")

                sections = data.get('sections', {})
                metadata = sections.get('metadata', {})
                content = sections.get('content', {})
                structure = sections.get('structure', {})
                resources = sections.get('resources', {})
                stats = data.get('stats', {})

                print(f"\n📝 Metadata:")
                print(f"  Title: {metadata.get('title', 'N/A')}")
                print(f"  Description: {metadata.get('description', 'N/A')[:100]}...")
                print(f"  Author: {metadata.get('author', 'N/A')}")

                print(f"\n📊 Content Statistics:")
                print(f"  Word Count: {stats.get('word_count', 'N/A')}")
                print(f"  Character Count: {stats.get('char_count', 'N/A')}")
                print(f"  Headings Found: {len(structure.get('headings', []))}")
                print(f"  Links Found: {len(resources.get('links', []))}")
                print(f"  Images Found: {len(resources.get('images', []))}")
                print(f"  Tables Found: {len(structure.get('tables', []))}")

                text_chunks = content.get('text_chunks', [])
                print(f"\n📄 Text Content Preview (first 500 characters):")
                print("-" * 70)
                preview = text_chunks[0] if text_chunks else ""
                print(preview[:500] + ("..." if len(preview) > 500 else ""))
                print("-" * 70)

                if structure.get('headings'):
                    print(f"\n📑 Headings Structure:")
                    for heading in structure['headings'][:10]:
                        indent = "  " * (heading['level'] - 1)
                        print(f"{indent}H{heading['level']}: {heading['text'][:60]}")
                
                # Save to file
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"forex_factory_scraped_{timestamp}.json"
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"\n💾 Full data saved to: {filename}")
                
                return data
            else:
                print(f"\n❌ Scraping failed: {data.get('error', 'Unknown error')}")
                return None
        else:
            print(f"\n❌ HTTP Error {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return None
            
    except requests.exceptions.Timeout:
        print("\n❌ Request timed out!")
        print("The page may be taking too long to load. Try increasing the timeout.")
        return None
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return None


def crawl_forex_factory():
    """Crawl Forex Factory for links"""
    print_section("3. Crawling Forex Factory for Links")
    
    print(f"URL: {FOREX_FACTORY_URL}")
    print("Discovering links and pages...\n")
    
    try:
        response = requests.post(
            f"{BASE_URL}/api/v1/crawl",
            json={
                "url": FOREX_FACTORY_URL,
                "max_links": 50,
                "force_selenium": True,
                "depth": 2
            },
            timeout=120
        )
        
        if response.status_code == 200:
            data = response.json()
            
            if data['success']:
                print("✅ Successfully crawled!")
                
                stats = data.get('stats', {})
                print(f"\n📊 Link Statistics:")
                for key, value in stats.items():
                    print(f"  {key.replace('_', ' ').title()}: {value}")

                sections = data.get('sections', {})
                aggregated = sections.get('aggregated_links', {})

                print(f"\n🔗 Aggregated Link Categories:")
                for category, link_list in aggregated.items():
                    if link_list:
                        print(f"\n  {category.upper()} ({len(link_list)} links):")
                        for i, link in enumerate(link_list[:5], 1):
                            print(f"    {i}. {link['url'][:60]}...")
                            if link.get('text'):
                                print(f"       Text: {link['text'][:50]}")
                        if len(link_list) > 5:
                            print(f"    ... and {len(link_list) - 5} more")

                visited_pages = sections.get('visited_pages', [])
                if visited_pages:
                    print(f"\n📄 Visited Pages ({len(visited_pages)}):")
                    for i, page in enumerate(visited_pages[:10], 1):
                        print(f"  {i}. {page}")
                    if len(visited_pages) > 10:
                        print(f"  ... and {len(visited_pages) - 10} more")
                
                # Save to file
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"forex_factory_crawled_{timestamp}.json"
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                print(f"\n💾 Crawl data saved to: {filename}")
                
                return data
            else:
                print(f"❌ Crawling failed: {data.get('error', 'Unknown error')}")
                return None
        else:
            print(f"❌ HTTP Error {response.status_code}")
            return None
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def main():
    """Run all tests"""
    print("\n" + "="*70)
    print("  FOREX FACTORY SCRAPER TEST")
    print("  Testing JavaScript-heavy page with anti-bot protection")
    print("="*70)
    
    # Test API health
    if not test_api_health():
        return
    
    # Scrape the article
    scrape_result = scrape_forex_factory()
    
    if scrape_result:
        # Crawl for links
        crawl_result = crawl_forex_factory()
    
    # Summary
    print_section("Test Summary")
    print("✅ API Health Check: Passed")
    print(f"{'✅' if scrape_result else '❌'} Article Scraping: {'Passed' if scrape_result else 'Failed'}")
    
    if scrape_result:
        sections = scrape_result.get('sections', {})
        stats = scrape_result.get('stats', {})
        resources = sections.get('resources', {})
        print(f"\nScraped Content:")
        print(f"  - {stats.get('word_count', 0)} words")
        print(f"  - {len(resources.get('links', []))} links")
        print(f"  - {len(resources.get('images', []))} images")
    
    print("\n" + "="*70)
    print("  TEST COMPLETE")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()
