"""
Metal Price REST API v2.1 — Production-Ready
=============================================
FastAPI application that reads latest prices directly from Redis.
Zero scraping logic — all data comes from the scraper daemon.

Design decisions:
  • Redis pool injected via module-level init in lifespan (not global None)
  • Type-safe helper functions with explicit None guards
  • CORS origins configurable via ALLOWED_ORIGINS env variable
  • Structured response models with Pydantic v2
  • Redis TTL awareness: returns 503 if key is missing (expired or not yet written)

Endpoints:
    GET  /              — API info
    GET  /health        — Redis connectivity + data freshness
    GET  /prices        — All metal prices + USDIDR exchange rate
    GET  /prices/{metal}?gram=N&currency=USD|IDR — Single metal with gram conversion
    GET  /exchange-rate — Current USDIDR rate
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Optional

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import (
    REDIS_URL,
    TROY_OUNCE_TO_GRAM,
    SCRAPE_TARGETS,
    METAL_TARGETS,
    METAL_KEYS,
    ALLOWED_ORIGINS,
    setup_logging,
)

logger = setup_logging("api")


# ──────────────────────────────────────────────────────────────────────
# Pydantic response models (Pydantic v2 style)
# ──────────────────────────────────────────────────────────────────────

class MetalPrice(BaseModel):
    metal: str
    price_usd: float = Field(description="Price per troy ounce in USD")
    price_per_gram_usd: float = Field(description="Price per gram in USD")
    price_per_gram_idr: Optional[float] = Field(
        default=None, description="Price per gram in IDR (if exchange rate available)"
    )
    currency: str = "USD"
    timestamp: str
    source: str = "TradingView"


class MetalPriceResponse(BaseModel):
    status: str
    data: list[MetalPrice]
    exchange_rate_usdidr: Optional[float] = Field(
        default=None, description="1 USD = X IDR"
    )
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


class ExchangeRateResponse(BaseModel):
    currency_pair: str
    rate: float
    source: str
    timestamp: str
    description: str


class HealthResponse(BaseModel):
    status: str
    redis_connected: bool
    metals_available: list[str]
    metals_count: int
    usdidr_available: bool
    data_freshness: dict[str, str]


# ──────────────────────────────────────────────────────────────────────
# Redis connection — managed via lifespan, never a bare global None
# ──────────────────────────────────────────────────────────────────────

class _RedisState:
    """Container for the Redis pool so it's never a bare module-level None."""
    pool: aioredis.Redis | None = None

    def get(self) -> aioredis.Redis:
        """Return the active pool or raise a clear 503."""
        if self.pool is None:
            raise HTTPException(
                status_code=503,
                detail="Redis connection not initialised. Service may be starting.",
            )
        return self.pool


_redis = _RedisState()


def get_redis() -> aioredis.Redis:
    """FastAPI dependency: returns the active Redis connection."""
    return _redis.get()


# Type alias for annotated dependency injection
RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


# ──────────────────────────────────────────────────────────────────────
# Redis helper functions
# ──────────────────────────────────────────────────────────────────────

async def _read_key(redis: aioredis.Redis, key: str) -> dict | None:
    """Read and deserialise a single Redis key. Returns None if missing/invalid."""
    raw: str | None = await redis.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to deserialise Redis key '%s': %r", key, raw)
        return None


async def _read_all_prices(
    redis: aioredis.Redis,
) -> tuple[dict[str, dict], dict | None]:
    """
    Batch-read all price keys from Redis using MGET (single round-trip).
    Returns: (metal_prices_dict, usdidr_data_or_None)
    """
    keys = [t["redis_key"] for t in SCRAPE_TARGETS]
    values: list[str | None] = await redis.mget(keys)

    metal_prices: dict[str, dict] = {}
    usdidr_data: dict | None = None

    for target, raw in zip(SCRAPE_TARGETS, values):
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Could not parse Redis value for key '%s'", target["redis_key"])
            continue

        if target["type"] == "currency":
            usdidr_data = data
        else:
            metal_prices[target["key"]] = data

    return metal_prices, usdidr_data


