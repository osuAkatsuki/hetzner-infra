#!/usr/bin/env bash
set -euo pipefail

# Hetzner Cloud CPX42 server bootstrap script
# Installs and configures all services for Akatsuki production

# CI deploys from this checkout, and the firewall unit runs scripts/apply-firewall.sh from it
if [ "$PWD" != /opt/akatsuki/hetzner-infra ]; then
    echo "ERROR: run from a clone of this repo at /opt/akatsuki/hetzner-infra"
    exit 1
fi

echo "=== Creating swap file (4GB) ==="
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

echo "=== Configuring automatic upgrades ==="
install -D -m 0644 config/apt/apt.conf.d/99-akatsuki-periodic-upgrades /etc/apt/apt.conf.d/99-akatsuki-periodic-upgrades

echo "=== Installing system packages ==="
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker.io docker-compose-v2 \
    mysql-server redis-server rabbitmq-server \
    postgresql postgresql-contrib postgresql-16-postgis-3 \
    nginx \
    python3-pip python3-venv \
    curl jq
pip3 install --break-system-packages awscli

echo "=== Configuring firewall ==="
bash scripts/apply-firewall.sh
install -m 0644 systemd/akatsuki-firewall.service /etc/systemd/system/akatsuki-firewall.service
systemctl daemon-reload
systemctl enable akatsuki-firewall.service

echo "=== Configuring journald ==="
mkdir -p /etc/systemd/journald.conf.d
cp config/systemd/journald.conf.d/akatsuki.conf /etc/systemd/journald.conf.d/akatsuki.conf
systemctl restart systemd-journald
journalctl --vacuum-size=1G

echo "=== Installing HashiCorp Vault ==="
wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
    > /etc/apt/sources.list.d/hashicorp.list
apt-get update
apt-get install -y vault

echo "=== Installing akatsuki-cli ==="
pip3 install --break-system-packages git+https://github.com/osuAkatsuki/akatsuki-cli

echo "=== Configuring Vault ==="
if [ -z "${VAULT_S3_ACCESS_KEY:-}" ] || [ -z "${VAULT_S3_SECRET_KEY:-}" ]; then
    echo "ERROR: VAULT_S3_ACCESS_KEY and VAULT_S3_SECRET_KEY must be set"
    echo "Source your .env file first: source .env"
    exit 1
fi
id vault 2>/dev/null || useradd --system --home /opt/vault --shell /bin/false vault
mkdir -p /opt/vault/data /opt/vault/tls /vault /etc/vault.d
touch /vault/vault-audit.log
# Vault's package installs a default config at /etc/vault.d/vault.hcl;
# overwrite it with ours so the default systemd unit picks it up. The unit
# also loads /etc/vault.d/vault.env, which holds the S3 credentials.
cp config/vault/config.hcl /etc/vault.d/vault.hcl
install -m 0600 /dev/null /etc/vault.d/vault.env
printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' "$VAULT_S3_ACCESS_KEY" "$VAULT_S3_SECRET_KEY" > /etc/vault.d/vault.env
chown -R vault:vault /opt/vault /vault /etc/vault.d
systemctl enable vault

echo "=== Configuring MySQL ==="
cp config/mysql/99-akatsuki.cnf /etc/mysql/mysql.conf.d/99-akatsuki.cnf
# Override default bind-address
sed -i 's/^bind-address\s*=.*/bind-address = 0.0.0.0/' /etc/mysql/mysql.conf.d/mysqld.cnf
systemctl restart mysql

echo "=== Configuring Redis ==="
cp config/redis/redis.conf /etc/redis/redis.conf
systemctl restart redis-server

echo "=== Configuring RabbitMQ ==="
if [ -z "${RABBITMQ_PASS:-}" ]; then
    echo "ERROR: RABBITMQ_PASS must be set"
    exit 1
fi
rabbitmqctl add_user rmq "$RABBITMQ_PASS" 2>/dev/null || rabbitmqctl change_password rmq "$RABBITMQ_PASS"
rabbitmqctl set_permissions -p / rmq '.*' '.*' '.*'

echo "=== Configuring PostgreSQL ==="
if [ -z "${POSTGRES_K8S_PASS:-}" ]; then
    echo "ERROR: POSTGRES_K8S_PASS must be set"
    exit 1
fi
sudo -u postgres psql -c "CREATE USER k8s WITH PASSWORD '$POSTGRES_K8S_PASS';" 2>/dev/null || \
    sudo -u postgres psql -c "ALTER USER k8s WITH PASSWORD '$POSTGRES_K8S_PASS';"
for db in akatsuki_ai_bot akatsuki_management_bot travelplanner; do
    sudo -u postgres psql -c "CREATE DATABASE $db OWNER k8s;" 2>/dev/null || true
done

echo "=== Configuring nginx ==="
cp config/nginx/nginx.conf /etc/nginx/nginx.conf
cp config/nginx/robots.txt /etc/nginx/robots.txt
rm -rf /etc/nginx/sites-enabled/*
cp config/nginx/sites-enabled/*.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
# ssl-termination.conf needs the Cloudflare origin certificate, which is kept out of the repo
for f in /etc/ssl/certs/akatsuki-origin.pem /etc/ssl/private/akatsuki-origin.key; do
    if [ ! -s "$f" ]; then
        echo "ERROR: $f is missing; install the Cloudflare origin certificate and key first"
        exit 1
    fi
done
nginx -t
systemctl restart nginx

echo "=== Configuring Docker Compose ==="
if [ -z "${WASABI_ACCESS_KEY:-}" ] || [ -z "${WASABI_SECRET_KEY:-}" ]; then
    echo "ERROR: WASABI_ACCESS_KEY and WASABI_SECRET_KEY must be set"
    exit 1
fi
envsubst < config/thanos/bucket.yml.tpl > config/thanos/bucket.yml
# docker-compose.yml mounts files from ./config
ln -sfn hetzner-infra/config /opt/akatsuki/config
cp config/docker-compose.yml /opt/akatsuki/docker-compose.yml

echo "=== Configuring cron jobs ==="
cp config/cron/akatsuki /etc/cron.d/akatsuki
chmod 644 /etc/cron.d/akatsuki
mkdir -p /opt/akatsuki/logs

echo "=== Enabling services ==="
systemctl enable docker mysql redis-server rabbitmq-server postgresql nginx vault

echo "=== Setup complete ==="
echo ""
echo "Remaining manual steps:"
echo "  1. Unseal Vault:  export VAULT_ADDR=http://127.0.0.1:8200 && vault operator unseal <key>"
echo "  2. Create /opt/akatsuki/.env with the variables docker-compose.yml uses (see README)"
echo "  3. Restore MySQL:  see restore-db.sh"
echo "  4. Start services: cd /opt/akatsuki && docker compose up -d"
