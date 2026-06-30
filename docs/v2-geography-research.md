# Phase 1: v2 Geography Data-Contract Research

This report profiles the current geography contract in `polisen-se-events-history` for the planned v2 export. It uses the current full v1 parquet from CrimeCity plus local administrative reference data; it does not use live network calls.

> **Phase 2 implementation update:** The final implementation decision is a clean breaking v2 schema in the existing `events.parquet` release asset. Legacy geography aliases `location_name`, `latitude`, and `longitude` are removed rather than kept for rollout compatibility.

## Executive summary

- The current `location_name` column is the raw Swedish Police API `location.name`, not always a municipality.
- In the profiled v1 parquet (`81,946` rows), raw API locations classify as:
  - `53,009` municipality rows (`64.69%`)
  - `28,937` county rows (`35.31%`)
  - `0` unknown rows
- A conservative title-suffix rule would deterministically assign a municipality for many county-level API rows, but only as derived enrichment rather than raw Police location evidence:
  - `60,274` rows have an official municipality as title suffix (`73.55%`).
  - `7,265` raw-county rows have a title municipality in the same county.
  - `0` raw-county/title-municipality cross-county conflicts were found.
  - `1` raw-municipality/title-municipality mismatch was found: event `472285`, API `Järfälla`, title suffix `Upplands-Bro`; both are in Stockholms län.
- Recent source behavior has changed materially:
  - 2026-Q2: `5,943/5,943` rows (`100%`) have raw API county locations.
  - Current `events.json` window: `500/500` rows are raw API county locations, all with `location.gps` present.
- Final v2 contract: preserve raw API geography as `api_location_*`, add deterministic derived administrative fields as `derived_*`, and remove legacy `location_name`, `latitude`, `longitude` from the export schema.

## Local reproducibility and provenance

The profile is a local Phase 1 spike. It is reproducible from the exact command below plus pinned local file hashes, but three inputs currently come from a sibling CrimeCity checkout and are **not** part of this repository. A clean upstream checkout can read the report and committed manifest, but cannot recompute the full profile unless those sibling inputs are present. That is acceptable for evidence gathering only; the production exporter uses the vendored SCB-derived `reference/swedish_admin_areas.csv` and must not depend on CrimeCity at runtime.

Run from this repository root:

```bash
uv run scripts/research/profile_geography_contract.py \
  --events-parquet ../crimecity3k/data/events.parquet \
  --boundaries ../crimecity3k/data/municipalities/boundaries.geojson \
  --population ../crimecity3k/data/municipalities/population.csv \
  --raw-events-json events.json \
  --output-dir tmp/geography-profile \
  --strict-provenance
```

The script compares the current run with the committed `docs/v2-geography-profile-manifest.json`. Strict comparison validates the manifest schema, script path/version/SHA-256, data input paths/existence/sizes/SHA-256 hashes, data cutoffs, reference checks, current raw-window GPS/API-granularity metrics, and headline profile/validation counts. Recorded git repo/head/dirty metadata is diagnostic only and is not part of strict comparison; content hashes are the authoritative input identity. Mismatches print a clear warning by default; `--strict-provenance` exits non-zero on mismatches or when the expected manifest is missing. Non-strict runs warn and skip comparison if the manifest is absent. To intentionally refresh the pinned manifest after reviewing a new data cut or script change, run the same command with `--manifest-out docs/v2-geography-profile-manifest.json --no-expected-manifest`.

Observed strict run result:

```text
manifest check: docs/v2-geography-profile-manifest.json matches strict content/script/profile invariants (git metadata diagnostic only)
events: 81,946
api municipality: 53,009
api county: 28,937
title municipality: 60,274
prospective derived municipality: 60,273
unresolved municipality: 21,673
cross-county conflicts: 0
self-checks: 14 passed
wrote: tmp/geography-profile/summary.json
wrote: tmp/geography-profile/summary.md
```

Artifacts and commit hygiene:

