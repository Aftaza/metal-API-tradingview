# 🚀 Panduan Deploy di VPS Kecil (1 Core / 1GB RAM)

## Gambaran Arsitektur

Konfigurasi khusus VPS kecil menggunakan **1 container scraper unified** yang menggabungkan semua target (Gold, Silver, Copper, USDIDR) ke dalam **1 browser Chromium** yang scrape secara **berurutan (sequential)**, bukan paralel.

### Perbandingan Resource

| Komponen | docker-compose.yml (default) | docker-compose.lowmem.yml |
|----------|------------------------------|---------------------------|
| Nginx | 64 MB | 32 MB |
| Redis | 128 MB | 48 MB |
| API | 256 MB (2 workers) | 128 MB (1 worker) |
| Scraper Gold | 512 MB | — |
| Scraper Silver | 512 MB | — |
| Scraper Copper | 512 MB | — |
| Scraper USDIDR | 768 MB | — |
| Scraper Unified | — | 512 MB |
| **TOTAL** | **~2.7 GB** | **~720 MB** |
| **Jumlah Container** | **7** | **4** |
| **Jumlah Browser** | **4 Chromium** | **1 Chromium** |

### Trade-off

- ✅ **Hemat memory**: 1 browser vs 4 browser → ~75% lebih hemat
- ✅ **Lebih stabil**: Tidak ada memory contention antara scraper
- ⚠️ **Latensi data sedikit lebih tinggi**: Karena scrape sequential (gold→silver→copper→usdidr), setiap target di-update ~15-30 detik lebih lambat dibanding mode paralel
- ⚠️ **Jika satu target lambat**, target lain tertunda

---

## Langkah-Langkah Deploy

### 1. Persiapan VPS

```bash
# Update sistem
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose plugin
sudo apt install -y docker-compose-plugin

# Tambah user ke group docker (agar tidak perlu sudo setiap kali)
sudo usermod -aG docker $USER

# Logout dan login kembali agar group berlaku
exit
```

### 2. Setup Swap (WAJIB untuk 1GB RAM!)

Swap adalah **WAJIB** karena Chromium kadang memerlukan burst memory yang melebihi 1GB.

```bash
# Cek apakah sudah ada swap
sudo swapon --show

# Buat swap file 2GB
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Buat permanen (survive reboot)
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Tuning swappiness — biarkan swap dipakai hanya kalau benar-benar perlu
sudo sysctl vm.swappiness=10
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf

# Verifikasi
free -h
```

**Output yang diharapkan:**
```
              total        used        free      shared  buff/cache   available
Mem:          981Mi       150Mi       400Mi        10Mi       430Mi       700Mi
Swap:         2.0Gi          0B       2.0Gi
```

### 3. Optimasi Kernel untuk Docker

```bash
# Tambahkan parameter optimasi
cat << 'EOF' | sudo tee -a /etc/sysctl.conf
# Docker / Container optimizations for low-memory VPS
vm.overcommit_memory=1
vm.vfs_cache_pressure=50
net.core.somaxconn=512
net.ipv4.tcp_max_syn_backlog=512
EOF

sudo sysctl -p
```

### 4. Upload Project ke VPS

**Opsi A: Via Git (Direkomendasikan)**
```bash
# Di VPS
cd ~
git clone <YOUR_REPO_URL> metal-API-tradingview
cd metal-API-tradingview
```

**Opsi B: Via SCP (dari komputer lokal)**
```bash
# Di komputer lokal
scp -r ./metal-API-tradingview user@vps-ip:~/metal-API-tradingview
```

### 5. Buat File .env

```bash
cd ~/metal-API-tradingview

cat << 'EOF' > .env
# Port nginx (gunakan 80 untuk HTTP langsung)
NGINX_PORT=80

# Interval scraping (dalam detik) — lebih tinggi = lebih hemat resource
# 15 detik per-round, tapi sequential jadi total ~60-90 detik per full cycle
SCRAPE_INTERVAL_SECONDS=15

# Timeout per-page (dalam ms)
SCRAPE_TIMEOUT_MS=30000

# Delay sebelum retry saat error
RECOVERY_DELAY_SECONDS=5
EOF
```

### 6. Build & Jalankan

```bash
cd ~/metal-API-tradingview

# Build images (ini akan memakan waktu ~5-10 menit pertama kali)
docker compose -f docker-compose.lowmem.yml build

# Jalankan semua services
docker compose -f docker-compose.lowmem.yml up -d

# Cek status
docker compose -f docker-compose.lowmem.yml ps
```

### 7. Verifikasi

```bash
# Cek log scraper unified
docker logs -f metal-scraper-unified --tail 50

# Cek API berjalan
curl http://localhost/health

# Cek harga metal
curl http://localhost/api/prices

# Cek penggunaan memory
docker stats --no-stream
```

