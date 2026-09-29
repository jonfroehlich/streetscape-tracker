# GSV query radius: how far from its grid point is the pano Google returns?

**Ran:** first measured in [#367](https://github.com/jonfroehlich/streetscape-tracker/issues/367) on 60 of 1,171 archived GSV city files (seed 0); re-measured 2026-09-29 by `scripts/gsv_query_radius_audit.py --sample 60 --seed 0` on 60 of the 1,157 cataloged GSV runs in a local `data/` copy, no API calls ·
**Verdict:** Google's metadata `radius` is a hint, not a bound.
10.4% (issue) and 11.2% (re-measure) of rows with a pano sit more than 50 m from their query point, so a GSV pano now counts for its grid point only within `analysis.GSV_QUERY_RADIUS_M` (50 m).

**Provenance.** The re-measure's numbers trace to [`gsv-query-radius_metrics.json`](gsv-query-radius_metrics.json), `generated_by` `scripts/gsv_query_radius_audit.py --sample 60 --seed 0`.
The issue's numbers are the original measurement, kept beside them; its sampling script lived in the issue, not the repo, so they trace to nothing committed.
The two samples are different files: same size and seed, but the issue sampled a 1,171-file corpus and the script samples the catalog's 1,157 runs, so seed 0 draws a different 60.
The local `data/` is a laptop copy, not production's catalog, so neither is a statement about production's current series.

## The question

`download_gsv.py` queries the Street View metadata endpoint with no `radius`, so Google applies its documented default of 50 m.
It stores `pano_lat`/`pano_lon`, but nothing ever checked how far the returned pano is from the query point.
Every row with a pano counted as a covered grid point, however far away the pano was.

## Method

A seeded random sample of 60 GSV run files.
For every row with a pano (status OK or NO_DATE, both pano coordinates present), the haversine distance from `(query_lat, query_lon)` to `(pano_lat, pano_lon)` (`geoutils.haversine_m`, the function the rule itself uses).
The script reads each file through the loader with `raw=True`, i.e. as Google answered it, and takes its file list from the catalog's `runs.csv_filename`, never from a glob of `data/`.

## Results

| | issue #367 | re-measure |
|---|---|---|
| files | 60 of 1,171 | 60 of 1,157 |
| rows with a pano | 8,652,929 | 4,619,715 |
| pano > 50 m from its query point | 901,700 (**10.4%**) | 518,660 (**11.2%**) |
| pano > 100 m | 187,035 (2.2%) | 124,515 (2.7%) |
| pano > 1 km | 3,068 (0.04%) | 1,017 (0.02%) |
| files with any row > 50 m | 57 of 60 | 60 of 60 |
| files with any row > 1 km | 21 of 60 | 22 of 60 |
| > 50 m rows with a non-Google `copyright_info` | 26,592 (2.9%) | 6,671 (1.3%) |

The re-measure also has the per-file distribution, which the issue did not quote.
The share of a file's pano rows beyond 50 m is p10 5.9%, p25 9.9%, **p50 12.5%**, p75 16.7% and p90 19.9% (n = 60, min 1.5%, max 27.4%).
So the pooled figure is close to the typical file, not a few bad cities averaged in.
Either way the bulk of the overshoot is Google's own panos at 50–100 m, and the continental-scale outliers are mostly user photospheres.

Worst cases, issue #367 sample:

| city | worst distance | what came back |
|---|---|---|
| Anchorage, AK | 11,185 km | pano at (9.50, 76.34), Kerala, India; `© D R` |
| Holyoke, MA | 8,586 km | `pano_lat == pano_lon` (42.2176, 42.2176), a malformed location |
| Lisbon | 4,404 km | pano at (3.6e-08, 3.6e-08), i.e. Null Island |
| Da Nang | 649 km | Mekong Delta; user photosphere |
| Ouray, CO | 631 km | southern New Mexico; user photosphere |
| St. Louis, MO | 385 km | Kansas City; user photosphere |

Worst cases, re-measure (every file's worst is in the metrics JSON):

| city | worst distance | what came back |
|---|---|---|
| Vernon, AL | 12,704 km | pano at (23.16, 57.27), Oman; `© Tharaka Abesinghe` |
| Newark, NJ | 12,044 km | pano at (27.52, 82.05), Nepal — and **`© Google`**, so the continental outliers are not only photospheres |
| Istanbul | 1,138 km | pano at (37.23, 41.33), southeastern Turkey; `© haLit Aslan` |
| Clay, WV | 45.9 km | user photosphere |
| Wichita, KS | 30.4 km | user photosphere |

The re-measure's sample holds no `lat == lon` or Null Island pano, so those malformed shapes are real (the issue found them) but rare.

The radius is not even monotone.
In ProjectSidewalk/SidewalkWebpage#5114, a `radius=25` query in Teaneck returned a pano 77 m away where `radius=50` returned a closer one, 45.8 m.
And in ProjectSidewalk/SidewalkWebpage#5091, a `radius=25` query for a Seattle street returned a photosphere in Syracuse, NY, 3,498 km away.

**Flagged, per this directory's rule:** these numbers contradict what the vendor documentation implies.
Google documents `radius` as the distance within which to search for a panorama; it does not say a pano outside that radius may come back.

## Decision

- **Tolerance 50 m**, because it is what the collection always intended (the documented default the downloader never overrides).
  A tighter value would bring grid coverage closer to "a pano near this spot", but it would also be a definition this data never asked for.
- **A read-side rule, never a rewrite.**
  The CSV records what Google said.
  `analysis.apply_query_radius` reclassifies a far pano as status `OUT_OF_RADIUS` at the loader seam, and `city.js` mirrors it.
- **GSV only.**
  The census providers assign panos to grid points from exact tile or bbox geometry, and the road walk bounds sample-to-pano distance itself (25 m), so neither has this overshoot.

The architecture is in [`docs/architecture.md`](../architecture.md), under "GSV query radius".

## Caveats

- The share is over **rows with a pano**, and GSV holds one row per grid point.
  So 10.4–11.2% is also the share of covered grid points that flip to uncovered at 50 m.
  That is a large move for a published coverage number, which is why running the repair over the series is left to the operator.
- 60 files is a sample.
  `scripts/gsv_query_radius_audit.py --sample 0` measures every cataloged GSV run on disk, and the first full pass of `scripts/recompute_run_stats.py` reports the per-run count on every line.
- Sending an explicit `radius` in the request would make the intent visible, but it cannot be the bound (see the Teaneck case).
  It also changes what we ask the provider, which falls under CLAUDE.md's READ THIS FIRST, so it is not part of this change.

## Replicate

`python scripts/gsv_query_radius_audit.py --data-dir <data dir> --sample 60 --seed 0` rewrites the metrics JSON; it opens the catalog read-only and makes no API calls.
`scripts/recompute_run_stats.py --provider gsv` as a dry run names the reclassified row count for each run it would change.
