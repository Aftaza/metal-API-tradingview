"""
Metal Price Real-time API System - TradingView Scraping
Scrap harga metal langsung dari TradingView tanpa API pihak ketiga
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import httpx
import json
import asyncio
from typing import Optional, Dict, List
import logging
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import re

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

# TradingView symbol mapping
TRADINGVIEW_SYMBOLS = {
    "gold": "COMEX:GC1!",        # Gold Futures
    "silver": "COMEX:SI1!",      # Silver Futures
    "platinum": "NYMEX:PL1!",    # Platinum Futures
    "palladium": "NYMEX:PA1!",   # Palladium Futures
    "copper": "COMEX:HG1!"       # Copper Futures
}

app = FastAPI(
    title="Metal Price API - TradingView",
    description="Real-time API untuk harga metal dari TradingView (Gold, Silver, Platinum, Palladium, Copper)",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def get_tradingview_price(symbol: str) -> Optional[float]:
    """
    Scrap harga dari TradingView menggunakan chart data endpoint
    Menggunakan reverse engineering dari TradingView web
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Set user agent untuk bypass anti-bot
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.tradingview.com/",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache"
            }
            
            # Method 1: Menggunakan symbol lookup API
            symbol_url = f"https://symbol-search.tradingview.com/symbol_search/?text={symbol}&type=futures&exchange=&lang=en"
            
            response = await client.get(symbol_url, headers=headers)
            response.raise_for_status()
            
            search_data = response.json()
            
            if not search_data or len(search_data) == 0:
                logger.warning(f"Symbol {symbol} tidak ditemukan di TradingView")
                return None
            
            # Ambil hasil pertama
            result = search_data[0]
            full_symbol = f"{result['exchange']}:{result['symbol']}"
            
            # Method 2: Scrap dari TradingView chart widget
            # Menggunakan endpoint yang digunakan oleh TradingView embed widget
            chart_url = "https://tradingview.com/chart"
            
            # Alternatif: Gunakan API endpoint untuk mendapatkan data chart
            api_url = f"https://www.tradingview.com/api/v1/quotes/?symbols={full_symbol}"
            
            response = await client.get(api_url, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            
            if "data" in data and len(data["data"]) > 0:
                price = data["data"][0].get("last_price")
                if price:
                    return float(price)
            
            return None
            
    except Exception as e:
        logger.error(f"Error scraping TradingView untuk {symbol}: {e}")
        return None

async def scrap_tradingview_direct() -> Optional[Dict]:
    """
    Scrap harga langsung menggunakan requests ke TradingView
    Menggunakan multiple methods untuk reliability
    """
    try:
        prices = {}
        
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1"
            }
            
            # Scrap menggunakan chart data endpoint
            for metal, symbol in TRADINGVIEW_SYMBOLS.items():
                try:
                    # Extract exchange dan symbol
                    parts = symbol.split(":")
                    exchange = parts[0]
                    sym = parts[1]
                    
                    # URL untuk mendapatkan quote data
                    url = f"https://www.tradingview.com/symbols/{exchange.lower()}-{sym.replace('!', '')}/data"
                    
                    response = await client.get(url, headers=headers)
                    
                    if response.status_code == 200:
                        # Coba extract harga dari HTML
                        html = response.text
                        
                        # Pattern untuk mencari harga (sesuaikan berdasarkan struktur HTML)
                        patterns = [
                            r'"last":\s*(\d+\.?\d*)',
                            r'"close":\s*(\d+\.?\d*)',
                            r'class="[^"]*price[^"]*"[^>]*>(\d+\.?\d*)',
                            r'data-value="(\d+\.?\d*)"'
                        ]
                        
                        price = None
                        for pattern in patterns:
                            match = re.search(pattern, html)
                            if match:
                                price = float(match.group(1))
                                break
                        
                        if price:
                            prices[metal] = price
                            logger.info(f"Scraped {metal}: ${price}")
                    
                    await asyncio.sleep(1)  # Rate limiting
                    
                except Exception as e:
                    logger.error(f"Error scraping {metal}: {e}")
                    continue
            
            if prices:
                return {
                    **prices,
                    "source": "TradingView"
                }
        
        return None
        
    except Exception as e:
        logger.error(f"Error dalam scrap_tradingview_direct: {e}")
        return None

