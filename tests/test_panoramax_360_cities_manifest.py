"""
The Panoramax-360 city manifest (panoramax_360_cities.csv, #406), registered
with scripts/register_frame.py --manifest.

The manifest is a purposive list — the 2026-10-01 Panoramax world screen's
richest NEW clusters (docs/experiments/panoramax-world-screen.md) — and, like
mapillary_360_cities.csv, its VALUES are a join against the vendored GeoNames
data keyed by geonameid, never hand-typed. These tests are that join run in
reverse, plus the SELECTION run in reverse: every row traces to one cluster of
the committed screen record, the rows are in descending 360° bound (so
``register_frame.py --limit N`` registers the richest first), and every
cluster the selection rule admits is either in the manifest or excluded by
name with a reason.

The city_ids are pinned as literals because registration freezes them forever.

The GeoNames and query_string assertions duplicate
tests/test_mapillary_360_cities_manifest.py rather than sharing a helper with
it: factoring them out would have meant editing the file that pins the
Mapillary batch's already-registered slugs, for no change in what either file
checks.
"""

import csv
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.build_worldwide_frame import (
    _MANIFEST_HEADER,
    load_admin1,
    load_cities,
    load_countries,
    query_string,
)
from scripts.register_frame import frame_identity
from streetscape_metadata_tracker import db

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "panoramax_360_cities.csv"
DATA_SOURCES = REPO_ROOT / "data_sources"
CLUSTERS = REPO_ROOT / "docs" / "experiments" / "panoramax-world-screen_clusters.csv"
PLACES = REPO_ROOT / "docs" / "experiments" / "panoramax-world-screen_places.csv"
OTHER_MANIFESTS = [REPO_ROOT / "mapillary_360_cities.csv", REPO_ROOT / "worldwide_frame.csv"]

# The selection rule (#406): a NEW cluster whose 360° upper bound clears this
# floor — lower in the US and Canada, the Project Sidewalk deployment priority.
BOUND_FLOOR = 100_000
BOUND_FLOOR_US_CA = 10_000
# Below the floor, admitted by name: #406's table lists it for an EU camera
# grant aimed at completing the city on foot.
NAMED_EXCEPTIONS = {"2963398": "Kilkenny"}

# The rule reads a CLUSTER's bound, summed around its anchor, but a row
# registers the cluster's NAME point. Where the anchor is far off, the name
# point's OWN 10 km bound can sit far below the floor, and the grid frozen
# there holds little of the imagery that admitted the cluster. Every row's own
# bound must clear its floor, or be listed here with what it actually is, so a
# future row cannot be admitted that way without someone deciding it.
# Values are (place name, own 10 km bound, anchor name, anchor's cluster bound).
OWN_BOUND_EXCEPTIONS = {
    # Kept by operator decision (#406's table names it). Its own bound is 2.7%
    # of the floor; the 109,576 was summed around Wevelgem, about 7 km WSW and
    # outside Kortrijk's ~11 km-wide grid. Expect a near-empty Panoramax series.
    "2794055": ("Kortrijk", 2_725, "Wevelgem", 109_576),
}

# Clusters the rule admits that are NOT in the manifest, keyed by the screen's
# cluster name, each with the reason. A new screen record that admits a cluster
# absent from both the manifest and this dict fails test_selection_is_complete.
EXCLUDED_CLUSTERS = {
    # tracked_split (below), and the only one of the six the rule admits.
    "Cergy-Pontoise": "tracked_split: anchor Herblay-sur-Seine is tracked (Paris)",
    # Post-snapshot: Cedar Falls IA was enabled on 2026-10-02, after the
    # screen's 10-01 catalog snapshot, and is 9 km from Waterloo's point
    # (inside the 25 km reuse radius) and Waterloo's own anchor.
    "Waterloo": "inside 25 km of Cedar Falls, enabled 2026-10-02",
    # Within 25 km of a richer row: their grids would overlap it, and each
    # cluster's bound was summed around an anchor closer to that neighbour.
    "Quimper": "20.4 km from Douarnenez; bound summed around anchor Concarneau",
    "Vienne": "24.9 km from Lyon; bound summed around anchor Givors, between them",
}

