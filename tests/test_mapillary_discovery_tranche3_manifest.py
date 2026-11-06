"""
mapillary_discovery_cities_tranche3.csv (#441): provenance of the third
registration tranche of the Mapillary discovery screen.

What these tests hold, each against a committed file rather than a literal
copied from the same place:

* the manifest is what the generator writes from the committed ranked record,
  byte for byte, and the selection re-derives independently from that record;
* every row joins its vendored GeoNames record (cities15000 or one of the
  tranche's pinned supplement rows), and its frozen ``city_id`` is pinned;
* every query string is the one the committed vetting runs geocoded, and every
  row resolved there (no row registration's centre guard would skip);
* the record's rows satisfy the eligibility rules the re-score states;
* the three street-fitted grids are the committed fit record's, and each still
  contains its GeoNames point;
* the writeup's advance prediction is the metrics JSON's, and that JSON names
  committed code in its ``generated_by``.
"""

import csv
import json
import math
from pathlib import Path

import pytest

from scripts import build_mapillary_discovery_tranche3 as gen
from scripts.build_worldwide_frame import _MANIFEST_HEADER, load_cities
from scripts.register_frame import frame_identity
from streetscape_metadata_tracker import db

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "mapillary_discovery_cities_tranche3.csv"
EXP = REPO_ROOT / "docs" / "experiments"
RANKED = EXP / "mapillary-discovery-screen_tranche3_ranked.csv"
VET = EXP / "mapillary-discovery-screen_tranche3_vet.csv"
VET_RETRY = EXP / "mapillary-discovery-screen_tranche3_vet_retry.csv"
GRIDS = EXP / "mapillary-discovery-screen_tranche3_grids.json"
METRICS = EXP / "mapillary-discovery-screen_tranche3_metrics.json"
WRITEUP = EXP / "mapillary-discovery-screen.md"
DATA_SOURCES = REPO_ROOT / "data_sources"

# What register_frame.py will freeze into the catalog: filenames and URLs.
EXPECTED_CITY_IDS = {
    "5404122": "north-tustin--california--united-states",
    "5367314": "live-oak--california--united-states",
    "5387687": "rio-linda--california--united-states",
    "5347287": "fair-oaks--california--united-states",
    "7262791": "royal-kunia--hawaii--united-states",
    "5597955": "kuna--idaho--united-states",
    "5856516": "ahuimanu--hawaii--united-states",
    "5703670": "elko--nevada--united-states",
    "5850554": "makakilo-city--hawaii--united-states",
    "4162783": "longwood--florida--united-states",
    "4385835": "eureka--missouri--united-states",
    "5582392": "sterling--colorado--united-states",
    "5843495": "worland--wyoming--united-states",
    "5850511": "makaha--hawaii--united-states",
    "5345032": "east-palo-alto--california--united-states",
    "5398563": "stanford--california--united-states",
    "5250399": "delavan--wisconsin--united-states",
    "4855806": "evansdale--iowa--united-states",
    "5266483": "pell-lake--wisconsin--united-states",
    "5327455": "belmont--california--united-states",
    "5334320": "carmel-by-the-sea--california--united-states",
    "5777793": "magna--utah--united-states",
    "5338783": "colton--california--united-states",
    "5784731": "white-city--utah--united-states",
    "5375938": "muscoy--california--united-states",
}

# The fit record keys its three towns by short name.
GRID_KEYS = {"5597955": "kuna", "5777793": "magna", "5334320": "carmel"}


def _read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def manifest_rows():
    return _read_csv(MANIFEST)


@pytest.fixture(scope="module")
def ranked():
    return _read_csv(RANKED)


@pytest.fixture(scope="module")
def vetted():
    """geonameid -> the vetting row that resolved it (the retry wins)."""
    out = {}
    for path in (VET, VET_RETRY):
        for row in _read_csv(path):
            if row["width_m"]:
                out[row["geonameid"]] = row
    return out


@pytest.fixture(scope="module")
def geonames():
    cities = {c.geonameid: c for c in load_cities(DATA_SOURCES / "cities15000.txt")}
    supplement = {c.geonameid: c for c in load_cities(DATA_SOURCES / "geonames_supplement.txt")}
    return cities, supplement


def test_manifest_is_what_the_generator_writes(tmp_path):
    out = tmp_path / "manifest.csv"
    assert gen.main(["--manifest-out", str(out)]) == 0
    assert out.read_bytes() == MANIFEST.read_bytes()


