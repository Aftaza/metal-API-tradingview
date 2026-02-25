"""
Scraper Daemon v2-Kitco — Single-Target Worker (Robust Edition)
================================================================
Standalone worker that scrapes a SINGLE target (set via SCRAPE_TARGET
env var) and writes the latest price into Redis.

Key reliability features:
  • Persistent page — reuses same browser page across cycles
  • Smart reload — uses page.reload() between cycles, falls back to goto()
  • Aggressive selector fallback — tries primary selector then fallbacks
  • Full browser restart — if browser crashes, relaunches Playwright entirely
  • Health watchdog — if no success for 5 min, force full restart

Architecture:
    SCRAPE_TARGET=gold python scraper_daemon.py
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone

import redis.asyncio as aioredis
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

from config import (
    REDIS_URL,
    SCRAPE_INTERVAL_SECONDS,
    SCRAPE_TIMEOUT_MS,
    RECOVERY_DELAY_SECONDS,
    SCRAPE_TARGET,
    get_active_target,
)

logger = logging.getLogger("scraper_daemon")

# ── Chromium launch args ─────────────────────────────────────────────
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--disable-software-rasterizer",
    "--disable-accelerated-2d-canvas",
    "--disable-features=TranslateUI",
    # NOTE: --single-process removed — it causes instability on heavy pages
]


# ──────────────────────────────────────────────────────────────────────
# Price extraction helpers
# ──────────────────────────────────────────────────────────────────────

def _parse_price(raw_text: str, target: dict) -> float | None:
    """Parse raw text into a validated float price."""
    try:
        cleaned = raw_text.replace(",", "").replace("$", "").strip()
        if not cleaned:
            return None

        # TradingView sometimes omits the decimal dot for metals
        if (
            target["source"] == "tradingview"
            and target["type"] == "metal"
            and "." not in cleaned
            and len(cleaned) > 3
        ):
            cleaned = cleaned[:-2] + "." + cleaned[-2:]

        value = float(cleaned)

        # Range validation
        if target["type"] == "currency":
            if 10_000 < value < 25_000:
                return value
            logger.warning(f"[{target['name']}] Value {value} outside USDIDR range")
        else:
            if 0.001 < value < 100_000:
                return value
            logger.warning(f"[{target['name']}] Value {value} outside metal range")

        return None
    except (ValueError, TypeError) as exc:
        logger.error(f"[{target['name']}] Parse error: {exc}")
        return None


# ──────────────────────────────────────────────────────────────────────
# Fallback selectors (ordered by priority)
# ──────────────────────────────────────────────────────────────────────

KITCO_FALLBACK_SELECTORS = [
    # Primary: XPath — first h3 sibling after the "Live X Price" heading
    "xpath=//h2[contains(text(),'Live')][contains(text(),'Price')]/following-sibling::h3[1]",
    # Fallback 1: h3 with tracking-[1px] (Kitco's price styling)
    "h3.tracking-\\[1px\\]",
    # Fallback 2: h3 with font-bold and text-4xl (distinctive price style)
    "h3.font-bold.text-4xl",
    # Fallback 3: h3 with font-mulish class (unique to price headings)
    "h3.font-mulish",
    # Fallback 4: broader — any h3 with font-bold
    "h3.font-bold.leading-normal",
]

# JS fallback for Kitco: scan h3 elements after the "Live" heading
_KITCO_JS_EXTRACT = """
() => {
    // Strategy 1: find h2 with "Live" and "Price", then get next h3 sibling
    const h2s = document.querySelectorAll('h2');
    for (const h2 of h2s) {
        if (h2.innerText.includes('Live') && h2.innerText.includes('Price')) {
            let sibling = h2.nextElementSibling;
            while (sibling) {
                if (sibling.tagName === 'H3') {
                    const t = sibling.innerText.trim();
                    if (t && /\\d/.test(t)) return t;
                }
                sibling = sibling.nextElementSibling;
            }
        }
    }
    // Strategy 2: find first h3 whose text looks like a price
    const h3s = document.querySelectorAll('h3');
    for (const h3 of h3s) {
        const t = h3.innerText.trim();
        if (t && /^[\\d,.]+$/.test(t) && t.length < 15) return t;
    }
    return null;
}
"""


TRADINGVIEW_FALLBACK_SELECTORS = [
    # Primary: class-based selector (unique, single element)
    "span.last-zoF9r75I",
    # Fallback 1: attribute-based selector
    "span[data-qa-id='symbol-last-value']",
    # Fallback 2: partial class match (survives hash changes)
    "span[class*='last-']",
]

# JS fallback: scan the DOM for the price element when CSS selectors fail.
# TradingView renders the last price as a <span> whose class starts with "last-".
_TV_JS_EXTRACT = """
() => {
    // Strategy 1: look for span whose className contains 'last-'
    const spans = document.querySelectorAll('span[class*="last-"]');
    for (const s of spans) {
        const t = s.innerText.trim();
        if (t && /\\d/.test(t) && t.length < 20) return t;
    }
    // Strategy 2: look for data-qa-id attribute
    const qa = document.querySelector('span[data-qa-id="symbol-last-value"]');
    if (qa) return qa.innerText.trim();
    return null;
}
"""


async def _try_extract_price(page: Page, target: dict) -> str | None:
    """
    Try multiple selectors to extract price text from the page.
    Uses wait_for with a reasonable timeout for the first selector,
    then quicker checks for fallbacks. For TradingView, also tries
    a JavaScript evaluation as the last resort.
    Returns the raw text, or None if nothing found.
    """
    source = target["source"]
    selectors = (
        KITCO_FALLBACK_SELECTORS if source == "kitco"
        else TRADINGVIEW_FALLBACK_SELECTORS
    )

    for i, selector in enumerate(selectors):
        try:
            # First selector gets a longer wait (page may still be rendering)
            timeout = 15000 if i == 0 else 5000

            locator = page.locator(selector).first
            # Wait for it to be visible
            await locator.wait_for(state="visible", timeout=timeout)
            text = await locator.inner_text(timeout=5000)
            text = text.strip()
            if text and any(c.isdigit() for c in text):
                return text
        except Exception:
            continue  # try next selector

    # Last resort: JS evaluation fallback
    js_extract = None
    if source == "tradingview":
        js_extract = _TV_JS_EXTRACT
    elif source == "kitco":
        js_extract = _KITCO_JS_EXTRACT

    if js_extract:
        try:
            result = await page.evaluate(js_extract)
            if result and any(c.isdigit() for c in result):
                logger.info(f"[{target['name']}] Price found via JS fallback: {result}")
                return result
        except Exception:
            pass

    return None


# ──────────────────────────────────────────────────────────────────────
# Browser lifecycle manager
# ──────────────────────────────────────────────────────────────────────

class BrowserManager:
    """
    Manages the Playwright → Browser → Context → Page lifecycle.
    Can fully restart the browser if it crashes.
    """

    def __init__(self) -> None:
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def launch_browser(self) -> None:
        """(Re-)launch Playwright + Chromium browser."""
        # Close everything including the Playwright subprocess
        # This is critical: if the Playwright Node.js process died,
        # self.pw is stale and must be recreated
        await self.shutdown()

        self.pw = await async_playwright().start()

        self.browser = await self.pw.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS,
        )
        logger.info("✓ Chromium launched")

    async def create_page(self, target: dict) -> Page:
        """Create a fresh context + page for the target."""
        # Close old context/page
        await self.close_page()

        if self.browser is None or not self.browser.is_connected():
            logger.warning("Browser disconnected — relaunching…")
            await self.launch_browser()

        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            bypass_csp=True,
        )
        self.context.set_default_timeout(SCRAPE_TIMEOUT_MS)

        self.page = await self.context.new_page()

        # Block heavy resources
        await self.page.route(
            "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4,webm,webp,ico}",
            lambda route: route.abort(),
        )

        # Navigate — use 'domcontentloaded' for both sources.
        # TradingView's constant WebSocket traffic means 'networkidle'
        # never fires, causing guaranteed timeouts.
        logger.info(
            f"[{target['name']}] Navigating to {target['url']} "
            f"(wait_until=domcontentloaded)"
        )
        await self.page.goto(
            target["url"],
            wait_until="domcontentloaded",
            timeout=SCRAPE_TIMEOUT_MS + 15000,
        )

        # Give JS time to render the price element.
        # TradingView needs longer — price arrives via WebSocket after DOM load.
        js_wait = 8000 if target["source"] == "tradingview" else 3000
        await self.page.wait_for_timeout(js_wait)

        return self.page

    async def close_page(self) -> None:
        """Close context + page, keep browser alive."""
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None

    async def close_all(self) -> None:
        """Close everything including the browser."""
        await self.close_page()
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

    async def shutdown(self) -> None:
        """Full shutdown including Playwright process."""
        await self.close_all()
        if self.pw is not None:
            try:
                await self.pw.stop()
            except Exception:
                pass
            self.pw = None


# ──────────────────────────────────────────────────────────────────────
# Worker coroutine — single target, persistent page, auto-recovery
# ──────────────────────────────────────────────────────────────────────

async def _worker(
    bm: BrowserManager,
    redis_pool: aioredis.Redis,
    target: dict,
) -> None:
    """
    Infinite-loop worker for a single target.

    Strategy:
        1. Create a persistent page (via BrowserManager)
        2. Loop: reload → extract price → write Redis → sleep
        3. On soft failure: increment backoff, try again
        4. After MAX_CONSECUTIVE_FAILURES or HEALTH_TIMEOUT:
           full page rebuild (or browser relaunch)
    """
    worker_name = target["name"]
    redis_key = target["redis_key"]
    source = target["source"]

    logger.info(f"[{worker_name}] Worker started  →  {target['url']}")
    logger.info(f"[{worker_name}] Source: {source}")

    MAX_CONSECUTIVE_FAILURES = 5
    MAX_BACKOFF_SECONDS = 30
    HEALTH_TIMEOUT_SECONDS = 300  # 5 min
    BROWSER_RESTART_SECONDS = 600  # 10 min — periodic full Chromium restart
    PAGE_RECYCLE_CYCLES = 30       # Recreate page every N cycles (Kitco)

    consecutive_failures = 0
    last_success_time = time.monotonic()
    browser_start_time = time.monotonic()
    cycle_count = 0

    page: Page | None = None

    while True:
        # ── Health watchdog: full restart if stuck ─────────────────
        elapsed_since_success = time.monotonic() - last_success_time
        elapsed_since_browser = time.monotonic() - browser_start_time
        needs_periodic_restart = (
            elapsed_since_browser > BROWSER_RESTART_SECONDS
            and page is not None
        )

        # Page recycling for Kitco: recreate page every N cycles
        # to prevent memory accumulation from repeated goto()
        needs_page_recycle = (
            source == "kitco"
            and page is not None
            and cycle_count > 0
            and cycle_count % PAGE_RECYCLE_CYCLES == 0
        )

        needs_full_restart = (
            page is None
            or consecutive_failures >= MAX_CONSECUTIVE_FAILURES
            or elapsed_since_success > HEALTH_TIMEOUT_SECONDS
            or needs_periodic_restart
        )

        if needs_full_restart:
            try:
                if needs_periodic_restart:
                    reason = (
                        f"periodic memory cleanup "
                        f"(browser age {elapsed_since_browser:.0f}s)"
                    )
                    logger.info(
                        f"[{worker_name}] 🔄 Full browser restart "
                        f"to prevent memory leaks"
                    )
                    await bm.launch_browser()
                    browser_start_time = time.monotonic()
                elif page is None and consecutive_failures == 0:
                    reason = "initial start"
                else:
                    reason = (
                        f"after {consecutive_failures} failures / "
                        f"{elapsed_since_success:.0f}s stuck"
                    )
                    # Full browser relaunch on persistent failures
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.info(
                            f"[{worker_name}] 🔄 Full browser restart "
                            f"due to {consecutive_failures} consecutive "
                            f"failures"
                        )
                        await bm.launch_browser()
                        browser_start_time = time.monotonic()

                logger.info(
                    f"[{worker_name}] Creating fresh page ({reason})"
                )
                page = await bm.create_page(target)
                consecutive_failures = 0
                cycle_count = 0
                logger.info(f"[{worker_name}] ✓ Page ready")
            except Exception as exc:
                consecutive_failures += 1
                backoff = min(
                    RECOVERY_DELAY_SECONDS * consecutive_failures,
                    MAX_BACKOFF_SECONDS,
                )
                logger.error(
                    f"[{worker_name}] Page creation failed "
                    f"(attempt #{consecutive_failures}): "
                    f"{type(exc).__name__}: {exc} — retrying in {backoff}s"
                )
                page = None
                await asyncio.sleep(backoff)
                continue

        elif needs_page_recycle:
            try:
                logger.info(
                    f"[{worker_name}] 🔄 Recycling page "
                    f"after {cycle_count} cycles to free memory"
                )
                page = await bm.create_page(target)
                logger.info(
                    f"[{worker_name}] ✓ Page recycled successfully"
                )
            except Exception as exc:
                logger.warning(
                    f"[{worker_name}] Page recycle failed: {exc} "
                    f"— will force full restart"
                )
                page = None
                continue

        # ── Scrape cycle ──────────────────────────────────────────
        try:
            if source == "tradingview":
                # TradingView updates the price via WebSocket in real-time.
                # No need to re-navigate — just read the current DOM.
                # A small wait ensures any WebSocket update settles.
                await page.wait_for_timeout(1000)
            else:
                # Kitco is server-rendered; re-navigate to get fresh data.
                # 'commit' fires when the response headers are received
                # (faster than 'domcontentloaded' on heavy JS pages).
                try:
                    await page.goto(
                        target["url"],
                        wait_until="commit",
                        timeout=SCRAPE_TIMEOUT_MS,
                    )
                except Exception as nav_exc:
                    logger.warning(
                        f"[{worker_name}] Navigation failed "
                        f"({type(nav_exc).__name__}), will retry"
                    )
                    raise  # let outer handler manage backoff

                # Give JS time to render the price element
                await page.wait_for_timeout(3000)

            # ── Extract price ─────────────────────────────────────
            raw_text = await _try_extract_price(page, target)

            if raw_text is None:
                raise RuntimeError("No price element found with any selector")

            price = _parse_price(raw_text, target)

            if price is not None:
                payload = json.dumps({
                    "price": price,
                    "source": "Kitco" if source == "kitco" else "TradingView",
                    "unit": target["unit"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

                await redis_pool.set(redis_key, payload)
                logger.info(
                    f"[{worker_name}] ✓ {price:>12,.4f}  →  Redis({redis_key})"
                )
                consecutive_failures = 0
                last_success_time = time.monotonic()
                cycle_count += 1
            else:
                logger.warning(
                    f"[{worker_name}] Extracted '{raw_text}' "
                    f"could not be parsed into a valid price"
                )
                consecutive_failures += 1

        except asyncio.CancelledError:
            logger.info(f"[{worker_name}] Worker cancelled, shutting down")
            break

        except Exception as exc:
            consecutive_failures += 1
            backoff = min(
                RECOVERY_DELAY_SECONDS * consecutive_failures,
                MAX_BACKOFF_SECONDS,
            )
            # Check if browser crashed
            err_name = type(exc).__name__
            if "TargetClosedError" in err_name or "closed" in str(exc).lower():
                logger.error(
                    f"[{worker_name}] Browser/context crashed — "
                    f"will relaunch on next cycle"
                )
                page = None  # force full restart on next iteration
                cycle_count = 0
            else:
                logger.error(
                    f"[{worker_name}] Scrape error "
                    f"(attempt #{consecutive_failures}): "
                    f"{err_name}: {exc}  — retrying in {backoff}s"
                )
            await asyncio.sleep(backoff)
            continue  # skip normal sleep

        # Normal interval between successful scrapes
        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)


# ──────────────────────────────────────────────────────────────────────
# Main entry-point
# ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    target = get_active_target()

    if target is None:
        logger.error(
            f"SCRAPE_TARGET='{SCRAPE_TARGET}' is not valid. "
            f"Set SCRAPE_TARGET to one of: gold, silver, copper, usdidr"
        )
        sys.exit(1)

    logger.info("=" * 65)
    logger.info("  SCRAPER DAEMON v2-Kitco — Single-Target Worker (Robust)")
    logger.info(f"  Target        : {target['name']}")
    logger.info(f"  Source        : {target['source']}")
    logger.info(f"  URL           : {target['url']}")
    logger.info(f"  Selector      : {target['selector']}")
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

    # Browser manager handles browser lifecycle + auto-restart
    bm = BrowserManager()
    await bm.launch_browser()

    try:
        await _worker(bm, redis_pool, target)
    except asyncio.CancelledError:
        logger.info("Daemon received cancellation signal")
    finally:
        await bm.shutdown()
        await redis_pool.aclose()
        logger.info("✓ Daemon shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
