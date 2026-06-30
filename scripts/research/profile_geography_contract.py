#!/usr/bin/env python3
"""Profile the Swedish Police event geography contract.

This is a Phase 1 research spike. It intentionally uses a conservative
algorithm only:

- exact normalized name matching (strip/collapse whitespace + casefold)
- parse the event-title suffix after the final comma
- classify raw API ``location.name`` as municipality/county/unknown
- validate title-derived municipalities against the raw API county when present
- no body/html parsing, no geocoding, no fuzzy aliases

Outputs deterministic JSON and Markdown summaries.
"""

# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "duckdb>=1.0.0",
# ]
# ///

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CRIMECITY_ROOT = REPO_ROOT.parent / "crimecity3k"
DEFAULT_EVENTS_PARQUET = DEFAULT_CRIMECITY_ROOT / "data" / "events.parquet"
DEFAULT_BOUNDARIES = DEFAULT_CRIMECITY_ROOT / "data" / "municipalities" / "boundaries.geojson"
DEFAULT_POPULATION = DEFAULT_CRIMECITY_ROOT / "data" / "municipalities" / "population.csv"
DEFAULT_RAW_EVENTS_JSON = REPO_ROOT / "events.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tmp" / "geography-profile"
DEFAULT_EXPECTED_MANIFEST = REPO_ROOT / "docs" / "v2-geography-profile-manifest.json"
SCRIPT_VERSION = 2
STRICT_PROVENANCE_POLICY = {
    "validated": [
        "manifest schema_version",
        "script path, version, and SHA-256",
        "data input paths, existence, size_bytes, and SHA-256",
        "full export and current raw-window data cutoffs",
        "reference-data checks",
        "current raw-window GPS and API-granularity metrics",
        "headline profile metrics and validation-status counts",
    ],
    "diagnostic_only": [
        "input_provenance.*.git.root",
        "input_provenance.*.git.head",
        "input_provenance.*.git.tracked_dirty",
        "input_provenance.*.git.path_status",
    ],
    "note": "Git metadata is recorded to aid investigation, but strict comparison uses content hashes and deterministic outputs as the authoritative provenance.",
}

# Spike-only county names/codes, manually inlined from SCB's official current
# county-code set ("Län och kommuner i kodnummerordning" / regional divisions;
# https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/),
# checked 2026-06-30. Phase 2 should replace this with a committed SCB-derived
# reference CSV rather than relying on this script table or CrimeCity at runtime.
COUNTY_TABLE_PROVENANCE = {
    "source": "SCB official regional divisions, current county-code set (Län och kommuner i kodnummerordning)",
    "url": "https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/",
    "checked_date": "2026-06-30",
    "scope": "Phase 1 spike-only; Phase 2 should use a committed SCB-derived reference CSV.",
}

COUNTIES: tuple[dict[str, str], ...] = (
    {"code": "01", "name": "Stockholms län"},
    {"code": "03", "name": "Uppsala län"},
    {"code": "04", "name": "Södermanlands län"},
    {"code": "05", "name": "Östergötlands län"},
    {"code": "06", "name": "Jönköpings län"},
    {"code": "07", "name": "Kronobergs län"},
    {"code": "08", "name": "Kalmar län"},
    {"code": "09", "name": "Gotlands län"},
    {"code": "10", "name": "Blekinge län"},
    {"code": "12", "name": "Skåne län"},
    {"code": "13", "name": "Hallands län"},
    {"code": "14", "name": "Västra Götalands län"},
    {"code": "17", "name": "Värmlands län"},
    {"code": "18", "name": "Örebro län"},
    {"code": "19", "name": "Västmanlands län"},
    {"code": "20", "name": "Dalarnas län"},
    {"code": "21", "name": "Gävleborgs län"},
    {"code": "22", "name": "Västernorrlands län"},
    {"code": "23", "name": "Jämtlands län"},
    {"code": "24", "name": "Västerbottens län"},
    {"code": "25", "name": "Norrbottens län"},
)

DATE_RE = re.compile(r"^(\d{4})-(\d{2})")


def normalize_name(name: str | None) -> str:
    """Normalize only enough for exact case-insensitive matching.

    This deliberately does not remove punctuation, accents, hyphens, or words.
    """

    return " ".join((name or "").strip().casefold().split())


def final_title_suffix(title: str | None) -> str:
    if not title or "," not in title:
        return ""
    return title.rsplit(",", 1)[1].strip()


def month_key(datetime_value: str | None) -> str:
    match = DATE_RE.match(datetime_value or "")
    if not match:
        return "unknown"
    return f"{match.group(1)}-{match.group(2)}"


def quarter_key(datetime_value: str | None) -> str:
    match = DATE_RE.match(datetime_value or "")
    if not match:
        return "unknown"
    year = match.group(1)
    month = int(match.group(2))
    return f"{year}-Q{math.ceil(month / 3)}"


def datetime_sort_key(datetime_value: str | None) -> tuple[int, int, int, int, int, int, str]:
    """Return a stable sortable key for Police API datetime strings."""

    text = datetime_value or ""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})", text)
    if not match:
        return (0, 0, 0, 0, 0, 0, text)
    year, month, day, hour, minute, second = (int(group) for group in match.groups())
    return (year, month, day, hour, minute, second, text)


