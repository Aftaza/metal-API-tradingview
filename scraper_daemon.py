"""
Metal Price Scraper Daemon v3 — httpx + BeautifulSoup SSR Extraction
=====================================================================
Lightweight, browser-free scraper daemon.

Architecture:
  • httpx.AsyncClient with HTTP/2, connection pooling, and custom headers
  • BeautifulSoup to locate <script id="__NEXT_DATA__"> (Kitco)
    or inline JSON state (TradingView) — no JavaScript execution required
  • Random jitter on the scrape interval to avoid fingerprinting
  • Exponential back-off on failures with a configurable cap
  • Last-Known-Good (LKG) Redis write: stale data is never deleted,
    so the API can always serve the most recent value

Environment variables (see config.py):
    SCRAPE_TARGET           = gold | silver | copper | usdidr
    SCRAPE_INTERVAL_SECONDS = 30          (base interval)
    SCRAPE_JITTER_FACTOR    = 0.3         (±30% random jitter)
    HTTP_TIMEOUT_SECONDS    = 20
    RECOVERY_DELAY_SECONDS  = 5
    MAX_CONSECUTIVE_FAILURES = 5
    REDIS_URL               = redis://redis:6379/0
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import redis.asyncio as aioredis
from bs4 import BeautifulSoup

from config import (
    DATA_STALENESS_SECONDS,  # noqa: F401 — imported for completeness
    HTTP_TIMEOUT_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    RECOVERY_DELAY_SECONDS,
    REDIS_URL,
    SCRAPE_INTERVAL_SECONDS,
    SCRAPE_JITTER_FACTOR,
    SCRAPE_TARGET,
    get_active_target,
)

logger = logging.getLogger("scraper_daemon")

# ---------------------------------------------------------------------------
# HTTP client configuration
# ---------------------------------------------------------------------------

# Rotate through a small set of realistic user-agent strings
_USER_AGENTS: list[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
]

# Shared headers that mimic a real browser request
_BASE_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

# ---------------------------------------------------------------------------
# Jitter helpers
# ---------------------------------------------------------------------------

import random  # noqa: E402 — after stdlib section only


def _jittered_interval() -> float:
    """Return a sleep duration around SCRAPE_INTERVAL_SECONDS ± SCRAPE_JITTER_FACTOR.

    E.g. with interval=30s and jitter=0.3:
        range: [21s, 39s] — uniform random within ±30% of the base.
    """
    base = SCRAPE_INTERVAL_SECONDS
    delta = base * SCRAPE_JITTER_FACTOR
    return base + random.uniform(-delta, delta)


# ---------------------------------------------------------------------------
# Price extraction — Kitco (Next.js __NEXT_DATA__ SSR)
# ---------------------------------------------------------------------------

def _extract_kitco_price(html: str, target: dict) -> float | None:
    """
    Parse the Kitco page HTML and extract the price from the embedded
    Next.js SSR JSON data block:

        <script id="__NEXT_DATA__" type="application/json">{ … }</script>

    The data path is:
        props.pageProps.dehydratedState.queries[*]
            where queryKey[0] == "metalQuote"
            → state.data.GetMetalQuoteV3.results[0].mid
    """
    symbol = target["kitco_symbol"]  # e.g. "AU", "AG", "CU"

    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag is None:
        logger.warning(f"[{target['name']}] __NEXT_DATA__ tag not found in HTML")
        return None

    try:
        data: dict[str, Any] = json.loads(tag.string)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error(f"[{target['name']}] Failed to parse __NEXT_DATA__ JSON: {exc}")
        return None

    # Navigate the dehydrated react-query state
    queries: list[dict] = (
        data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )

    for query in queries:
        key_list: list = query.get("queryKey", [])
        if not key_list or key_list[0] != "metalQuote":
            continue

        # Confirm symbol matches if present in queryKey params
        params = key_list[1] if len(key_list) > 1 else {}
        if isinstance(params, dict) and params.get("symbol", symbol) != symbol:
            continue

        quote_v3: dict = (
            query.get("state", {})
            .get("data", {})
            .get("GetMetalQuoteV3", {})
        )

        results: list[dict] = quote_v3.get("results", [])
        if not results:
            logger.warning(f"[{target['name']}] Empty results array in GetMetalQuoteV3")
            return None

        # Prefer "mid" (midpoint of bid/ask); fall back to "bid"
        row: dict = results[0]
        for field in ("mid", "bid", "ask"):
            raw = row.get(field)
            if raw is not None:
                try:
                    return float(raw)
                except (ValueError, TypeError):
                    continue

    logger.warning(f"[{target['name']}] metalQuote query for symbol={symbol} not found")
    return None


# ---------------------------------------------------------------------------
# Price extraction — TradingView (SSR inline state JSON)
# ---------------------------------------------------------------------------

# TradingView injects a JSON blob containing the current price as:
#   "trade":{"price":17368.0}
# This regex is fast (no DOM parsing needed) and stable across builds.
_TV_TRADE_PRICE_RE: re.Pattern[str] = re.compile(
    r'"trade"\s*:\s*\{"price"\s*:\s*([\d.]+)',
    re.IGNORECASE,
)

# Fallback regex: "close":"17368.0" inside daily_bar
_TV_DAILY_BAR_RE: re.Pattern[str] = re.compile(
    r'"daily_bar"\s*:\s*\{[^}]*"close"\s*:\s*"([\d.]+)"',
    re.IGNORECASE,
)


def _extract_tradingview_price(html: str, target: dict) -> float | None:
    """
    Extract the current USD/IDR rate from TradingView's SSR-injected JSON.

    TradingView embeds the symbol's current state directly in the HTML
    response (before any JS runs), typically inside a large inline JSON
    blob assigned to a JavaScript object.  No browser required.
    """
    # Strategy 1: "trade":{"price": N}  — most reliable
    m = _TV_TRADE_PRICE_RE.search(html)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # Strategy 2: daily_bar.close (slightly stale but still good)
    m2 = _TV_DAILY_BAR_RE.search(html)
    if m2:
        try:
            val = float(m2.group(1))
            logger.debug(
                f"[{target['name']}] Used daily_bar.close fallback: {val}"
            )
            return val
        except ValueError:
            pass

    logger.warning(f"[{target['name']}] Could not extract price from TradingView HTML")
    return None


# ---------------------------------------------------------------------------
# Price validation
# ---------------------------------------------------------------------------

def _validate_price(value: float, target: dict) -> bool:
    """Check that the extracted price falls within the expected range."""
    lo, hi = target["price_range"]
    if lo < value < hi:
        return True
    logger.warning(
        f"[{target['name']}] Price {value} outside expected range "
        f"({lo}, {hi}) — discarding"
    )
    return False


# ---------------------------------------------------------------------------
# HTTP fetch with retries
# ---------------------------------------------------------------------------

async def _fetch_html(
    client: httpx.AsyncClient,
    target: dict,
    ua_index: int,
) -> str | None:
    """Fetch the target page and return the raw HTML string, or None on error."""
    url = target["url"]
    headers = {
        **_BASE_HEADERS,
        "User-Agent": _USER_AGENTS[ua_index % len(_USER_AGENTS)],
    }

    try:
        resp = await client.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.text
    except httpx.TimeoutException:
        logger.warning(f"[{target['name']}] HTTP timeout after {HTTP_TIMEOUT_SECONDS}s")
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"[{target['name']}] HTTP {exc.response.status_code} from {url}"
        )
    except httpx.RequestError as exc:
        logger.warning(f"[{target['name']}] Request error: {exc}")

    return None


# ---------------------------------------------------------------------------
# Single scrape cycle
# ---------------------------------------------------------------------------

async def _scrape_once(
    client: httpx.AsyncClient,
    redis_pool: aioredis.Redis,
    target: dict,
    ua_index: int,
) -> bool:
    """
    Perform one scrape cycle for `target`.

    Returns True on success (price written to Redis), False otherwise.
    """
    html = await _fetch_html(client, target, ua_index)
    if html is None:
        return False

    # Extract price from HTML
    source = target["source"]
    if source == "kitco":
        price = _extract_kitco_price(html, target)
    else:
        price = _extract_tradingview_price(html, target)

    if price is None:
        return False

    if not _validate_price(price, target):
        return False

    # Build payload and write to Redis (LKG strategy — never delete old value)
    payload = json.dumps(
        {
            "price": price,
            "source": "Kitco" if source == "kitco" else "TradingView",
            "unit": target["unit"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    await redis_pool.set(target["redis_key"], payload)
    logger.info(
        f"[{target['name']}] ✓ {price:>12,.4f}  →  Redis({target['redis_key']})"
    )
    return True


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------

async def _worker(
    redis_pool: aioredis.Redis,
    target: dict,
) -> None:
    """
    Infinite scrape loop for a single target.

    Behaviour:
      • Jittered sleep between cycles to avoid periodic burst traffic
      • Exponential back-off on consecutive failures (capped)
      • User-agent rotation across cycles
      • Single persistent httpx.AsyncClient with connection reuse (HTTP/2)
    """
    name = target["name"]
    consecutive_failures = 0
    ua_index = 0

    logger.info(f"[{name}] Worker starting  →  {target['url']}")

    # Build a single AsyncClient per worker — connection pool is reused
    async with httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=2, max_connections=4),
    ) as client:
        while True:
            try:
                success = await _scrape_once(client, redis_pool, target, ua_index)
                ua_index += 1  # Rotate user-agent each cycle

                if success:
                    consecutive_failures = 0
                    sleep_secs = _jittered_interval()
                    logger.debug(f"[{name}] Next scrape in {sleep_secs:.1f}s")
                    await asyncio.sleep(sleep_secs)
                else:
                    consecutive_failures += 1
                    # Exponential back-off: 5s, 10s, 20s … capped at 120s
                    backoff = min(
                        RECOVERY_DELAY_SECONDS * (2 ** (consecutive_failures - 1)),
                        120,
                    )
                    log_fn = (
                        logger.error
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        else logger.warning
                    )
                    log_fn(
                        f"[{name}] Scrape failed "
                        f"(attempt #{consecutive_failures}) — retry in {backoff}s"
                    )
                    await asyncio.sleep(backoff)

            except asyncio.CancelledError:
                logger.info(f"[{name}] Worker cancelled — shutting down")
                break

            except Exception as exc:
                consecutive_failures += 1
                backoff = min(
                    RECOVERY_DELAY_SECONDS * (2 ** (consecutive_failures - 1)),
                    120,
                )
                logger.error(
                    f"[{name}] Unexpected error ({type(exc).__name__}: {exc}) "
                    f"— retry in {backoff}s"
                )
                await asyncio.sleep(backoff)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    target = get_active_target()

    if target is None:
        logger.error(
            f"SCRAPE_TARGET='{SCRAPE_TARGET}' is not valid. "
            f"Set SCRAPE_TARGET to one of: gold, silver, copper, usdidr"
        )
        sys.exit(1)

    logger.info("=" * 65)
    logger.info("  SCRAPER DAEMON v3 — httpx + BeautifulSoup SSR Extraction")
    logger.info(f"  Target   : {target['name']}")
    logger.info(f"  Source   : {target['source']}")
    logger.info(f"  URL      : {target['url']}")
    logger.info(f"  Interval : {SCRAPE_INTERVAL_SECONDS}s ± {SCRAPE_JITTER_FACTOR*100:.0f}%")
    logger.info(f"  Timeout  : {HTTP_TIMEOUT_SECONDS}s")
    logger.info(f"  Redis    : {REDIS_URL}")
    logger.info("=" * 65)

    # Wait for Redis
    redis_pool: aioredis.Redis | None = None
    while redis_pool is None:
        try:
            redis_pool = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
            )
            await redis_pool.ping()
            logger.info("✓ Connected to Redis")
        except Exception as exc:
            logger.warning(f"Redis not ready ({exc}), retrying in 2s…")
            redis_pool = None
            await asyncio.sleep(2)

    try:
        await _worker(redis_pool, target)
    except asyncio.CancelledError:
        logger.info("Daemon received cancellation signal")
    finally:
        await redis_pool.aclose()
        logger.info("✓ Daemon shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
