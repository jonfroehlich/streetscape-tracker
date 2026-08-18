# KartaView as a third provider: what the API actually gives us

**Ran:** 2026-08-18 ·
**Verdict:** The 360° imagery is real and openly licensed, but two things bite.
**Grab's fleet imagery carries no capture date**, which is the field this project
exists to track. And **`/1.0/list/nearby-photos/` does not return a spatial
sample**, so almost every percentage you can compute from it is a paging
artifact — including several in the first draft of this document.

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

**The endpoint pages by sequence, not by space.** As the query radius grows, the
number of distinct drives in the returned page *falls* and the 360° share
*rises*:

| Seattle | r=100 | r=200 | r=300 | r=400 | r=500 |
|---|---|---|---|---|---|
| distinct sequences | **12** | 10 | 6 | 2 | **1** |
| distinct uploaders | **9** | 5 | 4 | 2 | **1** |
| % SPHERE | **7.95** | 20.5 | 54.0 | 90.5 | **100.0** |

A genuine spatial sample would contain *more* drives in a bigger circle, not
fewer. And the ordering is provably not by distance: if it were, the r=500 page
would contain the r=100 photos, but those are 7.95% SPHERE and the r=500 page is
100% — they are disjoint sets. What actually happens is that one long 360°
sequence fills the 200-row page.

**So a percentage is trustworthy only where the sample is complete** — where
`n_sampled >= total_filtered_items`, i.e. nothing was paged away. That happens at
only 5 of 8 probe points, and only at the smallest radii. Every table below is
marked accordingly, and `per_radius[]` in the record carries the full ladder so
this is checkable.

This caught two errors in this study's own first draft: it reported Seattle at
100% SPHERE (actually ~8% locally) and Krabi at 100% null-dated (actually 31%).
Both were the artifact above.

## Findings

### 1. Grab fleet imagery has no capture date — and that is per-uploader, not per-city

`shot_date` is contributor EXIF; `date_added` is server-side upload time. The
durable relationship is visible where 360° share and null-date share track each
other almost exactly, at Malioboro (uploader `OpenStreetView`, the Grab fleet
account, `userId 44`):

| Malioboro | r=100 | r=200 | r=300 | r=400 | r=500 |
|---|---|---|---|---|---|
| % SPHERE | 19.0 | 63.5 | 100.0 | 100.0 | 100.0 |
| % null `shot_date` | 19.0 | 63.0 | 100.0 | 100.0 | 100.0 |

The two move together to within half a point across five radii: **the Grab 360°
sequences are exactly the undated ones.** Seattle is the control that shows this
is about the *uploader*, not the projection — its r=500 page is 100% SPHERE and
**0% null**, from a single community contributor. Community 360° is dated; Grab
fleet 360° is not.

The only fallback is `date_added`, which is *upload* time: Krabi's and
Yogyakarta's Grab sequences were bulk-uploaded on the same day (2025-11-19, with
adjacent sequence ids) though their imagery was captured at unknown and certainly
different times. So a collector may fall back to `date_added`, but **must record
which field it used**. Publishing an upload year in the same column as a capture
year would silently invalidate every temporal claim built on it — a mistake this
study's own tooling made before it was split into
`capture_year_counts_shot_date` and `upload_year_counts_date_added`.

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
architecturally like `collect_mapillary.py`, but with a costlier, flakier fetch,
and with the paging caveat above meaning **a sweep must paginate**, not sample.

### 3. Local 360° composition, where it can be measured honestly

Complete samples only (`n >= total`, nothing paged away):

| point | radius | n | % SPHERE | uploaders |
|---|---|---|---|---|
| Seattle | 100 m | 88 | **7.95** | 9 |
| Yogyakarta · grid centre | 100 m | 163 | **0.0** | 4 |
| Krabi | 100 m | 125 | **100.0** | 1 |
| Bucharest | 100/200/300 m | 21/67/145 | **0.0** | 1–2 |
| Langkawi | 1000 m | **11** | 100.0 | 1 |

Malioboro, Singapore and NYC never reach completeness — their totals exceed the
`ipp` cap even at r=100 — so no honest local share is available for them here.
Their large-radius 100% figures are the artifact, not a result.

### 4. Failures are backpressure, and they scale with density

`apiCode 690` / `408` arrive inside an **HTTP 400**. They mean *shrink the query*,
not *malformed request* — the opposite of the usual 4xx reading. Failure at
r=1000 m tracked local density: 4/4 at Yogyakarta's centre, ~2/4 at NYC, 0/4 at
sparse Krabi, Bucharest and Langkawi. Repeats at a *fixed* radius were perfectly
reproducible (four identical runs), so the variability is entirely in which rung
succeeds.

Sweep cost per km² is therefore **worst exactly where the imagery is richest** —
that, not the rate limit, is the real cost driver.

### 5. Rate limits: documented, unenforced, unobservable

