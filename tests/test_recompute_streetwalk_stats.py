"""Tests for scripts/recompute_streetwalk_stats.py (issue #262): re-derive every
road walk's stored stats from its snapshot CSV and its frozen GraphML under the
current coverage definition, refuse a series rather than score it against the
wrong frame, and never reach Overpass.

Fixtures are built the way production built them. Each walk is collected by
the REAL `collect.run_collect`, loading a real GraphML from the frozen-network
cache (a cache hit, so no network), with only the provider request primitive
served from memory: GSV's per-point fetch, or the whole census collector
(KartaView, Mapillary, Panoramax) for a census walk. A stale, pre-#257 walk is produced by the code
that produced it: the collector run with `street_coverage.PRESENT_STATUSES`
narrowed to `OK`, which is exactly what #257 widened.
"""

import gzip
import hashlib
import json
import math
import os

import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import pytest

import scripts.recompute_streetwalk_stats as recompute_module
from scripts.recompute_streetwalk_stats import COORD_TOLERANCE_DEG, main, match_frame
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import download_gsv as dg
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.naming import network_cache_path
from streetscape_metadata_tracker.walk_diff import compute_walk_diff, load_streetwalk_coverage
from streetscape_street_analyzer import collect, street_coverage
from streetscape_street_analyzer import download_street_network as dsn
from streetscape_street_analyzer.road_sampling import quantize_coord

CITY_QUERY = "Bend, Oregon, United States"
CITY_ID = "bend--oregon--united-states"
OTHER_QUERY = "Redmond, Oregon, United States"
OTHER_ID = "redmond--oregon--united-states"
D1, D2 = "2026-07-08", "2026-09-01"

# Every street_walks column collect.py writes from the artifact's summary.
# Spelled out here rather than imported, so dropping one from the script's own
# list cannot also drop it from the comparison.
EXPECTED_STAT_COLUMNS = (
    "edges_total",
    "edges_fully_covered",
    "mean_edge_coverage",
    "coverage_pct_by_length",
    "coverage_pct_by_length_any",
    "coverage_by_highway",
    "length_km",
    "length_km_covered",
    "length_km_covered_any",
    "median_covered_age_years",
)

# Samples north of this latitude come back with no capture date (NO_DATE):
# part of the long edge and all of the short one.
NO_DATE_NORTH_OF = 44.051

# The census walks (#290's shared join): each provider's collector is replaced
# whole, so every one of them reaches the recompute through the real collector.
CENSUS_PROVIDERS = ("kartaview", "mapillary", "panoramax")


@pytest.fixture(autouse=True)
def _no_batch_in_flight(monkeypatch):
    """The in-flight detector reads `ps`; a real run-due must not decide these tests."""
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: None)


def _graph(extra_edge=False, footway=False):
    g = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {1: (-121.30, 44.05), 2: (-121.30, 44.052), 3: (-121.30, 44.0525)}
    if extra_edge or footway:
        nodes[4] = (-121.299, 44.0525)
    for n, (x, y) in nodes.items():
        g.add_node(n, x=x, y=y)
    g.add_edge(1, 2, 0, osmid=10, highway="residential", length=222.0)
    g.add_edge(2, 1, 0, osmid=10, highway="residential", length=222.0)
    g.add_edge(2, 3, 0, osmid=11, highway="service", length=55.0)
    if extra_edge:
        g.add_edge(3, 4, 0, osmid=12, highway="residential", length=80.0)
    if footway:
        g.add_edge(3, 4, 0, osmid=13, highway="footway", length=80.0)
    return g


def _freeze(data_dir, city_id=CITY_ID, network_type="drive", **kw):
    path = network_cache_path(city_id, data_dir, network_type)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ox.save_graphml(_graph(**kw), path)
    return path


def _register(conn, name, lat, lon):
    db.register_city(
        conn,
        city_name=name,
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=lat,
        center_lon=lon,
        grid_width_m=200,
        grid_height_m=200,
        step_m=20,
    )