def numeric_id(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def dataset_cutoffs(rows: list[dict[str, Any]], id_key: str, datetime_key: str) -> dict[str, Any]:
    """Summarize which moving data snapshot a profile used."""

    if not rows:
        return {
            "row_count": 0,
            "min_datetime": None,
            "max_datetime": None,
            "min_event_id": None,
            "max_event_id": None,
        }

    ids = [event_id for row in rows if (event_id := numeric_id(row.get(id_key))) is not None]
    datetime_values = [row.get(datetime_key) or "" for row in rows if row.get(datetime_key)]
    return {
        "row_count": len(rows),
        "min_datetime": min(datetime_values, key=datetime_sort_key) if datetime_values else None,
        "max_datetime": max(datetime_values, key=datetime_sort_key) if datetime_values else None,
        "min_event_id": min(ids) if ids else None,
        "max_event_id": max(ids) if ids else None,
    }


def sql_path(path: Path) -> str:
    return str(path).replace("'", "''")


def portable_path(path: Path) -> str:
    """Render repo-local/sibling paths without machine-specific prefixes."""

    resolved = path.resolve(strict=False)
    try:
        rel = resolved.relative_to(REPO_ROOT)
        return "." if str(rel) == "." else str(rel)
    except ValueError:
        pass

    try:
        rel_from_repo = os.path.relpath(resolved, REPO_ROOT)
    except ValueError:
        return str(path)

    # Keep common sibling-repo inputs readable while avoiding surprising very
    # long relative paths for unrelated locations.
    if not rel_from_repo.startswith("../../"):
        return rel_from_repo
    return str(path)


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def git_info_for_path(path: Path) -> dict[str, Any] | None:
    cwd = path if path.is_dir() else path.parent
    root_text = git_output(cwd, "rev-parse", "--show-toplevel")
    if not root_text:
        return None

    root = Path(root_text)
    head = git_output(root, "rev-parse", "HEAD")
    rel_to_root = os.path.relpath(path.resolve(strict=False), root)
    path_status = git_output(root, "status", "--short", "--untracked-files=all", "--", rel_to_root) or ""
    return {
        "root": portable_path(root),
        "head": head,
        # Path-specific dirtiness matters here; unrelated dirty files in a sibling
        # checkout should not make the recorded input snapshot look modified.
        "tracked_dirty": bool(path_status),
        "path_status": path_status,
    }


def build_input_provenance(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    for key, path in paths.items():
        stat = path.stat() if path.exists() else None
        provenance[key] = {
            "path": portable_path(path),
            "exists": path.exists(),
            "size_bytes": stat.st_size if stat and path.is_file() else None,
            "sha256": sha256_file(path),
            "git": git_info_for_path(path),
        }
    return provenance


def pct(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def compact_count(numerator: int, denominator: int) -> str:
    return f"{numerator:,} ({pct(numerator, denominator):.2f}%)"


def md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def truncate(value: Any, max_len: int = 96) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_(none)_\n"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def duplicate_normalized_names(items: list[dict[str, Any]], name_key: str) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = defaultdict(list)
    for item in items:
        seen[normalize_name(item[name_key])].append(item[name_key])
    return {key: values for key, values in sorted(seen.items()) if len(values) > 1}


def load_reference(boundaries_path: Path, population_path: Path) -> dict[str, Any]:
    with boundaries_path.open("r", encoding="utf-8") as f:
        boundaries = json.load(f)

    municipalities: list[dict[str, str]] = []
    for feature in boundaries.get("features", []):
        props = feature.get("properties", {})
        code = str(props.get("id", ""))
        name = str(props.get("kom_namn", ""))
        county_code = str(props.get("lan_code", ""))
        municipalities.append(
            {
                "code": code,
                "name": name,
                "county_code": county_code,
                "county_name": next((c["name"] for c in COUNTIES if c["code"] == county_code), ""),
            }
        )

    population_rows: list[dict[str, str]] = []
    with population_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            population_rows.append(
                {
                    "code": str(row.get("kommun_kod", "")),
                    "name": str(row.get("kommun_namn", "")),
                    "population": str(row.get("population", "")),
                }
            )

    county_by_code = {county["code"]: dict(county) for county in COUNTIES}
    county_by_norm = {normalize_name(county["name"]): dict(county) for county in COUNTIES}
    municipality_by_code = {municipality["code"]: municipality for municipality in municipalities}
    municipality_by_norm = {normalize_name(municipality["name"]): municipality for municipality in municipalities}
    population_by_code = {row["code"]: row for row in population_rows}

    boundary_codes = set(municipality_by_code)
    population_codes = set(population_by_code)
    boundary_county_codes = {municipality["county_code"] for municipality in municipalities}
    county_codes = set(county_by_code)

    name_mismatches = []
    for code in sorted(boundary_codes & population_codes):
        boundary_name = municipality_by_code[code]["name"]
        population_name = population_by_code[code]["name"]
        if normalize_name(boundary_name) != normalize_name(population_name):
            name_mismatches.append(
                {"code": code, "boundaries_name": boundary_name, "population_name": population_name}
            )

    prefix_mismatches = [
        {
            "code": municipality["code"],
            "name": municipality["name"],
            "county_code": municipality["county_code"],
        }
        for municipality in municipalities
        if municipality["code"][:2] != municipality["county_code"]
    ]

    municipality_dupes = duplicate_normalized_names(municipalities, "name")
    county_dupes = duplicate_normalized_names(list(COUNTIES), "name")
    municipality_county_name_collisions = sorted(set(municipality_by_norm) & set(county_by_norm))

    return {
        "municipalities": municipalities,
        "counties": list(COUNTIES),
        "municipality_by_norm": municipality_by_norm,
        "municipality_by_code": municipality_by_code,
        "county_by_norm": county_by_norm,
        "county_by_code": county_by_code,
        "checks": {
            "boundaries_feature_count": len(municipalities),
            "population_row_count": len(population_rows),
            "county_count": len(COUNTIES),
            "boundary_codes_missing_in_population": sorted(boundary_codes - population_codes),
            "population_codes_missing_in_boundaries": sorted(population_codes - boundary_codes),
            "boundary_county_codes": sorted(boundary_county_codes),
            "county_codes_missing_in_static_table": sorted(boundary_county_codes - county_codes),
            "static_county_codes_not_used_by_boundaries": sorted(county_codes - boundary_county_codes),
            "municipality_code_prefix_mismatches": prefix_mismatches,
            "boundary_population_name_mismatches_normalized": name_mismatches,
            "duplicate_normalized_municipality_names": municipality_dupes,
            "duplicate_normalized_county_names": county_dupes,
            "municipality_county_name_collisions": municipality_county_name_collisions,
        },
    }


def classify_name(name: str | None, reference: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_name(name)
    if not normalized:
        return {"granularity": "unknown", "input": name or "", "normalized": normalized}

    municipality = reference["municipality_by_norm"].get(normalized)
    if municipality:
        return {
            "granularity": "municipality",
            "input": name or "",
            "normalized": normalized,
            "code": municipality["code"],
            "name": municipality["name"],
            "county_code": municipality["county_code"],
            "county_name": municipality["county_name"],
        }

    county = reference["county_by_norm"].get(normalized)
    if county:
        return {
            "granularity": "county",
            "input": name or "",
            "normalized": normalized,
            "code": county["code"],
            "name": county["name"],
        }

    return {"granularity": "unknown", "input": name or "", "normalized": normalized}


def load_events(events_path: Path) -> list[dict[str, Any]]:
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT
                event_id,
                datetime,
                name,
                summary,
                url,
                type,
                location_name,
                latitude,
                longitude
            FROM '{sql_path(events_path)}'
            ORDER BY TRY_CAST(event_id AS BIGINT), event_id
            """
        ).fetchall()
        columns = [desc[0] for desc in con.description]
    finally:
        con.close()
    return [dict(zip(columns, row, strict=False)) for row in rows]


def parse_gps(gps: str | None) -> tuple[float, float] | None:
    if not gps or "," not in gps:
        return None
    parts = gps.split(",")
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError:
        return None
    if not (55.0 <= lat <= 70.0 and 10.0 <= lon <= 25.0):
        return None
    return lat, lon


def load_raw_events_window(raw_events_path: Path, reference: dict[str, Any]) -> dict[str, Any]:
    if not raw_events_path.exists():
        return {"available": False, "path": portable_path(raw_events_path)}

    with raw_events_path.open("r", encoding="utf-8") as f:
        events = json.load(f)

    raw_events = events if isinstance(events, list) else []
    granularity_counts: Counter[str] = Counter()
    gps_missing = 0
    gps_invalid = 0
    with_gps = 0
    samples: list[dict[str, Any]] = []
    cutoff_rows: list[dict[str, Any]] = []

    for event in raw_events:
        location = event.get("location") or {}
        location_name = location.get("name") or ""
        gps = location.get("gps") or ""
        classified = classify_name(location_name, reference)
        granularity_counts[classified["granularity"]] += 1
        if gps:
            with_gps += 1
            if parse_gps(gps) is None:
                gps_invalid += 1
        else:
            gps_missing += 1
        cutoff_rows.append({"event_id": event.get("id"), "datetime": event.get("datetime")})
        if len(samples) < 5:
            samples.append(
                {
                    "event_id": str(event.get("id", "")),
                    "datetime": event.get("datetime", ""),
                    "name": event.get("name", ""),
                    "location_name": location_name,
                    "location_gps": gps,
                    "api_location_granularity": classified["granularity"],
                }
            )

    return {
        "available": True,
        "path": portable_path(raw_events_path),
        "event_count": len(raw_events),
        "cutoffs": dataset_cutoffs(cutoff_rows, "event_id", "datetime"),
        "with_location_gps": with_gps,
        "missing_location_gps": gps_missing,
        "invalid_location_gps": gps_invalid,
        "api_location_granularity_counts": dict(sorted(granularity_counts.items())),
        "samples": samples,
    }


def empty_stats() -> dict[str, int]:
    return {
        "total": 0,
        "summary": 0,
        "non_summary": 0,
        "api_municipality": 0,
        "api_county": 0,
        "api_unknown": 0,
        "title_municipality": 0,
        "title_county": 0,
        "title_unknown": 0,
        "prospective_derived_municipality": 0,
        "unresolved_municipality": 0,
        "cross_county_conflicts": 0,
        "api_municipality_title_mismatch_same_county": 0,
        "api_municipality_title_mismatch_different_county": 0,
        "api_municipality_title_county_same_county": 0,
        "api_municipality_title_county_different_county": 0,
        "api_county_title_municipality_conflict": 0,
    }


def apply_stat(stats: dict[str, int], record: dict[str, Any]) -> None:
    stats["total"] += 1
    if record["is_summary"]:
        stats["summary"] += 1
    else:
        stats["non_summary"] += 1

    stats[f"api_{record['api_location_granularity']}"] += 1
    stats[f"title_{record['title_suffix_granularity']}"] += 1
    if record["derived_municipality_code"]:
        stats["prospective_derived_municipality"] += 1
    else:
        stats["unresolved_municipality"] += 1
    if record["cross_county_conflict"]:
        stats["cross_county_conflicts"] += 1
    if record["api_municipality_title_mismatch_same_county"]:
        stats["api_municipality_title_mismatch_same_county"] += 1
    if record["api_municipality_title_mismatch_different_county"]:
        stats["api_municipality_title_mismatch_different_county"] += 1
    if record["api_municipality_title_county_same_county"]:
        stats["api_municipality_title_county_same_county"] += 1
    if record["api_municipality_title_county_different_county"]:
        stats["api_municipality_title_county_different_county"] += 1
    if record["api_county_title_municipality_conflict"]:
        stats["api_county_title_municipality_conflict"] += 1


def classify_records(events: list[dict[str, Any]], reference: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for event in events:
        raw = classify_name(event.get("location_name"), reference)
        suffix = final_title_suffix(event.get("name"))
        title = classify_name(suffix, reference)
        is_summary = str(event.get("type") or "").startswith("Sammanfattning")

        derived_municipality_code = None
        derived_municipality_name = None
        derived_county_code = None
        derived_county_name = None
        derivation_rule = "unresolved"
        validation_status = "not_applicable"
        cross_county_conflict = False
        api_county_title_municipality_conflict = False
        api_municipality_title_mismatch_same_county = False
        api_municipality_title_mismatch_different_county = False
        api_municipality_title_county_same_county = False
        api_municipality_title_county_different_county = False

        if raw["granularity"] == "municipality":
            raw_county_code = raw["county_code"]
            raw_county_name = raw["county_name"]
            if title["granularity"] == "municipality" and title["code"] != raw["code"]:
                if title["county_code"] == raw_county_code:
                    api_municipality_title_mismatch_same_county = True
                    validation_status = "api_municipality_title_mismatch_same_county"
                    # Leave unresolved: two municipality signals disagree.
                    derived_county_code = raw_county_code
                    derived_county_name = raw_county_name
                else:
                    api_municipality_title_mismatch_different_county = True
                    cross_county_conflict = True
                    validation_status = "api_municipality_title_mismatch_different_county"
            elif title["granularity"] == "county":
                if title["code"] == raw_county_code:
                    api_municipality_title_county_same_county = True
                    derived_municipality_code = raw["code"]
                    derived_municipality_name = raw["name"]
                    derived_county_code = raw_county_code
                    derived_county_name = raw_county_name
                    derivation_rule = "api_location_municipality"
                    validation_status = "accepted_api_municipality_title_county_same_county"
                else:
                    api_municipality_title_county_different_county = True
                    cross_county_conflict = True
                    validation_status = "api_municipality_title_county_conflict"
                    # Leave unresolved: municipality and title county contradict.
            else:
                derived_municipality_code = raw["code"]
                derived_municipality_name = raw["name"]
                derived_county_code = raw_county_code
                derived_county_name = raw_county_name
                derivation_rule = "api_location_municipality"
                validation_status = "accepted_api_municipality"

        elif raw["granularity"] == "county":
            derived_county_code = raw["code"]
            derived_county_name = raw["name"]
            if title["granularity"] == "municipality":
                if title["county_code"] == raw["code"]:
                    derived_municipality_code = title["code"]
                    derived_municipality_name = title["name"]
                    derived_county_code = title["county_code"]
                    derived_county_name = title["county_name"]
                    derivation_rule = "title_municipality_validated_by_api_county"
                    validation_status = "accepted_title_municipality_within_api_county"
                else:
                    api_county_title_municipality_conflict = True
                    cross_county_conflict = True
                    validation_status = "api_county_title_municipality_conflict"
                    derived_county_code = None
                    derived_county_name = None
            elif title["granularity"] == "county":
                validation_status = (
                    "title_county_matches_api_county"
                    if title["code"] == raw["code"]
                    else "title_county_differs_from_api_county"
                )
                if title["code"] != raw["code"]:
                    cross_county_conflict = True
                    derived_county_code = None
                    derived_county_name = None
            else:
                validation_status = "api_county_no_title_municipality"

        else:  # raw API location unknown
            if title["granularity"] == "municipality":
                derived_municipality_code = title["code"]
                derived_municipality_name = title["name"]
                derived_county_code = title["county_code"]
                derived_county_name = title["county_name"]
                derivation_rule = "title_municipality_without_api_admin_match"
                validation_status = "accepted_title_municipality_api_unknown"
            elif title["granularity"] == "county":
                derived_county_code = title["code"]
                derived_county_name = title["name"]
                validation_status = "accepted_title_county_api_unknown"
            else:
                validation_status = "unresolved_api_and_title_unknown"

        record = {
            "event_id": str(event.get("event_id") or ""),
            "datetime": event.get("datetime") or "",
            "month": month_key(event.get("datetime")),
            "quarter": quarter_key(event.get("datetime")),
            "name": event.get("name") or "",
            "summary": event.get("summary") or "",
            "url": event.get("url") or "",
            "type": event.get("type") or "",
            "is_summary": is_summary,
            "api_location_name": event.get("location_name") or "",
            "api_location_granularity": raw["granularity"],
            "api_location_code": raw.get("code"),
            "api_location_county_code": raw.get("county_code") or (raw.get("code") if raw["granularity"] == "county" else None),
            "title_suffix": suffix,
            "title_suffix_granularity": title["granularity"],
            "title_suffix_code": title.get("code"),
            "title_suffix_county_code": title.get("county_code") or (title.get("code") if title["granularity"] == "county" else None),
            "derived_municipality_code": derived_municipality_code,
            "derived_municipality_name": derived_municipality_name,
            "derived_county_code": derived_county_code,
            "derived_county_name": derived_county_name,
            "derivation_rule": derivation_rule,
            "validation_status": validation_status,
            "cross_county_conflict": cross_county_conflict,
            "api_county_title_municipality_conflict": api_county_title_municipality_conflict,
            "api_municipality_title_mismatch_same_county": api_municipality_title_mismatch_same_county,
            "api_municipality_title_mismatch_different_county": api_municipality_title_mismatch_different_county,
            "api_municipality_title_county_same_county": api_municipality_title_county_same_county,
            "api_municipality_title_county_different_county": api_municipality_title_county_different_county,
        }
        records.append(record)

    return records


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    overall = empty_stats()
    by_summary = {"summary": empty_stats(), "non_summary": empty_stats()}
    by_quarter: dict[str, dict[str, int]] = defaultdict(empty_stats)
    by_month: dict[str, dict[str, int]] = defaultdict(empty_stats)
    by_event_type: dict[str, dict[str, int]] = defaultdict(empty_stats)
    cross_api_title: Counter[tuple[str, str]] = Counter()
    derivation_rules: Counter[str] = Counter()
    validation_statuses: Counter[str] = Counter()
    raw_unknown_names: Counter[str] = Counter()
    title_unknown_suffixes: Counter[str] = Counter()
    title_county_suffixes: Counter[str] = Counter()

    for record in records:
        apply_stat(overall, record)
        apply_stat(by_summary["summary" if record["is_summary"] else "non_summary"], record)
        apply_stat(by_quarter[record["quarter"]], record)
        apply_stat(by_month[record["month"]], record)
        apply_stat(by_event_type[record["type"] or "(missing)"], record)
        cross_api_title[(record["api_location_granularity"], record["title_suffix_granularity"])] += 1
        derivation_rules[record["derivation_rule"]] += 1
        validation_statuses[record["validation_status"]] += 1
        if record["api_location_granularity"] == "unknown":
            raw_unknown_names[record["api_location_name"] or "(empty)"] += 1
        if record["title_suffix_granularity"] == "unknown":
            title_unknown_suffixes[record["title_suffix"] or "(empty/no comma)"] += 1
        if record["title_suffix_granularity"] == "county":
            title_county_suffixes[record["title_suffix"]] += 1

    return {
        "overall": overall,
        "summary_split": by_summary,
        "by_quarter": dict(sorted(by_quarter.items())),
        "by_month": dict(sorted(by_month.items())),
        "by_event_type": dict(sorted(by_event_type.items(), key=lambda kv: (-kv[1]["total"], kv[0]))),
        "cross_api_title": {f"api={api}|title={title}": count for (api, title), count in sorted(cross_api_title.items())},
        "derivation_rules": dict(sorted(derivation_rules.items())),
        "validation_statuses": dict(sorted(validation_statuses.items())),
        "raw_unknown_names": dict(raw_unknown_names.most_common()),
        "title_unknown_suffixes": dict(title_unknown_suffixes.most_common()),
        "title_county_suffixes": dict(title_county_suffixes.most_common()),
    }


def choose_examples(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    def sort_key(record: dict[str, Any]) -> tuple[str, int, str]:
        event_id = record.get("event_id") or "0"
        try:
            event_num = int(event_id)
        except ValueError:
            event_num = 0
        return (record.get("datetime") or "", event_num, event_id)

    sorted_records = sorted(records, key=sort_key)

    def slim(record: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "event_id",
            "datetime",
            "type",
            "is_summary",
            "api_location_name",
            "api_location_granularity",
            "title_suffix",
            "title_suffix_granularity",
            "derived_municipality_code",
            "derived_municipality_name",
            "validation_status",
            "name",
        ]
        return {key: record.get(key) for key in keys}

    unresolved = [r for r in sorted_records if not r["derived_municipality_code"]]
    selected_unresolved: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in unresolved:
        bucket = (
            f"{'summary' if record['is_summary'] else 'non_summary'}|"
            f"api={record['api_location_granularity']}|title={record['title_suffix_granularity']}|"
            f"validation={record['validation_status']}"
        )
        grouped[bucket].append(record)

    # Round-robin first examples from each unresolved bucket, then fill to 30.
    for bucket in sorted(grouped):
        for record in grouped[bucket][:5]:
            if record["event_id"] not in seen_ids:
                selected_unresolved.append(record)
                seen_ids.add(record["event_id"])
            if len(selected_unresolved) >= 30:
                break
        if len(selected_unresolved) >= 30:
            break
    if len(selected_unresolved) < 30:
        for record in unresolved:
            if record["event_id"] not in seen_ids:
                selected_unresolved.append(record)
                seen_ids.add(record["event_id"])
            if len(selected_unresolved) >= 30:
                break

    cross_county_conflicts = [r for r in sorted_records if r["cross_county_conflict"]]
    same_county_mismatches = [r for r in sorted_records if r["api_municipality_title_mismatch_same_county"]]
    accepted_county_title_municipality = [
        r for r in sorted_records if r["derivation_rule"] == "title_municipality_validated_by_api_county"
    ]

    return {
        "unresolved_examples": [slim(r) for r in selected_unresolved],
        "cross_county_conflict_examples": [slim(r) for r in cross_county_conflicts[:20]],
        "same_county_mismatch_examples": [slim(r) for r in same_county_mismatches[:20]],
        "accepted_county_title_municipality_examples": [slim(r) for r in accepted_county_title_municipality[:20]],
    }


def top_rows(stats_by_key: dict[str, dict[str, int]], limit: int | None = None) -> list[tuple[str, dict[str, int]]]:
    rows = sorted(stats_by_key.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    return rows if limit is None else rows[:limit]


def render_stats_row(label: str, stats: dict[str, int]) -> list[Any]:
    total = stats["total"]
    return [
        label,
        f"{total:,}",
        compact_count(stats["api_municipality"], total),
        compact_count(stats["api_county"], total),
        compact_count(stats["title_municipality"], total),
        compact_count(stats["title_county"], total),
        compact_count(stats["prospective_derived_municipality"], total),
        compact_count(stats["unresolved_municipality"], total),
        f"{stats['cross_county_conflicts']:,}",
    ]


def render_markdown(
    inputs: dict[str, str],
    input_provenance: dict[str, dict[str, Any]],
    data_cutoffs: dict[str, Any],
    reference: dict[str, Any],
    raw_window: dict[str, Any],
    aggregate_result: dict[str, Any],
    examples: dict[str, list[dict[str, Any]]],
) -> str:
    overall = aggregate_result["overall"]
    checks = reference["checks"]

    lines: list[str] = []
    lines.append("# Geography Contract Profile (generated)\n")
    lines.append("This generated profile is deterministic and uses exact normalized name matching only. It does not parse body/html text, geocode, or use fuzzy aliases.\n")

    lines.append("## Regeneration\n")
    lines.append("```bash")
    lines.append("uv run scripts/research/profile_geography_contract.py \\")
    lines.append(f"  --events-parquet {inputs['events_parquet']} \\")
    lines.append(f"  --boundaries {inputs['boundaries']} \\")
    lines.append(f"  --population {inputs['population']} \\")
    lines.append(f"  --raw-events-json {inputs['raw_events_json']} \\")
    lines.append(f"  --output-dir {inputs['output_dir']} \\")
    lines.append("  --strict-provenance")
    lines.append("```\n")

    lines.append("## Inputs\n")
    lines.append(markdown_table(["Input", "Path"], [[key, value] for key, value in inputs.items()]))

    lines.append("## Input provenance\n")
    lines.append(
        "Git repo/head/path status are diagnostic only; strict comparison validates path, existence, size, "
        "SHA-256, script metadata, cutoffs, and deterministic outputs.\n"
    )
    provenance_rows = []
    for key, metadata in input_provenance.items():
        git = metadata.get("git") or {}
        provenance_rows.append(
            [
                key,
                metadata.get("path", ""),
                metadata.get("size_bytes") if metadata.get("size_bytes") is not None else "",
                (metadata.get("sha256") or "")[:16] + ("…" if metadata.get("sha256") else ""),
                git.get("root", ""),
                (git.get("head") or "")[:12],
                "yes" if git.get("tracked_dirty") else "no",
                git.get("path_status", ""),
            ]
        )
    lines.append(
        markdown_table(
            ["Input", "Path", "Size bytes", "SHA-256", "Git repo", "Git HEAD", "Path dirty?", "Path status"],
            provenance_rows,
        )
    )

    lines.append("## Data cutoffs\n")
    cutoff_rows = []
    for key, cutoff in data_cutoffs.items():
        cutoff = cutoff or {}
        cutoff_rows.append(
            [
                key,
                cutoff.get("row_count", ""),
                cutoff.get("min_datetime", ""),
                cutoff.get("max_datetime", ""),
                cutoff.get("min_event_id", ""),
                cutoff.get("max_event_id", ""),
            ]
        )
    lines.append(markdown_table(["Input", "Rows", "Min datetime", "Max datetime", "Min event id", "Max event id"], cutoff_rows))

    lines.append("## Reference checks\n")
    lines.append(
        markdown_table(
            ["Check", "Value"],
            [
                ["Municipality features in boundaries.geojson", checks["boundaries_feature_count"]],
                ["Municipality rows in population.csv", checks["population_row_count"]],
                ["County codes/names in static table", checks["county_count"]],
                ["Boundary county codes", ", ".join(checks["boundary_county_codes"])],
                ["Boundary codes missing in population", len(checks["boundary_codes_missing_in_population"])],
                ["Population codes missing in boundaries", len(checks["population_codes_missing_in_boundaries"])],
                ["County codes missing in static table", len(checks["county_codes_missing_in_static_table"])],
                ["Static county codes unused by boundaries", len(checks["static_county_codes_not_used_by_boundaries"])],
                ["Municipality code prefix / county-code mismatches", len(checks["municipality_code_prefix_mismatches"])],
                ["Boundary/population normalized name mismatches", len(checks["boundary_population_name_mismatches_normalized"])],
                ["Duplicate normalized municipality names", len(checks["duplicate_normalized_municipality_names"])],
                ["Duplicate normalized county names", len(checks["duplicate_normalized_county_names"])],
                ["Municipality/county normalized name collisions", len(checks["municipality_county_name_collisions"])],
            ],
        )
    )

    lines.append("## County table provenance\n")
    lines.append(markdown_table(["Field", "Value"], [[key, value] for key, value in COUNTY_TABLE_PROVENANCE.items()]))

    lines.append("## Overall coverage\n")
    lines.append(
        markdown_table(
            [
                "Scope",
                "Rows",
                "API municipality",
                "API county",
                "Title municipality",
                "Title county",
                "Prospective derived municipality",
                "Unresolved municipality",
                "Cross-county conflicts",
            ],
            [
                render_stats_row("All events", overall),
                render_stats_row("Non-summary", aggregate_result["summary_split"]["non_summary"]),
                render_stats_row("Summary", aggregate_result["summary_split"]["summary"]),
            ],
        )
    )

    lines.append("## Raw API location vs title suffix\n")
    lines.append(
        markdown_table(
            ["API granularity", "Title suffix granularity", "Rows"],
            [
                [key.split("|title=")[0].replace("api=", ""), key.split("|title=")[1], f"{value:,}"]
                for key, value in aggregate_result["cross_api_title"].items()
            ],
        )
    )

    lines.append("## Prospective derivation rules\n")
    lines.append(
        markdown_table(
            ["Rule", "Rows"],
            [[key, f"{value:,}"] for key, value in aggregate_result["derivation_rules"].items()],
        )
    )

    lines.append("## Validation statuses\n")
    lines.append(
        markdown_table(
            ["Status", "Rows"],
            [[key, f"{value:,}"] for key, value in aggregate_result["validation_statuses"].items()],
        )
    )

    lines.append("## Coverage by quarter\n")
    quarter_rows = [render_stats_row(key, stats) for key, stats in aggregate_result["by_quarter"].items()]
    lines.append(
        markdown_table(
            [
                "Quarter",
                "Rows",
                "API municipality",
                "API county",
                "Title municipality",
                "Title county",
                "Prospective derived municipality",
                "Unresolved municipality",
                "Cross-county conflicts",
            ],
            quarter_rows,
        )
    )

    lines.append("## Coverage by recent month\n")
    recent_month_rows = [
        render_stats_row(key, stats)
        for key, stats in sorted(aggregate_result["by_month"].items(), reverse=True)[:18]
    ]
    lines.append(
        markdown_table(
            [
                "Month",
                "Rows",
                "API municipality",
                "API county",
                "Title municipality",
                "Title county",
                "Prospective derived municipality",
                "Unresolved municipality",
                "Cross-county conflicts",
            ],
            recent_month_rows,
        )
    )

    lines.append("## Coverage by top event types\n")
    event_type_rows = []
    for event_type, stats in top_rows(aggregate_result["by_event_type"], limit=30):
        total = stats["total"]
        event_type_rows.append(
            [
                truncate(event_type, 48),
                f"{total:,}",
                compact_count(stats["api_county"], total),
                compact_count(stats["title_municipality"], total),
                compact_count(stats["prospective_derived_municipality"], total),
                compact_count(stats["unresolved_municipality"], total),
                f"{stats['cross_county_conflicts']:,}",
            ]
        )
    lines.append(
        markdown_table(
            [
                "Event type",
                "Rows",
                "API county",
                "Title municipality",
                "Prospective derived municipality",
                "Unresolved municipality",
                "Cross-county conflicts",
            ],
            event_type_rows,
        )
    )

    lines.append("## Unmatched names/suffixes\n")
    lines.append("### Raw API location_name classified as unknown\n")
    lines.append(
        markdown_table(
            ["Raw API location_name", "Rows"],
            [[name, f"{count:,}"] for name, count in list(aggregate_result["raw_unknown_names"].items())[:50]],
        )
    )
    lines.append("### Title suffix classified as unknown (top 50)\n")
    lines.append(
        markdown_table(
            ["Title suffix", "Rows"],
            [[truncate(name, 80), f"{count:,}"] for name, count in list(aggregate_result["title_unknown_suffixes"].items())[:50]],
        )
    )
    lines.append("### Title suffix classified as county\n")
    lines.append(
        markdown_table(
            ["Title county suffix", "Rows"],
            [[name, f"{count:,}"] for name, count in aggregate_result["title_county_suffixes"].items()],
        )
    )

    if raw_window.get("available"):
        lines.append("## Current raw events.json GPS check\n")
        lines.append(
            markdown_table(
                ["Metric", "Value"],
                [
                    ["Raw events in current API window", f"{raw_window['event_count']:,}"],
                    ["Rows with location.gps", f"{raw_window['with_location_gps']:,}"],
                    ["Rows missing location.gps", f"{raw_window['missing_location_gps']:,}"],
                    ["Rows with invalid/out-of-bounds location.gps", f"{raw_window['invalid_location_gps']:,}"],
                    ["API granularity counts", json.dumps(raw_window["api_location_granularity_counts"], ensure_ascii=False, sort_keys=True)],
                ],
            )
        )
        lines.append(
            "This checks only the current raw API window. Full-history raw `location.gps` validity is not proven by "
            "the v1 parquet because that export preserved only parsed latitude/longitude; Phase 2 must preserve "
            "the raw GPS string directly while flattening raw JSON.\n"
        )

    lines.append("## Raw-county rows assigned by title municipality + API-county validation\n")
    lines.append(example_table(examples["accepted_county_title_municipality_examples"]))

    lines.append("## Unresolved examples\n")
    lines.append(example_table(examples["unresolved_examples"]))

    lines.append("## Cross-county conflict examples\n")
    lines.append(example_table(examples["cross_county_conflict_examples"]))

    lines.append("## Same-county API/title municipality mismatch examples\n")
    lines.append(example_table(examples["same_county_mismatch_examples"]))

    return "\n".join(lines)


def example_table(examples: list[dict[str, Any]]) -> str:
    rows = []
    for example in examples:
        rows.append(
            [
                example.get("event_id"),
                truncate(example.get("datetime"), 24),
                truncate(example.get("type"), 32),
                "yes" if example.get("is_summary") else "no",
                example.get("api_location_name"),
                example.get("api_location_granularity"),
                truncate(example.get("title_suffix"), 32),
                example.get("title_suffix_granularity"),
                example.get("derived_municipality_code") or "",
                truncate(example.get("validation_status"), 44),
                truncate(example.get("name"), 80),
            ]
        )
    return markdown_table(
        [
            "event_id",
            "datetime",
            "type",
            "summary?",
            "api_location",
            "api granularity",
            "title suffix",
            "title granularity",
            "derived kommun",
            "validation",
            "name",
        ],
        rows,
    )


def self_check_reference() -> dict[str, Any]:
    counties = (
        {"code": "01", "name": "One län"},
        {"code": "02", "name": "Two län"},
    )
    municipalities = [
        {"code": "0101", "name": "Alpha", "county_code": "01", "county_name": "One län"},
        {"code": "0102", "name": "Beta", "county_code": "01", "county_name": "One län"},
        {"code": "0201", "name": "Gamma", "county_code": "02", "county_name": "Two län"},
    ]
    return {
        "municipalities": municipalities,
        "counties": list(counties),
        "municipality_by_norm": {normalize_name(item["name"]): item for item in municipalities},
        "municipality_by_code": {item["code"]: item for item in municipalities},
        "county_by_norm": {normalize_name(item["name"]): dict(item) for item in counties},
        "county_by_code": {item["code"]: dict(item) for item in counties},
        "checks": {},
    }


def synthetic_event(raw_location: str, title_suffix: str) -> dict[str, Any]:
    return {
        "event_id": "1",
        "datetime": "2026-01-01 00:00:00 +01:00",
        "name": f"1 januari 00.00, Test, {title_suffix}",
        "summary": "",
        "url": "",
        "type": "Test",
        "location_name": raw_location,
        "latitude": None,
        "longitude": None,
    }


def run_self_checks() -> int:
    """Table-driven checks for the API-granularity/title-granularity matrix."""

    reference = self_check_reference()
    cases = [
        {
            "name": "api municipality + same title municipality",
            "raw": "Alpha",
            "suffix": "Alpha",
            "derived_municipality": "0101",
            "derived_county": "01",
            "status": "accepted_api_municipality",
            "conflict": False,
        },
        {
            "name": "api municipality + different title municipality in same county",
            "raw": "Alpha",
            "suffix": "Beta",
            "derived_municipality": None,
            "derived_county": "01",
            "status": "api_municipality_title_mismatch_same_county",
            "conflict": False,
        },
        {
            "name": "api municipality + title municipality in different county",
            "raw": "Alpha",
            "suffix": "Gamma",
            "derived_municipality": None,
            "derived_county": None,
            "status": "api_municipality_title_mismatch_different_county",
            "conflict": True,
        },
        {
            "name": "api municipality + same title county",
            "raw": "Alpha",
            "suffix": "One län",
            "derived_municipality": "0101",
            "derived_county": "01",
            "status": "accepted_api_municipality_title_county_same_county",
            "conflict": False,
        },
        {
            "name": "api municipality + different title county",
            "raw": "Alpha",
            "suffix": "Two län",
            "derived_municipality": None,
            "derived_county": None,
            "status": "api_municipality_title_county_conflict",
            "conflict": True,
        },
        {
            "name": "api municipality + unknown title suffix",
            "raw": "Alpha",
            "suffix": "Nowhere",
            "derived_municipality": "0101",
            "derived_county": "01",
            "status": "accepted_api_municipality",
            "conflict": False,
        },
        {
            "name": "api county + title municipality in same county",
            "raw": "One län",
            "suffix": "Alpha",
            "derived_municipality": "0101",
            "derived_county": "01",
            "status": "accepted_title_municipality_within_api_county",
            "conflict": False,
        },
        {
            "name": "api county + title municipality in different county",
            "raw": "One län",
            "suffix": "Gamma",
            "derived_municipality": None,
            "derived_county": None,
            "status": "api_county_title_municipality_conflict",
            "conflict": True,
        },
        {
            "name": "api county + same title county",
            "raw": "One län",
            "suffix": "One län",
            "derived_municipality": None,
            "derived_county": "01",
            "status": "title_county_matches_api_county",
            "conflict": False,
        },
        {
            "name": "api county + different title county",
            "raw": "One län",
            "suffix": "Two län",
            "derived_municipality": None,
            "derived_county": None,
            "status": "title_county_differs_from_api_county",
            "conflict": True,
        },
        {
            "name": "api county + unknown title suffix",
            "raw": "One län",
            "suffix": "Nowhere",
            "derived_municipality": None,
            "derived_county": "01",
            "status": "api_county_no_title_municipality",
            "conflict": False,
        },
        {
            "name": "api unknown + title municipality",
            "raw": "Mystery",
            "suffix": "Alpha",
            "derived_municipality": "0101",
            "derived_county": "01",
            "status": "accepted_title_municipality_api_unknown",
            "conflict": False,
        },
        {
            "name": "api unknown + title county",
            "raw": "Mystery",
            "suffix": "One län",
            "derived_municipality": None,
            "derived_county": "01",
            "status": "accepted_title_county_api_unknown",
            "conflict": False,
        },
        {
            "name": "api unknown + unknown title suffix",
            "raw": "Mystery",
            "suffix": "Nowhere",
            "derived_municipality": None,
            "derived_county": None,
            "status": "unresolved_api_and_title_unknown",
            "conflict": False,
        },
    ]

    for case in cases:
        record = classify_records([synthetic_event(case["raw"], case["suffix"])], reference)[0]
        actual = {
            "derived_municipality": record["derived_municipality_code"],
            "derived_county": record["derived_county_code"],
            "status": record["validation_status"],
            "conflict": record["cross_county_conflict"],
        }
        expected = {
            "derived_municipality": case["derived_municipality"],
            "derived_county": case["derived_county"],
            "status": case["status"],
            "conflict": case["conflict"],
        }
        if actual != expected:
            raise AssertionError(f"self-check failed for {case['name']}: expected {expected}, got {actual}")

    return len(cases)


def build_manifest(json_summary: dict[str, Any]) -> dict[str, Any]:
    profile = json_summary["profile"]
    return {
        "schema_version": 2,
        "generated_by": json_summary["run_context"]["script"],
        "script": json_summary["run_context"]["script_metadata"],
        "strict_provenance_policy": STRICT_PROVENANCE_POLICY,
        "inputs": json_summary["inputs"],
        "input_provenance": json_summary["input_provenance"],
        "data_cutoffs": json_summary["data_cutoffs"],
        "reference_checks": json_summary["reference"]["checks"],
        "current_raw_events_window": {
            "event_count": json_summary["current_raw_events_window"].get("event_count"),
            "with_location_gps": json_summary["current_raw_events_window"].get("with_location_gps"),
            "missing_location_gps": json_summary["current_raw_events_window"].get("missing_location_gps"),
            "invalid_location_gps": json_summary["current_raw_events_window"].get("invalid_location_gps"),
            "api_location_granularity_counts": json_summary["current_raw_events_window"].get(
                "api_location_granularity_counts"
            ),
        },
        "profile": {
            "overall": profile["overall"],
            "summary_split": profile["summary_split"],
            "cross_api_title": profile["cross_api_title"],
            "derivation_rules": profile["derivation_rules"],
            "validation_statuses": profile["validation_statuses"],
        },
    }


def compare_manifest(current: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []

    def add_value_mismatch(path: str, current_value: Any, expected_value: Any) -> None:
        mismatches.append(f"{path} differs: expected {expected_value!r}, got {current_value!r}")

    def compare_exact(path: str) -> None:
        current_value = current.get(path)
        expected_value = expected.get(path)
        if current_value != expected_value:
            add_value_mismatch(path, current_value, expected_value)

    for field in ("schema_version", "generated_by", "script", "strict_provenance_policy"):
        compare_exact(field)

    data_input_keys = ("events_parquet", "boundaries", "population", "raw_events_json")
    current_inputs = current.get("inputs", {})
    expected_inputs = expected.get("inputs", {})
    for input_name in data_input_keys:
        if current_inputs.get(input_name) != expected_inputs.get(input_name):
            add_value_mismatch(
                f"inputs.{input_name}",
                current_inputs.get(input_name),
                expected_inputs.get(input_name),
            )

    current_provenance = current.get("input_provenance", {})
    expected_provenance = expected.get("input_provenance", {})
    current_keys = set(current_provenance)
    expected_keys = set(expected_provenance)
    if current_keys != expected_keys:
        mismatches.append(
            "input provenance keys differ: "
            f"expected {sorted(expected_keys)!r}, got {sorted(current_keys)!r}"
        )

    for input_name in sorted(current_keys | expected_keys):
        metadata = current_provenance.get(input_name)
        expected_metadata = expected_provenance.get(input_name)
        if metadata is None or expected_metadata is None:
            continue
        for field in ("path", "exists", "size_bytes", "sha256"):
            if metadata.get(field) != expected_metadata.get(field):
                add_value_mismatch(
                    f"input_provenance.{input_name}.{field}",
                    metadata.get(field),
                    expected_metadata.get(field),
                )

    current_cutoffs = current.get("data_cutoffs", {})
    expected_cutoffs = expected.get("data_cutoffs", {})
    current_cutoff_keys = set(current_cutoffs)
    expected_cutoff_keys = set(expected_cutoffs)
    if current_cutoff_keys != expected_cutoff_keys:
        mismatches.append(
            "data cutoff keys differ: "
            f"expected {sorted(expected_cutoff_keys)!r}, got {sorted(current_cutoff_keys)!r}"
        )
    for cutoff_name in sorted(current_cutoff_keys | expected_cutoff_keys):
        cutoff = current_cutoffs.get(cutoff_name)
        expected_cutoff = expected_cutoffs.get(cutoff_name)
        if cutoff != expected_cutoff:
            add_value_mismatch(f"data_cutoffs.{cutoff_name}", cutoff, expected_cutoff)

    for field in ("reference_checks", "current_raw_events_window"):
        if current.get(field) != expected.get(field):
            add_value_mismatch(field, current.get(field), expected.get(field))

    for field in ("overall", "summary_split", "cross_api_title", "derivation_rules", "validation_statuses"):
        current_value = current.get("profile", {}).get(field)
        expected_value = expected.get("profile", {}).get(field)
        if current_value != expected_value:
            add_value_mismatch(f"profile.{field}", current_value, expected_value)

    return mismatches


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def warn_or_fail_manifest_mismatches(path: Path, mismatches: list[str], strict: bool) -> int:
    if not mismatches:
        print(
            f"manifest check: {portable_path(path)} matches strict content/script/profile invariants "
            "(git metadata diagnostic only)"
        )
        return 0

    print(
        f"WARNING: current inputs/results differ from expected manifest {portable_path(path)}.",
        file=sys.stderr,
    )
    print(
        "Strict comparison validates pinned content hashes, script metadata, cutoffs, reference checks, "
        "raw-window metrics, and headline profile metrics; recorded git metadata is diagnostic only.",
        file=sys.stderr,
    )
    print("This usually means the moving data snapshot changed; update the research report/manifest if intentional.", file=sys.stderr)
    for mismatch in mismatches:
        print(f"- {mismatch}", file=sys.stderr)
    return 1 if strict else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-parquet", type=Path, default=DEFAULT_EVENTS_PARQUET)
    parser.add_argument("--boundaries", type=Path, default=DEFAULT_BOUNDARIES)
    parser.add_argument("--population", type=Path, default=DEFAULT_POPULATION)
    parser.add_argument("--raw-events-json", type=Path, default=DEFAULT_RAW_EVENTS_JSON)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    parser.add_argument("--manifest-out", type=Path, default=None)
    parser.add_argument("--expected-manifest", type=Path, default=DEFAULT_EXPECTED_MANIFEST)
    parser.add_argument("--no-expected-manifest", action="store_true")
    parser.add_argument(
        "--strict-provenance",
        action="store_true",
        help="Exit non-zero on expected-manifest invariant mismatches; recorded git metadata is diagnostic only",
    )
    parser.add_argument("--self-check", action="store_true", help="Run table-driven classification self-checks and exit")
    args = parser.parse_args()

    self_check_count = run_self_checks()
    if args.self_check:
        print(f"self-checks: {self_check_count} passed")
        return 0

    output_dir = args.output_dir
    json_out = args.json_out or output_dir / "summary.json"
    markdown_out = args.markdown_out or output_dir / "summary.md"

    reference = load_reference(args.boundaries, args.population)
    events = load_events(args.events_parquet)
    records = classify_records(events, reference)
    aggregate_result = aggregate(records)
    examples = choose_examples(records)
    raw_window = load_raw_events_window(args.raw_events_json, reference)
    data_cutoffs = {
        "full_export_parquet": dataset_cutoffs(events, "event_id", "datetime"),
        "current_raw_events_window": raw_window.get("cutoffs") if raw_window.get("available") else None,
    }

    inputs = {
        "events_parquet": portable_path(args.events_parquet),
        "boundaries": portable_path(args.boundaries),
        "population": portable_path(args.population),
        "raw_events_json": portable_path(args.raw_events_json),
        "output_dir": portable_path(output_dir),
    }
    input_provenance = build_input_provenance(
        {
            "events_parquet": args.events_parquet,
            "boundaries": args.boundaries,
            "population": args.population,
            "raw_events_json": args.raw_events_json,
        }
    )

    script_path = Path(__file__).resolve()
    script_metadata = {
        "path": portable_path(script_path),
        "version": SCRIPT_VERSION,
        "sha256": sha256_file(script_path),
    }

    json_summary = {
        "inputs": inputs,
        "input_provenance": input_provenance,
        "data_cutoffs": data_cutoffs,
        "run_context": {
            "script": script_metadata["path"],
            "script_version": SCRIPT_VERSION,
            "script_sha256": script_metadata["sha256"],
            "script_metadata": script_metadata,
            "repo_root": portable_path(REPO_ROOT),
            "cwd": portable_path(Path.cwd()),
        },
        "algorithm": {
            "normalization": "strip/collapse whitespace + Unicode casefold; exact match only",
            "title_parsing": "suffix after final comma in event.name",
            "body_or_html_parsing": False,
            "fuzzy_aliases": False,
        },
        "reference": {
            "checks": reference["checks"],
            "counties": reference["counties"],
            "county_table_provenance": COUNTY_TABLE_PROVENANCE,
        },
        "current_raw_events_window": raw_window,
        "profile": aggregate_result,
        "examples": examples,
    }

    markdown = render_markdown(inputs, input_provenance, data_cutoffs, reference, raw_window, aggregate_result, examples)
    manifest = build_manifest(json_summary)

    expected_manifest_path = None if args.no_expected_manifest else args.expected_manifest
    manifest_status = 0
    if expected_manifest_path and expected_manifest_path.exists():
        expected_manifest = load_manifest(expected_manifest_path)
        if expected_manifest is not None:
            manifest_status = warn_or_fail_manifest_mismatches(
                expected_manifest_path,
                compare_manifest(manifest, expected_manifest),
                args.strict_provenance,
            )
    elif expected_manifest_path:
        message = f"expected manifest {portable_path(expected_manifest_path)} does not exist"
        if args.strict_provenance:
            print(f"ERROR: {message}; strict provenance requires it.", file=sys.stderr)
            manifest_status = 1
        else:
            print(f"WARNING: {message}; skipping manifest comparison.", file=sys.stderr)

    output_dir.mkdir(parents=True, exist_ok=True)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(json_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_out.write_text(markdown, encoding="utf-8")
    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    overall = aggregate_result["overall"]
    print(f"events: {overall['total']:,}")
    print(f"api municipality: {overall['api_municipality']:,}")
    print(f"api county: {overall['api_county']:,}")
    print(f"title municipality: {overall['title_municipality']:,}")
    print(f"prospective derived municipality: {overall['prospective_derived_municipality']:,}")
    print(f"unresolved municipality: {overall['unresolved_municipality']:,}")
    print(f"cross-county conflicts: {overall['cross_county_conflicts']:,}")
    print(f"self-checks: {self_check_count} passed")
    print(f"wrote: {portable_path(json_out)}")
    print(f"wrote: {portable_path(markdown_out)}")
    if args.manifest_out:
        print(f"wrote: {portable_path(args.manifest_out)}")
    return manifest_status


if __name__ == "__main__":
    raise SystemExit(main())
