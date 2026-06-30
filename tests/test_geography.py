import re
from pathlib import Path

import pytest

from geography import (
    EXPECTED_COUNTY_CODE_TO_NAME,
    EXPECTED_MUNICIPALITY_CODES,
    EXPECTED_REFERENCE_SHA256,
    GEOGRAPHY_EXPORT_FIELDS,
    classify_location_name,
    load_geography_reference,
    normalize_name,
    parse_gps,
    parse_title_suffix,
    reference_file_sha256,
    resolve_event_geography,
)


@pytest.fixture(scope="module")
def reference():
    return load_geography_reference()


def make_event(api_location_name, title, gps="58.410807,15.621373", summary=""):
    return {
        "id": 1,
        "name": title,
        "summary": summary,
        "location": {"name": api_location_name, "gps": gps},
    }


def assert_only_v2_geography_fields(result):
    assert tuple(result) == GEOGRAPHY_EXPORT_FIELDS
    assert "location_name" not in result
    assert "latitude" not in result
    assert "longitude" not in result


def test_reference_completeness(reference):
    reference_path = Path(__file__).resolve().parents[1] / "reference" / "swedish_admin_areas.csv"
    assert reference_file_sha256(reference_path) == EXPECTED_REFERENCE_SHA256

    assert len(reference.municipalities) == 290
    assert len(reference.counties) == 21
    assert {county.county_code: county.county_name for county in reference.counties} == dict(
        EXPECTED_COUNTY_CODE_TO_NAME
    )
    assert {area.municipality_code for area in reference.municipalities} == EXPECTED_MUNICIPALITY_CODES

    assert all(re.fullmatch(r"\d{4}", area.municipality_code) for area in reference.municipalities)
    assert all(re.fullmatch(r"\d{2}", area.county_code) for area in reference.municipalities)
    assert all(area.municipality_code.startswith(area.county_code) for area in reference.municipalities)

    gotland = reference.municipalities_by_code["0980"]
    assert gotland.municipality_name == "Gotland"
    assert gotland.county_code == "09"
    assert gotland.county_name == "Gotlands län"

    municipality_names = [normalize_name(area.municipality_name) for area in reference.municipalities]
    county_names = [normalize_name(county.county_name) for county in reference.counties]
    assert len(municipality_names) == len(set(municipality_names))
    assert len(county_names) == len(set(county_names))
    assert not (set(municipality_names) & set(county_names))


