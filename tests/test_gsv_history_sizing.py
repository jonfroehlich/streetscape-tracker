"""
Pins for the #442 sizing scripts and the committed record they produced.

``scripts/size_gsv_history_walks.py`` and ``scripts/gsv_history_walk_pair_changes.py``
read the catalog and walk CSVs only; ``scripts/gsv_history_sizing_analyze.py``
assembles their outputs into the committed record. All three run here over a
synthetic catalog built so that each selection rule the scripts rely on has a
row that would move a number if the rule were dropped: a same-day Mapillary walk
and a same-day ``all_public`` walk beside the GSV drive pair (the provider and
network filters), a current walk whose rows are in reverse order (the coordinate
join), two samples a metre apart (the 9-decimal key), a ``NO_DATE`` pano (the
PRESENT vocabulary), an undated gain (the dated-gain guard) and a pair whose
walks share half their samples (the 0.95 match filter). The committed
``docs/experiments/gsv-history-sizing_metrics.json`` is then recomputed from
the committed tables so a share quoted in the writeup cannot drift from the
numbers it summarizes.
"""

import filecmp
import gzip
import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts import gsv_history_sizing_analyze as analyze
from scripts import gsv_history_walk_pair_changes as pair_mod
from scripts import size_gsv_history_walks as size_mod
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

BEND = "bend--oregon--united-states"
REDMOND = "redmond--oregon--united-states"
SISTERS = "sisters--oregon--united-states"


def _row(i, pano, cdate, status="OK", copyright=GOOGLE, lat_offset=0.0):
    lat, lon = 44.0 + i * 1e-4 + lat_offset, -121.0 + i * 1e-4
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


# Bend: one sample per change class, plus the rows that make each filter load-bearing.
BEND_PREV = [
    _row(0, "A1", "2023-01-01"),  # newer-date change in curr
    _row(1, "B1", "2023-01-01"),  # same-date re-issue in curr
    _row(2, "C1", "2024-01-01"),  # older-date change in curr
    _row(3, None, None, status="ZERO_RESULTS"),  # dated gain in curr
    _row(4, "E1", "2023-01-01"),  # lost in curr
    _row(5, "F1", "2023-01-01"),  # unchanged
    _row(6, "G1", "2023-01-01", copyright="Someone"),  # third-party both walks: never Google
    _row(7, "I1", None, status="NO_DATE"),  # undated but PRESENT, unchanged
    _row(8, None, None, status="ZERO_RESULTS"),  # UNDATED gain in curr: not a new capture
    # 5e-7 deg from row 0: one key at 9 decimals, the same key as row 0 at 5.
    _row(0, "K1", "2023-01-01", lat_offset=5e-7),  # newer-date change in curr
]
BEND_CURR_IN_PREV_ORDER = [
    _row(0, "A2", "2025-01-01"),
    _row(1, "B2", "2023-01-01"),
    _row(2, "C2", "2022-01-01"),
    _row(3, "D1", "2025-01-01"),
    _row(4, None, None, status="ZERO_RESULTS"),
    _row(5, "F1", "2023-01-01"),
    _row(6, "G2", "2025-01-01", copyright="Someone"),
    _row(7, "I1", None, status="NO_DATE"),
    _row(8, "J1", None, status="NO_DATE"),
    _row(0, "K2", "2025-01-01", lat_offset=5e-7),
]
# The current walk is written in REVERSE order, so positional pairing pairs
# every sample with the wrong one and only a coordinate join classifies it.
BEND_CURR = list(reversed(BEND_CURR_IN_PREV_ORDER))
# Same-day distractors on other channels: Google panos that would change every
# count if the provider or network filter were dropped.
BEND_MAPILLARY = [_row(20 + i, f"X{i}", "2025-01-01") for i in range(5)]
BEND_ALL_PUBLIC = [_row(30 + i, f"Y{i}", "2025-01-01") for i in range(4)]

# Redmond: identical sample list in identical order (the size script's aligned pair),
# sharing pano F1 with Bend so the global distinct count is below the per-city sum.
REDMOND_PREV = [
    _row(0, "L1", "2023-01-01"),
    _row(1, "M1", "2023-01-01"),
    _row(5, "F1", "2023-01-01"),
]
REDMOND_CURR = [
    _row(0, "L1", "2023-01-01"),
    _row(1, "M2", "2025-01-01"),
    _row(5, "F1", "2023-01-01"),
]

