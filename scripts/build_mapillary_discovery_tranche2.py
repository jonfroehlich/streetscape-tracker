#!/usr/bin/env python3
"""
Select the second registration tranche of the Mapillary discovery screen (#383)
and write it as a ``register_frame.py`` manifest.

    python scripts/build_mapillary_discovery_tranche2.py \\
        --candidates <mapillary-discovery-screen_candidates.csv> \\
        --catalog-snapshot experiments/mapillary-discovery-383/prod/prod_snapshot.csv \\
        --also-registered <tranche 1's mapillary_discovery_cities.csv>

WHAT
----
The screen (``docs/experiments/mapillary-discovery-screen.md``, PR #419) scored
every GeoNames cities500 place by recent-360° Mapillary km within 2 km of its
point and kept 161 candidates. Its first tranche registered 25 of them. This
script applies the second tranche's rule to the same record:

1. score (km/km²) at least ``SCORE_FLOOR``;
2. more than ``REUSE_RADIUS_KM`` from every city known to the catalog — the
   production snapshot plus every ``--also-registered`` manifest (tranche 1,
   whose own rows are candidates and so drop out here at distance 0) plus
   ``EXTRA_KNOWN`` (cities registered after the snapshot that are in no
   manifest);
3. the place is in the vendored ``data_sources/cities15000.txt`` or is one of
   ``TRANCHE2_SUPPLEMENT_IDS``, this tranche's rows of the shared
   ``data_sources/geonames_supplement.txt``, because a manifest row is a join
   against vendored GeoNames data and the screen's cities500 frame is not
   vendored — any other cities500-only town is reported, never joined (a
   supplement row added later, for another manifest, changes nothing here);
   and its geometry resolved in the pre-registration vetting runs
   (``VETTING_FAILED`` lists the three that did not);
4. greedily, in rank order, more than ``REUSE_RADIUS_KM`` from every row
   already kept, where rank is (favoured creator first, then score
   descending) — so a favoured creator's town is never dropped for a richer
   neighbour;
5. every favoured-creator row is kept, then the rest up to ``CAP`` rows in
   total.

``OPERATOR_EXCEPTIONS`` waives rule 2 (and only rule 2) for a named town, and
``PAIR_EXCEPTIONS`` waives rule 4 for a named pair; the reason is recorded
beside each and the record's decision says so for every row an exception
admitted.

The rows are written in descending score, so ``register_frame.py --limit N``
registers the strongest first. Every candidate's decision is written to
``--record`` (the committed ``docs/experiments/mapillary-discovery-screen_tranche2.csv``),
which ``tests/test_mapillary_discovery_tranche2_manifest.py`` re-derives
independently.

No network. Reads local files only; the candidates and the snapshot are the
screen's own outputs.
"""

import argparse
import csv
import math
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

SCORE_FLOOR = 3.0  # the screen's own candidate threshold (km of recent 360° per km²)
REUSE_RADIUS_KM = 25.0  # register_frame.py's default reuse radius
CAP = 30

# The uploaders of Laurens, Iowa's single-creator sweep (91.6% street-km at
# 0.81 yr) and its sibling account: their towns go ahead of the cap.
FAVOURED_CREATORS = {
    "1361362982257515": "GIS_ISG",
    "1006984308465014": "UAS_ISG",
}

# Registered after the production snapshot and in no manifest: none on
# 2026-10-04 (Montreal and Ottawa are in the snapshot, Cedar Falls in tranche 1).
EXTRA_KNOWN: dict[str, tuple[float, float]] = {}

# Geocode-query overrides found by scripts/vet_manifest_geometry.py, keyed by
# geonameid; identity still comes from the GeoNames columns. Empty: the one
# vetting run (2026-10-04) found three bad geocodes and no tested fix, because
# an override is only committed after the four calls are re-run on it.
QUERY_OVERRIDES: dict[str, str] = {}

# Rows whose geometry did not resolve in that vetting run, so registration
# would skip them (the --max-center-km 10 guard): an eligibility rule, applied
# before the 25 km de-duplication. Each matched a COUNTY or a namesake, not
# the town; the suggested override is untested.
VETTING_FAILED = {
    "5703670": "Elko NV: matched Elko County, 32 km off; try 'Elko, Elko County, Nevada, United States'",
    "5367314": "Live Oak CA: matched Live Oak, Sutter County, 257 km off; the scored place is the "
    "Santa Cruz County CDP; try 'Live Oak, Santa Cruz County, California, United States'",
    "4130430": "Searcy AR: matched Searcy County, 113 km off (the city is in White County); "
    "try 'Searcy, White County, Arkansas, United States'",
}

