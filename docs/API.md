# API Reference

Base URL: `http://localhost:8000`

## Data Sources

| Data    | Source      | Selector                               | Unit                 |
| ------- | ----------- | -------------------------------------- | -------------------- |
| Gold    | Kitco       | `h3.font-mulish.text-4xl.font-bold`    | per troy ounce (USD) |
| Silver  | Kitco       | `h3.font-mulish.text-4xl.font-bold`    | per troy ounce (USD) |
| Copper  | Kitco       | `h3.font-mulish.text-4xl.font-bold`    | per lb (USD)         |
| USD/IDR | TradingView | `span[data-qa-id='symbol-last-value']` | exchange rate        |

## Endpoints

### `GET /`

Info tentang API, fitur, dan available endpoints.

---

### `GET /prices`

Semua harga metal dengan exchange rate USDIDR dan konversi IDR.

**Response:**

```json
{
  "status": "success",
  "data": [
    {
      "metal": "GOLD",
      "price_usd": 2650.3,
      "price_unit": "per troy ounce",
      "price_per_gram_usd": 85.2125,
      "price_per_gram_idr": 1363400.0,
      "currency": "USD/IDR",
      "timestamp": "2024-01-15T10:30:00",
      "source": "Kitco"
    },
    {
      "metal": "COPPER",
      "price_usd": 4.15,
      "price_unit": "per lb",
      "price_per_gram_usd": 0.0092,
      "price_per_gram_idr": 147.2,
      "currency": "USD/IDR",
      "timestamp": "2024-01-15T10:30:00",
      "source": "Kitco"
    }
  ],
  "exchange_rate_usdidr": 16000.0,
  "last_updated": "2024-01-15T10:30:00"
}
```

---

### `GET /prices/{metal}`

Harga metal spesifik dengan konversi gram.

**Parameters:**

| Parameter  | Type  | Required | Default | Deskripsi                       |
| ---------- | ----- | -------- | ------- | ------------------------------- |
| `metal`    | path  | ✅       | -       | `gold`, `silver`, atau `copper` |
| `gram`     | query | ✅       | -       | Berat dalam gram (> 0)          |
| `currency` | query | ❌       | `USD`   | `USD` atau `IDR`                |

**Example:** `GET /prices/gold?gram=10&currency=IDR`

**Response:**

```json
{
  "metal": "GOLD",
  "gram": 10.0,
  "price_per_unit_usd": 2650.3,
  "price_unit": "per troy ounce",
  "price_per_gram_usd": 85.2125,
  "total_price_usd": 852.13,
  "price_per_gram_idr": 1363400.0,
  "total_price_idr": 13634000.0,
  "currency": "IDR",
  "exchange_rate": 16000.0,
  "timestamp": "2024-01-15T10:30:00",
  "source": "Kitco",
  "conversion_info": {
    "unit_conversion": "1 troy ounce = 31.1034768 gram",
    "calculation_usd": "10g × $85.2125/g = $852.13",
    "exchange_rate_usdidr": 16000.0,
    "calculation_idr": "10g × Rp1,363,400/g = Rp13,634,000"
  }
}
```

---

### `GET /health`

Health check. Returns status dan jumlah tab aktif.

---

### `POST /refresh`

Manual refresh semua 4 tab (force reload halaman).

---

### `GET /symbols`

List semua data source URLs.

---

### `GET /exchange-rate`

USDIDR exchange rate saja.

---

### `GET /debug/cache`

Debug info: cached prices, HTML sizes, tab status.

## Price Conversion Logic

### Gold & Silver (per troy ounce)

```
price_per_gram = price_per_troy_ounce / 31.1034768
```

### Copper (per lb)

```
price_per_gram = price_per_lb / 453.592
```

### IDR Conversion

```
price_per_gram_idr = price_per_gram_usd × usdidr_rate
```

## Error Codes

| Code  | Deskripsi                                |
| ----- | ---------------------------------------- |
| `400` | Metal tidak valid                        |
| `503` | Data belum tersedia / masih initializing |
