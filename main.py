"""
Metal Price Real-time API System dengan Dynamic Currency Conversion
- Multi-tab Selenium untuk metal prices
- Dynamic USDIDR tab (on-demand, auto-close)
- Konversi otomatis USD ke IDR
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import asyncio
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

# TradingView URLs
TRADINGVIEW_SYMBOLS = {
    "gold": {"symbol": "XAUUSD", "url": "https://www.tradingview.com/symbols/XAUUSD/", "name": "Gold"},
    "silver": {"symbol": "XAGUSD", "url": "https://www.tradingview.com/symbols/XAGUSD/", "name": "Silver"},
    "platinum": {"symbol": "XPTUSD", "url": "https://www.tradingview.com/symbols/XPTUSD/", "name": "Platinum"},
    "palladium": {"symbol": "XPDUSD", "url": "https://www.tradingview.com/symbols/XPDUSD/", "name": "Palladium"},
    "copper": {"symbol": "XCUUSD", "url": "https://www.tradingview.com/symbols/XCUUSD/", "name": "Copper"}
}

USDIDR_URL = "https://www.tradingview.com/symbols/USDIDR/"

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

class MetalPriceWithGram(BaseModel):
    metal: str
    gram: float
    price_per_troy_ounce_usd: float
    price_per_gram_usd: float
    total_price_usd: float
    price_per_gram_idr: Optional[float] = None
    total_price_idr: Optional[float] = None
    currency: str
    exchange_rate: Optional[float] = None
    timestamp: str
    source: str = "TradingView"
    conversion_info: dict

# Global state
price_cache: Dict = {
    "gold": None,
    "silver": None,
    "platinum": None,
    "palladium": None,
    "copper": None,
    "last_update": None,
    "html_cache": {},
    "tab_status": {},
    "usdidr_rate": None,
    "usdidr_last_update": None
}

thread_pool = ThreadPoolExecutor(max_workers=5)
cache_lock = threading.RLock()

class MultiTabBrowserScraper:
    """Multi-tab browser scraper dengan dynamic USDIDR tab"""
    
    def __init__(self):
        self.driver = None
        self.tabs = {}  # metal -> tab handle mapping
        self.usdidr_tab = None  # Tab khusus untuk USDIDR
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
        """Initialize browser dengan 5 tab untuk metals"""
        logger.info("=" * 60)
        logger.info("Initializing Multi-Tab Browser Scraper...")
        logger.info("=" * 60)
        
        try:
            chrome_options = self._create_chrome_options()
            self.driver = webdriver.Chrome(options=chrome_options)
            logger.info("✓ Browser initialized")
            
            self.driver.set_page_load_timeout(30)
            self.driver.implicitly_wait(5)
            
            metals = list(TRADINGVIEW_SYMBOLS.keys())
            
            for idx, metal in enumerate(metals):
                try:
                    if idx == 0:
                        self.tabs[metal] = self.driver.current_window_handle
                        logger.info(f"Using existing tab for {metal.upper()}")
                    else:
                        self.driver.execute_script("window.open('');")
                        self.driver.switch_to.window(self.driver.window_handles[-1])
                        self.tabs[metal] = self.driver.current_window_handle
                        logger.info(f"Created new tab {idx} for {metal.upper()}")
                    
                    metal_data = TRADINGVIEW_SYMBOLS[metal]
                    try:
                        self.driver.get(metal_data['url'])
                        with cache_lock:
                            price_cache["tab_status"][metal] = "active"
                        logger.info(f"✓ Loaded {metal.upper()}: {metal_data['url']}")
                    except Exception as e:
                        logger.error(f"Error loading {metal.upper()}: {e}")
                        with cache_lock:
                            price_cache["tab_status"][metal] = "error"
                    
                    time.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Error creating/loading tab for {metal}: {e}")
                    with cache_lock:
                        price_cache["tab_status"][metal] = "error"
            
            logger.info("=" * 60)
            logger.info(f"✓ Browser initialization complete")
            logger.info("=" * 60)
            
            return True
            
        except Exception as e:
            logger.error(f"Error initializing multi-tab browser: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def open_usdidr_tab(self) -> bool:
        """Buka tab USDIDR secara dinamis"""
        logger.info("=" * 60)
        logger.info("Opening USDIDR tab dynamically...")
        logger.info("=" * 60)
        
        try:
            with self.lock:
                # Cek apakah tab sudah ada
                if self.usdidr_tab:
                    try:
                        self.driver.switch_to.window(self.usdidr_tab)
                        self.driver.execute_script("return true;")
                        logger.info("✓ USDIDR tab already exists and active")
                        return True
                    except:
                        logger.info("USDIDR tab exists but not responsive, recreating...")
                        self.usdidr_tab = None
                
                # Buat tab baru
                self.driver.execute_script("window.open('');")
                all_handles = self.driver.window_handles
                self.usdidr_tab = all_handles[-1]
                self.driver.switch_to.window(self.usdidr_tab)
                logger.info(f"✓ Created new USDIDR tab (total tabs: {len(all_handles)})")
                
                # Load URL
                logger.info(f"Loading USDIDR URL: {USDIDR_URL}")
                self.driver.get(USDIDR_URL)
                
                # Wait untuk element muncul dengan timeout lebih lama
                logger.info("Waiting for USDIDR page to load completely...")
                wait = WebDriverWait(self.driver, 20)
                
                # Wait untuk price element
                price_element = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']")),
                    message="USDIDR price element not found"
                )
                logger.info("✓ USDIDR price element found")
                
                # Wait untuk text muncul
                wait.until(
                    lambda d: len(d.find_element(By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']").text.strip()) > 0,
                    message="USDIDR price text not loaded"
                )
                
                # Extra wait untuk memastikan JavaScript selesai render
                time.sleep(2)
                
                logger.info("✓ USDIDR page loaded completely")
                logger.info("=" * 60)
                
                return True
                
        except TimeoutException as e:
            logger.error(f"Timeout loading USDIDR tab: {e}")
            self.usdidr_tab = None
            return False
        except Exception as e:
            logger.error(f"Error opening USDIDR tab: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self.usdidr_tab = None
            return False
    
    def close_usdidr_tab(self):
        """Tutup tab USDIDR untuk optimasi memory"""
        logger.info("Closing USDIDR tab...")
        
        try:
            with self.lock:
                if self.usdidr_tab:
                    try:
                        self.driver.switch_to.window(self.usdidr_tab)
                        self.driver.close()
                        self.usdidr_tab = None
                        logger.info("✓ USDIDR tab closed successfully")
                        
                        # Switch ke tab pertama (metal tab)
                        if self.tabs:
                            first_metal = list(self.tabs.keys())[0]
                            self.driver.switch_to.window(self.tabs[first_metal])
                            logger.info(f"Switched back to {first_metal} tab")
                    except Exception as e:
                        logger.warning(f"Error closing USDIDR tab: {e}")
                        self.usdidr_tab = None
                else:
                    logger.info("USDIDR tab already closed or not exists")
        except Exception as e:
            logger.error(f"Unexpected error closing USDIDR tab: {e}")
    
    def scrape_usdidr_rate(self) -> Optional[float]:
        """Scrape exchange rate USDIDR dari tab yang sudah dibuka"""
        logger.info("Scraping USDIDR exchange rate...")
        
        try:
            with self.lock:
                if not self.usdidr_tab:
                    logger.error("USDIDR tab not opened")
                    return None
                
                # Switch ke USDIDR tab
                self.driver.switch_to.window(self.usdidr_tab)
                
                # Refresh untuk data terbaru
                logger.info("Refreshing USDIDR page...")
                self.driver.refresh()
                
                # Wait untuk reload
                wait = WebDriverWait(self.driver, 15)
                wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']"))
                )
                
                # Extra wait untuk JavaScript rendering
                time.sleep(1.5)
                
                # Get page source
                html = self.driver.page_source
                logger.info(f"USDIDR HTML extracted ({len(html)} bytes)")
                
                # Parse dengan BeautifulSoup
                soup = BeautifulSoup(html, 'lxml')
                symbol_last_value = soup.find('span', attrs={'data-qa-id': 'symbol-last-value'})
                
                if symbol_last_value:
                    text_content = symbol_last_value.get_text(strip=True)
                    logger.info(f"Raw USDIDR text: {text_content}")
                    
                    # Parse rate (format: 15,750.50 atau 15750.50)
                    rate_str = text_content.replace(',', '')
                    
                    try:
                        rate = float(rate_str)
                        
                        # Validasi range (USDIDR biasanya 14000-17000)
                        if 10000 < rate < 20000:
                            logger.info(f"✓ USDIDR Rate: {rate:,.2f}")
                            
                            # Simpan ke cache
                            with cache_lock:
                                price_cache["usdidr_rate"] = rate
                                price_cache["usdidr_last_update"] = datetime.utcnow().isoformat()
                            
                            return rate
                        else:
                            logger.warning(f"USDIDR rate {rate} outside valid range")
                    except ValueError as e:
                        logger.error(f"Could not parse USDIDR rate {rate_str}: {e}")
                else:
                    logger.warning("Could not find USDIDR price element")
                
                return None
                
        except TimeoutException as e:
            logger.error(f"Timeout scraping USDIDR: {e}")
            return None
        except Exception as e:
            logger.error(f"Error scraping USDIDR: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def get_usdidr_rate_with_auto_tab(self) -> Optional[float]:
        """Get USDIDR rate dengan auto open/close tab"""
        logger.info("=" * 60)
        logger.info("Getting USDIDR rate (auto tab management)...")
        logger.info("=" * 60)
        
        try:
            # Buka tab
            if not self.open_usdidr_tab():
                logger.error("Failed to open USDIDR tab")
                return None
            
            # Scrape rate
            rate = self.scrape_usdidr_rate()
            
            # Tutup tab (cleanup)
            self.close_usdidr_tab()
            
            logger.info("=" * 60)
            if rate:
                logger.info(f"✓ Successfully got USDIDR rate: {rate:,.2f}")
            else:
                logger.error("Failed to get USDIDR rate")
            logger.info("=" * 60)
            
            return rate
            
        except Exception as e:
            logger.error(f"Error in get_usdidr_rate_with_auto_tab: {e}")
            # Pastikan tab ditutup meskipun error
            try:
                self.close_usdidr_tab()
            except:
                pass
            return None
    
    def _check_tab_health(self, metal: str) -> bool:
        """Check apakah tab masih sehat"""
        try:
            if metal not in self.tabs:
                return False
            self.driver.switch_to.window(self.tabs[metal])
            self.driver.execute_script("return true;")
            return True
        except (WebDriverException, Exception):
            return False
    
    def _recover_tab(self, metal: str) -> bool:
        """Recover crashed tab"""
        logger.warning(f"Attempting to recover tab for {metal}...")
        
        try:
            with self.lock:
                if metal in self.tabs:
                    try:
                        self.driver.switch_to.window(self.tabs[metal])
                        self.driver.close()
                    except:
                        pass
                
                self.driver.execute_script("window.open('');")
                self.driver.switch_to.window(self.driver.window_handles[-1])
                self.tabs[metal] = self.driver.current_window_handle
                
                metal_data = TRADINGVIEW_SYMBOLS[metal]
                self.driver.get(metal_data['url'])
                
                wait = WebDriverWait(self.driver, 15)
                wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']"))
                )
                
                with cache_lock:
                    price_cache["tab_status"][metal] = "recovered"
                logger.info(f"✓ Tab recovered successfully for {metal}")
                return True
                
        except Exception as e:
            logger.error(f"Tab recovery failed for {metal}: {e}")
            return False
    
    def load_and_save_html(self, metal: str, refresh: bool = False) -> bool:
        """Load tab dan simpan HTML"""
        try:
            with self.lock:
                if not self._check_tab_health(metal):
                    if not self._recover_tab(metal):
                        return False
                
                self.driver.switch_to.window(self.tabs[metal])
                
                if refresh:
                    self.driver.refresh()
                
                wait = WebDriverWait(self.driver, 10)
                wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']"))
                )
                
                html = self.driver.page_source
                if html and len(html) > 1000:
                    with cache_lock:
                        price_cache["html_cache"][metal] = html
                        price_cache["tab_status"][metal] = "active"
                    logger.info(f"✓ HTML extracted for {metal.upper()}")
                    return True
                    
        except Exception as e:
            logger.error(f"Error loading HTML for {metal}: {e}")
            return False
    
    def refresh_all_tabs(self, refresh: bool = False):
        """Refresh semua metal tabs"""
        results = {}
        for metal in TRADINGVIEW_SYMBOLS.keys():
            try:
                success = self.load_and_save_html(metal, refresh=refresh)
                results[metal] = success
            except Exception as e:
                logger.error(f"Error processing {metal}: {e}")
                results[metal] = False
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
        except Exception as e:
            logger.error(f"Error cleaning profile: {e}")

# Global browser instance
browser_scraper: Optional[MultiTabBrowserScraper] = None

def extract_price_from_html(metal: str) -> Optional[float]:
    """Extract harga dari HTML cache"""
    try:
        with cache_lock:
            html = price_cache["html_cache"].get(metal)
        
        if not html:
            return None
        
        soup = BeautifulSoup(html, 'lxml')
        symbol_last_value = soup.find('span', attrs={'data-qa-id': 'symbol-last-value'})
        
        if symbol_last_value:
            text_content = symbol_last_value.get_text(strip=True)
            price_str = text_content.replace(',', '')
            
            if len(price_str) > 3 and '.' not in price_str:
                price_str = price_str[:-2] + '.' + price_str[-2:]
            
            try:
                price = float(price_str)
                if 1 < price < 10000:
                    logger.info(f"✓ Extracted {metal.upper()}: ${price}")
                    return price
            except ValueError:
                pass
        
        return None
        
    except Exception as e:
        logger.error(f"Error extracting price for {metal}: {e}")
        return None

def extract_all_prices_parallel() -> Dict[str, float]:
    """Extract harga parallel dengan thread pool"""
    prices_found = {}
    futures = {}
    
    for metal in TRADINGVIEW_SYMBOLS.keys():
        future = thread_pool.submit(extract_price_from_html, metal)
        futures[metal] = future
    
    for metal, future in futures.items():
        try:
            price = future.result(timeout=10)
            if price:
                prices_found[metal] = price
                with cache_lock:
                    price_cache[metal] = {"price": price, "source": "TradingView"}
        except Exception as e:
            logger.error(f"Error getting price for {metal}: {e}")
    
    return prices_found

async def refresh_prices_on_request():
    """Refresh prices saat request"""
    if not browser_scraper:
        return False
    
    browser_scraper.refresh_all_tabs(refresh=False)
    prices_found = extract_all_prices_parallel()
    
    with cache_lock:
        price_cache["last_update"] = datetime.utcnow().isoformat()
    
    return len(prices_found) > 0

async def lifespan(app: FastAPI):
    """Lifespan context manager"""
    global browser_scraper
    logger.info("Application starting up...")
    
    browser_scraper = MultiTabBrowserScraper()
    if not browser_scraper.initialize():
        raise Exception("Browser initialization failed")
    
    await refresh_prices_on_request()
    
    yield
    
    logger.info("Application shutting down...")
    if browser_scraper:
        browser_scraper.close()
    thread_pool.shutdown(wait=True)

app = FastAPI(
    title="Metal Price API with IDR Conversion",
    description="Real-time Metal Prices with Dynamic USDIDR Conversion",
    version="3.0.0",
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
        "name": "Metal Price API with IDR Conversion",
        "version": "3.0.0",
        "features": [
            "Multi-tab Selenium untuk metal prices",
            "Dynamic USDIDR tab (on-demand, auto-close)",
            "Konversi otomatis USD ke IDR",
            "Parallel extraction dengan thread pool",
            "Auto-recovery untuk crashed tabs"
        ],
        "endpoints": {
            "GET /prices/{metal}?gram={value}&currency=IDR": "Get metal price with optional IDR conversion",
            "GET /prices": "Get all metal prices",
            "GET /health": "Health check",
            "POST /refresh": "Manual refresh"
        }
    }

@app.get("/health", tags=["Health"])
async def health_check():
    with cache_lock:
        cached_count = len([p for p in price_cache if p in TRADINGVIEW_SYMBOLS and price_cache[p]])
        usdidr_rate = price_cache.get("usdidr_rate")
    
    return {
        "status": "healthy" if cached_count > 0 else "initializing",
        "cached_metals": cached_count,
        "usdidr_rate": usdidr_rate,
        "usdidr_cached": usdidr_rate is not None,
        "browser_active": browser_scraper is not None
    }

@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices():
    """Get all metal prices"""
    await refresh_prices_on_request()
    
    with cache_lock:
        if not price_cache.get("last_update"):
            raise HTTPException(status_code=503, detail="Data not available")
        
        prices = []
        for metal in TRADINGVIEW_SYMBOLS.keys():
            if price_cache.get(metal):
                prices.append(
                    MetalPrice(
                        metal=metal.upper(),
                        price_usd=price_cache[metal]["price"],
                        timestamp=price_cache["last_update"]
                    )
                )
    
    if not prices:
        raise HTTPException(status_code=503, detail="No data available")
    
    return MetalPriceResponse(
        status="success",
        data=prices,
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
    - **metal**: Jenis metal (gold, silver, platinum, palladium, copper)
    - **gram**: Berat dalam gram (wajib, > 0)
    - **currency**: Mata uang output (USD atau IDR, default: USD)
    
    Returns:
    - Harga per troy ounce dan per gram
    - Total harga untuk gram yang diminta
    - Jika currency=IDR: nilai dalam IDR dengan exchange rate terbaru
    """
    
    metal = metal.lower()
    currency = currency.upper()
    
    if metal not in TRADINGVIEW_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Metal tidak valid. Gunakan: {', '.join(TRADINGVIEW_SYMBOLS.keys())}"
        )
    
    if gram <= 0:
        raise HTTPException(status_code=400, detail="Gram harus > 0")
    
    # Refresh metal prices
    await refresh_prices_on_request()
    
    with cache_lock:
        if not price_cache.get(metal):
            raise HTTPException(status_code=503, detail=f"{metal.upper()} data tidak tersedia")
        
        price_per_troy_ounce = price_cache[metal]["price"]
    
    # Kalkulasi USD
    price_per_gram_usd = price_per_troy_ounce / TROY_OUNCE_TO_GRAM
    total_price_usd = price_per_gram_usd * gram
    
    # Response default (USD)
    response_data = {
        "metal": metal.upper(),
        "gram": gram,
        "price_per_troy_ounce_usd": round(price_per_troy_ounce, 2),
        "price_per_gram_usd": round(price_per_gram_usd, 4),
        "total_price_usd": round(total_price_usd, 2),
        "currency": "USD",
        "timestamp": price_cache.get("last_update", ""),
        "conversion_info": {
            "troy_ounce_to_gram": TROY_OUNCE_TO_GRAM,
            "calculation_usd": f"{gram}g × ${round(price_per_gram_usd, 4)}/g = ${round(total_price_usd, 2)}"
        }
    }
    
    # Jika request IDR, scrape USDIDR
    if currency == "IDR":
        logger.info("IDR conversion requested, fetching USDIDR rate...")
        
        if not browser_scraper:
            raise HTTPException(status_code=503, detail="Browser not available")
        
        # Scrape USDIDR dengan auto tab management
        exchange_rate = browser_scraper.get_usdidr_rate_with_auto_tab()
        
        if not exchange_rate:
            raise HTTPException(
                status_code=503, 
                detail="Gagal mendapatkan exchange rate USDIDR. Silakan coba lagi."
            )
        
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
            "calculation_idr": f"{gram}g × Rp{round(price_per_gram_idr, 2):,.0f}/g = Rp{round(total_price_idr, 2):,.0f}",
            "usdidr_timestamp": price_cache.get("usdidr_last_update", "")
        })
    
    return MetalPriceWithGram(**response_data)

@app.post("/refresh", tags=["Admin"])
async def manual_refresh():
    """Manual refresh all prices"""
    if not browser_scraper:
        raise HTTPException(status_code=503, detail="Browser not available")
    
    browser_scraper.refresh_all_tabs(refresh=True)
    prices_found = extract_all_prices_parallel()
    
    with cache_lock:
        price_cache["last_update"] = datetime.utcnow().isoformat()
    
    return {
        "status": "success",
        "message": "Manual refresh completed",
        "prices_found": len(prices_found),
        "last_update": price_cache.get("last_update")
    }

@app.get("/symbols", tags=["Info"])
async def get_symbols():
    return {
        "metals": {m: d['url'] for m, d in TRADINGVIEW_SYMBOLS.items()},
        "usdidr": USDIDR_URL,
        "note": "USDIDR tab opened dynamically on-demand"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)