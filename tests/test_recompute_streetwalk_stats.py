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
from scripts.recompute_streetwalk_stats import (
    COORD_TOLERANCE_DEG,
    WalkRefused,
    main,
    match_frame,
)
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import download_gsv as dg
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.naming import (
    generate_streetwalk_diff_filename,
    network_cache_path,
)
from streetscape_metadata_tracker.walk_diff import compute_walk_diff, load_streetwalk_coverage
from streetscape_street_analyzer import collect, street_coverage
from streetscape_street_analyzer import download_street_network as dsn
from streetscape_street_analyzer.road_sampling import generate_samples, quantize_coord

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


def _graph(extra_edge=False, footway=False, node_offset=0):
    """``node_offset`` renumbers every OSM node over IDENTICAL geometry, as a
    refresh can: same samples, same coordinates, different edge ids."""
    g = nx.MultiDiGraph(crs="EPSG:4326")
    n1, n2, n3, n4 = (node_offset + i for i in (1, 2, 3, 4))
    nodes = {n1: (-121.30, 44.05), n2: (-121.30, 44.052), n3: (-121.30, 44.0525)}
    if extra_edge or footway:
        nodes[n4] = (-121.299, 44.0525)
    for n, (x, y) in nodes.items():
        g.add_node(n, x=x, y=y)
    g.add_edge(n1, n2, 0, osmid=10, highway="residential", length=222.0)
    g.add_edge(n2, n1, 0, osmid=10, highway="residential", length=222.0)
    g.add_edge(n2, n3, 0, osmid=11, highway="service", length=55.0)
    if extra_edge:
        g.add_edge(n3, n4, 0, osmid=12, highway="residential", length=80.0)
    if footway:
        g.add_edge(n3, n4, 0, osmid=13, highway="footway", length=80.0)
    return g


# The north end of a single-sample edge, found by search: the edge's one
# sample then sits one ULP from a 9-decimal half-way point, and pandas' default
# C float parser (what fileutils.load_city_csv_file reads with) puts the CSV's
# copy of it on the OTHER side, while Python's correctly rounded float() of the
# same text reproduces the sample exactly. The test re-measures this premise.
BOUNDARY_NORTH_LAT = 44.0501200169994


def _boundary_graph():
    """A boundary edge (south of NO_DATE_NORTH_OF, so its sample is OK and
    covered unless its key misses) plus an all-NO_DATE edge, so a pre-#257
    walk on it really moves."""
    g = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {
        1: (-121.30, 44.05),
        2: (-121.30, BOUNDARY_NORTH_LAT),
        3: (-121.299, 44.0515),
        4: (-121.299, 44.0518),
    }
    for n, (x, y) in nodes.items():
        g.add_node(n, x=x, y=y)
    g.add_edge(1, 2, 0, osmid=10, highway="residential", length=13.3)
    g.add_edge(3, 4, 0, osmid=11, highway="residential", length=33.3)
    return g


