# Metal Price API - Kitco + TradingView

Real-time metal price API menggunakan **Kitco** untuk harga metal (Gold, Silver, Copper) dan **TradingView** untuk exchange rate USD/IDR. Dibangun dengan FastAPI + Selenium multi-tab scraping.

## Features

- 🏷️ **3 Metal Prices** dari Kitco: Gold, Silver, Copper
- 💱 **Exchange Rate USDIDR** dari TradingView
- 🔄 **4 Persistent Browser Tabs** — Selenium dengan auto-recovery
- ⚡ **Parallel Extraction** — Thread pool untuk kecepatan
- 📊 **Konversi Gram & IDR** — Otomatis konversi per gram dan Rupiah
- 🔧 **Copper lb → gram** — Konversi otomatis dari per pound ke per gram

## Quick Start

### Production (Docker)

```bash
docker compose up -d --build
```

### Development

```bash
docker compose -f docker-compose.dev.yml up --build
```

API tersedia di: `http://localhost:8000`

## API Endpoints

| Method | Endpoint                               | Deskripsi                                   |
| ------ | -------------------------------------- | ------------------------------------------- |
| `GET`  | `/`                                    | Info API                                    |
| `GET`  | `/prices`                              | Semua harga metal + USDIDR + IDR conversion |
| `GET`  | `/prices/{metal}?gram=10&currency=IDR` | Harga metal per gram (gold, silver, copper) |
| `GET`  | `/health`                              | Health check                                |
| `POST` | `/refresh`                             | Manual refresh semua tab                    |
| `GET`  | `/symbols`                             | List data sources                           |
| `GET`  | `/exchange-rate`                       | USDIDR exchange rate                        |
| `GET`  | `/debug/cache`                         | Debug cache & tab status                    |

### Contoh Request

```bash
# Semua harga
curl http://localhost:8000/prices

# Gold 10 gram dalam IDR
curl "http://localhost:8000/prices/gold?gram=10&currency=IDR"

# Copper 100 gram (otomatis konversi lb→gram)
curl "http://localhost:8000/prices/copper?gram=100&currency=IDR"
```

## Data Sources

| Metal   | Source      | URL                                  | Unit           |
| ------- | ----------- | ------------------------------------ | -------------- |
| Gold    | Kitco       | `kitco.com/charts/gold`              | per troy ounce |
| Silver  | Kitco       | `kitco.com/charts/silver`            | per troy ounce |
| Copper  | Kitco       | `kitco.com/price/base-metals/copper` | per lb         |
| USD/IDR | TradingView | `tradingview.com/symbols/USDIDR`     | rate           |

### Copper Conversion

Kitco provides copper prices **per pound (lb)**. The API automatically converts:

```
1 lb = 453.592 gram
price_per_gram = price_per_lb / 453.592
```

## Environment Variables

| Variable          | Default | Deskripsi                               |
| ----------------- | ------- | --------------------------------------- |
| `LOG_LEVEL`       | `INFO`  | Log level (DEBUG, INFO, WARNING, ERROR) |
| `UPDATE_INTERVAL` | `300`   | Auto-update interval (detik)            |
| `SCRAP_TIMEOUT`   | `30`    | Selenium timeout (detik)                |

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  FastAPI App                      │
│                (main.py v4.0.0)                   │
├─────────────────────────────────────────────────┤
│  MultiTabBrowserScraper (Selenium)               │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────┐│
│  │  Gold   │ │ Silver  │ │ Copper  │ │ USDIDR ││
│  │ (Kitco) │ │ (Kitco) │ │ (Kitco) │ │  (TV)  ││
│  └─────────┘ └─────────┘ └─────────┘ └────────┘│
├─────────────────────────────────────────────────┤
│  Thread Pool (4 workers)                         │
│  Parallel HTML Extraction + Price Parsing        │
├─────────────────────────────────────────────────┤
│  Price Cache (in-memory Dict)                    │
│  Auto-Recovery for Crashed Tabs                  │
└─────────────────────────────────────────────────┘
```

## Documentation

- [API Reference](docs/API.md)
- [Deployment Guide](docs/DEPLOYMENT.md)

## Tech Stack

- **FastAPI** — Web framework
- **Selenium** — Browser scraping (headless Chrome)
- **BeautifulSoup** — HTML parsing
- **Docker** — Containerization
- **lxml** — Fast HTML parser
