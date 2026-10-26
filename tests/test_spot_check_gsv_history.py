"""
Pins for scripts/spot_check_gsv_history.py (#442): the documented metadata
API is stubbed, so no key and no network.
"""

import gzip
import json
import os
import sqlite3
from datetime import date

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
    _catalog_with_a_walk(db_path)
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


def _catalog_with_a_walk(db_path):
    """A real catalog that holds one walk (the read-only opener refuses an empty one)."""
    conn = db.connect(db_path)
    city_id = db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=40,
        grid_height_m=40,
        step_m=20,
    )
    db.register_street_walk(
        conn, city_id=city_id, run_date=date(2026, 5, 1), csv_filename="bend_walk.csv.gz"
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("version", [db.SCHEMA_VERSION - 1, db.SCHEMA_VERSION + 1])
def test_a_catalog_at_another_schema_version_is_refused_before_any_request_and_never_migrated(
    tmp_path, data_dir, version
):
    # The 2026-10-17 outage: this script's db.connect migrated the PROD
    # catalog. The version check now runs before the first request, in
    # either direction, and the file is untouched; at zero requests nothing
    # is spent against a catalog the ledger could not reach.
    path = _history(tmp_path)
    db_path = os.path.join(data_dir, "streetscape_tracker.db")
    _catalog_with_a_walk(db_path)
    raw = sqlite3.connect(db_path)
    raw.execute(f"PRAGMA user_version = {version}")
    raw.commit()
    raw.close()
    before = open(db_path, "rb").read()
    calls = []

    def fetch(pano_id):
        calls.append(pano_id)
        return 200, {"status": "OK", "pano_id": pano_id, "date": "2000-01"}

    out = tmp_path / "spot.json"
    argv = ["--history", str(path), "--out", str(out), "--limit", "4", "--rate-per-minute", "0"]
    assert main(argv + ["--db-path", db_path], fetch=fetch) == 2
    assert main(argv + ["--db-path", db_path, "--dry-run"], fetch=fetch) == 2
    assert calls == [] and not out.exists()
    assert open(db_path, "rb").read() == before
    raw = sqlite3.connect(db_path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == version
    raw.close()
    assert not os.path.exists(db_path + "-wal") and not os.path.exists(db_path + "-shm")
    # A missing catalog is refused the same way; nothing is created.
    missing = os.path.join(data_dir, "nope.db")
    assert main(argv + ["--db-path", missing], fetch=fetch) == 2
    assert calls == [] and not os.path.exists(missing)
