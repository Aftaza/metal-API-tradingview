"""
Metal Price Real-time API System - Multi-Tab Selenium dengan Thread Pool
- Single browser instance dengan 5 tab untuk scraping paralel
- Update saat ada request, bukan scheduler
- Inisialisasi 5 tab di startup
- Ekstraksi data harga dengan thread pool
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import asyncio
from typing import Optional, Dict, List
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import atexit
from bs4 import BeautifulSoup
import threading
from queue import Queue

# Selenium imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Configuration
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Data models
class MetalPrice(BaseModel):
    metal: str
    price_usd: float
    currency: str = "USD"
    timestamp: str
    source: str = "TradingView"

class MetalPriceResponse(BaseModel):
    status: str
    data: List[MetalPrice]
    last_updated: str

# Global state
price_cache: Dict = {
    "gold": None,
    "silver": None,
    "platinum": None,
    "palladium": None,
    "copper": None,
    "last_update": None,
    "html_cache": {}
}

# Thread pool untuk ekstraksi data harga paralel
thread_pool = ThreadPoolExecutor(max_workers=5)

# Lock untuk thread-safe access
cache_lock = threading.RLock()

# TradingView symbol mapping
TRADINGVIEW_SYMBOLS = {
    "gold": {
        "symbol": "XAUUSD",
        "url": "https://www.tradingview.com/symbols/XAUUSD/",
        "name": "Gold"
    },
    "silver": {
        "symbol": "XAGUSD",
        "url": "https://www.tradingview.com/symbols/XAGUSD/",
        "name": "Silver"
    },
    "platinum": {
        "symbol": "XPTUSD",
        "url": "https://www.tradingview.com/symbols/XPTUSD/",
        "name": "Platinum"
    },
    "palladium": {
        "symbol": "XPDUSD",
        "url": "https://www.tradingview.com/symbols/XPDUSD/",
        "name": "Palladium"
    },
    "copper": {
        "symbol": "XCUUSD",
        "url": "https://www.tradingview.com/symbols/XCUUSD/",
        "name": "Copper"
    }
}

class MultiTabBrowserScraper:
    """Multi-tab browser scraper - reuse single browser instance"""
    
    def __init__(self):
        self.driver = None
        self.tabs = {}  # metal -> tab handle mapping
        self.lock = threading.RLock()
    
    def initialize(self):
        """Initialize browser dengan 5 tab"""
        logger.info("=" * 60)
        logger.info("Initializing Multi-Tab Browser Scraper...")
        logger.info("=" * 60)
        
        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-plugins-discovery")
            chrome_options.add_argument("--disable-plugins")
            chrome_options.add_argument("--disable-images")
            chrome_options.add_argument("--no-first-run")
            chrome_options.add_argument("--no-default-browser-check")
            chrome_options.add_argument("--disable-default-apps")
            chrome_options.add_argument("--disable-features=VizDisplayCompositor")
            chrome_options.add_argument("--no-zygote")
            chrome_options.add_argument("--disable-setuid-sandbox")
            chrome_options.add_argument("--disable-ipc-flooding-protection")
            chrome_options.add_argument("--disable-background-timer-throttling")
            chrome_options.add_argument("--disable-backgrounding-occluded-windows")
            chrome_options.add_argument("--disable-renderer-backgrounding")
            chrome_options.add_argument("--memory-pressure-off")
            chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            
            # Set profile directory
            import tempfile, uuid
            self.profile_dir = f"/tmp/chrome-profile-{uuid.uuid4()}"
            chrome_options.add_argument(f"--user-data-dir={self.profile_dir}")
            
            self.driver = webdriver.Chrome(options=chrome_options)
            logger.info("✓ Browser initialized")
            
            # Buat 5 tab dan load URLs
            metals = list(TRADINGVIEW_SYMBOLS.keys())
            
            for idx, metal in enumerate(metals):
                if idx == 0:
                    # Tab pertama (sudah terbuka)
                    self.tabs[metal] = self.driver.current_window_handle
                    logger.info(f"Using existing tab for {metal.upper()}")
                else:
                    # Buat tab baru
                    self.driver.execute_script("window.open('');")
                    self.driver.switch_to.window(self.driver.window_handles[-1])
                    self.tabs[metal] = self.driver.current_window_handle
                    logger.info(f"Created new tab {idx} for {metal.upper()}")
                
                # Load URL
                metal_data = TRADINGVIEW_SYMBOLS[metal]
                self.driver.get(metal_data['url'])
                logger.info(f"✓ Loaded {metal.upper()} tab: {metal_data['url']}")
            
            logger.info("=" * 60)
            logger.info(f"✓ All 5 tabs initialized successfully")
            logger.info("=" * 60)
            
            return True
            
        except Exception as e:
            logger.error(f"Error initializing multi-tab browser: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def load_and_save_html(self, metal: str, refresh: bool = False) -> bool:
        """Load tab, tunggu render, dan simpan HTML
        
        Args:
            metal: Metal symbol
            refresh: Jika True, refresh halaman dulu. Jika False, hanya ambil HTML saat ini
        """
        try:
            with self.lock:
                if metal not in self.tabs:
                    logger.error(f"Tab untuk {metal} tidak ditemukan")
                    return False
                
                # Switch ke tab metal ini
                self.driver.switch_to.window(self.tabs[metal])
                logger.debug(f"Switched to {metal} tab")
                
                # Refresh halaman jika diperlukan
                if refresh:
                    self.driver.refresh()
                    logger.debug(f"Refreshed {metal} tab")
                    
                    # Wait untuk element muncul dan render setelah refresh
                    try:
                        wait = WebDriverWait(self.driver, 12)
                        
                        # Wait element muncul
                        symbol_element = wait.until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']"))
                        )
                        logger.debug(f"Element found for {metal}")
                        
                        # Wait sampai ada text
                        wait.until(
                            lambda d: len(d.find_element(By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']").text.strip()) > 0
                        )
                        logger.debug(f"Price rendered for {metal}")
                        
                    except TimeoutException:
                        logger.error(f"Timeout loading price for {metal}")
                        return False
                else:
                    # Tanpa refresh, hanya tunggu element ada dan punya text
                    # Karena TradingView auto-update, nilai sudah ter-update di halaman
                    try:
                        wait = WebDriverWait(self.driver, 8)
                        
                        # Check element ada dan punya text
                        symbol_element = wait.until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']"))
                        )
                        
                        # Ensure ada text (biasanya instant karena sudah render sebelumnya)
                        wait.until(
                            lambda d: len(d.find_element(By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']").text.strip()) > 0
                        )
                        logger.debug(f"HTML ready for {metal} (no refresh needed)")
                        
                    except TimeoutException:
                        logger.warning(f"Element timeout for {metal}, falling back to current page")
                
                # Simpan HTML
                html = self.driver.page_source
                with cache_lock:
                    price_cache["html_cache"][metal] = html
                logger.info(f"✓ HTML extracted for {metal.upper()}")
                return True
                    
        except Exception as e:
            logger.error(f"Error loading HTML for {metal}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def refresh_all_tabs(self, refresh: bool = False):
        """Refresh semua 5 tab secara berurutan dan simpan HTML
        
        Args:
            refresh: Jika True, refresh halaman dulu. Jika False, hanya extract HTML
        """
        action = "Refreshing" if refresh else "Extracting"
        logger.info(f"{action} all tabs...")
        
        results = {}
        for metal in TRADINGVIEW_SYMBOLS.keys():
            success = self.load_and_save_html(metal, refresh=refresh)
            results[metal] = success
            # Minimal delay antar tab
            import time
            time.sleep(0.3)
        
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

def extract_price_from_html(metal: str) -> Optional[float]:
    """Extract harga dari HTML yang sudah disimpan (untuk thread pool)"""
    try:
        with cache_lock:
            html = price_cache["html_cache"].get(metal)
        
        if not html:
            logger.warning(f"No HTML cached for {metal}")
            return None
        
        soup = BeautifulSoup(html, 'lxml')
        symbol_last_value = soup.find('span', attrs={'data-qa-id': 'symbol-last-value'})
        
        if symbol_last_value:
            text_content = symbol_last_value.get_text(strip=True)
            logger.debug(f"Raw text for {metal}: {text_content}")
            
            # Parse harga
            price_str = text_content.replace(',', '')
            
            if len(price_str) > 3 and '.' not in price_str:
                price_str = price_str[:-2] + '.' + price_str[-2:]
            
            try:
                price = float(price_str)
                if 1 < price < 10000:
                    logger.info(f"✓ Extracted {metal.upper()}: ${price}")
                    return price
                else:
                    logger.warning(f"Price {price} outside valid range for {metal}")
            except ValueError as e:
                logger.error(f"Could not parse price {price_str}: {e}")
        else:
            logger.warning(f"Could not find price element for {metal}")
        
        return None
        
    except Exception as e:
        logger.error(f"Error extracting price for {metal}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def extract_all_prices_parallel() -> Dict[str, float]:
    """Extract harga dari semua 5 tab secara paralel dengan thread pool"""
    logger.info("Extracting prices from cached HTML (parallel with thread pool)...")
    
    prices_found = {}
    
    # Submit semua tasks ke thread pool
    futures = {}
    for metal in TRADINGVIEW_SYMBOLS.keys():
        future = thread_pool.submit(extract_price_from_html, metal)
        futures[metal] = future
    
    # Collect results
    for metal, future in futures.items():
        try:
            price = future.result(timeout=10)
            if price:
                prices_found[metal] = price
                with cache_lock:
                    price_cache[metal] = {
                        "price": price,
                        "source": "TradingView"
                    }
        except Exception as e:
            logger.error(f"Error getting price for {metal}: {e}")
    
    return prices_found

async def refresh_prices_on_request():
    """Refresh prices saat ada request masuk"""
    
    if not browser_scraper:
        logger.error("Browser scraper not initialized")
        return False
    
    logger.info("=" * 60)
    logger.info("Extracting prices (no refresh - TradingView auto-updates)...")
    logger.info("=" * 60)
    
    # Hanya extract HTML dari tab yang sudah running, tanpa refresh
    refresh_results = browser_scraper.refresh_all_tabs(refresh=False)
    
    success_count = sum(1 for s in refresh_results.values() if s)
    logger.info(f"Successfully extracted {success_count}/5 tabs")
    
    # Extract harga dari HTML secara paralel dengan thread pool
    prices_found = extract_all_prices_parallel()
    
    # Update timestamp
    with cache_lock:
        price_cache["last_update"] = datetime.utcnow().isoformat()
    
    logger.info("=" * 60)
    logger.info(f"Extraction complete. Got {len(prices_found)} prices: {', '.join([m.upper() for m in prices_found.keys()])}")
    logger.info(f"Last update: {price_cache['last_update']}")
    logger.info("=" * 60)
    
    return len(prices_found) > 0

async def manual_refresh_prices():
    """Manual refresh - refresh semua tab dulu baru extract"""
    
    if not browser_scraper:
        logger.error("Browser scraper not initialized")
        return False
    
    logger.info("=" * 60)
    logger.info("Manual refresh - refreshing all tabs...")
    logger.info("=" * 60)
    
    # Refresh semua tab
    refresh_results = browser_scraper.refresh_all_tabs(refresh=True)
    
    success_count = sum(1 for s in refresh_results.values() if s)
    logger.info(f"Successfully refreshed {success_count}/5 tabs")
    
    # Extract harga dari HTML secara paralel dengan thread pool
    prices_found = extract_all_prices_parallel()
    
    # Update timestamp
    with cache_lock:
        price_cache["last_update"] = datetime.utcnow().isoformat()
    
    logger.info("=" * 60)
    logger.info(f"Manual refresh complete. Got {len(prices_found)} prices")
    logger.info(f"Last update: {price_cache['last_update']}")
    logger.info("=" * 60)
    
    return len(prices_found) > 0

async def lifespan(app: FastAPI):
    """Lifespan context manager untuk startup dan shutdown"""
    
    # Startup
    global browser_scraper
    logger.info("Application starting up...")
    
    browser_scraper = MultiTabBrowserScraper()
    if not browser_scraper.initialize():
        logger.error("Failed to initialize browser scraper")
        raise Exception("Browser initialization failed")
    
    # Initial refresh
    await refresh_prices_on_request()
    logger.info("Initial price update completed")
    
    yield
    
    # Shutdown
    logger.info("Application shutting down...")
    if browser_scraper:
        browser_scraper.close()
    thread_pool.shutdown(wait=True)
    logger.info("Shutdown completed")

app = FastAPI(
    title="Metal Price API - Multi-Tab TradingView",
    description="Real-time API dengan multi-tab browser (Gold, Silver, Platinum, Palladium, Copper)",
    version="2.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", tags=["Info"])
async def root():
    """Root endpoint"""
    return {
        "name": "Metal Price API - Multi-Tab TradingView",
        "version": "2.0.0",
        "source": "TradingView Multi-Tab Scraping (Selenium)",
        "features": [
            "Single browser instance dengan 5 tab",
            "Refresh saat ada request",
            "Parallel price extraction dengan thread pool",
            "Optimized untuk performance"
        ],
        "endpoints": {
            "GET /": "This endpoint",
            "GET /prices": "Get all metal prices (auto-refresh)",
            "GET /prices/{metal}": "Get specific metal price",
            "GET /health": "Health check",
            "GET /symbols": "Get list of symbols",
            "POST /refresh": "Manual price refresh"
        }
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    with cache_lock:
        cached_count = len([p for p in price_cache if p != "last_update" and p != "html_cache" and price_cache[p] is not None])
    
    return {
        "status": "healthy" if cached_count > 0 else "initializing",
        "last_update": price_cache.get("last_update"),
        "cached_metals": cached_count,
        "total_metals": len(TRADINGVIEW_SYMBOLS),
        "symbols": list(TRADINGVIEW_SYMBOLS.keys()),
        "browser_active": browser_scraper is not None
    }

@app.get("/metrics", tags=["Monitoring"])
async def metrics():
    """Metrics endpoint"""
    with cache_lock:
        cached_count = len([p for p in price_cache if p != "last_update" and p != "html_cache" and price_cache[p] is not None])
        last_update = price_cache.get("last_update")
    
    cache_age = None
    if last_update:
        try:
            last_update_dt = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
            cache_age = (datetime.utcnow().replace(tzinfo=last_update_dt.tzinfo) - last_update_dt).total_seconds()
        except:
            cache_age = None
    
    return {
        "up": True,
        "timestamp": datetime.utcnow().isoformat(),
        "cached_metals": cached_count,
        "total_metals": len(TRADINGVIEW_SYMBOLS),
        "cache_age_seconds": cache_age,
        "last_scrape_attempt": last_update,
        "browser_active": browser_scraper is not None,
        "thread_pool_workers": 5
    }

@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices():
    """Get semua harga metal - auto refresh saat request"""
    
    # Refresh data saat ada request
    await refresh_prices_on_request()
    
    with cache_lock:
        if not price_cache.get("last_update"):
            raise HTTPException(status_code=503, detail="Data not available yet...")
        
        metals = ["gold", "silver", "platinum", "palladium", "copper"]
        prices = []
        
        for metal in metals:
            if price_cache.get(metal):
                prices.append(
                    MetalPrice(
                        metal=metal.upper(),
                        price_usd=price_cache[metal]["price"],
                        timestamp=price_cache["last_update"],
                        source="TradingView"
                    )
                )
    
    if not prices:
        raise HTTPException(status_code=503, detail="No metal data available...")
    
    return MetalPriceResponse(
        status="success",
        data=prices,
        last_updated=price_cache.get("last_update", "")
    )

@app.get("/prices/{metal}", response_model=MetalPrice, tags=["Prices"])
async def get_metal_price(metal: str):
    """Get harga metal spesifik - auto refresh saat request"""
    
    metal = metal.lower()
    valid_metals = list(TRADINGVIEW_SYMBOLS.keys())
    
    if metal not in valid_metals:
        raise HTTPException(
            status_code=400,
            detail=f"Metal tidak valid. Gunakan: {', '.join(valid_metals)}"
        )
    
    # Refresh data saat ada request
    await refresh_prices_on_request()
    
    with cache_lock:
        if not price_cache.get(metal):
            raise HTTPException(status_code=503, detail=f"{metal.upper()} data tidak tersedia...")
        
        return MetalPrice(
            metal=metal.upper(),
            price_usd=price_cache[metal]["price"],
            timestamp=price_cache.get("last_update", ""),
            source="TradingView"
        )

@app.post("/refresh", tags=["Admin"])
async def manual_refresh():
    """Manual refresh prices - refresh semua tab"""
    success = await manual_refresh_prices()
    
    return {
        "status": "success" if success else "partial",
        "message": "Manual refresh: tabs refreshed and prices extracted",
        "last_update": price_cache.get("last_update"),
        "duration": "~12-15 seconds"
    }

@app.get("/symbols", tags=["Info"])
async def get_symbols():
    """Get list of symbols"""
    return {
        "symbols": {
            metal: data['url'] 
            for metal, data in TRADINGVIEW_SYMBOLS.items()
        },
        "description": "Metal symbols dari TradingView",
        "scraping_method": "Multi-Tab Selenium + Thread Pool Extraction"
    }

@app.get("/debug/cache", tags=["Debug"])
async def debug_cache():
    """Debug cache status"""
    with cache_lock:
        return {
            "last_update": price_cache.get("last_update"),
            "cached_metals": {
                metal: price_cache.get(metal, {}).get("price") 
                for metal in TRADINGVIEW_SYMBOLS.keys()
            },
            "html_cached_count": len(price_cache.get("html_cache", {})),
            "browser_active": browser_scraper is not None
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)