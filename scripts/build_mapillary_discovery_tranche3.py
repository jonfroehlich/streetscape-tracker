#!/usr/bin/env python3
"""
Select the third registration tranche of the Mapillary discovery screen (#441)
and write it as a ``register_frame.py`` manifest.

    python scripts/build_mapillary_discovery_tranche3.py

WHAT
----
The 2026-11-05 re-score (``scripts/mapillary_discovery_rescore/``, written up
in ``docs/experiments/mapillary-discovery-screen.md``, "Tranche 3") ranked 105
uncatalogued GeoNames places by V5 (age-weighted occupied cells times the top
uploader's shrunk walk record) and wrote them, in that order, to the committed
``docs/experiments/mapillary-discovery-screen_tranche3_ranked.csv``. Every row
there already passed the re-score's eligibility rules (V4 >= 0.7, top uploader
share >= 0.6, on-foot share < 0.5, median age <= 2 yr, population <= 60,000,
US or Canada, outside every catalog grid and > 5 km from its centre, and at
least 5 km from every higher-ranked row). This script applies the tranche's
selection rule to that record:

1. walk the record in order (V5 descending);
2. skip a row whose uploader already has ``PER_UPLOADER_CAP`` rows kept, so
   one prolific uploader cannot fill the tranche (marker_geo1 has 35 open
   towns, rking 28);
3. stop at ``CAP`` rows.

Each kept row is joined to vendored GeoNames by geonameid (cities15000 plus
``TRANCHE3_SUPPLEMENT_IDS``, this tranche's rows of the shared
``data_sources/geonames_supplement.txt``). ``QUERY_OVERRIDES`` replaces the
generated geocode query for the two rows whose first vetting run matched the
wrong place; the overrides were re-vetted before being committed. Identity
(and so the frozen ``city_id``) still comes from the GeoNames columns.

``GRID_FITS`` is not used to write the manifest: registration derives each
grid from the geocoded boundary, and three of those boundaries are mostly
empty ground. It is the geometry the runbook applies with
``scripts/resize_city.py`` AFTER the boundary audit (the audit would grow a
hand-sized grid back to the OSM bbox), and the test holds it to the committed
fit record.

No network. Reads committed files only, so re-running it reproduces the
committed manifest byte for byte.
"""

