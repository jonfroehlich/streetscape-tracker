# KartaView as a third provider: what the API actually gives us

**Ran:** 2026-08-18, re-measured 2026-08-19 ·
**Verdict:** The 360° imagery is real and openly licensed, and the integration is
shaped by three things. **(1)** There is no bulk metadata endpoint — a census is
a paginated radius sweep, so cost scales with imagery density rather than with
area. **(2)** Capture dates are missing for two unrelated populations, and for
one of them **v2 serves the upload timestamp as the capture date**, which no
null-check catches. **(3)** `/1.0/list/nearby-photos/` fills a page by *sequence*,
not by space, so a share taken over a truncated page describes one drive rather
than one neighbourhood.

## The question

[Issue #225](https://github.com/jonfroehlich/streetscape-tracker/issues/225)
proposes adding KartaView (formerly OpenStreetCam, now Grab) as a third provider
alongside GSV and Mapillary. The draw is a footprint the other two lack: Grab's
KartaCam2 fleet produced dense **360°** imagery in Southeast Asia under
**CC BY-SA 4.0**, materially cleaner licensing than Mapillary's for Project
Sidewalk's purposes.

Before writing a collector: is there a bulk endpoint, is 360° flagged, what does
it cost, what breaks, and are the issue's coverage claims true? (Per CLAUDE.md's
top-of-file rule the docs *and* forums came first — that rule exists because
undocumented per-IP limits banned makelab2 twice.)

## Read this before quoting any number below

**The endpoint fills a page by sequence, not by space.** A share computed over a
page that was cut short describes whichever drives happened to fill it. Singapore
is the clearest case in the current record:

| Singapore | r=200 | r=300 | r=400 | r=500 |
|---|---|---|---|---|
| sampled / total | **1224 / 1224** | 2000 / 2072 | 2000 / 2753 | 2000 / 3348 |
| distinct sequences | **18** | 17 | 13 | 9 |
| % SPHERE | **65.69** | 64.95 | 83.45 | 98.30 |

As the circle grows the page holds *fewer* distinct drives and the 360° share
climbs toward 100% — the opposite of what a spatial sample does.

**So a percentage is quotable only where the page held everything** —
`n_sampled >= total_filtered_items`, with `n_sampled > 0`. Each target's
`reported_*` fields in the record are taken from its **largest complete** rung;
`max_working_radius_m` sits beside them as a separate *cost* measurement (the
biggest circle the server answers at all, whose page is usually truncated). The
predicate is `kartaview_probe.is_complete_sample`, and the tests read it from
there rather than restating it.

Completeness is the whole defence, which is why *largest* complete is right and
smallest is wrong: once nothing was paged away, a bigger circle is strictly more
evidence. An earlier version of this rule preferred the smallest and picked
Langkawi's 1-photo sample over its 11-photo one.

This caught real errors. The first draft reported Seattle at 100% SPHERE and
Krabi at 100% null-dated; both were truncated pages.

## Findings

### 1. Capture dates are missing for TWO populations, and v2 invents one of them

`shot_date` is contributor EXIF; `date_added` is server-side upload time.

Across every complete rung — **194 distinct drives** — **19 are wholly undated**,
and they are two unrelated groups:

| | drives | projection | uploader | reading |
|---|---|---|---|---|
| Grab's 2025-11 ingest (`1161…`) | **12** | all SPHERE | `OpenStreetView` | systematic, one batch |
| ordinary community uploads | **7** | all PLANE | 4 different uploaders, 3 cities | everyday missing EXIF |

An earlier draft of this document said *"the only undated sequences are Grab's
2025-11 bulk upload"*, which made this look like one fixable ingest. That was an
artifact of where the sample could see: Seattle's only complete rung then held 88
photos and read 0% null; at r=400 it holds 1,534 and reads **8.87%** null. A
collector has to handle both, and only the Grab half is plausibly temporary.

**What does survive** is the narrower and more useful claim: **360° is not
inherently dateless.** 29 wholly-dated SPHERE drives sit in the same record as
the 12 undated ones, and Grab's *own* 2023 upload of Krabi is fully dated. So the
defect is scoped to an ingest, not to the projection or to the fleet.