def test_header_rows_and_city_ids(manifest_rows):
    assert list(manifest_rows[0]) == _MANIFEST_HEADER
    ids = [r["geonameid"] for r in manifest_rows]
    assert ids == list(EXPECTED_CITY_IDS)  # also pins the order (V5 descending)
    for row in manifest_rows:
        city_id = db.derive_city_id(*frame_identity(row))
        assert city_id == EXPECTED_CITY_IDS[row["geonameid"]]
        assert city_id.isascii()


def test_selection_rederives_from_the_record(manifest_rows, ranked):
    """
    Independently of the generator: the first 25 record rows once any row
    whose uploader already holds five earlier rows is skipped. The record's
    own ``rank_in_user`` column must agree with a fresh count.
    """
    seen = {}
    for row in ranked:
        seen[row["user"]] = seen.get(row["user"], 0) + 1
        assert int(row["rank_in_user"]) == seen[row["user"]], row["geonameid"]
    expected = [r["geonameid"] for r in ranked if int(r["rank_in_user"]) <= 5][:25]
    assert [r["geonameid"] for r in manifest_rows] == expected
    assert len(ranked) == 105


def test_record_rows_satisfy_the_rescore_rules(ranked):
    """The eligibility rules tier3.py applied, re-checked on the record."""
    for r in ranked:
        gid = r["geonameid"]
        assert float(r["v4"]) >= 0.7, gid
        assert float(r["top_share"]) >= 0.6, gid
        assert float(r["foot_share"]) < 0.5, gid
        assert float(r["med_age"]) <= 2, gid
        assert int(r["pop"]) <= 60000, gid
        assert r["cc"] in {"US", "CA"}, gid
        assert float(r["catalog_km"]) > 5, gid
    v5 = [float(r["v5"]) for r in ranked]
    assert v5 == sorted(v5, reverse=True)


def test_record_rows_are_five_km_apart(ranked):
    for i, a in enumerate(ranked):
        for b in ranked[:i]:
            la = math.radians(float(a["lat"]))
            dx = (float(a["lon"]) - float(b["lon"])) * 111.32 * math.cos(la)
            dy = (float(a["lat"]) - float(b["lat"])) * 110.57
            assert math.hypot(dx, dy) >= 5, (a["name"], b["name"])


def test_every_row_joins_its_vendored_geonames_record(manifest_rows, ranked, geonames):
    cities, supplement = geonames
    by_id = {r["geonameid"]: r for r in ranked}
    for row in manifest_rows:
        gid = row["geonameid"]
        city = cities.get(gid) or supplement[gid]
        name = gen.NAME_DEPARTURES.get(gid, (city.name, city.name))
        assert city.name == name[0], gid
        assert row["city"] == name[1], gid
        assert int(row["population"]) == city.population == int(by_id[gid]["pop"]), gid
        assert float(row["lat"]) == pytest.approx(city.lat)
        assert float(row["lon"]) == pytest.approx(city.lon)
        # The record rounds to 3 decimals.
        assert float(by_id[gid]["lat"]) == pytest.approx(city.lat, abs=6e-4), gid
        assert float(by_id[gid]["lon"]) == pytest.approx(city.lon, abs=6e-4), gid


def test_pinned_supplement_rows_are_exactly_the_cities500_only_rows(manifest_rows, geonames):
    cities, supplement = geonames
    cities500_only = {r["geonameid"] for r in manifest_rows if r["geonameid"] not in cities}
    assert cities500_only == gen.TRANCHE3_SUPPLEMENT_IDS
    assert gen.TRANCHE3_SUPPLEMENT_IDS <= set(supplement)


def test_every_query_is_the_one_vetting_geocoded(manifest_rows, vetted):
    """
    And every row resolved: a row the vet CSVs could not resolve would be
    skipped by registration's --max-center-km 10 guard.
    """
    assert set(vetted) == {r["geonameid"] for r in manifest_rows}
    for row in manifest_rows:
        v = vetted[row["geonameid"]]
        assert row["query_string"] == v["geocode_query"], row["geonameid"]
        assert float(v["offset_km"]) <= 10, row["geonameid"]
    retried = {r["geonameid"] for r in _read_csv(VET_RETRY)}
    assert retried == set(gen.QUERY_OVERRIDES)


def test_the_two_narrow_reuse_margins_clear_registrations_radius(manifest_rows):
    """
    register_frame.py's --overlap-km 5 measures from the manifest's own
    lat/lon. The record's catalog_km is a flat approximation over 3-decimal
    coordinates, so the two rows nearest the radius are held here with the
    registration's own haversine, against the catalog centres the runbook names.
    """
    from scripts.register_frame import _haversine_km

    neighbours = {
        # Catalog centres from the 2026-11-05 production snapshot.
        "5347287": (38.678514500000006, -121.2257818, 5.515),  # Fair Oaks -> orangevale--california
        "5250399": (42.584182, -88.632602, 5.512),  # Delavan -> delavan-lake--wisconsin
    }
    by_id = {r["geonameid"]: r for r in manifest_rows}
    for gid, (lat, lon, km) in neighbours.items():
        row = by_id[gid]
        d = _haversine_km(float(row["lat"]), float(row["lon"]), lat, lon)
        assert d == pytest.approx(km, abs=0.002), gid
        assert d > 5.0, gid


