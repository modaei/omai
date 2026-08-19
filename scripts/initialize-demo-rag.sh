#!/usr/bin/env sh
set -eu

# Recreate derived RAG data after loading a new sanitized demo database.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
config_file=${OMAI_ENV_FILE:-"$project_dir/.env"}
site_id=$(sed -n 's/^DEMO_SITE_ID=//p' "$config_file" | tail -n 1)

if [ -z "$site_id" ]; then
    echo "DEMO_SITE_ID must be configured in $config_file." >&2
    exit 1
fi

python_bin=${OMAI_PYTHON:-"$project_dir/.venv/bin/python"}
PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" -m omai.rag.migrate_vector_db
PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" -m omai.rag.index_operational_text --site-id "$site_id" --reset-site
