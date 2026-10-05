# Finding fresh 360° Mapillary towns outside the catalog (#383)

Measured 2026-10-02 from a laptop, for issue #383.
The question: **where in North America is there recent, systematic 360° Mapillary capture that a Project Sidewalk test deployment could use — whether or not we already track the place?**
The reference is Laurens, IA: its Mapillary road walk measured **91.6% of street-km covered by 360° imagery at a 0.81-year median age**, from one uploader's single sweep.
Road walks can only evaluate cities already in the catalog, and by 2026-10-01 the catalog had no uncollected Mapillary pool left, so finding new places is the constraint, not walking them.

Every number below traces to [`mapillary-discovery-screen_metrics.json`](mapillary-discovery-screen_metrics.json), produced by committed code named in its `generated_by`.
Ranked candidates are in [`mapillary-discovery-screen_candidates.csv`](mapillary-discovery-screen_candidates.csv), the per-city validation rows in [`mapillary-discovery-screen_validation.csv`](mapillary-discovery-screen_validation.csv), and the first registration tranche in [`mapillary_discovery_cities.csv`](../../mapillary_discovery_cities.csv).

## Decision

- **The coarsest sequence-layer zoom (z6) is a sufficient discovery instrument.** 97 tiles cover the contiguous US, southern Canada, Hawaii and two Alaskan regions, and the score they produce predicts measured street coverage (below).
- **Score places, not clusters.** Each GeoNames place gets the recent-360° sequence length within 2 km of its point, per km² of that disc.
- **25 uncatalogued towns were registered on production the same day, and enabled that afternoon** (see "What was done on production").
- The screen is worth building as a standing `screen-provider mapillary` subcommand (#383's second acceptance item); that design is a separate plan.

## The instrument

Mapillary's coverage vector tiles, `https://tiles.mapillary.com/maps/vtp/mly1_computed_public/2/{z}/{x}/{y}`, layer `sequence`.
The [API documentation](https://www.mapillary.com/developer/api-documentation) lists the layer at z6–14 as one LineString per capture sequence.
Measured properties on every feature: `id`, `captured_at` (epoch ms), `creator_id`, `is_pano`, `foot`, `image_id`, and `quality_score` on nearly all; `organization_id` on **20.8%** of the 3,030,733 unique sequences.
`image_id` is what makes uploader names resolvable: one Graph API call, `GET /{image_id}?fields=creator`, returns `{id, username}`.

A dense z6 tile is 5–10 MB and holds up to ~190,000 sequences.
The scan's wall clock (~25 min for 97 tiles) was protobuf decoding in Python, not the network.

## Requests, pacing and the per-IP question

Per CLAUDE.md, the docs and forum were read first.
The documented tile limit is 50,000/day per app; a staff reply in the [forum thread on that limit's scope](https://forum.mapillary.com/t/50-000-requests-day-rate-limit-scope/10644) adds that a "sudden spike" from one IP can be blocked earlier, which is the per-IP layer behind our four production blocks (see `docs/provider-access.md`).
So the collector runs serially at the production channels' jittered 40/min under the Mapillary tile host lock, caches every tile, and stops at the first response that is not 200/204.

**All 195 requests returned 200**: 106 tile requests (97 z6 tiles for the scan, 8 z10 tiles for calibration and one exploratory z8 tile) and 89 Graph API lookups 3 s apart.
They came from a laptop, never from makelab2, so none of it counts against production's per-IP sum.
**The committed collector did not make them.**
Exploratory scripts that preceded it (uncommitted, in the gitignored raw directory) made every request, with the same jittered 40/min pacing and the same stop-at-first-refusal rule but without the host lock; their log entries are the ones without a `host` key.
The committed `collect scan` and `collect calibrate` were then run over the cached tiles and made zero requests (`scan_manifest.json`: 0 requests, 97 cache hits), which is also how the committed numbers are regenerated.
The committed fetcher's stop rules — a 302, a non-200, or an HTML or JSON body stops the run, is logged and is never cached, and `--max-requests` is never exceeded — are pinned by fake-session tests rather than by this run.

## Zoom calibration: does z6 keep a one-town sweep?

Coarse zooms are simplified, and the risk was that a small town's sequences would be thinned away.
For each of eight catalog towns, the sequences touching a 3 km disc around its centre were counted at z6 and z10 (`calibration_zoom_recall`):

| Town | Sequences z6 / z10 | 360° z6 / z10 | Newest 360° (both zooms) |
|---|---|---|---|
| Grand Marais, MN | 21 / 14 | 15 / 10 | 2026-07-05 |
| Laurens, IA | 20 / 20 | 20 / 20 | 2025-11-04 |
| Meridian, ID | 943 / 917 | 239 / 231 | 2026-09-07 / 2026-08-25 |
| Richmond, VA | 144 / 203 | 128 / 138 | 2026-08-06 |
| Trotwood, OH | 117 / 126 | 117 / 126 | 2026-03-05 |
| Wasta, SD | 24 / 24 | 18 / 18 | 2021-09-15 |
| Waterbury, CT | 1,261 / 1,403 | 421 / 456 | 2026-08-31 |
| Waterville, ME | 165 / 174 | 51 / 56 | 2026-06-18 |

360° recall at z6 is 91–100% of z10 in six towns, and above 100% in Grand Marais and Meridian, where simplified z6 geometry reaches into the disc from slightly outside it.
The top recent uploader agrees at both zooms everywhere except Grand Marais, which has two uploaders close in size.
Wasta is the useful control: z6 reports its 360° imagery as entirely pre-2022, and its walk measured a 6.49-year median age.

## Clusters were tried first, and failed

The first analysis *(exploratory, uncommitted: its numbers below trace to no committed JSON)* grouped ~1 km cells holding recent 360° sequence length into connected clusters.
It recovered every reference town, but **one statewide sweep merged most of Connecticut into a single 10,687 km cluster** (uploader `ctroadway360`), swallowing Waterbury.
Any sweep that runs road-to-road between towns does this, so clusters cannot rank towns.
The committed analysis scores each place from its own point instead.

## The score, and its validation against measured walks

For each place, recent-360° sequence length (captured on or after 2024-10-02, two years before the scan) within **2 km** of its point, divided by the disc's 12.57 km².
Sequences are cut into pieces no longer than 0.5 km so length lands where it was driven.

The validation population is every production catalog city with a Mapillary drive walk whose centre lies inside a scanned z6 tile: **n = 1,111**.
Membership is decided by the tiles, not by a bounding box: ten walked cities inside the box sit in no scanned tile (Kodiak, Mexico City and Punta Cana among them), and kept, they would enter the validation as a score of 0 that nothing measured.
The score is taken at the catalog centre while the walk covers the whole frozen grid, so this understates how well the score works for a town-sized disc.
"Good" means walk coverage ≥ 50% of street-km **and** a median covered age ≤ 2 years.

**Spearman ρ between score and walk coverage: 0.666.**

| Score (km/km²) | Walked cities | Median walk coverage | Good (≥ 50%, ≤ 2 yr) |
|---|---|---|---|
| [0, 0.5) | 964 | 0.0% | 2 (0.2%) |
| [0.5, 1.5) | 71 | 17.0% | 0 |
| [1.5, 3) | 36 | 25.4% | 1 (2.8%) |
| [3, 6) | 19 | 45.3% | 4 (21.1%) |
| [6, ∞) | 21 | 58.3% | 7 (33.3%) |

At the candidate threshold of 3 km/km², **40 cities hold 11 of the 14 good ones** (79% recall).
The base rate over all 1,111 is 1.26%, so the threshold concentrates good cities about 22-fold (27.5% vs 1.26%).

Reference towns, score vs walk:

| City | Score | Walk coverage | Median age (yr) |
|---|---|---|---|
| Meridian, ID | 8.05 | 51.0% | 0.11 |
| Honolulu, HI | 6.71 | 69.7% | 1.00 |
| Trotwood, OH | 4.73 | 68.5% | 0.54 |
| Waterbury, CT | 4.11 | 36.3% | 5.36 |
| Laurens, IA | 3.75 | 91.6% | 0.81 |
| Waterville, ME | 1.53 | 30.3% | 0.30 |
| Grand Marais, MN | 1.24 | 45.7% | 0.57 |
| Juneau, AK | 0.54 | 84.2% | 2.36 |
| Richmond, VA | 0.12 | 36.3% | 2.05 |
| Wasta, SD | 0.00 | 56.3% | 6.49 |

Two failure shapes are visible.
**Small towns under-score**: Grand Marais's streets fill much less than the 2 km disc, so its sweep is divided by area it does not have; Laurens is the same shape, and only barely clears 3.
**A city whose centre is quiet under-scores**: Richmond's and Juneau's recent sweeps are away from the catalog centre.
Waterbury is the opposite case: a dense recent sweep (score 4.11) over a walk whose covered imagery is mostly old, because the old imagery still covers more streets.
The score is for ranking what to walk; the walk is the measurement.

## Uploaders are the unit that generalizes

Recent 360° capture in this region comes from **477 uploaders**, and the dense, town-scale sweeps come from a handful.
Top uploaders by recent 360° length (`top_creators_by_recent_pano_km`), with the number of candidate places each dominates:

| Uploader | Recent 360° km | Candidate places | Newest |
|---|---|---|---|
| `marker_geo1` | 40,535 | 31 | 2026-09-16 |
| `rking` | 17,478 | 30 | 2026-08-13 |
| `codgis` | 15,108 | 1 | 2026-09-25 |
| `quickness805` | 13,789 | 1 | 2026-07-05 |
| `ctroadway360` | 9,717 | 5 | 2026-09-29 |
| `mapillary01730` | 9,276 | 7 | 2026-09-24 |
| `Lanka6359` | 7,112 | 8 | 2026-08-10 |
| `hmhtb` | 6,681 | 11 | 2026-06-20 |

`marker_geo1` is Meridian's uploader and `rking` is Trotwood's; between them they dominate 61 of the 161 candidates.
`codgis` (Detroit's city capture, per #406's research) and `quickness805` are large in length but dominate one candidate each, so length alone does not find towns.
Laurens' uploader `GIS_ISG` has a sibling account, `UAS_ISG`; three of their towns are candidates — Fergus Falls, MN (5.38, 2026-04), Delavan Lake, WI (4.08, 2026-08) and Como, WI (3.94, 2025-12) — and none reached the first tranche, which is ranked by score.
The practical consequence: **once one town from an uploader walks well, that uploader's other towns are the next candidates**, which `scripts/mapillary_user_activity.py` (see `mapillary-user-activity.md`) can enumerate exactly.

## Candidates

2,713 GeoNames places (cities500, ≥ 500 people) hold at least 5 km of recent 360° length within 2 km; their score distribution is p10 0.51, p25 0.67, p50 1.23, p75 2.83, p90 6.51 km/km².
A candidate (`candidate_rules`) scores ≥ 3, has a median capture date on or after 2025-01-01, is ≥ 60% one uploader, is more than 10 km from every catalog centre, and is **not inside any catalog city's frozen grid rectangle** — places within 5 km of a stronger candidate are dropped.
That leaves **161**.

The grid-membership test matters: distance to a centre is not membership, since a frozen grid can be 40 km across.
Without it, the thinned list holds 193 places, **37** of them inside a catalog grid, e.g. Lower Pearl City inside Honolulu's grid and Dedham inside a neighbour's (`grid_rule_effect`).
Removing those 37 readmits 5 places that thinning had dropped in their favour, so the list shrinks by only 32 (193 − 37 + 5 = 161); 32 is the net change, not the count removed.
A readmitted place can in turn thin away one of the 156 survivors, so `grid_rule_effect` counts both by place id rather than by subtraction; here none was (`cascade_dropped_once_those_are_removed`: 0), so 5 is exact.
Those places are tracked on the grid, though not walked as towns of their own.

## What was done on production

The first tranche (`tranche_rules`: score ≥ 3.5, median capture ≥ 2025-03-01, ≥ 75% one uploader, ≤ 60,000 people, < 50% on foot, ≥ 8 km apart, at most three towns per uploader, top 25) was written as `mapillary_discovery_cities.csv` in `register_frame.py`'s manifest format.

**Each grid was computed before registering**, with the same four calls `register_frame_city` makes, and the offset to the GeoNames point read.
Two needed fixes, both now recorded in the analysis code:

- **Fond du Lac, WI matched Fond du Lac County** (58.5 × 44.0 km, clamped to 40 × 40 km = 4,000,000 GSV points, 7.7 km off).
  The override `Fond du Lac, Fond du Lac County, Wisconsin, United States` gives 10.6 × 10.0 km, 1.3 km off.
- **`Waipi'o Acres` does not geocode** with its apostrophe; apostrophes and ʻokina are dropped from manifest names (they would also reach `city_id`s and filenames).

The other 23 geocoded 0.0–4.6 km from their GeoNames point.
On 2026-10-02 the tranche was registered on makelab2 (`--overlap-km 5 --notes-label "mapillary discovery screen 2026-10-02"`): **25 new, 0 reused, 0 failed**, all **disabled**, every grid identical to the pre-computation to the metre.
Together they are **3,959,917 GSV grid points** (1,588 km²; largest Apex, NC at 474,512), summed from the registered dimensions in the registration log on makelab2 (`logs/register_mapillary_discovery_2026-10-02.log`).
**All 25 were enabled on 2026-10-02 at 15:20 PDT** with `scheduler enable-city`, 0 failures, which put them on every default channel from that night and enrolled them on the opt-in channels whose screen found imagery: both KartaView channels for all 25, both Panoramax channels for 12.
That is recorded from the project's operating notes for that day, not from a query of the production catalog made for this writeup.

## Caveats

- **Screening signal, never coverage.** z6 geometry is simplified, so lengths are approximate, and length says nothing about which streets were driven. A walk is the only measurement.
- **The validation is not a random sample.** The catalog over-represents places already suspected of good Mapillary coverage (Project Sidewalk cities, two earlier 360° searches), which flatters the precision figures and understates the base rate.
- **The 2 km disc fits a town, not a city.** Large places are scored on their core only, small ones are penalized for area they do not have.
- **"Recent" is a fixed cutoff** (2024-10-02); a re-run later must move it, or old sweeps age into the score.
- **cities500 was downloaded on the day**, not vendored; `data_sources/` holds only cities15000, which omits towns like Laurens.
- **The validation's walk table is a production snapshot**, label `makelab2-prod-f1885f5`, exported read-only.
- **Uploader identities are what the Graph API returns** for one sample image each; nothing here establishes who an account belongs to.

## Replicate

```bash
# raw outputs go to gitignored experiments/, never data/
curl -sSO https://download.geonames.org/export/dump/cities500.zip && unzip cities500.zip -d experiments/mapillary-discovery-383
python scripts/mapillary_discovery_collect.py scan --out-dir experiments/mapillary-discovery-383
python scripts/mapillary_discovery_collect.py calibrate --out-dir experiments/mapillary-discovery-383 --zooms 6,10 \
    --town laurens--iowa=42.8468,-94.8515 --town grand-marais--minnesota=47.7588,-90.3401 \
    --town waterville--maine=44.5433,-69.6628 --town trotwood--ohio=39.7972,-84.3113 \
    --town meridian--idaho=43.6085,-116.3923 --town waterbury--connecticut=41.5538,-73.044 \
    --town wasta--south-dakota=44.0694,-102.4459 --town richmond--virginia=37.5248,-77.4933
# read-only catalog export, run on production (every city's centre and grid, and its latest Mapillary drive walk)
python scripts/mapillary_discovery_collect.py catalog-snapshot --db data/streetscape_tracker.db --out prod_snapshot.csv
#   copied to experiments/mapillary-discovery-383/prod/prod_snapshot.csv
#   (the 2026-10-02 export ran these same two queries by hand, before the subcommand existed)
python scripts/mapillary_discovery_analyze.py ...   # exact arguments: metrics _about.generated_by
python scripts/mapillary_discovery_collect.py resolve-creators --out-dir experiments/mapillary-discovery-383 \
    --ids-from docs/experiments/mapillary-discovery-screen_candidates.csv:top_creator:200
python scripts/mapillary_discovery_analyze.py ...   # again, to attach usernames
```

`tests/test_mapillary_discovery.py` pins the sampling frame (97 tiles), the y-up tile-coordinate mapping, length conservation through sample splitting, the disc membership and weighted median of the score, grid-rectangle membership, the manifest's name and geocode overrides, every candidate and tranche rule where `apply_rules` applies it, the validation population's tile membership (tile edges, zero-sample cities, walked cities only, and the "good" thresholds), `grid_rule_effect`'s counts under a thinning cascade, the analyzer's refusal of a scan that stopped early and `collect scan` recording that stop, strict JSON with NaN as null, and the fetcher's stop rules and request cap.

Related: #383 (this screen), #406 (the 2026-10-01 web-research pass that preceded it), `mapillary-user-activity.md` (per-uploader enumeration), `mapillary-image-quality.md` (why `quality_score` ranks against on-foot imagery, and so is not used here).
