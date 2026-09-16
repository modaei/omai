# Phase 1 Implementation Plan: SAM1 Scalar Health Detection

## Objective

Implement the first production phase of SAM1 rod-pump health detection using 15-minute Graphite telemetry and existing Ometrics context. Phase 1 does not interpret raw dynamometer-card shapes. It replaces OMAI's current composite health score and one-day heuristic diagnoses with persisted, explainable health episodes.

The implemented Phase 1 diagnoses are:

1. `diagnostic_data_quality`
2. `increasing_cycling_declining_runtime`
3. `recurring_malfunction_pattern`
4. `structural_load_creep`
5. `configuration_or_override_change`

Every newly detected or materially changed episode receives an LLM explanation and differential-diagnosis ranking before the evaluator persists the updated result. Deterministic logic remains authoritative for all episode state and severity decisions. The evaluator does not send notifications.

## Scope boundaries

Included:

- Historical SAM1 scalar retrieval from Graphite.
- Automatic, per-well historical baseline derivation.
- Daily aggregates, state-transition events, deterministic episode lifecycle, and persisted results for downstream consumers.
- LLM-generated explanation and ranked differentials using a constrained evidence packet.
- OMAI read-only retrieval/reporting of persisted Phase 1 episodes.

Excluded:

- Fluid-pound, fillage-setpoint, valve, gas-interference, friction, counterbalance, or any raw-card diagnosis.
- Engineering-provided well profiles, rod-string fatigue, unit geometry, or setpoint-safety judgment.
- Writing health records from OMAI.
- Using an LLM to detect, clear, escalate, suppress, or validate an alert.

## Ownership and runtime design

```text
Graphite + Ometrics source tables
          │
          ▼
OMAI health evaluator (writer)
  ├─ derive baseline and daily metrics
  ├─ detect/update health episodes
  ├─ request LLM review for changed episodes
  └─ persist episode state and evidence
          │
          ▼
Ometrics health tables
          │
          ▼
OMAI read-only repository, report endpoint, and chat tool
```

The evaluator belongs in OMAI so scheduled processing and an on-demand “health status of well …” request use the same logic. OMAI writes only the derived health tables through its existing `DB_USER` connection; `OPERATIONAL_SQL_DB_USER` remains limited to ad hoc read-only SQL. A separate downstream consumer may read episodes and send notifications.

## Step 1 — Confirm and catalog live scalar sources

### 1.1 Build a metric resolver

Create an Ometrics service that reads active rod-pump points from `data_points`, joins their parent facility where necessary, and resolves a logical metric to its configured Graphite tag.

Graphite target format:

```text
MI3.{site_key}.{well_key}.{tag}
```

Retain existing aliases during resolution:

```text
peak_load_sp -> max_load
min_load_sp  -> min_load
peak_load_ls -> peak_load_last_stroke
min_load_ls  -> min_load_last_stroke
```

The resolver returns, for each required logical metric:

```text
well_id, logical_metric, data_point_id, data_point_name,
resolved_tag, graphite_target, unit, expected_cadence_minutes,
required, active, resolution_status, resolution_reason
```

### 1.2 Required Phase 1 metric catalog

| Diagnosis | Required logical metrics |
|---|---|
| Data quality | Timestamps for all required points; `Well State`; `Pump Status` where available |
| Cycling/runtime | `Yesterday Percent`, `30 Days Percent`, `Yesterday Cycles`, `Percent Run`, `Well State`, `Time In State`, control/operation modes |
| Malfunction recurrence | `Well State`, `Well State Code`, `State Code`, `Time In State`, `Pump Status` |
| Load creep | `Yesterday Peak Load`, `Yesterday Min Load`, `Peak Load Setpoint`, `Min Load Setpoint`, `Stroke Min`, `Stroke Length`, operation mode |
| Configuration/override change | All available SAM1 setpoint fields, control/operation modes, `Host Switch Setpoint`, `Pump Status` |

Do not infer a missing point from similarly named points. A point is `unavailable` when it cannot be resolved to one active `data_points` record and a valid Graphite target.

### 1.3 Validate actual coverage before enabling any well

For every rod well, query a 30-day sample of each target and record:

- sample count and first/latest timestamps;
- observed median sampling interval;
- numeric versus string/state value type;
- units where configured;
- null/frozen-data intervals;
- whether the requested logical metric can support its rule.

Create an enablement report. A well begins in `coverage_pending` and can move to `phase_1_enabled` only when the required metrics for at least one diagnosis have passed coverage checks.

## Step 2 — Add Ometrics persistence

Add three Ometrics migrations. OMAI is the only writer through `DB_USER`; `OPERATIONAL_SQL_DB_USER` must not be used for evaluator writes.

### 2.1 `rod_pump_health_historical_baselines`

One row per well, containing only the latest system-derived stable baseline.

```text
id bigint primary key
well_id bigint not null unique foreign key -> wells.id
last_refreshed_at datetime not null
observed_configuration_json json not null
expected_metric_catalog_json json not null
baseline_start_at datetime nullable
baseline_end_at datetime nullable
baseline_status varchar(32) not null
baseline_summary_json json not null
created_at / updated_at
```

`baseline_status` values:

```text
coverage_pending | candidate | stable | insufficient_data | invalidated
```

### 2.2 `rod_pump_health_daily_metrics`

One upserted row per well and operational calendar day.

```text
id bigint primary key
well_id bigint not null foreign key -> wells.id
metric_date date not null
runtime_percent decimal nullable
cycle_count decimal nullable
peak_load decimal nullable
min_load decimal nullable
load_range decimal nullable
median_spm decimal nullable
median_stroke_seconds decimal nullable
stroke_length decimal nullable
median_fillage decimal nullable
fillage_setpoint decimal nullable
peak_load_setpoint decimal nullable
min_load_setpoint decimal nullable
control_mode varchar nullable
operation_mode varchar nullable
host_switch varchar nullable
normal_run_minutes integer nullable
pump_off_event_count integer not null default 0
malfunction_event_count integer not null default 0
metric_coverage_json json not null
source_window_start datetime not null
source_window_end datetime not null
created_at / updated_at
```

Constraints and indexes:

```text
unique(well_id, metric_date)
index(well_id, metric_date desc)
```

### 2.3 `rod_pump_health_episodes`

One row for the lifetime of a diagnosis episode.

```text
id bigint primary key
well_id bigint not null foreign key -> wells.id
diagnosis_code varchar(96) not null
severity varchar(16) not null
state varchar(24) not null
opened_at datetime not null
last_seen_at datetime not null
cleared_at datetime nullable
confidence_band varchar(16) nullable
summary text nullable
recommended_action text nullable
suppression_reason text nullable
first_evidence_at datetime nullable
last_evidence_at datetime nullable
evidence_json json not null
created_at / updated_at
```

Indexes:

```text
index(well_id, state, diagnosis_code)
index(state, severity, last_seen_at)
```

`evidence_json` stores bounded deterministic evidence plus the LLM review. Keep the first trigger, latest observations, every severity escalation, and clearance evidence. Do not append an entry for every scheduler run.

## Step 3 — Build daily telemetry aggregation

### 3.1 Scheduler and windowing

Run the evaluator every two hours. On each run:

1. Process the current operational day for each enabled rod well.
2. Reprocess the preceding two days to capture late Graphite points and state transitions spanning midnight.
3. Run a nightly 45-day refresh to update baseline candidates and repair late history.

Use the site timezone to define `metric_date` and day boundaries.

### 3.2 Daily aggregation rules

| Field | Aggregation rule |
|---|---|
| `runtime_percent` | Use one end-of-day/most recent valid `Yesterday Percent` value. Do not average identical repeated daily reports. |
| `cycle_count` | Use one end-of-day/most recent valid `Yesterday Cycles` value. |
| `peak_load`, `min_load` | Use one end-of-day/most recent valid `Yesterday Peak Load` and `Yesterday Min Load`; `load_range = peak - min`. |
| `median_spm`, `median_stroke_seconds`, `stroke_length`, `median_fillage` | Median of valid 15-minute samples during stable normal pumping. |
| Setpoints/modes | Latest valid value for the day plus a flag when values changed during the day. |
| `normal_run_minutes` | Sum valid intervals in stable normal pumping. |
| Event counts | Count state transitions, not samples. |