# ──────────────────────────────────────────────────────────────────────
# Lifespan — connect / disconnect Redis pool
# ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Connecting to Redis: %s", REDIS_URL)

    pool = aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=10,
        retry_on_timeout=True,
        health_check_interval=30,  # keep-alive
    )

    # Wait until Redis is reachable (max 60s = 30 × 2s)
    for attempt in range(30):
        try:
            await pool.ping()
            logger.info("✓ Redis connected on attempt %d", attempt + 1)
            break
        except Exception as exc:
            logger.warning("Redis not ready (attempt %d/30): %s", attempt + 1, exc)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("Could not connect to Redis after 30 attempts (60s)")

    _redis.pool = pool

    yield  # Application runs here

    await pool.aclose()
    _redis.pool = None
    logger.info("✓ Redis connection closed")


# ──────────────────────────────────────────────────────────────────────
# FastAPI application
# ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Metal Price API",
    description=(
        "Real-time Gold, Silver & Copper prices with USD→IDR conversion.\n\n"
        "Data sourced from TradingView via async Playwright scraper daemon. "
        "All reads are sub-millisecond Redis lookups."
    ),
    version="2.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # allow_credentials must be False when allow_origins=["*"] per CORS spec
    allow_credentials=ALLOWED_ORIGINS != ["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────
# Exception handlers
# ──────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": "An unexpected error occurred."},
    )


# ──────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
async def root() -> dict:
    """API information and available endpoints."""
    return {
        "name": "Metal Price API",
        "version": "2.1.0",
        "architecture": "Async Playwright Scraper → Redis → FastAPI",
        "source": "TradingView",
        "metals": METAL_KEYS,
        "endpoints": {
            "GET /": "This endpoint",
            "GET /prices": "All metal prices with USDIDR and IDR conversion",
            "GET /prices/{metal}?gram=N&currency=USD|IDR": "Single metal with gram conversion",
            "GET /exchange-rate": "Current USDIDR exchange rate",
            "GET /health": "Health check with data freshness info",
            "GET /docs": "Interactive API documentation (Swagger UI)",
            "GET /redoc": "Alternative API documentation (ReDoc)",
        },
    }


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check(redis: RedisDep) -> HealthResponse:
    """
    Health check — verifies Redis connectivity and data freshness.

    Returns 'healthy' only when Redis is connected AND at least one metal
    price is available in the cache.
    """
    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    metal_prices, usdidr_data = await _read_all_prices(redis)

    # Check freshness: report how old each key's data is
    freshness: dict[str, str] = {}
    for target in SCRAPE_TARGETS:
        key = target["key"]
        if target["type"] == "metal":
            data = metal_prices.get(key)
        else:
            data = usdidr_data

        if data and "updated_at" in data:
            freshness[key] = data["updated_at"]
        else:
            freshness[key] = "unavailable"

    return HealthResponse(
        status="healthy" if redis_ok and len(metal_prices) > 0 else "degraded",
        redis_connected=redis_ok,
        metals_available=list(metal_prices.keys()),
        metals_count=len(metal_prices),
        usdidr_available=usdidr_data is not None,
        data_freshness=freshness,
    )


