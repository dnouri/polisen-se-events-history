import importlib.util
from pathlib import Path

import duckdb
import pytest

from geography import GEOGRAPHY_EXPORT_FIELDS, load_geography_reference


BASE_EXPORT_FIELDS = ["event_id", "datetime", "name", "summary", "url", "type"]
HTML_EXPORT_FIELDS = [
    "html_title",
    "html_preamble",
    "html_body",
    "html_published_datetime",
    "html_author",
    "html_available",
]
LEGACY_GEOGRAPHY_FIELDS = {"location_name", "latitude", "longitude"}


@pytest.fixture(scope="module")
def exporter():
    module_path = Path(__file__).resolve().parents[1] / "export-events.py"
    spec = importlib.util.spec_from_file_location("export_events_py", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reference():
    return load_geography_reference()


def test_parse_event_datetime_accepts_non_zero_padded_hour(exporter):
    parsed = exporter.parse_event_datetime("2026-06-30 7:32:17 +02:00")

    assert parsed.hour == 7
    assert parsed.tzinfo is not None


def test_flatten_event_emits_v2_schema_and_preserves_base_and_html(exporter, reference):
    event = {
        "id": 645690,
        "datetime": "2026-06-30 10:57:12 +02:00",
        "name": "30 juni 10.36, Trafikolycka, Mariestad",
        "summary": "Larm inkommer via SOS om en singelolycka på E20.",
        "url": "/aktuellt/handelser/2026/juni/30/30-juni-10.36-trafikolycka-mariestad/",
        "type": "Trafikolycka",
        "location": {"name": "Västra Götalands län", "gps": "58.252793,13.059643"},
        "html_title": "30 juni 10.36, Trafikolycka, Mariestad",
        "html_preamble": "Larm inkommer via SOS om en singelolycka på E20.",
        "html_body": "Föraren kontrolleras av ambulanspersonal.",
        "html_published_datetime": "2026-06-30T10:57:12+02:00",
        "html_author": "Polisen",
        "html_available": True,
    }

    row = exporter.flatten_event_for_export(event, reference)

    assert list(row) == BASE_EXPORT_FIELDS + list(GEOGRAPHY_EXPORT_FIELDS) + HTML_EXPORT_FIELDS
    assert set(GEOGRAPHY_EXPORT_FIELDS).issubset(row)
    assert LEGACY_GEOGRAPHY_FIELDS.isdisjoint(row)

    assert row["event_id"] == "645690"
    assert row["datetime"] == event["datetime"]
    assert row["name"] == event["name"]
    assert row["summary"] == event["summary"]
    assert row["url"] == event["url"]
    assert row["type"] == event["type"]

    assert row["api_location_name"] == "Västra Götalands län"
    assert row["api_location_gps"] == "58.252793,13.059643"
    assert row["api_location_granularity"] == "county"
    assert row["api_location_latitude"] == 58.252793
    assert row["api_location_longitude"] == 13.059643
    assert row["derived_municipality_code"] == "1493"
    assert row["derived_municipality_name"] == "Mariestad"
    assert row["derived_county_code"] == "14"
    assert row["derived_county_name"] == "Västra Götalands län"

    assert row["html_title"] == event["html_title"]
    assert row["html_preamble"] == event["html_preamble"]
    assert row["html_body"] == event["html_body"]
    assert row["html_published_datetime"] == event["html_published_datetime"]
    assert row["html_author"] == event["html_author"]
    assert row["html_available"] is True


@pytest.mark.parametrize(
    ("location", "expected_raw_gps"),
    [
        ({"name": "Östergötlands län", "gps": "not gps"}, "not gps"),
        ({"name": "Östergötlands län"}, None),
    ],
)
def test_flatten_event_invalid_or_missing_gps_emits_null_parsed_coords(
    exporter,
    reference,
    location,
    expected_raw_gps,
):
    event = {
        "id": 1,
        "datetime": "2026-06-30 10:57:12 +02:00",
        "name": "30 juni 10.36, Trafikolycka, Linköping",
        "summary": "",
        "url": "/aktuellt/handelser/test/",
        "type": "Trafikolycka",
        "location": location,
    }

    row = exporter.flatten_event_for_export(event, reference)

    assert row["api_location_name"] == "Östergötlands län"
    assert row["api_location_gps"] == expected_raw_gps
    assert row["api_location_latitude"] is None
    assert row["api_location_longitude"] is None
    assert row["derived_municipality_code"] == "0580"
    assert row["derived_municipality_name"] == "Linköping"
    assert row["derived_county_code"] == "05"
    assert row["derived_county_name"] == "Östergötlands län"
    assert LEGACY_GEOGRAPHY_FIELDS.isdisjoint(row)


def test_export_to_parquet_preserves_literal_schema_for_sparse_all_null_derived_municipality(
    exporter,
    reference,
    tmp_path,
):
    event = {
        "id": 2,
        "datetime": "2026-06-30 07:32:17 +02:00",
        "name": "30 juni 07.32, Sammanfattning natt, Östergötlands län",
        "summary": "Nattens händelser i länet.",
        "url": "/aktuellt/handelser/test-sparse/",
        "type": "Sammanfattning natt",
        "location": {"name": "Östergötlands län", "gps": "58.410807,15.621373"},
    }

    row = exporter.flatten_event_for_export(event, reference)
    assert row["derived_municipality_code"] is None
    assert row["derived_municipality_name"] is None

    output_path = tmp_path / "sparse.parquet"
    exporter.export_to_parquet([row], output_path)

    escaped_path = str(output_path).replace("'", "''")
    conn = duckdb.connect(database=":memory:")
    try:
        actual_schema = [
            (name, column_type)
            for name, column_type, *_rest in conn.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{escaped_path}')"
            ).fetchall()
        ]
        sparse_values = conn.execute(
            f"""
            SELECT derived_municipality_code, derived_municipality_name, html_available
            FROM read_parquet('{escaped_path}')
            """
        ).fetchone()
    finally:
        conn.close()

    assert actual_schema == [
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
    assert LEGACY_GEOGRAPHY_FIELDS.isdisjoint({name for name, _type in actual_schema})
    assert sparse_values == (None, None, None)
