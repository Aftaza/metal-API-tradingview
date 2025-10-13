# Metal Price API - TradingView

Real-time API for metal prices scraped from TradingView (Gold, Silver, Platinum, Palladium, Copper)

## Prerequisites

- Docker
- Docker Compose

## Quick Start

### Production Setup

1. Build and start the services:
```bash
docker-compose up --build
```

2. The API will be available at:
   - API: `http://localhost:8000`
   - Nginx Proxy: `http://localhost`

### Development Setup

1. Run in development mode with auto-reload:
```bash
docker-compose -f docker-compose.dev.yml up --build
```

## API Endpoints

- `GET /` - API information and available endpoints
- `GET /prices` - Get all metal prices
- `GET /prices/{metal}` - Get specific metal price (gold, silver, platinum, palladium, copper)
- `GET /health` - Health check
- `GET /metrics` - Monitoring metrics
- `GET /symbols` - Get list of symbols
- `POST /refresh` - Manual price refresh

## Environment Variables

The application uses the following environment variables:

- `LOG_LEVEL` - Log level (default: INFO)
- `UPDATE_INTERVAL` - Price update interval in seconds (default: 600)
- `SCRAP_TIMEOUT` - Scraping timeout in seconds (default: 20)

## Configuration

- The application scrapes prices every 10 minutes by default
- Chrome runs in headless mode without a GPU for container compatibility
- Each scraping operation uses a unique Chrome profile to avoid conflicts

## Troubleshooting

1. **Chrome errors**: Make sure the container has enough memory (at least 1GB)
2. **Permission errors**: Check that the /tmp directory has proper permissions
3. **Connection timeouts**: Verify your container has internet access

## Architecture

- `main.py` - FastAPI application with Selenium scraping
- `nginx.conf` - Reverse proxy with rate limiting
- `Dockerfile` - Application container
- `docker-compose.yml` - Production setup
- `docker-compose.dev.yml` - Development setup