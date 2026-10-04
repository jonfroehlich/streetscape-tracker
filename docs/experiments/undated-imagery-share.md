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

> **Correction (2026-10, [#289](https://github.com/jonfroehlich/streetscape-tracker/issues/289)): every Mapillary *of queried* figure in this writeup and its metrics file is wrong by construction and pending regeneration.**
> The script that produced them divided `status_no_date` by `runs.total_points`, and `total_points` is a **row** count.
> A Mapillary run is a census that writes one row per image plus one row per empty grid point, so that denominator was a mixture of images and points, not the grid it was read as.
> The affected numbers are Mapillary's `points_queried`, `pooled_pct_of_queried` and `per_run_pct_of_queried`, i.e. the Mapillary row of the phantom-delta table below and the 2.73 pp claim drawn from it.
> **Unaffected:** every *of present* figure (`no_date / (ok + no_date)` is images over images for a census, points over points for GSV, and never read `total_points`), the run counts, and the batch analysis.
> **GSV's *of queried* figures are believed unaffected, for a narrower reason than the format.**
> An ordinary GSV run writes one row per grid point, so its row count is its grid; but a legacy `is_baseline=1` run that was resumed can repeat rows for a point, and in such a run `total_points` overstated the grid too.
> Such a run's *of queried* figure is still right if it carries no `NO_DATE` row, since zero over either denominator is zero — and on a development catalog, every GSV run with repeated rows carried none (PR #422 review; a dev catalog, not production, so this is the expectation the regeneration checks, not a measurement of production).
> What those runs do move is GSV's pooled `points_queried`, which counted their repeated rows, and with it `pooled_pct_of_queried`, by an amount not measured on production.
> The regenerated metrics show whether production holds any: `runs_with_more_rows_than_grid_points` counts them, and GSV's `of_queried_kind` reads `exact` only if there are none.
> KartaView has no *of queried* figure here at all: its number is the API audit's, which is *of present* only.
>
> The fix is [#289](https://github.com/jonfroehlich/streetscape-tracker/issues/289)'s `runs.total_grid_points` (schema v20), the de-duplicated grid-point count, which the script now divides by.
> It cannot be regenerated from a development catalog, for the reason the next section records, and the production catalog's new column is NULL until `scripts/recompute_run_stats.py --only total_grid_points --provider <p> --execute`, run one provider at a time, backfills it there ([procedure](../operations.md#schema-v20-and-the-runstotal_grid_points-backfill-issue-289)).
> So no corrected number is quoted here, and none is estimated.
> The metrics file is left byte-for-byte as the pre-#289 script (as of `d974fd4`) wrote it, as the record of that pass, until it is regenerated on makelab2 after the backfill.
>
> Two things are known about the corrected figure without running it.
> Per run, the grid is never larger than the row count, so the new ratio is **at least** the old one: provided the backfill moves no run's `status_no_date`, the corrected per-run maximum cannot come back below 2.73.
> (The metrics file does not record which run that maximum belongs to, so this writeup does not name it; the regenerated file will, in `max_run_pct_of_queried`.)
> And it is an **upper bound** on the phantom shift rather than the shift itself, because its numerator still counts undated *images*, several of which can share a point, at points that may also hold a dated image; the exact census shift needs the per-point join, which only the CSV holds.
> So the corrected Mapillary figure can only settle the visibility question in one direction: a bound under 0.05 pp means invisible, and a bound above it settles nothing.

## Read the production catalog, not a dev one — the answer inverts

Recording this first because it nearly shipped as a finding.
The first pass of this measurement ran against a development laptop's catalog, which holds the full 1,144-city GSV baseline but only **three** Mapillary runs.
It concluded that Mapillary emits no undated imagery at all and that *"small but real for Mapillary is not supported by anything we hold."*

Production holds 1,201 Mapillary runs and says the opposite: **0.150%**, roughly **17× GSV's rate**.
The original claim was right and the dev-catalog refutation was an artifact of n=3.
A per-provider question cannot be answered from a catalog that is only well-populated for one provider, and the run counts have to be quoted next to the share for exactly that reason.

## Three sampling frames, never pooled

| | frame | pooled undated | runs with **any** | worst single run |
|---|---|---|---|---|
| **GSV** | 1,766 prod grid runs, 242.5M present panos | 0.0086% | 119 of 1,749 | **0.336%** |
| **Mapillary** | 1,201 prod grid runs, 68.3M present panos | 0.150% | **11 of 453** | **23.3%** |
| **KartaView** | API sample of 48 sequences, 59,263 photos | 9.56% | 10 of 48 sequences | — |

The first two are a census of what we collected; the third is a sample of what the provider serves, lifted from [`kartaview-shotdate-audit_metrics.json`](kartaview-shotdate-audit_metrics.json) rather than re-probed, since that number already has a writeup and caveats of its own ([`kartaview-feasibility.md`](kartaview-feasibility.md)).
They are not a controlled comparison.
We hold no KartaView runs at all — it became a scheduler channel in #248 but an opt-in one, so it collects only enrolled cities — and the audit's own writeup calls its sample Grab-heavy and its count a lower bound.

**And all three are proxies for the quantity actually at issue, which is the ROAD-WALK undated share.**
No walk has ever recorded one: `street_walks` and the coverage artifact counted covered samples and nothing else, which is the gap #257 closed by adding `dated_covered_samples` per edge and `covered_samples_dated`/`dated_pct_of_covered` to the artifact's summary blocks.
Until walks collected under that column accumulate, the grid share is the only estimate available, and it is an estimate of the right *order* rather than of the value
— a walk samples only on-street points, where imagery is denser and plausibly better dated.

## The distribution is the finding: undated imagery comes in batches

Both providers are zero through the 95th percentile, and both have a long right tail.
Per run, as a share of present panos:

| | n | p50 | p90 | p95 | max |
|---|---|---|---|---|---|
| GSV | 1,749 | 0.0% | 0.0% | 0.0046% | **0.336%** |
| Mapillary | 453 | 0.0% | 0.0% | 0.0% | **23.3%** |

**Mapillary's entire undated population is essentially one metropolitan area.**
Four Denver-area runs — Denver (66,441), Commerce City (32,059), Lakewood (2,378), Englewood (1,810) — account for 102,688 of the catalog's 102,733 undated Mapillary panos, or **99.96%**.
The remaining seven runs contribute 45 panos between them.
Note these are adjacent cities whose frozen grids overlap, and Mapillary is a *census* provider that keeps every image in the bbox, so the likeliest reading is **one contributor's upload batch counted four times** rather than four independent events.
That is a hypothesis from the geography, not something this measurement establishes; confirming it means comparing pano ids across the four snapshots.

**GSV's is concentrated too, just less dramatically**: zero in 1,630 of 1,749 runs, with the pooled figure carried by 119 runs, largely the big baseline metros (Los Angeles alone contributes 2,670).

So the shape generalizes across all three providers, and it is the transferable lesson here:
**an undated population is a property of an upload batch, not of a provider.**
A pooled per-provider rate describes no run in the distribution and will systematically understate what any single city can hit.

## Will the phantom delta be visible? For GSV rarely, for Mapillary not yet measured

`coverage_pct_by_length` is published to one decimal, so a shift under 0.05 percentage points rounds away completely.
The relevant denominator is undated panos over **points queried**, not over present panos, because that is the percentage-*point* shift a coverage rate takes:

| | p50 | p95 | max |
|---|---|---|---|
| GSV | 0.0% | 0.0028% | **0.329 pp** |
| Mapillary | ~~0.0%~~ | ~~0.0%~~ | ~~2.73 pp~~ — **wrong by construction, pending regeneration** ([#289](https://github.com/jonfroehlich/streetscape-tracker/issues/289), see the correction above) |

For the overwhelming majority of runs of either provider the Δ column will read exactly 0.0.
But the tail is not negligible: a GSV city can shift a third of a point.
The Mapillary claim that stood here — *"a Denver-metro Mapillary walk can shift 2.7 points"* — rested on the struck-through figure and is withdrawn until it is regenerated ([#289](https://github.com/jonfroehlich/streetscape-tracker/issues/289)).
What survives without it is the *of present* figure: Mapillary's worst run has 23.3% of its present imagery undated, so a visible shift there remains plausible, but how many points it moves is not measured.
(The metrics file does not say which run that is; the top contributors by undated count are the four Denver-area runs above, but a per-run maximum of a ratio need not be the largest numerator, so it is not named here.)
That is the case the dated note in [`../street-coverage.md`](../street-coverage.md) exists for.

An earlier review of the fix put GSV's maximum at 0.095% and concluded the delta was invisible everywhere; the production catalog says 0.329 pp for GSV, so the weaker claim is the true one — invisible in the overwhelming majority of runs, not in all of them.
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

Run it **on makelab2**, against the production catalog, and **only after** `scripts/recompute_run_stats.py --only total_grid_points --provider <p> --execute` has backfilled `runs.total_grid_points` there, one provider per invocation and never with a plain `--execute` ([procedure](../operations.md#schema-v20-and-the-runstotal_grid_points-backfill-issue-289), [#289](https://github.com/jonfroehlich/streetscape-tracker/issues/289)).
Reads `runs.status_ok`/`status_no_date`/`total_grid_points` and `kartaview-shotdate-audit_metrics.json`; no network, no credentials, seconds to run.
A run whose `total_grid_points` is still NULL is left out of every *of queried* figure and counted in `runs_without_grid_points`, so a regeneration before the backfill reports itself as incomplete rather than quietly measuring a subset; that count should be 0 before the numbers above are replaced.
Each provider's block now carries `of_queried_kind`: `upper_bound` for a census provider, for the reason the correction gives, and for any provider with a run holding more rows than grid points (`runs_with_more_rows_than_grid_points`); `exact` only otherwise.
It also names the run behind each per-run maximum (`max_run_pct_of_present`, `max_run_pct_of_queried`), so a writeup naming the worst run traces the name to the file.
`--catalog-label` is recorded in the metrics file and is not cosmetic — it is the only thing distinguishing this result from the dev-catalog run that concluded the opposite.

Re-run once KartaView walks have accumulated real `covered_samples_dated` values, at which point the grid proxy can be retired for the measurement itself.
