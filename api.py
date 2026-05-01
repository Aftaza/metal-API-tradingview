"""
Metal Price REST API v3 — Fault-Tolerant Redis-Backed FastAPI
==============================================================
Reads prices written by the httpx + BeautifulSoup scraper workers.

Fault-tolerance design:
  • Stale-data detection: warns if data is older than DATA_STALENESS_SECONDS
  • Degraded-mode responses: instead of 503, returns data with a
    `stale=true` flag and `X-Data-Staleness` header so the e-commerce
    platform can decide whether to show a warning to buyers
  • Partial data: if only some metals are available, those are returned
    and missing metals are listed under `missing_metals`
  • Explicit 503 only when ZERO data is available (complete blackout)
  • Redis reconnection: if Redis is temporarily unreachable, the API
    retries the connection and continues serving requests
  • Health endpoint exposes per-key staleness so monitoring tools
    (UptimeRobot, Grafana, etc.) can page on data freshness

Endpoints:
    GET  /            — API info
    GET  /health      — Redis + per-key freshness check
    GET  /prices      — All metal prices + USDIDR + IDR conversion
    GET  /prices/{metal}?gram=N&currency=USD|IDR — Single metal
    GET  /exchange-rate — Current USDIDR rate
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import (
    DATA_STALENESS_SECONDS,
    METAL_KEYS,
    METAL_TARGETS,
    POUND_TO_GRAM,
    REDIS_URL,
    SCRAPE_TARGETS,
    TROY_OUNCE_TO_GRAM,
)

logger = logging.getLogger("api")

# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class MetalPrice(BaseModel):
    metal: str
    price_usd: float
    price_per_gram_usd: float
    price_per_gram_idr: Optional[float] = None
    currency: str = "USD"
    timestamp: str
    source: str
    stale: bool = Field(default=False, description="True if data exceeds staleness threshold")
    age_seconds: Optional[int] = Field(
        default=None, description="Age of this data point in seconds"
    )


class MetalPriceResponse(BaseModel):
    status: str  # "success" | "degraded"
    data: list[MetalPrice]
    exchange_rate_usdidr: Optional[float] = None
    last_updated: str
    missing_metals: list[str] = Field(default_factory=list)
    exchange_rate_stale: bool = False


class MetalPriceWithGram(BaseModel):
    metal: str
    gram: float
    price_per_gram_usd: float
    total_price_usd: float
    price_per_gram_idr: Optional[float] = None
    total_price_idr: Optional[float] = None
    currency: str
    exchange_rate: Optional[float] = None
    timestamp: str
    source: str
    stale: bool = False
    age_seconds: Optional[int] = None
    conversion_info: dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _price_to_per_gram(price: float, unit: str) -> float:
    """Convert a raw price to price-per-gram based on its unit."""
    if unit == "troy_ounce":
        return price / TROY_OUNCE_TO_GRAM
    elif unit == "pound":
        return price / POUND_TO_GRAM
    return price  # currency — not applicable


def _age_seconds(iso_timestamp: str) -> int | None:
    """Return how many seconds ago `iso_timestamp` was written."""
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(timezone.utc)
        return int((now - ts).total_seconds())
    except Exception:
        return None


def _is_stale(iso_timestamp: str) -> bool:
    age = _age_seconds(iso_timestamp)
    if age is None:
        return True  # Unknown age → treat as stale
    return age > DATA_STALENESS_SECONDS


# ---------------------------------------------------------------------------
# Redis pool (module-level, initialised in lifespan)
# ---------------------------------------------------------------------------

redis_pool: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    """Return the shared Redis pool, reconnecting if needed."""
    global redis_pool
    if redis_pool is None:
        redis_pool = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
    return redis_pool


async def _read_redis_key(key: str) -> dict | None:
    """Read and deserialise a single Redis key. Returns None on any error."""
    try:
        r = await _get_redis()
        raw = await r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Corrupt JSON in Redis key {key!r}")
        return None
    except Exception as exc:
        logger.error(f"Redis read error for {key!r}: {exc}")
        return None


async def _read_all_prices() -> tuple[dict[str, dict], dict | None]:
    """
    Batch-read all price keys from Redis.

    Returns: (metal_prices_dict, usdidr_data_or_None)
    metal_prices_dict keys are target["key"] strings.
    """
    try:
        r = await _get_redis()
        keys = [t["redis_key"] for t in SCRAPE_TARGETS]
        values = await r.mget(keys)
    except Exception as exc:
        logger.error(f"Redis mget failed: {exc}")
        return {}, None

    metal_prices: dict[str, dict] = {}
    usdidr_data: dict | None = None

    for target, raw in zip(SCRAPE_TARGETS, values):
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        if target["type"] == "currency":
            usdidr_data = data
        else:
            data["_unit"] = target["unit"]
            metal_prices[target["key"]] = data

    return metal_prices, usdidr_data


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_pool
    logger.info(f"Connecting to Redis: {REDIS_URL}")

    redis_pool = aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=10,
        retry_on_timeout=True,
    )

    # Wait until Redis is reachable (up to 60 s)
    for attempt in range(30):
        try:
            await redis_pool.ping()
            logger.info("✓ Redis connected")
            break
        except Exception:
            logger.warning(f"Redis not ready (attempt {attempt + 1}/30)…")
            await asyncio.sleep(2)
    else:
        # Don't hard-crash — start in degraded mode; retry happens per-request
        logger.error("Could not reach Redis after 30 attempts — starting in degraded mode")

    yield

    if redis_pool is not None:
        await redis_pool.aclose()
    logger.info("✓ Redis connection closed")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Metal Price API v3",
    description=(
        "Real-time Metal Prices (Gold, Silver, Copper) + USDIDR Exchange Rate. "
        "Powered by httpx + BeautifulSoup SSR extraction — no browser required. "
        "Fault-tolerant: serves stale data with warnings instead of failing."
    ),
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/", tags=["Info"])
async def root():
    return {
        "name": "Metal Price API v3",
        "version": "3.0.0",
        "architecture": "httpx + BeautifulSoup SSR | Redis | FastAPI",
        "sources": {
            "gold": "Kitco (kitco.com/charts/gold) — Next.js __NEXT_DATA__",
            "silver": "Kitco (kitco.com/charts/silver) — Next.js __NEXT_DATA__",
            "copper": "Kitco (kitco.com/price/base-metals/copper) — Next.js __NEXT_DATA__",
            "usdidr": "TradingView (tradingview.com/symbols/USDIDR/) — SSR JSON",
        },
        "metals": METAL_KEYS,
        "fault_tolerance": {
            "stale_threshold_seconds": DATA_STALENESS_SECONDS,
            "behavior": (
                "Serves stale data with stale=true flag and X-Data-Staleness header. "
                "Returns 503 ONLY when zero data is available (complete blackout)."
            ),
        },
        "endpoints": {
            "GET /": "This endpoint",
            "GET /prices": "All metal prices with USDIDR and IDR conversion",
            "GET /prices/{metal}?gram=N&currency=USD|IDR": "Single metal with gram conversion",
            "GET /exchange-rate": "Current USDIDR exchange rate",
            "GET /health": "Detailed health check with per-key staleness",
        },
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """
    Detailed health check.

    Returns Redis connectivity status and per-key data freshness.
    Useful for monitoring (Grafana, UptimeRobot, etc.).
    """
    # Redis ping
    try:
        r = await _get_redis()
        await r.ping()
        redis_ok = True
    except Exception as exc:
        redis_ok = False
        logger.error(f"Health check Redis ping failed: {exc}")

    metal_prices, usdidr_data = await _read_all_prices()

    keys_status: dict[str, dict] = {}
    any_stale = False

    for target in SCRAPE_TARGETS:
        key = target["key"]
        if target["type"] == "metal":
            data = metal_prices.get(key)
        else:
            data = usdidr_data

        if data is None:
            keys_status[key] = {"available": False, "stale": True, "age_seconds": None}
            any_stale = True
            continue

        ts = data.get("updated_at", "")
        age = _age_seconds(ts)
        stale = _is_stale(ts)
        if stale:
            any_stale = True
        keys_status[key] = {
            "available": True,
            "stale": stale,
            "age_seconds": age,
            "price": data.get("price"),
            "updated_at": ts,
        }

    total_available = sum(1 for v in keys_status.values() if v["available"])
    status = (
        "healthy" if redis_ok and total_available == len(SCRAPE_TARGETS) and not any_stale
        else "degraded" if total_available > 0
        else "unavailable"
    )

    return {
        "status": status,
        "redis_connected": redis_ok,
        "data_keys": keys_status,
        "available_count": total_available,
        "total_count": len(SCRAPE_TARGETS),
        "staleness_threshold_seconds": DATA_STALENESS_SECONDS,
    }


@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices(response: Response):
    """
    Get all metal prices with USDIDR exchange rate and IDR conversion.

    Fault-tolerant behaviour:
      - If data is available but stale: returns it with `stale=true` and
        sets `X-Data-Staleness: <age>s` response header
      - If some metals are missing: returns available ones + `missing_metals` list
      - If ALL data is unavailable: returns HTTP 503
    """
    metal_prices, usdidr_data = await _read_all_prices()

    if not metal_prices:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_data",
                "message": (
                    "No metal price data available. "
                    "Scraper workers may still be starting or recovering."
                ),
            },
        )

    usdidr_rate: float | None = usdidr_data.get("price") if usdidr_data else None
    usdidr_ts: str = usdidr_data.get("updated_at", "") if usdidr_data else ""
    usdidr_stale = _is_stale(usdidr_ts) if usdidr_ts else True

    now_iso = datetime.now(timezone.utc).isoformat()
    prices: list[MetalPrice] = []
    missing_metals: list[str] = []
    latest_ts = ""
    overall_stale = usdidr_stale

    for target in METAL_TARGETS:
        key = target["key"]
        data = metal_prices.get(key)
        if data is None:
            missing_metals.append(key)
            continue

        ts = data.get("updated_at", now_iso)
        age = _age_seconds(ts)
        stale = _is_stale(ts)
        if stale:
            overall_stale = True
        if ts > latest_ts:
            latest_ts = ts

        price_raw: float = data["price"]
        unit = data.get("_unit", target["unit"])
        price_per_gram_usd = _price_to_per_gram(price_raw, unit)
        price_per_gram_idr = (
            round(price_per_gram_usd * usdidr_rate, 2) if usdidr_rate else None
        )

        prices.append(
            MetalPrice(
                metal=key.upper(),
                price_usd=price_raw,
                price_per_gram_usd=round(price_per_gram_usd, 4),
                price_per_gram_idr=price_per_gram_idr,
                currency="USD/IDR" if usdidr_rate else "USD",
                timestamp=ts,
                source=data.get("source", "Kitco"),
                stale=stale,
                age_seconds=age,
            )
        )

    # Set staleness header for downstream caching / e-commerce logic
    if overall_stale:
        max_age = max(
            (_age_seconds(p.timestamp) or 0) for p in prices
        ) if prices else 0
        response.headers["X-Data-Staleness"] = f"{max_age}s"
        response.headers["X-Scraper-Status"] = "recovering"
    else:
        response.headers["X-Scraper-Status"] = "live"

    return MetalPriceResponse(
        status="degraded" if (overall_stale or missing_metals) else "success",
        data=prices,
        exchange_rate_usdidr=round(usdidr_rate, 2) if usdidr_rate else None,
        last_updated=latest_ts or now_iso,
        missing_metals=missing_metals,
        exchange_rate_stale=usdidr_stale,
    )


@app.get("/prices/{metal}", response_model=MetalPriceWithGram, tags=["Prices"])
async def get_metal_price(
    metal: str,
    response: Response,
    gram: float = Query(..., description="Weight in grams", gt=0, examples=[10.0]),
    currency: str = Query(
        "USD",
        description="Output currency (USD or IDR)",
        pattern="^(USD|IDR)$",
    ),
):
    """
    Get a specific metal price with gram conversion.

    Fault-tolerant: returns stale data with `stale=true` and
    `X-Data-Staleness` header instead of failing.

    Parameters:
        metal: gold | silver | copper
        gram: weight in grams (required, > 0)
        currency: USD or IDR
    """
    metal = metal.lower()
    currency = currency.upper()

    if metal not in METAL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid metal. Available: {', '.join(METAL_KEYS)}",
        )

    target_config = next(t for t in METAL_TARGETS if t["key"] == metal)
    data = await _read_redis_key(f"price:{metal}")

    if data is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_data",
                "message": (
                    f"{metal.upper()} price is not available yet. "
                    "The scraper may still be initialising or recovering."
                ),
            },
        )

    ts = data.get("updated_at", datetime.now(timezone.utc).isoformat())
    age = _age_seconds(ts)
    stale = _is_stale(ts)

    if stale:
        response.headers["X-Data-Staleness"] = f"{age}s"
        response.headers["X-Scraper-Status"] = "recovering"
    else:
        response.headers["X-Scraper-Status"] = "live"

    price_raw: float = data["price"]
    unit = data.get("unit", target_config["unit"])
    price_per_gram_usd = _price_to_per_gram(price_raw, unit)
    total_price_usd = price_per_gram_usd * gram

    response_data: dict = {
        "metal": metal.upper(),
        "gram": gram,
        "price_per_gram_usd": round(price_per_gram_usd, 4),
        "total_price_usd": round(total_price_usd, 2),
        "currency": "USD",
        "timestamp": ts,
        "source": data.get("source", "Kitco"),
        "stale": stale,
        "age_seconds": age,
        "conversion_info": {
            "original_price": price_raw,
            "original_unit": unit,
            "calculation_usd": (
                f"{gram}g × ${round(price_per_gram_usd, 4)}/g "
                f"= ${round(total_price_usd, 2)}"
            ),
        },
    }

    if currency == "IDR":
        usdidr_data = await _read_redis_key("price:usdidr")

        if not usdidr_data or not usdidr_data.get("price"):
            # Degraded IDR: return USD with a warning rather than failing
            response_data["conversion_info"]["warning"] = (
                "USDIDR rate unavailable — IDR conversion not possible"
            )
        else:
            exchange_rate: float = usdidr_data["price"]
            price_per_gram_idr = price_per_gram_usd * exchange_rate
            total_price_idr = total_price_usd * exchange_rate
            usdidr_stale = _is_stale(usdidr_data.get("updated_at", ""))

            response_data.update(
                {
                    "price_per_gram_idr": round(price_per_gram_idr, 2),
                    "total_price_idr": round(total_price_idr, 2),
                    "currency": "IDR",
                    "exchange_rate": round(exchange_rate, 2),
                }
            )
            response_data["conversion_info"].update(
                {
                    "exchange_rate_usdidr": round(exchange_rate, 2),
                    "exchange_rate_stale": usdidr_stale,
                    "calculation_idr": (
                        f"{gram}g × Rp{round(price_per_gram_idr, 2):,.0f}/g "
                        f"= Rp{round(total_price_idr, 2):,.0f}"
                    ),
                }
            )

    return MetalPriceWithGram(**response_data)


@app.get("/exchange-rate", tags=["Currency"])
async def get_exchange_rate(response: Response):
    """Get the current USDIDR exchange rate from Redis."""
    data = await _read_redis_key("price:usdidr")

    if not data or not data.get("price"):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_data",
                "message": "USDIDR exchange rate not available",
            },
        )

    ts = data.get("updated_at", "")
    age = _age_seconds(ts)
    stale = _is_stale(ts)

    if stale:
        response.headers["X-Data-Staleness"] = f"{age}s"
        response.headers["X-Scraper-Status"] = "recovering"
    else:
        response.headers["X-Scraper-Status"] = "live"

    return {
        "currency_pair": "USDIDR",
        "rate": round(data["price"], 2),
        "source": data.get("source", "TradingView"),
        "timestamp": ts,
        "age_seconds": age,
        "stale": stale,
        "description": "1 USD = X IDR",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
