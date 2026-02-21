# Architecture — Metal Price API v2

## Overview

V2 is a **pure stream processing** architecture designed for extreme server efficiency and ultra-low API latency. The system is split into two fully decoupled processes that communicate exclusively through Redis.

### Design Principles

1. **Separation of Concerns** — Scraping and serving are independent processes. The API never waits for a browser.
2. **Redis as Single Source of Truth** — No SQL, no persistent database. Only the absolute latest price matters.
3. **Fail-Fast, Recover-Fast** — Each worker is self-healing with exponential backoff.
4. **Zero-Copy Reads** — API reads pre-serialised JSON from Redis; no computation on the hot path.

---

## System Components

### 1. Scraper Daemon (`scraper_daemon.py`)

A standalone Python process running 4 independent `asyncio.Task` workers inside a single event loop.

**Worker Lifecycle:**

```
┌──────────────────────────────────────────────────┐
│                 Worker Loop                       │
│                                                   │
│  ┌─────────┐    ┌──────────┐    ┌──────────────┐ │
│  │ Create   │───▶│ Navigate │───▶│ Wait for     │ │
│  │ Context  │    │ to URL   │    │ price element│ │
│  └─────────┘    └──────────┘    └──────┬───────┘ │
│                                        │          │
│  ┌─────────┐    ┌──────────┐    ┌──────▼───────┐ │
│  │  Sleep   │◀──│ SET Redis│◀───│ Extract &    │ │
│  │ (5s)    │    │          │    │ parse price  │ │
│  └────┬────┘    └──────────┘    └──────────────┘ │
│       │                                           │
│       └───────── Loop Forever ────────────────────┘
│                                                   │
│  On Error: Close context → backoff → restart      │
└──────────────────────────────────────────────────┘
```

**Key Design Decisions:**

- **One BrowserContext per iteration** — Each scrape creates a fresh, lightweight context (~5MB). This prevents memory leaks and stale state accumulation.
- **Resource blocking** — Images, fonts, videos are blocked via `page.route()` to speed up page load by ~60%.
- **Exponential backoff** — On failure, wait `min(5s × failures, 60s)` before retrying. Consecutive failures increase backoff; success resets it to 0.
- **Single Chromium instance, 4 contexts** — Playwright's architecture supports multiple isolated contexts sharing one browser process. This is more memory-efficient than 4 separate browsers.

### 2. Redis (`redis:7-alpine`)

Configured as a pure in-memory cache with **zero persistence**:

```
redis-server
  --appendonly no        # No AOF log
  --save ""              # No RDB snapshots
  --maxmemory 64mb       # Hard memory limit
  --maxmemory-policy allkeys-lru  # Evict least-recently-used
```

**Redis Keys:**

| Key | Value Format | Updated By |
|-----|-------------|------------|
| `price:gold` | `{"price": 2935.5, "source": "TradingView", "updated_at": "ISO8601"}` | Worker 1 |
| `price:silver` | Same format | Worker 2 |
| `price:copper` | Same format | Worker 3 |
| `price:usdidr` | Same format (price = exchange rate) | Worker 4 |

### 3. FastAPI API (`api.py`)

Stateless HTTP server that **only reads from Redis**. No scraping, no computation beyond unit conversion.

**Request Flow:**

```
Client ──▶ FastAPI ──▶ redis_pool.mget() ──▶ JSON response
                         │
                         └── Sub-millisecond read
```

**Performance Characteristics:**
- `MGET` reads 4 keys in a single Redis round-trip
- JSON is pre-serialised by the daemon; API only needs `json.loads()`
- Unit conversion (troy ounce → gram, USD → IDR) is simple arithmetic
- 2 Uvicorn workers handle concurrent HTTP requests

---

## Data Flow

```
TradingView Website
       │
       │  Playwright navigates, waits for
       │  span[data-qa-id='symbol-last-value']
       │
       ▼
  scraper_daemon.py
       │
       │  Parses text → validates range → JSON serialises
       │
       │  SET price:gold '{"price":2935.5,...}'
       ▼
     Redis
       │
       │  MGET price:gold price:silver price:copper price:usdidr
       │
       ▼
    api.py
       │
       │  Deserialise → unit conversion → Pydantic response
       │
       ▼
  HTTP Client (JSON)
```

---

## Recovery & Resilience

| Failure Scenario | Recovery Mechanism |
|-----------------|-------------------|
| Browser context crash | `finally` block closes context; worker continues loop |
| Page navigation timeout | Caught by `except Exception`; exponential backoff |
| Price element not found | Logged as warning; context closed, retried next cycle |
| Redis connection lost | Daemon retries connection in a loop; API returns 503 |
| Invalid price value | Range validation rejects it; last valid price stays in Redis |
| Container OOM / restart | `restart: unless-stopped` in Docker Compose |

---

## Memory Footprint

| Component | Memory Limit | Typical Usage |
|-----------|-------------|---------------|
| Redis | 128 MB | < 1 MB (4 keys) |
| FastAPI API | 256 MB | ~80 MB (2 workers) |
| Scraper Daemon | 2 GB | ~800 MB (Chromium + 4 contexts) |

---

## V1 vs V2 Comparison

| Aspect | V1 | V2 |
|--------|----|----|
| Scraping engine | Selenium (sync, threaded) | Playwright (async, native) |
| Data store | In-process Python dict | Redis (in-memory, shared) |
| Architecture | Monolith (913-line `main.py`) | Decoupled daemon + API |
| API latency | Scrapes on each request (seconds) | Sub-ms Redis reads |
| Concurrency model | ThreadPoolExecutor (6 threads) | asyncio tasks (4 workers) |
| Recovery | Tab-level, manual | Context-level, exponential backoff |
| Metals supported | 5 (Gold, Silver, Platinum, Palladium, Copper) | 3 (Gold, Silver, Copper) |
| Container count | 1 (monolith) | 3 (Redis + API + Daemon) |
| Browser tabs | 6 persistent tabs in 1 browser | 4 isolated contexts, fresh each cycle |
