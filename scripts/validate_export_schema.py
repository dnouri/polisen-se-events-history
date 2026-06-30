#!/usr/bin/env python3
"""Validate the minimal v2 release contract for a generated Parquet export."""

# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "duckdb>=1.0.0",
# ]
# ///

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from geography import load_geography_reference, validate_geography_reference  # noqa: E402

EXPECTED_PARQUET_SCHEMA = [
    ("event_id", "VARCHAR"),
    ("datetime", "VARCHAR"),
    ("name", "VARCHAR"),
    ("summary", "VARCHAR"),
    ("url", "VARCHAR"),
    ("type", "VARCHAR"),
    ("api_location_name", "VARCHAR"),
    ("api_location_gps", "VARCHAR"),
    ("api_location_granularity", "VARCHAR"),
    ("api_location_latitude", "DOUBLE"),
    ("api_location_longitude", "DOUBLE"),
    ("derived_municipality_code", "VARCHAR"),
    ("derived_municipality_name", "VARCHAR"),
    ("derived_county_code", "VARCHAR"),
    ("derived_county_name", "VARCHAR"),
    ("html_title", "VARCHAR"),
    ("html_preamble", "VARCHAR"),
    ("html_body", "VARCHAR"),
    ("html_published_datetime", "VARCHAR"),
    ("html_author", "VARCHAR"),
    ("html_available", "BOOLEAN"),
]
LEGACY_GEOGRAPHY_COLUMNS = {"location_name", "latitude", "longitude"}


def _duckdb_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _format_schema(schema: list[tuple[str, str]]) -> str:
    return "[" + ", ".join(f"{name}:{column_type}" for name, column_type in schema) + "]"


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

    if actual_schema != EXPECTED_PARQUET_SCHEMA:
        raise ValueError(
            "parquet schema mismatch: "
            f"expected {_format_schema(EXPECTED_PARQUET_SCHEMA)}, "
            f"got {_format_schema(actual_schema)}"
        )


def validate_reference() -> None:
    reference = load_geography_reference()
    validate_geography_reference(reference)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path, help="generated events.parquet to validate")
    args = parser.parse_args(argv)

    try:
        validate_reference()
        validate_parquet_schema(args.parquet)
    except Exception as exc:  # noqa: BLE001 - CLI should print concise validation failures.
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"OK: reference completeness and v2 parquet schema validated for {args.parquet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
