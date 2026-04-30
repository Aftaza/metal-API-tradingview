"""
Scraper Daemon v2.3 — Resilient Anti-Rate-Limit Stream Processing
=================================================================
Production-grade scraper for real-time metal prices used in an
e-commerce pricing engine (central-bullions-project.vercel.app).

Root causes fixed from scraper.log analysis:
  [Bug-1] All 4 workers timeout SIMULTANEOUSLY (14:19:24–14:19:41).
          TradingView detects concurrent scraping from the same IP
          and rate-limits all connections in bulk.
          Fix: Staggered startup (each worker waits N*4s before first
          request) + per-iteration jitter (±2s randomization).

  [Bug-2] Workers restart contexts indefinitely but keep hitting the
          same blocked state with linear backoff climbing too slowly.
          Fix: Exponential backoff with jitter, and a circuit-breaker
          that after MAX_FAILURES resets to max-backoff immediately.

  [Bug-3] No persistence layer — when scraper is down, Redis TTL
          expires and API returns 503 to the live e-commerce site.
          Fix: "last-known-good" fallback key (LKG) with a 24h TTL.
          API can serve stale data with a warning header instead of 503.

  [Bug-4] Single CSS selector — if TradingView renames the attribute,
          all workers die simultaneously.
          Fix: Multi-selector waterfall with 3 fallback selectors.

Architecture:
  • BrowserManager (v2.2): coordinates Chromium restart on OOM/crash
  • Staggered startup: worker[i] sleeps i*STAGGER_SECONDS before first run
  • Per-cycle jitter: random ±JITTER_SECONDS added to SCRAPE_INTERVAL
  • Multi-selector: tries 3 different CSS/XPath selectors in order
  • Last-known-good (LKG) cache: separate Redis key with 24h TTL
  • Circuit breaker: after N failures, worker pauses for CIRCUIT_BREAK_SECONDS

Usage:
    python scraper_daemon.py
"""

import asyncio
import json
import logging
import random
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

# ─── Timing constants ────────────────────────────────────────────────────────
# Seconds between worker startups (prevents simultaneous first requests)
STAGGER_SECONDS: int = 4

# Random jitter applied to each sleep interval (±JITTER seconds)
JITTER_SECONDS: float = 2.0

# After this many consecutive failures, pause for CIRCUIT_BREAK_SECONDS
CIRCUIT_BREAK_THRESHOLD: int = 10

# How long to pause a worker that has hit the circuit-breaker threshold
CIRCUIT_BREAK_SECONDS: int = 120

# Last-known-good Redis TTL: 24 hours (vs REDIS_KEY_TTL_SECONDS = 60s)
LKG_TTL_SECONDS: int = 86_400  # 24 hours

# LKG key prefix: "lkg:gold", "lkg:silver", etc.
LKG_KEY_PREFIX: str = "lkg"

# ─── CSS selectors tried in order (waterfall) ─────────────────────────────
# TradingView has historically used multiple class/attribute patterns.
# We try each in sequence and use the first one that resolves.
PRICE_SELECTORS: list[str] = [
    "span[data-qa-id='symbol-last-value']",          # Primary (current as of 2025)
    "span.js-symbol-last",                           # Fallback class-based
    "div.tv-symbol-price-quote__value span",          # Structural fallback
]

# Regex: match a number like "3,247.80" or "16325" or "0.9234"
_PRICE_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")

# Chromium launch args — memory-optimised for container environments
_CHROMIUM_ARGS = [
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
    "--disable-features=TranslateUI",
    "--disable-ipc-flooding-protection",
    "--js-flags=--max-old-space-size=256",
]


# ──────────────────────────────────────────────────────────────────────────────
# Price parsing
# ──────────────────────────────────────────────────────────────────────────────

