# GSV capture-history backfill: how many requests, and how fast history grows

**Ran:** 2026-10-09 (backfill size and change rate), 2026-10-17 (walk totals) ·
**Verdict:** query by **walk edge, not by pano or by sample**.
One query per edge is **8.9 M** requests; the distinct-pano count the obvious dedup reaches is **58.9 M**, only 8.2 % below the 64.1 M Google-anchored samples, because at 15 m spacing nearly every sample matches its own pano.
Between two walks ~62 days apart, 10.2 % of samples changed pano, but **29 % of those changes kept the same capture date and 5 % moved to an older one**, so a walk diff alone cannot stand in for a re-query.

Issue: [#442](https://github.com/jonfroehlich/streetscape-tracker/issues/442).
Numbers: [`gsv-history-sizing_metrics.json`](gsv-history-sizing_metrics.json), with the per-city and per-pair tables beside it.

## The question

A normal GSV run records one *current* capture date per point.
Google's time-travel feature exposes the full capture history at a location, every past drive-through and its month, through an undocumented endpoint (see [`capture-dates.md`](../capture-dates.md)).
Before pointing a paced crawler at it, two things had to be sized with no requests at all:

1. **How many queries a backfill takes**, under each plausible query unit — every grid point, every distinct current pano, every road-walk sample, or one query per walk edge.
2. **How fast the history grows** once backfilled, which sets the standing load of keeping it current.

Both are answerable from the road walks already on disk: a walk's sample points are the locations we care about, and its matched pano per sample is the anchor a history query would start from.

## Method

Two scripts, catalog and walk CSVs only, no network.

- [`scripts/size_gsv_history_walks.py`](../../scripts/size_gsv_history_walks.py) reads each city's **latest GSV `drive` walk** (1,274 cities), counts samples, PRESENT samples (`OK` or `NO_DATE`), official `© Google` samples, and distinct matched pano ids per city; a 64-bit blake2b hash per id makes the **global** distinct count fit in memory across 78 M rows.
  It also sums the catalog's `edges_total`, `length_km` and `length_km_covered` over the same walks (`--catalog-totals-only`, re-run 2026-10-17 because the 10-09 run predates that block).
- [`scripts/gsv_history_walk_pair_changes.py`](../../scripts/gsv_history_walk_pair_changes.py) takes the 121 cities walked at least twice, joins the last two walks on query coordinates rounded to 5 decimals (~1 m), and classifies every sample that is Google-anchored in both by whether its pano id changed and, if so, whether its capture date is newer, the same, or older.
  Every one of the 121 pairs matched 100 % of its samples, which is what deterministic sampling over a frozen network predicts.

The first script also counts pair changes, but only for pairs whose two CSVs hold the identical row list in the identical order; 71 of 121 qualify, so its pair figures are a strict subset and the second script's are the ones quoted.
The first script's per-pano and per-city numbers are the authoritative ones for the backfill.

The scripts committed here are the as-run copies plus an argparse front end and the totals block; the as-run md5s are recorded in the metrics JSON, and `tests/test_gsv_history_sizing.py` runs both over a synthetic catalog holding one sample of every change class and recomputes every share quoted below from the committed tables.

## Backfill size

Latest GSV `drive` walk per city, 1,274 cities, measured 2026-10-09.

| Quantity | Value |
|---|---|
| Walk sample points | 78,141,072 |
| PRESENT samples | 65,960,216 (84.4 %) |
| Official `© Google` samples | 64,143,008 (82.1 %) |
| Distinct Google panos, summed per city | 60,569,862 (−5.6 % vs samples) |
| **Distinct Google panos, global** | **58,872,073 (−8.2 % vs samples)** |
| Distinct panos of any copyright, global | 60,257,773 |
| Walk edges (catalog totals, 2026-10-17) | **8,945,469** |
| Street-km walked / covered | 1,169,630 / 940,863 (80.4 %) |

Dedup saves little because a sample every 15 m and a pano every ~10 m ([`pano-spacing.md`](pano-spacing.md)) means almost every sample has its own pano; the global pass over overlapping cities adds only 2.8 points to the within-city 5.6 %.
One query per walk edge is **6.6× fewer** than one per distinct pano, and the edge is the unit the project reports street coverage on, so the two passes the issue proposes are: pass 1 by edge midpoint everywhere, pass 2 by remaining sample point city by city.

The per-city distribution is heavily skewed, which sets the city order for pass 2:

| Distinct Google panos per city | p10 | p25 | p50 | p75 | p90 | p99 | max |
|---|---|---|---|---|---|---|---|
| n = 1,274 | 504 | 1,241 | 5,191 | 29,257 | 120,712 | 644,458 | 1,104,848 |

The top 10 % of cities hold 72.8 % of all distinct Google panos.
Edges per city run p10 48, p50 494, p90 16,276, p99 106,050, max 357,773.
The per-city Google-anchored share of samples is p10 64.9 %, p50 91.6 %, p90 98.5 %.

At a paced 1 request/s (the pilot default), pass 1 is ~104 days and the full distinct-pano set ~681 days; at 2/s, 52 and 341.
Those are the durations the issue's table carries.

## How fast it changes

121 cities walked twice; interval days p10 49, p50 62, p90 67; 4,513,196 samples Google-anchored in both walks.

| Between two walks | Samples | Share of changed |
|---|---|---|
| Matched pano changed | 458,571 (10.2 % of anchored) | — |
| — to a **newer** capture date | 301,502 | 65.7 % |
| — **same** capture date (re-issued id, no new capture) | 134,701 | 29.4 % |
| — to an **older** capture date (Google changed which capture it serves) | 22,365 | 4.9 % |
| Gained official imagery | 21,719 | — |
| Lost official imagery | 4,920 | — |

New captures (newer-date changes plus dated gains) came to **311,970 distinct panos**, or **~1,220 per day per million Google-anchored samples**.
Scaled to all 64.1 M anchored samples that is ~78 k new panos a day, ~0.9 requests/s if each were re-queried, which is why the issue's ongoing design derives new entries from walk diffs and re-queries only changed samples.

Two things make a walk diff an incomplete substitute for a re-query, and both are visible only because the capture date was compared alongside the id:

- **29 % of pano changes are not new captures.** A re-issued id with the same month adds nothing to history and should not be re-queried.
- **5 % run backwards.** A diff would read an older-date change as history being removed; only a re-query shows whether the older capture was always there.

### Caveats

- **One interval, 7 % of samples.** The rate is one ~62-day interval over 121 cities, not a trend.
- **A few re-driven cities dominate.** The per-city share of samples with a new capture is p25 0, p50 0.07 %, p75 2.1 %, p90 30 %, p99 103 % (gains are counted in the numerator but not the anchored-in-both denominator, so the share can exceed 1).
  47 of 121 pairs saw no new capture at all, and the five largest contributors (Cleveland, Chicago, Commerce City, Grant County, Portland) hold 70.8 % of the new captures.
  The per-million rate is therefore a catalog-wide average that any one city will sit far from.
- **The 14 M samples with no current official pano are not sized here.** Whether old imagery exists where none is served now is the pilot's question; if it does, those samples stay in scope and pass 2 grows by ~22 %.
- **Walk totals are eight days newer than the rest** (2026-10-17 vs 10-09); the city count is unchanged at 1,274, so the edge and street-km figures describe the same walk set.

## Decision

Backfill by walk edge first (pass 1), then by remaining sample point city by city (pass 2), from a dedicated address at a paced rate, and keep history current by deriving entries from walk-to-walk pano changes with a re-query only where the pano id changed.
The design, pacing and stopping rule are in [#442](https://github.com/jonfroehlich/streetscape-tracker/issues/442).

## Replication

```bash
# On the host holding the catalog and walk CSVs (makelab2); ~7 min over 78 M rows
python scripts/size_gsv_history_walks.py . out/summary.json
python scripts/gsv_history_walk_pair_changes.py . out/pair_changes.json
python scripts/size_gsv_history_walks.py . out/walk_totals.json --catalog-totals-only
```

`out/summary.json`, `out/pair_changes.json` and `out/walk_totals.json` are merged, under `backfill`, `change_rate` and `walk_totals`, into the committed metrics file; `per_city.csv`, `pairs.csv` and `pair_changes.csv` are committed beside it under the `gsv-history-sizing_` prefix.