# Sisters: the current walk holds half the previous one's samples, so the pair
# matches 50 % and is excluded from the change-rate totals.
SISTERS_PREV = [_row(i, f"{c}1", "2023-01-01") for i, c in enumerate("NOPQ")]
SISTERS_CURR = [_row(0, "N2", "2025-01-01"), _row(1, "O1", "2023-01-01")]


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
    bend = _city(conn, "Bend")
    _write_walk(data_dir, "bend-prev.csv.gz", BEND_PREV)
    _write_walk(data_dir, "bend-curr.csv.gz", BEND_CURR)
    _write_walk(data_dir, "bend-mly.csv.gz", BEND_MAPILLARY)
    _write_walk(data_dir, "bend-allpub.csv.gz", BEND_ALL_PUBLIC)
    _walk(conn, bend, 1, "bend-prev.csv.gz", edges_total=2, length_km=1.0, length_km_covered=0.5)
    _walk(conn, bend, 2, "bend-curr.csv.gz", edges_total=3, length_km=1.5, length_km_covered=1.0)
    _walk(conn, bend, 2, "bend-mly.csv.gz", provider="mapillary", edges_total=50, length_km=9.0)
    _walk(conn, bend, 2, "bend-allpub.csv.gz", network_type="all_public", edges_total=40)
    redmond = _city(conn, "Redmond")
    _write_walk(data_dir, "redmond-prev.csv.gz", REDMOND_PREV)
    _write_walk(data_dir, "redmond-curr.csv.gz", REDMOND_CURR)
    _walk(conn, redmond, 1, "redmond-prev.csv.gz", edges_total=2, length_km=1.0)
    _walk(
        conn, redmond, 2, "redmond-curr.csv.gz", edges_total=4, length_km=2.0, length_km_covered=1.0
    )
    # A LATER Mapillary walk: a latest-walk lookup that forgot the channel picks this
    # date and finds no GSV walk on it, dropping Redmond from every total.
    _write_walk(data_dir, "redmond-mly.csv.gz", [_row(40, "Z1", "2025-01-01")])
    _walk(conn, redmond, 3, "redmond-mly.csv.gz", provider="mapillary", edges_total=7)
    sisters = _city(conn, "Sisters")
    _write_walk(data_dir, "sisters-prev.csv.gz", SISTERS_PREV)
    _write_walk(data_dir, "sisters-curr.csv.gz", SISTERS_CURR)
    _walk(conn, sisters, 1, "sisters-prev.csv.gz", edges_total=5, length_km=3.0)
    _walk(conn, sisters, 2, "sisters-curr.csv.gz", edges_total=1, length_km=None)
    conn.commit()
    root = tmp_path / "repo"
    root.mkdir()
    os.symlink(data_dir, root / "data")
    return root


# ── The pair script ──────────────────────────────────────────────────────────


def test_pair_changes_classifies_one_sample_per_class(repo, tmp_path):
    s = measure_pair_changes(repo, tmp_path / "pc.json")
    assert s["pairs"] == 3 and s["missing_files"] == 0
    # Sisters matches 2 of 4 samples and is excluded from every total below.
    assert s["pairs_match_ge_95pct"] == 2
    assert s["match_frac_pctiles"] == {"0": 0.5, "10": 0.6, "50": 1.0}
    # google_both over Bend + Redmond: A, B, C, F, I (NO_DATE), K + L, M, F — never G
    # (third party) nor the gained/lost rows.
    assert s["google_both"] == 9
    assert (s["changed_newer_date"], s["changed_same_date"], s["changed_older_date"]) == (3, 1, 1)
    assert s["changed"] == 5
    # Bend gains D1 (dated) and J1 (undated), loses E1.
    assert (s["gained"], s["lost"]) == (2, 1)
    # New captures: the newer-date changes (A2, K2, M2) and the DATED gain D1; J1 is not one.
    assert s["new_capture_distinct_panos"] == 4
    assert s["new_capture_panos_per_day_per_1M_google_samples"] == pytest.approx(4 / 9 * 1e6)
    # No row merged under the collector's key: rows 0 and K sit 5e-7 deg apart.
    assert s["key_merged_rows"] == 0
    assert s["days_pctiles"] == {"10": 1.0, "50": 1.0, "90": 1.0}
    csv = pd.read_csv(tmp_path / "pair_changes.csv").set_index("city_id")
    assert list(csv.index) == [BEND, REDMOND, SISTERS]
    bend = csv.loc[BEND]
    assert bend["samples_curr"] == bend["samples_prev"] == bend["matched"] == 10
    assert bend["match_frac"] == 1.0
    assert (bend["changed_newer_date"], bend["changed_same_date"], bend["changed_older_date"]) == (
        2,
        1,
        1,
    )
    assert (bend["key_merged_curr"], bend["key_merged_prev"]) == (0, 0)
    assert csv.loc[SISTERS, "match_frac"] == 0.5