- `scripts/research/profile_geography_contract.py` — tracked reproducible Phase 1 spike script.
- `docs/v2-geography-profile-manifest.json` — tracked machine-readable manifest with script hash/version, input hashes, cutoffs, reference checks, and headline metrics.
- `tmp/geography-profile/summary.json` — generated full machine-readable profile; ignored by `.gitignore`, not intended for commit.
- `tmp/geography-profile/summary.md` — generated Markdown profile with longer tables/examples; ignored by `.gitignore`, not intended for commit.

Input provenance from the regenerated profile. Git fields are retained to aid investigation but are diagnostic only; strict mode validates the path/content fields and deterministic outputs instead.

| Input | Path | Size bytes | SHA-256 | Git repo | Git HEAD | Path dirty? |
| --- | --- | ---: | --- | --- | --- | --- |
| events parquet | `../crimecity3k/data/events.parquet` | 13,340,714 | `172684e33024a8050514e6a03bced421214cca89b7118a230a68b6cbf7865bf3` | `../crimecity3k` | `2fd6066a3dd2bd29bb3e1255130bca3ce5a6c53c` | no |
| boundaries | `../crimecity3k/data/municipalities/boundaries.geojson` | 1,979,751 | `5b65a1b22e3dd34731a1c1a4ec0ccd09c36ebb4da36e1d9e56d72fac04cfe25a` | `../crimecity3k` | `2fd6066a3dd2bd29bb3e1255130bca3ce5a6c53c` | no |
| population | `../crimecity3k/data/municipalities/population.csv` | 5,772 | `df002b4dff7b70ca33970b7d36ad7177df5a734ab00e0eca76e4c2f303e9f0c3` | `../crimecity3k` | `2fd6066a3dd2bd29bb3e1255130bca3ce5a6c53c` | no |
| raw events window | `events.json` | 206,295 | `5788d9cf164d3f7f0dbf1fcd1b4d2183af244ccc58907e2dc976869276e48df6` | `.` | `b1c9a6d3b24987979a736b2b45bab58214fb1090` | no |

Data cutoffs are separate because the full v1 parquet and current raw `events.json` are different snapshots:

| Input | Rows | Min datetime | Max datetime | Min event id | Max event id |
| --- | ---: | --- | --- | ---: | ---: |
| full export parquet | 81,946 | 2022-09-26 15:55:23 +02:00 | 2026-06-30 7:32:17 +02:00 | 371971 | 645625 |
| current raw events window | 500 | 2026-06-19 20:50:07 +02:00 | 2026-06-30 10:57:12 +02:00 | 643372 | 645690 |

Algorithm constraints:

- Normalize names with trim, whitespace collapse, and Unicode `casefold()` only.
- Match exact official municipality/county names only.
- Parse event title suffix after the final comma only.
- Do not parse `summary`, HTML body, or other narrative text.
- Do not geocode or use fuzzy aliases.
- Run 14 table-driven self-checks over the API-granularity/title-granularity matrix before profiling.

## Source and reference-data comparison

| Source | Local file / link | Role | Phase 1 finding |
| --- | --- | --- | --- |
| Swedish Police API docs | <https://polisen.se/om-polisen/om-webbplatsen/oppna-data/api-over-polisens-handelser/> | Defines raw event `location.name`/`location.gps` semantics. | API location may be county (`län`) or municipality; GPS is the center point for that administrative area, not incident coordinates. |
| Current raw API window | `events.json` | Verifies raw `location.gps` availability before export flattening for the latest 500/current API window only. | `500/500` current rows have `location.gps`; v1 parquet loses the raw GPS string, so full-history raw GPS validity is not proven in Phase 1. |
| Full current v1 export | `../crimecity3k/data/events.parquet` | Data profile input. | `81,946` rows; schema has only `location_name`, parsed `latitude`, parsed `longitude` for geography. |
| Municipality boundaries | `../crimecity3k/data/municipalities/boundaries.geojson` | Provides 290 municipality codes/names and `lan_code`. | 290 features; 21 county codes; every municipality code prefix equals `lan_code`. |
| Population / SCB-derived region metadata | `../crimecity3k/data/municipalities/population.csv` | Provides municipality code/name reference already used by CrimeCity. | 290 rows; exact one-to-one code match with boundaries; 0 normalized name mismatches. |
| Static county code/name table in spike | `scripts/research/profile_geography_contract.py` plus SCB regional divisions (<https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/>) | Spike-only inlined table of 21 county codes/names. | Manually copied from SCB's official current county-code set ("Län och kommuner i kodnummerordning" / regional divisions), checked 2026-06-30; covers every `lan_code` used by boundaries and has no duplicate normalized county names. |

