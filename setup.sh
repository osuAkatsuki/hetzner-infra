#!/usr/bin/env bash
set -euo pipefail

# Hetzner AX42-U server bootstrap script
# Installs and configures all services for Akatsuki production

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
mkdir -p /opt/vault/data /opt/vault/tls /vault
touch /vault/vault-audit.log
envsubst < config/vault/config.hcl > /opt/vault/config.hcl
chown -R vault:vault /opt/vault /vault
cp systemd/vault.service /etc/systemd/system/vault.service
systemctl daemon-reload
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
rm -rf /etc/nginx/sites-enabled/*
cp config/nginx/sites-enabled/*.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

echo "=== Enabling services ==="
systemctl enable docker mysql redis-server rabbitmq-server postgresql nginx vault

echo "=== Setup complete ==="
echo ""
echo "Remaining manual steps:"
echo "  1. Unseal Vault:  export VAULT_ADDR=http://127.0.0.1:8200 && vault operator unseal <key>"
echo "  2. Create .env:   echo 'VAULT_TOKEN=<token>' > /opt/akatsuki/.env"
echo "  3. Restore MySQL:  see restore-db.sh"
echo "  4. Create MySQL users: see create-mysql-users.sh"
echo "  5. Start services: cd /opt/akatsuki && docker compose up -d"
