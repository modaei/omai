#!/usr/bin/env sh
set -eu

# Generate values from metadata only. The command intentionally never reads
# current or historical telemetry values from the sanitized database.
deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
metrics_file=${1:-"$deployment_dir/data/synthetic-graphite.txt"}
days=${2:-30}

case "$days" in
  ''|*[!0-9]*)
    echo "Days must be a positive integer." >&2
    exit 1
    ;;
esac

cd "$deployment_dir"
docker compose --env-file .env -f compose.yaml exec -T omai sh -ec \
    "omai-generate-demo-graphite --site-id \"\$DEMO_SITE_ID\" --days $days" \
    > "$metrics_file"

echo "Generated synthetic Graphite metrics at $metrics_file."
