#!/usr/bin/env bash
set -euo pipefail

# Generates hetzner-local env files from vault backups.
# Replaces K8s/DO networking with localhost equivalents.

VAULT_DIR="vault-backups"
OUT_DIR="env"

mkdir -p "$OUT_DIR"

for src in "$VAULT_DIR"/*.env; do
    svc=$(basename "$src")
    echo "Processing $svc..."

    sed \
        -e 's/10\.118\.0\.4/127.0.0.1/g' \
        -e 's|bancho-service-rs-api-production\.default\.svc\.cluster\.local|127.0.0.1:5001|g' \
        -e 's|bancho-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:5001|g' \
        -e 's|score-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:7000|g' \
        -e 's|beatmaps-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:8081|g' \
        -e 's|performance-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:8665|g' \
        -e 's|akatsuki-api-production\.default\.svc\.cluster\.local|127.0.0.1:40001|g' \
        -e 's|users-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:8082|g' \
        -e 's|assets-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:8083|g' \
        -e 's|avatars-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:8083|g' \
        -e 's|profile-history-service-api-production\.default\.svc\.cluster\.local|127.0.0.1:8085|g' \
        "$src" > "$OUT_DIR/$svc"
done

echo "Done. Override env files written to $OUT_DIR/"
echo ""
echo "Verify changes:"
grep -rn '10\.118\.0\.\|\.default\.svc\.cluster\.local' "$OUT_DIR/" && echo "WARNING: unreplaced values found!" || echo "All networking values replaced."