**Output `docker stats` yang diharapkan:**
```
CONTAINER ID   NAME                    CPU %   MEM USAGE / LIMIT   
xxxxxxxxxxxx   metal-nginx             0.01%   5MiB / 32MiB
xxxxxxxxxxxx   metal-redis             0.10%   4MiB / 48MiB
xxxxxxxxxxxx   metal-api-v2            0.50%   45MiB / 128MiB
xxxxxxxxxxxx   metal-scraper-unified   15.0%   250MiB / 512MiB
```

---

## Monitoring & Maintenance

### Cek Penggunaan Memory Secara Real-time

```bash
# Docker stats (live)
docker stats

# Sistem keseluruhan
watch -n 5 free -h

# Detailed per-container
docker stats --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
```

### Restart Jika Masalah

```bash
# Restart semua
docker compose -f docker-compose.lowmem.yml restart

# Restart scraper saja (paling sering bermasalah)
docker compose -f docker-compose.lowmem.yml restart scraper

# Rebuild & restart (jika ada perubahan kode)
docker compose -f docker-compose.lowmem.yml up -d --build
```

### Auto-restart via Crontab (Opsional)

```bash
# Tambahkan cron job untuk restart scraper setiap 6 jam
# Ini membantu mencegah memory leak di container
crontab -e

# Tambahkan baris ini:
0 */6 * * * docker restart metal-scraper-unified >> /var/log/scraper-restart.log 2>&1
```

### Membersihkan Docker Cache

VPS dengan disk kecil bisa kehabisan space karena Docker images/cache.

```bash
# Hapus images yang tidak dipakai
docker image prune -f

# Hapus semua resources yang tidak dipakai (aggressive)
docker system prune -af

# Cek disk usage Docker
docker system df
```

---

## Troubleshooting

### ❌ Scraper crash karena OOM (Out of Memory)

**Gejala:** Container `metal-scraper-unified` terus restart, `docker logs` menunjukkan `Killed`.

**Solusi:**
1. Pastikan swap sudah aktif: `free -h`
2. Tingkatkan scrape interval: `SCRAPE_INTERVAL_SECONDS=30`
3. Restart container: `docker restart metal-scraper-unified`

### ❌ Build gagal karena memory

**Gejala:** `docker compose build` gagal dengan error memory.

**Solusi:**
```bash
# Build satu-satu, bukan semua sekaligus
docker compose -f docker-compose.lowmem.yml build nginx
docker compose -f docker-compose.lowmem.yml build redis
docker compose -f docker-compose.lowmem.yml build api
docker compose -f docker-compose.lowmem.yml build scraper
```

### ❌ Harga tidak ter-update

**Gejala:** API mengembalikan data lama atau kosong.

**Solusi:**
```bash
# Cek log scraper
docker logs metal-scraper-unified --tail 100

# Cek Redis langsung
docker exec metal-redis redis-cli GET price:gold
docker exec metal-redis redis-cli GET price:silver
docker exec metal-redis redis-cli GET price:copper
docker exec metal-redis redis-cli GET price:usdidr
```

### ❌ Disk penuh

```bash
# Cek disk
df -h

# Bersihkan Docker
docker system prune -af
docker volume prune -f

# Bersihkan log Docker
sudo truncate -s 0 /var/lib/docker/containers/*/*-json.log
```

---

## Tips Tambahan untuk VPS Kecil

### 1. Logging yang Lebih Hemat Disk

Tambahkan logging limit di docker-compose.lowmem.yml jika disk VPS kecil:

```yaml
services:
  scraper:
    logging:
      driver: "json-file"
      options:
        max-size: "5m"
        max-file: "2"
```

### 2. Gunakan Cloudflare (Gratis) di Depan VPS

Pasang Cloudflare sebagai proxy:
- **Cache API responses** (jika data hanya update setiap 15-30 detik, bisa di-cache)
- **Proteksi DDoS** gratis
- **SSL/HTTPS** gratis
- Mengurangi beban Nginx

### 3. Monitoring via UptimeRobot (Gratis)

Setup [UptimeRobot](https://uptimerobot.com/) untuk monitor:
- URL: `http://your-vps-ip/health`
- Interval: 5 menit
- Akan mengirim notifikasi jika API down

---

## Perbandingan Timeline Update

| Mode | Gold | Silver | Copper | USDIDR | Total Cycle |
|------|------|--------|--------|--------|-------------|
| **Paralel** (4 scraper) | ~10s | ~10s | ~10s | ~10s | ~10s |
| **Sequential** (1 scraper) | ~8s | ~16s | ~24s | ~35s | ~50-60s |

Dalam mode sequential, setiap metal bergantian di-scrape. Totalnya sekitar 50-60 detik per full cycle (termasuk JS rendering & delay antar target). Ini berarti setiap harga di-update ~1 menit sekali, yang masih sangat cukup untuk kebanyakan use case.
