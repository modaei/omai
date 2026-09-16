# Replacement Plan: SAM1 Rod-Pump Health Detection

## Decision

Replace the current OMAI rod-pump health implementation entirely. The replacement will be an evidence-based, per-well episode detector with three delivery phases. It will not use a composite `health` score, hard-coded card-area/load thresholds, or a single card/one-day endpoint change to name a mechanical fault.

The controller already protects against individual pump-off/fillage, peak/minimum load, low/no RPM, and malfunction-limit events. The replacement therefore detects their **trend, recurrence, inadequate recovery, or operating margin**, rather than generating a duplicate alert for every controller violation. Fluid-pound diagnosis is out of scope.

The health evaluator runs in OMAI so the identical logic is callable on schedule and on demand for a well-health request. It writes only derived health tables in Ometrics through a dedicated restricted writer credential. A structured LLM review is a required post-detection step for every new or materially changed episode. It provides the stored explanation and ranks differential diagnoses, while deterministic logic remains the source of truth for opening, severity, suppression, and clearing. The evaluator does not send notifications. A separate downstream consumer may read persisted episodes and decide whether to notify.

## Current-state findings and explicit retirement scope

Retire the current behavior in `src/omai/services/rod_pump_analysis_engine.py` and its corresponding report presentation:

- the 0–100 composite `health` score and categories;
- absolute `card_area`, `load_spread`, asymmetry, and impact-score triggers;
- a diagnosis from the latest 24-hour endpoint change;
- automatic `Pump-off / low inflow` and `Fluid pound` diagnoses;
- averaging every card of one side at a pull before diagnosis;
- a generic anomaly score that strengthens a diagnosis without diagnosis-specific persistence;
- a `Normal pumping` diagnosis when coverage is incomplete.

The replacement returns `unknown` or `insufficient data` where required inputs, card quality, a known-good baseline, or the SAM1 state-code mapping are unavailable. It does not convert missing evidence into a healthy score.

## Sources of record and access design

### Existing Ometrics tables

| Source | Role in replacement |
|---|---|
| `wells` | Well identity, site scope, `key`, and rod-pump population. |
| `data_points` | Live point catalog, parent facility/well association, device, configured `tag`, type, and active state. Resolve the metric catalog from this table; do not hard-code a display name as a Graphite target. |
| `data_point_data` | Latest scalar value only. Use for current-status display and freshness checks, not historical trends. |
| `rod_pump_monitoring_data` | Pull time and well association for live hourly card pulls. |
| `rod_pump_monitoring_data_cards` | Live pull-to-card association. |
| `rod_pump_cards` | Raw surface/downhole position-load arrays, `card_type`, card timestamp, and source `info`. |
| `rod_pump_card_archive` | Long-term selected raw-card evidence after live-card pruning. Prefer it for 30-day baselines and historical episode evidence. |
| `chart_notes`, `rod_pump_notes`, `well_shutdowns`, `well_histories` | Intervention and field-outcome context. Use them to exclude baseline windows and to label/validate detections; free text never creates a mechanical diagnosis by itself. |

`rod_pump_statuses` is not a current source: it was dropped by the Ometrics migration that removed `rod_pump_monitoring_data.rod_pump_status_id`. Do not design the replacement around it.

### Graphite history

Graphite is the source of historical SAM1 scalar data. Resolve every target from `data_points.tag` and the well/facility mapping, then fetch:

```text
MI3.{site_key}.{well_key}.{graphite_tag}
```

Use the existing Ometrics tag aliases when resolving a Graphite tag:

```text
peak_load_sp -> max_load
min_load_sp  -> min_load
peak_load_ls -> peak_load_last_stroke
min_load_ls  -> min_load_last_stroke
```

The current OMAI client already uses the correct hierarchy shape. The replacement retains that shape but resolves every tag from the active `data_points` catalog instead of relying on a fixed in-code metric list.

Required SAM1 metrics, resolved per well rather than assumed globally:

| Detection purpose | Logical metrics |
|---|---|
| Operating state/events | `Well State`, `Well State Code`, `State Code`, `Pump Status`, `Time In State` |
| Runtime/cycling | `Percent Run`, `Yesterday Percent`, `30 Days Percent`, `Yesterday Cycles` |
| Load margin/trend | `Yesterday Peak Load`, `Yesterday Min Load`, `Peak Load Setpoint`, `Min Load Setpoint`, `Current Load` |
| Operating comparability | `Stroke Seconds`, `Stroke Min`, `Stroke Length`, control and operation mode setpoints |
| Pump-off context only | `Pump Fillage`, `Fillage Setpoint`, downtime setpoints |
| Protection/configuration | all configured SAM1 setpoints, host-switch state, control/operation modes |

The evaluator must record a per-well coverage result for every logical metric: resolved tag, source target, units, latest timestamp, expected cadence, and missing-data reason. A missing tag is a coverage failure, not a zero-valued input.

## New Ometrics persistence tables

Add these through Ometrics migrations. The OMAI evaluator is the sole writer through its existing `DB_USER` connection. `OPERATIONAL_SQL_DB_USER` remains restricted to ad hoc read-only SQL.

### `rod_pump_health_historical_baselines`

One current system-derived historical baseline per well. It requires no engineering-maintained inputs.

```text
id, well_id, last_refreshed_at,
observed_configuration_json, expected_metric_catalog_json,
baseline_start_at, baseline_end_at, baseline_status,
baseline_summary_json, timestamps
```

The evaluator creates the baseline from a stable, data-complete historical window. `observed_configuration_json` is a snapshot of controller modes and setpoints seen in that window, not an approved configuration. `baseline_summary_json` stores derived normal ranges for stroke length, SPM, load, runtime, cycling, fillage, and card features. The evaluator updates this single row when it establishes a newer stable baseline; no historical baseline versions are retained.

### `rod_pump_health_daily_metrics`

One derived row per well and operational day. It retains reproducible aggregates from Graphite without copying every 15-minute raw point.

```text
id, well_id, metric_date,
runtime_percent, cycle_count, peak_load, min_load, load_range,
median_spm, median_stroke_seconds, stroke_length,
median_fillage, fillage_setpoint, peak_load_setpoint, min_load_setpoint,
control_mode, operation_mode, host_switch,
normal_run_minutes, pump_off_event_count, malfunction_event_count,
metric_coverage_json, source_window_start, source_window_end, timestamps
```

Unique key: `(well_id, metric_date)`. Index `(well_id, metric_date DESC)`.

### `rod_pump_health_card_features`

One immutable feature row for each selected side of each pull. Store the source-card identity so every conclusion remains traceable to raw evidence.

```text
id, well_id, pull_timestamp, side, source_kind, source_card_id,
card_timestamp, card_type, extractor_version,
quality_status, quality_reasons_json,
stroke_length, max_load, min_load, load_range, card_area,
pickup_position, release_position,
top_transition_width, bottom_transition_width,
top_corner_sharpness, bottom_corner_sharpness,
loop_width_avg, skew, discontinuities_json, shape_distance,
baseline_reference_json, timestamps
```

Unique key: `(well_id, pull_timestamp, side, extractor_version)`. Index `(well_id, side, pull_timestamp DESC)`.

Do not average unlike `card_type` values. For a pull/side, select the configured current card type; retain source-card IDs and mark any ambiguity as a quality failure.

### `rod_pump_health_episodes`

One row per active or closed diagnosis episode, rather than a new result row for every evaluation.

```text
id, well_id, diagnosis_code, severity, state,
opened_at, last_seen_at, cleared_at nullable,
confidence_band,
summary, recommended_action, suppression_reason nullable,
first_evidence_at, last_evidence_at,
evidence_json, timestamps
```

