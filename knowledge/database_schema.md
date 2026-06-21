# Ometrics Operational Database Schema

This document is the curated schema context for a future read-only SQL tool.
It is intentionally limited to operational tables that may be queried by Omai.
It is not a full database dump, and it must not be treated as permission to
query tables outside the allowlist below.

## SQL Safety Rules

- Only read-only `SELECT` queries may use this schema.
- Every query must be scoped to the selected site.
- Use the selected site through `site_id` filters or joins to a site-scoped parent table.
- Prefer display names over database IDs in final answers.
- Use ISO `YYYY-MM-DD` dates inside SQL/tool arguments.
- Format dates as `MM/DD/YYYY` in final user-facing answers.
- Do not query authentication, user, session, token, password, API key, or billing tables.
- Do not expose raw internal IDs unless they are required to explain a join or debug a data issue.

## Allowed Tables

The future SQL tool may only use these tables:

- `alarm_events`
- `alarm_logs`
- `batteries`
- `chart_notes`
- `data_points`
- `data_points_data`
- `flares`
- `flare_readings`
- `flow_meters`
- `flow_meter_readings`
- `general_notes`
- `knock_outs`
- `knock_out_readings`
- `lacts`
- `lact_readings`
- `linear_tank_readings`
- `mixed_tank_readings`
- `non_linear_tank_readings`
- `non_linear_tank_volume_mappings`
- `non_linear_tank_volume_mapping_details`
- `onrr_codes`
- `pumps`
- `pump_readings`
- `rod_pump_cards`
- `rod_pump_monitoring_data`
- `rod_pump_monitoring_data_cards`
- `rod_pump_notes`
- `run_tickets`
- `silent_profiles`
- `tanks`
- `treaters`
- `treater_readings`
- `water_draws`
- `water_plants`
- `water_plant_readings`
- `wells`
- `well_documents`
- `well_fluids`
- `well_histories`
- `well_injections`
- `well_shutdowns`
- `well_tests`
- `work_orders`
- `work_order_notes`

## Site-Scoped Parent Tables

These tables normally carry `site_id` directly and can be filtered with:

```sql
WHERE <table>.site_id = :site_id
```

- `batteries`
- `chart_notes`
- `data_points`
- `flares`
- `flow_meters`
- `general_notes`
- `knock_outs`
- `lacts`
- `pumps`
- `silent_profiles`
- `tanks`
- `treaters`
- `water_plants`
- `wells`
- `work_orders`

## Child Tables And Site Joins

Most reading/event tables do not need to expose `site_id` directly if they join
through their parent equipment table. Use these joins to enforce site scope.

### Alarms And Data Points

- `alarm_events.data_point_id -> data_points.id`
- `alarm_logs.data_point_id -> data_points.id`
- `data_points_data.data_point_id -> data_points.id`

Site filter:

```sql
JOIN data_points ON <child>.data_point_id = data_points.id
WHERE data_points.site_id = :site_id
```

Useful display fields are typically on `data_points`, such as facility, device,
and data point names.

Common alarm columns:

- `alarm_logs.data_point_id`
- `alarm_logs.event`
- `alarm_logs.comments`
- `alarm_logs.unix_timestamp`
- `alarm_logs.created_at`
- `data_points.facility_name`
- `data_points.device_name`
- `data_points.data_point_name`
- `data_points.tag_name`

For alarm-log date filtering, prefer `alarm_logs.created_at` unless the user
specifically needs the event timestamp embedded in comments. For "which data
points generated the most alarm logs", group by `data_points.data_point_name`
or by a combined display label using `facility_name`, `device_name`, and
`data_point_name`. For "which batteries had the most alarm logs", group by
`data_points.facility_name` when it contains battery names.

### Flares

- `flare_readings.flare_id -> flares.id`

Site filter:

```sql
JOIN flares ON flare_readings.flare_id = flares.id
WHERE flares.site_id = :site_id
```

### Flow Meters

- `flow_meter_readings.flow_meter_id -> flow_meters.id`

Site filter:

```sql
JOIN flow_meters ON flow_meter_readings.flow_meter_id = flow_meters.id
WHERE flow_meters.site_id = :site_id
```

### Knock Outs

- `knock_out_readings.knock_out_id -> knock_outs.id`

Site filter:

```sql
JOIN knock_outs ON knock_out_readings.knock_out_id = knock_outs.id
WHERE knock_outs.site_id = :site_id
```

### LACTs

- `lact_readings.lact_id -> lacts.id`

Site filter:

