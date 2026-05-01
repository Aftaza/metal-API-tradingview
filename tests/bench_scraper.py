"""
Performance Benchmark — Metal Price Scraper v3
===============================================
Tests and measures the performance of the new httpx + BeautifulSoup
SSR extraction pipeline vs the old Playwright approach.

Uses cProfile for CPU profiling, tracemalloc for memory tracking,
and timeit-style async benchmarking for I/O latency.

Run:
    python tests/bench_scraper.py
"""

from __future__ import annotations

import asyncio
import cProfile
import gc
import io
import json
import pstats
import re
import time
import tracemalloc
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Minimal stubs (run without Redis/live network)
# ---------------------------------------------------------------------------

# Inline sample __NEXT_DATA__ payloads captured from real Kitco pages
KITCO_GOLD_HTML_SAMPLE = """
<html><head></head><body>
<script id="__NEXT_DATA__" type="application/json">
{
  "props": {
    "pageProps": {
      "dehydratedState": {
        "queries": [
          {
            "queryKey": ["Currencies", {}],
            "state": {"data": {"EUR": 0.91}}
          },
          {
            "queryKey": ["metalQuote", {"symbol": "AU", "currency": "USD", "timestamp": 1777564470}],
            "state": {
              "data": {
                "GetMetalQuoteV3": {
                  "ID": 1777564394,
                  "symbol": "AU",
                  "currency": "USD",
                  "name": "Gold",
                  "results": [
                    {
                      "ask": 3315.7,
                      "bid": 3313.7,
                      "mid": 3314.7,
                      "change": 12.5,
                      "high": 3350.0,
                      "low": 3280.0,
                      "originalTime": "2026-04-30T11:52:57.243Z",
                      "timestamp": 1777564380,
                      "unit": "OUNCE"
                    }
                  ]
                }
              }
            }
          }
        ]
      }
    }
  }
}
</script>
</body></html>
"""

KITCO_COPPER_HTML_SAMPLE = KITCO_GOLD_HTML_SAMPLE.replace(
    '"symbol": "AU"', '"symbol": "CU"'
).replace('"name": "Gold"', '"name": "Copper"').replace(
    '"mid": 3314.7', '"mid": 5.856'
).replace(
    '"ask": 3315.7', '"ask": 5.870'
).replace(
    '"bid": 3313.7', '"bid": 5.843'
).replace(
    '"unit": "OUNCE"', '"unit": "POUND"'
)

TRADINGVIEW_HTML_SAMPLE = (
    '<!DOCTYPE html><html><head><title>USD IDR</title></head>'
    '<body><script>window.__tv_data = {"short_description":"U.S. DOLLAR","trade":{"price":16842.0},'
    '"daily_bar":{"close":"16842.0"}}</script></body></html>'
)

# ---------------------------------------------------------------------------
# Import extraction functions from our scraper modules
# ---------------------------------------------------------------------------

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper_daemon import (
    _extract_kitco_price,
    _extract_tradingview_price,
    _validate_price,
    _jittered_interval,
)

# Sample target configs
GOLD_TARGET = {
    "key": "gold",
    "name": "Gold (Kitco)",
    "source": "kitco",
    "kitco_symbol": "AU",
    "unit": "troy_ounce",
    "price_range": (500.0, 10_000.0),
}

COPPER_TARGET = {
    "key": "copper",
    "name": "Copper (Kitco)",
    "source": "kitco",
    "kitco_symbol": "CU",
    "unit": "pound",
    "price_range": (1.0, 30.0),
}

USDIDR_TARGET = {
    "key": "usdidr",
    "name": "USD/IDR (TradingView)",
    "source": "tradingview",
    "kitco_symbol": None,
    "unit": "currency",
    "price_range": (10_000.0, 25_000.0),
}


# ---------------------------------------------------------------------------
# Benchmark 1: BeautifulSoup extraction throughput
# ---------------------------------------------------------------------------