Raw GPS scope note: this phase checked raw `location.gps` only in the current `events.json` window. The full-history v1 parquet has parsed `latitude`/`longitude` but not the original GPS string, so it cannot prove raw GPS validity for historical rows. The v2 export preserves `api_location_gps` directly from raw JSON instead of reconstructing it from parsed coordinates.

Reference checks from the spike:

| Check | Value |
| --- | --- |
| Municipality features in boundaries | 290 |
| Municipality rows in population | 290 |
| County codes/names | 21 |
| Boundary codes missing in population | 0 |
| Population codes missing in boundaries | 0 |
| County codes missing from static county table | 0 |
| Municipality code prefix / county-code mismatches | 0 |
| Boundary/population normalized name mismatches | 0 |
| Duplicate normalized municipality names | 0 |
| Duplicate normalized county names | 0 |
| Municipality/county normalized name collisions | 0 |

Production reference-data decision for Phase 2:

1. Use the vendored upstream reference file generated from official SCB region metadata: `reference/swedish_admin_areas.csv` with `municipality_code`, `municipality_name`, `county_code`, `county_name`.
2. Use the existing CrimeCity files only as Phase 1 evidence; do not make the export depend on the CrimeCity repo at runtime.
3. Keep aliases/spelling variants out of v2. If aliases become necessary later, add an explicit alias table with tests and reject ambiguous aliases.

Gotland/code edge case: Gotlands län is county code `09`; Gotland municipality is `0980`. In the checked reference files, every municipality code's first two digits match its county code, including Gotland.

## Overall coverage

| Scope | Rows | API municipality | API county | Title municipality | Title county | Prospective derived municipality | Unresolved municipality | Cross-county conflicts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| All events | 81,946 | 53,009 (64.69%) | 28,937 (35.31%) | 60,274 (73.55%) | 21,670 (26.44%) | 60,273 (73.55%) | 21,673 (26.45%) | 0 |
| Non-summary | 67,992 | 52,971 (77.91%) | 15,021 (22.09%) | 60,232 (88.59%) | 7,758 (11.41%) | 60,231 (88.59%) | 7,761 (11.41%) | 0 |
| Summary | 13,954 | 38 (0.27%) | 13,916 (99.73%) | 42 (0.30%) | 13,912 (99.70%) | 42 (0.30%) | 13,912 (99.70%) | 0 |

Raw API location vs title suffix:

| API granularity | Title suffix granularity | Rows |
| --- | --- | --- |
| county | county | 21,670 |
| county | municipality | 7,265 |
| county | unknown | 2 |
| municipality | municipality | 53,009 |

Prospective derivation rules:

| Rule | Rows |
| --- | --- |
| `api_location_municipality` | 53,008 |
| `title_municipality_validated_by_api_county` | 7,265 |
| unresolved | 21,673 |

Validation statuses:

| Status | Rows |
| --- | --- |
| `accepted_api_municipality` | 53,008 |
| `accepted_title_municipality_within_api_county` | 7,265 |
| `title_county_matches_api_county` | 21,670 |
| `api_county_no_title_municipality` | 2 |
| `api_municipality_title_mismatch_same_county` | 1 |

