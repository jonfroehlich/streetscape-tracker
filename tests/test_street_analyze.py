"""
streetscape_street_analyzer.analyze CLI tests (no network).

Exercises the policy layer around the pure coverage logic: run selection
(latest vs explicit --run-date), the published artifact's name/metadata, and
each error exit. The OSM fetch is monkeypatched to synthetic edges, so the
only real I/O is the tmp catalog and a tiny csv.gz run file.
"""

import gzip
import json
import logging
import os

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from streetscape_metadata_tracker import db
from streetscape_street_analyzer import analyze

RUN_CSV = "bend--oregon--united-states_width_5000_height_5000_step_20_2026-07-02.csv.gz"


@pytest.fixture
def data_dir(tmp_path):
    """A tmp data dir holding a one-city catalog and a two-pano run csv.gz."""
    d = str(tmp_path)
    conn = db.connect(db.get_default_db_path(d))
    city_id = db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )
    from datetime import date

    db.register_run(conn, city_id=city_id, run_date=date(2026, 4, 1), csv_filename="old.csv.gz")
    db.register_run(conn, city_id=city_id, run_date=date(2026, 7, 2), csv_filename=RUN_CSV)
    conn.close()

    # Two official-Google panos ON the street, plus noise rows the selector
    # must drop (third-party copyright, ZERO_RESULTS).
    df = pd.DataFrame(
        {
            "query_lat": [44.0500, 44.0500, 44.0500, 44.0501],
            "query_lon": [-121.3100, -121.3095, -121.3090, -121.3085],
            "query_timestamp": ["2026-07-02T00:00:00+00:00"] * 4,
            "pano_lat": [44.0500, 44.0500, 44.0500, None],
            "pano_lon": [-121.3100, -121.3095, -121.3090, None],
            "pano_id": ["a", "b", "c", None],
            "capture_date": ["2024-07-01", "2025-07-01", "2023-01-01", None],
            "copyright_info": ["© Google", "© Google", "© Somebody Else", None],
            "status": ["OK", "OK", "OK", "ZERO_RESULTS"],
        }
    )
    df.to_csv(os.path.join(d, RUN_CSV), index=False, compression="gzip")
    return d


@pytest.fixture
def fake_edges(monkeypatch):
    """Replace the OSM fetch with one on-street edge and one far-away edge."""
    edges = gpd.GeoDataFrame(
        {
            "highway": ["residential", "service"],
            "length": [90.0, 90.0],
        },
        geometry=[
            LineString([(-121.3100, 44.0500), (-121.3090, 44.0500)]),  # has panos
            LineString([(-121.3100, 44.0600), (-121.3090, 44.0600)]),  # ~1.1 km away
        ],
        crs="EPSG:4326",
    )
    calls = {}

    def fake_fetch(city_row, data_dir, *, refresh=False, network_type="drive", conn=None):
        calls["refresh"] = refresh
        calls["conn"] = conn
        return edges

    monkeypatch.setattr(analyze, "fetch_street_edges", fake_fetch)
    return calls


def _run(data_dir, *argv):
    return analyze.main([*argv, "--data-dir", data_dir, "--log-level", "ERROR"])


def test_analyze_writes_artifact_for_latest_run(data_dir, fake_edges, capsys):
    assert _run(data_dir, "Bend, Oregon, United States") == 0
    out_path = os.path.join(
        data_dir,
        "bend--oregon--united-states_width_5000_height_5000_step_20_2026-07-02_streets.json.gz",
    )
    assert os.path.exists(out_path)
    with gzip.open(out_path, "rt") as fh:
        fc = json.load(fh)

    meta = fc["properties"]["metadata"]
    assert meta["kind"] == "street_coverage"
    assert meta["city_id"] == "bend--oregon--united-states"
    assert meta["provider"] == "gsv"
    assert meta["run_date"] == "2026-07-02"  # latest, not the 2026-04-01 run
    assert meta["source_csv"] == RUN_CSV
    assert meta["totals"]["segments"] == 2
    assert meta["totals"]["covered"] == 1
    assert meta["totals"]["uncovered"] == 1
    covered = [f["properties"]["covered"] for f in fc["features"]]
    assert sorted(covered) == [False, True]
    # The analyzer threads its catalog connection into the network fetch so
    # the frozen network gets registered (issue #103).
    assert fake_edges["conn"] is not None
    assert "1/2 segments covered" in capsys.readouterr().out