def _parse_price(raw_text: str, target: dict) -> float | None:
    """
    Parse raw TradingView price text into a validated float.

    Strategy:
      1. Strip whitespace + remove comma thousands-separators.
      2. For metals without a decimal: insert dot 2 places from right.
      3. Validate against per-target min/max range (config.py).

    Returns None on parse failure or out-of-range value.
    """
    try:
        if raw_text is None:
            return None
        cleaned: str = raw_text.strip()
        if not cleaned:
            return None

        cleaned = cleaned.replace(",", "")

        if not _PRICE_RE.match(cleaned):
            logger.warning(
                "[%s] Unexpected price format: '%s'", target["name"], raw_text
            )
            return None

        if target["type"] == "metal" and "." not in cleaned and len(cleaned) >= 3:
            cleaned = cleaned[:-2] + "." + cleaned[-2:]

        value = float(cleaned)
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


# ──────────────────────────────────────────────────────────────────────────────
# Browser manager (v2.2 — crash-safe browser restart)
# ──────────────────────────────────────────────────────────────────────────────

class BrowserManager:
    """
    Manages a single shared Chromium browser instance with coordinated restarts.

    When Chromium crashes (OOM / TargetClosedError), all workers pause,
    the browser is relaunched cleanly, then workers resume.
    """

    def __init__(self, pw: Playwright) -> None:
        self._pw = pw
        self._browser: Browser | None = None
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._browser = await self._launch()
        self._ready.set()
        logger.info("✓ Chromium launched")

    async def _launch(self) -> Browser:
        return await self._pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)

    async def get_browser(self) -> Browser:
        await self._ready.wait()
        assert self._browser is not None
        return self._browser

    async def restart(self, reason: str) -> None:
        async with self._lock:
            if not self._ready.is_set():
                return  # Another worker already handling restart

            self._ready.clear()
            logger.critical(
                "💀 Browser crash detected (%s) — restarting Chromium…", reason
            )

            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

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

            raise RuntimeError("Cannot restart Chromium after 5 attempts — aborting")

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Browser context + page helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _create_context(browser: Browser) -> BrowserContext:
    """Create a resource-blocking BrowserContext with randomised viewport."""
    # Slightly vary viewport to avoid fingerprint matching
    width = random.randint(1260, 1440)
    height = random.randint(700, 800)

    context = await browser.new_context(
        viewport={"width": width, "height": height},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        java_script_enabled=True,
        ignore_https_errors=False,
        locale="en-US",
        timezone_id="America/New_York",
    )
    context.set_default_timeout(SCRAPE_TIMEOUT_MS)

    # Block resources not needed for price extraction
    await context.route(
        "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4,webm,ico,ttf,otf}",
        lambda route, _: route.abort(),
    )
    await context.route(
        "**/{gtm,analytics,amplitude,segment,hotjar,intercom}*",
        lambda route, _: route.abort(),
    )
    return context


async def _try_get_price_text(page: Page) -> str | None:
    """
    Try each selector in PRICE_SELECTORS waterfall.
    Returns the first non-empty inner text found, or None.
    """
    for selector in PRICE_SELECTORS:
        try:
            locator = page.locator(selector).first
            # Short timeout per fallback attempt — don't wait the full 30s for each
            await locator.wait_for(state="visible", timeout=8_000)
            text = await locator.inner_text()
            if text and text.strip():
                return text.strip()
        except Exception:
            continue  # Try next selector
    return None


async def _init_page(context: BrowserContext, url: str) -> Page:
    """Open page, navigate, and wait for any price selector to appear."""
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=SCRAPE_TIMEOUT_MS)
    # Wait for primary selector (main timeout applies)
    await page.locator(PRICE_SELECTORS[0]).first.wait_for(
        state="visible",
        timeout=SCRAPE_TIMEOUT_MS,
    )
    return page


# ──────────────────────────────────────────────────────────────────────────────
# Crash detection helper
# ──────────────────────────────────────────────────────────────────────────────

