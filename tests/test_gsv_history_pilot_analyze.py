"""Pins for scripts/gsv_history_pilot_analyze.py (#442): offline, synthetic crawl files."""

import json
import os

import pandas as pd
import pytest

from scripts.gsv_history_pilot_analyze import analyze, main, summarize_edges
from streetscape_metadata_tracker.download_gsv_history import (
    QUERY_COLUMNS,
    SAMPLE_HISTORY_COLUMNS,
    WORKLIST_COLUMNS,
    output_paths,
)

STEM = "bend_walk"


def _worklist(tmp_path):
    rows = []
    for e in ("e0", "e1", "e2"):
        for idx, pass_no in ((1, 1), (0, 2), (2, 2)):
            rows.append(
                {
                    "city_id": "bend", "walk_csv_filename": "bend.csv.gz", "network_type": "drive",
                    "spacing_m": 15.0, "edge_id": e, "sample_idx": idx, "edge_samples": 3,
                    "pass": pass_no, "query_lat": 44.0, "query_lon": -121.0, "anchor_status": "OK",
                    "anchor_pano_id": f"cur-{e}", "anchor_capture_date": "2025-01-01",
                    "anchor_copyright": "© Google",
                }
            )  # fmt: skip
    path = tmp_path / f"{STEM}.worklist.csv.gz"
    pd.DataFrame(rows, columns=WORKLIST_COLUMNS).to_csv(path, index=False, compression="gzip")
    return str(path)


def _query(e, idx, dated, anchor_in, t):
    return {
        "edge_id": e, "sample_idx": idx, "http_status": 200, "response_code": 0,
        "panos_total": dated + 2, "panos_dated": dated, "panos_third_party": 0,
        "anchor_in_history": anchor_in, "current_copyright": "© 2026 Google",
        "country_code": "US", "street_name": None, "fetched_at": f"2026-10-17T17:{t:02d}:00+00:00",
    }  # fmt: skip


def _hist(e, idx, pano, year, third=False):
    return {
        "city_id": "bend", "walk_csv_filename": "bend.csv.gz", "pass": 1, "edge_id": e,
        "sample_idx": idx, "query_lat": 44.0, "query_lon": -121.0, "anchor_pano_id": f"cur-{e}",
        "anchor_capture_date": "2025-01-01", "pano_id": pano, "capture_date": f"{year}-06-01",
        "pano_lat": 44.0, "pano_lon": -121.0, "third_party": third, "is_current": False,
        "capture_day": None, "pano_type": 10 if third else 2, "elevation_m": None,
        "heading_deg": None, "pitch_deg": None, "roll_deg": None,
        "harvested_at": "2026-10-17T17:00:00+00:00",
    }  # fmt: skip


def _write(results, pass_no, queries, history):
    paths = output_paths(results, STEM, pass_no)
    pd.DataFrame(queries, columns=QUERY_COLUMNS).to_csv(
        paths["queries"], index=False, compression="gzip"
    )
    pd.DataFrame(history, columns=SAMPLE_HISTORY_COLUMNS).to_csv(
        paths["history"], index=False, compression="gzip"
    )