def _setup(tmp_path, monkeypatch, *, pano_offset_deg=0.0, second_city=False, all_public=False):
    """Catalog + frozen networks + a GSV fake. ``pano_offset_deg`` moves every
    south-of-NO_DATE pano north of its sample, to make the match distance bite."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    conn = db.connect(db.get_default_db_path(data_dir))
    _register(conn, "Bend", 44.05, -121.30)
    if second_city:
        _register(conn, "Redmond", 44.27, -121.17)
    conn.close()
    _freeze(data_dir)
    if second_city:
        _freeze(data_dir, city_id=OTHER_ID)
    if all_public:
        _freeze(data_dir, network_type="all_public", footway=True)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")
    monkeypatch.setenv("MAPILLARY_STREETS_ACCESS_TOKEN", "MLYTOKEN")
    monkeypatch.setenv("KARTAVIEW_STREETS_ACCESS_TOKEN", "KVTOKEN")

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        north = lat > NO_DATE_NORTH_OF
        return {
            "status": "OK",
            "location": {"lat": lat if north else lat + pano_offset_deg, "lng": lon},
            "pano_id": f"pano_{lat:.6f}_{lon:.6f}",
            "copyright": "© Google",
            "date": None if north else "2022-06",  # no date -> NO_DATE
        }

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)

    def fake_census(provider):
        async def fake(query_points, city, *args, **kw):
            """The census collectors' output shape: one row per query location,
            no copyright (so a GSV-only gate would score it at 0). Mapillary and
            KartaView take a token before `out_csv`; Panoramax takes none."""
            out_csv = args[-1]
            rows = []
            for lat, lon, *_ in query_points:
                north = lat > NO_DATE_NORTH_OF
                rows.append(
                    {
                        "query_lat": lat,
                        "query_lon": lon,
                        "query_timestamp": "2026-07-08T00:00:00+00:00",
                        "pano_lat": lat,
                        "pano_lon": lon,
                        "pano_id": f"{provider}_{lat:.6f}",
                        "capture_date": None if north else "2021-05-01",
                        "copyright_info": None,
                        "status": "NO_DATE" if north else "OK",
                    }
                )
            with gzip.open(out_csv, "wt", newline="") as fh:
                pd.DataFrame(rows).to_csv(fh, index=False)
            return {"df": load_city_csv_file(out_csv), "api_requests": 1, "api_requests_total": 1}

        return fake

    for provider in CENSUS_PROVIDERS:
        monkeypatch.setattr(
            collect, f"collect_{provider}_street_samples_async", fake_census(provider)
        )
    return data_dir


def _collect(
    data_dir,
    run_date,
    monkeypatch,
    *,
    old_definition,
    force=False,
    city=CITY_QUERY,
    provider="gsv",
    network_type="drive",
    match_dist=None,
):
    """Run the real collector; `old_definition` reproduces the pre-#257 one."""
    with monkeypatch.context() as m:
        if old_definition:
            m.setattr(street_coverage, "PRESENT_STATUSES", ("OK",))
        argv = [
            city,
            "--data-dir",
            data_dir,
            "--run-date",
            run_date,
            "--spacing",
            "15",
            "--max-requests-per-minute",
            "0",
            "--provider",
            provider,
            "--network-type",
            network_type,
        ]
        if match_dist is not None:
            argv += ["--match-dist", str(match_dist)]
        if force:
            argv.append("--force")
        assert collect.run_collect(collect.build_parser().parse_args(argv)) == 0


def _conn(data_dir):
    return db.connect(db.get_default_db_path(data_dir))


def _walk(data_dir, run_date, city_id=CITY_ID, provider="gsv", network_type="drive"):
    conn = _conn(data_dir)
    try:
        return dict(
            conn.execute(
                """SELECT * FROM street_walks WHERE city_id = ? AND run_date = ?
                   AND provider = ? AND network_type = ?""",
                (city_id, run_date, provider, network_type),
            ).fetchone()
        )
    finally:
        conn.close()


def _all_walks(data_dir):
    conn = _conn(data_dir)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM street_walks ORDER BY walk_id")]
    finally:
        conn.close()