For every aggregate, record completeness and source details in `metric_coverage_json`. A null is preserved as null; never coerce it to zero.

### 3.3 State transition normalizer

Use `Well State Code` as the canonical SAM1 state input. Keep `Well State` as a human-readable cross-check and retain `State Code` / `Pump Status` as supplementary context only after their meanings are verified for the deployed firmware.

Map the supplied `Well State Code` values into:

```text
normal_pumping
pump_off_or_downtime
malfunction_peak_load
malfunction_min_load
malfunction_setpoint
malfunction_low_rpm
malfunction_no_rpm
malfunction_no_crank
bad_load_signal
bad_position_signal
manual_or_host_override
other
unknown
```

Initial code mapping:

| Normalized category | `Well State Code` values |
|---|---|
| `normal_pumping` | 7 |
| `manual_or_host_override` | 8 Pumping Host, 10 Pumping HOA, 35 Downtime HOA Off, 36 Downtime Host Mode, 52 Downtime Operator Stop |
| `pump_off_or_downtime` | 9, 31, 37, 41, 42, 44, 46-51, 53-55, 58-65 |
| `malfunction_peak_load` | 33, 70 |
| `malfunction_min_load` | 34, 71 |
| `malfunction_setpoint` | 32, 72 |
| `malfunction_low_rpm` | 40, 73 |
| `malfunction_no_rpm` | 49, 75 |
| `malfunction_no_crank` | 50, 76 |
| `bad_position_signal` | 13, 38 |
| `bad_load_signal` | 12, 39 |
| `fault_or_lockout` | 1, 11, 16, 18-19, 28, 74, 77-80, 83-90 |
| `transition_or_service` | 2-6, 14-15, 17, 27, 29 |
| `unknown` | 0, 20-26, 30, 43, 45, 56-57, 66-69, 81-82, and any unmapped future code |

Codes 31-69 represent controller-managed downtime/protective behavior; codes 70-80 represent persistent malfunction/lockout states. The recurrence rule records both, but stores the raw code and whether the event was controlled downtime or a malfunction lockout so they can be reported and thresholded separately.

The normalizer must:

- detect a transition only when the mapped category changes;
- tolerate missing samples without inventing an event;
- retain raw values in diagnostic evidence;
- mark an unknown state as coverage degradation until the dictionary is updated.

Because Graphite stores 15-minute samples, count only state transitions visible in consecutive samples. Ignore a state that starts and clears entirely between samples. `Time In State` may corroborate an observed transition, but must not create an unseen event.

## Step 4 — Derive the historical baseline

### 4.1 Candidate-window selection

For each well, evaluate trailing 45 days and choose the newest 30-day candidate window that has:

- required metric coverage at or above the configured threshold;
- no active Phase 1 high episode;
- no known controller-service, treatment, or shutdown interval from `chart_notes`, `rod_pump_notes`, `well_shutdowns`, or `well_histories`;
- stable control/operation mode and stable stroke length;
- enough normal-run time for relevant daily values.

The system-derived baseline is a comparison reference, not a declaration that the well was physically healthy.

### 4.2 Baseline summary

Calculate and store, per included metric:

```text
count, median, p10, p90, MAD, Theil-Sen slope,
baseline window timestamps, coverage percentage
```

Store the observed setpoint/mode snapshot separately in `observed_configuration_json`.

### 4.3 Refresh/invalidation

Refresh the single baseline row nightly only when a newer stable candidate exists. Set `baseline_status = invalidated` immediately if a material configuration/stroke-mode change occurs. During invalidation, continue data-quality/configuration episodes but suppress trend diagnoses requiring the old comparison baseline.

## Step 5 — Implement deterministic Phase 1 rules

All rules operate on persisted daily metrics and current Graphite coverage. They must include the exact evidence values and deterministic recovery/clear condition.

### 5.1 `diagnostic_data_quality`

```text
Open advisory when a required active source is missing, stale, frozen,
non-numeric when numeric is required, or outside expected cadence:

- scalar gap >45 minutes while the well should communicate; or
- required logical metric cannot resolve to an active Graphite target; or
- an active state changes while a dependent metric remains frozen beyond policy.

Clear after 24 consecutive hours of required source coverage.
```

