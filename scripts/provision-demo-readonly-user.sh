#!/usr/bin/env sh
set -eu

# Create the least-privileged account used by Omai's operational SQL fallback.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
config_file=${OMAI_ENV_FILE:-"$project_dir/.env"}

env_value() {
    sed -n "s/^$1=//p" "$config_file" | tail -n 1
}

database_host=$(env_value DB_HOST)
database_port=$(env_value DB_PORT)
database_name=$(env_value DB_NAME)
admin_user=$(env_value DEMO_DB_ADMIN_USER)
admin_password=$(env_value DEMO_DB_ADMIN_PASSWORD)
readonly_user=$(env_value OPERATIONAL_SQL_DB_USER)
readonly_password=$(env_value OPERATIONAL_SQL_DB_PASSWORD)

if [ -z "$database_host" ] || [ -z "$database_port" ] || [ -z "$database_name" ] || [ -z "$admin_user" ] || [ -z "$admin_password" ] || [ -z "$readonly_user" ] || [ -z "$readonly_password" ]; then
    echo "Database admin and OPERATIONAL_SQL_DB credentials must be configured." >&2
    exit 1
fi

case "$readonly_user$readonly_password" in
  *"'"*|*"\\"*)
    echo "Read-only SQL credentials cannot contain quotes or backslashes." >&2
    exit 1
    ;;
esac

admin_mariadb() {
    MYSQL_PWD="$admin_password" mariadb --host "$database_host" --port "$database_port" --user "$admin_user" "$@"
}

admin_mariadb -e "CREATE USER IF NOT EXISTS '$readonly_user'@'%' IDENTIFIED BY '$readonly_password';"
admin_mariadb -e "REVOKE ALL PRIVILEGES, GRANT OPTION FROM '$readonly_user'@'%';"

tables="alarm_events alarm_logs batteries chart_notes data_points data_point_data flares flare_readings flow_meters flow_meter_readings general_notes knock_outs knock_out_readings lacts lact_readings linear_tank_readings mixed_tank_readings non_linear_tank_readings non_linear_tank_volume_mappings non_linear_tank_volume_mapping_details onrr_codes pumps pump_readings rod_pump_cards rod_pump_monitoring_data rod_pump_monitoring_data_cards rod_pump_notes run_tickets silent_profiles tanks treaters treater_readings water_draws water_plants water_plant_readings wells well_fluids well_histories well_injections well_shutdowns well_tests work_orders work_order_notes"
for table in $tables; do
    if admin_mariadb --skip-column-names -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '$database_name' AND table_name = '$table'" | grep -qx '1'; then
        admin_mariadb -e "GRANT SELECT ON \`$database_name\`.\`$table\` TO '$readonly_user'@'%';"
    fi
done

admin_mariadb -e "FLUSH PRIVILEGES;"
