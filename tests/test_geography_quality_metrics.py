from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import duckdb
import pytest

from export_schema import PARQUET_EXPORT_COLUMNS, PARQUET_EXPORT_SCHEMA
from geography import load_geography_reference, resolve_event_geography
from scripts import geography_quality_metrics as gqm


@pytest.fixture(scope="module")
def reference():
    return load_geography_reference()


def make_row(
    reference,
    *,
    event_id: int,
    datetime: str | None,
    name: str,
    event_type: str,
    api_location_name: str,
    gps: str = "58.410807,15.621373",
) -> dict:
    event = {
        "id": event_id,
        "datetime": datetime,
        "name": name,
        "summary": f"Summary {event_id}",
        "url": f"/aktuellt/handelser/test/{event_id}/",
        "type": event_type,
        "location": {"name": api_location_name, "gps": gps},
    }
    row = {
        "event_id": str(event_id),
        "datetime": datetime,
        "name": name,
        "summary": event["summary"],
        "url": event["url"],
        "type": event_type,
        "html_title": None,
        "html_preamble": None,
        "html_body": None,
        "html_published_datetime": None,
        "html_author": None,
        "html_available": None,
    }
    row.update(resolve_event_geography(event, reference))
    return row


def sample_rows(reference) -> list[dict]:
    return [
        make_row(
            reference,
            event_id=1,
            datetime="2026-01-01 10:00:00 +01:00",
            name="1 januari 10.00, Trafikolycka, Linköping",
            event_type="Trafikolycka",
            api_location_name="Östergötlands län",
        ),
        make_row(
            reference,
            event_id=2,
            datetime="2026-01-01 11:00:00 +01:00",
            name="1 januari 11.00, Sammanfattning natt, Östergötlands län",
            event_type="Sammanfattning natt",
            api_location_name="Östergötlands län",
        ),
        make_row(
            reference,
            event_id=3,
            datetime="2026-02-01 12:00:00 +01:00",
            name="1 februari 12.00, Farligt föremål, misstänkt, Upplands-Bro",
            event_type="Farligt föremål, misstänkt",
            api_location_name="Järfälla",
        ),
        make_row(
            reference,
            event_id=4,
            datetime="2026-02-01 13:00:00 +01:00",
            name="1 februari 13.00, Trafikolycka, Linköping",
            event_type="Trafikolycka",
            api_location_name="Stockholms län",
        ),
        make_row(
            reference,
            event_id=5,
            datetime="2026-03-01 14:00:00 +01:00",
            name="Title without comma",
            event_type="Övrigt",
            api_location_name="Okänd plats",
            gps="not gps",
        ),
    ]


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
        if normalized_rows:
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


@pytest.fixture()
def sample_parquet(tmp_path, reference):
    parquet_path = tmp_path / "events.parquet"
    write_parquet(parquet_path, sample_rows(reference))
    return parquet_path


