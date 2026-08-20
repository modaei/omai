#!/usr/bin/env sh
set -eu

# Import a private dump into a temporary MariaDB staging database, sanitize it,
# and replace the directly configured demo database. It must run only on a
# trusted administrator host.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dump=${1:?"Usage: refresh-demo-database.sh /secure/ometrics.sql.gz /secure/sanitization-seed"}
seed_file=${2:?"Usage: refresh-demo-database.sh /secure/ometrics.sql.gz /secure/sanitization-seed"}
config_file=${OMAI_ENV_FILE:-"$project_dir/.env"}

if [ ! -f "$source_dump" ] || [ ! -f "$seed_file" ] || [ ! -f "$config_file" ]; then
    echo "The source dump, seed file, and OMAI_ENV_FILE must exist." >&2
    exit 1
fi

env_value() {
    sed -n "s/^$1=//p" "$config_file" | tail -n 1
}

site_id=$(env_value DEMO_SITE_ID)
demo_user_id=$(env_value DEMO_USER_ID)
database_host=$(env_value DB_HOST)
database_port=$(env_value DB_PORT)
database_name=$(env_value DB_NAME)
admin_user=$(env_value DEMO_DB_ADMIN_USER)
admin_password=$(env_value DEMO_DB_ADMIN_PASSWORD)

if [ -z "$site_id" ] || [ -z "$demo_user_id" ] || [ -z "$database_host" ] || [ -z "$database_port" ] || [ -z "$database_name" ] || [ -z "$admin_user" ] || [ -z "$admin_password" ]; then
    echo "DEMO_SITE_ID, DEMO_USER_ID, DB_HOST, DB_PORT, DB_NAME, DEMO_DB_ADMIN_USER, and DEMO_DB_ADMIN_PASSWORD are required." >&2
    exit 1
fi

export DEMO_MYSQL_ROOT_PASSWORD="$admin_password"
python_bin=${OMAI_PYTHON:-"$project_dir/venv/bin/python"}
PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" -m omai.demo.sanitize_database \
    --input "$source_dump" \
    --seed-file "$seed_file" \
    --site-id "$site_id" \
    --demo-user-id "$demo_user_id" \
    --database-host "$database_host" \
    --database-port "$database_port" \
    --database-user "$admin_user" \
    --target-database "$database_name" \
    --rollback-directory "$project_dir/data/demo-rollbacks"
