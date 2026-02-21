"""
Metal Price REST API v2 — Ultra-Fast Redis-Only
=================================================
FastAPI application that reads the latest prices directly from Redis.
Zero scraping logic — all data comes from the scraper daemon.

Endpoints:
    GET  /            — API info
    GET  /health      — Redis connectivity + key freshness
    GET  /prices      — All metal prices + USDIDR exchange rate
    GET  /prices/{metal}?gram=N&currency=USD|IDR — Single metal with gram conversion
    GET  /exchange-rate — Current USDIDR rate
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import (
    REDIS_URL,
    TROY_OUNCE_TO_GRAM,
    SCRAPE_TARGETS,
    METAL_TARGETS,
    METAL_KEYS,
)

logger = logging.getLogger("api")

# ──────────────────────────────────────────────────────────────────────
# Pydantic response models
# ──────────────────────────────────────────────────────────────────────

class MetalPrice(BaseModel):
    metal: str
    price_usd: float
    price_per_gram_usd: float
    price_per_gram_idr: Optional[float] = None
    currency: str = "USD"
    timestamp: str
    source: str = "TradingView"


class MetalPriceResponse(BaseModel):
    status: str
    data: list[MetalPrice]
    exchange_rate_usdidr: Optional[float] = None
    last_updated: str


class MetalPriceWithGram(BaseModel):
    metal: str
    gram: float
    price_per_troy_ounce_usd: float
    price_per_gram_usd: float
    total_price_usd: float
    price_per_gram_idr: Optional[float] = None
    total_price_idr: Optional[float] = None
    currency: str
    exchange_rate: Optional[float] = None
    timestamp: str
    source: str = "TradingView"
    conversion_info: dict


# ──────────────────────────────────────────────────────────────────────
# Redis pool (module-level, initialised in lifespan)
# ──────────────────────────────────────────────────────────────────────

redis_pool: aioredis.Redis | None = None


async def _read_redis_key(key: str) -> dict | None:
    """Read and deserialise a single Redis key."""
    raw = await redis_pool.get(key)  # type: ignore[union-attr]
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def _read_all_prices() -> tuple[dict[str, dict], dict | None]:
    """
    Batch-read all price keys from Redis.
    Returns: (metal_prices_dict, usdidr_data_or_None)
    """
    keys = [t["redis_key"] for t in SCRAPE_TARGETS]
    values = await redis_pool.mget(keys)  # type: ignore[union-attr]

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
            metal_prices[target["key"]] = data

    return metal_prices, usdidr_data


# ──────────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────────

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

    # Wait until Redis is reachable
    for attempt in range(30):
        try:
            await redis_pool.ping()
            logger.info("✓ Redis connected")
            break
        except Exception:
            logger.warning(f"Redis not ready (attempt {attempt + 1}/30)…")
            import asyncio
            await asyncio.sleep(2)
    else:
        raise RuntimeError("Could not connect to Redis after 30 attempts")

    yield

    await redis_pool.aclose()
    logger.info("✓ Redis connection closed")


# ──────────────────────────────────────────────────────────────────────
# FastAPI application
# ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Metal Price API v2",
    description="Real-time Metal Prices — Redis Stream Processing",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
async def root():
    return {
        "name": "Metal Price API v2",
        "version": "2.0.0",
        "architecture": "Pure Stream Processing + Redis In-Memory",
        "source": "TradingView (Playwright Async Scraper Daemon)",
        "metals": METAL_KEYS,
        "endpoints": {
            "GET /": "This endpoint",
            "GET /prices": "All metal prices with USDIDR and IDR conversion",
            "GET /prices/{metal}?gram=N&currency=USD|IDR": "Single metal with gram conversion",
            "GET /exchange-rate": "Current USDIDR exchange rate",
            "GET /health": "Health check",
        },
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check — verifies Redis connectivity and data freshness."""
    try:
        await redis_pool.ping()  # type: ignore[union-attr]
        redis_ok = True
    except Exception:
        redis_ok = False

    metal_prices, usdidr_data = await _read_all_prices()

    return {
        "status": "healthy" if redis_ok and len(metal_prices) > 0 else "degraded",
        "redis_connected": redis_ok,
        "metals_available": list(metal_prices.keys()),
        "metals_count": len(metal_prices),
        "usdidr_available": usdidr_data is not None,
    }


