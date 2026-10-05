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
   `scripts/reregister_boundaries.py` (dry run; it writes the
   `reregister_plan.csv` / `manual_review.csv` the review page is built from,
   so skipping it builds an EMPTY page) → `scripts/build_boundary_review.py` →
   human review → `scripts/apply_decisions.py`. Swap rejects from
   `worldwide_candidates.csv`.
3. **Enable in the scheduler.** `scheduler enable-city CITY` for each vetted
   city (issue #374), which also enrols it on the opt-in channels behind their
   gates (`docs/operations.md`). Default-membership channels stay global (GSV
   and Mapillary); the scheduler staggers the cities over its cycle.

## Purposive additions (non-frame manifests)

The frame is a *stratified sample*, so it deliberately does not contain every city we might want.
Cities added for a specific reason live in their own manifest in the same format, registered by the same script — never appended to `worldwide_frame.csv`, which is the deterministic output of `build_worldwide_frame.py` and must keep tracing to it.

- `mapillary_360_cities.csv` (2026-08-31, 14 cities) — cities with a documented city-scale Mapillary 360° capture program that the catalog did not already track: BikeOttawa, Kaart in Melbourne, the Lithuanian Road Administration (Vilnius), Ramani Huria (Dar es Salaam), Mapillary's own showcase municipalities (Clovis NM, Johns Creek GA, and Sandusky as the seat of Erie County OH), Mapillary's home city (Malmo), and the CompleteTheMap Europe target cities Prague, Copenhagen, Munich, Milan, Barcelona and Brussels.
- `mapillary_discovery_cities_tranche2.csv` (2026-10-04, 12 cities, #383) — the second tranche of the Mapillary discovery screen's towns (PR #419, `experiments/mapillary-discovery-screen.md`), selected by a RULE over the screen's per-place scores rather than curated.
  Its own section is below; registering it is the runbook in [`operations.md`](operations.md) ("Registering the Mapillary discovery screen's second tranche").

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

`scripts/vet_manifest_geometry.py --manifest <csv>` is that vetting as a command: it calls `register_frame.resolve_frame_geometry`, the function `register_frame_city` freezes from, so the preview and the registration cannot drift apart, and it prints the offset, the OSM feature matched and each grid's request price from `scheduler.estimate_requests`.
It geocodes through Nominatim only (the library's 1.1 s limiter), opens no catalog, and refuses a `makelab*` host; run it from a laptop.

### `mapillary_discovery_cities_tranche2.csv` (#383)

**Selection.**
The input is the discovery screen's 161 candidates (`docs/experiments/mapillary-discovery-screen_candidates.csv` on PR #419's branch): GeoNames cities500 places scoring at least 3 km of recent 360° Mapillary sequence per km² within 2 km of their point, outside every catalog grid.
`scripts/build_mapillary_discovery_tranche2.py` applies the second tranche's rule and writes every candidate's decision to [`experiments/mapillary-discovery-screen_tranche2.csv`](experiments/mapillary-discovery-screen_tranche2.csv), which `tests/test_mapillary_discovery_tranche2_manifest.py` re-derives independently.
In order:

1. score ≥ 3 (all 161 pass; it is the screen's own floor);
2. more than 25 km from every city the catalog knows: the production snapshot the screen exported on 2026-10-02 (1,232 cities, Montréal and Ottawa among them) plus tranche 1's 25 towns (`mapillary_discovery_cities.csv`, Cedar Falls among them) — **116 dropped**, and one of them, Fergus Falls MN, admitted anyway by operator decision (below);
3. the place is in the vendored `cities15000.txt` or in `data_sources/geonames_supplement.txt`, since a manifest row is a join against vendored GeoNames data and the screen's frame, cities500, is not vendored — **30 dropped** (listed below);
4. its geometry resolved in the vetting run (below) — **3 dropped**;
5. greedily, GIS_ISG and UAS_ISG towns first and then by descending score, no row within 25 km of a row already kept — **1 dropped** (Sparks NV, 5.5 km from Reno);
6. those uploaders' towns ahead of a cap of 30 rows, the rest by score — the cap does not bind.

That leaves **12 rows**, in descending score, so `register_frame.py --limit N` registers the strongest first.
Re-running the script reproduces both committed files byte for byte (no network; the snapshot is gitignored on the laptop that ran the screen):

```bash
git show origin/mapillary-discovery-screen-383:docs/experiments/mapillary-discovery-screen_candidates.csv > /tmp/candidates.csv
git show origin/mapillary-discovery-screen-383:mapillary_discovery_cities.csv > /tmp/t1/mapillary_discovery_cities.csv
python scripts/build_mapillary_discovery_tranche2.py --candidates /tmp/candidates.csv \
    --catalog-snapshot experiments/mapillary-discovery-383/prod/prod_snapshot.csv \
    --also-registered /tmp/t1/mapillary_discovery_cities.csv
```

Each row is the scored place itself, at the point its 2 km disc was measured around, so no row is admitted on a neighbour's imagery (the defect #428's review found in a cluster-anchored selection).

**Fergus Falls is in by operator exception; the other GIS_ISG / UAS_ISG towns still wait.**
Laurens, Iowa's uploader and its sibling account have three candidates, and the rule alone drops all three.
Fergus Falls MN (5.38, GIS_ISG 100%, 67.6 km of recent 360° within 2 km) fails rule 2 only against `elizabeth--minnesota--united-states`, 11.5 km away.
Elizabeth's frozen grid is 1,624 x 812 m and Fergus Falls' vetted grid 11,551 x 8,879 m, so their half-diagonals (0.9 + 7.3 km) plus the vetted geocode's 1.4 km offset fall short of the distance and the rectangles cannot overlap: the reuse radius is a duplicate guard, and here the geometry proves there is no duplicate.
Registration uses `--overlap-km 5`, which admits it.
The exception waives rule 2 only (`OPERATOR_EXCEPTIONS` in the generator, pinned by the test with that geometry); "operator decision" is its whole provenance.
Fergus Falls is not in `cities15000.txt`, so its GeoNames line is copied verbatim from cities500 into `data_sources/geonames_supplement.txt`, which holds only the rows a committed manifest needs (`data_sources/README.md`).
Delavan Lake WI (4.08) is still 19.1 km from `clinton--wisconsin`, and Como WI (3.94) is still a cities500 place absent from the supplement; both wait.

**Needs a cities500 vendoring decision** — 30 candidates that pass every other rule but cannot be joined from `data_sources/`, because they are neither in `cities15000.txt` nor in the supplement (the screen downloaded cities500 on the day).
Each can be admitted the way Fergus Falls was, by copying its cities500 line into the supplement; vendoring all of cities500 (about 40 MB) is the alternative, and either is the owner's call.

| Place | geonameid | Score | Top uploader | Share | Population |
|---|---|---|---|---|---|
| Aptos, CA | 5324400 | 6.10 | marker_geo1 | 1.00 | 6,220 |
| Blende, CO | 5414264 | 3.55 | marker_geo1 | 1.00 | 878 |
| California, MD | 4350049 | 4.39 | stmaryscounty1 | 0.83 | 11,857 |
| Carmel-by-the-Sea, CA | 5334320 | 3.90 | pixelpete | 1.00 | 3,897 |
| Cedonia, MD | 4350831 | 3.17 | Rossitransportationgroup | 1.00 | 3,168 |
| Como, WI | 5249259 | 3.94 | GIS_ISG | 0.83 | 2,631 |
| Coolidge, AZ | 5290663 | 6.08 | rking | 1.00 | 12,297 |
| Eldorado, IL | 4237767 | 8.61 | Dale_Hat | 1.00 | 4,064 |
| Ephrata, WA | 5793832 | 5.92 | rking | 1.00 | 8,047 |
| Gilbert, MN | 5027943 | 3.10 | RS-EH-MAPR-1 | 1.00 | 1,792 |
| Hailey, ID | 5594956 | 4.94 | rking | 1.00 | 8,134 |
| Horizon West, FL | 7315230 | 5.54 | rking | 1.00 | 14,000 |
| Hudson, QC | 5978126 | 5.47 | zombiegraph | 1.00 | 5,088 |
| Kapa'a, HI | 5848280 | 3.01 | KauaiGIS | 1.00 | 10,699 |
| Lanare, CA | 5364937 | 3.10 | marker_geo1 | 1.00 | 589 |
| Leisure Knoll, NJ | 5100381 | 3.25 | DrivingRoundTown | 1.00 | 2,490 |
| Locust Grove, GA | 4206502 | 7.22 | CM-FDC | 1.00 | 5,790 |
| Mexia, TX | 4710963 | 6.59 | MEW-Utilities | 1.00 | 7,406 |
| Moyock, NC | 4481150 | 5.33 | vorpalblade | 1.00 | 3,759 |
| New Roads, LA | 4335096 | 3.21 | rking | 1.00 | 4,697 |
| North Auburn, CA | 5377266 | 4.00 | marker_geo1 | 1.00 | 13,022 |
| Rocky Mount, VA | 4782691 | 4.78 | rking | 1.00 | 4,799 |
| Shelter Cove, CA | 5571188 | 4.50 | marker_geo1 | 1.00 | 693 |
| Stacy, MN | 5048496 | 3.05 | quickness805 | 1.00 | 1,470 |
| Troy, NH | 5093821 | 4.36 | henryu | 1.00 | 1,221 |
| Webster City, IA | 4881096 | 3.10 | Hopen111 | 1.00 | 7,814 |
| West Pittston, PA | 5218853 | 4.43 | rking | 1.00 | 4,772 |
| White Marsh, MD | 4373426 | 4.22 | Rossitransportationgroup | 0.93 | 9,513 |
| Winters, CA | 5410125 | 3.79 | rking | 1.00 | 7,034 |
| Yreka, CA | 5574093 | 4.07 | marker_geo1 | 0.96 | 7,597 |

**Vetting (2026-10-04, from a laptop, one run of `vet_manifest_geometry.py` over the 14 rows the rule admitted before step 4).**
Fergus Falls was vetted in a second, single-row run on 2026-10-05, after its exception: the bare query matched the city's `boundary/administrative` polygon (not Otter Tail County), 1.4 km off, so it needs no override.
Three geocoded to the wrong feature and failed the 10 km center guard, so registration would skip them; with no second Nominatim run to test a fix, they are left out rather than given an untested override:

- **Elko, NV** matched Elko County, 32 km off; try `Elko, Elko County, Nevada, United States`.
- **Live Oak, CA** matched Live Oak in Sutter County, 257 km off; the scored place is the Santa Cruz County census-designated place, so try `Live Oak, Santa Cruz County, California, United States`.
- **Searcy, AR** matched Searcy County, 113 km off (the city is in White County); try `Searcy, White County, Arkansas, United States`.

The other 12 matched a `boundary/administrative` polygon, p50 1.65 km and max 5.5 km (Toms River's township) from their GeoNames point, so `--max-center-km 10` registers every row and `--center-from-geonames` is unnecessary.
Prices are for one collection: GSV and Mapillary are default-membership, so an enabled city is due on both the next night; the GSV walk is the scheduler's area proxy, an over-estimate by design; the Mapillary, KartaView and Panoramax walks read their grid run's census from the shared cache for 0 requests on a paired night (#290).

| # | City | Geocode query | OSM match | Grid W x H (m) | GSV points | GSV walk samples (area proxy) | Mapillary z14 tiles | KartaView requests | Panoramax z15 tiles | Offset (km) | Center | Flags |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Tracy | Tracy, California, United States | boundary/administrative | 14,870 x 13,082 | 487,320 | 96,616 | 64 | 198 | 240 | 4.0 | geocoded |  |
| 2 | Reno | Reno, Nevada, United States | boundary/administrative | 26,735 x 36,750 | 2,457,406 | 487,980 | 300 | 923 | 1,160 | 4.2 | geocoded |  |
| 3 | Elko |  |  |  |  |  |  |  |  |  |  | FAILED: 32 km off |
| 4 | Woodland | Woodland, California, United States | boundary/administrative | 10,976 x 7,980 | 219,600 | 43,502 | 35 | 86 | 108 | 3.0 | geocoded |  |
| 5 | Perris | Perris, California, United States | boundary/administrative | 7,690 x 17,333 | 333,795 | 66,201 | 45 | 140 | 162 | 1.3 | geocoded |  |
| 6 | Payson | Payson, Utah, United States | boundary/administrative | 10,999 x 8,066 | 222,200 | 44,063 | 35 | 86 | 130 | 1.9 | geocoded |  |
| 7 | Live Oak |  |  |  |  |  |  |  |  |  |  | FAILED: 257 km off |
| 8 | Searcy |  |  |  |  |  |  |  |  |  |  | FAILED: 113 km off |
| 9 | Galesburg | Galesburg, Illinois, United States | boundary/administrative | 11,752 x 10,107 | 297,528 | 58,992 | 48 | 129 | 168 | 0.1 | geocoded |  |
| 10 | Phoenixville | Phoenixville, Pennsylvania, United States | boundary/administrative | 4,057 x 4,821 | 49,126 | 9,714 | 9 | 21 | 30 | 1.1 | geocoded |  |
| 10a | Fergus Falls (second run) | Fergus Falls, Minnesota, United States | boundary/administrative | 11,551 x 8,879 | 256,632 | 50,938 | 56 | 113 | 180 | 1.4 | geocoded |  |
| 11 | Atwater | Atwater, California, United States | boundary/administrative | 6,395 x 5,215 | 83,520 | 16,563 | 20 | 36 | 48 | 1.3 | geocoded |  |
| 12 | Keene | Keene, New Hampshire, United States | boundary/administrative | 13,546 x 10,639 | 360,696 | 71,577 | 63 | 144 | 208 | 2.7 | geocoded |  |
| 13 | Buffalo | Buffalo, Minnesota, United States | boundary/administrative | 6,543 x 8,266 | 135,792 | 26,861 | 20 | 54 | 80 | 0.6 | geocoded |  |
| 14 | Toms River | Toms River, New Jersey, United States | boundary/administrative | 18,112 x 14,057 | 636,918 | 126,451 | 88 | 234 | 336 | 5.5 | geocoded |  |
| | **Total (12 resolved)** | | | | 5,540,533 | 1,099,458 | 783 | 2,164 | 2,850 | | | |

**Reno is the decision point.**
It qualifies by the rule (10.32, one uploader), but its score is the 2 km core of a 264,000-person city whose boundary is 26.7 x 36.8 km: it is 44% of the tranche's GSV points and 38% of its Mapillary tiles, and its walk will measure coverage over 982 km² from a sweep scored on 12.6.
The tranche's other eleven are towns of 13,000–89,000.
Dropping it is one row and one `EXPECTED_CITY_IDS` entry; the runbook enables it last, on its own night, so it can also simply be left disabled.

Nominatim can answer differently on the day of registration, so `register_frame.py --execute` prints each frozen W x H; compare it with this table before enabling anything, and treat a difference as a reason to stop.
A score ranks what to walk and never measures coverage (the screen's writeup, "Caveats"); the walk is the measurement.

## Refreshing the frame

Update the vendored GeoNames files (see `data_sources/README.md`) and/or
`gsv_coverage_regime.csv`, re-run the build, and **review the diff to
`worldwide_frame.csv` before re-registering** — a changed selection means new
frozen geometry, so only register genuinely new cities.
