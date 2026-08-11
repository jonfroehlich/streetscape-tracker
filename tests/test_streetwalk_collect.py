"""End-to-end tests for the road-walk collector CLI (issue #99).

Drive the real `collect.run_collect` flow with the OSM fetch and the GSV request
primitive both served from memory (the same technique as the grid batch tests).
Verify the two artifacts, the catalog row, the isolated `gsv_streets` budget
ledger, `--estimate`, rejection, and that the shared quota-retry engine reaches
the streets path.
"""

import asyncio
import gzip
import json
import os

import geopandas as gpd
import pytest
from shapely.geometry import LineString

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import download_gsv as dg
from streetscape_street_analyzer import collect

LONG_EDGE = LineString([(-121.30, 44.05), (-121.30, 44.052)])
SHORT_EDGE = LineString([(-121.30, 44.052), (-121.30, 44.0525)])
CITY_QUERY = "Bend, Oregon, United States"
CITY_ID = "bend--oregon--united-states"
RUN_DATE = "2026-07-08"


def _edges():
    return gpd.GeoDataFrame(
        {"edge_id": ["1_2", "2_3"], "highway": ["residential", "service"], "length": [222.0, 55.0]},
        geometry=[LONG_EDGE, SHORT_EDGE],
        crs="EPSG:4326",
    )


def _ok_google(lat, lon):
    return {
        "status": "OK",
        "location": {"lat": lat, "lng": lon},
        "pano_id": f"pano_{lat:.6f}_{lon:.6f}",
        "copyright": "© Google",
        "date": "2022-06",
    }


def _setup(tmp_path, monkeypatch):
    """Fresh data dir + catalog with one registered city; edges served locally."""
    data_dir = str(tmp_path)
    conn = db.connect(db.get_default_db_path(data_dir))
    db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.30,
        grid_width_m=200,
        grid_height_m=200,
        step_m=20,
    )
    conn.close()
    monkeypatch.setattr(collect, "fetch_street_edges", lambda *a, **k: _edges())
    return data_dir


def _args(data_dir, **overrides):
    argv = [
        CITY_QUERY,
        "--data-dir",
        data_dir,
        "--run-date",
        RUN_DATE,
        "--spacing",
        "15",
        "--max-requests-per-minute",
        "0",
    ]
    for k, v in overrides.items():
        argv += [f"--{k}", str(v)] if v is not True else [f"--{k}"]
    return collect.build_parser().parse_args(argv)


def _patch_instant_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant)


def test_collect_writes_artifacts_catalog_and_isolated_budget(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        assert api_key == "TESTKEY"  # the gsv_streets key, not the main key
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)

    rc = collect.run_collect(_args(data_dir))
    assert rc == 0

    csv_name = f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz"
    cov_name = csv_name[: -len(".csv.gz")] + "_coverage.json.gz"
    assert os.path.exists(os.path.join(data_dir, csv_name))
    cov_path = os.path.join(data_dir, cov_name)
    assert os.path.exists(cov_path)

    with gzip.open(cov_path, "rt") as fh:
        gj = json.load(fh)
    # All samples covered → every edge fully covered.
    totals = gj["properties"]["metadata"]["totals"]
    assert totals["mean_edge_coverage"] == 1.0
    assert totals["edges_fully_covered"] == totals["edges"] == 2

    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID)
    assert walk is not None
    assert walk["csv_filename"] == csv_name
    assert walk["coverage_filename"] == cov_name
    assert walk["spacing_m"] == 15.0
    assert walk["edges_total"] == 2
    # Budget isolation: usage lands under gsv_streets, never gsv.
    from datetime import date

    d = date.fromisoformat(RUN_DATE)
    queries = walk["api_requests"]
    assert queries == 19  # 15 + 4 unique samples, all distinct locations
    assert db.get_api_usage(conn, d, provider="gsv_streets") == queries
    assert db.get_api_usage(conn, d, provider="gsv") == 0
    conn.close()


