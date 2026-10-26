# Capture dates: what they describe, and how they are parsed

The date-derived statistics and the two ways they have been silently wrong (#213, #226), plus the
out-of-band history harvester. Read before changing any date definition, any reader that parses a
capture date, or `analysis.dated_unique_panos`.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## The capture-date columns describe official Google imagery, and only dates that can be true (issue #213)

**The capture-date columns describe official Google imagery, and only dates that can be true (issue #213).** `analysis.dated_unique_panos` is the single seam every date-derived statistic reads
— age stats, the capture-year histogram, the daily histogram, in both the catalog and the per-run JSON — and it drops two populations.
**(1) Impossible dates.** Contributor photospheres arrive with corrupt EXIF, and because `oldest/newest_capture_date` are a min and a max, *one* pano owns them: 22 production runs read 2611–2612 and 75 predated Street View, off 1–22 bad panos in cities of 175k–334k.
`EARLIEST_PLAUSIBLE_CAPTURE` is per provider (gsv 2007, mapillary **2004**, kartaview **2004**, panoramax **2004**
— each of the three community providers deliberately looser than its own founding, matching the identical rule each decoder applies at decode, because contributors upload genuinely old photographs) and the ceiling is the observation date itself, inclusive
— nothing is captured after the query that saw it, and GSV's month-precision dates are pinned to the 1st so they can only round toward the past.
Each provider's floor also catches a SENTINEL that provider actually serves, which is what makes the number worth measuring rather than choosing: Mapillary's is an epoch-zero device clock, and Panoramax's is a literal `1970-01-01` timestamp — two Paris pictures, found in issue #316's phase-1 survey.
A sentinel is not a null and no null check catches it; what it hands you is a well-formed, entirely plausible-looking timestamp, and the floor is the only thing between it and every date statistic the city has.
**(2) For gsv, third-party imagery.** Not merely defensive: the site has always *displayed* the Google-filtered figures (`adaptCityRecord` reads the per-run JSON's `google_panos` block), so an all-panos catalog column published under the same name as the map's "median age" was two different numbers wearing one label
— the driving page showed one and the overview map the other.
Empirically the copyright filter alone repaired every affected run on a 1,171-JSON catalog (zero `© Google` panos carried a bad date), and the date bound is what fixes the published **`all_panos`** block, which by definition the copyright filter cannot.
The median barely moves (0.000 yr for most runs; >1 yr for 18 of 1,113) because a median is robust to a handful of outliers — the min/max are what were destroyed.
Two consequences worth knowing: a gsv run whose copyright was never recorded (archival imports, #93) keeps every pano, mirroring the frontend's `google_panos_age_stats ?? all_panos_age_stats` fallback;
and the ~15 runs whose imagery is *entirely* third-party (remote Alaskan villages, Addis Ababa) now read NULL rather than reporting a photosphere's date as a drive
— "no Google imagery" is the honest answer, and it is also what stops a photosphere from manufacturing a `driven_unplanned` verdict.
The CSV itself is untouched: it records what the provider said, as the driving-plan archive keeps raw dirty dates beside a NULL parsed one,
so the guard has to be repeated wherever a raw date is read: `city.js` streams the run CSV and therefore carries the JS mirror `isPlausibleCaptureDate`,
and `vis.py`'s folium map and temporal histograms mask the same way (`_plottable_dated_rows`, which narrows dates **only**
— it deliberately does not adopt the pano_id dedup, since those plots have always counted pano references).
Backfill is `scripts/recompute_run_stats.py` (whole series in one pass, or a city's run history mixes two definitions and fakes a trend; `--regenerate-json` also rebuilds the published per-run JSON of runs whose CSV actually holds a bad date, **or whose capture-date columns the pass moves**
— see the loader paragraph below for why the second trigger is not redundant).

## Every one of those seams sits behind ONE reader, and its parse must be at least as permissive as the data on disk (issue #226)

**Every one of those seams sits behind ONE reader, and its parse must be at least as permissive as the data on disk (issue #226).** `fileutils.load_city_csv_file` parsed `capture_date` with a strict `format="%Y-%m-%d"`, and the legacy pre-2026 runs carry **month precision** (`2022-09`) and are never rewritten
— so every date in them coerced to `NaT` while the pano counts, which need no dates, came out perfect.
The result is the hardest shape of failure to notice: a catalog row that is complete, internally consistent and **wrong**, with NULL `oldest/newest_capture_date` and `median_pano_age_years` on cities as large as Nairobi (457k panos), Taipei (434k) and Amsterdam (283k).
Three things generalize past this one bug.
**(1) A repair handle is only as permissive as its reader.** `scripts/recompute_run_stats.py` reads through this same loader, so the whole-series #213 backfill of 2026-08-18 processed every affected run and left it NULL
— the tool that exists to fix stats could not see the ones that were broken.
The tell was an asymmetry worth reusing: across 1,665 gsv runs, 25 went value→NULL (correct — genuinely third-party-only cities) and **zero** went NULL→value.
A repair pass that can only ever clear a value and never restore one is suspicious on its own.
That check is now a committed measurement rather than a remembered one (`scripts/capture_date_precision_analyze.py --measure asymmetry`, which recomputes every gsv run under **both** readers and reports the direction per run).
**(2) The format is PINNED (`format="ISO8601"`), not inferred, and not the format-free `to_datetime(errors="coerce")` the issue originally proposed**
— that reads ONE format off the first non-null value and silently NaTs everything at another precision, so a file mixing `2022-09` and `2022-09-15` loses one of the two populations *depending on which row comes first* (measured both ways; `download_kartaview` already pins the same way after hitting this from the other direction).
ISO8601 accepts day, month and year precision at once and pins the short forms to the 1st, the convention `standardize_capture_date` has always applied
— and it is not slower (3M rows: 0.43 s vs 0.50 s), so there is no #157 cost.
**(3) Repairing the catalog does NOT repair the site.** `json_summarizer._build_provider_summary` takes `all_panos_age_stats` and the capture-year histogram from the **per-run JSON**, not from `runs`, and those JSONs were generated through the same loader.
So the repair is `--regenerate-json`, and its trigger had to widen: gating only on "the CSV holds an impossible date" (#213's rule) rebuilt 5 of the 8 affected runs and left Lagos, Nakuru and La Piedad publishing NULL ages, because whether an affected run *also* carries a corrupt date is luck.
The second trigger is "this pass moved the run's capture-date columns" — the JSON is built from the same seam as those columns, so a column that moves is direct evidence the published file holds pre-fix numbers.
The frontend needed the mirror of the same widening: `city.js` streams the run CSV, so month-precision dates reach `panoDateOrNull`, whose regex matched only `YYYY-MM-DD` and sent them to `new Date("2022-09")`
— a **UTC** parse, i.e. exactly the shift that function exists to prevent, at a whole month's magnitude rather than a day's (west of UTC `2022-09` read back as August and `2022-01` as **2021**; 504,925 of Amsterdam's panos, 4,093 of them January).
The CSVs themselves stay untouched, per the rule above: a run file records what the provider said, and the readers are what get fixed.
Everything measured here is written up in `docs/experiments/capture-date-precision.md`
— the format sweep over the whole corpus, the parse benchmark, and the asymmetry above
— because all three are re-asked whenever a date definition or a reader moves, and re-deriving them means re-reading 15 GB.

## The diffs the strict reader recorded, and why the comparison stays day-exact (issue #245)

Written after the split (2026-09-30), as #226's follow-up.
**#226 fixed the reader but left `run_diffs` alone, and two diffs carried its error.**
`diff.compute_run_diff` compares capture dates as strings, so under the strict reader every pano of a month-precision baseline compared as `None` against a real date on the later side
— a capture-date change recorded for every persisted pano.
On prod (measured 2026-08-22, per the issue) exactly 2 of the 443 diffs computed against a legacy baseline came from a month-precision one: Amsterdam's (71,233 of 71,233 persisted panos "re-dated") and Auckland's (82,156 of 82,204).
The 100.0% / 99.9% shares are the signature; no real re-drive touches every persisted pano.
The published detail CSVs, `cities.json.gz`'s `change` block and the city page's diff overlay all carried the same claim.

**The repair handle is `scripts/recompute_run_diffs.py`**, the diff twin of `recompute_run_stats.py`: it re-derives existing `run_diffs` rows from both runs' CSVs through the current loader, behind the collector's own gates (both CSVs present, `same_grid_geometry`), dry-run by default.
It updates a row IN PLACE rather than through `db.record_diff`, because INSERT OR REPLACE mints a new `diff_id` and the published change blocks pick the newest `diff_id` per run — re-recording an older comparison would change which baseline the site advertises.
Its detail file goes through the collector's own `diff.sync_diff_detail` (#265: written when the diff has changes, removed when it has none), and `--regenerate-json` checks every in-scope to-run's per-run JSON against the row it replays and rebuilds it when they disagree, so a re-run heals a JSON an aborted pass left stale.
Deletion is guarded (#403 review): a stored pointer that is not the row's own deterministic name is removed only when it is diff-detail-shaped and no other row names it, so a corrupt pointer can never take a run CSV or another diff's file with it.
`--provider gsv` is the scope for both known repairs: a census CSV is millions of rows, and every diff loads two.

**No precision-aware comparison was added, because the predicted false positive does not occur.**
The issue expected the NEXT diff of such a city to compare the baseline's `2022-09` (read as 2022-09-01) against "a real `2022-09-15`" and report nearly every pano, and proposed comparing at the coarser of the two precisions.
But a modern GSV run does not hold `2022-09-15`: the Street View metadata API in practice serves `YYYY-MM`, `download_common.standardize_capture_date` pins reduced precision to the 1st before a row is written, and `docs/experiments/capture-date-precision.md` (finding 3) measured **148,455,698 of 148,455,698 GSV day-shaped dates — 100.00% — on the 1st**.
Archival imports (`scripts/import_archival_scrapes.py`) append `-01` to a month the same way, and every month-precision file the sweep found is GSV.
So both sides read 2022-09-01 and compare EQUAL; `tests/test_diff.py` pins that end to end (writer → CSV → real loader → diff), with a genuinely re-dated pano as the negative control.
For GSV a precision-aware comparison would be a no-op, since there is nothing finer than a month to hide.
For a provider with genuine day precision it could only hide real day-level re-dates.
**What would re-open the question:** any GSV writer emitting genuine day precision (`standardize_capture_date` passes a `YYYY-MM-DD` through unchanged, so an API change would arrive silently — re-run the experiment's `sweep`), or a month-precision baseline for a census provider, whose runs carry real day precision (Mapillary's 3.00% on the 1st is what that looks like).

## The same reader must also read coordinates exactly, not just dates permissively (issue #425)

Written after the split (2026-10), as the coordinate twin of the #226 rule.
`fileutils.load_city_csv_file` is the one reader for coordinates as well as dates, and the #226 rule generalizes: **a reader must hand back what is on disk**, and for a float column "on disk" is the text, whose correctly rounded value is the float the writer had.
pandas' default C parser is not correctly rounded: on the dev catalog's six walk CSVs it returned 4.6–5.6 % of latitudes and 37.3–38.6 % of longitudes one ULP off their own text, never more than one.
That is invisible to every statistic that aggregates (measured under both parsers over eight dated grid runs: zero stored grid stats moved, zero query-radius status flips, identical grid keys).
It was fatal to the one consumer that keys on a rounded coordinate: `compute_streetwalk_coverage` joins samples to rows on a 9-decimal key, so a sample one ULP from a half-way point missed its own row and was scored uncovered — Seattle 5 of 247,292 samples on the dev catalog, a real +0.075 km and the published 98.4 → 98.5.
The loader therefore reads `float_precision="round_trip"`, and the test that pins it uses a literal from a real walk CSV that the default parser is known to misplace (`tests/test_fileutils.py`).
Two limits are stated rather than implied.
The option reaches the `np.float64` columns only: nullable `Float64Dtype` columns (`pano_lat`, `pano_lon`) are parsed by another path and are unchanged, and nothing keys on them.
And it costs 1.15–1.42× of load wall-clock with no memory cost (the 16.6M-row Detroit census: 29.6 → 37.1 s).
As with #226, the CSV is never rewritten and the repair handle is a whole-series recompute: here `scripts/recompute_streetwalk_stats.py`, since no grid stat moved.
Numbers: [`experiments/csv-float-parse.md`](experiments/csv-float-parse.md).

## Historical capture-date harvester (`download_gsv_history.py`, issues #2 and #442)

**Historical capture-date harvester (`download_gsv_history.py`, issues #2 and #442).** Separate, opt-in, and out-of-band from the run pipeline.
A normal run records only the *current* capture date per point;
this harvests the FULL official-Google capture history — every past drive-through and its month — which no documented Google API (free or paid) exposes.
It comes instead from an **unpublished endpoint** (the `MapsJsInternalService/SingleImageSearch` RPC behind the Maps-JS `getPanorama().time[]` array), queried directly with no API key.
Its older JSONP shim (`maps/api/js/GeoPhotoService.SingleImageSearch`) was **turned down by 2026-10-17**: the pilot's first 100 requests all got HTTP 200 and the body `GeoPhotoService.SingleImageSearch is decommissioned and turned down.`, which is exactly the shape change the raw store exists for; `download_gsv_history.RPC_URL` records the route that answers (request body and response offsets after sk-zk/streetlevel, MIT).
Because it is undocumented **there is no guarantee it keeps working**, and it is IP-identified rather than key-metered, which fixes three things about how it runs (#442):

- **Where.** Only from a dedicated address that no lab service shares — never makelab1 or makelab2, whose addresses carry Project Sidewalk and the nightly collection.
  The crawler is therefore **catalog-free**: it reads nothing but a work list and writes nothing but files, and its outputs come back to the catalog host under gitignored `experiments/`, never `data/`.
- **How fast.** ONE request in flight, paced by `download_common.AsyncRateLimiter` at a mean of 1/s with jitter 0.6 (the same pacer and coefficient of variation as Mapillary, #292); the pace governs, not sockets.
- **When to stop, written down in advance.** The FIRST 429 or 403, or 8 consecutive failed REQUESTS, ends the run and writes `blocked.json`, and `scripts/harvest_gsv_history.py` refuses to restart until that latch's 24 h cool-down has passed (`--clear-latch` is the operator's deliberate override).
  The breaker counts requests, not rows: every retry is an attempt the endpoint saw, and once it has tripped no further attempt is sent, so the ninth request is never made whatever `fetch_tries` is.
  **A 200 whose body is not an answer counts as a failed request too** — the retired route's `decommissioned` body (a code 5 that is not "no images"), an HTML anti-abuse page, an empty or non-JSON body: its key is NOT done (the ledger row records the reason in `unanswered`, the body is in the raw store, and the key is re-queried in the retry pass and on resume), and a run of them trips the breaker exactly as transport failures do, because that is what a refusal wearing a 200 looks like.
  **The latch is per HOST, not per work list** — a refusal is the address's — so it lives in `--latch-dir`, by default the PARENT of `--out-dir`: every work list crawled under one results directory shares it, and the crawler itself refuses to start while it is in force, not only the CLI.
  `--max-requests` ends a run cleanly instead (exit 83, "re-run to resume"), which is how a timer runs it in daily tranches; a cap is checked per row, so it is a soft ceiling by at most the in-flight row's retries.
  A missing work-list file, or one that is not a work list, exits 2 with a message, beside the argparse rejections.

The unit of work is a **road-walk sample point**, not a grid point (the grid-point sweep #2 shipped was retired in #442: it cost 560 M requests and kept no link between a past capture and the street it was found on).
`scripts/build_gsv_history_worklist.py` writes a city's work list on the catalog host, offline — the latest GSV `drive` walk's samples re-derived from the FROZEN network exactly as coverage derives them (`road_sampling.generate_samples` + the `quantize_coord` join), each with the walk's current official pano as its *anchor*; a city whose GraphML is missing is refused rather than fetched, because this is a place that could reach Overpass.
Pass 1 is each edge's midpoint sample (8.9 M rows catalog-wide), pass 2 the remaining samples, so the two passes query disjoint points; the sizing writeup, `experiments/gsv-history-sizing.md`, lands with [PR #445](https://github.com/jonfroehlich/streetscape-tracker/pull/445).
Per work list and pass the crawler streams three gzip files, each flush its own gzip member so a crash leaves every earlier member readable, and a cut final member is dropped before the next append (a file whose only member was cut is left at 0 bytes and gets its header again, never a headerless append).
Every reader of those files — the resume, the rebuild, `load_sample_history` — reads complete members only, through one streaming scan that is linear in the file size and never holds a file whole (the first version sliced per member, quadratic: about 30 minutes per daily start on a 1 M-sample city, with the file in RAM):
the **raw** bodies, verbatim, first — the only copy that survives a parser the endpoint has outgrown;
the per-sample **history** (one row per sample and dated pano, `SAMPLE_HISTORY_COLUMNS`): the pano served there now (`is_current`) plus every dated pano the service relates to it, each flagged `third_party` from the node's type code — a date no longer marks official imagery, since user photospheres come dated too, so the `© Google` analogue is that flag, never the presence of a date;
everything else a node carries is parsed too (`pano_type`, `capture_day` when served, `elevation_m`, `heading_deg`, `pitch_deg`, `roll_deg`) and the ledger keeps the location's `current_copyright`, `country_code` and `street_name`, because the request was paid for and the raw body is the only other place they live;
and the **queries** ledger last, one row per request with `anchor_in_history` (does the walk's own pano appear in its history?) as the pilot's parser-health signal.
The ledger IS the checkpoint: a key is done only once everything it stands for is on disk, and a key whose only ledger rows are `unanswered` is not done.
Because the ledger is written last, a crash between the history append and the ledger append leaves a key's history rows on disk un-done, and the resume appends them again; the file is never rewritten, so `download_gsv_history.load_sample_history` applies the rule on read — each key's LAST query only, and only keys the ledger holds as done — and every importer and analyzer reads through it, never `pd.read_csv`.
The parsed files are a VIEW of the raw store: `scripts/reparse_gsv_history_raw.py` rebuilds both from it through the same row builders AND the same CSV writer the crawl uses (`rebuild_from_raw`, `write_csv_gz`), byte-identical once decompressed, so a parser that learns to read more, or an endpoint whose shape moves again, costs zero requests — the last answer per key wins, a refusal line never replaces an answer, an unanswered body is kept with its reason only where the key has no answer, and a raw store whose final member a crash cut short rebuilds from everything before the cut with the bytes left out reported.
The per-city, per-pano artifact below (`{city_id}_..._gsv_history_YYYY-MM-DD.csv.gz`, its own `HISTORY_DTYPES` schema, NOT a run/`METADATA_DTYPES` file; `naming.parse_filename` rejects it, `parse_history_filename` parses it) and its `history_harvests` row (schema v3) are **derived** from those per-sample results by the import step that lands them, which #442 leaves to a follow-up.
**That importer must read the per-sample files through `load_sample_history` and carry `third_party` into the per-pano artifact** — the #109 summary filters on it, and a derivation that fed it per-sample rows unfiltered would count user photospheres as official captures.
History is near-static, so a point is harvested once; re-querying a point whose anchor pano has changed is a follow-up, not implemented.

### The capture-history summary and its guard (issue #109)

Each harvest now gets a sibling summary, `{city_id}_..._gsv_history_YYYY-MM-DD.json.gz` (its own schema, v1), written by the harvester right after it catalogs the row.
The aggregate points at it from `providers.<p>.capture_history`, and `city.html` renders it as a legend section ([`frontend.md`](frontend.md)).
The summary applies the same plausibility guard every run statistic does — `analysis.plausible_capture_mask`, through the same function rather than a copy.
The floor is the provider's (2007-01-01 for gsv), and the ceiling is the **harvest date, inclusive**: nothing can have been captured after the search that saw it, and the endpoint's month precision is pinned to the 1st, so a true date can only round toward the past.
`dated_unique_panos` is not called because it needs a run's `status` column; the third-party exclusion it applies by copyright is applied here on the harvest's `third_party` column (in `HISTORY_DTYPES` since #442), because on the RPC route user photospheres come DATED (the pilot: 20 + 179 rows) and a present date is no longer the official-imagery signal it was on the retired route.
A harvest written before the column existed came from that retired route, where "dated" was "official" by construction, and reads as all official.
The frame is deduplicated by `pano_id` defensively.
What the guard drops is **published**, as `panos.implausible_dates_dropped`, never silent — an undocumented endpoint is exactly where a sentinel date would arrive.
That count includes dates the loader could not parse (coerced to NaT, which the mask rejects), so it means "implausible or unreadable", not only "out of range".
The harvest CSV is never rewritten; its one reader is `fileutils.load_history_csv_file`, which pins `format="ISO8601"` with `errors="coerce"` for the #226 reason above.
A harvest that predates the summary gains one from `scripts/backfill_history_json.py` ([`operations.md`](operations.md)); the aggregate reads summaries and never builds them.
Still deferred (issue #109): a per-pano map layer of historical panos, harvest-to-harvest diffs, and an overview metric.
