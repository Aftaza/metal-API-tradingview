# Metal Price API v2

Real-time precious metal price API with ultra-low latency, powered by a pure stream processing architecture.

> **Scraper Daemon** continuously extracts live prices from TradingView → writes to **Redis** → **FastAPI** reads instantly → **Nginx** serves clients with rate limiting & security headers.

## Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │          Scraper Daemon Container           │
                         │                                             │
                         │  ┌────────┐  ┌────────┐  ┌────────┐       │
                         │  │Gold    │  │Silver  │  │Copper  │       │
                         │  │Worker 1│  │Worker 2│  │Worker 3│       │
                         │  └───┬────┘  └───┬────┘  └───┬────┘       │
                         │      │           │           │             │
                         │  ┌───┴───────────┴───────────┴───┐        │
                         │  │       Worker 4: USD/IDR        │        │
                         │  └──────────────┬─────────────────┘        │
                         └─────────────────│──────────────────────────┘
                                           │ SET price:*
                                           ▼
  Client ──▶ Nginx (80) ──▶ FastAPI (8000) ──▶ Redis (6379)
              │                 │
              │ Rate limit      │ MGET (sub-ms)
              │ Security headers│
              │ Gzip            └──▶ JSON response
              ▼
          HTTP Response
```

## Quick Start

### Local Development

```bash
# Clone and start all services
docker compose up --build -d

# Verify services are running
docker compose ps

# Check API health (wait ~15s for first scrape)
curl http://localhost/health
```

### VPS Deployment

```bash
# On your server
git clone https://github.com/YOUR_USERNAME/metal-API-tradingview.git
cd metal-API-tradingview

# Start all 4 services (Nginx + Redis + API + Scraper)
docker compose up --build -d

# Verify
curl http://YOUR_SERVER_IP/health
```

See [docs/deployment.md](docs/deployment.md) for complete guides (VPS, Railway, Render, DigitalOcean).

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
# Get all prices (via Nginx)
curl http://localhost/prices

# Get gold price for 10 grams in IDR
curl "http://localhost/prices/gold?gram=10&currency=IDR"

# Get silver price for 100 grams in USD
curl "http://localhost/prices/silver?gram=100&currency=USD"

# Get exchange rate
curl http://localhost/exchange-rate
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
| `NGINX_PORT` | `80` | Public-facing Nginx port |

## Project Structure

```
.
├── api.py                 # FastAPI REST API (Redis-only reads)
├── scraper_daemon.py      # 4 async Playwright workers
├── config.py              # Shared configuration
├── nginx.conf             # Nginx reverse proxy config
├── Dockerfile.api         # Lightweight API image (~120MB)
├── Dockerfile.scraper     # Playwright + Chromium image
├── docker-compose.yml     # 4-service orchestration
├── requirements.txt       # Python dependencies
├── .env                   # Environment variables
├── tests/
│   └── load_test.js       # k6 load testing script
└── docs/
    ├── architecture.md    # Detailed architecture documentation
    ├── deployment.md      # VPS, Railway, Render deploy guides
    └── load-testing.md    # Load testing guide
```

## Deployment

This project supports multiple deployment targets:

| Platform | Guide | Notes |
|----------|-------|-------|
| **VPS** (Ubuntu/Debian) | [docs/deployment.md](docs/deployment.md#vps-deployment) | Full Docker Compose + Nginx + SSL |
| **Railway** | [docs/deployment.md](docs/deployment.md#railway-deployment) | Managed Redis, public URL |
| **Render** | [docs/deployment.md](docs/deployment.md#render-deployment) | Web service + background worker |
| **DigitalOcean** | [docs/deployment.md](docs/deployment.md#digitalocean-app-platform) | App Platform with App Spec |

## Load Testing

This project includes a [Grafana k6](https://k6.io/) load test script. See [docs/load-testing.md](docs/load-testing.md) for full details.

```bash
# Quick smoke test
k6 run tests/load_test.js

# Sustained load test (20→50 VUs)
k6 run --env SCENARIO=load tests/load_test.js

# Stress test (up to 100 VUs)
k6 run --env SCENARIO=stress tests/load_test.js
```

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | Design decisions, data flow, and recovery mechanisms |
| [Deployment](docs/deployment.md) | VPS, Railway, Render, DigitalOcean guides + SSL setup |
| [Load Testing](docs/load-testing.md) | k6 setup, scenarios, and interpreting results |

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Reverse Proxy | Nginx 1.25 | Rate limiting, security headers, gzip |
| API Framework | FastAPI 0.115 | Ultra-fast async REST endpoints |
| In-Memory Store | Redis 7 Alpine | Latest price state (no persistence) |
| Scraping Engine | Playwright 1.49 | Async Chromium automation |
| Containerisation | Docker Compose | 4-service orchestration |
| Load Testing | Grafana k6 | Performance validation |

## License

MIT