import argparse
import csv
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_worldwide_frame import (  # noqa: E402
    _MANIFEST_HEADER,
    load_admin1,
    load_cities,
    load_countries,
    query_string,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_SOURCES = os.path.join(REPO_ROOT, "data_sources")
RANKED = os.path.join(
    REPO_ROOT, "docs", "experiments", "mapillary-discovery-screen_tranche3_ranked.csv"
)

CAP = 25
PER_UPLOADER_CAP = 5

# The 14 supplement rows this tranche may join (cities500-only towns). The
# supplement is shared by every purposive manifest, so the joinable set is
# pinned rather than read whole.
TRANCHE3_SUPPLEMENT_IDS = frozenset(
    {
        "4162783",  # Longwood, FL
        "4385835",  # Eureka, MO
        "4855806",  # Evansdale, IA
        "5250399",  # Delavan, WI
        "5266483",  # Pell Lake, WI
        "5334320",  # Carmel-by-the-Sea, CA
        "5375938",  # Muscoy, CA
        "5398563",  # Stanford, CA
        "5582392",  # Sterling, CO
        "5784731",  # White City, UT
        "5843495",  # Worland, WY
        "5850511",  # Makaha, HI
        "5856516",  # Ahuimanu, HI
        "7262791",  # Royal Kunia, HI
    }
)

# GeoNames' ASCII name keeps the Hawaiian 'okina as a leading apostrophe;
# tranche 1 dropped it (Waipio Acres, Ewa Gentry), and the vetting run geocoded
# the name without it. geonameid -> (GeoNames asciiname, manifest name).
NAME_DEPARTURES = {"5856516": ("'Ahuimanu", "Ahuimanu")}

# Geocode-query overrides found by scripts/vet_manifest_geometry.py on
# 2026-11-05 and re-vetted the same day (the _vet_retry CSV): each bare query
# matched another place, so registration's 10 km centre guard would skip it.
QUERY_OVERRIDES = {
    "5367314": "Live Oak, Santa Cruz County, California, United States",  # bare: Sutter County, 257 km off
    "5703670": "Elko, Elko County, Nevada, United States",  # bare: Elko County centroid, 32 km off
}

# Street-fitted grids, from docs/experiments/mapillary-discovery-screen_tranche3_grids.json:
# the bounding rectangle of the largest connected cluster of Census local
# streets inside the municipal polygon, plus 250 m. Applied after the audit.
# geonameid -> (center_lat, center_lon, width_m, height_m).
GRID_FITS = {
    "5597955": (43.49753, -116.42638, 4900, 5700),  # Kuna, ID (was 16,932 x 14,704)
    "5777793": (40.69992, -112.0871, 4500, 4100),  # Magna, UT (was 16,651 x 14,607)
    "5334320": (36.55307, -121.92104, 2100, 2900),  # Carmel-by-the-Sea, CA (was 9,550 x 4,032)
}


def select(ranked):
    """
    The kept rows of the ranked record, in record order: at most
    ``PER_UPLOADER_CAP`` per uploader, at most ``CAP`` in total.
    """
    kept, per_user = [], {}
    for row in ranked:
        if len(kept) == CAP:
            break
        if per_user.get(row["user"], 0) >= PER_UPLOADER_CAP:
            continue
        per_user[row["user"]] = per_user.get(row["user"], 0) + 1
        kept.append(row)
    return kept


def manifest_row(geonameid, cities, admin, countries):
    """One register_frame.py manifest row, joined from GeoNames by geonameid."""
    city = cities[geonameid]
    name = city.name
    if geonameid in NAME_DEPARTURES:
        expected, name = NAME_DEPARTURES[geonameid]
        assert city.name == expected, geonameid  # else the departure is stale
    named = city._replace(name=name)
    country = countries[city.iso2]
    generated = query_string(
        SimpleNamespace(city=named, iso2=city.iso2, country=country.name), admin
    )
    return {
        "query_string": QUERY_OVERRIDES.get(geonameid, generated),
        "city": name,
        "admin": admin.get(f"{city.iso2}.{city.admin1}", ""),
        "iso2": city.iso2,
        "country": country.name,
        "continent": country.continent,
        "size_band": "",
        "population": city.population,
        "coverage_regime": "",
        "geonameid": city.geonameid,
        "lat": city.lat,
        "lon": city.lon,
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ranked", default=RANKED, help="the re-score's ranked record")
    p.add_argument(
        "--manifest-out",
        default=os.path.join(REPO_ROOT, "mapillary_discovery_cities_tranche3.csv"),
    )
    p.add_argument("--data-sources", default=DATA_SOURCES)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cities = {
        c.geonameid: c for c in load_cities(os.path.join(args.data_sources, "cities15000.txt"))
    }
    cities.update(
        {
            c.geonameid: c
            for c in load_cities(os.path.join(args.data_sources, "geonames_supplement.txt"))
            if c.geonameid in TRANCHE3_SUPPLEMENT_IDS
        }
    )
    admin = load_admin1(os.path.join(args.data_sources, "admin1CodesASCII.txt"))
    countries = load_countries(os.path.join(args.data_sources, "countryInfo.txt"))
    with open(args.ranked, encoding="utf-8") as f:
        ranked = list(csv.DictReader(f))

    chosen = select(ranked)
    with open(args.manifest_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_MANIFEST_HEADER)
        w.writeheader()
        for row in chosen:
            w.writerow(manifest_row(row["geonameid"], cities, admin, countries))
    print(f"{len(chosen)} selected of {len(ranked)} ranked", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
