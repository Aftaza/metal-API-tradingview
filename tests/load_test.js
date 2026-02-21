/**
 * k6 Load Test — Metal Price API v2
 * ==================================
 * Tests all API endpoints under various load patterns.
 *
 * Prerequisites:
 *   - Install k6: https://grafana.com/docs/k6/latest/set-up/install-k6/
 *   - API must be running: docker compose up -d
 *
 * Usage:
 *   k6 run tests/load_test.js                         # default (smoke)
 *   k6 run --env SCENARIO=load tests/load_test.js     # sustained load
 *   k6 run --env SCENARIO=stress tests/load_test.js   # stress test
 *   k6 run --env SCENARIO=spike tests/load_test.js    # spike test
 */

import http from "k6/http";
import { check, group, sleep } from "k6";
import { Rate, Trend, Counter } from "k6/metrics";

// ── Custom Metrics ──────────────────────────────────────────────────
const errorRate = new Rate("error_rate");
const pricesLatency = new Trend("prices_latency", true);
const priceByMetalLatency = new Trend("price_by_metal_latency", true);
const healthLatency = new Trend("health_latency", true);
const exchangeRateLatency = new Trend("exchange_rate_latency", true);
const totalRequests = new Counter("total_requests");

// ── Configuration ───────────────────────────────────────────────────
const BASE_URL = __ENV.BASE_URL || "http://liveprice-metal.api.centralbullions.com";
const METALS = ["gold", "silver", "copper"];
const GRAMS = [1, 5, 10, 25, 50, 100, 500, 1000];
const CURRENCIES = ["USD", "IDR"];

// ── Scenarios ───────────────────────────────────────────────────────
const scenarios = {
  smoke: {
    executor: "constant-vus",
    vus: 1,
    duration: "30s",
  },
  load: {
    executor: "ramping-vus",
    startVUs: 0,
    stages: [
      { duration: "30s", target: 20 },   // ramp up
      { duration: "1m", target: 20 },    // steady state
      { duration: "30s", target: 50 },   // peak
      { duration: "1m", target: 50 },    // hold peak
      { duration: "30s", target: 0 },    // ramp down
    ],
  },
  stress: {
    executor: "ramping-vus",
    startVUs: 0,
    stages: [
      { duration: "30s", target: 20 },
      { duration: "1m", target: 50 },
      { duration: "1m", target: 100 },
      { duration: "2m", target: 100 },   // hold at max
      { duration: "30s", target: 0 },
    ],
  },
  spike: {
    executor: "ramping-vus",
    startVUs: 0,
    stages: [
      { duration: "10s", target: 5 },    // warm up
      { duration: "5s", target: 100 },   // spike!
      { duration: "30s", target: 100 },  // hold spike
      { duration: "10s", target: 5 },    // recover
      { duration: "30s", target: 5 },    // verify recovery
      { duration: "5s", target: 0 },
    ],
  },
};

const selectedScenario = __ENV.SCENARIO || "smoke";

export const options = {
  scenarios: {
    default: scenarios[selectedScenario] || scenarios.smoke,
  },
  thresholds: {
    http_req_duration: ["p(95)<500", "p(99)<1000"],   // 95th < 500ms, 99th < 1s
    error_rate: ["rate<0.10"],                         // < 10% error rate
    prices_latency: ["p(95)<300"],                     // /prices p95 < 300ms
    price_by_metal_latency: ["p(95)<200"],             // /prices/{metal} p95 < 200ms
    health_latency: ["p(95)<100"],                     // /health p95 < 100ms
  },
};

// ── Helper Functions ────────────────────────────────────────────────

