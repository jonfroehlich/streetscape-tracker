# What a KartaView census of a frozen grid costs

**Ran:** 2026-08-19 / 2026-08-20 UTC, from a laptop, authenticated (1,000 req/h).
**Record:** [`kartaview-sweep-cost_metrics.json`](kartaview-sweep-cost_metrics.json)
**Issue:** #225. Follow-on to [`kartaview-feasibility.md`](kartaview-feasibility.md).

[`kartaview-feasibility.md`](kartaview-feasibility.md) named exactly one number
as the gate on a production KartaView channel and could not supply it: **circles
× pages to cover a frozen grid bbox**. Its guess was "plausibly two to three
orders of magnitude more" than Mapillary's median 12 tile requests.

It is not. **The median catalog city costs about 12 requests too.** The cost is
almost entirely one geometric term, and the term that varies is not the one the
feasibility study expected.

## The question

Can KartaView run as a scheduled channel, and at what cadence over how many
cities? That needs a per-city request count, and the answer has to hold across
two orders of magnitude of bbox area and both of the density regimes the
feasibility study measured.

## Read this before quoting any number below

- **The geometric term is computed; only the overhead is sampled.** A city's
  cost is `root_cells × overhead`, and `root_cells` is exact once the radius is
  known. Every uncertainty below is in the radius and the overhead.
- **`summary.sweep_requests_estimate` is over the STUDY SET, not the catalog.**
  The set is deliberately half area-deciles and half density-regimes, so it
  over-weights large cities: its p50 of 210 is not a median city. The
  catalog-representative numbers are the decile table.
- **Ten of fourteen plans are truncated**, some severely — New York sampled
  **0.4%** of its root cells and Singapore **0.7%**. Their `plan_complete` is
  `false` and their `roots_probed`/`root_cells` are in the record. Treat their
  *overhead* as one thin sample; their *cell count* is exact.
