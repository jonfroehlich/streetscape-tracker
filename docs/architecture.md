# Core data model, pipeline and naming

The temporal model, the provider model, the catalog schema, the per-run pipeline, and the filename contract.
Read before touching `db.py`, `naming.py`, `cli.py`, or any code that constructs, parses, or globs an artifact filename.

Moved out of `CLAUDE.md` (2026-08-25); the router keeps this topic's short rules and points here for the detail.
An edit that changes a rule belongs in both files.

## Temporal model

Every run of a city is an immutable dated file `{city_id}_width_W_height_H_step_S[_PROVIDER]_YYYY-MM-DD.csv.gz` plus a sibling `.json.gz` summary (schema v2; carries a `provider` field).
**No provider token means gsv** — all pre-provider filenames and published URLs are unchanged.
The CSV is never rewritten: a run file records what the provider said on that date, and every later correction happens in readers or in the catalog (see [`capture-dates.md`](capture-dates.md) for the canonical example).

Each city's grid geometry is **frozen at registration** — future runs never re-geocode, so grids align exactly and diffs are meaningful; geometry is shared by all providers.
Legacy pre-2026 undated files are registered as `is_baseline=1` runs by `scripts/migrate_to_db.py` and are never renamed, so published URLs stay stable.

## The catalog

The SQLite catalog `data/streetscape_tracker.db` (`streetscape_metadata_tracker/db.py`, stdlib sqlite3/WAL, no ORM; schema v14, auto-migrated on connect) is the operational source of truth.
It is **local-only and never rsynced** — it lives in exactly one place, which is why the dated backups in [`catalog-backups.md`](catalog-backups.md) exist.

