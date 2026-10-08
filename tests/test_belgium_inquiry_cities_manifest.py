"""
The Belgium inquiry manifest (belgium_inquiry_cities.csv), registered on
production with scripts/register_frame.py --manifest on 2026-10-08 under the
notes label "belgium inquiry 2026-10-08", and enabled the same day.

The list is curated — three Flemish cities chosen for a deployment inquiry,
read off the 2026-10-08 Belgium screen (docs/experiments/belgium-screen.md) —
but, like mapillary_360_cities.csv, its VALUES are a join against the vendored
GeoNames data keyed by geonameid, never hand-typed. These tests are that join
run in reverse, plus:

* the city_ids, pinned as literals because registration froze them;
* the geometry production FROZE for each (centre, width, height, all OK at the
  boundary audit with no resize), pinned as literals, with the screen's 2 km
  scoring disc around each GeoNames point required to lie wholly inside it —
  so the screen numbers the writeup quotes describe imagery inside the grid;
* each row's screen record, joined on its GeoNames point to the committed
  docs/experiments/belgium-screen_places.csv, so the writeup's per-city
  numbers and the manifest cannot drift apart;
* no two rows, and no row and another committed manifest's row, within the
  5 km --overlap-km a purposive batch registers with. Antwerp and Mechelen are
  22 km apart, and Mechelen is ~21 km from the tracked Brussels point, so the
  default 25 km radius would have aliased Mechelen away.

The GeoNames and query_string assertions duplicate
tests/test_mapillary_360_cities_manifest.py rather than sharing a helper with
it, so the file pinning an already-registered batch's slugs is not edited.
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
MANIFEST = REPO_ROOT / "belgium_inquiry_cities.csv"
DATA_SOURCES = REPO_ROOT / "data_sources"
SCREEN_PLACES = REPO_ROOT / "docs" / "experiments" / "belgium-screen_places.csv"
OTHER_MANIFESTS = [
    REPO_ROOT / "mapillary_360_cities.csv",
    REPO_ROOT / "mapillary_discovery_cities.csv",
    REPO_ROOT / "mapillary_discovery_cities_tranche2.csv",
    REPO_ROOT / "panoramax_360_cities.csv",
    REPO_ROOT / "worldwide_frame.csv",
]

# register_frame.py --overlap-km for a purposive batch (docs/worldwide_sampling.md).
OVERLAP_KM = 5.0
# The screen scored each place over a disc of this radius around its point.
SCORE_DISC_KM = 2.0

# Permanent slugs, frozen at registration. Order matches the manifest.
EXPECTED_CITY_IDS = {
    "2803138": "antwerp--flanders--belgium",
    "2791537": "mechelen--flanders--belgium",
    "2802170": "beringen--flanders--belgium",
}

# The geometry production froze on 2026-10-08, read from the prod catalog's
# `cities` row: (center_lat, center_lon, width_m, height_m, boundary-audit
# verdict). All three audited OK and none was resized.
FROZEN_GEOMETRY = {
    "antwerp--flanders--belgium": (51.260461199999995, 4.3633156, 20346, 26072, "OK"),
    "mechelen--flanders--belgium": (51.034766700000006, 4.4595617999999995, 12553, 9744, "OK"),
    "beringen--flanders--belgium": (51.06106405, 5.232862, 15609, 9945, "OK"),
}

# The screen numbers the writeup quotes per registered city:
# (recent-360 Mapillary km/km2, top-creator share, Panoramax 360 upper bound).
SCREEN_RECORD = {
    "2803138": (22.14, 0.96, 4832),
    "2791537": (2.18, 0.68, 21605),
    "2802170": (1.45, 1.0, 4441),
}


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


@pytest.fixture(scope="module")
def manifest_rows():
    with open(MANIFEST, encoding="utf-8") as f:
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
    """No overrides: every query is the frame's "City, Admin, Country"."""
    for row in manifest_rows:
        city = geonames.cities[row["geonameid"]]
        record = SimpleNamespace(city=city, iso2=city.iso2, country=row["country"])
        assert row["query_string"] == query_string(record, geonames.admin), row["geonameid"]


def test_city_ids_are_the_pinned_permanent_slugs(manifest_rows):
    """What register_frame.py froze into the catalog: filenames and URLs."""
    for row in manifest_rows:
        city_id = db.derive_city_id(*frame_identity(row))
        assert city_id == EXPECTED_CITY_IDS[row["geonameid"]]
        assert city_id.isascii()


def test_frame_only_columns_are_blank(manifest_rows):
    """size_band and coverage_regime are the frame's strata; a purposive list has none."""
    for row in manifest_rows:
        assert row["size_band"] == ""
        assert row["coverage_regime"] == ""


def test_every_slug_has_frozen_geometry_that_passed_the_audit():
    assert set(FROZEN_GEOMETRY) == set(EXPECTED_CITY_IDS.values())
    for city_id, (_, _, width_m, height_m, verdict) in FROZEN_GEOMETRY.items():
        assert verdict == "OK", city_id
        assert 0 < width_m <= 40_000 and 0 < height_m <= 40_000, city_id  # the #166 cap


def test_the_scored_disc_lies_inside_the_frozen_grid(manifest_rows):
    """
    The grid is centred on the geocode, not the GeoNames point the screen
    scored, so the screen's numbers describe the grid only if the whole 2 km
    disc around that point is inside it.
    """
    for row in manifest_rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        clat, clon, width_m, height_m, _ = FROZEN_GEOMETRY[EXPECTED_CITY_IDS[row["geonameid"]]]
        dy = abs(lat - clat) * 110.57
        dx = abs(lon - clon) * 111.32 * math.cos(math.radians(clat))
        assert dy + SCORE_DISC_KM <= height_m / 2000, (row["city"], dy, height_m)
        assert dx + SCORE_DISC_KM <= width_m / 2000, (row["city"], dx, width_m)


def test_every_row_is_in_the_committed_screen_record(manifest_rows):
    """The writeup's per-city numbers are this row's record, joined on its point."""
    with open(SCREEN_PLACES, encoding="utf-8") as f:
        places = list(csv.DictReader(f))
    for row in manifest_rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        hits = [
            p
            for p in places
            if abs(float(p["lat"]) - lat) < 1e-4 and abs(float(p["lon"]) - lon) < 1e-4
        ]
        assert len(hits) == 1, row["city"]
        place = hits[0]
        assert place["place"] == row["city"]
        assert int(place["pop"]) == int(row["population"])
        density, share, pnx_360 = SCREEN_RECORD[row["geonameid"]]
        assert float(place["mly_km_per_km2"]) == density, row["city"]
        assert float(place["mly_top_creator_share"]) == share, row["city"]
        assert int(place["pnx_360_ub"]) == pnx_360, row["city"]


def test_no_two_rows_are_within_the_overlap_radius(manifest_rows):
    points = [(r["city"], float(r["lat"]), float(r["lon"])) for r in manifest_rows]
    for i, (a, alat, alon) in enumerate(points):
        for b, blat, blon in points[i + 1 :]:
            assert _haversine_km(alat, alon, blat, blon) > OVERLAP_KM, (a, b)


def test_no_row_is_within_the_overlap_radius_of_another_manifest(manifest_rows):
    for path in OTHER_MANIFESTS:
        with open(path, encoding="utf-8") as f:
            others = list(csv.DictReader(f))
        for row in manifest_rows:
            lat, lon = float(row["lat"]), float(row["lon"])
            for o in others:
                d = _haversine_km(lat, lon, float(o["lat"]), float(o["lon"]))
                assert d > OVERLAP_KM, (row["city"], path.name, o["city"])
