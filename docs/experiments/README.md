# How experiments are recorded

The rules for `docs/experiments/`. Read before running a measurement, and before writing up one you
have already run.

Split out of `CLAUDE.md` (2026-08-22); the router keeps this topic's short rules and points here for the evidence and detail.
An edit that changes a rule belongs in both files; anything written since the split is under its own heading and says so.

## Every measured question gets a writeup, however small

- **Every measured question gets a writeup in `docs/experiments/`, however small** — including negative, inconclusive and abandoned ones, and including a one-afternoon analysis over data already on disk.
  "Too small to write up" is not a category: the cost of a short writeup is minutes, and the thing it prevents is re-running a collection to re-learn an answer we already bought.
  Each writeup carries the decision it justifies, the caveats, and how to replicate it.

So far, alphabetically — keep that order, so two branches adding a writeup usually insert at different points and merge cleanly:

### `capture-date-precision.md`

Issue #226 — what capture-date formats are actually on disk, and what the loader's strict `'%Y-%m-%d'` cost.
Read entirely out of run CSVs already on disk — the same shape as `publish-duration.md` above, no network and no credentials — and the same verdict: worth the twenty minutes.
Three things generalize.
**(1) A reader's permissiveness bounds every statistic downstream of it, and a repair tool that reads through the same reader inherits the blind spot**
— `recompute_run_stats.py` processed all 8 affected *runs* in the whole-series #213 pass and left every one NULL.
**(2) The one-sided-change tell**: recompute the series under the old reader and the new one and compare DIRECTIONS, not magnitudes
— a pass that can only ever clear a value and never restore one is describing its own blind spot, and that is a check any definition change can run for free.
**(3) The sweep's denominator is the corpus; the repair's is the catalog, and they differ** — 9 files on disk carry month precision against 8 registered runs, the extra being an unregistered orphan (`saskatoon_sk_…`, superseded by the registered `saskatoon--sk_…` that carries day precision), which no catalog audit and no repair script can see.
Also worth knowing before trusting a format census: 77% of all `capture_date` cells in the corpus are ABSENT (ZERO_RESULTS fill), so a share taken over rows rather than over dated rows describes the grid, not the imagery.

### `carto-basemap-key.md`

Issue #284 — how CARTO enforces its basemap key, and what leaving raster for vector would cost.
Both answers are counter-intuitive enough to be worth the minutes.
**CARTO enforces by watermarking the tile, not by refusing the request**: a keyless request, a well-formed wrong key, and the right key under the wrong parameter name all return HTTP 200 with a byte-identical PNG stamped "API KEY REQUIRED", so no client-side error handling can detect it and the break presents as a styling bug.
That is why the detection path is differential (keyed bytes vs keyless bytes) rather than a pinned hash, and why the key had to be verified against our own URL form rather than the one CARTO documents.
**The domain CARTO collects at issue time is not enforced** — issued origin, foreign origin and no `Referer` return identical bytes — so the key is bearer-style, which separates unavoidable *exposure* from mitigable *abuse* and makes "same as any Mapbox client key" the wrong analogy.
**Vector is not a way off the key** (same key, same 5M/month ceiling), so it is purely a rendering-stack decision: maplibre-gl is 6.19x Leaflet's gzipped weight, plus a WebGL flake class and no basemap at all where WebGL is unavailable. Decision: stay on raster.

### `grid-density.md`

Issue #106 — why production stays on the 20 m grid; below ~10 m you buy redundancy, not information, since official panos sit ~10 m apart.

### `kartaview-feasibility.md`

Issue #225 — whether KartaView can be a third provider; it can, as a **census** like Mapillary rather than a GSV-style sample, since its only bulk path is a paginated radius sweep.
Two traps generalize beyond it: `/1.0/list/nearby-photos/` fills a page by **sequence**, so any share over a truncated page describes one drive rather than one neighbourhood
— and the page size is a *client* flag, `--ipp`, whose 200 default (against a 2,000 server cap) was what made the first pass conclude that most points could never be measured at all.
And KartaView's v2 serves **the upload timestamp as the capture date** for one 2025 Grab ingest, so `shotDate >= dateAdded` must be rejected outright: a null is honest, a plausible wrong date is not, which is #213's lesson arriving from a second vendor.

### `kartaview-sweep-cost.md`

Issue #225, follow-on to the feasibility study — what a KartaView census of a frozen grid actually costs, which was the one number gating a scheduled channel.
The **median catalog city is 12 circles and 16 requests**, the same order as Mapillary's median 12 tiles rather than the "two to three orders of magnitude more" the feasibility study guessed;
p95 is 636 and Singapore ~9,974, so it is the tail that decides affordability, not the median.
Quote `sweep_requests_observed` and not `sweep_requests_estimate`: the latter is the **geometric floor**, with retries and the per-city calibration ladder unpriced, and it undercounts by 1.54×.

### `kartaview-viewer-deeplink.md`

Issue #312 — why every KartaView pano link on the site opens an error page, and why the fix was a second link rather than a corrected URL.
The deep-link format is **theirs, not our guess**: their own SPA writes `details/{sequence_id}/{sequence_index}` into the address bar.
What fails is the one v1 call that page depends on (`POST /details` → `osv: null`), for **every sequence probed including KartaView's own documented example** — which is the control that makes this a statement about their service rather than about our data, and the reason nobody should "fix" the URL builder.
Generalizes past this provider: when a third-party page breaks on a link we build, probe **their** canonical example through the identical call before touching our code, and prefer a fallback keyed on geometry — the map link covers rows the photo link never could, since it needs only a position.

