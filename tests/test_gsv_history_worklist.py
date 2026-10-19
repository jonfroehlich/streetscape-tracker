"""
Tests for the GSV capture-history work-list builder (issue #442):
``streetscape_street_analyzer.history_worklist.build_worklist`` and
``scripts/build_gsv_history_worklist.py``. Offline; the one place the script
could reach Overpass is pinned shut.
"""

import gzip
import json
import os
import sqlite3
from datetime import date

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
import pytest
from shapely.geometry import LineString

from scripts.build_gsv_history_worklist import main as build_main
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.download_gsv_history import WORKLIST_COLUMNS, load_worklist
from streetscape_metadata_tracker.naming import network_cache_path
from streetscape_street_analyzer import download_street_network as dsn
from streetscape_street_analyzer.history_worklist import build_worklist
from streetscape_street_analyzer.road_sampling import generate_samples

GOOGLE = "© Google"
SPACING = 15.0
# ~222 m, ~55 m and ~111 m edges: 15, 4 and 7 samples at 15 m.
EDGES = {
    "1_2": LineString([(-121.30, 44.05), (-121.30, 44.052)]),
    "2_3": LineString([(-121.30, 44.052), (-121.30, 44.0525)]),
    "3_4": LineString([(-121.30, 44.0525), (-121.299, 44.0525)]),
}


def _edges(ids=EDGES):
    return gpd.GeoDataFrame(
        {"edge_id": list(ids), "highway": "residential"},
        geometry=list(ids.values()),
        crs="EPSG:4326",
    )


def _collected(samples, *, skip=(), third_party=(), empty=(), undated=()):
    """A walk CSV frame for the samples: Google OK by default, per-key overrides."""
    rows = []
    for s in samples.itertuples(index=False):
        key = (s.edge_id, s.sample_idx)
        if key in skip:
            continue
        row = {
            "query_lat": s.lat,
            "query_lon": s.lon,
            "query_timestamp": "2026-05-01T00:00:00+00:00",
            "pano_lat": s.lat,
            "pano_lon": s.lon,
            "pano_id": f"p-{s.edge_id}-{s.sample_idx}",
            "capture_date": pd.Timestamp("2023-06-01"),
            "copyright_info": GOOGLE,
            "status": "OK",
        }
        if key in third_party:
            row["copyright_info"] = "Someone Else"
        if key in empty:
            row.update(
                pano_id=None, capture_date=pd.NaT, copyright_info=None, status="ZERO_RESULTS"
            )
        if key in undated:
            row.update(capture_date=pd.NaT, status="NO_DATE")
        rows.append(row)
    return pd.DataFrame(rows)


def _build(edges, collected, **kw):
    kw.setdefault("city_id", "bend--oregon--united-states")
    kw.setdefault("walk_csv_filename", "bend_streetwalk.csv.gz")
    kw.setdefault("network_type", "drive")
    kw.setdefault("spacing_m", SPACING)
    return build_worklist(edges, collected, **kw)


