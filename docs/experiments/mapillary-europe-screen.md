# A Mapillary discovery screen of Europe

Measured 2026-11-25 from a laptop.
The question: which European places outside the catalog hold fresh, systematic 360° Mapillary capture, and does the score that selected the US tranches carry over?

Every number below traces to [`mapillary-europe-screen_metrics.json`](mapillary-europe-screen_metrics.json) (`generated_by`: `scripts/mapillary_discovery_score.py`, its exact arguments in `_about`), to the ranked record [`mapillary-europe-screen_ranked.csv`](mapillary-europe-screen_ranked.csv) or to the per-city validation rows in [`mapillary-europe-screen_validation.csv`](mapillary-europe-screen_validation.csv).
The instrument, the score and their US validation are in [`mapillary-discovery-screen.md`](mapillary-discovery-screen.md); this writeup only says what changed and what Europe returned.

## Decision

- **176 uncatalogued European places pass every rule of the tranche-3 selector** (`funnel.after_thinning`), from 96,767 GeoNames places scored.
- **The score is NOT validated in Europe, and this run cannot validate it.** Of the 35 catalog cities in the scanned tiles, none meets the strict bar on its walk, so there is no positive to rank; the rank agreement with walk coverage is ρ = 0.695 on n = 35.
- **A first European tranche is therefore the measurement**, not a deployment list. Nothing was registered or enabled.

## What was run, and what it cost

One region, `europe` = (-11.0, 34.5, 32.0, 71.5): 104 z6 tiles.
Iceland, the Canaries, Cyprus and everything east of 32°E were not scanned.

