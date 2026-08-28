# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in this repository.
It is a **router**: each section carries the short, mistake-preventing rules and points at a `docs/` file for the evidence, incident history and mechanism detail.
**New detail belongs in the topic doc, not here** — this file is loaded into every session and has a hard size limit; a rule that prevents a mistake earns its place, the forensics that justify it do not (enforced by `tests/test_claude_md_router.py`).

## What this project is

Streetscape Tracker analyzes street-level imagery coverage and temporal patterns in cities **over time**.
Three providers are collectable — Google Street View (GSV, the default), Mapillary (360° panos only), and KartaView.
GSV and Mapillary run nightly over **every enabled city**; KartaView is a scheduler channel too but **opt-in**, collecting only the cities an operator enrolled with `scheduler enroll-city` (#248), because one whole-catalog pass prices at ~186,000 requests ≈ 186 h.
The tool samples a geographic grid around a city center, queries each provider's metadata API, and produces immutable dated snapshots per (city, provider), run-to-run change summaries (panos added/removed, capture-date changes, coverage deltas), and interactive map visualizations.

## READ THIS FIRST: provider API access is the single point of failure

**This project is about acquiring data: if we cannot reach the provider APIs, nothing else functions**, and a missed month is a permanent hole in the temporal series that no later run can backfill.

**Before writing or deploying ANYTHING that changes how, how often, how fast, or from where we call a provider — new collectors, pacing, retry/backoff, concurrency, bulk catch-ups, host or IP migrations — you MUST first read that provider's official API docs AND its developer/community forums.** Before, not after a failure.

| Provider | Official docs | Community |
|---|---|---|
| GSV | [Street View Static API usage & billing](https://developers.google.com/maps/documentation/streetview/usage-and-billing) + the API's own docs | Google Maps Platform issue tracker; Stack Overflow `google-street-view` tag |
| Mapillary | [API documentation](https://www.mapillary.com/developer/api-documentation), incl. its rate-limits section | [forum.mapillary.com](https://forum.mapillary.com) — **not optional**: Mapillary's real operational limits are undocumented and described only there |

**The documented limit is not necessarily the binding one, and the forum is where you learn that.**
The 2026-08-12 case study: an undocumented per-IP throttle (302 → login) blocked both our Mapillary apps at ~21% of the documented per-app daily cap, and a forum thread had already described that exact failure, its per-IP scope, and its retry hazard before we sustained 370 req/min into it.
The full record — what is measured vs reported vs guessed, and every pacing and budget decision it forced — is in [`docs/provider-access.md`](docs/provider-access.md), whose opening section is the authoritative telling of this rule; an edit to either belongs in both.

Corollary: **treat any behavior you cannot find documented as unknown rather than unlimited**, pace conservatively by default, and never resolve "is this too fast?" by experiment against production credentials on the production host.

## Commands

```bash
source .venv/bin/activate          # standard venv, deps in requirements.txt
pytest                             # fast, no real network

# Collect dated snapshots — gsv,mapillary by default; per-provider skip if a run <80 days old exists
python streetscape_tracker.py "Seattle, WA"
python streetscape_tracker.py "Seattle, WA" --provider mapillary
python streetscape_tracker.py "Seattle, WA" --force --run-date 2026-07-02
python streetscape_tracker.py "Seattle, WA" --check-boundary        # preview search area only
python run_cities.py cities.txt --continue-on-error                 # batch

# Street coverage: analysis of an existing run, and road-walk collection (#99)
python -m streetscape_street_analyzer.analyze "Seattle, WA" --provider gsv
python -m streetscape_street_analyzer.collect "Seattle, WA" --estimate      # cost preview, no key used
python -m streetscape_street_analyzer.collect "Seattle, WA" --spacing 15
python -m streetscape_street_analyzer.collect "Seattle, WA" --provider mapillary
python -m streetscape_street_analyzer.collect "Seattle, WA" --network-type all_public   # a SEPARATE walk series, not a replacement

# Worldwide sampling frame (docs/worldwide_sampling.md)
python scripts/build_worldwide_frame.py
python scripts/register_frame.py   # dry-run preview; --execute stays disabled until boundary-vetted

# Publish data/ to the UW Makeability Lab web server (rsync over SSH)
./sync_data_to_server.sh --dry-run
```

### Scheduler (`python -m streetscape_metadata_tracker.scheduler <cmd>`)

| Subcommand | Purpose |
|---|---|
| `status` | Per-city schedule and budget status |
| `assign` | (Re)compute stagger assignments (writes `day_of_cycle` only, so it never un-enrolls a member) |
| `enroll-city CITY --channel C` | Opt one city into an **opt-in** channel's queue (#248); `--remove`/`--clear`/`--list` |
| `run-due [--dry-run]` | The nightly batch: collect stalest-due cities per channel, then the tail (aggregate, manifests, backup, publish) |
| `run-due --provider mapillary --limit 40` | On-demand single-channel catch-up (#214) — the ONLY supported bulk path; **Mapillary catch-ups are PAUSED** (see provider access below) |
| `assess-city "Newport, Kentucky" --estimate` | Same-day answer for a partner inquiry about an untracked city (#215); `--estimate` stops after the boundary and cost report, `--yes` runs it |
| `regenerate-aggregate [--publish]` | Rebuild `cities.json.gz` from the catalog (no collection), optionally rsync |
| `reconcile-walks [--dry-run]` | Catalog road walks that finished but were never registered |
| `fetch-driving-plan [--force]` | Snapshot Google's published driving plan out of band; `--from-file`/`--date` backfills a hand-saved snapshot |
| `backup-status [--alert]` | Catalog-backup health; nonzero when the newest backup is missing, >48 h old, or the last attempt failed; `--alert` emails when unhealthy (#193 daily timer) |
| `restore-backup FILE --to PATH` | Restore a dated backup; refuses an existing destination or orphaned `-wal`/`-shm` |
| `notify-failure` | Email the recent log (the systemd `OnFailure=` hook) |

`run-due` notes: `--limit` (≥1) overrides `[schedule].max_cities_per_day`; an unknown/disabled channel or a bad `--limit` exits 64, not 2; a filtered run advances only the named channels' clocks, **un-pairing those cities' snapshots**.
`assess-city` notes: a bad `--provider` or an unpaired `--width`/`--height` exits 64; answer from **street coverage, never grid coverage** (see operations below).
`enroll-city` notes: it only accepts a channel whose default membership is OFF — per-city exclusion on the other four is `cities.enabled`; an unknown channel, a default-membership channel, an unresolvable city or a disabled city exits 64 writing no row; enrolling BEFORE the channel is configured is supported on purpose (it prints a note), or the rollout order is impossible.

### One-time and repair scripts (`scripts/`)

All are catalog/disk-only (no API calls), dry-run by default, and take `--execute`.

| Script | Purpose |
|---|---|
| `migrate_to_db.py` | One-time migration of legacy undated data files into the catalog |
| `resize_city.py "City" --width W --height H` | Manually resize one city's frozen grid (escape hatch for what #91's bulk pass can't fix); refuses a city with real runs unless `--force` |
| `cap_oversized_grids.py [--include-collected]` | Cap every oversized frozen grid at once (#166); including collected cities breaks their diff continuity (no files are deleted) |
| `repair_streetwalk_names.py` | One-time rename of road-walk artifacts collected before streetwalk filenames carried a provider token |
| `backfill_streetwalk_coverage.py` | Backfill `street_walks.coverage_by_highway` (schema v11, #101) from artifacts on disk |
| `backfill_streetwalk_length.py` | Backfill the schema v12 `street_walks` columns; exits nonzero if an artifact's lengths contradict the row's cataloged coverage (wrong artifact matched) |
| `recompute_run_stats.py --provider gsv --regenerate-json` | Re-derive every run's stored stats from its CSV under the current analysis definitions — the repair handle whenever a stats definition moves (#213); a definition change is applied to the WHOLE series in one pass |

The boundary-audit workflow (does a frozen grid actually fit its city?) is a four-script chain, each with a pinning test: `audit_city_boundaries.py` → `build_boundary_review.py` → `apply_decisions.py` → `reregister_boundaries.py`.

### Credentials and config

Credentials live in `.env`, loaded per channel by `streetscape_metadata_tracker/config.py`:

| Channel | Env var | Notes |
|---|---|---|
| `gsv` | `GMAPS_API_KEY` | Street View Static API enabled |
| `mapillary` | `MAPILLARY_ACCESS_TOKEN` | Free client token |
| `kartaview` | `KARTAVIEW_ACCESS_TOKEN` | **Required, not optional**: anonymous is 100 req/h vs 1,000 authenticated — at 100/h a p95 city is hours and Singapore is days, so it is not a slower channel, it is no channel. Scheduled but **opt-in** (#248) |
| `gsv_streets` | `GMAPS_STREETS_API_KEY` | Isolated street-collection key (#99) with its own `api_usage` string, so street experiments can't exhaust production quotas; **live** |
| `mapillary_streets` | `MAPILLARY_STREETS_ACCESS_TOKEN` | Same isolation; **dormant** |

A run requires EVERY named provider's key up-front, `--provider all` included (fail-fast so the series can't drift); a single-provider run needs only its own key.

Three `--provider` flags exist with **different vocabularies** — never conflate them:

| Surface | Accepts | Shape |
|---|---|---|
| `streetscape_tracker.py --provider` | `gsv`, `mapillary`, `kartaview`, `all`; the retired `both` still works, with a notice (#247) | Comma-separated list; default `gsv,mapillary` |
| `scheduler run-due --provider` | The five scheduled channels: `gsv`, `gsv_streets`, `kartaview`, `mapillary`, `mapillary_streets` | Repeatable or comma-separated; no `all` or `both` |
| `scheduler assess-city --provider` | `gsv_streets`, `mapillary`, `mapillary_streets` — the GSV grid run is never part of it | Repeatable or comma-separated |

`--provider` is always a channel LIST, never a keyword whose meaning drifts with the provider count (#247).

Scheduler config is TOML (stdlib `tomllib`, Python ≥3.11): `config/scheduler.toml` is the repo default, and **production reads `config/scheduler.makelab1.toml`**, which diverges materially (budgets, paths, publishing) — editing the repo default alone changes nothing in production.

## Architecture

Each area below states its rules here and keeps its evidence in a `docs/` file.

**Data model, pipeline and naming → [`docs/architecture.md`](docs/architecture.md).**
Every run is an immutable dated snapshot on the city's **frozen grid geometry** (never re-geocoded, shared by all providers, so diffs are meaningful); **no filename provider token means gsv**, so all pre-provider names and published URLs are unchanged.
The SQLite catalog `data/streetscape_tracker.db` (schema v13, auto-migrated on connect) is the operational source of truth and is **local-only, never rsynced**.
`schedule_state.member` (v13, #248) is the one column where **NULL does not mean "not measured"** — it means "use `scheduler.CHANNEL_DEFAULT_MEMBERSHIP[channel]`", which is code-side so a new provider token cannot silently enrol the catalog (a missing entry is a `KeyError`, never a permissive default).
Each provider is an independent run series on the same grid: GSV is a *sample* (nearest pano per grid point), Mapillary and KartaView are *censuses* — so coverage rates are cross-provider comparable and raw pano counts are not.
Official-Google classification is an exact `© Google` match (`analysis.is_google_copyright`, mirrored in `city.js`), never a substring, since photographer names can contain "Google".
`streetscape_metadata_tracker/naming.py` is the single source of truth for filenames; `sanitize_city_query_str` must never change (canonical `city_id`s and legacy slugs depend on it).
**Every per-(city, provider) artifact puts the provider token right after `_step_{S}`** (gsv emits none): a name that omits it silently collides the moment two providers share a run date, and the second collection then skips as a successful no-op — so **never hand-build these names**, tests and fixtures included (`tests/e2e/build_fixture.py` calls the generators).
A run ≥95% REQUEST_DENIED/OVER_QUERY_LIMIT is rejected before cataloging (renamed `*.rejected`, excluded from publishing, nonzero exit).

**Capture dates → [`docs/capture-dates.md`](docs/capture-dates.md).**
`analysis.dated_unique_panos` is the single seam every date-derived statistic reads; it drops dates that cannot be true (a per-provider floor, the observation date as an inclusive ceiling) and, for gsv, third-party imagery.
The CSV is never rewritten — a run file records what the provider said — so the guard repeats at every reader, and each reader must be **at least as permissive as the data on disk**: `fileutils.load_city_csv_file` pins `format="ISO8601"` because legacy runs carry month precision, and a strict or inferred format silently nulls whole series (#226); `city.js` and `vis.py` carry the same widening.
The repair handle is `scripts/recompute_run_stats.py` over the **whole series in one pass** (a city's history must not mix two definitions), with `--regenerate-json` because repairing the catalog does not repair the site — and a repair tool that reads through the loader inherits the loader's blind spot (#226).
The out-of-band GSV capture-history harvester (#2) is documented there too.

**The census seam and the census providers → [`docs/census.md`](docs/census.md).**
The census pipeline — record → rows → grid assignment → written CSV — lives **once** in `census.py`, parameterized per provider; never copy it into a provider module, because the contracts it enforces are invisible in a review of the second copy.
It is columnar (a memory contract, #157), pinned byte-identical by a golden fixture (a formatting drift reads as phantom imagery churn in every diff), and the `image_columns` contract is enforced rather than documented.
`is_pano` is read through `census.census_is_pano`, never as a raw array; imagery-type stratification (#116) yields **two** coverage numbers — 360° and any-imagery — which are never conflated.
Four KartaView rules that must survive without a read:

- **`kartaview` IS a scheduler channel now (#248), and the only OPT-IN one** — declaring `[providers.kartaview]` enrolls nobody; its nightly queue is exactly the cities an operator ran `enroll-city` on, because a whole-catalog pass is ~186,000 requests ≈ 186 h.
  `_collect_due` hoists a city due *only* on an opt-in channel to the head of the slate (`all`, not `any`) — without which the channel would be scoped but never reached, since the union is gsv-ordered and the city cap truncates from the tail.
  It is also the fifth channel, so the effective `max_concurrent_channels` ceiling is **4 of 5** (`HOST_KARTAVIEW` is shared with nothing) — that figure is a property of the channel set's host graph, never a constant.
  Its cost arms ARE wired (#238): the estimate is the swept-circle lattice × the measured **1.80×**, never the GSV grid formula, and the previous run's observed `runs.api_requests` outranks that geometry as the **larger** of the two, never on its own.
- **`api_requests` is this process's spend and `api_requests_total` is the sweep's** — `db.add_api_usage` is additive and keyed by (date, provider), so a resumed night reporting the whole sweep would charge last night against tonight's budget gate.
- **HTTP 400 is backpressure here, not a malformed request** — typed permanent it would never be retried or subdivided, and every dense city would collect nothing.
- The capture-date rule is **`shot_date >= date_added` → NULL — `>=`, not `>`**.

**Provider access, per-IP limits and blocks → [`docs/provider-access.md`](docs/provider-access.md).**
This is what READ THIS FIRST points at; read it before changing any pacing, retry, concurrency, volume or host decision.

- **Mapillary `--limit` catch-ups are PAUSED** until #241's rolling multi-day guard lands: the second block (2026-08-20) arrived while the 60/min limiter was provably pinned, so the limiter is necessary but not sufficient and the operative constraint is a rolling 2–3 day per-IP accumulation window.
- When they resume, the only supported path is `scheduler run-due --provider mapillary --limit N`, **never a detached script** — the bespoke one with none of the scheduler's guards got makelab2 banned by Mapillary and Overpass in one night.
- Three third parties meter by **IP, not credential** — Mapillary's tile CDN, `overpass-api.de`, `kartaview.org` — so a per-process limiter cannot honour them alone; `host_lock.py` serializes them across processes.
- Exit-code families, **none of which records a scheduler failure** (`get_due_cities` filters on `consecutive_failures`, and nothing but a success resets it):

| Family | Codes | Meaning |
|---|---|---|
| Blocked | 75 / 76 / 81 | The third party refused this IP — trips the night-level breaker |
| Busy | 79 / 80 / 82 | Another local process holds the host lock |
| Sweep incomplete | 83 | A checkpointed partial sweep (#239) — the budget or deadline ran out, not a host condition; amnestied beside the host conditions (#238), while a SIGKILL has no exit code and still counts a failure, so kill-and-resume is bounded at five nights |

- A blocked or busy night still publishes, alerts unconditionally, and exits nonzero.
- makelab1 is **not** an escape hatch: Project Sidewalk serves Mapillary data off it, and that trade is never the right one.

**Scheduler → [`docs/scheduler.md`](docs/scheduler.md).**
`run-due` collects the stalest-due cities per enabled channel under per-channel daily budgets, then runs a tail — aggregate, streetwalk manifest, driving-plan summary, catalog backup, publish.
Channels run back-to-back, or concurrently in host-disjoint lanes when `[schedule].max_concurrent_channels` > 1 (default 1; channels sharing a per-IP host never overlap, so the effective ceiling is 4 of 5, and raising it in prod is gated only on verifying the two GSV keys live in separate Cloud projects).
**The tail is what makes a night visible, and it only runs if the city loop returns** — every way of ending the loop (deadline, SIGTERM wind-down, unexpected exception) returns counters instead of propagating, and each tail artifact reports a crash rather than raising.
**Publishing happens only at the end**, so a stale public site usually means the batch died or overran, not that the publisher broke.
Drive manual batches into a file (`>> logs/x.log 2>&1`), never a pipe.
`systemctl stop` is a real wind-down, not a kill; the unit's `TimeoutStopSec` must stay above the tail's measured components and below `max_batch_hours`.
Deployment lives in `deploy/` (5 systemd units + its README).

**Operator commands and publishing → [`docs/operations.md`](docs/operations.md).**
`assess-city` answers a partner inquiry about an untracked city the same day (register + both road walks + the cheap Mapillary grid run + publish).
**Answer from street coverage, never grid coverage** — grid points land on water, rail, parkland and rooftops, badly understating a deployment (Highland Heights: 55.6% of grid points vs 92.8% of street-km); a rectangle is not a city, and the pre-flight says so before anything is spent.
Publishing is declared in config (`[publish].local`), never inherited from the environment, so a hand-run publish and the nightly one take the identical path.

**Catalog backups → [`docs/catalog-backups.md`](docs/catalog-backups.md).**
The catalog lives in exactly one place, and lab storage being backed up is something to **verify, not assume**.
Dated copies go through SQLite's online backup API to a per-writer staging file, are verified with `PRAGMA integrity_check`, and only then `os.replace`d — a snapshot cannot catch a half-written file, and a bad copy cannot overwrite a good one.
`-wal`/`-shm` sidecars must never outlive their database file.
`scheduler backup-status` needs a caller other than the scheduler itself — that is the separate daily timer.

**Street coverage and road walks → [`docs/street-coverage.md`](docs/street-coverage.md).**
The second active collection modality beside the grid: walk each frozen OSM edge, sample every `--spacing` m, query the provider per sample — association by construction, coverage fractional per edge.
**A sample counts as covered when its status is PRESENT — `OK` *or* `NO_DATE`, never `OK` alone** (`analysis.PRESENT_STATUSES`, the grid's vocabulary): an undated pano still covers, it just ages nothing — and undated imagery arrives in batches, so the per-run MAX is the number that matters, not the pooled share (#251, #257).
**Grid coverage and street coverage are different denominators and never substitute for each other** (Seattle: 54.3% grid vs 98.4% street), and each `--network-type` is its own series with its own denominator, never a replacement for another.
Mapillary walks the same deterministic sample points via a tile census joined locally, so its cost tracks bbox **area**, not sample count or spacing.
Artifact names carry the provider token, and per-network ones the network token — generators in `naming.py` only.

**Google's driving plan → [`docs/driving-plan.md`](docs/driving-plan.md).**
Google overwrites its published plan at a single mutable URL, so every revision is permanently unobservable without our own dated archive — which is why a failed nightly fetch is reported, never silent.
The plan is **advisory, never a contract** in both directions: a closed or absent row is never evidence a city was not driven (Israel's rows say `publish=No` with 2018–19 windows; our runs record 2023 captures).
The raw feed archive lives outside `data/` (mirroring a vendor's file is not ours to republish); the derived join (`driving_plan.json.gz`) is published deliberately.

**Web frontend → [`docs/frontend.md`](docs/frontend.md).**
Static vanilla JS + Leaflet + Chart.js 4 in `www/`, no build step, fetching the published `data/`.
`www/js/streetscape-utils.js` holds the provider registry and `adaptCityRecord`, which flattens v1/v2/v3 aggregate records into one normalized shape.
It also holds `addBasemapLayer` and the CARTO key, which is **the one place a tile URL is built** — because CARTO enforces its key by watermarking the tile rather than by refusing the request: a keyless request, a wrong key and the right key under the wrong parameter name all return HTTP 200 and a byte-identical PNG stamped "API KEY REQUIRED", so no error handling anywhere can see it and a duplicated call site renders perfectly, watermarked.
The key is bearer-style (the issuing domain is measurably not enforced), so exposure is unavoidable but abuse is not; the detection path is `tests/e2e/test_basemap_key.py`, which compares a keyed tile against a keyless one by bytes.
`grid.html`/`streets.html`/`driving.html` are configuration over one shared table chassis with **no pagination or virtualization** (a new page's row count is a design constraint), and `createTableControls` (`www/js/table-controls.js`) **owns the whole query string** — two instances on one page fight over it, which is why `driving.html` renders unmatched plan areas as a summary rather than a second filtered table.
`grid.html` and `streets.html` are pivoted to **one row per city**, providers as sub-columns (#250), so "Collected by" is a **scope, not just a row filter** — it redirects what the numeric filters read, or "coverage over 80%" silently means "some provider's".
Anything fanning out over the provider registry must gate on presence in the payload — a registered provider is not a collected one.
Mapillary attribution is required by their ToS.

**Tests → [`docs/testing.md`](docs/testing.md).**
`pytest`, fast, no real network: downloader tests substitute an in-memory fetch primitive rather than mocking HTTP, and autouse fixtures neutralize pacing, the host lock, the Overpass probe and the backup directory suite-wide, so the suite can run during a live nightly batch.
That doc records **what each test pins and why**; when you add a test, add to the section it belongs to, so two PRs adding tests to different subsystems do not collide.

**Markdown conventions.**
Write every markdown file with **semantic line breaks** — one sentence, or one independent clause, per line — because git merges line by line, and a paragraph written as one long line conflicts every time two branches touch it (#254; `test_no_prose_line_is_long_enough_to_be_unmergeable` refuses prose lines over 700 chars).
Markdown joins consecutive lines back into one paragraph, so this changes nothing that renders.
Keep any list a doc enumerates **alphabetical**, so two branches adding an entry usually insert at different offsets and merge rather than collide.

## Notes

- **Every measured question gets a writeup in `docs/experiments/`, however small** — including negative, inconclusive and abandoned ones; "too small to write up" is not a category.
  The derived numbers (`{topic}_metrics.json`, summary CSVs, figures) are **committed** beside the writeup; only bulk raw collection data is gitignored, in `/experiments/{topic}/` and **never** under `data/`, which is rsynced to a public web server.
  A writeup quotes the **distribution** it summarizes (percentiles and n — the shape is usually the finding), every number traces to committed JSON produced by committed code named in its `generated_by`, and a number contradicting vendor documentation gets flagged, not quietly normalized.
  Rules, rationale and failure modes: [`docs/experiments/README.md`](docs/experiments/README.md).
  Existing writeups — keep this list alphabetical, and answer "should we sample finer or differently?" from them before re-running anything:

  - `capture-date-precision.md`
  - `carto-basemap-key.md`
  - `grid-density.md`
  - `kartaview-feasibility.md`
  - `kartaview-sweep-cost.md`
  - `pano-spacing.md`
  - `publish-duration.md`
  - `undated-imagery-share.md`

- Architecture decisions are recorded in `docs/adr/` — notably **ADR 0001: stay fully static, no backend**; large/dense-city rendering (#77, #58) is fixed with static artifacts (grid-binned overview → PMTiles), never a server.
- Published JSON artifacts and their schema versions are inventoried in [`docs/architecture.md`](docs/architecture.md): per-run summary v2, aggregate `cities.json.gz` v3, streetwalk manifest v1, driving-plan summary v1.
- `data/` contains thousands of files — avoid globbing or listing it wholesale.
- Legacy pre-2026 data files are undated; they are registered as `is_baseline=1` runs by the migration script and never renamed (published URLs stay stable).
- Runtime state that must never reach the public web server lives in gitignored siblings of `data/` — `logs/`, `backups/` (#145), `archive/` (#176), `locks/` (#208), `checkpoints/` (#239, #256) — and the publish rsync only walks `data/`, so anything there is structurally unpublishable.
- Logs go to `logs/`, never `data/`, in three tiers: the scheduler's own rotating `streetscape_scheduler.log`; a per-attempt `collect_{city_id}_{channel}_{date}.log` holding one collection subprocess's full output (a failed child's last 25 lines are also copied into the scheduler log, whose tail is what the `[alerts]` email sends); and `streetscape_service_console.log`, the unit's `StandardOutput=append:` safety net for anything else.
- The **worldwide frame** ([`docs/worldwide_sampling.md`](docs/worldwide_sampling.md)) is a stratified curated set (~56 cities: continent × size-band × GSV-coverage-regime) built deterministically from vendored GeoNames data (CC BY 4.0) in `data_sources/` (not rsynced, not git-ignored, unlike `data/`); GeoNames population is used only for binning, never as a reported variable.
- The sync-vs-async duplicate download path was removed in v2; the v1.0.0 tag preserves the old architecture.