async def scrap_alternative_source() -> Optional[Dict]:
    """
    Alternative scraping method menggunakan TradingView widget data
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            
            # Menggunakan TradingView chart page
            url = "https://www.tradingview.com/symbols/COMEX-GC1/"
            
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            
            html = response.text
            
            # Extract data dari script tags yang berisi chart data
            script_pattern = r'<script[^>]*>.*?"last":\s*(\d+\.?\d*).*?</script>'
            match = re.search(script_pattern, html, re.DOTALL)
            
            if match:
                logger.info("Alternative scraping method found data")
                # Process extracted data
                pass
            
            return None
            
    except Exception as e:
        logger.error(f"Error dalam alternative scraping: {e}")
        return None

async def update_prices():
    """Update harga dengan scraping dari TradingView"""
    global price_cache
    
    logger.info("Fetching metal prices from TradingView...")
    
    # Coba scrap langsung
    prices = await scrap_tradingview_direct()
    
    # Fallback ke alternative method
    if not prices:
        logger.info("Trying alternative scraping method...")
        prices = await scrap_alternative_source()
    
    if not prices:
        logger.error("All scraping methods failed")
        return
    
    # Update cache
    for metal in ["gold", "silver", "platinum", "palladium", "copper"]:
        if metal in prices:
            price_cache[metal] = {
                "price": prices[metal],
                "source": "TradingView"
            }
    
    price_cache["last_update"] = datetime.utcnow().isoformat()
    logger.info(f"Successfully updated {len(prices) - 1} metals")

# Background scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=update_prices,
    trigger="interval",
    seconds=300,  # Update setiap 5 menit
    name="metal_price_updater"
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

@app.on_event("startup")
async def startup_event():
    """Update prices pada startup"""
    await update_prices()

@app.get("/", tags=["Info"])
async def root():
    """Root endpoint"""
    return {
        "name": "Metal Price API - TradingView",
        "version": "1.0.0",
        "source": "TradingView Scraping",
        "endpoints": [
            "/prices - Get all metal prices",
            "/prices/{metal} - Get specific metal price",
            "/health - Health check"
        ]
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "last_update": price_cache.get("last_update"),
        "cached_metals": len([p for p in price_cache if p != "last_update" and price_cache[p] is not None])
    }

@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices():
    """Get semua harga metal terbaru dari TradingView"""
    
    if not price_cache.get("last_update"):
        raise HTTPException(status_code=503, detail="Data not available yet. Waiting for initial scrape...")
    
    metals = ["gold", "silver", "platinum", "palladium", "copper"]
    prices = []
    
    for metal in metals:
        if price_cache.get(metal):
            prices.append(
                MetalPrice(
                    metal=metal.upper(),
                    price_usd=price_cache[metal]["price"],
                    timestamp=price_cache["last_update"],
                    source=price_cache[metal]["source"]
                )
            )
    
    if not prices:
        raise HTTPException(status_code=503, detail="No metal data available")
    
    return MetalPriceResponse(
        status="success",
        data=prices,
        last_updated=price_cache.get("last_update", "")
    )

@app.get("/prices/{metal}", response_model=MetalPrice, tags=["Prices"])
async def get_metal_price(metal: str):
    """Get harga metal spesifik dari TradingView"""
    
    metal = metal.lower()
    valid_metals = ["gold", "silver", "platinum", "palladium", "copper"]
    
    if metal not in valid_metals:
        raise HTTPException(
            status_code=400,
            detail=f"Metal tidak valid. Gunakan: {', '.join(valid_metals)}"
        )
    
    if not price_cache.get(metal):
        raise HTTPException(status_code=503, detail=f"{metal} data tidak tersedia")
    
    return MetalPrice(
        metal=metal.upper(),
        price_usd=price_cache[metal]["price"],
        timestamp=price_cache.get("last_update", ""),
        source=price_cache[metal]["source"]
    )

@app.post("/refresh", tags=["Admin"])
async def manual_refresh(background_tasks: BackgroundTasks):
    """Manual refresh prices dari TradingView"""
    background_tasks.add_task(update_prices)
    return {"status": "Refresh in progress", "message": "Scraping data from TradingView..."}

@app.get("/symbols", tags=["Info"])
async def get_symbols():
    """Get list of TradingView symbols yang di-scrap"""
    return {
        "symbols": TRADINGVIEW_SYMBOLS,
        "description": "Semua harga di-scrap langsung dari TradingView futures contracts"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)