### 5.2 `increasing_cycling_declining_runtime`

```text
baseline_runtime = baseline median runtime
baseline_cycles  = baseline median cycle count

Open advisory when:
  7-day average runtime < baseline_runtime - max(10 percentage points, 15% relative)
  AND 7-day average cycles > 1.25 * baseline_cycles
  AND both conditions occur on >=5 of the last 7 eligible days.

Eligible day:
  stable operation/control mode, comparable stroke length,
  adequate normal-run minutes, and valid runtime/cycle metrics.

Clear after 7 eligible days where runtime and cycles return within baseline tolerance.
```

### 5.3 `recurring_malfunction_pattern`

```text
Open high when:
  >=3 distinct malfunction-state transitions occur in trailing 7 days;
  OR >=2 transitions of the same malfunction category occur in trailing 48 hours.

Clear after 14 days with no malfunction-state transition.
```

The episode evidence includes raw/mapped states, event timestamps, category counts, and restart-to-next-event intervals where observable.

### 5.4 `structural_load_creep`

```text
Eligible day:
  stable operation mode and stroke length,
  daily median SPM within +/-5% of baseline median,
  adequate normal-run minutes, and valid load/setpoint data.

peak_slope = Theil-Sen slope across trailing 30 eligible days
peak_margin = (current peak-load setpoint - trailing 7-day median peak load)
              / current peak-load setpoint

Open advisory when:
  peak_slope > 0
  AND 30-day peak-load increase >8%
  AND peak_margin <20%
  AND the rising condition persists >=7 eligible days.

Escalate to high when robust projection reaches the current peak-load setpoint
within 30 days.

Clear after 14 eligible days with stable/non-rising peak load and margin >=20%.
```

Never label this as a rod-fatigue calculation. It is a loading trend toward the controller’s existing limit.

### 5.5 `configuration_or_override_change`

```text
Open advisory when current controller setpoint/mode snapshot differs from
the baseline observed configuration, or manual/host override exceeds
the site-wide configured duration policy.

Clear after the configuration remains stable for 48 hours and a new
candidate baseline has been established, or after override is absent
for the configured recovery period.
```

This reports a detectable change only. It must not classify configuration as unsafe, incorrect, or unauthorized.

## Step 6 — Add required LLM review

### 6.1 Invocation trigger

Invoke the LLM after deterministic logic produces one of these events:

- candidate first meets persistence;
- candidate becomes active;
- severity escalates;
- material evidence changes, such as a new malfunction category or a meaningful load-margin deterioration.

Do not invoke for unchanged episodes or routine healthy evaluations.

### 6.2 Structured LLM packet

Provide only scoped, normalized evidence:

```json
{
  "detected_result": {
    "diagnosis_code": "structural_load_creep",
    "severity": "advisory",
    "state": "active",
    "rule_clauses": ["30-day peak load increase = 11.2%", "peak margin = 17.4%"]
  },
  "baseline": {
    "window": {"start": "...", "end": "..."},
    "summary": {"peak_load": {"median": 21400, "p10": 20500, "p90": 22300}}
  },
  "observed_configuration": {"operation_mode": "Automatic", "peak_load_setpoint": 28500},
  "relevant_history": [],
  "state_events": [],
  "data_limitations": [],
  "deterministic_alternatives": ["configuration change", "load-cell calibration change"]
}
```

Require JSON output with:

```text
plain_language_explanation
alert_summary
ranked_differentials[]: diagnosis, rank, rationale,
                         supporting_evidence, contradictory_evidence
operator_checks[]
data_limitations[]
```

### 6.3 Validation and failure policy

- Validate JSON shape, allowed fields, maximum lengths, and diagnosis naming before storage.
- Persist the compact request summary, model/prompt identifier, response, and validation status in the episode `evidence_json`.
- Do not use LLM output to alter diagnosis code, severity, episode state, suppression, or clearing.
- If the call fails or output is invalid, persist the deterministic episode with a template explanation and record the failure in `evidence_json`.

## Step 7 — Episode lifecycle and downstream-consumer contract