`state` is `candidate`, `active`, `acknowledged`, `cleared`, `rejected`, or `insufficient_data`. Index `(well_id, state, diagnosis_code)` and `(state, severity, last_seen_at)`.

`evidence_json` is an append-only-in-application array of evidence objects. Each object stores the observation time, source table/record, Graphite target when applicable, metric and baseline values, and the satisfied rule clause. Cap the retained evidence to the first trigger, most recent observations, severity escalations, and material changes so a long-running episode remains bounded.

## Common evaluation pipeline

Every phase uses this sequence:

```text
1. Resolve the well's current system-derived historical baseline.
2. Resolve logical metrics to active data_points and Graphite targets.
3. Fetch bounded Graphite history; validate cadence, units, and freshness.
4. Fetch live cards plus archive cards and choose one configured card per pull/side.
5. Exclude starts, planned downtime, manual tests, service/treatment windows,
   and 48 hours after a mode, setpoint, stroke-length, or speed change.
6. Build/update daily metrics and/or hourly card features.
7. Compare only with the same well's stable, data-complete baseline.
8. Evaluate the phase's deterministic rule with persistence and recovery criteria.
9. Open, update, deduplicate, escalate, or clear an episode.
10. For a new candidate, activation, severity escalation, or material evidence change, build the LLM review packet and request structured explanation/differential output.
11. Persist deterministic evidence, the LLM input summary, and the LLM output in the episode.
```

Defaults until field calibration: healthy baseline requires 30 clean operating days; trend rules use robust median and Theil-Sen slope; card rules require three of five valid cards unless their individual rule says otherwise. Baseline data after known treatments, controller service, workovers, shutdowns, or an active health episode are excluded.

### Core LLM review

The evaluator invokes the LLM only after deterministic logic has created or materially updated an episode. It does not send a request for routine healthy evaluations or unchanged active episodes.

The review packet contains:

```text
- deterministic diagnosis code, severity, persistence result, and recommended action;
- exact rule clauses that were satisfied and the observed/baseline values;
- current historical baseline snapshot and observed controller configuration snapshot;
- compact, time-ordered scalar history for the relevant rule window;
- current and baseline card-feature summaries, quality results, and source-card references;
- recent controller state transitions, configuration changes, chart notes, rod-pump notes,
  shutdowns, and already-active health episodes;
- explicit data limitations and competing deterministic evidence.
```

The LLM must return validated structured JSON:

```text
plain_language_explanation
ranked_differentials[]: { diagnosis, rank, rationale, supporting_evidence, contradictory_evidence }
operator_checks[]
data_limitations[]
alert_summary
```

The evaluator stores a compact copy of the packet, model/prompt identifier, and structured response in the episode's `evidence_json`. A downstream consumer may use the deterministic diagnosis/severity plus the LLM explanation and differential ranking when it creates a notification.

The LLM cannot create, rename, clear, suppress, or escalate an episode; alter a threshold; classify a configuration as unsafe; approve an action; or override SAM1 protection. If the LLM request, response validation, or safety check fails, the deterministic episode proceeds with a templated explanation and the failure is recorded in `evidence_json`.

## Phase 1 — Easy: scalar and state surveillance

**Goal:** Replace the current health score and unvalidated heuristics with robust daily/state episodes using existing Graphite data. No raw-card diagnosis is required.

### P1-1. Data coverage and freshness

```text
IF required 15-minute scalar data are absent for >45 minutes while the well should communicate,
   OR a required hourly card is absent for >2 expected pull intervals while the well is pumping,
   OR data are frozen/invalid while state changes,
THEN open `diagnostic_data_quality` advisory episode.
```

This episode suppresses diagnoses needing the missing source. It does not duplicate SAM1 `Bad Load` or `Bad Position` controller states.

### P1-2. Increasing cycling / declining runtime

