"""
Metal Price Real-time API System - TradingView Scraping dengan Selenium
Menggunakan headless browser untuk wait harga di-render oleh JavaScript
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import asyncio
from typing import Optional, Dict, List
import logging
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import re
from bs4 import BeautifulSoup

# Selenium imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
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

# Global cache
price_cache: Dict = {
    "gold": None,
    "silver": None,
    "platinum": None,
    "palladium": None,
    "copper": None,
    "last_update": None
}

# TradingView symbol mapping - menggunakan URL yang benar (UPPERCASE)
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
        "symbol": "XCULSD",
        "url": "https://www.tradingview.com/symbols/XCUUSD/",
        "name": "Copper"
    }
}

def get_chrome_driver():
    """Initialize Selenium Chrome WebDriver dengan headless mode"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # Headless mode (tanpa GUI)
    chrome_options.add_argument("--no-sandbox")  # Docker compatibility
    chrome_options.add_argument("--disable-dev-shm-usage")  # Reduce memory usage
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-plugins-discovery")
    chrome_options.add_argument("--disable-plugins")
    chrome_options.add_argument("--incognito")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-zygote")
    chrome_options.add_argument("--single-process")  # Important for Docker
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    
    # Set a specific temp directory for Chrome to avoid permission issues
    import tempfile
    temp_dir = tempfile.gettempdir()
    chrome_options.add_argument(f"--temp-directory={temp_dir}")
    
    # Disable extensions and maximize window
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-plugins")
    chrome_options.add_argument("--disable-images")
    chrome_options.add_argument("--disable-javascript")  # We'll enable when needed
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-features=VizDisplayCompositor")
    
    try:
        # Add a unique profile directory for each run to avoid conflicts
        import uuid
        profile_dir = f"/tmp/chrome-profile-{uuid.uuid4()}"
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")
        
        driver = webdriver.Chrome(options=chrome_options)
        return driver
    except Exception as e:
        logger.error(f"Error initializing Chrome driver: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def scrape_metal_price_selenium(metal: str, metal_data: Dict) -> Optional[float]:
    """
    Scrap harga metal dari TradingView menggunakan Selenium
    Wait untuk JavaScript render price selama 3-8 detik
    """
    driver = None
    profile_dir = None
    try:
        logger.info(f"Scraping {metal_data['name']} from {metal_data['url']} (Selenium)")
        
        # Generate unique profile directory
        import uuid
        profile_dir = f"/tmp/chrome-profile-{uuid.uuid4()}"
        
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-plugins-discovery")
        chrome_options.add_argument("--disable-plugins")
        chrome_options.add_argument("--disable-images")
        chrome_options.add_argument("--disable-javascript")  # We'll enable when needed
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")
        chrome_options.add_argument("--disable-default-apps")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-features=VizDisplayCompositor")
        chrome_options.add_argument("--no-zygote")
        chrome_options.add_argument("--disable-setuid-sandbox")
        chrome_options.add_argument("--disable-ipc-flooding-protection")
        chrome_options.add_argument("--disable-background-timer-throttling")
        chrome_options.add_argument("--disable-backgrounding-occluded-windows")
        chrome_options.add_argument("--disable-renderer-backgrounding")
        chrome_options.add_argument("--disable-features=TranslateUI")
        chrome_options.add_argument("--disable-ipc-flooding-protection")
        chrome_options.add_argument("--memory-pressure-off")
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        chrome_options.add_argument("--user-data-dir={}".format(profile_dir))
        
        # Extra memory options for Docker
        chrome_options.add_argument("--max_old_space_size=4096")
        
        driver = webdriver.Chrome(options=chrome_options)
        
        # Load halaman
        driver.get(metal_data['url'])
        logger.debug(f"Page loaded for {metal}")
        
        # Wait untuk span dengan data-qa-id="symbol-last-value" muncul
        # Max wait 15 detik to allow for JavaScript rendering
        try:
            wait = WebDriverWait(driver, 15)
            
            # Wait element untuk visible
            symbol_element = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']"))
            )
            logger.debug(f"Element found for {metal}, waiting for content...")
            
            # Wait sampai element memiliki text (price loaded)
            wait.until(
                lambda d: len(d.find_element(By.CSS_SELECTOR, "span[data-qa-id='symbol-last-value']").text.strip()) > 0
            )
            
            logger.debug(f"Price loaded for {metal}")
            
            # Get HTML setelah JavaScript render
            html = driver.page_source
            soup = BeautifulSoup(html, 'lxml')
            
            # Extract harga
            symbol_last_value = soup.find('span', attrs={'data-qa-id': 'symbol-last-value'})
            
            if symbol_last_value:
                text_content = symbol_last_value.get_text(strip=True)
                logger.debug(f"Raw text content for {metal}: {text_content}")
                
                # Remove komma
                price_str = text_content.replace(',', '')
                
                # Handle case dimana desimal terpisah
                if len(price_str) > 3 and '.' not in price_str:
                    price_str = price_str[:-2] + '.' + price_str[-2:]
                    logger.debug(f"After adding decimal: {price_str}")
                
                try:
                    price = float(price_str)
                    if 1 < price < 10000:  # Sanity check
                        logger.info(f"✓ {metal.upper()}: ${price}")
                        return price
                    else:
                        logger.warning(f"Price {price} outside valid range for {metal}")
                except ValueError as e:
                    logger.error(f"Could not parse price {price_str}: {e}")
            else:
                logger.warning(f"Could not find symbol-last-value span for {metal}")
            
        except TimeoutException:
            logger.error(f"Timeout waiting for price element for {metal}")
        
        return None
        
    except Exception as e:
        logger.error(f"Error scraping {metal} with Selenium: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None
    
    finally:
        # Always close driver
        if driver:
            try:
                driver.quit()
                logger.debug(f"Browser closed for {metal}")
            except Exception as e:
                logger.error(f"Error closing browser for {metal}: {e}")
        
        # Clean up profile directory
        if profile_dir:
            try:
                import shutil
                import os
                if os.path.exists(profile_dir):
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    logger.debug(f"Cleaned up profile directory: {profile_dir}")
            except Exception as e:
                logger.error(f"Error cleaning up profile directory for {metal}: {e}")

async def update_prices():
    """Update harga dengan scraping dari TradingView menggunakan Selenium"""
    global price_cache
    
    logger.info("=" * 60)
    logger.info("Starting price update from TradingView (Selenium)...")
    logger.info("=" * 60)
    
    prices_found = {}
    
    # Scrap setiap metal secara sequential
    for metal, metal_data in TRADINGVIEW_SYMBOLS.items():
        try:
            # Retry mechanism for robustness
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    price = await scrape_metal_price_selenium(metal, metal_data)
                    
                    if price:
                        prices_found[metal] = price
                        price_cache[metal] = {
                            "price": price,
                            "source": "TradingView"
                        }
                        logger.info(f"✓ Cached {metal.upper()}: ${price}")
                        break  # Success, exit retry loop
                    elif attempt < max_retries:
                        logger.warning(f"Attempt {attempt + 1} failed for {metal}, retrying...")
                        await asyncio.sleep(3)  # Wait before retry
                    else:
                        logger.warning(f"✗ Failed to get price for {metal.upper()} after {max_retries + 1} attempts")
                        
                except Exception as scrape_error:
                    logger.error(f"Scraping error for {metal} on attempt {attempt + 1}: {scrape_error}")
                    if attempt >= max_retries:
                        logger.error(f"✗ All attempts failed for {metal.upper()}")
                        break
                    await asyncio.sleep(3)  # Wait before retry
            
            # Rate limiting - tunggu 2 detik antar request
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Exception while scraping {metal}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            continue
    
    # Update timestamp
    price_cache["last_update"] = datetime.utcnow().isoformat()
    
    logger.info("=" * 60)
    logger.info(f"Update complete. Got prices for: {', '.join([m.upper() for m in prices_found.keys()])}")
    logger.info(f"Last update: {price_cache['last_update']}")
    logger.info("=" * 60)
    
    if not prices_found:
        logger.error("WARNING: No prices were successfully scraped!")
    
    return len(prices_found) > 0

# Background scheduler - Increase interval to reduce Chrome load
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=update_prices,
    trigger="interval",
    seconds=1200,  # Update setiap 20 menit to be gentler on Chrome
    name="metal_price_updater",
    id="updater_job"
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

async def lifespan(app: FastAPI):
    """
    Lifespan context manager untuk startup dan shutdown events
    """
    # Startup
    logger.info("Application starting up...")
    await update_prices()
    logger.info("Initial price update completed")
    
    yield
    
    # Shutdown
    logger.info("Application shutting down...")
    scheduler.shutdown()
    logger.info("Scheduler shutdown completed")

app = FastAPI(
    title="Metal Price API - TradingView",
    description="Real-time API untuk harga metal dari TradingView (Gold, Silver, Platinum, Palladium, Copper)",
    version="1.0.0",
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
        "name": "Metal Price API - TradingView",
        "version": "1.0.0",
        "source": "TradingView Direct Scraping (Selenium)",
        "endpoints": {
            "GET /": "This endpoint",
            "GET /prices": "Get all metal prices",
            "GET /prices/{metal}": "Get specific metal price (gold, silver, platinum, palladium, copper)",
            "GET /health": "Health check",
            "GET /symbols": "Get list of symbols",
            "POST /refresh": "Manual price refresh"
        }
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    cached_count = len([p for p in price_cache if p != "last_update" and price_cache[p] is not None])
    
    return {
        "status": "healthy" if cached_count > 0 else "waiting",
        "last_update": price_cache.get("last_update"),
        "cached_metals": cached_count,
        "total_metals": len(TRADINGVIEW_SYMBOLS),
        "symbols": list(TRADINGVIEW_SYMBOLS.keys())
    }

@app.get("/metrics", tags=["Monitoring"])
async def metrics():
    """Metrics endpoint for monitoring"""
    cached_count = len([p for p in price_cache if p != "last_update" and price_cache[p] is not None])
    
    # Calculate cache age
    last_update = price_cache.get("last_update")
    cache_age = None
    if last_update:
        try:
            from datetime import datetime
            last_update_dt = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
            cache_age = (datetime.now(last_update_dt.tzinfo) - last_update_dt).total_seconds()
        except:
            cache_age = None
    
    return {
        "up": True,
        "timestamp": datetime.utcnow().isoformat(),
        "cached_metals": cached_count,
        "total_metals": len(TRADINGVIEW_SYMBOLS),
        "cache_age_seconds": cache_age,
        "last_scrape_attempt": price_cache.get("last_update"),
        "scraping_status": "active" if scheduler.running else "inactive"
    }

@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices():
    """Get semua harga metal terbaru dari TradingView"""
    
    if not price_cache.get("last_update"):
        raise HTTPException(
            status_code=503, 
            detail="Data not available yet. Please wait for initial scrape (60-90 seconds)..."
        )
    
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
        raise HTTPException(
            status_code=503, 
            detail="No metal data available. Scraping in progress..."
        )
    
    return MetalPriceResponse(
        status="success",
        data=prices,
        last_updated=price_cache.get("last_update", "")
    )

@app.get("/prices/{metal}", response_model=MetalPrice, tags=["Prices"])
async def get_metal_price(metal: str):
    """Get harga metal spesifik dari TradingView"""
    
    metal = metal.lower()
    valid_metals = list(TRADINGVIEW_SYMBOLS.keys())
    
    if metal not in valid_metals:
        raise HTTPException(
            status_code=400,
            detail=f"Metal tidak valid. Gunakan: {', '.join(valid_metals)}"
        )
    
    if not price_cache.get(metal):
        raise HTTPException(
            status_code=503, 
            detail=f"{metal.upper()} data tidak tersedia. Tunggu scraping selesai..."
        )
    
    return MetalPrice(
        metal=metal.upper(),
        price_usd=price_cache[metal]["price"],
        timestamp=price_cache.get("last_update", ""),
        source="TradingView"
    )

@app.post("/refresh", tags=["Admin"])
async def manual_refresh(background_tasks: BackgroundTasks):
    """Manual refresh prices dari TradingView"""
    background_tasks.add_task(update_prices)
    return {
        "status": "refresh_initiated",
        "message": "Scraping data from TradingView with Selenium...",
        "estimated_duration": "60-90 seconds"
    }

@app.get("/symbols", tags=["Info"])
async def get_symbols():
    """Get list of symbols yang di-scrap"""
    return {
        "symbols": {
            metal: data['url'] 
            for metal, data in TRADINGVIEW_SYMBOLS.items()
        },
        "description": "Forex symbols dari TradingView dengan format USD",
        "scraping_method": "Selenium WebDriver (waits for JavaScript rendering)"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)