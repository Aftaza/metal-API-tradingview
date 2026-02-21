# Metal Price API v2

Real-time precious metal price API with ultra-low latency, powered by a pure stream processing architecture.

> **Scraper Daemon** continuously extracts live prices from TradingView → writes to **Redis** → **FastAPI** reads instantly and serves clients.

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Scraper Daemon Container            │
│                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│  │ Worker 1 │ │ Worker 2 │ │ Worker 3 │         │
│  │  Gold    │ │  Silver  │ │  Copper  │         │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘         │
│       │             │            │               │
│  ┌────┴─────────────┴────────────┴─────┐         │
│  │            Worker 4: USD/IDR        │         │
│  └─────────────────┬───────────────────┘         │
└────────────────────│────────────────────────────┘
                     │ SET price:*
                     ▼
            ┌────────────────┐
            │  Redis 7 Alpine │
            │  (in-memory)   │
            └───────┬────────┘
                    │ MGET
                    ▼
         ┌──────────────────┐
         │  FastAPI (api.py) │──── GET /prices
         │  Port 8000       │──── GET /prices/{metal}
         └──────────────────┘──── GET /exchange-rate
```

## Quick Start

```bash
# Clone and start all services
docker compose up --build -d

# Verify services are running
docker compose ps

# Check API health (wait ~15s for first scrape)
curl http://localhost:8000/health
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | API info and version |
| `GET` | `/health` | Redis connectivity and data freshness |
| `GET` | `/prices` | All metal prices + USDIDR rate + IDR conversion |
| `GET` | `/prices/{metal}?gram=N&currency=USD\|IDR` | Specific metal with gram/IDR conversion |
| `GET` | `/exchange-rate` | Current USD/IDR exchange rate |

### Example Requests

```bash
# Get all prices
curl http://localhost:8000/prices

# Get gold price for 10 grams in IDR
curl "http://localhost:8000/prices/gold?gram=10&currency=IDR"

# Get silver price for 100 grams in USD
curl "http://localhost:8000/prices/silver?gram=100&currency=USD"

# Get exchange rate
curl http://localhost:8000/exchange-rate
```

### Example Response — `/prices`

```json
{
  "status": "success",
  "data": [
    {
      "metal": "GOLD",
      "price_usd": 2935.50,
      "price_per_gram_usd": 94.3847,
      "price_per_gram_idr": 1537459.23,
      "currency": "USD/IDR",
      "timestamp": "2026-02-21T13:00:00+00:00",
      "source": "TradingView"
    }
  ],
  "exchange_rate_usdidr": 16290.50,
  "last_updated": "2026-02-21T13:00:00+00:00"
}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `SCRAPE_INTERVAL_SECONDS` | `5` | Delay between scrape cycles |
| `SCRAPE_TIMEOUT_MS` | `30000` | Playwright navigation timeout |
| `RECOVERY_DELAY_SECONDS` | `5` | Base delay before retry on failure |
| `LOG_LEVEL` | `INFO` | Logging level |

## Project Structure

```
.
├── api.py                 # FastAPI REST API (Redis-only reads)
├── scraper_daemon.py      # 4 async Playwright workers
├── config.py              # Shared configuration
├── Dockerfile.api         # Lightweight API image (~120MB)
├── Dockerfile.scraper     # Playwright + Chromium image
├── docker-compose.yml     # 3-service orchestration
├── requirements.txt       # Python dependencies
├── tests/
│   └── load_test.js       # k6 load testing script
└── docs/
    ├── architecture.md    # Detailed architecture documentation
    └── load-testing.md    # Load testing guide
```

## Load Testing

This project includes a [Grafana k6](https://k6.io/) load test script. See [docs/load-testing.md](docs/load-testing.md) for full details.

```bash
# Quick smoke test
k6 run tests/load_test.js

# Sustained load test (20→50 VUs)
k6 run --env SCENARIO=load tests/load_test.js

# Stress test (up to 200 VUs)
k6 run --env SCENARIO=stress tests/load_test.js
```

## Documentation

- [Architecture](docs/architecture.md) — Detailed design decisions, data flow, and recovery mechanisms
- [Load Testing](docs/load-testing.md) — k6 setup, scenarios, and interpreting results

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| API Framework | FastAPI 0.115 | Ultra-fast async REST endpoints |
| In-Memory Store | Redis 7 Alpine | Latest price state (no persistence) |
| Scraping Engine | Playwright 1.49 | Async Chromium automation |
| Containerisation | Docker Compose | 3-service orchestration |
| Load Testing | Grafana k6 | Performance validation |

## License

MIT