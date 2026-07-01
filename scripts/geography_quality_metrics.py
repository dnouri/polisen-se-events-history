#!/usr/bin/env python3
"""Generate release-level geography quality metrics for the v2 parquet artifact."""

# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "duckdb>=1.0.0",
# ]
# ///

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from geography import (  # noqa: E402
    DEFAULT_REFERENCE_PATH,
    EXPECTED_REFERENCE_SHA256,
    GEOGRAPHY_ASSIGNMENT_RULE_ORDER,
    GEOGRAPHY_CONFLICT_ORDER,
    GEOGRAPHY_SHAPE_ORDER,
    GeographyReference,
    GeographyResolution,
    GeographyShapeName,
    explain_event_geography,
    load_geography_reference,
    reference_file_sha256,
)
from scripts.validate_export_schema import (  # noqa: E402
    validate_parquet_schema,
    validate_parquet_semantics,
)

API_GRANULARITY_ORDER = ("municipality", "county", "unknown")
EXAMPLE_CATEGORIES = (
    "fully_unresolved",
    "same_county_municipality_mismatch",
    "cross_county_conflict",
)
DEFAULT_EXAMPLE_LIMIT = 10
DEFAULT_EVENT_TYPE_LIMIT = 15
DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
METRICS_PARQUET_COLUMNS = (
    "event_id",
    "datetime",
    "type",
    "name",
    "api_location_name",
    "api_location_gps",
    "api_location_granularity",
    "derived_municipality_code",
    "derived_municipality_name",
    "derived_county_code",
    "derived_county_name",
)

MUNICIPALITY_ASSIGNMENT_RULES = (
    "api_location_municipality",
    "title_municipality_validated_by_api_county",
    "title_municipality_without_api_admin_match",
)
COUNTY_ONLY_ASSIGNMENT_RULES = (
    "api_county_only",
    "title_county_without_api_admin_match",
    "same_county_municipality_mismatch_county_only",
)
FULLY_UNRESOLVED_ASSIGNMENT_RULES = (
    "cross_county_conflict_unresolved",
    "unresolved",
)
CONFLICT_ASSIGNMENT_RULES = {
    "same_county_municipality_mismatch": "same_county_municipality_mismatch_county_only",
    "cross_county_conflict": "cross_county_conflict_unresolved",
}


def _duckdb_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _duckdb_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _empty_counts(keys: tuple[str, ...]) -> dict[str, int]:
    return {key: 0 for key in keys}


def _count_mapping(counter: Counter[str], preferred_order: tuple[str, ...]) -> dict[str, int]:
    """Return a deterministic count mapping with preferred zero-valued keys first."""

    result = {key: int(counter.get(key, 0)) for key in preferred_order}
    for key in sorted(set(counter) - set(preferred_order)):
        result[key] = int(counter[key])
    return result