def test_pass_summary_and_edge_gain(tmp_path):
    wl = _worklist(tmp_path)
    results = tmp_path / "r"
    results.mkdir()
    # Pass 1: three midpoints; e2 answered with no history and a null anchor.
    _write(
        results, 1,
        [_query("e0", 1, 2, True, 0), _query("e1", 1, 3, False, 1), _query("e2", 1, 0, None, 2)],
        [_hist("e0", 1, "a", 2011), _hist("e0", 1, "b", 2019), _hist("e1", 1, "c", 2014),
         _hist("e1", 1, "d", 2019), _hist("e1", 1, "CIHM0ogX", 2021, third=True)],
    )  # fmt: skip
    # Pass 2: e0's other samples add one new pano and repeat one; e1 adds nothing.
    _write(
        results, 2,
        [_query("e0", 0, 2, True, 10), _query("e0", 2, 1, True, 11), _query("e1", 0, 1, True, 12)],
        [_hist("e0", 0, "a", 2011), _hist("e0", 0, "z", 2008), _hist("e0", 2, "z", 2008),
         _hist("e1", 0, "c", 2014)],
    )  # fmt: skip
    sc = tmp_path / "spot.json"
    sc.write_text(json.dumps({"requests": 5, "statuses": {"OK": 5}, "month_match": 5, "month_mismatch": 0,
                              "pano_id_differs": 0, "unknown": 0, "error": 0,
                              "month_match_share_of_answered": 1.0, "seed": 1}))  # fmt: skip
    m = analyze(str(results), wl, str(sc))
    p1 = m["passes"]["1"]
    assert p1["worklist_rows"] == 3 and p1["requests_answered"] == 3
    assert p1["anchored_samples"] == 2 and p1["anchor_in_history"] == 1
    assert p1["anchor_in_history_share"] == 0.5
    assert p1["zero_history_samples"] == 1 and p1["third_party_rows"] == 1
    assert p1["unanchored_samples"] == 1 and p1["unanchored_samples_with_history"] == 0
    assert p1["history_rows"] == 5 and p1["distinct_official_panos"] == 4
    assert p1["capture_year_histogram"] == {"2011": 1, "2014": 1, "2019": 2}
    assert p1["elapsed_min"] == 2.0 and p1["achieved_rate_per_min"] == 1.5
    e = m["edges"]
    # e0: pass 1 found {a, b}; both passes {a, b, z}. e1: {c, d} both ways. e2: nothing.
    assert e["edges"] == 2
    assert (e["distinct_official_panos_pass1_sum"], e["distinct_official_panos_both_sum"]) == (4, 5)
    assert e["ratio_both_to_pass1"] == 1.25 and e["edges_where_pass2_added_panos"] == 1
    assert e["distinct_official_panos_global_both"] == 5
    # Months: e0 midpoint saw {2011-06, 2019-06}, both passes add 2008-06 (2 of 3);
    # e1 saw both of its months (complete).
    assert e["edges_where_midpoint_sees_every_month"] == 1
    assert e["months_per_edge_pass1_mean"] == 2.0 and e["months_per_edge_both_mean"] == 2.5
    assert e["midpoint_month_coverage_share_mean"] == pytest.approx((2 / 3 + 1) / 2, abs=1e-4)
    assert m["spot_check"]["month_match"] == 5 and m["city_id"] == "bend"


def test_pass_one_only_has_no_edge_block_and_main_writes_json(tmp_path, capsys):
    wl = _worklist(tmp_path)
    results = tmp_path / "r"
    results.mkdir()
    _write(results, 1, [_query("e0", 1, 1, True, 0)], [_hist("e0", 1, "a", 2011)])
    m = analyze(str(results), wl, None)
    assert "edges" not in m and "spot_check" not in m and m["passes"]["1"]["elapsed_min"] is None
    out = tmp_path / "m.json"
    assert main(["--results-dir", str(results), "--worklist", wl, "--out", str(out)]) == 0
    assert json.load(open(out))["generated_by"] == "scripts/gsv_history_pilot_analyze.py"
    assert (
        main(["--results-dir", str(tmp_path / "empty"), "--worklist", wl, "--out", str(out)]) == 1
    )


