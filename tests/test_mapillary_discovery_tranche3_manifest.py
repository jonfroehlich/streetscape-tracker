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
OVERLAPS = EXP / "mapillary-discovery-screen_tranche3_overlaps.json"
OPS = REPO_ROOT / "docs" / "operations.md"
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
    The centres are hand-copied from the gitignored 2026-11-05 snapshot.
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
    assert fits.pop("_about")["generated_by"] == "scripts/mapillary_discovery_rescore/fit_grids.py"
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


def _overlap_rows():
    return {r["geonameid"]: r for r in json.loads(OVERLAPS.read_text(encoding="utf-8"))["rows"]}


def _registration_cost(vetted):
    """geonameid -> (lattice GSV points, Mapillary tiles, KartaView, Panoramax) as registered."""
    fits = json.loads(GRIDS.read_text(encoding="utf-8"))
    rows = _overlap_rows()
    out = {}
    for gid, v in vetted.items():
        tiles = int(v["mapillary_z14_tiles"])
        if gid in GRID_KEYS:
            tiles = fits[GRID_KEYS[gid]]["tiles_new"]
            lat, lon, w, h = gen.GRID_FITS[gid]
            assert rows[gid]["grid"] == [lat, lon, w, h], gid
            assert rows[gid]["gsv_lattice_points"] == (int(w / 20) + 1) * (int(h / 20) + 1), gid
        else:  # an unfitted grid's lattice count is the vetting run's own
            assert rows[gid]["gsv_lattice_points"] == int(v["gsv_points"]), gid
        out[gid] = (
            rows[gid]["gsv_lattice_points"],
            tiles,
            int(v["kartaview_requests"]),
            int(v["panoramax_z15_tiles"]),
        )
    return out


def _tranche_totals(vetted):
    """(vetted GSV, vetted tiles, fitted GSV, fitted tiles) over the 25 rows."""
    gsv = sum(int(v["gsv_points"]) for v in vetted.values())
    tiles = sum(int(v["mapillary_z14_tiles"]) for v in vetted.values())
    cost = _registration_cost(vetted).values()
    return gsv, tiles, sum(c[0] for c in cost), sum(c[1] for c in cost)


# The runbook's two nights, by manifest city name.
NIGHTS = {
    1: "White City, Ahuimanu, Pell Lake, Live Oak, Magna, Makakilo City, Muscoy, Belmont, "
    "Longwood, Delavan, Sterling, Eureka, Elko",
    2: "Carmel-by-the-Sea, East Palo Alto, Rio Linda, Stanford, Royal Kunia, Evansdale, "
    "Makaha, Kuna, North Tustin, Fair Oaks, Worland, Colton",
}


def test_the_runbooks_night_table_is_the_committed_records(manifest_rows, vetted):
    by_name = {r["city"]: r["geonameid"] for r in manifest_rows}
    cost = _registration_cost(vetted)
    named = [n for names in NIGHTS.values() for n in names.split(", ")]
    assert sorted(named) == sorted(by_name)  # every town on exactly one night
    ops = OPS.read_text(encoding="utf-8")
    for night, names in NIGHTS.items():
        c = [cost[by_name[n]] for n in names.split(", ")]
        gsv, tiles, kv, px = (sum(x[i] for x in c) for i in range(4))
        line = (
            f"| {night} | {names} | {gsv:,} ({gsv / 48000 / 60:.2f}) | {tiles} | "
            f"≤ {kv:,} ({kv / 16 / 60:.2f}) | ≤ {px:,} ({round(px / 60)}) |"
        )
        assert line in ops, line


