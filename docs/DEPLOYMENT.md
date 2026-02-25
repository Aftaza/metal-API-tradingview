# Deployment Guide

## Prerequisites

- Docker & Docker Compose
- Minimum 1GB RAM (Selenium headless Chrome)
- Network access ke `kitco.com` dan `tradingview.com`

## Docker Deployment

### Production

```bash
# Build dan jalankan
docker compose up -d --build

# Cek logs
docker compose logs -f

# Restart
docker compose restart
```

### Development

```bash
docker compose -f docker-compose.dev.yml up --build
```

## Environment Variables

| Variable          | Default | Deskripsi                               |
| ----------------- | ------- | --------------------------------------- |
| `LOG_LEVEL`       | `INFO`  | Log level (DEBUG, INFO, WARNING, ERROR) |
| `UPDATE_INTERVAL` | `300`   | Auto-update interval dalam detik        |
| `SCRAP_TIMEOUT`   | `30`    | Selenium page load timeout dalam detik  |

## Resource Requirements

| Resource | Minimum | Recommended |
| -------- | ------- | ----------- |
| RAM      | 512 MB  | 1 GB        |
| CPU      | 1 core  | 2 cores     |
| Disk     | 500 MB  | 1 GB        |

> **Note:** Selenium headless Chrome membutuhkan ~400-600 MB RAM. 4 persistent tabs aktif secara bersamaan.

## Nginx Reverse Proxy (Optional)

```nginx
server {
    listen 80;
    server_name api.example.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

## Health Check

```bash
curl http://localhost:8000/health
```

Response healthy:

```json
{
  "status": "healthy",
  "cached_metals": 3,
  "total_metals": 3,
  "active_tabs": 4,
  "total_tabs": 4
}
```

## Troubleshooting

### Browser initialization failed

- Pastikan container memiliki cukup RAM (min 512MB)
- Cek apakah Chrome/Chromium terinstall di container

### Timeout saat scraping

- Cek koneksi internet container ke `kitco.com` dan `tradingview.com`
- Naikkan `SCRAP_TIMEOUT` jika koneksi lambat

### Tab crashed / stale element

- Sistem memiliki auto-recovery — tab akan direcover otomatis
- Gunakan `POST /refresh` jika masih bermasalah
- Restart container jika masalah persists

### Exchange rate tidak tersedia

- USDIDR tab menggunakan TradingView — pastikan akses tidak diblokir
- Cek `GET /debug/cache` untuk melihat detail status
