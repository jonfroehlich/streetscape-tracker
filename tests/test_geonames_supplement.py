"""
data_sources/geonames_supplement.txt: verbatim GeoNames cities500 lines for
purposive-manifest towns too small to be in the vendored cities15000.txt, so a
manifest test can join every row back to vendored GeoNames data by geonameid.

The supplement is SHARED by every committed manifest, so this file holds its
own invariants rather than any one manifest's test:

* each line is a parseable 19-column GeoNames row, no geonameid appears twice
  (the dict load would hide a duplicate), and none is in cities15000;
* every row is used by some committed manifest, so it cannot grow into an
  unreviewed copy of cities500;
* tranche 1 of the Mapillary discovery screen (mapillary_discovery_cities.csv,
  #383, PR #419's branch — already registered on production, so it is joined,
  never edited) joins on geonameid against cities15000 plus the supplement,
  on name, lat, lon and population.

Tranche 1's manifest lands with PR #419, which must merge before this branch.
Until it does, the join of the manifest file skips and the ten cities500 rows
it needs are pinned here as literals (TRANCHE1_CITIES500_ROWS), which are
joined to the supplement unconditionally; once the file is present, the
literals must equal its cities500-only rows exactly.
"""

import csv
from pathlib import Path

import pytest

from scripts.build_worldwide_frame import load_cities

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_SOURCES = REPO_ROOT / "data_sources"
SUPPLEMENT = DATA_SOURCES / "geonames_supplement.txt"
TRANCHE1_MANIFEST = REPO_ROOT / "mapillary_discovery_cities.csv"
GEONAMES_COLUMNS = 19

# Tranche 1's rows that are not in cities15000, as its manifest writes them:
# geonameid -> (city, population, lat, lon). Copied from
# mapillary_discovery_cities.csv on PR #419's branch.
TRANCHE1_CITIES500_ROWS = {
    "4769339": ("Lexington", 7262, 37.78402, -79.44282),
    "4737676": ("Trophy Club", 11759, 32.9979, -97.18362),
    "5854718": ("Waipio Acres", 5531, 21.46485, -158.01331),
    "5086321": ("Francestown", 1571, 42.98758, -71.81258),
    "5090096": ("New Boston", 4934, 42.97619, -71.69396),
    "5400065": ("Sunnyslope", 5153, 34.01196, -117.43338),
    "4930183": ("Bedford", 12502, 42.49065, -71.27617),
    "7261418": ("Heber-Overgaard", 2822, 34.41414, -110.56956),
    "5089746": ("Mont Vernon", 2166, 42.89453, -71.67424),
    "5777332": ("Lindon", 10810, 40.34329, -111.72076),
}

# Tranche 1's two departures from GeoNames' ASCII name: its generator dropped
# the Hawaiian ʻokina, which GeoNames' asciiname spells as an apostrophe (one
# row is in cities15000, one in the supplement). The manifest is registered,
# so each departure is named here rather than corrected there.
# geonameid -> (GeoNames asciiname, manifest city).
TRANCHE1_NAME_DEPARTURES = {
    "5854718": ("Waipi'o Acres", "Waipio Acres"),
    "5855070": ("'Ewa Gentry", "Ewa Gentry"),
}


def _manifest_name(city):
    """The name tranche 1's manifest writes for a GeoNames record."""
    departure = TRANCHE1_NAME_DEPARTURES.get(city.geonameid)
    if departure:
        assert city.name == departure[0], city.geonameid  # else the departure is stale
        return departure[1]
    return city.name


@pytest.fixture(scope="module")
def cities15000():
    return {c.geonameid: c for c in load_cities(DATA_SOURCES / "cities15000.txt")}


@pytest.fixture(scope="module")
def supplement():
    return {c.geonameid: c for c in load_cities(SUPPLEMENT)}


def _committed_manifest_ids():
    """{manifest name: geonameids} for every committed manifest with that column."""
    paths = sorted(REPO_ROOT.glob("*.csv")) + [DATA_SOURCES / "geocode_overrides.csv"]
    out = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "geonameid" in (reader.fieldnames or []):
                out[path.name] = {r["geonameid"] for r in reader}
    return out


def test_supplement_lines_are_unique_geonames_rows_absent_from_cities15000(supplement, cities15000):
    lines = SUPPLEMENT.read_text(encoding="utf-8").splitlines()
    assert lines, "the supplement is empty"
    ids = [line.split("\t")[0] for line in lines]
    assert len(ids) == len(set(ids)), "a duplicated line"
    assert all(len(line.split("\t")) == GEONAMES_COLUMNS for line in lines)
    assert len(lines) == len(supplement), "an unparseable or non-P line"
    for gid in supplement:
        assert gid not in cities15000, gid


def test_every_supplement_row_is_used_by_a_committed_manifest(supplement):
    """
    Used by SOME manifest, not by one in particular: the supplement is shared.
    Tranche 1's ten rows count through their literals until its manifest lands.
    """
    used = set().union(*_committed_manifest_ids().values())
    if not TRANCHE1_MANIFEST.exists():
        used |= set(TRANCHE1_CITIES500_ROWS)
    unused = set(supplement) - used
    assert not unused, unused


def test_tranche1_cities500_rows_join_the_supplement(supplement):
    """Unconditional: the literals for tranche 1's cities500-only rows join."""
    for gid, (name, population, lat, lon) in TRANCHE1_CITIES500_ROWS.items():
        city = supplement[gid]
        assert _manifest_name(city) == name, gid
        assert city.population == population, gid
        assert city.lat == pytest.approx(lat), gid
        assert city.lon == pytest.approx(lon), gid


def test_tranche1_manifest_joins_vendored_geonames(cities15000, supplement):
    """
    Every row of mapillary_discovery_cities.csv matches its vendored GeoNames
    record (cities15000 or the supplement) on name, lat, lon and population,
    and its cities500-only rows are exactly TRANCHE1_CITIES500_ROWS.
    """
    if not TRANCHE1_MANIFEST.exists():
        pytest.skip("mapillary_discovery_cities.csv lands with PR #419")
    joinable = {**supplement, **cities15000}
    with open(TRANCHE1_MANIFEST, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 25
    for row in rows:
        gid = row["geonameid"]
        city = joinable[gid]
        assert row["city"] == _manifest_name(city), gid
        assert int(row["population"]) == city.population, gid
        assert float(row["lat"]) == pytest.approx(city.lat), gid
        assert float(row["lon"]) == pytest.approx(city.lon), gid
    cities500_only = {
        r["geonameid"]: (r["city"], int(r["population"]), float(r["lat"]), float(r["lon"]))
        for r in rows
        if r["geonameid"] not in cities15000
    }
    assert cities500_only == TRANCHE1_CITIES500_ROWS
