"""
Test script for verifying the backfill pipeline with provided URLs
Tests scraping, category extraction, and database storage
"""
import sys
import logging
from typing import List
from datetime import datetime

from config import Config
from scraper_client import ScraperClient
from analyzer import GeminiAnalyzer
from db_manager import DatabaseManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('test_provided_urls.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Test URLs
TEST_URLS = [
    "https://www.forexfactory.com/news/1377736",
    "https://www.forexfactory.com/news/1377601",
    "https://www.forexfactory.com/news/1377504"
]


def test_url(url: str, scraper: ScraperClient, analyzer: GeminiAnalyzer, db: DatabaseManager) -> dict:
    """
    Test a single URL through the entire pipeline
    
    Returns:
        dict with test results
    """
    logger.info("=" * 80)
    logger.info(f"Testing URL: {url}")
    logger.info("=" * 80)
    
    result = {
        'url': url,
        'content_id': url.split('/')[-1],
        'scraping_success': False,
        'category_found': False,
        'category_value': None,
        'analysis_success': False,
        'db_storage_success': False,
        'errors': []
    }
    
    try:
        # Step 1: Scrape the article
        logger.info(f"Step 1: Scraping article...")
        scraped_data = scraper.scrape_article(url, force_selenium=True)
        
        if not scraped_data:
            error = "Scraping failed - no data returned"
            logger.error(error)
            result['errors'].append(error)
            return result
        
        result['scraping_success'] = True
        
        # Check if content is valid
        if not scraped_data.get('is_valid', False):
            error = f"Content validation failed: {scraped_data.get('validation_reason', 'Unknown')}"
            logger.warning(error)
            result['errors'].append(error)
        
        # Step 2: Check category extraction
        logger.info(f"Step 2: Checking category extraction...")
        category = scraped_data.get('forexfactory_category')
        
        if category:
            result['category_found'] = True
            result['category_value'] = category
            logger.info(f"✓ Category found: {category}")
        else:
            error = "No category extracted from article"
            logger.warning(error)
            result['errors'].append(error)
        
        # Display scraped data summary
        logger.info(f"Scraped content summary:")
        logger.info(f"  - Method: {scraped_data.get('method')}")
        logger.info(f"  - Word count: {scraped_data.get('word_count')}")
        logger.info(f"  - Content length: {len(scraped_data.get('content', ''))} chars")
        logger.info(f"  - Published date: {scraped_data.get('published_date')}")
        logger.info(f"  - Category: {category or 'NOT FOUND'}")
        logger.info(f"  - Is valid: {scraped_data.get('is_valid')}")
        
        # Step 3: Analyze with AI (skip if category not in allowed list)
        logger.info(f"Step 3: Analyzing with AI...")
        
        # Extract content_id from URL
        content_id = url.split('/')[-1]
        
        # Analyze
        analysis_result = analyzer.analyze_with_rag(
            headline=scraped_data.get('metadata', {}).get('title', 'Unknown'),
            content=scraped_data['content'],
            content_id=content_id
        )
        
        if analysis_result:
            result['analysis_success'] = True
            logger.info(f"✓ Analysis completed")
            logger.info(f"  - Forex relevant: {analysis_result.get('forex_relevant')}")
            logger.info(f"  - Primary instrument: {analysis_result.get('primary_instrument')}")
            logger.info(f"  - Sentiment score: {analysis_result.get('sentiment_score')}")
            logger.info(f"  - Confidence: {analysis_result.get('analysis_confidence')}")
            logger.info(f"  - News category (AI): {analysis_result.get('news_category')}")
        else:
            error = "AI analysis failed"
            logger.error(error)
            result['errors'].append(error)
            return result
        
        # Step 4: Check database storage (verify category column)
        logger.info(f"Step 4: Testing database storage...")
        
        try:
            email_id = db.upsert_analysis(
                content_id=content_id,
                analysis_data=analysis_result,
                scraped_content=scraped_data['content'],
                published_date=scraped_data.get('published_date'),
                headline=scraped_data.get('metadata', {}).get('title'),
                forexfactory_url=url,
                forexfactory_category=category
            )
            
            if email_id:
                result['db_storage_success'] = True
                result['email_id'] = email_id
                logger.info(f"✓ Database storage successful (email_id: {email_id})")
                logger.info(f"✓ Category value stored: {category}")
            else:
                error = "Database storage returned no email_id"
                logger.error(error)
                result['errors'].append(error)
                
        except Exception as e:
            error = f"Database storage error: {str(e)}"
            logger.error(error)
            result['errors'].append(error)
        
    except Exception as e:
        error = f"Unexpected error: {str(e)}"
        logger.error(error, exc_info=True)
        result['errors'].append(error)
    
    return result


def main():
    """Main test function"""
    logger.info("=" * 80)
    logger.info("Starting Backfill Pipeline Test")
    logger.info(f"Testing {len(TEST_URLS)} URLs")
    logger.info("=" * 80)
    
    # Validate configuration
    try:
        Config.validate()
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        return
    
    # Initialize components
    logger.info("Initializing components...")
    scraper = ScraperClient()
    analyzer = GeminiAnalyzer()
    db = DatabaseManager()
    
    # Test each URL
    results = []
    for url in TEST_URLS:
        result = test_url(url, scraper, analyzer, db)
        results.append(result)
    
    # Print summary
    logger.info("\n" + "=" * 80)
    logger.info("TEST SUMMARY")
    logger.info("=" * 80)
    
    for result in results:
        logger.info(f"\nURL: {result['url']}")
        logger.info(f"  Content ID: {result['content_id']}")
        logger.info(f"  ✓ Scraping: {'SUCCESS' if result['scraping_success'] else 'FAILED'}")
        logger.info(f"  ✓ Category Found: {'YES' if result['category_found'] else 'NO'}")
        if result['category_value']:
            logger.info(f"    Category Value: {result['category_value']}")
        logger.info(f"  ✓ Analysis: {'SUCCESS' if result['analysis_success'] else 'FAILED'}")
        logger.info(f"  ✓ DB Storage: {'SUCCESS' if result['db_storage_success'] else 'FAILED'}")
        
        if result['errors']:
            logger.info(f"  ⚠ Errors: {len(result['errors'])}")
            for error in result['errors']:
                logger.info(f"    - {error}")
    
    # Overall success rate
    total = len(results)
    scraping_success = sum(1 for r in results if r['scraping_success'])
    category_found = sum(1 for r in results if r['category_found'])
    analysis_success = sum(1 for r in results if r['analysis_success'])
    db_success = sum(1 for r in results if r['db_storage_success'])
    
    logger.info(f"\n{'=' * 80}")
    logger.info("OVERALL RESULTS")
    logger.info(f"{'=' * 80}")
    logger.info(f"Total URLs tested: {total}")
    logger.info(f"Scraping success rate: {scraping_success}/{total} ({scraping_success/total*100:.1f}%)")
    logger.info(f"Category extraction rate: {category_found}/{total} ({category_found/total*100:.1f}%)")
    logger.info(f"Analysis success rate: {analysis_success}/{total} ({analysis_success/total*100:.1f}%)")
    logger.info(f"DB storage success rate: {db_success}/{total} ({db_success/total*100:.1f}%)")
    
    if db_success == total and category_found == total:
        logger.info("\n✓ ALL TESTS PASSED!")
    else:
        logger.info("\n⚠ SOME TESTS FAILED - Check logs for details")


if __name__ == "__main__":
    main()
