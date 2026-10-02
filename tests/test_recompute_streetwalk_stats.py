"""Tests for scripts/recompute_streetwalk_stats.py (issue #262): re-derive every
road walk's stored stats from its snapshot CSV and its frozen GraphML under the
current coverage definition, refuse a series rather than score it against the
wrong frame, and never reach Overpass.

Fixtures are built the way production built them. Each walk is collected by
the REAL `collect.run_collect`, loading a real GraphML from the frozen-network
cache (a cache hit, so no network), with only the GSV request primitive served
from memory. A stale, pre-#257 walk is produced by the code that produced it:
the collector run with `street_coverage.PRESENT_STATUSES` narrowed to `OK`,
which is exactly what #257 widened (`ok = m["status"] == "OK"` before it).
"""

import gzip
import hashlib
import json
import os
from datetime import date

import networkx as nx
import osmnx as ox
import pandas as pd
import pytest

import scripts.recompute_streetwalk_stats as recompute_module
from scripts.recompute_streetwalk_stats import STAT_COLUMNS, main
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import download_gsv as dg
from streetscape_metadata_tracker.naming import network_cache_path
from streetscape_metadata_tracker.walk_diff import compute_walk_diff, load_streetwalk_coverage
from streetscape_street_analyzer import collect, street_coverage
from streetscape_street_analyzer import download_street_network as dsn

CITY_QUERY = "Bend, Oregon, United States"
CITY_ID = "bend--oregon--united-states"
D1, D2 = "2026-07-08", "2026-09-01"

# Samples north of this latitude come back from "GSV" with no capture date,
# i.e. status NO_DATE: part of the long edge and all of the short one.
NO_DATE_NORTH_OF = 44.051


@pytest.fixture(autouse=True)
def _no_batch_in_flight(monkeypatch):
    """The in-flight detector reads `ps`; a real run-due must not decide these tests."""
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: None)


def _graph(extra_edge=False):
    g = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {1: (-121.30, 44.05), 2: (-121.30, 44.052), 3: (-121.30, 44.0525)}
    if extra_edge:
        nodes[4] = (-121.299, 44.0525)
    for n, (x, y) in nodes.items():
        g.add_node(n, x=x, y=y)
    g.add_edge(1, 2, 0, osmid=10, highway="residential", length=222.0)
    g.add_edge(2, 1, 0, osmid=10, highway="residential", length=222.0)
    g.add_edge(2, 3, 0, osmid=11, highway="service", length=55.0)
    if extra_edge:
        g.add_edge(3, 4, 0, osmid=12, highway="residential", length=80.0)
    return g


def _freeze(data_dir, **kw):
    path = network_cache_path(CITY_ID, data_dir, "drive")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ox.save_graphml(_graph(**kw), path)
    return path


def _setup(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
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
    _freeze(data_dir)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        return {
            "status": "OK",
            "location": {"lat": lat, "lng": lon},
            "pano_id": f"pano_{lat:.6f}_{lon:.6f}",
            "copyright": "© Google",
            # No date -> the downloader records NO_DATE.
            "date": None if lat > NO_DATE_NORTH_OF else "2022-06",
        }

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)
    return data_dir


def _collect(data_dir, run_date, monkeypatch, *, old_definition, force=False):
    """Run the real collector; `old_definition` reproduces the pre-#257 one."""
    with monkeypatch.context() as m:
        if old_definition:
            m.setattr(street_coverage, "PRESENT_STATUSES", ("OK",))
        argv = [
            CITY_QUERY,
            "--data-dir",
            data_dir,
            "--run-date",
            run_date,
            "--spacing",
            "15",
            "--max-requests-per-minute",
            "0",
        ] + (["--force"] if force else [])
        assert collect.run_collect(collect.build_parser().parse_args(argv)) == 0


def _conn(data_dir):
    return db.connect(db.get_default_db_path(data_dir))


def _walk(data_dir, run_date):
    conn = _conn(data_dir)
    try:
        return dict(
            conn.execute(
                "SELECT * FROM street_walks WHERE city_id = ? AND run_date = ?",
                (CITY_ID, run_date),
            ).fetchone()
        )
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


def _artifact(data_dir, run_date):
    return load_streetwalk_coverage(
        os.path.join(data_dir, _walk(data_dir, run_date)["coverage_filename"])
    )