No profiled row had raw API municipality plus a title-county suffix. The script still has table-driven self-checks for that matrix branch: same-county title county remains accepted from the raw municipality with a separate status, while different-county title county leaves municipality/county unresolved and counts a cross-county conflict.

## Coverage by quarter

| Quarter | Rows | API municipality | API county | Title municipality | Prospective derived municipality | Unresolved municipality |
| --- | --- | --- | --- | --- | --- | --- |
| 2022-Q3 | 9 | 9 (100.00%) | 0 (0.00%) | 9 (100.00%) | 9 (100.00%) | 0 (0.00%) |
| 2022-Q4 | 112 | 108 (96.43%) | 4 (3.57%) | 108 (96.43%) | 108 (96.43%) | 4 (3.57%) |
| 2023-Q1 | 1,046 | 834 (79.73%) | 212 (20.27%) | 834 (79.73%) | 834 (79.73%) | 212 (20.27%) |
| 2023-Q2 | 6,796 | 5,116 (75.28%) | 1,680 (24.72%) | 5,116 (75.28%) | 5,116 (75.28%) | 1,680 (24.72%) |
| 2023-Q3 | 6,293 | 4,760 (75.64%) | 1,533 (24.36%) | 4,760 (75.64%) | 4,760 (75.64%) | 1,533 (24.36%) |
| 2023-Q4 | 6,213 | 4,595 (73.96%) | 1,618 (26.04%) | 4,595 (73.96%) | 4,594 (73.94%) | 1,619 (26.06%) |
| 2024-Q1 | 5,946 | 4,425 (74.42%) | 1,521 (25.58%) | 4,425 (74.42%) | 4,425 (74.42%) | 1,521 (25.58%) |
| 2024-Q2 | 6,311 | 4,732 (74.98%) | 1,579 (25.02%) | 4,732 (74.98%) | 4,732 (74.98%) | 1,579 (25.02%) |
| 2024-Q3 | 5,954 | 4,423 (74.29%) | 1,531 (25.71%) | 4,423 (74.29%) | 4,423 (74.29%) | 1,531 (25.71%) |
| 2024-Q4 | 6,395 | 4,682 (73.21%) | 1,713 (26.79%) | 4,682 (73.21%) | 4,682 (73.21%) | 1,713 (26.79%) |
| 2025-Q1 | 6,420 | 4,785 (74.53%) | 1,635 (25.47%) | 4,785 (74.53%) | 4,785 (74.53%) | 1,635 (25.47%) |
| 2025-Q2 | 6,431 | 4,739 (73.69%) | 1,692 (26.31%) | 4,740 (73.71%) | 4,740 (73.71%) | 1,691 (26.29%) |
| 2025-Q3 | 6,062 | 4,372 (72.12%) | 1,690 (27.88%) | 4,372 (72.12%) | 4,372 (72.12%) | 1,690 (27.88%) |
| 2025-Q4 | 6,301 | 4,535 (71.97%) | 1,766 (28.03%) | 4,535 (71.97%) | 4,535 (71.97%) | 1,766 (28.03%) |
| 2026-Q1 | 5,714 | 894 (15.65%) | 4,820 (84.35%) | 3,970 (69.48%) | 3,970 (69.48%) | 1,744 (30.52%) |
| 2026-Q2 | 5,943 | 0 (0.00%) | 5,943 (100.00%) | 4,188 (70.47%) | 4,188 (70.47%) | 1,755 (29.53%) |

## Coverage by recent month