def test_pair_changes_as_of_a_date_before_any_second_walk_records_no_pairs(repo, tmp_path):
    s = measure_pair_changes(repo, tmp_path / "pc.json", as_of="2026-05-01")
    assert s["pairs"] == 0 and s["pairs_match_ge_95pct"] == 0
    assert s["as_of"] == "2026-05-01"
    assert s["generated_by"].endswith("--as-of 2026-05-01")
    assert s["changed"] == 0 and s["google_both"] == 0
    assert s["days_pctiles"] == {} and s["new_frac_pctiles"] == {}
    assert s["new_capture_panos_per_day_per_1M_google_samples"] is None
    assert list(pd.read_csv(tmp_path / "pair_changes.csv").columns)[:3] == [
        "city_id",
        "days",
        "samples_curr",
    ]


def test_pair_changes_counts_a_missing_walk_file_and_skips_its_city(repo, conn, tmp_path):
    _walk(conn, BEND, 3, "bend-never-written.csv.gz", edges_total=3)
    conn.commit()
    s = measure_pair_changes(repo, tmp_path / "pc.json")
    assert s["missing_files"] == 1
    assert s["pairs"] == 2
    assert list(pd.read_csv(tmp_path / "pair_changes.csv")["city_id"]) == [REDMOND, SISTERS]


def test_pair_changes_reports_a_row_the_collector_key_merges(repo, conn, tmp_path):
    # The collector never writes two rows with one 9-decimal key; a file that holds
    # one is reported, not silently paired by whichever row came first.
    _write_walk(
        repo / "data", "bend-dup.csv.gz", BEND_CURR_IN_PREV_ORDER + [BEND_CURR_IN_PREV_ORDER[5]]
    )
    _walk(conn, BEND, 3, "bend-dup.csv.gz", edges_total=3)
    conn.commit()
    s = measure_pair_changes(repo, tmp_path / "pc.json")
    assert s["key_merged_rows"] == 1
    bend = pd.read_csv(tmp_path / "pair_changes.csv").set_index("city_id").loc[BEND]
    assert (bend["key_merged_curr"], bend["key_merged_prev"]) == (1, 0)
    assert bend["samples_curr"] == bend["matched"] == 10 and bend["changed"] == 0


# ── The size script ──────────────────────────────────────────────────────────


def test_size_counts_distinct_panos_per_city_and_globally(repo, tmp_path):
    s = measure_size(repo, tmp_path / "s.json")
    assert s["cities"] == 3 and s["missing_files"] == 0
    assert s["samples"] == 15
    # Bend curr: A2 B2 C2 D1 F1 G2 I1 J1 K2 present (9; G2 third-party, I1 and J1
    # NO_DATE), Redmond: L1 M2 F1, Sisters: N2 O1.
    assert s["present_samples"] == 14 and s["google_samples"] == 13
    assert s["distinct_google_sum_per_city"] == 13
    assert s["distinct_google_global"] == 12  # F1 shared between Bend and Redmond
    assert s["distinct_all_global"] == 13
    assert s["distinct_google_per_city_pctiles"]["50"] == 3.0
    # Three pairs; only Redmond's walks hold identical coordinates in identical order.
    assert s["pairs_total"] == 3 and s["pairs_aligned"] == 1
    assert (s["pairs_changed"], s["pairs_gained"], s["pairs_lost"]) == (1, 0, 0)
    assert s["pairs_changed_distinct_new_panos"] == 1
    per_city = pd.read_csv(tmp_path / "per_city.csv").set_index("city_id")
    assert per_city.loc[BEND, "distinct_google"] == 8
    assert per_city.loc[BEND, "present"] == 9
    pairs = pd.read_csv(tmp_path / "pairs.csv").set_index("city_id")
    assert pairs["aligned"].to_dict() == {BEND: False, REDMOND: True, SISTERS: False}
    assert s["walk_totals"]["cities"] == 3
    assert (
        s["generated_by"]
        == f"python scripts/size_gsv_history_walks.py {repo} {tmp_path / 's.json'}"
    )


