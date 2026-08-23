# Capture dates: what they describe, and how they are parsed

The date-derived statistics and the two ways they have been silently wrong (#213, #226), plus the
out-of-band history harvester. Read before changing any date definition, any reader that parses a
capture date, or `analysis.dated_unique_panos`.

Split out of `CLAUDE.md` on 2026-08-22 so the always-loaded file stays under Claude Code's
size limit. The prose moved here is the original, with cross-reference pointers repaired where
they would otherwise dangle across the new file boundary; anything written since the split is
under its own heading and says so. `CLAUDE.md` keeps the short rule for each section and points
here for the evidence, the incident history and the details — keep the two in sync.

## The capture-date columns describe official Google imagery, and only dates that can be true (issue #213)

**The capture-date columns describe official Google imagery, and only dates that can be true (issue #213).** `analysis.dated_unique_panos` is the single seam every date-derived statistic reads
— age stats, the capture-year histogram, the daily histogram, in both the catalog and the per-run JSON — and it drops two populations.
**(1) Impossible dates.** Contributor photospheres arrive with corrupt EXIF, and because `oldest/newest_capture_date` are a min and a max, *one* pano owns them: 22 production runs read 2611–2612 and 75 predated Street View, off 1–22 bad panos in cities of 175k–334k.
`EARLIEST_PLAUSIBLE_CAPTURE` is per provider (gsv 2007, mapillary **2004**
— deliberately looser than its 2013 founding, matching the identical rule `download_mapillary.captured_at_to_iso_date` already applies at decode, because contributors upload genuinely old photographs) and the ceiling is the observation date itself, inclusive
— nothing is captured after the query that saw it, and GSV's month-precision dates are pinned to the 1st so they can only round toward the past.
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

## Historical capture-date harvester (`download_gsv_history.py`, issue #2)

**Historical capture-date harvester (`download_gsv_history.py`, issue #2).** Separate, opt-in, and out-of-band from the run pipeline.
A normal run records only the *current* capture date per grid point;
this harvests the FULL official-Google capture history — every past drive-through and its month — which no documented Google API (free or paid) exposes.
It comes instead from an **unpublished endpoint** (`GeoPhotoService.SingleImageSearch`, the backend behind the Maps-JS `getPanorama().time[]` array), queried directly with no API key.
Because it is undocumented **there is no guarantee it keeps working**, and it is IP-identified rather than key-metered, so the harvester is deliberately gentle: low concurrency, per-request jitter, exponential backoff on throttle responses (429/403/503), a circuit breaker that aborts on a run of throttles (cf.
`analysis.detect_systemic_failure`), and a resumable `.harvesting` checkpoint.
It sweeps the city's frozen grid, keeps only panos that carry a date (a present date is the endpoint-native signal of official Google imagery
— the analogue of the `© Google` filter), de-dups by pano_id, and writes a distinct dated artifact `{city_id}_..._gsv_history_YYYY-MM-DD.csv.gz` (its own `HISTORY_DTYPES` schema, NOT a run/`METADATA_DTYPES` file; `naming.parse_filename` rejects it, `parse_history_filename` parses it) plus a `history_harvests` catalog row (schema v3).
History is near-static, so a city is harvested once and re-swept rarely.
Run via `scripts/harvest_gsv_history.py "City"` (city must already be registered so its grid is frozen).
Downstream JSON/diff/web-viz are intentionally deferred.
