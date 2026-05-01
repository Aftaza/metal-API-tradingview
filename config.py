"""
Metal Price Scraper v3 — Configuration
========================================
Architecture: httpx + BeautifulSoup SSR JSON extraction
No headless browser required.

Sources:
  - Gold, Silver, Copper → Kitco.com (Next.js SSR → __NEXT_DATA__)
  - USDIDR               → TradingView (SSR inline JSON state)
"""

import os
import logging

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

# ---------------------------------------------------------------------------
# Scraping tuning
# ---------------------------------------------------------------------------
# Base interval; actual jitter is ±SCRAPE_JITTER_FACTOR * interval
SCRAPE_INTERVAL_SECONDS: int = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "30"))
SCRAPE_JITTER_FACTOR: float = float(os.getenv("SCRAPE_JITTER_FACTOR", "0.3"))

# HTTP request timeout (seconds)
HTTP_TIMEOUT_SECONDS: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))

# How many consecutive failures before a target logs an error-level alert
RECOVERY_DELAY_SECONDS: int = int(os.getenv("RECOVERY_DELAY_SECONDS", "5"))
MAX_CONSECUTIVE_FAILURES: int = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))

# Maximum age before data is treated as stale (seconds) — used by the API
DATA_STALENESS_SECONDS: int = int(os.getenv("DATA_STALENESS_SECONDS", "300"))  # 5 min

# ---------------------------------------------------------------------------
# Conversion constants
# ---------------------------------------------------------------------------
TROY_OUNCE_TO_GRAM: float = 31.1034768
POUND_TO_GRAM: float = 453.59237

# ---------------------------------------------------------------------------
# Scraping targets
#
# source           : "kitco" or "tradingview"
# kitco_symbol     : Kitco metal symbol for __NEXT_DATA__ extraction (AU/AG/CU)
# url              : Page URL to fetch
# type             : "metal" or "currency"
# unit             : "troy_ounce" | "pound" | "currency"
# price_range      : (min, max) — sanity check
# ---------------------------------------------------------------------------
SCRAPE_TARGETS: list[dict] = [
    {
        "key": "gold",
        "redis_key": "price:gold",
        "name": "Gold (Kitco)",
        "url": "https://www.kitco.com/charts/gold",
        "type": "metal",
        "source": "kitco",
        "kitco_symbol": "AU",
        "unit": "troy_ounce",
        "price_range": (500.0, 10_000.0),
    },
    {
        "key": "silver",
        "redis_key": "price:silver",
        "name": "Silver (Kitco)",
        "url": "https://www.kitco.com/charts/silver",
        "type": "metal",
        "source": "kitco",
        "kitco_symbol": "AG",
        "unit": "troy_ounce",
        "price_range": (5.0, 500.0),
    },
    {
        "key": "copper",
        "redis_key": "price:copper",
        "name": "Copper (Kitco)",
        "url": "https://www.kitco.com/price/base-metals/copper",
        "type": "metal",
        "source": "kitco",
        "kitco_symbol": "CU",
        "unit": "pound",
        "price_range": (1.0, 30.0),
    },
    {
        "key": "usdidr",
        "redis_key": "price:usdidr",
        "name": "USD/IDR (TradingView)",
        "url": "https://www.tradingview.com/symbols/USDIDR/",
        "type": "currency",
        "source": "tradingview",
        "kitco_symbol": None,
        "unit": "currency",
        "price_range": (10_000.0, 25_000.0),
    },
]

# ---------------------------------------------------------------------------
# Derived helpers
# ---------------------------------------------------------------------------
METAL_TARGETS: list[dict] = [t for t in SCRAPE_TARGETS if t["type"] == "metal"]
METAL_KEYS: list[str] = [t["key"] for t in METAL_TARGETS]
ALL_REDIS_KEYS: list[str] = [t["redis_key"] for t in SCRAPE_TARGETS]

# Single-target mode — set SCRAPE_TARGET env var to a key (gold, silver, ...)
SCRAPE_TARGET: str | None = os.getenv("SCRAPE_TARGET", None)


def get_active_target() -> dict | None:
    """Return the single target this container should scrape, or None."""
    if SCRAPE_TARGET is None:
        return None
    for t in SCRAPE_TARGETS:
        if t["key"] == SCRAPE_TARGET:
            return t
    return None
