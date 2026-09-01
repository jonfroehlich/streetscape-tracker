# The census seam, and the census providers

How a census provider (every image in an area, rather than the nearest one to a query point) becomes
METADATA-schema rows, and what the shared seam guarantees. Read before adding a provider or touching
`census.py`, `download_mapillary.py` or `download_kartaview.py`.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## The census machinery is provider-agnostic and lives in `census.py`

**The census machinery is provider-agnostic and lives in `census.py`, not in a provider module.** A *census* provider returns every image in an area rather than the nearest one to a query point, so the pipeline from decoded records to METADATA-schema rows is one shape shared by Mapillary's vector tiles and any later census provider.
`streetscape_metadata_tracker/census.py` holds it once — `records_to_census`/`census_column`, `concat_census`, `dedupe_census`, `status_for_capture_dates`, `build_image_rows`, `build_empty_rows`
— parameterized by a provider's *schema* (census dtypes, output dtypes) and its *columns* (the copyright convention plus its extras, supplied as an `image_columns(picked)` callable).
**The seam covers the whole back half, not just record→rows**: `build_grid`/`CensusGrid` and `write_census_grid_run` are the ~150 lines from a fetched census to the written run CSV
— grid assignment, the pano / FLAT_ONLY / ZERO_RESULTS three-frame build, the sequential gzip write, the read-back
— so `download_mapillary_metadata_async` is now a ~60-line wrapper (preamble → grid → its own fetch → the shared tail) and a second provider's is the same shape.
Three provider bindings parameterize it, two of them the `(picked)`-shaped callables above plus `capture_dates_for(census, positions)`, which takes **positions rather than a taken sub-frame** precisely so a provider indexes the one or two date columns it needs instead of materializing a multi-million-row census a second time.
**The census is popped out of the fetch-result dict, making the tail its sole owner**: it drops the frame before the CSV writes, so a caller that also bound `census = fetched["census"]` would pin all 19M Detroit rows alive through both writes and the release would buy nothing
— with every runtime test still green, which is why `tests/test_census.py` asserts the caller's *source* is free of that binding
— and finds the callers by **sweeping the repo** for `write_census_grid_run` rather than reading a registry, so there is nothing for a new collector to register in, but a collector living outside its `_PACKAGES` (`streetscape_metadata_tracker`, `streetscape_street_analyzer`, `scripts`) is never checked at all.
The banned spellings include `.get("census")` and `.copy()` — the latter pins a whole second census, so a guard matching only the bare subscript would wave through the worse bug
— while an inline scalar read (`len(fetched["census"])`) stays allowed.
The grid geodesy those functions need — `grid_bbox`, `assign_to_grid`, `_meters_per_degree`
— moved down to `download_common.py` beside `generate_grid_arrays`, since it is pure lattice math that merely happened to live in the Mapillary module and a second provider was already reaching it through a function-local cross-provider import;
`download_mapillary` re-exports the two public names plus `_M_PER_DEG_LAT` in the redundant `X as X` form (which is what marks a re-export to ruff) so no call site moved;
`_meters_per_degree` is deliberately NOT re-exported, having no caller outside `download_common`.
**The read side of the naming contract is the token, not the caller's memory.** pandas ignores dtype keys a file lacks, but a column present in the file and **absent** from the mapping is *inferred*
— which turns KartaView's nullable-`Int64` `sequence_index` into float64 and its numeric-looking string `way_id` into a float, differently depending on which module opened the file.
So `fileutils.load_city_csv_file` takes a `dtypes=` argument *and* defaults it from the file's own provider token (`fileutils.dtypes_for_run_path` → `config.PROVIDER_RUN_DTYPES`), covering run files and road-walk snapshots alike;
an unparseable name keeps the historical Mapillary default, which is a superset of the shared core so GSV and legacy reads are unchanged.
Pass `dtypes=` explicitly only where the provider is known and the path may not be parseable — the census tail's read-back is the one such caller.
**A schema is only reachable once `naming.KNOWN_PROVIDERS` carries its token**: both filename regexes gate on that tuple
— the state kartaview sat in between its collector landing and #225 phase 3b wiring the token, when `PROVIDER_RUN_DTYPES["kartaview"]` was dead and a KartaView run read as Mapillary.
`test_a_run_schema_is_reachable_from_a_filename` now asserts the reachability directly (it was a strict xfail while the token was pending, so the two could not be brought into step in the wrong order), because `test_every_known_provider_has_a_run_schema` checks the containment in the direction a schema-without-token state satisfies vacuously.
`download_mapillary.py` keeps every name that was public as a thin binding, so no call site moved (`census_column` is the exception, and is not one: it was `_census_column`, private, with no caller outside the module): `collect_mapillary.py`, the golden-fixture test and the tile tests import exactly what they did before.
**`is_pano` is the shared 360° boolean**, which is what lets `collect_mapillary.nearest_images_to_samples` be reused verbatim by a second census provider that normalizes its own flag into it — it touches only `lon`/`lat`/`is_pano`.
Its neighbour `build_streetwalk_rows` is **not** reusable and a second provider's road walk will have to generalize it: it indexes `captured_at_ms`, calls Mapillary's date parser, and calls the Mapillary *bindings* of `build_image_rows`/`build_empty_rows`, so it is Mapillary-specific in three separate ways.
Either way `is_pano` is read through `census.census_is_pano`, never `census["is_pano"].to_numpy()`, because the dtype a provider declares decides whether `~is_pano` works at all and the two wrong declarations fail differently: a nullable `"boolean"` converts cleanly until the first null,
then degrades to an object array where `~` raises `boolean value of NA is ambiguous` **after the whole paced fetch is spent**;
a plain `object` column of Python bools never raises at all — `~True` is `-2`, `~False` is `-1`, both truthy
— so `np.flatnonzero(~x)` selects every row and marks the entire city FLAT_ONLY, into an immutable dated snapshot.
The accessor reads a null projection as flat (the imagery is still recorded) and logs it as the decoder bug it is, and returns immediately without an `isna` pass on a non-nullable column, since it sits on #157's path.
The reason this is a module rather than a copy is the next paragraph: all three of #157's contracts are invisible in a review of the second copy, and `tests/test_census.py` therefore drives the generic layer with a deliberately un-Mapillary-like schema
— a different column count, a different order, a different copyright convention and no `captured_at_ms`, though *not* different `id`/`lat`/`lon` names, which `dedupe_census` and `build_image_rows` index directly and which are therefore a contract the census frame owes the seam;
`is_pano` is on that same list wherever the grid tail is under test, since the tail indexes it by name too, and the test schema declares it **nullable** precisely so the seam cannot quietly depend on Mapillary's non-nullable choice.
The golden fixture can only ever see one provider's binding, so it cannot tell a parameterized module from a Mapillary-shaped one with a schema argument bolted on.
**The `image_columns` contract is enforced rather than documented** (`_check_image_columns`): `pd.DataFrame(..., columns=list(dtypes))` selects and reorders, so a binding that omits a declared column, misspells one, or collides with the shared core publishes an all-null column, a vanished column, or a silently overwritten `pano_lat`
— three silent failures, all of them into an immutable dated snapshot.
A seam whose contract is unenforced is a copy waiting to happen, which is the thing this module exists to prevent.