| Table | Key / uniqueness | Holds |
|---|---|---|
| `cities` | `city_id` PK | Canonical identity + **frozen grid geometry** (center, width, height, step), `enabled` flag |
| `city_aliases` | `alias_slug` PK | Legacy slugs (e.g. `albany--ny`) → `city_id`, so the same query never re-geocodes |
| `runs` | UNIQUE(city_id, provider, run_date) | Per-run stats incl. the #213 capture-date columns and the v14 census provenance; `unique_google_panos` is NULL for non-gsv runs |
| `run_diffs` | UNIQUE(from_run_id, to_run_id) | Run-to-run change counters + detail filename |
| `api_usage` | PK(usage_date, provider) | Daily request-budget ledger; **additive** (`add_api_usage`); streets channels metered under their own strings (#99) |
| `schedule_state` | PK(city_id, provider) | Stagger day, last attempt/success, `consecutive_failures` (reset only by a success), `member` (per-channel membership, #248) |
| `history_harvests` | UNIQUE(city_id, provider, harvest_date) | Out-of-band GSV capture-history harvests (#2) |
| `street_networks` | UNIQUE(city_id, network_type) | Frozen OSM networks (#103); GraphML lives unpublished under `data/osm_cache/` |
| `street_walks` | UNIQUE(city_id, provider, network_type, run_date) | Road-walk collection runs (#99) — a second modality with its own unit of observation |
| `street_walk_diffs` | UNIQUE(from_walk_id, to_walk_id) | Walk-to-walk street-coverage diffs (#101) |
| `driving_plan_snapshots` | UNIQUE(fetch_date) | One row per fetch of Google's driving-plan feed (#176); the only family not city-keyed |
| `driving_plan_entries` | FK → snapshot | The feed's rows, exploded per (record, district), stored verbatim |

Schema-version history that still matters when reading old rows:
v11 added `street_walks.coverage_by_highway` (#101, backfilled by `scripts/backfill_streetwalk_coverage.py`);
v12 added the `street_walks` absolute-length columns (`length_km`, `length_km_covered`, `length_km_covered_any`) **and `median_covered_age_years`** — an age, not a length, stored because a median cannot be recovered from per-bucket medians (backfilled by `scripts/backfill_streetwalk_length.py`).
For all of those, NULL means "not measured", never zero and never a copy of a sibling column.

v13 added `schedule_state.member` (#248), and it is the **one deliberate exception** to the rule above: here NULL means "fall back to this channel's default membership", not "not measured".
The table of defaults is `scheduler.CHANNEL_DEFAULT_MEMBERSHIP`, deliberately code-side rather than config, so adding a provider token cannot silently enrol the whole catalog — a missing entry is a `KeyError`, not a permissive default.
Every channel scheduled today defaults to member, so the `ALTER TABLE`'s all-NULL fill leaves dueness byte-identical; `kartaview` defaults to non-member, which is what makes it a legal but inert channel until an operator runs `scheduler enroll-city`.
The column is **not** named `enabled` for a mechanical reason: `cities.enabled` already exists, and `get_due_cities` reads `SELECT c.*` through `sqlite3.Row`, where two columns of one name collapse to one key holding the wrong value.

v14 added `census_fetched_by` / `census_fetched_at` to **both** `runs` and `street_walks` (#290), and they are back under the ordinary rule: NULL means "not measured".
They record which channel's credential and ledger actually paid for a shared census and when the provider was observed, which is what makes an `api_requests` of **0** on a fully collected city explicable rather than alarming — see the census-cache section of [`census.md`](census.md).
Every gsv run and walk, every legacy import, and every row salvaged by `_reconcile_orphaned_run`/`_reconcile_orphaned_walk` (which read artifacts off disk and cannot know) keep NULL.
`RunRow` gains the two fields as well, and that is not optional: `_row_to_run` builds `RunRow(**dict(row))` from a `SELECT *`, so a column without a matching field is a `TypeError` on every `get_latest_run` against a migrated catalog rather than a missing feature.

## Provider model

Each provider is an independent run series on the same frozen grid.

GSV issues one metadata request per grid point (nearest pano — a grid *sample*).
Mapillary (`download_mapillary.py`) reads z14 vector tiles (~10–100 requests/city, `mapbox-vector-tile` dep) and keeps **every** `is_pano` image assigned to its nearest grid point (a *census*), one CSV row per pano; bogus contributor timestamps become NO_DATE.
KartaView (`download_kartaview.py`) is the second census provider, a paginated radius sweep (#225/#239), and keeps exactly the same thing — `is_pano` is `projection == "SPHERE"`, and a flat photo never gets a row of its own.
Neither census writes a row per flat image: a grid point covered *only* by flat imagery gets one `FLAT_ONLY` marker instead of ZERO_RESULTS (#116), which is what makes the second, wider **any-imagery** coverage number reportable beside the GSV-comparable 360° one — the two are never conflated, and GSV emits no `FLAT_ONLY` rows at all.
The census pipeline itself — record → rows → grid assignment → written CSV — lives once in `census.py`; see [`census.md`](census.md) for its contracts, and [`capture-dates.md`](capture-dates.md) for the date seam every statistic reads through.

Every provider writes the identical 9-column **core** (`config.METADATA_DTYPES`) and appends its own extras — Mapillary 16 columns in total, KartaView 18, all built by the one `census.build_image_rows`.
So coverage rates are cross-provider comparable, but raw pano counts are census-vs-sample and are not.
`runs.unique_google_panos` is NULL for non-gsv runs.
Official-Google classification is an exact `© Google` match (`analysis.is_google_copyright`, shared by stats/JSON/vis and mirrored in `city.js`) — never a substring, since photographer names can contain "Google".

## Pipeline per run

`streetscape_metadata_tracker/cli.py` is the policy layer; `--provider` threads through everything.
The steps below are per (city, provider, run_date):

1. `db.resolve_city()` — known cities reuse frozen geometry (zero geocoding);
   unknown cities geocode once via `geoutils.py` (rate-limited Nominatim) and register, with the user's query slug saved as an alias so the same query never re-geocodes.
   `--check-boundary` uses the same resolution, so preview filenames and geometry match what a real run would produce.
2. Skip policy per (city, provider): `--min-days-since-last-run` (default 80) unless `--force`.
3. Downloader dispatch — `download_gsv.py` (gsv; resumes via a `.downloading` sibling) or the two census providers, `download_mapillary.py` (#256) and `download_kartaview.py` (#239), which **both resume via a `checkpoints/` directory**:
   the caller supplies that path — keyed on the CHANNEL, and for a walk on its `--network-type` too, since a road walk crawls the same frozen bbox and would otherwise resume the grid run's crawl into the wrong ledger (or the other walk's, into the wrong row) — and discards it once the artifact is durable.
   The caller also supplies a `census_cache/` path (#290), keyed the opposite way — on the PROVIDER, city and bbox, with no channel, variant or date — into which a COMPLETED crawl is promoted so the next consumer of that observation reads it for zero requests.
   Provider-agnostic grid/date/error helpers (`generate_grid_points`, `standardize_capture_date`, `DownloadError`) live in `download_common.py`, and the shared checkpoint plumbing in `checkpointing.py`, so no provider imports from another's module.
   Caller supplies the output path;
   all three return `api_requests` for the per-provider budget ledger, and both census providers add `api_requests_total`
   — the sweep's cumulative spend across resumes, which is for the `runs` row and the operator and must **not** reach the additive daily ledger.
4. Guard: a run ≥95% REQUEST_DENIED/OVER_QUERY_LIMIT (`analysis.detect_systemic_failure`) is rejected before cataloging
   — csv renamed `*.rejected` (excluded from the publish glob), nonzero exit so the scheduler counts a failure.
   Otherwise `analysis.calculate_run_stats()` + `db.register_run()`.
5. `diff.compute_run_diff()` vs the previous run of the same provider → `run_diffs` row + published detail file (`{city_id}_diff_[PROVIDER_]{FROM}_to_{TO}.csv.gz`; gsv keeps the tokenless form).
6. `json_summarizer.generate_city_metadata_summary_as_json()` — per-run JSON v2, ages pinned to `run_date` (deterministic); gsv runs include the `google_panos` block, other providers only `all_panos`.
   Then `generate_aggregate_v2()` builds `cities.json.gz` (schema v3) from the DB: per city `{city_id, city, providers: {gsv: {latest, runs, change}, mapillary: {...}}}`, with per-provider global histograms.

## The filename contract

`streetscape_metadata_tracker/naming.py` is the single source of truth for generating and parsing artifact filenames; nothing else may construct one.
Four filename generations exist on disk and its regex must accept all of them:

| Generation | Example |
|---|---|
| Legacy undated | `seattle--wa_width_1000_height_1000_step_20.csv.gz` |
| Legacy buggy float step | `seattle--wa_width_1000_height_1000_step_20.0.csv.gz` |
| Dated (gsv, current) | `seattle--washington--united-states_width_1000_height_1000_step_20_2026-07-02.csv.gz` |
| Dated + provider token | `seattle--washington--united-states_width_1000_height_1000_step_20_mapillary_2026-07-02.csv.gz` |

`sanitize_city_query_str` behavior must never change — canonical `city_id`s and all legacy file slugs depend on it (note: interior periods are preserved, e.g. `st.-louis`).

**Every artifact family puts the provider token in the same place** — right after `_step_{S}`, with gsv emitting none so pre-provider names stay byte-identical: run files (`generate_run_filename`) and road-walk files (`generate_streetwalk_filename`) both.
A per-(city, provider) artifact that omits the token silently collides the moment two providers are collected on one run date, which is exactly how the streetwalk artifacts were broken: the second channel's collection then skips as a successful no-op.
So never construct one of these names by hand — tests and fixtures included; `tests/e2e/build_fixture.py` calls the generator for this reason.

## Published artifact schema versions

Every published JSON artifact carries a `schema_version`; the frontend's `adaptCityRecord` normalizes across aggregate versions (see [`frontend.md`](frontend.md)).

| Artifact | File | Version |
|---|---|---|
| Per-run summary | `{base}.json.gz` | 2 |
| Aggregate | `cities.json.gz` | 3 |
| Streetwalk manifest | `streetwalks.json.gz` | 1 |
| Driving-plan summary | `driving_plan.json.gz` | 1 |

The streetwalk manifest's version deliberately stayed 1 across the v12 catalog additions: every one of them is additive, and no existing key changed shape or meaning.
The four scalar v12 keys (`length_km`, `length_km_covered`, `length_km_covered_any`, `median_covered_age_years`) are written unconditionally, so on a walk cataloged before v12 they are **present carrying `null`**, not absent; only the optional `coverage_by_highway` and `change` blocks are omitted when they have nothing to say.
So key presence is never a version probe, and a null length means "this walk has no length cataloged" — never "this manifest predates lengths".
One v1 manifest legitimately holds both, since a pre-v12 walk reads NULL until `scripts/backfill_streetwalk_length.py` runs.
