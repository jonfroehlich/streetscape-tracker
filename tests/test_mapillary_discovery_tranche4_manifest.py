"""
The fourth Mapillary discovery manifest (mapillary_discovery_cities_tranche4.csv):
six places read off a 2026-11-19 sweep of ONE uploader's Mapillary sequences,
the uploader the screen's own record already ranked for Laurens, Iowa
(docs/experiments/mapillary-discovery-screen.md, "Tranche 4").

Unlike tranches 2 and 3 the list is CURATED, not selected by a rule over a
committed score, so there is no generator to re-run and no ranked record to
re-derive it from. What is held here is everything that can drift after the
choice was made:

* each row's VALUES, a join against the vendored GeoNames data keyed by
  geonameid (cities15000 for Beaver Dam and Duluth, the cities500 supplement
  for the four Walworth County villages), never hand-typed;
* each row's query, which must be the one the committed vetting record
  geocoded (Walworth's is county-qualified: the bare name matched Walworth
  COUNTY, 16 km off);
* the city_ids registration will freeze;
* the reuse margins `register_frame.py --overlap-km` sees, from the committed
  overlap record, against the radius the runbook passes;
* the runbook's and the writeup's totals, against the vetting record.

It deliberately pins NO frozen geometry: nothing was registered when this was
written, so there is no prod state to record yet.
"""

import csv
import json
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
MANIFEST = REPO_ROOT / "mapillary_discovery_cities_tranche4.csv"
EXP = REPO_ROOT / "docs" / "experiments"
VET = EXP / "mapillary-discovery-screen_tranche4_vet.csv"
VET_RETRY = EXP / "mapillary-discovery-screen_tranche4_vet_retry.csv"
OVERLAPS = EXP / "mapillary-discovery-screen_tranche4_overlaps.json"
OPS = REPO_ROOT / "docs" / "operations.md"
WRITEUP = EXP / "mapillary-discovery-screen.md"
DATA_SOURCES = REPO_ROOT / "data_sources"

# The slugs registration will freeze. Order matches the manifest.
EXPECTED_CITY_IDS = {
    "5245193": "beaver-dam--wisconsin--united-states",
    "5259169": "lake-geneva--wisconsin--united-states",
    "5279098": "williams-bay--wisconsin--united-states",
    "5254150": "genoa-city--wisconsin--united-states",
    "5277801": "walworth--wisconsin--united-states",
    "5024719": "duluth--minnesota--united-states",
}

# The one query that is not the frame generator's "City, Admin, Country".
QUERY_OVERRIDES = {"5277801": "Walworth, Walworth County, Wisconsin, United States"}

# The four rows too small for cities15000, joined through the supplement.
SUPPLEMENT_IDS = {"5259169", "5279098", "5254150", "5277801"}

# The runbook's --overlap-km, and the headroom every reuse margin must keep
# above it (tranche 3's rule: a geocode may drift on the day).
OVERLAP_KM = 3.0
HEADROOM_KM = 1.5

# The runbook's two nights, by city name.
NIGHTS = {
    1: "Walworth, Williams Bay, Genoa City, Lake Geneva, Beaver Dam",
    2: "Duluth",
}


