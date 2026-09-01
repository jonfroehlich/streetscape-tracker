"""
The Mapillary-360 city manifest (mapillary_360_cities.csv, registered with
scripts/register_frame.py --manifest).

The manifest is a hand-curated list — cities with documented Mapillary 360°
capture programs — but its *values* are not hand-typed: every row is a join
against the vendored GeoNames data, keyed by geonameid. These tests are that
join, run in reverse, and they are the file's provenance: there is no generator
script to point at, so a row whose coordinates, admin name or spelling drifted
away from GeoNames fails here rather than silently freezing a wrong grid.

The city_ids are pinned as literals on purpose. Registration freezes them
forever (filenames and published URLs depend on them), so a change in
db.derive_city_id or naming.sanitize_city_query_str must break a test rather
than quietly rename a city.
"""

import csv
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
MANIFEST = REPO_ROOT / "mapillary_360_cities.csv"
DATA_SOURCES = REPO_ROOT / "data_sources"

# GeoNames' asciiname is the identity rule (docs/worldwide_sampling.md), and
# this is the one place we depart from it: "Malmoe" reads as a typo in a
# display name and a URL, and the slug is permanent.
ASCII_SPELLING_OVERRIDES = {"2692969": "Malmo"}

# Two cities whose GeoNames-derived query matches the WRONG OSM feature, caught
# before registration by comparing each geocoded center against the GeoNames
# coordinates (the 50 km --max-center-km guard sees neither):
#   Sandusky -> "Sandusky County, Ohio", a different county ~36 km west of the
#     city, which is in Erie County.
#   Brussels -> the City of Brussels commune (8.7x13.1 km), about a fifth of the
#     19-commune Brussels-Capital Region the coverage number is meant to describe.
# An override changes only the GEOCODE query. Identity — and so the frozen
# city_id — still comes from the GeoNames city/admin/country columns.
QUERY_OVERRIDES = {
    "5170691": "Sandusky, Erie County, Ohio, United States",
    "2800866": "Bruxelles-Capitale, Belgium",
}

# Permanent slugs, frozen at registration. Order matches the manifest.
EXPECTED_CITY_IDS = {
    "6094817": "ottawa--ontario--canada",
    "2158177": "melbourne--victoria--australia",
    "593116": "vilnius--lithuania",
    "3067696": "prague--czechia",
    "2618425": "copenhagen--capital-region--denmark",
    "2867714": "munich--bavaria--germany",
    "3173435": "milan--lombardy--italy",
    "3128760": "barcelona--catalonia--spain",
    "2800866": "brussels--belgium",
    "2692969": "malmo--skane--sweden",
    "160263": "dar-es-salaam--tanzania",
    "5462393": "clovis--new-mexico--united-states",
    "6331909": "johns-creek--georgia--united-states",
    "5170691": "sandusky--ohio--united-states",
}


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
    assert set(ids) == set(EXPECTED_CITY_IDS)


@pytest.mark.parametrize("field", ["query_string", "city", "iso2", "country", "geonameid"])
def test_required_columns_are_populated(manifest_rows, field):
    assert all(row[field] for row in manifest_rows)


def test_every_row_matches_its_vendored_geonames_record(manifest_rows, geonames):
    """The join, in reverse: nothing in a row may drift from GeoNames."""
    for row in manifest_rows:
        gid = row["geonameid"]
        city = geonames.cities[gid]
        country = geonames.countries[city.iso2]
        expected_name = ASCII_SPELLING_OVERRIDES.get(gid, city.name)

        assert row["city"] == expected_name, gid
        assert row["iso2"] == city.iso2, gid
        assert row["country"] == country.name, gid
        assert row["continent"] == country.continent, gid
        assert int(row["population"]) == city.population, gid
        assert float(row["lat"]) == pytest.approx(city.lat), gid
        assert float(row["lon"]) == pytest.approx(city.lon), gid
        assert row["admin"] == geonames.admin.get(f"{city.iso2}.{city.admin1}", ""), gid


def test_query_strings_are_what_the_frame_generator_would_write(manifest_rows, geonames):
    """
    Same "City, Admin, Country" construction as the worldwide frame, including
    effective_admin dropping an admin that just repeats the city name — so a
    Nominatim query here behaves exactly like a frame query, except for the
    declared QUERY_OVERRIDES.
    """
    for row in manifest_rows:
        gid = row["geonameid"]
        city = geonames.cities[gid]
        record = SimpleNamespace(
            city=city._replace(name=row["city"]),
            iso2=city.iso2,
            country=row["country"],
        )
        generated = query_string(record, geonames.admin)
        if gid not in QUERY_OVERRIDES:
            assert row["query_string"] == generated, gid
            continue
        assert row["query_string"] == QUERY_OVERRIDES[gid], gid
        # An override that matches what the generator would write anyway is a
        # lie about why it exists — and would silently stop being tested.
        assert QUERY_OVERRIDES[gid] != generated, gid


def test_city_ids_are_the_pinned_permanent_slugs(manifest_rows):
    """
    What register_frame.py will freeze into the catalog. ASCII, comma-free and
    unchanging: these become filenames and published URLs.
    """
    for row in manifest_rows:
        city_id = db.derive_city_id(*frame_identity(row))
        assert city_id == EXPECTED_CITY_IDS[row["geonameid"]]
        assert city_id.isascii()


def test_frame_only_columns_are_blank(manifest_rows):
    """
    size_band and coverage_regime are stratification variables of the sampling
    frame (docs/worldwide_sampling.md); this list is purposive, so they carry no
    meaning here and are left empty rather than half-filled. register_frame.py
    reads neither.
    """
    for row in manifest_rows:
        assert row["size_band"] == ""
        assert row["coverage_regime"] == ""