100 req/hr anonymous and 1,000 authenticated, per the official FAQ (reachable
only by scraping `kartaview.org/main.*.js` — the docs are a JS SPA) and
corroborated by Bellingcat. Neither was enforced when measured (130 consecutive
requests, zero 429s), and there are **no `X-RateLimit-*` or `Retry-After` headers
at all**, so a client cannot observe its own budget. We pace to the documented
figure regardless — CLAUDE.md's corollary, that undocumented behaviour is unknown
rather than unlimited.

The whole of this study fits inside the anonymous tier.

### 6. What is better than Mapillary

- **360° flagging**: `projection` ∈ `SPHERE`/`PLANE` plus `field_of_view` on every
  bulk row, free. (Filter **client-side**; the documented server-side
  `projection=` filter causes timeouts.)
- **Sequence identity**: `sequence_id` is first-class and richer than Mapillary's
  — per-drive bbox, `deviceName` (`KartaCam2`), photo count, and `userId`, which
  is what separates the Grab fleet (`userId 44`, `OpenStreetView`) from
  individuals. Given finding 1, **`userId` is the field that tells you whether a
  photo can be dated at all.**
- **Licence**: CC BY-SA 4.0 on imagery and metadata, with a publisher-specified
  citation string. ShareAlike is viral over derived data.

### 7. The issue's coverage claims

| claim (#225) | measured |
|---|---|
| Yogyakarta ~1.6M images / 11,400 km of roads | **unsourced** — on no Grab or KartaView page. Published figures are 85 GB open, 23.8 TB by request. |
| Singapore in the Grab open-360 release | **No** — the release is Yogyakarta / **Langkawi** / Krabi. Singapore does have dense imagery (10,903 in one r=1000 m circle) but no complete sample was obtainable. |
| Langkawi released with 360 coverage | **11 photos** at r=1000 m (complete), vs thousands elsewhere; coverage tile 2 KB against 15–24 KB. Effectively empty on the live API. |
| Bucharest dense legacy Telenav | Present but **0% 360** — complete samples at three radii, all `PLANE`. |
| North America overwhelmingly flat dashcam | **Probably true — an earlier draft of this document wrongly called it stale.** Seattle's only complete sample is **7.95% SPHERE across 9 uploaders**. The 100% figure that suggested otherwise was one 360° sequence filling a page. |

Krabi is the one point with a complete sample that is **100% SPHERE** — it is the
strongest case in the set, and it is a Grab fleet city.

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
[openstreetcam.org#404](https://github.com/kartaview/openstreetcam.org/issues/404).

## What this justifies

**Collect KartaView for coverage, not for recency**, and only with date
provenance built in.

1. **Road walks first** (the #225 plan's PR 2). Street coverage is what Project
   Sidewalk uses; it needs *presence*, which `projection == "SPHERE"` gives
   cleanly, and does not need a trustworthy capture date.
2. **A sweep must paginate, not sample.** Finding 3 means one page per circle
   measures a drive, not a neighbourhood. This is the single biggest cost
   unknown remaining and should be measured before PR 2 is scoped.
3. **Carry the date's source.** A `date_source` column beside `capture_date`, or
   nulls — never a silent `shot_date or date_added`.
4. **Grid census (PR 3) is lower value**, because the per-run JSON and aggregate
   are built around capture-date statistics KartaView cannot supply for its best
   imagery.
5. **Budget by density, not area.** Dense cities need r≈300 m and still fail
   intermittently; sparse ones take r=1000 m.

## Caveats

- **n = 200 per page** (`ipp`), against totals in the thousands. Only 5 of 8
  points ever returned a complete sample; the rest support no share claim.
- **Every figure is local to one point.** Two Yogyakarta points 1 km apart differ
  completely. No city-level share is claimed anywhere in this document.
- **One session, one IP, one afternoon.** The rate limit was never enforced
  against us, so nothing here bounds behaviour under sustained load — the axis
  that produced the Mapillary ban (#198).
- Langkawi's emptiness is measured on the **live API**; its imagery may exist only
  in the 85 GB downloadable bundle.

## Replicating

```bash
python scripts/kartaview_probe.py --targets                 # list probe points
python scripts/kartaview_probe.py --area krabi              # one point, ~6 requests
python scripts/kartaview_probe.py --area nyc --repeat 4     # repeatability at fixed radius

# the canonical record — every rung of the radius ladder, ~48 requests
python scripts/kartaview_probe.py --area all --all-radii --docs-dir docs/experiments
```

The last command is the **sole producer** of
[`kartaview-feasibility_metrics.json`](kartaview-feasibility_metrics.json), and
every number above is drawn from it. `--all-radii` is what makes the paging
finding checkable: without it the ladder stops at the first success and records
only that rung, which is precisely the rung that misleads. Each target's
`per_radius[]` carries `n_sampled` beside `total_filtered_items`, so completeness
is verifiable per row; `_about.generated_by` is spelled from the actual
arguments, so a scratch run cannot claim the canonical invocation.

No credential is required; set `KARTAVIEW_ACCESS_TOKEN` in `.env` to pace at
1,000 req/hr instead of 100. The script **refuses to run on a `makelab*` host** —
finding a provider's limits with the nightly batch's IP is how the last two bans
happened.
