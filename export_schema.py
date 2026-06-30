"""Shared v2 export schema constants.

Keep the Parquet column order/types in one importable module so the exporter,
validator, and tests do not drift.
"""

from __future__ import annotations

from geography import GEOGRAPHY_EXPORT_FIELDS

BASE_EXPORT_FIELDS = ("event_id", "datetime", "name", "summary", "url", "type")
HTML_EXPORT_FIELDS = (
    "html_title",
    "html_preamble",
    "html_body",
    "html_published_datetime",
    "html_author",
    "html_available",
)
LEGACY_GEOGRAPHY_COLUMNS = frozenset({"location_name", "latitude", "longitude"})

_FIELD_TYPES = {
    "event_id": "VARCHAR",
    "datetime": "VARCHAR",
    "name": "VARCHAR",
    "summary": "VARCHAR",
    "url": "VARCHAR",
    "type": "VARCHAR",
    "api_location_name": "VARCHAR",
    "api_location_gps": "VARCHAR",
    "api_location_granularity": "VARCHAR",
    "api_location_latitude": "DOUBLE",
    "api_location_longitude": "DOUBLE",
    "derived_municipality_code": "VARCHAR",
    "derived_municipality_name": "VARCHAR",
    "derived_county_code": "VARCHAR",
    "derived_county_name": "VARCHAR",
    "html_title": "VARCHAR",
    "html_preamble": "VARCHAR",
    "html_body": "VARCHAR",
    "html_published_datetime": "VARCHAR",
    "html_author": "VARCHAR",
    "html_available": "BOOLEAN",
}

PARQUET_EXPORT_COLUMNS = BASE_EXPORT_FIELDS + GEOGRAPHY_EXPORT_FIELDS + HTML_EXPORT_FIELDS
PARQUET_EXPORT_SCHEMA = tuple((column, _FIELD_TYPES[column]) for column in PARQUET_EXPORT_COLUMNS)