| Month | Rows | API municipality | API county | Title municipality | Prospective derived municipality | Unresolved municipality |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-06 | 1,920 | 0 (0.00%) | 1,920 (100.00%) | 1,351 (70.36%) | 1,351 (70.36%) | 569 (29.64%) |
| 2026-05 | 2,052 | 0 (0.00%) | 2,052 (100.00%) | 1,470 (71.64%) | 1,470 (71.64%) | 582 (28.36%) |
| 2026-04 | 1,971 | 0 (0.00%) | 1,971 (100.00%) | 1,367 (69.36%) | 1,367 (69.36%) | 604 (30.64%) |
| 2026-03 | 1,872 | 0 (0.00%) | 1,872 (100.00%) | 1,273 (68.00%) | 1,273 (68.00%) | 599 (32.00%) |
| 2026-02 | 1,820 | 0 (0.00%) | 1,820 (100.00%) | 1,273 (69.95%) | 1,273 (69.95%) | 547 (30.05%) |
| 2026-01 | 2,022 | 894 (44.21%) | 1,128 (55.79%) | 1,424 (70.43%) | 1,424 (70.43%) | 598 (29.57%) |
| 2025-12 | 2,038 | 1,441 (70.71%) | 597 (29.29%) | 1,441 (70.71%) | 1,441 (70.71%) | 597 (29.29%) |
| 2025-11 | 2,112 | 1,546 (73.20%) | 566 (26.80%) | 1,546 (73.20%) | 1,546 (73.20%) | 566 (26.80%) |
| 2025-10 | 2,151 | 1,548 (71.97%) | 603 (28.03%) | 1,548 (71.97%) | 1,548 (71.97%) | 603 (28.03%) |
| 2025-09 | 2,140 | 1,563 (73.04%) | 577 (26.96%) | 1,563 (73.04%) | 1,563 (73.04%) | 577 (26.96%) |
| 2025-08 | 2,060 | 1,500 (72.82%) | 560 (27.18%) | 1,500 (72.82%) | 1,500 (72.82%) | 560 (27.18%) |
| 2025-07 | 1,862 | 1,309 (70.30%) | 553 (29.70%) | 1,309 (70.30%) | 1,309 (70.30%) | 553 (29.70%) |

## Coverage by event type (top 20)

| Event type | Rows | API county | Title municipality | Prospective derived municipality | Unresolved municipality |
| --- | --- | --- | --- | --- | --- |
| Trafikolycka | 12,738 | 1,803 (14.15%) | 12,657 (99.36%) | 12,657 (99.36%) | 81 (0.64%) |
| Sammanfattning natt | 12,337 | 12,307 (99.76%) | 32 (0.26%) | 32 (0.26%) | 12,305 (99.74%) |
| Brand | 5,357 | 765 (14.28%) | 5,326 (99.42%) | 5,326 (99.42%) | 31 (0.58%) |
| Rattfylleri | 4,842 | 695 (14.35%) | 4,778 (98.68%) | 4,778 (98.68%) | 64 (1.32%) |
| Trafikkontroll | 4,793 | 2,818 (58.79%) | 2,071 (43.21%) | 2,071 (43.21%) | 2,722 (56.79%) |
| Övrigt | 4,674 | 3,710 (79.38%) | 1,094 (23.41%) | 1,094 (23.41%) | 3,580 (76.59%) |
| Misshandel | 3,821 | 625 (16.36%) | 3,725 (97.49%) | 3,725 (97.49%) | 96 (2.51%) |
| Stöld | 2,911 | 368 (12.64%) | 2,852 (97.97%) | 2,852 (97.97%) | 59 (2.03%) |
| Trafikolycka, personskada | 2,412 | 279 (11.57%) | 2,365 (98.05%) | 2,365 (98.05%) | 47 (1.95%) |
| Trafikbrott | 1,905 | 264 (13.86%) | 1,858 (97.53%) | 1,858 (97.53%) | 47 (2.47%) |
| Sammanfattning kväll och natt | 1,597 | 1,591 (99.62%) | 6 (0.38%) | 6 (0.38%) | 1,591 (99.62%) |
| Stöld/inbrott | 1,490 | 201 (13.49%) | 1,462 (98.12%) | 1,462 (98.12%) | 28 (1.88%) |
| Arbetsplatsolycka | 1,414 | 164 (11.60%) | 1,404 (99.29%) | 1,404 (99.29%) | 10 (0.71%) |
| Skadegörelse | 1,349 | 170 (12.60%) | 1,343 (99.56%) | 1,343 (99.56%) | 6 (0.44%) |
| Fylleri/LOB | 1,252 | 68 (5.43%) | 1,222 (97.60%) | 1,222 (97.60%) | 30 (2.40%) |
| Rån | 1,225 | 98 (8.00%) | 1,218 (99.43%) | 1,218 (99.43%) | 7 (0.57%) |
| Trafikolycka, vilt | 1,204 | 424 (35.22%) | 841 (69.85%) | 841 (69.85%) | 363 (30.15%) |
| Försvunnen person | 1,136 | 206 (18.13%) | 1,101 (96.92%) | 1,101 (96.92%) | 35 (3.08%) |
| Narkotikabrott | 1,065 | 142 (13.33%) | 1,051 (98.69%) | 1,051 (98.69%) | 14 (1.31%) |
| Trafikolycka, singel | 977 | 126 (12.90%) | 969 (99.18%) | 969 (99.18%) | 8 (0.82%) |