def test_build_metrics_json_shape_and_counts(sample_parquet, reference):
    metrics = gqm.build_metrics(sample_parquet, reference=reference, example_limit=2)

    assert metrics["schema_version"] == 2
    assert metrics["dataset"] == {
        "total_rows": 5,
        "min_date": "2026-01-01",
        "max_date": "2026-03-01",
        "invalid_datetime_rows": 0,
        "distinct_event_types": 4,
        "summary_rows": 1,
        "non_summary_rows": 4,
    }
    assert metrics["api_location_granularity_counts"] == {
        "municipality": 1,
        "county": 3,
        "unknown": 1,
    }
    assert metrics["derived_municipality"] == {"assigned": 1, "unassigned": 4}
    assert metrics["derived_county"] == {"assigned": 3, "unassigned": 2}
    assert metrics["geography_shape_counts"] == {
        "municipality_assigned": 1,
        "county_only": 2,
        "fully_unresolved": 2,
    }
    assert metrics["assignment_rule_counts"] == {
        "api_location_municipality": 0,
        "title_municipality_validated_by_api_county": 1,
        "api_county_only": 1,
        "title_municipality_without_api_admin_match": 0,
        "title_county_without_api_admin_match": 0,
        "same_county_municipality_mismatch_county_only": 1,
        "cross_county_conflict_unresolved": 1,
        "unresolved": 1,
    }
    assert metrics["source_conflict_counts"] == {
        "same_county_municipality_mismatch": 1,
        "cross_county_conflict": 1,
    }
    assert metrics["source_signal_counts"] == {
        "api_county__title_county": 1,
        "api_county__title_municipality": 2,
        "api_municipality__title_municipality": 1,
        "api_unknown__title_unknown": 1,
    }
    assert metrics["reference"]["municipality_count"] == 290
    assert metrics["reference"]["county_count"] == 21
    assert metrics["examples"]["same_county_municipality_mismatch"][0]["event_id"] == "3"
    assert metrics["examples"]["cross_county_conflict"][0]["event_id"] == "4"
    assert metrics["examples"]["fully_unresolved"][0]["event_id"] == "5"

    by_month = {item["month"]: item for item in metrics["by_month"]}
    assert by_month["2026-01"]["rows"] == 2
    assert by_month["2026-02"]["source_conflict_counts"]["cross_county_conflict"] == 1
    assert by_month["2026-02"]["assignment_rule_counts"]["cross_county_conflict_unresolved"] == 1

    by_type = {item["type"]: item for item in metrics["by_event_type"]}
    assert by_type["Trafikolycka"]["rows"] == 2
    assert by_type["Sammanfattning natt"]["summary_rows"] == 1
    assert by_type["Farligt föremål, misstänkt"]["geography_shape_counts"]["county_only"] == 1

    gqm.check_metrics_consistency(metrics)


def test_cli_check_writes_json_markdown_and_workflow_outputs(sample_parquet, tmp_path):
    json_path = tmp_path / "geography-quality.json"
    markdown_path = tmp_path / "geography-quality.md"
    github_output_path = tmp_path / "github-output.txt"

    assert (
        gqm.main(
            [
                str(sample_parquet),
                "--json",
                str(json_path),
                "--markdown",
                str(markdown_path),
                "--github-output",
                str(github_output_path),
                "--check",
            ]
        )
        == 0
    )

    written = json.loads(json_path.read_text(encoding="utf-8"))
    assert written["assignment_rule_counts"]["same_county_municipality_mismatch_county_only"] == 1
    assert written["assignment_rule_counts"]["cross_county_conflict_unresolved"] == 1
    assert written["source_conflict_counts"]["same_county_municipality_mismatch"] == 1
    assert written["source_conflict_counts"]["cross_county_conflict"] == 1

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "# Geography Quality Metrics" in markdown
    assert "Rows: **5**" in markdown
    assert "## Assignment rule counts" in markdown
    assert "## Source conflict counts" in markdown
    assert "## Source signal counts" in markdown
    assert "api_county__title_municipality" in markdown
    assert "## By month" in markdown
    assert "## By event type" in markdown
    assert "same_county_municipality_mismatch" in markdown
    assert "cross_county_conflict" in markdown

    github_output = github_output_path.read_text(encoding="utf-8")
    output_values = dict(line.split("=", 1) for line in github_output.splitlines())
    assert output_values["total_rows"] == "5"
    assert output_values["min_date"] == "2026-01-01"
    assert output_values["max_date"] == "2026-03-01"
    assert output_values["derived_municipality_assigned"] == "1"
    assert output_values["derived_municipality_assigned_pct"] == "20.0%"
    assert output_values["county_only_rows"] == "2"
    assert output_values["county_only_pct"] == "40.0%"
    assert output_values["same_county_municipality_mismatch"] == "1"
    assert output_values["same_county_municipality_mismatch_pct"] == "20.0%"
    assert output_values["cross_county_conflict"] == "1"
    assert output_values["cross_county_conflict_pct"] == "20.0%"
    assert output_values["assignment_rule_cross_county_conflict_unresolved"] == "1"