Per CLAUDE.md the Mapillary API documentation and forum were re-read first: the documented tile limit is unchanged at 50,000 per day per app, and the [forum thread on that limit's scope](https://forum.mapillary.com/t/50-000-requests-day-rate-limit-scope/10644) had no post after 2026-08-28.
Requests were serial at the production channels' jittered 40/min, with the stop-at-first-refusal rule, from a residential address that production does not use, on a separate free app's token (`collection.token_vars`).

**All 198 requests returned 200** (`collection.by_host_and_status`): 112 tile requests (104 z6 for the scan and 8 z10 for calibration) and 86 Graph API lookups 3 s apart.
The tiles total 415 MB and the largest is 20.7 MB (`collection.tile_bytes`, `collection.largest_tile_bytes`).
The scan holds 3,570,520 sequence rows, of which the 360° ones sample to 4,258,699 points over 693,115 km (`samples`).

## What changed in the tooling

`collect scan` holds every sequence in memory, which Europe's density made a risk, and the score that passed the tranche-3 test lived only in the frozen `scripts/mapillary_discovery_rescore/` scripts, hardcoded to the 2026-10-02 scan.
So `collect fetch` caches tiles without building `sequences.parquet`, and `scripts/mapillary_discovery_score.py` reads that cache one tile at a time and takes the scan date, the population cap and the countries as arguments.
The score is unchanged: V4, the age-weighted share of occupied 250 m cells, best of the 1, 2 and 3 km discs.

### Parity

Before any European request, the new scorer was run over the cached 2026-10-02 US tiles and compared with the committed [`mapillary-discovery-screen_tranche3_ranked.csv`](mapillary-discovery-screen_tranche3_ranked.csv).
All 105 rows matched on V0, V1, V4, top-uploader share, on-foot share, median age, newest age and catalog distance to within 0.0005, which is that record's three-decimal rounding, and on uploader and uploader count exactly.
That comparison's inputs are gitignored, so it is recorded here and not pinned by a test.
The new ranking differs from the old in two deliberate ways: it orders by V4 where the old one ordered by V5, and it admits a place by its TILE where the old one used a longitude box (which is why New Glasgow, NS appears in a US re-run and did not before).

## Calibration: z6 keeps Europe's dense tiles

The US calibration was made around small towns, so it could not say whether z6 drops sequences in a dense European city centre.
Recent 360° sequences within 3 km of eight city centres, at z6 against z10 (`calibration.towns`):

| City | z6 | z10 | z6 / z10 | Same top uploader |
|---|---|---|---|---|
| Brussels | 1,485 | 1,600 | 0.928 | yes |
| Antwerp | 586 | 620 | 0.945 | yes |
| Braga | 34 | 28 | 1.214 | no |
| Paris | 0 | 0 | n/a | n/a |
| Amsterdam | 180 | 170 | 1.059 | no |
| Budapest | 1,397 | 1,384 | 1.009 | yes |
| Belgrade | 24 | 28 | 0.857 | yes |
| Zurich | 44 | 51 | 0.863 | yes |

The two tiles cover different ground (a z10 tile is smaller than the disc), so a ratio above 1 is expected and only a low one would be a finding.
The lowest is 0.857; the densest centres, Brussels and Budapest, keep 0.928 and 1.009.

## The score against Europe's walked cities

V4 over all 96,767 scored places is p50 0.0, p90 0.1641, p99 0.6018, p99.9 1.0693 (`v4_of_scored_places`), so the 0.7 cut is above the 99th percentile here as it was in the US.

35 catalog cities sit in the scanned tiles and all have a Mapillary drive walk (`validation.catalog_cities_in_scanned_tiles`, `validation.walked_catalog_cities_in_scanned_tiles`).
**0 of them meet the strict bar** (≥ 50% of street-km in 360° at a median age ≤ 2 years), so average precision is undefined and the only agreement measure is rank correlation: ρ(V4, walk coverage) = 0.695, ρ(V0, walk coverage) = 0.637.
The 7 at V4 ≥ 0.7:

| City | V4 | Walk coverage (360°) | Median age (yr) |
|---|---|---|---|
| Brussels | 1.19 | 85.2% | 2.87 |
| Milan | 1.17 | 34.9% | 1.48 |
| Berlin | 1.03 | 70.1% | 3.62 |
| Vilnius | 0.97 | 62.4% | 2.27 |
| Mechelen | 0.82 | 48.3% | 2.22 |
| Belgrade | 0.81 | 24.2% | 1.92 |
| Amsterdam | 0.74 | 80.0% | 10.00 |

4 of the 7 reach 50% coverage, and each of those misses on age.
Two readings, both of which a tranche would test:

- **These are large cities, and the score reads only a 3 km core.** Antwerp scores 0.20 at its centre and walked at 58.5%; the Belgium screen found its sweep in the surrounding districts. A catalog city's walk covers a grid tens of kilometres wide, so the two are measuring different ground.
- **The catalog's European cities were not chosen for Mapillary.** Most are Project Sidewalk deployments and worldwide-frame capitals, a population where old imagery is the norm, unlike the US validation set that two earlier 360° searches had enriched.

## Ranked places

The rules are tranche 3's (`rules`): V4 ≥ 0.7, top-uploader share ≥ 0.6, on-foot share < 0.5, median age ≤ 2 years, outside every catalog grid and more than 5 km from every catalog centre, thinned at 5 km.
**There is no population cap and no country filter**, unlike tranche 3; population is a column.

The funnel (`funnel`): 642 places at V4 ≥ 0.7, 232 of them inside a catalog grid already, 410 open, 271 passing every rule, 176 after thinning.
By country (`ranked_by_country`): DE 37, BE 29, FR 20, AT 17, PL 12, GB 10, IT 9, GR 6, and 19 more countries with fewer.
Population of the ranked places (`ranked_population`): p10 934, p25 4,134, p50 11,070, p75 31,952, p90 79,639.

The first fifteen by V4:

| Place | Country | Population | Top uploader | V4 | Median age (yr) | Top share |
|---|---|---|---|---|---|---|
| Lievendaal | NL | 3,175 | toho | 1.17 | 0.23 | 0.93 |
| Joensuu | FI | 78,398 | joensuucity360 | 1.16 | 1.46 | 1.00 |
| Cirie | IT | 17,439 | canfe | 1.16 | 1.59 | 1.00 |
| Wilten | AT | 18,142 | european-panorama | 1.15 | 1.17 | 0.99 |
| Budakalasz | HU | 1,003 | grozsa11 | 1.15 | 0.42 | 0.93 |
| Ballerup | DK | 40,000 | neogeografen | 1.14 | 0.69 | 0.61 |
| Szolnok | HU | 71,285 | osminng | 1.11 | 1.29 | 0.75 |
| Sankt Johann in Tirol | AT | 9,428 | european-panorama | 1.11 | 0.83 | 1.00 |
| Loerrach | DE | 47,002 | fmc_Mlvln | 1.11 | 0.17 | 0.84 |
| Plaisance-du-Touch | FR | 18,501 | sogefi | 1.11 | 0.22 | 1.00 |
| Marupe | LV | 19,096 | ursus | 1.09 | 1.04 | 0.91 |
| Rixensart | BE | 21,616 | usagersactifsrixensart | 1.09 | 1.72 | 0.97 |
| Homyel' | BY | 501,193 | 360-m5-by | 1.09 | 1.42 | 1.00 |
| Wattens | AT | 7,881 | european-panorama | 1.07 | 1.11 | 1.00 |
| Boom | BE | 15,810 | konink360 | 1.06 | 1.76 | 0.77 |

The 14 ranked places of 100,000 people or more, which a 60,000 cap would have removed:

| Place | Country | Population | Top uploader | V4 | Median age (yr) | On-foot share |
|---|---|---|---|---|---|---|
| Homyel' | BY | 501,193 | 360-m5-by | 1.09 | 1.42 | 0.03 |
| Hoofddorp | NL | 132,734 | Lowiekse | 1.03 | 0.26 | 0.18 |
| Salzburg | AT | 157,245 | OidaHeast | 1.03 | 1.73 | 0.07 |
| Heilbronn | DE | 120,733 | changchun1 | 0.88 | 0.15 | 0.03 |
| Koeln | DE | 1,024,621 | popball | 0.86 | 0.29 | 0.13 |
| Vigo | ES | 293,642 | EAPI | 0.86 | 0.41 | 0.02 |
| Swindon | GB | 201,669 | swinetown | 0.82 | 0.16 | 0.00 |
| Krakow | PL | 816,614 | awizonosz | 0.80 | 0.49 | 0.16 |
| Nuremberg | DE | 515,543 | ZXStreetview | 0.79 | 1.13 | 0.00 |
| Freiburg | DE | 237,460 | RicardaH | 0.79 | 0.78 | 0.03 |
| Chisinau | MD | 635,994 | Sku1255 | 0.78 | 1.16 | 0.07 |
| Aberdeen | GB | 198,590 | coetserw | 0.75 | 0.21 | 0.09 |
| Livorno | IT | 157,017 | momix | 0.73 | 0.77 | 0.15 |
| Bristol | GB | 479,024 | southglos | 0.71 | 0.31 | 0.25 |

A large place is scored on its core only, so a row here says the centre is densely and recently covered, not that the city is.

## Uploaders

The 176 ranked places have 86 distinct top uploaders, 27 of whom top at least two; the ten with the most (`ranked_by_uploader`):

| Uploader | Ranked places | Countries | Examples |
|---|---|---|---|
| `sogefi` | 15 | FR | Plaisance-du-Touch, Leguevin, La Teste-de-Buch |
| `european-panorama` | 14 | AT, DE | Wilten, Sankt Johann in Tirol, Wattens |
| `Eebie` | 8 | BE | Sint-Truiden, Diest, Tongeren |
| `konink360` | 7 | BE | Boom, Kontich, Duffel |
| `Planungsgesellschaft_RV-K` | 7 | DE | Bad Homburg vor der Hoehe, Kronberg, Floersheim |
| `canfe` | 6 | IT | Cirie, Caselle Torinese, Mathi |
| `usagersactifsrixensart` | 5 | BE | Rixensart, Couture-Saint-Germain, Ottignies |
| `Mobilitaetswerk` | 5 | DE | Pruem, Spremberg, Eisenach |
| `Oderradler` | 5 | DE | Seelow, Frankfurt (Oder), Jamlitz |
| `padididejurbise` | 5 | BE | Strepy-Bracquegnies, Saint-Ghislain, Havre |

In the US the uploader was the unit that generalized, and the number of towns an uploader holds did not predict how they walk.
No uploader record is applied to this ranking, because no European uploader has a strict hit to its name.
**Six ranked uploaders do have a record, and it is 0 strict hits in 8 walked catalog cities** (`ranked_uploader_record`; an uploader's cities are the walked ones where it holds at least half the weighted 360° length, the `user` column of the validation record):

| Uploader | Ranked places | Walked cities it dominates (street-km covered, median age) |
|---|---|---|
| `european-panorama` | 14 | Munich (24.7%, 1.23 yr) |
| `Eebie` | 8 | Beringen (48.2%, 2.09 yr) |
| `konink360` | 7 | Mechelen (48.3%, 2.22 yr), Antwerp (58.5%, 2.21 yr) |
| `southglos` | 2 | Lisbon (27.9%, 2.45 yr), Bath (6.6%, 0.07 yr) |
| `erias` | 1 | Vienna (9.0%, 0.99 yr) |
| `sanjorgepinho` | 1 | Porto (21.0%, 1.94 yr) |

Those 33 ranked places are the ones with evidence against them.
Much of it is weak: Munich, Vienna, Lisbon, Porto and Antwerp are large cities scored on a 3 km core, Antwerp misses on age alone, and Mechelen and Beringen miss both coverage and age by small margins.
Bath is the exception, a small city that misses coverage outright.
Uploader identities are what the Graph API returns for one sample image each; nothing here establishes who an account belongs to.

## First tranche

The screen has no validated positive in Europe, so a first tranche is the measurement.
[`mapillary_discovery_cities_europe1.csv`](../../mapillary_discovery_cities_europe1.csv) is written by `scripts/build_mapillary_discovery_europe1.py` from the ranked record above, and its record of what the selection knew is [`mapillary-europe-screen_tranche1_metrics.json`](mapillary-europe-screen_tranche1_metrics.json).
Nothing is registered by this writeup; registering is the runbook in [`operations.md`](../operations.md) ("Registering the Mapillary discovery screen's first European tranche").

It has two arms:

- **Small (20 places, the arm the prediction covers).** The first 20 places under 100,000 people in V4 order, at most two per uploader. This is the kind of place the score was tested on in the US. The 20 come from 16 uploaders and 10 countries.
- **Large (5 places, exploratory).** The first five of 100,000 people or more: Gomel, Hoofddorp, Salzburg, Heilbronn and Cologne. A large city is scored on a 3 km core only, and nothing says how that carries to its whole grid, so no prediction is made for them.

Four of the small arm's places belong to uploaders with a record of 0 strict hits (`small_arm_with_an_uploader_record`): Wilten and Sankt Johann in Tirol (`european-panorama`), Boom and Kontich (`konink360`).
They were kept, because whether that record matters is one of the things the tranche can show.

### Advance prediction

Written before any of the 25 was registered (`advance_prediction_small_arm`).
The rate is tranche 3's outcome, 20 of 25 strict hits (0.80), all 25 of them US towns.
Tranches 1 and 2 are left out: the V4 ≥ 0.7 threshold was read off their own walks, so their 20 of 23 is in-sample, and tranche 3 is the one test made after the threshold was fixed.
At that rate 20 places give 16.0 expected strict hits, and "the US rate carries to small European places" is **falsified below 13 strict hits** on the small arm's first Mapillary drive walks (the largest count whose lower tail is at most 5%; the tail below 13 is 0.032).
Strict is the same bar as everywhere here: at least 50% of street-km covered by 360° imagery at a median age of at most two years.
Four of the 20 already sit at a screen median age above 1.7 years (Rixensart, Boom, Kontich and Sint-Amandsberg), and both of tranche 3's age misses were places like that.

### Vetting

`scripts/vet_manifest_geometry.py` geocoded every row on 2026-11-25 ([`_tranche1_vet.csv`](mapillary-europe-screen_tranche1_vet.csv), re-vetted rows in [`_tranche1_vet_retry.csv`](mapillary-europe-screen_tranche1_vet_retry.csv)).
Four rows needed a query override, each re-vetted before being committed:

| Place | First pass | Override | Grid after |
|---|---|---|---|
| Joensuu | the 2,381 km² municipality's midpoint, 31 km off: refused | `Keskusta, Joensuu, Finland` (the centre district) | 2,054 x 4,458 m |
| Gomel | a railway station, 680 x 1,112 m | `Gomel, Belarus` | 28,861 x 25,296 m |
| Budakalasz | the county name matched nothing; the bare fallback won | `Budakalasz, Hungary` | unchanged |
| Salzburg | likewise for "State of Salzburg" | `Salzburg, Austria` | unchanged |

Three names depart from GeoNames, each asserted against the vendored value by the generator: Cologne (ASCII name `Koeln`) and Gomel (`Homyel'`), Gomel's region (`Homyel' Voblasc'`, written `Gomel Oblast`; left as GeoNames spells it, its apostrophes would reach the city id, and respelled it repeats the city's name, so identity drops it and the id is `gomel--belarus`), and the country of the two Dutch rows (`Netherlands`, as the catalog's one Dutch city is registered, not GeoNames' `The Netherlands`).
Three rows geocode to a district rather than a municipality and are registered as the district: Lievendaal (a neighbourhood of Eindhoven), Wilten (a suburb of Innsbruck) and Joensuu's centre.
The runbook restores each to its vetted grid after the boundary audit, which could otherwise grow it to the parent city.
Sint-Amandsberg is a district of Ghent too, but geocodes to its own administrative boundary.

One collection of all 25 is 7,654,613 GSV grid points and 1,553 Mapillary z14 tiles: 610 tiles for the 20 small places and 943 for the five large ones.
Cologne and Gomel are 360 tiles each.

From [`_tranche1_overlaps.json`](mapillary-europe-screen_tranche1_overlaps.json) (`generated_by`: `scripts/mapillary_discovery_europe1_overlaps.py`, against the 2026-11-25 production snapshot of 1,307 cities):

- No row's GeoNames point is within 8.8 km of a catalog centre (Boom, 8.877 km from Mechelen), and the nearest in-batch pair is Fonsorbes, 5.116 km from Plaisance-du-Touch's vetted centre.
- Three vetted grids share ground with a tracked neighbour: 23.5% of Budakalasz's grid lies inside Budapest's, 13.0% of Kontich's inside Antwerp's and 6.1% of Hoofddorp's inside Amsterdam's. Each place's point is outside the neighbour's grid, which is what the screen required; the municipal rectangle is wider than the point.

## Caveats

- **Screening signal, never coverage.** z6 geometry is simplified and length says nothing about which streets were driven. A walk is the only measurement.
- **The 0.7 threshold is untested here.** It held once, in the US, on 25 towns of at most 60,000 people. Europe's ranked places include cities fifteen times that size and uploaders it has never seen.
- **GeoNames `cities500` lists districts and suburbs as places.** Wilten is a district of Innsbruck, Sint-Amandsberg of Ghent, Falchera of Turin; the 5 km thinning removes some of these duplicates and not all. A row is a point, and the municipality to register is a decision made at vetting.
- **A population of 0 is GeoNames' missing value**, not a village.
- **Catalog distance is to a grid centre.** Places 7 to 20 km from Brussels or Antwerp (Boom, Kontich, Duffel, Rixensart) are outside those grids and may still be the same sweep the Belgium screen described.
- **The prod snapshot is the catalog as of 2026-11-25** (`makelab2`, commit `9cd70ec`), exported read-only.

## Replicate

```bash
# raw outputs go to gitignored experiments/, never data/
E=experiments/mapillary-discovery-europe
python scripts/mapillary_discovery_collect.py fetch --out-dir $E --regions europe --dry-run   # 104 tiles, no request
python scripts/mapillary_discovery_collect.py fetch --out-dir $E --regions europe --max-requests 110
python scripts/mapillary_discovery_collect.py calibrate --out-dir $E --max-requests 10 --zooms 6,10 --recent-since 2024-11-25 \
    --town brussels=50.8466,4.3528 --town antwerp=51.2194,4.4025 --town braga=41.5454,-8.4265 --town paris=48.8566,2.3522 \
    --town amsterdam=52.3676,4.9041 --town budapest=47.4979,19.0402 --town belgrade=44.7866,20.4489 --town zurich=47.3769,8.5417
# read-only catalog export, run on production and captured locally
python scripts/mapillary_discovery_collect.py catalog-snapshot --db data/streetscape_tracker.db --out /dev/stdout > prod_snapshot.csv
python scripts/mapillary_discovery_score.py ...   # exact arguments: metrics _about.generated_by (first without --creators)
python scripts/mapillary_discovery_collect.py resolve-creators --out-dir $E --max-requests 130 --ids-from $E/score/places_ranked.csv:user:176
python scripts/mapillary_discovery_score.py ...   # again, to attach usernames
```

`cities500.txt` is the GeoNames download already used by the US screen.
A re-run with every tile cached makes zero requests.

Related: `mapillary-discovery-screen.md` (the instrument, the US validation, the tranche-3 prediction and its outcome), `belgium-screen.md` (the one earlier European screen), `mapillary-user-activity.md` (per-uploader enumeration).