def _diff_row(data_dir, to_date):
    conn = _conn(data_dir)
    try:
        row = conn.execute(
            """SELECT d.* FROM street_walk_diffs d
               JOIN street_walks w ON w.walk_id = d.to_walk_id
               WHERE w.run_date = ?""",
            (to_date,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _artifact(data_dir, run_date, **kw):
    name = _walk(data_dir, run_date, **kw)["coverage_filename"]
    return load_streetwalk_coverage(os.path.join(data_dir, name))


def _tree_digest(root, *, without_db=False):
    """Every file under root, by relative path, with a content hash."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    if without_db:
        # db.connect touches the file itself (journal mode), so the catalog is
        # compared by content and every other file by bytes.
        out.pop("streetscape_tracker.db")
    return out


def _rewrite_csv(path, fn):
    """Apply fn to a snapshot CSV's raw string frame (simulating what is on disk)."""
    with gzip.open(path, "rt") as fh:
        raw = pd.read_csv(fh, dtype=str)
    with gzip.open(path, "wt", newline="") as fh:
        fn(raw).to_csv(fh, index=False)


def _run(data_dir, *flags):
    return main(["--data-dir", data_dir, *flags])


# ── the recompute itself ─────────────────────────────────────────────────────


def test_no_date_walk_recomputes_to_exactly_what_the_collector_writes_now(tmp_path, monkeypatch):
    """A pre-#257 walk holding NO_DATE samples recomputes to HIGHER coverage, and
    the rewritten row and artifact are identical to what the current collector
    writes from the same inputs -- so the tool cannot drift from collect.py."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    csv_path = os.path.join(data_dir, before["csv_filename"])
    with open(csv_path, "rb") as fh:
        csv_bytes = fh.read()
    # The fixture's premise: the old definition really did drop NO_DATE samples.
    assert before["coverage_pct_by_length"] < 100.0

    assert _run(data_dir, "--execute") == 0
    recomputed_row = _walk(data_dir, D1)
    recomputed_artifact = _artifact(data_dir, D1)
    assert recomputed_row["coverage_pct_by_length"] > before["coverage_pct_by_length"]
    assert recomputed_row["length_km_covered"] > before["length_km_covered"]
    with open(csv_path, "rb") as fh:
        assert fh.read() == csv_bytes, "the snapshot CSV must never be rewritten"

    # What the collector writes today from the same responses on the same date.
    _collect(data_dir, D1, monkeypatch, old_definition=False, force=True)
    collector_row = _walk(data_dir, D1)
    assert {c: recomputed_row[c] for c in EXPECTED_STAT_COLUMNS} == {
        c: collector_row[c] for c in EXPECTED_STAT_COLUMNS
    }
    assert recomputed_artifact == _artifact(data_dir, D1)


@pytest.mark.parametrize("provider", CENSUS_PROVIDERS)
def test_a_stale_census_walk_recomputes_to_exactly_what_the_collector_writes_now(
    tmp_path, monkeypatch, provider
):
    """Every census provider, not just one: a pre-#257 census walk recomputes UP
    (never to ~0, which a GSV copyright gate applied to a census CSV would give)
    and lands on exactly what the current collector writes from the same rows."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True, provider=provider)
    before = _walk(data_dir, D1, provider=provider)
    assert 0.0 < before["coverage_pct_by_length"] < 100.0  # premise: NO_DATE was dropped

    assert _run(data_dir, "--execute") == 0
    recomputed_row = _walk(data_dir, D1, provider=provider)
    recomputed_artifact = _artifact(data_dir, D1, provider=provider)
    assert recomputed_row["coverage_pct_by_length"] == 100.0

    _collect(data_dir, D1, monkeypatch, old_definition=False, force=True, provider=provider)
    collector_row = _walk(data_dir, D1, provider=provider)
    assert {c: recomputed_row[c] for c in EXPECTED_STAT_COLUMNS} == {
        c: collector_row[c] for c in EXPECTED_STAT_COLUMNS
    }
    assert recomputed_artifact == _artifact(data_dir, D1, provider=provider)


def test_census_broad_network_and_match_distance_walks_recompute_to_themselves(
    tmp_path, monkeypatch, capsys
):
    """Walks collected under the CURRENT definition are a fixed point, across the
    axes a GSV/drive/default-match fixture cannot see: a census walk of each
    census provider (no copyright, so a GSV gate would zero it), an all_public walk on its own
    GraphML of the same city (a network type forced to drive, or a network cache
    keyed on the city alone, scores it against the wrong frame), and a gsv walk
    at a non-default match distance whose panos sit between 10 m and 25 m."""
    data_dir = _setup(tmp_path, monkeypatch, pano_offset_deg=0.00013, all_public=True)
    for run_date in (D1, D2):
        _collect(data_dir, run_date, monkeypatch, old_definition=False, match_dist=10)
        for provider in CENSUS_PROVIDERS:
            _collect(data_dir, run_date, monkeypatch, old_definition=False, provider=provider)
        _collect(data_dir, run_date, monkeypatch, old_definition=False, network_type="all_public")
    gsv = _walk(data_dir, D1)
    mly = _walk(data_dir, D1, provider="mapillary")
    broad = _walk(data_dir, D1, network_type="all_public")
    # Premises: each axis is live in this fixture.
    assert gsv["match_dist_m"] == 10.0 and gsv["coverage_pct_by_length"] < 100.0
    assert mly["coverage_pct_by_length"] == 100.0
    assert "footway" in json.loads(broad["coverage_by_highway"])
    rows = _all_walks(data_dir)
    digest = _tree_digest(data_dir, without_db=True)

    assert _run(data_dir, "--execute") == 0
    out = capsys.readouterr().out
    assert "5 series (10 walks) scanned, 5 unchanged" in out
    assert _all_walks(data_dir) == rows
    assert _tree_digest(data_dir, without_db=True) == digest


def test_a_second_pass_changes_nothing(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    assert _run(data_dir, "--execute") == 0
    row = _walk(data_dir, D1)
    digest = _tree_digest(data_dir, without_db=True)
    capsys.readouterr()
    assert _run(data_dir, "--execute") == 0
    assert "1 unchanged" in capsys.readouterr().out
    assert _tree_digest(data_dir, without_db=True) == digest
    assert _walk(data_dir, D1) == row


def test_catalog_only_moves_the_rows_and_says_the_site_is_mixed(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    old_cov = _walk(data_dir, D1)["coverage_pct_by_length"]
    artifact_before = _artifact(data_dir, D1)

    assert _run(data_dir, "--execute", "--catalog-only") == 0
    out = capsys.readouterr().out
    assert _walk(data_dir, D1)["coverage_pct_by_length"] > old_cov
    assert _artifact(data_dir, D1) == artifact_before
    assert "left stale (--catalog-only)" in out
    assert "re-creating the phantom delta" in out

    # A second catalog-only pass writes nothing, so it prints no "changed" line.
    assert _run(data_dir, "--execute", "--catalog-only") == 0
    out = capsys.readouterr().out
    assert "  changed:" not in out and "left stale (--catalog-only)" in out


def test_the_filters_select_whole_series_and_nothing_else(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch, second_city=True)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D1, monkeypatch, old_definition=True, provider="mapillary")
    _collect(data_dir, D1, monkeypatch, old_definition=True, city=OTHER_QUERY)
    gsv, mly = _walk(data_dir, D1), _walk(data_dir, D1, provider="mapillary")
    other = _walk(data_dir, D1, city_id=OTHER_ID)

    assert _run(data_dir, "--execute", "--provider", "mapillary") == 0
    assert _walk(data_dir, D1) == gsv
    assert _walk(data_dir, D1, city_id=OTHER_ID) == other
    assert (
        _walk(data_dir, D1, provider="mapillary")["coverage_pct_by_length"]
        > (mly["coverage_pct_by_length"])
    )

    assert _run(data_dir, "--execute", "--city", OTHER_ID) == 0
    assert _walk(data_dir, D1) == gsv
    assert (
        _walk(data_dir, D1, city_id=OTHER_ID)["coverage_pct_by_length"]
        > (other["coverage_pct_by_length"])
    )


# ── the sample frame ─────────────────────────────────────────────────────────


def test_match_frame_tolerates_one_ulp_across_the_half_way_boundary():
    """Two coordinates one ULP apart on either side of quantize_coord's half-way
    point have DIFFERENT 9-decimal keys; they are still one location, and the
    returned samples carry the CSV's coordinates so the scorer's key join hits."""
    # The float nearest 44.0512345675 rounds up and its lower neighbour down.
    hi_side = 44.0512345675
    lo_side = float(np.nextafter(hi_side, -math.inf))
    assert quantize_coord(lo_side, -121.3) != quantize_coord(hi_side, -121.3)  # premise
    samples = pd.DataFrame(
        {
            "edge_id": ["1_2", "1_2", "2_3"],
            "sample_idx": [0, 1, 0],
            "lat": [lo_side, 44.0505, 44.0521],
            "lon": [-121.3, -121.3, -121.3],
        }
    )
    csv = pd.DataFrame(
        {
            "query_lat": [hi_side, 44.0505 + 0.5 * COORD_TOLERANCE_DEG, 44.0521, 44.0521],
            "query_lon": [-121.3, -121.3, -121.3, -121.3],
        }
    )
    out, n_noise, n_dup = match_frame(samples, csv)
    assert (n_noise, n_dup) == (2, 1)
    assert out["lat"].tolist() == [hi_side, 44.0505 + 0.5 * COORD_TOLERANCE_DEG, 44.0521]
    assert {quantize_coord(a, b) for a, b in zip(out["lat"], out["lon"], strict=True)} == {
        quantize_coord(a, b) for a, b in zip(csv["query_lat"], csv["query_lon"], strict=True)
    }


def test_sub_tolerance_noise_in_the_csv_still_scores_every_sample(tmp_path, monkeypatch, capsys):
    """End to end: a walk whose CSV coordinates moved by less than the tolerance
    (enough to change their 9-decimal keys) is accepted AND scored as before --
    loosening the check without substituting the CSV coordinates would score
    those samples as uncovered."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=False)
    before = _walk(data_dir, D1)
    csv_path = os.path.join(data_dir, before["csv_filename"])

    def jitter(raw):
        lat = raw["query_lat"].astype(float)
        raw["query_lat"] = [repr(v + 0.6 * COORD_TOLERANCE_DEG) for v in lat]
        return raw

    _rewrite_csv(csv_path, jitter)
    assert _run(data_dir, "--execute") == 0
    out = capsys.readouterr().out
    assert "1 unchanged" in out and "float noise" in out
    assert _walk(data_dir, D1) == before


def test_duplicate_csv_rows_are_accepted_and_counted(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=False)
    before = _walk(data_dir, D1)
    csv_path = os.path.join(data_dir, before["csv_filename"])
    _rewrite_csv(csv_path, lambda raw: pd.concat([raw, raw.iloc[:2]], ignore_index=True))

    assert _run(data_dir, "--execute") == 0
    out = capsys.readouterr().out
    assert "1 unchanged" in out and "2 duplicate CSV rows" in out
    assert _walk(data_dir, D1) == before


def test_a_sample_frame_mismatch_refuses_the_whole_series(tmp_path, monkeypatch, capsys):
    """One walk whose CSV no longer matches the regenerated frame skips the
    SERIES: the other walk, which would move, is left on the old definition too,
    so the history never mixes two definitions."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=True)
    # Drop one sample row from D2's CSV: the count pre-check still passes
    # (sample_points counts the regenerated frame), so only the key match can see it.
    _rewrite_csv(os.path.join(data_dir, _walk(data_dir, D2)["csv_filename"]), lambda r: r.iloc[1:])
    rows_before = (_walk(data_dir, D1), _walk(data_dir, D2))
    digest = _tree_digest(data_dir, without_db=True)

    assert _run(data_dir, "--execute") == 1
    out = capsys.readouterr().out
    assert f"{CITY_ID} [gsv/drive]: 2 walk(s) skipped" in out
    assert "(sample key mismatch)" in out and "1 series refused (sample key mismatch 1)" in out
    assert (_walk(data_dir, D1), _walk(data_dir, D2)) == rows_before
    assert _tree_digest(data_dir, without_db=True) == digest


def test_a_refreshed_network_is_refused_by_the_sample_count(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    _freeze(data_dir, extra_edge=True)  # a --refresh overwrote the network in place

    assert _run(data_dir, "--execute") == 1
    out = capsys.readouterr().out
    assert "the network was refreshed since this walk" in out
    assert "(sample count mismatch 1)" in out
    assert _walk(data_dir, D1) == before


def test_a_missing_graphml_refuses_and_never_reaches_overpass(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    os.remove(network_cache_path(CITY_ID, data_dir, "drive"))

    calls = []

    def forbidden(name):
        def _fail(*a, **k):
            calls.append(name)
            raise AssertionError(f"{name} reached")

        return _fail

    for name in ("fetch_graph", "fetch_street_edges", "_download_graph", "_overpass_refusing"):
        monkeypatch.setattr(dsn, name, forbidden(name))
    monkeypatch.setattr(ox, "graph_from_bbox", forbidden("graph_from_bbox"))

    assert _run(data_dir, "--execute") == 1
    out = capsys.readouterr().out
    assert "refusing rather than fetching it from Overpass" in out
    assert "(missing GraphML 1)" in out
    assert calls == []
    assert _walk(data_dir, D1) == before


# ── the dry run and concurrency ──────────────────────────────────────────────


def test_the_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=False)
    digest = _tree_digest(data_dir)

    assert _run(data_dir) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "would change" in out
    # Byte-identical, catalog included, and no -wal/-shm left behind.
    assert _tree_digest(data_dir) == digest


def test_execute_is_refused_while_run_due_is_in_flight(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: "pid 1: run-due")
    assert _run(data_dir, "--execute") == 64
    assert _walk(data_dir, D1) == before


def test_a_run_due_starting_mid_pass_stops_before_the_next_write(tmp_path, monkeypatch, capsys):
    """The start-of-pass check passes; the per-series re-check finds a batch."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    answers = iter([None, "pid 9: run-due"])
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: next(answers))
    assert _run(data_dir, "--execute") == 1
    assert "a run-due started" in capsys.readouterr().out
    assert _walk(data_dir, D1) == before


def test_a_series_changed_since_selection_is_abandoned(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    real_recompute = recompute_module.recompute_walk

    def recompute_then_collect(row, edges, data_dir_, report):
        rec = real_recompute(row, edges, data_dir_, report)
        _collect(data_dir, D2, monkeypatch, old_definition=True)  # a manual collect lands
        return rec

    monkeypatch.setattr(recompute_module, "recompute_walk", recompute_then_collect)
    assert _run(data_dir, "--execute") == 1
    assert "its walks changed during the pass; abandoned unwritten" in capsys.readouterr().out
    assert _walk(data_dir, D1) == before


# ── walk diffs ───────────────────────────────────────────────────────────────


def _removed_listing(out):
    marker = "remove these there:"
    assert marker in out
    return out.split(marker, 1)[1]


def test_a_phantom_diff_recomputes_under_one_definition_and_loses_its_file(
    tmp_path, monkeypatch, capsys
):
    """#257's phantom: an old-definition walk diffed against a new-definition one
    with IDENTICAL imagery reports a coverage gain. After the recompute the row
    is a diff of two same-definition artifacts, it has no changes, its detail
    file is gone from disk and its pointer is NULL (#265)."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=False)
    phantom = _diff_row(data_dir, D2)
    # The fixture's premise: the collector really recorded the phantom.
    assert phantom["coverage_pct_by_length_delta"] > 0
    assert phantom["detail_filename"] is not None
    detail_path = os.path.join(data_dir, phantom["detail_filename"])
    assert os.path.exists(detail_path)

    # Catalog-only first: a diff reads the artifacts, so it is left and said so.
    assert _run(data_dir, "--execute", "--catalog-only") == 0
    assert "diff:" in capsys.readouterr().out
    assert (
        _diff_row(data_dir, D2)["coverage_pct_by_length_delta"]
        == phantom["coverage_pct_by_length_delta"]
    )

    assert _run(data_dir, "--execute") == 0
    out = capsys.readouterr().out
    healed = _diff_row(data_dir, D2)
    assert healed["coverage_pct_by_length_delta"] == 0.0
    assert healed["coverage_fraction_changed"] == 0
    assert healed["detail_filename"] is None
    assert not os.path.exists(detail_path)
    assert phantom["detail_filename"] in _removed_listing(out)

    # One definition on both sides: the row is the diff of the two artifacts now on disk.
    expected = compute_walk_diff(_artifact(data_dir, D1), _artifact(data_dir, D2))
    assert healed["edges_gained_coverage"] == expected.edges_gained_coverage
    assert healed["coverage_pct_by_length_delta"] == expected.coverage_pct_by_length_delta
    assert not expected.has_changes

    # And the manifest's change block followed it.
    with gzip.open(os.path.join(data_dir, "streetwalks.json.gz"), "rt") as fh:
        (entry,) = json.load(fh)["walks"]
    assert entry["run_date"] == D2
    assert entry["coverage_pct_by_length"] == _walk(data_dir, D2)["coverage_pct_by_length"]
    assert entry["change"]["coverage_pct_by_length_delta"] == 0.0


def test_a_stale_detail_file_is_rewritten_by_content(tmp_path, monkeypatch, capsys):
    """A diff with real changes whose detail file holds the wrong rows, with
    every counter and the pointer right, is still rewritten."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=False)
    _collect(data_dir, D2, monkeypatch, old_definition=False)
    # A real change: D2's southern imagery vanishes.
    csv2 = os.path.join(data_dir, _walk(data_dir, D2)["csv_filename"])

    def drop_south(raw):
        south = raw["query_lat"].astype(float) <= NO_DATE_NORTH_OF
        raw.loc[south, ["pano_lat", "pano_lon", "pano_id", "capture_date"]] = None
        raw.loc[south, "status"] = "ZERO_RESULTS"
        return raw

    _rewrite_csv(csv2, drop_south)
    assert _run(data_dir, "--execute") == 0
    row = _diff_row(data_dir, D2)
    assert row["detail_filename"] is not None and row["edges_lost_coverage"] == 0
    path = os.path.join(data_dir, row["detail_filename"])
    with gzip.open(path, "rt") as fh:
        good = fh.read()
    with gzip.open(path, "wt") as fh:
        fh.write("edge_id,change_type\nbogus,edge_added\n")
    capsys.readouterr()

    assert _run(data_dir, "--execute") == 0
    assert "detail file stale" in capsys.readouterr().out
    with gzip.open(path, "rt") as fh:
        assert fh.read() == good


def test_a_missing_diff_row_is_recorded_when_a_same_frame_predecessor_exists(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=True)
    conn = _conn(data_dir)
    conn.execute("DELETE FROM street_walk_diffs")
    conn.commit()
    conn.close()

    assert _run(data_dir, "--execute") == 0
    row = _diff_row(data_dir, D2)
    assert row is not None and row["from_walk_id"] == _walk(data_dir, D1)["walk_id"]


def test_a_failed_rediff_is_healed_by_the_next_pass(tmp_path, monkeypatch, capsys):
    """compute_and_record_walk_diff commits the delete before it writes; a
    failure in between leaves no row, and the next pass records it."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=False)
    real = recompute_module.compute_and_record_walk_diff

    def delete_then_fail(conn, *, walk_id, **kw):
        db.delete_walk_diff_for_walk(conn, walk_id)
        raise OSError("disk full")

    monkeypatch.setattr(recompute_module, "compute_and_record_walk_diff", delete_then_fail)
    assert _run(data_dir, "--execute") == 1
    assert _diff_row(data_dir, D2) is None

    monkeypatch.setattr(recompute_module, "compute_and_record_walk_diff", real)
    capsys.readouterr()
    assert _run(data_dir, "--execute") == 0
    assert "record one" in capsys.readouterr().out
    assert _diff_row(data_dir, D2)["coverage_pct_by_length_delta"] == 0.0


def test_a_changed_match_distance_is_not_a_diff_pair(tmp_path, monkeypatch, capsys):
    """The collector skips a pair whose match distance changed; so does the
    recompute, rather than reporting a missing row on every pass."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=False)
    _collect(data_dir, D2, monkeypatch, old_definition=False, match_dist=10)
    assert _diff_row(data_dir, D2) is None  # premise
    assert _run(data_dir) == 0
    assert "1 unchanged" in capsys.readouterr().out


def test_unknown_provider_is_an_argument_error(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run(data_dir, "--provider", "bing")
    assert exc.value.code == 2