@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices():
    """
    Get all metal prices with USDIDR exchange rate and IDR conversion.

    Data is read directly from Redis (sub-millisecond).
    """
    metal_prices, usdidr_data = await _read_all_prices()

    if not metal_prices:
        raise HTTPException(
            status_code=503,
            detail="No metal data available yet. Scraper daemon may still be starting.",
        )

    usdidr_rate: float | None = usdidr_data["price"] if usdidr_data else None
    now_iso = datetime.now(timezone.utc).isoformat()

    prices: list[MetalPrice] = []
    latest_ts = ""

    for target in METAL_TARGETS:
        key = target["key"]
        data = metal_prices.get(key)
        if data is None:
            continue

        price_usd = data["price"]
        price_per_gram_usd = price_usd / TROY_OUNCE_TO_GRAM
        price_per_gram_idr = (
            price_per_gram_usd * usdidr_rate if usdidr_rate else None
        )
        ts = data.get("updated_at", now_iso)
        if ts > latest_ts:
            latest_ts = ts

        prices.append(
            MetalPrice(
                metal=key.upper(),
                price_usd=price_usd,
                price_per_gram_usd=round(price_per_gram_usd, 4),
                price_per_gram_idr=round(price_per_gram_idr, 2) if price_per_gram_idr else None,
                currency="USD/IDR" if usdidr_rate else "USD",
                timestamp=ts,
                source="TradingView",
            )
        )

    return MetalPriceResponse(
        status="success",
        data=prices,
        exchange_rate_usdidr=round(usdidr_rate, 2) if usdidr_rate else None,
        last_updated=latest_ts or now_iso,
    )


@app.get("/prices/{metal}", response_model=MetalPriceWithGram, tags=["Prices"])
async def get_metal_price(
    metal: str,
    gram: float = Query(..., description="Weight in grams", gt=0, examples=[10.0]),
    currency: str = Query("USD", description="Output currency (USD or IDR)", pattern="^(USD|IDR)$"),
):
    """
    Get a specific metal price with gram conversion.

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

    # Read from Redis
    redis_key = f"price:{metal}"
    data = await _read_redis_key(redis_key)

    if data is None:
        raise HTTPException(
            status_code=503,
            detail=f"{metal.upper()} data not available yet",
        )

    price_per_troy_ounce = data["price"]
    price_per_gram_usd = price_per_troy_ounce / TROY_OUNCE_TO_GRAM
    total_price_usd = price_per_gram_usd * gram
    ts = data.get("updated_at", datetime.now(timezone.utc).isoformat())

    response_data: dict = {
        "metal": metal.upper(),
        "gram": gram,
        "price_per_troy_ounce_usd": round(price_per_troy_ounce, 2),
        "price_per_gram_usd": round(price_per_gram_usd, 4),
        "total_price_usd": round(total_price_usd, 2),
        "currency": "USD",
        "timestamp": ts,
        "conversion_info": {
            "troy_ounce_to_gram": TROY_OUNCE_TO_GRAM,
            "calculation_usd": f"{gram}g × ${round(price_per_gram_usd, 4)}/g = ${round(total_price_usd, 2)}",
        },
    }

    # IDR conversion
    if currency == "IDR":
        usdidr_data = await _read_redis_key("price:usdidr")

        if not usdidr_data or not usdidr_data.get("price"):
            raise HTTPException(
                status_code=503,
                detail="USDIDR exchange rate not available yet. Try again shortly.",
            )

        exchange_rate = usdidr_data["price"]
        price_per_gram_idr = price_per_gram_usd * exchange_rate
        total_price_idr = total_price_usd * exchange_rate

        response_data.update({
            "price_per_gram_idr": round(price_per_gram_idr, 2),
            "total_price_idr": round(total_price_idr, 2),
            "currency": "IDR",
            "exchange_rate": round(exchange_rate, 2),
        })
        response_data["conversion_info"].update({
            "exchange_rate_usdidr": round(exchange_rate, 2),
            "calculation_idr": (
                f"{gram}g × Rp{round(price_per_gram_idr, 2):,.0f}/g "
                f"= Rp{round(total_price_idr, 2):,.0f}"
            ),
        })

    return MetalPriceWithGram(**response_data)


@app.get("/exchange-rate", tags=["Currency"])
async def get_exchange_rate():
    """Get the current USDIDR exchange rate from Redis."""
    data = await _read_redis_key("price:usdidr")

    if not data or not data.get("price"):
        raise HTTPException(
            status_code=503,
            detail="USDIDR exchange rate not available",
        )

    return {
        "currency_pair": "USDIDR",
        "rate": round(data["price"], 2),
        "source": data.get("source", "TradingView"),
        "timestamp": data.get("updated_at", ""),
        "description": "1 USD = X IDR",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