# Towns admitted although rule 2 drops them: an operator decision, each with
# the geometric reason the reuse radius (a duplicate guard) does not apply.
OPERATOR_EXCEPTIONS = {
    "5026416": "Fergus Falls MN: its only known city within 25 km is "
    "elizabeth--minnesota--united-states, 11.5 km away, whose frozen grid is "
    "1,624 x 812 m; Fergus Falls' vetted grid is 11,551 x 8,879 m, so the two "
    "rectangles cannot overlap and there is no duplicate to guard against; "
    "registration uses --overlap-km 5, which admits it",
    "5250402": "Delavan Lake WI: its only known city within 25 km is "
    "clinton--wisconsin--united-states, 19.1 km away, whose frozen grid is "
    "2,174 x 2,648 m; Delavan Lake's vetted grid is 6,043 x 5,136 m, so the two "
    "rectangles cannot overlap and there is no duplicate to guard against; "
    "registration uses --overlap-km 5, which admits it",
}

# Pairs of rows admitted although rule 4 (no two rows within 25 km) would drop
# the second: an operator decision, two distinct places whose vetted grids do
# not overlap; registered with --overlap-km 5, which admits both.
PAIR_EXCEPTIONS = {
    frozenset({"5250402", "5249259"}): "Delavan Lake and Como WI, 12.7 km apart: "
    "vetted grids 6,043 x 5,136 m and 5,622 x 2,924 m, whose half-diagonals plus "
    "both geocode offsets (2.0 and 0.7 km) total 9.8 km",
}
EXCEPTION_DECISION = "selected (operator exception)"

# The supplement rows this tranche may join (Fergus Falls, Delavan Lake,
# Como). The supplement file is shared by every purposive manifest, so the
# joinable set is pinned rather than read whole: a later manifest's row must
# not turn one of this frozen record's "cities500 only" drops into a pick.
TRANCHE2_SUPPLEMENT_IDS = frozenset({"5026416", "5250402", "5249259"})

