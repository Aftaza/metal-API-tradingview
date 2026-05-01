"""
Metal Price Scraper Unified v3 — httpx + BeautifulSoup SSR Extraction
======================================================================
Runs ALL targets (gold, silver, copper, usdidr) in a SINGLE asyncio
event loop using concurrent httpx requests — no browser required.

Designed for low-resource VPS environments.

Architecture:
  • Single asyncio.gather() for all concurrent HTTP fetches
  • Shared httpx.AsyncClient (HTTP/2, connection pooling)
  • Random jitter ± SCRAPE_JITTER_FACTOR around SCRAPE_INTERVAL_SECONDS
  • Per-target failure counters + exponential back-off skip logic
  • LKG (Last-Known-Good) strategy: never deletes existing Redis data

Usage:
    python scraper_unified.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import redis.asyncio as aioredis
from bs4 import BeautifulSoup

from config import (
    HTTP_TIMEOUT_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    RECOVERY_DELAY_SECONDS,
    REDIS_URL,
    SCRAPE_INTERVAL_SECONDS,
    SCRAPE_JITTER_FACTOR,
    SCRAPE_TARGETS,
)

logger = logging.getLogger("scraper_unified")

# ---------------------------------------------------------------------------
# HTTP headers / user-agent pool (same as scraper_daemon)
# ---------------------------------------------------------------------------

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
# Jitter
# ---------------------------------------------------------------------------


def _jittered_interval() -> float:
    base = SCRAPE_INTERVAL_SECONDS
    delta = base * SCRAPE_JITTER_FACTOR
    return base + random.uniform(-delta, delta)


# ---------------------------------------------------------------------------
# Extraction logic (shared with scraper_daemon)
# ---------------------------------------------------------------------------

def _extract_kitco_price(html: str, target: dict) -> float | None:
    """Extract price from Kitco's Next.js __NEXT_DATA__ SSR block."""
    symbol = target["kitco_symbol"]

    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag is None:
        logger.warning(f"[{target['name']}] __NEXT_DATA__ tag not found")
        return None

    try:
        data: dict[str, Any] = json.loads(tag.string)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error(f"[{target['name']}] JSON parse error: {exc}")
        return None

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

        params = key_list[1] if len(key_list) > 1 else {}
        if isinstance(params, dict) and params.get("symbol", symbol) != symbol:
            continue

        results: list[dict] = (
            query.get("state", {})
            .get("data", {})
            .get("GetMetalQuoteV3", {})
            .get("results", [])
        )
        if not results:
            return None

        row = results[0]
        for field in ("mid", "bid", "ask"):
            raw = row.get(field)
            if raw is not None:
                try:
                    return float(raw)
                except (ValueError, TypeError):
                    continue

    logger.warning(f"[{target['name']}] metalQuote/{symbol} not found in queries")
    return None


_TV_TRADE_PRICE_RE: re.Pattern[str] = re.compile(
    r'"trade"\s*:\s*\{"price"\s*:\s*([\d.]+)',
    re.IGNORECASE,
)
_TV_DAILY_BAR_RE: re.Pattern[str] = re.compile(
    r'"daily_bar"\s*:\s*\{[^}]*"close"\s*:\s*"([\d.]+)"',
    re.IGNORECASE,
)


def _extract_tradingview_price(html: str, target: dict) -> float | None:
    """Extract price from TradingView's SSR-injected JSON state."""
    m = _TV_TRADE_PRICE_RE.search(html)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    m2 = _TV_DAILY_BAR_RE.search(html)
    if m2:
        try:
            val = float(m2.group(1))
            logger.debug(f"[{target['name']}] Used daily_bar fallback: {val}")
            return val
        except ValueError:
            pass

    logger.warning(f"[{target['name']}] Could not extract TradingView price")
    return None


def _validate_price(value: float, target: dict) -> bool:
    lo, hi = target["price_range"]
    if lo < value < hi:
        return True
    logger.warning(
        f"[{target['name']}] Price {value} outside range ({lo}, {hi})"
    )
    return False


# ---------------------------------------------------------------------------
# Per-target scrape (one HTTP request)
# ---------------------------------------------------------------------------

