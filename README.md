# akatsuki infrastructure

Infrastructure configuration for [akatsuki.gg](https://akatsuki.gg), an osu! private server.

This repo contains the production docker-compose, nginx configs, Terraform definitions, Grafana provisioning, and server bootstrap scripts. It is meant to describe the whole production host: Terraform creates the server, firewall, and DNS records, `setup.sh` provisions a fresh server, and CI keeps the host in sync with `main`.

## Repository structure

```
config/
  docker-compose.yml        # All production services; CI copies it to /opt/akatsuki and applies it
  nginx/                    # Reverse proxy configs (sites-enabled, upstreams, TLS termination)
  mysql/                    # MySQL tuning (99-akatsuki.cnf)
  redis/                    # Redis config
  grafana/provisioning/     # Datasources and dashboards
  prometheus/               # Prometheus scrape config
  loki/                     # Log storage config
  alloy/                    # Collector: container, journal and host-file logs, MySQL/Redis metrics
  thanos/                   # Object storage config template (bucket.yml.tpl)
  vault/                    # HashiCorp Vault config
  apt/, cron/, systemd/     # apt periodic settings, host cron jobs, journald limits
tf/                         # Terraform (Hetzner Cloud server and firewall, Cloudflare DNS)
scripts/                    # Firewall, metrics backfill
systemd/                    # Unit that re-applies the firewall at boot
setup.sh                    # Server bootstrap (full provisioning)
```

## Local development setup

This guide covers setting up core Akatsuki services locally. It's written for AI coding agents but works for humans too.

### Prerequisites

- Docker and Docker Compose
- MySQL 8.0+
- Redis 6+
- RabbitMQ 3.x (optional, only needed for score processing pipeline)
- Git access to [osuAkatsuki](https://github.com/osuAkatsuki) repos

### 1. Data stores

Start MySQL, Redis, and optionally RabbitMQ. You can run them natively or via Docker:

```bash
# Option A: Native (macOS example)
brew install mysql redis rabbitmq
brew services start mysql
brew services start redis
brew services start rabbitmq  # optional

# Option B: Docker (just the data stores)
docker run -d --name mysql -p 3306:3306 -e MYSQL_ALLOW_EMPTY_PASSWORD=1 mysql:8.0 \
  --default-authentication-plugin=mysql_native_password
docker run -d --name redis -p 6379:6379 redis:7
docker run -d --name rabbitmq -p 5672:5672 rabbitmq:3  # optional
```

### 2. Database schema

Clone and apply the schema migrations:

```bash
git clone git@github.com:osuAkatsuki/mysql-database.git
cd mysql-database

# Create the database
mysql -u root -e "CREATE DATABASE akatsuki;"

# Apply migrations (they're numbered and forward-only)
for f in migrations/*.up.sql; do
  mysql -u root akatsuki < "$f"
done
```

This creates ~60 tables: `users`, `scores`, `beatmaps`, `clans`, etc.

### 3. Service configuration

All services support two configuration modes:

**Without Vault (recommended for local dev):**

Set `PULL_SECRETS_FROM_VAULT=0` (or omit it) and pass environment variables directly. Each service repo has a `.env.example` showing required vars.

**With Vault (production):**

Services use [akatsuki-cli](https://github.com/osuAkatsuki/akatsuki-cli) to pull secrets at startup via `akatsuki vault get <service-name> <env> -o .env`.

### 4. Core services

These are the minimum services for a functional osu! server:

| Service | Repo | Port | Purpose |
|---------|------|------|---------|
| bancho-service-rs | [osuAkatsuki/bancho-service-rs](https://github.com/osuAkatsuki/bancho-service-rs) | 5001 | Game server (osu! protocol) |
| score-service | [osuAkatsuki/score-service](https://github.com/osuAkatsuki/score-service) | 7000 | Score submission |
| beatmaps-service | [osuAkatsuki/beatmaps-service](https://github.com/osuAkatsuki/beatmaps-service) | 8080 | Beatmap metadata & files |
| performance-service | [osuAkatsuki/performance-service](https://github.com/osuAkatsuki/performance-service) | 8665 | PP calculation |
| akatsuki-api | [osuAkatsuki/akatsuki-api](https://github.com/osuAkatsuki/akatsuki-api) | 40001 | REST API |
| hanayo | [osuAkatsuki/hanayo](https://github.com/osuAkatsuki/hanayo) | 46221 | Website frontend |

Clone each service, copy `.env.example` to `.env`, and fill in the connection details:

```bash
# Common env vars across most services
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASS=
DB_NAME=akatsuki
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
APP_ENV=local
APP_COMPONENT=api
```

score-service uses separate read/write DB connections (`READ_DB_*` / `WRITE_DB_*`). For local dev, point both at the same MySQL instance.

### 5. Service dependencies

```
beatmaps-service  (standalone - needs MySQL, osu! API key)
     ^
     |
performance-service  (needs beatmaps-service for .osu files)
     ^
     |
score-service  (needs beatmaps-service, performance-service, MySQL, Redis, S3)
     ^
     |
bancho-service-rs  (needs beatmaps-service, performance-service, MySQL, Redis)

akatsuki-api  (standalone - needs MySQL, Redis)
hanayo  (needs akatsuki-api, MySQL, Redis)
```

Start services bottom-up: beatmaps-service first, then performance-service, then the rest.

### 6. S3 storage (optional)

score-service and beatmaps-service use S3 for replays and beatmap files. For local dev, you can use [MinIO](https://min.io/) as a local S3-compatible store:

```bash
docker run -d --name minio -p 9000:9000 -p 9001:9001 \
  minio/minio server /data --console-address ":9001"

# Default credentials: minioadmin/minioadmin
# Create buckets via the console at http://localhost:9001
```

### 7. nginx (optional)

If you want domain-based routing locally (matching production), copy the nginx configs:

```bash
cp config/nginx/nginx.conf /etc/nginx/nginx.conf
cp config/nginx/sites-enabled/*.conf /etc/nginx/sites-enabled/
# ssl-termination.conf needs the production origin certificate; drop it locally
rm /etc/nginx/sites-enabled/ssl-termination.conf

# Edit upstreams.conf to point at localhost ports
# Add entries to /etc/hosts:
# 127.0.0.1  akatsuki.gg c.akatsuki.gg osu.akatsuki.gg a.akatsuki.gg
```

For simple local dev, you can skip nginx and hit services directly on their ports.

## Deployment

Push to `main` triggers CI (`.github/workflows/deploy.yml`), which SSHes to the server, checks out `main` in `/opt/akatsuki/hetzner-infra`, and applies it:

- Copies host config (apt, journald, MySQL, cron, Vault, nginx) into place. `/etc/nginx/sites-enabled` is mirrored, so deleting a file from the repo removes that site. nginx is validated and reloaded.
- Applies the firewall and enables `akatsuki-firewall.service`, which re-applies it at boot.
- Restarts Redis if `redis.conf` changed. CI never restarts MySQL or Vault; restart those by hand (Vault then needs unsealing).
- Runs `docker compose up -d`, which recreates containers whose definition changed and starts any that are stopped (cron services are profile-gated and stay off). Then restarts any container whose mounted config under `config/` changed after it started.

Application images are built and deployed from each service's own repo; their deploys pull the image and recreate their own service.

## Rebuilding the server

1. **Terraform** (`tf/`) creates the server, its firewall, and the DNS records. It runs in CI (`.github/workflows/terraform.yml`): pull requests that touch `tf/` get a plan, and merges to `main` apply it, using the repository secrets `HCLOUD_TOKEN`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, `SSH_PUBLIC_KEY`, and `WASABI_ACCESS_KEY` / `WASABI_SECRET_KEY` (the state bucket). DNS records point at the server's address, so a new server updates them. The server has `prevent_destroy`, so CI can never delete or replace it.
2. **Origin certificate**: install the Cloudflare origin certificate at `/etc/ssl/certs/akatsuki-origin.pem` and its key at `/etc/ssl/private/akatsuki-origin.key`.
3. **Bootstrap**: clone this repo to `/opt/akatsuki/hetzner-infra`, create `.env` from `.env.example`, then run `set -a && source .env && set +a && ./setup.sh`.
4. Follow the manual steps `setup.sh` prints at the end (unseal Vault, create `/opt/akatsuki/.env`, restore MySQL, start services).
   Backups contain the `akatsuki` database but not MySQL's accounts, so recreate those by hand after restoring. Each service's password is in its Vault entry.

Secrets are kept out of the repo, in these files on the host:

| File | Contents | Created by |
|------|----------|------------|
| `/opt/akatsuki/hetzner-infra/.env` | `setup.sh` inputs (see `.env.example`) | hand |
| `/opt/akatsuki/.env` | Variables `docker-compose.yml` interpolates: `VAULT_TOKEN`, `WASABI_*`, `GRAFANA_*`, `MYSQL_EXPORTER_PASSWORD` | hand |
| `/etc/vault.d/vault.env` | Vault's S3 credentials | `setup.sh` |
| `config/thanos/bucket.yml` | Thanos object storage config (gitignored) | `setup.sh`, from `bucket.yml.tpl` |
| `/etc/ssl/private/akatsuki-origin.key` | Cloudflare origin certificate key | hand |

## License

MIT
