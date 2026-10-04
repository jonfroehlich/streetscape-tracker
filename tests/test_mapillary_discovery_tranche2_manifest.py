"""
The second tranche of the Mapillary discovery screen (#383):
mapillary_discovery_cities_tranche2.csv, registered with
scripts/register_frame.py --manifest.

The rows are the output of a RULE over the screen's committed per-place record,
docs/experiments/mapillary-discovery-screen_tranche2.csv (one row per screen
candidate, with its score and the rule's decision), written by
scripts/build_mapillary_discovery_tranche2.py. These tests are that rule run
again, independently of the script, plus the GeoNames join run in reverse:

* every manifest row matches its vendored GeoNames record and its
  query_string is what build_worldwide_frame.query_string writes;
* the city_ids are pinned as literals, because registration freezes them;
* the selection is re-derived from the record: score >= 3, more than 25 km
  from every city the catalog knows (the 2026-10-02 production snapshot, whose
  distance is a committed column, plus the 25 tranche-1 towns registered the
  same day, re-checked here from literals), joinable from the vendored
  cities15000, geometry resolved at vetting, no two rows within 25 km (greedy,
  GIS_ISG / UAS_ISG towns first), at most 30 rows with those towns ahead of
  the cap, in descending score;
* each row's score is the score OF THAT PLACE, at the very point registered —
  the defect #428's review found in Kortrijk (admitted on a neighbour's
  imagery) cannot happen here.

The GeoNames and query_string assertions duplicate
tests/test_mapillary_360_cities_manifest.py rather than sharing a helper, so
the file pinning that registered batch's slugs is not edited.
"""

import csv
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_mapillary_discovery_tranche2 as gen
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
MANIFEST = REPO_ROOT / "mapillary_discovery_cities_tranche2.csv"
RECORD = REPO_ROOT / "docs" / "experiments" / "mapillary-discovery-screen_tranche2.csv"
DATA_SOURCES = REPO_ROOT / "data_sources"
OTHER_MANIFESTS = [REPO_ROOT / "mapillary_360_cities.csv", REPO_ROOT / "worldwide_frame.csv"]

SCORE_FLOOR = 3.0
REUSE_RADIUS_KM = 25.0
CAP = 30
FAVOURED_CREATORS = {"1361362982257515", "1006984308465014"}  # GIS_ISG, UAS_ISG

# Registered on production AFTER the 2026-10-02 catalog snapshot the record's
# nearest_known_km was measured against: the 25 towns of
# mapillary_discovery_cities.csv (tranche 1, PR #419's branch; Cedar Falls
# among them), registered 2026-10-02 and enabled the same day. Montreal and
# Ottawa are in the snapshot already, and are repeated so a row cannot land
# next to either. Coordinates are those manifests' GeoNames points.
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
    "Ottawa ON": (45.41117, -75.69812),
    "Petersburg VA": (37.22793, -77.40193),
    "State College PA": (40.79339, -77.86),
    "Sunnyslope CA": (34.01196, -117.43338),
    "Trophy Club TX": (32.9979, -97.18362),
    "Waipio Acres HI": (21.46485, -158.01331),
}

# Rows the rule admits whose geometry did NOT resolve in the one vetting run
# (scripts/vet_manifest_geometry.py, 2026-10-04): each geocoded to a county or
# a namesake more than --max-center-km 10 from its GeoNames point, so
# register_frame.py would skip it. Kept out until a tested query override
# exists (the suggested ones are in the generator's VETTING_FAILED).
VETTING_FAILED = {
    "5703670": "Elko NV -> Elko County, 32 km off",
    "5367314": "Live Oak CA -> Live Oak in Sutter County, 257 km off",
    "4130430": "Searcy AR -> Searcy County, 113 km off",
}

# No query overrides: the three bad geocodes above had no tested fix, and an
# untested override is not committed (docs/worldwide_sampling.md).
QUERY_OVERRIDES: dict[str, str] = {}

# The favoured uploaders' candidates and why none is a row. The owner's
# priority is more towns like Laurens, Iowa (GIS_ISG); this pins that the
# tranche does NOT deliver them, and why, so it cannot be read as an oversight.
FAVOURED_CANDIDATE_DECISIONS = {
    "Como": "cities500 only",
    "Delavan Lake": "within 25 km of a known city",
    "Fergus Falls": "within 25 km of a known city",
}