# The six `tracked_split` clusters (writeup finding 5): "new" by one point and
# "tracked" by the other (the name point vs the anchor the bound was summed
# around), so neither settles whether the catalog already covers the imagery.
# Never manifest rows, whatever their bound; only Cergy-Pontoise clears it.
TRACKED_SPLIT = {
    "Aalst",
    "Antwerp",
    "Cergy-Pontoise",
    "Leuven",
    "Saint-Quentin-en-Yvelines",
    "Umraniye",
}

# Cities added to the production catalog after the screen's 2026-10-01
# snapshot, which the screen's "new" flag therefore cannot see: the 25 towns of
# mapillary_discovery_cities.csv (PR #419's branch; Cedar Falls among them) and
# Montreal. Coordinates are those files' GeoNames points.
POST_SNAPSHOT_CITIES = {
    "Apex NC": (35.73265, -78.85029),
    "Bedford MA": (42.49065, -71.27617),
    "Cedar Falls IA": (42.52776, -92.44547),
    "Cottonwood Heights UT": (40.61967, -111.81021),
    "Eureka CA": (40.80207, -124.16367),
    "Ewa Gentry HI": (21.33999, -158.03039),
    "Fond du Lac WI": (43.775, -88.43883),
    "Foothill Farms CA": (38.67877, -121.35114),
    "Foster City CA": (37.55855, -122.27108),
    "Francestown NH": (42.98758, -71.81258),
    "Heber-Overgaard AZ": (34.41414, -110.56956),
    "Hopewell VA": (37.30432, -77.2872),
    "Kaysville UT": (41.03522, -111.93855),
    "Lexington VA": (37.78402, -79.44282),
    "Lindon UT": (40.34329, -111.72076),
    "Los Altos CA": (37.38522, -122.11413),
    "Mont Vernon NH": (42.89453, -71.67424),
    "Montreal QC": (45.50884, -73.58781),
    "Morton IL": (40.61282, -89.45926),
    "New Boston NH": (42.97619, -71.69396),
    "Orangevale CA": (38.67851, -121.22578),
    "Petersburg VA": (37.22793, -77.40193),
    "State College PA": (40.79339, -77.86),
    "Sunnyslope CA": (34.01196, -117.43338),
    "Trophy Club TX": (32.9979, -97.18362),
    "Waipio Acres HI": (21.46485, -158.01331),
}

# register_frame.py's default reuse radius: two rows closer than this would be
# one physical place registered twice, or two grids that overlap.
REUSE_RADIUS_KM = 25.0

# Rows whose GeoNames-derived query freezes the wrong grid, found by
# scripts/vet_manifest_geometry.py before registration (2026-10-04):
#   Mayenne -> the Mayenne DEPARTEMENT (the commune shares its name), 17 km
#     off the GeoNames point, so the center guard skips it; naming the
#     departement as the commune's container resolves the commune, 0.4 km off.
#   Muscatine -> Muscatine COUNTY (48,979 x 29,487 m, capped to 40 km: 2.95 M
#     GSV points); naming the county as the container resolves the city.
# Three other rows (Bordeaux, Bayonne, Angouleme) do NOT geocode under
# GeoNames' "New Aquitaine" and register through register_frame's bare
# "City, Country" fallback, which vetting showed resolves the commune; they
# carry no override, because an override equal to that fallback is forbidden.
# An override changes only the GEOCODE query: identity, and so the frozen
# city_id, still comes from the GeoNames columns.
QUERY_OVERRIDES = {
    "2994935": "Mayenne, Mayenne, Pays de la Loire, France",
    "4868404": "Muscatine, Muscatine County, Iowa, United States",
}

