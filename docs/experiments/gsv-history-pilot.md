# GSV capture-history pilot: does the endpoint still answer, and does the parser still match?

**Ran:** 2026-10-17, from the dedicated crawler host ·
**Verdict:** the endpoint [#2](https://github.com/jonfroehlich/streetscape-tracker/issues/2) shipped against is **gone**, and the one the Maps JavaScript API uses **answers in full**: 2,000 Seattle edge midpoints at 1 request/s returned **15,515** capture-history rows, **7.8 dated captures per sample**, spanning 2007 to 2026, and a 200-id sample agreed with the documented metadata API on the capture month **200 times out of 200**.
**The midpoint query does not stand in for its street**: pass 2 over the same edges' other 21,728 samples found **13.3×** the distinct panos pass 1 had, and the midpoint alone saw on average **76 %** of the capture months its edge was driven in (every one of them on only 664 of 2,000 edges) — so a per-edge backfill measures re-capture frequency low by about a quarter, and the query unit is a real decision, taken below.

Issue: [#442](https://github.com/jonfroehlich/streetscape-tracker/issues/442) (step 3 of its plan).
Numbers: [`gsv-history-pilot_metrics.json`](gsv-history-pilot_metrics.json), written by `scripts/gsv_history_pilot_analyze.py` over the crawl's parsed files and the spot check's own metrics.

## The questions

The backfill will spend months of paced requests against an undocumented source, so before any of that the issue asked for a pilot of a few thousand requests to answer four things:

1. Does the parser still match the response shape?
2. Do the harvested months agree with what the documented metadata request reports for the same pano ids?
3. What comes back at a sample with **no** current official pano: old imagery where none is served now, or nothing?
4. How much history do the non-midpoint samples add per street segment, i.e. is the backfill one query per edge (8.9 M) or one per sample (59 M)?

## Method

- **Where**: a small dedicated host whose address no lab service shares, with no inbound ports and no credentials on it. The catalog host built the work list and ran the spot check.
- **What**: the latest Seattle GSV `drive` walk (2026-09-11), first 2,000 edges in sample order; `scripts/build_gsv_history_worklist.py --limit 2000`.
  Pass 1 is each edge's midpoint sample (2,000 rows), pass 2 the rest (21,728).
  99.2 % of the samples carry a current official pano (the *anchor*).
- **How**: `scripts/harvest_gsv_history.py` at the stopping-rule defaults — one request in flight, 60/min mean with jitter 0.6, stop on the first 429/403 or 8 consecutive transport failures behind a 24 h latch.
- **Check 2**: `scripts/spot_check_gsv_history.py`, 200 distinct official pano ids drawn with seed 442 from pass 1's history, each asked of the documented `pano=` metadata request on the streets key from makelab2, 300/min; spend recorded in the ledger.

## Finding 1: the route #2 shipped is turned down, and the raw store caught it

The first 100 requests went to the JSONP-style URL the #2 harvester used (`maps/api/js/GeoPhotoService.SingleImageSearch?pb=…&callback=`).
Every one answered **HTTP 200** with the body

```
[[5,"generic","GeoPhotoService.SingleImageSearch is decommissioned and turned down."]]
```

The parser found no panos, the queries ledger showed `panos_dated = 0` for all 100, and the raw file held the reason verbatim.
That is the case the raw store exists for: a 200 with an empty parse is indistinguishable from "no imagery here" without the body.
The run was stopped by hand after 100 requests, and those answers are kept beside the results as evidence.

The route the Maps JavaScript API itself calls, `POST maps.googleapis.com/$rpc/google.internal.maps.mapsjs.v1.MapsJsInternalService/SingleImageSearch` with an `application/json+protobuf` body, answers.
Six probes returned full histories (16, 8 and 5 captures back to 2011 at three anchored samples; one back to 2008).
The request body and response offsets follow [sk-zk/streetlevel](https://github.com/sk-zk/streetlevel) (MIT), the maintained client of that RPC; the crawler was switched to it and pass 1 restarted from zero.

Two things about the new response shape changed the parser:

- **The current pano is part of the list**, with its own date, so it is kept as a history row flagged `is_current`.
- **A date no longer marks official imagery.** User photospheres come dated on this route (pano type code 10, ids of the `CIHM0og…` shape), where on the old route they came undated and "has a date" was the official-imagery filter.
  Every row now carries `third_party` from the node's type code, with the id-shape rule as fallback.
  In pass 1 that was 20 of 15,515 rows (0.13 %).

## Finding 2: pass 1 — the midpoint query, 2,000 edges

| Measure | Value |
|---|---|
| Requests answered | 2,000 of 2,000, all HTTP 200 |
| Response code 0 (imagery) / 5 (none) | 1,998 / 2 |
| Elapsed, achieved pace | 32.9 min, 60.8 requests/min |
| Dated panos per sample: mean · p10 · p25 · p50 · p75 · p90 | 7.76 · 3 · 6 · 8 · 10 · 12 |
| History rows · third-party rows | 15,515 · 20 |
| Distinct official panos | 15,490 |
| Samples with no history at all | 2 |
| Anchor present in its own history | 1,694 of 1,984 anchored (85.4 %) |

Capture years of the official rows, 2007 to 2026 (n = 15,495):

| 2007 | 2008 | 2009 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1,008 | 1,292 | 6 | 1,678 | 187 | 30 | 1,504 | 858 | 686 | 1,013 | 1,347 | 1,675 | 1,318 | 356 | 551 | 376 | 1,600 | 10 |

The shape is the finding: Seattle's streets were driven in distinct campaigns (2007–08, 2011, 2014, 2017–19, 2021, 2025), and the current date alone, which is all a run records, sees only the last of them.
The 2010 and 2020 gaps are real absences in what Google serves, not parser holes; the spot check below reads the same months back from the documented API.

**The 14.6 % of anchors absent from their own history** is the number to carry forward.
The walk matched its anchor with the documented metadata request at the same point and radius; the RPC's own nearest pano differed at 290 samples, and the anchor was not among the related panos it listed.
Whether those are the service choosing a different nearest pano for the same 50 m, or histories that genuinely omit a currently served pano, is answerable from the raw bodies (every one is kept) and is left to the pass-2 section.

## Finding 3: the documented API agrees on every sampled month

200 distinct official pano ids from pass 1's history, seeded, asked of `maps/api/streetview/metadata?pano=…`:

| Result | Count |
|---|---|
| Status `OK` | 200 |
| Capture month matches ours | **200** |
| Month differs | 0 |
| A different pano id echoed | 0 |
| Unknown to the documented API (`ZERO_RESULTS`) | 0 |

Every year in the sample matched completely (2007: 13 of 13 … 2025: 25 of 25).
So the undocumented list and the documented per-pano request describe the same captures, and a harvested month can be quoted with the documented API's confidence.
It also means none of the 200 historical panos has been retired from the documented API — the history the RPC lists is still individually addressable.

## Finding 4: samples with no current official pano still hold history

188 of the 23,728 samples carry no anchor: the walk found no current official pano within its radius there.

| | Pass 1 | Pass 2 |
|---|---|---|
| Unanchored samples | 16 | 172 |
| …that returned dated panos | 14 | 135 (78 %) |
| Dated panos per unanchored sample, mean | 7.3 | 7.7 |

So a street that reads as uncovered today was, three times out of four, driven before, and about as often as a covered one.
Those samples stay in scope: they are exactly the "lost coverage" the temporal series cannot see from current dates.
(Why the walk found nothing there while the search did is the same question as the absent anchors in finding 2 — the walk's documented request and this RPC pick their nearest pano differently at the margin — and is answerable from the raw bodies.)

## Finding 5: what pass 2 adds per edge — the query unit

Pass 2: 21,728 requests in 363 min (59.8/min), all HTTP 200; 21,690 with imagery and 38 with none; 191,644 history rows (179 third-party), **181,706 distinct official panos**; dated panos per sample mean 8.8 (p10 4, p50 8, p90 15); the anchor present in its own history for 86.2 % of anchored samples.

The issue's sizing assumed a search near an edge's midpoint returns the segment's history.
It does not: a search returns the captures **at that point** (the current pano and the panos of earlier drives within reach of it), so each 15 m sample contributes its own set.

| Per edge, official panos | Pass 1 (midpoint) | Pass 1 + 2 |
|---|---|---|
| Distinct panos, summed over the 2,000 edges | 15,495 | 206,337 (**13.3×**) |
| Distinct panos, global | 15,490 | 196,427 |
| Edges where pass 2 added at least one pano | — | 1,895 of 2,000 |
| Panos pass 2 added per edge: p10 · p50 · p90 | — | 6 · 42 · 202 |

For the re-capture question the unit is the **capture month**, not the pano: how many distinct months was this street driven in?

| Per edge, distinct capture months | Pass 1 | Pass 1 + 2 |
|---|---|---|
| Mean | 7.7 | 11.4 |
| Edges where the midpoint saw every month | 664 of 2,000 (33 %) | — |
| Share of the edge's months the midpoint saw: p10 · p25 · p50 · p75 | 0.34 · 0.63 · 0.83 · 1.00 (mean 0.76) | — |

So one query per edge sees three quarters of the drives a street had, complete on a third of streets, and biases re-capture frequency low by about a quarter on the rest.
Whether two or three well-placed samples per edge close that gap is answerable from pass 2's data alone (the samples are already there), and is the next analysis, not the next crawl.

## Decisions

- **Switch routes and keep raw.** Done in [#446](https://github.com/jonfroehlich/streetscape-tracker/pull/446): the RPC route, `third_party` and `is_current` on every row, and `scripts/reparse_gsv_history_raw.py` so the parsed files are a view of the raw store that any later parser can regenerate for zero requests.
- **The pace held.** 23,728 requests over 6.6 hours at a measured ~60/min with no non-200 answer; nothing here argues for raising it, and READ THIS FIRST argues against resolving that by experiment.
- **Unanchored samples stay in the work lists** (finding 4).
- **The query unit is not settled by this pilot.** The issue's pass-1-then-pass-2 plan still holds as a *sequence* — pass 1 everywhere yields a usable 76 %-of-months dataset early — but the issue's claim that pass 1 "yields segment-level history" is false as stated and is corrected there.
  Before the backfill starts, measure from pass 2's data how month coverage grows with k evenly spaced samples per edge; that sets whether the catalog-wide cost is nearer 8.9 M (k = 1), a small multiple of it, or the 59 M of every sample.

## Caveats

- One city, 2,000 edges in sample order (so spatially clustered), one day. The capture-year shape and the 76 % month coverage are Seattle's, where streets have been driven ~11 times; a town driven twice will look different.
- The spot check samples official ids only; third-party ids were not checked against the documented API.
- Finding 1's 100 requests were spent before anything could be read; a pilot that starts with one request would have cost one.

## Replication

```bash
# catalog host
python scripts/build_gsv_history_worklist.py --city "Seattle, WA" --limit 2000 --out-dir W
# crawler host
python scripts/harvest_gsv_history.py --worklist W/<stem>.worklist.csv.gz --pass 1 --out-dir R
python scripts/harvest_gsv_history.py --worklist W/<stem>.worklist.csv.gz --pass 2 --out-dir R
# catalog host, on a 200-id sample of R/<stem>.pass1.history.csv.gz
python scripts/spot_check_gsv_history.py --history SAMPLE.csv --limit 200 --seed 442 --out spot.json --db-path data/streetscape_tracker.db
# wherever R is
python scripts/gsv_history_pilot_analyze.py --results-dir R --worklist W/<stem>.worklist.csv.gz --spot-check spot.json --out docs/experiments/gsv-history-pilot_metrics.json
```
