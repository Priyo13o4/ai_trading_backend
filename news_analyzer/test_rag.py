#!/usr/bin/env python3
"""
Test RAG functionality by searching for similar articles in the vector database.
"""

import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from db_manager import DatabaseManager
from analyzer import GeminiAnalyzer
from config import Config
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_rag_search():
    """Test vector similarity search for RAG"""
    
    # Initialize components
    db_manager = DatabaseManager()  # Uses Config.DATABASE_URL by default
    
    analyzer = GeminiAnalyzer(
        api_key=Config.GEMINI_API_KEY,
        model_name=Config.GEMINI_MODEL,
        db_manager=db_manager
    )
    
    # Test queries
    test_queries = [
        "Federal Reserve raises interest rates due to inflation",
        "ECB announces new monetary policy",
        "Trump announces new tariffs on China",
        "Bitcoin price surges after institutional adoption"
    ]
    
    print("\n" + "="*80)
    print("RAG SIMILARITY SEARCH TEST")
    print("="*80)
    
    for query in test_queries:
        print(f"\n📝 Query: {query}")
        print("-" * 80)
        
        # Generate embedding
        logger.info(f"Generating embedding for: {query}")
        embedding = analyzer.generate_embedding(query)
        
        if not embedding:
            print("❌ Failed to generate embedding")
            continue
        
        # Search for similar articles
        logger.info("Searching for similar articles...")
        similar_articles = db_manager.search_similar_news(
            embedding=embedding,
            limit=5,
            similarity_threshold=0.6
        )
        
        if not similar_articles:
            print("❌ No similar articles found")
            continue
        
        print(f"\n✅ Found {len(similar_articles)} similar articles:\n")
        
        for idx, article in enumerate(similar_articles, 1):
            headline = article.get('headline', 'Unknown')
            created_at = article.get('created_at')
            instruments = article.get('forex_instruments', [])
            importance = article.get('importance_score', 'N/A')
            sentiment = article.get('sentiment_score', 0.0)
            similarity = article.get('similarity', 0.0)
            
            date_str = created_at.strftime('%Y-%m-%d %H:%M') if created_at else 'Unknown'
            instruments_str = ', '.join(instruments[:3]) if instruments else 'None'
            
            print(f"{idx}. [{date_str}] {headline[:80]}...")
            print(f"   📊 Similarity: {similarity:.3f} | Importance: {importance}/5 | "
                  f"Sentiment: {sentiment:+.2f}")
            print(f"   💱 Instruments: {instruments_str}")
            print()
    
    print("="*80)
    print("Test complete!")
    print("="*80)


if __name__ == "__main__":
    try:
        test_rag_search()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)
        sys.exit(1)
