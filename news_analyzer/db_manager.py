"""
Database Manager for News Analysis
Handles database operations including fetching unprocessed news and storing analysis results.
"""
import psycopg
from psycopg.rows import dict_row
from typing import List, Dict, Optional, Tuple
import logging
from datetime import datetime
import json

from config import Config

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages all database operations for news analysis"""
    
    def __init__(self, database_url: str = None):
        self.database_url = database_url or Config.DATABASE_URL
        self._ensure_vector_extension()
    
    def _ensure_vector_extension(self):
        """Ensure pgvector extension is enabled"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                    conn.commit()
            logger.info("pgvector extension ensured")
        except Exception as e:
            logger.warning(f"Could not ensure pgvector extension: {e}")
    
    def get_connection(self):
        """Get a new database connection"""
        return psycopg.connect(self.database_url)
    
    def get_last_analyzed_content_id(self) -> Optional[str]:
        """
        Get the last successfully analyzed forexfactory_content_id.
        Used for resume capability.
        
        Returns:
            str: The last analyzed content_id, or None if no records exist
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT forexfactory_content_id
                        FROM email_news_analysis
                        WHERE forexfactory_content_id IS NOT NULL
                        ORDER BY created_at DESC
                        LIMIT 1
                    """)
                    result = cur.fetchone()
                    if result:
                        last_id = result[0]
                        logger.info(f"Resume point found: {last_id}")
                        return last_id
                    else:
                        logger.info("No previous analysis found, starting from beginning")
                        return None
        except Exception as e:
            logger.error(f"Error fetching last analyzed content ID: {e}")
            return None
    
    def get_unprocessed_news_urls(self, limit: int = None, start_after_id: str = None) -> List[Dict]:
        """
        Get all news URLs from email_news_analysis that need processing.
        
        This fetches records that have a forexfactory_content_id but may need reprocessing.
        For backfill, we'll process all historical records.
        
        Args:
            limit: Maximum number of records to fetch (None for all)
            start_after_id: Resume from this content_id (exclusive)
        
        Returns:
            List of dicts with url, content_id, headline, and published_date info
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    # Build query based on resume point
                    if start_after_id:
                        query = """
                            SELECT 
                                forexfactory_content_id,
                                headline,
                                email_received_at,
                                created_at
                            FROM email_news_analysis
                            WHERE forexfactory_content_id IS NOT NULL
                                AND created_at > (
                                    SELECT created_at 
                                    FROM email_news_analysis 
                                    WHERE forexfactory_content_id = %s
                                )
                            ORDER BY created_at ASC
                        """
                        params = [start_after_id]
                    else:
                        query = """
                            SELECT 
                                forexfactory_content_id,
                                headline,
                                email_received_at,
                                created_at
                            FROM email_news_analysis
                            WHERE forexfactory_content_id IS NOT NULL
                            ORDER BY created_at ASC
                        """
                        params = []
                    
                    if limit:
                        query += " LIMIT %s"
                        params.append(limit)
                    
                    cur.execute(query, params)
                    results = cur.fetchall()
                    
                    # Construct ForexFactory URLs
                    news_items = []
                    for row in results:
                        content_id = row['forexfactory_content_id']
                        news_items.append({
                            'url': f"https://www.forexfactory.com/news/{content_id}",
                            'content_id': content_id,
                            'headline': row['headline'],
                            'email_received_at': row['email_received_at'],
                            'db_created_at': row['created_at']
                        })
                    
                    logger.info(f"Found {len(news_items)} news items to process")
                    return news_items
                    
        except Exception as e:
            logger.error(f"Error fetching unprocessed news: {e}")
            return []
    
    def check_if_analyzed(self, content_id: str) -> bool:
        """
        Check if a news article has already been analyzed with AI.
        
        Args:
            content_id: The forexfactory_content_id
        
        Returns:
            bool: True if analysis exists with ai_analysis_summary
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Treat as "analyzed" only if the core analysis exists AND the newer
                    # user-centric fields are populated. This allows reprocessing older rows
                    # that were generated before the schema/prompt upgrade.
                    cur.execute("""
                        SELECT COUNT(*)
                        FROM email_news_analysis
                        WHERE forexfactory_content_id = %s
                            AND ai_analysis_summary IS NOT NULL
                            AND ai_analysis_summary != ''
                            AND human_takeaway IS NOT NULL
                            AND human_takeaway != ''
                            AND attention_score IS NOT NULL
                            AND news_state IS NOT NULL
                            AND market_pressure IS NOT NULL
                            AND attention_window IS NOT NULL
                            AND confidence_label IS NOT NULL
                            AND expected_followups IS NOT NULL
                    """, (content_id,))
                    count = cur.fetchone()[0]
                    return count > 0
        except Exception as e:
            logger.error(f"Error checking if analyzed: {e}")
            return False
    
    def upsert_analysis(self, content_id: str, analysis_data: Dict, scraped_content: str, 
                       published_date: Optional[datetime] = None, headline: str = None,
                       forexfactory_url: str = None, forexfactory_category: str = None) -> Optional[int]:
        """
        Insert or update news analysis in the database.
        
        Args:
            content_id: The forexfactory_content_id
            analysis_data: Dict containing AI analysis results
            scraped_content: The full scraped article content
            published_date: Article published date (will be stored in email_received_at)
            headline: Article headline
            forexfactory_url: Full ForexFactory article URL
            forexfactory_category: ForexFactory category (e.g., 'High Impact Breaking News', 'Technical Analysis')
        
        Returns:
            int: The email_id (primary key), or None on error
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Simple INSERT for backfill (no conflict expected)
                    cur.execute("""
                        INSERT INTO email_news_analysis (
                            forexfactory_content_id,
                            headline,
                            original_email_content,
                            ai_analysis_summary,
                            forex_relevant,
                            forex_instruments,
                            primary_instrument,
                            us_political_related,
                            forexfactory_category,
                            trade_deal_related,
                            central_bank_related,
                            importance_score,
                            sentiment_score,
                            analysis_confidence,
                            news_category,
                            entities_mentioned,
                            trading_sessions,
                            market_impact_prediction,
                            impact_timeframe,
                            volatility_expectation,
                            similar_news_context,
                            similar_news_ids,
                            human_takeaway,
                            attention_score,
                            news_state,
                            market_pressure,
                            attention_window,
                            confidence_label,
                            expected_followups,
                            email_received_at,
                            forexfactory_urls
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        RETURNING email_id
                    """, (
                        content_id,
                        headline,
                        scraped_content[:5000],  # Limit content length
                        analysis_data.get('ai_analysis_summary'),
                        analysis_data.get('forex_relevant', False),
                        analysis_data.get('forex_instruments', []),
                        analysis_data.get('primary_instrument'),
                        analysis_data.get('us_political_related', False),
                        forexfactory_category,
                        self._detect_trade_deal_related(analysis_data),
                        self._detect_central_bank_related(analysis_data),
                        analysis_data.get('importance_score'),
                        float(analysis_data.get('sentiment_score', 0)),
                        float(analysis_data.get('analysis_confidence', 0)),
                        analysis_data.get('news_category'),
                        analysis_data.get('entities_mentioned', []),
                        analysis_data.get('trading_sessions', []),
                        analysis_data.get('market_impact_prediction'),
                        analysis_data.get('impact_timeframe'),
                        analysis_data.get('volatility_expectation'),
                        analysis_data.get('similar_news_context'),
                        analysis_data.get('similar_news_ids', []),
                        analysis_data.get('human_takeaway'),
                        analysis_data.get('attention_score'),
                        analysis_data.get('news_state'),
                        analysis_data.get('market_pressure'),
                        analysis_data.get('attention_window'),
                        analysis_data.get('confidence_label'),
                        analysis_data.get('expected_followups', []),
                        published_date,
                        [forexfactory_url] if forexfactory_url else None
                    ))
                    
                    result = cur.fetchone()
                    conn.commit()
                    
                    if result:
                        email_id = result[0]
                        logger.info(f"Successfully upserted analysis for content_id {content_id} (email_id: {email_id})")
                        return email_id
                    else:
                        logger.error(f"Upsert returned no email_id for content_id {content_id}")
                        return None
                        
        except Exception as e:
            logger.error(f"Error upserting analysis for {content_id}: {e}")
            return None
    
    def _detect_trade_deal_related(self, analysis_data: Dict) -> bool:
        """Helper to detect trade deal related news"""
        entities = analysis_data.get('entities_mentioned', [])
        category = analysis_data.get('news_category', '')
        summary = analysis_data.get('ai_analysis_summary', '').lower()
        
        trade_keywords = ['trade', 'deal', 'usmca', 'tariff', 'agreement']
        return (category == 'trade' or 
                any(kw in summary for kw in trade_keywords) or
                any('trade' in str(e).lower() for e in entities))
    
    def _detect_central_bank_related(self, analysis_data: Dict) -> bool:
        """Helper to detect central bank related news"""
        entities = analysis_data.get('entities_mentioned', [])
        category = analysis_data.get('news_category', '')
        
        cb_keywords = ['fed', 'ecb', 'boe', 'central bank', 'federal reserve', 'monetary policy']
        return (category == 'central_bank' or
                any(keyword in str(e).lower() for e in entities for keyword in cb_keywords))
    
    def insert_vector_embedding(self, content_id: str, content: str, embedding: List[float],
                               metadata: Dict) -> Optional[int]:
        """
        Insert vector embedding into email_news_vectors table.
        
        Args:
            content_id: The forexfactory_content_id
            content: Text content that was embedded
            embedding: The embedding vector (768 dimensions for Gemini)
            metadata: Additional metadata as JSONB
        
        Returns:
            int: The vector_store_id (primary key), or None on error
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Add content_id to metadata
                    metadata['forexfactory_content_id'] = content_id
                    
                    cur.execute("""
                        INSERT INTO email_news_vectors (content, embedding_half, metadata)
                        VALUES (%s, %s::halfvec, %s)
                        RETURNING id
                    """, (
                        content,
                        embedding,
                        json.dumps(metadata)
                    ))
                    
                    result = cur.fetchone()
                    if result:
                        vector_id = result[0]
                        conn.commit()
                        logger.info(f"Inserted vector embedding for {content_id} (vector_id: {vector_id})")
                        return vector_id
                    else:
                        logger.error(f"No vector_id returned for {content_id}")
                        return None
                    
        except Exception as e:
            logger.error(f"Error inserting vector embedding for {content_id}: {e}", exc_info=True)
            return None
    
    def update_vector_store_id(self, content_id: str, vector_store_id: int) -> bool:
        """
        Update the vector_store_id in email_news_analysis table.
        
        Args:
            content_id: The forexfactory_content_id
            vector_store_id: The ID from email_news_vectors
        
        Returns:
            bool: True if successful
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE email_news_analysis
                        SET vector_store_id = %s
                        WHERE forexfactory_content_id = %s
                    """, (vector_store_id, content_id))
                    conn.commit()
                    logger.info(f"Updated vector_store_id for {content_id}")
                    return True
        except Exception as e:
            logger.error(f"Error updating vector_store_id: {e}")
            return False
    
    def search_similar_vectors(self, embedding: List[float], limit: int = 5) -> List[Dict]:
        """
        Search for similar news articles using vector similarity.
        
        Args:
            embedding: Query embedding vector
            limit: Number of similar results to return
        
        Returns:
            List of dicts with similar news info and similarity scores
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("""
                        SELECT 
                            id,
                            content,
                            metadata,
                            1 - (embedding_half <=> %s::halfvec) as similarity
                        FROM email_news_vectors
                        ORDER BY embedding_half <=> %s::halfvec
                        LIMIT %s
                    """, (embedding, embedding, limit))
                    
                    results = cur.fetchall()
                    logger.info(f"Found {len(results)} similar vectors")
                    return results
                    
        except Exception as e:
            logger.error(f"Error searching similar vectors: {e}")
            return []
    
    def search_similar_news(self, embedding: list, limit: int = 5, 
                           similarity_threshold: float = 0.7) -> list:
        """
        Search for similar news articles using vector similarity with full article context.
        
        Joins email_news_vectors with email_news_analysis to get:
        - Vector similarity
        - Analysis summary and metadata
        - Trading-related information (instruments, sentiment, importance)
        
        Args:
            embedding: Query embedding vector (768 dimensions)
            limit: Maximum number of similar articles to return (default: 5)
            similarity_threshold: Minimum similarity score (0-1, default: 0.7)
        
        Returns:
            List of dicts with: email_id, headline, ai_analysis_summary, forex_instruments,
            importance_score, sentiment_score, created_at, similarity
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    # Query joins vectors with analysis for complete context
                    cur.execute("""
                        SELECT 
                            a.email_id,
                            a.headline,
                            a.ai_analysis_summary,
                            a.forex_instruments,
                            a.importance_score,
                            a.sentiment_score,
                            a.news_category,
                            a.market_impact_prediction,
                            a.email_received_at,
                            a.created_at,
                            1 - (v.embedding_half <=> %s::halfvec) as similarity
                        FROM email_news_vectors v
                        JOIN email_news_analysis a 
                            ON v.metadata->>'forexfactory_content_id' = a.forexfactory_content_id
                        WHERE a.ai_analysis_summary IS NOT NULL
                            AND a.email_received_at IS NOT NULL
                            AND a.email_received_at > NOW() - INTERVAL '180 days'
                            AND (1 - (v.embedding_half <=> %s::halfvec)) >= %s
                        ORDER BY similarity DESC, a.email_received_at DESC
                        LIMIT %s
                    """, (embedding, embedding, similarity_threshold, limit))
                    
                    results = cur.fetchall()
                    
                    if results:
                        logger.info(f"Found {len(results)} similar articles (threshold: {similarity_threshold})")
                        for idx, result in enumerate(results, 1):
                            logger.debug(f"  {idx}. [{result['created_at']}] {result['headline'][:60]}... "
                                       f"(similarity: {result['similarity']:.3f})")
                    else:
                        logger.info(f"No similar articles found above threshold {similarity_threshold}")
                    
                    return results
                    
        except Exception as e:
            logger.error(f"Error searching similar news: {e}")
            return []
    
    def get_news_count_by_status(self) -> Dict[str, int]:
        """Get statistics about processed vs unprocessed news"""
        try:
            with self.get_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("""
                        SELECT 
                            COUNT(*) as total,
                            COUNT(CASE WHEN ai_analysis_summary IS NOT NULL THEN 1 END) as analyzed,
                            COUNT(CASE WHEN ai_analysis_summary IS NULL THEN 1 END) as unanalyzed
                        FROM email_news_analysis
                        WHERE forexfactory_content_id IS NOT NULL
                    """)
                    result = cur.fetchone()
                    return dict(result)
        except Exception as e:
            logger.error(f"Error getting news count: {e}")
            return {'total': 0, 'analyzed': 0, 'unanalyzed': 0}