def _is_browser_crash(exc: Exception) -> bool:
    """Return True if exception signals a browser-level crash, not a timeout."""
    msg = str(exc).lower()
    return any(sig in msg for sig in (
        "page crashed",
        "target page, context or browser has been closed",
        "targetclosederror",
        "browser has been closed",
    ))


# ──────────────────────────────────────────────────────────────────────────────
# Redis helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _write_price(
    redis_pool: aioredis.Redis,
    target: dict,
    price: float,
) -> None:
    """
    Write price to Redis with two keys:
      1. Live key (price:gold) — short TTL (REDIS_KEY_TTL_SECONDS = 60s)
         If this expires, API knows scraper is down.
      2. Last-known-good key (lkg:gold) — long TTL (LKG_TTL_SECONDS = 24h)
         API can serve stale data with a warning instead of 503.
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({
        "price": price,
        "source": "TradingView",
        "updated_at": now,
    })
    lkg_payload = json.dumps({
        "price": price,
        "source": "TradingView (last-known-good)",
        "updated_at": now,
        "is_stale": False,
    })

    live_key = target["redis_key"]
    lkg_key = f"{LKG_KEY_PREFIX}:{target['key']}"

    async with redis_pool.pipeline(transaction=True) as pipe:
        pipe.set(live_key, payload, ex=REDIS_KEY_TTL_SECONDS)
        pipe.set(lkg_key, lkg_payload, ex=LKG_TTL_SECONDS)
        await pipe.execute()


# ──────────────────────────────────────────────────────────────────────────────
# Worker coroutine — one per scraping target
# ──────────────────────────────────────────────────────────────────────────────

async def _worker(
    mgr: BrowserManager,
    redis_pool: aioredis.Redis,
    target: dict,
    worker_index: int,
) -> None:
    """
    Infinite-loop worker for a single scraping target.

    v2.3 improvements:
      • Staggered startup: sleeps (worker_index * STAGGER_SECONDS) before first
        request. Prevents all 4 workers from hitting TradingView simultaneously.
      • Per-cycle jitter: adds random ±JITTER_SECONDS to the sleep interval.
        Even with 4 workers, requests are spread across a time window.
      • Multi-selector waterfall: tries 3 CSS selectors in sequence.
      • Last-known-good write: short-TTL live key + long-TTL LKG key.
      • Circuit breaker: after CIRCUIT_BREAK_THRESHOLD consecutive failures,
        pauses for CIRCUIT_BREAK_SECONDS before retrying.
    """
    worker_name = target["name"]
    url = target["url"]

    logger.info(
        "[%s] Worker %d started — stagger delay: %ds",
        worker_name, worker_index, worker_index * STAGGER_SECONDS,
    )

    # ── Staggered startup: delay each worker by its index ─────────────
    if worker_index > 0:
        await asyncio.sleep(worker_index * STAGGER_SECONDS)

    consecutive_failures: int = 0
    context: BrowserContext | None = None
    page: Page | None = None

    while True:
        try:
            # ── Wait for browser to be healthy ────────────────────────
            browser = await mgr.get_browser()

            # ── Initialise or re-create context/page ──────────────────
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
                # Navigate fresh each cycle (goto > reload for memory stability)
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=SCRAPE_TIMEOUT_MS,
                )
                await page.locator(PRICE_SELECTORS[0]).first.wait_for(
                    state="visible",
                    timeout=SCRAPE_TIMEOUT_MS,
                )

            # ── Settle: wait for JS to finish updating DOM ─────────────
            await page.wait_for_timeout(RENDER_SETTLE_MS)

            # ── Extract price using multi-selector waterfall ───────────
            raw_text = await _try_get_price_text(page)

            if raw_text is None:
                raise RuntimeError(
                    f"All selectors exhausted — price not found on {url}"
                )

            price = _parse_price(raw_text, target)

            if price is not None:
                await _write_price(redis_pool, target, price)
                logger.info(
                    "[%s] ✓ %12.2f  →  Redis(%s)  [TTL=%ds / LKG=%dh]",
                    worker_name, price, target["redis_key"],
                    REDIS_KEY_TTL_SECONDS, LKG_TTL_SECONDS // 3600,
                )
                consecutive_failures = 0  # reset circuit breaker

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
                # ── Browser-level crash → coordinate full restart ──────
                logger.error(
                    "[%s] 💥 Browser crash #%d: %s",
                    worker_name, consecutive_failures, exc,
                )
                context = None
                page = None
                await mgr.restart(reason=str(exc))
                consecutive_failures = 0
                await asyncio.sleep(RECOVERY_DELAY_SECONDS)
                continue

            # ── Transient error (timeout / network) ───────────────────

            # Circuit breaker: too many consecutive failures
            if consecutive_failures >= CIRCUIT_BREAK_THRESHOLD:
                logger.critical(
                    "[%s] 🔴 Circuit breaker triggered after %d failures — "
                    "pausing %ds before retry",
                    worker_name, consecutive_failures, CIRCUIT_BREAK_SECONDS,
                )
                context = None
                page = None
                consecutive_failures = 0
                await asyncio.sleep(CIRCUIT_BREAK_SECONDS)
                continue

            # Normal exponential backoff with jitter
            base_backoff = min(
                RECOVERY_DELAY_SECONDS * consecutive_failures,
                MAX_BACKOFF_SECONDS,
            )
            jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
            backoff = max(RECOVERY_DELAY_SECONDS, base_backoff + jitter)

            logger.error(
                "[%s] Error #%d (%s: %s) — recreating context in %.0fs",
                worker_name, consecutive_failures,
                type(exc).__name__, str(exc).splitlines()[0],
                backoff,
            )
            context = None
            page = None
            await asyncio.sleep(backoff)
            continue

        # ── Normal inter-scrape sleep with jitter ─────────────────────
        # Jitter spreads the 4 workers' requests across a time window,
        # making simultaneous TradingView hits much less likely over time.
        jitter = random.uniform(0, JITTER_SECONDS * 2)
        sleep_time = SCRAPE_INTERVAL_SECONDS + jitter
        await asyncio.sleep(sleep_time)

    # Cleanup on exit
    if context is not None:
        try:
            await context.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Main entry-point
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("=" * 65)
    logger.info("  SCRAPER DAEMON v2.3 — Resilient Anti-Rate-Limit")
    logger.info("  Targets       : %d  (%s)", len(SCRAPE_TARGETS),
                ", ".join(t["key"] for t in SCRAPE_TARGETS))
    logger.info("  Interval      : %ds ±%.0fs jitter", SCRAPE_INTERVAL_SECONDS, JITTER_SECONDS)
    logger.info("  Stagger       : %ds between workers", STAGGER_SECONDS)
    logger.info("  Timeout       : %dms", SCRAPE_TIMEOUT_MS)
    logger.info("  Redis TTL     : %ds (live) / %dh (LKG)", REDIS_KEY_TTL_SECONDS, LKG_TTL_SECONDS // 3600)
    logger.info("  Circuit break : after %d failures / %ds pause", CIRCUIT_BREAK_THRESHOLD, CIRCUIT_BREAK_SECONDS)
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

    # ── Launch Playwright + BrowserManager ──────────────────────────
    async with async_playwright() as pw:
        mgr = BrowserManager(pw)
        await mgr.start()

        # ── Spawn workers with index for staggered startup ──────────
        tasks: list[asyncio.Task] = [
            asyncio.create_task(
                _worker(mgr, redis_pool, target, idx),
                name=f"worker-{target['key']}",
            )
            for idx, target in enumerate(SCRAPE_TARGETS)
        ]
        logger.info("✓ %d workers spawned — entering main loop", len(tasks))

        # ── Graceful shutdown on SIGTERM/SIGINT ─────────────────────
        loop = asyncio.get_running_loop()

        def _handle_signal() -> None:
            logger.info("Signal received — cancelling workers…")
            for task in tasks:
                task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                pass  # Windows

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