**Datedness is decided per drive — but not absolutely.** 439 of 440 per-sequence
rows are wholly dated or wholly undated, which is what licenses attributing one
sampled photo's verdict to a whole sequence. The exception is real and is
recorded: Bucharest sequence `2723` (127 photos, PLANE, a community uploader) is
**14.17%** dated. The previous draft asserted "never mixed" from a seven-row
hand-made table; the committed cross-tab found the counterexample on its first
run. Treat the extrapolation as sound for the Grab batch — one SPHERE ingest —
and never as a law.

#### v2 does not report that batch as undated. It reports an ingest timestamp.

A photo cannot be captured after it was uploaded, so `shotDate < dateAdded` is an
invariant every honest record satisfies. A separate pass audited it directly
across 12 points in 7 cities
([`kartaview-shotdate-audit_metrics.json`](kartaview-shotdate-audit_metrics.json)),
asking v2 for one photo per sequence:

| | sequences | photos |
|---|---|---|
| audited | 48 | 59,263 |
| **violating `shotDate >= dateAdded`** | **10** | **5,665** |

Every violating sequence is the same population: id `1161…`, `SPHERE`,
`deviceName KartaCam2`, uploader `OpenStreetView` (Grab, `userId 44`),
`date_added` **2025-11-19** — across all three open-release cities (Krabi 7,
Yogyakarta 2, Langkawi 1). Photo counts are `countActivePhotos`, each sequence's
full size, so 5,665 is the count for the sequences reached — extrapolated per the
per-drive finding above — and still a lower bound on the batch.

The violation is not always "later": 3 of the 10 (185 photos) read
`shotDate == dateAdded` to the second — Langkawi sequence `11616157` is
`2025-11-19 11:18:29` on both. So the predicate has to be `>=`, not `>`; a strict
`>` is exactly the near-miss guard that lets bad data through. The rest run ahead
of their own upload by seconds to minutes (`11616132`: captured 11:18:35,
uploaded 10:54:07).

**A collector reading v2 cannot detect this by null-checking**, because v2 hands
it a non-null, entirely plausible timestamp. It has to apply the invariant — the
same posture as `plan_match.plausible_capture_date`, which exists because #213
found corrupt third-party EXIF poisoning GSV's capture statistics. A null is
honest and can be handled; a wrong date that looks right cannot.

**Caveat on that audit, and it is the reason to re-run it.** Within its 48
sequences, v1-null and v2-invalid named exactly the same drives. But that pass
selected sequences from each point's *largest working* radius — the truncated
page — so its sample is Grab-heavy and contains **none** of the seven community
undated drives the probe has since found. Whether v2 also fabricates a date for
those, or honestly returns null, is **not yet measured**. The audit script now
walks down to a complete sample; the re-run is outstanding.

**What our own dated snapshots add**, given `date_added` already timestamps
uploads per photo to the second: not capture bounds, but *removal* detection,
detection of a later `shot_date` backfill, and — each time a future batch does
carry `shot_date` — another measurement of the capture→upload lag, which is the
only route from `date_added` to a capture estimate.

### 2. There is no bulk metadata endpoint — the shape is a radius sweep

Not Mapillary's tiles, not GSV's per-point lookup, but a third thing:

- **No metadata vector tiles.** Coverage tiles
  (`/2.0/sequence/tiles/{x}/{y}/{z}.png`) carry geometry only — no ids, dates or
  projection — and their `.json`/`.geojson` variants returned empty at every tile
  tried, *including the official docs' own Jakarta example*.
- **The v2 spatial query is effectively dead.** Any unconstrained
  `/2.0/photo/?lat=&lng=&radius=` returns `apiCode 408 "Query timeout"` —
  including the exact query Grab's own shipped `mcp-karta-view` server issues.
- **What works:** `POST /1.0/list/nearby-photos/` in **radius mode**. Bbox mode
  errors or returns zero in the **southern hemisphere**, i.e. at both cities we
  registered.

A census is therefore a sweep of overlapping circles plus a local join —
architecturally like `collect_mapillary.py`, but on a costlier, flakier fetch,
and **a sweep must paginate rather than sample**.

### 3. Local 360° composition

Every target now reaches a complete sample (see finding 4 for why that changed).
Each row is that target's largest complete rung:

| point | radius | n | % SPHERE | drives | uploaders |
|---|---|---|---|---|---|
| Krabi | 500 m | 1,476 | **100.0** | 11 | 1 |
| Langkawi | 1000 m | 11 | **100.0** | 3 | 1 |
| Singapore | 200 m | 1,224 | **65.69** | 18 | 4 |
| Yogyakarta · Malioboro | 300 m | 1,773 | **13.54** | 49 | 8 |
| Seattle | 400 m | 1,534 | **11.80** | 39 | 17 |
| NYC | 300 m | 1,374 | **7.06** | 26 | 8 |
| Yogyakarta · grid centre | 300 m | 1,673 | **0.48** | 43 | 12 |
| Bucharest | 500 m | 507 | **0.00** | 7 | 3 |

Krabi is the strongest case in the set and it is a Grab fleet city. Seattle and
NYC are the useful pair for the issue's North America claim: two independent
points, 39 and 26 drives, both near ~10%.

### 4. Backpressure, and the page-size trade

`apiCode 690` / `408` arrive inside an **HTTP 400**. They mean *shrink the query*,
not *malformed request* — the opposite of the usual 4xx reading.

The single biggest correction in this revision: **`--ipp` defaulted to 200 while
the documented server cap is 2,000**, and that flag — not the API — was what
capped the original study at "5 of 8 complete samples" with no honest local share
for Malioboro, Singapore or NYC. Their r=100 totals are 274 / 336 / 326. At the
cap, for the **identical 48 requests**, all 8 targets reach a complete sample.

It is a trade, not a free win, and both halves are in the record: a 2,000-row page
is a heavier query, so the backpressure ceiling drops. At `ipp=2000`, r=1000 fails
at 5 of 8 targets and r=500 at 2 of 8, where at `ipp=200` most of those answered.
Net it is strongly worth it — completeness at r=200–500 is what makes a share
quotable at all — but a sweep must budget for failures at the large radii.

Sweep cost per km² is therefore worst exactly where the imagery is richest. That,
not the rate limit, is the real cost driver.

### 5. Rate limits: documented, unenforced, unobservable

100 req/hr anonymous and 1,000 authenticated, per the official FAQ (reachable
only by scraping `kartaview.org/main.*.js` — the docs are a JS SPA) and
corroborated by Bellingcat. Neither was enforced when measured (130 consecutive
requests, zero 429s), and there are **no `X-RateLimit-*` or `Retry-After` headers
at all**, so a client cannot observe its own budget. We pace to the documented
figure regardless — CLAUDE.md's corollary, that undocumented behaviour is unknown
rather than unlimited.

The probe is 48 requests; the shot-date audit is ~110. Both passes ran
authenticated, which each record states in `_about.authenticated`.

### 6. What is better than Mapillary

- **360° flagging**: `projection` ∈ `SPHERE`/`PLANE` plus `field_of_view` on every
  bulk row, free. (Filter **client-side**; the documented server-side
  `projection=` filter causes timeouts.)
- **Sequence identity**: `sequence_id` is first-class and richer than Mapillary's
  — per-drive bbox, `deviceName` (`KartaCam2`), photo count, and `userId`, which
  separates the Grab fleet (`userId 44`, `OpenStreetView`) from individuals.
- **Licence**: CC BY-SA 4.0 on the imagery, with a publisher-specified citation
  string; ShareAlike is viral over derived data. Whether photo *metadata* is
  separately licensed is **unverified** — and metadata, not imagery, is what this
  project would republish, so it has to be settled with
  `geo.kartaview@grabtaxi.com` before a collector ships.

### 7. The issue's coverage claims

