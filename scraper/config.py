"""
Configuration settings for the web scraper API
"""
from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # API Settings
    API_TITLE: str = "Web Scraper & Crawler API"
    API_VERSION: str = "1.0.0"
    API_DESCRIPTION: str = "Professional web scraping and crawling service with anti-bot detection"
    
    # Server Settings
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    RELOAD: bool = False
    
    # Scraper Settings
    DEFAULT_TIMEOUT: int = 30
    MAX_RETRIES: int = 3
    RETRY_DELAY: int = 2
    
    # Selenium Settings
    HEADLESS: bool = True
    SELENIUM_TIMEOUT: int = 30
    PAGE_LOAD_TIMEOUT: int = 30
    IMPLICIT_WAIT: int = 10
    SELENIUM_BROWSER_BINARY: str = ""
    SELENIUM_DRIVER_PATH: str = ""

    # Manual / safest scraping mode
    # - When running on your laptop (outside Docker), set HEADLESS=False so you can
    #   solve Cloudflare/login challenges.
    # - Set CHROME_USER_DATA_DIR to persist cookies/sessions across requests.
    USE_UNDETECTED_CHROME: bool = False
    # When enabled, launches undetected_chromedriver with minimal options
    # (closer to a real user session; fewer flags = fewer startup crashes on macOS).
    MINIMAL_UC_MODE: bool = False
    CHROME_USER_DATA_DIR: str = ""
    CHROME_PROFILE_DIRECTORY: str = ""
    MANUAL_CHALLENGE_MAX_SECONDS: int = 300

    # Cloudflare challenge handling
    # - If a Cloudflare/human verification interstitial is detected, the scraper will
    #   wait (headful) to let you solve it, and then re-check the page.
    # - If you don't solve it within the allowed attempts, the scraper will close
    #   the Chrome instance and return a sentinel error so the analyzer can STOP.
    CLOUDFLARE_CHALLENGE_MAX_ATTEMPTS: int = 2
    CLOUDFLARE_CHALLENGE_WAIT_SECONDS: int = 100

    # Debug: when a challenge is detected, log extra evidence (title/current_url).
    # This does not change detection behavior; it only helps verify false positives.
    CLOUDFLARE_DEBUG_EVIDENCE: bool = False
    
    # Crawler Settings
    MAX_CRAWL_DEPTH: int = 3
    MAX_LINKS_PER_PAGE: int = 100
    
    # Output Settings
    MAX_TEXT_SECTION_LENGTH: int = 2000  # characters per text slice
    SUMMARY_SENTENCE_COUNT: int = 3
    
    # Content Settings
    MIN_TEXT_LENGTH: int = 50
    MAX_CONTENT_LENGTH: int = 1000000  # 1MB
    FETCH_DOCUMENT_LINKS: bool = True
    DOCUMENT_LINK_LIMIT: int = 2
    DOCUMENT_MAX_BYTES: int = 5 * 1024 * 1024
    
    # User Agents Pool
    USER_AGENTS: List[str] = [
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    ]

    # Search Defaults
    SEARCH_DEFAULT_REGION: str = "wt-wt"
    SEARCH_DEFAULT_SAFE: str = "moderate"
    SEARCH_MAX_PREVIEW: int = 5
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