# Permanent slugs, frozen at registration. Order matches the manifest.
EXPECTED_CITY_IDS = {
    "5403191": "tracy--california--united-states",
    "5511077": "reno--nevada--united-states",
    "5410430": "woodland--california--united-states",
    "5382146": "perris--california--united-states",
    "5779548": "payson--utah--united-states",
    "4893392": "galesburg--illinois--united-states",
    "5205849": "phoenixville--pennsylvania--united-states",
    "5325187": "atwater--california--united-states",
    "5088262": "keene--new-hampshire--united-states",
    "5019588": "buffalo--minnesota--united-states",
    "4504476": "toms-river--new-jersey--united-states",
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
def record():
    with open(RECORD, encoding="utf-8") as f:
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


def _rederive(record, vendored_ids):
    """
    The selection rule over the committed record, written independently of
    the generator: {geonameid: decision} and the selected ids in manifest order.
    """
    decisions = {}
    pool = []
    for r in record:
        gid = r["geonameid"]
        if float(r["score_km_per_km2"]) < SCORE_FLOOR:
            decisions[gid] = "below score floor"
        elif float(r["nearest_known_km"]) <= REUSE_RADIUS_KM:
            decisions[gid] = "within 25 km of a known city"
        elif gid not in vendored_ids:
            decisions[gid] = "cities500 only"
        elif gid in VETTING_FAILED:
            decisions[gid] = "failed geometry vetting"
        else:
            pool.append(r)
    pool.sort(
        key=lambda r: (r["top_creator_id"] not in FAVOURED_CREATORS, -float(r["score_km_per_km2"]))
    )
    kept = []
    for r in pool:
        clash = [
            k
            for k in kept
            if _haversine_km(float(r["lat"]), float(r["lon"]), float(k["lat"]), float(k["lon"]))
            <= REUSE_RADIUS_KM
        ]
        if clash:
            decisions[r["geonameid"]] = f"within 25 km of selected {clash[0]['name']}"
        else:
            kept.append(r)
    favoured = [r for r in kept if r["top_creator_id"] in FAVOURED_CREATORS]
    others = [r for r in kept if r["top_creator_id"] not in FAVOURED_CREATORS]
    chosen = favoured + others[: max(0, CAP - len(favoured))]
    for r in kept:
        decisions[r["geonameid"]] = "selected" if r in chosen else "over the cap"
    chosen.sort(key=lambda r: -float(r["score_km_per_km2"]))
    return decisions, [r["geonameid"] for r in chosen]


# --- the manifest's GeoNames provenance --------------------------------------


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
    "City, Admin, Country", as the worldwide frame writes it; there are no
    overrides, and the generator agrees there are none.
    """
    assert gen.QUERY_OVERRIDES == QUERY_OVERRIDES == {}
    for row in manifest_rows:
        city = geonames.cities[row["geonameid"]]
        record = SimpleNamespace(city=city, iso2=city.iso2, country=row["country"])
        assert row["query_string"] == query_string(record, geonames.admin), row["geonameid"]


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


# --- the selection, re-derived from the committed record ---------------------


def test_record_flags_cities15000_membership_truthfully(record, geonames):
    """The one record column the test can recompute from vendored data, it does."""
    for r in record:
        expected = "yes" if r["geonameid"] in geonames.cities else "no"
        assert r["in_cities15000"] == expected, r["name"]


def test_selection_rederives_from_the_record(manifest_rows, record, geonames):
    """
    The rule, run again here: the manifest is exactly its output, in order, and
    every candidate's committed decision is the one the rule gives it.
    """
    decisions, chosen = _rederive(record, set(geonames.cities))
    assert [row["geonameid"] for row in manifest_rows] == chosen
    for r in record:
        assert r["decision"] == decisions[r["geonameid"]], r["name"]


def test_each_row_is_scored_at_its_own_registered_point(manifest_rows, record):
    """
    The anti-Kortrijk check (#428's review): the score that admitted a row was
    measured within 2 km of the very GeoNames point registration centres on,
    and clears the floor on its own.
    """
    by_id = {r["geonameid"]: r for r in record}
    for row in manifest_rows:
        scored = by_id[row["geonameid"]]
        assert float(scored["lat"]) == pytest.approx(float(row["lat"])), row["city"]
        assert float(scored["lon"]) == pytest.approx(float(row["lon"])), row["city"]
        assert float(scored["score_km_per_km2"]) >= SCORE_FLOOR, row["city"]


def test_rows_are_in_descending_score(manifest_rows, record):
    """So register_frame.py --limit N registers the strongest N first."""
    by_id = {r["geonameid"]: r for r in record}
    scores = [float(by_id[row["geonameid"]]["score_km_per_km2"]) for row in manifest_rows]
    assert scores == sorted(scores, reverse=True)


def test_vetting_failures_are_named_and_agree_with_the_generator(record):
    assert set(gen.VETTING_FAILED) == set(VETTING_FAILED)
    failed = {r["geonameid"] for r in record if r["decision"] == "failed geometry vetting"}
    assert failed == set(VETTING_FAILED)


def test_favoured_creators_candidates_and_why_none_is_a_row(record):
    favoured = {
        r["name"]: r["decision"] for r in record if r["top_creator_id"] in FAVOURED_CREATORS
    }
    assert favoured == FAVOURED_CANDIDATE_DECISIONS
    assert set(gen.FAVOURED_CREATORS) == FAVOURED_CREATORS


def test_no_two_rows_are_within_the_reuse_radius(manifest_rows):
    points = [(r["city"], float(r["lat"]), float(r["lon"])) for r in manifest_rows]
    for i, (a, alat, alon) in enumerate(points):
        for b, blat, blon in points[i + 1 :]:
            assert _haversine_km(alat, alon, blat, blon) > REUSE_RADIUS_KM, (a, b)


def test_no_row_is_within_the_reuse_radius_of_a_known_registration(manifest_rows, record):
    """
    The record's nearest_known_km saw the production snapshot; this re-checks
    each row against what was registered after it (tranche 1) and the committed
    manifests. It also holds that column to be no larger than the distance to
    the nearest tranche-1 town, i.e. that the record was built WITH tranche 1
    in its known set (Montreal and Ottawa are in the snapshot at their
    geocoded centres, so they are left out of that comparison).
    """
    others = dict(POST_SNAPSHOT_CITIES)
    for path in OTHER_MANIFESTS:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                others[f"{path.name}:{r['city']}"] = (float(r["lat"]), float(r["lon"]))
    tranche1 = {
        k: v for k, v in POST_SNAPSHOT_CITIES.items() if k not in ("Montreal QC", "Ottawa ON")
    }
    by_id = {r["geonameid"]: r for r in record}
    for row in manifest_rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        nearest = min(_haversine_km(lat, lon, olat, olon) for olat, olon in others.values())
        assert nearest > REUSE_RADIUS_KM, row["city"]
        recorded = float(by_id[row["geonameid"]]["nearest_known_km"])
        nearest_t1 = min(_haversine_km(lat, lon, olat, olon) for olat, olon in tranche1.values())
        assert REUSE_RADIUS_KM < recorded <= round(nearest_t1, 1) + 0.05, row["city"]


# --- the generator's rule on synthetic candidates ----------------------------


def _cand(gid, lat, lon, score, creator="1"):
    return {
        "geonameid": gid,
        "name": f"P{gid}",
        "lat": str(lat),
        "lon": str(lon),
        "km_per_km2": str(score),
        "top_creator": creator,
    }


def test_generator_keeps_a_favoured_town_over_a_richer_neighbour():
    far = {"elsewhere": (0.0, 0.0)}
    rich = _cand("1", 40.0, -100.0, 9.0)
    favoured = _cand("2", 40.05, -100.0, 3.5, creator="1361362982257515")  # ~5.6 km away
    chosen = gen.select([rich, favoured], far, {"1", "2"})
    assert [c["geonameid"] for c in chosen] == ["2"]
    assert rich["decision"] == "within 25 km of selected P2"


def test_generator_puts_favoured_towns_ahead_of_the_cap(monkeypatch):
    monkeypatch.setattr(gen, "CAP", 2)
    far = {"elsewhere": (0.0, 0.0)}
    cands = [_cand(str(i), 30.0 + i, -100.0, 10.0 - i) for i in range(3)]
    cands.append(_cand("9", 45.0, -100.0, 3.1, creator="1006984308465014"))
    chosen = gen.select(cands, far, {c["geonameid"] for c in cands})
    assert [c["geonameid"] for c in chosen] == ["0", "9"]  # descending score
    assert cands[1]["decision"] == cands[2]["decision"] == "over the cap"


def test_generator_reports_cities500_known_and_vetting_drops():
    known = {"tracked": (40.0, -100.0)}
    near = _cand("1", 40.1, -100.0, 8.0)  # ~11 km from a known city
    small = _cand("2", 42.0, -100.0, 7.0)  # not vendored
    failed = _cand("5703670", 44.0, -100.0, 6.0)  # Elko's id
    weak = _cand("4", 46.0, -100.0, 2.9)
    ok = _cand("5", 48.0, -100.0, 3.0)
    chosen = gen.select([near, small, failed, weak, ok], known, {"1", "5703670", "4", "5"})
    assert [c["geonameid"] for c in chosen] == ["5"]
    assert near["decision"] == "within 25 km of a known city"
    assert small["decision"] == "cities500 only"
    assert failed["decision"] == "failed geometry vetting"
    assert weak["decision"] == "below score floor"
