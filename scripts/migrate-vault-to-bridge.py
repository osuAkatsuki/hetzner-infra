"""Migrate Vault secrets from 127.0.0.1 to host.docker.internal.

One-time migration script for moving from host networking to bridge networking.
"""

import json
import subprocess
import sys

SERVICES = [
    "admin-panel",
    "ai-discord-bot",
    "air-conditioning",
    "akatsuki-api",
    "assets-service",
    "bancho-service-rs",
    "beatmaps-service",
    "hanayo",
    "management-discord-bot",
    "nachalo-konca",
    "new-cron",
    "payments-service",
    "performance-service",
    "profile-history-service",
    "rework-frontend",
    "score-service",
    "travelplanner-api",
    "users-service",
]

OLD_HOST = "127.0.0.1"
NEW_HOST = "host.docker.internal"


def get_secrets(service: str) -> dict[str, str]:
    result = subprocess.run(
        ["vault", "kv", "get", "-format=json", f"secrets/production/{service}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  SKIP: could not read secrets ({result.stderr.strip()})")
        return {}
    return json.loads(result.stdout)["data"]["data"]


def put_secrets(service: str, data: dict[str, str]) -> bool:
    kv_args = [f"{k}={v}" for k, v in data.items()]
    result = subprocess.run(
        ["vault", "kv", "put", f"secrets/production/{service}"] + kv_args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return False
    return True


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=== DRY RUN (no changes will be made) ===\n")

    for service in SERVICES:
        print(f"--- {service} ---")
        secrets = get_secrets(service)
        if not secrets:
            continue

        changed = {}
        for key, value in secrets.items():
            if OLD_HOST in str(value):
                new_value = str(value).replace(OLD_HOST, NEW_HOST)
                changed[key] = (value, new_value)

        if not changed:
            print("  No 127.0.0.1 references found")
            continue

        for key, (old_val, new_val) in changed.items():
            print(f"  {key}: {old_val} -> {new_val}")

        if not dry_run:
            updated = {**secrets}
            for key, (_, new_val) in changed.items():
                updated[key] = new_val
            if put_secrets(service, updated):
                print("  UPDATED")
            else:
                print("  FAILED")
                sys.exit(1)

    print("\nDone!")


if __name__ == "__main__":
    main()
