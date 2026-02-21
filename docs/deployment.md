# Deployment Guide

## Table of Contents

- [VPS Deployment](#vps-deployment)
- [Railway Deployment](#railway-deployment)
- [Render Deployment](#render-deployment)
- [DigitalOcean App Platform](#digitalocean-app-platform)
- [SSL / HTTPS Setup](#ssl--https-setup)
- [Monitoring & Logs](#monitoring--logs)
- [Troubleshooting](#troubleshooting)

---

## VPS Deployment

### Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 cores | 4 cores |
| RAM | 3 GB | 4 GB |
| Storage | 10 GB | 20 GB |
| OS | Ubuntu 22.04+ / Debian 12+ | Ubuntu 24.04 LTS |

### Step 1 — Install Docker

```bash
# Ubuntu / Debian
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl gnupg

# Add Docker GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add repo
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Allow non-root
sudo usermod -aG docker $USER
newgrp docker
```

### Step 2 — Clone & Configure

```bash
git clone https://github.com/YOUR_USERNAME/metal-API-tradingview.git
cd metal-API-tradingview

# (Optional) Edit environment variables
cp .env.example .env
nano .env
```

### Step 3 — Deploy

```bash
# Build and start all 4 services (nginx + redis + api + scraper)
docker compose up --build -d

# Verify all services are running
docker compose ps

# Check logs
docker compose logs -f --tail=50
```

### Step 4 — Verify

```bash
# Health check (via Nginx on port 80)
curl http://YOUR_SERVER_IP/health

# Get all prices
curl http://YOUR_SERVER_IP/prices

# Get gold price in IDR
curl "http://YOUR_SERVER_IP/prices/gold?gram=10&currency=IDR"
```

### Firewall Setup (UFW)

```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP (Nginx)
sudo ufw allow 443/tcp   # HTTPS (if using SSL)
sudo ufw enable
```

> **Note:** Redis (6379) and the API (8000) are **not exposed** to the public. They are only accessible within the Docker network via Nginx.

---

## SSL / HTTPS Setup

### Option A — Certbot (Let's Encrypt)

```bash
# Install certbot
sudo apt install -y certbot

# Get certificate (stop nginx first)
docker compose stop nginx
sudo certbot certonly --standalone -d api.yourdomain.com
docker compose start nginx
```

Then update `nginx.conf` to add the SSL server block:

```nginx
server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;

    # ... (same location blocks as port 80)
}

# Redirect HTTP → HTTPS
server {
    listen 80;
    server_name api.yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

And add the certificate volume to `docker-compose.yml`:

```yaml
nginx:
  volumes:
    - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    - /etc/letsencrypt:/etc/letsencrypt:ro
```

### Option B — Cloudflare Proxy

1. Point your domain's DNS to the VPS IP via Cloudflare
2. Enable **Proxied** (orange cloud) on the DNS record
3. Set SSL mode to **Full (Strict)** in Cloudflare dashboard
4. Keep Nginx on port 80 — Cloudflare handles SSL termination

---

## Railway Deployment

[Railway](https://railway.app) supports Docker Compose natively.

### Step 1 — Create Project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub Repo**
2. Select the `metal-API-tradingview` repository

### Step 2 — Add Redis

1. In Railway dashboard → **+ New** → **Database** → **Redis**
2. Copy the `REDIS_URL` from the Redis service's **Connect** tab

### Step 3 — Deploy the API

1. Click on the API service → **Settings** → set Dockerfile to `Dockerfile.api`
2. In **Variables**, add:
   ```
   REDIS_URL=redis://default:PASSWORD@HOST:PORT  ← from step 2
   LOG_LEVEL=INFO
   PORT=8000
   ```
3. Under **Networking**, generate a public domain

### Step 4 — Deploy the Scraper Daemon

1. **+ New** → **Docker Service** → same GitHub repo
2. Set Dockerfile to `Dockerfile.scraper`
3. In **Variables**, add:
   ```
   REDIS_URL=redis://default:PASSWORD@HOST:PORT  ← same Redis URL
   SCRAPE_INTERVAL_SECONDS=5
   SCRAPE_TIMEOUT_MS=30000
   RECOVERY_DELAY_SECONDS=5
   LOG_LEVEL=INFO
   ```

> **Note:** Railway does not support `shm_size` in Docker. The scraper may need the `--disable-dev-shm-usage` Chromium flag. If you encounter issues, add this argument to `scraper_daemon.py` in the browser launch args.

### Final Architecture on Railway

```
┌─────────────┐    ┌──────────────┐    ┌──────────────────┐
│ API Service │◄──►│ Railway Redis│◄──►│ Scraper Service  │
│ (Dockerfile │    │  (managed)   │    │ (Dockerfile      │
│  .api)      │    └──────────────┘    │  .scraper)       │
└──────┬──────┘                        └──────────────────┘
       │
  Public URL
```

---

## Render Deployment

[Render](https://render.com) supports Docker natively.

### Step 1 — Redis

1. Dashboard → **New** → **Redis** → select region → Create
2. Copy the **Internal URL** (e.g., `redis://red-xxx:6379`)

### Step 2 — API Service

1. **New** → **Web Service** → connect GitHub repo
2. Set **Docker** as runtime, Dockerfile path: `Dockerfile.api`
3. Environment variables:
   ```
   REDIS_URL=<internal Redis URL from step 1>
   LOG_LEVEL=INFO
   ```
4. Instance type: **Starter** ($7/mo) or **Standard** ($25/mo)

### Step 3 — Scraper Daemon

1. **New** → **Background Worker** → same repo
2. Dockerfile path: `Dockerfile.scraper`
3. Environment variables:
   ```
   REDIS_URL=<same Redis URL>
   SCRAPE_INTERVAL_SECONDS=5
   SCRAPE_TIMEOUT_MS=30000
   RECOVERY_DELAY_SECONDS=5
   LOG_LEVEL=INFO
   ```
4. Instance type: **Standard** ($25/mo) — needs at least 2 GB RAM for Chromium

---

## DigitalOcean App Platform

### Using App Spec

Create `.do/app.yaml`:

```yaml
name: metal-price-api
services:
  - name: api
    dockerfile_path: Dockerfile.api
    http_port: 8000
    instance_count: 1
    instance_size_slug: basic-s
    envs:
      - key: REDIS_URL
        scope: RUN_TIME
        value: ${redis.DATABASE_URL}
      - key: LOG_LEVEL
        value: INFO

workers:
  - name: scraper-daemon
    dockerfile_path: Dockerfile.scraper
    instance_count: 1
    instance_size_slug: professional-s
    envs:
      - key: REDIS_URL
        scope: RUN_TIME
        value: ${redis.DATABASE_URL}
      - key: SCRAPE_INTERVAL_SECONDS
        value: "5"
      - key: SCRAPE_TIMEOUT_MS
        value: "30000"

databases:
  - name: redis
    engine: REDIS
    version: "7"
```

---

## Monitoring & Logs

### Docker Compose (VPS)

```bash
# Follow all logs
docker compose logs -f

# Specific service
docker compose logs -f scraper-daemon
docker compose logs -f api
docker compose logs -f nginx

# Resource usage
docker stats

# Redis memory
docker exec metal-redis redis-cli INFO memory | grep used_memory_human
```

### Set Up Log Rotation

Create `/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

Then restart Docker:

```bash
sudo systemctl restart docker
```

---

## Troubleshooting

### Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| `502 Bad Gateway` | API container is not running or still starting | `docker compose logs api` — wait for Redis connection |
| `503 Service Unavailable` | Scraper hasn't populated Redis yet | Wait 15-30s after startup for first scrape |
| Scraper OOM killed | Chromium needs more memory | Increase `memory` limit or `shm_size` |
| Nginx returns `429` | Rate limit exceeded | Adjust `rate` and `burst` in `nginx.conf` |
| Redis connection refused | Redis container has not started | Check `docker compose ps` — ensure Redis is healthy |
| Prices show `null` | TradingView changed HTML structure | Check scraper logs, update CSS selector in `scraper_daemon.py` |

### Auto-Restart on Boot (VPS)

```bash
# Enable Docker to start on boot
sudo systemctl enable docker

# Containers with restart: unless-stopped will auto-start
```

### Update Deployment

```bash
cd metal-API-tradingview
git pull origin main
docker compose up --build -d
```
