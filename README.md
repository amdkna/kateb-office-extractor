# Kateb Office Extractor

A Windows-friendly collector and desktop data manager for official-office
markers returned by the Kateb map API.

The collector scans a configurable geographic area, writes results directly to
SQLite, normalizes telephone numbers, and prevents duplicates. A companion
desktop app provides live per-column filtering, sortable tables, legacy JSON
import, and Excel export.

> This is an independent data-collection utility. Use it responsibly and in
> accordance with the website's terms, access controls, and acceptable-use
> policies.

## Features

- Authenticated collection through a dedicated Chrome profile
- Efficient triangular map coverage with substantially less overlap than a
  square coordinate grid
- Adaptive follow-up probes in crowded areas where responses may be capped
- Direct SQLite storage—no per-entry JSON files
- Telephone normalization for Latin, Persian, and Arabic digits
- Database-enforced telephone uniqueness
- Resumable collection with a persistent coordinate ledger
- Failed-coordinate tracking and retry-only restarts
- Windows desktop UI with:
  - graphical website collection and progress
  - live street-map coverage with color-coded scan squares
  - selectable, copyable live console logs
  - pause/resume and force-stop controls
  - configurable batch throttling
  - live search above every column
  - ascending and descending column sorting
  - pagination
  - legacy JSON-folder import
  - filtered and sorted Excel export
- No-console desktop launcher using `pythonw.exe`

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- Google Chrome
- Access to the Kateb official-office map

## Quick start

### 1. Set up the environment

Double-click:

```text
setup.bat
```

The setup script accepts either the Windows `py` launcher or a `python`
command on `PATH`. It creates `.venv`, installs dependencies, and copies
`.env.example` to `.env` when needed.

### 2. Review the configuration

Edit `.env`, then double-click:

```text
check-config.bat
```

This prints the configured endpoint, authentication mode, database path,
geographic bounds, coverage strategy, estimated request count, and approximate
run time without contacting the API.

### 3. Collect data

Double-click:

```text
run.bat
```

Chrome opens using the dedicated profile in `chrome-profile/`. Sign in if
required, wait until the map is visible, return to the collector window, and
press Enter.

Each successful coordinate is committed immediately. If the run stops or the
session expires, start `run.bat` again: completed coordinates are removed from
the queue and only failed or unattempted coordinates are requested.

### 4. Open the desktop manager

Double-click:

```text
run-office-manager.vbs
```

The VBS launcher starts `office_manager.pyw` through `pythonw.exe`, so no black
command window remains open behind the application.

Open the **Website collection** tab to run the collector graphically. Chrome
still opens for authenticated access, but collector output is displayed inside
the app instead of a terminal.

## Default Greater Tehran coverage

The example configuration covers the Greater Tehran metropolitan rectangle:

```dotenv
MIN_LAT=35.45
MAX_LAT=35.95
MIN_LNG=50.80
MAX_LNG=52.05
COVERAGE_RADIUS_KM=1.8
```

Saved responses showed that the known geographic endpoint returns offices up
to approximately 2 km from a requested coordinate. The collector uses a
conservative 1.8 km effective radius and places query centers on a staggered
triangular lattice. This covers the target rectangle—including a small edge
buffer—with much less overlap than a dense square grid.

The current default produces about 826 base coordinates. Locations returning
at least `SATURATION_THRESHOLD` entries receive local follow-up probes in case
the API is limiting results near 50.

## Configuration

Copy `.env.example` to `.env` and adjust values locally. Never commit `.env`.

| Setting | Purpose |
| --- | --- |
| `BASE_URL` | Kateb web application base URL |
| `GEO_ENDPOINT` | Geographic office endpoint |
| `SEARCH_URL` | Map page opened in Chrome |
| `AUTH_MODE` | `browser` is recommended because public calls return HTTP 401 |
| `CHROME_EXE` | Path to the installed Chrome executable |
| `CHROME_PROFILE_DIR` | Dedicated reusable authentication profile |
| `HEADLESS` | Keep `false` when interactive sign-in may be required |
| `DATABASE_PATH` | SQLite file shared by collector and desktop app |
| `MIN_LAT`, `MAX_LAT` | Target latitude bounds |
| `MIN_LNG`, `MAX_LNG` | Target longitude bounds |
| `COVERAGE_RADIUS_KM` | Conservative effective coverage radius |
| `SATURATION_THRESHOLD` | Response size that triggers denser local probes |
| `REQUEST_DELAY_SECONDS` | Delay between requests |
| `BATCH_QUERY_COUNT` | Number of API queries between longer rest periods |
| `BATCH_DELAY_SECONDS` | Length of each batch rest period in seconds |
| `REQUEST_TIMEOUT_SECONDS` | Per-request timeout |
| `MAX_RETRIES` | Retry count for transient HTTP failures |
| `VERIFY_SSL` | Enable TLS certificate verification |

