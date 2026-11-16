#!/usr/bin/env bash
# Writes each compose service container's Docker restart count to node-exporter's textfile
# collector, as docker_container_restart_count. A crash-looping container can run for a second or
# two at a time, too briefly for cAdvisor to see each run, so the count comes from Docker itself.
# Run every minute from config/cron/akatsuki.
set -euo pipefail

dir=/var/lib/node_exporter/textfile
mkdir -p "$dir"

ids=$(docker ps -aq --filter label=com.docker.compose.project=akatsuki --filter label=com.docker.compose.oneoff=False)
# A container removed between the two calls makes inspect fail; the rest are still printed
counts=$(docker inspect -f '{{.Name}} {{index .Config.Labels "com.docker.compose.service"}} {{.RestartCount}}' $ids 2>/dev/null || true)

tmp=$(mktemp "$dir/.docker.prom.XXXXXX")
{
  echo "# HELP docker_container_restart_count Times Docker's restart policy has restarted the container since it was created."
  echo "# TYPE docker_container_restart_count gauge"
  while read -r name service count; do
    if [ -n "$name" ]; then
      printf 'docker_container_restart_count{name="%s",service="%s"} %s\n' "${name#/}" "$service" "$count"
    fi
  done <<< "$counts"
} > "$tmp"
chmod 644 "$tmp"
mv "$tmp" "$dir/docker.prom"