```text
baseline_runtime = median(daily runtime over previous 30 clean days)
baseline_cycles  = median(daily cycles over previous 30 clean days)

IF mode/profile/stroke comparability is valid
   AND seven-day average runtime < baseline_runtime - max(10 percentage points, 15% relative)
   AND seven-day average cycles > 1.25 * baseline_cycles
   AND both are true on >=5 of the last 7 days
THEN open `increasing_cycling_declining_runtime` advisory episode.
```

Include fillage and downtime state as context only. This is not an alert titled “fluid pound.”

### P1-3. Recurrent malfunction pattern

Maintain a site/firmware-specific SAM1 state-code dictionary in the evaluator configuration. Count state **transitions** into a malfunction category, not repeated 15-minute samples.

```text
IF >=3 distinct malfunction transitions occur in 7 days
   OR >=2 transitions of the same category occur within 48 hours
THEN open `recurring_malfunction_pattern` high episode.
```

Evidence includes category, event times, auto-restart outcome, and time from restart to next event. Individual controller malfunctions remain controller-owned events.

### P1-4. Structural-load creep

```text
eligible day = unchanged mode and stroke length
               AND median SPM within +/-5% of baseline
               AND adequate normal runtime

peak_slope = Theil-Sen slope for 30 eligible daily peak-load values
peak_margin = (current peak setpoint - 7-day median peak load) / current peak setpoint

IF peak slope > 0
   AND 30-day peak-load increase > 8%
   AND peak margin < 20%
   AND trend persists >=7 eligible days
THEN open `structural_load_creep` advisory episode.

IF robust projection reaches the configured limit within 30 days
THEN escalate to high.
```

Use a rising peak plus falling minimum load as corroborating load-range expansion. A step change produces a configuration/calibration review, not this diagnosis.

### P1-5. Configuration and override change

```text
IF current setpoint/mode differs from the stable profile snapshot,
   OR manual/host override exceeds the site policy duration,
THEN open `configuration_or_override_change` advisory episode.
```

This alert reports a detectable change; it does not call the setting unsafe or incorrect. If a P1/P2 episode first appears within 48 hours of a configuration change, attach the correlation. It is evidence for review, not proof of causation.

### Phase-1 acceptance criteria

- All Graphite targets resolve from actual `data_points` configuration and use the active site/well/tag hierarchy.
- Daily values are deduplicated; repeated “Yesterday” telemetry never inflates counts.
- SAM1 state-code dictionary is tested against real event samples.
- Every episode has a persistent JSON evidence trail and deterministic clear condition.
- The API/report returns episodes and coverage, never a synthetic health score.

## Phase 2 — Medium: card-feature diagnostics

**Goal:** Add explainable hourly card diagnostics after raw card arrays and normal-card baselines are validated.

### Card preprocessing

1. Read live pull/card rows first and use `rod_pump_card_archive` to complete historical coverage.
2. Select a configured card type per side and pull; never blend different card types.
3. Validate array length, finite numeric points, traversal order, repeated-position branches, position span, load span, timestamp alignment, and expected side.
4. Normalize position to percent of observed stroke. Preserve raw units alongside normalized features.
5. Split upstroke/downstroke using verified traversal/orientation metadata. If orientation is unknown, mark the card unusable; do not assume the first half is the upstroke.
6. Store extracted features and compare them only with clean, comparable cards from the same well.

### P2-1. Gas interference despite acceptable fillage

```text
IF downhole pickup position > max(15% of stroke, clean-baseline pickup + calibrated tolerance)
   AND upstroke transition is rounded/delayed versus baseline
   AND fillage is above its controller setpoint
   AND >=4 consecutive valid hourly cards meet the condition
THEN open `gas_interference` advisory episode.
```

This diagnoses gas-compression behavior that can persist while fillage remains above the SAM1 shutdown target.

### P2-2. Traveling-valve leakage / delayed closure

