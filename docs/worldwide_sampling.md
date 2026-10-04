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
3. **Enable in the scheduler.** `scheduler enable-city CITY` for each vetted
   city (issue #374), which also enrols it on the opt-in channels behind their
   gates (`docs/operations.md`). Default-membership channels stay global (GSV
   and Mapillary); the scheduler staggers the cities over its cycle.

## Purposive additions (non-frame manifests)

The frame is a *stratified sample*, so it deliberately does not contain every city we might want.
Cities added for a specific reason live in their own manifest in the same format, registered by the same script — never appended to `worldwide_frame.csv`, which is the deterministic output of `build_worldwide_frame.py` and must keep tracing to it.

- `mapillary_360_cities.csv` (2026-08-31, 14 cities) — cities with a documented city-scale Mapillary 360° capture program that the catalog did not already track: BikeOttawa, Kaart in Melbourne, the Lithuanian Road Administration (Vilnius), Ramani Huria (Dar es Salaam), Mapillary's own showcase municipalities (Clovis NM, Johns Creek GA, and Sandusky as the seat of Erie County OH), Mapillary's home city (Malmo), and the CompleteTheMap Europe target cities Prague, Copenhagen, Munich, Milan, Barcelona and Brussels.
- `panoramax_360_cities.csv` (2026-10-04, 40 cities, #406) — the richest Panoramax 360° clusters the 2026-10-01 world screen found outside the catalog ([`experiments/panoramax-world-screen.md`](experiments/panoramax-world-screen.md)), selected by a RULE rather than curated: every new cluster with a 360° upper bound of at least 100,000 (10,000 in the US and Canada), plus Kilkenny by name.
  Its own section is below; registering it is the runbook in [`operations.md`](operations.md) ("Registering a purposive manifest on production").

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
A third failure mode was caught only after registration (#302): `Copenhagen, Capital Region, Denmark` resolves to a polygon-less `place/city` node, so `get_search_dimensions` falls back to the node's bbox (20.1x35.6 km, reaching into countryside and the Øresund), and `Copenhagen Municipality, Denmark` resolves to the municipality (17.7x13.3 km).
That city was corrected in the catalog with `resize_city.py`, and the override is in the manifest too — a fix that lives only in the catalog is lost the next time the manifest is registered against a fresh one.
`get_city_location_data` restricts structured search to settlement types, so an override phrased as a region name may match a museum instead — always re-run the four calls on the override before committing it.

Vetting is the same requirement as for the frame, but the full audit chain is disproportionate for a handful of cities: read the registered rectangles back out of `cities`, and for anything that looks wrong render it with `streetscape_tracker.py "<query>" --check-boundary` and correct it with `scripts/resize_city.py` (safe only while the city has no runs).
The trap for these cities is the opposite of the province centroid the `--max-center-km` guard catches: several have a *tiny* core municipality as their OSM boundary — the City of Brussels and the City of Melbourne LGA are both a few km across inside metros many times larger.

Since #406 that vetting is a command, `scripts/vet_manifest_geometry.py --manifest <csv>`: it calls `register_frame.resolve_frame_geometry`, the function `register_frame_city` freezes from, so the preview and the registration cannot drift apart, and it prints the offset, the OSM feature matched and each grid's request price from `scheduler.estimate_requests`.
It geocodes through Nominatim only (the library's 1.1 s limiter), opens no catalog, and refuses a `makelab*` host; run it from a laptop.

### `panoramax_360_cities.csv` (#406)

**Selection.**
The rows are the new clusters of [`experiments/panoramax-world-screen_clusters.csv`](experiments/panoramax-world-screen_clusters.csv) (not within 25 km of the screen's 2026-10-01 catalog snapshot) whose 360° upper bound is at least 100,000, or 10,000 in the US and Canada, plus Kilkenny (73,765) by name for its city-completion camera grant.
A row is the cluster's NAME point — its most populous GeoNames place — so Strasbourg is the row even though its bound was summed around the Lingolsheim anchor.
The rows are ordered by descending bound, so `register_frame.py --limit N` registers the richest N first.
Excluded, each with its reason in `tests/test_panoramax_360_cities_manifest.py`:

- Cergy-Pontoise — one of the writeup's six `tracked_split` clusters (its anchor is within 25 km of Paris), and the only one of the six that clears the floor.
- Quimper — 20.4 km from Douarnenez, a richer row, and its bound was summed around Concarneau.
- Vienne — 24.9 km from Lyon, and its bound was summed around Givors, between the two.
- Waterloo, Iowa — 9 km from Cedar Falls, enabled on production on 2026-10-02, after the screen's snapshot.

The test also checks every row against the cities registered since the snapshot (the 25 towns of `mapillary_discovery_cities.csv` on PR #419's branch, and Montréal) and against the two committed manifests; nothing else is within 25 km.

**Identity.**
The slugs follow the GeoNames rule unchanged, which has two visible consequences.
French admin-1 names are GeoNames' PRE-2016 regions (`lyon--rhone-alpes--france`, `besancon--bourgogne--france`), not today's Auvergne-Rhône-Alpes or Bourgogne-Franche-Comté.
Marseille's slug carries an apostrophe, `marseille--provence-alpes-cote-d'azur--france`, as `Ust'-Ilimsk`'s already does; the scheduler's printed commands `shlex`-quote such an id.

**Vetting (2026-10-04, from a laptop, 49 Nominatim requests in two runs).**
The first run covered all 40 rows as first written; the second re-ran the four calls on the two query overrides it led to (and on two rejected alternatives).

- **Mayenne** matched the Mayenne *département* — the commune shares its name — 17 km off, so registration would have skipped it.
  `Mayenne, Mayenne, Pays de la Loire, France` resolves the commune, 0.4 km off; `53100 Mayenne, France` resolves a 17x16 km postal-code area and was rejected.
- **Muscatine** matched Muscatine *County*, 49x29 km, capped to 40 km: 2.95 M GSV points for a town of 24,000.
  `Muscatine, Muscatine County, Iowa, United States` resolves the city (13.7x13.4 km); `City of Muscatine, Iowa, United States` does not geocode.
- **Bordeaux, Bayonne and Angoulême** do not geocode under GeoNames' `New Aquitaine` and register through `register_frame.py`'s bare `City, France` fallback, which resolves each commune within 2.5 km.
  They carry no override, since an override equal to that fallback is forbidden by the test.
- **Norman, Oklahoma** is the real city boundary (33.7x22.5 km, Lake Thunderbird included), 7.5 km off: 1.9 M GSV points, the second-largest grid here.
  It is kept, and priced last.
- **Kilkenny** matched a `place/town` node with no polygon, Copenhagen's failure mode (#302), but its node extent (5.4x5.6 km) covers the town and stays inside the 40 km cap.

Every other row matched a `boundary/administrative` polygon (Immokalee, a census-designated place, a `boundary/census` one) within 3.8 km of its GeoNames point.
The offsets over all 40 are p50 1.65 km and max 7.5 km, so `--max-center-km 10` registers every row, and `--center-from-geonames` is unnecessary.
The table is the script's output with the two overrides' rows from the second run; KartaView is priced on the GeoNames point because the first run predates that column (no row's offset exceeds 7.5 km).
All prices are for one collection: GSV and Mapillary are default-membership, so an enabled city is due on both the next night; the GSV walk is the scheduler's area proxy, an over-estimate by design; the Mapillary, KartaView and Panoramax walks are not listed because each reads its grid run's census from the shared cache for 0 requests on a paired night (#290).

| # | City | Geocode query | OSM match | Grid W x H (m) | GSV points | GSV walk samples (area proxy) | Mapillary z14 tiles | KartaView requests | Panoramax z15 tiles | Offset (km) | Center | Flags |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Strasbourg | Strasbourg, Grand Est, France | boundary/administrative | 10,917 x 17,153 | 468,468 | 93,005 | 88 | 187 | 330 | 2.1 | geocoded |  |
| 2 | Lyon | Lyon, Rhone-Alpes, France | boundary/administrative | 9,849 x 11,214 | 276,573 | 54,855 | 42 | 100 | 168 | 1.4 | geocoded |  |
| 3 | Bordeaux | Bordeaux, France | boundary/administrative | 8,299 x 11,713 | 243,190 | 48,279 | 42 | 97 | 154 | 2.5 | geocoded | FALLBACK-QUERY |
| 4 | Lille | Lille, Hauts-de-France, France | boundary/administrative | 11,161 x 6,721 | 188,383 | 37,256 | 40 | 72 | 150 | 0.7 | geocoded |  |
| 5 | Laval | Laval, Pays de la Loire, France | boundary/administrative | 7,716 x 7,413 | 143,206 | 28,408 | 30 | 64 | 100 | 1.6 | geocoded |  |
| 6 | Saint-Nazaire | Saint-Nazaire, Pays de la Loire, France | boundary/administrative | 9,884 x 9,715 | 240,570 | 47,691 | 49 | 88 | 169 | 2.1 | geocoded |  |
| 7 | Caen | Caen, Normandy, France | boundary/administrative | 6,054 x 7,034 | 106,656 | 21,149 | 24 | 45 | 80 | 1.0 | geocoded |  |
| 8 | Montpellier | Montpellier, Occitanie, France | boundary/administrative | 10,838 x 9,627 | 261,244 | 51,820 | 42 | 100 | 156 | 0.2 | geocoded |  |
| 9 | Le Havre | Le Havre, Normandy, France | boundary/administrative | 9,327 x 9,840 | 230,231 | 45,582 | 48 | 88 | 168 | 1.7 | geocoded |  |
| 10 | Nantes | Nantes, Pays de la Loire, France | boundary/administrative | 12,339 x 12,815 | 395,497 | 78,535 | 72 | 162 | 272 | 2.4 | geocoded |  |
| 11 | Brest | Brest, Brittany, France | boundary/administrative | 10,445 x 11,370 | 297,587 | 58,983 | 56 | 129 | 195 | 2.2 | geocoded |  |
| 12 | Montauban | Montauban, Occitanie, France | boundary/administrative | 12,527 x 15,661 | 491,568 | 97,438 | 80 | 194 | 285 | 1.0 | geocoded |  |
| 13 | Orleans | Orleans, Centre-Val de Loire, France | boundary/administrative | 5,457 x 13,373 | 182,637 | 36,244 | 36 | 72 | 136 | 3.3 | geocoded |  |
| 14 | Grenoble | Grenoble, Rhone-Alpes, France | boundary/administrative | 5,935 x 6,660 | 99,198 | 19,631 | 20 | 45 | 72 | 0.6 | geocoded |  |
| 15 | Besancon | Besancon, Bourgogne, France | boundary/administrative | 10,788 x 13,273 | 358,560 | 71,117 | 63 | 144 | 238 | 1.4 | geocoded |  |
| 16 | Morlaix | Morlaix, Brittany, France | boundary/administrative | 5,613 x 7,383 | 103,970 | 20,582 | 20 | 43 | 80 | 2.2 | geocoded |  |
| 17 | Toulouse | Toulouse, Occitanie, France | boundary/administrative | 13,322 x 15,111 | 504,252 | 99,983 | 80 | 198 | 304 | 1.0 | geocoded |  |
| 18 | Bayonne | Bayonne, France | boundary/administrative | 7,085 x 7,667 | 136,320 | 26,979 | 30 | 64 | 90 | 1.4 | geocoded | FALLBACK-QUERY |
| 19 | Mayenne | Mayenne, Mayenne, Pays de la Loire, France | boundary/administrative | 5,426 x 6,400 | 87,312 | 17,247 | 20 | 36 | 72 | 0.4 | geocoded |  |
| 20 | Lons-le-Saunier | Lons-le-Saunier, Bourgogne, France | boundary/administrative | 3,835 x 4,297 | 41,280 | 8,184 | 16 | 21 | 42 | 0.1 | geocoded |  |
| 21 | Tours | Tours, Centre-Val de Loire, France | boundary/administrative | 6,380 x 10,081 | 161,600 | 31,944 | 35 | 72 | 117 | 0.7 | geocoded |  |
| 22 | Angouleme | Angouleme, France | boundary/administrative | 7,079 x 5,727 | 101,598 | 20,135 | 20 | 54 | 72 | 0.8 | geocoded | FALLBACK-QUERY |
| 23 | Mannheim | Mannheim, Baden-Wurttemberg, Germany | boundary/administrative | 12,733 x 20,033 | 638,274 | 126,689 | 126 | 270 | 459 | 2.8 | geocoded |  |
| 24 | Douarnenez | Douarnenez, Brittany, France | boundary/administrative | 8,145 x 6,876 | 140,352 | 27,815 | 36 | 54 | 110 | 1.8 | geocoded |  |
| 25 | Marseille | Marseille, Provence-Alpes-Cote d'Azur, France | boundary/administrative | 24,650 x 24,598 | 1,516,590 | 301,149 | 225 | 583 | 841 | 1.8 | geocoded |  |
| 26 | Beaune | Beaune, Bourgogne, France | boundary/administrative | 8,382 x 7,033 | 147,840 | 29,278 | 30 | 54 | 99 | 0.4 | geocoded |  |
| 27 | Kortrijk | Kortrijk, Flanders, Belgium | boundary/administrative | 11,159 x 15,975 | 445,842 | 88,538 | 88 | 172 | 315 | 3.0 | geocoded |  |
| 28 | Cherbourg | Cherbourg, Normandy, France | boundary/administrative | 14,750 x 10,485 | 387,450 | 76,811 | 88 | 158 | 300 | 1.9 | geocoded |  |
| 29 | Ulm | Ulm, Baden-Wurttemberg, Germany | boundary/administrative | 14,837 x 17,990 | 667,800 | 132,569 | 132 | 257 | 460 | 3.8 | geocoded |  |
| 30 | Kilkenny | Kilkenny, Leinster, Ireland | place/town | 5,366 x 5,629 | 75,858 | 15,001 | 25 | 28 | 81 | 0.5 | geocoded |  |
| 31 | Ottumwa | Ottumwa, Iowa, United States | boundary/administrative | 7,887 x 12,661 | 250,430 | 49,595 | 40 | 97 | 135 | 1.5 | geocoded |  |
| 32 | Marshalltown | Marshalltown, Iowa, United States | boundary/administrative | 11,263 x 8,641 | 244,212 | 48,337 | 48 | 100 | 140 | 2.0 | geocoded |  |
| 33 | Norman | Norman, Oklahoma, United States | boundary/administrative | 33,722 x 22,522 | 1,901,249 | 377,211 | 216 | 691 | 816 | 7.5 | geocoded |  |
| 34 | Newton | Newton, Iowa, United States | boundary/administrative | 7,176 x 6,432 | 115,598 | 22,924 | 20 | 54 | 64 | 1.0 | geocoded |  |
| 35 | Mason City | Mason City, Iowa, United States | boundary/administrative | 13,661 x 8,828 | 302,328 | 59,897 | 54 | 126 | 176 | 1.1 | geocoded |  |
| 36 | Muscatine | Muscatine, Muscatine County, Iowa, United States | boundary/administrative | 13,730 x 13,356 | 458,916 | 91,077 | 72 | 180 | 256 | 3.1 | geocoded |  |
| 37 | Fort Dodge | Fort Dodge, Iowa, United States | boundary/administrative | 9,058 x 9,492 | 215,175 | 42,702 | 36 | 88 | 121 | 2.0 | geocoded |  |
| 38 | Davenport | Davenport, Iowa, United States | boundary/administrative | 16,446 x 18,167 | 748,107 | 148,391 | 110 | 280 | 399 | 2.3 | geocoded |  |
| 39 | Owatonna | Owatonna, Minnesota, United States | boundary/administrative | 8,381 x 9,602 | 202,020 | 39,968 | 36 | 75 | 120 | 2.1 | geocoded |  |
| 40 | Immokalee | Immokalee, Florida, United States | boundary/census | 7,909 x 5,263 | 104,544 | 20,673 | 15 | 43 | 48 | 0.6 | geocoded |  |
| | **Total (40 resolved)** | | | | 13,682,485 | 2,713,672 | 2,350 | 5,385 | 8,390 | | | |

Nominatim can answer differently on the day of registration than on the day of vetting, so `register_frame.py --execute` prints each frozen W x H; compare it with this table before enabling anything, and treat a difference as a reason to stop.
The numbers are upper-bound-selected, not coverage: Immokalee's 360° share is the one the screen's own `/api/search` probe contradicted (its 20 sampled pictures were flat phone imagery), so expect it to measure low.

## Refreshing the frame

Update the vendored GeoNames files (see `data_sources/README.md`) and/or
`gsv_coverage_regime.csv`, re-run the build, and **review the diff to
`worldwide_frame.csv` before re-registering** — a changed selection means new
frozen geometry, so only register genuinely new cities.
