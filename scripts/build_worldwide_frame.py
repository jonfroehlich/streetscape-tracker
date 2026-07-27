#!/usr/bin/env python3
"""
Build the worldwide city-sampling frame for Streetscape Tracker.

Produces a *stratified, curated* set of ~50-80 cities spanning
``continent x size-band x GSV-coverage-regime``, chosen deterministically from
vendored GeoNames data (see ``data_sources/``). This is the sampling frame for
comparing street-level imagery coverage/recency across countries and providers;
it augments (never replaces) the original US set.

Design (see ``docs/worldwide_sampling.md``):
  * Size bands come from reproducible population thresholds, not hand-picking:
    ``large`` >= LARGE_MIN, ``small`` in [SMALL_MIN, SMALL_MAX].
  * Within each inhabited continent we take the COUNTRIES_PER_CONTINENT most
    urban-significant *eligible* countries (those having both a large and a
    small qualifying city) and select one large + one small city from each.
  * Countries with limited/absent official Google Street View
    (``data_sources/gsv_coverage_regime.csv``) are force-included so the
    cross-provider (GSV-absent, Mapillary-present) gap is represented.

Everything is deterministic (population desc, then name, then geonameid — no
randomness), so re-running against the same inputs yields the identical frame.

Outputs (repo root, neither under ``data/``):
  * ``cities_worldwide.txt``    -- run_cities.py / streetscape_tracker.py query lines.
  * ``worldwide_frame.csv``     -- the selected frame, one row per city (manifest
                                   for the paper; consumed by register_frame.py).
  * ``worldwide_candidates.csv``-- the full ranked eligible-country pool, so a
                                   city that fails boundary vetting can be
                                   swapped for an alternate without re-deriving.

No network access; reads only the vendored files.

Usage:
    python scripts/build_worldwide_frame.py
    python scripts/build_worldwide_frame.py --countries-per-continent 6
"""

import argparse
import csv
import math
import os
import re
import sys
from collections import defaultdict, namedtuple

# --- Tunable selection parameters --------------------------------------------

LARGE_MIN = 1_000_000  # a "large" city: metro population at/above this
SMALL_MIN = 50_000  # a "small" city: population within this band
SMALL_MAX = 250_000
SMALL_TARGET = 100_000  # pick the small city nearest this population, so
# the small stratum is genuinely small (not just
# the top of the band, ~250k, every time)
MIN_SEPARATION_KM = 75  # the small city must be at least this far from
# the country's large pick, so we don't sample a
# borough/suburb of the same metropolitan area
COUNTRIES_PER_CONTINENT = 5  # most-urban eligible countries per continent
MIN_MAPILLARY_FIRST = 4  # sanity floor: cities from sparse/absent regimes

# GeoNames continent codes -> display names (inhabited only; AN excluded).
INHABITED_CONTINENTS = {
    "AF": "Africa",
    "AS": "Asia",
    "EU": "Europe",
    "NA": "North America",
    "SA": "South America",
    "OC": "Oceania",
}

# --- Data types --------------------------------------------------------------

City = namedtuple("City", "geonameid name iso2 admin1 population lat lon")
Country = namedtuple("Country", "name continent")  # continent = GeoNames code
FrameRecord = namedtuple("FrameRecord", "city iso2 country continent size_band regime")

Config = namedtuple(
    "Config",
    "large_min small_min small_max small_target min_separation_km "
    "countries_per_continent min_mapillary_first",
)

DEFAULT_CONFIG = Config(
    LARGE_MIN,
    SMALL_MIN,
    SMALL_MAX,
    SMALL_TARGET,
    MIN_SEPARATION_KM,
    COUNTRIES_PER_CONTINENT,
    MIN_MAPILLARY_FIRST,
)

# --- Loaders (thin, file-format specific) ------------------------------------

