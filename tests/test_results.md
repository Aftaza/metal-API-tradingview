## 🎯 Test summary  

The load test of the **Metal Price API** ran the `load` scenario (ramp 0→20→50 VUs over ~3.5 min) and **failed** — 3 of 6 thresholds breached, driven by a **68.2% HTTP failure rate** (12,781 of 18,739 requests).  

---  

## 🚨 Threshold violations  

- `error_rate`: **~68%** vs target `<10%` — critical failure  
- `health_latency p(95)`: exceeded `<100ms` (overall p95 was 221ms)  
- `price_by_metal_latency p(95)`: exceeded `<200ms`  
- Passing: `http_req_duration` p95 (221ms < 500ms) and p99 (223ms < 1000ms) — but these are likely skewed by fast-failing error responses  

---  

## 🐢 Latency analysis  

Response times are very consistent (mean: **209ms**, p95: **221ms**, max: **228ms**) with low variance — this narrow spread strongly suggests failures return quickly with error responses, not actual slow processing.  

---  

## 🔥 Error distribution  

Only **37% of checks passed** (34,820 / 93,695 hits). Failing endpoints identified:  
- `/prices/platinum?gram=10` — possible 404 (endpoint may not exist)  
- `/exchange-rate` — high error rate  
- `/health` — health checks failing under load (implies redis or server saturation)  

---  

## 📈 Cloud Insights  

**Reliability (score: 0.33 — poor)**  
- High HTTP failure rate across all three endpoints above  
- Errors suggest: invalid URLs, missing auth, or server saturation  

**Script quality (score: ~0.998 — good, one warning)**  
- URL cardinality: **53+ unique `url` label values** across `http_req_duration`, `http_req_waiting`, `http_req_sending` — the script iterates over `METALS × GRAMS × CURRENCIES` combinations without URL grouping, bloating metric cardinality  

**Load generator (score: 1.0 — passed)**  
- No CPU or memory overutilization on the load generator side  

---  

## 🔍 Potential bottlenecks  

- The target server at `54.179.250.48` appears to be the constraint — `/health` failing means the server itself was unhealthy or rate-limiting under 50 VUs  
- Possible 429 (rate limiting) or 502/503 responses from Nginx; the script's `safeJson()` guard handles HTML error pages, confirming this is expected  
- `/prices/platinum` may simply be a non-existent endpoint (script bug)  

---  

## ⏭️ Recommended next steps  

1. **Fix the script**: Remove `/prices/platinum` if it doesn't exist, or add it to the API. Verify `/exchange-rate` URL is correct.  
2. **Add URL grouping**: Use the `name` tag for parameterized URLs (e.g., `http.get(url, { tags: { name: '/prices/{metal}' } })`) to reduce 53+ cardinality series.  
3. **Debug the server**: Check target server logs/CPU/memory — the `/health` failures at 50 VUs suggest resource saturation or connection limits.  
4. **Run a smoke test first**: Run 1 VU × 30s locally or via `SCENARIO=smoke` to confirm basic connectivity before load testing.