function randomItem(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function makeRequest(method, url, expectedStatus = 200) {
  const res = method === "GET" ? http.get(url) : http.post(url);
  totalRequests.add(1);
  errorRate.add(res.status !== expectedStatus);
  return res;
}

/**
 * Safely parse JSON from a response.
 * Returns null if the response is HTML or malformed (e.g. Nginx 429/502 error page).
 */
function safeJson(res) {
  try {
    return res.json();
  } catch (_) {
    return null;
  }
}

/**
 * Check Content-Type header is JSON before attempting parse.
 */
function isJsonResponse(res) {
  const ct = res.headers["Content-Type"] || "";
  return ct.indexOf("application/json") !== -1;
}

// ── Main Test Function ──────────────────────────────────────────────

export default function () {
  // ── 1. Health Check ──
  group("Health Check", () => {
    const res = makeRequest("GET", `${BASE_URL}/health`);
    healthLatency.add(res.timings.duration);

    check(res, {
      "health: status 200": (r) => r.status === 200,
      "health: is JSON": (r) => isJsonResponse(r),
      "health: redis connected": (r) => {
        const body = safeJson(r);
        return body !== null && body.redis_connected === true;
      },
      "health: status is healthy": (r) => {
        const body = safeJson(r);
        return body !== null && body.status === "healthy";
      },
    });
  });

  sleep(0.1);

  // ── 2. Get All Prices ──
  group("Get All Prices", () => {
    const res = makeRequest("GET", `${BASE_URL}/prices`);
    pricesLatency.add(res.timings.duration);

    check(res, {
      "prices: status 200": (r) => r.status === 200,
      "prices: is JSON": (r) => isJsonResponse(r),
      "prices: has data array": (r) => {
        const body = safeJson(r);
        return body !== null && Array.isArray(body.data) && body.data.length > 0;
      },
      "prices: has exchange rate": (r) => {
        const body = safeJson(r);
        return body !== null && body.exchange_rate_usdidr !== null;
      },
      "prices: status success": (r) => {
        const body = safeJson(r);
        return body !== null && body.status === "success";
      },
      "prices: has all metals": (r) => {
        const body = safeJson(r);
        if (body === null || !Array.isArray(body.data)) return false;
        const metals = body.data.map((d) => d.metal);
        return (
          metals.includes("GOLD") &&
          metals.includes("SILVER") &&
          metals.includes("COPPER")
        );
      },
    });
  });

  sleep(0.1);

  // ── 3. Get Specific Metal Price (USD) ──
  group("Get Metal Price (USD)", () => {
    const metal = randomItem(METALS);
    const gram = randomItem(GRAMS);
    const url = `${BASE_URL}/prices/${metal}?gram=${gram}&currency=USD`;
    const res = makeRequest("GET", url);
    priceByMetalLatency.add(res.timings.duration);

    check(res, {
      "metal-usd: status 200": (r) => r.status === 200,
      "metal-usd: is JSON": (r) => isJsonResponse(r),
      "metal-usd: correct metal": (r) => {
        const body = safeJson(r);
        return body !== null && body.metal === metal.toUpperCase();
      },
      "metal-usd: correct gram": (r) => {
        const body = safeJson(r);
        return body !== null && body.gram === gram;
      },
      "metal-usd: has price_per_gram_usd": (r) => {
        const body = safeJson(r);
        return body !== null && body.price_per_gram_usd > 0;
      },
      "metal-usd: has total_price_usd": (r) => {
        const body = safeJson(r);
        return body !== null && body.total_price_usd > 0;
      },
      "metal-usd: currency is USD": (r) => {
        const body = safeJson(r);
        return body !== null && body.currency === "USD";
      },
      "metal-usd: has conversion_info": (r) => {
        const body = safeJson(r);
        return body !== null && body.conversion_info !== undefined;
      },
    });
  });

  sleep(0.1);

  // ── 4. Get Specific Metal Price (IDR) ──
  group("Get Metal Price (IDR)", () => {
    const metal = randomItem(METALS);
    const gram = randomItem(GRAMS);
    const url = `${BASE_URL}/prices/${metal}?gram=${gram}&currency=IDR`;
    const res = makeRequest("GET", url);
    priceByMetalLatency.add(res.timings.duration);

    check(res, {
      "metal-idr: status 200": (r) => r.status === 200,
      "metal-idr: is JSON": (r) => isJsonResponse(r),
      "metal-idr: currency is IDR": (r) => {
        const body = safeJson(r);
        return body !== null && body.currency === "IDR";
      },
      "metal-idr: has exchange_rate": (r) => {
        const body = safeJson(r);
        return body !== null && body.exchange_rate > 0;
      },
      "metal-idr: has price_per_gram_idr": (r) => {
        const body = safeJson(r);
        return body !== null && body.price_per_gram_idr > 0;
      },
      "metal-idr: has total_price_idr": (r) => {
        const body = safeJson(r);
        return body !== null && body.total_price_idr > 0;
      },
      "metal-idr: IDR conversion info": (r) => {
        const body = safeJson(r);
        return (
          body !== null &&
          body.conversion_info &&
          body.conversion_info.exchange_rate_usdidr > 0
        );
      },
    });
  });

  sleep(0.1);

  // ── 5. Exchange Rate ──
  group("Exchange Rate", () => {
    const res = makeRequest("GET", `${BASE_URL}/exchange-rate`);
    exchangeRateLatency.add(res.timings.duration);

    check(res, {
      "exchange: status 200": (r) => r.status === 200,
      "exchange: is JSON": (r) => isJsonResponse(r),
      "exchange: pair is USDIDR": (r) => {
        const body = safeJson(r);
        return body !== null && body.currency_pair === "USDIDR";
      },
      "exchange: rate in valid range": (r) => {
        const body = safeJson(r);
        if (body === null) return false;
        return body.rate > 10000 && body.rate < 25000;
      },
      "exchange: has timestamp": (r) => {
        const body = safeJson(r);
        return body !== null && body.timestamp !== "";
      },
    });
  });

  sleep(0.1);

  // ── 6. API Root ──
  group("API Root", () => {
    const res = makeRequest("GET", `${BASE_URL}/`);

    check(res, {
      "root: status 200": (r) => r.status === 200,
      "root: is JSON": (r) => isJsonResponse(r),
      "root: version 2.0.0": (r) => {
        const body = safeJson(r);
        return body !== null && body.version === "2.0.0";
      },
      "root: has metals list": (r) => {
        const body = safeJson(r);
        return (
          body !== null &&
          Array.isArray(body.metals) &&
          body.metals.length === 3
        );
      },
    });
  });

  sleep(0.1);

  // ── 7. Error Handling — Invalid Metal ──
  group("Error: Invalid Metal", () => {
    const res = makeRequest("GET", `${BASE_URL}/prices/platinum?gram=10`, 400);

    check(res, {
      "invalid-metal: status 400": (r) => r.status === 400,
    });
  });

  sleep(0.3);
}

// ── Summary Handler ─────────────────────────────────────────────────

export function handleSummary(data) {
  const p95 = data.metrics.http_req_duration?.values?.["p(95)"] || 0;
  const p99 = data.metrics.http_req_duration?.values?.["p(99)"] || 0;
  const median = data.metrics.http_req_duration?.values?.med || 0;
  const total = data.metrics.total_requests?.values?.count || 0;
  const errors = data.metrics.error_rate?.values?.rate || 0;

  console.log("\n╔══════════════════════════════════════════════════╗");
  console.log("║     Metal Price API v2 — Load Test Results      ║");
  console.log("╠══════════════════════════════════════════════════╣");
  console.log(`║  Scenario    : ${selectedScenario.padEnd(33)}║`);
  console.log(`║  Total Reqs  : ${String(total).padEnd(33)}║`);
  console.log(`║  Error Rate  : ${(errors * 100).toFixed(2).padStart(6)}%${" ".repeat(26)}║`);
  console.log(`║  Latency p50 : ${median.toFixed(2).padStart(8)} ms${" ".repeat(22)}║`);
  console.log(`║  Latency p95 : ${p95.toFixed(2).padStart(8)} ms${" ".repeat(22)}║`);
  console.log(`║  Latency p99 : ${p99.toFixed(2).padStart(8)} ms${" ".repeat(22)}║`);
  console.log("╚══════════════════════════════════════════════════╝\n");

  return {
    stdout: JSON.stringify(data, null, 2),
  };
}
