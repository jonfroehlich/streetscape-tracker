# Worldwide city-sampling frame

This document is the reproducible methodology for Streetscape Tracker's **worldwide**
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

- `cities_worldwide.txt` — `run_cities.py`/`streetscape_tracker.py`-compatible query
  lines (double-quoted so names with apostrophes survive shlex parsing).
- `worldwide_frame.csv` — the selected frame, one row per city, with
  `query_string, city, admin, iso2, country, continent, size_band,
  population, coverage_regime, geonameid, lat, lon`. This is the manifest for the paper and
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
| `Bogota, Colombia` | `bogota--colombia` |
| `Shanghai, China` | `shanghai--china` |

An admin-1 name that merely restates the city (`Lima Province`, `Kyiv City`,
`Ho Chi Minh City (HCMC)`, `Bogota D.C.`) is dropped from both the query and
the identity (`build_worldwide_frame.effective_admin`), so city-state-like
slugs stay clean. `sanitize_city_query_str` itself is unchanged (it is a
frozen contract); we simply feed it clean inputs. Megacities inherit the
registration-time grid cap of 40 km/side (`cli.MAX_GRID_DIM_M`), so Shanghai's
~437×308 km administrative boundary clamps to 40×40 km. That ceiling was 80 km
until issue #166, when production showed 80 km still admitted grids no night
could collect — Cairo's ~10.5M points exceeded the entire daily gsv budget and
were skipped every night. `scripts/cap_oversized_grids.py` applied the same
40 km cap retroactively to already-registered cities.

Some frame cities were **already registered** earlier under geocoder-derived
slugs (e.g. `são-paulo--são-paulo--brazil`, `istanbul--marmara-region--turkey`).
`register_frame.py` detects these by distance (an existing city within
`--overlap-km`, default 25 km, of the GeoNames coordinates), aliases the frame
slug to the existing `city_id`, and never creates a duplicate — the existing
run series and published URLs stay authoritative.

## From frame to collection

Registration must run against the catalog the scheduler reads — i.e. on the
scheduler host (makelab2), after the merged code is deployed there.

