# Phase 2 Implementation Plan: Rod-Pump Card Diagnostics

## Objective

Add validated, explainable diagnostics from hourly surface and downhole dynamometer cards. Phase 2 uses only cards whose `card_type` is `current`. Each pull contains multiple current cards per side; the existing archive medoid-selection method chooses one representative real card for the surface side and one for the downhole side. Phase 2 uses the same selection. It writes derived records and episodes only; it does not send notifications. The `rod_pump_monitoring` report configuration remains the authoritative well scope. A well with an active `well_shutdowns` interval is excluded from feature extraction and diagnosis.

All Phase 2 diagnoses begin in **review-only** mode. They become active only after card orientation, feature quality, and labeled field examples have been accepted for the relevant well class.

## Diagnoses

1. `gas_interference`
2. `traveling_valve_leak_or_delayed_closure`
3. `standing_valve_leak_or_delayed_opening`
4. `progressive_mechanical_friction`
5. `loading_balance_change_suspected`
6. `declining_pump_performance`

These names describe a pattern or suspected change. They do not confirm a mechanical failure, worn pump, paraffin, scale, or an unsafe controller setting.

## Data and persistence

### Existing sources

- `rod_pump_monitoring_data` and `rod_pump_monitoring_data_cards`: latest live card pulls and their well associations.
- `rod_pump_cards`: raw card arrays and card metadata.
- `rod_pump_card_archive`: retained historical card coverage.
- `rod_pump_health_daily_metrics`: `Pump Fillage`, loads, `Stroke Min`, `Stroke Length`, modes, and setpoints already derived from Graphite.
- `rod_pump_health_historical_baselines`: current scalar baseline and configuration.
- `rod_pump_health_episodes`: episode lifecycle and bounded JSON evidence.

### New table: `rod_pump_health_card_features`

One row per valid or invalid selected card side from a pull. This is an idempotent derived record written by OMAI using `DB_USER`.

```text
id bigint primary key
well_id bigint not null -> wells.id
pull_timestamp bigint not null
card_side varchar(16) not null              # surface | downhole
card_type varchar(64) not null
source_card_id bigint nullable -> rod_pump_cards.id
observed_at datetime not null
quality_status varchar(32) not null         # valid | invalid | orientation_unknown | incomplete
quality_reasons_json json not null
orientation varchar(24) nullable            # verified_upstroke_first | verified_downstroke_first
raw_position_min decimal nullable
raw_position_max decimal nullable
raw_load_min decimal nullable
raw_load_max decimal nullable
feature_json json not null
selection_metadata_json json not null
created_at / updated_at
unique(well_id, pull_timestamp, card_side, card_type)
index(well_id, observed_at)
```

`feature_json` preserves raw and normalized card features: point count, normalized stroke span, upstroke/downstroke pickup and release positions, top/bottom transition width and sharpness, residual bottom load, loop area, load spread, loading asymmetry, card-similarity distance, and local discontinuities. `selection_metadata_json` preserves the selected source-card ID, candidate-card count, medoid distance, and selection algorithm (`current-card-medoid-v1`).

### New table: `rod_pump_health_card_baselines`

One newest clean-card baseline per comparable well/card side/card type. No versioning is kept.

```text
id bigint primary key
well_id bigint not null -> wells.id
card_side varchar(16) not null
card_type varchar(64) not null
last_refreshed_at datetime not null
baseline_start_at datetime nullable
baseline_end_at datetime nullable
baseline_status varchar(32) not null        # stable | insufficient_data | invalidated
eligible_card_count int unsigned not null
feature_summary_json json not null
exclusion_summary_json json not null
created_at / updated_at
unique(well_id, card_side, card_type)
```

`feature_summary_json` contains robust 30-day medians and tolerances for each feature. `exclusion_summary_json` records why cards were rejected: invalid orientation, low fillage/pump-off, shutdown, configuration change, active high episode, or non-comparable speed/stroke/mode.

Episode evidence stays in `rod_pump_health_episodes.evidence_json`; it records source feature-row IDs, pull timestamps, current/baseline values, scalar comparison values, rule thresholds, persistence counts, and Ometrics AI output.

## Step 1 — Card selection and quality gate

