# What a KartaView census of a frozen grid costs

**Ran:** 2026-08-19 / 2026-08-20 UTC, from a laptop, authenticated (1,000 req/h).
**Record:** [`kartaview-sweep-cost_metrics.json`](kartaview-sweep-cost_metrics.json)
**Issue:** #225. Follow-on to [`kartaview-feasibility.md`](kartaview-feasibility.md).

[`kartaview-feasibility.md`](kartaview-feasibility.md) named exactly one number
as the gate on a production KartaView channel and could not supply it: **circles
× pages to cover a frozen grid bbox**. Its guess was "plausibly two to three
orders of magnitude more" than Mapillary's median 12 tile requests.

It is not. **The median catalog city is 12 circles and cost 16 requests** — the
same order as Mapillary, not two to three above it. The cost is almost entirely
one geometric term, and the term that varies is not the one the feasibility
study expected.

## The question

Can KartaView run as a scheduled channel, and at what cadence over how many
cities? That needs a per-city request count, and the answer has to hold across
two orders of magnitude of bbox area and both of the density regimes the
feasibility study measured.

## Read this before quoting any number below

- **There are two cost numbers and they are not interchangeable.**
  `sweep_requests_observed` is what the walk actually issued — cells, pages,
  **retries** and the per-city **calibration ladder**. `sweep_requests_estimate`
  is the **geometric floor** beneath it: cells plus pages, as if no circle ever
  had to be retried and the radius were already known. The floor is what a
  collector can compute up front from bbox area (the analogue of Mapillary's
  `estimate_tile_count`), so it is what a budget guard gets to work with — but
  over this study it is **1.54× too low** (19,173 against 29,589). Quote the
  observed number for "what does a sweep cost". Quote the floor as the floor.
- **The geometric term is computed; only the overhead is sampled.** A city's
  floor is `root_cells` plus the pages its densest circles need, and
  `root_cells` is exact once the radius is known. Every uncertainty below is in
  the radius and the overhead.
- **`summary.*` is over the STUDY SET, not the catalog.** The set is
  deliberately half area-deciles and half density-regimes, so it over-weights
  large cities: its observed p50 of 557 is not a median city. The
  catalog-representative numbers are the decile table and the `catalog` block.
  Both p50s and p90s are over **n = 14**, so a p90 sits between the 12th and
  13th city and is a shape, not a quantile.
- **Ten of fourteen plans are truncated**, some severely — New York sampled
  **0.4%** of its root cells and Singapore **0.7%**. Their `plan_complete` is
  `false` and their `roots_probed`/`root_cells` are in the record. Treat their
  *overhead* as one thin sample; their *cell count* is exact.
- **Two of those ten had the cap fall inside a root's subdivision cascade**
  (`cells_pending_at_cutoff` = 4 for both Horace and New York). That root sits
  in the scaling denominator while its four unvisited children are missing from
  the numerator, so **those two rows are biased low** — by roughly one root's
  cost, i.e. single-digit percent for Horace and a few hundred requests for New
  York. The record names the hole rather than papering over it.