def test_release_workflow_only_consumes_declared_geography_outputs(sample_parquet, reference, tmp_path):
    metrics = gqm.build_metrics(sample_parquet, reference=reference)
    github_output_path = tmp_path / "github-output.txt"
    gqm.write_github_output(metrics, github_output_path)
    produced_outputs = {
        line.split("=", 1)[0]
        for line in github_output_path.read_text(encoding="utf-8").splitlines()
    }

    workflow = Path(".github/workflows/release-parquet.yml").read_text(encoding="utf-8")
    consumed_outputs = set(re.findall(r"steps\.geography_quality\.outputs\.([A-Za-z0-9_]+)", workflow))

    assert consumed_outputs
    assert consumed_outputs <= produced_outputs
    assert "steps.stats.outputs" not in workflow


@pytest.mark.parametrize(
    ("row_index", "expected_rule", "expected_conflict"),
    [
        (0, "title_municipality_validated_by_api_county", "none"),
        (1, "api_county_only", "none"),
        (2, "same_county_municipality_mismatch_county_only", "same_county_municipality_mismatch"),
        (3, "cross_county_conflict_unresolved", "cross_county_conflict"),
        (4, "unresolved", "none"),
    ],
)
def test_metrics_uses_shared_resolver_decision_metadata(reference, row_index, expected_rule, expected_conflict):
    row = sample_rows(reference)[row_index]

    resolution = gqm._resolution_from_row(row, reference)

    assert resolution.assignment_rule == expected_rule
    assert resolution.conflict_status == expected_conflict


def test_check_metrics_consistency_rejects_arithmetic_mismatch(sample_parquet, reference):
    metrics = gqm.build_metrics(sample_parquet, reference=reference)
    broken = copy.deepcopy(metrics)
    broken["dataset"]["non_summary_rows"] += 1

    with pytest.raises(ValueError, match=r"summary_rows \+ non_summary_rows"):
        gqm.check_metrics_consistency(broken)


def test_cli_check_rejects_empty_v2_parquet(tmp_path):
    parquet_path = tmp_path / "empty.parquet"
    write_parquet(parquet_path, [])

    assert gqm.main([str(parquet_path), "--check"]) == 1


@pytest.mark.parametrize("bad_datetime", ["not a date", "2026-01-01 garbage", "2026-01-01 10:00:00", None])
def test_cli_check_rejects_invalid_or_missing_datetime(tmp_path, reference, bad_datetime):
    rows = sample_rows(reference)
    rows[0]["datetime"] = bad_datetime
    parquet_path = tmp_path / "bad_datetime.parquet"
    write_parquet(parquet_path, rows)

    assert gqm.main([str(parquet_path), "--check"]) == 1


def test_cli_check_rejects_resolver_contract_mismatch(tmp_path, reference):
    rows = sample_rows(reference)
    rows[3].update(
        {
            "derived_municipality_code": "0580",
            "derived_municipality_name": "Linköping",
            "derived_county_code": "05",
            "derived_county_name": "Östergötlands län",
        }
    )
    parquet_path = tmp_path / "bad_contract.parquet"
    write_parquet(parquet_path, rows)

    assert gqm.main([str(parquet_path), "--check"]) == 1


def test_cli_check_rejects_schema_mismatch(tmp_path):
    bad_parquet = tmp_path / "bad.parquet"
    conn = duckdb.connect(database=":memory:")
    try:
        conn.execute("CREATE TEMP TABLE bad_events AS SELECT '1'::VARCHAR AS event_id")
        conn.table("bad_events").to_parquet(str(bad_parquet), compression="zstd")
    finally:
        conn.close()

    assert gqm.main([str(bad_parquet), "--check"]) == 1
