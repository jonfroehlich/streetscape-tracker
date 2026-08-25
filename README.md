# Streetscape Tracker

Streetscape Tracker (formerly *GSV Tracker*) is a Python tool for analyzing street-level imagery coverage and temporal patterns in cities **over time** — Google Street View (GSV) and, as of 2026, [Mapillary](https://www.mapillary.com/) 360° panoramas.
It samples geographic grids around city centers, queries each provider's metadata API, and produces dated snapshots per city — computing what changed between snapshots (panoramas added/removed, capture dates updated, coverage deltas) and rendering interactive visualizations of when and where imagery was captured.

This research project began in 2021 by Professor Jon E. Froehlich and was also part of the [UC Berkeley Data Science Discovery Program](https://cdss.berkeley.edu/discovery/projects) in 2023 with students Joseph Chen, Wenjing Yi, and Jingfeng Yang.
Here's the [original pitch sheet in Google Docs](https://docs.google.com/document/d/1hfgvS_JHRmhkVtj_LBZ2qd_TO-50L6g0crlV8nTBy9s/edit?tab=t.0).
The [v1.0.0 release](https://github.com/jonfroehlich/streetscape-tracker/releases/tag/v1.0.0) of this tool supported our [GeoIndustry 2025 paper](https://doi.org/10.1145/3764919.3770883) on GSV coverage and socioeconomic indicators (see also [GSVantage](https://github.com/makeabilitylab/GSVantage)).

## How temporal tracking works

Every collection run of a city produces an immutable dated snapshot
(`{city}_width_W_height_H_step_S_YYYY-MM-DD.csv.gz`; Mapillary runs add a
provider token: `..._step_S_mapillary_YYYY-MM-DD.csv.gz`). A SQLite catalog
(`data/streetscape_tracker.db`) records each city's identity and **frozen grid
geometry** (so future runs sample the exact same points), every run's
stats, and run-to-run diffs — each provider keeps its own independent run
series on the same grid. Re-running a (city, provider) sooner than
`--min-days-since-last-run` (default 80) days is skipped unless you pass
`--force`. A scheduler (see `deploy/README.md`) staggers ~13 cities/day so
the full corpus re-collects roughly quarterly without exceeding API limits;
a city due for both providers runs them back-to-back with the same run date
so cross-provider snapshots align.

## Imagery providers

`--provider` is a comma-separated channel LIST, defaulting to `gsv,mapillary` (issue #247).
The named providers run back-to-back with the same run date, so their series stay in sync;
naming one (`--provider mapillary`) restricts to it.
`--provider all` collects every provider the naming contract knows about — never the default, because KartaView's sweep is serial and hours long for a metro.
The retired spelling `both` still works, with a deprecation notice: it named two of two when it was written and names two of three now.

| | Google Street View (`--provider gsv`) | Mapillary (`--provider mapillary`) |
|---|---|---|
| API model | One metadata request per grid point ("nearest pano to X?") | Bulk z14 vector tiles (~10–100 requests per city) |
| What's kept | The nearest pano per grid point | **Every** 360° pano (`is_pano`), assigned to its nearest grid point; flat phone photos are excluded |
| Credential | `GMAPS_API_KEY` ([console.cloud.google.com](https://console.cloud.google.com/apis/credentials), Street View Static API enabled) | `MAPILLARY_ACCESS_TOKEN` (free client token from [mapillary.com/dashboard/developers](https://www.mapillary.com/dashboard/developers)) |

Both go in `.env` in the project root.
Every provider named by `--provider` must have its credential up-front — `--provider all` included — and a missing key fails before anything downloads, so the series can't drift out of sync.

**Comparing numbers across providers.** Both providers use the identical
frozen grid, so *coverage rate* (% of grid points with a pano) is directly
comparable. Raw *pano counts* are not: GSV counts are a grid **sample**
(one nearest pano per point — dense imagery is undercounted), while
Mapillary counts are a **census** of every pano in the area.

**Attribution.** Mapillary metadata is used under their
[terms](https://www.mapillary.com/terms) (CC BY-SA); anything derived from
it must visibly credit Mapillary, which the bundled web frontend does
automatically.

## 1. Setup and Installation

We recommend using a standard Python virtual environment (`.venv`) to manage dependencies.

1. **Clone the repository:**

```bash
git clone https://github.com/jonfroehlich/streetscape-tracker.git
cd streetscape-tracker
```

2. **Create a virtual environment:**

```bash
python -m venv .venv
```

3. **Activate the environment:**

**Mac/Linux:**

```bash
source .venv/bin/activate
```

**Windows:**

```cmd
.venv\Scripts\activate
```

4. **Install dependencies:**

```bash
pip install -r requirements.txt
```

## 2. Available Scripts

The repository includes several scripts divided into core data collection tools and data utility scripts.

### Core Data Collection Tools

* **`streetscape_tracker.py`**: The primary asynchronous data collection script. Each invocation collects one dated snapshot of a city, catalogs it, and diffs it against the previous run.
* **`run_cities.py`**: A batch-processing wrapper that runs `streetscape_tracker.py` across multiple cities sequentially by reading configurations from a text file (e.g., `cities.txt`).
* **`python -m streetscape_metadata_tracker.scheduler`**: The staggered quarterly scheduler (`status`, `assign`, `run-due`, `regenerate-aggregate`, `assess-city` subcommands).
  `regenerate-aggregate [--publish]` rebuilds `cities.json.gz` from the catalog without collecting — handy after a schema change or manual run.
  `assess-city "City, Region"` is the same-day answer for a city we don't track yet: it registers the city, walks its streets on both providers, publishes, and prints the street-km coverage a deployment decision actually turns on (`--estimate` reports boundary fit and cost first, with no provider requests).
  See `deploy/README.md` for running it as a systemd timer.

### Data Utilities & Analysis

* **`python -m streetscape_street_analyzer.analyze`**: OSM street-coverage analysis for an existing run — overlays the city's frozen OpenStreetMap street network on the run's panos and writes a `{base}_streets.json.gz` GeoJSON (per-segment covered/uncovered plus a by-street-type summary) that the city page renders as an optional overlay.
  Example: `python -m streetscape_street_analyzer.analyze "Seattle, WA" --provider gsv`.
  The network is fetched once per city and frozen (`data/osm_cache/`, cataloged in `street_networks`); `--refresh` re-fetches it.
  Uses no imagery-provider API.
  (Street-coverage *collection* is planned separately and will use its own isolated credentials, `GMAPS_STREETS_API_KEY` / `MAPILLARY_STREETS_ACCESS_TOKEN` — see issue #99.)
* **`streetscape_compare_data.py`**: Compares two run metadata files for the same city and reports panos added/removed, capture-date changes, and coverage transitions.
* **`generate_json.py`**: Generates missing JSON metadata summary files for existing run data directories.
* **`check_status_codes.py`**: Analyzes API status-code distributions across data files.
* **`scripts/migrate_to_db.py`**: One-time migration that registers pre-temporal-tracking data files as baseline runs in the catalog (dry-run by default).

## 3. Usage Examples

### Basic Usage

To analyze a city's coverage using default settings (1000m x 1000m grid, 20m steps) — this collects a GSV **and** a Mapillary snapshot with the same run date, the `gsv,mapillary` default:

```bash
python streetscape_tracker.py "Seattle, WA"
```

### Choosing Which Providers to Collect

```bash
python streetscape_tracker.py "Seattle, WA" --provider gsv          # one channel
python streetscape_tracker.py "Seattle, WA" --provider mapillary
python streetscape_tracker.py "Seattle, WA" --provider gsv,kartaview  # any list
python streetscape_tracker.py "Seattle, WA" --provider all          # every provider; needs every key
```

A name the CLI doesn't know, and an empty selection (`--provider ""`), are both refused at parse time rather than collecting nothing and exiting 0.

A Mapillary run reuses the city's frozen grid and takes seconds — a whole
city is a few dozen tile requests rather than one request per grid point.

### Preview Search Area

Before executing a large download, you can generate an HTML map to preview your search boundary:

```bash
python streetscape_tracker.py "Seattle, WA" --check-boundary
```

### Tuning Concurrency (For Large Areas)

For larger queries, you can adjust the batch size and connection limits to optimize network usage against the Google API (500 requests/second limit):

```bash
python streetscape_tracker.py "Portland, OR" --batch-size 200 --connection-limit 100
```

### Batch Processing Multiple Cities

Create a `cities.txt` file where each line is a standard command configuration:

```text
# cities.txt
Seattle, WA --width 2000 --height 2000 --step 25
Portland, OR --width 1500 --height 1500
Vancouver, BC
```

Then run the batch script:

```bash
python run_cities.py cities.txt --continue-on-error
```

### Which cities are tracked?

The catalog is **not** limited to US state capitals — it spans ~1,100 US cities
plus a few dozen international ones, assembled from several sources: a US
census-stratified study list (`cities/`, see
[`cities/README.md`](cities/README.md)), archival baseline imports, and ad-hoc
additions (collaborator and Project Sidewalk requests). A proposed worldwide
frame is in progress. The full provenance and sampling methodology — the record
to cite in publications — lives in
[`docs/city_sampling.md`](docs/city_sampling.md).

## 4. Output Files

Each run generates the following files in your designated `--download-dir` (defaults to `./data`), where `{base}` is `{city_id}_width_{W}_height_{H}_step_{S}_{YYYY-MM-DD}` (Mapillary runs insert a `mapillary` token before the date):

* **`{base}.csv.gz`**: The core compressed data file containing all downloaded metadata for this run (identical 9-column schema for both providers).
* **`{base}.json.gz`**: A JSON summary (schema v2) with coverage/age statistics, temporal histograms, and the change-vs-previous-run block.
* **`{base}_failed_points.csv`**: A log of coordinates that failed to download after all retry attempts (GSV runs only).
* **`{city_id}_diff_{FROM}_to_{TO}.csv.gz`**: Per-pano change detail between two runs of the same provider (written when changes exist; Mapillary diffs insert a `mapillary` token after `_diff`).
* **`{base}_streets.json.gz`**: Optional OSM street-coverage GeoJSON for the run (written by `streetscape_street_analyzer.analyze`, not by the run itself); the city page shows a street overlay + breakdown panel when present.
* **`cities.json.gz`**: The aggregate consumed by the web frontend (schema v3) — one entry per city, grouped by provider, with each provider's latest stats, run history, and change summary.
* **`streetscape_tracker.db`**: The SQLite catalog (cities, runs, diffs, schedule state). Local only; never published.
* **`vis/{base}.html`**: An interactive map visualization of the run (unless `--no-visual` is passed).

## Other Helpful Tools

Other helpful tools include:

Starting in 2022, [sv-map](https://sv-map.netlify.app/) archives blue Street View lines from Google Maps daily, so users can compare the evolution of Street View over time. sv-map downloads Street View coverage lines as **images**, up to a certain zoom level. To display historical coverage differences on the website, it highlights the difference in pixels between the images of different dates. 
<img width="1506" height="856" alt="image" src="https://github.com/user-attachments/assets/1fd0b35d-ae57-4a29-90d8-b81be23246fd" />

[Virtual Streets](https://virtualstreets.org/) provides a blog that describes new coverage to Google Street View

## License

Distributed under the MIT License. See `LICENSE` for more information.
