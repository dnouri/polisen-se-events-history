#!/usr/bin/env python3
"""Validate the v2 release schema and deterministic geography contract."""

# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "duckdb>=1.0.0",
# ]
# ///

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from export_schema import LEGACY_GEOGRAPHY_COLUMNS, PARQUET_EXPORT_SCHEMA  # noqa: E402
from geography import (  # noqa: E402
    GEOGRAPHY_EXPORT_FIELDS,
    SWEDEN_LATITUDE_BOUNDS,
    SWEDEN_LONGITUDE_BOUNDS,
    GeographyReference,
    classify_location_name,
    load_geography_reference,
    parse_gps,
    resolve_event_geography,
    validate_geography_reference,
)

ALLOWED_API_LOCATION_GRANULARITIES = frozenset({"municipality", "county", "unknown"})
EXAMPLE_LIMIT = 5


def _duckdb_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _duckdb_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _format_schema(schema: Sequence[tuple[str, str]]) -> str:
    return "[" + ", ".join(f"{name}:{column_type}" for name, column_type in schema) + "]"


def _format_examples(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    formatted_rows = []
    for row in rows:
        formatted_rows.append(
            "{" + ", ".join(f"{column}={value!r}" for column, value in zip(columns, row)) + "}"
        )
    return "[" + ", ".join(formatted_rows) + "]"


def _is_close(actual: float | None, expected: float) -> bool:
    return actual is not None and math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)


def _values_match(actual: object, expected: object) -> bool:
    if isinstance(expected, float):
        return isinstance(actual, (float, int)) and math.isclose(
            float(actual), expected, rel_tol=0.0, abs_tol=1e-9
        )
    return actual == expected


def read_parquet_schema(path: Path) -> list[tuple[str, str]]:
    parquet_literal = _duckdb_string_literal(str(path))
    conn = duckdb.connect(database=":memory:")
    try:
        return [
            (name, column_type)
            for name, column_type, *_rest in conn.execute(
                f"DESCRIBE SELECT * FROM read_parquet({parquet_literal})"
            ).fetchall()
        ]
    finally:
        conn.close()


def validate_parquet_schema(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"parquet file not found: {path}")

    actual_schema = read_parquet_schema(path)
    actual_columns = {name for name, _type in actual_schema}
    legacy_columns = sorted(actual_columns & LEGACY_GEOGRAPHY_COLUMNS)
    if legacy_columns:
        raise ValueError(f"legacy geography columns present: {legacy_columns}")

    expected_schema = list(PARQUET_EXPORT_SCHEMA)
    if actual_schema != expected_schema:
        raise ValueError(
            "parquet schema mismatch: "
            f"expected {_format_schema(expected_schema)}, "
            f"got {_format_schema(actual_schema)}"
        )


def _append_sql_check(
    conn: duckdb.DuckDBPyConnection,
    errors: list[str],
    where_sql: str,
    message: str,
    columns: Sequence[str] = ("event_id",),
) -> None:
    count = conn.execute(f"SELECT count(*) FROM events WHERE {where_sql}").fetchone()[0]
    if not count:
        return

    column_sql = ", ".join(_duckdb_identifier(column) for column in columns)
    examples = conn.execute(
        f"SELECT {column_sql} FROM events WHERE {where_sql} LIMIT {EXAMPLE_LIMIT}"
    ).fetchall()
    errors.append(f"{message}: {count} row(s), examples {_format_examples(columns, examples)}")


def _append_api_location_classification_checks(
    conn: duckdb.DuckDBPyConnection,
    reference: GeographyReference,
    errors: list[str],
) -> None:
    rows = conn.execute(
        """
        SELECT api_location_name, api_location_granularity, count(*) AS rows
        FROM events
        GROUP BY api_location_name, api_location_granularity
        ORDER BY rows DESC, api_location_name, api_location_granularity
        """
    ).fetchall()

    mismatches = []
    for api_location_name, exported_granularity, row_count in rows:
        expected_granularity = classify_location_name(api_location_name, reference).granularity
        if exported_granularity != expected_granularity:
            mismatches.append(
                f"api_location_name={api_location_name!r}: expected {expected_granularity!r}, "
                f"got {exported_granularity!r} ({row_count} row(s))"
            )

    if mismatches:
        errors.append("api_location_name/granularity mismatch: " + "; ".join(mismatches[:EXAMPLE_LIMIT]))