def test_analyze_explicit_run_date_and_refresh_flag(data_dir, fake_edges):
    # The 2026-04-01 run's csv.gz is not on disk -> error exit, proving the
    # explicit --run-date selected the older run rather than the latest.
    assert _run(data_dir, "Bend, Oregon, United States", "--run-date", "2026-04-01") == 1
    assert _run(data_dir, "Bend, Oregon, United States", "--refresh") == 0
    assert fake_edges["refresh"] is True


def _overwrite_run_csv(data_dir, copyright_values):
    """Rewrite the latest run's csv.gz with two on-street OK panos whose only
    difference is their copyright_info column."""
    df = pd.DataFrame(
        {
            "query_lat": [44.0500, 44.0500],
            "query_lon": [-121.3100, -121.3095],
            "query_timestamp": ["2026-07-02T00:00:00+00:00"] * 2,
            "pano_lat": [44.0500, 44.0500],
            "pano_lon": [-121.3100, -121.3095],
            "pano_id": ["a", "b"],
            "capture_date": ["2024-07-01", "2025-07-01"],
            "copyright_info": copyright_values,
            "status": ["OK", "OK"],
        }
    )
    df.to_csv(os.path.join(data_dir, RUN_CSV), index=False, compression="gzip")


def _covered_count(data_dir):
    out_path = os.path.join(
        data_dir,
        "bend--oregon--united-states_width_5000_height_5000_step_20_2026-07-02_streets.json.gz",
    )
    with gzip.open(out_path, "rt") as fh:
        return json.load(fh)["properties"]["metadata"]["totals"]["covered"]


def test_analyze_warns_on_legacy_no_copyright(data_dir, fake_edges, caplog):
    # A legacy pre-copyright baseline: copyright_info entirely empty. Nothing
    # matches the © Google filter, so the artifact is all-uncovered — and the
    # analyzer must say so rather than let it read as a real coverage gap.
    _overwrite_run_csv(data_dir, [None, None])
    with caplog.at_level(logging.WARNING):
        assert _run(data_dir, "Bend, Oregon, United States") == 0
    assert "legacy pre-copyright baseline" in caplog.text
    assert _covered_count(data_dir) == 0


def test_analyze_warns_on_third_party_only(data_dir, fake_edges, caplog):
    # Modern CSV, but every pano is third-party (no official Google imagery):
    # correctly 0 covered, and the warning must NOT blame legacy metadata.
    _overwrite_run_csv(data_dir, ["© Joel Cohen", "© Morty Globus"])
    with caplog.at_level(logging.WARNING):
        assert _run(data_dir, "Bend, Oregon, United States") == 0
    assert "no official" in caplog.text
    assert "legacy pre-copyright baseline" not in caplog.text
    assert _covered_count(data_dir) == 0


def test_analyze_error_exits(data_dir, fake_edges, tmp_path_factory):
    assert _run(data_dir, "Nowhere, KS") == 1  # unknown city
    assert _run(data_dir, "Bend, Oregon, United States", "--provider", "mapillary") == 1  # no run
    assert _run(data_dir, "Bend, Oregon, United States", "--run-date", "2019-01-01") == 1
    empty = str(tmp_path_factory.mktemp("empty"))
    assert _run(empty, "Bend, Oregon, United States") == 1  # no catalog DB


def test_a_pano_on_the_street_counts_however_far_its_query_point_was(data_dir, fake_edges):
    """analyze.py loads the GRID run, so the loader's query-radius gate (issue
    #367) would fire here -- and must not. This analysis judges a pano by its
    distance to a STREET (its own match_dist), so a pano standing on the street
    covers it even when the grid point that found it was ~300 m away. Filtered,
    the pano would read OUT_OF_RADIUS and the street would go uncovered."""
    df = pd.DataFrame(
        {
            "query_lat": [44.0527],  # ~300 m north of the on-street edge
            "query_lon": [-121.3095],
            "query_timestamp": ["2026-07-02T00:00:00+00:00"],
            "pano_lat": [44.0500],  # on the edge
            "pano_lon": [-121.3095],
            "pano_id": ["a"],
            "capture_date": ["2024-07-01"],
            "copyright_info": ["© Google"],
            "status": ["OK"],
        }
    )
    df.to_csv(os.path.join(data_dir, RUN_CSV), index=False, compression="gzip")
    assert _run(data_dir, "Bend, Oregon, United States") == 0
    assert _covered_count(data_dir) == 1
