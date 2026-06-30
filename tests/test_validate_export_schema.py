from pathlib import Path

import duckdb
import pytest

from export_schema import PARQUET_EXPORT_COLUMNS, PARQUET_EXPORT_SCHEMA
from geography import load_geography_reference
from scripts.validate_export_schema import validate_parquet_schema, validate_parquet_semantics


@pytest.fixture(scope="module")
def reference():
    return load_geography_reference()


def test_public_parquet_schema_literal_snapshot():
    assert PARQUET_EXPORT_SCHEMA == (
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
    )


def valid_row(**updates):
    row = {
        "event_id": "1",
        "datetime": "2026-06-30 10:57:12 +02:00",
        "name": "30 juni 10.36, Trafikolycka, Linköping",
        "summary": "Test row",
        "url": "/aktuellt/handelser/test/",
        "type": "Trafikolycka",
        "api_location_name": "Östergötlands län",
        "api_location_gps": "58.410807,15.621373",
        "api_location_granularity": "county",
        "api_location_latitude": 58.410807,
        "api_location_longitude": 15.621373,
        "derived_municipality_code": "0580",
        "derived_municipality_name": "Linköping",
        "derived_county_code": "05",
        "derived_county_name": "Östergötlands län",
        "html_title": None,
        "html_preamble": None,
        "html_body": None,
        "html_published_datetime": None,
        "html_author": None,
        "html_available": None,
    }
    row.update(updates)
    return row


def write_parquet(path: Path, rows: list[dict]) -> None:
    normalized_rows = [{column: row.get(column) for column in PARQUET_EXPORT_COLUMNS} for row in rows]
    table_schema_sql = ",\n".join(
        f'    "{column}" {duckdb_type}' for column, duckdb_type in PARQUET_EXPORT_SCHEMA
    )
    projection_sql = ",\n".join(
        f"    CAST(struct_extract(x, '{column}') AS {duckdb_type}) AS \"{column}\""
        for column, duckdb_type in PARQUET_EXPORT_SCHEMA
    )

    conn = duckdb.connect(database=":memory:")
    try:
        conn.execute(f"CREATE TEMP TABLE export_events (\n{table_schema_sql}\n)")
        conn.execute(
            f"""
            INSERT INTO export_events
            SELECT
{projection_sql}
            FROM (SELECT unnest($events) AS x)
            """,
            {"events": normalized_rows},
        )
        conn.table("export_events").to_parquet(str(path), compression="zstd")
    finally:
        conn.close()


def test_validator_accepts_valid_v2_semantic_row(tmp_path, reference):
    parquet_path = tmp_path / "valid.parquet"
    write_parquet(parquet_path, [valid_row()])

    validate_parquet_schema(parquet_path)
    validate_parquet_semantics(parquet_path, reference)


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"api_location_granularity": "municipality"}, "api_location_name/granularity mismatch"),
        ({"derived_municipality_code": "580"}, "invalid derived_municipality_code"),
        ({"derived_municipality_name": None}, "derived_municipality code/name null pairing mismatch"),
        ({"derived_county_name": "Stockholms län"}, "derived county mismatch"),
        ({"api_location_latitude": 99.0}, "api_location_latitude outside Sweden bounds"),
        ({"api_location_longitude": None}, "api_location latitude/longitude null pairing mismatch"),
        ({"api_location_gps": "not gps"}, "api_location_gps parse mismatch"),
    ],
)
def test_validator_rejects_v2_semantic_invariant_failures(tmp_path, reference, updates, match):
    parquet_path = tmp_path / "invalid.parquet"
    write_parquet(parquet_path, [valid_row(**updates)])

    validate_parquet_schema(parquet_path)
    with pytest.raises(ValueError, match=match):
        validate_parquet_semantics(parquet_path, reference)


def test_validator_rejects_cross_county_conflict_with_non_null_derived_fields(tmp_path, reference):
    parquet_path = tmp_path / "cross_county_conflict.parquet"
    write_parquet(
        parquet_path,
        [
            valid_row(
                api_location_name="Stockholms län",
                api_location_granularity="county",
                derived_municipality_code="0580",
                derived_municipality_name="Linköping",
                derived_county_code="05",
                derived_county_name="Östergötlands län",
            )
        ],
    )

    validate_parquet_schema(parquet_path)
    with pytest.raises(ValueError, match="geography contract mismatch"):
        validate_parquet_semantics(parquet_path, reference)


def test_validator_rejects_legacy_geography_columns(tmp_path):
    parquet_path = tmp_path / "legacy.parquet"
    conn = duckdb.connect(database=":memory:")
    try:
        conn.execute(
            """
            CREATE TEMP TABLE legacy_events AS
            SELECT
                '1'::VARCHAR AS event_id,
                'Linköping'::VARCHAR AS location_name,
                58.410807::DOUBLE AS latitude,
                15.621373::DOUBLE AS longitude
            """
        )
        conn.table("legacy_events").to_parquet(str(parquet_path), compression="zstd")
    finally:
        conn.close()

    with pytest.raises(ValueError, match="legacy geography columns present"):
        validate_parquet_schema(parquet_path)
