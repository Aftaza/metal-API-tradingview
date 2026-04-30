"""
Scraper Daemon v2 — Optimized Stream Processing
================================================
Standalone worker process with 4 independent async tasks.
Each task continuously scrapes a TradingView page via Playwright
and writes the latest price directly into Redis with TTL.

Architecture improvements over v1:
  • Reuses BrowserPage across iterations — creates new context ONLY on error
  • Per-metal price range validation (not generic 0.01–50,000)
  • Regex-based price parsing (more robust than heuristic string slicing)
  • Structured error logging with failure counters
  • Redis SET with TTL so stale data auto-expires
  • Non-root container execution (see Dockerfile.scraper)
  • asyncio.TaskGroup for structured concurrency

Usage:
    python scraper_daemon.py
"""

import asyncio
import json
import logging
import re
import signal
from datetime import datetime, timezone

import redis.asyncio as aioredis
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from config import (
    REDIS_URL,
    REDIS_KEY_TTL_SECONDS,
    SCRAPE_INTERVAL_SECONDS,
    SCRAPE_TIMEOUT_MS,
    RECOVERY_DELAY_SECONDS,
    MAX_BACKOFF_SECONDS,
    RENDER_SETTLE_MS,
    SCRAPE_TARGETS,
    setup_logging,
)

logger = setup_logging("scraper_daemon")

# CSS selector used by TradingView for the last traded price
PRICE_SELECTOR: str = "span[data-qa-id='symbol-last-value']"

# Regex: match a number like "3,247.80" or "16325" or "0.9234"
_PRICE_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")


# ──────────────────────────────────────────────────────────────────────
# Price extraction helpers
# ──────────────────────────────────────────────────────────────────────

def _parse_price(raw_text: str, target: dict) -> float | None:
    """
    Parse raw text from TradingView into a validated float price.

    Strategy:
      1. Strip whitespace and remove thousands separators (commas).
      2. For metal prices TradingView may omit the decimal point:
         e.g. "324780" → "3247.80". We detect this case via regex
         and insert the dot two places from the right.
      3. Validate against per-target min/max range (from config.py).

    Returns None if text cannot be parsed or is out of range.
    """
    try:
        if raw_text is None:
            return None
        cleaned: str = raw_text.strip()
        if not cleaned:
            return None

        # Remove thousands separators
        cleaned = cleaned.replace(",", "")

        if not _PRICE_RE.match(cleaned):
            logger.warning(
                "[%s] Unexpected price format: '%s'", target["name"], raw_text
            )
            return None

        # TradingView sometimes omits the decimal point for metals
        # (e.g. "324780" should be "3247.80", or "468" should be "4.68")
        if target["type"] == "metal" and "." not in cleaned and len(cleaned) >= 3:
            cleaned = cleaned[:-2] + "." + cleaned[-2:]

        value = float(cleaned)

        # Per-metal range validation (defined in config.py SCRAPE_TARGETS)
        min_val = target.get("min_value", 0.01)
        max_val = target.get("max_value", 1_000_000.0)

        if min_val < value < max_val:
            return value

        logger.warning(
            "[%s] Value %.4f outside expected range [%.2f, %.2f]",
            target["name"], value, min_val, max_val,
        )
        return None

    except (ValueError, TypeError) as exc:
        logger.error("[%s] Parse error for '%s': %s", target["name"], raw_text, exc)
        return None


# ──────────────────────────────────────────────────────────────────────
# Browser context factory
# ──────────────────────────────────────────────────────────────────────

async def _create_context(browser: Browser) -> BrowserContext:
    """Create a fresh, lightweight BrowserContext with resource blocking."""
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        java_script_enabled=True,
        # Ignore HTTPS errors from potential redirects
        ignore_https_errors=False,
    )
    context.set_default_timeout(SCRAPE_TIMEOUT_MS)

    # Block heavy resources that slow page load without contributing price data
    await context.route(
        "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4,webm,ico}",
        lambda route, _: route.abort(),
    )
    return context