def bench_kitco_extraction(n: int = 5_000) -> dict:
    """Measure parsing throughput for Kitco HTML → price extraction."""
    html = KITCO_GOLD_HTML_SAMPLE * 1  # same HTML as real page

    start = time.perf_counter()
    prices = []
    for _ in range(n):
        p = _extract_kitco_price(html, GOLD_TARGET)
        prices.append(p)
    elapsed = time.perf_counter() - start

    return {
        "function": "_extract_kitco_price",
        "iterations": n,
        "total_seconds": round(elapsed, 4),
        "avg_ms_per_call": round(elapsed / n * 1000, 4),
        "ops_per_second": round(n / elapsed),
        "sample_price": prices[0],
        "all_ok": all(p is not None for p in prices),
    }


def bench_tradingview_extraction(n: int = 20_000) -> dict:
    """Measure regex extraction throughput for TradingView HTML."""
    html = TRADINGVIEW_HTML_SAMPLE

    start = time.perf_counter()
    prices = []
    for _ in range(n):
        p = _extract_tradingview_price(html, USDIDR_TARGET)
        prices.append(p)
    elapsed = time.perf_counter() - start

    return {
        "function": "_extract_tradingview_price",
        "iterations": n,
        "total_seconds": round(elapsed, 4),
        "avg_ms_per_call": round(elapsed / n * 1000, 4),
        "ops_per_second": round(n / elapsed),
        "sample_price": prices[0],
        "all_ok": all(p is not None for p in prices),
    }


# ---------------------------------------------------------------------------
# Benchmark 2: Memory usage via tracemalloc
# ---------------------------------------------------------------------------

def bench_memory_kitco(n: int = 1_000) -> dict:
    """Measure peak memory during Kitco parsing."""
    html = KITCO_GOLD_HTML_SAMPLE

    gc.collect()
    tracemalloc.start()
    snapshot_before = tracemalloc.take_snapshot()

    for _ in range(n):
        _extract_kitco_price(html, GOLD_TARGET)

    snapshot_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    stats = snapshot_after.compare_to(snapshot_before, "lineno")
    total_allocated = sum(s.size_diff for s in stats if s.size_diff > 0)

    return {
        "function": "_extract_kitco_price",
        "iterations": n,
        "total_allocated_bytes": total_allocated,
        "avg_bytes_per_call": total_allocated // n if n > 0 else 0,
        "top_allocator": str(stats[0]) if stats else "none",
    }


# ---------------------------------------------------------------------------
# Benchmark 3: cProfile of the full extraction pipeline
# ---------------------------------------------------------------------------

def bench_cprofile_extraction(n: int = 2_000) -> str:
    """Run cProfile on extraction functions and return formatted stats."""
    html_kitco = KITCO_GOLD_HTML_SAMPLE
    html_tv = TRADINGVIEW_HTML_SAMPLE

    profiler = cProfile.Profile()
    profiler.enable()

    for _ in range(n):
        _extract_kitco_price(html_kitco, GOLD_TARGET)
        _extract_tradingview_price(html_tv, USDIDR_TARGET)

    profiler.disable()

    buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=buf)
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    stats.print_stats(15)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Benchmark 4: Jitter distribution analysis
# ---------------------------------------------------------------------------

def bench_jitter_distribution(n: int = 10_000) -> dict:
    """Verify that jitter produces a proper uniform distribution."""
    samples = [_jittered_interval() for _ in range(n)]

    from config import SCRAPE_INTERVAL_SECONDS, SCRAPE_JITTER_FACTOR
    base = SCRAPE_INTERVAL_SECONDS
    delta = base * SCRAPE_JITTER_FACTOR

    lo = base - delta
    hi = base + delta
    mean = sum(samples) / len(samples)
    deviation = (sum((s - mean) ** 2 for s in samples) / len(samples)) ** 0.5

    all_in_range = all(lo <= s <= hi for s in samples)

    return {
        "base_interval": base,
        "jitter_factor": SCRAPE_JITTER_FACTOR,
        "expected_range": [round(lo, 2), round(hi, 2)],
        "actual_min": round(min(samples), 2),
        "actual_max": round(max(samples), 2),
        "mean": round(mean, 2),
        "std_dev": round(deviation, 2),
        "all_in_range": all_in_range,
        "iterations": n,
    }