async def _scrape_target(
    client: httpx.AsyncClient,
    redis_pool: aioredis.Redis,
    target: dict,
    ua_index: int,
) -> bool:
    """Fetch and parse a single target. Returns True on success."""
    url = target["url"]
    headers = {
        **_BASE_HEADERS,
        "User-Agent": _USER_AGENTS[ua_index % len(_USER_AGENTS)],
    }

    try:
        resp = await client.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        html = resp.text
    except httpx.TimeoutException:
        logger.warning(f"[{target['name']}] Timeout after {HTTP_TIMEOUT_SECONDS}s")
        return False
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"[{target['name']}] HTTP {exc.response.status_code} from {url}"
        )
        return False
    except httpx.RequestError as exc:
        logger.warning(f"[{target['name']}] Request error: {exc}")
        return False

    # Parse
    if target["source"] == "kitco":
        price = _extract_kitco_price(html, target)
    else:
        price = _extract_tradingview_price(html, target)

    if price is None or not _validate_price(price, target):
        return False

    payload = json.dumps(
        {
            "price": price,
            "source": "Kitco" if target["source"] == "kitco" else "TradingView",
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
# Main loop
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("=" * 65)
    logger.info("  SCRAPER UNIFIED v3 — httpx + BeautifulSoup SSR Extraction")
    logger.info(f"  Targets  : {', '.join(t['name'] for t in SCRAPE_TARGETS)}")
    logger.info(f"  Interval : {SCRAPE_INTERVAL_SECONDS}s ± {SCRAPE_JITTER_FACTOR*100:.0f}%")
    logger.info(f"  Timeout  : {HTTP_TIMEOUT_SECONDS}s")
    logger.info(f"  Redis    : {REDIS_URL}")
    logger.info("=" * 65)

    # Wait for Redis
    redis_pool: aioredis.Redis | None = None
    while redis_pool is None:
        try:
            redis_pool = aioredis.from_url(
                REDIS_URL, decode_responses=True, socket_connect_timeout=5
            )
            await redis_pool.ping()
            logger.info("✓ Connected to Redis")
        except Exception as exc:
            logger.warning(f"Redis not ready ({exc}), retrying in 2s…")
            redis_pool = None
            await asyncio.sleep(2)

    # Per-target consecutive failure counter
    failure_counts: dict[str, int] = {t["key"]: 0 for t in SCRAPE_TARGETS}
    ua_index = 0
    round_count = 0

    try:
        async with httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        ) as client:
            while True:
                round_count += 1
                round_start = time.monotonic()

                # Determine which targets should run this round
                # (targets with too many failures get skipped for back-off)
                active_targets = []
                skipped = []
                for t in SCRAPE_TARGETS:
                    fails = failure_counts[t["key"]]
                    if fails > 0 and fails % MAX_CONSECUTIVE_FAILURES == 0:
                        # Skip this round — still in back-off
                        skipped.append(t["key"])
                    else:
                        active_targets.append(t)

                if skipped:
                    logger.info(f"── Skipping (back-off): {skipped}")

                # Fetch all active targets CONCURRENTLY
                tasks = [
                    _scrape_target(client, redis_pool, t, ua_index)
                    for t in active_targets
                ]
                results: list[bool] = await asyncio.gather(*tasks, return_exceptions=False)

                # Update failure counters
                ok_count = 0
                for target, success in zip(active_targets, results):
                    key = target["key"]
                    if success:
                        failure_counts[key] = 0
                        ok_count += 1
                    else:
                        failure_counts[key] += 1
                        count = failure_counts[key]
                        backoff = min(
                            RECOVERY_DELAY_SECONDS * (2 ** (count - 1)), 120
                        )
                        log_fn = (
                            logger.error if count >= MAX_CONSECUTIVE_FAILURES
                            else logger.warning
                        )
                        log_fn(
                            f"[{target['name']}] Failure #{count} — "
                            f"back-off active next {backoff:.0f}s"
                        )

                ua_index += 1  # Rotate UA each round
                round_duration = time.monotonic() - round_start
                sleep_secs = max(0.0, _jittered_interval() - round_duration)

                logger.info(
                    f"── Round {round_count}: {ok_count}/{len(active_targets)} OK "
                    f"in {round_duration:.2f}s | next in {sleep_secs:.1f}s ──"
                )
                await asyncio.sleep(sleep_secs)

    except asyncio.CancelledError:
        logger.info("Daemon received cancellation signal")
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        if redis_pool is not None:
            await redis_pool.aclose()
        logger.info("✓ Unified daemon shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
