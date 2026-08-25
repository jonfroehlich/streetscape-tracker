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
We hold no KartaView runs at all (it is not a scheduler channel), and the audit's own writeup calls its sample Grab-heavy and its count a lower bound.

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

## Will the phantom delta be visible? For GSV rarely, for Mapillary yes

`coverage_pct_by_length` is published to one decimal, so a shift under 0.05 percentage points rounds away completely.
The relevant denominator is undated panos over **points queried**, not over present panos, because that is the percentage-*point* shift a coverage rate takes:

| | p50 | p95 | max |
|---|---|---|---|
| GSV | 0.0% | 0.0028% | **0.329 pp** |
| Mapillary | 0.0% | 0.0% | **2.73 pp** |

For the overwhelming majority of runs of either provider the Δ column will read exactly 0.0.
But the tail is not negligible: a GSV city can shift a third of a point, and **a Denver-metro Mapillary walk can shift 2.7 points** — which is not merely visible, it is larger than most real run-to-run coverage changes and would read as a substantial imagery refresh.
That is the case the dated note in [`../street-coverage.md`](../street-coverage.md) exists for.

An earlier review of the fix put GSV's maximum at 0.095% and concluded the delta was invisible everywhere; the production catalog says 0.329 pp for GSV and 2.73 pp for Mapillary, so the weaker claim is the true one — invisible in the overwhelming majority of runs, not in all of them.

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
Reads `runs.status_ok`/`status_no_date`/`total_points` and `kartaview-shotdate-audit_metrics.json`; no network, no credentials, seconds to run.
`--catalog-label` is recorded in the metrics file and is not cosmetic — it is the only thing distinguishing this result from the dev-catalog run that concluded the opposite.

Re-run once KartaView walks have accumulated real `covered_samples_dated` values, at which point the grid proxy can be retired for the measurement itself.
