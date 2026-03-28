# News Analyzer - Backfill Tool

Automated system for scraping, analyzing, and storing forex news articles using Gemini AI.

## Features

- ✅ **Automatic Scraping**: Fetches full article content from ForexFactory
- ✅ **AI Analysis**: Uses Gemini AI with structured output for comprehensive analysis
- ✅ **Rate Limiting**: Built-in exponential backoff to handle API rate limits
- ✅ **Resume Capability**: Automatically resumes from last processed article
- ✅ **Vector Embeddings**: Generates and stores embeddings for similarity search
- ✅ **Database Integration**: Stores analysis in PostgreSQL with pgvector
- ✅ **Model Comparison**: Test script to compare different Gemini models

## Quick Start

### 1. Database Migration (One-time)

First, run the database migration to rename `trump_related` → `us_political_related`:

```bash
docker exec -i n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data < migration_rename_trump_to_us_political.sql
```

Or manually:
```bash
docker exec -it n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data
# Then run the SQL from the migration file
```

### 2. Build the Container

```bash
cd /path/to/ai_trading_bot
docker-compose build news-analyzer
```

### 3. Run Model Comparison Test (Optional but Recommended)

Test which Gemini model performs best:

```bash
docker-compose run --rm news-analyzer python test_models.py --articles 5
```

This will:
- Test available Gemini 2.0 models (Flash, Flash Thinking Exp, etc.)
- Analyze 5 real articles from your database
- Compare response times, confidence scores, and accuracy
- Generate a detailed JSON report
- Provide a recommendation

### 4. Run the Backfill

Process all historical news articles:

```bash
# Process all articles
docker-compose run --rm news-analyzer python main.py

# Process only 100 articles
docker-compose run --rm news-analyzer python main.py --limit 100

# Start from beginning (ignore resume)
docker-compose run --rm news-analyzer python main.py --no-resume
```

## Configuration

Environment variables (set in `.env` or `docker-compose.yml`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | From .env |
| `GEMINI_API_KEY` | Google Gemini API key | From .env |
| `GEMINI_MODEL` | Model to use | `gemini-2.0-flash-exp` |
| `SCRAPER_BASE_URL` | Scraper service URL | `http://tradingbot-scraper:8000` |
| `MAX_REQUESTS_PER_MINUTE` | API rate limit | 15 |
| `ENABLE_RESUME` | Auto-resume from last article | `true` |

## How It Works

### Processing Pipeline

```
1. Fetch unprocessed news from DB
   ↓
2. Resume from last analyzed article (if enabled)
   ↓
3. For each article:
   ├─ Scrape full content from ForexFactory
   ├─ Extract published date from HTML
   ├─ Analyze with Gemini AI (with rate limiting)
   ├─ Generate embedding vector
   ├─ Store analysis in email_news_analysis table
   └─ Store embedding in email_news_vectors table
   ↓
4. Update progress and statistics
```

### Resume Capability

The system automatically tracks progress:
- On startup, checks `email_news_analysis` for the last analyzed article
- Resumes processing from that point
- If interrupted (Ctrl+C), progress is saved
- Next run continues from where it left off

### Rate Limiting

Built-in rate limiter protects against API quota exhaustion:
- In-memory rate tracking (15 requests/minute by default)
- Exponential backoff on errors (2s → 4s → 8s → 16s → 32s → 60s max)
- Automatic retry on rate limit errors (up to 5 attempts)
- Configurable delays between batches

## Analysis Output

Each news article gets comprehensive analysis:

| Field | Description |
|-------|-------------|
| `forex_relevant` | Whether news affects forex markets |
| `forex_instruments` | Affected pairs (EURUSD, XAUUSD, BTCUSD, etc.) |
| `primary_instrument` | Most affected instrument |
| `importance_score` | 1-5 scale |
| `sentiment_score` | -1.0 (bearish) to 1.0 (bullish) |
| `analysis_confidence` | 0.0 to 1.0 |
| `news_category` | economic_data, central_bank, geopolitical, etc. |
| `entities_mentioned` | Key entities (Fed, ECB, Biden, etc.) |
| `trading_sessions` | Affected sessions (London, New York, etc.) |
| `market_impact_prediction` | bullish, bearish, neutral, mixed |
| `impact_timeframe` | immediate, intraday, daily, weekly, long-term |
| `volatility_expectation` | low, medium, high, extreme |
| `us_political_related` | Whether US political news |
| `ai_analysis_summary` | Detailed analysis text |

## Monitoring

### Logs

- **Container logs**: `docker logs tradingbot-news-analyzer`
- **Application log**: `news_analyzer/logs/news_analyzer.log`
- **Model comparison log**: `model_comparison.log`

### Progress Tracking

The script prints progress every 10 articles:
```
Progress: 50 processed | 48 successful | 1 failed | 1 skipped
Rate: 0.25 items/sec | Elapsed: 200s
```

### Statistics

Check database stats:
```sql
SELECT 
    COUNT(*) as total,
    COUNT(CASE WHEN ai_analysis_summary IS NOT NULL THEN 1 END) as analyzed,
    COUNT(CASE WHEN ai_analysis_summary IS NULL THEN 1 END) as unanalyzed
FROM email_news_analysis
WHERE forexfactory_content_id IS NOT NULL;
```

## Troubleshooting

### "No models available for testing"
- Check your Gemini API key is valid
- Some experimental models may not be available in all regions
- The script will automatically fallback to stable models

### "Scraper service connection failed"
- Ensure the scraper container is running: `docker ps | grep scraper`
- Check scraper health: `curl http://localhost:8000/health`
- Restart scraper if needed: `docker-compose restart scraper`

### "Rate limit exceeded"
- The script will automatically retry with exponential backoff
- If persistent, reduce `MAX_REQUESTS_PER_MINUTE` in config
- Consider using a paid Gemini API tier for higher limits

### "Failed to extract published date"
- Not critical - the script will continue
- Uses existing `email_received_at` as fallback
- Some ForexFactory articles may not have clear date metadata

### "Database connection failed"
- Check postgres container: `docker ps | grep postgres`
- Verify credentials in `.env` file
- Test connection: `docker exec -it n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data`

## Performance

Expected processing times (on typical hardware):
- **Scraping**: 2-5 seconds per article
- **AI Analysis**: 3-8 seconds per article (depends on model)
- **Embedding**: 1-2 seconds per article
- **Total**: ~8-15 seconds per article

For 1000 articles: approximately 2.5-4 hours

## Files

```
news_analyzer/
├── __init__.py                              # Package init
├── config.py                                # Configuration
├── db_manager.py                            # Database operations
├── scraper_client.py                        # Scraper integration
├── analyzer.py                              # Gemini AI analyzer
├── main.py                                  # Main orchestration
├── test_models.py                           # Model comparison tool
├── requirements.txt                         # Python dependencies
├── Dockerfile                               # Container definition
├── migration_rename_trump_to_us_political.sql  # DB migration
└── README.md                                # This file
```

## Notes

- **One-time backfill**: This tool is designed for historical data processing
- **Resume by default**: Always resumes from last point unless `--no-resume`
- **Safe interruption**: Press Ctrl+C anytime, progress is saved
- **Crypto support**: Analysis includes major crypto pairs (BTC, ETH) alongside forex
- **US political detection**: Automatically flags US political news (not just Trump)

## Support

For issues or questions:
1. Check logs: `docker logs tradingbot-news-analyzer`
2. Review database status with SQL queries above
3. Test individual components (scraper health, Gemini API connection)
