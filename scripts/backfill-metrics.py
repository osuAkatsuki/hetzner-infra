#!/usr/bin/env python3
"""Backfill metrics from Grafana Cloud Prometheus into Thanos S3 storage.

Streams data to per-block files on disk to keep memory usage low.
Processes one metric at a time, appending samples to 2h block files.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path

GRAFANA_CLOUD_URL = (
    "https://prometheus-prod-32-prod-ca-east-0.grafana.net/api/prom"
)
GRAFANA_CLOUD_USER = "2240917"
GRAFANA_CLOUD_TOKEN = os.environ.get(
    "GRAFANA_CLOUD_TOKEN",
    "glc_eyJvIjoiMTMzNzAxMSIsIm4iOiJoZXR6bmVyLW1pZ3JhdGlvbi1yZW"
    "FkLWhldHpuZXItbWlncmF0aW9uLXJlYWQiLCJrIjoiWWI2MzM5RHRKUnJJ"
    "NDQydzY0aUNMZzlqIiwibSI6eyJyIjoicHJvZC1jYS1lYXN0LTAifX0=",
)

WORK_DIR = Path("/tmp/metrics-backfill")
S3_BUCKET = "akatsuki.pw"
S3_ENDPOINT = "https://s3.ca-central-1.wasabisys.com"
S3_PREFIX = "observability/thanos"

START_TIME = datetime(2025, 3, 1, tzinfo=timezone.utc)
END_TIME = datetime(2026, 3, 7, tzinfo=timezone.utc)

QUERY_WINDOW = timedelta(days=7)
STEP = "300s"

BLOCK_SECONDS = 2 * 3600  # 2h blocks for Thanos

MAX_RETRIES = 3
API_DELAY = 0.2

PROGRESS_FILE = WORK_DIR / "progress.json"


def make_auth_header():
    creds = b64encode(
        f"{GRAFANA_CLOUD_USER}:{GRAFANA_CLOUD_TOKEN}".encode()
    ).decode()
    return f"Basic {creds}"


AUTH_HEADER = make_auth_header()


def query_api(endpoint, params=None):
    url = f"{GRAFANA_CLOUD_URL}/{endpoint}"
    if params:
        data = urllib.parse.urlencode(params).encode()
    else:
        data = None

    req = urllib.request.Request(url, data=data)
    req.add_header("Authorization", AUTH_HEADER)

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 5))
                print(f"  Rate limited, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            if attempt == MAX_RETRIES - 1:
                print(f"  HTTP {e.code}: {body[:200]}", flush=True)
                return None
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                print(f"  Failed after {MAX_RETRIES} attempts: {e}",
                      flush=True)
                return None
            time.sleep(2 ** attempt)
    return None


def get_all_metric_names():
    result = query_api("api/v1/label/__name__/values")
    return sorted(result["data"])


def escape_label_value(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def append_series_to_block_files(series_list, blocks_dir):
    """Append query_range result directly to per-block files on disk.

    Returns number of samples written.
    """
    file_handles = {}
    samples = 0

    try:
        for series in series_list:
            metric = series["metric"]
            values = series["values"]

            metric_name = metric.get("__name__", "unknown")
            labels = {
                k: v for k, v in sorted(metric.items()) if k != "__name__"
            }
            if labels:
                label_str = (
                    "{"
                    + ",".join(
                        f'{k}="{escape_label_value(v)}"'
                        for k, v in labels.items()
                    )
                    + "}"
                )
            else:
                label_str = ""

            for timestamp, value in values:
                ts = float(timestamp)
                block_start = int(ts - (ts % BLOCK_SECONDS))
                block_file = blocks_dir / f"{block_start}.txt"

                if block_start not in file_handles:
                    file_handles[block_start] = open(block_file, "a")

                file_handles[block_start].write(
                    f"{metric_name}{label_str} {value} {timestamp}\n"
                )
                samples += 1
    finally:
        for fh in file_handles.values():
            fh.close()

    return samples


def create_and_upload_block(block_file, output_base):
    """Create a TSDB block from an OpenMetrics file and upload to S3."""
    block_dir = output_base / block_file.stem
    block_dir.mkdir(exist_ok=True)

    # Add EOF marker
    with open(block_file, "a") as f:
        f.write("# EOF\n")

    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{block_file}:/data/metrics.txt:ro",
            "-v", f"{block_dir}:/data/output:rw",
            "--entrypoint", "promtool",
            "prom/prometheus:latest",
            "tsdb", "create-blocks-from", "openmetrics",
            "/data/metrics.txt",
            "/data/output",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"    promtool error: {result.stderr[:300]}", flush=True)
        shutil.rmtree(block_dir, ignore_errors=True)
        return 0

    uploaded = 0
    for ulid_dir in block_dir.iterdir():
        if not ulid_dir.is_dir():
            continue

        s3_path = f"s3://{S3_BUCKET}/{S3_PREFIX}/{ulid_dir.name}/"
        result = subprocess.run(
            [
                "aws", "s3", "sync",
                str(ulid_dir), s3_path,
                "--endpoint-url", S3_ENDPOINT,
                "--quiet",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    S3 upload error: {result.stderr[:200]}", flush=True)
        else:
            uploaded += 1

    shutil.rmtree(block_dir, ignore_errors=True)
    return uploaded


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed_windows": []}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f)


def main():
    print("=== Grafana Cloud Metrics Backfill ===", flush=True)

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    progress = load_progress()
    completed = set(progress["completed_windows"])

    print("Fetching metric names...", flush=True)
    metric_names = get_all_metric_names()
    print(f"Found {len(metric_names)} metrics", flush=True)

    windows = []
    current = START_TIME
    while current < END_TIME:
        window_end = min(current + QUERY_WINDOW, END_TIME)
        windows.append((current, window_end))
        current = window_end

    print(f"Time range: {START_TIME.date()} to {END_TIME.date()}", flush=True)
    print(f"Total windows: {len(windows)} ({QUERY_WINDOW.days}d each)",
          flush=True)
    print(f"Already completed: {len(completed)}", flush=True)
    print(flush=True)

    total_blocks = 0
    total_samples = 0

    for win_idx, (win_start, win_end) in enumerate(windows):
        win_key = f"{win_start.isoformat()}_{win_end.isoformat()}"
        if win_key in completed:
            continue

        print(f"[{win_idx + 1}/{len(windows)}] "
              f"{win_start.date()} to {win_end.date()}", flush=True)

        blocks_dir = WORK_DIR / f"window_{win_idx}"
        blocks_dir.mkdir(exist_ok=True)

        window_samples = 0
        metrics_with_data = 0
        errors = 0

        for m_idx, metric_name in enumerate(metric_names):
            params = {
                "query": metric_name,
                "start": win_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": win_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "step": STEP,
            }
            result = query_api("api/v1/query_range", params)

            if result is None:
                errors += 1
                time.sleep(API_DELAY)
                continue

            if (
                result.get("status") == "success"
                and result["data"]["result"]
            ):
                samples = append_series_to_block_files(
                    result["data"]["result"], blocks_dir
                )
                if samples > 0:
                    window_samples += samples
                    metrics_with_data += 1

            if (m_idx + 1) % 100 == 0:
                print(f"  {m_idx + 1}/{len(metric_names)} metrics queried, "
                      f"{window_samples:,} samples so far", flush=True)

            time.sleep(API_DELAY)

        print(f"  Fetched {metrics_with_data} metrics with data, "
              f"{window_samples:,} samples, {errors} errors", flush=True)
        total_samples += window_samples

        # Create TSDB blocks and upload
        block_files = sorted(blocks_dir.glob("*.txt"))
        if block_files:
            print(f"  Creating {len(block_files)} TSDB blocks...", flush=True)
            output_base = WORK_DIR / f"output_{win_idx}"
            output_base.mkdir(exist_ok=True)

            for bf_idx, block_file in enumerate(block_files):
                uploaded = create_and_upload_block(block_file, output_base)
                total_blocks += uploaded

                if (bf_idx + 1) % 20 == 0:
                    print(f"    {bf_idx + 1}/{len(block_files)} blocks "
                          f"processed", flush=True)

            shutil.rmtree(output_base, ignore_errors=True)

        # Cleanup window data
        shutil.rmtree(blocks_dir, ignore_errors=True)

        completed.add(win_key)
        progress["completed_windows"] = list(completed)
        save_progress(progress)

        print(f"  Window complete: {total_blocks} total blocks uploaded",
              flush=True)

    print(f"\n=== BACKFILL COMPLETE ===", flush=True)
    print(f"Total samples: {total_samples:,}", flush=True)
    print(f"Total TSDB blocks uploaded: {total_blocks}", flush=True)


if __name__ == "__main__":
    main()