def test_pass_one_is_each_edge_midpoint_and_pass_two_the_rest():
    edges = _edges()
    samples = generate_samples(edges, SPACING)
    wl, stats = _build(edges, _collected(samples))
    assert list(wl.columns) == WORKLIST_COLUMNS
    assert len(wl) == len(samples) == stats["samples"]
    p1 = wl[wl["pass"] == 1]
    assert sorted(p1["edge_id"]) == sorted(EDGES)
    assert all(p1["sample_idx"] == p1["edge_samples"] // 2)
    assert stats["pass1_rows"] == 3 and stats["pass2_rows"] == len(samples) - 3
    assert stats["edges"] == 3
    # The union of the two passes is the sample set: nothing queried twice,
    # nothing skipped.
    assert set(zip(wl["edge_id"], wl["sample_idx"], strict=True)) == set(
        zip(samples["edge_id"], samples["sample_idx"], strict=True)
    )
    assert not wl.duplicated(["edge_id", "sample_idx"]).any()


def test_anchors_come_from_the_quantized_coordinate_join():
    edges = _edges()
    samples = generate_samples(edges, SPACING)
    coll = _collected(
        samples,
        skip={("1_2", 3)},
        third_party={("1_2", 4)},
        empty={("1_2", 5)},
        undated={("1_2", 6)},
    )
    # A csv.gz round-trip perturbation below the 9-decimal key must still join.
    coll["query_lat"] = coll["query_lat"] + 1e-12
    wl, stats = _build(edges, coll)
    w = wl.set_index(["edge_id", "sample_idx"])
    assert w.loc[("1_2", 0), "anchor_pano_id"] == "p-1_2-0"
    assert w.loc[("1_2", 0), "anchor_capture_date"] == "2023-06-01"
    assert pd.isna(w.loc[("1_2", 3), "anchor_pano_id"]) and pd.isna(
        w.loc[("1_2", 3), "anchor_status"]
    )
    assert pd.isna(w.loc[("1_2", 4), "anchor_pano_id"])  # third party is never an anchor
    assert w.loc[("1_2", 4), "anchor_copyright"] == "Someone Else"  # but is recorded
    assert pd.isna(w.loc[("1_2", 5), "anchor_pano_id"])
    assert w.loc[("1_2", 5), "anchor_status"] == "ZERO_RESULTS"
    # NO_DATE is PRESENT: an anchor with no date.
    assert w.loc[("1_2", 6), "anchor_pano_id"] == "p-1_2-6"
    assert pd.isna(w.loc[("1_2", 6), "anchor_capture_date"])
    assert stats["unmatched_samples"] == 1
    assert stats["anchored"] == len(wl) - 3
    assert stats["anchored_share"] == round((len(wl) - 3) / len(wl), 4)


def test_coincident_samples_collapse_to_the_first_edge_and_are_counted():
    # Two edges over IDENTICAL geometry: every sample of the second coincides.
    edges = _edges({"1_2": EDGES["1_2"], "9_10": EDGES["1_2"]})
    samples = generate_samples(edges, SPACING)
    wl, stats = _build(edges, _collected(samples))
    assert set(wl["edge_id"]) == {"1_2"}
    assert stats["coincident_dropped"] == len(samples) // 2
    assert stats["samples"] == len(samples) // 2


def test_limit_keeps_whole_edges_in_sample_order():
    edges = _edges()
    samples = generate_samples(edges, SPACING)
    wl, stats = _build(edges, _collected(samples), limit=2)
    assert set(wl["edge_id"]) == {"1_2", "2_3"}
    assert stats["edges"] == 2
    assert (wl["edge_id"] == "1_2").sum() == (samples["edge_id"] == "1_2").sum()
    with pytest.raises(ValueError):
        _build(edges, _collected(samples), limit=0)


def test_empty_network_yields_an_empty_worklist():
    empty = pd.DataFrame(
        columns=["query_lat", "query_lon", "status", "pano_id", "capture_date", "copyright_info"]
    )
    wl, stats = _build(_edges({}), empty)
    assert len(wl) == 0 and list(wl.columns) == WORKLIST_COLUMNS
    assert stats["anchored_share"] is None


# ── The script, against a tmp catalog ──────────────────────────────────────


def _graph():
    g = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {1: (-121.30, 44.05), 2: (-121.30, 44.052), 3: (-121.30, 44.0525)}
    for n, (x, y) in nodes.items():
        g.add_node(n, x=x, y=y)
    g.add_edge(1, 2, 0, osmid=10, highway="residential", length=222.0)
    g.add_edge(2, 3, 0, osmid=11, highway="residential", length=55.0)
    return g


@pytest.fixture
def catalog(conn, data_dir, monkeypatch):
    city_id = db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.051,
        center_lon=-121.30,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
    )
    graphml = network_cache_path(city_id, data_dir, "drive")
    os.makedirs(os.path.dirname(graphml), exist_ok=True)
    ox.save_graphml(_graph(), graphml)
    edges = dsn.graph_to_edges(ox.load_graphml(graphml))
    samples = generate_samples(edges, SPACING)
    walk_name = "bend_walk.csv.gz"
    with gzip.open(os.path.join(data_dir, walk_name), "wt", encoding="utf-8") as f:
        coll = _collected(samples)
        coll["capture_date"] = coll["capture_date"].dt.strftime("%Y-%m-%d")
        coll.to_csv(f, index=False)
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 5, 1),
        csv_filename=walk_name,
        spacing_m=SPACING,
    )
    conn.commit()

    def never(*a, **kw):
        raise AssertionError("the builder must never reach Overpass")

    monkeypatch.setattr(dsn, "fetch_graph", never)
    monkeypatch.setattr(dsn, "fetch_street_edges", never)
    return city_id, graphml, len(samples)