def test_duplicated_and_stranded_history_rows_and_unanswered_queries_are_not_counted(tmp_path):
    # The crawl appends history rows before the ledger row, so a crash
    # between the two leaves duplicates on disk (and rows no ledger row
    # stands for); a 200 that answered nothing leaves a ledger row with
    # `unanswered` set. The analyzer counts none of them as answers.
    wl = _worklist(tmp_path)
    results = tmp_path / "r"
    results.mkdir()
    unanswered = _query("e2", 1, 0, None, 1) | {"unanswered": "response code 5: decommissioned"}
    answered_later = _query("e2", 1, 1, True, 3)
    _write(
        results, 1,
        [_query("e0", 1, 2, True, 0), unanswered, _query("e1", 1, 1, True, 2), answered_later],
        [
            # e0's rows twice: the second copy is the resume's, a later harvested_at.
            _hist("e0", 1, "a", 2011) | {"harvested_at": "2026-10-17T17:00:00+00:00"},
            _hist("e0", 1, "b", 2019) | {"harvested_at": "2026-10-17T17:00:00+00:00"},
            _hist("e0", 1, "a", 2011) | {"harvested_at": "2026-10-17T17:05:00+00:00"},
            _hist("e0", 1, "b", 2019) | {"harvested_at": "2026-10-17T17:05:00+00:00"},
            _hist("e1", 1, "c", 2014),
            _hist("e2", 1, "d", 2015),
            # A key the ledger never recorded (crashed before any ledger row).
            _hist("e9", 1, "zz", 2000),
        ],
    )  # fmt: skip
    p1 = analyze(str(results), wl, None)["passes"]["1"]
    assert p1["requests_answered"] == 3 and p1["unanswered_rows"] == 1
    assert p1["history_rows"] == 4 and p1["distinct_official_panos"] == 4
    assert p1["capture_year_histogram"] == {"2011": 1, "2014": 1, "2015": 1, "2019": 1}
    assert p1["zero_history_samples"] == 0  # the unanswered row's 0 is not a zero-history answer


def test_the_spot_checks_per_year_split_is_carried(tmp_path):
    wl = _worklist(tmp_path)
    results = tmp_path / "r"
    results.mkdir()
    _write(results, 1, [_query("e0", 1, 1, True, 0)], [_hist("e0", 1, "a", 2011)])
    sc = tmp_path / "spot.json"
    sc.write_text(
        json.dumps(
            {"requests": 2, "month_match": 2, "by_year": {"2007": {"n": 2, "month_match": 2}}}
        )
    )
    m = analyze(str(results), wl, str(sc))
    assert m["spot_check"]["by_year"] == {"2007": {"n": 2, "month_match": 2}}


def test_summarize_edges_counts_distinct_panos_not_rows():
    h1 = pd.DataFrame([_hist("e0", 1, "a", 2011), _hist("e0", 1, "a", 2011)])
    h2 = pd.DataFrame([_hist("e0", 0, "a", 2011)])
    e = summarize_edges(h1, h2)
    assert (
        e["distinct_official_panos_pass1_sum"] == 1 and e["distinct_official_panos_both_sum"] == 1
    )
    assert e["ratio_both_to_pass1"] == 1.0 and e["edges_where_pass2_added_panos"] == 0


def test_committed_pilot_metrics_hold_the_figures_the_writeup_quotes():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "docs", "experiments", "gsv-history-pilot_metrics.json")) as f:
        m = json.load(f)
    p1, p2, e, sc = m["passes"]["1"], m["passes"]["2"], m["edges"], m["spot_check"]
    assert m["generated_by"] == "scripts/gsv_history_pilot_analyze.py"
    assert (p1["requests_answered"], p2["requests_answered"]) == (2000, 21728)
    assert p1["http_status"] == {"200": 2000} and p2["http_status"] == {"200": 21728}
    assert (p1["history_rows"], p1["third_party_rows"], p1["distinct_official_panos"]) == (
        15515,
        20,
        15490,
    )
    assert (p2["history_rows"], p2["third_party_rows"], p2["distinct_official_panos"]) == (
        191644,
        179,
        181706,
    )
    assert (
        round(p1["anchor_in_history_share"], 3) == 0.854
        and round(p2["anchor_in_history_share"], 3) == 0.862
    )
    assert (p1["unanchored_samples"], p1["unanchored_samples_with_history"]) == (16, 14)
    assert (p2["unanchored_samples"], p2["unanchored_samples_with_history"]) == (172, 135)
    assert round(e["ratio_both_to_pass1"], 1) == 13.3 and e["edges_where_pass2_added_panos"] == 1895
    assert e["edges_where_midpoint_sees_every_month"] == 664
    assert round(e["midpoint_month_coverage_share_mean"], 2) == 0.76
    assert (e["months_per_edge_pass1_mean"], e["months_per_edge_both_mean"]) == (
        pytest.approx(7.7, abs=0.05),
        pytest.approx(11.4, abs=0.05),
    )
    assert (sc["requests"], sc["month_match"], sc["month_mismatch"], sc["unknown"]) == (
        200,
        200,
        0,
        0,
    )