def test_loader_rejects_truncated_internally_valid_reference(tmp_path):
    source = Path(__file__).resolve().parents[1] / "reference" / "swedish_admin_areas.csv"
    truncated = tmp_path / "truncated_admin_areas.csv"
    truncated.write_text("\n".join(source.read_text(encoding="utf-8").splitlines()[:11]) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expected 290 municipalities"):
        load_geography_reference(truncated)


def test_loader_rejects_wrong_county_name_even_when_codes_and_counts_are_valid(tmp_path):
    source = Path(__file__).resolve().parents[1] / "reference" / "swedish_admin_areas.csv"
    mutated = tmp_path / "wrong_county_name.csv"
    mutated.write_text(
        source.read_text(encoding="utf-8").replace(
            ",05,Östergötlands län",
            ",05,Östergötland län",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="county code/name mapping mismatch"):
        load_geography_reference(mutated)


def test_loader_rejects_wrong_municipality_code_even_when_shape_and_counts_are_valid(tmp_path):
    source = Path(__file__).resolve().parents[1] / "reference" / "swedish_admin_areas.csv"
    mutated = tmp_path / "wrong_municipality_code.csv"
    mutated.write_text(
        source.read_text(encoding="utf-8").replace(
            "0114,Upplands Väsby,01,Stockholms län",
            "0116,Upplands Väsby,01,Stockholms län",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected municipality code set"):
        load_geography_reference(mutated)


def test_classify_location_name_exact_reference_matches(reference):
    municipality = classify_location_name("Linköping", reference)
    assert municipality.granularity == "municipality"
    assert municipality.municipality_code == "0580"
    assert municipality.county_code == "05"
    assert municipality.municipality_name == "Linköping"

    county = classify_location_name("Östergötlands län", reference)
    assert county.granularity == "county"
    assert county.county_code == "05"
    assert county.county_name == "Östergötlands län"

    unknown = classify_location_name("Gamla stan", reference)
    assert unknown.granularity == "unknown"
    assert unknown.code is None


def test_classify_location_name_normalizes_swedish_chars_case_and_whitespace(reference):
    municipality = classify_location_name("  öReBRO  ", reference)
    assert municipality.granularity == "municipality"
    assert municipality.municipality_code == "1880"

    county = classify_location_name("  väStra   GÖtalands  LÄN ", reference)
    assert county.granularity == "county"
    assert county.county_code == "14"


def test_parse_gps_valid_missing_invalid_and_out_of_bounds():
    assert parse_gps("58.410807,15.621373") == (58.410807, 15.621373)
    assert parse_gps(" 58.410807 , 15.621373 ") == (58.410807, 15.621373)

    assert parse_gps(None) is None
    assert parse_gps("") is None
    assert parse_gps("not gps") is None
    assert parse_gps("58.410807;15.621373") is None
    assert parse_gps("58.410807,15.621373,0") is None
    assert parse_gps("58.410807,not-a-number") is None

    assert parse_gps("54.999,15.0") is None
    assert parse_gps("70.001,15.0") is None
    assert parse_gps("58.0,9.999") is None
    assert parse_gps("58.0,25.001") is None


def test_parse_title_suffix_final_comma_no_comma_and_empty_suffix():
    assert parse_title_suffix("30 juni 10.36, Trafikolycka, Mariestad") == "Mariestad"
    assert parse_title_suffix("A, B, C") == "C"
    assert parse_title_suffix("No comma") is None
    assert parse_title_suffix(None) is None
    assert parse_title_suffix("Trailing comma, ") == ""


def test_resolver_clear_api_municipality_match(reference):
    result = resolve_event_geography(
        make_event("Linköping", "30 juni 10.36, Trafikolycka, Linköping"),
        reference,
    )

    assert_only_v2_geography_fields(result)
    assert result == {
        "api_location_name": "Linköping",
        "api_location_gps": "58.410807,15.621373",
        "api_location_granularity": "municipality",
        "api_location_latitude": 58.410807,
        "api_location_longitude": 15.621373,
        "derived_municipality_code": "0580",
        "derived_municipality_name": "Linköping",
        "derived_county_code": "05",
        "derived_county_name": "Östergötlands län",
    }


def test_resolver_county_title_is_not_municipality(reference):
    result = resolve_event_geography(
        make_event("Östergötlands län", "30 juni 10.36, Sammanfattning natt, Östergötlands län"),
        reference,
    )

    assert result["api_location_granularity"] == "county"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] == "05"
    assert result["derived_county_name"] == "Östergötlands län"


def test_resolver_api_county_and_title_municipality_same_county(reference):
    result = resolve_event_geography(
        make_event("Östergötlands län", "30 juni 10.36, Trafikolycka, Linköping"),
        reference,
    )

    assert result["api_location_granularity"] == "county"
    assert result["derived_municipality_code"] == "0580"
    assert result["derived_municipality_name"] == "Linköping"
    assert result["derived_county_code"] == "05"
    assert result["derived_county_name"] == "Östergötlands län"


def test_resolver_cross_county_conflict_leaves_all_derived_geography_null(reference):
    result = resolve_event_geography(
        make_event("Stockholms län", "30 juni 10.36, Trafikolycka, Linköping"),
        reference,
    )

    assert result["api_location_granularity"] == "county"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] is None
    assert result["derived_county_name"] is None


def test_resolver_api_municipality_and_different_same_county_title_municipality(reference):
    result = resolve_event_geography(
        make_event("Järfälla", "25 november 13.49, Farligt föremål, misstänkt, Upplands-Bro"),
        reference,
    )

    assert result["api_location_granularity"] == "municipality"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] == "01"
    assert result["derived_county_name"] == "Stockholms län"


def test_resolver_api_municipality_and_same_county_title_county_keeps_municipality(reference):
    result = resolve_event_geography(
        make_event("Linköping", "30 juni 10.36, Sammanfattning natt, Östergötlands län"),
        reference,
    )

    assert result["api_location_granularity"] == "municipality"
    assert result["derived_municipality_code"] == "0580"
    assert result["derived_municipality_name"] == "Linköping"
    assert result["derived_county_code"] == "05"
    assert result["derived_county_name"] == "Östergötlands län"


def test_resolver_api_municipality_and_different_title_county_is_cross_county_conflict(reference):
    result = resolve_event_geography(
        make_event("Linköping", "30 juni 10.36, Sammanfattning natt, Stockholms län"),
        reference,
    )

    assert result["api_location_granularity"] == "municipality"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] is None
    assert result["derived_county_name"] is None


def test_resolver_api_municipality_and_cross_county_title_municipality_is_conflict(reference):
    result = resolve_event_geography(
        make_event("Linköping", "30 juni 10.36, Trafikolycka, Stockholm"),
        reference,
    )

    assert result["api_location_granularity"] == "municipality"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] is None
    assert result["derived_county_name"] is None


def test_resolver_api_county_and_different_title_county_is_cross_county_conflict(reference):
    result = resolve_event_geography(
        make_event("Östergötlands län", "30 juni 10.36, Sammanfattning natt, Stockholms län"),
        reference,
    )

    assert result["api_location_granularity"] == "county"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] is None
    assert result["derived_county_name"] is None


def test_resolver_api_county_without_title_municipality_keeps_county_only(reference):
    result = resolve_event_geography(
        make_event("Östergötlands län", "Title without comma"),
        reference,
    )

    assert result["api_location_granularity"] == "county"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] == "05"
    assert result["derived_county_name"] == "Östergötlands län"


def test_resolver_unknown_api_with_exact_title_municipality_is_accepted(reference):
    result = resolve_event_geography(
        make_event("Okänd plats", "30 juni 10.36, Trafikolycka, Linköping"),
        reference,
    )

    assert result["api_location_granularity"] == "unknown"
    assert result["derived_municipality_code"] == "0580"
    assert result["derived_municipality_name"] == "Linköping"
    assert result["derived_county_code"] == "05"
    assert result["derived_county_name"] == "Östergötlands län"


def test_resolver_unknown_api_with_exact_title_county_derives_county_only(reference):
    result = resolve_event_geography(
        make_event("Okänd plats", "30 juni 10.36, Sammanfattning natt, Östergötlands län"),
        reference,
    )

    assert result["api_location_granularity"] == "unknown"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] == "05"
    assert result["derived_county_name"] == "Östergötlands län"


def test_resolver_unresolved_rows_do_not_parse_summary_or_body_text(reference):
    result = resolve_event_geography(
        make_event("Okänd plats", "Title without comma", summary="Text mentions Linköping."),
        reference,
    )

    assert result["api_location_granularity"] == "unknown"
    assert result["derived_municipality_code"] is None
    assert result["derived_municipality_name"] is None
    assert result["derived_county_code"] is None
    assert result["derived_county_name"] is None