def _freeze(data_dir, city_id=CITY_ID, network_type="drive", graph=None, **kw):
    path = network_cache_path(city_id, data_dir, network_type)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ox.save_graphml(graph if graph is not None else _graph(**kw), path)
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
    """Every file under root, by relative path, with a content hash.

    ``without_db`` is for an --execute pass: db.connect touches the catalog
    itself (journal mode), so the catalog is compared by content elsewhere,
    and every --execute pass regenerates the manifest (stamping a fresh
    ``generated_at``), so it is compared by everything else it holds."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    if without_db:
        out.pop("streetscape_tracker.db")
        manifest = os.path.join(root, "streetwalks.json.gz")
        if os.path.exists(manifest):
            out["streetwalks.json.gz"] = _manifest_body(root)
    return out


def _manifest_body(data_dir):
    with gzip.open(os.path.join(data_dir, "streetwalks.json.gz"), "rt") as fh:
        body = json.load(fh)
    body.pop("generated_at", None)
    return body


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
    before = samples.copy()
    assert match_frame(samples, csv) == (2, 1)
    # Validation only: the samples are never moved onto the CSV's coordinates.
    pd.testing.assert_frame_equal(samples, before, check_exact=True)


def test_match_frame_refuses_two_csv_locations_closer_than_twice_the_tolerance():
    """Two CSV locations with different 9-decimal keys but within 2x the
    tolerance of each other make a tolerant match ambiguous, so the frame is
    refused even though each sample here has an exact key match of its own."""
    lat_a = 44.0505
    lat_b = 44.0505 + 1.5 * COORD_TOLERANCE_DEG
    assert quantize_coord(lat_a, -121.3) != quantize_coord(lat_b, -121.3)  # premise
    samples = pd.DataFrame(
        {
            "edge_id": ["1_2", "1_2"],
            "sample_idx": [0, 1],
            "lat": [lat_a, lat_b],
            "lon": [-121.3] * 2,
        }
    )
    csv = pd.DataFrame({"query_lat": [lat_a, lat_b], "query_lon": [-121.3, -121.3]})
    with pytest.raises(WalkRefused, match="2 CSV locations lie within"):
        match_frame(samples, csv)


def _keys(lats, lons):
    """quantize_coord keys from Python floats, as the scorer's own zip yields them."""
    return [quantize_coord(la, lo) for la, lo in zip(list(lats), list(lons), strict=True)]


@pytest.mark.parametrize("provider", ("gsv", "mapillary"))
def test_a_sample_the_loader_parses_across_the_boundary_scores_as_the_collector_scores_it(
    tmp_path, monkeypatch, capsys, provider
):
    """A REAL collector run whose one boundary sample comes back through
    load_city_csv_file one ULP across quantize_coord's half-way point. The
    collector's key join misses it, so it scores that sample uncovered; the
    recompute must too, so it equals a --force collection under the same
    definition. Substituting the CSV's coordinates into the samples would
    score it covered and break the equality."""
    data_dir = _setup(tmp_path, monkeypatch)
    graph_path = _freeze(data_dir, graph=_boundary_graph())
    _collect(data_dir, D1, monkeypatch, old_definition=True, provider=provider)
    before = _walk(data_dir, D1, provider=provider)
    csv_path = os.path.join(data_dir, before["csv_filename"])

    # The premise, measured on this run's own CSV: its text is exact (Python's
    # correctly rounded parse reproduces every regenerated sample), and the
    # loader's parse moves exactly one key -- the boundary sample's.
    samples = generate_samples(dsn.graph_to_edges(ox.load_graphml(graph_path)), 15)
    sample_keys = _keys(samples["lat"], samples["lon"])
    with gzip.open(csv_path, "rt") as fh:
        exact = pd.read_csv(fh, float_precision="round_trip")
    loaded = load_city_csv_file(csv_path)
    assert set(_keys(exact["query_lat"], exact["query_lon"])) == set(sample_keys)
    missed = set(sample_keys) - set(_keys(loaded["query_lat"], loaded["query_lon"]))
    assert len(missed) == 1
    (boundary,) = samples[[k in missed for k in sample_keys]].itertuples()
    assert boundary.edge_id == "1_2"
    assert float(repr(boundary.lat)) == boundary.lat
    # And the collector scored it uncovered: that edge is all of the gap.
    assert before["coverage_pct_by_length"] < 100.0

    assert _run(data_dir, "--execute") == 0
    out = capsys.readouterr().out
    assert "1 samples matched only within" in out and "scored uncovered" in out
    recomputed_row = _walk(data_dir, D1, provider=provider)
    recomputed_artifact = _artifact(data_dir, D1, provider=provider)
    assert recomputed_row["coverage_pct_by_length"] > before["coverage_pct_by_length"]  # NO_DATE

    _collect(data_dir, D1, monkeypatch, old_definition=False, force=True, provider=provider)
    collector_row = _walk(data_dir, D1, provider=provider)
    assert {c: recomputed_row[c] for c in EXPECTED_STAT_COLUMNS} == {
        c: collector_row[c] for c in EXPECTED_STAT_COLUMNS
    }
    assert recomputed_artifact == _artifact(data_dir, D1, provider=provider)
    edge = next(f for f in recomputed_artifact["features"] if f["properties"]["edge_id"] == "1_2")
    assert edge["properties"]["coverage_fraction"] == 0.0


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


