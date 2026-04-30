"""
Scraper Daemon v2.2 — Fault-Tolerant Stream Processing
=======================================================
Standalone worker process: one async task per scraping target.
Each task continuously scrapes a TradingView page via Playwright
and writes the latest price directly into Redis with TTL.

Architecture improvements in v2.2 (crash bug fixes):
  • Browser restart: when Chromium crashes (OOM / Page crashed /
    TargetClosedError), the daemon relaunches the ENTIRE browser
    process — workers are NOT stuck waiting on a dead browser object.
  • Memory management: aggressive resource blocking + `--single-process`
    flag removed (was causing instability). Added `--js-flags=--max-old-space-size`
    to limit V8 heap.
  • Selector disambiguation: uses `.first` (Playwright locator) instead of
    `query_selector` to avoid "2 elements" timeout ambiguity.
  • Page reuse replaced by goto on each cycle: more stable than reload()
    which can crash under memory pressure. Context is still shared.
  • Global browser health monitor: detects browser crashes and coordinates
    a clean restart across all workers via asyncio.Event.

Root causes fixed (from scraper.logs analysis):
  [Bug-1] Page.crashed → OOM due to 4 tabs doing reload() every 5s without
          enough shared memory. Fix: increase shm_size + limit V8 heap.
  [Bug-2] TargetClosedError on browser.new_context() after browser crash.
          Workers retried infinitely on a dead browser. Fix: browser-level
          restart coordinator replaces entire browser object.
  [Bug-3] "locator resolved to 2 elements" → used .first locator explicitly.

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
    Playwright,
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

# How many consecutive worker errors before we declare the browser dead
_BROWSER_CRASH_THRESHOLD = 3

# Chromium launch args — tuned for low-memory container environments
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",          # Use /tmp instead of /dev/shm
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-features=TranslateUI",
    "--disable-ipc-flooding-protection",
    "--memory-pressure-off",
    "--max_old_space_size=256",         # Cap V8 heap per tab at 256MB
    "--js-flags=--max-old-space-size=256",
]


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
# Browser lifecycle manager
# ──────────────────────────────────────────────────────────────────────

class BrowserManager:
    """
    Manages a single shared Chromium browser instance.

    Provides a thread-safe restart mechanism so that when the browser
    crashes (OOM, Page crashed, TargetClosedError), ALL workers pause,
    the browser is relaunched, and workers resume with fresh contexts.

    This fixes Bug-2 from the scraper.logs crash analysis where workers
    were stuck in infinite retry loops against a dead browser object.
    """

    def __init__(self, pw: Playwright) -> None:
        self._pw = pw
        self._browser: Browser | None = None
        # Event is SET when browser is healthy, CLEARED during restart
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Launch the browser for the first time."""
        self._browser = await self._launch()
        self._ready.set()
        logger.info("✓ Chromium launched")

    async def _launch(self) -> Browser:
        return await self._pw.chromium.launch(
            headless=True,
            args=_CHROMIUM_ARGS,
        )

    async def get_browser(self) -> Browser:
        """Wait until the browser is healthy, then return it."""
        await self._ready.wait()
        assert self._browser is not None
        return self._browser

    async def restart(self, reason: str) -> None:
        """
        Restart the browser process. Only one coroutine executes the
        restart; others wait via the asyncio.Event.
        """
        async with self._lock:
            if not self._ready.is_set():
                # Another worker already triggered restart — just wait
                return

            self._ready.clear()  # Block all workers while restarting
            logger.critical(
                "💀 Browser crash detected (%s) — restarting Chromium…", reason
            )

            # Kill the crashed browser
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

            # Wait a moment before relaunching (avoid tight restart loops)
            await asyncio.sleep(5)

            for attempt in range(1, 6):
                try:
                    self._browser = await self._launch()
                    self._ready.set()
                    logger.info("✓ Chromium restarted on attempt %d", attempt)
                    return
                except Exception as exc:
                    logger.error(
                        "Browser restart attempt %d/5 failed: %s", attempt, exc
                    )
                    await asyncio.sleep(10 * attempt)

            # If we can't restart after 5 attempts, re-raise so the daemon exits
            raise RuntimeError("Cannot restart Chromium after 5 attempts — aborting")

    async def close(self) -> None:
        """Cleanly close the browser on daemon shutdown."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass


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
        ignore_https_errors=False,
    )
    context.set_default_timeout(SCRAPE_TIMEOUT_MS)

    # Block heavy resources that slow page load without contributing price data
    await context.route(
        "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4,webm,ico,ttf,otf}",
        lambda route, _: route.abort(),
    )
    # Also block analytics/tracking that accumulate memory
    await context.route(
        "**/gtm.js*||**/analytics.js*||**/amplitude*||**/segment*",
        lambda route, _: route.abort(),
    )
    return context


async def _init_page(context: BrowserContext, url: str) -> Page:
    """
    Open a new page and navigate to URL.

    Uses goto() on every cycle (not reload()) for better memory stability.
    The page object itself is reused across cycles via the calling worker.
    """
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=SCRAPE_TIMEOUT_MS)
    # Wait for the FIRST price element to appear
    # Using .first fixes Bug-3: "locator resolved to 2 elements"
    await page.locator(PRICE_SELECTOR).first.wait_for(
        state="visible",
        timeout=SCRAPE_TIMEOUT_MS,
    )
    return page


# ──────────────────────────────────────────────────────────────────────
# Helper: detect whether an exception means the browser is dead
# ──────────────────────────────────────────────────────────────────────

def _is_browser_crash(exc: Exception) -> bool:
    """
    Return True if the exception indicates a browser-level crash
    (as opposed to a transient network / selector timeout).
    """
    msg = str(exc).lower()
    crash_signals = (
        "page crashed",
        "target page, context or browser has been closed",
        "targetclosederror",
        "browser has been closed",
    )
    return any(sig in msg for sig in crash_signals)


# ──────────────────────────────────────────────────────────────────────
# Worker coroutine — one per scraping target
# ──────────────────────────────────────────────────────────────────────

async def _worker(
    mgr: BrowserManager,
    redis_pool: aioredis.Redis,
    target: dict,
) -> None:
    """
    Infinite-loop worker for a single scraping target.

    v2.2 lifecycle:
      • Waits for BrowserManager to signal readiness before each cycle.
      • Creates a BrowserContext once, then navigates (goto) on each iteration.
      • goto() is more memory-stable than reload() under sustained load.
      • On transient errors (timeout): recreates context with exponential backoff.
      • On browser crash (TargetClosedError / Page crashed): signals BrowserManager
        to restart Chromium, then waits for the new browser to be ready.
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
            # ── Wait for browser to be healthy ───────────────────────
            browser = await mgr.get_browser()

            # ── Initialise or re-create context/page ─────────────────
            if context is None or page is None or page.is_closed():
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                context = await _create_context(browser)
                page = await _init_page(context, url)
                logger.info("[%s] ✓ Page (re)initialised", worker_name)
            else:
                # Reuse context but navigate fresh (more stable than reload)
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=SCRAPE_TIMEOUT_MS,
                )
                await page.locator(PRICE_SELECTOR).first.wait_for(
                    state="visible",
                    timeout=SCRAPE_TIMEOUT_MS,
                )

            # ── Wait for DOM to settle after JS updates ───────────────
            await page.wait_for_timeout(RENDER_SETTLE_MS)

            # ── Extract price text (use .first to avoid 2-element ambiguity) ──
            raw_text: str = await page.locator(PRICE_SELECTOR).first.inner_text()
            price = _parse_price(raw_text, target)

            if price is not None:
                payload = json.dumps(
                    {
                        "price": price,
                        "source": "TradingView",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

                # ── Write to Redis with TTL ───────────────────────────
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

        except Exception as exc:
            consecutive_failures += 1

            if _is_browser_crash(exc):
                # ── Browser-level crash: coordinate full restart ──────
                logger.error(
                    "[%s] 💥 Browser crash error #%d: %s",
                    worker_name, consecutive_failures, exc,
                )
                # Invalidate local context/page — they reference a dead browser
                context = None
                page = None

                # Trigger browser restart (only first caller does it, others wait)
                await mgr.restart(reason=str(exc))

                # After restart, reset failure counter and give browser time
                consecutive_failures = 0
                await asyncio.sleep(RECOVERY_DELAY_SECONDS)
                continue

            else:
                # ── Transient error: recreate context with backoff ────
                backoff = min(
                    RECOVERY_DELAY_SECONDS * consecutive_failures,
                    MAX_BACKOFF_SECONDS,
                )
                logger.error(
                    "[%s] Error #%d (%s: %s) — recreating context in %ds",
                    worker_name, consecutive_failures,
                    type(exc).__name__, str(exc).splitlines()[0],
                    backoff,
                )
                context = None
                page = None
                await asyncio.sleep(backoff)
                continue

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
    logger.info("  SCRAPER DAEMON v2.2 — Fault-Tolerant Stream Processing")
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

    # ── Launch Playwright + BrowserManager ───────────────────────────
    async with async_playwright() as pw:
        mgr = BrowserManager(pw)
        await mgr.start()

        # ── Spawn one worker per target ──────────────────────────────
        tasks: list[asyncio.Task] = [
            asyncio.create_task(
                _worker(mgr, redis_pool, target),
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
            await mgr.close()
            await redis_pool.aclose()
            logger.info("✓ Daemon shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