# Permanent slugs, frozen at registration. Order matches the manifest. The
# admin component is GeoNames' admin-1 ASCII name, kept as is (the identity
# rule). For France those are the post-2016 regions, two under GeoNames'
# truncated names: FR.84 "Rhone-Alpes" is Auvergne-Rhone-Alpes and FR.27
# "Bourgogne" is Bourgogne-Franche-Comte. So Besancon and Lons-le-Saunier,
# both in Franche-Comte, carry "bourgogne", and the public label reads
# "Besancon, Bourgogne, France".
EXPECTED_CITY_IDS = {
    "2973783": "strasbourg--grand-est--france",
    "2996944": "lyon--rhone-alpes--france",
    "3031582": "bordeaux--new-aquitaine--france",
    "2998324": "lille--hauts-de-france--france",
    "3005866": "laval--pays-de-la-loire--france",
    "2977921": "saint-nazaire--pays-de-la-loire--france",
    "3029241": "caen--normandy--france",
    "2992166": "montpellier--occitanie--france",
    "3003796": "le-havre--normandy--france",
    "2990969": "nantes--pays-de-la-loire--france",
    "3030300": "brest--brittany--france",
    "2993002": "montauban--occitanie--france",
    "2989317": "orleans--centre-val-de-loire--france",
    "3014728": "grenoble--rhone-alpes--france",
    "3033123": "besancon--bourgogne--france",
    "2991772": "morlaix--brittany--france",
    "2972315": "toulouse--occitanie--france",
    "3034475": "bayonne--new-aquitaine--france",
    "2994935": "mayenne--pays-de-la-loire--france",
    "2997626": "lons-le-saunier--bourgogne--france",
    "2972191": "tours--centre-val-de-loire--france",
    "3037598": "angouleme--new-aquitaine--france",
    "2873891": "mannheim--baden-wurttemberg--germany",
    "3020996": "douarnenez--brittany--france",
    "2995469": "marseille--provence-alpes-cote-d'azur--france",
    "3034126": "beaune--bourgogne--france",
    "2794055": "kortrijk--flanders--belgium",
    "3025466": "cherbourg--normandy--france",
    "2820256": "ulm--baden-wurttemberg--germany",
    "2963398": "kilkenny--leinster--ireland",
    "4870380": "ottumwa--iowa--united-states",
    "4866371": "marshalltown--iowa--united-states",
    "4543762": "norman--oklahoma--united-states",
    "4868907": "newton--iowa--united-states",
    "4866445": "mason-city--iowa--united-states",
    "4868404": "muscatine--iowa--united-states",
    "4857486": "fort-dodge--iowa--united-states",
    "4853423": "davenport--iowa--united-states",
    "5040647": "owatonna--minnesota--united-states",
    "4159553": "immokalee--florida--united-states",
}


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


def _admitted(cluster) -> bool:
    """The selection rule over one row of the screen's cluster record."""
    if cluster["in_catalog"] != "no" or cluster["candidate"] != "yes":
        return False
    floor = BOUND_FLOOR_US_CA if cluster["cc"] in ("US", "CA") else BOUND_FLOOR
    return int(cluster["ub_360_10km"]) >= floor