def _append_derived_reference_checks(
    conn: duckdb.DuckDBPyConnection,
    reference: GeographyReference,
    errors: list[str],
) -> None:
    county_mismatches = []
    county_rows = conn.execute(
        """
        SELECT derived_county_code, derived_county_name, count(*) AS rows
        FROM events
        WHERE derived_county_code IS NOT NULL
        GROUP BY derived_county_code, derived_county_name
        ORDER BY rows DESC, derived_county_code, derived_county_name
        """
    ).fetchall()
    for county_code, county_name, row_count in county_rows:
        county = reference.counties_by_code.get(county_code)
        if county is None:
            county_mismatches.append(
                f"derived_county_code={county_code!r} is not in reference ({row_count} row(s))"
            )
        elif county.county_name != county_name:
            county_mismatches.append(
                f"derived_county_code={county_code!r}: expected name {county.county_name!r}, "
                f"got {county_name!r} ({row_count} row(s))"
            )

    if county_mismatches:
        errors.append("derived county mismatch: " + "; ".join(county_mismatches[:EXAMPLE_LIMIT]))

    municipality_mismatches = []
    municipality_rows = conn.execute(
        """
        SELECT
            derived_municipality_code,
            derived_municipality_name,
            derived_county_code,
            derived_county_name,
            count(*) AS rows
        FROM events
        WHERE derived_municipality_code IS NOT NULL
        GROUP BY
            derived_municipality_code,
            derived_municipality_name,
            derived_county_code,
            derived_county_name
        ORDER BY rows DESC, derived_municipality_code, derived_municipality_name
        """
    ).fetchall()
    for municipality_code, municipality_name, county_code, county_name, row_count in municipality_rows:
        municipality = reference.municipalities_by_code.get(municipality_code)
        if municipality is None:
            municipality_mismatches.append(
                f"derived_municipality_code={municipality_code!r} is not in reference ({row_count} row(s))"
            )
            continue

        expected = (
            municipality.municipality_name,
            municipality.county_code,
            municipality.county_name,
        )
        actual = (municipality_name, county_code, county_name)
        if actual != expected:
            municipality_mismatches.append(
                f"derived_municipality_code={municipality_code!r}: expected "
                f"(name, county_code, county_name)={expected!r}, got {actual!r} ({row_count} row(s))"
            )

    if municipality_mismatches:
        errors.append(
            "derived municipality mismatch: " + "; ".join(municipality_mismatches[:EXAMPLE_LIMIT])
        )


def _append_gps_parse_checks(conn: duckdb.DuckDBPyConnection, errors: list[str]) -> None:
    rows = conn.execute(
        """
        SELECT
            api_location_gps,
            api_location_latitude,
            api_location_longitude,
            count(*) AS rows
        FROM events
        GROUP BY api_location_gps, api_location_latitude, api_location_longitude
        ORDER BY rows DESC, api_location_gps, api_location_latitude, api_location_longitude
        """
    ).fetchall()

    mismatches = []
    for gps, latitude, longitude, row_count in rows:
        expected = parse_gps(gps)
        if expected is None:
            if latitude is not None or longitude is not None:
                mismatches.append(
                    f"api_location_gps={gps!r}: expected null parsed coordinates, "
                    f"got ({latitude!r}, {longitude!r}) ({row_count} row(s))"
                )
            continue

        expected_latitude, expected_longitude = expected
        if not (_is_close(latitude, expected_latitude) and _is_close(longitude, expected_longitude)):
            mismatches.append(
                f"api_location_gps={gps!r}: expected parsed coordinates {expected!r}, "
                f"got ({latitude!r}, {longitude!r}) ({row_count} row(s))"
            )

    if mismatches:
        errors.append("api_location_gps parse mismatch: " + "; ".join(mismatches[:EXAMPLE_LIMIT]))


def _append_resolved_geography_contract_checks(
    conn: duckdb.DuckDBPyConnection,
    reference: GeographyReference,
    errors: list[str],
) -> None:
    """Compare exported geography fields with the deterministic resolver contract."""

    select_columns = ("event_id", "name", *GEOGRAPHY_EXPORT_FIELDS)
    column_sql = ", ".join(_duckdb_identifier(column) for column in select_columns)
    cursor = conn.execute(f"SELECT {column_sql} FROM events")

    mismatch_count = 0
    examples: list[str] = []
    while rows := cursor.fetchmany(10_000):
        for row in rows:
            exported = dict(zip(select_columns, row))
            expected = resolve_event_geography(
                {
                    "name": exported["name"],
                    "location": {
                        "name": exported["api_location_name"],
                        "gps": exported["api_location_gps"],
                    },
                },
                reference,
            )

            field_mismatches = []
            for field in GEOGRAPHY_EXPORT_FIELDS:
                actual_value = exported[field]
                expected_value = expected[field]
                if not _values_match(actual_value, expected_value):
                    field_mismatches.append(
                        f"{field} expected {expected_value!r} got {actual_value!r}"
                    )

            if not field_mismatches:
                continue

            mismatch_count += 1
            if len(examples) < EXAMPLE_LIMIT:
                examples.append(
                    f"event_id={exported['event_id']!r}, name={exported['name']!r}, "
                    f"api_location_name={exported['api_location_name']!r}, "
                    f"api_location_gps={exported['api_location_gps']!r}: "
                    + ", ".join(field_mismatches)
                )

    if mismatch_count:
        errors.append(
            "geography contract mismatch against resolve_event_geography(): "
            f"{mismatch_count} row(s), examples [" + "; ".join(examples) + "]"
        )