# GeoNames "geoname" table column indices (tab-separated, no header).
_CITY_ASCIINAME = 2
_CITY_LAT = 4
_CITY_LON = 5
_CITY_FEATURE_CLASS = 6
_CITY_COUNTRY = 8
_CITY_ADMIN1 = 10
_CITY_POPULATION = 14


def load_cities(path):
    """Parse GeoNames cities15000.txt into a list of City (feature class P)."""
    cities = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) <= _CITY_POPULATION:
                continue
            if cols[_CITY_FEATURE_CLASS] != "P":  # populated places only
                continue
            try:
                population = int(cols[_CITY_POPULATION])
            except ValueError:
                continue
            try:
                lat, lon = float(cols[_CITY_LAT]), float(cols[_CITY_LON])
            except ValueError:
                continue
            cities.append(
                City(
                    geonameid=cols[0],
                    name=cols[_CITY_ASCIINAME],
                    iso2=cols[_CITY_COUNTRY],
                    admin1=cols[_CITY_ADMIN1],
                    population=population,
                    lat=lat,
                    lon=lon,
                )
            )
    return cities


def load_countries(path):
    """Parse GeoNames countryInfo.txt into {iso2: Country(name, continent)}."""
    countries = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 9 or not cols[0]:
                continue
            countries[cols[0]] = Country(name=cols[4], continent=cols[8])
    return countries


def load_admin1(path):
    """
    Parse admin1CodesASCII.txt into {"<iso2>.<admin1code>": admin_name}.

    Uses the ASCII column (col 2), not the localized name (col 1), so admin
    names feeding city_ids/filenames/URLs stay ASCII and consistent with the
    existing (US) dataset — e.g. "Sant Julia de Loria", not "Sant Julià...".
    """
    admin = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 3:
                continue
            admin[cols[0]] = cols[2] or cols[1]
    return admin


def load_coverage(path):
    """Parse gsv_coverage_regime.csv into {iso2: regime}; skips # comments."""
    coverage = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(row for row in f if not row.startswith("#"))
        for row in reader:
            coverage[row["iso2"].strip()] = row["regime"].strip()
    return coverage


# --- Selection (pure logic; unit-tested with synthetic data) -----------------


def _size_band(population, config):
    if population >= config.large_min:
        return "large"
    if config.small_min <= population <= config.small_max:
        return "small"
    return None


def _haversine_km(a, b):
    """Great-circle distance in km between two cities (from their lat/lon)."""
    r = 6371.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _pick_large(candidates):
    """The country's primary city: largest population (then name, geonameid)."""
    return sorted(candidates, key=lambda c: (-c.population, c.name, c.geonameid))[0]


def _pick_small(large_pick, candidates, config):
    """
    A representative *small* city that is a distinct settlement, not a borough
    of the primary city: prefer candidates at least ``min_separation_km`` from
    the large pick (fall back to the farthest available if none qualify), then
    the one whose population is nearest ``small_target`` (then name, geonameid).
    """
    far = [c for c in candidates if _haversine_km(large_pick, c) >= config.min_separation_km]
    pool = far or [max(candidates, key=lambda c: _haversine_km(large_pick, c))]
    return sorted(
        pool, key=lambda c: (abs(c.population - config.small_target), c.name, c.geonameid)
    )[0]


def eligible_countries(cities, countries, config):
    """
    {iso2: (large_pick, small_pick)} for every country (in an inhabited
    continent) that has at least one qualifying large *and* one small city.
    """
    large = defaultdict(list)
    small = defaultdict(list)
    for c in cities:
        country = countries.get(c.iso2)
        if country is None or country.continent not in INHABITED_CONTINENTS:
            continue
        band = _size_band(c.population, config)
        if band == "large":
            large[c.iso2].append(c)
        elif band == "small":
            small[c.iso2].append(c)

    eligible = {}
    for iso2 in set(large) & set(small):
        large_pick = _pick_large(large[iso2])
        eligible[iso2] = (large_pick, _pick_small(large_pick, small[iso2], config))
    return eligible


