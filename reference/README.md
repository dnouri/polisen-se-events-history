# Swedish administrative area reference

`swedish_admin_areas.csv` is the vendored runtime reference for v2 geography enrichment. The exporter does not fetch reference data from the network at runtime.

## Contents

One row per current Swedish municipality:

- `municipality_code` — official 4-digit municipality code
- `municipality_name` — official Swedish municipality name
- `county_code` — official 2-digit county code (`municipality_code` prefix)
- `county_name` — official Swedish county (`län`) name

## Provenance

- Municipality codes/names and county codes follow SCB regional divisions, specifically the current county/municipality code ordering ("Län och kommuner i kodnummerordning").
- County names are the official SCB Swedish county names (for example `Gotlands län`, county code `09`).
- The file was assembled for the v2 geography work and cross-checked against the Phase 1 SCB-derived boundary/population inputs described in `docs/v2-geography-research.md`.

## Validation and updates

`geography.load_geography_reference()` validates completeness at runtime: 290 municipalities, 21 counties, and the expected SCB county-code set.

When SCB regional divisions change, update this CSV in the same shape, keep names/codes official, and update the expected administrative constants in `geography.py` plus the corresponding geography tests; do not change the CSV alone. Then run the geography/export tests and `scripts/validate_export_schema.py` against a generated Parquet file before publishing.
