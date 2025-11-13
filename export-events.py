#!/usr/bin/env python3
"""
Export Swedish Police Events to Parquet/JSON with optional HTML enrichment.

This script extracts events from git history, enriches with HTML narrative data,
and exports to Parquet, JSON, or JSONL format for analysis and visualization.

Architecture:
1. Git Archaeology: Extract events.json from all commits
2. Deduplication: Keep most recent version of each event
3. GPS Parsing: Convert "lat,lon" strings to separate DOUBLE columns
4. HTML Enrichment: Extract structured narrative data (optional)
5. Date Filtering: Filter by event datetime (optional)
6. Export: Auto-detect format from file extension

Usage:
    uv run export-events.py --output events.parquet
    uv run export-events.py --output events.json --include-html
    uv run export-events.py --start-date 2024-01-01 --end-date 2024-12-31 --output 2024.parquet
"""

# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "lxml>=5.0.0",
#     "duckdb>=1.0.0",
#     "tqdm>=4.66.0",
# ]
# ///

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
from lxml import html as lxml_html
from tqdm import tqdm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def parse_gps_string(gps_str: str) -> tuple[float, float]:
    """
    Parse GPS string to (latitude, longitude) tuple.

    Input format: "62.63227,17.940871" (lat,lon)
    Output: (62.63227, 17.940871)

    Raises ValueError if format is invalid or coordinates out of Swedish bounds.
    """
    if not gps_str or ',' not in gps_str:
        raise ValueError(f"Invalid GPS format: {gps_str}")

    parts = gps_str.split(',')
    if len(parts) != 2:
        raise ValueError(f"Expected 2 coordinates, got {len(parts)}: {gps_str}")

    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError as e:
        raise ValueError(f"Could not parse coordinates: {gps_str}") from e

    # Validate Swedish bounds (permissive)
    if not (55.0 <= lat <= 70.0):
        raise ValueError(f"Latitude {lat} outside Swedish bounds (55-70°N)")
    if not (10.0 <= lon <= 25.0):
        raise ValueError(f"Longitude {lon} outside Swedish bounds (10-25°E)")

    return lat, lon


def extract_html_data(html_path: Path) -> dict:
    """
    Extract structured narrative data from HTML file.

    Returns dict with:
        - html_title: Event title (h1)
        - html_preamble: Summary paragraph
        - html_body: Full narrative (paragraphs joined with \n)
        - html_published_datetime: ISO 8601 timestamp (or None)
        - html_author: Author name
        - html_available: True
    """
    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    tree = lxml_html.fromstring(html_content)

    # Find main event content container
    event_div = tree.cssselect('.event-page.editorial-content')
    if not event_div:
        raise ValueError("Could not find .event-page.editorial-content in HTML")

    event_div = event_div[0]

    # Extract title
    title_elem = event_div.cssselect('h1')
    html_title = title_elem[0].text_content().strip() if title_elem else None

    # Extract preamble
    preamble_elem = event_div.cssselect('p.preamble')
    html_preamble = preamble_elem[0].text_content().strip() if preamble_elem else None

    # Extract body (all paragraphs in editorial-html, exclude boilerplate)
    body_div = event_div.cssselect('.text-body.editorial-html')
    if body_div:
        paragraphs = body_div[0].cssselect('p')
        html_body = '\n'.join(p.text_content().strip() for p in paragraphs)
    else:
        html_body = None

    # Extract publication datetime (may not exist in older files)
    time_elem = event_div.cssselect('time.date')
    if time_elem and time_elem[0].get('datetime'):
        html_published_datetime = time_elem[0].get('datetime')
    else:
        html_published_datetime = None

    # Extract author (usually "Polisen")
    author_elem = event_div.cssselect('.published-container span')
    if author_elem and len(author_elem) > 1:
        html_author = author_elem[1].text_content().strip()
    else:
        html_author = None

    return {
        'html_title': html_title,
        'html_preamble': html_preamble,
        'html_body': html_body,
        'html_published_datetime': html_published_datetime,
        'html_author': html_author,
        'html_available': True
    }