def test_grid_fits_are_the_committed_fit_record(manifest_rows, vetted):
    fits = json.loads(GRIDS.read_text(encoding="utf-8"))
    assert set(fits) == set(GRID_KEYS.values())
    assert set(gen.GRID_FITS) == set(GRID_KEYS)
    for gid, key in GRID_KEYS.items():
        lat, lon, w, h = gen.GRID_FITS[gid]
        f = fits[key]
        assert [lat, lon] == f["new_center"], key
        assert [w, h] == f["new_wh"], key
        # The fit replaces the vetted (registration) grid and shrinks it.
        v = vetted[gid]
        assert f["old_wh"] == [int(v["width_m"]), int(v["height_m"])], key
        assert w * h < int(v["width_m"]) * int(v["height_m"]), key
        assert f["geo_pt_in_new"] is True, key
    by_id = {r["geonameid"]: r for r in manifest_rows}
    for gid, (lat, lon, w, h) in gen.GRID_FITS.items():
        row = by_id[gid]
        dx = (float(row["lon"]) - lon) * 111.32 * math.cos(math.radians(lat)) * 1000
        dy = (float(row["lat"]) - lat) * 110.57 * 1000
        assert abs(dx) <= w / 2 and abs(dy) <= h / 2, gid


def _tranche_totals(vetted):
    """(vetted GSV, vetted tiles, fitted GSV, fitted tiles) over the 25 rows."""
    fits = json.loads(GRIDS.read_text(encoding="utf-8"))
    gsv = sum(int(v["gsv_points"]) for v in vetted.values())
    tiles = sum(int(v["mapillary_z14_tiles"]) for v in vetted.values())
    fit_gsv, fit_tiles = gsv, tiles
    for gid, key in GRID_KEYS.items():
        fit_gsv += fits[key]["gsv_new"] - int(vetted[gid]["gsv_points"])
        fit_tiles += fits[key]["tiles_new"] - int(vetted[gid]["mapillary_z14_tiles"])
    return gsv, tiles, fit_gsv, fit_tiles


def test_the_docs_tranche_totals_are_the_committed_records(vetted):
    gsv, tiles, fit_gsv, fit_tiles = _tranche_totals(vetted)
    writeup = WRITEUP.read_text(encoding="utf-8")
    assert (
        f"the tranche is {fit_gsv:,} GSV grid points and {fit_tiles} Mapillary z14 tiles" in writeup
    )
    assert f"down from {gsv:,} and {tiles} on the vetted grids" in writeup
    ops = (REPO_ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    assert f"{fit_gsv:,} GSV grid points and {fit_tiles} Mapillary z14 tiles" in ops


def test_advance_prediction_is_the_metrics_jsons(ranked):
    metrics = json.loads(METRICS.read_text(encoding="utf-8"))
    for path in metrics["generated_by"]:
        assert (REPO_ROOT / path).is_file(), path
    pred = metrics["advance_prediction_tranche3"]
    t7 = [
        r
        for r in metrics["v4_threshold"]
        if r["v4_at_least"] == 0.7 and r["population"] == "tranche"
    ]
    assert len(t7) == 1
    assert pred["rate"] == round(t7[0]["good2"] / t7[0]["n"], 3)
    assert pred["n"] == gen.CAP
    text = WRITEUP.read_text(encoding="utf-8")
    assert f"fewer than **{pred['falsified_below']}**" in text
    assert f"{t7[0]['good2']} of {t7[0]['n']}" in text
    # The prediction's basis needs every row at the threshold it was measured at.
    assert min(float(r["v4"]) for r in ranked[:40]) >= 0.7


def test_generator_select_caps_per_uploader_and_in_total():
    rows = [{"user": "a", "geonameid": str(i)} for i in range(7)]
    rows += [{"user": f"u{i}", "geonameid": f"x{i}"} for i in range(40)]
    kept = gen.select(rows)
    assert len(kept) == gen.CAP
    assert [r["geonameid"] for r in kept[:5]] == ["0", "1", "2", "3", "4"]
    assert {"5", "6"}.isdisjoint(r["geonameid"] for r in kept)
    assert kept[5]["geonameid"] == "x0"
