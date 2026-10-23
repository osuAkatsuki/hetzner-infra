#!/usr/bin/env bash
set -euo pipefail

# Restore MySQL database from S3 backup
# Usage: ./restore-db.sh [backup_name]
# Example: ./restore-db.sh 2026-03-06T12:16Z

BACKUP_NAME="${1:-}"
S3_ENDPOINT_URL="https://s3.ca-central-1.wasabisys.com"
S3_BUCKET_NAME="akatsuki.pw"
EXPORT_DIR="/tmp/db-restore"

if [ -z "$BACKUP_NAME" ]; then
    echo "Available backups:"
    aws s3 ls "s3://${S3_BUCKET_NAME}/db-backups/" \
        --endpoint-url="${S3_ENDPOINT_URL}" \
        | tail -10
    echo ""
    echo "Usage: $0 <backup_name>"
    echo "Example: $0 2026-03-06T12:16Z"
    exit 1
fi

echo "=== Downloading backup ${BACKUP_NAME} from S3 ==="
rm -rf "${EXPORT_DIR}"
mkdir -p "${EXPORT_DIR}"
aws s3 sync \
    --endpoint-url="${S3_ENDPOINT_URL}" \
    "s3://${S3_BUCKET_NAME}/db-backups/${BACKUP_NAME}" \
    "${EXPORT_DIR}"

echo "=== Creating database ==="
mysql -e "CREATE DATABASE IF NOT EXISTS akatsuki;"

echo "=== Restoring tables ==="
total=$(ls "${EXPORT_DIR}"/*.sql.gz 2>/dev/null | wc -l)
count=0
for f in "${EXPORT_DIR}"/*.sql.gz; do
    table_name=$(basename "$f" .sql.gz | sed 's/^akatsuki\.//')
    count=$((count + 1))
    echo "[${count}/${total}] Restoring ${table_name}..."
    gunzip -c "$f" | mysql akatsuki
done

echo "=== Restore complete ==="
echo "Restored ${count} tables from backup ${BACKUP_NAME}"

rm -rf "${EXPORT_DIR}"