def test_size_as_of_reads_each_city_walks_up_to_that_date_only(repo, tmp_path):
    s = measure_size(repo, tmp_path / "s.json", as_of="2026-05-01")
    assert s["as_of"] == "2026-05-01"
    assert s["cities"] == 3 and s["pairs_total"] == 0
    # The day-1 walks: Bend prev (A1 B1 C1 E1 F1 I1 K1 Google), Redmond prev, Sisters prev.
    assert s["google_samples"] == 7 + 3 + 4
    assert s["walk_totals"]["edges_total"] == 2 + 2 + 5


def test_size_counts_a_missing_latest_walk_file(repo, conn, tmp_path):
    _walk(conn, BEND, 3, "bend-never-written.csv.gz", edges_total=3)
    conn.commit()
    s = measure_size(repo, tmp_path / "s.json")
    assert s["missing_files"] == 1 and s["cities"] == 2


def test_catalog_totals_read_each_city_latest_walk_and_count_nulls(repo, conn):
    t = catalog_totals(conn)
    assert t["cities"] == 3
    # Each city's LATEST gsv drive walk (day 2), never its earlier one, never
    # Bend's same-day Mapillary (50 edges) or all_public (40 edges) walks, and
    # never Redmond's later Mapillary walk (7 edges, no GSV walk that day).
    assert t["edges_total"] == 3 + 4 + 1
    assert t["length_km"] == pytest.approx(1.5 + 2.0)
    assert t["length_km_covered"] == pytest.approx(1.0 + 1.0)
    assert t["walks_missing_length_km"] == 1
    assert t["walks_missing_length_km_covered"] == 1
    assert t["walks_missing_edges_total"] == 0
    assert t["edges_per_city_pctiles"] == {"10": 1.4, "50": 3.0, "90": 3.8, "99": 3.98, "100": 4.0}
    assert catalog_totals(conn, as_of="2026-05-01")["edges_total"] == 2 + 2 + 5


def test_size_totals_only_skips_the_csv_scan(repo, tmp_path, monkeypatch):
    import scripts.size_gsv_history_walks as mod

    monkeypatch.setattr(
        mod, "load", lambda *a: pytest.fail("CSV scanned under --catalog-totals-only")
    )
    s = measure_size(repo, tmp_path / "t.json", totals_only=True, as_of="2026-05-02")
    assert set(s) == {"generated_by", "generated_at", "as_of", "walk_totals"}
    assert s["generated_by"] == (
        f"python scripts/size_gsv_history_walks.py {repo} {tmp_path / 't.json'}"
        " --as-of 2026-05-02 --catalog-totals-only"
    )
    assert json.loads((tmp_path / "t.json").read_text())["walk_totals"]["edges_total"] == 8


def test_both_mains_normalize_a_compact_as_of_before_the_text_compare(repo, tmp_path, capsys):
    # date.fromisoformat accepts "20260501", but run_date is compared as TEXT and
    # "2026-05-02" <= "20260501" is true, so an unnormalized cut admits every later walk.
    assert size_mod.main([str(repo), str(tmp_path / "s.json"), "--as-of", "20260501"]) == 0
    s = json.loads((tmp_path / "s.json").read_text())
    assert s["as_of"] == "2026-05-01" and s["generated_by"].endswith("--as-of 2026-05-01")
    assert s["google_samples"] == 7 + 3 + 4 and s["pairs_total"] == 0
    assert pair_mod.main([str(repo), str(tmp_path / "pc.json"), "--as-of", "20260501"]) == 0
    c = json.loads((tmp_path / "pc.json").read_text())
    assert c["as_of"] == "2026-05-01" and c["generated_by"].endswith("--as-of 2026-05-01")
    assert c["pairs"] == 0
    assert "generated_at" in c
    capsys.readouterr()


# ── The assembler ────────────────────────────────────────────────────────────