- The whole study is one session on one IP. Nothing here bounds behaviour under
  sustained load — the axis that produced the Mapillary ban (#198).

## Method

A sweep's requests are

```
requests = calibration + cells_visited + retries + sum over leaves of (pages(total) - 1)
```

because every cell costs one page-1 request whether it ends up a leaf or gets
subdivided, and page 1 reports `totalFilteredItems` for that circle. **Planning
a sweep is therefore paying its first half, and it prices the second half
exactly.** This study issues page 1 only — it never fetches pages 2+ — so the
cell half is measured and the page half computed.

The first two terms are the ones an earlier draft of this file left out. A
retry is a whole HTTP request: `_probe_cell` issues `attempt + 1` of them and
each takes a rate-limiter token, so pricing retries at zero while arguing (below)
that retrying beats subdividing is arguing from a model in which the argument is
free. Across the study the retries cost **174 requests against 392 cells
visited** — 44% on top — and the calibration ladders another **72**. Retries are
scaled with the cells they belong to; calibration is added after the scaling,
because it is paid once per city however many roots get walked.

Cells are squares covered by their circumscribed circle, so the lattice covers
the bbox with no gap and the sweep re-sees each photo ~π/2 times. That
redundancy is why the collector's cross-cell dedup is load-bearing, and why
`photos_seen_sum_over_cells` is what a sweep fetches while
`photos_in_bbox_estimate` divides it out.

Three facts measured while building this decide the design; two contradict the
feasibility study. **Facts 1 and 2 were measured interactively at the console
while the walk was being designed, and their raw responses were not retained** —
there is no committed JSON behind either table, only the counts written down
here. They are reported as what they are: field observations, quotable as
direction and not re-derivable from this repo.

**1. `/1.0/list/nearby-photos/` pagination is exhaustive.** *(Unretained
observation.)* Seattle (47.6062, −122.3321), r=400, ipp=200: pages 1–6 returned
200/200/200/200/200/4 rows with **zero id overlap between any pair**, union =
1,004 = `totalFilteredItems`, page 7 empty. The feasibility probe never
incremented `page`, so this was untested — and the cost model above rests
entirely on it. A truncated circle is **paged**, not subdivided.

**2. apiCode 690 is not a function of page size, and it is not stable in time.**
*(Unretained observation.)* The feasibility study read the ipp trade as "a
bigger page is a heavier query, so the backpressure ceiling drops". At an empty
location that does not hold:

| Horace, ND (46.7588, −96.9038) | result |
|---|---|
| r=1000, ipp=2000, 6 attempts | **0/6 answered** |
| r=1000, ipp=200, 4 attempts | **0/4 answered** |
| r=250, ipp=2000, 4 attempts | 4/4 answered |
| r=125 | answered, **0 photos** |

Page size made no difference across ten attempts, and the location holds no
imagery at all — so 690 is not backpressure from result size there. But it is
also not a fixed property of the location: ~45 minutes later the same point
answered **2/2 at r=1000** during the study run, and *that* half is in the
record (`horace…calibration`). Both observations are real and the record keeps
them apart; the honest reading is that a refusal is a transient to be retried,
not a measurement of anything.

**3. Retrying is 4× cheaper than subdividing.** A retry costs one request; a
subdivision costs four, each of which may cascade — 1 + 4 + 16 = 21 to the
floor. Measured over the study: **88 cells cleared on retry, for 174 extra
requests in total** (two different units — cells against requests — and they
were quoted as one, "88 of 174 retries cleared", until this line). With them the
refusal rate over cells visited is **3.57%**, with **zero** floor failures. 88
cells rescued for 174 requests is a good trade against 88 × 4 subdivisions, and
it is the trade the observed cost number now actually prices.

So the walk calibrates a radius once per city (the feasibility probe's own
ladder, ≤ 6 rungs × 2 probes plus whatever retries they need — 2 to 13 requests,
measured), tiles at it, and retries a refusal three times before subdividing.
Only backpressure may subdivide: asking a server for four requests where it just
failed to serve one is the shape of the Mapillary block, not a fix for it.

## Finding 1: the cell count is one geometric term

`root_cells` tracks `bbox_area / (2 r²)`, and the excess over it is pure
ceiling -- `ceil(W/s) * ceil(H/s)` against `W*H/s²` -- so it shrinks as the bbox
grows: within **10%** above ~350 km², **20%** above ~150 km², **30%** above
~50 km²:

| city | area km² | r | root cells | area/(2r²) | ratio |
|---|---|---|---|---|---|
| Milwaukee | 737.6 | 1000 | 384 | 369 | 1.04 |
| Las Vegas | 2413.7 | 1000 | 1254 | 1207 | 1.04 |
| Singapore | 2547.2 | 500 | 5130 | 5094 | **1.01** |
| New York | 2316.6 | 500 | 4690 | 4633 | **1.01** |
| Attleboro | 150.3 | 1000 | 88 | 75 | 1.17 |
| Ithaca | 19.7 | 1000 | 12 | 10 | 1.22 |
| Buck Grove | 1.0 | 1000 | 1 | 0.5 | 2.00 |

The excess on small cities is that ceiling: a 1 km² bbox still needs one whole
cell. It is bounded and it never goes the other way -- the lattice does not
under-cover.

**So the radius is a factor-of-four lever on the whole cost**, and it is the one
thing a two-probe calibration decides. Everything else — retries, the ladder,
pages, cascades — rides on top of this term as a multiplier; finding 2 measures
it at a median 1.80×.

## Finding 2: cost by catalog percentile

The decile half of the study set walks the catalog's own area distribution
(`catalog` block in the record: all **1,144 enabled cities**, **192,568 km²** of
frozen bbox, measured 2026-08-20 by the SQL in *Replicating* below).

| catalog pct | city | area km² | r | root cells | floor | **sweep requests** | roots sampled |
|---|---|---|---|---|---|---|---|
| p5 | Buck Grove, IA | 1.0 | 1000 | 1 | 1 | **4** | 100% |
| p20 | South Tucson, AZ | 3.3 | 1000 | 4 | 4 | **6** | 100% |
| p35 | Emmitsburg, MD | 8.0 | 1000 | 6 | 6 | **9** | 100% |
| **p50** | **Ithaca, MI** | **19.7** | **1000** | **12** | **12** | **16** | **100%** |
| p65 | Horace, ND | 55.9 | 1000 | 35 | 210 | **478** | 11.4% |
| p80 | Attleboro, MA | 150.3 | 1000 | 88 | 88 | **94** | 62.5% |
| p90 | Chandler, AZ | 354.8 | 1000 | 195 | 975 | **1257** | 8.7% |
| p95 | Milwaukee, WI | 737.6 | 1000 | 384 | 384 | **636** | 8.9% |
| p99 | Las Vegas, NV | 2413.7 | 1000 | 1254 | 1551 | **1885** | 3.0% |

At 1,000 req/h the median city is **58 seconds** and the p95 city is **38
minutes**. The p99 city is 1.9 h.

**Seven of the fourteen cities have a floor exactly equal to their cell count**
— no extra pages, no cascades. What they still pay on top of it is the overhead
the floor omits, and it is not negligible: Attleboro 88 → 94, Bend 80 → 92, but
Milwaukee 384 → **636**, because its 34-root sample carried 22 retry requests
and those scale up with the cells they belong to (×11.3). The overhead has three
unrelated sources and the record separates them:

- **Retries and the calibration ladder**, everywhere: 174 retry requests and 72
  calibration requests across the study. No city escapes the ladder, which is
  why **not one** of the fourteen cost exactly its cell count on the observed
  number even where its floor did.
- **Pages**, where imagery is dense enough that a circle exceeds one 2,000-row
  page. Chandler +56 extra pages (2,900 photos/km²), Seattle +31 (773),
  Singapore +15 (1,469), Las Vegas +1.
- **Refusal cascades**, where cells refuse and subdivide. Horace is the only bad
  case: its floor is already 6.00× its cell count with **zero** extra pages, and
  its observed cost is **13.66×**.

Together those put the observed cost at a median **1.80×** the cell count
(`summary.observed_over_root_cells`), max 13.66×. That multiplier — not the bare
cell count — is what a budget guard has to carry.

Horace is the finding worth carrying forward: it is a **sparse** bbox and the
most expensive per km² in the decile half. That inverts the feasibility study's
expectation that "sweep cost per km² is worst exactly where the imagery is
richest".

## Finding 3: density buys a smaller radius, and that is the real multiplier

| city | area km² | photos/km² | r | root cells | floor | sweep requests |
|---|---|---|---|---|---|---|
| Singapore | 2547.2 | 1,469 | **500** | 5130 | 7329 | **9974** |
| New York | 2316.6 | 159 | **500** | 4690 | 6665 | **12355** |
| Manila | 509.1 | 60 | **500** | 1044 | 1378 | **2139** |
| Seattle | 498.5 | 773 | 1000 | 260 | 490 | 644 |
| Bend | 154.9 | 23 | 1000 | 80 | 80 | 92 |

Three cities calibrated to r=500 and each paid ~4× the cells for it. Note the
correlation with measured density is weak — New York and Manila calibrated down
at 159 and 60 photos/km² while Seattle held r=1000 at 773. The radius is not
predicted by density any more than by page size; it has to be measured per city,
which the calibration ladder costs 2–13 requests to do.

**Singapore, the strongest 360° city in the feasibility study (65.7% SPHERE),
costs ~9,970 requests — 10 h of paced fetching; New York, on a thinner sample,
~12,400.**

## What this justifies

- **A scheduled channel is affordable.** The median city is under a minute. A
  40-city curated set built around the Grab fleet markets is dominated by its
  two or three metros; on the numbers above that is a few nights per pass
  against a 90-day cycle, not a nightly burden.
- **A whole-catalog channel is not, yet.** Tiling all 1,144 enabled cities'
  frozen bboxes at r=1000 is **103,561 circles** — that is the floor, **104 h**
  of paced fetching. Carrying the study's median **1.80×** overhead puts one
  pass at **~186,000 requests ≈ 186 h**, and any city that calibrates to r=500
  quadruples its share. That is a real option on a long cycle, but it is not the
  place to start. (The earlier "~96,000 requests ≈ 96 h" in this section was the
  floor's *floor*: `area / (2 r²)` without the lattice ceiling, without retries
  and without calibration.)
- **Budget by bbox area, not by imagery.** `estimate_requests` for a KartaView
  channel should be `bbox_area / (2 r²)` — the only term computable before the
  walk — defaulting r to 1000, refining it from the previous run's calibrated
  value, and **multiplied by the measured overhead of ~1.8×**
  (`summary.observed_over_root_cells.p50`). Without it the guard is
  systematically under: 1.54× under in aggregate across this study
  (`summary.observed_over_floor`).

## Caveats

- **The extrapolation is unvalidated above 12 root cells.** Only four plans
  completed — Buck Grove (1 cell), South Tucson (4), Emmitsburg (6) and Ithaca
  (12). Bend was *chosen* as the control that would fit a complete plan cheaply
  and so validate the scaling on a real-sized bbox, and at 80 cells against a
  60-request cap **it did not complete either** (51 of 80). So every scaled
  figure in the tables above rests on a method that has never been checked
  against a completed plan bigger than a village. Re-running Bend alone with
  `--max-requests-per-city 100` is the cheap fix and it has not been done.
- **The big-city numbers are thin.** New York's 12,355 rests on 19 of 4,690 root
  cells; Singapore's 9,974 on 35 of 5,130. The seeded shuffle makes those
  samples spatially unbiased, but the overhead multiplier they carry is one
  sample. The cell counts underneath them are exact.
- **Horace and New York are additionally biased low** by a root whose cascade
  the request cap cut in half; see the reading notes at the top.
- **The calibrated radius rests on two probes.** A city that answers at the
  centre and one inset corner is tiled at that radius everywhere; Horace shows a
  bbox can refuse in places its calibration points did not.
- **Refusals are time-varying** (finding 2), so a radius calibrated in a good
  window may cost cascades later, and one calibrated in a bad window is too
  small for the whole run. Both errors are bounded by the retry-then-subdivide
  policy, neither is eliminated by it.
- **Nothing here measures sustained load.** 638 requests over ~2 h, well inside
  the documented 1,000/h. The Mapillary ban came from an axis this study does
  not touch.
- No page beyond page 1 was ever fetched, so the page half of every number is
  computed from `totalFilteredItems` rather than observed.
- **Two of the three design facts above were not retained.** The Seattle
  pagination sweep and the Horace ladder were run at the console before the
  script existed, and only the counts survive. The cost model rests on the first
  of them, so re-establishing it — one city, ~7 requests, written into the
  record — is the cheapest outstanding item here.

## Replicating

```bash
python scripts/kartaview_sweep_cost.py --cities          # the study set
python scripts/kartaview_sweep_cost.py --city bend--oregon--united-states

# the measuring run -- 14 cities, 638 paced requests against the provider
python scripts/kartaview_sweep_cost.py --sample default --docs-dir docs/experiments

# re-derive the record's computed fields from its own raw counters, and refresh
# the catalog block from the local cities table. NO network, NO provider request
python scripts/kartaview_sweep_cost.py --recompute-from-record --catalog-summary \
    --docs-dir docs/experiments
```

Those two commands together are the **sole producers** of
[`kartaview-sweep-cost_metrics.json`](kartaview-sweep-cost_metrics.json), and
they are named apart in it: `_about.measured_by` is the run that spent the
requests, `_about.generated_by` is what wrote the bytes now on disk. Every
number above is drawn from that record. `_about.generated_by` is spelled from
the actual arguments — including `--seed`, which decides *which* roots a
truncated plan probes — so a scratch run cannot claim the canonical invocation.
`tests/test_kartaview_sweep_cost.py` recomputes the quoted tables from the
record.

The catalog totals come from the record's `catalog` block, which is the only
part of this study that reads a local table rather than the provider. Its query
is stored verbatim in `catalog.sql` and is exactly:

```sql
SELECT city_id, center_lat, center_lon, grid_width_m, grid_height_m, step_m
FROM cities WHERE enabled = 1
```

Areas are bbox areas (the frozen grid plus its half-step margin, the same
measure every per-city row uses), so they run ~1% above the raw grid areas the
study set was originally drawn on; the decile cities land at the same
percentiles either way.

The script **refuses to run on a `makelab*` host**, by the same
`refuse_on_collection_host` the feasibility probe uses — imported, not copied,
and pinned by identity in the tests. Finding a provider's limits with the
nightly batch's IP is how the last two bans happened. The offline
`--recompute-from-record` path runs before that guard, deliberately: it issues
no provider request at all.
