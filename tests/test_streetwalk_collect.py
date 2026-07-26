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