## Examples

### Raw-county rows assigned by title municipality + API-county validation

| event_id | datetime | type | API location | title suffix | derived municipality |
| --- | --- | --- | --- | --- | --- |
| 581682 | 2025-05-02 17:39:57 +02:00 | Trafikbrott | Stockholms län | Sollentuna | 0163 Sollentuna |
| 620027 | 2026-01-02 23:52:32 +01:00 | Mord/dråp | Västernorrlands län | Ånge | 2260 Ånge |
| 620462 | 2026-01-08 13:46:16 +01:00 | Försvunnen person | Västra Götalands län | Uddevalla | 1485 Uddevalla |
| 622115 | 2026-01-20 11:47:32 +01:00 | Arbetsplatsolycka | Skåne län | Hässleholm | 1293 Hässleholm |
| 622124 | 2026-01-20 13:05:17 +01:00 | Bråk | Skåne län | Malmö | 1280 Malmö |
| 622134 | 2026-01-20 13:47:43 +01:00 | Trafikolycka, personskada | Västra Götalands län | Göteborg | 1480 Göteborg |
| 622135 | 2026-01-20 14:07:46 +01:00 | Knivlagen | Stockholms län | Stockholm | 0180 Stockholm |
| 622140 | 2026-01-20 14:31:09 +01:00 | Rattfylleri | Örebro län | Lindesberg | 1885 Lindesberg |

### Unresolved examples

These remain unresolved because the title suffix is a county, the title suffix is missing/unknown, or API/title municipality signals disagree. This is expected; unresolved rows must remain in the dataset with `derived_municipality_code = null` and be discoverable through global search.