def get_all_commits() -> list[tuple[str, str]]:
    """
    Get all commits that modified events.json.

    Returns list of (commit_sha, commit_date) tuples, oldest first.
    """
    try:
        result = subprocess.run(
            ['git', 'log', '--format=%H|%aI', '--reverse', '--', 'events.json'],
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Git command failed: {e}")
        logger.error(f"Make sure you're in a git repository with events.json history")
        sys.exit(1)

    commits = []
    for line in result.stdout.strip().split('\n'):
        if '|' in line:
            sha, date = line.split('|', 1)
            commits.append((sha, date))

    return commits


def extract_events_from_commit(commit_sha: str) -> list[dict]:
    """
    Extract events from a specific git commit.

    Returns list of event dicts, or empty list if extraction fails.
    """
    try:
        result = subprocess.run(
            ['git', 'show', f'{commit_sha}:events.json'],
            capture_output=True,
            text=True,
            check=True
        )
        events = json.loads(result.stdout)
        return events if isinstance(events, list) else []
    except subprocess.CalledProcessError:
        logger.warning(f"Could not extract events.json from commit {commit_sha[:8]}")
        return []
    except json.JSONDecodeError:
        logger.warning(f"Invalid JSON in commit {commit_sha[:8]}")
        return []


def extract_all_events(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> dict[str, dict]:
    """
    Extract all events from git history with deduplication.

    Strategy: Keep LAST occurrence (most recent version) of each event.
    Optionally filter by event datetime.

    Returns dict mapping event_id -> event_data
    """
    commits = get_all_commits()
    logger.info(f"Found {len(commits)} commits modifying events.json")

    events_by_id = {}
    total_extracted = 0

    for commit_sha, commit_date in tqdm(commits, desc="Extracting from commits"):
        events = extract_events_from_commit(commit_sha)
        total_extracted += len(events)

        for event in events:
            event_id = str(event.get('id', ''))
            if not event_id:
                continue

            # Keep last occurrence (overwrites previous)
            events_by_id[event_id] = event

    logger.info(f"Total events extracted: {total_extracted:,}")
    logger.info(f"Unique events after deduplication: {len(events_by_id):,}")

    # Filter by date if specified
    if start_date or end_date:
        filtered = {}
        for event_id, event in events_by_id.items():
            event_datetime_str = event.get('datetime', '')
            if not event_datetime_str:
                continue

            try:
                # Parse datetime: "2025-11-13 20:14:24 +01:00"
                event_dt = datetime.fromisoformat(event_datetime_str)

                if start_date and event_dt < start_date:
                    continue
                if end_date and event_dt > end_date:
                    continue

                filtered[event_id] = event
            except ValueError:
                logger.warning(f"Could not parse datetime for event {event_id}: {event_datetime_str}")
                continue

        logger.info(f"Events after date filtering: {len(filtered):,}")
        return filtered

    return events_by_id


def enrich_with_html(
    events: dict[str, dict],
    html_dir: Path
) -> dict[str, dict]:
    """
    Enrich events with HTML narrative data.

    For each event, extracts structured fields from HTML file if available.
    Skips missing files with warnings, sets html_available=False.

    Returns updated events dict.
    """
    missing_count = 0
    processed_count = 0
    error_count = 0

    for event_id, event in tqdm(events.items(), desc="Enriching with HTML"):
        html_path = html_dir / f"{event_id}.html"

        if not html_path.exists():
            logger.warning(f"HTML file not found: {event_id}")
            missing_count += 1
            event['html_available'] = False
            event['html_title'] = None
            event['html_preamble'] = None
            event['html_body'] = None
            event['html_published_datetime'] = None
            event['html_author'] = None
        else:
            try:
                html_data = extract_html_data(html_path)
                event.update(html_data)
                processed_count += 1
            except Exception as e:
                logger.error(f"Failed to parse HTML for {event_id}: {e}")
                error_count += 1
                event['html_available'] = False
                event['html_title'] = None
                event['html_preamble'] = None
                event['html_body'] = None
                event['html_published_datetime'] = None
                event['html_author'] = None

    logger.info(f"HTML enrichment: {processed_count} processed, {missing_count} missing, {error_count} errors")

    return events


def flatten_event_for_export(event: dict) -> dict:
    """
    Flatten event structure for export.

    Converts:
        - location.name -> location_name
        - location.gps -> latitude, longitude (parsed)
        - id -> event_id (string)
    """
    flattened = {
        'event_id': str(event.get('id', '')),
        'datetime': event.get('datetime', ''),
        'name': event.get('name', ''),
        'summary': event.get('summary', ''),
        'url': event.get('url', ''),
        'type': event.get('type', ''),
    }

    # Parse location
    location = event.get('location', {})
    flattened['location_name'] = location.get('name', '')

    gps_str = location.get('gps', '')
    if gps_str:
        try:
            lat, lon = parse_gps_string(gps_str)
            flattened['latitude'] = lat
            flattened['longitude'] = lon
        except ValueError as e:
            logger.warning(f"Invalid GPS for event {flattened['event_id']}: {e}")
            flattened['latitude'] = None
            flattened['longitude'] = None
    else:
        flattened['latitude'] = None
        flattened['longitude'] = None

    # Add HTML fields if present
    if 'html_available' in event:
        flattened['html_title'] = event.get('html_title')
        flattened['html_preamble'] = event.get('html_preamble')
        flattened['html_body'] = event.get('html_body')
        flattened['html_published_datetime'] = event.get('html_published_datetime')
        flattened['html_author'] = event.get('html_author')
        flattened['html_available'] = event.get('html_available', False)

    return flattened


def export_to_parquet(events: list[dict], output_path: Path) -> None:
    """Export events to Parquet using DuckDB."""
    logger.info(f"Exporting to Parquet: {output_path}")

    conn = duckdb.connect(':memory:')

    # Create table from Python list
    conn.execute("CREATE TABLE events AS SELECT * FROM events")

    # Export to Parquet with compression
    conn.execute(f"COPY events TO '{output_path}' (FORMAT PARQUET, COMPRESSION 'zstd')")

    conn.close()

    file_size = output_path.stat().st_size
    logger.info(f"Exported {len(events):,} events to {output_path} ({file_size:,} bytes)")


def export_to_json(events: list[dict], output_path: Path) -> None:
    """Export events to JSON."""
    logger.info(f"Exporting to JSON: {output_path}")

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    file_size = output_path.stat().st_size
    logger.info(f"Exported {len(events):,} events to {output_path} ({file_size:,} bytes)")


def export_to_jsonl(events: list[dict], output_path: Path) -> None:
    """Export events to JSONL (one JSON object per line)."""
    logger.info(f"Exporting to JSONL: {output_path}")

    with open(output_path, 'w', encoding='utf-8') as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')

    file_size = output_path.stat().st_size
    logger.info(f"Exported {len(events):,} events to {output_path} ({file_size:,} bytes)")


def get_format_from_filename(output_path: Path) -> str:
    """Auto-detect export format from file extension."""
    ext = output_path.suffix.lower()

    if ext == '.parquet':
        return 'parquet'
    elif ext == '.json':
        return 'json'
    elif ext == '.jsonl':
        return 'jsonl'
    else:
        raise ValueError(
            f"Unknown file extension: {ext}\n"
            f"Supported formats: .parquet, .json, .jsonl"
        )


def main():
    parser = argparse.ArgumentParser(
        description='Export Swedish Police Events with optional HTML enrichment',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export all events to Parquet
  %(prog)s --output events.parquet

  # Export with HTML enrichment
  %(prog)s --output events.json --include-html

  # Export 2024 events only
  %(prog)s --start-date 2024-01-01 --end-date 2024-12-31 --output 2024.parquet

Format is auto-detected from file extension (.parquet, .json, .jsonl)
        """
    )

    parser.add_argument(
        '--output', '-o',
        type=Path,
        required=True,
        help='Output file path (.parquet, .json, or .jsonl)'
    )

    parser.add_argument(
        '--include-html',
        action='store_true',
        help='Enrich events with HTML narrative data from html/ directory'
    )

    parser.add_argument(
        '--html-dir',
        type=Path,
        default=Path('html'),
        help='Directory containing HTML files (default: html/)'
    )

    parser.add_argument(
        '--start-date',
        type=str,
        help='Filter events from this date (YYYY-MM-DD)'
    )

    parser.add_argument(
        '--end-date',
        type=str,
        help='Filter events until this date (YYYY-MM-DD)'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    # Configure logging level
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Parse dates
    start_date = None
    end_date = None

    if args.start_date:
        try:
            start_date = datetime.fromisoformat(args.start_date).replace(hour=0, minute=0, second=0)
            logger.info(f"Start date: {start_date.isoformat()}")
        except ValueError:
            logger.error(f"Invalid start date format: {args.start_date}. Use YYYY-MM-DD")
            sys.exit(1)

    if args.end_date:
        try:
            end_date = datetime.fromisoformat(args.end_date).replace(hour=23, minute=59, second=59)
            logger.info(f"End date: {end_date.isoformat()}")
        except ValueError:
            logger.error(f"Invalid end date format: {args.end_date}. Use YYYY-MM-DD")
            sys.exit(1)

    # Step 1: Git archaeology
    logger.info("Starting git archaeology...")
    events_by_id = extract_all_events(start_date, end_date)

    if not events_by_id:
        logger.error("No events found!")
        sys.exit(1)

    # Step 2: HTML enrichment (optional)
    if args.include_html:
        if not args.html_dir.exists():
            logger.error(f"HTML directory not found: {args.html_dir}")
            sys.exit(1)

        logger.info("Starting HTML enrichment...")
        events_by_id = enrich_with_html(events_by_id, args.html_dir)

    # Step 3: Flatten for export
    logger.info("Flattening events for export...")
    events_list = [flatten_event_for_export(e) for e in events_by_id.values()]

    # Step 4: Export
    output_format = get_format_from_filename(args.output)

    if output_format == 'parquet':
        export_to_parquet(events_list, args.output)
    elif output_format == 'json':
        export_to_json(events_list, args.output)
    elif output_format == 'jsonl':
        export_to_jsonl(events_list, args.output)

    logger.info("Export complete!")


if __name__ == '__main__':
    main()