```text
tv_evidence = late/incomplete upstroke load pickup
              + wider or less sharp top transition
              + deterioration in downhole normal-card similarity

IF calibrated tv_evidence exceeds threshold on >=3 of the last 5 valid cards
   AND deterioration persists/progresses for >=5 days
   AND pump-off/low-fillage cards are excluded
THEN open `traveling_valve_leak_or_delayed_closure` advisory episode.
```

Escalate only for persistence and increasing magnitude; the alert remains “pattern consistent with,” not confirmation.

### P2-3. Standing-valve leakage / delayed opening

```text
sv_evidence = delayed/incomplete downstroke load release
              + wider or less sharp bottom transition
              + residual load deviation at bottom of stroke

IF calibrated sv_evidence exceeds threshold on >=3 of the last 5 valid cards
   AND deterioration persists/progresses for >=5 days
   AND pump-off/low-fillage cards are excluded
THEN open `standing_valve_leak_or_delayed_opening` advisory episode.
```

### P2-4. Progressive mechanical friction

```text
IF surface loop-width and/or load range exceeds its clean baseline tolerance
   AND surface card area/load trend supports increased resistance
   AND daily load trend supports the same direction
   AND speed, stroke length, mode, and fillage state are comparable
   AND condition persists >=10 days
THEN open `progressive_mechanical_friction` advisory episode.
```

Use “inspect for paraffin, scale, rod-guide wear, or rod/tubing drag” as the action. Do not name paraffin solely from a card pattern.

### P2-5. Counterbalance or loading-balance change suspected

```text
IF surface-card loading asymmetry shifts from clean baseline for >=14 days
   AND peak/minimum daily loads move in opposite directions
   AND no speed, stroke length, configuration, or calibration change explains it
THEN open `loading_balance_change_suspected` advisory episode.
```

Without pumping-unit geometry and torque/balance calculations, this is a change detector—not a confirmed counterbalance diagnosis.

### P2-6. Declining pump performance / possible wear

```text
IF seven-day average fillage declines for >=10 days
   AND card area or a valve-leakage score deteriorates in the same period
   AND fillage setpoint, operation mode, stroke length, and speed are unchanged
THEN open `declining_pump_performance` advisory episode.
```

Do not diagnose worn plunger/barrel from fillage decline alone. The stored explanation presents inflow change, gas interference, and leakage as competing causes.

### Phase-2 acceptance criteria

- Feature definitions and card orientation are verified against known-good cards.
- At least one labeled field example and one confirmed negative example exist per enabled diagnosis.
- Card data quality failures block a mechanical diagnosis.
- Alerts include current/baseline feature values and linked card evidence.
- Confirmed field outcomes are retained in the existing chart notes and rod-pump notes; use those records when calibrating later rules.

## Phase 3 — Difficult: engineering models and rare patterns

**Goal:** Introduce high-value but lower-specificity diagnoses only after Phase 2 produces labeled outcomes.

### P3-1. Possible gas lock

```text
IF downhole card area <10% of clean 30-day median
   AND well is stably Running/Pumping Normal
   AND surface/downhole cards and scalar signals pass quality checks
   AND normal stroke length is confirmed
   AND condition persists for >=2 cards
THEN open `possible_gas_lock` high candidate episode for human review.
```

Gas lock is not auto-notified as confirmed until field validation demonstrates acceptable precision for the deployed card model.

### P3-2. Localized plunger sticking or tagging

```text
IF a non-TDC/non-BDC local load discontinuity is present
   at the same normalized position +/-3% on >=3 valid cards
   AND it is not explained by position sync, load signal, or card inversion
THEN open `possible_localized_plunger_sticking` high candidate episode.
```

### P3-3. Tubing movement / unanchored tubing pattern

```text
IF calibrated downhole-card skew/shear exceeds baseline tolerance on >=5 comparable cards
   AND loading, speed, fillage, and card-quality changes do not explain it
THEN open `possible_tubing_movement` advisory candidate episode.
```

### P3-4. Rod-string fatigue-margin decline

