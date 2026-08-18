#!/usr/bin/env sh
set -eu

# Input must be Graphite plaintext lines: metric.path value unix_timestamp.
# The synthetic-data generator writes this file from the anonymized demo model.
deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
metrics_file=${1:-"$deployment_dir/data/synthetic-graphite.txt"}

if [ ! -f "$metrics_file" ]; then
    echo "Synthetic Graphite data not found: $metrics_file" >&2
    exit 1
fi

cd "$deployment_dir"
docker compose --env-file .env -f compose.yaml exec -T graphite bash -c 'cat > /dev/tcp/127.0.0.1/2003' < "$metrics_file"

echo "Imported synthetic Graphite metrics from $metrics_file."