RECORD_COLUMNS = [
    "rank",
    "geonameid",
    "name",
    "admin1",
    "cc",
    "lat",
    "lon",
    "pop",
    "score_km_per_km2",
    "recent_360_km_2km",
    "top_creator_id",
    "top_creator_username",
    "top_creator_share",
    "median_captured",
    "nearest_known",
    "nearest_known_km",
    "in_cities15000",
    "decision",
]


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def load_known(snapshot_path, also_registered):
    """{label: (lat, lon)} for every city the catalog knows or will know."""
    known = {}
    with open(snapshot_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["table"] == "cities":
                known[r["city_id"]] = (float(r["lat"]), float(r["lon"]))
    for path in also_registered:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                known[f"{os.path.basename(path)}:{r['city']}"] = (float(r["lat"]), float(r["lon"]))
    known.update(EXTRA_KNOWN)
    return known


def select(candidates, known, vendored_ids, supplement_ids=frozenset()):
    """
    Annotate each candidate dict with ``nearest_known``, ``nearest_known_km``,
    ``in_cities15000`` and ``decision``; return the selected rows in
    descending score. ``decision`` is ``selected`` (or ``EXCEPTION_DECISION``)
    or the first rule that dropped the row. ``vendored_ids`` is cities15000;
    ``supplement_ids`` the supplement rows, which are joinable too.
    """
    for c in candidates:
        lat, lon = float(c["lat"]), float(c["lon"])
        name, (klat, klon) = min(
            known.items(), key=lambda kv: haversine_km(lat, lon, kv[1][0], kv[1][1])
        )
        c["nearest_known"] = name
        c["nearest_known_km"] = round(haversine_km(lat, lon, klat, klon), 1)
        c["in_cities15000"] = "yes" if c["geonameid"] in vendored_ids else "no"
        if float(c["km_per_km2"]) < SCORE_FLOOR:
            c["decision"] = "below score floor"
        elif c["nearest_known_km"] <= REUSE_RADIUS_KM and c["geonameid"] not in OPERATOR_EXCEPTIONS:
            c["decision"] = "within 25 km of a known city"
        elif c["in_cities15000"] == "no" and c["geonameid"] not in supplement_ids:
            c["decision"] = "cities500 only"
        elif c["geonameid"] in VETTING_FAILED:
            c["decision"] = "failed geometry vetting"
        else:
            c["decision"] = None

    pool = [c for c in candidates if c["decision"] is None]
    pool.sort(key=lambda c: (c["top_creator"] not in FAVOURED_CREATORS, -float(c["km_per_km2"])))
    kept = []
    waived = set(OPERATOR_EXCEPTIONS)  # ids an exception admitted
    for c in pool:
        lat, lon = float(c["lat"]), float(c["lon"])
        near = [
            k
            for k in kept
            if haversine_km(lat, lon, float(k["lat"]), float(k["lon"])) <= REUSE_RADIUS_KM
        ]
        excused = [
            k for k in near if frozenset({c["geonameid"], k["geonameid"]}) in PAIR_EXCEPTIONS
        ]
        near = [k for k in near if k not in excused]
        if near:
            c["decision"] = f"within 25 km of selected {near[0]['name']}"
        else:
            kept.append(c)
            if excused:
                waived.update({c["geonameid"]} | {k["geonameid"] for k in excused})
    favoured = [c for c in kept if c["top_creator"] in FAVOURED_CREATORS]
    others = [c for c in kept if c["top_creator"] not in FAVOURED_CREATORS]
    room = max(0, CAP - len(favoured))
    chosen = favoured + others[:room]
    for c in others[room:]:
        c["decision"] = "over the cap"
    for c in chosen:
        c["decision"] = EXCEPTION_DECISION if c["geonameid"] in waived else "selected"
    return sorted(chosen, key=lambda c: -float(c["km_per_km2"]))


def manifest_row(candidate, cities, admin, countries):
    """One register_frame.py manifest row, joined from GeoNames by geonameid."""
    city = cities[candidate["geonameid"]]
    country = countries[city.iso2]
    generated = query_string(
        SimpleNamespace(city=city, iso2=city.iso2, country=country.name), admin
    )
    return {
        "query_string": QUERY_OVERRIDES.get(city.geonameid, generated),
        "city": city.name,
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


def record_row(rank, c):
    return {
        "rank": rank,
        "geonameid": c["geonameid"],
        "name": c["name"],
        "admin1": c["admin1"],
        "cc": c["cc"],
        "lat": c["lat"],
        "lon": c["lon"],
        "pop": c["pop"],
        "score_km_per_km2": c["km_per_km2"],
        "recent_360_km_2km": c["km_in_disc"],
        "top_creator_id": c["top_creator"],
        "top_creator_username": c["top_creator_username"],
        "top_creator_share": c["top_share"],
        "median_captured": c["median_captured"],
        "nearest_known": c["nearest_known"],
        "nearest_known_km": c["nearest_known_km"],
        "in_cities15000": c["in_cities15000"],
        "decision": c["decision"],
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--candidates", required=True, help="the screen's candidates CSV (#419)")
    p.add_argument("--catalog-snapshot", required=True, help="the screen's prod_snapshot.csv")
    p.add_argument(
        "--also-registered",
        action="append",
        default=[],
        help="a manifest of cities registered after the snapshot (repeatable)",
    )
    p.add_argument(
        "--record",
        default=os.path.join(
            REPO_ROOT, "docs", "experiments", "mapillary-discovery-screen_tranche2.csv"
        ),
    )
    p.add_argument(
        "--manifest-out",
        default=os.path.join(REPO_ROOT, "mapillary_discovery_cities_tranche2.csv"),
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cities = {c.geonameid: c for c in load_cities(os.path.join(DATA_SOURCES, "cities15000.txt"))}
    supplement = {
        c.geonameid: c
        for c in load_cities(os.path.join(DATA_SOURCES, "geonames_supplement.txt"))
        if c.geonameid in TRANCHE2_SUPPLEMENT_IDS
    }
    admin = load_admin1(os.path.join(DATA_SOURCES, "admin1CodesASCII.txt"))
    countries = load_countries(os.path.join(DATA_SOURCES, "countryInfo.txt"))
    with open(args.candidates, encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))
    known = load_known(args.catalog_snapshot, args.also_registered)

    chosen = select(candidates, known, set(cities), set(supplement))
    candidates.sort(key=lambda c: -float(c["km_per_km2"]))
    with open(args.record, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RECORD_COLUMNS)
        w.writeheader()
        for rank, c in enumerate(candidates, 1):
            w.writerow(record_row(rank, c))
    with open(args.manifest_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_MANIFEST_HEADER)
        w.writeheader()
        for c in chosen:
            w.writerow(manifest_row(c, {**supplement, **cities}, admin, countries))
    print(f"{len(chosen)} selected of {len(candidates)} candidates", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
