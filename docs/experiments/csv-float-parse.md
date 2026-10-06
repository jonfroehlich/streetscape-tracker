# CSV float parse: does the loader read coordinates as they are on disk?

**Ran:** 2026-10-05, on the DEV catalog (a laptop copy, not production) ·
**Verdict:** no — pandas' default C parser read 4.6–5.6 % of walk latitudes and 37.3–38.6 % of longitudes one ULP off their own text.
That moved 9 sample keys across six walk CSVs (Seattle 5 of 247,292, Corvallis `all_public` 2 of 83,928 per provider), and the default parser scored all 9 uncovered.
Under round-trip the two gsv walks' scores move; Corvallis mapillary `all_public`'s 2 samples cover nothing either way.
Fixed with `float_precision="round_trip"` in `fileutils.load_city_csv_file` (issue #425); no grid statistic moves.

## The question

[#425](https://github.com/jonfroehlich/streetscape-tracker/issues/425) was found by the walk recompute (#262): its frame check could not match a handful of regenerated samples to their own CSV rows by the exact 9-decimal key, although the CSV text was exactly the sample's `repr`.
Every road-walk collector reads its CSV back through `fileutils.load_city_csv_file`, and `street_coverage.compute_streetwalk_coverage` joins samples to rows on `road_sampling.quantize_coord`'s 9-decimal key.
So the question is how often the loader's parse differs from the text, and what that costs each consumer of the loaded coordinates.

## Method

Read entirely out of files already on disk; no network, no credentials, the catalog opened read-only.
`scripts/csv_float_parse_analyze.py` does three things.

- **Walk CSVs.** Every CSV `street_walks` names, its `query_lat`/`query_lon` read three ways: pandas' default parser, `float_precision="round_trip"`, and Python's correctly rounded `float()` of the text.
  The script refuses unless round-trip equals `float()` of the text on every value, since that is the reference everything else is compared to.
  Keys are built from Python floats (`.tolist()`), as the scorer's own `zip` over a Series yields them; `round()` on an `np.float64` takes numpy's path and can land a half-way value on the other side, which would miscount.
- **Walk scoring.** Each walk re-scored by `recompute_streetwalk_stats.recompute_walk` against its frozen GraphML, once through the real loader with the default parser and once with round-trip.
  Both use the current coverage definition (#257 included), so the difference is #425's alone.
- **Grid runs.** The four largest dated grid runs per provider (8 runs, 18,229,993 rows), loaded through the real loader under both parsers, compared on every `calculate_run_stats` value (tolerance 1e-9, `recompute_run_stats._equalish`'s), query-radius status, `count_grid_points`, `diff._grid_keys`, and the per-run JSON's center and bounds.

## Findings

Walk CSVs (dev catalog; every off value is exactly one ULP: 7.1e-15° for latitude, 1.42e-14° for longitude):

| Walk CSV | Rows | Lat off | Lon off | Keys shifted |
|---|---|---|---|---|
| Adrian gsv/drive 2026-07-17 | 464 | 26 (5.6 %) | 179 (38.6 %) | 0 |
| Seattle gsv/drive 2026-07-22 | 247,292 | 11,416 (4.6 %) | 92,273 (37.3 %) | 5 |
| Adrian mapillary/drive 2026-07-27 | 464 | 26 (5.6 %) | 179 (38.6 %) | 0 |
| Corvallis mapillary/drive 2026-07-27 | 25,555 | 1,178 (4.6 %) | 9,693 (37.9 %) | 0 |
| Corvallis mapillary/all_public 2026-07-27 | 83,928 | 3,892 (4.6 %) | 31,359 (37.4 %) | 2 |
| Corvallis gsv/all_public 2026-07-27 | 83,928 | 3,892 (4.6 %) | 31,359 (37.4 %) | 2 |

Across the six files the latitude share runs 4.6 % to 5.6 % (p50 4.6 %) and the longitude share 37.3 % to 38.6 % (p50 37.6 %).
The issue's "21–22 % of query coordinates" is the pooled share over both columns (0.21 here); the per-column split is the shape worth keeping.

Scoring, default → round-trip loader:

- **Seattle gsv/drive**: the 5 shifted samples were scored uncovered and are now covered; `edges_fully_covered` 32394 → 32399, `length_km_covered` 3615.349 → 3615.424, `coverage_pct_by_length` 98.4 → 98.5.
- **Corvallis gsv/all_public**: `edges_fully_covered` 24145 → 24146, `length_km_covered` 872.853 → 872.867; the published percentage does not move at one decimal.
- **Corvallis mapillary/all_public**: the same 2 samples shift, but nothing moves: a missed key scores a sample uncovered, and under round-trip their own rows carry nothing the scorer counts as covering them either.
- **The other three walks**: nothing shifts, nothing moves.
- The recompute's tolerance-only matches (`n_noise`) drop from 5, 2 and 2 to 0 on every walk.

Grid runs: across all 8, zero `calculate_run_stats` values move, zero query-radius status flips (largest `query_distance_m` change 2.5e-9 m), and `count_grid_points` and the diff's grid keys are identical.
The per-run JSON's `query_bounds` differs in the last digit in 3 runs and its center in 1, which is not a definition and changes no number anyone reads.
Loader wall-clock under round-trip is 1.15× to 1.42× the default over the 6 runs whose load took at least 0.1 s (p50 1.26); the 16,569,307-row Detroit Mapillary census went from 29.6 s to 37.1 s.
Memory is unchanged: the dtypes are the same.

## Decision

- **Fix the loader, not the scorer.**
  The text is exact, so the only wrong link is the parse; a correctly rounded read makes every 9-decimal key consumer right without a tolerant join in the scoring hot path, and it stops the loader misreporting what is on disk — the #226 rule (`capture-date-precision.md`), generalized from dates to floats.
- **The option applies to the whole `read_csv` call.**
  It reaches the `np.float64` columns only; nullable `Float64Dtype` columns (`pano_lat`, `pano_lon`) take another parse path, are unchanged, and nothing keys on them.
- **Accept the parse cost.** 1.15–1.42× wall-clock and no memory is a bounded cost on paths that already run in hours-budgeted windows.
- **The walk recompute keeps its 1e-8° frame tolerance**, so it never refuses a series over a sub-ULP difference, but a tolerance-only match is now flagged as unexpected.
- **No grid recompute and no run-diff recompute**: nothing they store moves.
- **The repair is the whole-series walk recompute** (`scripts/recompute_streetwalk_stats.py`), run once after deploy; the runbook is in `docs/operations.md`.

## Caveats

- Dev catalog only: six walk CSVs from three cities and eight dated grid runs.
  The walks are gsv and mapillary only; no kartaview or panoramax walk CSV was measured, although both walk channels run in production.
  The production re-measure is the walk recompute's dry run after deploy, which lists every walk that moves.
- pandas 3.0.1 on macOS.
  The misparse is in pandas' own C parser rather than libc, so it should not be platform-specific; the tests re-measure the premise so a pandas that fixes its parser is reported.
- The asymmetry between columns (longitude roughly 8× latitude) is reported, not explained.
- Parse timings are single wall-clock readings on one laptop.

## Replicating

```bash
python scripts/csv_float_parse_analyze.py --data-dir data --docs-dir docs/experiments
```

The record is `csv-float-parse_metrics.json`; `tests/test_csv_float_parse_analyze.py` checks that this writeup still quotes it.