def validate_parquet_semantics(path: Path, reference: GeographyReference) -> None:
    """Validate documented v2 geography invariants and resolver output."""

    parquet_literal = _duckdb_string_literal(str(path))
    granularity_literals = ", ".join(
        _duckdb_string_literal(value) for value in sorted(ALLOWED_API_LOCATION_GRANULARITIES)
    )
    conn = duckdb.connect(database=":memory:")
    try:
        conn.execute(f"CREATE TEMP VIEW events AS SELECT * FROM read_parquet({parquet_literal})")
        errors: list[str] = []

        _append_sql_check(
            conn,
            errors,
            f"api_location_granularity IS NULL OR api_location_granularity NOT IN ({granularity_literals})",
            "invalid api_location_granularity",
            ("event_id", "api_location_name", "api_location_granularity"),
        )
        _append_sql_check(
            conn,
            errors,
            "derived_municipality_code IS NOT NULL "
            "AND NOT regexp_full_match(derived_municipality_code, '^[0-9]{4}$')",
            "invalid derived_municipality_code",
            ("event_id", "derived_municipality_code", "derived_municipality_name"),
        )
        _append_sql_check(
            conn,
            errors,
            "derived_county_code IS NOT NULL "
            "AND NOT regexp_full_match(derived_county_code, '^[0-9]{2}$')",
            "invalid derived_county_code",
            ("event_id", "derived_county_code", "derived_county_name"),
        )
        _append_sql_check(
            conn,
            errors,
            "(derived_municipality_code IS NULL AND derived_municipality_name IS NOT NULL) "
            "OR (derived_municipality_code IS NOT NULL AND derived_municipality_name IS NULL)",
            "derived_municipality code/name null pairing mismatch",
            ("event_id", "derived_municipality_code", "derived_municipality_name"),
        )
        _append_sql_check(
            conn,
            errors,
            "(derived_county_code IS NULL AND derived_county_name IS NOT NULL) "
            "OR (derived_county_code IS NOT NULL AND derived_county_name IS NULL)",
            "derived_county code/name null pairing mismatch",
            ("event_id", "derived_county_code", "derived_county_name"),
        )
        _append_sql_check(
            conn,
            errors,
            "derived_municipality_code IS NOT NULL AND derived_county_code IS NULL",
            "derived municipality missing derived county",
            ("event_id", "derived_municipality_code", "derived_county_code"),
        )
        _append_sql_check(
            conn,
            errors,
            "(api_location_latitude IS NULL AND api_location_longitude IS NOT NULL) "
            "OR (api_location_latitude IS NOT NULL AND api_location_longitude IS NULL)",
            "api_location latitude/longitude null pairing mismatch",
            ("event_id", "api_location_gps", "api_location_latitude", "api_location_longitude"),
        )
        _append_sql_check(
            conn,
            errors,
            f"api_location_latitude IS NOT NULL AND "
            f"(api_location_latitude < {SWEDEN_LATITUDE_BOUNDS[0]} "
            f"OR api_location_latitude > {SWEDEN_LATITUDE_BOUNDS[1]})",
            "api_location_latitude outside Sweden bounds",
            ("event_id", "api_location_gps", "api_location_latitude"),
        )
        _append_sql_check(
            conn,
            errors,
            f"api_location_longitude IS NOT NULL AND "
            f"(api_location_longitude < {SWEDEN_LONGITUDE_BOUNDS[0]} "
            f"OR api_location_longitude > {SWEDEN_LONGITUDE_BOUNDS[1]})",
            "api_location_longitude outside Sweden bounds",
            ("event_id", "api_location_gps", "api_location_longitude"),
        )

        _append_api_location_classification_checks(conn, reference, errors)
        _append_derived_reference_checks(conn, reference, errors)
        _append_gps_parse_checks(conn, errors)
        _append_resolved_geography_contract_checks(conn, reference, errors)

        if errors:
            raise ValueError("parquet semantic validation failed: " + "; ".join(errors))
    finally:
        conn.close()


def validate_reference() -> GeographyReference:
    reference = load_geography_reference()
    validate_geography_reference(reference)
    return reference


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path, help="generated events.parquet to validate")
    args = parser.parse_args(argv)

    try:
        reference = validate_reference()
        validate_parquet_schema(args.parquet)
        validate_parquet_semantics(args.parquet, reference)
    except Exception as exc:  # noqa: BLE001 - CLI should print concise validation failures.
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"OK: reference identity, v2 parquet schema, and geography contract validated for {args.parquet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