def _tree_digest(root):
    """Every file under root, by relative path, with a content hash."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


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

    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 0
    recomputed_row = _walk(data_dir, D1)
    recomputed_artifact = _artifact(data_dir, D1)
    assert recomputed_row["coverage_pct_by_length"] > before["coverage_pct_by_length"]
    assert recomputed_row["length_km_covered"] > before["length_km_covered"]
    with open(csv_path, "rb") as fh:
        assert fh.read() == csv_bytes, "the snapshot CSV must never be rewritten"

    # What the collector writes today from the same responses on the same date.
    _collect(data_dir, D1, monkeypatch, old_definition=False, force=True)
    collector_row = _walk(data_dir, D1)
    assert {c: recomputed_row[c] for c in STAT_COLUMNS} == {
        c: collector_row[c] for c in STAT_COLUMNS
    }
    assert recomputed_artifact == _artifact(data_dir, D1)


def test_a_second_pass_changes_nothing(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 0
    row = _walk(data_dir, D1)
    digest = _tree_digest(data_dir)
    # db.connect touches the file itself (journal mode), so the catalog is
    # compared by content and every other file by bytes.
    digest.pop("streetscape_tracker.db")
    capsys.readouterr()
    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 0
    assert "1 unchanged" in capsys.readouterr().out
    after = _tree_digest(data_dir)
    after.pop("streetscape_tracker.db")
    assert after == digest
    assert _walk(data_dir, D1) == row


def test_without_regenerate_artifacts_only_the_catalog_moves(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    old_cov = _walk(data_dir, D1)["coverage_pct_by_length"]
    artifact_before = _artifact(data_dir, D1)

    assert _run(data_dir, "--execute") == 0
    assert _walk(data_dir, D1)["coverage_pct_by_length"] > old_cov
    assert _artifact(data_dir, D1) == artifact_before
    assert "NOT rewritten" in capsys.readouterr().out


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_sample_frame_mismatch_refuses_the_whole_series(tmp_path, monkeypatch, capsys):
    """One walk whose CSV no longer matches the regenerated frame skips the
    SERIES: the other walk, which would move, is left on the old definition too,
    so the history never mixes two definitions."""
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=True)
    # Drop one sample row from D2's CSV: the count pre-check still passes
    # (sample_points counts the regenerated frame), so only the key set can see it.
    d2_csv = os.path.join(data_dir, _walk(data_dir, D2)["csv_filename"])
    with gzip.open(d2_csv, "rt") as fh:
        raw = pd.read_csv(fh, dtype=str)
    with gzip.open(d2_csv, "wt", newline="") as fh:
        raw.iloc[1:].to_csv(fh, index=False)
    rows_before = (_walk(data_dir, D1), _walk(data_dir, D2))
    digest = _tree_digest(data_dir)
    digest.pop("streetscape_tracker.db")

    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and f"{CITY_ID} [gsv/drive]: 2 walk(s) skipped" in out
    assert "sample frame mismatch" in out
    assert (_walk(data_dir, D1), _walk(data_dir, D2)) == rows_before
    after = _tree_digest(data_dir)
    after.pop("streetscape_tracker.db")
    assert after == digest


def test_a_refreshed_network_is_refused_by_the_sample_count(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    _freeze(data_dir, extra_edge=True)  # a --refresh overwrote the network in place

    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 1
    assert "the network was refreshed since this walk" in capsys.readouterr().out
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

    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 1
    out = capsys.readouterr().out
    assert "refusing rather than fetching it from Overpass" in out
    assert calls == []
    assert _walk(data_dir, D1) == before


# ── the dry run ──────────────────────────────────────────────────────────────


def test_the_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=False)
    digest = _tree_digest(data_dir)

    assert _run(data_dir, "--regenerate-artifacts") == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "would change" in out
    # Byte-identical, catalog included, and no -wal/-shm left behind.
    assert _tree_digest(data_dir) == digest


# ── walk diffs ───────────────────────────────────────────────────────────────


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
    assert _run(data_dir, "--execute") == 0
    assert "diff left stale" in capsys.readouterr().out
    assert (
        _diff_row(data_dir, D2)["coverage_pct_by_length_delta"]
        == phantom["coverage_pct_by_length_delta"]
    )

    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 0
    out = capsys.readouterr().out
    healed = _diff_row(data_dir, D2)
    assert healed["coverage_pct_by_length_delta"] == 0.0
    assert healed["coverage_fraction_changed"] == 0
    assert healed["detail_filename"] is None
    assert not os.path.exists(detail_path)
    assert phantom["detail_filename"] in out  # listed for removal from the web server

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


def test_a_walk_with_no_diff_row_is_not_given_one(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    _collect(data_dir, D2, monkeypatch, old_definition=True)
    conn = _conn(data_dir)
    conn.execute("DELETE FROM street_walk_diffs")
    conn.commit()
    conn.close()

    assert _run(data_dir, "--regenerate-artifacts", "--execute") == 0
    assert _diff_row(data_dir, D2) is None


def test_unknown_provider_is_an_argument_error(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run(data_dir, "--provider", "bing")
    assert exc.value.code == 2


def test_execute_is_refused_while_run_due_is_in_flight(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    _collect(data_dir, D1, monkeypatch, old_definition=True)
    before = _walk(data_dir, D1)
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: "pid 1: run-due")
    assert _run(data_dir, "--execute") == 64
    assert _walk(data_dir, D1) == before
    assert date.fromisoformat(before["run_date"])  # untouched row is still the D1 walk
