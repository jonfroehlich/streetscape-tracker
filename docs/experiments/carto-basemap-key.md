# CARTO's basemap key: how it is enforced, and what leaving raster would cost

Issue #284.
Two questions measured on 2026-08-28, both cheap, both with answers that are easy to get wrong from the documentation alone.

Numbers here come from [`carto-basemap-key_metrics.json`](carto-basemap-key_metrics.json), produced by `scripts/measure_carto_basemap.py --out-dir docs/experiments`.
It sends the site's public basemap key to CARTO — the same key the deployed page sends on every tile request — and fetches two libraries from cdnjs.
No imagery provider, no credentials, no catalog; about a dozen requests.
There is no distribution to quote: both questions are deterministic, so what follows is a response matrix rather than percentiles, and the replication is re-running the one command.

## 1. CARTO enforces the key by watermarking the tile, not by refusing the request

Every way of getting the key wrong returns HTTP 200 and `image/png`, on our own URL form (`dark_all` shorthand, `{s}` subdomains, `{r}` retina), tile `12/655/1583`:

| Request | Status | Bytes | SHA-256 (first 16) |
|---|---|---|---|
| `?key=<our key>` | 200 | 16,945 | `d36c88d307ff56ad` |
| no query string at all | 200 | 15,516 | `cb4906d807d177ca` |
| `?key=<well-formed but wrong>` | 200 | 15,516 | `cb4906d807d177ca` |
| `?api_key=<our key>` | 200 | 15,516 | `cb4906d807d177ca` |

**The three failure modes are byte-identical to each other.**
A missing key, a wrong key and the right key under the wrong parameter name are literally the same response — a valid PNG with `API KEY REQUIRED / carto.com/basemaps/apikey` printed diagonally across the map.

Three things follow, and they are the reason this is written down.

**(1) No client-side error handling can ever detect this.**
There is no failed request, no non-2xx, no `errorTileUrl` path, no console warning.
The browser receives a perfectly good image and draws it.
The site rendered watermarked for an unknown period before a human happened to say the maps looked odd, and it presented as a styling bug rather than an outage — which is the wrong first hypothesis, and cost time.

**(2) The parameter name is load-bearing and unguessable from the docs.**
CARTO's own documentation shows the `rastertiles/voyager/{z}/{x}/{y}.png` path; we use the `dark_all` shorthand.
`api_key` is the plausible spelling and it silently fails.
So the key had to be verified against **our** URL form rather than the documented one, and any future change to that URL has to be re-verified the same way.

**(3) A detection path has to compare bytes.**
`tests/e2e/test_basemap_key.py` fetches the same tile with and without the key and requires the two to differ.
Differential rather than a pinned watermark hash, so it survives CARTO restyling the notice; it goes red for a revoked key, an exhausted quota and a dropped parameter alike.
Pinning `d36c88d3…` instead would go red every time CARTO edits the cartography, which they do continuously.

## 2. The domain CARTO collects at issue time is not enforced

CARTO asks for a domain when issuing the key. Sending the key with three different `Referer` headers:

| `Referer` | Status | Bytes | Same bytes as the issued origin? |
|---|---|---|---|
| the domain the key was issued for | 200 | 16,945 | — |
| a domain it was not issued for | 200 | 16,945 | yes |
| no `Referer` header at all | 200 | 16,945 | yes |

All three are identical, so **the key is bearer-style: it works from anywhere.**

This is the finding that changes what to do, because it separates two things that get conflated.
*Exposure* is unavoidable and not worth arguing about — the browser sends the key to CARTO on every tile request, so it is readable off the deployed page wherever the repo keeps it, and there is no build step to inject it at (ADR 0001).
*Abuse* is a different question, and it is mitigable.
The usual protection for a public map key is exactly this origin lock — it is what makes a Mapbox or Google Maps JS key safe to publish — and here it is absent.
So the comparison "same as any Mapbox / Google client key" does not hold, a copy lifted from this public repo works anywhere, and the free ceiling of 5M tile requests per calendar month is shared with whoever takes it.
Exhausting that ceiling degrades to the same silent watermark as having no key.

**The action is to ask CARTO to enforce the domain they already collected**, not to try to hide the key.
Until then the ceiling is the exposure, and `tests/e2e/test_basemap_key.py` is how we would find out.

## 3. Leaving raster for vector is a rendering-stack decision, not a way off the key

Worth stating plainly because it is the intuitive escape hatch and it is not one: CARTO's key page counts the free tier across the raster **and** vector services, and CARTO says the vector key requirement is coming, just not live yet.
So switching does not remove the key, the ceiling, or anything above.

What it does cost is the renderer.
Leaflet cannot draw MVT at all, so vector means replacing it with maplibre-gl.
Gzipped transfer size off cdnjs, both halves each library needs to draw a map:

| Library | Script | Stylesheet | Total gzipped |
|---|---|---|---|
| Leaflet 1.9.4 | 42,605 B | 3,526 B | **46,131 B** |
| maplibre-gl 5.24.0 | 275,453 B | 10,090 B | **285,543 B** |

**6.19× the bytes**, and that is the floor rather than the estimate: it excludes `@maplibre/maplibre-gl-leaflet`, which is not on cdnjs at all and would pull in a second CDN origin — and a cdnjs asset is already one of the two known e2e flakes.

Two costs that are not bytes and matter more:

- **WebGL in a headless screenshot suite is a new flake class**, on a job that already carries two.
- **Where WebGL is unavailable there is no basemap at all**, where raster simply works.

Set against that, CARTO has announced no raster end date.
Their FAQ says they are *"considering stopping data updates to the raster basemaps, in which case raster cartography will stay where it is and the gap will widen over time"* (read 2026-08-28).
That is materially weaker than a deprecation date, and the distinction is worth preserving: "stated retirement path" would have someone budgeting a migration against a deadline that does not exist.

**Decision: stay on raster.**
Revisit when CARTO announces an actual end date, or if the dense-city rendering path is ever reworked — MapLibre renders on the GPU, which is what `RENDER_CAP` and the `preferCanvas` subsampling exist to work around (#77 / #58), so that is the one change that would make the 6.19× buy something.
That is a project, not a maintenance chore, and it should be decided on rendering grounds rather than on key grounds.

## Caveats

- **Both answers are properties of CARTO's service on 2026-08-28, not invariants.**
  The `Referer` result in particular could change without notice, and would change silently in the safe direction (our own requests carry the issued origin) — so a future re-run is the only way to learn it.
- **The byte counts are one tile.**
  Tile size varies with content; the comparison is only ever between requests for the *same* tile, which is why the test is differential and never a threshold.
- **The bundle sizes are cdnjs's current latest.**
  They move with each release. The ratio is the durable number, not the absolute bytes.
- **`wrong_key_value` mutates the tail of the real key** rather than using an unrelated string, so it tests a well-formed key that is not ours — the realistic rotation-gone-wrong case, not a malformed input.