@pytest.fixture(scope="module")
def manifest_rows():
    with open(MANIFEST, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def geonames():
    """The vendored GeoNames join: the two city files, admin-1 names, countries."""
    return SimpleNamespace(
        big={c.geonameid: c for c in load_cities(DATA_SOURCES / "cities15000.txt")},
        small={c.geonameid: c for c in load_cities(DATA_SOURCES / "geonames_supplement.txt")},
        admin=load_admin1(DATA_SOURCES / "admin1CodesASCII.txt"),
        countries=load_countries(DATA_SOURCES / "countryInfo.txt"),
    )


@pytest.fixture(scope="module")
def vetted():
    """geonameid -> the vetting row that RESOLVED (the retry file wins)."""
    out = {}
    for path in (VET, VET_RETRY):
        with open(path, encoding="utf-8") as f:
            out.update({r["geonameid"]: r for r in csv.DictReader(f) if r["width_m"]})
    return out


@pytest.fixture(scope="module")
def overlap_rows():
    with open(OVERLAPS, encoding="utf-8") as f:
        return {r["geonameid"]: r for r in json.load(f)["rows"]}


def test_header_rows_and_city_ids(manifest_rows):
    """register_frame.py reads this file; the slugs are what it will freeze."""
    assert list(manifest_rows[0]) == _MANIFEST_HEADER
    assert [r["geonameid"] for r in manifest_rows] == list(EXPECTED_CITY_IDS)
    for row in manifest_rows:
        city_id = db.derive_city_id(*frame_identity(row))
        assert city_id == EXPECTED_CITY_IDS[row["geonameid"]]
        assert city_id.isascii()
        assert row["size_band"] == "" and row["coverage_regime"] == ""


def test_every_row_joins_its_vendored_geonames_record(manifest_rows, geonames):
    """The join, in reverse, and which file each row came from."""
    for row in manifest_rows:
        gid = row["geonameid"]
        assert (gid in geonames.small) == (gid in SUPPLEMENT_IDS), gid
        assert (gid in geonames.big) != (gid in geonames.small), gid
        city = geonames.big.get(gid) or geonames.small[gid]
        country = geonames.countries[city.iso2]
        assert row["city"] == city.name, gid
        assert row["iso2"] == city.iso2, gid
        assert row["country"] == country.name, gid
        assert row["continent"] == country.continent, gid
        assert int(row["population"]) == city.population, gid
        assert float(row["lat"]) == pytest.approx(city.lat), gid
        assert float(row["lon"]) == pytest.approx(city.lon), gid
        assert row["admin"] == geonames.admin.get(f"{city.iso2}.{city.admin1}", ""), gid


def test_queries_are_the_generators_except_the_named_override(manifest_rows, geonames):
    for row in manifest_rows:
        gid = row["geonameid"]
        city = geonames.big.get(gid) or geonames.small[gid]
        record = SimpleNamespace(city=city, iso2=city.iso2, country=row["country"])
        generated = query_string(record, geonames.admin)
        assert row["query_string"] == QUERY_OVERRIDES.get(gid, generated), gid
        assert (row["query_string"] != generated) == (gid in QUERY_OVERRIDES), gid


def test_every_query_is_the_one_vetting_geocoded(manifest_rows, vetted):
    """
    A row's price and margins describe the grid vetting saw only if
    registration sends the same query. Every row resolved on a boundary,
    unflagged, from the geocoded centre, within the runbook's 10 km guard.
    """
    assert set(vetted) == set(EXPECTED_CITY_IDS)
    for row in manifest_rows:
        v = vetted[row["geonameid"]]
        assert v["geocode_query"] == row["query_string"], row["city"]
        assert v["osm_match"] == "boundary/administrative", row["city"]
        assert v["center"] == "geocoded" and v["flags"] == "", row["city"]
        assert float(v["offset_km"]) <= 2.1, row["city"]


def test_walworths_bare_query_is_recorded_as_the_county(manifest_rows):
    """Why the override exists: the first vetting pass refused the bare name."""
    with open(VET, encoding="utf-8") as f:
        first = {r["geonameid"]: r for r in csv.DictReader(f)}
    assert first["5277801"]["width_m"] == ""
    assert "16 km from the GeoNames coordinates" in first["5277801"]["flags"]


def test_registration_reuse_margins_leave_headroom_at_the_runbooks_radius(
    manifest_rows, overlap_rows, vetted
):
    """
    find_overlap measures from a row's GeoNames point to every catalog centre,
    and with --execute to the GEOCODED centres of rows the same run registered
    earlier. The record holds both margins per row, on the vetted grid.
    """
    assert list(overlap_rows) == [r["geonameid"] for r in manifest_rows]
    for gid, r in overlap_rows.items():
        v = vetted[gid]
        assert r["grid"] == [
            float(v["center_lat"]),
            float(v["center_lon"]),
            int(v["width_m"]),
            int(v["height_m"]),
        ]
        assert r["gsv_lattice_points"] == int(v["gsv_points"])
    margins = [r["nearest_catalog_km"] for r in overlap_rows.values()]
    margins += [
        r["nearest_earlier_row_km"] for r in overlap_rows.values() if "nearest_earlier_row_km" in r
    ]
    assert min(margins) >= OVERLAP_KM + HEADROOM_KM
    # The margin that sets the radius: tranche 3's 4 km would leave 0.6 km.
    lake_geneva = overlap_rows["5259169"]
    assert lake_geneva["nearest_catalog"] == "como--wisconsin--united-states"
    assert lake_geneva["nearest_catalog_km"] == pytest.approx(4.609, abs=0.001)
    assert min(margins) == lake_geneva["nearest_catalog_km"]
    ops = OPS.read_text(encoding="utf-8")
    assert '--notes-label "mapillary discovery screen 2026-11-19 tranche 4" --overlap-km 3 ' in ops


def test_the_writeups_overlaps_are_the_overlap_record(overlap_rows):
    """Two grids share ground with a tracked neighbour; the writeup says how much."""
    shares = {
        r["city"]: r["catalog_overlaps"] for r in overlap_rows.values() if r["catalog_overlaps"]
    }
    assert shares == {
        "Lake Geneva": {"como--wisconsin--united-states": 0.057},
        "Genoa City": {"tranche3:Pell Lake": 0.113},
    }
    writeup = WRITEUP.read_text(encoding="utf-8")
    assert "5.7% of Lake Geneva's grid lies inside Como's" in writeup
    assert "11.3% of Genoa City's inside Pell Lake's" in writeup


def test_the_runbooks_night_table_is_the_vetting_record(manifest_rows, vetted):
    by_name = {r["city"]: r["geonameid"] for r in manifest_rows}
    assert sorted(n for names in NIGHTS.values() for n in names.split(", ")) == sorted(by_name)
    ops = OPS.read_text(encoding="utf-8")
    for night, names in NIGHTS.items():
        rows = [vetted[by_name[n]] for n in names.split(", ")]
        gsv, tiles, kv, px = (
            sum(int(r[k]) for r in rows)
            for k in (
                "gsv_points",
                "mapillary_z14_tiles",
                "kartaview_requests",
                "panoramax_z15_tiles",
            )
        )
        line = (
            f"| {night} | {names} | {gsv:,} ({gsv / 48000 / 60:.2f}) | {tiles} | "
            f"≤ {kv:,} | ≤ {px:,} ({round(px / 60)}) |"
        )
        assert line in ops, line


def test_the_docs_totals_are_the_vetting_record(vetted):
    gsv = sum(int(r["gsv_points"]) for r in vetted.values())
    tiles = sum(int(r["mapillary_z14_tiles"]) for r in vetted.values())
    assert (gsv, tiles) == (2_238_953, 388)
    duluth = vetted["5024719"]
    assert (int(duluth["gsv_points"]), int(duluth["mapillary_z14_tiles"])) == (1_873_872, 306)
    for path in (OPS, WRITEUP):
        text = path.read_text(encoding="utf-8")
        assert "2,238,953 GSV grid points and 388 Mapillary z14 tiles" in text, path.name
        assert "1,873,872" in text and "306" in text, path.name
