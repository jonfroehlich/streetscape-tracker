# The census seam, and the census providers

How a census provider (every image in an area, rather than the nearest one to a query point) becomes
METADATA-schema rows, and what the shared seam guarantees. Read before adding a provider or touching
`census.py`, `download_mapillary.py` or `download_kartaview.py`.

Split out of `CLAUDE.md` on 2026-08-22 so the always-loaded file stays under Claude Code's
size limit. The prose moved here is the original, with cross-reference pointers repaired where
they would otherwise dangle across the new file boundary; anything written since the split is
under its own heading and says so. `CLAUDE.md` keeps the short rule for each section and points
here for the evidence, the incident history and the details — keep the two in sync.

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

## Still NOT a scheduler channel (issue #248)

**Still NOT a scheduler channel, and a `[providers.kartaview]` block is refused rather than accepted — recorded and dropped at load, with `run-due`/`assess-city` exiting `USAGE_EXIT_CODE` while it exists.** The token that makes the CLI work also makes that config block *parse*,
and four arms in `scheduler.py` were fail-open behind it.
**Three are now wired (#238)**: `city_timeout_seconds` has a `_kartaview_timeout_seconds` arm where its allow-list used to return the flat `city_timeout_minutes` floor (SIGKILLing Singapore's ~10.4 h sweep at 180 min, with a killed child recording **no** `api_usage`);
`estimate_requests` has an `estimate_kartaview_requests` arm where it used to fall through to the **GSV grid formula**;
`enabled_providers` ranks it explicitly last rather than by `rank.get(p, 99)`'s accident — the rule being "most expensive first, EXCEPT where truncation is cheapest to absorb", with this channel as the exception that proves the second half (the mechanism, and the four rationales that were wrong before it, are in [`docs/scheduler.md`](scheduler.md));
and `_run_one_city` now hands the child `--kartaview-max-requests-per-minute`, without which a timeout derived from the configured rate would be measured against a rate the sweep never used.
None of those raised, so `UNWIRED_CHANNELS` is what refuses — a misspelled channel is a typo worth dropping silently, but this one is spelled correctly and would have collected wrongly every night.
The refusal is scoped to the commands that launch channels rather than raised at load: `load_scheduler_config` drops the block from `providers` (so nothing downstream can price or launch it) and records the error in `SchedulerConfig.unwired_channel_errors`, `run-due` and `assess-city` refuse with `USAGE_EXIT_CODE` while it is non-empty, and the read-only subcommands
— `backup-status` and `restore-backup` are the incident-time handles
— keep working with the error in the log, which a load-time `ValueError` used to take down over a block they could never act on.
`CHANNEL_HOSTS["kartaview"]` is declared regardless, because `test_every_scheduled_channel_declares_its_per_ip_hosts` asserts set **equality** against `KNOWN_PROVIDERS`.
**The remaining blocker is dueness** — `get_due_cities` gates on `cities.enabled` alone, so a channel would put all 1,144 cities in its queue at ~186,000 requests per pass, which `docs/experiments/kartaview-sweep-cost.md` says outright is not affordable yet.

**The cost arms, and the two numbers that are easy to swap (#238).** The estimate is `estimate_sweep_requests` × **1.80×**, and the multiplier is not optional: the lattice counts one page-1 per root circle and prices neither the extra pages, the backpressure retries nor the per-city calibration ladder.
Use **1.80×** (`summary.observed_over_root_cells.p50`) and not the **1.54×** the same study reports, because the two have different denominators — 1.54× is `observed_over_floor`, measured against a floor that counts cells *plus pages 2+*, where `estimate_sweep_requests` counts cells alone, so quoting it here would under-price the pages twice over.
The timeout then divides that by the channel's pace × `_SWEEP_ACHIEVED_RATE_FRACTION`, which is **0.5, deliberately BELOW the tile census's 0.8** — the opposite of the intuition that a serial walk tracks its limiter more closely than a concurrent one.
A concurrent fetch hides per-request latency behind other requests in flight, so its limiter binds; the sweep is serial by design, so its wall-clock per request is `max(pacing_interval, latency)` with nothing to overlap, and at 16/min that interval is only 3.75 s against a page carrying up to 2,000 photo records.
The error is deliberately asymmetric — under-timing is the whole defect being fixed, while over-timing is bounded by the batch deadline clamp and, since #239, an eventual kill just resumes tomorrow.
**That last clause is true of the WORK and not of the SCHEDULE, and the distinction is load-bearing enough to state twice.**
A *deliberate* pause exits `SWEEP_INCOMPLETE_EXIT_CODE` (83), which `_run_city_channels` amnesties beside the two host conditions — no `consecutive_failure`, the city stays due and leads tomorrow's stalest-first queue, and the child ledgered its own spend before exiting, so unlike a kill it costs the budget ledger nothing.
A *kill* has no exit code at all, so nothing can distinguish one that checkpointed real progress from one that made none, and it still counts a failure — five of which quarantine the city for a 90-day cycle.
So resumption after a SIGKILL is bounded at five nights, which is why the derivation is deliberately loose (0.5 × 1.5 ≈ 3× the honest paced wall-clock) and why a metro whose *clamped* timeout genuinely cannot finish in five needs the dueness work in #248 rather than a bigger constant.

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
One thing #238 deliberately left alone: the `est > budget` arm skips a city that can never fit the daily budget **permanently**, which is not resumability-aware — a metro priced above any sane KartaView budget would be skipped loudly forever rather than sweeping what tonight affords and checkpointing the rest.
That is **#274**, which depends on #273 for the plumbing and on #248 for there being a channel at all — the cities it skips forever (Singapore ~9,974 requests, New York ~12,355) are precisely the ones #239's checkpoint was built for.
A road walk is also still absent: `collect_mapillary.build_streetwalk_rows` is Mapillary-specific in three separate ways and has to be generalized first.
