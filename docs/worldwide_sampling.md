# Worldwide city-sampling frame

This document is the reproducible methodology for GSV Tracker's **worldwide**
city sample: the set of cities we track to compare street-level imagery coverage
and recency across countries and providers (Google Street View and Mapillary).

The frame **augments** the original US set (US state capitals in `cities.txt`);
it does not replace it. Existing US cities keep their frozen geometry, run
history, and published URLs.

## Design goals

- **Stratified and curated, not exhaustive.** ~50–80 cities spanning
  `continent × city-size band × GSV-coverage regime`, rather than every country
  (~600–780 cities), which would front-load heavy boundary-review and
  megacity-runtime cost for cities we'd rarely inspect.
- **Reproducible.** Selection is fully deterministic from vendored inputs, so
  re-running the build yields the identical frame.
- **Expandable.** Because grid geometry is frozen per city and adding a city is
  just one more catalog row, the frame can grow over time without disturbing
  existing series.

## Data source

City identity, location, size, and administrative/continent metadata come from
**[GeoNames](https://www.geonames.org/)**, © GeoNames, licensed
**CC BY 4.0**. We vendor three of its standard export tables under
`data_sources/` (see `data_sources/README.md` for schema and refresh
instructions):

| File | Provides |
|------|----------|
| `cities15000.txt` | Populated places with population > 15,000 (~34k): ASCII name, ISO-2 country, admin-1 code, population, coordinates. |
| `countryInfo.txt` | ISO-2 → country name and continent code. |
| `admin1CodesASCII.txt` | admin-1 code → region name (for geocoding queries). |

We vendor the files (rather than call an API at build time) so the frame is
reproducible from a fixed snapshot; the README documents refreshing from the
authoritative GeoNames dumps.

**Scope of use — population is a stratification tool only.** GeoNames population
figures are aggregated from mixed national sources and are city-proper (not
metropolitan) with non-uniform vintage. We use them **only to bin cities into
large/small strata**, never as a reported study variable. Coverage/recency
metrics come entirely from the provider metadata APIs, not from GeoNames.

### GSV coverage regime

`data_sources/gsv_coverage_regime.csv` (hand-maintained) tags countries whose
official Google Street View coverage is `sparse` or `absent` (default is
`present`). This is a small editable lookup, not a dataset; update it as
provider coverage changes.

## Selection algorithm

Implemented in `scripts/build_worldwide_frame.py`; parameters are constants at
the top of that file.

1. **Size bands** (population thresholds):
   - `large`: population ≥ **1,000,000**.
   - `small`: **50,000 ≤ population ≤ 250,000**.
   - Populations between the bands are ignored (keeps the strata separated).
2. **Eligible countries**: a country is eligible only if it has at least one
   qualifying `large` **and** one qualifying `small` city, so every selected
   country contributes a clean large+small pair.
3. **Primary (large) pick**: the country's most populous `large` city.
4. **Small pick**: a *distinct settlement*, not a borough of the primary city.
   We require it to be at least **75 km** from the large pick (fall back to the
   farthest available if none qualify), then choose the one whose population is
   nearest a **100,000** target — so the "small" stratum is genuinely small and
   geographically separate, rather than a ~250k inner suburb of the megacity.
5. **Per-continent quota**: within each inhabited continent (Africa, Asia,
   Europe, North America, South America, Oceania; Antarctica excluded), take the
   **5** most urban-significant eligible countries (ranked by primary-city
   population).
6. **Coverage-regime force-inclusion**: any eligible country marked `sparse` or
   `absent` is included even if it falls below the quota, guaranteeing the
   cross-provider (GSV-absent, Mapillary-present) contrast is represented.

All ordering uses deterministic tie-breaks (population, then name, then GeoNames
id) — no randomness — so the output is stable across runs.

### GSV-absent countries are included, Mapillary-first

Countries such as China are kept in the frame. A GSV run there records mostly
`ZERO_RESULTS` — a legitimate "no imagery here" signal (it passes the
systemic-failure guard, which only trips on `REQUEST_DENIED`/`OVER_QUERY_LIMIT`),
not a failure — while Mapillary carries the actual coverage. The GSV-vs-Mapillary
gap in these places is a finding, not a hole in the data.

## Outputs

Running `python scripts/build_worldwide_frame.py` writes (repo root):

- `cities_worldwide.txt` — `run_cities.py`/`gsv_tracker.py`-compatible query
  lines (double-quoted so names with apostrophes survive shlex parsing).
- `worldwide_frame.csv` — the selected frame, one row per city, with
  `query_string, city, iso2, country, continent, size_band, population,
  coverage_regime, geonameid, lat, lon`. This is the manifest for the paper and
  the input to `scripts/register_frame.py`.
- `worldwide_candidates.csv` — the full ranked eligible-country pool, so a city
  that fails boundary vetting can be swapped for an alternate without
  re-deriving the frame.

The current build yields **56 cities** across all 6 inhabited continents,
including 6 cities from sparse/absent-GSV countries.

## Fitting the existing dataset (identity & slugs)

Worldwide cities are registered into the **same** catalog, with the **same**
frozen-geometry model, filename contract, aggregate JSON, and frontend as the
original US cities — they are not a separate silo. The one integration hazard is
naming: a city's canonical `city_id` (and therefore every filename and published
URL) is a sanitized slug of its city/state/country names, and the existing
dataset is entirely ASCII.

If identity were taken from the geocoder's free-form response, international
cities would produce inconsistent slugs — e.g. `são-paulo--são-paulo--brazil`
(non-ASCII, URL-fragile) or `bogota--bogota--capital-district--colombia` (a
comma in the geocoded region name splits into a malformed extra slug component).

So `scripts/register_frame.py` **pins identity to the vendored GeoNames ASCII
names** (city `asciiname` + admin-1 ASCII name + English country name), using the
geocoder only for grid geometry. The results are ASCII, comma-free, and
structurally identical to the US slugs:

| Query | city_id |
|-------|---------|
| `Sao Paulo, Brazil` | `sao-paulo--brazil` |
| `Bogota, Bogota D.C., Colombia` | `bogota--bogota-d.c--colombia` |
| `Shanghai, China` | `shanghai--china` |

`sanitize_city_query_str` itself is unchanged (it is a frozen contract); we
simply feed it clean inputs. Megacities inherit the existing 80 km grid cap
(Shanghai's ~437×308 km administrative boundary clamps to 80×80 km).

## From frame to collection

1. **Register + freeze geometry** (no download):
   `python scripts/register_frame.py` geocodes each new city once (rate-limited
   Nominatim) and freezes its grid via the same path a real run uses
   (`cli._resolve_geometry`). Idempotent; use `--dry-run` to preview and
   `--limit N` to do a batch at a time.
2. **Vet boundaries before collecting.** International OSM boundary quality
   varies, so run the boundary-audit workflow on the newly registered cities
   before enabling them: `scripts/audit_city_boundaries.py` →
   `scripts/build_boundary_review.py` → human review →
   `scripts/apply_decisions.py`. Swap rejects from `worldwide_candidates.csv`.
3. **Enable in the scheduler.** Set the vetted cities `enabled = 1`. Provider
   enablement stays global (both GSV and Mapillary); the scheduler staggers the
   cities over its cycle.

## Refreshing the frame

Update the vendored GeoNames files (see `data_sources/README.md`) and/or
`gsv_coverage_regime.csv`, re-run the build, and **review the diff to
`worldwide_frame.csv` before re-registering** — a changed selection means new
frozen geometry, so only register genuinely new cities.
