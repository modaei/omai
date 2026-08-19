#!/usr/bin/env sh
set -eu

# Send generated Graphite plaintext metrics to a host-local or private Carbon
# endpoint using only the host's Bash TCP support.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
metrics_file=${1:-"$project_dir/data/synthetic-graphite.txt"}
graphite_host=${DEMO_GRAPHITE_HOST:-127.0.0.1}
graphite_port=${DEMO_GRAPHITE_PORT:-2003}

if [ ! -f "$metrics_file" ]; then
    echo "Synthetic Graphite data not found: $metrics_file" >&2
    exit 1
fi

bash -c 'cat > "/dev/tcp/$1/$2"' -- "$graphite_host" "$graphite_port" < "$metrics_file"
