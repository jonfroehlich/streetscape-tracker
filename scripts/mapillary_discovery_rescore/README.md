# Mapillary discovery re-score (#441, tranche 3)

Research code from the 2026-11-05 run, committed as re-run on 2026-11-06 so the tranche-3 record traces to the code that produced it (the lint exemption in `pyproject.toml` is scoped to this directory for that reason).
Since the 11-05 run the scripts gained JSON exports of what they print, `overlaps.py` was added, and `tier3.py`'s cluster bootstrap now leaves out every copy of a resampled city (the writeup says what that changed); the ranked record is byte-identical to the 11-05 output, and the grid record differs from it only by its added `_about` key.
Every script makes **zero Mapillary requests**: it reads the 2026-10-02 z6 scan's cached sequences.
Only `fetch_boundaries.py` touches the network (Nominatim and Overpass, from a laptop, cached per town).
The writeup is `docs/experiments/mapillary-discovery-screen.md`, "Tranche 3".

Run from the repository root; `W` is any scratch directory and the raw inputs are the gitignored `experiments/mapillary-discovery-383/` files (`sequences.parquet`, `cities500.txt`, `creators_resolved.json`, `scan_manifest.json`) plus a read-only production snapshot (`scripts/mapillary_discovery_collect.py catalog-snapshot`):

```bash
T=experiments/mapillary-discovery-383/tier_2026-11-05
python scripts/mapillary_discovery_rescore/features.py "$W" "$T/prod_snapshot.csv"   # per-place and per-catalog-city features
cp "$T/prod_snapshot.csv" "$W/"
python scripts/mapillary_discovery_rescore/evaluate.py "$W"                          # scores vs walks -> evaluate_metrics.json
python scripts/mapillary_discovery_rescore/tier3.py "$W" docs/experiments/mapillary-discovery-screen_tranche3_metrics.json
#   also writes experiments/mapillary-discovery-383/tranche3_ranked_2026-11-05.csv,
#   committed as docs/experiments/mapillary-discovery-screen_tranche3_ranked.csv
python scripts/build_mapillary_discovery_tranche3.py                                  # the manifest, from the committed record

python scripts/mapillary_discovery_rescore/overlaps.py "$T/prod_snapshot.csv" docs/experiments/mapillary-discovery-screen_tranche3_overlaps.json

# grid fits for Kuna, Magna and Carmel-by-the-Sea (K holds <town>_poly.json and tiger/tl_2024_*_roads.zip)
K=$T/boundaries
python scripts/mapillary_discovery_rescore/fetch_boundaries.py "$K"
python scripts/mapillary_discovery_rescore/fit_grids.py "$K" "$T/prod_snapshot.csv"   # -> $K/fit_results.json, committed as _tranche3_grids.json
python scripts/mapillary_discovery_rescore/plot_grids.py "$K" SWEEP_COLOR GRID_COLOR   # -> $K/boundaries_map.png, committed as _tranche3_grids.png
#   (two hex colours; the ones the committed PNG used were not recorded, so a re-plot matches it in content, not pixels)
```

`fit_grids.py` reads the vetting CSV at `$K/../tranche3_vet.csv` (committed as `docs/experiments/mapillary-discovery-screen_tranche3_vet.csv`).
The TIGER files are the Census Bureau's 2024 county road files for counties 16001, 49035 and 06053.
