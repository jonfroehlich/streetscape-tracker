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
  same day, re-checked here from literals) unless named in
  OPERATOR_EXCEPTIONS, joinable from the vendored cities15000 or this
  tranche's three data_sources/geonames_supplement.txt rows
  (TRANCHE2_SUPPLEMENT_IDS — pinned, so a later tranche adding supplement
  rows cannot change this frozen record's decisions), geometry resolved at
  vetting, no two rows within 25 km (greedy, GIS_ISG / UAS_ISG towns first),
  at most 30 rows with those towns ahead of the cap, in descending score;
* each row's score is the score OF THAT PLACE, measured at its own GeoNames
  point — the defect #428's review found in Kortrijk (admitted on a
  neighbour's imagery) cannot happen here. Registration centres the grid on
  the GEOCODE, not on that point (0.1-5.5 km away), so a separate test
  requires the scored point inside the vetted grid and at least three
  quarters of the scored disc's area with it;
* every vetting number the tests and docs quote (grids, offsets, the
  failures, the pricing table) traces to the three committed vet CSVs,
  docs/experiments/mapillary-discovery-screen_tranche2_vet*.csv.

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
WORLDWIDE_SAMPLING_DOC = REPO_ROOT / "docs" / "worldwide_sampling.md"

# The three runs of scripts/vet_manifest_geometry.py --csv, committed verbatim:
# the 14 rows the rule admitted before vetting (2026-10-04), then Fergus Falls
# alone and Delavan Lake + Como (2026-10-05), each after its exception.
VET_CSVS = [
    REPO_ROOT / "docs" / "experiments" / f"mapillary-discovery-screen_tranche2_{name}.csv"
    for name in ("vet", "vet_fergus", "vet_lakes")
]

SCORE_FLOOR = 3.0
REUSE_RADIUS_KM = 25.0
CAP = 30
SCORE_DISC_KM = 2.0  # the screen scored recent-360° km within 2 km of the point
MIN_DISC_INSIDE_GRID = 0.75  # measured minimum 0.771 (Como, n=200 lattice); see the test

# This tranche's joinable set is cities15000 plus exactly these supplement
# rows (Fergus Falls, Delavan Lake, Como). The supplement is shared, and a
# row another manifest adds must not change a decision this record froze.
TRANCHE2_SUPPLEMENT_IDS = frozenset({"5026416", "5250402", "5249259"})
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

# Operator decision: towns admitted although they are within 25 km of a known
# city. The reuse radius is a duplicate guard, and here the geometry proves
# there is no duplicate: (known city, recorded km, its frozen grid W x H m from
# the 2026-10-02 production snapshot, this town's vetted grid W x H m, the
# vetted geocode's offset from the GeoNames point in km — the grid is centred
# on the geocode, not on the point the distance was measured from).
# Registration uses --overlap-km 5.
OPERATOR_EXCEPTIONS = {
    "5026416": ("elizabeth--minnesota--united-states", 11.5, (1624, 812), (11551, 8879), 1.4),
    "5250402": ("clinton--wisconsin--united-states", 19.1, (2174, 2648), (6043, 5136), 2.0),
}
REGISTRATION_OVERLAP_KM = 5.0
EXCEPTION_DECISION = "selected (operator exception)"

# Operator decision: pairs of rows admitted although they are within 25 km of
# each other — two distinct places whose vetted grids do not overlap,
# registered with --overlap-km 5, which admits both. Each id maps to its
# vetted grid W x H m and the vetted geocode's offset from its GeoNames point.
PAIR_EXCEPTIONS = {
    frozenset({"5250402", "5249259"}): {
        "5250402": ((6043, 5136), 2.0),  # Delavan Lake
        "5249259": ((5622, 2924), 0.7),  # Como
    },
}

# The favoured uploaders' candidates and what became of each. The priority is
# more towns like Laurens, Iowa (GIS_ISG / UAS_ISG): all three are in by
# operator exception, each pinned by geometry below.
FAVOURED_CANDIDATE_DECISIONS = {
    "Como": EXCEPTION_DECISION,
    "Delavan Lake": EXCEPTION_DECISION,
    "Fergus Falls": EXCEPTION_DECISION,
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
    "5026416": "fergus-falls--minnesota--united-states",
    "5325187": "atwater--california--united-states",
    "5088262": "keene--new-hampshire--united-states",
    "5019588": "buffalo--minnesota--united-states",
    "5250402": "delavan-lake--wisconsin--united-states",
    "5249259": "como--wisconsin--united-states",
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
    """
    The vendored GeoNames join: cities15000 plus the supplement's verbatim
    cities500 rows (``cities``), cities15000 alone, admin-1 names, countries.
    """
    cities15000 = {c.geonameid: c for c in load_cities(DATA_SOURCES / "cities15000.txt")}
    supplement = {c.geonameid: c for c in load_cities(DATA_SOURCES / "geonames_supplement.txt")}
    return SimpleNamespace(
        cities={**supplement, **cities15000},
        cities15000=cities15000,
        supplement=supplement,
        admin=load_admin1(DATA_SOURCES / "admin1CodesASCII.txt"),
        countries=load_countries(DATA_SOURCES / "countryInfo.txt"),
    )


@pytest.fixture(scope="module")
def vetted():
    """{geonameid: vet row} over the three committed vetting runs."""
    rows = {}
    for path in VET_CSVS:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                assert r["geonameid"] not in rows, r["geonameid"]  # vetted once
                rows[r["geonameid"]] = r
    return rows


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
        elif float(r["nearest_known_km"]) <= REUSE_RADIUS_KM and gid not in OPERATOR_EXCEPTIONS:
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
    admitted_by_exception = set(OPERATOR_EXCEPTIONS)
    for r in pool:
        clash = [
            k
            for k in kept
            if _haversine_km(float(r["lat"]), float(r["lon"]), float(k["lat"]), float(k["lon"]))
            <= REUSE_RADIUS_KM
        ]
        paired = [
            k for k in clash if frozenset({r["geonameid"], k["geonameid"]}) in PAIR_EXCEPTIONS
        ]
        clash = [k for k in clash if k not in paired]
        if clash:
            decisions[r["geonameid"]] = f"within 25 km of selected {clash[0]['name']}"
        else:
            kept.append(r)
            for k in paired:
                admitted_by_exception |= {r["geonameid"], k["geonameid"]}
    favoured = [r for r in kept if r["top_creator_id"] in FAVOURED_CREATORS]
    others = [r for r in kept if r["top_creator_id"] not in FAVOURED_CREATORS]
    chosen = favoured + others[: max(0, CAP - len(favoured))]
    for r in kept:
        if r not in chosen:
            decisions[r["geonameid"]] = "over the cap"
        elif r["geonameid"] in admitted_by_exception:
            decisions[r["geonameid"]] = EXCEPTION_DECISION
        else:
            decisions[r["geonameid"]] = "selected"
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
        expected = "yes" if r["geonameid"] in geonames.cities15000 else "no"
        assert r["in_cities15000"] == expected, r["name"]


def test_selection_rederives_from_the_record(manifest_rows, record, geonames):
    """
    The rule, run again here: the manifest is exactly its output, in order, and
    every candidate's committed decision is the one the rule gives it.
    """
    assert TRANCHE2_SUPPLEMENT_IDS <= set(geonames.supplement)
    assert gen.TRANCHE2_SUPPLEMENT_IDS == TRANCHE2_SUPPLEMENT_IDS
    decisions, chosen = _rederive(record, set(geonames.cities15000) | TRANCHE2_SUPPLEMENT_IDS)
    assert [row["geonameid"] for row in manifest_rows] == chosen
    for r in record:
        assert r["decision"] == decisions[r["geonameid"]], r["name"]


def test_each_row_is_scored_at_its_own_geonames_point(manifest_rows, record):
    """
    The anti-Kortrijk check (#428's review): the score that admitted a row was
    measured within 2 km of that row's own GeoNames point (the manifest's
    lat/lon), and clears the floor on its own. Registration centres the grid
    on the geocode instead; the next test covers that gap.
    """
    by_id = {r["geonameid"]: r for r in record}
    for row in manifest_rows:
        scored = by_id[row["geonameid"]]
        assert float(scored["lat"]) == pytest.approx(float(row["lat"])), row["city"]
        assert float(scored["lon"]) == pytest.approx(float(row["lon"])), row["city"]
        assert float(scored["score_km_per_km2"]) >= SCORE_FLOOR, row["city"]


def _grid_frame_km(lat, lon, grid):
    """
    The vetted grid rectangle in a local equirectangular frame centred on
    (lat, lon), in km: (centre x, centre y, half-width, half-height).
    """
    clat, clon = float(grid["center_lat"]), float(grid["center_lon"])
    kx, ky = 111.32 * math.cos(math.radians(clat)), 110.57
    return (
        (clon - lon) * kx,
        (clat - lat) * ky,
        float(grid["width_m"]) / 2000,
        float(grid["height_m"]) / 2000,
    )


def _disc_fraction_inside(lat, lon, grid, n=200):
    """
    Share of the AREA of the SCORE_DISC_KM disc around (lat, lon) that lies in
    the axis-aligned vetted grid rectangle, on an n x n lattice.
    """
    cx, cy, hw, hh = _grid_frame_km(lat, lon, grid)
    step = 2 * SCORE_DISC_KM / n
    total = inside = 0
    for i in range(n):
        x = -SCORE_DISC_KM + (i + 0.5) * step
        for j in range(n):
            y = -SCORE_DISC_KM + (j + 0.5) * step
            if x * x + y * y <= SCORE_DISC_KM**2:
                total += 1
                inside += abs(x - cx) <= hw and abs(y - cy) <= hh
    return inside / total


def test_each_rows_scored_disc_lies_inside_its_vetted_grid(manifest_rows, vetted):
    """
    The score was measured around the GeoNames point, but the frozen grid is
    centred on the geocode (0.1-5.5 km away in the vetting run). So: the
    scored point must lie inside the vetted grid, and at least
    MIN_DISC_INSIDE_GRID of the scored disc's AREA with it.

    Area, not imagery: the imagery-weighted share (92-100%, measured from the
    screen's raw segments) needs data the repo does not hold. By area the
    measured minimum is 0.771 (Como), with Delavan Lake 0.790 and
    Phoenixville 0.814 — narrow grids or offset centres — Atwater 0.997, and
    1.0 for the other ten.
    """
    for row in manifest_rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        grid = vetted[row["geonameid"]]
        cx, cy, hw, hh = _grid_frame_km(lat, lon, grid)
        assert abs(cx) <= hw and abs(cy) <= hh, row["city"]  # the scored point itself
        assert _disc_fraction_inside(lat, lon, grid) >= MIN_DISC_INSIDE_GRID, row["city"]


def test_rows_are_in_descending_score(manifest_rows, record):
    """So register_frame.py --limit N registers the strongest N first."""
    by_id = {r["geonameid"]: r for r in record}
    scores = [float(by_id[row["geonameid"]]["score_km_per_km2"]) for row in manifest_rows]
    assert scores == sorted(scores, reverse=True)


def test_vetting_failures_are_named_and_agree_with_the_generator(record):
    assert set(gen.VETTING_FAILED) == set(VETTING_FAILED)
    failed = {r["geonameid"] for r in record if r["decision"] == "failed geometry vetting"}
    assert failed == set(VETTING_FAILED)


def test_vetting_literals_trace_to_the_committed_vet_csvs(manifest_rows, vetted):
    """
    Every grid, offset and failure this file pins is a value of the committed
    vet CSVs: each manifest row resolved there unflagged within the 10 km
    guard, the failures are exactly VETTING_FAILED, and the exceptions'
    vetted W x H and offsets are those runs' numbers.
    """
    for row in manifest_rows:
        v = vetted[row["geonameid"]]
        assert v["flags"] == "", row["city"]
        assert float(v["offset_km"]) <= 10.0, row["city"]
        assert v["geocode_query"] == row["query_string"], row["city"]
    failed = {gid for gid, v in vetted.items() if v["flags"].startswith("FAILED")}
    assert failed == set(VETTING_FAILED)
    assert set(vetted) == {row["geonameid"] for row in manifest_rows} | failed

    def as_vetted(gid):
        v = vetted[gid]
        return (int(v["width_m"]), int(v["height_m"])), float(v["offset_km"])

    for gid, (*_, grid, offset_km) in OPERATOR_EXCEPTIONS.items():
        assert (grid, offset_km) == as_vetted(gid), gid
    for members in PAIR_EXCEPTIONS.values():
        for gid, (grid, offset_km) in members.items():
            assert (grid, offset_km) == as_vetted(gid), gid


def test_the_docs_vetting_table_is_the_committed_vet_csvs(manifest_rows, vetted):
    """
    docs/worldwide_sampling.md's vetting table, its totals and its offset
    summary are the committed vet CSVs, formatted; a hand-edited cell fails.
    """
    doc = WORLDWIDE_SAMPLING_DOC.read_text(encoding="utf-8")
    rows = [vetted[row["geonameid"]] for row in manifest_rows]
    price_cols = [
        "gsv_points",
        "gsv_streets_samples",
        "mapillary_z14_tiles",
        "kartaview_requests",
        "panoramax_z15_tiles",
    ]
    for v in rows:
        cells = [
            v["geocode_query"],
            v["osm_match"],
            f"{int(v['width_m']):,} x {int(v['height_m']):,}",
            *(f"{int(v[c]):,}" for c in price_cols),
            v["offset_km"],
            v["center"],
        ]
        assert "| " + " | ".join(cells) + " |" in doc, v["city"]
    totals = " | ".join(f"{sum(int(v[c]) for v in rows):,}" for c in price_cols)
    assert f"| **Total ({len(rows)} resolved)** | | | | {totals} |" in doc
    offsets = sorted(float(v["offset_km"]) for v in rows)
    mid = len(offsets) // 2
    p50 = (offsets[mid - 1] + offsets[mid]) / 2 if len(offsets) % 2 == 0 else offsets[mid]
    assert f"p50 {p50:.2f} km and max {offsets[-1]:.1f} km" in doc


def test_operator_exceptions_cannot_duplicate_their_neighbour(manifest_rows, record):
    """
    The reuse radius guards against one place registered twice. For each
    exception the record's nearest known city is the named one at the named
    distance, the two grids' half-diagonals plus the geocode's offset sum to
    less than that distance (so the rectangles cannot overlap whatever their
    orientation, even with the grid centred off the GeoNames point), and the
    distance clears the --overlap-km registration actually uses.
    """
    assert set(gen.OPERATOR_EXCEPTIONS) == set(OPERATOR_EXCEPTIONS)
    assert gen.EXCEPTION_DECISION == EXCEPTION_DECISION
    by_id = {r["geonameid"]: r for r in record}
    in_manifest = {row["geonameid"] for row in manifest_rows}
    for gid, (neighbour, km, (nw, nh), (w, h), offset_km) in OPERATOR_EXCEPTIONS.items():
        r = by_id[gid]
        assert gid in in_manifest
        assert r["decision"] == EXCEPTION_DECISION
        assert r["nearest_known"] == neighbour
        assert float(r["nearest_known_km"]) == km
        assert km <= REUSE_RADIUS_KM  # else the exception is stale
        half_diagonals_km = (math.hypot(nw, nh) + math.hypot(w, h)) / 2 / 1000
        assert half_diagonals_km + offset_km < km, gid
        assert km > REGISTRATION_OVERLAP_KM, gid


def test_favoured_creators_candidates_and_what_became_of_each(record):
    favoured = {
        r["name"]: r["decision"] for r in record if r["top_creator_id"] in FAVOURED_CREATORS
    }
    assert favoured == FAVOURED_CANDIDATE_DECISIONS
    assert set(gen.FAVOURED_CREATORS) == FAVOURED_CREATORS


def test_no_two_rows_are_within_the_reuse_radius(manifest_rows):
    """Except a declared PAIR_EXCEPTIONS pair, which has its own geometry test."""
    points = [(r["geonameid"], float(r["lat"]), float(r["lon"])) for r in manifest_rows]
    for i, (a, alat, alon) in enumerate(points):
        for b, blat, blon in points[i + 1 :]:
            if frozenset({a, b}) in PAIR_EXCEPTIONS:
                continue
            assert _haversine_km(alat, alon, blat, blon) > REUSE_RADIUS_KM, (a, b)


def test_pair_exceptions_are_distinct_places_whose_grids_cannot_overlap(manifest_rows, record):
    """
    A pair of rows inside each other's reuse radius is admitted only when both
    are rows, both decisions say exception, their GeoNames points are farther
    apart than the two vetted half-diagonals plus both geocode offsets (so the
    rectangles cannot overlap whatever their orientation), and the distance
    clears the --overlap-km registration actually uses.
    """
    assert set(gen.PAIR_EXCEPTIONS) == set(PAIR_EXCEPTIONS)
    rows = {row["geonameid"]: row for row in manifest_rows}
    by_id = {r["geonameid"]: r for r in record}
    for pair, members in PAIR_EXCEPTIONS.items():
        assert set(members) == set(pair)
        a, b = sorted(pair)
        assert a in rows and b in rows, pair
        assert by_id[a]["decision"] == by_id[b]["decision"] == EXCEPTION_DECISION
        km = _haversine_km(
            float(rows[a]["lat"]),
            float(rows[a]["lon"]),
            float(rows[b]["lat"]),
            float(rows[b]["lon"]),
        )
        assert km <= REUSE_RADIUS_KM  # else the exception is stale
        reach_km = sum(math.hypot(w, h) / 2 / 1000 + off for (w, h), off in members.values())
        assert reach_km < km, (pair, reach_km, km)
        assert km > REGISTRATION_OVERLAP_KM, pair


def test_no_row_is_within_the_reuse_radius_of_a_known_registration(manifest_rows, record):
    """
    The record's nearest_known_km saw the production snapshot (an
    OPERATOR_EXCEPTIONS row is exempt from its 25 km floor, and is checked in
    its own test); this re-checks
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
        assert recorded <= round(nearest_t1, 1) + 0.05, row["city"]
        if row["geonameid"] not in OPERATOR_EXCEPTIONS:
            assert recorded > REUSE_RADIUS_KM, row["city"]


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


def test_generator_exception_waives_only_the_known_city_radius():
    known = {"tiny": (46.0, -96.0)}
    exc = _cand("5026416", 46.1, -96.0, 5.4)  # ~11 km from a known city
    plain = _cand("7", 46.0, -96.1, 6.0)  # ~8 km from it, no exception
    chosen = gen.select([exc, plain], known, set(), {"5026416", "7"})
    assert [c["geonameid"] for c in chosen] == ["5026416"]
    assert exc["decision"] == gen.EXCEPTION_DECISION
    assert plain["decision"] == "within 25 km of a known city"
    # an exception does not waive the GeoNames join
    unjoinable = _cand("5026416", 46.1, -96.0, 5.4)
    assert gen.select([unjoinable], known, set()) == []
    assert unjoinable["decision"] == "cities500 only"


def test_generator_pair_exception_admits_only_the_named_pair():
    far = {"elsewhere": (0.0, 0.0)}
    a = _cand("5250402", 42.58, -88.63, 4.08)  # Delavan Lake's id
    b = _cand("5249259", 42.61, -88.48, 3.94)  # Como's id, ~12.7 km away
    c = _cand("8", 42.60, -88.55, 3.5)  # an unnamed third within 25 km of both
    chosen = gen.select([a, b, c], far, {"5250402", "5249259", "8"})
    assert [x["geonameid"] for x in chosen] == ["5250402", "5249259"]
    assert a["decision"] == b["decision"] == gen.EXCEPTION_DECISION
    assert c["decision"] == "within 25 km of selected P5250402"


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


# --- the generator's I/O half: main() on a synthetic data_sources/ -----------

_CANDIDATE_COLUMNS = [
    "geonameid",
    "name",
    "admin1",
    "cc",
    "lat",
    "lon",
    "pop",
    "km_per_km2",
    "km_in_disc",
    "top_creator",
    "top_creator_username",
    "top_share",
    "median_captured",
]


def _geonames_line(gid, name, lat, lon, population):
    """A 19-column GeoNames row in California, US (feature class P)."""
    cols = [""] * 19
    cols[0], cols[1], cols[2] = gid, name, name
    cols[4], cols[5], cols[6] = str(lat), str(lon), "P"
    cols[8], cols[10], cols[14] = "US", "CA", str(population)
    return "\t".join(cols) + "\n"


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def generated(tmp_path, monkeypatch):
    """
    Run gen.main over synthetic inputs and return its record and manifest.

    * 31 far-apart cities15000 towns "100".."130" (scores 9.0 down to 6.0):
      with "900" that is 32 eligible rows, and the cap of 30 drops the two
      weakest;
    * "200" sits 24.996 km from the snapshot's only city, a distance the
      generator rounds to exactly 25.0 — inside the radius, since it is `<=`;
    * "300" is 5.6 km from a town in an --also-registered manifest only;
    * "900" is a pinned supplement row (joinable, score 8.3); "901" is a
      supplement row the tranche does not pin ("cities500 only", never joined).
    """
    ds = tmp_path / "data_sources"
    ds.mkdir()
    for name in ("admin1CodesASCII.txt", "countryInfo.txt"):
        (ds / name).symlink_to(DATA_SOURCES / name)
    places = [(str(100 + i), 20.0 + 0.5 * i, -100.0, 9.0 - 0.1 * i) for i in range(31)]
    places += [("200", 50.0 + 0.2248, -100.0, 8.5), ("300", 60.05, -100.0, 8.4)]
    (ds / "cities15000.txt").write_text(
        "".join(
            _geonames_line(gid, f"Town{gid}", lat, lon, 20000 + int(gid))
            for gid, lat, lon, _ in places
        ),
        encoding="utf-8",
    )
    supplement = [("900", 70.0, -100.0, 8.3), ("901", 72.0, -100.0, 8.2)]
    (ds / "geonames_supplement.txt").write_text(
        "".join(
            _geonames_line(gid, f"Town{gid}", lat, lon, 1000 + int(gid))
            for gid, lat, lon, _ in supplement
        ),
        encoding="utf-8",
    )
    candidates = [
        dict(
            zip(
                _CANDIDATE_COLUMNS,
                [gid, f"Town{gid}", "CA", "US", lat, lon, 1, score, 1.0, "1", "u", 1.0, "2026"],
                strict=True,
            )
        )
        for gid, lat, lon, score in places + supplement
    ]
    _write_csv(tmp_path / "candidates.csv", _CANDIDATE_COLUMNS, candidates)
    _write_csv(
        tmp_path / "snapshot.csv",
        ["table", "city_id", "lat", "lon"],
        [{"table": "cities", "city_id": "known--city", "lat": 50.0, "lon": -100.0}],
    )
    _write_csv(
        tmp_path / "t1.csv",
        ["city", "lat", "lon"],
        [{"city": "Neighbour", "lat": 60.0, "lon": -100.0}],
    )
    monkeypatch.setattr(gen, "DATA_SOURCES", str(ds))
    monkeypatch.setattr(gen, "TRANCHE2_SUPPLEMENT_IDS", frozenset({"900"}))
    # fmt: off
    argv = [
        "--candidates", str(tmp_path / "candidates.csv"),
        "--catalog-snapshot", str(tmp_path / "snapshot.csv"),
        "--also-registered", str(tmp_path / "t1.csv"),
        "--record", str(tmp_path / "record.csv"),
        "--manifest-out", str(tmp_path / "manifest.csv"),
    ]
    # fmt: on
    assert gen.main(argv) == 0
    with open(tmp_path / "record.csv", encoding="utf-8") as f:
        record = {r["geonameid"]: r for r in csv.DictReader(f)}
    with open(tmp_path / "manifest.csv", encoding="utf-8") as f:
        manifest = list(csv.DictReader(f))
    return SimpleNamespace(record=record, manifest=manifest)


def test_generator_constants_are_the_tests_constants():
    assert (gen.SCORE_FLOOR, gen.REUSE_RADIUS_KM, gen.CAP) == (SCORE_FLOOR, REUSE_RADIUS_KM, CAP)


def test_generator_main_caps_at_thirty_rows(generated):
    """32 eligible towns (31 plus the pinned supplement row): 30 rows, the 2 weakest over."""
    assert len(generated.manifest) == CAP
    over = sorted(gid for gid, r in generated.record.items() if r["decision"] == "over the cap")
    assert over == ["129", "130"]


def test_generator_main_reads_the_known_radius_inclusively(generated):
    r = generated.record["200"]
    assert (r["nearest_known"], r["nearest_known_km"]) == ("known--city", "25.0")
    assert r["decision"] == "within 25 km of a known city"


def test_generator_main_reads_also_registered_manifests(generated):
    r = generated.record["300"]
    assert r["nearest_known"] == "t1.csv:Neighbour"
    assert r["decision"] == "within 25 km of a known city"


def test_generator_main_joins_only_the_pinned_supplement_rows(generated):
    """
    900 (pinned) is joined from the supplement file, not from cities15000;
    901 is in the same file but not pinned, so it is never joinable.
    """
    rows = {r["geonameid"]: r for r in generated.manifest}
    assert generated.record["900"]["decision"] == "selected"
    assert generated.record["900"]["in_cities15000"] == "no"
    assert (rows["900"]["city"], int(rows["900"]["population"])) == ("Town900", 1900)
    assert generated.record["901"]["decision"] == "cities500 only"
    assert "901" not in rows


def test_generator_main_writes_each_row_from_its_geonames_record(generated):
    """The manifest row's values come from the vendored record, not the candidate."""
    rows = {r["geonameid"]: r for r in generated.manifest}
    row = rows["100"]
    assert row["city"] == "Town100"
    assert row["query_string"] == "Town100, California, United States"
    assert (row["admin"], row["iso2"], row["country"], row["continent"]) == (
        "California",
        "US",
        "United States",
        "NA",
    )
    assert int(row["population"]) == 20100
    assert (float(row["lat"]), float(row["lon"])) == (20.0, -100.0)
    assert row["size_band"] == row["coverage_regime"] == ""
    scores = [float(generated.record[g]["score_km_per_km2"]) for g in rows]
    assert scores == sorted(scores, reverse=True)
