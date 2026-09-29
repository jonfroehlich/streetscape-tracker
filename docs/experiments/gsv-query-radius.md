# GSV query radius: how far from its grid point is the pano Google returns?

**Ran:** measured in [#367](https://github.com/jonfroehlich/streetscape-tracker/issues/367) on 60 of 1,171 archived GSV city files, seed 0, no API calls ·
**Verdict:** Google's metadata `radius` is a hint, not a bound.
10.4% of rows with a pano sit more than 50 m from their query point, so a GSV pano now counts for its grid point only within `analysis.GSV_QUERY_RADIUS_M` (50 m).

**Provenance, stated because it breaks this directory's usual rule:** the sampling script lived in the issue, not the repo, so no `gsv-query-radius_metrics.json` is committed.
Every number below is quoted from the issue body and was not re-measured for this writeup.

## The question

`download_gsv.py` queries the Street View metadata endpoint with no `radius`, so Google applies its documented default of 50 m.
It stores `pano_lat`/`pano_lon`, but nothing ever checked how far the returned pano is from the query point.
Every row with a pano counted as a covered grid point, however far away the pano was.

## Method

A random sample of 60 of the 1,171 GSV city files in `data/` (seed 0).
For every row with a pano, the haversine distance from `(query_lat, query_lon)` to `(pano_lat, pano_lon)`.

## Results

| | rows | share of rows with a pano |
|---|---|---|
| rows with a pano | 8,652,929 | |
| pano > 50 m from its query point | 901,700 | **10.4%** |
| pano > 100 m | 187,035 | 2.2% |
| pano > 1 km | 3,068 | 0.04% |

- 57 of 60 cities have at least one row beyond 50 m, and 21 have at least one beyond 1 km.
- Of the > 50 m rows, 26,592 carry a non-Google `copyright_info`.
  So the bulk of the overshoot is Google's own panos at 50–100 m, and the continental-scale outliers are user photospheres.

Worst case per city, top of the sample:

| city | worst distance | what came back |
|---|---|---|
| Anchorage, AK | 11,185 km | pano at (9.50, 76.34), Kerala, India; `© D R` |
| Holyoke, MA | 8,586 km | `pano_lat == pano_lon` (42.2176, 42.2176), a malformed location |
| Lisbon | 4,404 km | pano at (3.6e-08, 3.6e-08), i.e. Null Island |
| Da Nang | 649 km | Mekong Delta; user photosphere |
| Ouray, CO | 631 km | southern New Mexico; user photosphere |
| St. Louis, MO | 385 km | Kansas City; user photosphere |

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
  So 10.4% is also the share of covered grid points that flip to uncovered at 50 m.
  That is a large move for a published coverage number, which is why running the repair over the series is left to the operator.
- 60 files is a sample, and the per-city spread is not quoted in the issue.
  The first full pass of `scripts/recompute_run_stats.py` reports the per-run count on every line, and that is the whole-catalog distribution.
- Sending an explicit `radius` in the request would make the intent visible, but it cannot be the bound (see the Teaneck case).
  It also changes what we ask the provider, which falls under CLAUDE.md's READ THIS FIRST, so it is not part of this change.

## Replicate

Recompute the distances with `geoutils.haversine_m` over any sample of GSV run CSVs, or run `scripts/recompute_run_stats.py --provider gsv` as a dry run.
Its report line names the reclassified row count for each run it would change.