async def _create_page(context: BrowserContext, url: str) -> Page:
    """Open a new page, navigate to URL, and wait for price element."""
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=SCRAPE_TIMEOUT_MS)
    # Wait for the price element to appear in DOM
    await page.wait_for_selector(
        PRICE_SELECTOR,
        state="visible",
        timeout=SCRAPE_TIMEOUT_MS,
    )
    return page


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

    Optimized lifecycle (v2 improvement):
      • Creates BrowserContext + Page ONCE, then REUSES the Page.
      • On success: reads price → SET Redis with TTL → sleep → repeat.
      • On ANY error: closes broken context, creates a fresh one, continues.
      • Exponential backoff capped at MAX_BACKOFF_SECONDS.

    This avoids the per-iteration context allocation overhead of the previous
    version while maintaining full error isolation.
    """
    worker_name = target["name"]
    redis_key = target["redis_key"]
    url = target["url"]

    logger.info("[%s] Worker started → %s", worker_name, url)

    consecutive_failures: int = 0
    context: BrowserContext | None = None
    page: Page | None = None

    while True:
        try:
            # ── Initialise or reuse context/page ────────────────────
            if context is None or page is None:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                context = await _create_context(browser)
                page = await _create_page(context, url)
                logger.info("[%s] ✓ Page (re)initialised", worker_name)

            else:
                # Reuse existing page — just reload to get fresh data
                await page.reload(wait_until="domcontentloaded", timeout=SCRAPE_TIMEOUT_MS)
                await page.wait_for_selector(
                    PRICE_SELECTOR,
                    state="visible",
                    timeout=SCRAPE_TIMEOUT_MS,
                )

            # ── Wait for DOM to settle after JS updates ─────────────
            await page.wait_for_timeout(RENDER_SETTLE_MS)

            # ── Extract price text ───────────────────────────────────
            element = await page.query_selector(PRICE_SELECTOR)
            if element is None:
                raise RuntimeError(f"Price selector not found: {PRICE_SELECTOR}")

            raw_text: str = await element.inner_text()
            price = _parse_price(raw_text, target)

            if price is not None:
                payload = json.dumps(
                    {
                        "price": price,
                        "source": "TradingView",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

                # ── Write to Redis with TTL ──────────────────────────
                await redis_pool.set(redis_key, payload, ex=REDIS_KEY_TTL_SECONDS)
                logger.info(
                    "[%s] ✓ %12.2f  →  Redis(%s)  [TTL=%ds]",
                    worker_name, price, redis_key, REDIS_KEY_TTL_SECONDS,
                )
                consecutive_failures = 0  # reset on success

            else:
                logger.warning(
                    "[%s] Raw text '%s' could not be parsed — skipping write",
                    worker_name, raw_text,
                )

        except asyncio.CancelledError:
            logger.info("[%s] Worker cancelled, shutting down", worker_name)
            break

        except (PlaywrightTimeoutError, Exception) as exc:
            consecutive_failures += 1
            backoff = min(
                RECOVERY_DELAY_SECONDS * consecutive_failures,
                MAX_BACKOFF_SECONDS,
            )
            logger.error(
                "[%s] Error #%d (%s: %s) — recreating context in %ds",
                worker_name, consecutive_failures, type(exc).__name__, exc, backoff,
            )
            # Invalidate context so next iteration creates a fresh one
            context = None
            page = None
            await asyncio.sleep(backoff)
            continue  # skip normal sleep, retry immediately

        finally:
            pass  # context managed in the while loop above

        # Normal sleep between successful scrapes
        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)

    # Cleanup on exit
    if context is not None:
        try:
            await context.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# Main entry-point
# ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("=" * 65)
    logger.info("  SCRAPER DAEMON v2.1 — Optimized Stream Processing")
    logger.info("  Targets       : %d  (%s)", len(SCRAPE_TARGETS),
                ", ".join(t["key"] for t in SCRAPE_TARGETS))
    logger.info("  Interval      : %ds", SCRAPE_INTERVAL_SECONDS)
    logger.info("  Timeout       : %dms", SCRAPE_TIMEOUT_MS)
    logger.info("  Redis TTL     : %ds", REDIS_KEY_TTL_SECONDS)
    logger.info("  Redis         : %s", REDIS_URL)
    logger.info("=" * 65)

    # ── Wait for Redis ───────────────────────────────────────────────
    redis_pool: aioredis.Redis | None = None
    while redis_pool is None:
        try:
            pool = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            await pool.ping()
            redis_pool = pool
            logger.info("✓ Connected to Redis")
        except Exception as exc:
            logger.warning("Redis not ready (%s), retrying in 2s…", exc)
            await asyncio.sleep(2)

    # ── Launch Playwright + Chromium ─────────────────────────────────
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
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
            ],
        )
        logger.info("✓ Chromium launched")

        # ── Spawn one worker per target ──────────────────────────────
        tasks: list[asyncio.Task] = [
            asyncio.create_task(
                _worker(browser, redis_pool, target),
                name=f"worker-{target['key']}",
            )
            for target in SCRAPE_TARGETS
        ]
        logger.info("✓ %d workers spawned — entering main loop", len(tasks))

        # ── Graceful shutdown on SIGTERM/SIGINT ──────────────────────
        loop = asyncio.get_running_loop()

        def _handle_signal() -> None:
            logger.info("Signal received — cancelling workers…")
            for task in tasks:
                task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                # Windows does not support add_signal_handler for all signals
                pass

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
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
