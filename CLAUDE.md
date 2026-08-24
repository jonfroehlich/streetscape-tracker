# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Streetscape Tracker analyzes street-level imagery coverage and temporal patterns in cities **over time**, for two providers: Google Street View (GSV, the default) and Mapillary (360° panos only). It samples a geographic grid around a city center, queries each provider's metadata API, and produces immutable dated snapshots per (city, provider) plus run-to-run change summaries (panos added/removed, capture-date changes, coverage deltas) and interactive map visualizations.

## READ THIS FIRST: provider API access is the project's single point of failure

**This entire project is about acquiring data. If we cannot reach the provider APIs, the project does not function** — no snapshots, no diffs, no site, and the temporal series has a permanent hole that no later run can backfill, because a missed month is missed forever.

So: **before writing or deploying ANY functionality that changes how, how often, how fast, or from where we call GSV or Mapillary, you MUST first read both the official API documentation and the developer/community forums for that provider.** Not after a failure. Before. This is a hard precondition, not a nicety, and it applies to new collectors, new channels, concurrency or pacing changes, retry/backoff logic, bulk catch-ups, and any migration that moves collection to a different host or IP.

- **GSV:** [Street View Static API usage & billing](https://developers.google.com/maps/documentation/streetview/usage-and-billing), the API's own docs, and the Google Maps Platform issue tracker / Stack Overflow `google-street-view` tag.
- **Mapillary:** [API documentation](https://www.mapillary.com/developer/api-documentation) (including its rate-limits section) **and** [forum.mapillary.com](https://forum.mapillary.com) — the forum is not optional. Mapillary's real operational limits are undocumented, and the forum is the only place they are described at all.

**The documented limit is not necessarily the binding one, and the forum is where you learn that.** The 2026-08-12 incident is the case study: the docs say `tiles.mapillary.com` allows 50,000 requests/day **per app** and returns **4xx** when exceeded; what actually stopped us was an **undocumented per-IP throttle** that redirected **302 → login** at 10,659 requests (~21% of the documented cap), blocking both of our Mapillary applications from one host at once while the same token worked fine elsewhere. A forum thread ([Inconsistent authentication issues](https://forum.mapillary.com/t/inconsistent-authentication-issues/5821)) had already described that exact failure, already identified it as per-IP rather than per-token, and — critically — already reported that **retrying during a block appears to extend it**. Reading it first would have told us the ceiling was per-IP before we sustained **370 req/min** into it, and would have flagged the retry hazard our nightly re-probing then ran straight into (whether that re-probing actually prolonged the block is a hypothesis, not a measured fact — the measured/reported/guessed tiers must stay distinct). This paragraph is a condensation of the one that opens [`docs/provider-access.md`](docs/provider-access.md); the two say the same thing, and an edit to either belongs in both. The full record of both blocks, what is measured versus merely reported versus guessed, and every pacing and budget decision they forced is in [`docs/provider-access.md`](docs/provider-access.md).

Corollary: **treat any behavior you cannot find documented as unknown rather than unlimited**, pace conservatively by default, and never resolve "is this too fast?" by experiment against production credentials on the production host.

## Setup and common commands

```bash
source .venv/bin/activate          # standard venv, deps in requirements.txt
pip install -r requirements.txt
pytest                             # run the test suite (fast, no network)

# Collect dated snapshots of a city — BOTH providers by default, same run
# date (per-provider skip if a run <80 days old exists)
python streetscape_tracker.py "Seattle, WA"
python streetscape_tracker.py "Seattle, WA" --provider mapillary   # restrict to one provider
python streetscape_tracker.py "Seattle, WA" --force --run-date 2026-07-02
python streetscape_tracker.py "Seattle, WA" --check-boundary   # preview search area only

# Worldwide sampling frame (stratified ~50-80 cities; see docs/worldwide_sampling.md)
python scripts/build_worldwide_frame.py            # regenerate frame from data_sources/
python scripts/register_frame.py                   # dry-run preview; --execute registers (disabled until boundary-vetted)

# Batch + scheduler
python run_cities.py cities.txt --continue-on-error
python -m streetscape_metadata_tracker.scheduler status
python -m streetscape_metadata_tracker.scheduler run-due --dry-run
# On-demand single-channel catch-up (issue #214) — the ONLY supported bulk path;
# never a detached script. Mapillary catch-ups are PAUSED until #241's rolling
# multi-day guard lands (see docs/provider-access.md). --limit (>= 1) overrides [schedule].max_cities_per_day;
# an unknown/disabled channel or a bad --limit exits 64, not 2. NOTE: this
# advances only the named channels' clocks, so it un-pairs those cities' snapshots.
python -m streetscape_metadata_tracker.scheduler run-due --provider mapillary --limit 40
python -m streetscape_metadata_tracker.scheduler regenerate-aggregate --publish   # rebuild cities.json.gz from the catalog (no collection) and rsync
python -m streetscape_metadata_tracker.scheduler reconcile-walks --dry-run        # catalog road walks that finished but were never registered
# Same-day answer for a partner inquiry about a city we don't track (issue #215):
# register + both road walks + the cheap Mapillary grid run + publish, then print
# the street-km numbers and a city-page link. --estimate stops after the boundary
# and cost report; a bad --provider or an unpaired --width/--height exits 64.
python -m streetscape_metadata_tracker.scheduler assess-city "Newport, Kentucky" --estimate
python -m streetscape_metadata_tracker.scheduler assess-city "Newport, Kentucky" --yes
# Snapshot Google's published driving plan out of band (run-due does it nightly);
# --from-file/--date backfills a hand-saved snapshot
python -m streetscape_metadata_tracker.scheduler fetch-driving-plan [--force]
# Catalog-backup health + inventory of the assets published nowhere (nonzero exit
# when the newest backup is missing, older than 48 h, or the last attempt failed)
python -m streetscape_metadata_tracker.scheduler backup-status
# ...and --alert emails the report when unhealthy (what the daily monitor timer
# runs, issue #193); silent when healthy, exit status unchanged
python -m streetscape_metadata_tracker.scheduler backup-status --alert
# Restore a dated backup; refuses an existing destination or orphaned -wal/-shm
python -m streetscape_metadata_tracker.scheduler restore-backup backups/FILE.backup --to /tmp/recovered.db

# One-time migration of legacy (undated) data files into the catalog
python scripts/migrate_to_db.py            # dry run; --execute to apply

# Manually resize one city's frozen grid (escape hatch for point-box bboxes that
# issue #91's bulk re-registration can't fix). Catalog-only, no API calls; dry
# run by default, and refuses a city with real runs unless --force.
python scripts/resize_city.py "Browning, MT" --width 2500 --height 2500 --execute

# Cap every oversized frozen grid at once (issue #166). Same catalog-only,
# dry-run-by-default contract; skips cities with real dated runs unless
# --include-collected (that breaks their diff continuity — no files are deleted).
python scripts/cap_oversized_grids.py                      # preview
python scripts/cap_oversized_grids.py --execute

# One-time rename of road-walk artifacts collected before streetwalk filenames
# carried a provider token (non-gsv walks only); dry run, --execute to apply
python scripts/repair_streetwalk_names.py

# One-time backfill of street_walks.coverage_by_highway (schema v11, issue #101)
# from coverage artifacts already on disk; no API calls, dry run by default
python scripts/backfill_streetwalk_coverage.py --execute

# One-time backfill of the street_walks absolute-length columns (schema v12),
# same shape; exits nonzero if an artifact's lengths contradict the row's
# already-cataloged coverage percentage (i.e. the wrong artifact was matched)
python scripts/backfill_streetwalk_length.py --execute

# Re-derive every run's stored stats from its CSV under the current analysis
# definitions (dry run by default). The repair handle whenever a stats
# definition moves — most recently issue #213's capture-date columns, whose
# meaning change must be applied to the WHOLE series in one pass;
# --regenerate-json additionally rebuilds the published per-run JSON of runs
# whose CSV holds an impossible capture date
python scripts/recompute_run_stats.py --provider gsv --regenerate-json --execute

# OSM street coverage for an existing run (writes {run_stem}_streets.json.gz)
python -m streetscape_street_analyzer.analyze "Seattle, WA" --provider gsv

# Road-walk street-coverage collection (#99): queries GSV along OSM edges on the
# isolated gsv_streets key; --estimate reports edge/sample/query counts, no key
python -m streetscape_street_analyzer.collect "Seattle, WA" --estimate
python -m streetscape_street_analyzer.collect "Seattle, WA" --spacing 15
# Mapillary road-walk: a tile census + local join; cost tracks bbox AREA (catalog
# median 12 tiles, max 870), not sample count or spacing
python -m streetscape_street_analyzer.collect "Seattle, WA" --provider mapillary
# Walk alleys, footpaths, park trails and cycleways too — a SEPARATE walk series
# from the default 'drive' one, not a replacement (see docs/street-coverage.md)
python -m streetscape_street_analyzer.collect "Seattle, WA" --network-type all_public

# Publish data/ to the UW Makeability Lab web server (rsync over SSH)
./sync_data_to_server.sh --dry-run
```

Credentials in `.env`, loaded by `streetscape_metadata_tracker/config.py` per provider: `GMAPS_API_KEY` (Street View Static API enabled) for gsv, `MAPILLARY_ACCESS_TOKEN` (free client token) for mapillary. The default `--provider both` requires both keys up-front (fail-fast so the series can't drift); a single-provider run needs only its own key. A third provider adds `KARTAVIEW_ACCESS_TOKEN` for kartaview — **required rather than optional**, because KartaView's anonymous tier is 100 requests/hour against 1,000 authenticated, and at 100/h a p95 city is hours and Singapore is days: it is not a slower channel, it is no channel. Two further channels isolate street-coverage *collection* (issue #99) — `GMAPS_STREETS_API_KEY` / `MAPILLARY_STREETS_ACCESS_TOKEN`, separate keys so street experiments can't exhaust the production collectors' quotas, metered under their own `api_usage` provider strings (`gsv_streets` / `mapillary_streets`). `GMAPS_STREETS_API_KEY`/`gsv_streets` is now live — the road-walk collector (below) reads it; `mapillary_streets` stays dormant. Scheduler config lives in `config/scheduler.toml` (stdlib `tomllib`, Python ≥3.11).

## Architecture

Each area below states its rule here and keeps its evidence, incident history and mechanism detail in a `docs/` file. **New detail belongs in the topic doc, not in this file** — this one is loaded into every session and has a hard size limit; a rule that prevents a mistake earns its place here, the forensics that justify it do not.

**Temporal model.** Every run of a city is an immutable dated file `{city_id}_width_W_height_H_step_S[_PROVIDER]_YYYY-MM-DD.csv.gz` plus a sibling `.json.gz` summary (schema v2; carries a `provider` field). **No provider token means gsv** — all pre-provider filenames and published URLs are unchanged. The SQLite catalog `data/streetscape_tracker.db` (`streetscape_metadata_tracker/db.py`, stdlib sqlite3/WAL, no ORM; schema v12, auto-migrated on connect) is the operational source of truth: `cities` (canonical `city_id` + **frozen grid geometry** — future runs never re-geocode, so grids align exactly and diffs are meaningful; geometry is shared by all providers), `city_aliases` (legacy slugs like `albany--ny`), `runs` (UNIQUE(city_id, provider, run_date)), `run_diffs`, `api_usage` (daily budget ledger, keyed by (date, provider)), `schedule_state` (keyed by (city, provider)), `history_harvests` (issue #2), `street_networks` (frozen OSM networks, issue #103), `street_walks` (road-walk collection runs, issue #99; UNIQUE(city_id, provider, network_type, run_date)), `street_walk_diffs` (walk-to-walk street-coverage diffs, issue #101), and `driving_plan_snapshots` + `driving_plan_entries` (Google's published collection plan, issue #176). The DB is local-only, never rsynced.

**Provider model.** Each provider is an independent run series on the same frozen grid. GSV: one metadata request per grid point (nearest pano — a grid *sample*). Mapillary (`download_mapillary.py`): z14 vector tiles (~10–100 requests/city, `mapbox-vector-tile` dep), keeps **every** `is_pano` image assigned to its nearest grid point (a *census*), one CSV row per pano plus ZERO_RESULTS fill; bogus contributor timestamps become NO_DATE. Every provider writes the identical 9-column **core** (`config.METADATA_DTYPES`) and appends its own extras — Mapillary 16 columns in total, KartaView 18, all built by the one `census.build_image_rows` — so coverage rates are cross-provider comparable but raw pano counts are census-vs-sample and are not. `runs.unique_google_panos` is NULL for non-gsv runs. Official-Google classification is an exact `© Google` match (`analysis.is_google_copyright`, shared by stats/JSON/vis and mirrored in `city.js`) — never substring, since photographer names can contain "Google".

**Capture dates: what they describe and how they parse → [`docs/capture-dates.md`](docs/capture-dates.md).** `analysis.dated_unique_panos` is the single seam every date-derived statistic reads — age stats, both histograms, catalog and per-run JSON alike — and it deliberately drops two populations: dates that **cannot be true** (a per-provider floor, and the observation date itself as an inclusive ceiling) and, for gsv, **third-party imagery** (an exact `© Google` match, never a substring, since photographer names can contain "Google"). The CSV is never rewritten — a run file records what the provider said — so the guard has to be repeated at every reader, and each reader must be **at least as permissive as the data on disk**: `fileutils.load_city_csv_file` pins `format="ISO8601"` rather than a strict `%Y-%m-%d` or a format-free inference, because legacy runs carry month precision and inference reads one format off the first non-null value; `city.js` and `vis.py` carry the same widening. The repair handle is `scripts/recompute_run_stats.py`, run over the **whole series in one pass** (a city's run history must not mix two definitions), with `--regenerate-json` because repairing the catalog does not repair the site. Note the trap that cost us #226: a repair tool that reads through the loader inherits the loader's blind spot. The out-of-band GSV capture-history harvester (#2) is documented there too.

**The census seam, and the census providers → [`docs/census.md`](docs/census.md).** A *census* provider returns every image in an area rather than the nearest one to a query point, and that entire pipeline — record → rows → grid assignment → the written CSV — lives **once** in `census.py`, parameterized by a provider's schema and columns. Never copy it into a provider module: the contracts it enforces are invisible in a review of the second copy. It is **columnar, and that is a memory contract** (#157 — Detroit is 19M rows, and per-image dicts cost ~0.74 GB per 1M images), its output is pinned **byte-identical** by a golden fixture because `diff.py` would read a formatting or ordering drift as phantom imagery churn in every Mapillary city, and the `image_columns` contract is **enforced rather than documented**, since each way of getting it wrong publishes a silently wrong column into an immutable dated snapshot. `is_pano` is the shared 360° boolean and is read through `census.census_is_pano`, never as a raw array. Mapillary (z14 vector tiles) and KartaView (a paginated radius sweep, #225/#239) are the two census providers. Imagery-type stratification (#116) yields **two** coverage numbers — 360° and any-imagery — which are never conflated. Four KartaView rules have to survive without a read, because three of them have already been gotten wrong once: **`--provider kartaview` collects (#251), but it is still NOT a scheduler channel** — a `[providers.kartaview]` block is refused rather than accepted, with `run-due`/`assess-city` exiting `USAGE_EXIT_CODE` while one exists, since three arms in `scheduler.py` are fail-open and would price and time it as a GSV grid (#248); **`api_requests` is this process's spend and `api_requests_total` is the sweep's**, because `db.add_api_usage` is additive and keyed by (date, provider), so a resumed night reporting the whole sweep charges last night against tonight's budget gate; **`HTTP 400` is backpressure here, not a malformed request**, and typed permanent it would never be retried or subdivided, so every dense city would collect nothing; and the capture-date rule is **`shot_date >= date_added` → NULL, `>=` and not `>`**.

**Provider access, per-IP limits and blocks → [`docs/provider-access.md`](docs/provider-access.md).** This is what the READ THIS FIRST rule at the top of this file points at; read it before changing any pacing, retry, concurrency, volume or host decision. The rules that must survive without a read: **Mapillary `--limit` catch-ups are PAUSED** until #241's rolling multi-day guard lands — #214's bet that throughput rather than volume was the trigger was **falsified** on 2026-08-20 by a second block arriving while the 60/min limiter was provably pinned, so the limiter is necessary but not sufficient and the operative constraint is a rolling 2–3 day per-IP accumulation window. When they resume, the only supported path is `scheduler run-due --provider mapillary --limit N`, **never a detached script** — the bespoke one that had none of the scheduler's guards got makelab2 banned by both Mapillary and Overpass in a single night — and a filtered run has a real cost, since it un-pairs those cities' snapshots. Three third parties meter us by **IP rather than by credential** — Mapillary's tile CDN, `overpass-api.de` and `kartaview.org` — so a per-process limiter cannot honour them alone and `host_lock.py` serializes them across processes; the exit codes distinguish "the third party refused this IP" (75/76/81) from "another local process holds the lock" (79/80/82), and only the former trips the night-level breaker. **Neither kind of skip records a failure**, because `get_due_cities` filters on `consecutive_failures` and nothing but a success resets it. A blocked or busy night still publishes, alerts unconditionally, and exits nonzero. makelab1 is **not** an escape hatch: Project Sidewalk serves Mapillary data off it, and that trade is never the right one.

**Pipeline per run** (`streetscape_metadata_tracker/cli.py` is the policy layer; `--provider` threads through everything):
1. `db.resolve_city()` — known cities reuse frozen geometry (zero geocoding); unknown cities geocode once via `geoutils.py` (rate-limited Nominatim) and register, with the user's query slug saved as an alias so the same query never re-geocodes. `--check-boundary` uses the same resolution, so preview filenames and geometry match what a real run would produce.
2. Skip policy per (city, provider): `--min-days-since-last-run` (default 80) unless `--force`.
3. Downloader dispatch — `download_gsv.py` (gsv; resumes via a `.downloading` sibling), `download_mapillary.py` (no resume — runs take seconds) or `download_kartaview.py` (resumes via a `checkpoints/` directory, #239, because a metro sweep is hours; the caller supplies that path and discards it once the artifact is durable); provider-agnostic grid/date/error helpers (`generate_grid_points`, `standardize_capture_date`, `DownloadError`) live in `download_common.py` so no provider imports from another's module. Caller supplies the output path; all three return `api_requests` for the per-provider budget ledger, and KartaView adds `api_requests_total` — the sweep's cumulative spend across resumes, which is for the `runs` row and the operator and must **not** reach the additive daily ledger.
4. Guard: a run ≥95% REQUEST_DENIED/OVER_QUERY_LIMIT (`analysis.detect_systemic_failure`) is rejected before cataloging — csv renamed `*.rejected` (excluded from the publish glob), nonzero exit so the scheduler counts a failure. Otherwise `analysis.calculate_run_stats()` + `db.register_run()`.
5. `diff.compute_run_diff()` vs the previous run of the same provider → `run_diffs` row + published detail file (`{city_id}_diff_[PROVIDER_]{FROM}_to_{TO}.csv.gz`; gsv keeps the tokenless form).
6. `json_summarizer.generate_city_metadata_summary_as_json()` — per-run JSON v2, ages pinned to `run_date` (deterministic); gsv runs include the `google_panos` block, other providers only `all_panos`. Then `generate_aggregate_v2()` builds `cities.json.gz` (**schema v3**) from the DB: per city `{city_id, city, providers: {gsv: {latest, runs, change}, mapillary: {...}}}`, with per-provider global histograms.

**Filename parsing is a contract.** `streetscape_metadata_tracker/naming.py` is the single source of truth; its regex accepts all filename generations (legacy `_step_20`, buggy `_step_20.0`, dated, provider-tagged). `sanitize_city_query_str` behavior must never change — canonical `city_id`s and all legacy file slugs depend on it (note: interior periods are preserved, e.g. `st.-louis`). **Every artifact family puts the provider token in the same place** — right after `_step_{S}`, with gsv emitting none so pre-provider names stay byte-identical: run files (`generate_run_filename`) and road-walk files (`generate_streetwalk_filename`) both. A per-(city, provider) artifact that omits the token silently collides the moment two providers are collected on one run date, which is exactly how the streetwalk artifacts were broken — so never construct one of these names by hand (tests and fixtures included; `tests/e2e/build_fixture.py` calls the generator for this reason).

**Scheduler → [`docs/scheduler.md`](docs/scheduler.md).** `run-due` collects the stalest-due cities, runs each enabled channel as its own subprocess under a per-channel daily budget, then runs a tail — aggregate, streetwalk manifest, driving-plan summary, catalog backup, publish. **The tail is what makes a night visible, and it only runs if the city loop returns**, so every way of ending the loop (deadline, SIGTERM wind-down, unexpected exception) returns counters instead of propagating, and each tail artifact reports a crash rather than raising. Publishing happens **only at the end**, so a stale public site usually means the batch died or overran rather than that the publisher broke. A dead output pipe is treated as an ordinary condition in four separate places — but still drive manual batches into a file (`>> logs/x.log 2>&1`), never a pipe. `systemctl stop` is a real wind-down, not a kill, and the unit's `TimeoutStopSec` must stay above the tail's measured components and below `max_batch_hours`.

**Operator commands: same-day assessments and publishing → [`docs/operations.md`](docs/operations.md).** `scheduler assess-city "City, Region"` registers a city and collects both road walks plus the cheap Mapillary grid run so a partner inquiry can be answered the same day. **Answer from street coverage, never grid coverage** — grid points land on water, rail, parkland and rooftops, so the grid figure badly understates what a deployment would get (Highland Heights: 55.6% of grid points, 92.8% of street-km). A rectangle is not a city, and the pre-flight says so before anything is spent. Publishing is declared in config (`[publish].local`), not inherited from the environment, so a hand-run publish and the nightly one take the identical path.

**Catalog backups → [`docs/catalog-backups.md`](docs/catalog-backups.md).** The catalog is the operational source of truth and is never rsynced, so it lives in exactly one place — and lab storage being backed up is something to verify, not assume (it was not, until 2026-08-05). Dated copies go through SQLite's **online backup API**, to a per-writer staging file, verified with `PRAGMA integrity_check`, and only then `os.replace`d, so a snapshot cannot catch a half-written file and a bad copy cannot overwrite a good one. `-wal`/`-shm` sidecars must never outlive their database file. `scheduler backup-status` exits nonzero when the newest copy is missing, stale (>48 h) or the last attempt failed — and it needs a caller other than the scheduler, which is what the separate daily timer is for.

**Street coverage and road walks → [`docs/street-coverage.md`](docs/street-coverage.md).** A second, active collection modality beside the grid: walk each frozen OSM edge, sample every `--spacing` m, and query the provider at each sample, so association is by construction and coverage is fractional per edge. **A sample counts as covered when its status is PRESENT — `OK` *or* `NO_DATE` — never `OK` alone** (`analysis.PRESENT_STATUSES`, the grid's vocabulary): an undated pano is still imagery within reach, so it covers and simply ages nothing. Both halves of `streetscape_street_analyzer` got that wrong independently (#251 finding 9, then #257), and for KartaView the NO_DATE population is large by construction. **Grid coverage and street coverage are different denominators and never substitute for each other** — Seattle reads 54.3% grid against 98.4% street — and each `--network-type` is its own series with its own denominator, never a replacement for another. Both providers walk the same deterministic sample points, but Mapillary reaches them through a tile census joined locally, so its cost tracks bbox **area** rather than sample count. Every per-(city, provider) artifact carries the provider token, and per-network ones the network token: a name that omits one silently collides, and the second collection then skips as a successful no-op. Never hand-build these filenames — use the generators in `naming.py`.

**Google's driving plan, and the join against observed imagery → [`docs/driving-plan.md`](docs/driving-plan.md).** Google publishes where it plans to drive at a single mutable URL it **overwrites in place**, so every revision is permanently unobservable without our own dated archive; a night we fail to snapshot is a revision nobody can recover, which is why that fetch failing is reported rather than passed over in silence. Treat the plan as **advisory, never a contract** in both directions: Google's own note says listed areas may include towns within driving distance, and Israel's rows read `publish=No` with 2018–19 windows while our runs record 2023 captures — so a closed or absent plan row is **never** evidence a city was not driven. The raw feed archive lives outside `data/` (mirroring a vendor's file is not ours to republish); the derived join does publish.

**Web frontend → [`docs/frontend.md`](docs/frontend.md).** Static vanilla JS + Leaflet + Chart.js 4, no build step, fetching the published `data/`. `streetscape-utils.js` holds the provider registry and `adaptCityRecord`, which flattens v1/v2/v3 aggregate records into one normalized shape. `grid.html`/`streets.html`/`driving.html` are configuration over a shared table chassis, not bespoke pages — and that chassis has **no pagination or virtualization**, so a new page's row count is a design constraint, while `createTableControls` **owns the whole query string**, so two instances on one page fight over it — which is why `driving.html` renders unmatched plan areas as a summary rather than a second filtered table. **`grid.html` and `streets.html` are pivoted to one row per CITY, providers as sub-columns (issue #250)** — a row per (city, provider) scattered a city's own series to opposite ends of the table under every sort, defeating the provider comparison the pages exist for; those two filter through per-filter histogram-sliders rather than the sorted-column distribution strip, which `driving.html` keeps. A pivoted row holds one number per provider, so **“Collected by” is a SCOPE, not just a row filter** — it redirects what the numeric filters read, or “coverage over 80%” silently means “some provider's”. Mapillary attribution is required by their ToS.

**Tests → [`docs/testing.md`](docs/testing.md).** `pytest`, fast, no real network — downloader tests substitute an in-memory fetch primitive rather than mocking HTTP, and autouse fixtures neutralize pacing, the host lock, the Overpass probe and the backup directory suite-wide so the suite can run during a live nightly batch. That file records **what each test pins and why**, which is what makes a failing test readable as a contract rather than a puzzle; add to it when you add a test.

## Notes

- **Every measured question gets a writeup in `docs/experiments/`, however small** — including negative, inconclusive and abandoned ones, and including a one-afternoon analysis over data already on disk. "Too small to write up" is not a category: the cost is minutes, and what it prevents is re-running a collection to re-learn an answer we already bought. The **derived** numbers (`{topic}_metrics.json`, any summary CSV, the figures) are **committed** beside the writeup; only the bulk raw collection data is gitignored, in `/experiments/{topic}/` and **never** under `data/`, which the publisher rsyncs to a public web server. A writeup quotes the **distribution** it summarizes — percentiles and n, not just a headline number, since the shape is usually the finding — and every number in it traces to that committed JSON, which in turn must be produced by committed code named in its `generated_by`. A number that contradicts vendor documentation gets flagged, not quietly normalized. The rules, the rationale, and the failure modes that produced them: [`docs/experiments/README.md`](docs/experiments/README.md). Existing writeups: `grid-density.md`, `pano-spacing.md`, `kartaview-feasibility.md`, `kartaview-sweep-cost.md`, `publish-duration.md`, `capture-date-precision.md` — answer "should we sample finer / differently?" from these before re-running anything.

- Architecture decisions are recorded in `docs/adr/`. Notably **ADR 0001: stay fully static, no backend** — the public site has zero server-side runtime by design; large/dense-city rendering (#77, #58) is fixed with static artifacts (grid-binned overview → PMTiles), never a server.
- `data/` contains thousands of files — avoid globbing/listing it wholesale.
- Legacy pre-2026 data files are undated; they're registered as `is_baseline=1` runs by the migration script and are never renamed (published URLs stay stable).
- The sync-vs-async duplicate download path was removed in v2 (`download.py`, `gsv_tracker_single.py`); v1.0.0 tag preserves the old architecture.
- Runtime state that must never reach the public web server lives in siblings of `data/`, all gitignored: `logs/`, `backups/` (#145), `archive/` (#176), `locks/` (#208) and `checkpoints/` (#239). The publish rsync only walks `data/`, so anything here is structurally unpublishable.
- Logs go to `logs/`, never `data/` (data/ is synced to a public web server). Three tiers: the scheduler's own rotating `streetscape_scheduler.log`; a per-attempt `collect_{city_id}_{channel}_{date}.log` holding one collection subprocess's full output (the children `basicConfig` to stderr, so before this their tracebacks were inherited into a systemd journal the service account can't read — every `collection failed` line lost its cause); and `streetscape_service_console.log`, the unit's `StandardOutput=append:` safety net for anything else (uncaught traceback, OOM notice). A failed child's last 25 lines are copied into the scheduler log too, since the `[alerts]` email sends only that log's tail.
- The **worldwide frame** (`docs/worldwide_sampling.md`) is a stratified curated set (~56 cities: `continent × size-band × GSV-coverage-regime`) built deterministically from vendored GeoNames data (CC BY 4.0) in `data_sources/`. `scripts/build_worldwide_frame.py` emits `cities_worldwide.txt` + `worldwide_frame.csv`; GeoNames population is used only for large/small binning, never as a reported variable. `data_sources/` is not rsynced and not git-ignored (unlike `data/`).
