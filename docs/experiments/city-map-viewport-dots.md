# The city map's dot tier, before and after drawing every pano in view

Issue #77.
Measured 2026-11-14 on synthetic dense cities, before and after the change that makes a city over `RENDER_CAP` (40,000) draw every panorama in a view whose padded drawn area (the visible map plus a 10% margin each side, `DRAWN_AREA_PAD`, with hysteresis) holds at most the cap (`docs/frontend.md`, "Every pano in view is drawn on a city over the render cap").
Two questions: does a street-level view now show what is there, and what does that cost.

Numbers here come from two committed files:

- [`city-map-viewport-dots_metrics.json`](city-map-viewport-dots_metrics.json), produced by `scripts/measure_city_map_render.py` in headless Chromium 149 (1440×900, Apple M1 Max, macOS).
  "Before" is `git archive` of `www/` at main `e9c4528`, "after" is this branch's `www/`, served side by side to the same browser.
  The record names the after copy by `www_tree` (`git rev-parse <commit>:www`, the content hash of the committed `www/`) and `www_dirty: false`, so it can be checked against whichever commit it lands in.
  It was re-run in full after the PR's first review, whose fixes changed the sampled tier's per-move work; the first run's record is in the history at `c70d1f3`.
- [`city-map-viewport-dots_index_bench.json`](city-map-viewport-dots_index_bench.json), produced by `node scripts/bench_pano_index.js` (Node 24), which calls the page's own `buildPanoIndex`, `countPerCell`, `countInBounds` and `idsInBounds` on seeded uniform points over a Houston-sized box, to reach sizes a browser load cannot.

No imagery provider, Overpass or Nominatim was contacted, and no catalog was read: the cities are written by `tests/e2e/build_fixture.write_dense_city`, the page's libraries load from cdnjs as in the e2e suite, and basemap tiles and analytics are aborted.

## The cities, and why they are a fair test of the dot tier and a weak one of load

`write_dense_city` lays a street lattice, streets every 100 m with a panorama every 10 m, so density is constant and a larger city is only a wider one.
Three sizes: 60,000, 200,000 and 1,000,000 panoramas (0.9, 3.1 and 15.4 MB gzipped CSV).
A street-level view at zoom 16 therefore holds about 7,200 panoramas at every size, which isolates the question of what the dot tier draws.
Load is a weaker test: a real city's streets are not a lattice and its CSV carries more columns.
Each copy loaded each city 5 times; each load then zoomed +2 from the fitted view, jumped to zoom 16 at the centre, and panned 300 px six times (n = 5 for loads and single moves, n = 30 for pans).
Timings are to the end of the synchronous `moveend` handler (`sync`) and to the second animation frame after the move (`paint`).

## 1. A street-level view now draws every panorama in it

Panoramas drawn inside the visible map at zoom 16, over the 30 pans:

| City | Before: drawn in view (p25 / p50 / p75) | After: drawn in view (p25 / p50 / p75) | After: truly in view |
|---|---|---|---|
| 60,000 | 3,176 / 4,462 / 4,844 | 4,714 / 6,615 / 7,180 | identical to drawn on every pan |
| 200,000 | 1,454 / 1,483 / 1,486 | 7,264 / 7,402 / 7,418 | identical |
| 1,000,000 | 281 / 287 / 288 | 7,056 / 7,180 / 7,180 | identical |

The pans walk off the 60,000 city's east edge, which is why its counts fall; the other two are wide enough not to.
At the first street-level stop on the 1,000,000 city, before drew 288 of the 7,195 panoramas in view (4%), and after drew all 7,195.
That is the defect #77 names: before, one global stride sample of 40,000 was drawn at every zoom, so its density in view fell as the city grew, and zooming in never showed more.
After, the "truly in view" column is the page's own exact count (`countInBounds` over the visible map), and on every one of the 90 pans it equals the markers inside the visible map.
At the fitted view and at +2 both copies draw the same 40,000-marker sample (the after copy says so in the legend), except the 60,000 city at +2, whose view already holds few enough to switch to the complete tier.

## 2. What it costs per move

Medians, milliseconds, `sync` / `paint`:

| City | Move | Before | After |
|---|---|---|---|
| 60,000 | zoom +2 (enters complete) | 38.2 / 51.6 | 102.8 / 135.0 |
| 60,000 | pan at z16 | 4.8 / 12.4 | 10.0 / 31.9 |
| 200,000 | zoom +2 (stays sampled) | 52.5 / 63.4 | 53.3 / 64.3 |
| 200,000 | jump to z16 (enters complete) | 42.2 / 46.5 | 116.7 / 142.9 |
| 200,000 | pan at z16 | 5.6 / 9.9 | 12.9 / 36.3 |
| 1,000,000 | zoom +2 (stays sampled) | 77.2 / 86.2 | 79.3 / 88.7 |
| 1,000,000 | jump to z16 (enters complete) | 60.2 / 63.1 | 149.7 / 175.2 |
| 1,000,000 | pan at z16 | 5.5 / 14.1 | 13.1 / 37.5 |

