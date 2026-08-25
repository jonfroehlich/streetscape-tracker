# How much imagery carries no usable capture date? (issue #257)

**Question.** Issue #257 made a `NO_DATE` pano count as road-walk coverage, matching the grid.
The change was justified in three prose claims — that undated imagery is *"large by construction for KartaView, small but real for Mapillary, and empty in practice for GSV"* — and none of them was a number,
even though `docs/experiments/` and the catalog held both of the numbers involved.

Two decisions rest on them.
**(1)** The fix produces a one-time phantom positive coverage delta on every pre-existing walk series, published in `streets.html`'s Δ column;
whether that is invisible noise or the largest change a city's series has ever recorded is a matter of how big the undated population is.
**(2)** Since the fix, `covered` and `dated` are different populations, so `median_covered_age_years` is taken over a subset — and how much of a subset decides whether it can still be read as "the age of the imagery."

**Answer.** Three orders of magnitude apart, so both decisions have different answers per provider.
GSV's undated share is **0.0101%** of present panos, and for **1,070 of 1,146** runs it is exactly zero.
KartaView's is **9.56%** of audited photos — and, unlike GSV's, it is not scattered noise but one dated bulk ingest, which makes it the provider's *newest* imagery.

Numbers below come from [`undated-imagery-share_metrics.json`](undated-imagery-share_metrics.json), written by `scripts/undated_imagery_share_analyze.py`.
Read entirely out of the catalog and an already-committed metrics file: no network, no credentials, no collection.

## Two sampling frames, never pooled

| | frame | undated share |
|---|---|---|
| **GSV** | census of 1,157 catalog grid runs, 151.9M present panos | **0.0101%** of present |
| **Mapillary** | census of 3 catalog grid runs, 15.3M present panos | **0.0%** of present |
| **KartaView** | API sample of 48 sequences, 59,263 photos | **9.56%** of present |

The first two are a census of what we collected; the third is a sample of what the provider serves, lifted from [`kartaview-shotdate-audit_metrics.json`](kartaview-shotdate-audit_metrics.json) rather than re-probed, since that number already has a writeup and caveats of its own ([`kartaview-feasibility.md`](kartaview-feasibility.md)).
They are not a controlled comparison and the gap between them is an order-of-magnitude claim, not a measured ratio.
Two reasons that matters: we hold **no KartaView runs at all** (it is not a scheduler channel), and the audit's own writeup calls its sample Grab-heavy and its count a lower bound.

**And all three are proxies for the quantity actually at issue, which is the ROAD-WALK undated share.**
No walk has ever recorded one: `street_walks` and the coverage artifact counted covered samples and nothing else, which is the gap #257 closed by adding `dated_covered_samples` per edge and `covered_samples_dated`/`dated_pct_of_covered` to the artifact's summary blocks.
Until walks collected under that column accumulate, the grid share is the only estimate available, and it is an estimate of the right *order* rather than of the value
— a walk samples only on-street points, where imagery is denser and plausibly better dated.

## The distribution is the finding: GSV's undated imagery is not spread thin, it is absent

Pooling GSV to 0.0101% understates how concentrated it is.
Per run, as a share of present panos (n = 1,146 runs with any present imagery):

| min | p25 | p50 | p75 | p90 | p95 | max |
|---|---|---|---|---|---|---|
| 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0027% | 0.323% |

**1,070 of 1,146 runs carry not one undated pano.** The share is zero through the 90th percentile, and the pooled figure is carried by 76 runs — largely the big baseline metros, where Los Angeles alone contributes 2,670 of the corpus's 15,355.
So "empty in practice for GSV" was directionally right, and is better stated as *empty in 93% of runs, and never above a third of a percent.*

**Mapillary's "small but real" is the claim that does not survive.**
Across 15.3M present panos — 15.3M of them Detroit's single census — **zero** rows are `NO_DATE`.
The mechanism is real and is in the code (`download_mapillary` maps a bogus `captured_at_ms` to `NO_DATE`), but its observed rate in our own data is 0.
That is a claim about three runs and should not be read as a provider constant; it does mean nobody should expect a visible Mapillary phantom delta from this fix.

## Will the phantom delta be visible? Only at the extreme

`coverage_pct_by_length` is published to one decimal, so a shift under 0.05 percentage points rounds away completely.
The relevant denominator here is undated panos over **points queried**, not over present panos, because that is the percentage-*point* shift a coverage rate takes:

| | p50 | p95 | max |
|---|---|---|---|
| GSV, undated as % of queried points | 0.0% | 0.0015% | **0.219%** |

So for at least 95% of GSV runs the delta is three orders of magnitude below the published precision and the Δ column will read exactly 0.0.
**At the maximum it is 0.22 points, which does render** — a "+0.2" that is the fix landing, not Google driving.
The review that prompted this measurement put the GSV maximum at 0.095% and concluded the delta was invisible everywhere;
the catalog says 0.219%, so the weaker claim is the true one: invisible in the overwhelming majority of runs, not in all of them.

## Why the age median is the sharper problem, and only for KartaView

A phantom coverage delta is a one-time event that a dated note can explain away.
The age median is permanent, and it is biased rather than noisy.

Every one of the 10 violating KartaView sequences in the audit is the same population: `date_added` **2025-11-19**, one Grab bulk ingest.
So KartaView's undated imagery is not a random 9.56% of its photos — **it is disproportionately its newest**, and dropping it from an age median can only drag that median **older**.
A KartaView city could refresh substantially and have its published median age move the wrong way.

This is exactly why `dated_pct_of_covered` had to go into the artifact rather than being left inferable: an age over 100% of an edge's coverage and an age over 3% of it are different measurements, and before #257 nothing recorded which one you were reading.
For GSV the same field will read 100.0 or a hair under, which is the point — the number is only interesting where it is not.

## Replicating

```bash
python scripts/undated_imagery_share_analyze.py --docs-dir docs/experiments
```

Reads `runs.status_ok`/`status_no_date`/`total_points` from the local catalog and `kartaview-shotdate-audit_metrics.json` from `docs/experiments/`.
No network, no credentials, seconds to run.
Re-run it on the production catalog when a Mapillary series longer than three runs exists, and again once KartaView walks have accumulated real `covered_samples_dated` values — at which point the proxy can be retired for the measurement itself.
