"""
Model Comparison Test Script
Compares Gemini 3.0 Flash Preview vs Gemini 2.5 Flash on actual news articles.
"""
import logging
import sys
import json
import time
from typing import List, Dict, Tuple
from datetime import datetime

from google import genai

from config import Config
from db_manager import DatabaseManager
from scraper_client import ScraperClient
from analyzer import GeminiAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('model_comparison.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


class ModelComparator:
    """Compare different Gemini models on news analysis tasks"""
    
    # Models to test - LATEST VERSIONS
    MODELS_TO_TEST = [
        "gemini-3-flash-preview",      # Newest flash
        "gemini-2.5-flash",             # Previous stable flash
    ]
    
    def __init__(self):
        Config.validate()
        self.db_manager = DatabaseManager()
        self.scraper_client = ScraperClient()
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        
        # Check which models are actually available
        self.available_models = self._check_available_models()
    
    def _check_available_models(self) -> List[str]:
        """Check which models are available in the API"""
        try:
            all_models = list(self.client.models.list())
            available_names = [m.name for m in all_models]
            
            logger.info(f"Available Gemini models: {len(available_names)}")
            
            # Filter to only the ones we want to test
            test_models = []
            for model_name in self.MODELS_TO_TEST:
                # Check both with and without 'models/' prefix
                if f"models/{model_name}" in available_names or model_name in available_names:
                    test_models.append(model_name)
                    logger.info(f"  ✓ {model_name}")
                else:
                    logger.warning(f"  ✗ {model_name} not available")
            
            # Fallback to other new models if preferred ones aren't available
            if not test_models:
                logger.warning("Preferred models not available, checking alternatives...")
                fallback_models = ["gemini-3-pro-preview", "gemini-2.5-pro", "gemini-exp-1206"]
                for model_name in fallback_models:
                    if f"models/{model_name}" in available_names:
                        test_models.append(model_name)
                        logger.info(f"  ✓ {model_name} (fallback)")
                        if len(test_models) >= 2:
                            break
            
            return test_models
            
        except Exception as e:
            logger.error(f"Error checking available models: {e}")
            return ["gemini-2.5-flash"]  # Ultimate fallback
    
    def get_sample_articles(self, count: int = 5) -> List[Dict]:
        """
        Get sample articles from the database that have content_ids.
        
        Args:
            count: Number of articles to retrieve
        
        Returns:
            List of article dicts with url, content_id, headline
        """
        try:
            with self.db_manager.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT 
                            forexfactory_content_id,
                            headline,
                            email_received_at
                        FROM email_news_analysis
                        WHERE forexfactory_content_id IS NOT NULL
                            AND headline IS NOT NULL
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (count,))
                    
                    rows = cur.fetchall()
                    
                    articles = []
                    for row in rows:
                        articles.append({
                            'content_id': row[0],
                            'url': f"https://www.forexfactory.com/news/{row[0]}",
                            'headline': row[1],
                            'email_received_at': row[2]
                        })
                    
                    logger.info(f"Retrieved {len(articles)} sample articles from database")
                    return articles
                    
        except Exception as e:
            logger.error(f"Error getting sample articles: {e}")
            return []
    
    def test_model(self, model_name: str, article: Dict, scraped_content: str) -> Tuple[Dict, float]:
        """
        Test a single model on an article.
        
        Args:
            model_name: Name of the Gemini model
            article: Article metadata dict
            scraped_content: Full article content
        
        Returns:
            Tuple of (analysis_result, response_time)
        """
        logger.info(f"Testing model: {model_name}")
        
        try:
            # Create analyzer with this specific model
            analyzer = GeminiAnalyzer(model_name=model_name)
            
            # Time the analysis
            start_time = time.time()
            
            analysis = analyzer.analyze_news(
                headline=article['headline'],
                content=scraped_content,
                url=article['url'],
                us_political_related=False
            )
            
            response_time = time.time() - start_time
            
            logger.info(f"  Response time: {response_time:.2f}s")
            
            return analysis, response_time
            
        except Exception as e:
            logger.error(f"  Error testing {model_name}: {e}")
            return None, 0.0
    
    def compare_models(self, num_articles: int = 5):
        """
        Main comparison method.
        
        Args:
            num_articles: Number of articles to test on
        """
        logger.info("=" * 80)
        logger.info("Starting Model Comparison Test")
        logger.info("=" * 80)
        
        if not self.available_models:
            logger.error("No models available for testing!")
            return
        
        logger.info(f"Models to test: {', '.join(self.available_models)}")
        
        # Get sample articles
        articles = self.get_sample_articles(num_articles)
        if not articles:
            logger.error("No articles found for testing!")
            return
        
        logger.info(f"Testing on {len(articles)} articles\n")
        
        # Results storage
        results = {model: [] for model in self.available_models}
        
        # Test each article with each model
        for idx, article in enumerate(articles, 1):
            logger.info(f"\n{'='*80}")
            logger.info(f"Article {idx}/{len(articles)}: {article['headline'][:80]}")
            logger.info(f"URL: {article['url']}")
            logger.info(f"{'='*80}\n")
            
            # Scrape article once
            logger.info("Scraping article...")
            scraped_data = self.scraper_client.scrape_article(article['url'])
            
            if not scraped_data or not scraped_data['content']:
                logger.error("Failed to scrape article, skipping")
                continue
            
            content = scraped_data['content']
            logger.info(f"Scraped {len(content)} characters\n")
            
            # Test each model
            for model_name in self.available_models:
                analysis, response_time = self.test_model(model_name, article, content)
                
                if analysis:
                    results[model_name].append({
                        'article_id': article['content_id'],
                        'headline': article['headline'],
                        'analysis': analysis,
                        'response_time': response_time
                    })
                
                # Small delay between models to avoid rate limits
                time.sleep(3)
            
            # Delay between articles
            if idx < len(articles):
                logger.info("\nWaiting before next article...")
                time.sleep(5)
        
        # Generate comparison report
        self._generate_report(results)
    
    def _generate_report(self, results: Dict[str, List[Dict]]):
        """
        Generate and save comparison report.
        
        Args:
            results: Dict mapping model names to their results
        """
        logger.info("\n" + "=" * 80)
        logger.info("COMPARISON REPORT")
        logger.info("=" * 80)
        
        report = {
            'generated_at': datetime.now().isoformat(),
            'models_tested': list(results.keys()),
            'summary': {},
            'detailed_results': results
        }
        
        # Calculate summary statistics for each model
        for model_name, model_results in results.items():
            if not model_results:
                continue
            
            response_times = [r['response_time'] for r in model_results]
            confidences = [r['analysis']['analysis_confidence'] for r in model_results 
                          if r['analysis']]
            importance_scores = [r['analysis']['importance_score'] for r in model_results 
                                if r['analysis']]
            forex_relevant_count = sum(1 for r in model_results 
                                      if r['analysis'] and r['analysis']['forex_relevant'])
            
            summary = {
                'total_articles': len(model_results),
                'avg_response_time': sum(response_times) / len(response_times) if response_times else 0,
                'min_response_time': min(response_times) if response_times else 0,
                'max_response_time': max(response_times) if response_times else 0,
                'avg_confidence': sum(confidences) / len(confidences) if confidences else 0,
                'avg_importance_score': sum(importance_scores) / len(importance_scores) if importance_scores else 0,
                'forex_relevant_count': forex_relevant_count,
                'forex_relevant_pct': (forex_relevant_count / len(model_results) * 100) if model_results else 0
            }
            
            report['summary'][model_name] = summary
            
            # Print summary
            logger.info(f"\n{model_name}:")
            logger.info(f"  Articles analyzed:     {summary['total_articles']}")
            logger.info(f"  Avg response time:     {summary['avg_response_time']:.2f}s")
            logger.info(f"  Response time range:   {summary['min_response_time']:.2f}s - {summary['max_response_time']:.2f}s")
            logger.info(f"  Avg confidence:        {summary['avg_confidence']:.3f}")
            logger.info(f"  Avg importance score:  {summary['avg_importance_score']:.1f}")
            logger.info(f"  Forex relevant:        {summary['forex_relevant_count']}/{summary['total_articles']} ({summary['forex_relevant_pct']:.1f}%)")
        
        # Save detailed report to JSON
        report_filename = f"model_comparison_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_filename, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        logger.info(f"\n✓ Detailed report saved to: {report_filename}")
        
        # Recommendation
        logger.info("\n" + "=" * 80)
        logger.info("RECOMMENDATION")
        logger.info("=" * 80)
        
        if len(results) > 1:
            best_model = self._recommend_model(report['summary'])
            logger.info(f"Recommended model: {best_model}")
            logger.info(f"Reason: Best balance of speed, confidence, and accuracy")
        
        logger.info("=" * 80)
    
    def _recommend_model(self, summaries: Dict) -> str:
        """
        Recommend the best model based on performance metrics.
        
        Args:
            summaries: Dict of model summaries
        
        Returns:
            str: Name of recommended model
        """
        # Scoring: balance speed, confidence, and consistency
        scores = {}
        
        for model_name, summary in summaries.items():
            if summary['total_articles'] == 0:
                continue
            
            # Normalize metrics (lower response time is better)
            speed_score = 1 / (summary['avg_response_time'] + 0.1)  # Avoid division by zero
            confidence_score = summary['avg_confidence']
            
            # Combined score (weighted average)
            total_score = (speed_score * 0.4) + (confidence_score * 0.6)
            scores[model_name] = total_score
        
        if scores:
            best_model = max(scores, key=scores.get)
            return best_model
        
        return list(summaries.keys())[0] if summaries else "unknown"


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Compare Gemini Models for News Analysis')
    parser.add_argument('--articles', type=int, default=5,
                       help='Number of articles to test (default: 5)')
    
    args = parser.parse_args()
    
    comparator = ModelComparator()
    
    try:
        comparator.compare_models(num_articles=args.articles)
    except KeyboardInterrupt:
        logger.info("\n\nInterrupted by user.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