| event_id | datetime | type | summary? | API location | title suffix | reason |
| --- | --- | --- | --- | --- | --- | --- |
| 376300 | 2022-10-15 14:09:08 +02:00 | Mord/dråp | no | Västra Götalands län | Västra Götalands län | title county matches API county |
| 377453 | 2022-10-23 13:13:55 +02:00 | Mord/dråp | no | Östergötlands län | Östergötlands län | title county matches API county |
| 384517 | 2022-11-17 14:49:48 +01:00 | Mord/dråp | no | Uppsala län | Uppsala län | title county matches API county |
| 392271 | 2022-12-23 13:01:50 +01:00 | Mord/dråp, försök | no | Västra Götalands län | Västra Götalands län | title county matches API county |
| 393822 | 2023-01-06 10:54:59 +01:00 | Mord/dråp, försök | no | Gotlands län | Gotlands län | title county matches API county |
| 586066 | 2025-05-20 13:09:32 +02:00 | Trafikkontroll | no | Västerbottens län | empty | API county, no title municipality |
| 643427 | 2026-06-20 17:20:20 +02:00 | Brand | no | Kronobergs län | empty | API county, no title municipality |
| 472285 | 2023-11-25 13:49:29 +01:00 | Farligt föremål, misstänkt | no | Järfälla | Upplands-Bro | API municipality/title municipality mismatch in same county |
| 410687 | 2023-03-21 6:54:58 +01:00 | Sammanfattning natt | yes | Norrbottens län | Norrbottens län | summary, title county matches API county |
| 410685 | 2023-03-21 7:00:19 +01:00 | Sammanfattning natt | yes | Hallands län | Hallands län | summary, title county matches API county |
| 410683 | 2023-03-21 7:00:38 +01:00 | Sammanfattning kväll och natt | yes | Västra Götalands län | Västra Götalands län | summary, title county matches API county |
| 410688 | 2023-03-21 7:01:13 +01:00 | Sammanfattning natt | yes | Västerbottens län | Västerbottens län | summary, title county matches API county |
| 410690 | 2023-03-21 7:16:47 +01:00 | Sammanfattning natt | yes | Västernorrlands län | Västernorrlands län | summary, title county matches API county |
| 395188 | 2023-01-10 17:53:39 +01:00 | Mord/dråp | no | Örebro län | Örebro län | title county matches API county |
| 406666 | 2023-03-06 22:38:04 +01:00 | Mord/dråp, försök | no | Stockholms län | Stockholms län | title county matches API county |
| 409410 | 2023-03-18 13:30:33 +01:00 | Mord/dråp | no | Värmlands län | Värmlands län | title county matches API county |
| 409613 | 2023-03-20 18:58:15 +01:00 | Trafikkontroll | no | Norrbottens län | Norrbottens län | title county matches API county |
| 409614 | 2023-03-20 19:00:42 +01:00 | Trafikkontroll | no | Västerbottens län | Västerbottens län | title county matches API county |
| 409626 | 2023-03-20 20:36:17 +01:00 | Mord/dråp | no | Jönköpings län | Jönköpings län | title county matches API county |
| 409635 | 2023-03-20 21:45:23 +01:00 | Övrigt | no | Hallands län | Hallands län | title county matches API county |
| 409634 | 2023-03-20 21:45:31 +01:00 | Övrigt | no | Västra Götalands län | Västra Götalands län | title county matches API county |
| 410732 | 2023-03-21 11:36:43 +01:00 | Trafikkontroll | no | Västerbottens län | Västerbottens län | title county matches API county |
| 410740 | 2023-03-21 12:45:41 +01:00 | Övrigt | no | Västra Götalands län | Västra Götalands län | title county matches API county |
| 410741 | 2023-03-21 12:45:50 +01:00 | Övrigt | no | Hallands län | Hallands län | title county matches API county |
| 410745 | 2023-03-21 12:48:25 +01:00 | Rån | no | Örebro län | Örebro län | title county matches API county |

Cross-county conflict examples: none found.

Same-county API/title municipality mismatch:

| event_id | datetime | type | API location | title suffix | recommendation |
| --- | --- | --- | --- | --- | --- |
| 472285 | 2023-11-25 13:49:29 +01:00 | Farligt föremål, misstänkt | Järfälla | Upplands-Bro | Leave `derived_municipality_code` null and count as a mismatch. |

## Recommended v2 schema and null semantics

| Field | Type intent | Null / value semantics |
| --- | --- | --- |
| `api_location_name` | string | Raw `event.location.name` from the Police API. Null only if the source field is missing. It may be a municipality name, county name, or another unmatched value. |
| `api_location_gps` | string | Raw `event.location.gps` from the Police API. Preserve the exact raw string; null only if missing. |
| `api_location_granularity` | enum string | `municipality`, `county`, or `unknown`, from exact reference-name classification of `api_location_name`. Prefer non-null `unknown` over null. |
| `api_location_latitude` | double | Parsed latitude from `api_location_gps`; null if missing, invalid, or outside broad Swedish bounds. |
| `api_location_longitude` | double | Parsed longitude from `api_location_gps`; null if missing, invalid, or outside broad Swedish bounds. |
| `derived_municipality_code` | string | Official 4-digit municipality code when deterministically assigned; otherwise null. Valid null means “not deterministically assigned,” not an error. |
| `derived_municipality_name` | string | Official municipality name for `derived_municipality_code`; null whenever code is null. |
| `derived_county_code` | string | Official 2-digit county code when deterministically assigned from accepted API/title evidence; null for unknown or conflicting evidence. County may be populated even when municipality is null. |
| `derived_county_name` | string | Official county name for `derived_county_code`; null whenever code is null. |

