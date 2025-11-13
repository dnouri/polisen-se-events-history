# Swedish Police Events Archive

A git-based archive of Swedish police events, scraped twice hourly from the official Polisen.se API, providing 2+ years of historical crime data across all regions and event types.

**Live Site:** [View the interactive map](https://dnouri.github.io/polisen-se-events-history/)

[![Latest Scrape](https://github.com/dnouri/polisen-se-events-history/workflows/Scrape%20latest%20data/badge.svg)](https://github.com/dnouri/polisen-se-events-history/actions)

---

## Overview

This project implements the [git-scraping pattern](https://simonwillison.net/2020/Oct/9/git-scraping/) to create a historical archive of Swedish police events. Every 30 minutes, GitHub Actions:

1. Fetches the latest events from `https://polisen.se/api/events`
2. Archives individual HTML event pages to `html/` directory
3. Commits changes to git if data has changed
4. Deploys the updated visualization to GitHub Pages

With **28,000+ commits** over 2+ years of operation, this archive provides a reliable, continuously updated dataset of Swedish crime events.

**Data Source:** [Swedish Police Authority Open Data API](https://polisen.se/om-polisen/om-webbplatsen/oppna-data/api-over-polisens-handelser/)

---

## Quick Start

```bash
# Clone the repository (note: 1.4+ GB repository due to HTML archive)
git clone https://github.com/dnouri/polisen-se-events-history.git
cd polisen-se-events-history

# For faster clone, use shallow clone:
git clone --depth 1 https://github.com/dnouri/polisen-se-events-history.git

# View the current events data
cat events.json | jq '.[0]'  # Show first event

# View the interactive map
python3 -m http.server 8000
# Open http://localhost:8000
```

---

## Data Overview

### Current Snapshot (`events.json`)

- **Records:** ~500 events (9-day rolling window)
- **Update Frequency:** Every 30 minutes (`6,36 * * * *` cron)
- **File Size:** ~205 KB
- **Data Completeness:** 100% (no missing fields)
- **Event Types:** 57 distinct categories
- **Geographic Coverage:** 161 locations nationwide

### Event Schema

```json
{
  "id": 612685,
  "datetime": "2025-11-13 20:14:24 +01:00",
  "name": "13 november 18.28, Stöld, Härnösand",
  "summary": "Misstänkt stöld i butik, Kronholmen.",
  "url": "/aktuellt/handelser/2025/november/13/13-november-18.28-stold-harnosand/",
  "type": "Stöld",
  "location": {
    "name": "Härnösand",
    "gps": "62.63227,17.940871"
  }
}
```

**All text is in Swedish.** Event types, summaries, and location names use Swedish terminology.

### Top Event Categories

| Type | Translation | Frequency |
|------|-------------|-----------|
| `Trafikolycka` | Traffic accident | 15.8% |
| `Sammanfattning natt` | Night summary | 14.2% |
| `Rattfylleri` | Drunk driving | 7.8% |
| `Misshandel` | Assault | 6.0% |
| `Brand` | Fire | 5.2% |
| `Stöld` | Theft | 3.8% |
| `Rån` | Robbery | 2.4% |

### Historical Archive (`html/` directory)

- **Total Files:** 27,437+ HTML snapshots
- **Size:** 1.1 GB
- **Format:** Full webpage snapshots from polisen.se
- **Naming:** `{event_id}.html` (e.g., `612685.html`)
- **Content:** Complete event narratives, metadata, and context
- **Coverage:** 2+ years of archived incidents

**Note:** HTML files are only downloaded once per event ID (incremental archival). Thus, updates to existing HTML reports are currently lost. They contain full website boilerplate (~30-40 KB overhead) plus 5-10 KB of actual event content.

---

## Visualization

The project includes a basic interactive map built with:

- **Leaflet.js** for map rendering (OpenStreetMap tiles)
- **Leaflet.MarkerCluster** for marker aggregation
- **Type-based filtering** with emoji categorization
- **Date range selection**
- **Full-text search** across event names and summaries
- **Local storage** for filter persistence

### Features

✅ Click markers to view event details
✅ Filter by 57+ event types with checkboxes
✅ Search events by keyword
✅ Select date range with date pickers
✅ Mobile-responsive with collapsible sidebar
✅ Color-coded badges and emoji icons

### Limitations

❌ City-level markers only (cluster at urban centers)
❌ No time series analysis or trend visualization
❌ No crime rate normalization (absolute counts only)
❌ Limited to current 9-day window (no historical view)
❌ Client-side rendering (performance degrades with large datasets)

---

## Technical Architecture

### Automated Scraping Workflow

**File:** `.github/workflows/scrape.yml`

```yaml
Schedule: '6,36 * * * *'  # Every 30 minutes (48 times/day)
Steps:
  1. Fetch JSON: curl https://polisen.se/api/events | jq . > events.json
  2. Download HTML: bash download-html.sh (incremental, only new events)
  3. Commit: git commit -m "Latest data: {timestamp}" (only if changed)
  4. Push: Trigger deployment to GitHub Pages
```

**Incremental HTML Archival:**
The `download-html.sh` script checks if `html/{id}.html` exists before downloading. This prevents re-downloading 27K+ files on every run.

```bash
while read -r id url; do
    if [ ! -f html/${id}.html ]; then
        curl https://polisen.se/${url} -o html/${id}.html
    fi
done < <(cat events.json | jq -r '.[] | "\(.id) \(.url)"')
```

### Data Retention Strategy

**Current Approach:**
- `events.json` overwrites on every scrape (9-day rolling window)
- HTML files persist forever (incremental addition)
- Git history preserves all changes to `events.json`

**Historical Analysis:**
To analyze trends over time, you must:
1. Mine git history: `git log --all -- events.json`
2. Checkout historical commits: `git show <commit-hash>:events.json`
3. Aggregate data across commits

**Limitation:** The API only returns recent events (~9 days). Older events may not appear in `events.json` but are preserved in HTML archives.

### Storage Characteristics

| Component | Size | Count | Growth Rate |
|-----------|------|-------|-------------|
| `events.json` | ~200 KB | 500 events | Stable (overwrites) |
| HTML archive | 1+ GB | 27,000+ files | +50-100 MB/month |
| Git history | ~200+ MB | 28,000+ commits | +10-20 MB/month |
| **Total** | **~1.4+ GB** | - | ~60-120 MB/month |

**Clone Time:** ~2-5 minutes on broadband
**Shallow Clone:** `git clone --depth 1` reduces to ~1.1 GB

---

## Known Issues & Limitations

### Data Quality

1. **Location Precision:** City centroids only (~5-50 km accuracy). Not suitable for street-level analysis.
2. **Event Summaries:** "Sammanfattning natt" (night summary) entries aggregate multiple incidents, inflating event counts by ~14%.
3. **API Window:** Only 9 days of current data. Historical analysis requires git archaeology.
4. **HTML files:** Only the first version of HTML reports is ever stored in the `html/` folder. Updates are lost.
5. **Language:** All text in Swedish (event types, summaries, locations).

---

## Data Access

### Current Events

- **JSON:** [`events.json`](events.json) (updates every 30 minutes)
- **Web Interface:** [Interactive map](https://dnouri.github.io/polisen-se-events-history/)

### Historical Events

- **Git History:** `git log --all -- events.json`
- **HTML Archive:** `html/` directory (27k+ files)

### Example Queries

**Find all robberies in Stockholm:**
```bash
cat events.json | jq '.[] | select(.type == "Rån" and .location.name == "Stockholm")'
```

**Count events by type:**
```bash
cat events.json | jq -r '.[].type' | sort | uniq -c | sort -rn
```

**Extract all GPS coordinates:**
```bash
cat events.json | jq -r '.[] | "\(.location.name),\(.location.gps)"' > coordinates.csv
```

**View historical events from specific date:**
```bash
git log --all --format=%H -- events.json | while read commit; do
    echo "=== Commit: $commit ==="
    git show $commit:events.json | jq -r '.[0].datetime' | head -1
done
```

---

## Advanced Data Export

The `export-events.py` script extracts the complete historical dataset from git history and exports to analysis-ready formats.

### Features

- **Git Archaeology:** Extracts events from all 28,000+ commits (2+ years of history)
- **Deduplication:** Keeps most recent version of each event
- **GPS Parsing:** Converts location strings to separate `latitude`/`longitude` DOUBLE columns
- **HTML Enrichment:** Optionally extracts structured narrative data from archived HTML files
- **Multiple Formats:** Auto-detects output format from extension (.parquet, .json, .jsonl)
- **Date Filtering:** Export specific time periods

### Usage

```bash
# Export all events to Parquet (recommended for analysis)
uv run export-events.py --output all_events.parquet

# Export specific time period
uv run export-events.py \
  --start-date 2024-01-01 \
  --end-date 2024-12-31 \
  --output 2024_events.parquet

# Include HTML narrative content
uv run export-events.py --include-html --output enriched.parquet

# Export to JSON for debugging
uv run export-events.py --output events.json
```

### Parquet Output Schema

The exported Parquet file contains:

**Core Fields (from git history):**
- `event_id` (VARCHAR) - Unique event identifier
- `datetime` (VARCHAR) - ISO 8601 timestamp with timezone
- `name` (VARCHAR) - Event title
- `summary` (VARCHAR) - Brief description
- `url` (VARCHAR) - Path to full report on polisen.se
- `type` (VARCHAR) - Event category (57 types)
- `location_name` (VARCHAR) - City/region name
- `latitude` (DOUBLE) - WGS84 latitude (city centroid, ~5-50km precision)
- `longitude` (DOUBLE) - WGS84 longitude (city centroid)

**HTML Fields (if --include-html used):**
- `html_title` (VARCHAR) - Full event title from HTML
- `html_preamble` (VARCHAR) - Summary paragraph
- `html_body` (VARCHAR) - Complete narrative text
- `html_published_datetime` (VARCHAR) - ISO 8601 timestamp from HTML
- `html_author` (VARCHAR) - Report author
- `html_available` (BOOLEAN) - Whether HTML was found and parsed

**Key characteristics:**
- Geographic coordinates as separate DOUBLE columns (H3/DuckDB-ready)
- All unique events across 2+ years (deduplicated by ID)
- Optimized for spatial analysis and visualization
- Compatible with DuckDB, pandas, and geospatial tools

### Example Analysis with DuckDB

```sql
-- Load and query Parquet file
INSTALL h3; LOAD h3;

-- Aggregate by H3 hexagon (resolution 6 ≈ 6km)
SELECT
    h3_latlng_to_cell(latitude, longitude, 6) as h3_cell,
    COUNT(*) as event_count,
    array_agg(DISTINCT type) as crime_types
FROM 'all_events.parquet'
GROUP BY h3_cell
ORDER BY event_count DESC
LIMIT 20;
```

---

## Credits

- **Data Source:** [Swedish Police Authority](https://polisen.se)
- **Inspiration:** [Git Scraping Pattern](https://simonwillison.net/2020/Oct/9/git-scraping/) by Simon Willison

---

## License

The code in this repository is released into the public domain.

The data is sourced from the Swedish Police Authority's open data API and is subject to their terms of use.
