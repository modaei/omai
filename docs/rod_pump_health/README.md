# Rod pump health analyzer

`POST /rod-pump-dynograph-analysis` accepts only a well database ID:

```json
{"well_id": 23}
```

UMetriqs validates the current-site scope, calls this endpoint synchronously, and saves the returned
analysis in `rod_pump_health_analyses`. OMAI resolves the well's site from the operational database
and checks the `rod_pump_monitoring` configuration before reading its archived cards.

## Data preparation and analysis

OMAI reads retained current cards marked `daily_baseline` from the archive database. It uses actual
capture timestamps, deduplicates repeated captures, and selects the lower median card for each day.
The window covers fourteen calendar days ending on the latest available daily card. Event-only and
pending cards are excluded. These daily representatives are not known healthy cards.

OMAI submits the selected surface cards to Omreports' existing `rod_pump_downhole_cards` report API
in one batch. Archive IDs are carried through the existing `card_id` field to match results to their
source cards. Omreports requires no changes. Settings snapshots are not captured; pump curves use
current controller settings. Surface analysis can continue if pump calculation fails.

OMAI computes physical features and Theil–Sen trends, renders field cards, pairwise normalized
field/template comparisons, and historical overlays, then sends images and measured JSON to its
configured vision-capable model. The prompt asks it to interpret recent cards against supplied
templates and explain historical trends using card data and rod-pump knowledge. It returns up to two
concise diagnoses with one reasoning paragraph each. OMAI preserves returned findings without
applying evidence-reference or match-strength filters. A successful empty review is healthy;
provider or review failures remain unavailable.
The analyzer does not write database records or restore the legacy score/episode pipeline.

OMAI computes the input signature and reads the latest saved analysis from the operational database.
An unchanged successful analysis returns `unchanged: true` without another LLM request. Downhole
calculation still runs before this comparison. UMetriqs verifies that its saved signature matches
before clearing the refresh indicator. New results include prepared `input_cards`, measurements,
findings, window timestamps, and the signature for UMetriqs to persist.

Versions remain part of the API and cache signature, not separate database columns. Update the
corresponding constants in both services when analyzer or template behavior changes.
`reference-guide.pdf` is the supplied qualitative reference, not ground truth or instructions.

## Configuration

OMAI requires SELECT access to `wells`, `report_configurations`, and `rod_pump_health_analyses` in
the operational database, and to `rod_pump_card_archive` in the archive database. Its database
settings must identify the same operational and archive databases used by UMetriqs.

Operational connection settings remain `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME`.
Configure the archive connection with `DB_ARCHIVE_HOST`, `DB_ARCHIVE_PORT`, `DB_ARCHIVE_USERNAME`,
`DB_ARCHIVE_PASSWORD`, and `DB_ARCHIVE_DATABASE`. Unset archive options fall back to the operational
connection. `DB_ARCHIVE_USER` is also accepted when `DB_ARCHIVE_USERNAME` is not set. Set `TIMEZONE`
to UMetriqs' application timezone so both services use the same daily boundaries.

`OMREPORTS_API_URL` identifies the existing report endpoint. OMAI allows up to 180 seconds for its
calculation request. UMetriqs allows 360 seconds for the combined OMAI request; browser/controller
refresh limits remain 420 seconds. Reverse-proxy and PHP-FPM timeouts must accommodate those limits.
No queue or worker is involved.

Install the OMAI requirements, including Pillow. Live interpretation requires configured LLM
credentials and a model supporting image input and structured output. Provider failures return a
review failure; UMetriqs retains the previous successful analysis.

## Validation

```sh
pytest tests/test_rod_pump_health_analysis.py tests/test_rod_pump_health_reader.py
```

Tests cover well/site scope, daily selection, unit conversion, existing report responses, missing
pump curves, changed-source invalidation, and unchanged-result caching. The request rejects uploaded
card arrays; OMAI always fetches source data itself. Trend estimates require at least three archived
cards with measured values; missing days are never interpolated.

## Failure diagnostics

Failed reviews now return `review_error` and log a warning with the well ID in OMAI's server output.
Diagnostics distinguish LLM/provider errors from unusable-card coverage. Provider messages are
bounded and redact authentication tokens and image blobs. OMAI preserves the model's returned
findings instead of dropping them for evidence-reference or match-strength checks.

UMetriqs logs the reason and includes it in the health command's per-well error and saved refresh error.
Previous successful results remain intact. Deploy both services and restart the OMAI process before
rerunning `php artisan rod-pump-health:analyze --site-id=...` to see these expanded diagnostics.

## Review guidance

The LLM reviews Lufkin SAM1 controller data from a unit without a VFD. It compares recent cards with
same-side templates and reviews archived cards for patterns and trends. Its prompt requests a
successful empty review when no problem is identified and asks it to explain the oil-field meaning
of a historical trend rather than only repeating measurements.

## Review behavior

The LLM returns up to two concise diagnoses and one reasoning paragraph for each. UMetriqs shows the
returned findings without evidence-based filtering. SAM1 pumping-state selection remains ten
minutes before and five minutes after the selected current card. A new analyzer version invalidates
cached reviews so older findings are replaced on the next analysis.

UMetriqs no longer rejects responses for an analyzer/template version mismatch. OMAI still includes
its versions in input signatures so the revised prompt invalidates cached earlier interpretations.
