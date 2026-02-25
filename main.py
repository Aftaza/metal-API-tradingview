"""
Metal Price Real-time API System dengan 4 Persistent Tabs
- 3 tab untuk metal prices dari Kitco (Gold, Silver, Copper)
- 1 tab untuk USDIDR exchange rate dari TradingView (persistent)
- Auto-recovery untuk crashed tabs
- Parallel extraction dengan thread pool
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Dict, List
import logging
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from bs4 import BeautifulSoup

# Selenium imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, WebDriverException, StaleElementReferenceException
)

# Configuration
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Konstanta
TROY_OUNCE_TO_GRAM = 31.1034768
POUND_TO_GRAM = 453.592

# Kitco URLs (gold & silver per troy ounce, copper per lb)
KITCO_SOURCES = {
    "gold": {"url": "https://www.kitco.com/charts/gold", "name": "Gold", "unit": "tryounce"},
    "silver": {"url": "https://www.kitco.com/charts/silver", "name": "Silver", "unit": "tryounce"},
    "copper": {"url": "https://www.kitco.com/price/base-metals/copper", "name": "Copper", "unit": "lb"}
}

# Kitco wait selector (Bid price h3)
KITCO_WAIT_SELECTOR = "h3.font-mulish"

USDIDR_CONFIG = {
    "symbol": "USDIDR",
    "url": "https://www.tradingview.com/symbols/USDIDR/",
    "name": "USD to IDR"
}

# TradingView wait selector (untuk USDIDR)
TRADINGVIEW_WAIT_SELECTOR = "span[data-qa-id='symbol-last-value']"

def _get_wait_selector(key: str) -> str:
    """Return CSS selector for waiting based on source"""
    if key == "usdidr":
        return TRADINGVIEW_WAIT_SELECTOR
    return KITCO_WAIT_SELECTOR

# Data models
class MetalPrice(BaseModel):
    metal: str
    price_usd: float
    price_unit: str  # "per troy ounce" or "per lb"
    price_per_gram_usd: float
    price_per_gram_idr: Optional[float] = None
    currency: str = "USD"
    timestamp: str
    source: str = "Kitco"

class MetalPriceResponse(BaseModel):
    status: str
    data: List[MetalPrice]
    exchange_rate_usdidr: Optional[float] = None
    last_updated: str

class MetalPriceWithGram(BaseModel):
    metal: str
    gram: float
    price_per_unit_usd: float
    price_unit: str  # "per troy ounce" or "per lb"
    price_per_gram_usd: float
    total_price_usd: float
    price_per_gram_idr: Optional[float] = None
    total_price_idr: Optional[float] = None
    currency: str
    exchange_rate: Optional[float] = None
    timestamp: str
    source: str = "Kitco"
    conversion_info: dict

# Global state
price_cache: Dict = {
    "gold": None,
    "silver": None,
    "copper": None,
    "usdidr": None,
    "last_update": None,
    "html_cache": {},
    "tab_status": {}
}

thread_pool = ThreadPoolExecutor(max_workers=4)  # 3 metals + 1 USDIDR
cache_lock = threading.RLock()

class MultiTabBrowserScraper:
    """Multi-tab browser scraper dengan 4 persistent tabs"""
    
    def __init__(self):
        self.driver = None
        self.tabs = {}  # metal/usdidr -> tab handle mapping
        self.lock = threading.RLock()
        self.profile_dir = None
    
    def _create_chrome_options(self):
        """Create optimized Chrome options"""
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-images")
        chrome_options.add_argument("--window-size=1280,720")
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        import tempfile, uuid
        self.profile_dir = f"/tmp/chrome-profile-{uuid.uuid4()}"
        chrome_options.add_argument(f"--user-data-dir={self.profile_dir}")
        
        prefs = {
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.popups": 2,
        }
        chrome_options.add_experimental_option("prefs", prefs)
        
        return chrome_options
    
    def initialize(self):
        """Initialize browser dengan 4 persistent tabs (3 metals + 1 USDIDR)"""
        logger.info("=" * 60)
        logger.info("Initializing Browser with 4 Persistent Tabs...")
        logger.info("=" * 60)
        
        try:
            chrome_options = self._create_chrome_options()
            self.driver = webdriver.Chrome(options=chrome_options)
            logger.info("✓ Browser initialized")
            
            self.driver.set_page_load_timeout(30)
            self.driver.implicitly_wait(5)
            
            # List semua tab yang akan dibuat: 3 metals + 1 USDIDR
            all_tabs_config = []
            
            # Tambahkan metals dari Kitco
            for metal, config in KITCO_SOURCES.items():
                all_tabs_config.append({
                    "key": metal,
                    "url": config['url'],
                    "name": config['name']
                })
            
            # Tambahkan USDIDR
            all_tabs_config.append({
                "key": "usdidr",
                "url": USDIDR_CONFIG['url'],
                "name": USDIDR_CONFIG['name']
            })
            
            total_tabs = len(all_tabs_config)
            
            # Buat semua tab
            for idx, tab_config in enumerate(all_tabs_config):
                try:
                    key = tab_config['key']
                    url = tab_config['url']
                    name = tab_config['name']
                    
                    if idx == 0:
                        # Tab pertama (sudah terbuka)
                        self.tabs[key] = self.driver.current_window_handle
                        logger.info(f"Tab {idx+1}/{total_tabs}: Using existing tab for {name.upper()}")
                    else:
                        # Buat tab baru
                        self.driver.execute_script("window.open('');")
                        self.driver.switch_to.window(self.driver.window_handles[-1])
                        self.tabs[key] = self.driver.current_window_handle
                        logger.info(f"Tab {idx+1}/{total_tabs}: Created new tab for {name.upper()}")
                    
                    # Load URL
                    try:
                        logger.info(f"Loading {name}...")
                        self.driver.get(url)
                        
                        # Wait untuk page load (selector berbeda per source)
                        wait_selector = _get_wait_selector(key)
                        wait = WebDriverWait(self.driver, 20)
                        wait.until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
                        )
                        
                        # Extra wait untuk rendering
                        time.sleep(1.5)
                        
                        with cache_lock:
                            price_cache["tab_status"][key] = "active"
                        
                        logger.info(f"✓ Loaded {name.upper()}: {url}")
                        
                    except Exception as e:
                        logger.error(f"Error loading {name.upper()}: {e}")
                        with cache_lock:
                            price_cache["tab_status"][key] = "error"
                    
                    time.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Error creating/loading tab for {name}: {e}")
                    with cache_lock:
                        price_cache["tab_status"][key] = "error"
            
            logger.info("=" * 60)
            logger.info(f"✓ Browser initialization complete - {total_tabs} tabs active")
            logger.info(f"  Metals (Kitco): {list(KITCO_SOURCES.keys())}")
            logger.info(f"  Currency (TradingView): USDIDR")
            logger.info("=" * 60)
            
            return True
            
        except Exception as e:
            logger.error(f"Error initializing browser: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def _check_tab_health(self, key: str) -> bool:
        """Check apakah tab masih sehat"""
        try:
            if key not in self.tabs:
                return False
            self.driver.switch_to.window(self.tabs[key])
            self.driver.execute_script("return true;")
            return True
        except (WebDriverException, Exception):
            return False
    
    def _recover_tab(self, key: str) -> bool:
        """Recover crashed tab"""
        logger.warning(f"Attempting to recover tab for {key}...")
        
        try:
            with self.lock:
                # Tentukan URL berdasarkan key
                if key == "usdidr":
                    url = USDIDR_CONFIG['url']
                    name = USDIDR_CONFIG['name']
                else:
                    url = KITCO_SOURCES[key]['url']
                    name = KITCO_SOURCES[key]['name']
                
                # Close tab yang rusak
                if key in self.tabs:
                    try:
                        self.driver.switch_to.window(self.tabs[key])
                        self.driver.close()
                        logger.info(f"Closed crashed tab for {name}")
                    except:
                        pass
                
                # Buat tab baru
                self.driver.execute_script("window.open('');")
                self.driver.switch_to.window(self.driver.window_handles[-1])
                self.tabs[key] = self.driver.current_window_handle
                logger.info(f"Created new tab for {name}")
                
                # Load URL
                self.driver.get(url)
                
                # Wait untuk page render (selector berbeda per source)
                wait_selector = _get_wait_selector(key)
                wait = WebDriverWait(self.driver, 15)
                wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
                )
                
                with cache_lock:
                    price_cache["tab_status"][key] = "recovered"
                logger.info(f"✓ Tab recovered successfully for {name}")
                return True
                
        except Exception as e:
            logger.error(f"Tab recovery failed for {key}: {e}")
            with cache_lock:
                price_cache["tab_status"][key] = "error"
            return False
    
    def load_and_save_html(self, key: str, refresh: bool = False) -> bool:
        """Load tab dan simpan HTML dengan auto-recovery
        
        Args:
            key: Tab key (metal name atau 'usdidr')
            refresh: Jika True, refresh halaman dulu
        """
        try:
            with self.lock:
                # Check tab health
                if not self._check_tab_health(key):
                    logger.warning(f"Tab for {key} is unhealthy, attempting recovery...")
                    if not self._recover_tab(key):
                        logger.error(f"Failed to recover tab for {key}")
                        return False
                
                if key not in self.tabs:
                    logger.error(f"Tab untuk {key} tidak ditemukan")
                    return False
                
                # Switch ke tab
                self.driver.switch_to.window(self.tabs[key])
                logger.debug(f"Switched to {key} tab")
                
                # Refresh halaman jika diperlukan
                if refresh:
                    try:
                        self.driver.refresh()
                        logger.debug(f"Refreshed {key} tab")
                    except WebDriverException as e:
                        logger.error(f"Refresh failed for {key}: {e}")
                        return self._recover_tab(key) and self.load_and_save_html(key, refresh)
                
                # Wait untuk element muncul (selector berbeda per source)
                try:
                    wait = WebDriverWait(self.driver, 10)
                    wait_selector = _get_wait_selector(key)
                    
                    # Wait element visible
                    wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)),
                        message=f"Element not found for {key}"
                    )
                    
                    # Ensure ada text
                    wait.until(
                        lambda d: len(d.find_element(By.CSS_SELECTOR, wait_selector).text.strip()) > 0,
                        message=f"No text for {key}"
                    )
                    
                    logger.debug(f"HTML ready for {key}")
                    
                except TimeoutException as e:
                    logger.error(f"Timeout for {key}: {e}")
                    return False
                except StaleElementReferenceException:
                    logger.error(f"Stale element for {key}, retrying...")
                    return self.load_and_save_html(key, refresh)
                
                # Simpan HTML
                try:
                    html = self.driver.page_source
                    if html and len(html) > 1000:
                        with cache_lock:
                            price_cache["html_cache"][key] = html
                            price_cache["tab_status"][key] = "active"
                        logger.info(f"✓ HTML extracted for {key.upper()} ({len(html)} bytes)")
                        return True
                    else:
                        logger.warning(f"Invalid HTML size for {key}")
                        return False
                        
                except Exception as e:
                    logger.error(f"Error saving HTML for {key}: {e}")
                    return False
                    
        except WebDriverException as e:
            logger.error(f"WebDriver error for {key}: {e}")
            with cache_lock:
                price_cache["tab_status"][key] = "error"
            return False
        except Exception as e:
            logger.error(f"Unexpected error for {key}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def refresh_all_tabs(self, refresh: bool = False, include_usdidr: bool = True):
        """Refresh semua tab (metals + USDIDR)
        
        Args:
            refresh: Jika True, refresh halaman dulu
            include_usdidr: Jika True, include USDIDR tab
        """
        action = "Refreshing" if refresh else "Extracting"
        logger.info(f"{action} all tabs...")
        
        results = {}
        
        # Refresh metal tabs
        for metal in KITCO_SOURCES.keys():
            try:
                success = self.load_and_save_html(metal, refresh=refresh)
                results[metal] = success
            except Exception as e:
                logger.error(f"Error processing {metal}: {e}")
                results[metal] = False
            time.sleep(0.3)
        
        # Refresh USDIDR tab
        if include_usdidr:
            try:
                success = self.load_and_save_html("usdidr", refresh=refresh)
                results["usdidr"] = success
            except Exception as e:
                logger.error(f"Error processing USDIDR: {e}")
                results["usdidr"] = False
        
        return results
    
    def close(self):
        """Close browser dan cleanup"""
        try:
            if self.driver:
                self.driver.quit()
                logger.info("✓ Browser closed")
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
        
        try:
            import shutil, os
            if self.profile_dir and os.path.exists(self.profile_dir):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                logger.info("✓ Profile directory cleaned")
        except Exception as e:
            logger.error(f"Error cleaning profile: {e}")

# Global browser instance
browser_scraper: Optional[MultiTabBrowserScraper] = None

def extract_price_from_html(key: str) -> Optional[float]:
    """Extract harga/rate dari HTML yang sudah disimpan
    
    Args:
        key: 'gold', 'silver', 'copper', atau 'usdidr'
    """
    try:
        with cache_lock:
            html = price_cache["html_cache"].get(key)
        
        if not html:
            logger.warning(f"No HTML cached for {key}")
            return None
        
        soup = BeautifulSoup(html, 'lxml')
        
        if key == "usdidr":
            # TradingView selector untuk USDIDR
            symbol_last_value = soup.find('span', attrs={'data-qa-id': 'symbol-last-value'})
            
            if symbol_last_value:
                text_content = symbol_last_value.get_text(strip=True)
                logger.debug(f"Raw text for {key}: {text_content}")
                
                price_str = text_content.replace(',', '')
                
                # Handle formatting untuk USDIDR
                if len(price_str) > 3 and '.' not in price_str:
                    price_str = price_str[:-2] + '.' + price_str[-2:]
                
                try:
                    value = float(price_str)
                    if 10000 < value < 20000:
                        logger.info(f"✓ Extracted {key.upper()}: {value:,.2f}")
                        return value
                    else:
                        logger.warning(f"USDIDR {value} outside valid range")
                except ValueError as e:
                    logger.error(f"Could not parse value {price_str}: {e}")
            else:
                logger.warning(f"Could not find price element for {key}")
        else:
            # Kitco selector untuk metals - Bid price h3
            # Cari h3 dengan class yang mengandung font-mulish, text-4xl, font-bold
            bid_h3 = soup.find('h3', class_=lambda c: c and 'font-mulish' in c and 'text-4xl' in c and 'font-bold' in c)
            
            if bid_h3:
                text_content = bid_h3.get_text(strip=True)
                logger.debug(f"Raw text for {key}: {text_content}")
                
                # Parse price (format: 5,156.30 atau 5.7773)
                price_str = text_content.replace(',', '')
                
                try:
                    value = float(price_str)
                    
                    # Validasi range berdasarkan metal
                    if key == "copper":
                        # Copper price per lb: 0.1 - 20
                        if 0.1 < value < 20:
                            logger.info(f"✓ Extracted {key.upper()}: ${value}/lb")
                            return value
                        else:
                            logger.warning(f"Copper price {value} outside valid range")
                    else:
                        # Gold/Silver price per troy ounce: 1 - 100,000
                        if 1 < value < 100000:
                            logger.info(f"✓ Extracted {key.upper()}: ${value}/oz")
                            return value
                        else:
                            logger.warning(f"Price {value} outside valid range for {key}")
                            
                except ValueError as e:
                    logger.error(f"Could not parse value {price_str}: {e}")
            else:
                logger.warning(f"Could not find Kitco bid price element for {key}")
        
        return None
        
    except Exception as e:
        logger.error(f"Error extracting price for {key}: {e}")
        return None

def extract_all_prices_parallel(include_usdidr: bool = True) -> Dict[str, float]:
    """Extract semua prices/rates secara paralel dengan thread pool
    
    Args:
        include_usdidr: Jika True, include USDIDR extraction
    """
    logger.info("Extracting prices from cached HTML (parallel)...")
    
    prices_found = {}
    futures = {}
    
    # Submit metal extraction tasks
    for metal in KITCO_SOURCES.keys():
        future = thread_pool.submit(extract_price_from_html, metal)
        futures[metal] = future
    
    # Submit USDIDR extraction task
    if include_usdidr:
        future = thread_pool.submit(extract_price_from_html, "usdidr")
        futures["usdidr"] = future
    
    # Collect results
    for key, future in futures.items():
        try:
            value = future.result(timeout=10)
            if value:
                prices_found[key] = value
                with cache_lock:
                    if key == "usdidr":
                        price_cache["usdidr"] = {"rate": value, "source": "TradingView"}
                    else:
                        price_cache[key] = {"price": value, "source": "Kitco"}
        except Exception as e:
            logger.error(f"Error getting value for {key}: {e}")
    
    return prices_found

async def refresh_prices_on_request(include_usdidr: bool = True):
    """Refresh prices saat ada request"""
    
    if not browser_scraper:
        logger.error("Browser scraper not initialized")
        return False
    
    logger.info("=" * 60)
    logger.info("Extracting prices and exchange rate...")
    logger.info("=" * 60)
    
    # Extract HTML dari semua tab
    refresh_results = browser_scraper.refresh_all_tabs(refresh=False, include_usdidr=include_usdidr)
    
    success_count = sum(1 for s in refresh_results.values() if s)
    total_tabs = 4 if include_usdidr else 3
    logger.info(f"Successfully extracted {success_count}/{total_tabs} tabs")
    
    # Extract prices/rates secara paralel
    values_found = extract_all_prices_parallel(include_usdidr=include_usdidr)
    
    # Update timestamp
    with cache_lock:
        price_cache["last_update"] = datetime.utcnow().isoformat()
    
    logger.info("=" * 60)
    logger.info(f"Extraction complete. Got {len(values_found)} values")
    logger.info(f"Last update: {price_cache['last_update']}")
    logger.info("=" * 60)
    
    return len(values_found) > 0

async def manual_refresh_prices():
    """Manual refresh - refresh semua tab dulu baru extract"""
    
    if not browser_scraper:
        logger.error("Browser scraper not initialized")
        return False
    
    logger.info("=" * 60)
    logger.info("Manual refresh - refreshing all 4 tabs...")
    logger.info("=" * 60)
    
    # Refresh semua tab dengan auto-recovery
    refresh_results = browser_scraper.refresh_all_tabs(refresh=True, include_usdidr=True)
    
    success_count = sum(1 for s in refresh_results.values() if s)
    logger.info(f"Successfully refreshed {success_count}/4 tabs")
    
    # Extract prices/rates secara paralel
    values_found = extract_all_prices_parallel(include_usdidr=True)
    
    # Update timestamp
    with cache_lock:
        price_cache["last_update"] = datetime.utcnow().isoformat()
    
    logger.info("=" * 60)
    logger.info(f"Manual refresh complete. Got {len(values_found)} values")
    logger.info(f"Last update: {price_cache['last_update']}")
    logger.info("=" * 60)
    
    return len(values_found) > 0

async def lifespan(app: FastAPI):
    """Lifespan context manager untuk startup dan shutdown"""
    
    # Startup
    global browser_scraper
    logger.info("Application starting up...")
    
    browser_scraper = MultiTabBrowserScraper()
    if not browser_scraper.initialize():
        logger.error("Failed to initialize browser scraper")
        raise Exception("Browser initialization failed")
    
    # Initial refresh untuk semua tab
    await refresh_prices_on_request(include_usdidr=True)
    logger.info("Initial price update completed")
    
    yield
    
    # Shutdown
    logger.info("Application shutting down...")
    if browser_scraper:
        browser_scraper.close()
    thread_pool.shutdown(wait=True)
    logger.info("Shutdown completed")

app = FastAPI(
    title="Metal Price API - Kitco + TradingView",
    description="Real-time Metal Prices from Kitco + USDIDR Exchange Rate from TradingView (4 Active Tabs)",
    version="4.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", tags=["Info"])
async def root():
    return {
        "name": "Metal Price API - Kitco + TradingView",
        "version": "4.0.0",
        "source": {
            "metals": "Kitco (Selenium Scraping)",
            "currency": "TradingView (Selenium Scraping)"
        },
        "features": [
            "4 persistent tabs (3 metals + 1 USDIDR)",
            "Parallel extraction dengan thread pool",
            "Auto-recovery untuk crashed tabs",
            "Real-time exchange rate USDIDR",
            "Konversi otomatis USD ke IDR",
            "Copper per lb → gram conversion"
        ],
        "tabs": {
            "metals": list(KITCO_SOURCES.keys()),
            "currency": "USDIDR",
            "total": 4
        },
        "endpoints": {
            "GET /": "This endpoint",
            "GET /prices": "Get all metal prices with USDIDR rate and IDR conversion",
            "GET /prices/{metal}?gram={value}&currency=IDR": "Get specific metal price with gram conversion",
            "GET /health": "Health check",
            "POST /refresh": "Manual refresh all tabs",
            "GET /symbols": "Get list of symbols",
            "GET /exchange-rate": "Get USDIDR exchange rate",
            "GET /debug/cache": "Debug cache and tab status"
        }
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    with cache_lock:
        metal_count = len([p for p in price_cache if p in KITCO_SOURCES and price_cache[p]])
        usdidr_rate = price_cache.get("usdidr", {}).get("rate")
        tab_status = price_cache.get("tab_status", {})
    
    active_tabs = sum(1 for s in tab_status.values() if s == "active")
    
    return {
        "status": "healthy" if metal_count > 0 else "initializing",
        "last_update": price_cache.get("last_update"),
        "cached_metals": metal_count,
        "total_metals": len(KITCO_SOURCES),
        "usdidr_rate": usdidr_rate,
        "active_tabs": active_tabs,
        "total_tabs": 4,
        "tab_status": tab_status,
        "browser_active": browser_scraper is not None
    }

@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices():
    """
    Get semua harga metal dengan exchange rate USDIDR dan harga per gram IDR
    
    Returns:
    - Harga metal per troy ounce (USD)
    - Harga per gram (USD)
    - Harga per gram (IDR)
    - Exchange rate USDIDR
    """
    
    # Refresh semua data (metals + USDIDR)
    await refresh_prices_on_request(include_usdidr=True)
    
    with cache_lock:
        if not price_cache.get("last_update"):
            raise HTTPException(status_code=503, detail="Data not available yet")
        
        # Get USDIDR rate
        usdidr_rate = None
        if price_cache.get("usdidr"):
            usdidr_rate = price_cache["usdidr"].get("rate")
        
        # Build metal prices
        metals = list(KITCO_SOURCES.keys())
        prices = []
        
        for metal in metals:
            if price_cache.get(metal):
                raw_price = price_cache[metal]["price"]
                metal_config = KITCO_SOURCES[metal]
                
                # Konversi ke per gram tergantung unit
                if metal_config["unit"] == "lb":
                    price_per_gram_usd = raw_price / POUND_TO_GRAM
                    price_unit = "per lb"
                else:
                    price_per_gram_usd = raw_price / TROY_OUNCE_TO_GRAM
                    price_unit = "per troy ounce"
                
                # Hitung harga per gram IDR jika ada rate
                price_per_gram_idr = None
                if usdidr_rate:
                    price_per_gram_idr = price_per_gram_usd * usdidr_rate
                
                prices.append(
                    MetalPrice(
                        metal=metal.upper(),
                        price_usd=raw_price,
                        price_unit=price_unit,
                        price_per_gram_usd=round(price_per_gram_usd, 4),
                        price_per_gram_idr=round(price_per_gram_idr, 2) if price_per_gram_idr else None,
                        currency="USD/IDR" if usdidr_rate else "USD",
                        timestamp=price_cache["last_update"],
                        source="Kitco"
                    )
                )
    
    if not prices:
        raise HTTPException(status_code=503, detail="No metal data available")
    
    return MetalPriceResponse(
        status="success",
        data=prices,
        exchange_rate_usdidr=round(usdidr_rate, 2) if usdidr_rate else None,
        last_updated=price_cache.get("last_update", "")
    )

@app.get("/prices/{metal}", response_model=MetalPriceWithGram, tags=["Prices"])
async def get_metal_price(
    metal: str,
    gram: float = Query(..., description="Berat dalam gram", gt=0, example=10.0),
    currency: str = Query("USD", description="Currency (USD atau IDR)", regex="^(USD|IDR)$")
):
    """
    Get harga metal dengan konversi gram dan mata uang
    
    Parameters:
    - **metal**: Jenis metal (gold, silver, copper)
    - **gram**: Berat dalam gram (wajib, > 0)
    - **currency**: Mata uang output (USD atau IDR, default: USD)
    
    Returns:
    - Harga per unit (troy ounce atau lb) dan per gram
    - Total harga untuk gram yang diminta
    - Jika currency=IDR: nilai dalam IDR dengan exchange rate terbaru
    """
    
    metal = metal.lower()
    currency = currency.upper()
    
    if metal not in KITCO_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Metal tidak valid. Gunakan: {', '.join(KITCO_SOURCES.keys())}"
        )
    
    if gram <= 0:
        raise HTTPException(status_code=400, detail="Gram harus > 0")
    
    # Refresh metal prices dan USDIDR (jika perlu IDR)
    include_usdidr = (currency == "IDR")
    await refresh_prices_on_request(include_usdidr=include_usdidr)
    
    with cache_lock:
        if not price_cache.get(metal):
            raise HTTPException(status_code=503, detail=f"{metal.upper()} data tidak tersedia")
        
        raw_price = price_cache[metal]["price"]
    
    metal_config = KITCO_SOURCES[metal]
    
    # Kalkulasi USD berdasarkan unit
    if metal_config["unit"] == "lb":
        price_per_gram_usd = raw_price / POUND_TO_GRAM
        price_unit = "per lb"
        unit_factor_label = f"1 lb = {POUND_TO_GRAM} gram"
    else:
        price_per_gram_usd = raw_price / TROY_OUNCE_TO_GRAM
        price_unit = "per troy ounce"
        unit_factor_label = f"1 troy ounce = {TROY_OUNCE_TO_GRAM} gram"
    
    total_price_usd = price_per_gram_usd * gram
    
    # Response default (USD)
    response_data = {
        "metal": metal.upper(),
        "gram": gram,
        "price_per_unit_usd": round(raw_price, 4),
        "price_unit": price_unit,
        "price_per_gram_usd": round(price_per_gram_usd, 4),
        "total_price_usd": round(total_price_usd, 2),
        "currency": "USD",
        "timestamp": price_cache.get("last_update", ""),
        "source": "Kitco",
        "conversion_info": {
            "unit_conversion": unit_factor_label,
            "calculation_usd": f"{gram}g × ${round(price_per_gram_usd, 4)}/g = ${round(total_price_usd, 2)}"
        }
    }
    
    # Jika request IDR, ambil USDIDR rate dari cache
    if currency == "IDR":
        with cache_lock:
            usdidr_data = price_cache.get("usdidr")
        
        if not usdidr_data or not usdidr_data.get("rate"):
            raise HTTPException(
                status_code=503, 
                detail="USDIDR exchange rate tidak tersedia. Silakan coba lagi."
            )
        
        exchange_rate = usdidr_data["rate"]
        
        # Konversi ke IDR
        price_per_gram_idr = price_per_gram_usd * exchange_rate
        total_price_idr = total_price_usd * exchange_rate
        
        response_data.update({
            "price_per_gram_idr": round(price_per_gram_idr, 2),
            "total_price_idr": round(total_price_idr, 2),
            "currency": "IDR",
            "exchange_rate": round(exchange_rate, 2)
        })
        
        response_data["conversion_info"].update({
            "exchange_rate_usdidr": round(exchange_rate, 2),
            "calculation_idr": f"{gram}g × Rp{round(price_per_gram_idr, 2):,.0f}/g = Rp{round(total_price_idr, 2):,.0f}"
        })
    
    return MetalPriceWithGram(**response_data)

@app.post("/refresh", tags=["Admin"])
async def manual_refresh():
    """Manual refresh all prices dan USDIDR rate"""
    success = await manual_refresh_prices()
    
    with cache_lock:
        tab_status = price_cache.get("tab_status", {})
        usdidr_rate = price_cache.get("usdidr", {}).get("rate")
    
    return {
        "status": "success" if success else "partial",
        "message": "Manual refresh completed for all 4 tabs",
        "last_update": price_cache.get("last_update"),
        "tab_status": tab_status,
        "usdidr_rate": round(usdidr_rate, 2) if usdidr_rate else None,
        "total_tabs": 4
    }

@app.get("/symbols", tags=["Info"])
async def get_symbols():
    """Get list of symbols"""
    return {
        "metals": {
            metal: data['url'] 
            for metal, data in KITCO_SOURCES.items()
        },
        "currency": {
            "usdidr": USDIDR_CONFIG['url']
        },
        "description": "Metal prices dari Kitco, USDIDR dari TradingView",
        "scraping_method": "Multi-Tab Selenium (4 Persistent Tabs) + Thread Pool Extraction + Auto-Recovery",
        "total_tabs": 4
    }

@app.get("/debug/cache", tags=["Debug"])
async def debug_cache():
    """Debug cache status dan tab health"""
    with cache_lock:
        cached_metals = {
            metal: price_cache.get(metal, {}).get("price") 
            for metal in KITCO_SOURCES.keys()
        }
        html_size = {
            key: len(price_cache.get("html_cache", {}).get(key, ""))
            for key in list(KITCO_SOURCES.keys()) + ["usdidr"]
        }
        tab_status = price_cache.get("tab_status", {})
        usdidr_rate = price_cache.get("usdidr", {}).get("rate")
    
    return {
        "last_update": price_cache.get("last_update"),
        "cached_metals": cached_metals,
        "usdidr_rate": usdidr_rate,
        "html_cache_size_bytes": html_size,
        "tab_status": tab_status,
        "browser_active": browser_scraper is not None,
        "total_cached_metals": len([p for p in cached_metals.values() if p]),
        "active_tabs": sum(1 for s in tab_status.values() if s == "active"),
        "total_tabs": 4
    }

@app.get("/exchange-rate", tags=["Currency"])
async def get_exchange_rate():
    """Get current USDIDR exchange rate"""
    
    # Refresh USDIDR
    await refresh_prices_on_request(include_usdidr=True)
    
    with cache_lock:
        usdidr_data = price_cache.get("usdidr")
    
    if not usdidr_data or not usdidr_data.get("rate"):
        raise HTTPException(
            status_code=503,
            detail="USDIDR exchange rate tidak tersedia"
        )
    
    return {
        "currency_pair": "USDIDR",
        "rate": round(usdidr_data["rate"], 2),
        "source": usdidr_data.get("source", "TradingView"),
        "timestamp": price_cache.get("last_update", ""),
        "description": "1 USD = X IDR"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)