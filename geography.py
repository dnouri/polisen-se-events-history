"""Deterministic Swedish administrative geography enrichment.

The production exporter should use only the vendored reference file in
``reference/swedish_admin_areas.csv``.  Matching is deliberately conservative:
exact official names after whitespace normalization and Unicode case-folding;
no aliases, fuzzy spelling, geocoding, or narrative/body parsing.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

LocationGranularity = Literal["municipality", "county", "unknown"]

DEFAULT_REFERENCE_PATH = Path(__file__).resolve().parent / "reference" / "swedish_admin_areas.csv"
REFERENCE_COLUMNS = ("municipality_code", "municipality_name", "county_code", "county_name")
EXPECTED_MUNICIPALITY_COUNT = 290
EXPECTED_COUNTY_COUNT = 21
EXPECTED_COUNTY_CODES = frozenset(
    {
        "01",
        "03",
        "04",
        "05",
        "06",
        "07",
        "08",
        "09",
        "10",
        "12",
        "13",
        "14",
        "17",
        "18",
        "19",
        "20",
        "21",
        "22",
        "23",
        "24",
        "25",
    }
)
GEOGRAPHY_EXPORT_FIELDS = (
    "api_location_name",
    "api_location_gps",
    "api_location_granularity",
    "api_location_latitude",
    "api_location_longitude",
    "derived_municipality_code",
    "derived_municipality_name",
    "derived_county_code",
    "derived_county_name",
)
SWEDEN_LATITUDE_BOUNDS = (55.0, 70.0)
SWEDEN_LONGITUDE_BOUNDS = (10.0, 25.0)


@dataclass(frozen=True)
class AdminArea:
    municipality_code: str
    municipality_name: str
    county_code: str
    county_name: str


@dataclass(frozen=True)
class County:
    county_code: str
    county_name: str


@dataclass(frozen=True)
class GeographyReference:
    municipalities: tuple[AdminArea, ...]
    counties: tuple[County, ...]
    municipalities_by_code: Mapping[str, AdminArea]
    municipalities_by_normalized_name: Mapping[str, AdminArea]
    counties_by_code: Mapping[str, County]
    counties_by_normalized_name: Mapping[str, County]


@dataclass(frozen=True)
class LocationClassification:
    granularity: LocationGranularity
    input_name: str | None
    normalized_name: str
    municipality_code: str | None = None
    municipality_name: str | None = None
    county_code: str | None = None
    county_name: str | None = None

    @property
    def code(self) -> str | None:
        if self.granularity == "municipality":
            return self.municipality_code
        if self.granularity == "county":
            return self.county_code
        return None

    @property
    def name(self) -> str | None:
        if self.granularity == "municipality":
            return self.municipality_name
        if self.granularity == "county":
            return self.county_name
        return None


def normalize_name(name: str | None) -> str:
    """Normalize only enough for exact case-insensitive reference matching."""

    return " ".join((name or "").strip().casefold().split())


def validate_geography_reference(reference: GeographyReference, source: str | Path | None = None) -> None:
    """Reject incomplete Swedish administrative references at runtime."""

    source_label = f" in {source}" if source is not None else ""
    errors: list[str] = []

    if len(reference.municipalities) != EXPECTED_MUNICIPALITY_COUNT:
        errors.append(
            f"expected {EXPECTED_MUNICIPALITY_COUNT} municipalities, got {len(reference.municipalities)}"
        )

    if len(reference.counties) != EXPECTED_COUNTY_COUNT:
        errors.append(f"expected {EXPECTED_COUNTY_COUNT} counties, got {len(reference.counties)}")

    county_codes = set(reference.counties_by_code)
    if county_codes != EXPECTED_COUNTY_CODES:
        errors.append(
            "expected county code set "
            f"{sorted(EXPECTED_COUNTY_CODES)}, missing {sorted(EXPECTED_COUNTY_CODES - county_codes)}, "
            f"unexpected {sorted(county_codes - EXPECTED_COUNTY_CODES)}"
        )

    municipality_county_codes = {area.county_code for area in reference.municipalities}
    if municipality_county_codes != EXPECTED_COUNTY_CODES:
        errors.append(
            "municipality rows must cover county code set "
            f"{sorted(EXPECTED_COUNTY_CODES)}, missing {sorted(EXPECTED_COUNTY_CODES - municipality_county_codes)}, "
            f"unexpected {sorted(municipality_county_codes - EXPECTED_COUNTY_CODES)}"
        )

    if errors:
        raise ValueError(f"incomplete geography reference{source_label}: " + "; ".join(errors))


def load_geography_reference(path: str | Path = DEFAULT_REFERENCE_PATH) -> GeographyReference:
    """Load and validate the vendored Swedish municipality/county reference."""

    reference_path = Path(path)
    with reference_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = tuple(reader.fieldnames or ())
        missing_columns = [column for column in REFERENCE_COLUMNS if column not in fieldnames]
        if missing_columns:
            raise ValueError(f"missing reference columns in {reference_path}: {missing_columns}")

        rows = [
            {
                "municipality_code": (row.get("municipality_code") or "").strip(),
                "municipality_name": (row.get("municipality_name") or "").strip(),
                "county_code": (row.get("county_code") or "").strip(),
                "county_name": (row.get("county_name") or "").strip(),
            }
            for row in reader
        ]

    municipalities: list[AdminArea] = []
    for row_number, row in enumerate(rows, start=2):
        municipality_code = row["municipality_code"]
        municipality_name = row["municipality_name"]
        county_code = row["county_code"]
        county_name = row["county_name"]

        if not re.fullmatch(r"\d{4}", municipality_code):
            raise ValueError(f"invalid municipality_code on row {row_number}: {municipality_code!r}")
        if not re.fullmatch(r"\d{2}", county_code):
            raise ValueError(f"invalid county_code on row {row_number}: {county_code!r}")
        if municipality_code[:2] != county_code:
            raise ValueError(
                f"municipality/county code mismatch on row {row_number}: "
                f"{municipality_code!r} not in {county_code!r}"
            )
        if not municipality_name:
            raise ValueError(f"missing municipality_name on row {row_number}")
        if not county_name:
            raise ValueError(f"missing county_name on row {row_number}")

        municipalities.append(
            AdminArea(
                municipality_code=municipality_code,
                municipality_name=municipality_name,
                county_code=county_code,
                county_name=county_name,
            )
        )

    municipalities_by_code: dict[str, AdminArea] = {}
    municipalities_by_normalized_name: dict[str, AdminArea] = {}
    counties_by_code: dict[str, County] = {}
    counties_by_normalized_name: dict[str, County] = {}

    for municipality in municipalities:
        existing_municipality = municipalities_by_code.get(municipality.municipality_code)
        if existing_municipality is not None:
            raise ValueError(f"duplicate municipality_code: {municipality.municipality_code!r}")
        municipalities_by_code[municipality.municipality_code] = municipality

        normalized_municipality = normalize_name(municipality.municipality_name)
        existing_by_name = municipalities_by_normalized_name.get(normalized_municipality)
        if existing_by_name is not None:
            raise ValueError(
                "duplicate normalized municipality_name: "
                f"{municipality.municipality_name!r} conflicts with {existing_by_name.municipality_name!r}"
            )
        municipalities_by_normalized_name[normalized_municipality] = municipality

        county = County(municipality.county_code, municipality.county_name)
        existing_county = counties_by_code.get(county.county_code)
        if existing_county is not None and existing_county.county_name != county.county_name:
            raise ValueError(
                f"conflicting county_name for county_code {county.county_code!r}: "
                f"{existing_county.county_name!r} vs {county.county_name!r}"
            )
        counties_by_code[county.county_code] = county

    for county in counties_by_code.values():
        normalized_county = normalize_name(county.county_name)
        existing_county = counties_by_normalized_name.get(normalized_county)
        if existing_county is not None:
            raise ValueError(
                "duplicate normalized county_name: "
                f"{county.county_name!r} conflicts with {existing_county.county_name!r}"
            )
        counties_by_normalized_name[normalized_county] = county

    collisions = sorted(set(municipalities_by_normalized_name) & set(counties_by_normalized_name))
    if collisions:
        raise ValueError(f"municipality/county normalized-name collisions: {collisions}")

    reference = GeographyReference(
        municipalities=tuple(sorted(municipalities, key=lambda area: area.municipality_code)),
        counties=tuple(sorted(counties_by_code.values(), key=lambda county: county.county_code)),
        municipalities_by_code=municipalities_by_code,
        municipalities_by_normalized_name=municipalities_by_normalized_name,
        counties_by_code=counties_by_code,
        counties_by_normalized_name=counties_by_normalized_name,
    )
    validate_geography_reference(reference, reference_path)
    return reference


def classify_location_name(name: str | None, reference: GeographyReference) -> LocationClassification:
    """Classify an API/title location name against exact official references."""

    normalized = normalize_name(name)
    if not normalized:
        return LocationClassification("unknown", name, normalized)

    municipality = reference.municipalities_by_normalized_name.get(normalized)
    if municipality is not None:
        return LocationClassification(
            granularity="municipality",
            input_name=name,
            normalized_name=normalized,
            municipality_code=municipality.municipality_code,
            municipality_name=municipality.municipality_name,
            county_code=municipality.county_code,
            county_name=municipality.county_name,
        )

    county = reference.counties_by_normalized_name.get(normalized)
    if county is not None:
        return LocationClassification(
            granularity="county",
            input_name=name,
            normalized_name=normalized,
            county_code=county.county_code,
            county_name=county.county_name,
        )

    return LocationClassification("unknown", name, normalized)


def parse_title_suffix(title: str | None) -> str | None:
    """Return the stripped suffix after the final comma, or None without a comma."""

    if title is None:
        return None
    text = str(title)
    if "," not in text:
        return None
    return text.rsplit(",", 1)[1].strip()


def parse_gps(gps: str | None) -> tuple[float, float] | None:
    """Parse a Police API ``location.gps`` string if it is within broad Sweden bounds."""

    if gps is None:
        return None
    text = str(gps).strip()
    if not text or "," not in text:
        return None

    parts = text.split(",")
    if len(parts) != 2:
        return None

    try:
        latitude = float(parts[0].strip())
        longitude = float(parts[1].strip())
    except ValueError:
        return None

    if not (SWEDEN_LATITUDE_BOUNDS[0] <= latitude <= SWEDEN_LATITUDE_BOUNDS[1]):
        return None
    if not (SWEDEN_LONGITUDE_BOUNDS[0] <= longitude <= SWEDEN_LONGITUDE_BOUNDS[1]):
        return None

    return latitude, longitude


def resolve_event_geography(event: Mapping[str, Any], reference: GeographyReference) -> dict[str, Any]:
    """Resolve v2 geography export fields for one raw Police API event."""

    location = event.get("location")
    location_mapping = location if isinstance(location, Mapping) else {}
    api_location_name = _optional_string(location_mapping.get("name") if "name" in location_mapping else None)
    api_location_gps = _optional_string(location_mapping.get("gps") if "gps" in location_mapping else None)

    api_location = classify_location_name(api_location_name, reference)
    title_suffix = parse_title_suffix(_optional_string(event.get("name") if "name" in event else None))
    title_location = classify_location_name(title_suffix, reference)
    coordinates = parse_gps(api_location_gps)

    derived_municipality_code: str | None = None
    derived_municipality_name: str | None = None
    derived_county_code: str | None = None
    derived_county_name: str | None = None

    if api_location.granularity == "municipality":
        if title_location.granularity == "municipality" and title_location.municipality_code != api_location.municipality_code:
            if title_location.county_code == api_location.county_code:
                # Two municipality signals disagree, but they agree on county.
                derived_county_code = api_location.county_code
                derived_county_name = api_location.county_name
            # Cross-county municipality conflicts remain fully unresolved.
        elif title_location.granularity == "county" and title_location.county_code != api_location.county_code:
            # Municipality signal and title county contradict each other.
            pass
        else:
            derived_municipality_code = api_location.municipality_code
            derived_municipality_name = api_location.municipality_name
            derived_county_code = api_location.county_code
            derived_county_name = api_location.county_name

    elif api_location.granularity == "county":
        derived_county_code = api_location.county_code
        derived_county_name = api_location.county_name
        if title_location.granularity == "municipality":
            if title_location.county_code == api_location.county_code:
                derived_municipality_code = title_location.municipality_code
                derived_municipality_name = title_location.municipality_name
                derived_county_code = title_location.county_code
                derived_county_name = title_location.county_name
            else:
                derived_county_code = None
                derived_county_name = None
        elif title_location.granularity == "county" and title_location.county_code != api_location.county_code:
            derived_county_code = None
            derived_county_name = None

    else:
        if title_location.granularity == "municipality":
            derived_municipality_code = title_location.municipality_code
            derived_municipality_name = title_location.municipality_name
            derived_county_code = title_location.county_code
            derived_county_name = title_location.county_name
        elif title_location.granularity == "county":
            derived_county_code = title_location.county_code
            derived_county_name = title_location.county_name

    return {
        "api_location_name": api_location_name,
        "api_location_gps": api_location_gps,
        "api_location_granularity": api_location.granularity,
        "api_location_latitude": coordinates[0] if coordinates is not None else None,
        "api_location_longitude": coordinates[1] if coordinates is not None else None,
        "derived_municipality_code": derived_municipality_code,
        "derived_municipality_name": derived_municipality_name,
        "derived_county_code": derived_county_code,
        "derived_county_name": derived_county_name,
    }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
