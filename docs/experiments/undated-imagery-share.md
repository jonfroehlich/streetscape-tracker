# How much imagery carries no usable capture date? (issue #257)

**Question.** Issue #257 made a `NO_DATE` pano count as road-walk coverage, matching the grid.
The change was justified in three prose claims — that undated imagery is *"large by construction for KartaView, small but real for Mapillary, and empty in practice for GSV"* — and none of them was a number,
even though `docs/experiments/` and the catalog held both of the numbers involved.

Two decisions rest on them.
**(1)** The fix produces a one-time phantom positive coverage delta on every pre-existing walk series, published in `streets.html`'s Δ column;
whether that is invisible noise or the largest change a city's series has ever recorded is a matter of how big the undated population is.
**(2)** Since the fix, `covered` and `dated` are different populations, so `median_covered_age_years` is taken over a subset — and how much of a subset decides whether it can still be read as "the age of the imagery."

**Answer, and it is not the one the pooled averages suggest.**
Undated imagery does not arrive as diffuse noise that a mean can describe.
It arrives in **batches**, and we now have three independent instances of that shape: KartaView's single 2025-11-19 Grab ingest, Mapillary's Denver-metro uploads, and GSV's handful of big-baseline metros.
So the number that matters for any decision is the **per-run maximum**, not the pooled share — they differ by three orders of magnitude within a single provider.

Numbers below come from [`undated-imagery-share_metrics.json`](undated-imagery-share_metrics.json), written by `scripts/undated_imagery_share_analyze.py` against the **makelab2 production catalog** (`catalog_label: makelab2-prod`).
Read entirely out of the catalog and an already-committed metrics file: no network, no credentials, no collection.