### `pano-spacing.md`

GSV vs Mapillary capture interval — Mapillary samples 1.4–3.5× finer, and **any per-pano analysis must group by `sequence_id` first**: pooling across contributors collapses the measured interval by 2–8× because the nearest image is usually someone else's drive.
GSV publishes no drive identifier, so the same correction is impossible there — which makes Mapillary's number the better-founded one, the opposite of the intuition.

### `publish-duration.md`

Issues #218/#230 — how long the nightly publish actually takes, so `scheduler.PUBLISH_TIMEOUT_S` is sized rather than guessed;
read entirely out of logs already on disk, which is the smallest an experiment gets and still worth the twenty minutes.
Its transferable lesson is that a log holds **three** populations that look alike
— the exact `Published in N.N s` line, the pre-#229 `Publishing via` → next-line *upper bound*, and failures, whose 0.05–0.30 s values are not publish durations at all and halve p25 if pooled
— plus a fourth thing that is not an observation: a healthy pre-#229 night logged nothing after `Publishing via`, so most nights are simply unmeasurable and are reported as an excluded count rather than dropped.

### `undated-imagery-share.md`

Issue #257 — how much imagery carries no usable capture date, per provider, which is the number three prose claims in the road-walk fix rested on.
**Undated imagery arrives in batches, not as diffuse noise**, so the per-run maximum is the number a decision has to survive and the pooled share describes no run in the distribution: GSV pools to 0.0086% but its worst run is 0.34%, Mapillary pools to 0.150% but its worst run is **23.3%**, and KartaView is 9.56% of audited photos concentrated in one 2025-11-19 ingest.
Three things generalize.
**(1) An undated population is a property of an upload BATCH, not of a provider** — Mapillary's entire undated corpus is 99.96% four adjacent Denver-metro runs, and KartaView's is one 2025-11-19 Grab ingest, so a pooled per-provider rate systematically understates what any single city can hit.
**(2) A batch is a single point in time by construction**, which makes the bias directional rather than merely noisy: KartaView's undated imagery is its *newest*, so excluding it from an age median drags the median **older** instead of widening its error bar.
**(3) A per-provider question cannot be answered from a catalog that is well-populated for only one provider** — and this one nearly shipped wrong because of it.
The first pass ran against a dev laptop holding 1,144 GSV cities but **three** Mapillary runs, and concluded Mapillary emits no undated imagery at all; production's 1,201 runs say 0.150%, about 17× GSV's rate.
Hence `--catalog-label`, recorded in the metrics file: it is the only thing separating that result from its opposite.
The catalog half is a census and the KartaView half is an API sample read from `kartaview-shotdate-audit_metrics.json`; the two frames are reported side by side and never pooled.
Both are proxies for the road-walk share, which no walk recorded until #257 added `dated_covered_samples`.

The generating code is `scripts/{topic}_{collect,analyze,common}.py` (kept so the result can be reproduced, not because it runs routinely) and its test pins the sampling invariant.
What is genuinely identical across studies lives in one place instead: `scripts/experiment_style.py` for the figure palette, `scripts/experiment_stats.py` for the percentile/`describe` summary every writeup quotes.
That second one exists because the same linear-interpolation percentile had been written three times, and two writeups quoting p90s computed by two implementations are comparing numbers nobody checked agree.
Note the trap it left behind: `pos = q * (n - 1)` is grouped the way it is on purpose, because the algebraically equal `(n - 1) * q` differs by an ULP on some samples and the study records are pinned byte-for-byte.
Answer "should we sample finer / differently?" questions from these before re-running anything.

## The derived numbers are committed; only the bulk raw data is gitignored

- **The derived numbers are committed; only the bulk raw data is gitignored.** This split is the whole point and it was got wrong once.
  `docs/experiments/{topic}_metrics.json` (plus any small summary CSV and the figures) is **committed** beside the writeup;
  the raw collection CSVs land in the gitignored `/experiments/{topic}/` — **never** under `data/`, which the publisher rsyncs to the public web server.
  The failure mode this exists to prevent is real: #106's writeup quoted a single p50 while the full spacing and offset distributions lived only in one gitignored directory on one laptop, so the recoverable record of a 348k-query experiment was one sentence.
  A writeup must **quote the distribution it summarizes** — percentiles and n, not just a headline number
  — because the shape is usually the finding (there, the share of spacings inside a ±1 m band around 10 m — 92.5% / 79.5% / 57.2% across the three areas
  — is what shows GSV's interval is machine-regulated *and* that Seattle has a second mode; the median, 9.5–10.1 m everywhere, shows neither, and a percentile spread such as the IQR hides a bimodal shape just as well).

## Numbers cited in a writeup must be traceable to committed code

- **Numbers cited in a writeup must be traceable to that committed JSON, and that JSON must be produced by committed code**
  — its `generated_by` names a command the repo can actually run (`grid-density_metrics.json` is written only by `grid_density_analyze.py --area all --docs-dir docs/experiments`, and a test regenerates the summary CSV from it and recomputes every quoted share from its histograms);
  a merge or recomputation that lives in a chat transcript is the same single-copy failure one layer up.
  And a number that contradicts vendor documentation gets flagged in the writeup rather than quietly normalized to what the docs say
  — #106's query→pano offsets run past Google's documented 50 m default radius (p99 146 m in Adrian) with no explanation, which is a finding, not an error to round away.
  Same posture as the top-of-file rule on undocumented provider behavior: what we measured outranks what is published, and the disagreement is recorded.
