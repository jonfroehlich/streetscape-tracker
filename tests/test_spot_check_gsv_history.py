"""
Pins for scripts/spot_check_gsv_history.py (#442): the documented metadata
API is stubbed, so no key and no network.
"""

import gzip
import json
import os

import pandas as pd
import pytest

from scripts.spot_check_gsv_history import main, sample_ids, spot_check
from streetscape_metadata_tracker import db


def _history(tmp_path):
    rows = []
    for i in range(30):
        rows.append(
            {
                "edge_id": f"e{i % 5}",
                "sample_idx": 0,
                "pano_id": f"official{i:02d}",
                "capture_date": f"20{10 + i % 10}-0{1 + i % 9}-01",
                "third_party": False,
                "is_current": i % 5 == 0,
            }
        )
    rows.append(rows[0] | {"edge_id": "dup"})  # the same pano seen from a second sample
    rows.append(
        {"edge_id": "e9", "sample_idx": 0, "pano_id": "CIHM0ogUSER", "capture_date": "2019-11-01",
         "third_party": True, "is_current": False}
    )  # fmt: skip
    path = tmp_path / "x.pass1.history.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        pd.DataFrame(rows).to_csv(f, index=False)
    return path


def test_sample_is_distinct_official_seeded_and_capped(tmp_path):
    h = pd.read_csv(_history(tmp_path), dtype={"pano_id": str})
    s = sample_ids(h, 10, seed=1)
    assert len(s) == 10 and s["pano_id"].is_unique and not s["third_party"].any()
    assert list(s["pano_id"]) == list(sample_ids(h, 10, seed=1)["pano_id"])
    assert list(s["pano_id"]) != list(sample_ids(h, 10, seed=2)["pano_id"])
    assert (
        len(sample_ids(h, 100, seed=1)) == 30
    )  # distinct official ids, the duplicate counted once
    assert len(sample_ids(h, 100, seed=1, include_third_party=True)) == 31


def test_spot_check_classifies_every_answer_and_paces(tmp_path):
    h = pd.read_csv(_history(tmp_path), dtype={"pano_id": str})
    s = sample_ids(h, 6, seed=0)
    ours = dict(zip(s["pano_id"], s["capture_date"], strict=True))
    ids = list(s["pano_id"])

    def fetch(pano_id):
        i = ids.index(pano_id)
        if i == 0:
            return 200, {"status": "ZERO_RESULTS"}  # retired pano
        if i == 1:
            return 200, {"status": "OK", "pano_id": "someoneelse", "date": ours[pano_id][:7]}
        if i == 2:
            return 200, {"status": "OK", "pano_id": pano_id, "date": "1999-01"}
        if i == 3:
            return 500, {"status": "HTTP 500"}
        return 200, {"status": "OK", "pano_id": pano_id, "date": ours[pano_id][:7]}

    sleeps = []
    m = spot_check(s, fetch, rate_per_minute=120, sleep=sleeps.append)
    assert (m["month_match"], m["month_mismatch"], m["pano_id_differs"], m["unknown"], m["error"]) == (
        2, 1, 1, 1, 1,
    )  # fmt: skip
    assert m["requests"] == 6 and m["month_match_share_of_answered"] == pytest.approx(
        2 / 3, abs=1e-4
    )
    assert [x["kind"] for x in m["mismatches"]] == ["pano_id", "month"]
    assert m["mismatches"][1]["theirs"] == "1999-01"
    assert sleeps == [0.5] * 5  # a gap between requests, none before the first
    assert sum(v["n"] for v in m["by_year"].values()) == 6


def test_main_writes_metrics_records_spend_and_dry_runs_without_fetching(tmp_path, data_dir):
    path = _history(tmp_path)
    out = tmp_path / "spot.json"
    calls = []

    def fetch(pano_id):
        calls.append(pano_id)
        return 200, {"status": "OK", "pano_id": pano_id, "date": "2000-01"}

    assert (
        main(["--history", str(path), "--out", str(out), "--limit", "4", "--dry-run"], fetch=fetch)
        == 0
    )
    assert calls == [] and not out.exists()
    db_path = os.path.join(data_dir, "streetscape_tracker.db")
    db.connect(db_path).close()
    assert (
        main(
            ["--history", str(path), "--out", str(out), "--limit", "4", "--rate-per-minute", "0",
             "--db-path", db_path],
            fetch=fetch,
        )
        == 0
    )  # fmt: skip
    metrics = json.load(open(out))
    assert len(calls) == 4 and metrics["requests"] == 4 and metrics["month_mismatch"] == 4
    assert metrics["generated_by"] == "scripts/spot_check_gsv_history.py"
    conn = db.connect(db_path)
    total = conn.execute(
        "SELECT SUM(requests) FROM api_usage WHERE provider = 'gsv_streets'"
    ).fetchone()[0]
    assert total == 4
