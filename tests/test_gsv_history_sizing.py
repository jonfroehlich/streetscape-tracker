"""
Pins for the #442 sizing scripts and the committed record they produced.

``scripts/size_gsv_history_walks.py`` and ``scripts/gsv_history_walk_pair_changes.py``
read the catalog and walk CSVs only. Both are run here over a synthetic catalog
whose two walks hold one example of every change class, and the committed
``docs/experiments/gsv-history-sizing_metrics.json`` is recomputed from the
committed per-city tables so a share quoted in the writeup cannot drift from
the numbers it summarizes.
"""

import gzip
import json
import os
from datetime import date

import pandas as pd
import pytest

from scripts.gsv_history_walk_pair_changes import measure as measure_pair_changes
from scripts.size_gsv_history_walks import catalog_totals
from scripts.size_gsv_history_walks import measure as measure_size
from streetscape_metadata_tracker import db

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(PROJECT_ROOT, "docs", "experiments")
METRICS = os.path.join(DOCS, "gsv-history-sizing_metrics.json")

GOOGLE = "© Google"
WALK_COLS = [
    "query_lat",
    "query_lon",
    "query_timestamp",
    "pano_lat",
    "pano_lon",
    "pano_id",
    "capture_date",
    "copyright_info",
    "status",
]


def _row(i, pano, cdate, status="OK", copyright=GOOGLE):
    lat, lon = 44.0 + i * 1e-4, -121.0 + i * 1e-4
    return {
        "query_lat": lat,
        "query_lon": lon,
        "query_timestamp": "2026-05-01T00:00:00+00:00",
        "pano_lat": lat,
        "pano_lon": lon,
        "pano_id": pano,
        "capture_date": cdate,
        "copyright_info": copyright if pano else None,
        "status": status,
    }


# One sample per change class, in the order the writeup names them.
PREV = [
    _row(0, "A1", "2023-01-01"),  # newer-date change in curr
    _row(1, "B1", "2023-01-01"),  # same-date re-issue in curr
    _row(2, "C1", "2024-01-01"),  # older-date change in curr
    _row(3, None, None, status="ZERO_RESULTS"),  # gained in curr
    _row(4, "E1", "2023-01-01"),  # lost in curr
    _row(5, "F1", "2023-01-01"),  # unchanged
    _row(6, "G1", "2023-01-01", copyright="Someone"),  # third-party both walks: never Google
]
CURR = [
    _row(0, "A2", "2025-01-01"),
    _row(1, "B2", "2023-01-01"),
    _row(2, "C2", "2022-01-01"),
    _row(3, "D1", "2025-01-01"),
    _row(4, None, None, status="ZERO_RESULTS"),
    _row(5, "F1", "2023-01-01"),
    _row(6, "G2", "2025-01-01", copyright="Someone"),
]


def _write_walk(data_dir, name, rows):
    with gzip.open(os.path.join(data_dir, name), "wt", encoding="utf-8") as f:
        pd.DataFrame(rows, columns=WALK_COLS).to_csv(f, index=False)


def _city(conn, name):
    return db.register_city(
        conn,
        city_name=name,
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
    )


def _walk(conn, cid, day, name, **cols):
    db.register_street_walk(
        conn,
        city_id=cid,
        run_date=date(2026, 5, day),
        csv_filename=name,
        spacing_m=15.0,
        **cols,
    )


