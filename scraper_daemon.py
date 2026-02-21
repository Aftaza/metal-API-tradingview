"""
Scraper Daemon v2 — Pure Stream Processing
============================================
Standalone worker process that runs 4 independent async tasks, each
continuously scraping a TradingView page via Playwright and writing
the latest price directly into Redis.

Usage:
    python scraper_daemon.py

Architecture:
    • Single Playwright Chromium browser instance (shared)
    • 4 isolated BrowserContexts (one per target) — true concurrency
    • Each worker: while True → navigate → extract → SET Redis → sleep
    • Auto-recovery: on any error, close broken context → wait → restart
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from playwright.async_api import async_playwright, Browser, BrowserContext

from config import (
    REDIS_URL,
    SCRAPE_INTERVAL_SECONDS,
    SCRAPE_TIMEOUT_MS,
    RECOVERY_DELAY_SECONDS,
    SCRAPE_TARGETS,
)

logger = logging.getLogger("scraper_daemon")

# CSS selector used by TradingView for the last traded price
PRICE_SELECTOR = "span[data-qa-id='symbol-last-value']"


# ──────────────────────────────────────────────────────────────────────
# Price extraction helpers
# ──────────────────────────────────────────────────────────────────────

def _parse_price(raw_text: str, target: dict) -> float | None:
    """Parse raw text from TradingView into a validated float price."""
    try:
        cleaned = raw_text.replace(",", "").strip()
        if not cleaned:
            return None

        # TradingView sometimes omits the decimal dot for metals
        if target["type"] == "metal" and "." not in cleaned and len(cleaned) > 3:
            cleaned = cleaned[:-2] + "." + cleaned[-2:]

        value = float(cleaned)

        # Range validation
        if target["type"] == "currency":
            if 10_000 < value < 25_000:
                return value
            logger.warning(f"[{target['name']}] Value {value} outside USDIDR range")
        else:
            if 0.01 < value < 50_000:
                return value
            logger.warning(f"[{target['name']}] Value {value} outside metal range")

        return None
    except (ValueError, TypeError) as exc:
        logger.error(f"[{target['name']}] Parse error: {exc}")
        return None


# ──────────────────────────────────────────────────────────────────────
# Worker coroutine — one per scraping target
# ──────────────────────────────────────────────────────────────────────

async def _worker(
    browser: Browser,
    redis_pool: aioredis.Redis,
    target: dict,
) -> None:
    """
    Infinite-loop worker for a single scraping target.

    Lifecycle per iteration:
        1. Create a fresh BrowserContext (lightweight, ~5 MB)
        2. Open new page, navigate to TradingView symbol URL
        3. Wait for price element to appear
        4. Extract text, parse, validate
        5. SET the value into Redis as JSON
        6. Close context, sleep, repeat

    On ANY exception the context is safely torn down and the loop
    continues after a recovery delay.
    """
    worker_name = target["name"]
    redis_key = target["redis_key"]
    url = target["url"]

    logger.info(f"[{worker_name}] Worker started  →  {url}")

    consecutive_failures = 0
    MAX_BACKOFF_SECONDS = 60

    while True:
        context: BrowserContext | None = None
        try:
            # ── 1. Fresh context ────────────────────────────────────
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                java_script_enabled=True,
            )
            context.set_default_timeout(SCRAPE_TIMEOUT_MS)

            page = await context.new_page()

            # Block heavy resources to speed up page load
            await page.route(
                "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4,webm}",
                lambda route: route.abort(),
            )

            # ── 2. Navigate ─────────────────────────────────────────
            await page.goto(url, wait_until="domcontentloaded")

            # ── 3. Wait for price element ───────────────────────────
            element = await page.wait_for_selector(
                PRICE_SELECTOR,
                state="visible",
                timeout=SCRAPE_TIMEOUT_MS,
            )

            if element is None:
                logger.warning(f"[{worker_name}] Price element not found")
                raise RuntimeError("Price element not found")

            # Small extra wait for rendering to stabilise
            await page.wait_for_timeout(800)

            # ── 4. Extract & parse ──────────────────────────────────
            raw_text = await element.inner_text()
            price = _parse_price(raw_text, target)

            if price is not None:
                payload = json.dumps({
                    "price": price,
                    "source": "TradingView",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

                # ── 5. Write to Redis ───────────────────────────────
                await redis_pool.set(redis_key, payload)
                logger.info(
                    f"[{worker_name}] ✓ {price:>12,.2f}  →  Redis({redis_key})"
                )
                consecutive_failures = 0  # reset on success
            else:
                logger.warning(
                    f"[{worker_name}] Extracted text '{raw_text}' "
                    f"could not be parsed into a valid price"
                )

        except asyncio.CancelledError:
            logger.info(f"[{worker_name}] Worker cancelled, shutting down")
            break

        except Exception as exc:
            consecutive_failures += 1
            backoff = min(
                RECOVERY_DELAY_SECONDS * consecutive_failures,
                MAX_BACKOFF_SECONDS,
            )
            logger.error(
                f"[{worker_name}] Error (attempt #{consecutive_failures}): "
                f"{type(exc).__name__}: {exc}  — retrying in {backoff}s"
            )
            await asyncio.sleep(backoff)
            continue  # skip the normal sleep & go straight to retry

        finally:
            # ── 6. Always tear down context ─────────────────────────
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass  # already broken, ignore

        # Normal interval between successful scrapes
        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)


# ──────────────────────────────────────────────────────────────────────
# Main entry-point
# ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("=" * 65)
    logger.info("  SCRAPER DAEMON v2 — Pure Stream Processing")
    logger.info(f"  Targets       : {len(SCRAPE_TARGETS)}")
    logger.info(f"  Interval      : {SCRAPE_INTERVAL_SECONDS}s")
    logger.info(f"  Timeout       : {SCRAPE_TIMEOUT_MS}ms")
    logger.info(f"  Redis         : {REDIS_URL}")
    logger.info("=" * 65)

    # Wait for Redis to be ready
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

    # Launch Playwright + Chromium
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
            ],
        )
        logger.info(f"✓ Chromium launched (pid {browser._impl_obj._browser_process.pid if hasattr(browser._impl_obj, '_browser_process') else '?'})")

        # Spawn one async task per target
        tasks: list[asyncio.Task] = []
        for target in SCRAPE_TARGETS:
            task = asyncio.create_task(
                _worker(browser, redis_pool, target),
                name=f"worker-{target['key']}",
            )
            tasks.append(task)

        logger.info(f"✓ {len(tasks)} workers spawned — entering main loop")

        try:
            # Block until all tasks finish (they shouldn't unless cancelled)
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Daemon received cancellation signal")
        finally:
            # Graceful shutdown
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await browser.close()
            await redis_pool.aclose()
            logger.info("✓ Daemon shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