def select_frame(cities, countries, coverage, config=DEFAULT_CONFIG):
    """
    Return the selected FrameRecords (deterministic).

    Top COUNTRIES_PER_CONTINENT eligible countries per continent (ranked by
    large-pick population), plus every eligible country whose coverage regime is
    not "present" (force-included for the cross-provider story), one large + one
    small city each.
    """
    eligible = eligible_countries(cities, countries, config)

    by_continent = defaultdict(list)
    for iso2 in eligible:
        by_continent[countries[iso2].continent].append(iso2)

    selected = []
    for continent in sorted(INHABITED_CONTINENTS):
        ranked = sorted(
            by_continent.get(continent, []), key=lambda i: (-eligible[i][0].population, i)
        )
        selected.extend(ranked[: config.countries_per_continent])

    for iso2 in sorted(eligible):
        if coverage.get(iso2, "present") != "present" and iso2 not in selected:
            selected.append(iso2)

    records = []
    for iso2 in selected:
        large_pick, small_pick = eligible[iso2]
        continent = INHABITED_CONTINENTS[countries[iso2].continent]
        regime = coverage.get(iso2, "present")
        for city, band in ((large_pick, "large"), (small_pick, "small")):
            records.append(
                FrameRecord(
                    city=city,
                    iso2=iso2,
                    country=countries[iso2].name,
                    continent=continent,
                    size_band=band,
                    regime=regime,
                )
            )

    records.sort(key=lambda r: (r.continent, r.iso2, r.size_band))
    return records


def effective_admin(city_name, admin_name):
    """
    The admin-1 name to use in queries and catalog identity, or None when it
    would just duplicate the city name.

    Strips a trailing parenthetical (GeoNames has e.g. "Ho Chi Minh City
    (HCMC)") and drops the admin entirely when what remains equals or starts
    with the city name — otherwise city-state-like slugs pick up a redundant
    (and parenthesized) middle component. Shared with scripts/register_frame.py
    so query strings and registered identity always agree.
    """
    if not admin_name:
        return None
    admin = re.sub(r"\s*\([^)]*\)$", "", admin_name).strip()
    if not admin or admin.lower().startswith(city_name.lower()):
        return None
    return admin


def query_string(record, admin_names):
    """
    A Nominatim-friendly structured query: "City, Admin, Country", falling back
    to "City, Country" when the admin-1 name is missing or duplicates the city.
    """
    city = record.city.name
    admin = effective_admin(city, admin_names.get(f"{record.iso2}.{record.city.admin1}"))
    if admin:
        return f"{city}, {admin}, {record.country}"
    return f"{city}, {record.country}"


# --- Output ------------------------------------------------------------------

_MANIFEST_HEADER = [
    "query_string",
    "city",
    "admin",
    "iso2",
    "country",
    "continent",
    "size_band",
    "population",
    "coverage_regime",
    "geonameid",
    "lat",
    "lon",
]


def _admin_name(record, admin_names):
    """The city's admin-1 (state/province) ASCII name, or '' if unavailable."""
    return admin_names.get(f"{record.iso2}.{record.city.admin1}", "")


def _manifest_row(record, admin_names):
    return {
        "query_string": query_string(record, admin_names),
        "city": record.city.name,
        "admin": _admin_name(record, admin_names),
        "iso2": record.iso2,
        "country": record.country,
        "continent": record.continent,
        "size_band": record.size_band,
        "population": record.city.population,
        "coverage_regime": record.regime,
        "geonameid": record.city.geonameid,
        "lat": record.city.lat,
        "lon": record.city.lon,
    }


def write_manifest(path, records, admin_names):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_MANIFEST_HEADER)
        writer.writeheader()
        for record in records:
            writer.writerow(_manifest_row(record, admin_names))