Do not implement this diagnosis with the currently available data. It requires rod-string taper, grade/diameter, depth, unit geometry, and engineering load limits that are not accessible.

```text
calculate stress range from peak/minimum polished-rod load
calculate fatigue margin with the supplied engineering method
accumulate cycle exposure from daily cycle count

IF margin is below the supplied engineering threshold
   OR margin declines persistently while stress/cycle exposure rises
THEN open `rod_string_fatigue_exposure` high episode.
```

Until those inputs become available, retain only the Phase-1 load-creep advisory.

### Phase-3 acceptance criteria

- Each rare diagnosis has a documented validation set with field-confirmed outcomes.
- Precision, recall, false-alert burden, and time-to-detection are measured by diagnosis and well class.
- A human review gate remains for gas lock, sticking, tubing movement, and new classifier outputs until validation thresholds are established from field outcomes.
- LLM review receives only normalized, scoped diagnostic evidence, returns validated structured JSON, and records the model/prompt identifier with the response.

## Episode persistence policy

| Event | Action |
|---|---|
| Candidate first meets persistence | Create/update candidate and persist deterministic/LLM evidence. |
| Candidate becomes active | Persist active state, evidence, and recommended action for downstream consumers. |
| Same episode continues | Update evidence only when materially changed. |
| Severity escalates | Persist the escalation and updated LLM differential ranking. |
| Inputs become invalid | Suppress the mechanical episode and open/maintain data-quality episode. |
| Normal recovery persists | Clear after diagnosis-specific recovery period; record closure evidence. |
| Technician closes work | Keep the outcome in the existing chart note or rod-pump note; do not overwrite original episode evidence. |

## OMAI and API replacement work

1. Replace `RodPumpAnalysisClient` Graphite retrieval with catalog-driven target resolution using `MI3.{site}.{well}.{tag}`.
2. Remove the existing `rod_pump_analysis_engine` health score, anomaly-fusion, and legacy named rules.
3. Add an OMAI read-only health repository that loads current episodes, JSON evidence, feature history, and coverage from the new Ometrics tables, plus related existing chart notes and rod-pump notes.
4. Retain `POST /rod-pump-health-report`, but change its response to fleet episodes ordered by severity, persistence, and evidence recency. Return `coverage` and `suppression` explicitly.
5. Update `analyze_rod_pump` to answer with active/recent episodes, associated evidence, missing-data limitations, and field outcomes. It must not recompute a conflicting live diagnosis in OMAI.
6. Replace `docs/rod_pump_rules_and_variables.html` with user-facing documentation maintained alongside the deployed evaluator rules.
7. Delete obsolete tests tied to the retired composite score and add deterministic tests for Graphite target construction, state transitions, daily deduplication, baseline exclusion, persistence, escalation, clearing, suppression, and evidence traceability.

## Rollout and backfill

1. Inventory active rod wells, available tags, card cadence, state-code values, and archive coverage. Do not enable a diagnosis for a well without its required coverage.
2. Add tables and profile-generation workflow. Backfill data-complete, stable historical windows to establish candidate baselines.
3. Backfill 90 days of daily metrics and available card features; write episodes in shadow mode only.
4. Compare shadow episodes with chart notes, rod-pump notes, shutdowns, and technician feedback. Tune thresholds by well class, not across incompatible units.
5. Enable Phase 1 episode persistence for wells passing coverage/baseline checks.
6. Add Phase 2 in review-only mode, then promote individually validated diagnoses to active persisted episodes.
7. Keep Phase 3 behind explicit approval and human-review gates.

## Success measures

- No diagnosis is emitted when required source coverage is invalid.
- Every persisted episode links to concrete Graphite/card evidence and the baseline values used by the rule.
- Duplicate controller protection events decline; recurrence/trend alerts become actionable.
- False-alert rate, median lead time, technician-confirmation rate, and time-to-clear are measured per diagnosis.
- OMAI explains the same persisted episode that Ometrics notified; no competing health scores or live heuristics remain.