def test_registration_reuse_margins_leave_headroom_at_the_runbooks_radius(manifest_rows):
    """
    register_frame.py appends each newly registered row's GEOCODED centre to the
    cities later rows are checked against, so the in-batch margin is GeoNames
    point to vetted centre; overlaps.json records it per row. The runbook's
    --overlap-km must sit at least 1.5 km under every margin.
    """
    rows = _overlap_rows()
    assert list(rows) == [r["geonameid"] for r in manifest_rows]
    ops = OPS.read_text(encoding="utf-8")
    assert '--notes-label "mapillary discovery screen 2026-11-05 tranche 3" --overlap-km 4 ' in ops
    margins = [r["nearest_catalog_km"] for r in rows.values()]
    margins += [r["nearest_earlier_row_km"] for r in rows.values() if "nearest_earlier_row_km" in r]
    assert min(margins) >= 4 + 1.5
    stanford = rows["5398563"]
    assert stanford["nearest_earlier_row"] == "East Palo Alto"
    assert stanford["nearest_earlier_row_km"] == pytest.approx(5.558, abs=0.001)


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
    # The floor, recomputed: the smallest k with P(X < k) <= 5% at the rate is
    # the largest k whose P(X <= k - 1) is still under 5%.
    n, p = pred["n"], t7[0]["good2"] / t7[0]["n"]

    def cdf(k):
        return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))

    floor = max(k for k in range(n + 1) if cdf(k - 1) <= 0.05)
    assert pred["falsified_below"] == floor
    assert pred["p_below_floor_at_rate"] == round(cdf(floor - 1), 3)
    assert pred["expected_strict_hits"] == round(n * p, 1)
    total = [
        r
        for r in metrics["v4_threshold"]
        if r["v4_at_least"] == 0.0 and r["population"] == "tranche"
    ][0]
    text = WRITEUP.read_text(encoding="utf-8")
    assert f"fewer than **{floor}**" in text
    assert f"probability {pred['p_below_floor_at_rate']:.3f}" in text
    assert f"**{pred['expected_strict_hits']} strict hits of 25**" in text
    assert f"observed rate ({p:.3f})" in text
    assert (
        f"{t7[0]['good2']} of {t7[0]['n']} strict hits and {t7[0]['any50']} of {t7[0]['n']}" in text
    )
    below = (total["good2"] - t7[0]["good2"], total["n"] - t7[0]["n"])
    assert f"against {below[0]} of {below[1]} strict hits below it" in text


def test_the_writeups_rescore_numbers_are_the_metrics_jsons():
    metrics = json.loads(METRICS.read_text(encoding="utf-8"))
    text = WRITEUP.read_text(encoding="utf-8")
    scores = metrics["evaluation"]["by_target"]["good2"]["scores"]
    v0 = scores["V0 committed: recent length, 2 km"]
    v4 = scores["V4 age-weighted cells, best of 1/2/3 km"]
    v5 = scores["V5 V4 x LOO uploader record"]
    for s in (v0, v4, v5):
        cell = f"| {s['spearman_rho_vs_walk_coverage']:.3f} | {s['average_precision']:.3f} |"
        assert cell in text, cell
    for name, s in (("V4", v4), ("V5", v5)):
        diff = s["average_precision"] - v0["average_precision"]
        assert f"AP({name}) − AP(V0) = +{diff:.3f}" in text, name
        lo, hi = metrics["ap_diff_vs_V0_uploader_clustered"][name]["ci95"]
        assert f"[{lo:+.3f}, {hi:+.3f}]".replace("-", "−") in text, name
    f = metrics["funnel"]
    assert (
        f"{f['places_v4_at_least_0_7']} places had V4 ≥ 0.7, {f['outside_grids_and_over_5km']} "
        f"of them outside the catalog, {f['pass_all_rules']} passed every rule and "
        f"{f['after_5km_thinning']} survived"
    ) in text
    card = {u["user"]: u for u in metrics["uploader_scorecard"]}
    for user in ("quickness805", "ctroadway360", "nthp_gis", "rking", "marker_geo1", "hmhtb"):
        assert f"`{user}` {card[user]['good2']}" in text, user
        assert f"of {card[user]['walked']}" in text, user


def test_the_writeups_overlaps_are_the_overlap_records():
    text = WRITEUP.read_text(encoding="utf-8")
    rows = {r["city"]: r["catalog_overlaps"] for r in _overlap_rows().values()}

    def pct(city, neighbour):
        return round(rows[city][neighbour] * 100)

    m = rows["Magna"]
    assert (
        f"Magna's fitted grid lies {pct('Magna', 'west-valley-city--utah--united-states')}% inside "
        f"West Valley City's and {pct('Magna', 'salt-lake-city--utah--united-states')}% inside "
        f"Salt Lake City's"
    ) in text
    assert len(m) == 2
    assert (
        f"Rio Linda's {pct('Rio Linda', 'sacramento--california--united-states')}% inside Sacramento's"
        in text
    )
    assert (
        f"White City's {pct('White City', 'cottonwood-heights--utah--united-states')}% inside Cottonwood Heights'"
        in text
    )
    small = {c: o for c, o in rows.items() if o and max(o.values()) < 0.1}
    assert {c: list(o) for c, o in small.items()} == {
        "Fair Oaks": ["orangevale--california--united-states"],
        "Royal Kunia": ["honolulu--hawaii--united-states"],
        "Delavan": ["delavan-lake--wisconsin--united-states"],
        "Belmont": ["foster-city--california--united-states"],
    }
    lo = min(round(max(o.values()) * 100) for o in small.values())
    hi = max(round(max(o.values()) * 100) for o in small.values())
    assert (
        f"{len(small)} more overlap a neighbour by {lo}–{hi}%".replace("4 more", "four more")
        in text
    )
    assert (
        "(Fair Oaks with Orangevale, Royal Kunia with Honolulu, Delavan with tranche 2's "
        "Delavan Lake, Belmont with Foster City)"
    ) in text