@pytest.fixture(scope="module")
def manifest_rows():
    with open(MANIFEST, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def clusters():
    with open(CLUSTERS, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def places():
    with open(PLACES, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def geonames():
    """The vendored GeoNames join: cities by id, admin-1 names, countries."""
    cities = {c.geonameid: c for c in load_cities(DATA_SOURCES / "cities15000.txt")}
    return SimpleNamespace(
        cities=cities,
        admin=load_admin1(DATA_SOURCES / "admin1CodesASCII.txt"),
        countries=load_countries(DATA_SOURCES / "countryInfo.txt"),
    )


def _cluster_of(row, clusters):
    """
    The screen cluster this row is: the one whose NAME point is this row's
    GeoNames point. Joined on coordinates, because the record carries GeoNames'
    accented display name (Orléans) and the manifest its ASCII name (Orleans).
    """
    lat, lon = float(row["lat"]), float(row["lon"])
    matches = [
        c
        for c in clusters
        if c["cc"] == row["iso2"]
        and abs(float(c["lat"]) - lat) < 1e-4
        and abs(float(c["lon"]) - lon) < 1e-4
    ]
    assert len(matches) == 1, (row["geonameid"], [m["name"] for m in matches])
    return matches[0]


def test_header_is_the_frame_manifest_format(manifest_rows):
    """register_frame.py reads this file, so the columns must be its columns."""
    assert list(manifest_rows[0]) == _MANIFEST_HEADER


def test_rows_are_unique_and_complete(manifest_rows):
    ids = [row["geonameid"] for row in manifest_rows]
    assert len(ids) == len(set(ids))
    assert ids == list(EXPECTED_CITY_IDS)


@pytest.mark.parametrize("field", ["query_string", "city", "iso2", "country", "geonameid"])
def test_required_columns_are_populated(manifest_rows, field):
    assert all(row[field] for row in manifest_rows)


def test_every_row_matches_its_vendored_geonames_record(manifest_rows, geonames):
    """The join, in reverse: nothing in a row may drift from GeoNames."""
    for row in manifest_rows:
        gid = row["geonameid"]
        city = geonames.cities[gid]
        country = geonames.countries[city.iso2]

        assert row["city"] == city.name, gid
        assert row["iso2"] == city.iso2, gid
        assert row["country"] == country.name, gid
        assert row["continent"] == country.continent, gid
        assert int(row["population"]) == city.population, gid
        assert float(row["lat"]) == pytest.approx(city.lat), gid
        assert float(row["lon"]) == pytest.approx(city.lon), gid
        assert row["admin"] == geonames.admin.get(f"{city.iso2}.{city.admin1}", ""), gid


def test_query_strings_are_what_the_frame_generator_would_write(manifest_rows, geonames):
    """
    The worldwide frame's "City, Admin, Country" construction, except for the
    declared QUERY_OVERRIDES — each of which must differ from both the
    generated query and register_frame's bare fallback, or it would be a lie
    about why it exists.
    """
    for row in manifest_rows:
        gid = row["geonameid"]
        city = geonames.cities[gid]
        record = SimpleNamespace(city=city, iso2=city.iso2, country=row["country"])
        generated = query_string(record, geonames.admin)
        if gid not in QUERY_OVERRIDES:
            assert row["query_string"] == generated, gid
            continue
        assert row["query_string"] == QUERY_OVERRIDES[gid], gid
        assert QUERY_OVERRIDES[gid] != generated, gid
        assert QUERY_OVERRIDES[gid] != f"{row['city']}, {row['country']}", gid


def test_city_ids_are_the_pinned_permanent_slugs(manifest_rows):
    """What register_frame.py will freeze into the catalog: filenames and URLs."""
    for row in manifest_rows:
        city_id = db.derive_city_id(*frame_identity(row))
        assert city_id == EXPECTED_CITY_IDS[row["geonameid"]]
        assert city_id.isascii()


def test_frame_only_columns_are_blank(manifest_rows):
    """size_band and coverage_regime are the frame's strata; a purposive list has none."""
    for row in manifest_rows:
        assert row["size_band"] == ""
        assert row["coverage_regime"] == ""


def test_every_row_is_an_admitted_new_cluster_of_the_screen(manifest_rows, clusters):
    """Each row traces to one cluster the selection rule admits, or a named exception."""
    for row in manifest_rows:
        cluster = _cluster_of(row, clusters)
        assert cluster["in_catalog"] == "no", row["geonameid"]
        assert cluster["candidate"] == "yes", row["geonameid"]
        assert cluster["name"] not in EXCLUDED_CLUSTERS, row["geonameid"]
        assert cluster["name"] not in TRACKED_SPLIT, row["geonameid"]
        if row["geonameid"] in NAMED_EXCEPTIONS:
            assert cluster["name"] == NAMED_EXCEPTIONS[row["geonameid"]]
            assert not _admitted(cluster)  # else the exception is stale
        else:
            assert _admitted(cluster), row["geonameid"]


def _place_of(row, places):
    """The screen's per-place record at this row's GeoNames point."""
    lat, lon = float(row["lat"]), float(row["lon"])
    matches = [
        p
        for p in places
        if p["cc"] == row["iso2"]
        and abs(float(p["lat"]) - lat) < 1e-4
        and abs(float(p["lon"]) - lon) < 1e-4
    ]
    assert len(matches) == 1, (row["geonameid"], [m["name"] for m in matches])
    return matches[0]


def test_every_row_clears_the_floor_on_its_own_bound(manifest_rows, places):
    """
    A row's own name-point bound clears its floor, or it is a named exception:
    the cluster bound alone admitted Kortrijk on imagery summed around
    Wevelgem, outside Kortrijk's grid.
    """
    for row in manifest_rows:
        gid = row["geonameid"]
        if gid in NAMED_EXCEPTIONS:
            continue  # admitted by name below the floor on every reading
        place = _place_of(row, places)
        own = int(place["ub_360_10km"])
        floor = BOUND_FLOOR_US_CA if row["iso2"] in ("US", "CA") else BOUND_FLOOR
        if gid not in OWN_BOUND_EXCEPTIONS:
            assert own >= floor, (row["city"], own, floor)
            continue
        name, own_bound, anchor_name, anchor_bound = OWN_BOUND_EXCEPTIONS[gid]
        assert own < floor, row["city"]  # else the exception is stale
        assert (row["city"], own) == (name, own_bound)
        anchor = [
            p
            for p in places
            if p["cluster_rank"] == place["cluster_rank"] and p["is_anchor"] == "yes"
        ]
        assert [(a["name"], int(a["ub_360_10km"])) for a in anchor] == [(anchor_name, anchor_bound)]


def test_selection_is_complete(manifest_rows, clusters):
    """Every cluster the rule admits is in the manifest or excluded with a reason."""
    in_manifest = {_cluster_of(row, clusters)["name"] for row in manifest_rows}
    admitted = {c["name"] for c in clusters if _admitted(c)}
    assert admitted - in_manifest == set(EXCLUDED_CLUSTERS)


def test_rows_are_in_descending_360_bound(manifest_rows, clusters):
    """So register_frame.py --limit N registers the richest N first."""
    bounds = [int(_cluster_of(row, clusters)["ub_360_10km"]) for row in manifest_rows]
    assert bounds == sorted(bounds, reverse=True)


def test_no_two_rows_are_within_the_reuse_radius(manifest_rows):
    points = [(r["geonameid"], float(r["lat"]), float(r["lon"])) for r in manifest_rows]
    for i, (a, alat, alon) in enumerate(points):
        for b, blat, blon in points[i + 1 :]:
            assert _haversine_km(alat, alon, blat, blon) > REUSE_RADIUS_KM, (a, b)


def test_no_row_is_within_the_reuse_radius_of_a_known_registration(manifest_rows):
    """
    The screen's "new" flag saw the 2026-10-01 catalog only. This repeats the
    check against what has been (or is being) registered since, and against
    the committed manifests, so a row cannot be silently aliased onto one of
    them by register_frame.py's reuse rule — or registered beside it.
    """
    others = dict(POST_SNAPSHOT_CITIES)
    for path in OTHER_MANIFESTS:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                others[f"{path.name}:{r['city']}"] = (float(r["lat"]), float(r["lon"]))
    for row in manifest_rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        for name, (olat, olon) in others.items():
            assert _haversine_km(lat, lon, olat, olon) > REUSE_RADIUS_KM, (row["city"], name)