> **Correction (2026-10, [#289](https://github.com/jonfroehlich/streetscape-tracker/issues/289)): the first production pass's Mapillary *of queried* figures were wrong by construction, and this file now carries their regeneration.**
> The script that produced them divided `status_no_date` by `runs.total_points`, and `total_points` is a **row** count.
> A Mapillary run is a census that writes one row per image plus one row per empty grid point, so that denominator was a mixture of images and points, not the grid it was read as.
> The affected numbers were Mapillary's `points_queried`, `pooled_pct_of_queried` and `per_run_pct_of_queried` — the old 2.73 pp maximum among them.
> **Unaffected:** every *of present* figure (`no_date / (ok + no_date)` is images over images for a census, points over points for GSV, and never read `total_points`), the run counts, and the batch analysis.
>
> The fix is #289's `runs.total_grid_points` (schema v20), the de-duplicated grid-point count, which the script now divides by.
> On 2026-10-04 it was backfilled on makelab2 for all 4,947 production runs ([procedure](../operations.md#schema-v20-and-the-runstotal_grid_points-backfill-issue-289)), and the metrics file was regenerated there by the committed script at `e32584d`; `runs_without_grid_points` is 0 for every provider, so no run is missing from an *of queried* figure.
> The pre-#289 pass is preserved in git history as written at `d974fd4`.
>
> The correction predicted one thing before it was run: per run, the grid is never larger than the row count, so the corrected Mapillary maximum could not come back below 2.73.
> It came back at **3.00 pp** (`catalog.mapillary.per_run_pct_of_queried.max`), held by **Commerce City, Colorado, 2026-07-31** (`max_run_pct_of_queried`) — not Denver, which holds the largest undated *count*; a per-run maximum of a ratio need not be the largest numerator.
>
> **Every *of queried* figure is an upper bound on the phantom shift, not the shift itself** (`of_queried_kind: upper_bound` for all four providers).
> For a census provider the numerator counts undated *images*, several of which can share a point, at points that may also hold a dated image; the exact census shift needs the per-point join, which only the CSV holds.
> GSV is an upper bound too, for a narrower reason: 13 of its 2,429 production runs hold more rows than grid points (`runs_with_more_rows_than_grid_points`, legacy runs that repeated a point), and in such a run an undated row counted twice still counts twice in the numerator.
> So an *of queried* figure settles visibility in one direction only: a bound under 0.05 pp means invisible, and a bound above it settles nothing.

## Read the production catalog, not a dev one — the answer inverts

Recording this first because it nearly shipped as a finding.
The first pass of this measurement ran against a development laptop's catalog, which holds the full 1,144-city GSV baseline but only **three** Mapillary runs.
It concluded that Mapillary emits no undated imagery at all and that *"small but real for Mapillary is not supported by anything we hold."*

Production said the opposite: at the first production pass (1,201 Mapillary runs) **0.150%**, roughly 17× GSV's rate, and at the regeneration (1,959 runs) **0.109%**, roughly **12×** GSV's 0.0092%.
The original claim was right and the dev-catalog refutation was an artifact of n=3.
A per-provider question cannot be answered from a catalog that is only well-populated for one provider, and the run counts have to be quoted next to the share for exactly that reason.

## Three sampling frames, never pooled

| | frame | pooled undated | runs with **any** | worst single run |
|---|---|---|---|---|
| **GSV** | 2,429 prod grid runs, 309.9M present panos | 0.0092% | 149 of 2,405 | **0.345%** |
| **Mapillary** | 1,959 prod grid runs, 95.6M present panos | 0.109% | **19 of 789** | **23.3%** |
| **KartaView** (catalog) | 527 prod grid runs, 89,548 present photos | 7.24% | 2 of 6 | **32.2%** |
| **KartaView** (audit) | API sample of 48 sequences, 59,263 photos | 9.56% | 10 of 48 sequences | — |
| **Panoramax** | 32 prod grid runs, 2.63M present pictures | 0.00011% | 1 of 27 | 0.00084% |

"Runs with any" is out of the runs holding any present imagery (`per_run_pct_of_present.n`), since a run with none has no share to take.
The catalog rows are a census of what we collected; the audit row is a sample of what the provider serves, lifted from [`kartaview-shotdate-audit_metrics.json`](kartaview-shotdate-audit_metrics.json) rather than re-probed, since that number already has a writeup and caveats of its own ([`kartaview-feasibility.md`](kartaview-feasibility.md)).
They are not a controlled comparison.
KartaView's catalog row is new since the first pass, and it is thin: the channel is opt-in (#248), and only **6** of its 527 runs hold any imagery at all, so it is two cities rather than a rate.
Its 7.24% sits beside the audit's 9.56%, which its own writeup calls Grab-heavy and a lower bound; the two frames agree in order and are still not pooled.

**And all three are proxies for the quantity actually at issue, which is the ROAD-WALK undated share.**
No walk has ever recorded one: `street_walks` and the coverage artifact counted covered samples and nothing else, which is the gap #257 closed by adding `dated_covered_samples` per edge and `covered_samples_dated`/`dated_pct_of_covered` to the artifact's summary blocks.
Until walks collected under that column accumulate, the grid share is the only estimate available, and it is an estimate of the right *order* rather than of the value
— a walk samples only on-street points, where imagery is denser and plausibly better dated.

## The distribution is the finding: undated imagery comes in batches

GSV is zero through the 90th percentile and Mapillary through the 95th, and both have a long right tail.
Per run, as a share of present panos:

| | n | p50 | p90 | p95 | max | worst run (`max_run_pct_of_present`) |
|---|---|---|---|---|---|---|
| GSV | 2,405 | 0.0% | 0.0% | 0.0025% | **0.345%** | Del Mar, California, 2025-01-22 |
| Mapillary | 789 | 0.0% | 0.0% | 0.0% | **23.3%** | Commerce City, Colorado, 2026-07-31 |
| KartaView | 6 | 0.0% | 1.97% | 32.2% | **32.2%** | Yogyakarta, Indonesia, 2026-08-28 |
| Panoramax | 27 | 0.0% | 0.0% | 0.0% | 0.00084% | Paris, France, 2026-10-01 |

KartaView's n of 6 makes its percentiles a list rather than a distribution: two runs carry all 6,480 of its undated photos, Yogyakarta (5,451) and Krabi (1,029, 1.97% of its present imagery), both collected 2026-08-28.

**Mapillary's undated population is still mostly one metropolitan area, but no longer only one.**
Four Denver-area runs — Denver (66,441), Commerce City (32,059), Lakewood (2,378), Englewood (1,810) — account for 102,688 of the catalog's 104,668 undated Mapillary panos, or **98.1%** (99.96% at the first production pass).
The fifth-largest is a second, unrelated batch: **Jefferson City, Missouri, 2026-09-29, 1,453** — a city collected after the first pass.
The remaining 14 runs contribute 527 panos between them.
Note these are adjacent cities whose frozen grids overlap, and Mapillary is a *census* provider that keeps every image in the bbox, so the likeliest reading is **one contributor's upload batch counted four times** rather than four independent events.
That is a hypothesis from the geography, not something this measurement establishes; confirming it means comparing pano ids across the four snapshots.

**GSV's is concentrated too, just less dramatically**: zero in 2,256 of 2,405 runs, with the pooled figure carried by 149 runs, largely the big metros (Los Angeles's two runs contribute 2,448 and 1,724, New York's 1,280 and 1,223).
The same GSV runs' counts are lower here than at the first pass (Los Angeles 2025-01-03 read 2,670 then), which this measurement does not explain; a candidate is #367's query-radius rule, which moves a GSV row out of `status_no_date` into `status_out_of_radius` when `recompute_run_stats.py` re-derives a run, but whether that pass ran on these runs is not checked here.

So the shape generalizes across every provider with any undated imagery, and it is the transferable lesson here:
**an undated population is a property of an upload batch, not of a provider.**
A pooled per-provider rate describes no run in the distribution and will systematically understate what any single city can hit.

## Will the phantom delta be visible? For most runs never; the tail is bounded, not measured

`coverage_pct_by_length` is published to one decimal, so a shift under 0.05 percentage points rounds away completely.
The relevant denominator is undated panos over **points queried**, not over present panos, because that is the percentage-*point* shift a coverage rate takes:

Every figure in this table is an **upper bound** on that shift, for the reasons the correction gives:

| | n | p50 | p95 | max | worst run (`max_run_pct_of_queried`) |
|---|---|---|---|---|---|
| GSV | 2,429 | 0.0 pp | 0.0010 pp | **≤ 0.320 pp** | East Hollywood, California, 2026-08-09 |
| Mapillary | 1,959 | 0.0 pp | 0.0 pp | **≤ 3.00 pp** | Commerce City, Colorado, 2026-07-31 |
| KartaView | 527 | 0.0 pp | 0.0 pp | **≤ 0.97 pp** | Yogyakarta, Indonesia, 2026-08-28 |
| Panoramax | 32 | 0.0 pp | 0.0 pp | ≤ 0.0005 pp | Paris, France, 2026-10-01 |

For the overwhelming majority of runs the Δ column will read exactly 0.0, and not merely by rounding: a run with no undated imagery cannot shift at all, and only 149 of 2,429 GSV runs, 19 of 1,959 Mapillary runs, 2 of 527 KartaView runs and 1 of 32 Panoramax runs hold any (`runs_with_any_no_date`).
The tail is where the bound stops settling anything.
A GSV city can shift at most a third of a point, which is still visible at one decimal.
A Mapillary city can shift **at most 3.00 points** — the claim that stood here was *"a Denver-metro Mapillary walk can shift 2.7 points"*, which rested on the withdrawn row-count figure and named a run the file did not record; the corrected bound belongs to Commerce City, and it is a ceiling rather than the shift.
Because every bound in the tail is above 0.05 pp, the measurement says a visible shift is *possible* for those few runs, not that it happens; the exact census shift needs the per-point join.
That is the case the dated note in [`../street-coverage.md`](../street-coverage.md) exists for.

An earlier review of the fix put GSV's maximum at 0.095% and concluded the delta was invisible everywhere; production says up to 0.320 pp for GSV (0.329 at the first pass), so the weaker claim is the true one — invisible in the overwhelming majority of runs, not in all of them.
(That paragraph also cited 2.73 pp for Mapillary, the figure withdrawn above.)

## Why the age median is the sharper problem

A phantom coverage delta is a one-time event that a dated note can explain away.
The age median is permanent, and it is biased rather than noisy.

Every one of the 10 violating KartaView sequences in the audit is the same population: `date_added` **2025-11-19**, one Grab bulk ingest.
So KartaView's undated imagery is not a random 9.56% of its photos — **it is disproportionately its newest**, and dropping it from an age median can only drag that median **older**.
A KartaView city could refresh substantially and have its published median age move the wrong way.
The batch shape found here for Mapillary suggests the same hazard applies wherever a batch lands, since a single upload is a single point in time by construction — whether it biases old or young depends on when that batch was captured, which is precisely what an undated batch does not tell you.

This is why `dated_pct_of_covered` had to go into the artifact rather than being left inferable: an age over 100% of an edge's coverage and an age over 3% of it are different measurements, and before #257 nothing recorded which one you were reading.
For most runs of either provider the field will read 100.0, which is the point — the number is only interesting where it is not.

## Replicating

```bash
python scripts/undated_imagery_share_analyze.py --docs-dir docs/experiments --catalog-label makelab2-prod
```

Run it **on makelab2**, against the production catalog.
`runs.total_grid_points` was backfilled there on 2026-10-04 (`scripts/recompute_run_stats.py --only total_grid_points --provider <p> --execute`, one provider per invocation and never with a plain `--execute`; [procedure](../operations.md#schema-v20-and-the-runstotal_grid_points-backfill-issue-289), [#289](https://github.com/jonfroehlich/streetscape-tracker/issues/289)), and a newly cataloged run gets it from `calculate_run_stats`.
Reads `runs.status_ok`/`status_no_date`/`total_grid_points` and `kartaview-shotdate-audit_metrics.json`; no network, no credentials, seconds to run.
A run whose `total_grid_points` is still NULL is left out of every *of queried* figure and counted in `runs_without_grid_points`, so a regeneration before the backfill reports itself as incomplete rather than quietly measuring a subset; it is 0 for every provider in the committed file.
Each provider's block now carries `of_queried_kind`: `upper_bound` for a census provider, for the reason the correction gives, and for any provider with a run holding more rows than grid points (`runs_with_more_rows_than_grid_points`); `exact` only otherwise.
It also names the run behind each per-run maximum (`max_run_pct_of_present`, `max_run_pct_of_queried`), so a writeup naming the worst run traces the name to the file.
`--catalog-label` is recorded in the metrics file and is not cosmetic — it is the only thing distinguishing this result from the dev-catalog run that concluded the opposite.

Re-run once KartaView walks have accumulated real `covered_samples_dated` values, at which point the grid proxy can be retired for the measurement itself.