@app.get("/prices", response_model=MetalPriceResponse, tags=["Prices"])
async def get_all_prices(redis: RedisDep) -> MetalPriceResponse:
    """
    Get all metal prices with USDIDR exchange rate and IDR conversion.

    Data is read directly from Redis (sub-millisecond latency).
    Returns 503 if the scraper daemon has not written data yet.
    """
    metal_prices, usdidr_data = await _read_all_prices(redis)

    if not metal_prices:
        raise HTTPException(
            status_code=503,
            detail=(
                "No metal price data available. "
                "Scraper daemon may still be starting (allow ~30s)."
            ),
        )

    usdidr_rate: float | None = usdidr_data["price"] if usdidr_data else None
    now_iso = datetime.now(timezone.utc).isoformat()

    prices: list[MetalPrice] = []
    latest_ts: str = ""

    for target in METAL_TARGETS:
        key = target["key"]
        data = metal_prices.get(key)
        if data is None:
            continue

        price_usd: float = data["price"]
        price_per_gram_usd: float = price_usd / TROY_OUNCE_TO_GRAM
        price_per_gram_idr: float | None = (
            price_per_gram_usd * usdidr_rate if usdidr_rate else None
        )
        ts: str = data.get("updated_at", now_iso)
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
    redis: RedisDep,
    gram: float = Query(
        ...,
        description="Weight in grams",
        gt=0,
        examples=[10.0],
    ),
    currency: str = Query(
        "USD",
        description="Output currency: USD or IDR",
        pattern="^(USD|IDR)$",
    ),
) -> MetalPriceWithGram:
    """
    Get a specific metal price with gram-based conversion.

    - **metal**: `gold` | `silver` | `copper`
    - **gram**: weight in grams (must be > 0)
    - **currency**: `USD` (default) or `IDR`

    When `currency=IDR`, the current USDIDR exchange rate is fetched from
    Redis and applied. Returns 503 if the exchange rate is unavailable.
    """
    metal_key = metal.lower()
    currency = currency.upper()

    if metal_key not in METAL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid metal '{metal}'. Available: {', '.join(METAL_KEYS)}",
        )

    data = await _read_key(redis, f"price:{metal_key}")
    if data is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{metal_key.upper()} data not available yet. "
                "Scraper daemon may still be starting."
            ),
        )

    price_per_troy_ounce: float = data["price"]
    price_per_gram_usd: float = price_per_troy_ounce / TROY_OUNCE_TO_GRAM
    total_price_usd: float = price_per_gram_usd * gram
    ts: str = data.get("updated_at", datetime.now(timezone.utc).isoformat())

    response: dict = {
        "metal": metal_key.upper(),
        "gram": gram,
        "price_per_troy_ounce_usd": round(price_per_troy_ounce, 2),
        "price_per_gram_usd": round(price_per_gram_usd, 4),
        "total_price_usd": round(total_price_usd, 2),
        "currency": "USD",
        "timestamp": ts,
        "source": "TradingView",
        "conversion_info": {
            "troy_ounce_to_gram": TROY_OUNCE_TO_GRAM,
            "calculation_usd": (
                f"{gram}g × ${round(price_per_gram_usd, 4)}/g "
                f"= ${round(total_price_usd, 2)}"
            ),
        },
    }

    # IDR conversion — fetch exchange rate from Redis
    if currency == "IDR":
        usdidr_data = await _read_key(redis, "price:usdidr")
        if not usdidr_data or not usdidr_data.get("price"):
            raise HTTPException(
                status_code=503,
                detail=(
                    "USDIDR exchange rate not available. "
                    "Scraper daemon may still be starting."
                ),
            )

        exchange_rate: float = usdidr_data["price"]
        price_per_gram_idr: float = price_per_gram_usd * exchange_rate
        total_price_idr: float = total_price_usd * exchange_rate

        response.update(
            {
                "price_per_gram_idr": round(price_per_gram_idr, 2),
                "total_price_idr": round(total_price_idr, 2),
                "currency": "IDR",
                "exchange_rate": round(exchange_rate, 2),
            }
        )
        response["conversion_info"].update(
            {
                "exchange_rate_usdidr": round(exchange_rate, 2),
                "calculation_idr": (
                    f"{gram}g × Rp{round(price_per_gram_idr, 2):,.0f}/g "
                    f"= Rp{round(total_price_idr, 2):,.0f}"
                ),
            }
        )

    return MetalPriceWithGram(**response)


@app.get("/exchange-rate", response_model=ExchangeRateResponse, tags=["Currency"])
async def get_exchange_rate(redis: RedisDep) -> ExchangeRateResponse:
    """Get the current USDIDR exchange rate from Redis."""
    data = await _read_key(redis, "price:usdidr")

    if not data or not data.get("price"):
        raise HTTPException(
            status_code=503,
            detail="USDIDR exchange rate not available. Scraper may still be starting.",
        )

    return ExchangeRateResponse(
        currency_pair="USDIDR",
        rate=round(data["price"], 2),
        source=data.get("source", "TradingView"),
        timestamp=data.get("updated_at", ""),
        description="1 USD = X IDR",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8000,
        workers=1,
        log_level="info",
    )
