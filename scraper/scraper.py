"""
Web Scraper with dual engines: BeautifulSoup (fast) and Selenium (JS-enabled)
Includes anti-bot detection bypass mechanisms
"""
import os
import time
import random
import shutil
import threading
import platform
import re
import subprocess
from typing import Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import WebDriverException
from tenacity import retry, stop_after_attempt, wait_exponential
from config import settings
from utils import (
    get_stable_user_agent,
    is_valid_url,
    estimate_js_requirement,
    decode_response_content,
    logger
)


class WebScraper:
    """
    Dual-engine web scraper with anti-bot protection bypass
    """
    
    def __init__(self):
        # Use a stable UA for the lifetime of this scraper process.
        # Frequent UA rotation (with stable TLS/cookies) can look automated.
        self._requests_user_agent = get_stable_user_agent()
        # Keep Selenium UA stable per driver instance.
        # Also keep it aligned with the requests UA to reduce fingerprint mismatch
        # when falling back between engines.
        self._selenium_user_agent = self._requests_user_agent
        self.session = self._create_session()
        self._driver_lock = threading.Lock()
        self._driver: Optional[webdriver.Chrome] = None
        
    def _create_session(self) -> requests.Session:
        """Create a requests session with headers"""
        session = requests.Session()
        session.headers.update({
            'User-Agent': self._requests_user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0',
        })
        return session

    def _running_in_docker(self) -> bool:
        return os.path.exists('/.dockerenv')

    def _resolve_driver_backend(self, minimal_uc_mode: bool) -> str:
        backend = (getattr(settings, 'DRIVER_BACKEND', 'auto') or 'auto').strip().lower()
        if backend not in {'auto', 'selenium', 'uc'}:
            logger.warning("Invalid DRIVER_BACKEND=%r; using 'auto'", backend)
            backend = 'auto'

        if backend == 'auto':
            if minimal_uc_mode:
                return 'uc'
            if bool(getattr(settings, 'USE_UNDETECTED_CHROME', False)):
                return 'uc'
            return 'selenium'
        return backend

    def _cleanup_chrome_profile_locks(self, chrome_user_data_dir: str) -> None:
        if not chrome_user_data_dir:
            return
        lock_files = [
            'SingletonLock',
            'SingletonCookie',
            'SingletonSocket',
            'lockfile',
        ]
        for name in lock_files:
            path = os.path.join(chrome_user_data_dir, name)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as exc:
                logger.debug("Could not remove Chrome profile lock file %s: %s", path, exc)
    
    def _get_selenium_driver(self) -> webdriver.Chrome:
        """
        Create an undetected Chrome driver to bypass anti-bot protection
        
        Returns:
            Configured Chrome WebDriver
        """
        try:
            options = webdriver.ChromeOptions()

            minimal_uc_mode = bool(getattr(settings, 'MINIMAL_UC_MODE', False))
            driver_user_agent = self._selenium_user_agent
            
            # Headless mode
            if settings.HEADLESS:
                options.add_argument('--headless=new')

            # Persist session/cookies when running locally (helps with FF login + challenges)
            chrome_user_data_dir = (getattr(settings, 'CHROME_USER_DATA_DIR', '') or '').strip()
            if chrome_user_data_dir:
                self._cleanup_chrome_profile_locks(chrome_user_data_dir)
                options.add_argument(f'--user-data-dir={chrome_user_data_dir}')

            chrome_profile_dir = (getattr(settings, 'CHROME_PROFILE_DIRECTORY', '') or '').strip()
            if chrome_profile_dir:
                options.add_argument(f'--profile-directory={chrome_profile_dir}')

            if minimal_uc_mode:
                # Mimic the stable forexfactory-scraper approach: undetected_chromedriver with
                # very few flags. This reduces startup crashes on macOS and often avoids
                # anti-bot loops triggered by "weird" Chrome flags.
                options.add_argument('--window-size=1400,1000')
            else:
                # Anti-detection arguments
                options.add_argument('--disable-blink-features=AutomationControlled')
                # These flags are primarily for Linux/containers. On macOS they can
                # cause Chrome to fail to start (e.g. "DevToolsActivePort file doesn't exist").
                if platform.system() == 'Linux':
                    options.add_argument('--disable-dev-shm-usage')
                    options.add_argument('--no-sandbox')
                options.add_argument('--disable-gpu')
                options.add_argument('--disable-software-rasterizer')
                options.add_argument(f'--user-agent={driver_user_agent}')

                # Keep arguments minimal; extra flags can make some anti-bot systems loop.
                options.add_argument('--window-size=1920,1080')
                options.add_argument('--start-maximized')
            
            # Preferences to avoid detection
            prefs = {
                'profile.default_content_setting_values': {
                    'notifications': 2,
                    'media_stream': 2,
                },
                'credentials_enable_service': False,
                'profile.password_manager_enabled': False
            }
            options.add_experimental_option('prefs', prefs)

            # Resolve binary and driver paths allowing overrides via env or config
            binary_location = (
                settings.SELENIUM_BROWSER_BINARY
                or os.getenv('CHROME_BIN')
                or shutil.which('chromium')
                or shutil.which('google-chrome')
            )

            if not binary_location:
                # Common macOS install locations
                mac_paths = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                ]
                for candidate in mac_paths:
                    if os.path.exists(candidate):
                        binary_location = candidate
                        break
            driver_path = (
                settings.SELENIUM_DRIVER_PATH
                or os.getenv('CHROMEDRIVER_PATH')
                or shutil.which('chromedriver')
            )

            if binary_location:
                options.binary_location = binary_location
            else:
                logger.warning("Chromium binary not found; Selenium may fail to start")

            # By default prefer Selenium Manager for local host runs on macOS.
            # webdriver-manager can occasionally return a binary that exits immediately.
            if not driver_path and os.getenv("USE_WEBDRIVER_MANAGER", "0") == "1":
                try:
                    from webdriver_manager.chrome import ChromeDriverManager

                    driver_path = ChromeDriverManager().install()
                    logger.info(f"Downloaded chromedriver via webdriver-manager: {driver_path}")
                except Exception as exc:
                    logger.warning(f"Unable to download chromedriver via webdriver-manager: {exc}")
                    driver_path = None

            service = Service(executable_path=driver_path) if driver_path else None

            # Create driver
            driver_backend = self._resolve_driver_backend(minimal_uc_mode)
            use_undetected = driver_backend == 'uc'
            if use_undetected:
                try:
                    import undetected_chromedriver as uc
                except Exception as exc:
                    raise RuntimeError(f"undetected_chromedriver backend requested but unavailable: {exc}") from exc

            if use_undetected:
                # uc.Chrome manages driver patching automatically.
                # Use deterministic browser major pinning to avoid version drift.
                uc_kwargs: Dict[str, Any] = {
                    "options": options,
                    "use_subprocess": bool(getattr(settings, 'UC_USE_SUBPROCESS', False)),
                }

                if binary_location:
                    uc_kwargs["browser_executable_path"] = binary_location

                    major_match = None
                    uc_version_main = int(getattr(settings, 'UC_VERSION_MAIN', 0) or 0)
                    try:
                        version_output = subprocess.check_output(
                            [binary_location, "--version"],
                            text=True,
                            stderr=subprocess.STDOUT,
                        )
                        major_match = re.search(r"(\d+)\.\d+\.\d+\.\d+", version_output)
                    except Exception as exc:
                        logger.warning(f"Could not determine Chrome version for UC pinning: {exc}")

                    if uc_version_main > 0:
                        uc_kwargs["version_main"] = uc_version_main
                    elif major_match:
                        uc_kwargs["version_main"] = int(major_match.group(1))

                driver = uc.Chrome(**uc_kwargs)
            else:
                # If service is None, Selenium Manager will attempt to locate/download a driver.
                if service is not None:
                    driver = webdriver.Chrome(service=service, options=options)
                else:
                    driver = webdriver.Chrome(options=options)

            logger.info("Driver backend initialized: %s (headless=%s, in_docker=%s)", driver_backend, settings.HEADLESS, self._running_in_docker())
            
            # Execute stealth scripts (skip in minimal mode to keep behavior closer to
            # a real interactive browser session and avoid CDP-related oddities).
            if not minimal_uc_mode:
                driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                driver.execute_cdp_cmd('Network.setUserAgentOverride', {
                    "userAgent": driver_user_agent
                })
                driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                    'source': '''
                        delete Object.getPrototypeOf(navigator).webdriver
                    '''
                })
            
            # Set timeouts
            driver.set_page_load_timeout(settings.PAGE_LOAD_TIMEOUT)
            driver.implicitly_wait(settings.IMPLICIT_WAIT)
            
            logger.info("Selenium driver created successfully")
            return driver
            
        except Exception as e:
            logger.error(f"Error creating Selenium driver: {e}")
            raise

    def _ensure_driver(self) -> webdriver.Chrome:
        """Get (or lazily create) the single Selenium driver."""
        if self._driver is None:
            logger.info("Initializing Selenium driver...")
            self._driver = self._get_selenium_driver()
        return self._driver

    def _normalize_driver_window_context(self, driver: webdriver.Chrome) -> None:
        """Keep Selenium attached to a single tab to avoid hidden/pseudo-window drift."""
        try:
            target_handle = driver.current_window_handle
            handles = driver.window_handles
            if not handles:
                return

            if target_handle not in handles:
                target_handle = handles[0]

            for handle in list(handles):
                if handle == target_handle:
                    continue
                try:
                    driver.switch_to.window(handle)
                    driver.close()
                except Exception as exc:
                    logger.debug("Could not close extra Selenium tab %s: %s", handle, exc)

            driver.switch_to.window(target_handle)
            try:
                driver.maximize_window()
            except Exception:
                pass
        except Exception as exc:
            logger.debug("Could not normalize Selenium window context: %s", exc)

    def _reset_driver(self) -> None:
        """Force-close and recreate the Selenium driver on next use."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
        self._driver = None
    
    @retry(stop=stop_after_attempt(1), wait=wait_exponential(multiplier=1, min=2, max=10))
    def scrape_with_requests(self, url: str) -> Tuple[str, int]:
        """
        Scrape using requests and BeautifulSoup (fast, for static content)
        
        Args:
            url: URL to scrape
            
        Returns:
            Tuple of (HTML content, status code)
        """
        logger.info(f"Scraping with requests: {url}")
        
        # Keep UA stable for the lifetime of the session.
        self.session.headers['User-Agent'] = self._requests_user_agent
        
        response = self.session.get(
            url,
            timeout=settings.DEFAULT_TIMEOUT,
            allow_redirects=True
        )
        response.raise_for_status()

        html = decode_response_content(response)

        logger.info(f"Successfully scraped with requests: {url} (status: {response.status_code})")
        return html, response.status_code
    
    def scrape_with_selenium(self, url: str, wait_for_js: bool = True) -> str:
        """
        Scrape using Selenium (for JavaScript-rendered content)
        Uses a single persistent Chrome instance guarded by a lock.
        
        Args:
            url: URL to scrape
            wait_for_js: Whether to wait for JavaScript to load
            
        Returns:
            HTML content
        """
        logger.info(f"Scraping with Selenium: {url}")

        def _is_notice_login_required(title_l: str, page_l: str) -> bool:
            # ForexFactory shows a Notice page for login-only content.
            # This is NOT a solvable Cloudflare challenge; waiting here just stalls the run.
            if "notice" in title_l and "forex factory" in title_l:
                if "please log in" in page_l:
                    return True
                if "only accessible to registered traders" in page_l:
                    return True
            return False

        def _looks_like_article_page(page_l: str) -> bool:
            # Article pages include stable container hints. If these are present,
            # we should scrape immediately even if Cloudflare JS assets exist.
            return (
                "li.news__article" in page_l
                or "news__article" in page_l
                or "news__title" in page_l
                or "news__body" in page_l
            )

        def _challenge_matches(title_l: str, page_l: str) -> list[str]:
            """Return markers for a real Cloudflare/human-verification challenge.

            IMPORTANT: Some normal/notice pages may include Cloudflare JS assets (e.g. /cdn-cgi/...) but are not
            solvable challenges. We only treat it as a challenge if it looks like an interstitial.
            """
            if not title_l and not page_l:
                return []

            # If the content is a Notice/login-required page, do NOT treat as challenge.
            if _is_notice_login_required(title_l, page_l):
                return []

            # If it looks like a normal article page, do NOT treat as challenge.
            if _looks_like_article_page(page_l):
                return []

            matches: list[str] = []

            # Real interstitials
            if "just a moment" in title_l:
                matches.append("title:just a moment")

            if "please verify you are a human" in title_l:
                matches.append("title:please verify you are a human")

            if "verify you are human" in page_l:
                matches.append("page:verify you are human")
            if "needs to review the security of your connection" in page_l:
                matches.append("page:review the security")
            if "cf-challenge" in page_l:
                matches.append("page:cf-challenge")
            if "cloudflare" in page_l and "attention required" in page_l:
                matches.append("page:cloudflare + attention required")

            # Cloudflare JS assets alone are not sufficient. Only treat as a challenge if combined with other
            # indicators above, or if we detect explicit interstitial markup.
            if "/cdn-cgi/challenge-platform" in page_l and matches:
                matches.append("page:/cdn-cgi/challenge-platform")

            return matches

        def _log_challenge_debug_evidence(driver, title_l: str, page_l: str, matches: list[str]) -> None:
            if not getattr(settings, "CLOUDFLARE_DEBUG_EVIDENCE", False):
                return
            try:
                logger.warning(
                    "Cloudflare detector matched=%s title=%r current_url=%r",
                    matches,
                    driver.title,
                    getattr(driver, "current_url", ""),
                )

                # Log a tiny excerpt around the first matched pattern (best-effort)
                patterns = [
                    "verify you are human",
                    "needs to review the security of your connection",
                    "/cdn-cgi/challenge-platform",
                    "cf-challenge",
                    "attention required",
                    "cloudflare",
                ]
                for pat in patterns:
                    idx = page_l.find(pat)
                    if idx != -1:
                        start = max(0, idx - 80)
                        end = min(len(page_l), idx + 160)
                        logger.warning("Cloudflare page excerpt around %r: %r", pat, page_l[start:end])
                        break
            except Exception:
                pass

        def _wait_for_manual_challenge(driver, *, attempt: int) -> bool:
            if settings.HEADLESS:
                return False

            max_attempts = int(getattr(settings, 'CLOUDFLARE_CHALLENGE_MAX_ATTEMPTS', 2) or 2)
            per_attempt_wait = int(getattr(settings, 'CLOUDFLARE_CHALLENGE_WAIT_SECONDS', 60) or 60)
            per_attempt_wait = max(5, per_attempt_wait)

            logger.warning(
                "Cloudflare/human verification detected (attempt %s/%s). "
                "Solve it in the Chrome window within %ss...",
                attempt,
                max_attempts,
                per_attempt_wait,
            )

            if getattr(settings, "CLOUDFLARE_DEBUG_EVIDENCE", False):
                try:
                    logger.warning(
                        "Cloudflare evidence: title=%r current_url=%r",
                        driver.title,
                        getattr(driver, "current_url", ""),
                    )
                except Exception:
                    pass

            # Per requirement: do not poll every N seconds and do not return early.
            # Just wait the full window (even if user solves early), then proceed.
            time.sleep(float(per_attempt_wait))

            title_l = (driver.title or "").lower()
            page_l = (driver.page_source or "").lower()
            if not _challenge_matches(title_l, page_l):
                logger.info("Challenge appears cleared after wait window.")
                return True
            return False

        with self._driver_lock:
            driver = self._ensure_driver()

            try:
                self._normalize_driver_window_context(driver)

                # Navigate to URL
                driver.get(url)

                if wait_for_js:
                    # Wait for body to be present
                    try:
                        WebDriverWait(driver, settings.SELENIUM_TIMEOUT).until(
                            EC.presence_of_element_located((By.TAG_NAME, "body"))
                        )
                    except TimeoutException:
                        logger.warning(f"Timeout waiting for page body: {url}")

                    # Cloudflare / bot challenges are sometimes served temporarily.
                    # Poll for the real article page to appear.
                    # If a Cloudflare/human verification page is detected, allow manual solving
                    # in headful mode, up to N attempts. If not solved, CLOSE Chrome and fail.
                    max_attempts = int(getattr(settings, 'CLOUDFLARE_CHALLENGE_MAX_ATTEMPTS', 2) or 2)
                    for attempt in range(1, max_attempts + 1):
                        title_l = (driver.title or "").lower()
                        page_l = (driver.page_source or "").lower()
                        matches = _challenge_matches(title_l, page_l)
                        if not matches:
                            break

                        _log_challenge_debug_evidence(driver, title_l, page_l, matches)

                        cleared = _wait_for_manual_challenge(driver, attempt=attempt)
                        if cleared:
                            break

                        # Give it a second shot by refreshing (in case user was AFK).
                        if attempt < max_attempts:
                            try:
                                driver.refresh()
                            except Exception:
                                driver.get(url)

                    # Final check: if still challenge, abort hard.
                    title_l = (driver.title or "").lower()
                    page_l = (driver.page_source or "").lower()
                    if _challenge_matches(title_l, page_l):
                        logger.error(
                            "Cloudflare challenge not solved after %s attempts. Closing Chrome.",
                            max_attempts,
                        )
                        self._reset_driver()
                        raise RuntimeError("cloudflare_unsolved")

                    # Additional wait + scroll to load lazy content
                    time.sleep(random.uniform(1, 2))
                    self._scroll_page(driver)

                html_content = driver.page_source
                logger.info(f"Successfully scraped with Selenium: {url}")
                return html_content

            except (WebDriverException, TimeoutException) as e:
                logger.error(f"Selenium driver error scraping {url}: {e}")
                # Driver can get into a bad state; reset and allow caller to retry/fallback.
                self._reset_driver()
                raise
            except Exception as e:
                logger.error(f"Error scraping with Selenium: {e}")
                raise
    
    def _scroll_page(self, driver):
        """Scroll page to trigger lazy loading"""
        try:
            # Get page height
            scroll_pause_time = 0.5
            last_height = driver.execute_script("return document.body.scrollHeight")
            
            # Scroll in steps
            for i in range(3):
                # Scroll down
                driver.execute_script(f"window.scrollTo(0, {last_height * (i + 1) / 3});")
                time.sleep(scroll_pause_time)
            
            # Scroll back to top
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(scroll_pause_time)
            
        except Exception as e:
            logger.warning(f"Error scrolling page: {e}")
    
    def scrape(
        self, 
        url: str, 
        force_selenium: bool = True,  # Changed default to True - always use Selenium
        auto_detect_js: bool = True
    ) -> Dict[str, Any]:
        """
        Intelligent scraping with automatic fallback
        Now defaults to Selenium for better reliability with Cloudflare-protected sites
        
        Args:
            url: URL to scrape
            force_selenium: Force using Selenium (default: True)
            auto_detect_js: Automatically detect if JS is needed
            
        Returns:
            Dictionary with scraping results
        """
        if not is_valid_url(url):
            raise ValueError(f"Invalid URL: {url}")
        
        result = {
            'success': False,
            'url': url,
            'html': '',
            'method': '',
            'status_code': None,
            'error': None
        }
        
        try:
            # Always use Selenium by default (changed from False to True)
            if force_selenium:
                result['html'] = self.scrape_with_selenium(url)
                result['method'] = 'selenium'
                result['success'] = True
                return result
            
            # Try requests first (faster) - only if explicitly disabled force_selenium
            try:
                html, status_code = self.scrape_with_requests(url)
                result['html'] = html
                result['status_code'] = status_code
                result['method'] = 'requests'
                
                # Check if JS rendering is needed
                if auto_detect_js and estimate_js_requirement(html):
                    logger.info(f"JavaScript detected, switching to Selenium for: {url}")
                    result['html'] = self.scrape_with_selenium(url)
                    result['method'] = 'selenium (auto-detected)'
                
                result['success'] = True
                return result
                
            except Exception as e:
                logger.warning(f"Requests engine failed ({e}); attempting Selenium as last resort")
                result['html'] = self.scrape_with_selenium(url)
                result['method'] = 'selenium (fallback)'
                result['success'] = True
                return result
                
        except Exception as e:
            logger.error(f"Scraping failed for {url}: {e}")
            result['error'] = str(e)
            return result
    
    def close(self):
        """Clean up resources"""
        # Close the single Selenium driver
        with self._driver_lock:
            if self._driver is not None:
                try:
                    logger.info("Closing Selenium driver...")
                    self._driver.quit()
                except Exception as e:
                    logger.error(f"Error closing Selenium driver: {e}")
                finally:
                    self._driver = None
        
        if self.session:
            self.session.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
