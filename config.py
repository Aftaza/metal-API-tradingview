"""
Metal Price API — Shared Configuration
=======================================
All constants, environment variables, and scraping targets defined here.
Import this module in api.py and scraper_daemon.py.

Environment Variables:
    REDIS_URL                  Redis connection string (default: redis://redis:6379/0)
    LOG_LEVEL                  Logging verbosity (default: INFO)
    SCRAPE_INTERVAL_SECONDS    Seconds between successful scrapes (default: 5)
    SCRAPE_TIMEOUT_MS          Playwright wait timeout in ms (default: 15000)
    RECOVERY_DELAY_SECONDS     Base delay on scraper error (default: 5)
    ALLOWED_ORIGINS            Comma-separated CORS origins (default: *)
"""

import os

# ---------------------------------------------------------------------------
# Logging — configured in setup_logging(), NOT at module level
# ---------------------------------------------------------------------------
import logging


def setup_logging(name: str = "root") -> logging.Logger:
    """
    Configure and return a named logger.
    Call this once at the entry-point of each service (api.py, scraper_daemon.py).
    Using a function avoids the side-effect problem of calling basicConfig at import time.
    """
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,  # Override any prior basicConfig calls
    )
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Redis TTL — keys expire after this many seconds if scraper stops writing.
# At SCRAPE_INTERVAL_SECONDS=5, 60s gives 12 missed cycles before data is
# considered stale and auto-evicted from Redis.
REDIS_KEY_TTL_SECONDS: int = int(os.getenv("REDIS_KEY_TTL_SECONDS", "60"))

# ---------------------------------------------------------------------------
# Scraping tuning
# ---------------------------------------------------------------------------
SCRAPE_INTERVAL_SECONDS: int = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "5"))
SCRAPE_TIMEOUT_MS: int = int(os.getenv("SCRAPE_TIMEOUT_MS", "15000"))
RECOVERY_DELAY_SECONDS: int = int(os.getenv("RECOVERY_DELAY_SECONDS", "5"))
MAX_BACKOFF_SECONDS: int = int(os.getenv("MAX_BACKOFF_SECONDS", "60"))

# Extra ms to wait after price element is visible before reading text.
# Allows TradingView JS to finish updating the DOM.
RENDER_SETTLE_MS: int = int(os.getenv("RENDER_SETTLE_MS", "600"))

# ---------------------------------------------------------------------------
# Conversion constant
# ---------------------------------------------------------------------------
TROY_OUNCE_TO_GRAM: float = 31.1034768

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS: list[str] = (
    ["*"] if _raw_origins == "*" else [o.strip() for o in _raw_origins.split(",") if o.strip()]
)

# ---------------------------------------------------------------------------
# Scraping targets  — 3 metals + USDIDR exchange rate
#
# Each entry:
#   key        short identifier used in Redis keys and API responses
#   redis_key  full Redis key string
#   url        TradingView symbol page URL
#   name       human-readable label (for logging)
#   type       "metal" | "currency"
# ---------------------------------------------------------------------------
SCRAPE_TARGETS: list[dict] = [
    {
        "key": "gold",
        "redis_key": "price:gold",
        "url": "https://www.tradingview.com/symbols/XAUUSD/",
        "name": "Gold (XAUUSD)",
        "type": "metal",
        # Valid price range in USD per troy ounce
        "min_value": 500.0,
        "max_value": 5_000.0,
    },
    {
        "key": "silver",
        "redis_key": "price:silver",
        "url": "https://www.tradingview.com/symbols/XAGUSD/",
        "name": "Silver (XAGUSD)",
        "type": "metal",
        "min_value": 5.0,
        "max_value": 500.0,
    },
    {
        "key": "copper",
        "redis_key": "price:copper",
        "url": "https://www.tradingview.com/symbols/XCUUSD/",
        "name": "Copper (XCUUSD)",
        "type": "metal",
        "min_value": 0.5,
        "max_value": 50.0,
    },
    {
        "key": "usdidr",
        "redis_key": "price:usdidr",
        "url": "https://www.tradingview.com/symbols/USDIDR/",
        "name": "USD/IDR",
        "type": "currency",
        "min_value": 10_000.0,
        "max_value": 25_000.0,
    },
]

# Quick lookup helpers (computed once at import time)
METAL_TARGETS: list[dict] = [t for t in SCRAPE_TARGETS if t["type"] == "metal"]
METAL_KEYS: list[str] = [t["key"] for t in METAL_TARGETS]
ALL_REDIS_KEYS: list[str] = [t["redis_key"] for t in SCRAPE_TARGETS]

# O(1) lookup by key — avoids repeated list scans
TARGET_BY_KEY: dict[str, dict] = {t["key"]: t for t in SCRAPE_TARGETS}