def test_collect_publishes_the_streetwalk_manifest(tmp_path, monkeypatch):
    """
    The collector is a manual CLI outside the scheduler, and the city page can
    only discover a coverage artifact through `streetwalks.json.gz` (issue
    #155). So collecting must refresh the manifest itself — otherwise a fresh
    walk publishes but stays invisible until the next nightly run-due or an
    explicit regenerate-aggregate.
    """
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)

    assert collect.run_collect(_args(data_dir)) == 0

    manifest_path = os.path.join(data_dir, "streetwalks.json.gz")
    assert os.path.exists(manifest_path), "collect must write the sidecar manifest"
    with gzip.open(manifest_path, "rt") as fh:
        manifest = json.load(fh)

    assert len(manifest["walks"]) == 1
    walk = manifest["walks"][0]
    assert walk["city_id"] == CITY_ID
    assert walk["provider"] == "gsv"
    assert walk["run_date"] == RUN_DATE
    # The advertised filename must be the artifact actually on disk — a mismatch
    # is a 404 in the browser, the one failure mode the manifest exists to avoid.
    assert os.path.exists(os.path.join(data_dir, walk["coverage_filename"]))
    assert walk["coverage_pct_by_length"] == 100.0
    assert walk["uncovered_pct_by_length"] == 0.0


def test_rejected_collect_leaves_no_manifest_entry(tmp_path, monkeypatch):
    """
    A systemically-failed walk is not cataloged, so it must not be advertised
    either — the frontend would fetch a `.rejected` artifact that isn't there.
    """
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    async def denied(lat, lon, api_key, session, timeout, limiter=None):
        return {"status": "REQUEST_DENIED"}

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", denied)
    _patch_instant_sleep(monkeypatch)

    assert collect.run_collect(_args(data_dir)) == 1

    manifest_path = os.path.join(data_dir, "streetwalks.json.gz")
    if os.path.exists(manifest_path):  # not written on the reject path today
        with gzip.open(manifest_path, "rt") as fh:
            assert json.load(fh)["walks"] == []


