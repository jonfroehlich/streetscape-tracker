# Google's driving plan, and the join against observed imagery

The forward-looking archive (#176) and what it means read beside the capture dates we measure.
Read before touching `driving_plan.py`, `plan_match.py` or `www/driving.html`.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## Driving-plan archive (`driving_plan.py`, issue #176)

**Driving-plan archive (`driving_plan.py`, issue #176).** The forward-looking counterpart to the history harvester: Google publishes where it *plans* to drive next at a single mutable, unauthenticated URL (`.../streetview/static/feed/driving/data.json`) that it **overwrites in place**, so every plan revision — a window shifting, a county dropped, a row retiring
— is permanently unobservable without our own dated archive.
Same posture as #2's harvester (an undocumented asset with no guarantee it keeps working; the archive is the hedge against its own fragility) but a far smaller mechanism: stdlib `urllib`, one request to one static URL **once a day**, hard-capped by a politeness gate that short-circuits before any network I/O when today's snapshot row exists.
Catalog: `driving_plan_snapshots` gets a row on **every** fetch ("we looked, it was X"
— sha256, record count, `changed` flag), while the gzipped artifact and the exploded `driving_plan_entries` (one row per (record, district), since the feed comma-joins districts) are written **only when the hash differs**, so an unchanged feed costs one row rather than a duplicate file.
`publish` is stored verbatim and never filtered — the Yes→No flip is the campaign-closed signal, recovered by joining consecutive changed snapshots
— and dirty dates (`13/1/19`, truncated month names) keep the raw string beside a NULL parsed date so no record is ever dropped.
**RAW artifacts live outside `data/`** (`archive/gsv_driving_plan/`, gitignored): the publish rsync ships every `*.json.gz`, and archiving there would republish Google's feed verbatim.
The **derived** join does ship (see "Driving plan × observed imagery" below)
— mirror vs. analysis is the line, and it is stated at all three sites that used to say simply "not published" (`driving_plan.py`, `db.py`'s schema comment, `scheduler.DrivingPlanConfig`).
A `[driving_plan]` config block (default-on) drives a `run-due` hook placed **before** the city loop
— so zero-due nights and deadline/SIGTERM kills can't skip it
— that never *stops* the night (#167's posture) but **does report it**: the returned error reaches `_finish_batch`, which names `DRIVING-PLAN FETCH FAILED` in the alert subject and exits nonzero while still publishing.
Get the asymmetry right before "simplifying" that back to silence
— Google overwrites the feed in place, so a night we fail to snapshot is a revision nobody can ever recover, whereas the plan *summary* rebuild beside it (which already alerted) is regenerable from the catalog at any time.
Silent-and-green meant a week of blocked fetches read as seven clean nights.
`scheduler fetch-driving-plan [--force] [--from-file P --date D]` is the manual/backfill handle, same `ingest()` path.
Treat the plan as advisory, never a contract: Google's own note says listed cities "may include smaller cities and towns within driving distance", so absence is not a guarantee of no driving.

## Driving plan × observed imagery (`plan_match.py` + `generate_driving_plan_summary`)

**Driving plan × observed imagery (`plan_match.py` + `json_summarizer.generate_driving_plan_summary`, `www/driving.html`).** The payoff #176 deferred: the plan read together with the capture dates we measure.
**Neither source is trustworthy alone, and the join is what shows it.** Israel's feed rows all read `publish=No` with 2018–19 windows while our 2026-08-12 runs record newest captures of **2023-10** (Tel-Aviv) and **2023-09** (Haifa)
— Google drove Israel four years after the feed said the campaign closed and never revised it.
So a `closed`/`not_listed` verdict is **never** evidence an area was not driven; `driven_unplanned` names that case, and the page says so in prose, in the verdict tooltip, and in a test that pins the wording.
The reciprocal caveat is archive depth: the first snapshot is **2026-07-31**, so for any drive that already happened the join can only report "plan silent or stale"
— this is a *prospective* instrument that compounds as windows open.
**The join is a string match on state, not geometry.** US districts are counties, which looks like it demands city→county resolution;
measured on prod it does not — 1,981 US entries across 51 states carry only **17 distinct windows**, and **49 of 51 states give every listed county the same window** (Idaho and Oregon differ only by an active-plus-closed pair).
Google publishes one seasonal window per state and enumerates its counties, so `cities.state_name` → feed `region` resolves **1,112 of 1,113 US cities** (92% of the catalog) for a dict lookup;
TIGER boundaries, a resolver script and a link table were all designed and then deleted as measurably worthless.
Tiers are `manual` → `region` → `district` → `country`, published per row so weak matches are visible, and a **country-tier match deliberately does NOT populate a record's `matched_city_ids`**
— it means only "this city's country is in the plan", and letting it through put Salem, Oregon in Idaho's coverage list and erased 67 genuine collection targets.
`normalize_country` folds the feed's local-language and misspelled names (`Brasil`, `España`, `Italia`, `Eesti`, `Hrvatska`, `Kazahkstan`, `FYROM`, `Bosnia` vs `Bosnia and Herzegovina`), without which Spain/Mexico/Brazil read as "not in plan" while their entries sit there under another spelling;
`MANUAL_LINKS` covers what no rule reaches (Israel's Hebrew districts, and DC, whose feed region is `Washington DC` beside a *separate* `Washington` state row).
`parse_loose_date` recovers the feed's dirty `D/M/YY` values **day-first** (unambiguous: `28/11/18`, `16/12/18`) purely for classification — the catalog keeps raw-beside-NULL untouched
— and any window it touches is flagged `window_approximate` so a heuristic never reads as published fact.
**Capture dates are still filtered through `plausible_capture_date`** even though issue #213 fixed the columns at the source: they *used* to be computed over every pano including third-party photospheres, so 22 production runs read 2611–2612 and 75 predated Street View, and publishing those would both look absurd and **manufacture a `driven_unplanned` verdict out of a typo**.
The guard stays because it answers what the fix cannot — a catalog row written before the backfill still holds 2611, and a corrupt date carrying `© Google` would pass the filter
— so an impossible value is treated as absent, last line rather than only line.
The artifact publishes `cities` (one per tracked city, `plan`/`observed` absent-not-null), `records` (one per **feed record**, ~2.8k — not per exploded district, ~11.7k) and `revisions`;
~280 KB gzipped against `cities.json.gz`'s 716 KB.

## Both coverage measures are shown, named, and never conflated

**Both coverage measures are shown, named, and never conflated.** *Grid coverage* is an AREA measure (share of a lattice's sample points with imagery) and exists for every collected city;
*street coverage* is a NETWORK measure (share of road-km driven) joined from `streetwalks.json.gz` via the existing `lookupStreetwalk`, and is the closer answer to "was this actually driven"
— Seattle reads **54.3% grid vs 98.4% street**, because the lattice covers water, parks and private land that no road walk visits.
Different denominators, so street coverage is never a fallback for grid and an unwalked city renders "no data" rather than 0%; only the `drive` network is joined, since `all_public` changes the denominator again.
The manifest is fetched alongside the artifact with `streets.js`'s asymmetric-criticality split (the plan is the page and its failure is fatal; a missing manifest only drops one column).

## Rows are PLACES, not cities

**Rows are PLACES, not cities.** The table unions every tracked city with every plan record covering *no* tracked city (~1,214 + ~2,700 = **~3,900 rows**), because a "Tracked?" column only carries information if untracked places are rows too
— that was the original ask, and anchoring on cities as the row universe is what briefly made this look like it needed a second table.
A record that *does* match tracked cities is represented by those cities and deliberately not duplicated, or Idaho would appear once as a plan area and again as Boise.
Area rows get their `verdict` from the same `plan_match.classify` (with no observation, so they can only ever be a plan status) rather than a JS reimplementation that would drift, and are **labelled by the districts they cover, not their region**
— Google's feed carries ten separate Accra records (different districts, different windows), so a region-only label rendered ten distinct campaigns as one duplicated row.
`planStatus` is likewise three-valued (Active / **Elapsed** / Closed): 214 records are still flagged `publish=Yes` with a window that closed months ago, and calling those "Active" put the column in direct contradiction with the row's own "Campaign closed" verdict.
**The row ceiling is measured, not guessed**: the chassis' pure pipeline costs 28 ms at 1,501 rows (`grid.html` before its #250 pivot dropped it to ~1,190), ~70 ms at 3,900, and **211 ms plus 8.6 MB of HTML at 11,765**
— which is why districts are never exploded into rows and instead stay searchable as a joined string on their record ("Ada" finds Boise).
No virtualization or paging was needed.

## Two views of the past, both shallow, and the page says so

**Two views of the past, both shallow, and the page says so.** (1) `capture_years`
— a per-city `[first_year, [counts…]]` histogram (dense, so the year keys aren't repeated 1,214 times; +25 KB gzipped for 966 cities) rendered as a **sparkline**, since a median age cannot distinguish a city driven in 2019+2022+2024 from one driven once.
It is read from the per-run JSON's `google_panos` block, which is copyright-filtered.
Until issue #213 that made the sparkline **more trustworthy than the `newest_capture` column beside it**, since the corrupt third-party EXIF only contaminated `all_panos`
— the columns are now narrowed at the source, so the two views finally describe one population and the sparkline is no longer the odd one out.
Out-of-window years are dropped **individually** (`_compact_capture_years`, bounded by the same `plan_match.EARLIEST_PLAUSIBLE_CAPTURE`) rather than the whole histogram being refused for spanning >40 years, which is what it did before: a single corrupt 1970 or 2611 bucket — exactly #213's signature
— erased a city's entire real capture history instead of the one bad year.
(2) `revisions` — `plan_match.diff_snapshots` over consecutive **changed** snapshots (entries exist only for those, so consecutive members are exactly the comparable pairs, with no gap to reconstruct).
Grouped by (country, region), *not* by record: keyed by record, a `publish` flip reads as an unrelated delete plus insert.
This is where the Austria/Steiermark **`ibraltar`** corruption surfaces as what it is — a district list rewritten, not a region removed.
Counters are exact; example lists are capped (`_MAX_DETAIL`) so a feed-wide edit can't republish the feed.
**What is NOT available**: per-pano capture history needs #2's harvester and `history_harvests` is **empty — it has never been run**, so "the past" here means dates on *currently published* imagery plus our own snapshot diffs.
The page states both that and the 2026-07-31 archive start, so a short revision log can't read as "Google rarely changes anything".
Regenerated wherever the aggregate is, plus **unconditionally in `_finish_batch`**
— unlike the aggregate and manifest, which gate on `succeeded > 0`, because the feed changes on its own ~weekly schedule and gating would leave the plan stale on exactly the quiet nights.