| claim (#225) | measured |
|---|---|
| Yogyakarta ~1.6M images / 11,400 km of roads | **unsourced** — on no Grab or KartaView page. Published figures are 85 GB open, 23.8 TB by request. |
| Singapore in the Grab open-360 release | **No** — the release is Yogyakarta / **Langkawi** / Krabi. But Singapore's imagery is dense and **65.69% SPHERE** on a complete sample, so it is a strong point regardless. |
| Langkawi released with 360 coverage | **11 photos** in one r=1000 m circle (complete), vs thousands elsewhere. Sparse where sampled, *not* empty island-wide: a second point 14 km away reached a 102-photo community sequence. |
| Bucharest dense legacy Telenav | Present but **0% 360** — 507 photos, 7 drives, 3 uploaders, all `PLANE`. |
| North America overwhelmingly flat dashcam | **Supported.** Seattle **11.8%** SPHERE over 39 drives / 17 uploaders and NYC **7.06%** over 26 drives. An earlier draft called this claim stale off a truncated page. |

### 8. Maintenance risk: decaying, not dying

Grab is still collecting (Yogyakarta sequences uploaded 2025-11). But the web repo
has **147 open issues**, recent ones unanswered — including a leaked MySQL
collation error inside `findNearbyPhotos`, the exact function a collector calls,
open since 2025-07.

Measured this session: **KartaView's OSM login has been broken for ~2 years.** OSM
issues the code correctly, then their backend returns `401 "Invalid access
token"`. Their frontend calls OSM's OAuth**2** endpoint while their config still
files OSM under `authProviders.oauth1` and the bundle carries OAuth1 parameters;
OSM shut down OAuth 1.0a on 2024-06-01. The migration was completed for their
uploader (`upload-scripts#139`, closed 2024-06-28) but the portal's equivalent
(`openstreetcam.org#392`) has been open and uncommented since 2023-10-31.
Reported as
[openstreetcam.org#404](https://github.com/kartaview/openstreetcam.org/issues/404),
and the capture-date defect as
[openstreetcam.org#405](https://github.com/kartaview/openstreetcam.org/issues/405).

## What this justifies: a Mapillary-shaped collector

**Collect KartaView for coverage, not for recency**, with date provenance built
in — and build it as a **census**, because that is what a radius sweep returns.

### Why census, and what it buys us for free

A sweep returns *every* photo in a circle, so KartaView is a census like
Mapillary, not a per-grid-point sample like GSV. That one fact settles
cross-provider comparability, because the repo already owns the census→CSV
machinery *and* its caveats:

| | GSV | Mapillary | KartaView |
|---|---|---|---|
| primitive | metadata request per grid point | z14 vector tiles | `nearby-photos`, radius, paged |
| kind | **sample** | **census** | **census** |
| 360° flag | n/a | `is_pano` | `projection == "SPHERE"` |
| flat imagery | never emitted | `is_pano == false` | `projection == "PLANE"` |
| drive id | **none published** | `sequence_id` | `sequence_id` (+ device, `userId`) |
| date hazard | corrupt 3rd-party EXIF (#213) | bogus contributor timestamps | **`shotDate >= dateAdded`** |

Consequences, stated so they can be quoted:

- **Coverage rates are directly comparable across all three.** Same frozen grid,
  same `analysis.PRESENT_STATUSES`.
- **Pano counts are comparable to Mapillary but not to GSV** — census vs sample.
  This is the existing caveat, unchanged.
- **360° vs any-imagery works out of the box** (#116): `SPHERE` → `OK`/`NO_DATE`,
  `PLANE` → `FLAT_ONLY` with a null date.
- **`runs.unique_google_panos` is NULL**, as for Mapillary.
- KartaView publishes `sequence_id`, so `pano-spacing.md`'s "group by drive
  first" correction applies — same as Mapillary, impossible for GSV.

### The seams to reuse, and the ones to generalize

- **Reuse unchanged:** `download_mapillary.assign_to_grid` (pure geometry),
  `download_common.generate_grid_points` / `standardize_capture_date` /
  `AsyncRateLimiter` / `redact_credentials` / `HostBlockedError` +
  `host_exit_code`, `host_lock.host_lock`, and all of `analysis.py` / `diff.py` /
  `json_summarizer` — those read the CSV and so are provider-agnostic already.
- **Generalize rather than copy:** `records_to_census` / `_CENSUS_DTYPES`,
  `build_image_rows` / `build_empty_rows`, `dedupe_census`. They are currently
  Mapillary-column-shaped. Generalizing them keeps #157's memory contract — the
  columnar census, the two-rule dedup, the byte-identical golden CSV — in one
  place; a drifted copy is exactly the streetwalk-provider-token class of bug.
- **Genuinely new:** the circle-sweep geometry, the fetch/decode half, and the
  capture-date invariant.

### The capture-date rule: apply the invariant at decode, no schema change

A `shotDate >= dateAdded` value becomes a **null `capture_date`**, so the row's
status is `NO_DATE`. That keeps the shared 9-column `METADATA_DTYPES` untouched,
still counts the imagery as *present* for coverage (`PRESENT_STATUSES` is
`OK`+`NO_DATE`), and keeps it out of every dated statistic — exactly where
`captured_at_to_iso_dates` already applies Mapillary's floor, and exactly #213's
posture. Add `analysis.EARLIEST_PLAUSIBLE_CAPTURE["kartaview"]` as a loose
contributor-archive floor, not a fleet floor.

### Order of work

1. **Road walks first** (the #225 plan's PR 2). Street coverage is what Project
   Sidewalk uses; it needs *presence*, which `projection == "SPHERE"` gives
   cleanly, and does not need a trustworthy capture date.
2. **Measure the sweep cost before scoping anything else.** This is the one
   number that gates production and we do not have it: circles × pages to cover a
   frozen grid bbox. Mapillary's median city is 12 tiles; KartaView is plausibly
   two to three orders of magnitude more. Build `--estimate` first, mirroring
   `estimate_tile_count` / `collect --estimate`.
3. **Grid census (PR 3) is lower value**, because the per-run JSON and aggregate
   are built around capture-date statistics KartaView cannot supply for its best
   imagery.
4. **Budget by density, not area.** Dense points need r≈200–300 m and still fail
   intermittently at r≥500; sparse ones take r=1000 m.

### Running it on prod

The probe scripts **stay laptop-only** — `refuse_on_collection_host` is correct
and must not be relaxed. A probe exists to find a provider's limits by poking at
them, and both prior per-IP bans landed on makelab2.

The *collector* is a different animal — a paced client honouring a documented
limit, which is what the Mapillary channel already is on prod. It runs there as a
normal channel: `KARTAVIEW_ACCESS_TOKEN` in the prod `.env`,
`[providers.kartaview]` in `config/scheduler.makelab1.toml` (**not**
`scheduler.toml` — prod reads the makelab1 file), its own `api_usage` ledger,
stagger and cadence from `assign_schedule`, and — because KartaView meters by
**IP** with no observable headers — a `HOST_KARTAVIEW` entry in the `host_lock`
set beside `mapillary_tiles` and `overpass`, with its own `HOST_EXIT_CODES` value
so the night-level breaker works. Do not enable the channel until step 2's number
exists.

## Caveats

- **Every figure is local to one point.** The two Yogyakarta points, ~1 km apart,
  read 0.48% and 13.54% SPHERE. No city-level share is claimed anywhere here.
- **Page size is a measurement parameter**, and the record states it. Shares at
  `ipp=200` and `ipp=2000` are not comparable, because the first study's
  incomplete rungs describe drives rather than circles.
- **One session, one IP, one afternoon.** The rate limit was never enforced
  against us, so nothing here bounds behaviour under sustained load — the axis
  that produced the Mapillary ban (#198).
- **The shot-date audit predates the completeness fix.** Its sequence selection
  came from truncated pages and is Grab-heavy; its scope claims are sound for what
  it sampled, but the community undated drives are outside it. Re-run pending.
- Langkawi's sparseness is measured on the **live API** at one point per pass; its
  bulk imagery may exist only in the 85 GB downloadable bundle.

## Replicating

```bash
python scripts/kartaview_probe.py --targets                 # list probe points
python scripts/kartaview_probe.py --area krabi              # one point, <= 6 requests

# the canonical record — every rung of the radius ladder, 48 requests
python scripts/kartaview_probe.py --area all --all-radii --docs-dir docs/experiments

# the capture-date audit — 13 points, then two calls per sequence, ~110 requests
python scripts/kartaview_shotdate_audit.py --docs-dir docs/experiments
```

These two commands are the **sole producers** of, respectively,
[`kartaview-feasibility_metrics.json`](kartaview-feasibility_metrics.json) and
[`kartaview-shotdate-audit_metrics.json`](kartaview-shotdate-audit_metrics.json),
and every number above is drawn from one of them.

`--all-radii` is what makes the paging finding checkable: without it the ladder
stops at the first success and records only that rung, which is precisely the rung
that misleads. Each target's `per_radius[]` carries `n_sampled`,
`total_filtered_items` and `complete` so the gate is verifiable per row, plus
`per_sequence[]` — the per-drive cross-tab behind finding 1, computed from pages
already fetched at zero extra request cost. `_about.generated_by` is spelled from
the actual arguments, so a scratch run cannot claim the canonical invocation, and
`_about.ipp` records the page size the shares were measured at.

No credential is required for the probe; the audit needs one to stay inside an
hour. Set `KARTAVIEW_ACCESS_TOKEN` in `.env` to pace at 1,000 req/hr instead of
100. Both scripts **refuse to run on a `makelab*` host** — finding a provider's
limits with the nightly batch's IP is how the last two bans happened.