def test_script_writes_the_worklist_and_sidecar(catalog, data_dir, tmp_path, capsys):
    city_id, _, n_samples = catalog
    out = tmp_path / "worklists"
    assert build_main(["--city", city_id, "--data-dir", data_dir, "--out-dir", str(out)]) == 0
    wl = load_worklist(str(out / "bend_walk.worklist.csv.gz"))
    assert len(wl) == n_samples
    assert wl["city_id"].unique().tolist() == [city_id]
    assert wl["walk_csv_filename"].unique().tolist() == ["bend_walk.csv.gz"]
    assert (wl["pass"] == 1).sum() == 2
    assert wl["anchor_pano_id"].notna().all()
    side = json.load(open(out / "bend_walk.worklist.json"))
    assert side["walk_run_date"] == "2026-05-01" and side["spacing_m"] == SPACING
    assert side["samples"] == n_samples and side["anchored"] == n_samples
    assert "1 of 1 work lists written" in capsys.readouterr().out


def test_script_limit_and_all(catalog, data_dir, tmp_path):
    city_id, _, _ = catalog
    out = tmp_path / "w"
    assert build_main(["--all", "--data-dir", data_dir, "--out-dir", str(out), "--limit", "1"]) == 0
    wl = load_worklist(str(out / "bend_walk.worklist.csv.gz"))
    assert wl["edge_id"].nunique() == 1
    assert json.load(open(out / "bend_walk.worklist.json"))["limit"] == 1


def test_script_refuses_a_missing_graphml_rather_than_fetching(catalog, data_dir, tmp_path, capsys):
    city_id, graphml, _ = catalog
    os.remove(graphml)
    out = tmp_path / "w"
    assert build_main(["--city", city_id, "--data-dir", data_dir, "--out-dir", str(out)]) == 1
    err = capsys.readouterr().err
    assert "refusing rather than fetching it from Overpass" in err
    assert not out.exists()


def test_script_refuses_an_unknown_city_and_a_missing_catalog(data_dir, tmp_path, capsys):
    assert build_main(["--city", "nowhere", "--data-dir", data_dir]) == 2
    assert "no catalog" in capsys.readouterr().err
    db.connect(os.path.join(data_dir, "streetscape_tracker.db")).close()
    assert build_main(["--city", "nowhere", "--data-dir", data_dir]) == 2
    assert "catalogs no runs or walks" in capsys.readouterr().err


def test_script_refuses_an_unknown_city(catalog, data_dir, capsys):
    assert build_main(["--city", "nowhere", "--data-dir", data_dir]) == 2
    assert "not a registered city" in capsys.readouterr().err


def test_script_never_migrates_the_catalog(catalog, data_dir, tmp_path, capsys):
    """The 2026-10-17 outage: a newer checkout's db.connect migrated PROD."""
    db_path = os.path.join(data_dir, "streetscape_tracker.db")
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    before = open(db_path, "rb").read()
    out = tmp_path / "w"
    assert build_main(["--all", "--data-dir", data_dir, "--out-dir", str(out)]) == 2
    assert f"schema version {db.SCHEMA_VERSION - 1}" in capsys.readouterr().err
    assert open(db_path, "rb").read() == before
    assert not out.exists()
