#!/usr/bin/env sh
set -eu

# Omai validates SQL before execution, but this separate database identity is
# the final read-only boundary for the public demo's SQL fallback tool.
deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$deployment_dir"

docker compose --env-file .env -f compose.yaml exec -T mariadb sh <<'CONTAINER_SCRIPT'
set -eu

# Deployment secrets are administrator-managed. Restrict them to characters
# valid in SQL string literals when creating the database principal.
case "$OPERATIONAL_SQL_DB_USER$OPERATIONAL_SQL_DB_PASSWORD" in
  *"'"*|*"\\"*)
    echo "The read-only SQL credentials cannot contain quotes or backslashes." >&2
    exit 1
    ;;
esac

root_args="-uroot -p$MYSQL_ROOT_PASSWORD"
mariadb $root_args -e "CREATE USER IF NOT EXISTS '$OPERATIONAL_SQL_DB_USER'@'%' IDENTIFIED BY '$OPERATIONAL_SQL_DB_PASSWORD';"
mariadb $root_args -e "REVOKE ALL PRIVILEGES, GRANT OPTION FROM '$OPERATIONAL_SQL_DB_USER'@'%';"

tables="alarm_events alarm_logs batteries chart_notes data_points data_points_data flares flare_readings flow_meters flow_meter_readings general_notes knock_outs knock_out_readings lacts lact_readings linear_tank_readings mixed_tank_readings non_linear_tank_readings non_linear_tank_volume_mappings non_linear_tank_volume_mapping_details onrr_codes pumps pump_readings rod_pump_cards rod_pump_monitoring_data rod_pump_monitoring_data_cards rod_pump_notes run_tickets silent_profiles tanks treaters treater_readings water_draws water_plants water_plant_readings wells well_documents well_fluids well_histories well_injections well_shutdowns well_tests work_orders work_order_notes"
for table in $tables; do
  mariadb $root_args -e "GRANT SELECT ON \`$MYSQL_DATABASE\`.\`$table\` TO '$OPERATIONAL_SQL_DB_USER'@'%';"
done

mariadb $root_args -e "FLUSH PRIVILEGES;"
CONTAINER_SCRIPT

echo "Provisioned the Omai read-only SQL user."