1. Filter every pull to `card_type = current`; ignore every other card type.
2. For each side, run the existing archive `current-card-medoid-v1` selection across the five current cards. It chooses the real current card with the lowest total normalized-curve distance to the other valid candidates. Do not average or geometrically merge curves: averaging can blur valve transitions, pickup, and localized discontinuities.
3. Store features only for that selected surface medoid and selected downhole medoid. Retain candidate count and medoid distance as quality context.
4. Join live pulls first, then use archive records for history gaps. The archive already retains the same selected medoid per side/pull.
5. Validate every selected card before extracting a feature:
   - finite numeric position/load arrays and compatible lengths;
   - minimum point count and non-zero position/load span;
   - traversal/orientation metadata present and internally consistent;
   - expected surface/downhole side;
   - pull/card timestamp alignment;
   - no unsupported repeated-position branch or inversion.
6. Normalize position to observed-stroke percent while retaining raw units.
7. Split upstroke and downstroke only from verified orientation metadata. An unknown orientation is `orientation_unknown`, never guessed.
8. Apply a per-card stable-pumping-state gate before a card can establish a baseline or support a diagnosis. With 15-minute `Well State Code` samples, the card must be continuously in one of `7` (Pumping Normal), `8` (Pumping Host), `9` (Pumping Timed Mode), or `10` (Pumping HOA) for the preceding **15 minutes**, at the card timestamp, and for the following **10 minutes**. Evaluate the state run around the individual card timestamp; do not discard an entire day merely because it contains an otherwise normal controller pump-off cycle. A missing, downtime, malfunction, stopped, calibration, or other non-pumping state in this window makes the card ineligible. States that begin and clear entirely between samples remain out of scope.

Invalid cards are persisted for coverage/audit but cannot establish a baseline or trigger a mechanical diagnosis.

## Step 2 — Add feature extraction to the existing evaluator

Extend `omai-evaluate-rod-pump-health`; do not create a new rod-pump job.

- Scope: configured `rod_pump_monitoring` wells that are not currently shut down.
- Cadence: its existing two-hour cadence.
- For each well, process unprocessed live pulls and a bounded late-data lookback.
- Select one current-card medoid per side, upsert its features by `(well_id, pull_timestamp, card_side, card_type)`, then evaluate current Phase 2 candidate rules using persisted features and the saved baseline.
- Current cards that fail quality/orientation checks are persisted as invalid feature rows and cannot trigger a diagnosis.

Extracted features:

| Card side | Features |
|---|---|
| Downhole | pickup position, load-release position, upstroke transition width/sharpness, bottom transition width/sharpness, residual bottom load, normalized area, normal-card similarity, skew/shear |
| Surface | loop width, load range/spread, card area, loading asymmetry, top/bottom transition features |
| Both | point count, normalized stroke span, raw load range, quality/orientation result, comparison metadata |

## Step 3 — Add card-baseline refresh to the existing history refresh

Extend `omai-refresh-rod-pump-health-history`; do not create a new rod-pump job. It runs nightly after its existing scalar history refresh work.

For each well/side/type, use the newest 30-day eligible-card window. It also backfills missing card features from retained archive pulls before calculating the baseline. Exclude cards when:

- the well was shut down;
- card quality or orientation failed;
- the per-card stable-pumping-state gate fails: `Well State Code` is not continuously in states 7, 8, 9, or 10 for 15 minutes before the card, at the card, and 10 minutes after the card;
- operation mode, control mode, `Stroke Min`, `Stroke Length`, or relevant configuration differs materially from the comparison set;
- a high-severity active health episode overlaps the card;
- the card is not the configured type for that side.

Require a minimum validated-card count before a baseline is `stable`. Store robust medians plus calibrated tolerance bands, not an average card copied from another well.

## Step 4 — Review-only diagnostic rules

### Gas interference despite acceptable fillage

Open a review candidate when four consecutive valid downhole cards show:

- pickup position later than both 15% of observed stroke and the clean-card tolerance;
- rounded/delayed upstroke transition relative to baseline;
- `Pump Fillage` above `Fillage Setpoint`.

Supporting metrics:

```text
Downhole pickup position: {current}% of observed stroke (30-day clean-card median: {baseline}%)
Upstroke transition width: {current} (30-day clean-card median: {baseline})
Pump Fillage: {value}% (Fillage Setpoint: {setpoint}%)
Consecutive qualifying valid cards: {count}
```

### Traveling-valve leakage or delayed closure

Require late/incomplete upstroke pickup, widened/less-sharp top transition, and deteriorated downhole-card similarity on at least three of the latest five valid cards, persisting or progressing for five days. Exclude cards that fail the per-card stable-pumping-state gate.

Supporting metrics: upstroke pickup position, top-transition width/sharpness, normal-card similarity distance, qualifying cards/5, and persistence days.

### Standing-valve leakage or delayed opening

Require delayed/incomplete downstroke release, widened/less-sharp bottom transition, and residual bottom-load deviation on at least three of the latest five valid cards, persisting or progressing for five days. Exclude cards that fail the per-card stable-pumping-state gate.

Supporting metrics: downstroke release position, bottom-transition width/sharpness, residual bottom load, qualifying cards/5, and persistence days.

### Progressive mechanical friction

Require elevated surface loop width and/or load spread, a supporting surface-card area/load trend, matching daily load direction, comparable `Stroke Min`, `Stroke Length`, modes, and fillage, for ten days.

Supporting metrics: surface loop width, load spread, surface-card area, 7-day/30-day `Yesterday Peak Load` and `Yesterday Min Load`, `Stroke Min`, `Stroke Length`, `Pump Fillage`, and comparator eligibility.

### Loading-balance change suspected

Require a 14-day surface-card loading-asymmetry shift, opposite-direction peak/minimum daily load movement, and no speed, stroke-length, configuration, or calibration explanation.

Supporting metrics: surface loading asymmetry, 14-day `Yesterday Peak Load` trend, 14-day `Yesterday Min Load` trend, `Stroke Min`, `Stroke Length`, controller setting changes, and calibration status.

### Declining pump performance

Require a seven-day `Pump Fillage` average declining for ten days plus deteriorating downhole-card area or valve-leakage feature, with unchanged `Fillage Setpoint`, operation mode, `Stroke Length`, and `Stroke Min`.

Supporting metrics: 7-day and 30-day `Pump Fillage`, downhole-card area, valve-leakage feature trend, `Fillage Setpoint`, `Operation Mode Setpoint`, `Stroke Length`, and `Stroke Min`.

## Step 5 — Episode lifecycle and Ometrics AI

Use the existing episode table and lifecycle. Phase 2 candidates remain `candidate` in review-only mode; no downstream notification is sent.

For a material candidate/change, persist deterministic evidence first, then request Ometrics AI for explanation, differential ranking, and recommended checks. Its packet includes only normalized feature values, units, baseline period, scalar context, card IDs/pull timestamps, quality exclusions, and rule result. It cannot create, clear, escalate, or validate a diagnosis.

Supporting-metrics text in Ometrics is deterministic. Ometrics AI explanation is displayed separately and must preserve typed units.

## Step 6 — Ometrics page and API

Extend Rod Pumps Health with Phase 2 diagnosis labels, hoverable Supporting metrics cards, and links to the source surface/downhole card pull. Display quality exclusions and baseline status before a diagnosis. Do not use health-score terminology.

## Step 7 — Validation and rollout

1. Backfill card features for 90 days in shadow mode.
2. Produce per-well feature coverage: valid-card count, orientation success, selected type, and clean-baseline eligibility.
3. For each diagnosis, assemble at least one known field-positive and one confirmed negative example.
4. Run all six rules in review-only mode; compare candidates with chart notes and rod-pump notes.
5. Tune tolerances by comparable well/unit class, not across the field.
6. Promote one diagnosis at a time only after accepted precision, false-alert burden, and lead-time review.

## Acceptance criteria

- No Phase 2 diagnosis is emitted from an invalid, unknown-orientation, shut-down, pump-off, or non-comparable card.
- Every candidate links to current and baseline feature rows and the triggering card pulls.
- Supporting metrics show exact card-feature names, scalar data-point names, values, units, and comparison windows.
- Surface and downhole cards are never blended as a single baseline.
- Ometrics AI unit validation and fallback work for every Phase 2 packet.
