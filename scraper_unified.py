"""
Scraper Unified — All Targets in One Browser (Low-Memory Edition)
================================================================
Runs ALL scraping targets (gold, silver, copper, usdidr) in a SINGLE
Chromium instance, scraping them SEQUENTIALLY. This is designed for
low-resource VPS environments (1 core / 1GB RAM).

Key design decisions for low-memory operation:
  • ONE Chromium instance shared across all targets
  • SEQUENTIAL scraping — only one page open at a time
  • Page is CLOSED after each target, not reused
  • Periodic full browser restart to prevent memory creep
  • Longer intervals between scrapes to reduce CPU pressure

Architecture:
    python scraper_unified.py
    → Loops through [gold, silver, copper, usdidr] one by one
    → Opens page → scrapes → closes page → next target
    → After all targets done, sleeps for SCRAPE_INTERVAL_SECONDS
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
    SCRAPE_TARGETS,
)

logger = logging.getLogger("scraper_unified")

# ── Chromium launch args (aggressive memory savings) ─────────────────
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
    # Extra memory-saving flags for low-RAM VPS
    "--js-flags=--max-old-space-size=128",
    "--disable-features=AudioServiceOutOfProcess",
    "--disable-features=IsolateOrigins",
    "--disable-site-isolation-trials",
    "--renderer-process-limit=2",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
]


# ──────────────────────────────────────────────────────────────────────
# Price extraction helpers (same as scraper_daemon.py)
# ──────────────────────────────────────────────────────────────────────

def _parse_price(raw_text: str, target: dict) -> float | None:
    """Parse raw text into a validated float price."""
    try:
        cleaned = raw_text.replace(",", "").replace("$", "").strip()
        if not cleaned:
            return None

        if (
            target["source"] == "tradingview"
            and target["type"] == "metal"
            and "." not in cleaned
            and len(cleaned) > 3
        ):
            cleaned = cleaned[:-2] + "." + cleaned[-2:]

        value = float(cleaned)

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
# Fallback selectors (same as scraper_daemon.py)
# ──────────────────────────────────────────────────────────────────────

KITCO_FALLBACK_SELECTORS = [
    "xpath=//h2[contains(text(),'Live')][contains(text(),'Price')]/following-sibling::h3[1]",
    "h3.tracking-\\[1px\\]",
    "h3.font-bold.text-4xl",
    "h3.font-mulish",
    "h3.font-bold.leading-normal",
]

_KITCO_JS_EXTRACT = """
() => {
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
    const h3s = document.querySelectorAll('h3');
    for (const h3 of h3s) {
        const t = h3.innerText.trim();
        if (t && /^[\\d,.]+$/.test(t) && t.length < 15) return t;
    }
    return null;
}
"""

TRADINGVIEW_FALLBACK_SELECTORS = [
    "span.last-zoF9r75I",
    "span[data-qa-id='symbol-last-value']",
    "span[class*='last-']",
]

_TV_JS_EXTRACT = """
() => {
    const spans = document.querySelectorAll('span[class*="last-"]');
    for (const s of spans) {
        const t = s.innerText.trim();
        if (t && /\\d/.test(t) && t.length < 20) return t;
    }
    const qa = document.querySelector('span[data-qa-id="symbol-last-value"]');
    if (qa) return qa.innerText.trim();
    return null;
}
"""


async def _try_extract_price(page: Page, target: dict) -> str | None:
    """Try multiple selectors to extract price text from the page."""
    source = target["source"]
    selectors = (
        KITCO_FALLBACK_SELECTORS if source == "kitco"
        else TRADINGVIEW_FALLBACK_SELECTORS
    )

    for i, selector in enumerate(selectors):
        try:
            timeout = 15000 if i == 0 else 5000
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout)
            text = await locator.inner_text(timeout=5000)
            text = text.strip()
            if text and any(c.isdigit() for c in text):
                return text
        except Exception:
            continue

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
# Scrape a single target (open page → extract → close page)
# ──────────────────────────────────────────────────────────────────────

async def _scrape_one_target(
    browser: Browser,
    redis_pool: aioredis.Redis,
    target: dict,
) -> bool:
    """
    Scrape a single target in an isolated context.
    Opens a new context+page, scrapes, then closes everything.
    Returns True on success, False on failure.
    """
    worker_name = target["name"]
    context = None
    page = None

    try:
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            bypass_csp=True,
        )
        context.set_default_timeout(SCRAPE_TIMEOUT_MS)

        page = await context.new_page()

        # Block heavy resources to save memory & bandwidth
        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4,webm,webp,ico}",
            lambda route: route.abort(),
        )

        # Navigate
        logger.info(f"[{worker_name}] Navigating to {target['url']}")
        await page.goto(
            target["url"],
            wait_until="domcontentloaded",
            timeout=SCRAPE_TIMEOUT_MS + 15000,
        )

        # Wait for JS rendering
        js_wait = 8000 if target["source"] == "tradingview" else 3000
        await page.wait_for_timeout(js_wait)

        # Extract price
        raw_text = await _try_extract_price(page, target)
        if raw_text is None:
            logger.warning(f"[{worker_name}] No price element found")
            return False

        price = _parse_price(raw_text, target)
        if price is None:
            logger.warning(
                f"[{worker_name}] Extracted '{raw_text}' "
                f"could not be parsed into a valid price"
            )
            return False

        # Write to Redis
        payload = json.dumps({
            "price": price,
            "source": "Kitco" if target["source"] == "kitco" else "TradingView",
            "unit": target["unit"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        await redis_pool.set(target["redis_key"], payload)
        logger.info(f"[{worker_name}] ✓ {price:>12,.4f}  →  Redis({target['redis_key']})")
        return True

    except Exception as exc:
        logger.error(
            f"[{worker_name}] Scrape error: "
            f"{type(exc).__name__}: {exc}"
        )
        return False

    finally:
        # CRITICAL: Always close context+page to free memory
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────
# Main loop — sequential round-robin through all targets
# ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("=" * 65)
    logger.info("  SCRAPER UNIFIED — Low-Memory Sequential Mode")
    logger.info(f"  Targets     : {', '.join(t['name'] for t in SCRAPE_TARGETS)}")
    logger.info(f"  Interval    : {SCRAPE_INTERVAL_SECONDS}s (between full rounds)")
    logger.info(f"  Timeout     : {SCRAPE_TIMEOUT_MS}ms")
    logger.info(f"  Redis       : {REDIS_URL}")
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

    # Browser lifecycle
    BROWSER_RESTART_ROUNDS = 10  # Restart browser every N full rounds
    round_count = 0

    pw = None
    browser = None

    try:
        while True:
            # Launch or restart browser
            if browser is None or not browser.is_connected() or round_count % BROWSER_RESTART_ROUNDS == 0:
                if round_count > 0:
                    logger.info(
                        f"🔄 Restarting browser (round {round_count}, "
                        f"periodic cleanup)"
                    )

                # Shutdown old browser + Playwright
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                if pw is not None:
                    try:
                        await pw.stop()
                    except Exception:
                        pass

                pw = await async_playwright().start()
                browser = await pw.chromium.launch(
                    headless=True,
                    args=CHROMIUM_ARGS,
                )
                logger.info("✓ Chromium launched (single instance for all targets)")

            # Scrape each target sequentially
            success_count = 0
            for target in SCRAPE_TARGETS:
                ok = await _scrape_one_target(browser, redis_pool, target)
                if ok:
                    success_count += 1

                # Small delay between targets to let memory settle
                await asyncio.sleep(2)

            round_count += 1
            logger.info(
                f"── Round {round_count} complete: "
                f"{success_count}/{len(SCRAPE_TARGETS)} targets OK ──"
            )

            # Sleep before next round
            await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        logger.info("Daemon received cancellation signal")
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
        if redis_pool is not None:
            await redis_pool.aclose()
        logger.info("✓ Unified daemon shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