def test_a_csv_location_no_sample_reaches_is_refused(tmp_path, monkeypatch, capsys):
    """The other direction: every regenerated sample matches, but the CSV holds
    a query location none of them reaches (it was walked on another frame). The
    scorer would silently ignore that row, so only the frame check can see it."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)

    def add_stray(raw):
        stray = raw.iloc[[0]].copy()
        stray["query_lat"] = "44.06"  # ~900 m north of the network's last node
        return pd.concat([raw, stray], ignore_index=True)

    _rewrite_csv(os.path.join(data_dir, before["csv_filename"]), add_stray)
    assert _run(data_dir, "--execute") == 1
    out = capsys.readouterr().out
    assert "1 of the CSV's" in out and "(sample key mismatch 1)" in out
    assert _walk(data_dir, D1) == before


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


def test_a_renumbered_network_is_refused_by_the_edge_ids(tmp_path, monkeypatch, capsys):
    """A refresh that renumbered the OSM nodes over identical geometry passes
    the count AND the coordinate checks; only the edge ids differ, and the walk
    diff keys on them, so the series is refused rather than rewritten."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)  # a walk that WOULD move
    before = _walk(data_dir, D1)
    _freeze(data_dir, node_offset=100)

    assert _run(data_dir, "--execute") == 1
    out = capsys.readouterr().out
    assert (
        "edge id mismatch: the frozen network's 2 edge ids are not the stored artifact's 2" in out
    )
    assert "(edge id mismatch 1)" in out
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
    manifest = os.path.join(data_dir, "streetwalks.json.gz")
    with open(manifest, "rb") as fh:
        manifest_bytes = fh.read()
    answers = iter([None, "pid 9: run-due"])
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: next(answers))
    assert _run(data_dir, "--execute") == 1
    out = capsys.readouterr().out
    assert "a run-due started" in out
    assert _walk(data_dir, D1) == before
    # The batch owns the manifest now (both writers share its fixed .tmp name).
    assert "manifest NOT regenerated: a run-due is running" in out
    with open(manifest, "rb") as fh:
        assert fh.read() == manifest_bytes


def test_a_rerun_heals_a_manifest_an_interrupted_pass_never_reached(tmp_path, monkeypatch):
    """A pass that committed its writes and died before the manifest leaves a
    re-run nothing to write; the manifest must be regenerated anyway."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    manifest = os.path.join(data_dir, "streetwalks.json.gz")
    with open(manifest, "rb") as fh:
        stale = fh.read()
    assert _run(data_dir, "--execute") == 0
    with open(manifest, "wb") as fh:  # as if the first pass had died before it
        fh.write(stale)
    assert _manifest_body(data_dir)["walks"][0]["coverage_pct_by_length"] < 100.0  # premise

    assert _run(data_dir, "--execute") == 0
    (entry,) = _manifest_body(data_dir)["walks"]
    assert entry["coverage_pct_by_length"] == _walk(data_dir, D1)["coverage_pct_by_length"]


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


def _real_change_pair(data_dir, monkeypatch):
    """D1 -> D2 under one definition with a REAL change (D2's southern imagery
    gone), so the diff row has counters, a pointer and a detail file. Returns
    the healed row and the detail file's bytes, decompressed."""
    _collect(data_dir, D1, monkeypatch, old_definition=False)
    _collect(data_dir, D2, monkeypatch, old_definition=False)

    def drop_south(raw):
        south = raw["query_lat"].astype(float) <= NO_DATE_NORTH_OF
        raw.loc[south, ["pano_lat", "pano_lon", "pano_id", "capture_date"]] = None
        raw.loc[south, "status"] = "ZERO_RESULTS"
        return raw

    _rewrite_csv(os.path.join(data_dir, _walk(data_dir, D2)["csv_filename"]), drop_south)
    assert _run(data_dir, "--execute") == 0
    row = _diff_row(data_dir, D2)
    assert row["detail_filename"] is not None and row["coverage_fraction_changed"] > 0  # premise
    with gzip.open(os.path.join(data_dir, row["detail_filename"]), "rt") as fh:
        return row, fh.read()