@pytest.fixture
def repo(tmp_path, data_dir, conn):
    """A repo dir whose data/ holds the test catalog and the walk files."""
    cid = _city(conn, "Bend")
    _write_walk(data_dir, "bend-prev.csv.gz", PREV)
    _write_walk(data_dir, "bend-curr.csv.gz", CURR)
    _walk(conn, cid, 1, "bend-prev.csv.gz", edges_total=3, length_km=1.0, length_km_covered=0.5)
    _walk(conn, cid, 2, "bend-curr.csv.gz", edges_total=3, length_km=1.5, length_km_covered=1.0)
    # A second city walked once, sharing pano F1 with Bend (an overlapping walk),
    # so the global distinct count is smaller than the per-city sum.
    other = _city(conn, "Sisters")
    _write_walk(
        data_dir, "sisters.csv.gz", [_row(5, "F1", "2023-01-01"), _row(7, "H1", "2023-01-01")]
    )
    _walk(conn, other, 2, "sisters.csv.gz", edges_total=2, length_km=None, length_km_covered=None)
    conn.commit()
    root = tmp_path / "repo"
    root.mkdir()
    os.symlink(data_dir, root / "data")
    return root


def test_pair_changes_classifies_one_sample_per_class(repo, tmp_path):
    s = measure_pair_changes(repo, tmp_path / "pc.json")
    assert s["pairs"] == 1 and s["pairs_match_ge_95pct"] == 1
    # google_both: A, B, C, F — never G (third party) nor the gained/lost rows.
    assert s["google_both"] == 4
    assert (s["changed_newer_date"], s["changed_same_date"], s["changed_older_date"]) == (1, 1, 1)
    assert s["changed"] == 3
    assert (s["gained"], s["lost"]) == (1, 1)
    # New captures are the newer-date change (A2) and the gained dated pano (D1).
    assert s["new_capture_distinct_panos"] == 2
    assert s["new_capture_panos_per_day_per_1M_google_samples"] == pytest.approx(2 / (1 * 4) * 1e6)
    csv = pd.read_csv(tmp_path / "pair_changes.csv")
    assert list(csv["city_id"]) == ["bend--oregon--united-states"]
    assert csv.loc[0, "match_frac"] == 1.0


def test_size_counts_distinct_panos_per_city_and_globally(repo, tmp_path):
    s = measure_size(repo, tmp_path / "s.json")
    assert s["cities"] == 2 and s["missing_files"] == 0
    assert s["samples"] == 9
    # Bend curr: A2 B2 C2 D1 F1 G2 present (6; G2 is third-party), Sisters: F1 H1.
    assert s["present_samples"] == 8 and s["google_samples"] == 7
    assert s["distinct_google_sum_per_city"] == 7
    assert s["distinct_google_global"] == 6  # F1 shared between the two cities
    assert s["distinct_all_global"] == 7
    # The aligned pair (identical coordinates in identical order) is counted.
    assert s["pairs_total"] == 1 and s["pairs_aligned"] == 1
    assert (s["pairs_changed"], s["pairs_gained"], s["pairs_lost"]) == (3, 1, 1)
    per_city = pd.read_csv(tmp_path / "per_city.csv").set_index("city_id")
    assert per_city.loc["bend--oregon--united-states", "distinct_google"] == 5
    assert s["walk_totals"]["cities"] == 2


def test_catalog_totals_read_each_city_latest_walk_and_count_nulls(repo, conn):
    t = catalog_totals(conn)
    assert t["cities"] == 2
    # Bend's LATEST walk (day 2), never its earlier one, plus Sisters.
    assert t["edges_total"] == 5
    assert t["length_km"] == pytest.approx(1.5)
    assert t["length_km_covered"] == pytest.approx(1.0)
    assert t["walks_missing_length_km"] == 1
    assert t["walks_missing_edges_total"] == 0


def test_size_totals_only_skips_the_csv_scan(repo, tmp_path, monkeypatch):
    import scripts.size_gsv_history_walks as mod

    monkeypatch.setattr(
        mod, "load", lambda *a: pytest.fail("CSV scanned under --catalog-totals-only")
    )
    s = measure_size(repo, tmp_path / "t.json", totals_only=True)
    assert set(s) == {"generated_by", "generated_at", "walk_totals"}
    assert json.loads((tmp_path / "t.json").read_text())["walk_totals"]["edges_total"] == 5


# ── The committed record ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def metrics():
    with open(METRICS, encoding="utf-8") as f:
        return json.load(f)