- The whole study is one session on one IP. Nothing here bounds behaviour under
  sustained load — the axis that produced the Mapillary ban (#198).

## Method

A sweep's requests are

```
requests = cells_visited + sum over leaves of (pages(total) - 1)
```

because every cell costs one page-1 request whether it ends up a leaf or gets
subdivided, and page 1 reports `totalFilteredItems` for that circle. **Planning
a sweep is therefore paying its first half, and it prices the second half
exactly.** This study issues page 1 only — it never fetches pages 2+ — so the
cell half is measured and the page half computed.

Cells are squares covered by their circumscribed circle, so the lattice covers
the bbox with no gap and the sweep re-sees each photo ~π/2 times. That
redundancy is why the collector's cross-cell dedup is load-bearing, and why
`photos_seen_sum_over_cells` is what a sweep fetches while
`photos_in_bbox_estimate` divides it out.

Three facts measured while building this decide the design; two contradict the
feasibility study.

**1. `/1.0/list/nearby-photos/` pagination is exhaustive.** Seattle
(47.6062, −122.3321), r=400, ipp=200: pages 1–6 returned 200/200/200/200/200/4
rows with **zero id overlap between any pair**, union = 1,004 =
`totalFilteredItems`, page 7 empty. The feasibility probe never incremented
`page`, so this was untested — and the cost model above rests entirely on it. A
truncated circle is **paged**, not subdivided.

**2. apiCode 690 is not a function of page size, and it is not stable in time.**
The feasibility study read the ipp trade as "a bigger page is a heavier query,
so the backpressure ceiling drops". At an empty location that does not hold:

| Horace, ND (46.7588, −96.9038) | result |
|---|---|
| r=1000, ipp=2000, 6 attempts | **0/6 answered** |
| r=1000, ipp=200, 4 attempts | **0/4 answered** |
| r=250, ipp=2000, 4 attempts | 4/4 answered |
| r=125 | answered, **0 photos** |

Page size made no difference across ten attempts, and the location holds no
imagery at all — so 690 is not backpressure from result size there. But it is
also not a fixed property of the location: ~45 minutes later the same point
answered **2/2 at r=1000** during the study run. Both observations are real and
the record keeps them apart; the honest reading is that a refusal is a transient
to be retried, not a measurement of anything.

**3. Retrying is 4× cheaper than subdividing.** A retry costs one request; a
subdivision costs four, each of which may cascade — 1 + 4 + 16 = 21 to the
floor. Measured over the study: **88 of 174 retries cleared**, and with them the
refusal rate over cells visited is **3.57%**, with **zero** floor failures.

So the walk calibrates a radius once per city (the feasibility probe's own
ladder, ≤ 6 rungs × 2 probes, accepted only if every probe answers), tiles at
it, and retries a refusal three times before subdividing. Only backpressure may
subdivide: asking a server for four requests where it just failed to serve one
is the shape of the Mapillary block, not a fix for it.

## Finding 1: the cost is one geometric term

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
thing a two-probe calibration decides.

## Finding 2: cost by catalog percentile

The decile half of the study set walks the catalog's own area distribution
(measured over all 1,144 enabled cities on 2026-08-19).

| catalog pct | city | area km² | r | root cells | **sweep requests** | roots sampled |
|---|---|---|---|---|---|---|
| p5 | Buck Grove, IA | 1.0 | 1000 | 1 | **1** | 100% |
| p20 | South Tucson, AZ | 3.3 | 1000 | 4 | **4** | 100% |
| p35 | Emmitsburg, MD | 8.0 | 1000 | 6 | **6** | 100% |
| **p50** | **Ithaca, MI** | **19.7** | **1000** | **12** | **12** | **100%** |
| p65 | Horace, ND | 55.9 | 1000 | 35 | **210** | 11.4% |
| p80 | Attleboro, MA | 150.3 | 1000 | 88 | **88** | 62.5% |
| p90 | Chandler, AZ | 354.8 | 1000 | 195 | **975** | 8.7% |
| p95 | Milwaukee, WI | 737.6 | 1000 | 384 | **384** | 8.9% |
| p99 | Las Vegas, NV | 2413.7 | 1000 | 1254 | **1551** | 3.0% |

At 1,000 req/h the median city is **43 seconds** and the p95 city is **23
minutes**. The p99 city is 1.5 h.

**Seven of the fourteen cities cost exactly their cell count** — overhead 1.00×.
The rest carry overhead from two unrelated causes, and the record separates
them:

- **Pages**, where imagery is dense enough that a circle exceeds one 2,000-row
  page. Chandler +56 extra pages (2,900 photos/km²), Seattle +31 (773), Singapore
  +15 (1,469).
- **Refusal cascades**, where cells refuse and subdivide. Horace is the only bad
  case: 6.00× overhead with **zero** extra pages.

Horace is the finding worth carrying forward: it is a **sparse** bbox and the
most expensive per km² in the decile half. That inverts the feasibility study's
expectation that "sweep cost per km² is worst exactly where the imagery is
richest".

## Finding 3: density buys a smaller radius, and that is the real multiplier

| city | area km² | photos/km² | r | root cells | sweep requests |
|---|---|---|---|---|---|
| Singapore | 2547.2 | 1,469 | **500** | 5130 | **7329** |
| New York | 2316.6 | 159 | **500** | 4690 | **6665** |
| Manila | 509.1 | 60 | **500** | 1044 | **1378** |
| Seattle | 498.5 | 773 | 1000 | 260 | 490 |
| Bend | 154.9 | 23 | 1000 | 80 | 80 |

Three cities calibrated to r=500 and each paid ~4× the cells for it. Note the
correlation with measured density is weak — New York and Manila calibrated down
at 159 and 60 photos/km² while Seattle held r=1000 at 773. The radius is not
predicted by density any more than by page size; it has to be measured per city,
which is exactly what calibration costs ≤ 13 requests to do.

**Singapore, the strongest 360° city in the feasibility study (65.7% SPHERE), is
the most expensive at ~7,300 requests — 7.3 h of paced fetching.**

## What this justifies

- **A scheduled channel is affordable.** The median city is 43 seconds. A
  40-city curated set built around the Grab fleet markets is dominated by its
  two or three metros; on the numbers above that is a few nights per pass
  against a 90-day cycle, not a nightly burden.
- **A whole-catalog channel is not, yet.** Summing the frozen bboxes of all
  1,144 enabled cities gives **191,835 km²**; at r=1000 and overhead 1.0 that is
  **~96,000 requests ≈ 96 h** of paced fetching for one pass — and any city that
  calibrates to r=500 quadruples its share. That is a real option on a long
  cycle, but it is not the place to start.
- **Budget by bbox area, not by imagery.** `estimate_requests` for a KartaView
  channel should be `bbox_area / (2 r²)`, defaulting r to 1000 and refining it
  from the previous run's calibrated value.

## Caveats

- **The big-city numbers are thin.** New York's 6,665 rests on 19 of 4,690 root
  cells; Singapore's 7,329 on 35 of 5,130. The seeded shuffle makes those
  samples spatially unbiased, but the overhead multiplier they carry is one
  sample. The cell counts underneath them are exact.
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

## Replicating

```bash
python scripts/kartaview_sweep_cost.py --cities          # the study set
python scripts/kartaview_sweep_cost.py --city bend--oregon--united-states

# the canonical record -- 14 cities, 638 requests
python scripts/kartaview_sweep_cost.py --sample default --docs-dir docs/experiments
```

That last command is the **sole producer** of
[`kartaview-sweep-cost_metrics.json`](kartaview-sweep-cost_metrics.json), and
every number above is drawn from it (or, for the catalog totals, from the
`cities` table by the query named in finding 3's paragraph).
`_about.generated_by` is spelled from the actual arguments, so a scratch run
cannot claim the canonical invocation. `tests/test_kartaview_sweep_cost.py`
recomputes the quoted tables from the record.

The script **refuses to run on a `makelab*` host**, by the same
`refuse_on_collection_host` the feasibility probe uses — imported, not copied,
and pinned by identity in the tests. Finding a provider's limits with the
nightly batch's IP is how the last two bans happened.