### 7.1 Episode matching

Use one active/candidate episode per `(well_id, diagnosis_code)`. On each evaluator run:

- update the existing episode if the same diagnosis remains supported;
- append bounded new evidence only when meaningful evidence changes;
- create a new episode after a previous episode was cleared and a new trigger occurs;
- do not create a new episode or append evidence for unchanged observations.

### 7.2 Downstream-consumer contract

| Episode change | Persisted result for downstream consumers |
|---|---|
| Candidate created | Persist candidate plus deterministic evidence and LLM review. |
| Becomes active | Persist deterministic diagnosis/severity, LLM alert summary, and operator checks. |
| Severity escalates | Persist escalation, latest evidence, and updated LLM differential ranking. |
| New material evidence | Update episode and LLM review. |
| Clears | Persist clearance evidence. |
| LLM failure | Persist template explanation; never drop an otherwise valid high episode. |

## Step 8 — Replace OMAI’s current health path

1. Add an OMAI health repository/evaluator for the three new Ometrics health tables, using its dedicated health-writer connection only for derived records.
2. Update `RodPumpAnalysisClient` to invoke the Phase 1 evaluator on demand for a well, then return persisted episodes, evidence, coverage, baseline status, and recent scalar summaries instead of calculating health locally.
3. Remove the composite `health_scores`, anomaly fusion, raw-card rules, paraffin prediction gate, and legacy Phase 1-incompatible diagnoses from `rod_pump_analysis_engine.py`.
4. Keep `POST /rod-pump-health-report`, but make it rank persisted active episodes by severity, state, last evidence time, and confidence band.
5. Update `analyze_rod_pump` tool wording and tests so chat exposes the persisted deterministic result plus the saved LLM explanation/differentials.
6. Do not include Phase 2 card-feature diagnoses until their feature table and validation work are implemented.

## Step 9 — Tests

### Unit tests

- Graphite target resolution uses `MI3.{site}.{well}.{tag}` and configured aliases.
- Missing/ambiguous data point is coverage failure, not a default value.
- Daily aggregation deduplicates `Yesterday …` values.
- State normalizer counts transitions, including midnight boundaries, only once.
- Baseline candidate excludes unstable modes, missing coverage, intervention windows, and active high episodes.
- Each diagnosis meets its persistence, escalation, suppression, and clearing conditions at boundary values.
- Configuration change generates a change alert, never an unsafe-config classification.
- LLM packet contains only scoped evidence; malformed LLM output falls back to template explanation.
- `evidence_json` retains trigger/escalation/clearance evidence and remains bounded.

### Integration tests

- Ometrics evaluator writes and upserts baseline, daily metric, and episode rows.
- Graphite late data updates the prior two operational days without duplicate events.
- One active episode is updated across runs without duplicate rows or unbounded evidence growth.
- OMAI read-only client returns the persisted episode and does not execute legacy rule logic.
- A simulated LLM timeout does not prevent persistence of a high deterministic episode.

## Step 10 — Rollout

1. Deploy migrations and metric resolver with evaluation disabled.
2. Run 90-day backfill in shadow mode for all rod wells.
3. Produce the coverage report; enable only wells with passing metric coverage and stable baseline candidates.
4. Run Phase 1 episodes and LLM reviews for two weeks; inspect persisted results with downstream delivery disabled.
5. Compare episodes with chart notes, rod-pump notes, shutdowns, and operator review.
6. Tune site-wide thresholds only after reviewing false positives, missed known events, lead time, and alert volume.
7. Make persisted active episodes available to the downstream notification consumer after acceptance review.
8. Remove the legacy OMAI health-score output after all enabled wells use persisted Phase 1 episodes.

## Phase 1 acceptance criteria

- Every enabled well has resolved Graphite targets, coverage evidence, and a stable historical baseline.
- No Phase 1 diagnosis is emitted from missing, stale, or ambiguous data.
- Each episode is traceable to Graphite targets, observed values, baseline values, state transitions, and clear conditions.
- Every activated or materially changed episode has a valid saved LLM explanation or documented template fallback.
- OMAI and every downstream consumer read the same persisted episode; no legacy score or conflicting diagnosis remains.
