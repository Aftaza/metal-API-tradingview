"""
V2 Configuration — Shared constants and environment-based settings.
All scraping targets, Redis keys, and tuning parameters defined here.
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
SCRAPE_INTERVAL_SECONDS: int = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "3"))
SCRAPE_TIMEOUT_MS: int = int(os.getenv("SCRAPE_TIMEOUT_MS", "15000"))
RECOVERY_DELAY_SECONDS: int = int(os.getenv("RECOVERY_DELAY_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Conversion constant
# ---------------------------------------------------------------------------
TROY_OUNCE_TO_GRAM: float = 31.1034768

# ---------------------------------------------------------------------------
# Scraping targets  (4 workers)
# Each entry: redis_key, TradingView URL, human-readable name, type
# ---------------------------------------------------------------------------
SCRAPE_TARGETS: list[dict] = [
    {
        "key": "gold",
        "redis_key": "price:gold",
        "url": "https://www.tradingview.com/symbols/XAUUSD/",
        "name": "Gold (XAUUSD)",
        "type": "metal",
    },
    {
        "key": "silver",
        "redis_key": "price:silver",
        "url": "https://www.tradingview.com/symbols/XAGUSD/",
        "name": "Silver (XAGUSD)",
        "type": "metal",
    },
    {
        "key": "copper",
        "redis_key": "price:copper",
        "url": "https://www.tradingview.com/symbols/XCUUSD/",
        "name": "Copper (XCUUSD)",
        "type": "metal",
    },
    {
        "key": "usdidr",
        "redis_key": "price:usdidr",
        "url": "https://www.tradingview.com/symbols/USDIDR/",
        "name": "USD/IDR",
        "type": "currency",
    },
]

# Quick lookup helpers
METAL_TARGETS = [t for t in SCRAPE_TARGETS if t["type"] == "metal"]
METAL_KEYS = [t["key"] for t in METAL_TARGETS]
ALL_REDIS_KEYS = [t["redis_key"] for t in SCRAPE_TARGETS]
