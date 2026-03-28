#!/usr/bin/env python3
"""
Test full pipeline with specific articles (valid and invalid).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db_manager import DatabaseManager
from analyzer import GeminiAnalyzer
from scraper_client import ScraperClient
from config import Config
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_article(content_id: str, url: str, db_manager, scraper, analyzer):
    """Test full pipeline for a single article"""
    
    print(f"\n{'='*80}")
    print(f"Testing Article: {content_id}")
    print(f"URL: {url}")
    print(f"{'='*80}\n")
    
    # Step 1: Scrape
    print("📥 STEP 1: Scraping article...")
    scraped_data = scraper.scrape_article(url)
    
    if not scraped_data:
        print("❌ Scraping failed completely\n")
        return False
    
    print(f"✓ Scraped {len(scraped_data.get('content', ''))} characters")
    print(f"  - Published: {scraped_data.get('published_date', 'unknown')}")
    print(f"  - Category: {scraped_data.get('forexfactory_category', 'unknown')}")
    print(f"  - Valid: {scraped_data.get('is_valid', True)}")
    print(f"  - Validation reason: {scraped_data.get('validation_reason', 'N/A')}")
    
    # Check validation
    if not scraped_data.get('is_valid', True):
        print(f"\n⚠️  Article failed validation: {scraped_data.get('validation_reason')}")
        print("❌ Pipeline stopped (as expected for invalid articles)\n")
        return False
    
    # Check published date
    if not scraped_data.get('published_date'):
        print("\n⚠️  No published date found - using current timestamp for testing")
        scraped_data['published_date'] = None  # Will use DEFAULT in DB
    
    content = scraped_data['content']
    headline = scraped_data.get('metadata', {}).get('title', 'Unknown headline')
    
    print(f"\n📰 Headline: {headline[:100]}...")
    print(f"📝 Content preview: {content[:200]}...\n")
    
    # Step 2: RAG Analysis
    print("🤖 STEP 2: Analyzing with RAG...")
    analysis = analyzer.analyze_with_rag(
        headline=headline,
        content=content,
        url=url,
        us_political_related=False,
    )
    if not analysis:
        print("❌ Analysis failed\n")
        return False
    
    print(f"✓ Analysis complete")
    print(f"  - Forex relevant: {analysis.get('forex_relevant')}")
    print(f"  - Importance: {analysis.get('importance_score')}/5")
    print(f"  - Sentiment: {analysis.get('sentiment_score', 0):+.2f}")
    print(f"  - Primary instrument: {analysis.get('primary_instrument')}")
    print(f"  - Similar articles used: {len(analysis.get('similar_news_ids', []))}")
    if analysis.get('similar_news_ids'):
        print(f"    IDs: {analysis.get('similar_news_ids')}")
    
    # Show analysis summary and stored content
    print(f"\n📊 Analysis Summary:")
    summary = analysis.get('ai_analysis_summary', '')
    print(f"  {summary[:300]}..." if len(summary) > 300 else f"  {summary}")
    
    print(f"\n📄 Stored Content Preview (first 500 chars):")
    print(f"  {content[:500]}...")

    # Verify new user-centric fields exist in the response
    user_centric_fields = [
        'human_takeaway', 'attention_score', 'news_state', 'market_pressure',
        'attention_window', 'confidence_label', 'expected_followups'
    ]
    missing_user_fields = [f for f in user_centric_fields if f not in analysis]
    if missing_user_fields:
        print(f"\n❌ Missing user-centric fields in analysis JSON: {missing_user_fields}")
        return False

    print("\n👤 User-Centric Layer:")
    print(f"  - human_takeaway: {analysis.get('human_takeaway')}")
    print(f"  - attention_score: {analysis.get('attention_score')}")
    print(f"  - news_state: {analysis.get('news_state')}")
    print(f"  - market_pressure: {analysis.get('market_pressure')}")
    print(f"  - attention_window: {analysis.get('attention_window')}")
    print(f"  - confidence_label: {analysis.get('confidence_label')}")
    print(f"  - expected_followups: {analysis.get('expected_followups')}")
    
    # Step 3: Generate embedding
    print("\n🧮 STEP 3: Generating embedding...")
    embedding_text = analysis.get('content_for_embedding', '') or headline
    embedding = analyzer.generate_embedding(embedding_text)
    
    if not embedding:
        print("❌ Embedding generation failed\n")
        return False
    
    print(f"✓ Generated embedding ({len(embedding)} dimensions)")
    
    # Step 4: Store in database
    print("\n💾 STEP 4: Storing in database...")
    
    # Store analysis
    email_id = db_manager.upsert_analysis(
        content_id=content_id,
        analysis_data=analysis,
        scraped_content=content,
        published_date=scraped_data['published_date'],
        headline=headline,
        forexfactory_url=url
    )
    
    if not email_id:
        print("❌ Failed to store analysis\n")
        return False
    
    print(f"✓ Stored analysis (email_id: {email_id})")
    
    # Store embedding
    metadata = {
        'headline': headline,
        'primary_instrument': analysis.get('primary_instrument'),
        'importance_score': analysis.get('importance_score'),
        'news_category': analysis.get('news_category'),
        'email_id': email_id
    }
    
    vector_id = db_manager.insert_vector_embedding(
        content_id=content_id,
        content=embedding_text[:1000],
        embedding=embedding,
        metadata=metadata
    )
    
    if not vector_id:
        print("❌ Failed to store embedding\n")
        return False
    
    print(f"✓ Stored embedding (vector_id: {vector_id})")
    
    # Update vector_store_id
    updated = db_manager.update_vector_store_id(content_id, vector_id)
    if updated:
        print(f"✓ Linked vector to analysis")
    
    # Step 5: Verify in database
    print("\n✅ STEP 5: Verifying in database...")
    
    with db_manager.get_connection() as conn:
        with conn.cursor() as cur:
            # Check analysis
            cur.execute("""
                SELECT email_id, headline, ai_analysis_summary, 
                       forex_relevant, importance_score, sentiment_score,
                       similar_news_ids, vector_store_id, forexfactory_urls,
                       human_takeaway, attention_score, news_state, market_pressure,
                       attention_window, confidence_label, expected_followups
                FROM email_news_analysis 
                WHERE forexfactory_content_id = %s
            """, (content_id,))
            
            row = cur.fetchone()
            if row:
                print(f"✓ Found in email_news_analysis:")
                print(f"  - email_id: {row[0]}")
                print(f"  - headline: {row[1][:80]}...")
                print(f"  - summary: {row[2][:100] if row[2] else 'None'}...")
                print(f"  - forex_relevant: {row[3]}")
                print(f"  - importance: {row[4]}")
                print(f"  - sentiment: {row[5]}")
                print(f"  - similar_news_ids: {row[6]}")
                print(f"  - vector_store_id: {row[7]}")
                print(f"  - forexfactory_urls: {row[8]}")
                print(f"  - human_takeaway: {row[9]}")
                print(f"  - attention_score: {row[10]}")
                print(f"  - news_state: {row[11]}")
                print(f"  - market_pressure: {row[12]}")
                print(f"  - attention_window: {row[13]}")
                print(f"  - confidence_label: {row[14]}")
                print(f"  - expected_followups: {row[15]}")
            else:
                print("❌ Not found in email_news_analysis")
                return False
            
            # Check vector
            cur.execute("""
                SELECT id, metadata->>'headline', 
                       pg_column_size(embedding_half) as embedding_size
                FROM email_news_vectors
                WHERE metadata->>'forexfactory_content_id' = %s
            """, (content_id,))
            
            row = cur.fetchone()
            if row:
                print(f"\n✓ Found in email_news_vectors:")
                print(f"  - vector_id: {row[0]}")
                print(f"  - headline: {row[1][:80] if row[1] else 'None'}...")
                print(f"  - embedding_size: {row[2]} bytes")
            else:
                print("❌ Not found in email_news_vectors")
                return False
    
    print(f"\n{'='*80}")
    print("✅ PIPELINE TEST PASSED")
    print(f"{'='*80}\n")
    
    return True


def main():
    """Test two articles - one valid, one invalid"""
    
    # Initialize components
    db_manager = DatabaseManager()
    scraper = ScraperClient()
    analyzer = GeminiAnalyzer(db_manager=db_manager)
    
    test_articles = [
        ("1378030", "https://www.forexfactory.com/news/1378030")
    ]
    
    print("\n" + "="*80)
    print("FULL PIPELINE TEST - Valid and Invalid Articles")
    print("="*80)
    
    results = {}
    
    for content_id, url in test_articles:
        try:
            success = test_article(content_id, url, db_manager, scraper, analyzer)
            results[content_id] = "PASSED" if success else "FAILED (expected for invalid)"
        except Exception as e:
            logger.error(f"Error testing {content_id}: {e}", exc_info=True)
            results[content_id] = f"ERROR: {e}"
    
    # Summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    for content_id, result in results.items():
        print(f"Article {content_id}: {result}")
    print("="*80)
    
    # Ask user to verify
    print("\n📊 You can now query the database to verify:")
    print(f"   SELECT * FROM email_news_analysis WHERE forexfactory_content_id IN ('1378030');")
    print(f"\n🧹 To clean up test data:")
    print(f"   DELETE FROM email_news_vectors WHERE metadata->>'forexfactory_content_id' IN ('1378030');")
    print(f"   DELETE FROM email_news_analysis WHERE forexfactory_content_id IN ('1378030');")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)
        sys.exit(1)