Recommended deterministic assignment policy:

1. Classify `api_location_name` exactly against official municipality and county names.
2. Parse the title suffix after the final comma; if there is no comma, treat the suffix as missing/unknown.
3. If API location is a municipality and the title suffix is the same municipality or unknown, set derived municipality from API location.
4. If API location is a municipality and the title suffix is that municipality's county, set derived municipality from API location but count that broader title signal separately in aggregate metrics.
5. If API location is a municipality and the title suffix points to a different municipality or a different county, leave `derived_municipality_code` null and count the mismatch/conflict.
6. If API location is a county and title suffix is a municipality in that county, set derived municipality from the title suffix as deterministic title-derived enrichment.
7. If API location is a county and the title suffix is a different county or a municipality in a different county, leave conflicting derived geography unresolved and count the cross-county conflict.
8. If API location is unknown and title suffix is a municipality, set derived municipality from title suffix but count it separately in quality metrics.
9. Never parse body text and never distribute county-only rows across municipalities.

Do not add per-row confidence/source/notes/status fields in v2. The rule is reconstructible from `api_location_*`, title, and the committed reference data, while derivation-rule/validation counts belong in release metrics. Daily release checks should publish real source-data mismatches/conflicts as metrics rather than fail solely because they are non-zero; fail the release for impossible reference/schema conditions such as duplicate official names, missing reference codes, invalid code shapes, or missing required columns.

## Compatibility and release-artifact decision

The chosen Phase 2 rollout is a clean breaking schema in the existing `events.parquet` release asset:

- Remove `location_name`, `latitude`, and `longitude`.
- Add explicit `api_location_*` and `derived_*` fields in the same artifact.
- Do not publish compatibility aliases or a parallel v1/v2 artifact for this phase.
- Optionally add a future release metrics artifact such as `geography-quality.json` containing row counts, coverage, conflicts, unresolved counts, and reference version/source.

This supersedes the Phase 1 compatibility recommendation to keep legacy aliases for a transition period.

## Phase 1 decisions encoded

1. **V2 rollout posture:** publish a clean breaking v2 geography schema in the existing `events.parquet`; remove `location_name`, `latitude`, and `longitude` rather than keeping compatibility aliases.
2. **Production reference data:** use the vendored SCB-derived upstream reference file `reference/swedish_admin_areas.csv` with `municipality_code`, `municipality_name`, `county_code`, `county_name`. Do not depend on CrimeCity at runtime.
3. **Conflict/mismatch policy:** if API/title evidence points to different municipalities, including the observed same-county mismatch, leave `derived_municipality_code` null. If API/title evidence points to different counties, including raw-municipality plus different title county, leave conflicting derived geography unresolved and count it in release metrics. Do not fail a daily release solely for real source-data conflicts; do fail for impossible reference/schema conditions.
4. **Aliases/spelling variants:** no aliases in v2 unless a future explicit alias table documents and tests them. Use exact official names only for now.

## Phase 1 conclusion

The data supports a simple, explainable v2 contract. Raw Police API geography must be preserved as raw API fields; deterministic municipality enrichment should be separate and nullable. Exact title-suffix matching validated by API county provides useful title-derived assignments for many recent county-level rows without claiming that titles are raw location evidence. County-only summaries and county-only non-summary rows remain valid unresolved rows and need downstream global search rather than forced municipality assignment. The Phase 1 rollout/reference/conflict/alias decisions above are no longer open questions for implementation planning.
