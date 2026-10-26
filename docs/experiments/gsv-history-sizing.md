# GSV capture-history backfill: how many requests, and how fast history grows

**Ran:** 2026-10-09 (first measurement); record re-made 2026-10-26 from the committed scripts over the same walk files (`--as-of 2026-10-09`) ·
**Verdict:** the query unit is **every road-walk sample point** — **78.1 M** requests, **~904 days** at the paced 60/min on one address — with one query per walk edge midpoint (**8.9 M**, ~104 days) run first as the opening pass of the same crawl, not as a cheaper substitute.
Deduplicating by pano does not change that: the distinct-pano count the obvious dedup reaches is **58.9 M**, only 8.2 % below the 64.1 M Google-anchored samples, because at 15 m spacing nearly every sample matches its own pano.
Between two walks ~62 days apart, 10.2 % of samples changed pano, but **29 % of those changes kept the same capture month and 5 % moved to an older one**, so a walk diff can only nominate a sample for a re-query; it cannot say what happened there.

Issue: [#442](https://github.com/jonfroehlich/streetscape-tracker/issues/442).
Numbers: [`gsv-history-sizing_metrics.json`](gsv-history-sizing_metrics.json), with the per-city and per-pair tables beside it; every figure quoted below is a value in that file's `backfill`, `change_rate`, `walk_totals` or `derived` block, rounded.

## The question

A normal GSV run records one *current* capture date per point.
Google's time-travel feature exposes the full capture history at a location, every past drive-through and its month, through an undocumented endpoint (see [`capture-dates.md`](../capture-dates.md)).
Before pointing a paced crawler at it, two things had to be sized with no requests at all:

1. **How many queries a backfill takes**, under each plausible query unit — every distinct current pano, every road-walk sample, or one query per walk edge.
2. **How fast the history grows** once backfilled, which sets the standing load of keeping it current.

Both are answerable from the road walks already on disk: a walk's sample points are the locations we care about, and its matched pano per sample is the anchor a history query would start from.

## Method

Two measurement scripts and one assembler, catalog and walk CSVs only, no network.

- [`scripts/size_gsv_history_walks.py`](../../scripts/size_gsv_history_walks.py) reads each city's **latest GSV `drive` walk** (1,274 cities), counts samples, PRESENT samples (`OK` or `NO_DATE`), official `© Google` samples, and distinct matched pano ids per city; a 64-bit blake2b hash per id makes the **global** distinct count fit in memory across 78 M rows.
  It also sums the catalog's `edges_total`, `length_km` and `length_km_covered` over the same walks (`--catalog-totals-only`).
- [`scripts/gsv_history_walk_pair_changes.py`](../../scripts/gsv_history_walk_pair_changes.py) takes the 121 cities walked at least twice, joins the last two walks on the collector's own sample key (`road_sampling.quantize_coord`, 9 decimals), and classifies every sample that is Google-anchored in both by whether its pano id changed and, if so, whether its capture date is newer, the same, or older.
  Every one of the 121 pairs matched 100 % of its samples, which is what deterministic sampling over a frozen network predicts, and the key merged no row (`change_rate.key_merged_rows`).
- [`scripts/gsv_history_sizing_analyze.py`](../../scripts/gsv_history_sizing_analyze.py) merges the two scripts' raw outputs (gitignored `experiments/gsv-history-sizing/out/`) into the committed metrics file, copies the three tables beside it, and computes every share, ratio and duration quoted here under `derived`.

The first script also counts pair changes, but only for pairs whose two CSVs hold the identical row list in the identical order; 71 of 121 qualify, so its pair figures are a strict subset and the second script's are the ones quoted.
The first script's per-pano and per-city numbers are the authoritative ones for the backfill.

The 2026-10-09 run keyed the pair join at 5 decimals (~1 m), which merged 656 genuinely distinct samples across 45 of the 121 pairs and measured its "100 % matched" after that merge; the committed record was re-made on 2026-10-26 under the 9-decimal key, with `--as-of 2026-10-09` selecting the same walk files.
The backfill block and both of its tables reproduced byte for byte.
The change-rate counts moved by the un-merged rows: anchored-in-both 4,513,196 → 4,513,758, changed 458,571 → 458,687 (newer 301,502 → 301,581, same month 134,701 → 134,731, older 22,365 → 22,372), lost 4,920 → 4,921, new captures 311,970 → 311,973, and the rate 1,220.48 → 1,220.34 per day per million; every percentage quoted below rounds as it did.
The walk totals are now read as of the same date as the rest: edges and street-km are unchanged, and covered street-km moved 940,863 → 940,510 because the first totals run (2026-10-17) had let eight days of re-walks on unchanged networks into the sum.
`tests/test_gsv_history_sizing.py` runs all three scripts over a synthetic catalog in which each selection rule has a row that would move a number if the rule were dropped, and recomputes every share quoted below from the committed tables.

## Backfill size

Latest GSV `drive` walk per city, 1,274 cities, as of 2026-10-09.

| Quantity | Value |
|---|---|
| Walk sample points | 78,141,072 |
| PRESENT samples | 65,960,216 (84.4 %) |
| Official `© Google` samples | 64,143,008 (82.1 %) |
| Samples with no current official pano | 13,998,064 (21.8 % of the anchored count) |
| Distinct Google panos, summed per city | 60,569,862 (−5.6 % vs samples) |
| Distinct Google panos, global | 58,872,073 (−8.2 % vs samples) |
| Distinct panos of any copyright, global | 60,257,773 |
| Walk edges (catalog totals) | 8,945,469 |
| Street-km walked / covered | 1,169,630 / 940,510 (80.4 %) |

Dedup saves little because a sample every 15 m and a pano every ~10 m ([`pano-spacing.md`](pano-spacing.md)) means almost every sample has its own pano; the global pass over overlapping cities adds only 2.65 percentage points to the within-city 5.6 % (8.2 % against 5.6 %).
One query per walk edge is 6.6× fewer than one per distinct pano, and the edge is the unit the project reports street coverage on — but #442's pilot (written up with the crawler) found that a search returns the captures *at* a point, not along the edge, so one query per edge is a first pass over the same points, not a smaller answer.

| Query unit | Requests | Days at 60/min | Days at 120/min |
|---|---|---|---|
| Walk edge, one query at the midpoint (pass 1) | 8,945,469 | 104 | 52 |
| Distinct official Google pano, global | 58,872,073 | 681 | 341 |
| **Every walk sample point (the chosen unit)** | **78,141,072** | **904** | **452** |

The per-city distribution is heavily skewed, which sets the city order for pass 2:

| Distinct Google panos per city | p10 | p25 | p50 | p75 | p90 | p99 | max |
|---|---|---|---|---|---|---|---|
| n = 1,274 | 504 | 1,241 | 5,191 | 29,257 | 120,712 | 644,458 | 1,104,848 |

The top 10 % of cities hold 72.8 % of all distinct Google panos.
Edges per city run p10 48, p50 494, p90 16,276, p99 106,050, max 357,773.
The per-city Google-anchored share of samples is p10 64.9 %, p50 91.6 %, p90 98.5 %.

## How fast it changes

121 cities walked twice; interval days p10 49, p50 62, p90 67; 4,513,758 samples Google-anchored in both walks.

| Between two walks | Samples | Share of changed |
|---|---|---|
| Matched pano changed | 458,687 (10.2 % of anchored) | — |
| — to a **newer** capture month | 301,581 | 65.7 % |
| — **same** capture month (a re-issued id, or another capture of that month) | 134,731 | 29.4 % |
| — to an **older** capture month (Google changed which capture it serves) | 22,372 | 4.9 % |
| — one side undated | 3 | — |
| Gained official imagery | 21,719 | — |
| Lost official imagery | 4,921 | — |

New captures (newer-date changes plus dated gains) came to **311,973 distinct panos**, or **~1,220 per day per million Google-anchored samples**.
Scaled to all 64.1 M anchored samples that is ~78 k new panos a day, ~0.9 requests/s if each were re-queried.
The ongoing design re-queries every sample whose matched pano changed or that gained imagery, because the diff cannot tell which of them is a new capture; that candidate set is 1.54× the new captures, ~121 k samples a day at the same scaling, ~1.4 requests/s.

Two things make a walk diff a trigger for a re-query rather than a substitute for one, and both are visible only because the capture date was compared alongside the id:

- **29 % of pano changes are not new captures.** A re-issued id with the same month adds nothing to history — but with month-precision dates it cannot be told apart from a neighbouring frame of the same drive or a second drive in the same month, so it is found out by the re-query, not before it.
- **5 % run backwards.** A diff would read an older-date change as history being removed; only a re-query shows whether the older capture was always there.

### Caveats

- **One interval, 7 % of samples.** The rate is one ~62-day interval over 121 cities, not a trend.
- **The 121 cities are not a random draw.** They are the earliest-walked cities, Project Sidewalk deployments prominent among them; scaling their per-million rate to all 64.1 M anchored samples assumes the rest of the catalog is re-driven at the same rate, which nothing here measures.
- **A few re-driven cities dominate.** The per-city share of samples with a new capture is p25 0, p50 0.07 %, p75 2.1 %, p90 30 %, p99 103 % (gains are counted in the numerator but not the anchored-in-both denominator, so the share can exceed 1).
  47 of 121 pairs saw no new capture at all, and the five largest contributors (Cleveland, Chicago, Commerce City, Grant County, Portland) hold 70.8 % of the new captures.
  The per-million rate is therefore a catalog-wide average that any one city will sit far from.
- **"Same month" is a class, not a cause.** The table reads a same-month id change as a re-issued id because that is the simplest account, but the data cannot distinguish it from a different frame of the same month; the crawl's per-point histories are where that question goes next.
- **The 14 M samples with no current official pano are in scope.** #442's pilot found past imagery at most unanchored points (its writeup records the share), so the chosen unit counts every sample, anchored or not; the 904-day figure already includes them.

## Decision

Query every road-walk sample point (78.1 M), from a dedicated address at a paced 60/min, one request in flight: pass 1 is one query per walk edge midpoint everywhere, pass 2 the remaining sample points city by city, Project Sidewalk deployment cities first.
Keep history current by re-querying every sample whose matched pano changed or that gained imagery between two walks — a walk diff is the trigger, never the answer, since a third of pano changes are not new captures and only the re-query says which.
**This supersedes the ongoing-design rule [#442](https://github.com/jonfroehlich/streetscape-tracker/issues/442) was written with** — skip same-date re-issues, and re-query once per changed walk edge rather than per sample — because the capture-date comparison above shows a same-month change cannot be recognised as a re-issue before the re-query, and the pilot showed a query answers for a point, not an edge; the issue text still carries the older rule until it is updated.
The pacing and stopping rule are in [#442](https://github.com/jonfroehlich/streetscape-tracker/issues/442).

## Replication

```bash
# On the host holding the catalog and walk CSVs (makelab2); ~10 min over 78 M rows
python scripts/size_gsv_history_walks.py . experiments/gsv-history-sizing/out/summary.json --as-of 2026-10-09
python scripts/gsv_history_walk_pair_changes.py . experiments/gsv-history-sizing/out/pair_changes.json --as-of 2026-10-09
python scripts/size_gsv_history_walks.py . experiments/gsv-history-sizing/out/walk_totals.json --as-of 2026-10-09 --catalog-totals-only
# Then, in the checkout that commits the record
python scripts/gsv_history_sizing_analyze.py --raw-dir experiments/gsv-history-sizing/out --docs-dir docs/experiments --catalog-label makelab2-prod
```

`--as-of` selects the walks dated on or before 2026-10-09 (a compact `20261009` is normalized first, since the cut is a text compare on `run_date`), so a catalog that has kept collecting reproduces the record; the assembler refuses raw outputs that disagree about their row counts, their `as_of`, or the totals block the scan embeds against the one the totals run wrote.