1. **Register + freeze geometry** (no download, no provider API calls):
   `python scripts/register_frame.py` previews (dry run is the default;
   overlap detection needs no geocoding, so the preview is instant), then
   `--execute` geocodes each genuinely new city once (rate-limited Nominatim)
   and freezes its grid via the same helpers a real run uses
   (`cli._resolve_geometry`'s new-city branch). Idempotent; `--limit N` does a
   batch at a time. New cities are registered **disabled** (`enabled = 0`) so
   the scheduler cannot collect them before vetting. A geocoded center more
   than `--max-center-km` (default 50) from the GeoNames coordinates is
   rejected — big non-US metros can geocode to a province centroid (Ho Chi
   Minh City once landed ~100 km off) — and listed for manual review;
   `--center-from-geonames` falls back to the GeoNames coordinates instead.
   A city Nominatim cannot geocode under any Latin spelling gets a
   replacement manifest row in `data_sources/geocode_overrides.csv` (native
   script query, same GeoNames identity) — register those with
   `--manifest data_sources/geocode_overrides.csv`.
2. **Vet boundaries before collecting.** International OSM boundary quality
   varies, so run the boundary-audit workflow on the newly registered cities
   before enabling them: `scripts/audit_city_boundaries.py` →
   `scripts/build_boundary_review.py` → human review →
   `scripts/apply_decisions.py`. Swap rejects from `worldwide_candidates.csv`.
3. **Enable in the scheduler.** Set the vetted cities `enabled = 1`. Provider
   enablement stays global (both GSV and Mapillary); the scheduler staggers the
   cities over its cycle.

## Purposive additions (non-frame manifests)

The frame is a *stratified sample*, so it deliberately does not contain every city we might want.
Cities added for a specific reason live in their own manifest in the same format, registered by the same script — never appended to `worldwide_frame.csv`, which is the deterministic output of `build_worldwide_frame.py` and must keep tracing to it.

- `mapillary_360_cities.csv` (2026-08-31, 14 cities) — cities with a documented city-scale Mapillary 360° capture program that the catalog did not already track: BikeOttawa, Kaart in Melbourne, the Lithuanian Road Administration (Vilnius), Ramani Huria (Dar es Salaam), Mapillary's own showcase municipalities (Clovis NM, Johns Creek GA, and Sandusky as the seat of Erie County OH), Mapillary's home city (Malmo), and the CompleteTheMap Europe target cities Prague, Copenhagen, Munich, Milan, Barcelona and Brussels.

Two things differ from a frame registration:

- **Label the batch**: `--notes-label "mapillary 360 leaders"` writes that into `cities.notes`, so the vetting and enable steps can select exactly this batch and a later reader can tell where a city came from. Without it every registered city claims to be a frame city.
- **Shrink the overlap radius**: `--overlap-km 5`, not the default 25. The default exists to catch one physical city registered twice under different slugs, and at 25 km it also swallows genuine neighbours — Johns Creek sits 16 km from the already-registered Sugar Hill GA, and Sandusky 17 km from Kelleys Island OH, so both would have been *aliased away* instead of registered. Always read the dry run's `reused-existing` count before `--execute`.

The values in a purposive manifest are still a GeoNames join keyed by `geonameid`, not hand-typed coordinates; `tests/test_mapillary_360_cities_manifest.py` re-runs that join and is the file's provenance, since there is no generator script to name.
It also pins the permanent `city_id`s as literals and records the one deliberate departure from GeoNames' ASCII names (`Malmoe` -> `Malmo`, because the slug outlives the spelling in filenames and published URLs).

**Vet before registering, not after.** For a batch this size the cheapest vetting is to compute the geometry registration *would* freeze — `get_city_location_data` -> `resolve_center` -> `get_search_dimensions` -> `cap_dimensions`, the same four calls `register_frame_city` makes — and read two numbers per city: the grid dimensions, and the distance from the geocoded center to the manifest's GeoNames coordinates.
That offset is the tell, and on the 2026-08-31 batch it found four cities the `--max-center-km` guard would have waved through at its default of 50 km:

| City | What Nominatim matched | Offset | Fix |
|---|---|---|---|
| Sandusky OH | `Sandusky County, Ohio` — a *different* county, ~36 km west of the city (which is in Erie County) | 36.1 km | query override |
| Melbourne | Greater Melbourne (153x122 km) | 34.7 km | `--center-from-geonames` |
| Dar es Salaam | Dar es Salaam Region (102x69 km), midpoint offshore-ward | 22.6 km | `--center-from-geonames` |
| Ottawa | the amalgamated, mostly rural City of Ottawa (87x64 km) | 19.7 km | `--center-from-geonames` |

The nine cities that were fine all sat within 5.5 km, so `--max-center-km 10 --center-from-geonames` separates the two groups exactly: it recenters an over-large administrative match onto the GeoNames downtown point (the grid is clamped to 40 km/side anyway) and leaves every good geocode alone.
That does NOT fix a *wrong-feature* match, whose dimensions come from the wrong polygon — for those, override the geocode query in the manifest (`Sandusky, Erie County, Ohio, United States`) and keep identity on the GeoNames columns, so the frozen `city_id` is unchanged.
The same override handles the opposite failure, a match that is too SMALL: "Brussels" resolves to the City of Brussels commune (8.7x13.1 km), about a fifth of the 19-commune Brussels-Capital Region, and `Bruxelles-Capitale, Belgium` resolves to the region (16.8x16.7 km).
`get_city_location_data` restricts structured search to settlement types, so an override phrased as a region name may match a museum instead — always re-run the four calls on the override before committing it.

Vetting is the same requirement as for the frame, but the full audit chain is disproportionate for a handful of cities: read the registered rectangles back out of `cities`, and for anything that looks wrong render it with `streetscape_tracker.py "<query>" --check-boundary` and correct it with `scripts/resize_city.py` (safe only while the city has no runs).
The trap for these cities is the opposite of the province centroid the `--max-center-km` guard catches: several have a *tiny* core municipality as their OSM boundary — the City of Brussels and the City of Melbourne LGA are both a few km across inside metros many times larger.

## Refreshing the frame

Update the vendored GeoNames files (see `data_sources/README.md`) and/or
`gsv_coverage_regime.csv`, re-run the build, and **review the diff to
`worldwide_frame.csv` before re-registering** — a changed selection means new
frozen geometry, so only register genuinely new cities.
