"""
Main Orchestration Script for News Analysis Backfill
Fetches unprocessed news, scrapes articles, analyzes with Gemini, and stores results.
"""
import logging
import os
import sys
import time
import re
import json
import signal
from typing import Dict, Optional
from datetime import datetime

from config import Config
from db_manager import DatabaseManager
from scraper_client import ScraperClient, CloudflareChallengeUnsolvedError
from analyzer import GeminiAnalyzer, RateLimitError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('news_analyzer.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


class TransientScrapeError(Exception):
    """Raised for transient scraping failures (network/HTTP/Scraper issues)."""


class TransientAIError(Exception):
    """Raised for retryable Gemini failures (analysis/embedding) without re-scraping."""

    def __init__(self, message: str, *, payload: Optional[Dict] = None):
        super().__init__(message)
        self.payload = payload


class NewsAnalysisOrchestrator:
    """Orchestrates the news analysis backfill process"""
    
    # Categories to analyze - only process these, skip all others
    ALLOWED_CATEGORIES = [
        "Breaking News / High Impact",
        "Breaking News / Medium Impact",
        "Breaking News / Low Impact",
        "Breaking News (High Impact)",
        "Breaking News (Medium Impact)",
        "Breaking News (Low Impact)",
        "High Impact Breaking News",
        "Medium Impact Breaking News",
        "Low Impact Breaking News",
        "Fundamental Analysis",
        "Technical Analysis"
    ]
    
    def __init__(self):
        # Validate configuration
        Config.validate()
        
        # Initialize components
        self.db_manager = DatabaseManager()
        self.scraper_client = ScraperClient()
        self.analyzer = GeminiAnalyzer(db_manager=self.db_manager)
        
        # Statistics
        self.stats = {
            'total_processed': 0,
            'successful': 0,
            'failed': 0,
            'skipped': 0,
            'started_at': datetime.now()
        }
    
    def run(self, limit: Optional[int] = None):
        """
        Main execution method for backfill process.
        
        Args:
            limit: Maximum number of articles to process (None for all)
        """
        logger.info("=" * 80)
        logger.info("Starting News Analysis Backfill")
        logger.info("=" * 80)
        
        # Test connections
        if not self._test_connections():
            logger.error("Connection tests failed. Exiting.")
            return
        
        # Get resume point - start from last content_id in database
        start_content_id = None
        if Config.ENABLE_RESUME:
            start_content_id = self.db_manager.get_last_analyzed_content_id()
            if start_content_id:
                # Convert to int and increment by 1 to start from next
                try:
                    start_content_id = int(start_content_id) + 1
                    logger.info(f"Resuming from content_id: {start_content_id}")
                except ValueError:
                    logger.error(f"Invalid content_id format: {start_content_id}")
                    return
        
        if not start_content_id:
            logger.error("No starting content_id found. Cannot proceed with backfill.")
            return
        
        # Get statistics
        stats = self.db_manager.get_news_count_by_status()
        logger.info(f"Database statistics: {stats}")
        
        # Generate sequential content_ids and process them
        logger.info(f"Starting backfill from content_id {start_content_id}")
        logger.info(f"Limit: {limit or 'unlimited'}")
        
        processed_count = 0
        current_content_id = start_content_id
        consecutive_failures = 0
        max_consecutive_failures = 50  # Stop if 50 consecutive invalid pages
        
        while True:
            # Check if we've reached the limit
            if limit and processed_count >= limit:
                logger.info(f"Reached limit of {limit} processed articles")
                break
            
            # Check if too many consecutive failures (might have reached end of available articles)
            if consecutive_failures >= max_consecutive_failures:
                logger.info(f"Reached {max_consecutive_failures} consecutive failures. Likely at end of available articles.")
                break
            
            logger.info(f"\n[{processed_count + 1}] Trying content_id: {current_content_id}")
            
            # Create news item dict
            news_item = {
                'url': f"https://www.forexfactory.com/news/{current_content_id}",
                'content_id': str(current_content_id),
                'headline': f"Article {current_content_id}",  # Will be updated after scraping
                'email_received_at': None,
                'db_created_at': None
            }
            
            try:
                # Try to process this content_id
                success = self._process_news_item(news_item)
                
                if success:
                    self.stats['successful'] += 1
                    processed_count += 1
                    consecutive_failures = 0  # Reset counter on success
                    
                    # Sleep between successful items
                    time.sleep(2)
                else:
                    # Page was invalid/skipped
                    consecutive_failures += 1
                
            except RateLimitError as e:
                # Rate limit should propagate to stop backfill until quota resets
                logger.error(f"Rate limit hit while processing {current_content_id}: {e}")
                logger.error("Stopping backfill. Quota will reset tomorrow or upgrade your plan.")
                raise
            except CloudflareChallengeUnsolvedError as e:
                logger.error(f"Cloudflare challenge not solved; stopping backfill: {e}")
                raise
            except Exception as e:
                logger.error(f"Failed to process {current_content_id}: {e}")
                self.stats['failed'] += 1
                consecutive_failures += 1
            
            finally:
                self.stats['total_processed'] += 1
                current_content_id += 1  # Move to next content_id
            
            # Progress update every 10 items
            if self.stats['total_processed'] % 10 == 0:
                self._print_progress()
        
        # Final summary
        self._print_final_summary()

    def run_range(
        self,
        start_id: int,
        end_id: int,
        *,
        continue_on_error: bool = False,
        sleep_seconds: float = 10.0,
        force_reprocess: bool = False,
    ) -> Dict[str, int]:
        """Process an inclusive numeric content_id range sequentially."""
        if not self._test_connections():
            logger.error("Connection tests failed. Exiting.")
            return {"processed": 0, "skipped": 0, "failed": 0}

        start_id = int(start_id)
        end_id = int(end_id)
        if end_id < start_id:
            start_id, end_id = end_id, start_id

        logger.info("=" * 80)
        logger.info(f"Starting range backfill: {start_id} -> {end_id}")
        logger.info(f"continue_on_error={continue_on_error}")
        logger.info(f"force_reprocess={force_reprocess}")
        logger.info("=" * 80)

        # Reset shared stats so Ctrl+C summary is accurate for range runs.
        self.stats['total_processed'] = 0
        self.stats['successful'] = 0
        self.stats['failed'] = 0
        self.stats['skipped'] = 0
        self.stats['started_at'] = datetime.now()

        started_monotonic = time.monotonic()

        processed = 0
        skipped = 0
        failed = 0
        deferred_ai: Dict[str, Dict] = {}
        deferred_recovered = 0
        deferred_failed = 0

        interrupted = False

        try:
            for cid in range(start_id, end_id + 1):
                news_item = {
                    'url': f"https://www.forexfactory.com/news/{cid}",
                    'content_id': str(cid),
                    'headline': f"Article {cid}",
                    'email_received_at': None,
                    'db_created_at': None
                }

                try:
                    ok = self._process_news_item(news_item, force_reprocess=force_reprocess)
                    if ok:
                        processed += 1
                    else:
                        skipped += 1
                except KeyboardInterrupt:
                    interrupted = True
                    logger.info("[STOP] Ctrl+C received; stopping range run now")
                    raise
                except TransientAIError as e:
                    # Retry Gemini failures once at end without re-scraping.
                    failed += 1
                    logger.error(f"Deferred AI retry for content_id {cid}: {e}")
                    if not getattr(e, "payload", None):
                        logger.error(
                            "TransientAIError missing payload; cannot defer without re-scrape"
                        )
                    else:
                        deferred_ai[str(cid)] = {
                            "payload": e.payload,
                        }
                except CloudflareChallengeUnsolvedError as e:
                    # User was AFK or challenge couldn't be cleared. Stop the run immediately.
                    logger.error(f"Cloudflare challenge not solved; stopping backfill: {e}")
                    raise
                except RateLimitError:
                    raise
                except Exception as e:
                    failed += 1
                    logger.error(f"Failed content_id {cid}: {type(e).__name__}: {e}")
                    if not continue_on_error:
                        raise

                # Keep shared stats in sync for Ctrl+C summary.
                done = processed + skipped + failed
                self.stats['total_processed'] = done
                self.stats['successful'] = processed
                self.stats['skipped'] = skipped
                self.stats['failed'] = failed

                # Slow, steady cadence reduces bot signals and avoids hammering.
                if sleep_seconds and sleep_seconds > 0:
                    time.sleep(float(sleep_seconds))

                if done % 25 == 0:
                    elapsed = time.monotonic() - started_monotonic
                    tokens = self.analyzer.get_token_totals()
                    logger.info(
                        self._format_run_line(
                            "PROGRESS",
                            done=done,
                            ok=processed,
                            skipped=skipped,
                            failed=failed,
                            elapsed_s=elapsed,
                            tokens=tokens,
                        )
                    )

            # Deferred retry pass (no re-scrape). We re-run the same content_id pipeline,
            # but only once, and only for AI-related failures.
            if deferred_ai and not interrupted:
                logger.info(
                    f"[DEFERRED] Retrying {len(deferred_ai)} AI-failed items at end (no re-scrape)"
                )
                for cid, payload in list(deferred_ai.items()):
                    try:
                        self._retry_ai_only(payload=payload["payload"])
                        deferred_recovered += 1
                        del deferred_ai[cid]
                        logger.info(f"[DEFERRED] ✓ Recovered content_id {cid}")
                    except CloudflareChallengeUnsolvedError:
                        raise
                    except RateLimitError:
                        raise
                    except Exception as e:
                        deferred_failed += 1
                        logger.error(f"[DEFERRED] ✗ Still failing content_id {cid}: {type(e).__name__}: {e}")

        finally:
            elapsed = time.monotonic() - started_monotonic
            tokens = self.analyzer.get_token_totals()
            done = processed + skipped + failed
            logger.info(
                self._format_run_line(
                    "DONE",
                    done=done,
                    ok=processed,
                    skipped=skipped,
                    failed=failed,
                    elapsed_s=elapsed,
                    tokens=tokens,
                    deferred_remaining=len(deferred_ai),
                    deferred_recovered=deferred_recovered,
                    deferred_failed=deferred_failed,
                )
            )

        return {"processed": processed, "skipped": skipped, "failed": failed}

    # Backward-compatible alias for older tooling/scripts.
    run_range_concurrent = run_range
    
    def _test_connections(self) -> bool:
        """Test all service connections"""
        logger.info("Testing service connections...")
        
        # Test database
        try:
            stats = self.db_manager.get_news_count_by_status()
            logger.info(f"✓ Database connection OK (Total records: {stats['total']})")
        except Exception as e:
            logger.error(f"✗ Database connection failed: {e}")
            return False
        
        # Test scraper
        if not self.scraper_client.test_connection():
            logger.warning("✗ Scraper service health-check failed (continuing anyway)")
        else:
            logger.info("✓ Scraper service OK")
        
        # Test Gemini API
        if not self.analyzer.test_connection():
            logger.error("✗ Gemini API connection failed")
            return False
        logger.info("✓ Gemini API OK")
        
        return True
    
    def _process_news_item(self, news_item: Dict, *, force_reprocess: bool = False) -> bool:
        """
        Process a single news item through the complete pipeline.
        
        Args:
            news_item: Dict with url, content_id, headline, etc.
        
        Returns:
            bool: True if successfully processed, False if skipped/invalid
        """
        return self._process_news_item_with_components(
            news_item=news_item,
            db_manager=self.db_manager,
            scraper_client=self.scraper_client,
            analyzer=self.analyzer,
            update_stats=True,
            force_reprocess=force_reprocess,
        )

    def _process_news_item_with_components(
        self,
        news_item: Dict,
        db_manager: DatabaseManager,
        scraper_client: ScraperClient,
        analyzer: GeminiAnalyzer,
        update_stats: bool,
        force_reprocess: bool = False,
    ) -> bool:
        content_id = news_item['content_id']
        url = news_item['url']
        headline = news_item['headline']
        
        # Step 1: Check if already analyzed
        if db_manager.check_if_analyzed(content_id):
            if not force_reprocess:
                logger.info(f"Already analyzed, skipping: {content_id}")
                if update_stats:
                    self.stats['skipped'] += 1
                return False
            logger.info(f"Already analyzed, but --force enabled; reprocessing: {content_id}")
        
        # Step 2: Scrape the article
        logger.info(f"Scraping article: {url}")
        scraped_data = scraper_client.scrape_article(url)
        if not scraped_data or not scraped_data.get('content'):
            raise TransientScrapeError(f"Scrape failed or empty content for {url}")

        # If scraper validation flagged an invalid page (including Cloudflare human verification), skip.
        if not scraped_data.get('is_valid', True):
            reason = scraped_data.get('validation_reason', 'Invalid content')
            logger.warning(f"SKIPPING - Invalid FF content: {reason}")
            if update_stats:
                self.stats['skipped'] += 1
            return False
        
        article_content = scraped_data['content']
        published_date = scraped_data['published_date']
        ff_category = scraped_data.get('forexfactory_category')
        
        # Update headline from metadata if available
        metadata = scraped_data.get('metadata', {})
        if metadata.get('headline'):
            headline = metadata['headline']
            logger.info(f"Updated headline from metadata: {headline}")

        page_title = (metadata.get('title') or '').strip()
        if page_title:
            logger.info(f"Page title: {page_title}")

        snippet = (article_content or '').strip().replace('\n', ' ')
        if snippet:
            logger.info(f"Content snippet: {snippet[:200]}")
        
        logger.info(f"Scraped {len(article_content)} characters, "
                   f"published: {published_date or 'unknown'}, "
                   f"category: {ff_category or 'unknown'}")
        
        # Step 2.5: Validate ForexFactory content
        if not scraped_data.get('is_valid', True):
            logger.warning(f"SKIPPING - Invalid FF content: {scraped_data.get('validation_reason', 'unknown')}")
            if update_stats:
                self.stats['skipped'] += 1
            return False
        
        # Step 2.6: Check published date - skip if missing
        if not published_date:
            logger.warning(f"SKIPPING - No published date found for: {url}")
            if update_stats:
                self.stats['skipped'] += 1
            return False
        
        # Step 2.7: Check category filter - SKIP unwanted categories
        if ff_category:
            if not self._is_allowed_category(ff_category):
                logger.warning(f"SKIPPING - Category '{ff_category}' not in allowed list")
                if update_stats:
                    self.stats['skipped'] += 1
                return False
            logger.info(f"✓ Category '{ff_category}' is allowed, proceeding...")
        else:
            logger.warning("No category found, proceeding anyway (might be older article)")
        
        # Step 3: Analyze with Gemini AI using RAG
        try:
            self._analyze_and_store(
                content_id=content_id,
                url=url,
                headline=headline,
                article_content=article_content,
                published_date=published_date,
                ff_category=ff_category,
                db_manager=db_manager,
                analyzer=analyzer,
            )
        except TransientAIError as e:
            if getattr(e, "payload", None) is None:
                e.payload = {
                    "content_id": content_id,
                    "url": url,
                    "headline": headline,
                    "article_content": article_content,
                    "published_date": published_date,
                    "ff_category": ff_category,
                }
            raise
        
        logger.info(f"✓ Successfully processed {content_id}")
        return True

    def _retry_ai_only(self, *, payload: Dict) -> None:
        """Deferred end-of-range retry that reuses previously scraped content."""
        self._analyze_and_store(
            content_id=str(payload["content_id"]),
            url=str(payload["url"]),
            headline=str(payload.get("headline") or f"Article {payload['content_id']}"),
            article_content=str(payload.get("article_content") or ""),
            published_date=payload["published_date"],
            ff_category=payload.get("ff_category"),
            db_manager=self.db_manager,
            analyzer=self.analyzer,
        )

    def _analyze_and_store(
        self,
        *,
        content_id: str,
        url: str,
        headline: str,
        article_content: str,
        published_date: datetime,
        ff_category: Optional[str],
        db_manager: DatabaseManager,
        analyzer: GeminiAnalyzer,
    ) -> None:
        """Run Gemini analysis + embedding + persistence (no scraping)."""
        logger.info(f"Step 3: Analyzing with Gemini AI (RAG mode) for content_id {content_id}...")
        logger.info(f"  Model: {Config.GEMINI_MODEL}")
        logger.info(f"  Content length: {len(article_content)} chars")

        us_political_related = self._detect_us_political_keywords(headline, article_content)
        logger.info(f"  US political related: {us_political_related}")

        analysis_result = None
        last_error: Optional[BaseException] = None

        # Immediate retry for Gemini hiccups (e.g., malformed JSON output) WITHOUT re-scraping.
        for attempt in range(1, 3):
            try:
                logger.info(f"  Calling Gemini API... (attempt {attempt}/2)")
                analysis_result = analyzer.analyze_with_rag(
                    headline=headline,
                    content=article_content,
                    url=url,
                    us_political_related=us_political_related,
                    max_similar=Config.RAG_MAX_SIMILAR,
                    published_date=published_date,
                    forexfactory_category=ff_category,
                )
                if analysis_result:
                    break
            except RateLimitError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(f"  Gemini analysis attempt {attempt} failed: {type(e).__name__}: {e}")
                time.sleep(1.5)

        if not analysis_result:
            logger.error(f"AI analysis failed for {content_id}")
            raise TransientAIError(str(last_error) if last_error else "AI analysis failed")

        logger.info(f"  ✓ Gemini analysis completed successfully")
        if Config.PRINT_ANALYSIS:
            try:
                rendered = json.dumps(analysis_result, ensure_ascii=False, indent=2, default=str)
                # Avoid dumping extreme payloads (shouldn't happen, but be safe).
                if len(rendered) > 20000:
                    rendered = rendered[:20000] + "\n... (truncated)"
                logger.info(f"[ANALYSIS_JSON] content_id={content_id}\n{rendered}")
            except Exception as e:
                logger.warning(f"Failed to render analysis JSON: {type(e).__name__}: {e}")
        logger.info(
            f"Analysis: forex_relevant={analysis_result['forex_relevant']}, "
            f"importance={analysis_result['importance_score']}, "
            f"sentiment={analysis_result['sentiment_score']:.2f}"
        )

        logger.info(f"Step 4: Generating embedding for content_id {content_id}...")
        embedding_text = analysis_result.get('content_for_embedding', '') or headline
        logger.info(f"  Embedding text length: {len(embedding_text)} chars")

        embedding_vector = None
        last_embed_error: Optional[BaseException] = None
        for attempt in range(1, 3):
            try:
                embedding_vector = analyzer.generate_embedding(embedding_text)
                if embedding_vector:
                    break
            except RateLimitError:
                raise
            except Exception as e:
                last_embed_error = e
                logger.warning(f"  Embedding attempt {attempt} failed: {type(e).__name__}: {e}")
                time.sleep(1.0)

        if not embedding_vector:
            logger.error(f"Failed to generate embedding for {content_id}")
            raise TransientAIError(str(last_embed_error) if last_embed_error else "Embedding generation failed")

        logger.info(f"  ✓ Embedding generated (dimension: {len(embedding_vector)})")

        logger.info(f"Step 5: Storing analysis in database for content_id {content_id}...")
        email_id = db_manager.upsert_analysis(
            content_id=content_id,
            analysis_data=analysis_result,
            scraped_content=article_content,
            published_date=published_date,
            headline=headline,
            forexfactory_url=url,
            forexfactory_category=ff_category,
        )

        if not email_id:
            logger.error("Failed to store analysis")
            raise ValueError("Database upsert failed")

        logger.info(f"Step 6: Storing embedding in vector database for content_id {content_id}...")
        metadata = {
            'headline': headline,
            'published_date': published_date.isoformat() if published_date else None,
            'forexfactory_category': ff_category or None,
            'primary_instrument': analysis_result.get('primary_instrument'),
            'importance_score': analysis_result.get('importance_score'),
            'news_category': analysis_result.get('news_category'),
            'sentiment_score': float(analysis_result.get('sentiment_score', 0.0)),
            'analysis_confidence': float(analysis_result.get('analysis_confidence', 0.0)),
        }

        vector_id = self.db_manager.insert_vector_embedding(
            content_id=content_id,
            content=embedding_text,
            embedding=embedding_vector,
            metadata=metadata,
        )

        if not vector_id:
            logger.warning("Failed to store embedding, but analysis was saved")
        else:
            db_manager.update_vector_store_id(content_id, vector_id)

    

    def _detect_us_political_keywords(self, headline: str, content: str) -> bool:
        """Simple keyword-based detection for US political news"""
        text = (headline + ' ' + content[:500]).lower()
        
        us_keywords = [
            'trump', 'biden', 'white house', 'congress', 'senate',
            'us president', 'administration', 'washington',
            'republican', 'democrat', 'us government'
        ]
        
        return any(keyword in text for keyword in us_keywords)
    
    def _is_allowed_category(self, category: str) -> bool:
        """
        Check if ForexFactory category is in allowed list.
        
        Args:
            category: ForexFactory category string
        
        Returns:
            True if category should be analyzed, False to skip
        """
        if not category:
            return True  # Allow if no category detected
        
        category_lower = category.lower().strip()

        # Normalize: collapse punctuation/slashes/parentheses to spaces.
        normalized = re.sub(r"[^a-z0-9]+", " ", category_lower).strip()

        # Explicit allow for analysis types.
        if "fundamental analysis" in normalized or "technical analysis" in normalized:
            return True

        # Robust Breaking News matching across common variants:
        # - "Breaking News / High Impact"
        # - "Breaking News (High Impact)"
        # - "High Impact Breaking News"
        if "breaking" in normalized and "news" in normalized:
            if "high" in normalized and "impact" in normalized:
                return True
            if "medium" in normalized and "impact" in normalized:
                return True
            if "low" in normalized and "impact" in normalized:
                return True

        # Fallback: old substring matching against configured variants.
        for allowed in self.ALLOWED_CATEGORIES:
            allowed_norm = re.sub(r"[^a-z0-9]+", " ", allowed.lower()).strip()
            if allowed_norm and (allowed_norm in normalized or normalized in allowed_norm):
                return True

        return False
    
    def _print_progress(self):
        """Print progress statistics"""
        elapsed = (datetime.now() - self.stats['started_at']).total_seconds()
        rate = self.stats['total_processed'] / elapsed if elapsed > 0 else 0
        
        logger.info("-" * 60)
        logger.info(f"Progress: {self.stats['total_processed']} processed | "
                   f"{self.stats['successful']} successful | "
                   f"{self.stats['failed']} failed | "
                   f"{self.stats['skipped']} skipped")
        logger.info(f"Rate: {rate:.2f} items/sec | Elapsed: {elapsed:.0f}s")
        logger.info("-" * 60)

    def _format_run_line(
        self,
        tag: str,
        *,
        done: int,
        ok: int,
        skipped: int,
        failed: int,
        elapsed_s: float,
        tokens: dict,
        deferred_remaining: int | None = None,
        deferred_recovered: int | None = None,
        deferred_failed: int | None = None,
    ) -> str:
        elapsed_s_i = int(round(elapsed_s))
        elapsed_min = elapsed_s / 60.0 if elapsed_s > 0 else 0.0
        avg_item_s = (elapsed_s / done) if done > 0 else 0.0
        avg_ok_s = (elapsed_s / ok) if ok > 0 else 0.0
        tokens_total = int(tokens.get('total', 0) or 0)
        avg_tokens_ok = (tokens_total / ok) if ok > 0 else 0.0

        base = (
            f"[{tag}] "
            f"done={done:4d} ok={ok:4d} skipped={skipped:4d} failed={failed:4d} | "
            f"elapsed={elapsed_s_i:4d}s ({elapsed_min:4.1f}m) | "
            f"avg/item={avg_item_s:4.1f}s avg/ok={avg_ok_s:4.1f}s | "
            f"tokens_in={int(tokens.get('prompt', 0) or 0):5d} "
            f"tokens_out={int(tokens.get('output', 0) or 0):5d} "
            f"tokens_total={tokens_total:5d} avg_tokens/ok={avg_tokens_ok:5.1f}"
        )

        if deferred_remaining is None:
            return base

        return (
            base
            + (
                f" | deferred_remaining={deferred_remaining} "
                f"deferred_recovered={deferred_recovered or 0} "
                f"deferred_failed={deferred_failed or 0}"
            )
        )
    
    def _print_final_summary(self):
        """Print final summary statistics"""
        elapsed = (datetime.now() - self.stats['started_at']).total_seconds()
        tokens = self.analyzer.get_token_totals()
        logger.info(
            self._format_run_line(
                "FINAL",
                done=int(self.stats.get('total_processed', 0) or 0),
                ok=int(self.stats.get('successful', 0) or 0),
                skipped=int(self.stats.get('skipped', 0) or 0),
                failed=int(self.stats.get('failed', 0) or 0),
                elapsed_s=float(elapsed),
                tokens=tokens,
            )
        )


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='News Analysis Backfill Tool')
    parser.add_argument('--limit', type=int, default=None,
                       help='Maximum number of articles to process')
    parser.add_argument('--range', nargs=2, type=int, metavar=('START_ID', 'END_ID'),
                       help='Process an inclusive ForexFactory content_id range')
    parser.add_argument('--continue-on-error', action='store_true',
                       help='Continue range processing even if an item errors')
    parser.add_argument('--sleep-seconds', type=float, default=10.0,
                       help='Sleep between items in range mode')
    parser.add_argument('--force', action='store_true',
                       help='Reprocess items even if already analyzed (range mode)')
    parser.add_argument('--no-resume', action='store_true',
                       help='Disable resume functionality (start from beginning)')
    
    args = parser.parse_args()
    
    # Override config if needed
    if args.no_resume:
        Config.ENABLE_RESUME = False
    
    # Create and run orchestrator
    orchestrator = NewsAnalysisOrchestrator()

    # Best-effort signal handling so SIGTERM/SIGHUP behave like Ctrl+C.
    # Note: Ctrl+C will only reach the process when `docker exec` allocates a TTY (use `-it`).
    def _handle_stop_signal(signum, frame):
        raise KeyboardInterrupt

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _handle_stop_signal)
        except Exception:
            pass
    try:
        signal.signal(signal.SIGHUP, _handle_stop_signal)
    except Exception:
        pass
    
    try:
        if args.range:
            start_id, end_id = args.range
            orchestrator.run_range(
                start_id=start_id,
                end_id=end_id,
                continue_on_error=args.continue_on_error,
                sleep_seconds=args.sleep_seconds,
                force_reprocess=args.force,
            )
        else:
            orchestrator.run(limit=args.limit)
    except KeyboardInterrupt:
        tokens = orchestrator.analyzer.get_token_totals()
        done = int(orchestrator.stats.get('total_processed', 0) or 0)
        ok = int(orchestrator.stats.get('successful', 0) or 0)
        skipped = int(orchestrator.stats.get('skipped', 0) or 0)
        failed = int(orchestrator.stats.get('failed', 0) or 0)
        elapsed = (datetime.now() - orchestrator.stats['started_at']).total_seconds()
        logger.info(
            orchestrator._format_run_line(
                "STOP",
                done=done,
                ok=ok,
                skipped=skipped,
                failed=failed,
                elapsed_s=float(elapsed),
                tokens=tokens,
            )
        )
        logger.info(orchestrator._format_run_line(
            "FINAL",
            done=done,
            ok=ok,
            skipped=skipped,
            failed=failed,
            elapsed_s=float(elapsed),
            tokens=tokens,
        ))
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
