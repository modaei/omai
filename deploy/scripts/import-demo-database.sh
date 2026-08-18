#!/usr/bin/env sh
set -eu

# The dump must be created by the separate sanitization process. This script
# never accepts or transforms a production database backup.
deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
dump_file=${1:-"$deployment_dir/data/ometrics-demo.sql.gz"}

if [ ! -f "$dump_file" ]; then
    echo "Sanitized database dump not found: $dump_file" >&2
    exit 1
fi

cd "$deployment_dir"
gzip -dc "$dump_file" | docker compose --env-file .env -f compose.yaml exec -T mariadb sh -c 'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'

echo "Imported sanitized demo database from $dump_file."