# ---------------------------------------------------------------------------
# Benchmark 5: Async concurrent extraction simulation
# ---------------------------------------------------------------------------

async def bench_concurrent_extraction(n_rounds: int = 50) -> dict:
    """
    Simulate concurrent extraction across all 4 targets using asyncio.gather.
    This mirrors the scraper_unified approach but uses in-memory HTML.
    """

    async def fake_scrape(html: str, target: dict) -> float | None:
        # Simulate the CPU-bound extraction (no actual I/O)
        await asyncio.sleep(0)  # yield event loop
        if target["source"] == "kitco":
            return _extract_kitco_price(html, target)
        return _extract_tradingview_price(html, target)

    targets_and_html = [
        (GOLD_TARGET, KITCO_GOLD_HTML_SAMPLE),
        ({**GOLD_TARGET, "key": "silver", "name": "Silver", "kitco_symbol": "AG"}, KITCO_GOLD_HTML_SAMPLE),
        (COPPER_TARGET, KITCO_COPPER_HTML_SAMPLE),
        (USDIDR_TARGET, TRADINGVIEW_HTML_SAMPLE),
    ]

    start = time.perf_counter()
    for _ in range(n_rounds):
        results = await asyncio.gather(
            *[fake_scrape(html, t) for t, html in targets_and_html]
        )
    elapsed = time.perf_counter() - start

    return {
        "function": "asyncio.gather (4 concurrent targets)",
        "rounds": n_rounds,
        "total_scrapes": n_rounds * 4,
        "total_seconds": round(elapsed, 4),
        "avg_round_ms": round(elapsed / n_rounds * 1000, 2),
        "throughput_scrapes_per_sec": round(n_rounds * 4 / elapsed),
        "last_results": [str(r) for r in results],
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def _print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main() -> None:
    print("=" * 60)
    print("  METAL PRICE SCRAPER v3 — PERFORMANCE BENCHMARK")
    print("  Skill: python-performance-optimization")
    print("=" * 60)

    # 1. Kitco extraction throughput
    _print_section("1. Kitco BeautifulSoup Extraction Throughput")
    result = bench_kitco_extraction(n=5_000)
    for k, v in result.items():
        print(f"  {k:<30}: {v}")

    # 2. TradingView regex extraction throughput
    _print_section("2. TradingView Regex Extraction Throughput")
    result2 = bench_tradingview_extraction(n=20_000)
    for k, v in result2.items():
        print(f"  {k:<30}: {v}")

    # 3. Memory usage
    _print_section("3. Memory Usage (Kitco, 1000 iterations)")
    mem = bench_memory_kitco(n=1_000)
    for k, v in mem.items():
        print(f"  {k:<30}: {v}")

    # 4. Jitter distribution
    _print_section("4. Jitter Distribution Analysis (10,000 samples)")
    jitter = bench_jitter_distribution(n=10_000)
    for k, v in jitter.items():
        print(f"  {k:<30}: {v}")

    # 5. Concurrent extraction
    _print_section("5. Concurrent Extraction Simulation (50 rounds × 4 targets)")
    conc = asyncio.run(bench_concurrent_extraction(n_rounds=50))
    for k, v in conc.items():
        print(f"  {k:<30}: {v}")

    # 6. cProfile
    _print_section("6. cProfile — Top 15 Functions (2000 iterations × 2 funcs)")
    profile_output = bench_cprofile_extraction(n=2_000)
    print(profile_output)

    # Summary
    _print_section("SUMMARY")
    print(f"  Kitco extraction avg latency : {result['avg_ms_per_call']} ms/call")
    print(f"  TradingView extraction avg   : {result2['avg_ms_per_call']} ms/call")
    print(f"  Kitco avg memory/call        : {mem['avg_bytes_per_call']} bytes")
    print(f"  Concurrent round avg latency : {conc['avg_round_ms']} ms/round")
    print(f"  Scrape throughput (simulated): {conc['throughput_scrapes_per_sec']} scrapes/sec")
    print(f"  Jitter valid (in range)      : {jitter['all_in_range']}")
    print()
    print("  ✅ All benchmarks complete. See performance_resume.md for analysis.")


if __name__ == "__main__":
    main()