The full distributions (n, min, p25–p95, max) are in the metrics file.
**The cost is the markers, not the index.**
A pan after the change adds the panoramas that came into view, where before it added none (the drawn set never changed), and the pan's p95 stays under 43 ms at every size.
The worst single move is entering the complete tier on the largest city: 40,000 markers removed and about 10,500 added, p95 184.9 ms to paint.
That happens once per entry, not per pan, because the tier has hysteresis (leaving needs 1.25 × the budget).
A pan's cost is flat across city size (p50 paint 31.9, 36.3, 37.5 ms), which is what a view-sized drawn set should give.
**A move that stays sampled now costs what it did before** (1,000,000 city, zoom +2: 79.3 ms sync against 77.2 before).
In the first run it cost 94.5 against 73.5, because every sampled-tier `moveend` rebuilt and re-diffed the unchanged 40,000-marker sample; the planner now reuses the drawn set when the sample, the date and the open popup are unchanged (`plan.reused`), and only counts the view.

## 3. Load and memory are unchanged in kind

| City | Load p50 before → after (ms) | JS heap p50 before → after (MB) | Index build in page p50 (ms) | Index size |
|---|---|---|---|---|
| 60,000 | 979 → 1,002 | 101.6 → 98.5 | 8.2 | 0.49 MB, level 16, 199 cells |
| 200,000 | 1,918 → 1,927 | 292.9 → 281.5 | 20.1 | 1.63 MB, level 16, 598 cells |
| 1,000,000 | 7,406 → 7,766 | 1,335.3 → 1,285.6 | 93.6 | 8.13 MB, level 16, 2,856 cells |

Load is navigation to the capture-date chart existing; heap is after a forced GC.
At 1,000,000 panoramas load rose 0.36 s at the median, about 5% (p25–p75 7,374–7,433 ms before, 7,533–7,995 after; the first run measured +0.64 s, about 9%, so the size of the rise is itself not stable across runs), of which the index build is 0.1 s; the rest is unexplained here, and the per-pano columns filled during streaming are the obvious candidate, unmeasured.
Those columns hold 19 bytes of data per panorama, but they grow by doubling and the page keeps views of the grown buffers, so 19 to 38 bytes a panorama are retained (19 to 38 MB at 1,000,000), plus one marker reference per panorama in `panoMarkersById`.
Heap at load is slightly LOWER after (both copies have the same 40,000 markers on the map at the fitted view, so why is not established here), and after the moves it is within 4% either way (`heap_bytes_after_moves`: 1,297 MB after against 1,335 MB before on the largest city); the index's own arrays, 8 MB there, are small beside it.
**Both copies hold one Leaflet marker per panorama, built during streaming**: about 1.3 KB of heap per panorama on the 1,000,000 city, which is what makes a large city slow to load and large in memory.
This change does not touch that, and was not meant to; it is the next step on #77 (lazy markers), and the measurement says it is where the time and memory are, not in the index.

## 4. The index alone, at sizes past a browser load

Node, seeded uniform points, medians of 3:

| Points | Level | Cells | Build (ms) | Count whole city (ms) | Count street view (ms) | Ids street view (ms) | Index bytes |
|---|---|---|---|---|---|---|---|
| 1,000,000 | 16 | 8,160 | 159 | 1.0 | 0.38 | 0.18 | 8.4 MB |
| 2,700,000 | 15 | 2,064 | 435 | 6.3 | 0.22 | 0.38 | 21.7 MB |
| 15,000,000 | 14 (packed) | 528 | 2,762 | 58.5 | 3.8 | 7.3 | 120.0 MB |

The per-pano columns the index reads add 19 bytes a panorama here (285 MB at 15,000,000), exactly, because the benchmark sizes its builder to the point count; the page's builder grows by doubling and retains 19 to 38 (up to 570 MB at that size).
Uniform points fill more cells than a real city's streets do, so these are not the in-page figures above (1,000,000 lattice points built in 94 ms over 2,856 cells).
All three sizes take the packed-key sort: 15,000,000 is under 2^24 (16,777,216), and `indexLayout(15e6)` returns level 14, 24 id bits, packed.
The comparator fallback, which only a city over 2^24 panoramas takes, is not benchmarked here; the node suite holds it to the packed sort's order (`forceComparatorSort`), not to a time.
A city of 15,000,000 could not load in this page anyway, since its markers alone would need tens of gigabytes (about 1.3 KB each, section 3).

## Caveats

- **Synthetic, one machine, one browser.** The lattice is uniform where real cities cluster, and an M1 Max is faster than most phones; the relative before/after comparison is the finding, not the milliseconds.
- **One-off protocol.** This cannot catch a later regression; the e2e tests in `tests/e2e/test_smoke.py` (`docs/testing.md`, "The viewport-true dot tier") are what guard the behaviour.
- **`paint` is two animation frames**, a proxy for "the renderer has drawn it", not a measured paint.
- **Run-to-run spread is larger than some of the differences.** The two full runs agree on every drawn and in-view count exactly, but their 1,000,000-pano load rise was +9% and +5%; read a timing difference of a few percent as no difference.
- No figure here comes from the dev catalog or from a production city.

## Decision

Keep the view-dependent dot tier: at street level it draws every panorama in view at every city size measured, where before the share fell to 4% at 1,000,000.
The next work on #77 is lazy markers (build a marker when it is first drawn), because load and memory are dominated by one marker per panorama and this change leaves that untouched.

## Replicate

```bash
mkdir -p /tmp/before && git archive origin/main www | tar -x -C /tmp/before
python scripts/measure_city_map_render.py --www before=/tmp/before/www --www after=www \
  --sizes 60000,200000,1000000 --repeats 5 --work-dir /tmp/dense \
  --out docs/experiments/city-map-viewport-dots_metrics.json
node scripts/bench_pano_index.js --sizes 1000000,2700000,15000000 --repeats 3 \
  --out docs/experiments/city-map-viewport-dots_index_bench.json
```