```sql
JOIN lacts ON lact_readings.lact_id = lacts.id
WHERE lacts.site_id = :site_id
```

### Pumps And Rod Pump Data

- `pump_readings.pump_id -> pumps.id`
- `rod_pump_cards.pump_id -> pumps.id`
- `rod_pump_monitoring_data.pump_id -> pumps.id`
- `rod_pump_notes.pump_id -> pumps.id`
- `rod_pump_monitoring_data_cards.rod_pump_monitoring_data_id -> rod_pump_monitoring_data.id`

Site filter for pump records:

```sql
JOIN pumps ON <child>.pump_id = pumps.id
WHERE pumps.site_id = :site_id
```

Site filter for monitoring-data cards:

```sql
JOIN rod_pump_monitoring_data
  ON rod_pump_monitoring_data_cards.rod_pump_monitoring_data_id = rod_pump_monitoring_data.id
JOIN pumps ON rod_pump_monitoring_data.pump_id = pumps.id
WHERE pumps.site_id = :site_id
```

### Tanks

- `linear_tank_readings.tank_id -> tanks.id`
- `mixed_tank_readings.tank_id -> tanks.id`
- `non_linear_tank_readings.tank_id -> tanks.id`
- `non_linear_tank_volume_mappings.tank_id -> tanks.id`
- `run_tickets.tank_id -> tanks.id`
- `water_draws.tank_id -> tanks.id`

Site filter:

```sql
JOIN tanks ON <child>.tank_id = tanks.id
WHERE tanks.site_id = :site_id
```

Non-linear tank volume mapping details are scoped through their mapping:

```sql
JOIN non_linear_tank_volume_mappings
  ON non_linear_tank_volume_mapping_details.non_linear_tank_volume_mapping_id =
     non_linear_tank_volume_mappings.id
JOIN tanks ON non_linear_tank_volume_mappings.tank_id = tanks.id
WHERE tanks.site_id = :site_id
```

### Treaters

- `treater_readings.treater_id -> treaters.id`

Site filter:

```sql
JOIN treaters ON treater_readings.treater_id = treaters.id
WHERE treaters.site_id = :site_id
```

### Water Plants

- `water_plant_readings.water_plant_id -> water_plants.id`

Site filter:

```sql
JOIN water_plants ON water_plant_readings.water_plant_id = water_plants.id
WHERE water_plants.site_id = :site_id
```

### Wells

- `well_documents.well_id -> wells.id`
- `well_fluids.well_id -> wells.id`
- `well_histories.well_id -> wells.id`
- `well_injections.well_id -> wells.id`
- `well_shutdowns.well_id -> wells.id`
- `well_tests.well_id -> wells.id`

Site filter:

```sql
JOIN wells ON <child>.well_id = wells.id
WHERE wells.site_id = :site_id
```

Use `wells.name` as the display name. For Hartzog numeric references such as
`4048`, match by suffix or by `HARTZOG DRAW UNIT 4048` where appropriate.

Common shutdown columns:

- `well_shutdowns.date`
- `well_shutdowns.hours`
- `well_shutdowns.long_shutdown`
- `well_shutdowns.long_shutdown_start`
- `well_shutdowns.long_shutdown_end`
- `well_shutdowns.downtime_code`
- `well_shutdowns.comments`

For "total shutdown hours", sum `well_shutdowns.hours` and group by
`wells.name`.

### Work Orders

- `work_order_notes.work_order_id -> work_orders.id`

Site filter:

```sql
JOIN work_orders ON work_order_notes.work_order_id = work_orders.id
WHERE work_orders.site_id = :site_id
```

## Common Time Columns

The exact available columns should still be validated by the SQL tool before
execution, but the current operational conventions are:

- Daily notes and shutdowns often use `date`.
- Reading and work-order records often use `time`.
- Alarm events often use `happened_on`.
- Alarm logs commonly use `created_at`; they do not normally have
  `alarm_date`, `alarm_time`, or `happened_on` columns.
- Data point time-series rows often use timestamp/time columns on `data_points_data`.
- Long shutdowns use `long_shutdown_start` and `long_shutdown_end`.
- Well history uses `changed_at`.
- Standard Laravel tables may have `created_at` and `updated_at`.

## Query Preference

Use existing structured Omai tools before SQL for standard report, reading,
shutdown, timeline, capability, and RAG questions. The future SQL tool should be
reserved for ad hoc analysis, cross-table checks, unusual aggregations, and
debugging questions that cannot be answered well by existing tools.
