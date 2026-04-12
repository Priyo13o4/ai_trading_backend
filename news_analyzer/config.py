"""
Configuration for News Analyzer
"""
import os
from typing import Optional

class Config:
    """Configuration settings for the news analyzer"""
    
    # Database Configuration
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://Priyo13o4:priyodip13o4@n8n-postgres:5432/ai_trading_bot_data"
    )
    
    # Gemini API Configuration
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
    GEMINI_EMBEDDING_MODEL: str = "models/gemini-embedding-001"  # Gemini embedding model
    
    # Rate Limiting Configuration (for Gemini API)
    MAX_REQUESTS_PER_MINUTE: int = 15  # Conservative limit
    RATE_LIMIT_WINDOW: int = 60  # seconds
    
    # Retry Configuration
    MAX_RETRIES: int = 5
    INITIAL_RETRY_DELAY: int = 2  # seconds
    MAX_RETRY_DELAY: int = 60  # seconds
    EXPONENTIAL_BASE: float = 2.0
    
    # Scraper Configuration
    SCRAPER_BASE_URL: str = os.getenv(
        "SCRAPER_BASE_URL",
        "http://tradingbot-scrapling:8010"
    )
    # Needs to be long enough for headful/manual Cloudflare solving.
    SCRAPER_TIMEOUT: int = int(os.getenv("SCRAPER_TIMEOUT", "240"))  # seconds
    
    # Processing Configuration
    BATCH_SIZE: int = 10  # Process N articles before committing
    SLEEP_BETWEEN_BATCHES: int = 10  # seconds

    # RAG Configuration
    RAG_MAX_SIMILAR: int = 5
    
    # Allowed ForexFactory Categories
    # NOTE: ForexFactory category naming varies across pages/scrapers.
    # Keep this broad enough to include common variants.
    ALLOWED_CATEGORIES: set = {
        "Breaking News (High Impact)",
        "Breaking News (Medium Impact)",
        "Breaking News (Low Impact)",
        "Breaking News / High Impact",
        "Breaking News / Medium Impact",
        "Breaking News / Low Impact",
        "High Impact Breaking News",
        "Medium Impact Breaking News",
        "Low Impact Breaking News",
        "Fundamental Analysis",
        "Technical Analysis",
    }
    
    # Vector Store Configuration
    VECTOR_TABLE_NAME: str = "email_news_vectors"
    VECTOR_DIMENSION: int = 3072  # gemini-embedding-001 dimension
    
    # Resume Configuration
    ENABLE_RESUME: bool = True

    # Logging / Debug
    # When enabled, prints the full Gemini analysis JSON to logs.
    PRINT_ANALYSIS: bool = os.getenv("PRINT_ANALYSIS", "0").strip().lower() in {"1", "true", "yes", "y"}
    
    @classmethod
    def validate(cls) -> bool:
        """Validate critical configuration"""
        if not cls.DATABASE_URL:
            raise ValueError("DATABASE_URL is required")
        return True