## Database design

The default database is `data/office_data.sqlite3`.

### `offices`

Stores office details, the original telephone text, normalized telephone,
address, office ID, coordinates, source URL, timestamps, and the original JSON
payload. A partial unique index guarantees that every non-empty normalized
telephone occurs at most once.

When a repeated telephone is found, the existing row is updated rather than
duplicated. Office ID is used as a secondary identity when telephone data is
missing.

### `scan_points`

Acts as the persistent coordinate ledger. Each row contains:

- endpoint
- latitude and longitude
- `done` or `failed` status
- attempt count
- entries found
- inserted and updated counts
- latest error
- last-attempt timestamp

At startup the collector reconstructs the configured map grid and subtracts
all `done` points. Only failed and never-attempted coordinates remain in the
network request queue.

### `import_history`

Records legacy JSON-folder imports made through the desktop application.

## Telephone deduplication

Telephone numbers are converted to a comparison-safe representation before
storage:

- Persian and Arabic digits become Latin digits
- spaces, dashes, parentheses, and other punctuation are removed
- `+98` and `0098` Iranian prefixes are normalized
- a ten-digit mobile number beginning with `9` receives a leading `0`

For example, these values resolve to the same unique number:

```text
۰۹۱۲-۵۲۵-۴۲۱۴
+98 912 525 4214
0098-912-525-4214
9125254214
```

## Desktop data manager

The **All data** tab reads directly from SQLite. Search fields above the
columns update results while typing, and clicking a heading toggles ascending
or descending order.

The **File** menu provides:

- **Import new data** — recursively loads legacy JSON files and applies the
  same telephone deduplication rules
- **Export to Excel** — exports the current filtered and sorted result set to
  an `.xlsx` workbook

### Graphical website collection

The **Website collection** tab includes:

- **Coverage map** — shows OpenStreetMap beneath transparent scan squares;
  green is downloaded, red is failed, and blue has not been tried yet
- **Live map totals** — reports downloaded, failed, and pending coordinates and
  refreshes after every request
- **Start / resume collection** — starts the normal retry-aware scan in a
  background thread
- **Pause / Resume** — pauses between requests and also pauses active delay
  countdowns; an already-running request is allowed to finish safely
- **Force stop** — signals collection to stop immediately after any active
  request returns; all completed database work remains saved
- **Batch delay** — configurable as “after every X API queries, wait Y seconds”
- **Progress bar** — shows processed and remaining coordinates
- **Collector console** — displays live logs in a scrollable text area

Console text is selectable and copyable using Ctrl+C, the **Copy selected**
button, or the right-click menu. The console can be cleared without affecting
the database or coordinate ledger.

Street-map tiles are downloaded from OpenStreetMap on first use and cached in
`data/map_tiles`. Previously loaded areas therefore remain visible offline.

The default batch throttle is:

```dotenv
BATCH_QUERY_COUNT=10
BATCH_DELAY_SECONDS=10
```

Values entered in the graphical tab apply to that run. Set either value to `0`
to disable the longer batch delay. The ordinary per-query delay from
`REQUEST_DELAY_SECONDS` remains active.

## Resume and rescan behavior

Normal restart—recommended:

```powershell
.\.venv\Scripts\python.exe extractor.py
```

Completed coordinates are skipped.

Intentional full rescan:

```powershell
.\.venv\Scripts\python.exe extractor.py --rescan
```

Existing office rows are still updated rather than duplicated.

## Running tests

After setup:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests cover grid geometry, boundary coverage, telephone normalization,
SQLite uniqueness, schema migration, failed-coordinate logging, retry-only
resume behavior, filtering, sorting, and Excel package validity.

## Project files

```text
extractor.py              Authenticated geographic collector
office_data.py            SQLite, import, deduplication, query, and XLSX logic
office_manager.pyw        Tkinter desktop application
setup.bat                 Windows environment setup
check-config.bat          Configuration-only validation
run.bat                   Collector launcher
run-office-manager.vbs    Hidden-console desktop launcher
.env.example              Safe configuration template
tests/                    Automated test suite
```

## Data and privacy

The following local artifacts are intentionally excluded from Git:

- `.env`
- `chrome-profile/`
- `data/`
- `output/`
- `.venv/`
- SQLite databases
- generated Excel workbooks

The dedicated Chrome profile may contain authentication state. Do not share or
commit it.

## Known limitation

The collector uses the known geographic marker endpoint. If clicking a marker
loads additional detail through another endpoint, that separate request must be
captured and implemented before those extra fields can be collected.
