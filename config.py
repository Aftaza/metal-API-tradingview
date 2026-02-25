"""
V2-Kitco Configuration — Shared constants and environment-based settings.
Scraping targets use Kitco.com for metals and TradingView for USDIDR.
Each scraper container runs a single target via SCRAPE_TARGET env var.
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
SCRAPE_INTERVAL_SECONDS: int = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "10"))
SCRAPE_TIMEOUT_MS: int = int(os.getenv("SCRAPE_TIMEOUT_MS", "30000"))
RECOVERY_DELAY_SECONDS: int = int(os.getenv("RECOVERY_DELAY_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Conversion constants
# ---------------------------------------------------------------------------
TROY_OUNCE_TO_GRAM: float = 31.1034768
POUND_TO_GRAM: float = 453.59237

# ---------------------------------------------------------------------------
# Scraping targets (4 workers — each running in its own container)
#
# source    : "kitco" or "tradingview"
# selector  : CSS selector for the price element (Playwright format)
# unit      : "troy_ounce" (Gold/Silver) | "pound" (Copper) | "currency" (USDIDR)
#
# NOTE: Kitco page structure (as of Feb 2026):
#   <h2>Live gold Price</h2>
#   <h3>2,935.40</h3>   ← this is the price we extract
# The primary selector field is for logging; actual extraction uses
# fallback lists in scraper_daemon.py.
# ---------------------------------------------------------------------------
SCRAPE_TARGETS: list[dict] = [
    {
        "key": "gold",
        "redis_key": "price:gold",
        "url": "https://www.kitco.com/charts/gold",
        "name": "Gold (Kitco)",
        "type": "metal",
        "source": "kitco",
        "selector": "xpath=//h2[contains(text(),'Live')][contains(text(),'Price')]/following-sibling::h3[1]",
        "unit": "troy_ounce",
    },
    {
        "key": "silver",
        "redis_key": "price:silver",
        "url": "https://www.kitco.com/charts/silver",
        "name": "Silver (Kitco)",
        "type": "metal",
        "source": "kitco",
        "selector": "xpath=//h2[contains(text(),'Live')][contains(text(),'Price')]/following-sibling::h3[1]",
        "unit": "troy_ounce",
    },
    {
        "key": "copper",
        "redis_key": "price:copper",
        "url": "https://www.kitco.com/price/base-metals/copper",
        "name": "Copper (Kitco)",
        "type": "metal",
        "source": "kitco",
        "selector": "xpath=//h2[contains(text(),'Live')][contains(text(),'Price')]/following-sibling::h3[1]",
        "unit": "pound",
    },
    {
        "key": "usdidr",
        "redis_key": "price:usdidr",
        "url": "https://www.tradingview.com/symbols/USDIDR/",
        "name": "USD/IDR (TradingView)",
        "type": "currency",
        "source": "tradingview",
        "selector": "span.last-zoF9r75I",
        "unit": "currency",
    },
]

# ---------------------------------------------------------------------------
# Single-target mode — each container scrapes only one target
# Set SCRAPE_TARGET env var to the key (gold, silver, copper, usdidr)
# ---------------------------------------------------------------------------
SCRAPE_TARGET: str | None = os.getenv("SCRAPE_TARGET", None)

def get_active_target() -> dict | None:
    """Return the single target this container should scrape, or None."""
    if SCRAPE_TARGET is None:
        return None
    for t in SCRAPE_TARGETS:
        if t["key"] == SCRAPE_TARGET:
            return t
    return None

# Quick lookup helpers
METAL_TARGETS = [t for t in SCRAPE_TARGETS if t["type"] == "metal"]
METAL_KEYS = [t["key"] for t in METAL_TARGETS]
ALL_REDIS_KEYS = [t["redis_key"] for t in SCRAPE_TARGETS]