## The census is columnar, and that is a memory contract (issue #157)

**The census is columnar, and that is a memory contract (issue #157).** A census is not a few thousand rows — Colorado Springs is 6.5M features, Detroit 19M
— so `fetch_city_images_async` returns `census`, a **DataFrame** (`records_to_census`), never a list of per-image dicts, and every step from grid assignment to the CSV write is array work on positional indices into it.
The dicts cost ~0.74 GB per 1M images and the row-wise pipeline built three more full-census structures on top (`(img, (i,j))` pairs, a 16-key dict per row, then a concat), which is why both of Colorado Springs' Mapillary channels hit the 180-minute timeout every night having printed nothing after `Decoded …`: with `MemoryHigh=4G`/`MemoryMax=8G` the process was in permanent reclaim, not computing.
Measured peak on its geometry fell **7.1 GB → 3.2 GB** (the per-1M-image slope from 0.87 GB to 0.32 GB).
Three things this shape depends on, none of them incidental: **(1)** the per-tile conversion happens inside `fetch_one`, because `asyncio.gather` holds every tile's result until the last lands
— returning dicts and converting after the gather would keep the whole city's dicts alive and buy nothing;
**(2)** the cross-tile dedup (`dedupe_census`) reproduces the `images_by_id[id] = record` it replaced, which is **two rules, not one**: a repeated id takes the **last** copy's values (an image published in two tiles carries coordinates quantized to each tile's own extent, so preferring the other copy can move it to a neighbouring grid point) but keeps the position of its **first** appearance (assigning to an existing dict key overwrites the value without reordering the key).
`drop_duplicates(subset="id", keep="last")` satisfies only the first and moves the surviving row, which reorders essentially every real city's CSV — render-buffer duplicates are ubiquitous
— so the dedup is a `pd.factorize` + last-position scatter instead.
The golden fixture cannot see this: its duplicate is the last feature of the last tile, the one arrangement where the two orderings coincide, so a dedicated test pins it;
**(3)** the written CSV is **byte-identical** to the row-wise output, pinned by `tests/fixtures/mapillary_golden_run.csv`
— a run file is an immutable dated snapshot and `diff.py` compares it to the previous one, so a formatting or ordering drift would show up as a large phantom diff in every Mapillary city with no way to distinguish it from real imagery churn.
Regenerate that fixture only with `REGEN_MAPILLARY_GOLDEN=1` and review the diff.
The one documented exception is the two **grid** columns: `query_lat`/`query_lon` come from geographiclib's geodesic solve and libm's `sin`/`cos`/`atan2` are not correctly rounded, so macOS and glibc disagree in the last ULP (~7e-15 deg, measured)
— the fixture compares those two numerically at 1e-9 deg and everything else byte for byte.
Nothing downstream can see that wobble: `diff.py` keys grid points at `_COORD_DECIMALS = 6` (~11 cm).
Do not "fix" this by regenerating the fixture on one platform, which just moves the failure to the other.
`captured_at_to_iso_date` survives as the readable scalar statement of the date rules;
`captured_at_to_iso_dates` is what the collectors call, and a test pins them element-wise (they diverge on timestamps pandas represents happily and Python's `datetime` cannot, so the validity mask must be applied *before* `strftime`).

## Imagery-type stratification (issue #116)

**Imagery-type stratification (issue #116).** Mapillary flat/perspective imagery (`is_pano` false) is no longer discarded at decode.
A grid point covered *only* by flat imagery — no 360° pano — becomes a single `FLAT_ONLY` row (a presence marker carrying the nearest flat image but a **null** capture_date, so flat timestamps never enter any dated-stat path) instead of ZERO_RESULTS.
This yields two coverage numbers, never conflated: **360° coverage** (`coverage_rate_pct`, statuses OK+NO_DATE
— unchanged, still GSV-comparable; GSV emits no FLAT_ONLY) and **any-imagery coverage** (`any_imagery_coverage_rate_pct`, OK+NO_DATE+FLAT_ONLY).
The status vocabulary lives in `analysis.py` (`PRESENT_STATUSES` for 360°, `ANY_IMAGERY_STATUSES`, `FLAT_ONLY`);
`runs` carries `status_flat_only`, `any_imagery_coverage_rate_pct`, and `num_flat_images` (the flat *census* magnitude
— a downloader artifact threaded through the CLI, **not** reconstructable from the CSV since flat-only points collapse to one row, so `recompute_run_stats.py` refreshes the first two but never `num_flat_images`).
Frontend: an "Any imagery" color-by metric (`streetscape-utils.js` `METRICS.coverage_any`), an overview-popup any-imagery line, and a toggleable flat-only marker layer on the city page (`city.js`).
All fields fall back to the 360° rate for GSV/pre-v7 data, so old records read unchanged.

## KartaView is the second census provider, and its primitive is a paginated radius sweep (issue #225)

**KartaView is the second census provider, and its primitive is a paginated radius sweep (issue #225).** There is no bulk metadata endpoint: the coverage tiles carry geometry only, their `.json`/`.geojson` variants return empty at every tile tried (including the official docs' own example), and any unconstrained `/2.0/photo/?lat=&lng=&radius=` answers `apiCode 408 "Query timeout"`
— including the exact query Grab's own shipped MCP server issues.
The one reliable spatial path is `POST /1.0/list/nearby-photos/` in **radius** mode;
bbox mode errors or returns zero in the **southern hemisphere**, i.e. at the Grab fleet cities that are the whole reason to add the provider.
So `download_kartaview.py` tiles the frozen grid's bbox with squares, covers each with its circumscribed circle, and pages each circle to exhaustion
— unwrapping an **antimeridian**-crossing bbox first, exactly as `download_mapillary.tiles_for_bbox` already does, because `max_lon - min_lon` goes negative there and collapsed Taveuni FJ to 29 of its 841 cells while `_bbox_area_m2` reported the wrap as 1.5 million km² and so let the failed-area guard see 0.004%: a 97%-empty census returned as a clean success
— a census like Mapillary's, bound to `census.py` (schema + `image_columns`), with `projection == "SPHERE"` supplying #116's `is_pano` and `PLANE` the flat imagery.
**`HTTP 400` is BACKPRESSURE here, not a malformed request** (`apiCode` 690 or 408 inside it), which is the opposite of the usual 4xx reading and the easiest thing in this module to get wrong: typed as permanent it would never be retried or subdivided and every dense city would collect nothing.
**Three measured facts decide the walk, two of them correcting the feasibility study that preceded them.** (1) Pagination is **exhaustive**
— Seattle r=400 ipp=200 returned pages 1–6 with zero id overlap between any pair, union == `totalFilteredItems`, page 7 empty
— so a truncated circle is *paged*, not subdivided, and page 1 prices the rest of the circle before we pay for it.
(2) `apiCode 690` is **flaky**, not a function of (radius, ipp): Horace ND — a bbox holding *no* imagery at all
— refused r=1000 on 0/6 attempts at ipp=2000 and 0/4 at ipp=200, answered r=250 on 4/4, then answered r=1000 on 2/2 forty-five minutes later.
So a refusal is retried before it is believed (retrying is **4× cheaper** than subdividing: one request against four, each of which may cascade 1 + 4 + 16 to the floor; 88 cells cleared on retry during the study, for 174 extra requests in total).
(3) The working radius is a property of the **location**, varies 4× across the catalog and is **not** predicted by density
— Seattle held r=1000 at a higher measured photo density than either New York or Manila, both of which calibrated down to 500
— so it is measured once per city (`calibrate_radius`, bounded by `rungs × (probes + retries)` = **30** at the defaults, a rung accepted only if *every* probe on it answers
— and abandoned the moment one does not, since the rung is already lost.
The obvious `rungs × probes` = 12 is **wrong**: it assumes a probe costs one request, and a refused probe costs `retries + 1`.
Measured at the shipped defaults before the early break, a city where nothing answers spent 48) rather than rediscovered at every cell.
Only backpressure may subdivide: asking a server for four requests where it just failed to serve one is the shape of #198, not a fix for it, so a transport fault is retried and then recorded as a **failed cell**, and a `ResponseError` (unparseable body, item-less envelope) is asked exactly once
— except a rejected credential (`CredentialRejectedError`, 401/403), which stops the whole sweep at its first appearance: a dead token answers identically at every cell, so per-cell recording re-asked it across the entire remaining lattice.
That rule holds on **every** page, which it did not originally: the deep-paging branch subdivided on `refused` *or* `broken`, so a page-2 transport fault or 401 fanned one request into four and cascaded to the floor
— measured at 42 requests for a rejected credential and 105 for a single TCP reset, against docstrings promising the opposite.
**Cost is one geometric term**: `root_cells` tracks `bbox_area / (2 r²)`
— to within 10% only above ~1,000 km² (at 400 km² it is 12%), and the residual is `ceil()` in both axes, so it is bounded by the bbox's **aspect ratio** rather than by its area: a long thin coastal grid runs wider.
`estimate_sweep_requests` therefore counts the lattice exactly rather than evaluating the formula, so a KartaView channel is budgeted by **bbox area, not by imagery** (`estimate_sweep_requests`, the analogue of `estimate_tile_count`).
Measured over 14 cities for 638 requests: the median catalog city is **12 circles but ~16 requests** (58 s), p95 **636**, Singapore **~9,974**
— read `sweep_requests_observed`, never the geometric floor, because the floor prices neither the retries nor the calibration ladder and the study's own overhead ratio is **1.54×**.
See `docs/experiments/kartaview-sweep-cost.md`, and note two caveats it carries: ten of its fourteen plans are truncated (the *cell counts* are exact, the overhead is a thin sample), and **no plan above 12 root cells ever completed**, so the extrapolation behind every figure above the median is unvalidated rather than merely thin.
**The capture-date rule is `shot_date >= date_added` → NULL, `>=` and not `>`.** `shot_date` is contributor EXIF and `date_added` is server upload time, and they are **never merged**
— a `shot_date or date_added` fallback would file Krabi's undated 2025 bulk upload beside Seattle's genuine 2025 capture year, indistinguishably — so `date_added` is published as its own column instead.
The invariant is measured, not defensive: **v2 serves the upload timestamp as the capture date** for Grab's 2025-11-19 open-360 ingest (10 of 48 audited sequences, 5,665 photos, every one `SPHERE`/`KartaCam2`/uploader `OpenStreetView`), which no null-check catches because what it hands you is a non-null, entirely plausible timestamp;
three of those ten read equal *to the second*, which is why a strict `>` is precisely the near-miss guard that lets it through. v1 reports that batch as null today, so the invariant is what stops the same defect arriving through this door — via a backfill or a v2 migration — and it costs nothing.
`analysis.EARLIEST_PLAUSIBLE_CAPTURE["kartaview"]` is 2004, deliberately looser than the 2016 launch for the same reason Mapillary's is looser than 2013.
One further date hazard is **not** a KartaView property but a pandas one, and it is #226's defect from a second direction: the API mixes precisions inside one page (`2025-09-01 17:57:05.000` beside `2025-09-20 21:08:37`), `pd.to_datetime` infers **one** format from the first non-null value, and with `errors="coerce"` every value at the other precision silently becomes `NaT`
— so the vectorized parser pins `format="ISO8601"` and a test pins the pin.
**Pacing and the host lock:** KartaView documents 100 req/hour anonymous and 1,000 authenticated (the FAQ is reachable only by scraping `kartaview.org/main.*.js` — the docs are a JS SPA
— corroborated by Bellingcat), returns **no** `X-RateLimit-*` or `Retry-After` headers at all, and enforced neither figure when measured (130 consecutive requests, zero 429s).
A client therefore cannot observe its own budget, so we pace to the published number regardless: `DEFAULT_SWEEP_REQUESTS_PER_MINUTE` is 16 (960/hour, under the documented ceiling by construction since the limiter's burst is a single token),
the sweep is **serial** (pacing is the bottleneck, the next question depends on the last answer, and fanning out into a server that answers 400 under load is #198's shape), and one `host_lock(HOST_KARTAVIEW)` is held for the whole sweep
— which for Singapore is hours, so any concurrent KartaView work gets the busy code rather than a queue.
KartaView is the **third** locked host (`HOST_LABELS` is the registry; there is no separate set) and has its own exit codes
— **81 blocked / 82 busy**, skipping 77/78 because those are `EX_NOPERM`/`EX_CONFIG` and a plausible-sounding wrong answer to "what does 77 mean?" is worse than an unallocated number.
A redirect, an HTML body or a 429 is a `HostBlockedError` and stops the sweep at the **first** request (#205, exact rather than bounded by a concurrency limit, because the walk is serial);
**401/403 stays a plain `DownloadError`**, scoped to the credential, per the Mapillary precedent.
Two conditions that look alike and are not: a city where **no rung answers at any calibration point** is a `DownloadError` and never a host condition (that is a property of the bbox — Horace again
— and typing it host-wide would let one such city skip every other city's KartaView channel for the night), and it is never recorded as an *empty* city, since a 0-pano census publishes and diffs as "every pano in the city removed".
A sweep that leaves more than `MAX_FAILED_AREA_FRACTION` (2%, measured as **area** because subdivision means cells are not one size) unmeasured refuses to finalize.
A resume **re-probes carried failed cells before the unvisited roots** (a refusal is time-varying — Horace again), so a cell stays failed only by refusing again;
each leaves the durable failed set only at the moment it is actually re-swept, so a stop or crash mid-pass keeps the not-yet-re-probed tail failed rather than losing it.
**"Unmeasured" and "not yet visited" are different facts, and the checkpoint is what separates them (issue #239).** A sweep is hours of paced fetching — Singapore ~9,974 requests ≈ 10.4 h at 16/min
— and until #239 any interruption discarded every request already paid for: a SIGKILL from `city_timeout_minutes`, a `systemctl stop`, an OOM, a crash in the caller's tail all restarted at cell zero, which is what made a per-city candidacy ceiling (excluding exactly Singapore, the strongest 360° city in the feasibility study) or a `max_batch_hours` bump look necessary.
`fetch_city_images_async(checkpoint_path=…)` instead commits what it has answered
— a **directory** of Parquet parts plus a `state.json` **commit record written last**, so a part beyond the record is a torn write, ignored and deleted — and a later process resumes from it.
Six things about it are load-bearing.
**(1) The periodic in-sweep commit IS the feature; the `finally` commit is a bonus.** `cli.py` installs no SIGTERM handler and Python's default disposition terminates without unwinding, so `finally` does not run on the most common deliberate interruption, nor on a SIGKILL, nor on an OOM kill — do not "simplify" the two into one.
Its cadence is measured in **requests** (`DEFAULT_CHECKPOINT_REQUEST_INTERVAL` = 32, two minutes at the shipped pace), not roots, because a root is not a fixed cost: a clean one is a single request while one cascading to the radius floor is 85 cells at up to `retries + 1` attempts, ~340 requests, and per-root flushing would have both the worse worst case and one part file per root
— 5,130 of them for Singapore, which is its root-cell count, not its 9,974 requests.
**(2) The parts are Parquet, not CSV**, and that is a correctness constraint: every string column is provider-supplied and pandas' default `na_values` claims `NA`, `null`, `None`, `nan`
— measured on this schema, a username of "NA" and a way_id of "null" both return as `<NA>`
— so a resumed run would publish *different* rows than an uninterrupted one, in a repo whose census output is pinned byte-for-byte by a golden fixture (`keep_default_na=False` only mirrors the bug: every genuine null becomes `""`).
Parts are also read strictly in **index order**, never by a directory glob, because index order is fetch order and `dedupe_census` keeps a repeated id at its **first** position — and the sweep re-sees ~π/2 of everything by construction.
**The age cap is measured from `created_at` — when the FIRST commit landed — not from `updated_at` (issue #272), and the commit record is format v2 because of it.**
The `finally` commit rewrites the record on nights that swept nothing: a host block, a rejected credential, a budget stop at request 1.
A host-blocked night deliberately records no `consecutive_failures`, so the same stalest city is re-attempted the next night and the next — and ageing from the last write would let such a city refresh its own clock forever and hold rows from any distance in the past under the limit.
That is the same fix Mapillary's checkpoint already carried; a v1 record has no first-commit stamp at all, and adopting `updated_at` in its place would be exactly the bug, so it is discarded by the ordinary format-mismatch arm and its sweep restarts once.

**(3) The radius is pinned in the checkpoint**, since a refusal is time-varying (Horace again) and a re-calibrating resume could land on a different rung, re-tiling the bbox so that `roots_done` indexes a lattice it was never recorded against.
**(4) A commit always writes the sweep as of the last completed boundary — a root's, or a re-probed failed cell's**
— that invariant is what makes the `finally` commit safe from any exception path and lets the DFS stack stay un-persisted, at the cost of rolling a partial cell's rows, failed cells and counters back so it is re-swept from its own top.
**(5) `api_requests` is this PROCESS's spend and `api_requests_total` is the sweep's**, because `db.add_api_usage` is additive and keyed by (date, provider), so a resumed night reporting the whole sweep would charge last night against tonight's budget gate;
this is the opposite of `download_gsv_history`'s checkpoint, correctly, since its caller writes `record_harvest` and never touches the ledger.
**(6) The path key is (city, grid geometry, CHANNEL) and must be DATE-FREE and outside `data/`**
— a run is dated on the day it *completes*, so a date-bearing path (the `.downloading`/`.harvesting` convention, where a collection finishes in one night) would restart every night from zero;
and the directory holds a partial census, the one artifact that must never reach the publisher.
The channel is not optional either: a KartaView road walk will sweep the same frozen `grid_bbox` at the same ipp and radius, so bbox, ipp, radius and root_count all match and the two channels would resume each other's sweeps
— harmless to the census, but they meter into separate `api_usage` ledgers, so one would inherit the other's `api_requests_total`.
All checkpoint I/O happens **inside** the `host_lock(HOST_KARTAVIEW)` hold, so it needs no lock of its own.
A mismatched bbox, ipp, radius, format version, root count or part row-count sum **discards** the checkpoint and sweeps afresh rather than refusing, following `get_processed_points` and `_load_checkpoint`: a checkpoint is not a comparison, so the worst case of ignoring one is wasted work rather than a wrong artifact.
Consequently `max_requests` stops meaning "destroy the night" and starts meaning "spend this much tonight": a trip with a checkpoint raises `SweepIncompleteError` (a plain `DownloadError`, **never** a `HostUnavailableError`
— that maps to exit 81 and would trip the night-level breaker over one city's budget) and publishes nothing, exactly as before, but the spend survives.
A complete checkpoint with no failed cells is a **finalize-only resume**, costing zero requests — one carrying failed cells spends only their re-probes
— which also recovers a crash in the caller's artifact-writing tail
— and that last part is why **the caller, not this module, calls `discard_checkpoint`**: the census comes back as a DataFrame and the dated CSV, stats, `runs` row, JSON and diff are all written afterwards, so a delete before returning would cover every interruption except the ones happening after it.
Two things bound a caller that forgets: `load_checkpoint` says `COMPLETE … discard_checkpoint` at WARNING, and `CHECKPOINT_MAX_AGE_S` (**7 days**) discards any checkpoint older than that rather than resuming it.
That age bound is the one guard here protecting an *artifact* rather than a night's work: frozen geometry never changes, so every other check still passes months later, and a city interrupted and then sat out a long gap (a channel switched off after a per-IP block, `consecutive_failures` quarantining it for a 90-day cycle) would otherwise splice last quarter's rows into a snapshot dated today.
Seven days is well above the worst legitimate multi-night sweep (~10.4 h) and well under the 80-day `min_days_since_last_run`.
This is what turns the whole-catalog pass the cost study calls "not affordable yet" into "a pass takes N nights".
**Collectable by hand, not scheduled (#225 phase 3b).** `download_kartaview_metadata_async` is the grid-run wrapper
— preamble → `census.build_grid` → the sweep → `census.write_census_grid_run`
— and is a ~60-line twin of Mapillary's, with the three provider bindings (`_kartaview_capture_dates`, `_kartaview_image_columns`, `KARTAVIEW_METADATA_DTYPES`) being the whole of what differs.
`naming.KNOWN_PROVIDERS` carries the token, so `PROVIDER_RUN_DTYPES["kartaview"]` is finally reachable and a run reads with its own schema instead of inferring `sequence_index` to float64 and `way_id` to a float;
`streetscape_tracker.py --provider kartaview` collects a city.
**KartaView is never in the default channel set**
— a 16 req/min serial sweep is hours for a metro and must never be something you get by typing nothing.
`--provider` is a comma-separated LIST whose default is the stated `gsv,mapillary` (issue #247), so KartaView is reached only by naming it or by `--provider all`, which expands from `naming.KNOWN_PROVIDERS` and fails fast if any named provider's credential is absent.
The retired `both` still works, with a deprecation notice: it named two of two when it was written and names two of three now, which is precisely why redefining it in place was refused
— every bare `streetscape_tracker.py "City"` would have silently acquired both a third mandatory credential and the sweep.
`cli.py` owns the checkpoint lifecycle end to end: it builds the date-free, channel-scoped path (`checkpoint_path_for`, under the gitignored `checkpoints/` sibling, realpath'd for the same reason `host_lock.lock_dir` is), and it calls `discard_checkpoint` **last, after `register_run` commits the runs row**
— the wrapper only echoes the path back, because a discard inside the downloader ran before that row existed, so a `register_run` failure cost the whole multi-night sweep (PR #251 review finding 2);
deleting any earlier is what would make a crash in the tail re-pay it.
`SweepIncompleteError` propagates to `SWEEP_INCOMPLETE_EXIT_CODE` (83), and `--kartaview-max-requests` is what makes that reachable by hand — a bounded sweep that publishes nothing but keeps its spend.
The one thing that is genuinely provider-shaped rather than shared is `_points_in_cells`, the `unmeasured_mask` over `failed_cells`: it masks each cell's **square** (the circle is what the request covered and is 1.57× the area, so masking with it would mark neighbouring cells that *were* measured as unknown) and loops rather than packing one key, because subdivision means cells are not one size.

## The Mapillary tile census checkpoints too, and its reassembly order is the contract (issue #256)

**Both census providers now resume, and the Mapillary half is shaped by one fact the KartaView half does not have to deal with: its tiles are fetched concurrently.**
The plumbing they share — where checkpoints live, how a directory is named and validated, how a rename into it is made durable, how a finished one is removed — is in `checkpointing.py`,
which exists because no provider module may import from another's and a shared home is the alternative to a copy.
What each provider keeps *inside* its directory is its own, because that part is shaped by the crawl.

**Parts are keyed by tile `(x, y)`, not by fetch order, and reassembly walks `tiles_for_bbox`.**
KartaView visits its root cells in a deterministic order, so it can number parts `0..n` and replay them in that order.
`_fetch_city_images` runs `asyncio.gather` behind a semaphore, so completion order is nondeterministic — a fetch-order index would make a resumed census depend on which tiles happened to land before the interruption.
Since `tiles_for_bbox` is pure, the order is **recomputed rather than stored**: walk the tile list, take each tile's frame from this run if it was fetched now and from its part file otherwise.
`gather` preserves argument order, so an uninterrupted run and a resumed one hand `concat_census` positionally identical input.
That is the whole byte-identity mechanism, and it is what keeps `dedupe_census`'s first-position/last-value rule on a **border duplicate** — an image inside two tiles — from resolving differently depending on which night fetched which copy.
The golden fixture is the test: a run interrupted after one tile and resumed must reproduce it byte for byte, which is what `tests/test_mapillary_resume.py` asserts against the very fixture `test_mapillary.py` pins the uninterrupted path with.
Get this wrong and `diff.py` reports imagery churn in every Mapillary city, indistinguishable from a real re-drive.

**Only successful tiles are committed**, so a 404 or a timeout stays refetchable and #168's tolerance keeps measuring failures against the **full** tile set rather than against whatever one process attempted.
One tile is one paced request, so per-tile commits are at most one small write per second at 60/min — and they make #205's fail-fast salvage automatic, since everything fetched before the fatal is already durable when it re-raises.
A **zero-row tile gets a record and no file**: most tiles over a real bbox are empty, and a part for each would mean 870 files for Moscow to say nothing.

**Checkpointing here fails OPEN, which is a deliberate divergence from KartaView's fail-fast.**
There the trade is right — ten hours of paid-for crawl is worth more than the night that loses it.
The worst Mapillary city is ~15 minutes, so an unwritable directory or a failing commit logs one warning, latches, and lets the fetch carry on unprotected: a city must never fail over its own safety net.
An unusable checkpoint (format, bbox, zoom, channel, variant, tile count, age, a missing or short part) is **discarded and refetched**, never raised on — and unlike KartaView's it is deleted rather than left, because tile-keyed part names are never overwritten by a crawl that does not resume them.

**Because the discard DELETES, the checkpoint is loaded before its directory is created, and that order is load-bearing.**
Creating first means the directory the fresh handle is about to commit into is the one the discard just removed: every commit fails, `degraded` latches on the first tile, and the whole city fetches unprotected.
The case that produces is the age cap, i.e. the first attempt after a multi-day block — so the one run that most needs the safety net would be the one running without it.
Creating afterwards still happens before the first request, which is the property that actually matters: an unwritable path must fail in a second rather than fifteen minutes in.

**A part is fsynced before it is renamed, and the directory after**, the same four fsyncs per commit `download_kartaview._commit_checkpoint` spends and for the same reason.
Without them the part-then-state ordering holds against a process crash — where the page cache survives and the fsync buys nothing — but not against a power loss, where the two renames may reach the disk in either order.
What that leaves is not a wrong artifact, since the loader's footer check catches a part shorter than its record; it is the loss of the **whole** checkpoint rather than the last tile.

**The two counters mean what they mean on the KartaView side**, and one subtlety is easy to lose: spend that happens *after* the last committed tile — the requests a block refuses, which `api_usage` counts deliberately — is written into the record by `_commit_spend` on the failure paths.
Without that, a resumed run's catalog row prices the city below what it actually cost, because those requests die with the process while the ledger keeps them.
It is skipped when nothing was committed, so a city blocked on request 1 still leaves no directory behind.

**That write is also why the age cap is measured from `created_at` — when the FIRST tile was committed — and never from the last write.**
`_commit_spend` rewrites the record on a night that committed no tile at all, and a host-blocked night deliberately records no `consecutive_failures`, so the same stalest city is re-attempted the next night and the next.
Ageing from the last write would let a city refresh its own clock indefinitely and hold rows from any distance in the past under the limit — defeating the one guard in this design that protects an **artifact** rather than a night's work.
`created_at` is stamped by the first write of a crawl and carried forward by every later one; `updated_at` still moves, for an operator reading the directory.

**The path key is (city, grid geometry, channel, variant), and the variant is what separates two WALKS of one city.**
The channel separates a walk from a grid run, but `drive` and `all_public` are different series over the same frozen bbox in the same street channel — which is why `generate_streetwalk_filename` carries the network token.
Without the variant a walk that dies after its census but before `register_street_walk` leaves a checkpoint the other network type's walk re-finalizes for zero requests, writing the first crawl's `api_requests_total` into the second's row.
Both walks read the same tiles, so the census is identical and nothing downstream would show it.
A grid run has exactly one crawl per channel and passes no variant, which keeps its path byte-identical to the one #239 shipped.
That paragraph closes with "both walks read the same tiles, so the census is identical and nothing downstream would show it" — **still true of checkpoints, where the identity is a hazard.
The census cache below is where the same identity is exploited instead.**

## The census cache — fetch once per (provider, bbox), reuse across channels (issue #290)

**A completed checkpoint is PROMOTED into `census_cache/<provider>/<city_id>_<bbox>` rather than deleted, and every later consumer of that (provider, city, bbox) observation reads it for zero requests.**
The observation being paid for repeatedly was measured, not suspected: the ledger showed byte-identical daily totals for `mapillary` and `mapillary_streets` (#287), because a road walk fetches the identical z14 census over the identical frozen bbox and then joins it onto sample points locally.
It is worse than 2× — a second walk at `--network-type all_public` is a third copy, and #258's KartaView walk would repeat the pattern against a sweep that is ~10.4 h for Singapore.
A paired Mapillary night now costs one census instead of two, `all_public` walks are free, and `assess-city`'s grid+walk pair halves.

**THE KEY IS GEOMETRY AND THE MARKER IS THE RECORD, and that is the whole difference from a checkpoint path.**
`checkpoint_path_for` keys the channel and the variant because two crawls must never resume each other's spend.
`census_cache_path_for` keys **neither** — the census content depends on (provider, frozen bbox, when it was fetched) and on nothing else — and who paid, under which channel and variant, is written INTO the entry as `census_cache.json`.
Both mistakes are silent: a cache keyed by channel would reuse nothing and every census would still be bought twice with nothing failing, and a checkpoint *not* keyed by channel would let two crawls resume each other into the wrong ledger under the wrong credential.
The bbox is folded in at 6 dp for the reason a checkpoint's is: a frozen grid can be re-registered (`scripts/resize_city.py`, `cap_oversized_grids.py`), and an entry keyed on the slug alone would survive a resize.

**The marker is written INSIDE the checkpoint first, and the single rename is the commit point.**
`promote_checkpoint_to_cache` writes `census_cache.json` durably into the checkpoint directory, then one `os.replace` moves the whole directory under the cache name (an EXDEV copy lands under `<entry>.tmp` and is renamed in one step too).
So at every instant an entry exists under its name it is complete and stamped; a crash anywhere leaves either the checkpoint (with one extra file both providers' debris purges ignore) or the finished entry, and a False return can promise the checkpoint is still where it was.
The first version wrote the marker AFTER the rename, and that window (an `ENOSPC` on the marker, or the subprocess-timeout SIGKILL, which for KartaView lands at the END of a sweep — exactly there) had two consequences: the directory had moved while the fetch reported it had not, so a tail crash lost a paid-for crawl from both places; and the lock-free tail prune could delete a census a concurrent manual run had just renamed and not yet stamped.
Promotion is best effort throughout: any `OSError` warns, removes the stray marker and returns False, and the caller then keeps its checkpoint and discards it exactly as before, because a city must never fail over its own optimization.
It is the **last statement before the success return**, after `dedupe_census` and therefore after every `raise`, so a moved directory and a propagating exception cannot coexist; and it runs inside the host lock, so no other process can read an entry mid-replace.

**A hit is RECONCILED with the consumer's own checkpoint before it is reused (`reconcile_cache_hit`), because a bare "hit means reuse" was wrong in three ways.**
A checkpoint may already sit at the consumer's `checkpoint_path`: if its crawl started AFTER the cached observation it is the newer one — an interrupted `--refetch-census` sweep the operator asked for — and it is resumed instead; if it is OLDER (last night's walk, host-blocked at tile 300 before the grid run promoted) the entry supersedes it and it is discarded there, since a hit returns before the checkpoint is opened and no pruner walks `checkpoints/`.
And an entry may be THIS crawl's own — the same (channel, variant), coming back because its tail died between promotion and the catalog row: that is #239/#256's re-finalize, and a resume from a COMPLETE checkpoint re-probes what failed because a refusal is time-varying, so an own entry with failed work is handed BACK to the checkpoint path (rename, marker removed) for the ordinary resume path to finish and re-promote.
An own entry with nothing failed is reused as-is, which is the same result the resume would produce for no requests.

**The lifecycle lives ONCE, in `checkpointing.py`, and each provider plugs in only its two checks.**
`load_cached_store` is the loader skeleton (marker window, `run_date`, the never-raise posture, delete-what-nobody-can-use), `census_cache_marker` the one marker builder, `reused_census_provenance` the reuse accounting, `observation_timestamp` the `query_timestamp` rule, and `crawl_store_for`/`frozen_bbox`/`CENSUS_PROVIDERS` the one derivation of both paths and of "is this a census provider" for the grid CLI, the walk collector and the scheduler's probe.
The first version copied all of that per provider, and the copies had already drifted: KartaView compared the channel alone for `same_crawl` where Mapillary compared (channel, variant), which would have priced an `all_public` walk's re-finalize of a `drive` crawl as the crawl itself the day #258 lands.
A provider-shaped refusal that is the CALLER's rather than the entry's — KartaView's page size, an explicit radius — is raised as `CacheEntryUnusableHere` and leaves the entry for the consumers it does fit; a `--ipp 200` harness must not cost every other consumer the grid run's ten-hour Singapore sweep.

**An in-flight checkpoint is never a cache entry, and completeness is the check that separates them.**
A resume is allowed to be partial by definition; a cache entry that is missing tiles or root cells would publish those points as genuine no-imagery — absence never observed — in an immutable dated snapshot.
So a promoted entry is validated by the same geometric/footer cascade a resume is (`_validate_tile_store`, `_validate_sweep_store`, factored out for exactly this reason) **plus** completeness: for Mapillary `done ∪ marker.failed == tiles`, for KartaView `roots_done == root_count`.
Promotion is refused for anything short of that — a `degraded` Mapillary checkpoint whose commits latched off, an interrupted crawl, a `SweepIncompleteError` pause.
KartaView needs one clause more, because its `finally` commit is deliberately best-effort: its failure is swallowed, which can leave the on-disk store lagging the in-memory census while the counters still read complete, so promotion additionally requires that last commit to have landed (`final_commit_ok`), nothing left uncommitted, and the recorded failed set to equal the session's.

**The reuse window is `CENSUS_REUSE_MAX_AGE_S`, which IS `CHECKPOINT_MAX_AGE_S` (7 days) rather than a second constant that happens to agree**, because both answer the same question: how far apart may two halves of one dated observation be?
It is aged from `crawl_started_at` — the crawl's first commit, when the provider was actually observed — which every promoter records (both promote only a crawl that committed), so a marker without one is refused rather than dated from its completion.
A multi-night crawl's last commit says nothing about how old its oldest rows are.
The window cannot see the other direction, and the consumer's `run_date` (carried in `CensusCache`) is that guard: an entry whose crawl finished after the snapshot date being written is refused **without being deleted** — a backdated `--force --run-date` must not publish rows Mapillary served after the snapshot's own ceiling, which `plausible_capture_mask` would then drop as "cannot be true", while the same entry is exactly right for the consumer dated tomorrow.

**Failed tiles and cells are INHERITED by a cross-channel reuser rather than re-probed.**
The reuser is republishing the same observation, so the same points read `REQUEST_FAILED` in both artifacts.
This differs deliberately from a same-channel *resume*, which re-probes carried failures because a refusal is time-varying (fact 2) — a resume continues one crawl, a reuse republishes a finished one, and mixing a fresher probe into a dated snapshot buys nothing.
The crawl's OWN channel is therefore never let inherit its own holes: `reconcile_cache_hit` hands its entry back to the checkpoint so the resume re-probes them (above).

**A reused census restamps `query_timestamp` with when the provider was observed; a fresh one keeps this process's clock.**
Every row of a reused census was fetched by another collection, possibly on an earlier night, and `json_summarizer` reports a run's start/end from that column.
Gating the restamp on reuse rather than on the provenance being present is what keeps #256's byte-identity contract between an interrupted census and an uninterrupted one, which is written against `started_at`.
`calculate_run_stats` still ages imagery against `run_date`, so a reused census can skew a pano age by at most the window — 7 days against an 80-day cadence.

**`api_requests` is 0 for every reuser; `api_requests_total` is not, and the asymmetry is easy to get backwards.**
The daily ledger is additive and keyed by (date, provider), so charging a reuser anything would bill one channel's spend against another's budget gate — the per-IP figure #241/#267/#286 reason about.
But a reader whose (channel, variant) matches the marker's is not reusing anything: it is #239/#256's re-finalize, a caller that died before its artifact was durable coming back to write the row for a crawl **it** paid for, and that row must still price the collection.
That comparison is the PAIR, from the one `same_crawl`/`reused_census_provenance` both providers call — the variant included, since two walks of one city differ only in it.
A different channel records 0, and schema v14's `census_fetched_by`/`census_fetched_at` on `runs` and `street_walks` are what make that 0 explicable rather than alarming.

**`--refetch-census` is the opt-out, and it is deliberately not `--force`.**
`--force` is about this run date's artifacts: a collection whose tail crashed after writing its CSV is re-run with `--force` and must re-finalize for zero requests rather than re-pay a census it already bought.
`--refetch-census` is about the *observation* — take it now — and it still promotes, so the consumers behind it get the fresher census rather than a re-fetch each.

**Scheduler-side, `_channel_estimate` returns 0 on a cache probe hit while `estimate_requests` stays cache-blind.**
The gates fed by the first are `est > budget` and `used + est > budget`, so without it the cheapest channel of the night — a walk whose census the grid run bought minutes earlier — is exactly the one a nearly-spent budget defers, and the pairing never happens on the nights it helps most.
The second also feeds `_mapillary_timeout_seconds`/`_kartaview_timeout_seconds`, where a 0 would collapse a child's timeout onto the flat floor.
The probe reads the **marker only** — no part files — because it prices every channel of every due city on every night; a hit is therefore a strong hint rather than a promise, and the consumer's own loader still validates and refetches, which is the safe direction.
Two things narrow how strong: the marker records the provider's commit-record format (`store_format_version`, from the `STORE_FORMAT_VERSIONS` table both providers read their own constant from) and the probe compares it, so a format bump is a MISS at the probe rather than a hit the child then refuses and refetches at full cost with the budget gate already passed and no in-child cap; and the scheduler probes with the window narrowed by `max_batch_hours`, so an entry that would expire between slate time and the child's load is priced as the fetch it will become.
It also **deletes nothing**, unlike the consumer-side loader: a planning caller holds no host lock, and deleting a refused entry is the job of the consumer that holds it, and of the tail prune.
The tail prune (`prune_census_cache`) removes expired entries and the debris of a failed move (`<entry>.tmp`, a marker-less directory) once it has sat for a day — safe without a lock precisely because promotion is one rename of a stamped directory — and that is the only thing bounding the cache's size: an entry is written for every census a night fetches and is not overwritten until that city comes round again, ~80 days later.

## An opt-in scheduler channel (issue #248)

**KartaView is a scheduler channel, and the only one whose membership is opt-in: `[providers.kartaview]` in `config/scheduler.toml` declares it, and its nightly queue is exactly the cities an operator enrolled with `scheduler enroll-city`.** Getting here took four fail-open arms plus dueness, and the reason each one looks as it does is that the token making the CLI work also made that config block *parse*.
**Three are now wired (#238)**: `city_timeout_seconds` has a `_kartaview_timeout_seconds` arm where its allow-list used to return the flat `city_timeout_minutes` floor (SIGKILLing Singapore's ~10.4 h sweep at 180 min, with a killed child recording **no** `api_usage`);
`estimate_requests` has an `estimate_kartaview_requests` arm where it used to fall through to the **GSV grid formula**;
`enabled_providers` ranks it explicitly last rather than by `rank.get(p, 99)`'s accident — the rule being "most expensive first, EXCEPT where truncation is cheapest to absorb", with this channel as the exception that proves the second half (the mechanism, and the four rationales that were wrong before it, are in [`docs/scheduler.md`](scheduler.md));
and `_run_one_city` now hands the child `--kartaview-max-requests-per-minute`, without which a timeout derived from the configured rate would be measured against a rate the sweep never used.
None of those raised, so `UNWIRED_CHANNELS` is what refused a `[providers.kartaview]` block until all five were wired — a misspelled channel is a typo worth dropping silently, but this one was spelled correctly and would have collected wrongly every night.
That mechanism is still there and now holds nothing: `load_scheduler_config` drops an unwired block from `providers` and records the error in `SchedulerConfig.unwired_channel_errors`, `run-due` and `assess-city` refuse with `USAGE_EXIT_CODE` while it is non-empty, and the read-only subcommands
— `backup-status` and `restore-backup` are the incident-time handles
— keep working with the error in the log, which a load-time `ValueError` used to take down over a block they could never act on.
Keeping the empty dict and its tests (driven by a monkeypatched synthetic entry) is deliberate: that record/drop/don't-raise asymmetry is what the next unwired channel inherits, and rebuilding it under time pressure is how a fail-open arm gets missed again.
`CHANNEL_HOSTS["kartaview"]` is declared because `test_every_scheduled_channel_declares_its_per_ip_hosts` asserts set **equality** against `KNOWN_PROVIDERS` — and because that host is shared with nothing, which is what moves the effective `max_concurrent_channels` ceiling from 3-of-4 to 4-of-5.
**Dueness is now wired too (#248).** `get_due_cities` no longer gates on `cities.enabled` alone: `schedule_state.member` is per (city, channel), and `scheduler.CHANNEL_DEFAULT_MEMBERSHIP` says what a NULL means per channel — `True` for the four scheduled channels, so their dueness is byte-identical, and `False` for `kartaview`, so its nightly queue is the cities an operator enrolled with `scheduler enroll-city` rather than all 1,144 at ~186,000 requests per pass.
`_collect_due` then hoists a city due *only* on an opt-in channel to the head of the slate, because the union is ordered by first appearance and `max_cities_per_day` truncates from the tail — without that the channel is scoped but never reached.
The seed set is deliberately a handful of hand-verified cities rather than the cost study's ~40-city Grab-fleet set: prove a night end to end, widen after.
No city has ever had a cataloged KartaView run, so every enrolled city prices from `estimate_kartaview_requests`' geometry tier, which `docs/experiments/kartaview-sweep-cost.md` records as ~4× under on the metros that calibrate to r=500.
One consequence to state rather than discover at widening time: a **kartaview-only city is inexpressible**, since registering one makes it `enabled = 1` and therefore a member of all four default channels.

**The cost arms, and the two numbers that are easy to swap (#238).** The estimate is `estimate_sweep_requests` × **1.80×**, and the multiplier is not optional: the lattice counts one page-1 per root circle and prices neither the extra pages, the backpressure retries nor the per-city calibration ladder.
Use **1.80×** (`summary.observed_over_root_cells.p50`) and not the **1.54×** the same study reports, because the two have different denominators — 1.54× is `observed_over_floor`, measured against a floor that counts cells *plus pages 2+*, where `estimate_sweep_requests` counts cells alone, so quoting it here would under-price the pages twice over.
The timeout then divides that by the channel's pace × `_SWEEP_ACHIEVED_RATE_FRACTION`, which is **0.5, deliberately BELOW the tile census's 0.8** — the opposite of the intuition that a serial walk tracks its limiter more closely than a concurrent one.
A concurrent fetch hides per-request latency behind other requests in flight, so its limiter binds; the sweep is serial by design, so its wall-clock per request is `max(pacing_interval, latency)` with nothing to overlap, and at 16/min that interval is only 3.75 s against a page carrying up to 2,000 photo records.
The error is deliberately asymmetric — under-timing is the whole defect being fixed, while over-timing is bounded by the batch deadline clamp and, since #239, an eventual kill just resumes tomorrow.
**That last clause is true of the WORK and not of the SCHEDULE, and the distinction is load-bearing enough to state twice.**
A *deliberate* pause exits `SWEEP_INCOMPLETE_EXIT_CODE` (83), which `_run_city_channels` amnesties beside the two host conditions — no `consecutive_failure`, the city stays due and leads tomorrow's stalest-first queue, and the child ledgered its own spend before exiting, so unlike a kill it costs the budget ledger nothing.
A *kill* has no exit code at all, so nothing can distinguish one that checkpointed real progress from one that made none, and it still counts a failure — five of which quarantine the city for a 90-day cycle.
So resumption after a SIGKILL is bounded at five nights, which is why the derivation is deliberately loose (0.5 × 1.5 ≈ 3× the honest paced wall-clock) and why a metro whose *clamped* timeout genuinely cannot finish in five needed the dueness work in #248 rather than a bigger constant.
That work has landed, and what it actually buys is that the five nights are **consecutive**: `_collect_due` hoists a city due only on an opt-in channel to the head of the slate, so a checkpointed sweep resumes tomorrow instead of falling to the tail of the union and returning months later with its five nights spent over a whole cycle.
Since #273 the arm that hoists is usually the **pause**, not the SIGKILL: `_run_one_city` hands the child the night's remaining budget as `--kartaview-max-requests`, so a sweep out of budget exits 83, which records no failure and consumes no city-cap slot.
The five-night bound therefore no longer binds a budget-paused city at all; what bounds it is `CHECKPOINT_MAX_AGE_S`, seven days from the checkpoint's first commit.
The SIGKILL arm remains for a sweep that runs out of **wall clock** rather than budget, and the five-night bound is that arm's.

**1.80× is a MEDIAN used as a ceiling, and the tail is not where you would guess.** The same summary puts `observed_over_root_cells.max` at **13.66×**, and that city is Horace, ND — **p65**, 55.9 km², 35 root cells, 478 requests observed — so the multiplier is ~7.6× low on a *mid-catalog* city, because refusal cascades make **sparse** bboxes the expensive ones (which inverts the feasibility study's expectation that cost per km² is worst where imagery is richest).
The timeout absorbs that: it under-times only where the true overhead exceeds 5.4× the cells this estimator counts *and* the honest wall-clock already exceeds the 180-minute floor, and no study city does both — Horace is 30 minutes of fetching, and Singapore's 1.94× is nowhere near 5.4×.
The daily **budget** guard does not absorb it, because it is a pre-flight check and this is the only channel whose estimate is not exact (grid points and tile counts both are), so a city can overspend what the ledger said it could afford.
The stop for that already exists and is deliberately not wired: #239's `--kartaview-max-requests` pauses at a request cap and checkpoints the rest, but `_run_one_city` holds the full daily ceiling rather than the remainder and the grid CLI has no budget flag to hand it to, so it is **#273** — the plumbing half, which bounds an overrun on its own — and **#274** is the policy half below that depends on it.

**The estimate reads the previous run's OBSERVED cost before it reaches for geometry, and that ordering is doing real work.** Radius is a 4× lever the up-front lattice cannot see: it must assume the default `r=1000`, while Singapore, New York and Manila all calibrate down to `r=500`, and nothing durable records that — the checkpoint pins it for one sweep and `cli.py` discards it once the run is cataloged.
So tier 1 is `runs.api_requests`, which for KartaView holds `api_requests_total`, the sweep's cumulative spend — carrying the radius, the pages, the retries and the ladder in one already-measured number, and previously read by nothing.
A paused sweep raises `SweepIncompleteError` and never reaches `register_run`, so a row there always describes a *complete* sweep rather than a partial night.
**Tier 1 is the LARGER of the prior and the geometry, never the prior alone**, because the prior describes the bbox as it was *then* — this is the one channel whose cost tracks bbox **area** directly, and the frozen grid is mutable through `scripts/resize_city.py --force` and `cap_oversized_grids.py --include-collected`.
Every other arm of `estimate_requests` recomputes from today's geometry on every call, so taking the larger is how this one keeps that property: a grid re-registered larger falls back to geometry instead of pricing a bbox that no longer exists, while the measured number still wins wherever the bbox is unchanged or has shrunk.
The hazard is the one `city_timeout_seconds`' Anchorage comment already names, reached from inside the fix rather than by the old fall-through.
Tier 2 (a first run) is geometry × 1.80×, and is under by ~4× on exactly those metros: Singapore's ~1,273 circles price at ~2,332 requests against the 9,974 actually spent.
That is survivable in one direction only — #239's checkpoint means the resulting SIGKILL resumes tomorrow instead of discarding the night, bounded at five nights as above — and tier 1 corrects it from the second run onward.
Note what none of this buys: a metro's honest timeout *exceeds* `max_batch_hours` outright, so the deadline clamp is what bounds it in a real night, and that is the intended outcome rather than a defect.
**Both budget arms are now resumability-aware (#274), and neither applies to either KartaView channel.**
They exist because every other channel is all-or-nothing — a partial GSV grid, a partial tile census and a partial road walk are not runs, so refusing to start is the honest answer.
A sweep is not all-or-nothing: it spends what tonight affords, checkpoints the unvisited roots and exits 83, and nothing is finalized or published until the lattice is complete, so the run is simply dated the day it completes.
`_run_city_channels` therefore launches an enrolled sweep with `budget - used` as its cap whatever the estimate says, instead of skipping it — the cities the old `est > budget` arm skipped forever (Singapore ~9,974 requests, New York ~12,355) being precisely the ones #239's checkpoint was built for.
`est` is deliberately not consulted on this branch, because `estimate_kartaview_requests` prices the WHOLE sweep even for a city resuming from a checkpoint (its observed tier reads a `runs` row, and a paused sweep never reaches `register_run`), so gating on it is the over-pricing the branch exists to stop.

The one floor that remains is `_MIN_SWEEP_LAUNCH_REQUESTS`, and it is derived rather than chosen.
A budget exhausted during radius **calibration** raises a plain `DownloadError` — "nothing was swept and nothing is checkpointed" — not `SweepIncompleteError`, so it takes none of the exit-83 amnesty and counts a real `consecutive_failure`.
The floor is the ladder's own documented bound (`len(RADIUS_LADDER_M) * (probes + retries)`, 30 at the defaults) plus one root cell's full attempt, read from those constants so retuning the ladder carries it along.

The road walk is no longer absent: #258 generalized `collect_mapillary.build_streetwalk_rows` into the shared `census_walk.py` scorer and added `collect_kartaview.py`, and #299 made `kartaview_streets` the sixth scheduler channel — opt-in, like the grid channel it pairs with.
It reads the same census by the same radius sweep, so it inherits the grid channel's cost arms wholesale: the same 1.80x overhead, the same geometric-floor estimate, and the same request cap, which is why both KartaView channels are `CHANNEL_RESUMABLE`.