def test_committed_backfill_block_matches_its_per_city_table(metrics):
    b = metrics["backfill"]
    pc = pd.read_csv(os.path.join(DOCS, "gsv-history-sizing_per_city.csv"))
    assert len(pc) == b["cities"] == 1274
    for col, key in (
        ("samples", "samples"),
        ("google", "google_samples"),
        ("present", "present_samples"),
    ):
        assert int(pc[col].sum()) == b[key]
    assert int(pc["distinct_google"].sum()) == b["distinct_google_sum_per_city"]
    for q, v in b["distinct_google_per_city_pctiles"].items():
        assert pc["distinct_google"].quantile(int(q) / 100) == pytest.approx(v)
    # The writeup's dedup shares: within-city, then global, against Google samples.
    assert 1 - b["distinct_google_sum_per_city"] / b["google_samples"] == pytest.approx(
        0.0557, abs=5e-4
    )
    assert 1 - b["distinct_google_global"] / b["google_samples"] == pytest.approx(0.0822, abs=5e-4)
    assert b["google_samples"] / b["samples"] == pytest.approx(0.821, abs=5e-4)
    top_decile = pc["distinct_google"].sort_values(ascending=False).head(len(pc) // 10)
    assert top_decile.sum() / b["distinct_google_sum_per_city"] == pytest.approx(0.728, abs=5e-4)
    # The walk totals: one query per edge is the issue's pass 1.
    assert b["cities"] == metrics["walk_totals"]["cities"]
    assert metrics["walk_totals"]["edges_total"] == 8945469
    assert metrics["walk_totals"]["edges_total"] / b["distinct_google_global"] == pytest.approx(
        1 / 6.6, abs=2e-3
    )


def test_committed_change_rate_block_matches_its_pair_table(metrics):
    c = metrics["change_rate"]
    pcs = pd.read_csv(os.path.join(DOCS, "gsv-history-sizing_pair_changes.csv"))
    assert len(pcs) == c["pairs"] == 121
    good = pcs[pcs["match_frac"] >= 0.95]
    assert len(good) == c["pairs_match_ge_95pct"] == 121
    for key in (
        "google_both",
        "changed",
        "changed_newer_date",
        "changed_same_date",
        "changed_older_date",
        "gained",
        "lost",
        "new_capture_distinct_panos",
    ):
        assert int(good[key].sum()) == c[key]
    rate = (
        good["new_capture_distinct_panos"].sum() / (good["days"] * good["google_both"]).sum() * 1e6
    )
    assert rate == pytest.approx(c["new_capture_panos_per_day_per_1M_google_samples"])
    for q, v in c["days_pctiles"].items():
        assert good["days"].quantile(int(q) / 100) == pytest.approx(v)
    # The writeup's split of changed panos: newer 66%, same date 29%, older 5%.
    assert c["changed_newer_date"] / c["changed"] == pytest.approx(0.657, abs=5e-4)
    assert c["changed_same_date"] / c["changed"] == pytest.approx(0.294, abs=5e-4)
    assert c["changed_older_date"] / c["changed"] == pytest.approx(0.049, abs=5e-4)
    assert c["changed"] / c["google_both"] == pytest.approx(0.1016, abs=5e-4)
    # Concentration: the five largest contributors hold 70.8 % of new captures, and
    # 47 pairs saw none.
    top5 = good.sort_values("new_capture_distinct_panos", ascending=False).head(5)
    assert top5["new_capture_distinct_panos"].sum() / c[
        "new_capture_distinct_panos"
    ] == pytest.approx(0.708, abs=5e-4)
    assert int((good["changed_newer_date"] + good["gained"] == 0).sum()) == 47


def test_committed_generated_by_names_runnable_scripts(metrics):
    for cmd in metrics["generated_by"]:
        script = cmd.split()[1]
        assert os.path.exists(os.path.join(PROJECT_ROOT, script)), cmd
    assert "walk_totals" in metrics