def test_estimate_needs_no_key_and_writes_nothing(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("GMAPS_STREETS_API_KEY", raising=False)

    def boom(*a, **k):  # must not be called under --estimate
        raise AssertionError("no API request may be issued during --estimate")

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", boom)

    rc = collect.run_collect(_args(data_dir, estimate=True))
    assert rc == 0
    assert not any(f.endswith(".csv.gz") for f in os.listdir(data_dir))


def test_systemic_failure_is_rejected_not_cataloged(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    async def denied(lat, lon, api_key, session, timeout, limiter=None):
        return {"status": "REQUEST_DENIED"}

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", denied)

    rc = collect.run_collect(_args(data_dir))
    assert rc == 1
    csv_name = f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz"
    assert os.path.exists(os.path.join(data_dir, csv_name + ".rejected"))
    assert not os.path.exists(os.path.join(data_dir, csv_name))
    conn = db.connect(db.get_default_db_path(data_dir))
    assert db.get_latest_street_walk(conn, CITY_ID) is None
    conn.close()


def test_shared_quota_retry_engine_reaches_streets_path(tmp_path, monkeypatch):
    """Throttle every point once (HTTP 200 OVER_QUERY_LIMIT), then succeed:
    the collector must retry via the shared engine, so requests ≈ 2× queries."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")
    _patch_instant_sleep(monkeypatch)
    seen = {}

    async def throttle_then_ok(lat, lon, api_key, session, timeout, limiter=None):
        key = (round(lat, 9), round(lon, 9))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 1:
            return {"status": "OVER_QUERY_LIMIT"}
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", throttle_then_ok)

    rc = collect.run_collect(_args(data_dir))
    assert rc == 0
    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID)
    assert walk["api_requests"] == 19 * 2  # 19 initial + 19 retried
    conn.close()


def test_immutable_snapshot_skips_without_force(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")
    calls = {"n": 0}

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        calls["n"] += 1
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)

    assert collect.run_collect(_args(data_dir)) == 0
    first = calls["n"]
    # Second run, same date, no --force: skip cleanly without re-querying.
    assert collect.run_collect(_args(data_dir)) == 0
    assert calls["n"] == first


# --- Broad networks: alleys, footpaths, park trails -------------------------

FOOTWAY_EDGE = LineString([(-121.3005, 44.05), (-121.3005, 44.052)])
ALLEY_EDGE = LineString([(-121.3010, 44.05), (-121.3010, 44.051)])


def _edges_for_network(network_type):
    """
    Edges per osmnx network type, as the real fetch would return them.

    'drive' is motorized public roads only. 'all_public' is a strict superset
    that additionally carries the footway and the alley — the classes the whole
    broad-network change exists to reach.
    """
    if network_type == "drive":
        return _edges()
    return gpd.GeoDataFrame(
        {
            "edge_id": ["1_2", "2_3", "3_4", "4_5"],
            "highway": ["residential", "service", "footway", "service"],
            "service": [None, None, None, "alley"],
            "length": [222.0, 55.0, 222.0, 111.0],
        },
        geometry=[LONG_EDGE, SHORT_EDGE, FOOTWAY_EDGE, ALLEY_EDGE],
        crs="EPSG:4326",
    )


def test_two_network_types_walk_one_city_on_one_date_without_colliding(tmp_path, monkeypatch):
    """
    A city's 'drive' and 'all_public' walks are different amounts of work over
    different edge sets, and both can legitimately be collected the same night.

    Without a network token in the filename they produce byte-identical names,
    so the second collection finds the first's snapshot on disk, hits the
    immutable-per-date guard, and returns 0 — a *success* that silently
    collected nothing. That is exactly how the provider token's absence broke
    the Mapillary arm, so it is asserted the same way: both artifacts, both
    catalog rows, and proof the second run actually queried.
    """
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        collect,
        "fetch_street_edges",
        lambda *a, **k: _edges_for_network(k.get("network_type", "drive")),
    )
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")
    calls = {"n": 0}

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        calls["n"] += 1
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)

    assert collect.run_collect(_args(data_dir)) == 0
    after_drive = calls["n"]
    assert collect.run_collect(_args(data_dir, **{"network-type": "all_public"})) == 0
    # The broad walk really ran: it queried again, over a larger network.
    assert calls["n"] > after_drive

    drive_csv = f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz"
    broad_csv = (
        f"{CITY_ID}_width_200_height_200_step_20_streetwalk_allpublic_sp15_{RUN_DATE}.csv.gz"
    )
    assert drive_csv != broad_csv
    for name in (drive_csv, broad_csv):
        assert os.path.exists(os.path.join(data_dir, name)), f"{name} missing"
        cov = name[: -len(".csv.gz")] + "_coverage.json.gz"
        assert os.path.exists(os.path.join(data_dir, cov)), f"{cov} missing"

    conn = db.connect(db.get_default_db_path(data_dir))
    rows = conn.execute(
        "SELECT network_type, csv_filename, edges_total FROM street_walks ORDER BY network_type"
    ).fetchall()
    assert [r["network_type"] for r in rows] == ["all_public", "drive"]
    assert rows[0]["csv_filename"] != rows[1]["csv_filename"]
    # The broad walk covers strictly more edges than the drive one.
    assert rows[0]["edges_total"] > rows[1]["edges_total"]
    # Each series is independently addressable.
    assert db.get_latest_street_walk(conn, CITY_ID)["network_type"] == "drive"
    assert (
        db.get_latest_street_walk(conn, CITY_ID, "gsv", "all_public")["csv_filename"] == broad_csv
    )
    conn.close()


def test_broad_walk_reports_footway_and_alley_buckets(tmp_path, monkeypatch):
    """
    The point of walking a broad network: the by-type breakdown must separate
    park paths and back streets from roads, or the artifact can't answer
    "how does path coverage compare to road coverage?".
    """
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        collect,
        "fetch_street_edges",
        lambda *a, **k: _edges_for_network(k.get("network_type", "drive")),
    )
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)
    assert collect.run_collect(_args(data_dir, **{"network-type": "all_public"})) == 0

    cov = (
        f"{CITY_ID}_width_200_height_200_step_20_streetwalk_allpublic_sp15_"
        f"{RUN_DATE}_coverage.json.gz"
    )
    with gzip.open(os.path.join(data_dir, cov), "rt") as fh:
        gj = json.load(fh)
    meta = gj["properties"]["metadata"]
    # The network type is recorded, so a reader can't mistake a drive
    # artifact's missing footways for a city that has none.
    assert meta["network_type"] == "all_public"
    by_type = meta["coverage_by_highway"]
    assert "footway" in by_type
    # `service=alley` is split out from generic service roads.
    assert "alley" in by_type
    assert "residential" in by_type
    buckets = {f["properties"]["highway"] for f in gj["features"]}
    assert {"residential", "service", "footway", "alley"} == buckets


# ── Walk-to-walk diffs at the CLI seam (issue #101) ─────────────────────────

SECOND_DATE = "2026-10-01"


def _collect_ok(data_dir, monkeypatch, **overrides):
    """Run one collect with every sample answered © Google OK."""

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)
    return collect.run_collect(_args(data_dir, **overrides))


def _walk_diff_rows(data_dir):
    conn = db.connect(db.get_default_db_path(data_dir))
    try:
        return conn.execute("SELECT * FROM street_walk_diffs").fetchall()
    finally:
        conn.close()


def _read_manifest(data_dir):
    with gzip.open(os.path.join(data_dir, "streetwalks.json.gz"), "rt") as fh:
        return json.load(fh)


def test_first_walk_is_diff_free_everywhere(tmp_path, monkeypatch):
    """A first walk has nothing to diff against: no street_walk_diffs row, no
    detail file, and no 'change' key in its manifest entry."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert _collect_ok(data_dir, monkeypatch) == 0

    assert _walk_diff_rows(data_dir) == []
    assert not [f for f in os.listdir(data_dir) if "streetwalkdiff" in f]
    (entry,) = _read_manifest(data_dir)["walks"]
    assert "change" not in entry


def test_second_walk_records_diff_detail_and_manifest_change(tmp_path, monkeypatch):
    """A second walk with degraded imagery yields the whole temporal payoff:
    a street_walk_diffs row, a published detail csv.gz, and a manifest entry
    carrying the change block with the right date span."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert _collect_ok(data_dir, monkeypatch) == 0

    # Second walk: samples on the short edge (lat >= ~44.052) lose coverage.
    async def degraded_fetch(lat, lon, api_key, session, timeout, limiter=None):
        if lat >= 44.0519:
            return {"status": "ZERO_RESULTS"}
        return _ok_google(lat, lon)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", degraded_fetch)
    assert collect.run_collect(_args(data_dir, **{"run-date": SECOND_DATE})) == 0

    (row,) = _walk_diff_rows(data_dir)
    assert row["edges_lost_coverage"] >= 1
    assert row["edges_gained_coverage"] == 0
    assert row["edges_added"] == 0 and row["edges_removed"] == 0
    assert row["coverage_pct_by_length_delta"] < 0
    detail_name = row["detail_filename"]
    assert detail_name == f"{CITY_ID}_streetwalkdiff_{RUN_DATE}_to_{SECOND_DATE}.csv.gz"
    assert os.path.exists(os.path.join(data_dir, detail_name))

    (entry,) = _read_manifest(data_dir)["walks"]
    assert entry["run_date"] == SECOND_DATE
    assert entry["change"]["from"] == RUN_DATE
    assert entry["change"]["to"] == SECOND_DATE
    assert entry["change"]["diff_file"] == detail_name
    assert entry["change"]["edges_lost_coverage"] == row["edges_lost_coverage"]


def test_identical_second_walk_records_row_without_detail_file(tmp_path, monkeypatch):
    """'Diffed, nothing changed' is a recorded fact — a row with NULL
    detail_filename and no file on disk (mirrors the grid diff)."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert _collect_ok(data_dir, monkeypatch) == 0
    assert _collect_ok(data_dir, monkeypatch, **{"run-date": SECOND_DATE}) == 0

    (row,) = _walk_diff_rows(data_dir)
    assert row["detail_filename"] is None
    assert row["edges_aligned"] == 2
    assert not [f for f in os.listdir(data_dir) if "streetwalkdiff" in f]
    # The manifest still advertises the (empty) change: "no change since X"
    # is a statement, not an absence.
    (entry,) = _read_manifest(data_dir)["walks"]
    assert entry["change"]["edges_gained_coverage"] == 0
    assert entry["change"]["diff_file"] is None


def test_spacing_change_skips_the_diff(tmp_path, monkeypatch):
    """A different --spacing is a different sample frame; the walk catalogs
    fine but no diff is recorded (the same_grid_geometry gate semantics)."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert _collect_ok(data_dir, monkeypatch) == 0
    assert _collect_ok(data_dir, monkeypatch, **{"run-date": SECOND_DATE, "spacing": 30}) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    walks = conn.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0]
    conn.close()
    assert walks == 2
    assert _walk_diff_rows(data_dir) == []


def test_diff_failure_never_fails_the_collect(tmp_path, monkeypatch):
    """A diff bug must never fail a fully-paid-for, already-cataloged crawl."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert _collect_ok(data_dir, monkeypatch) == 0

    def boom(*a, **k):
        raise RuntimeError("diff exploded")

    monkeypatch.setattr(collect, "compute_and_record_walk_diff", boom)
    assert _collect_ok(data_dir, monkeypatch, **{"run-date": SECOND_DATE}) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID)
    conn.close()
    assert walk["run_date"] == SECOND_DATE  # the walk itself was cataloged
    (entry,) = _read_manifest(data_dir)["walks"]  # and the manifest refreshed
    assert entry["run_date"] == SECOND_DATE


def test_collect_catalogs_coverage_by_highway(tmp_path, monkeypatch):
    """The catalog column carries the artifact's per-bucket dict verbatim."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert _collect_ok(data_dir, monkeypatch) == 0

    csv_name = f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz"
    with gzip.open(
        os.path.join(data_dir, csv_name[: -len(".csv.gz")] + "_coverage.json.gz"), "rt"
    ) as fh:
        artifact_breakdown = json.load(fh)["properties"]["metadata"]["coverage_by_highway"]

    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID)
    conn.close()
    assert json.loads(walk["coverage_by_highway"]) == artifact_breakdown
    assert "residential" in artifact_breakdown


def test_collect_catalogs_absolute_street_lengths(tmp_path, monkeypatch):
    """The v12 length columns come from the artifact's totals, and the stored
    percentage must agree with the stored lengths — they are published side by
    side, so a disagreement would be visible on the streets page."""
    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert _collect_ok(data_dir, monkeypatch) == 0

    csv_name = f"{CITY_ID}_width_200_height_200_step_20_streetwalk_sp15_{RUN_DATE}.csv.gz"
    with gzip.open(
        os.path.join(data_dir, csv_name[: -len(".csv.gz")] + "_coverage.json.gz"), "rt"
    ) as fh:
        totals = json.load(fh)["properties"]["metadata"]["totals"]

    conn = db.connect(db.get_default_db_path(data_dir))
    walk = db.get_latest_street_walk(conn, CITY_ID)
    conn.close()

    assert walk["length_km"] == totals["length_km"]
    assert walk["length_km_covered"] == totals["length_km_covered"]
    assert walk["length_km_covered_any"] == totals["length_km_covered_any"]
    assert walk["median_covered_age_years"] == totals["median_covered_age_years"]
    assert walk["length_km"] > 0
    assert 100.0 * walk["length_km_covered"] / walk["length_km"] == pytest.approx(
        walk["coverage_pct_by_length"], abs=0.05
    )
