#!/usr/bin/env sh
set -eu

# This is intentionally manual. The public demo does not run a periodic worker
# or indexer; re-run after loading a new sanitized demo dataset.
deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$deployment_dir"

docker compose --env-file .env -f compose.yaml exec -T omai omai-vector-db-migrate
docker compose --env-file .env -f compose.yaml exec -T omai sh -ec 'omai-index-operational-text --site-id "$DEMO_SITE_ID" --reset-site'

echo "Created the vector schema and indexed the configured demo site."