@pytest.fixture
def raw_dir(repo, tmp_path):
    raw = tmp_path / "out"
    measure_size(repo, raw / "summary.json", as_of="2026-05-02")
    measure_pair_changes(repo, raw / "pair_changes.json", as_of="2026-05-02")
    measure_size(repo, raw / "walk_totals.json", totals_only=True, as_of="2026-05-02")
    return raw


def _generated_by_values(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "generated_by":
                yield v
            else:
                yield from _generated_by_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _generated_by_values(v)


def test_analyze_assembles_the_record_and_copies_the_tables(repo, raw_dir, tmp_path):
    docs = tmp_path / "docs"
    rc = analyze.main(
        ["--raw-dir", str(raw_dir), "--docs-dir", str(docs), "--catalog-label", "fixture"]
    )
    assert rc == 0
    record = json.loads((docs / "gsv-history-sizing_metrics.json").read_text())
    assert record["generated_by"] == analyze.docs_generated_by(str(raw_dir), str(docs), "fixture")
    assert record["catalog_label"] == "fixture"
    # Every provenance stamp, nested ones included, names a script the repo holds.
    stamps = list(_generated_by_values(record))
    assert len(stamps) == 4
    for stamp in stamps:
        assert stamp.startswith("python scripts/")
        assert os.path.exists(os.path.join(PROJECT_ROOT, stamp.split()[1])), stamp
    assert set(record["inputs"]) == {"summary.json", "pair_changes.json", "walk_totals.json"}
    assert record["inputs"]["summary.json"]["as_of"] == "2026-05-02"
    for name, prov in record["inputs"].items():
        assert set(prov) == {"generated_by", "generated_at", "as_of"}, name
        assert prov["generated_by"].split()[2] == str(repo)
    for block in ("backfill", "change_rate", "walk_totals"):
        assert "generated_by" not in record[block] and "generated_at" not in record[block]
    assert "walk_totals" not in record["backfill"]
    assert record["backfill"]["distinct_google_global"] == 12
    assert record["walk_totals"]["edges_total"] == 8
    assert record["change_rate"]["changed"] == 5
    for raw_name, docs_name in analyze.TABLES.items():
        assert filecmp.cmp(raw_dir / raw_name, docs / docs_name, shallow=False)
    d = record["derived"]
    assert d["google_share_of_samples"] == pytest.approx(13 / 15)
    assert d["unanchored_samples"] == 2
    assert d["dedup_within_city_share"] == 0
    assert d["dedup_global_share"] == pytest.approx(1 / 13)
    assert d["dedup_global_extra_percentage_points"] == pytest.approx(100 / 13)
    assert d["requests_per_unit"] == {
        "walk_edge": 8,
        "distinct_google_pano": 12,
        "walk_sample": 15,
    }
    assert d["days_at_requests_per_minute"]["60"]["walk_sample"] == pytest.approx(15 / 86400)
    assert d["days_at_requests_per_minute"]["120"]["walk_edge"] == pytest.approx(8 / 172800)
    assert d["distinct_google_per_walk_edge"] == pytest.approx(12 / 8)
    # Per-city shares 0.8 (Bend), 1.0, 1.0: p10 interpolates to 0.84.
    assert d["google_share_per_city_pctiles"] == pytest.approx({"10": 0.84, "50": 1.0, "90": 1.0})
    assert (d["changed_newer_share"], d["changed_same_share"], d["changed_older_share"]) == (
        pytest.approx(3 / 5),
        pytest.approx(1 / 5),
        pytest.approx(1 / 5),
    )
    assert d["changed_undated"] == 0
    assert d["google_both_share_of_google_samples"] == pytest.approx(9 / 13)
    assert d["pairs_with_no_new_capture"] == 0
    assert d["top_contributors"] == [BEND, REDMOND]
    assert d["top_contributors_share_of_new_captures"] == 1.0
    rate = record["change_rate"]["new_capture_panos_per_day_per_1M_google_samples"]
    assert d["new_panos_per_day_scaled_to_all_google_samples"] == pytest.approx(rate * 13 / 1e6)
    assert d["requests_per_second_if_each_new_pano_requeried"] == pytest.approx(
        rate * 13 / 1e6 / 86400
    )
    # Re-query candidates: 5 changed + 2 gained, against 4 new captures.
    assert d["requery_candidates"] == 7
    assert d["requery_candidates_per_new_capture_pano"] == pytest.approx(7 / 4)
    assert d["requery_candidates_per_day_scaled_to_all_google_samples"] == pytest.approx(
        rate * 13 / 1e6 * 7 / 4
    )
    assert d["requests_per_second_if_each_candidate_requeried"] == pytest.approx(
        rate * 13 / 1e6 * 7 / 4 / 86400
    )


def test_analyze_refuses_raw_outputs_that_are_not_one_measurement(raw_dir, tmp_path):
    table = raw_dir / "pair_changes.csv"
    table.write_text("".join(table.read_text().splitlines(keepends=True)[:-1]))
    with pytest.raises(SystemExit, match="pair_changes rows"):
        analyze.read_raw(str(raw_dir))


def test_analyze_refuses_a_totals_block_measured_as_of_another_date(raw_dir):
    totals = json.loads((raw_dir / "walk_totals.json").read_text())
    totals["as_of"] = "2026-05-01"
    (raw_dir / "walk_totals.json").write_text(json.dumps(totals))
    with pytest.raises(SystemExit, match="as_of"):
        analyze.read_raw(str(raw_dir))


def test_analyze_refuses_a_totals_block_that_differs_from_the_scan_s_own(raw_dir):
    totals = json.loads((raw_dir / "walk_totals.json").read_text())
    totals["walk_totals"]["edges_total"] += 1
    (raw_dir / "walk_totals.json").write_text(json.dumps(totals))
    with pytest.raises(SystemExit, match="walk_totals"):
        analyze.read_raw(str(raw_dir))


# ── The committed record ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def metrics():
    with open(METRICS, encoding="utf-8") as f:
        return json.load(f)


def test_committed_generated_by_names_runnable_scripts_at_every_level(metrics):
    assert metrics["generated_by"] == analyze.docs_generated_by(
        analyze.RAW_DIR_DEFAULT, analyze.DOCS_DIR_DEFAULT, "makelab2-prod"
    )
    stamps = list(_generated_by_values(metrics))
    assert len(stamps) == 4
    for stamp in stamps:
        assert stamp.startswith("python scripts/"), stamp
        assert os.path.exists(os.path.join(PROJECT_ROOT, stamp.split()[1])), stamp
    # Every block was measured as of the same catalog date, from the checkout root.
    assert {v["as_of"] for v in metrics["inputs"].values()} == {"2026-10-09"}
    for name, prov in metrics["inputs"].items():
        assert set(prov) == {"generated_by", "generated_at", "as_of"}, name
        assert prov["generated_by"].split()[2] == "."
    # Nothing names the crawler host (the only host named is the catalog's).
    assert "makelab2" in metrics["catalog_label"]
    text = Path(METRICS).read_text(encoding="utf-8")
    assert "amazonaws" not in text and "ec2" not in text


def test_committed_backfill_block_matches_its_per_city_table(metrics):
    b = metrics["backfill"]
    d = metrics["derived"]
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
    # The writeup's dedup shares: within-city 5.6 %, global 8.2 %, 2.65 points apart.
    assert d["dedup_within_city_share"] == pytest.approx(
        1 - b["distinct_google_sum_per_city"] / b["google_samples"]
    )
    assert d["dedup_within_city_share"] == pytest.approx(0.0557, abs=5e-4)
    assert d["dedup_global_share"] == pytest.approx(0.0822, abs=5e-4)
    assert d["dedup_global_extra_percentage_points"] == pytest.approx(2.65, abs=5e-3)
    assert d["google_share_of_samples"] == pytest.approx(0.821, abs=5e-4)
    assert d["present_share_of_samples"] == pytest.approx(0.844, abs=5e-4)
    assert d["unanchored_share_of_google_samples"] == pytest.approx(0.218, abs=5e-4)
    top_decile = pc["distinct_google"].sort_values(ascending=False).head(len(pc) // 10)
    assert d["top_decile_cities_share_of_distinct_google"] == pytest.approx(
        top_decile.sum() / b["distinct_google_sum_per_city"]
    )
    assert d["top_decile_cities_share_of_distinct_google"] == pytest.approx(0.728, abs=5e-4)
    share = pc["google"] / pc["samples"]
    for q, v in d["google_share_per_city_pctiles"].items():
        assert share.quantile(int(q) / 100) == pytest.approx(v)
    assert [round(v * 100, 1) for v in d["google_share_per_city_pctiles"].values()] == [
        64.9,
        91.6,
        98.5,
    ]
    # The walk totals: one query per edge is pass 1, every sample is the chosen unit.
    assert b["cities"] == metrics["walk_totals"]["cities"]
    assert d["requests_per_unit"] == {
        "walk_edge": metrics["walk_totals"]["edges_total"],
        "distinct_google_pano": b["distinct_google_global"],
        "walk_sample": b["samples"],
    }
    assert d["distinct_google_per_walk_edge"] == pytest.approx(6.6, abs=0.05)
    days = d["days_at_requests_per_minute"]
    assert [round(v) for v in days["60"].values()] == [104, 681, 904]
    assert [round(v) for v in days["120"].values()] == [52, 341, 452]
    for unit, n in d["requests_per_unit"].items():
        assert days["60"][unit] == pytest.approx(n / 86400)


def test_committed_change_rate_block_matches_its_pair_table(metrics):
    c = metrics["change_rate"]
    d = metrics["derived"]
    pcs = pd.read_csv(os.path.join(DOCS, "gsv-history-sizing_pair_changes.csv"))
    assert len(pcs) == c["pairs"] == 121
    good = pcs[pcs["match_frac"] >= 0.95]
    assert len(good) == c["pairs_match_ge_95pct"] == 121
    # Under the collector's key no pair merged a row, so every match is a sample.
    assert c["key_merged_rows"] == 0
    assert (pcs["key_merged_curr"] == 0).all() and (pcs["key_merged_prev"] == 0).all()
    assert (pcs["matched"] == pcs[["samples_curr", "samples_prev"]].max(axis=1)).all()
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
    # The writeup's split of changed panos: newer 65.7 %, same date 29.4 %, older 4.9 %.
    assert d["changed_newer_share"] == pytest.approx(c["changed_newer_date"] / c["changed"])
    assert d["changed_newer_share"] == pytest.approx(0.657, abs=5e-4)
    assert d["changed_same_share"] == pytest.approx(0.294, abs=5e-4)
    assert d["changed_older_share"] == pytest.approx(0.049, abs=5e-4)
    assert d["changed_share_of_google_both"] == pytest.approx(0.1016, abs=5e-4)
    assert d["changed_undated"] == c["changed"] - (
        c["changed_newer_date"] + c["changed_same_date"] + c["changed_older_date"]
    )
    assert d["google_both_share_of_google_samples"] == pytest.approx(0.070, abs=5e-3)
    # Concentration: the five largest contributors hold 70.8 % of new captures, and
    # 47 pairs saw none.
    top5 = good.sort_values("new_capture_distinct_panos", ascending=False).head(5)
    assert d["top_contributors"] == top5["city_id"].tolist()
    assert d["top_contributors_share_of_new_captures"] == pytest.approx(
        top5["new_capture_distinct_panos"].sum() / c["new_capture_distinct_panos"]
    )
    assert d["top_contributors_share_of_new_captures"] == pytest.approx(0.708, abs=5e-4)
    assert d["pairs_with_no_new_capture"] == 47
    assert int((good["changed_newer_date"] + good["gained"] == 0).sum()) == 47
    # Scaled to every Google-anchored sample: ~78 k new panos a day, ~0.9 req/s.
    assert d["new_panos_per_day_scaled_to_all_google_samples"] == pytest.approx(
        c["new_capture_panos_per_day_per_1M_google_samples"]
        * metrics["backfill"]["google_samples"]
        / 1e6
    )
    assert d["new_panos_per_day_scaled_to_all_google_samples"] == pytest.approx(78e3, rel=0.01)
    assert d["requests_per_second_if_each_new_pano_requeried"] == pytest.approx(0.9, abs=0.05)
    # Re-querying every changed or gained sample is ~1.5x the new captures, ~1.4 req/s.
    assert d["requery_candidates"] == c["changed"] + c["gained"]
    assert d["requery_candidates_per_new_capture_pano"] == pytest.approx(1.54, abs=0.005)
    assert d["requests_per_second_if_each_candidate_requeried"] == pytest.approx(
        d["requests_per_second_if_each_new_pano_requeried"]
        * d["requery_candidates_per_new_capture_pano"]
    )
    assert d["requests_per_second_if_each_candidate_requeried"] == pytest.approx(1.4, abs=0.05)
    assert d["changed_undated"] == 3