def _corrupt_diff(data_dir, column, value):
    conn = _conn(data_dir)
    with conn:
        conn.execute(
            f"UPDATE street_walk_diffs SET {column} = ? WHERE to_walk_id = ?",
            (value, _walk(data_dir, D2)["walk_id"]),
        )
    conn.close()


def _without_id(row):
    """A diff row minus what every re-diff restamps (the orchestrator deletes
    and re-inserts)."""
    return {k: v for k, v in row.items() if k not in ("diff_id", "computed_at")}


@pytest.mark.parametrize(
    "column, corrupt",
    [
        # Another real walk (a different series), so only the comparison can see it.
        ("from_walk_id", lambda d: _walk(d, D1, provider="mapillary")["walk_id"]),
        ("coverage_fraction_changed", lambda d: _diff_row(d, D2)["coverage_fraction_changed"] + 3),
        # The right file stays on disk with the right rows; only the pointer is gone.
        ("detail_filename", lambda d: None),
    ],
)
def test_each_stale_diff_field_alone_is_repaired(tmp_path, monkeypatch, capsys, column, corrupt):
    """Each staleness criterion on its own: one field of an otherwise correct
    diff row is wrong, and the pass re-diffs it back to the correct row."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=False, provider="mapillary")
    good, detail = _real_change_pair(data_dir, monkeypatch)
    _corrupt_diff(data_dir, column, corrupt(data_dir))
    assert _diff_row(data_dir, D2)[column] != good[column]  # premise
    capsys.readouterr()

    assert _run(data_dir, "--execute") == 0
    assert f"diff: {column} " in capsys.readouterr().out
    assert _without_id(_diff_row(data_dir, D2)) == _without_id(good)
    with gzip.open(os.path.join(data_dir, good["detail_filename"]), "rt") as fh:
        assert fh.read() == detail


@pytest.mark.parametrize("row_state", ("null_pointer", "missing_row"))
def test_an_orphan_detail_file_removed_here_is_listed_for_the_web_server(
    tmp_path, monkeypatch, capsys, row_state
):
    """A no-changes pair with a detail file at the deterministic name that no
    row points at (an orphan from before #265): the re-diff removes it, and it
    is as published as any other, so it must be listed for removal there."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=False)
    _collect(data_dir, D2, monkeypatch, old_definition=False)
    assert _diff_row(data_dir, D2)["detail_filename"] is None  # premise: no changes
    name = generate_streetwalk_diff_filename(CITY_ID, D1, D2, provider="gsv", network_type="drive")
    path = os.path.join(data_dir, name)
    with gzip.open(path, "wt") as fh:
        fh.write("edge_id,change_type\n1_2,coverage_changed\n")
    if row_state == "missing_row":
        conn = _conn(data_dir)
        with conn:
            conn.execute("DELETE FROM street_walk_diffs")
        conn.close()

    assert _run(data_dir, "--execute") == 0
    out = capsys.readouterr().out
    assert not os.path.exists(path)
    assert name in _removed_listing(out)
    assert _diff_row(data_dir, D2)["detail_filename"] is None


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
