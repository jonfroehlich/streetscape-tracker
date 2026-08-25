# Streetscape Tracker

Streetscape Tracker (formerly *GSV Tracker*) analyzes street-level imagery coverage and temporal patterns in cities **over time** — who has imagery, how fresh it is, and how it changes.
It collects from three providers: Google Street View (GSV), [Mapillary](https://www.mapillary.com/) (360° panoramas), and [KartaView](https://kartaview.org/).
Each collection run samples a frozen geographic grid around a city center, queries the provider's metadata API, and produces an immutable dated snapshot — so re-running a city months later yields a true diff: panoramas added and removed, capture dates updated, coverage deltas.

This research project began in 2021 under Professor Jon E. Froehlich and was part of the [UC Berkeley Data Science Discovery Program](https://cdss.berkeley.edu/discovery/projects) in 2023 with students Joseph Chen, Wenjing Yi, and Jingfeng Yang ([original pitch sheet](https://docs.google.com/document/d/1hfgvS_JHRmhkVtj_LBZ2qd_TO-50L6g0crlV8nTBy9s/edit?tab=t.0)).
The [v1.0.0 release](https://github.com/jonfroehlich/streetscape-tracker/releases/tag/v1.0.0) supported our [GeoIndustry 2025 paper](https://doi.org/10.1145/3764919.3770883) on GSV coverage and socioeconomic indicators (see also [GSVantage](https://github.com/makeabilitylab/GSVantage)).

## How it works

Every run of a (city, provider) produces an immutable dated `csv.gz` snapshot plus a JSON summary, cataloged in a local SQLite database that also freezes each city's grid geometry — future runs sample the exact same points, so snapshots align and diffs are meaningful.
Each provider keeps its own independent run series on the shared grid, and a nightly scheduler staggers cities so the full corpus re-collects roughly quarterly without exceeding API limits.
The full data model, pipeline, and filename contract live in [`docs/architecture.md`](docs/architecture.md).

## Imagery providers

| | GSV (`gsv`) | Mapillary (`mapillary`) | KartaView (`kartaview`) |
|---|---|---|---|
| API model | One metadata request per grid point | Bulk z14 vector tiles (~10–100 requests/city) | Paginated radius sweep |
| What's kept | The nearest pano per grid point (a *sample*) | Every 360° pano, assigned to its nearest grid point, plus one `FLAT_ONLY` marker where only flat imagery covers a point (a *census*) | The same census as Mapillary — 360° panos plus `FLAT_ONLY` markers, never a row per flat photo |
| Credential (`.env`) | `GMAPS_API_KEY` | `MAPILLARY_ACCESS_TOKEN` | `KARTAVIEW_ACCESS_TOKEN` (required — the anonymous tier is unusably slow) |
| Nightly scheduler | Yes | Yes | No — CLI collection only |

`--provider` is a comma-separated channel list defaulting to `gsv,mapillary`, and every named provider's key must be present up-front so the series can't drift.
`--provider all` collects every provider the naming contract knows about — never the default, because KartaView's radius sweep is serial and runs for hours on a metro (see [`docs/experiments/kartaview-sweep-cost.md`](docs/experiments/kartaview-sweep-cost.md)).

**Comparing providers.** All providers share the identical frozen grid, so *coverage rate* (% of grid points with a 360° pano) is directly comparable.
Raw *pano counts* are not: a GSV sample undercounts dense imagery, while a census counts everything.
The two censuses also report a second, wider *any-imagery* coverage rate that counts the flat-only points; it is never conflated with the 360° rate.

**Attribution.** Mapillary metadata is used under their [terms](https://www.mapillary.com/terms) (CC BY-SA); anything derived from it must visibly credit Mapillary, which the bundled web frontend does automatically.

## Install

```bash
git clone https://github.com/jonfroehlich/streetscape-tracker.git
cd streetscape-tracker
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then put your API keys in a `.env` file in the project root (see the credential table above).

## Usage

```bash
# Collect a city with default settings (1000m x 1000m grid, 20m steps) — one GSV
# and one Mapillary snapshot on the same run date
python streetscape_tracker.py "Seattle, WA"

# Choose providers
python streetscape_tracker.py "Seattle, WA" --provider gsv
python streetscape_tracker.py "Seattle, WA" --provider gsv,kartaview
python streetscape_tracker.py "Seattle, WA" --provider all   # includes the hours-long KartaView sweep

# Preview the search boundary on a map before spending any requests
python streetscape_tracker.py "Seattle, WA" --check-boundary

# Batch: one city per line, with optional per-city flags
python run_cities.py cities.txt --continue-on-error
```

Re-running a (city, provider) sooner than `--min-days-since-last-run` (default 80 days) is skipped unless you pass `--force`.
Street-level coverage analysis and collection (road walks along the OSM street network) live in `streetscape_street_analyzer` — see [`docs/street-coverage.md`](docs/street-coverage.md).

Three standalone tools sit beside the tracker at the repo root, all offline (no API keys, no network):

```bash
python streetscape_compare_data.py old.csv.gz new.csv.gz     # diff two runs of one provider
python generate_json.py                                      # rebuild missing per-run JSON summaries
python check_status_codes.py data/some_run.csv.gz            # status-code breakdown of a run CSV
```


For the nightly scheduler, its systemd units, and operator commands like the same-day `assess-city` report, see [`deploy/README.md`](deploy/README.md) and [`docs/operations.md`](docs/operations.md).

## Output files

Everything lands in `./data` (override with `--download-dir`), where `{base}` is `{city_id}_width_{W}_height_{H}_step_{S}[_provider]_{YYYY-MM-DD}` — no provider token means GSV.

- **`{base}.csv.gz`** — the run's metadata snapshot (identical 9-column core across providers).
- **`{base}.json.gz`** — coverage/age statistics, temporal histograms, and the change-vs-previous-run block.
- **`{city_id}_diff_{FROM}_to_{TO}.csv.gz`** — per-pano change detail between two runs of the same provider.
- **`{base}_streets.json.gz`** — optional OSM street-coverage overlay, written by `streetscape_street_analyzer.analyze`.
- **`{base}_failed_points.csv`** — written only when grid points still failed after every retry: `lat,lon,i,j,status`, one row each. Above 1% of points the run is refused outright and no snapshot is finalized, so this sidecar accompanies kept runs, whose residual failures also appear as rows in the snapshot itself.
- **`cities.json.gz`** — the aggregate the website consumes: per city, per provider, latest stats + run history + change summary.
- **`streetscape_tracker.db`** — the SQLite catalog. Local only, never published.
- **`vis/{base}.html`** — an interactive map of the run.

Filename and schema details: [`docs/architecture.md`](docs/architecture.md).

## The website

A fully static frontend (vanilla JS + Leaflet, no build step, no backend) renders the published `data/`:

- **`index.html`** — the map: every tracked city, colored by coverage or freshness.
- **`city.html`** — one city's runs, diffs, temporal histograms, and street-coverage overlay.
- **`grid.html`** / **`streets.html`** — sortable cross-city tables of grid and street coverage, one row per city with providers side by side.
- **`driving.html`** — Google's published driving plan joined against what we actually observe.

Frontend architecture and contracts: [`docs/frontend.md`](docs/frontend.md).

## Which cities are tracked?

Roughly 1,100 US cities plus a worldwide stratified frame (~56 cities), assembled from a census-stratified study list ([`cities/README.md`](cities/README.md)), archival baseline imports, collaborator requests, and the GeoNames-based worldwide frame.
The full provenance and sampling methodology — the record to cite in publications — is [`docs/city_sampling.md`](docs/city_sampling.md).

## Further reading

| Topic | Doc |
|---|---|
| Data model, pipeline, filenames | [`docs/architecture.md`](docs/architecture.md) |
| Provider rate limits and access incidents | [`docs/provider-access.md`](docs/provider-access.md) |
| Street coverage and road walks | [`docs/street-coverage.md`](docs/street-coverage.md) |
| Nightly scheduler | [`docs/scheduler.md`](docs/scheduler.md) |
| Measured experiments | [`docs/experiments/README.md`](docs/experiments/README.md) |

## Related tools

[sv-map](https://sv-map.netlify.app/) archives Google's blue Street View coverage lines daily as images and diffs them pixel-wise; [Virtual Streets](https://virtualstreets.org/) blogs about new Street View coverage.
Both track *where* coverage exists; Streetscape Tracker tracks the underlying metadata — counts, capture dates, and change — across providers.

## License

Distributed under the MIT License. See `LICENSE` for more information.
