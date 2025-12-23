# Swedish Police Events Archive

68,000+ Swedish police events (2022-2025) queryable directly via DuckDB - no download required.

[![Latest Scrape](https://github.com/dnouri/polisen-se-events-history/workflows/Scrape%20latest%20data/badge.svg)](https://github.com/dnouri/polisen-se-events-history/actions)

---

## Query Now

Analyze 2+ years of Swedish crime data directly from your terminal:

```sql
-- Top event types (run with: duckdb -c "...")
SELECT type, COUNT(*) as count
FROM 'https://github.com/dnouri/polisen-se-events-history/releases/download/data-latest/events.parquet'
GROUP BY type ORDER BY count DESC LIMIT 10;
```

```
┌───────────────────────────┬───────┐
│           type            │ count │
├───────────────────────────┼───────┤
│ Trafikolycka              │ 10434 │
│ Sammanfattning natt       │ 10092 │
│ Brand                     │  4407 │
│ Trafikkontroll            │  4068 │
│ Rattfylleri               │  4001 │
│ Misshandel                │  3124 │
│ Stöld                     │  2497 │
│ Trafikolycka, personskada │  2095 │
│ ...                       │   ... │
└───────────────────────────┴───────┘
```

```sql
-- Traffic accidents by city
SELECT location_name, COUNT(*) as accidents
FROM 'https://github.com/dnouri/polisen-se-events-history/releases/download/data-latest/events.parquet'
WHERE type = 'Trafikolycka'
GROUP BY location_name ORDER BY accidents DESC LIMIT 10;
```

```
┌──────────────┬───────────┐
│ location_name│ accidents │
├──────────────┼───────────┤
│ Malmö        │       466 │
│ Örebro       │       291 │
│ Helsingborg  │       257 │
│ Umeå         │       254 │
│ Göteborg     │       248 │
│ Sundsvall    │       227 │
│ Växjö        │       208 │
│ Luleå        │       206 │
│ ...          │       ... │
└──────────────┴───────────┘
```

```sql
-- Events per month
SELECT substr(datetime, 1, 7) as month, COUNT(*) as events
FROM 'https://github.com/dnouri/polisen-se-events-history/releases/download/data-latest/events.parquet'
GROUP BY month ORDER BY month DESC LIMIT 6;
```

```
┌─────────┬────────┐
│  month  │ events │
├─────────┼────────┤
│ 2025-11 │   2112 │
│ 2025-10 │   2151 │
│ 2025-09 │   2140 │
│ 2025-08 │   2060 │
│ 2025-07 │   1862 │
│ 2025-06 │   2108 │
└─────────┴────────┘
```

---

## Dataset Overview

| Metric | Value |
|--------|-------|
| Total events | 68,000+ |
| Date range | Sept 2022 - Present |
| Event types | 94 categories |
| Locations | 311 municipalities |
| Update frequency | Daily (04:15 UTC) |
| Format | Apache Parquet (~11 MB) |

**All text is in Swedish.** Event types (`Trafikolycka`, `Misshandel`, `Stöld`), summaries, and locations use Swedish terminology.

### Top Event Categories

| Swedish | English | Count |
|---------|---------|-------|
| Trafikolycka | Traffic accident | 10,434 |
| Sammanfattning natt | Night summary | 10,092 |
| Brand | Fire | 4,407 |
| Trafikkontroll | Traffic control | 4,068 |
| Rattfylleri | Drunk driving | 4,001 |
| Misshandel | Assault | 3,124 |
| Stöld | Theft | 2,497 |

---

## Data Schema

```sql
DESCRIBE SELECT * FROM 'events.parquet';
```

| Column | Type | Description |
|--------|------|-------------|
| `event_id` | VARCHAR | Unique identifier |
| `datetime` | VARCHAR | ISO 8601 timestamp with timezone |
| `name` | VARCHAR | Event title (Swedish) |
| `summary` | VARCHAR | Brief description |
| `url` | VARCHAR | Path to full report on polisen.se |
| `type` | VARCHAR | Event category (94 types) |
| `location_name` | VARCHAR | Municipality/city name |
| `latitude` | DOUBLE | WGS84 latitude |
| `longitude` | DOUBLE | WGS84 longitude |
| `html_title` | VARCHAR | Full title from archived HTML |
| `html_preamble` | VARCHAR | Summary paragraph |
| `html_body` | VARCHAR | Complete narrative text |
| `html_published_datetime` | VARCHAR | Publication timestamp |
| `html_author` | VARCHAR | Report author |
| `html_available` | BOOLEAN | Whether HTML was parsed |

### Location Precision

**Coordinates are municipality centroids, not incident addresses.** The Swedish Police API reports all events for a city at a single point (e.g., all Stockholm events at 59.33°N, 18.07°E). This affects analysis:

- Suitable for: Municipality-level aggregation, regional trends, event type distribution
- Not suitable for: Street-level analysis, neighborhood patterns, distance calculations

---

## Other Access Methods

### Interactive Map

**Live Site:** [dnouri.github.io/polisen-se-events-history](https://dnouri.github.io/polisen-se-events-history/)

A basic web interface showing recent events (9-day window) with filtering by type and date.

### Current Events JSON

```bash
# Latest ~500 events (9-day rolling window, updates every 30 min)
curl -s https://raw.githubusercontent.com/dnouri/polisen-se-events-history/main/events.json | jq '.[0]'
```

```json
{
  "id": 612685,
  "datetime": "2025-11-13 20:14:24 +01:00",
  "name": "13 november 18.28, Stöld, Härnösand",
  "summary": "Misstänkt stöld i butik, Kronholmen.",
  "type": "Stöld",
  "location": { "name": "Härnösand", "gps": "62.63227,17.940871" }
}
```

### Clone Repository

```bash
# Full history (1.4+ GB due to HTML archive)
git clone https://github.com/dnouri/polisen-se-events-history.git

# Shallow clone (faster, ~1.1 GB)
git clone --depth 1 https://github.com/dnouri/polisen-se-events-history.git
```

### Download Parquet

```bash
curl -LO https://github.com/dnouri/polisen-se-events-history/releases/download/data-latest/events.parquet
```

---

## How It Works

This project uses the [git-scraping pattern](https://simonwillison.net/2020/Oct/9/git-scraping/) to archive Swedish police events:

1. **Every 30 minutes**: GitHub Actions fetches `https://polisen.se/api/events`
2. **HTML archival**: Individual event pages are downloaded to `html/` directory
3. **Git history**: Changes are committed, preserving full history
4. **Daily export**: All events are extracted from git history and published as Parquet

With 28,000+ commits over 2+ years, this provides a reliable historical dataset.

### Data Pipeline

```
polisen.se/api/events (9-day rolling window)
    ↓ GitHub Actions (every 30 min)
events.json + html/{id}.html
    ↓ Git history (28,000+ commits)
export-events.py --include-html
    ↓ Daily release
events.parquet (68,000+ unique events)
```

### Generate Parquet Locally

```bash
# Clone with full history
git clone https://github.com/dnouri/polisen-se-events-history.git
cd polisen-se-events-history

# Export all events (requires uv)
uv run export-events.py --include-html --output events.parquet

# Export specific date range
uv run export-events.py --start-date 2024-01-01 --end-date 2024-12-31 --output 2024.parquet
```

---

## Known Limitations

1. **Municipality centroids**: Coordinates are city center points, not actual incident locations
2. **Night summaries**: `Sammanfattning natt` entries aggregate multiple incidents (~15% of events)
3. **Swedish only**: All text in Swedish
4. **HTML snapshots**: Only first version of each event page is archived; updates are lost
5. **9-day API window**: Historical data requires git archaeology or the Parquet export

---

## Credits

- **Data Source:** [Swedish Police Authority Open Data API](https://polisen.se/om-polisen/om-webbplatsen/oppna-data/api-over-polisens-handelser/)
- **Pattern:** [Git Scraping](https://simonwillison.net/2020/Oct/9/git-scraping/) by Simon Willison

---

## License

Code: Public Domain
Data: Subject to [Swedish Police Authority terms of use](https://polisen.se)