def write_cities_txt(path, records, admin_names):
    """Emit run_cities.py-compatible query lines, grouped by continent.

    Queries are double-quoted so names containing apostrophes (e.g. N'Djamena)
    survive run_cities.py's shlex parsing.
    """
    lines = [
        "# Worldwide city-sampling frame (generated by",
        "# scripts/build_worldwide_frame.py). Do not hand-edit; edit the build",
        "# inputs in data_sources/ and regenerate. See docs/worldwide_sampling.md.",
        "#",
        "# Each line is one city query (double-quoted). Cities must be registered",
        "# and their grids boundary-vetted (scripts/register_frame.py + the",
        "# boundary-audit workflow) before enabling in the scheduler.",
        "",
    ]
    current = None
    for record in records:
        if record.continent != current:
            current = record.continent
            lines.append(f"# --- {current} ---")
        q = query_string(record, admin_names)
        tag = f"  # {record.size_band}, {record.regime}"
        lines.append(f'"{q}"{tag}')
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_candidates(path, cities, countries, coverage, admin_names, config):
    """Full ranked eligible-country pool (large+small picks) for swap-ins."""
    eligible = eligible_countries(cities, countries, config)
    rows = []
    for iso2, (large_pick, small_pick) in eligible.items():
        country = countries[iso2]
        continent = INHABITED_CONTINENTS[country.continent]
        regime = coverage.get(iso2, "present")
        for city, band in ((large_pick, "large"), (small_pick, "small")):
            rows.append(FrameRecord(city, iso2, country.name, continent, band, regime))
    # rank by continent, then country urban weight (large pop desc), then band
    rank = {r.iso2: eligible[r.iso2][0].population for r in rows}
    rows.sort(key=lambda r: (r.continent, -rank[r.iso2], r.iso2, r.size_band))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_MANIFEST_HEADER)
        writer.writeheader()
        for record in rows:
            writer.writerow(_manifest_row(record, admin_names))


# --- CLI ---------------------------------------------------------------------


def _default(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--cities", default=_default("data_sources", "cities15000.txt"))
    p.add_argument("--countries", default=_default("data_sources", "countryInfo.txt"))
    p.add_argument("--admin1", default=_default("data_sources", "admin1CodesASCII.txt"))
    p.add_argument("--coverage", default=_default("data_sources", "gsv_coverage_regime.csv"))
    p.add_argument("--out-txt", default=_default("cities_worldwide.txt"))
    p.add_argument("--out-manifest", default=_default("worldwide_frame.csv"))
    p.add_argument("--out-candidates", default=_default("worldwide_candidates.csv"))
    p.add_argument("--countries-per-continent", type=int, default=COUNTRIES_PER_CONTINENT)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = DEFAULT_CONFIG._replace(countries_per_continent=args.countries_per_continent)

    cities = load_cities(args.cities)
    countries = load_countries(args.countries)
    admin_names = load_admin1(args.admin1)
    coverage = load_coverage(args.coverage)

    records = select_frame(cities, countries, coverage, config)

    write_cities_txt(args.out_txt, records, admin_names)
    write_manifest(args.out_manifest, records, admin_names)
    write_candidates(args.out_candidates, cities, countries, coverage, admin_names, config)

    # Summary to stdout
    by_continent = defaultdict(int)
    mapillary_first = 0
    for r in records:
        by_continent[r.continent] += 1
        if r.regime != "present":
            mapillary_first += 1
    print(f"Selected {len(records)} cities across {len(by_continent)} continents:")
    for continent in sorted(by_continent):
        print(f"  {continent:<15} {by_continent[continent]}")
    print(f"  {'(sparse/absent GSV)':<15} {mapillary_first}")
    print(f"\nWrote:\n  {args.out_txt}\n  {args.out_manifest}\n  {args.out_candidates}")

    if mapillary_first < config.min_mapillary_first:
        print(
            f"\nWARNING: only {mapillary_first} sparse/absent-regime cities "
            f"(< MIN_MAPILLARY_FIRST={config.min_mapillary_first}); "
            f"check data_sources/gsv_coverage_regime.csv.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