def _sum_counts(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    return sum(int(mapping.get(key, 0)) for key in keys)


def _valid_date_prefix(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not DATE_PREFIX_RE.match(text):
        return None

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            # The Police API occasionally uses non-zero-padded hours.
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        return None
    return parsed.date().isoformat()


def _month_bucket(value: object) -> str:
    prefix = _valid_date_prefix(value)
    return prefix[:7] if prefix is not None else "invalid"


def _is_summary_type(event_type: object) -> bool:
    return isinstance(event_type, str) and event_type.startswith("Sammanfattning")


def _json_scalar(value: Any) -> Any:
    """Normalize values returned by DuckDB before JSON serialization."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def read_v2_rows(parquet_path: Path) -> list[dict[str, Any]]:
    """Read only the v2 parquet columns needed for metrics in deterministic order."""

    parquet_literal = _duckdb_string_literal(str(parquet_path))
    column_sql = ", ".join(_duckdb_identifier(column) for column in METRICS_PARQUET_COLUMNS)
    order_sql = "datetime NULLS FIRST, event_id NULLS FIRST"

    conn = duckdb.connect(database=":memory:")
    try:
        cursor = conn.execute(
            f"SELECT {column_sql} FROM read_parquet({parquet_literal}) ORDER BY {order_sql}"
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        {column: _json_scalar(value) for column, value in zip(METRICS_PARQUET_COLUMNS, row)}
        for row in rows
    ]


def exported_geography_shape(row: Mapping[str, Any]) -> GeographyShapeName:
    """Classify the exported derived geography null-shape only."""

    if row.get("derived_municipality_code") is not None:
        return "municipality_assigned"
    if row.get("derived_county_code") is not None:
        return "county_only"
    return "fully_unresolved"


def _resolution_from_row(row: Mapping[str, Any], reference: GeographyReference) -> GeographyResolution:
    """Re-use the resolver's decision metadata from exported raw API/title fields."""

    return explain_event_geography(
        {
            "name": row.get("name"),
            "location": {
                "name": row.get("api_location_name"),
                "gps": row.get("api_location_gps"),
            },
        },
        reference,
    )


def _new_breakdown(bucket: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "rows": 0,
        "summary_rows": 0,
        "non_summary_rows": 0,
        "derived_municipality_assigned": 0,
        "derived_municipality_unassigned": 0,
        "derived_county_assigned": 0,
        "derived_county_unassigned": 0,
        "geography_shape_counts": _empty_counts(GEOGRAPHY_SHAPE_ORDER),
        "assignment_rule_counts": _empty_counts(GEOGRAPHY_ASSIGNMENT_RULE_ORDER),
        "source_conflict_counts": _empty_counts(GEOGRAPHY_CONFLICT_ORDER),
    }
    if bucket is not None:
        item["bucket"] = bucket
    return item


def _update_breakdown(
    item: dict[str, Any],
    row: Mapping[str, Any],
    resolution: GeographyResolution,
    shape: GeographyShapeName,
) -> None:
    item["rows"] += 1
    if _is_summary_type(row.get("type")):
        item["summary_rows"] += 1
    else:
        item["non_summary_rows"] += 1

    if row.get("derived_municipality_code") is not None:
        item["derived_municipality_assigned"] += 1
    else:
        item["derived_municipality_unassigned"] += 1

    if row.get("derived_county_code") is not None:
        item["derived_county_assigned"] += 1
    else:
        item["derived_county_unassigned"] += 1

    item["geography_shape_counts"][shape] += 1
    item["assignment_rule_counts"][resolution.assignment_rule] += 1
    if resolution.conflict_status != "none":
        item["source_conflict_counts"][resolution.conflict_status] += 1


def _ordered_breakdown_item(key_name: str, key: Any, item: Mapping[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {key_name: key}
    for name in (
        "rows",
        "summary_rows",
        "non_summary_rows",
        "derived_municipality_assigned",
        "derived_municipality_unassigned",
        "derived_county_assigned",
        "derived_county_unassigned",
        "geography_shape_counts",
        "assignment_rule_counts",
        "source_conflict_counts",
    ):
        ordered[name] = item[name]
    return ordered


def _example_from_row(
    row: Mapping[str, Any],
    resolution: GeographyResolution,
    category: str,
) -> dict[str, Any]:
    return {
        "event_id": row.get("event_id"),
        "datetime": row.get("datetime"),
        "type": row.get("type"),
        "name": row.get("name"),
        "api_location_name": row.get("api_location_name"),
        "api_location_granularity": row.get("api_location_granularity"),
        "title_suffix": resolution.title_suffix,
        "category": category,
        "assignment_rule": resolution.assignment_rule,
        "conflict_status": resolution.conflict_status,
        "reason": resolution.reason,
        "derived_municipality_code": row.get("derived_municipality_code"),
        "derived_municipality_name": row.get("derived_municipality_name"),
        "derived_county_code": row.get("derived_county_code"),
        "derived_county_name": row.get("derived_county_name"),
    }


def build_metrics(
    parquet_path: Path,
    reference: GeographyReference | None = None,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> dict[str, Any]:
    """Build deterministic geography quality metrics from a v2 parquet artifact."""

    validate_parquet_schema(parquet_path)
    reference = reference or load_geography_reference()
    rows = read_v2_rows(parquet_path)

    total_rows = len(rows)
    summary_rows = 0
    date_prefixes: list[str] = []
    invalid_datetime_rows = 0
    event_types: set[str] = set()

    api_granularity_counter: Counter[str] = Counter()
    shape_counter: Counter[str] = Counter()
    assignment_rule_counter: Counter[str] = Counter()
    conflict_counter: Counter[str] = Counter()
    source_signal_counter: Counter[str] = Counter()
    event_type_breakdowns: dict[Any, dict[str, Any]] = {}
    month_breakdowns: dict[str, dict[str, Any]] = {}
    examples: dict[str, list[dict[str, Any]]] = {category: [] for category in EXAMPLE_CATEGORIES}

    for row in rows:
        event_type = row.get("type")
        if isinstance(event_type, str):
            event_types.add(event_type)
        if _is_summary_type(event_type):
            summary_rows += 1

        date_prefix = _valid_date_prefix(row.get("datetime"))
        if date_prefix is None:
            invalid_datetime_rows += 1
        else:
            date_prefixes.append(date_prefix)

        api_granularity = row.get("api_location_granularity")
        api_granularity_counter[str(api_granularity) if api_granularity is not None else "null"] += 1

        shape = exported_geography_shape(row)
        shape_counter[shape] += 1

        resolution = _resolution_from_row(row, reference)
        assignment_rule_counter[resolution.assignment_rule] += 1
        if resolution.conflict_status != "none":
            conflict_counter[resolution.conflict_status] += 1
        source_signal_counter[
            f"api_{resolution.api_granularity}__title_{resolution.title_granularity}"
        ] += 1

        type_key = event_type
        if type_key not in event_type_breakdowns:
            event_type_breakdowns[type_key] = _new_breakdown(None)
        _update_breakdown(event_type_breakdowns[type_key], row, resolution, shape)

        month = _month_bucket(row.get("datetime"))
        if month not in month_breakdowns:
            month_breakdowns[month] = _new_breakdown(month)
        _update_breakdown(month_breakdowns[month], row, resolution, shape)

        example_category: str | None = None
        if resolution.conflict_status != "none":
            example_category = resolution.conflict_status
        elif shape == "fully_unresolved":
            example_category = "fully_unresolved"
        if (
            example_category in examples
            and len(examples[example_category]) < example_limit
        ):
            examples[example_category].append(_example_from_row(row, resolution, example_category))

    non_summary_rows = total_rows - summary_rows
    derived_municipality_assigned = sum(
        1 for row in rows if row.get("derived_municipality_code") is not None
    )
    derived_county_assigned = sum(1 for row in rows if row.get("derived_county_code") is not None)

    by_event_type = [
        _ordered_breakdown_item("type", event_type, item)
        for event_type, item in sorted(
            event_type_breakdowns.items(),
            key=lambda pair: (-pair[1]["rows"], "" if pair[0] is None else str(pair[0])),
        )
    ]
    by_month = [
        _ordered_breakdown_item("month", month, item)
        for month, item in sorted(month_breakdowns.items(), key=lambda pair: pair[0])
    ]

    reference_path_label = str(DEFAULT_REFERENCE_PATH.relative_to(REPO_ROOT))
    metrics: dict[str, Any] = {
        "schema_version": 2,
        "dataset": {
            "total_rows": total_rows,
            "min_date": min(date_prefixes) if date_prefixes else None,
            "max_date": max(date_prefixes) if date_prefixes else None,
            "invalid_datetime_rows": invalid_datetime_rows,
            "distinct_event_types": len(event_types),
            "summary_rows": summary_rows,
            "non_summary_rows": non_summary_rows,
        },
        "reference": {
            "path": reference_path_label,
            "sha256": reference_file_sha256(DEFAULT_REFERENCE_PATH),
            "expected_sha256": EXPECTED_REFERENCE_SHA256,
            "municipality_count": len(reference.municipalities),
            "county_count": len(reference.counties),
        },
        "api_location_granularity_counts": _count_mapping(
            api_granularity_counter,
            API_GRANULARITY_ORDER,
        ),
        "derived_municipality": {
            "assigned": derived_municipality_assigned,
            "unassigned": total_rows - derived_municipality_assigned,
        },
        "derived_county": {
            "assigned": derived_county_assigned,
            "unassigned": total_rows - derived_county_assigned,
        },
        "geography_shape_counts": _count_mapping(shape_counter, GEOGRAPHY_SHAPE_ORDER),
        "assignment_rule_counts": _count_mapping(
            assignment_rule_counter,
            GEOGRAPHY_ASSIGNMENT_RULE_ORDER,
        ),
        "source_conflict_counts": _count_mapping(conflict_counter, GEOGRAPHY_CONFLICT_ORDER),
        "source_signal_counts": _count_mapping(source_signal_counter, ()),
        "by_event_type": by_event_type,
        "by_month": by_month,
        "examples": examples,
    }
    return metrics


def _append_assignment_shape_consistency(
    errors: list[str],
    assignments: Mapping[str, Any],
    shapes: Mapping[str, Any],
    prefix: str,
) -> None:
    municipality_from_rules = _sum_counts(assignments, MUNICIPALITY_ASSIGNMENT_RULES)
    county_only_from_rules = _sum_counts(assignments, COUNTY_ONLY_ASSIGNMENT_RULES)
    unresolved_from_rules = _sum_counts(assignments, FULLY_UNRESOLVED_ASSIGNMENT_RULES)

    if municipality_from_rules != int(shapes.get("municipality_assigned", 0)):
        errors.append(f"{prefix} municipality assignment rules must equal municipality_assigned shape")
    if county_only_from_rules != int(shapes.get("county_only", 0)):
        errors.append(f"{prefix} county-only assignment rules must equal county_only shape")
    if unresolved_from_rules != int(shapes.get("fully_unresolved", 0)):
        errors.append(f"{prefix} unresolved assignment rules must equal fully_unresolved shape")


def _append_conflict_rule_consistency(
    errors: list[str],
    assignments: Mapping[str, Any],
    conflicts: Mapping[str, Any],
    prefix: str,
) -> None:
    for conflict, rule in CONFLICT_ASSIGNMENT_RULES.items():
        if int(conflicts.get(conflict, 0)) != int(assignments.get(rule, 0)):
            errors.append(f"{prefix} conflict count {conflict!r} must equal assignment rule {rule!r}")


def check_metrics_consistency(metrics: Mapping[str, Any]) -> None:
    """Fail on arithmetic/internal inconsistencies in generated metrics."""

    errors: list[str] = []
    dataset = metrics["dataset"]
    total_rows = int(dataset["total_rows"])

    def append_if(condition: bool, message: str) -> None:
        if condition:
            errors.append(message)

    append_if(total_rows <= 0, "total_rows must be greater than zero")
    append_if(
        int(dataset["summary_rows"]) + int(dataset["non_summary_rows"]) != total_rows,
        "summary_rows + non_summary_rows must equal total_rows",
    )
    append_if(
        total_rows > 0 and int(dataset.get("invalid_datetime_rows", 0)) != 0,
        "invalid_datetime_rows must be zero",
    )

    api_total = sum(int(value) for value in metrics["api_location_granularity_counts"].values())
    append_if(api_total != total_rows, "api_location_granularity_counts must sum to total_rows")

    municipality = metrics["derived_municipality"]
    append_if(
        int(municipality["assigned"]) + int(municipality["unassigned"]) != total_rows,
        "derived_municipality assigned + unassigned must equal total_rows",
    )

    county = metrics["derived_county"]
    append_if(
        int(county["assigned"]) + int(county["unassigned"]) != total_rows,
        "derived_county assigned + unassigned must equal total_rows",
    )

    shapes = metrics["geography_shape_counts"]
    shape_total = _sum_counts(shapes, GEOGRAPHY_SHAPE_ORDER)
    append_if(shape_total != total_rows, "geography_shape_counts must sum to total_rows")
    append_if(
        int(shapes.get("municipality_assigned", 0)) != int(municipality["assigned"]),
        "municipality_assigned shape count must equal derived_municipality.assigned",
    )
    append_if(
        int(shapes.get("county_only", 0)) + int(shapes.get("municipality_assigned", 0))
        != int(county["assigned"]),
        "derived_county.assigned must equal municipality_assigned + county_only shapes",
    )

    assignments = metrics["assignment_rule_counts"]
    assignment_total = _sum_counts(assignments, GEOGRAPHY_ASSIGNMENT_RULE_ORDER)
    append_if(assignment_total != total_rows, "assignment_rule_counts must sum to total_rows")
    _append_assignment_shape_consistency(errors, assignments, shapes, "top-level")

    conflicts = metrics["source_conflict_counts"]
    conflict_total = _sum_counts(conflicts, GEOGRAPHY_CONFLICT_ORDER)
    append_if(conflict_total > total_rows, "source_conflict_counts cannot exceed total_rows")
    _append_conflict_rule_consistency(errors, assignments, conflicts, "top-level")

    signal_total = sum(int(value) for value in metrics["source_signal_counts"].values())
    append_if(signal_total != total_rows, "source_signal_counts must sum to total_rows")

    by_event_type_total = sum(int(item["rows"]) for item in metrics["by_event_type"])
    append_if(by_event_type_total != total_rows, "by_event_type rows must sum to total_rows")
    by_month_total = sum(int(item["rows"]) for item in metrics["by_month"])
    append_if(by_month_total != total_rows, "by_month rows must sum to total_rows")

    for section_name in ("by_event_type", "by_month"):
        section = metrics[section_name]
        append_if(
            sum(int(item["summary_rows"]) for item in section) != int(dataset["summary_rows"]),
            f"{section_name} summary_rows must sum to dataset summary_rows",
        )
        append_if(
            sum(int(item["non_summary_rows"]) for item in section) != int(dataset["non_summary_rows"]),
            f"{section_name} non_summary_rows must sum to dataset non_summary_rows",
        )
        append_if(
            sum(int(item["derived_municipality_assigned"]) for item in section)
            != int(municipality["assigned"]),
            f"{section_name} derived_municipality_assigned must sum to top-level count",
        )
        append_if(
            sum(int(item["derived_municipality_unassigned"]) for item in section)
            != int(municipality["unassigned"]),
            f"{section_name} derived_municipality_unassigned must sum to top-level count",
        )
        append_if(
            sum(int(item["derived_county_assigned"]) for item in section) != int(county["assigned"]),
            f"{section_name} derived_county_assigned must sum to top-level count",
        )
        append_if(
            sum(int(item["derived_county_unassigned"]) for item in section)
            != int(county["unassigned"]),
            f"{section_name} derived_county_unassigned must sum to top-level count",
        )
        for shape in GEOGRAPHY_SHAPE_ORDER:
            append_if(
                sum(int(item["geography_shape_counts"].get(shape, 0)) for item in section)
                != int(shapes.get(shape, 0)),
                f"{section_name} geography shape {shape!r} must sum to top-level count",
            )
        for rule in GEOGRAPHY_ASSIGNMENT_RULE_ORDER:
            append_if(
                sum(int(item["assignment_rule_counts"].get(rule, 0)) for item in section)
                != int(assignments.get(rule, 0)),
                f"{section_name} assignment rule {rule!r} must sum to top-level count",
            )
        for conflict in GEOGRAPHY_CONFLICT_ORDER:
            append_if(
                sum(int(item["source_conflict_counts"].get(conflict, 0)) for item in section)
                != int(conflicts.get(conflict, 0)),
                f"{section_name} conflict count {conflict!r} must sum to top-level count",
            )

        for item in section:
            rows = int(item["rows"])
            item_shapes = item["geography_shape_counts"]
            item_assignments = item["assignment_rule_counts"]
            item_conflicts = item["source_conflict_counts"]
            append_if(
                int(item["summary_rows"]) + int(item["non_summary_rows"]) != rows,
                f"{section_name} item summary/non-summary mismatch for {item!r}",
            )
            append_if(
                int(item["derived_municipality_assigned"])
                + int(item["derived_municipality_unassigned"])
                != rows,
                f"{section_name} item derived municipality mismatch for {item!r}",
            )
            append_if(
                int(item["derived_county_assigned"]) + int(item["derived_county_unassigned"])
                != rows,
                f"{section_name} item derived county mismatch for {item!r}",
            )
            append_if(
                _sum_counts(item_shapes, GEOGRAPHY_SHAPE_ORDER) != rows,
                f"{section_name} item geography_shape_counts must sum to rows for {item!r}",
            )
            append_if(
                int(item_shapes.get("municipality_assigned", 0))
                != int(item["derived_municipality_assigned"]),
                f"{section_name} item municipality shape/derived mismatch for {item!r}",
            )
            append_if(
                int(item_shapes.get("municipality_assigned", 0))
                + int(item_shapes.get("county_only", 0))
                != int(item["derived_county_assigned"]),
                f"{section_name} item county shape/derived mismatch for {item!r}",
            )
            append_if(
                _sum_counts(item_assignments, GEOGRAPHY_ASSIGNMENT_RULE_ORDER) != rows,
                f"{section_name} item assignment_rule_counts must sum to rows for {item!r}",
            )
            _append_assignment_shape_consistency(
                errors,
                item_assignments,
                item_shapes,
                f"{section_name} item",
            )
            append_if(
                _sum_counts(item_conflicts, GEOGRAPHY_CONFLICT_ORDER) > rows,
                f"{section_name} item source_conflict_counts cannot exceed rows for {item!r}",
            )
            _append_conflict_rule_consistency(
                errors,
                item_assignments,
                item_conflicts,
                f"{section_name} item",
            )

    reference = metrics["reference"]
    append_if(
        reference["sha256"] != reference["expected_sha256"],
        "reference sha256 must match expected_sha256",
    )

    if errors:
        raise ValueError("metrics consistency check failed: " + "; ".join(errors))


def _pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a"
    return f"{(numerator / denominator) * 100:.1f}%"


def _markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _append_breakdown_table(
    lines: list[str],
    title: str,
    items: list[Mapping[str, Any]],
    key_name: str,
    total_rows: int,
    limit: int | None = None,
) -> None:
    lines.extend(
        [
            "",
            title,
            "",
            "| Bucket | Rows | Share | Derived municipality | County-only | Fully unresolved | Same-county mismatches | Cross-county conflicts |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if not items:
        lines.append("| No rows | 0 | n/a | 0 | 0 | 0 | 0 | 0 |")
        return

    displayed = items[:limit] if limit is not None else items
    for item in displayed:
        rows = int(item["rows"])
        shapes = item["geography_shape_counts"]
        conflicts = item["source_conflict_counts"]
        lines.append(
            f"| {_markdown_cell(item.get(key_name))} "
            f"| {rows} "
            f"| {_pct(rows, total_rows)} "
            f"| {item['derived_municipality_assigned']} "
            f"| {shapes['county_only']} "
            f"| {shapes['fully_unresolved']} "
            f"| {conflicts['same_county_municipality_mismatch']} "
            f"| {conflicts['cross_county_conflict']} |"
        )
    if limit is not None and len(items) > limit:
        lines.append(f"| … {len(items) - limit} more |  |  |  |  |  |  |  |")


def render_markdown(metrics: Mapping[str, Any]) -> str:
    """Render a compact deterministic Markdown report."""

    dataset = metrics["dataset"]
    total_rows = int(dataset["total_rows"])
    municipality = metrics["derived_municipality"]
    county = metrics["derived_county"]
    shapes = metrics["geography_shape_counts"]
    assignments = metrics["assignment_rule_counts"]
    conflicts = metrics["source_conflict_counts"]
    reference = metrics["reference"]

    lines = [
        "# Geography Quality Metrics",
        "",
        f"Rows: **{total_rows}**",
        f"Date range: **{dataset['min_date']}** to **{dataset['max_date']}**",
        f"Distinct event types: **{dataset['distinct_event_types']}**",
        "",
        "## Summary",
        "",
        "| Metric | Rows | Share |",
        "|---|---:|---:|",
        f"| Summary rows | {dataset['summary_rows']} | {_pct(int(dataset['summary_rows']), total_rows)} |",
        f"| Non-summary rows | {dataset['non_summary_rows']} | {_pct(int(dataset['non_summary_rows']), total_rows)} |",
        f"| Derived municipality assigned | {municipality['assigned']} | {_pct(int(municipality['assigned']), total_rows)} |",
        f"| Derived municipality unassigned | {municipality['unassigned']} | {_pct(int(municipality['unassigned']), total_rows)} |",
        f"| Derived county assigned | {county['assigned']} | {_pct(int(county['assigned']), total_rows)} |",
        f"| Derived county unassigned | {county['unassigned']} | {_pct(int(county['unassigned']), total_rows)} |",
        f"| County-only rows | {shapes['county_only']} | {_pct(int(shapes['county_only']), total_rows)} |",
        f"| No derived geography rows | {shapes['fully_unresolved']} | {_pct(int(shapes['fully_unresolved']), total_rows)} |",
        f"| Same-county municipality mismatches | {conflicts['same_county_municipality_mismatch']} | {_pct(int(conflicts['same_county_municipality_mismatch']), total_rows)} |",
        f"| Cross-county conflicts | {conflicts['cross_county_conflict']} | {_pct(int(conflicts['cross_county_conflict']), total_rows)} |",
        "",
        "## API location granularity",
        "",
        "| Granularity | Rows | Share |",
        "|---|---:|---:|",
    ]

    for granularity, count in metrics["api_location_granularity_counts"].items():
        lines.append(f"| {granularity} | {count} | {_pct(int(count), total_rows)} |")

    lines.extend(
        [
            "",
            "## Geography shape counts",
            "",
            "| Shape | Rows | Share |",
            "|---|---:|---:|",
        ]
    )
    for shape in GEOGRAPHY_SHAPE_ORDER:
        count = int(shapes.get(shape, 0))
        lines.append(f"| {shape} | {count} | {_pct(count, total_rows)} |")

    lines.extend(
        [
            "",
            "## Assignment rule counts",
            "",
            "| Assignment rule | Rows | Share |",
            "|---|---:|---:|",
        ]
    )
    for rule in GEOGRAPHY_ASSIGNMENT_RULE_ORDER:
        count = int(assignments.get(rule, 0))
        lines.append(f"| {rule} | {count} | {_pct(count, total_rows)} |")

    lines.extend(
        [
            "",
            "## Source conflict counts",
            "",
            "| Conflict | Rows | Share |",
            "|---|---:|---:|",
        ]
    )
    for conflict in GEOGRAPHY_CONFLICT_ORDER:
        count = int(conflicts.get(conflict, 0))
        lines.append(f"| {conflict} | {count} | {_pct(count, total_rows)} |")

    lines.extend(
        [
            "",
            "## Source signal counts",
            "",
            "Ancillary context for raw API location granularity crossed with title-suffix granularity.",
            "",
            "| Source signal | Rows | Share |",
            "|---|---:|---:|",
        ]
    )
    for signal, count in metrics["source_signal_counts"].items():
        signal_count = int(count)
        lines.append(f"| {signal} | {signal_count} | {_pct(signal_count, total_rows)} |")

    _append_breakdown_table(lines, "## By month", list(metrics["by_month"]), "month", total_rows)
    _append_breakdown_table(
        lines,
        f"## By event type (top {DEFAULT_EVENT_TYPE_LIMIT})",
        list(metrics["by_event_type"]),
        "type",
        total_rows,
        limit=DEFAULT_EVENT_TYPE_LIMIT,
    )

    lines.extend(
        [
            "",
            "## Reference",
            "",
            f"- Reference: `{reference['path']}`",
            f"- SHA-256: `{reference['sha256']}`",
            f"- Municipalities: {reference['municipality_count']}",
            f"- Counties: {reference['county_count']}",
            "",
            "## Representative examples",
        ]
    )

    for category in EXAMPLE_CATEGORIES:
        lines.extend(["", f"### {category}", ""])
        examples = metrics["examples"].get(category, [])
        if not examples:
            lines.append("No examples.")
            continue
        lines.extend(
            [
                "| event_id | datetime | type | api_location_name | title_suffix | assignment_rule | reason |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for example in examples:
            lines.append(
                "| "
                + " | ".join(
                    _markdown_cell(example.get(column))
                    for column in (
                        "event_id",
                        "datetime",
                        "type",
                        "api_location_name",
                        "title_suffix",
                        "assignment_rule",
                        "reason",
                    )
                )
                + " |"
            )

    lines.append("")
    return "\n".join(lines)


def write_json(metrics: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(metrics: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(metrics), encoding="utf-8")


def write_github_output(metrics: Mapping[str, Any], path: Path) -> None:
    dataset = metrics["dataset"]
    total_rows = int(dataset["total_rows"])
    municipality = metrics["derived_municipality"]
    county = metrics["derived_county"]
    shapes = metrics["geography_shape_counts"]
    conflicts = metrics["source_conflict_counts"]
    assignments = metrics["assignment_rule_counts"]
    lines = [
        f"total_rows={dataset['total_rows']}",
        f"min_date={dataset['min_date']}",
        f"max_date={dataset['max_date']}",
        f"derived_municipality_assigned={municipality['assigned']}",
        f"derived_municipality_assigned_pct={_pct(int(municipality['assigned']), total_rows)}",
        f"derived_municipality_unassigned={municipality['unassigned']}",
        f"derived_municipality_unassigned_pct={_pct(int(municipality['unassigned']), total_rows)}",
        f"derived_county_assigned={county['assigned']}",
        f"derived_county_assigned_pct={_pct(int(county['assigned']), total_rows)}",
        f"derived_county_unassigned={county['unassigned']}",
        f"derived_county_unassigned_pct={_pct(int(county['unassigned']), total_rows)}",
        f"county_only_rows={shapes['county_only']}",
        f"county_only_pct={_pct(int(shapes['county_only']), total_rows)}",
        f"fully_unresolved_rows={shapes['fully_unresolved']}",
        f"fully_unresolved_pct={_pct(int(shapes['fully_unresolved']), total_rows)}",
        f"same_county_municipality_mismatch={conflicts['same_county_municipality_mismatch']}",
        f"same_county_municipality_mismatch_pct={_pct(int(conflicts['same_county_municipality_mismatch']), total_rows)}",
        f"cross_county_conflict={conflicts['cross_county_conflict']}",
        f"cross_county_conflict_pct={_pct(int(conflicts['cross_county_conflict']), total_rows)}",
    ]
    for rule in GEOGRAPHY_ASSIGNMENT_RULE_ORDER:
        lines.append(f"assignment_rule_{rule}={assignments[rule]}")
        lines.append(f"assignment_rule_{rule}_pct={_pct(int(assignments[rule]), total_rows)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path, help="generated v2 events.parquet artifact")
    parser.add_argument("--json", type=Path, help="write metrics JSON to this path")
    parser.add_argument("--markdown", type=Path, help="write metrics Markdown to this path")
    parser.add_argument(
        "--check",
        dest="check",
        action="store_true",
        default=True,
        help=(
            "run release-safety checks (default): fail on empty artifacts, invalid datetimes, "
            "schema/reference/contract/arithmetic inconsistencies, but not real "
            "unresolved/conflict rows"
        ),
    )
    parser.add_argument(
        "--no-check",
        dest="check",
        action="store_false",
        help="skip release-safety semantic/consistency checks; schema validation still runs",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="append compact metrics keys to a GitHub Actions output file",
    )
    parser.add_argument(
        "--example-limit",
        type=int,
        default=DEFAULT_EXAMPLE_LIMIT,
        help=f"maximum examples per unresolved/conflict category (default: {DEFAULT_EXAMPLE_LIMIT})",
    )
    args = parser.parse_args(argv)

    try:
        reference = load_geography_reference()
        metrics = build_metrics(args.parquet, reference=reference, example_limit=args.example_limit)
        if args.check:
            validate_parquet_semantics(args.parquet, reference)
            check_metrics_consistency(metrics)
        else:
            print(
                "WARNING: --no-check used; skipped release-safety semantic/consistency checks",
                file=sys.stderr,
            )
        if args.json:
            write_json(metrics, args.json)
        if args.markdown:
            write_markdown(metrics, args.markdown)
        if args.github_output:
            write_github_output(metrics, args.github_output)
        if not args.json and not args.markdown:
            print(json.dumps(metrics, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - CLI should print concise release failures.
        print(f"geography quality metrics failed: {exc}", file=sys.stderr)
        return 1

    print(
        "OK: geography quality metrics generated "
        f"for {args.parquet} ({metrics['dataset']['total_rows']} rows)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
