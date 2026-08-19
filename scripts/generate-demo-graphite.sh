#!/usr/bin/env sh
set -eu

# Generate deterministic synthetic telemetry from sanitized metadata only.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_file=${1:-"$project_dir/data/synthetic-graphite.txt"}
days=${2:-30}
python_bin=${OMAI_PYTHON:-"$project_dir/venv/bin/python"}

mkdir -p "$(dirname -- "$output_file")"
PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" -m omai.demo.generate_graphite --days "$days" --output "$output_file"
