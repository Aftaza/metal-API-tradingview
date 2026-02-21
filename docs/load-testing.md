# Load Testing Guide — Grafana k6

## Prerequisites

### Install k6

```bash
# Windows (winget)
winget install grafana.k6

# Windows (chocolatey)
choco install k6

# macOS
brew install k6

# Docker (no install needed)
docker run --rm -i grafana/k6 run - < tests/load_test.js
```

### Start the API

```bash
docker compose up --build -d

# Wait ~15s for scraper daemon to populate Redis
curl http://localhost:8000/health
# Should return: {"status": "healthy", ...}
```

---

## Running Tests

### Scenarios

The test script includes 4 pre-defined scenarios:

| Scenario | VUs | Duration | Purpose |
|----------|-----|----------|---------|
| `smoke` | 1 VU | 30s | Verify API works correctly |
| `load` | 0→20→50→0 | ~3.5 min | Normal production load |
| `stress` | 0→50→100→200→0 | ~5 min | Find breaking point |
| `spike` | 5→300→5 | ~1.5 min | Sudden traffic burst |

### Commands

```bash
# Smoke test (default)
k6 run tests/load_test.js

# Sustained load
k6 run --env SCENARIO=load tests/load_test.js

# Stress test
k6 run --env SCENARIO=stress tests/load_test.js

# Spike test
k6 run --env SCENARIO=spike tests/load_test.js

# Custom base URL (for remote server)
k6 run --env SCENARIO=load --env BASE_URL=http://your-server:8000 tests/load_test.js
```

### Using Docker (no local install)

```bash
# From project root
docker run --rm -i --network=host grafana/k6 run - < tests/load_test.js

# With scenario
docker run --rm -i --network=host -e SCENARIO=load grafana/k6 run - < tests/load_test.js
```

---

## What Gets Tested

Each virtual user (VU) runs this sequence per iteration:

1. **`GET /health`** — Verify Redis connectivity
2. **`GET /prices`** — Fetch all metals (validates data structure)
3. **`GET /prices/{metal}?currency=USD`** — Random metal + random gram weight
4. **`GET /prices/{metal}?currency=IDR`** — Same with IDR conversion
5. **`GET /exchange-rate`** — USDIDR rate validation
6. **`GET /`** — API version check
7. **`GET /prices/platinum`** — Error handling (invalid metal → 400)

---

## Custom Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `prices_latency` | Trend | Response time for `/prices` |
| `price_by_metal_latency` | Trend | Response time for `/prices/{metal}` |
| `health_latency` | Trend | Response time for `/health` |
| `exchange_rate_latency` | Trend | Response time for `/exchange-rate` |
| `error_rate` | Rate | Percentage of non-expected status codes |
| `total_requests` | Counter | Total requests made |

---

## Pass/Fail Thresholds

| Metric | Threshold | Description |
|--------|-----------|-------------|
| `http_req_duration p(95)` | < 200ms | 95th percentile overall latency |
| `http_req_duration p(99)` | < 500ms | 99th percentile overall latency |
| `error_rate` | < 5% | Maximum acceptable error rate |
| `prices_latency p(95)` | < 150ms | `/prices` endpoint target |
| `price_by_metal_latency p(95)` | < 100ms | `/prices/{metal}` endpoint target |
| `health_latency p(95)` | < 50ms | `/health` endpoint target |

---

## Interpreting Results

### Example Output

```
╔══════════════════════════════════════════════════╗
║     Metal Price API v2 — Load Test Results      ║
╠══════════════════════════════════════════════════╣
║  Scenario    : load                              ║
║  Total Reqs  : 12847                             ║
║  Error Rate  :  0.00%                            ║
║  Latency p50 :     2.34 ms                       ║
║  Latency p95 :     8.91 ms                       ║
║  Latency p99 :    15.20 ms                       ║
╚══════════════════════════════════════════════════╝
```

### What to Look For

- **p95 < 200ms** — The API should comfortably meet this since it's just Redis reads
- **Error rate = 0%** — All endpoints should return expected status codes
- **Consistent latency under load** — If p99 jumps significantly at higher VU counts, Redis or the API container may need more resources
- **No 503 errors** — If you see 503s, the scraper daemon may not have populated Redis yet

### Troubleshooting High Latency

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| p95 > 200ms | Redis connection pool exhaustion | Increase Redis `maxclients` |
| Sudden p99 spike | Docker memory limit hit | Increase API container memory |
| 503 errors | Redis is empty | Wait for scraper daemon startup |
| Error rate > 0% | API container restarting | Check `docker compose logs api` |
