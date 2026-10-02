"""Tests for scripts/recompute_run_diffs.py (issue #245): re-derive existing
``run_diffs`` rows from the two runs' CSVs under the current reader, keep each
row's ``diff_id``, and keep the published detail file a function of the result.

Fixtures are built the way the pipeline builds them: a real catalog, runs
registered through ``db``, CSVs written to disk and read back through the REAL
loader, every filename from a generator. A stale row is produced by the code
that produced it in production where that is possible — the phantom row is
computed by ``compute_run_diff`` over a baseline parsed with the pre-#244
strict format, which is the one thing #244 changed in the loader — rather than
by typing numbers no collector could have written.
"""

import gzip
import json
import os
import sqlite3
from datetime import date

import pandas as pd
import pytest

import scripts.recompute_run_diffs as recompute_module
from scripts.recompute_run_diffs import main
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.cli import _compute_and_record_diff
from streetscape_metadata_tracker.diff import (
    compute_run_diff,
    generate_diff_filename,
    write_diff_detail,
)
from streetscape_metadata_tracker.download_common import standardize_capture_date
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.json_summarizer import regenerate_run_json
from streetscape_metadata_tracker.naming import generate_run_filename
from streetscape_metadata_tracker.scheduler import USAGE_EXIT_CODE
from tests.conftest import make_city_df, make_mapillary_city_df, write_city_csv_gz

BASE, D1, D2 = date(2024, 12, 15), date(2026, 4, 1), date(2026, 7, 1)

# A legacy baseline as the pre-2026 downloader wrote it: MONTH precision.
BASELINE_PANOS = [
    ("keep_a", "2022-09"),
    ("keep_b", "2019-01"),
    ("redated", "2020-05"),
    ("removed", "2018-03"),
]
# The next collection of the same city, dated the way the GSV writer dates
# them: the API's YYYY-MM through standardize_capture_date, pinned to the 1st.
LATER_PANOS = [
    ("keep_a", standardize_capture_date("2022-09")),
    ("keep_b", standardize_capture_date("2019-01")),
    ("redated", standardize_capture_date("2023-06")),  # a genuine re-drive
    ("added", standardize_capture_date("2025-02")),
]
# What the comparison genuinely says once the baseline's dates are readable.
GENUINE = {
    "panos_added": 1,
    "panos_removed": 1,
    "panos_persisted": 3,
    "capture_date_changed": 1,
}

COUNTERS = (
    "grid_aligned",
    "panos_added",
    "panos_removed",
    "panos_persisted",
    "capture_date_changed",
    "points_gained_coverage",
    "points_lost_coverage",
    "coverage_delta_pct",
    "detail_filename",
)


# ── fixture builders ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_batch_in_flight(monkeypatch):
    """The in-flight detector reads `ps`; a real run-due on the machine running
    the suite must not decide these tests."""
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: None)


def _city(conn, name="Bend"):
    return db.register_city(
        conn,
        city_name=name,
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="us",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
    )


def _run(conn, data_dir, city_id, run_date, panos, provider="gsv", width=1000, baseline=False):
    """Write one run's CSV under its generated name and catalog it."""
    name = generate_run_filename(city_id, width, 1000, 20, run_date, provider=provider) + ".csv.gz"
    factory = make_mapillary_city_df if provider == "mapillary" else make_city_df
    write_city_csv_gz(factory(panos, run_date=run_date), os.path.join(data_dir, name))
    run_id = db.register_run(
        conn,
        city_id=city_id,
        run_date=run_date,
        csv_filename=name,
        provider=provider,
        is_baseline=baseline,
    )
    return run_id, name


def _strict_load(path):
    """The pre-#244 loader: identical except capture_date's strict format."""
    df = load_city_csv_file(path)
    with gzip.open(path, "rt") as fh:
        raw = pd.read_csv(fh, dtype=str)["capture_date"]
    df["capture_date"] = pd.to_datetime(raw, format="%Y-%m-%d", errors="coerce")
    return df


def _record_as_old_collector(conn, data_dir, city_id, from_run, to_run, provider="gsv"):
    """Record the diff the pre-#244 collector would have: baseline read through
    the strict format, detail file written under the generated name."""
    (from_id, from_csv), (to_id, to_csv) = from_run, to_run
    diff = compute_run_diff(
        _strict_load(os.path.join(data_dir, from_csv)),
        load_city_csv_file(os.path.join(data_dir, to_csv)),
    )
    from_date = conn.execute("SELECT run_date FROM runs WHERE run_id = ?", (from_id,)).fetchone()[0]
    to_date = conn.execute("SELECT run_date FROM runs WHERE run_id = ?", (to_id,)).fetchone()[0]
    detail = generate_diff_filename(city_id, from_date, to_date, provider=provider)
    write_diff_detail(diff, os.path.join(data_dir, detail))
    return db.record_diff(
        conn,
        city_id=city_id,
        from_run_id=from_id,
        to_run_id=to_id,
        grid_aligned=diff.grid_aligned,
        panos_added=diff.panos_added,
        panos_removed=diff.panos_removed,
        panos_persisted=diff.panos_persisted,
        capture_date_changed=diff.capture_date_changed,
        points_gained_coverage=diff.points_gained_coverage,
        points_lost_coverage=diff.points_lost_coverage,
        coverage_delta_pct=diff.coverage_delta_pct,
        detail_filename=detail,
    )


def _record_as_collector(conn, data_dir, city_id, from_run, to_run, to_date, provider="gsv"):
    """Record the diff through the CURRENT collector, with every argument it passes."""
    city_row = db.resolve_city(conn, city_id)
    prev = db.get_previous_run(conn, city_id, to_date, provider=provider)
    assert prev.run_id == from_run[0]
    df_new = load_city_csv_file(os.path.join(data_dir, to_run[1]))
    _compute_and_record_diff(
        conn, city_row, prev, to_run[0], to_date, df_new, data_dir, provider=provider
    )
    return conn.execute(
        "SELECT diff_id FROM run_diffs WHERE from_run_id = ? AND to_run_id = ?",
        (from_run[0], to_run[0]),
    ).fetchone()[0]


def _phantom_series(conn, data_dir):
    """#245's shape: a month-precision baseline, its successor, and the phantom row."""
    city_id = _city(conn)
    base = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS, baseline=True)
    later = _run(conn, data_dir, city_id, D2, LATER_PANOS)
    diff_id = _record_as_old_collector(conn, data_dir, city_id, base, later)
    detail = os.path.join(
        data_dir, generate_diff_filename(city_id, BASE.isoformat(), D2.isoformat())
    )
    return city_id, base, later, diff_id, detail


def _row(conn, diff_id):
    return conn.execute("SELECT * FROM run_diffs WHERE diff_id = ?", (diff_id,)).fetchone()


def _snapshot(conn, diff_id):
    return dict(_row(conn, diff_id))


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _detail_rows(path):
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh, dtype=str, keep_default_na=False)
    return sorted(
        (r.change_type, r.pano_id, r.old_capture_date, r.new_capture_date) for r in df.itertuples()
    )


def _main(data_dir, *extra):
    return main(["--data-dir", data_dir, *extra])


# ── the phantom repair, end to end ───────────────────────────────────────────


def test_the_phantom_fixture_is_what_the_old_collector_recorded(conn, data_dir):
    """Guards the fixture itself: the stored row must carry the phantom (every
    persisted pano 're-dated'), or the repair test below proves nothing."""
    _, _, _, diff_id, detail = _phantom_series(conn, data_dir)
    row = _row(conn, diff_id)
    assert row["capture_date_changed"] == row["panos_persisted"] == 3
    assert sum(1 for r in _detail_rows(detail) if r[0] == "capture_date_changed") == 3


def test_phantom_repair_dry_run_writes_nothing_and_execute_repairs(conn, data_dir, capsys):
    city_id, _, later, diff_id, detail = _phantom_series(conn, data_dir)
    json_name = regenerate_run_json(conn, later[0], data_dir)
    json_path = os.path.join(data_dir, json_name)
    before_row, before_detail = _snapshot(conn, diff_id), _read_bytes(detail)
    before_json = _read_bytes(json_path)

    assert _main(data_dir) == 0
    out = capsys.readouterr().out
    assert "would change" in out and "capture_date_changed 3 -> 1" in out
    assert _snapshot(conn, diff_id) == before_row
    assert _read_bytes(detail) == before_detail
    assert _read_bytes(json_path) == before_json

    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    row = _row(conn, diff_id)
    assert row is not None, "the row was replaced, not updated: its diff_id changed"
    assert {k: row[k] for k in GENUINE} == GENUINE
    assert row["detail_filename"] == os.path.basename(detail)
    # Content, not existence: the phantom file existed before the repair too.
    assert _detail_rows(detail) == sorted(
        [
            ("capture_date_changed", "redated", "2020-05-01", "2023-06-01"),
            ("pano_added", "added", "", "2025-02-01"),
            ("pano_removed", "removed", "2018-03-01", ""),
        ]
    )

    # A second pass is a no-op, computed_at included.
    after = _snapshot(conn, diff_id)
    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    assert "1 unchanged, 0 changed" in capsys.readouterr().out
    assert _snapshot(conn, diff_id) == after


def test_a_row_that_recomputes_to_no_changes_loses_its_detail_file(conn, data_dir, capsys):
    """The #265 interaction: a phantom-only diff (nothing but the dates
    'changed') is, read correctly, no diff at all — so its published file goes
    and the row records none."""
    city_id = _city(conn)
    base = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS[:2], baseline=True)
    later = _run(conn, data_dir, city_id, D2, LATER_PANOS[:2])
    diff_id = _record_as_old_collector(conn, data_dir, city_id, base, later)
    detail_name = generate_diff_filename(city_id, BASE.isoformat(), D2.isoformat())
    assert _row(conn, diff_id)["capture_date_changed"] == 2  # the phantom

    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    row = _row(conn, diff_id)
    assert row["capture_date_changed"] == 0
    assert row["detail_filename"] is None
    assert not os.path.exists(os.path.join(data_dir, detail_name))
    out = capsys.readouterr().out
    assert f"remove these there:\n  {detail_name}" in out


# ── the #367 query-radius re-diff (issue #394) ──────────────────────────────


def _far_pano_series(conn, data_dir):
    """A GSV pair across #367's 50 m rule: point A held only a pano ~1.1 km
    from its query point on D1 and a near one on D2; point B holds the same
    near pano both times. The stale row is what the pre-#367 collector
    recorded, i.e. both sides read RAW, so the far pano counted as coverage."""
    city_id = _city(conn)
    runs = []
    for run_date, a_pano, a_offset_deg in ((D1, "far_a", 0.01), (D2, "near_a", 0.0001)):
        name = generate_run_filename(city_id, 1000, 1000, 20, run_date, provider="gsv") + ".csv.gz"
        df = make_city_df([(a_pano, "2024-01-01"), ("keep_b", "2024-01-01")], run_date=run_date)
        df.loc[0, "pano_lat"] = df.loc[0, "query_lat"] + a_offset_deg
        write_city_csv_gz(df, os.path.join(data_dir, name))
        run_id = db.register_run(
            conn, city_id=city_id, run_date=run_date, csv_filename=name, provider="gsv"
        )
        runs.append((run_id, name))
    (from_id, from_csv), (to_id, to_csv) = runs
    stale = compute_run_diff(
        load_city_csv_file(os.path.join(data_dir, from_csv), raw=True),
        load_city_csv_file(os.path.join(data_dir, to_csv), raw=True),
    )
    detail = generate_diff_filename(city_id, D1.isoformat(), D2.isoformat())
    write_diff_detail(stale, os.path.join(data_dir, detail))
    diff_id = db.record_diff(
        conn,
        city_id=city_id,
        from_run_id=from_id,
        to_run_id=to_id,
        grid_aligned=stale.grid_aligned,
        panos_added=stale.panos_added,
        panos_removed=stale.panos_removed,
        panos_persisted=stale.panos_persisted,
        capture_date_changed=stale.capture_date_changed,
        points_gained_coverage=stale.points_gained_coverage,
        points_lost_coverage=stale.points_lost_coverage,
        coverage_delta_pct=stale.coverage_delta_pct,
        detail_filename=detail,
    )
    return runs[1], diff_id, os.path.join(data_dir, detail)


def test_a_gsv_rediff_applies_the_query_radius_rule(conn, data_dir):
    """The script re-diffs through the loader's DEFAULT path, so #367's 50 m
    rule reaches both sides: the far pano stops being coverage that was
    replaced (one removed, no point gained) and becomes a point that GAINED
    coverage (nothing removed). A loader call with raw=True, or any reader
    that skips analysis.apply_query_radius, leaves the stale row as it is."""
    later, diff_id, detail = _far_pano_series(conn, data_dir)
    stale = _row(conn, diff_id)
    # Guard the fixture: the stored row really is the pre-#367 reading.
    assert (stale["panos_added"], stale["panos_removed"]) == (1, 1)
    assert (stale["points_gained_coverage"], stale["points_lost_coverage"]) == (0, 0)
    regenerate_run_json(conn, later[0], data_dir)
    assert _published_change(conn, data_dir, later[0])["panos_removed"] == 1

    assert (
        _main(data_dir, "--provider", "gsv", "--execute", "--regenerate-json", "--no-publish-json")
        == 0
    )
    row = _row(conn, diff_id)
    assert (row["panos_added"], row["panos_removed"], row["panos_persisted"]) == (1, 0, 1)
    assert (row["points_gained_coverage"], row["points_lost_coverage"]) == (1, 0)
    assert row["coverage_delta_pct"] > 0
    assert _detail_rows(detail) == [("pano_added", "near_a", "", "2024-01-01")]
    assert _published_change(conn, data_dir, later[0])["panos_removed"] == 0


# ── idempotence and the detail file ─────────────────────────────────────────


def test_a_correct_row_is_left_untouched(conn, data_dir, capsys):
    city_id = _city(conn)
    a = _run(conn, data_dir, city_id, D1, [("p1", "2020-05-01")])
    b = _run(conn, data_dir, city_id, D2, [("p2", "2024-05-01")])
    diff_id = _record_as_collector(conn, data_dir, city_id, a, b, D2)
    before = _snapshot(conn, diff_id)
    detail = os.path.join(data_dir, before["detail_filename"])
    before_detail = _read_bytes(detail)

    for _ in range(2):
        assert _main(data_dir, "--execute", "--no-publish-json") == 0
        assert "1 unchanged, 0 changed" in capsys.readouterr().out
        assert _snapshot(conn, diff_id) == before
        assert _read_bytes(detail) == before_detail


def test_right_counters_with_the_detail_file_missing_rewrite_the_file(conn, data_dir):
    city_id = _city(conn)
    a = _run(conn, data_dir, city_id, D1, [("p1", "2020-05-01")])
    b = _run(conn, data_dir, city_id, D2, [("p2", "2024-05-01")])
    diff_id = _record_as_collector(conn, data_dir, city_id, a, b, D2)
    detail = os.path.join(data_dir, _row(conn, diff_id)["detail_filename"])
    os.remove(detail)

    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    assert _detail_rows(detail) == sorted(
        [("pano_added", "p2", "", "2024-05-01"), ("pano_removed", "p1", "2020-05-01", "")]
    )


# ── diff_id is kept, so the current comparison stays current ────────────────


def test_recomputing_an_older_comparison_keeps_the_newer_one_current(conn, data_dir):
    """Two rows into one to_run: A->C (stale, older diff_id) and B->C (current,
    newer). Recomputing A->C with INSERT OR REPLACE would hand it the MAX
    diff_id and flip every published change block to compare against A."""
    city_id = _city(conn)
    a = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS, baseline=True)
    b = _run(conn, data_dir, city_id, D1, LATER_PANOS[:3])
    c = _run(conn, data_dir, city_id, D2, LATER_PANOS)
    stale_id = _record_as_old_collector(conn, data_dir, city_id, a, c)
    current_id = _record_as_collector(conn, data_dir, city_id, b, c, D2)
    assert current_id > stale_id

    def advertised():
        return {r["city_id"]: r["diff_from_run_date"] for r in db.get_latest_runs_all(conn)}

    assert advertised()[city_id] == D1.isoformat()
    assert _main(data_dir, "--execute", "--regenerate-json", "--no-publish-json") == 0
    assert advertised()[city_id] == D1.isoformat()
    assert _row(conn, stale_id)["capture_date_changed"] == GENUINE["capture_date_changed"]
    # The rebuilt per-run JSON replays the SAME comparison the aggregate reads.
    json_name = conn.execute("SELECT json_filename FROM runs WHERE run_id = ?", (c[0],)).fetchone()
    with gzip.open(os.path.join(data_dir, json_name[0]), "rt") as fh:
        change = json.load(fh)["change_from_previous_run"]
    assert change["from_run_date"] == D1.isoformat()


# ── rows the collector's gates refuse ───────────────────────────────────────


def test_geometry_mismatch_and_missing_csv_rows_are_skipped_untouched(conn, data_dir, capsys):
    city_id = _city(conn)
    other_grid = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS, width=2000, baseline=True)
    a = _run(conn, data_dir, city_id, D1, LATER_PANOS)
    b = _run(conn, data_dir, city_id, D2, LATER_PANOS[:2])
    # Stale rows (the phantom shape) on both pairs, then each pair broken.
    mismatch_id = _record_as_old_collector(conn, data_dir, city_id, other_grid, a)
    missing_id = _record_as_old_collector(conn, data_dir, city_id, a, b)
    os.remove(os.path.join(data_dir, b[1]))
    before = {i: _snapshot(conn, i) for i in (mismatch_id, missing_id)}
    details = {i: _read_bytes(os.path.join(data_dir, before[i]["detail_filename"])) for i in before}

    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    out = capsys.readouterr().out
    assert "grid geometry differs" in out
    assert f"CSV missing ({b[1]})" in out
    assert "2 skipped, 0 failed" in out
    for i in before:
        assert _snapshot(conn, i) == before[i]
        assert _read_bytes(os.path.join(data_dir, before[i]["detail_filename"])) == details[i]


def test_a_row_that_fails_is_counted_and_the_pass_continues(conn, data_dir, capsys):
    city_id = _city(conn)
    a = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS, baseline=True)
    b = _run(conn, data_dir, city_id, D1, LATER_PANOS)
    c = _run(conn, data_dir, city_id, D2, LATER_PANOS)
    broken_id = _record_as_old_collector(conn, data_dir, city_id, a, b)
    good_id = _record_as_old_collector(conn, data_dir, city_id, a, c)
    with open(os.path.join(data_dir, b[1]), "wb") as fh:
        fh.write(b"not gzip")

    assert _main(data_dir, "--execute", "--no-publish-json") == 1
    assert "1 changed, 0 skipped, 1 failed" in capsys.readouterr().out
    assert _row(conn, broken_id)["capture_date_changed"] == 3
    assert _row(conn, good_id)["capture_date_changed"] == 1


# ── selection ───────────────────────────────────────────────────────────────


@pytest.fixture
def three_stale_series(conn, data_dir):
    """Bend gsv, Bend mapillary and Salem gsv, each with one stale row (stored
    counters a recompute will move)."""
    ids, city_ids = {}, {}
    for city_name, provider in (("Bend", "gsv"), ("Bend", "mapillary"), ("Salem", "gsv")):
        if city_name not in city_ids:
            city_ids[city_name] = _city(conn, city_name)
        city_id = city_ids[city_name]
        a = _run(conn, data_dir, city_id, D1, [("p1", "2020-05-01")], provider=provider)
        b = _run(conn, data_dir, city_id, D2, [("p2", "2024-05-01")], provider=provider)
        diff_id = _record_as_collector(conn, data_dir, city_id, a, b, D2, provider=provider)
        conn.execute("UPDATE run_diffs SET panos_added = 99 WHERE diff_id = ?", (diff_id,))
        conn.commit()
        ids[(city_name, provider)] = diff_id
    return ids


def _repaired(conn, ids):
    return {key for key, i in ids.items() if _row(conn, i)["panos_added"] != 99}


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--provider", "gsv"], {("Bend", "gsv"), ("Salem", "gsv")}),
        (
            ["--provider", "mapillary,gsv"],
            {("Bend", "gsv"), ("Bend", "mapillary"), ("Salem", "gsv")},
        ),
        (["--city", "Salem, Oregon, United States"], {("Salem", "gsv")}),
        (["--city", "bend--oregon--united-states"], {("Bend", "gsv"), ("Bend", "mapillary")}),
        (
            ["--city", "Bend, Oregon, United States", "--provider", "mapillary"],
            {("Bend", "mapillary")},
        ),
        ([], {("Bend", "gsv"), ("Bend", "mapillary"), ("Salem", "gsv")}),
    ],
)
def test_filters_select_whole_series(conn, data_dir, three_stale_series, args, expected):
    assert _main(data_dir, "--execute", "--no-publish-json", *args) == 0
    assert _repaired(conn, three_stale_series) == expected


def test_diff_id_selects_only_the_named_row(conn, data_dir, three_stale_series):
    target = three_stale_series[("Bend", "mapillary")]
    assert _main(data_dir, "--execute", "--no-publish-json", "--diff-id", str(target)) == 0
    assert _repaired(conn, three_stale_series) == {("Bend", "mapillary")}


def test_an_unknown_city_or_provider_is_refused(conn, data_dir, three_stale_series):
    """An unknown provider is an argument error (2, argparse's); an unknown city
    needs the catalog to judge, and is a refusal (64, as `run-due --city`)."""
    with pytest.raises(SystemExit) as exc:
        _main(data_dir, "--execute", "--no-publish-json", "--provider", "bing")
    assert exc.value.code == 2
    assert _main(data_dir, "--execute", "--no-publish-json", "--city", "Atlantis") == (
        USAGE_EXIT_CODE
    )
    assert _repaired(conn, three_stale_series) == set()


def test_a_nonexistent_db_path_is_refused_and_not_created(data_dir, tmp_path):
    missing = tmp_path / "elsewhere" / "streetscape_tracker.db"
    missing.parent.mkdir()
    for mode in ([], ["--execute"]):
        assert _main(data_dir, "--db-path", str(missing), *mode) == USAGE_EXIT_CODE
        assert not missing.exists()


# ── published artifacts ─────────────────────────────────────────────────────


@pytest.mark.parametrize("regenerate", [True, False])
def test_regenerate_json_rewrites_the_change_block_and_only_under_the_flag(
    conn, data_dir, regenerate
):
    _, _, later, _, _ = _phantom_series(conn, data_dir)
    json_path = os.path.join(data_dir, regenerate_run_json(conn, later[0], data_dir))
    before = _read_bytes(json_path)

    flags = ["--regenerate-json"] if regenerate else []
    assert _main(data_dir, "--execute", "--no-publish-json", *flags) == 0
    with gzip.open(json_path, "rt") as fh:
        change = json.load(fh)["change_from_previous_run"]
    if regenerate:
        assert change["capture_date_changed"] == GENUINE["capture_date_changed"]
        assert change["panos_added"] == GENUINE["panos_added"]
    else:
        assert _read_bytes(json_path) == before
        assert change["capture_date_changed"] == 3  # the phantom, still published


def test_execute_rebuilds_the_aggregate_and_driving_plan(conn, data_dir):
    """Both read run_diffs straight from the catalog (the aggregate's change
    block via get_diff_for_run, the driving page's via get_latest_runs_all),
    so a repaired row must reach them without waiting for a run-due tail."""
    city_id, base, later, _, _ = _phantom_series(conn, data_dir)
    for run_id in (base[0], later[0]):
        regenerate_run_json(conn, run_id, data_dir)

    assert _main(data_dir, "--execute", "--regenerate-json") == 0
    with gzip.open(os.path.join(data_dir, "cities.json.gz"), "rt") as fh:
        aggregate = json.load(fh)
    (record,) = [c for c in aggregate["cities"] if c["city_id"] == city_id]
    assert record["providers"]["gsv"]["change"]["capture_date_changed"] == 1
    with gzip.open(os.path.join(data_dir, "driving_plan.json.gz"), "rt") as fh:
        plan = json.load(fh)
    (plan_city,) = [c for c in plan["cities"] if c["city_id"] == city_id]
    assert plan_city["observed"]["gsv"]["change"]["capture_date_changed"] == 1


# ── #403 review: deletion is guarded ────────────────────────────────────────


def _point_row_at(conn, diff_id, name):
    conn.execute("UPDATE run_diffs SET detail_filename = ? WHERE diff_id = ?", (name, diff_id))
    conn.commit()


def _phantom_series_for(conn, data_dir, city_name):
    """_phantom_series for a named city, returning (city_id, base, later, diff_id)."""
    city_id = _city(conn, city_name)
    base = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS, baseline=True)
    later = _run(conn, data_dir, city_id, D2, LATER_PANOS)
    return city_id, base, later, _record_as_old_collector(conn, data_dir, city_id, base, later)


def _salem_collector_row(conn, data_dir):
    salem = _city(conn, "Salem")
    a = _run(conn, data_dir, salem, D1, [("p1", "2020-05-01")])
    b = _run(conn, data_dir, salem, D2, [("p2", "2024-05-01")])
    return _record_as_collector(conn, data_dir, salem, a, b, D2)


def test_a_stray_pointer_at_a_run_csv_leaves_the_csv(conn, data_dir, capsys):
    """The blocking finding: a stored pointer naming another city's RUN CSV
    must never get that paid-for observation deleted."""
    _, _, _, diff_id, detail = _phantom_series(conn, data_dir)
    salem = _city(conn, "Salem")
    _, salem_csv = _run(conn, data_dir, salem, D1, [("s1", "2020-01-01")])
    _point_row_at(conn, diff_id, salem_csv)

    assert _main(data_dir, "--execute", "--no-publish-json") == 1
    assert os.path.exists(os.path.join(data_dir, salem_csv))
    assert _row(conn, diff_id)["detail_filename"] == os.path.basename(detail)
    out = capsys.readouterr().out
    assert "KEPT, NOT DELETED" in out and salem_csv in out
    assert f"remove these there:\n  {salem_csv}" not in out


def test_a_stray_pointer_at_another_rows_detail_leaves_it(conn, data_dir):
    """The second reproduction: a `--city Bend` pass must not delete Salem's
    published detail file, which Salem's own row still names."""
    _, _, _, bend_diff, _ = _phantom_series(conn, data_dir)
    salem_diff = _salem_collector_row(conn, data_dir)
    salem_detail = _row(conn, salem_diff)["detail_filename"]
    before = _read_bytes(os.path.join(data_dir, salem_detail))
    _point_row_at(conn, bend_diff, salem_detail)

    city = "Bend, Oregon, United States"
    assert _main(data_dir, "--execute", "--no-publish-json", "--city", city) == 1
    assert _read_bytes(os.path.join(data_dir, salem_detail)) == before
    assert _row(conn, salem_diff)["detail_filename"] == salem_detail


def test_an_unreferenced_diff_shaped_stray_is_removed(conn, data_dir, capsys):
    """The legitimate case: a stray pointer at a diff detail nothing else names
    is this row's leftover, and goes."""
    city_id, _, _, diff_id, detail = _phantom_series(conn, data_dir)
    stray = generate_diff_filename(city_id, D1.isoformat(), D2.isoformat())
    with gzip.open(os.path.join(data_dir, stray), "wt") as fh:
        fh.write("change_type\n")
    _point_row_at(conn, diff_id, stray)

    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    assert not os.path.exists(os.path.join(data_dir, stray))
    assert _row(conn, diff_id)["detail_filename"] == os.path.basename(detail)
    assert f"  {stray}" in capsys.readouterr().out  # listed for the web server


def test_a_deterministic_name_another_row_claims_is_left_untouched(conn, data_dir):
    _, _, _, diff_id, detail = _phantom_series(conn, data_dir)
    salem_diff = _salem_collector_row(conn, data_dir)
    _point_row_at(conn, salem_diff, os.path.basename(detail))
    before_row, before_file = _snapshot(conn, diff_id), _read_bytes(detail)

    assert _main(data_dir, "--execute", "--no-publish-json", "--diff-id", str(diff_id)) == 1
    assert _snapshot(conn, diff_id) == before_row
    assert _read_bytes(detail) == before_file


def test_a_failed_removal_fails_the_pass_and_records_no_file(conn, data_dir, monkeypatch):
    city_id = _city(conn)
    base = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS[:2], baseline=True)
    later = _run(conn, data_dir, city_id, D2, LATER_PANOS[:2])
    diff_id = _record_as_old_collector(conn, data_dir, city_id, base, later)
    detail = os.path.join(data_dir, _row(conn, diff_id)["detail_filename"])
    real_remove = os.remove

    def refuse(path, *args, **kwargs):
        if os.path.abspath(path) == os.path.abspath(detail):
            raise PermissionError("read-only for the test")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", refuse)
    assert _main(data_dir, "--execute", "--no-publish-json") == 1
    assert os.path.exists(detail)
    assert _row(conn, diff_id)["detail_filename"] is None


# ── #403 review: idempotence covers the pointer, the clock and the content ──


def test_a_wrong_pointer_with_right_counters_is_repointed(conn, data_dir):
    city_id = _city(conn)
    a = _run(conn, data_dir, city_id, D1, [("p1", "2020-05-01")])
    b = _run(conn, data_dir, city_id, D2, [("p2", "2024-05-01")])
    diff_id = _record_as_collector(conn, data_dir, city_id, a, b, D2)
    name = _row(conn, diff_id)["detail_filename"]
    _point_row_at(conn, diff_id, None)

    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    assert _row(conn, diff_id)["detail_filename"] == name


def test_a_changed_row_gets_a_fresh_computed_at(conn, data_dir):
    _, _, _, diff_id, _ = _phantom_series(conn, data_dir)
    conn.execute("UPDATE run_diffs SET computed_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    assert _row(conn, diff_id)["computed_at"] > "2026"


def test_right_counters_over_old_reader_content_rewrite_the_file(conn, data_dir, capsys):
    """The reviewer's reproduction: every baseline pano replaced, so nothing
    persisted and the counters agree under BOTH readers — but the old reader
    wrote each removed pano's old_capture_date blank."""
    city_id = _city(conn)
    base = _run(
        conn, data_dir, city_id, BASE, [("x1", "2022-09"), ("x2", "2021-03")], baseline=True
    )
    later = _run(conn, data_dir, city_id, D2, [("y1", standardize_capture_date("2024-05"))])
    diff_id = _record_as_old_collector(conn, data_dir, city_id, base, later)
    detail = os.path.join(data_dir, _row(conn, diff_id)["detail_filename"])
    assert ("pano_removed", "x1", "", "") in _detail_rows(detail)  # the stale content
    before = _snapshot(conn, diff_id)

    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    assert "content stale" in capsys.readouterr().out
    assert ("pano_removed", "x1", "2022-09-01", "") in _detail_rows(detail)
    after = _snapshot(conn, diff_id)
    assert {c: after[c] for c in COUNTERS} == {c: before[c] for c in COUNTERS}


# ── #403 review: the dry run writes nothing, catalog included ───────────────


def _tree(data_dir):
    """Every file in data_dir with its bytes and mtime."""
    out = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        out[name] = (_read_bytes(path), os.stat(path).st_mtime_ns)
    return out


def test_a_dry_run_leaves_data_byte_identical(conn, data_dir, capsys):
    """Over a row that recomputes to NO changes (the branch that removes a
    file under --execute) and one with changes, with --regenerate-json over a
    stale JSON: file list, bytes and mtimes unchanged, the catalog included,
    and no -wal/-shm left behind."""
    city_id = _city(conn)
    base = _run(conn, data_dir, city_id, BASE, BASELINE_PANOS[:2], baseline=True)
    later = _run(conn, data_dir, city_id, D2, LATER_PANOS[:2])
    _record_as_old_collector(conn, data_dir, city_id, base, later)
    _phantom_series_for(conn, data_dir, "Salem")
    regenerate_run_json(conn, later[0], data_dir)
    conn.execute("UPDATE run_diffs SET panos_added = 99 WHERE to_run_id = ?", (later[0],))
    conn.commit()  # so that JSON now disagrees with the catalog
    conn.close()  # the fixture's writer: its own sidecars would mask ours
    before = _tree(data_dir)
    assert not [n for n in before if n.endswith(("-wal", "-shm"))]

    assert _main(data_dir, "--regenerate-json") == 0
    out = capsys.readouterr().out
    assert "detail file remove" in out and "2 would change" in out
    assert "2 per-run JSON(s) already disagree" in out  # one stale, one missing
    assert _tree(data_dir) == before


def test_another_schema_version_is_refused_not_migrated(conn, data_dir):
    _phantom_series(conn, data_dir)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    db_path = os.path.join(data_dir, "streetscape_tracker.db")
    before = _read_bytes(db_path)
    for mode in ([], ["--execute"]):
        assert _main(data_dir, *mode) == USAGE_EXIT_CODE
        assert _read_bytes(db_path) == before


# ── #403 review: refuse beside a live batch ─────────────────────────────────


def test_execute_is_refused_while_a_run_due_is_in_flight(conn, data_dir, monkeypatch, caplog):
    _, _, _, diff_id, detail = _phantom_series(conn, data_dir)
    before_row, before_file = _snapshot(conn, diff_id), _read_bytes(detail)
    monkeypatch.setattr(recompute_module, "_run_due_in_flight", lambda: "pid 1: scheduler run-due")
    assert _main(data_dir, "--execute") == USAGE_EXIT_CODE
    assert _snapshot(conn, diff_id) == before_row
    assert _read_bytes(detail) == before_file
    assert _main(data_dir) == 0  # a dry run proceeds, and says why it is only that
    assert "in flight" in caplog.text


# ── #403 review: the published JSON is part of "unchanged" ──────────────────


def _published_change(conn, data_dir, run_id):
    name = conn.execute("SELECT json_filename FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    with gzip.open(os.path.join(data_dir, name), "rt") as fh:
        return json.load(fh)["change_from_previous_run"]


def test_a_pass_aborted_mid_way_is_healed_by_a_rerun(conn, data_dir, monkeypatch):
    """The reviewer's reproduction, made an abort the per-row handling cannot
    catch: Ctrl-C during the SECOND row's update. A clean re-run with
    --execute --regenerate-json leaves every catalog row and its published
    change block agreeing."""
    series = [_phantom_series_for(conn, data_dir, name) for name in ("Bend", "Salem")]
    for _, _, later, _ in series:
        regenerate_run_json(conn, later[0], data_dir)
    real_update = db.update_diff
    calls = []

    def interrupt_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_update(*args, **kwargs)

    monkeypatch.setattr(db, "update_diff", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        _main(data_dir, "--execute", "--regenerate-json", "--no-publish-json")
    monkeypatch.setattr(db, "update_diff", real_update)

    assert _main(data_dir, "--execute", "--regenerate-json", "--no-publish-json") == 0
    for _, _, later, diff_id in series:
        assert _row(conn, diff_id)["capture_date_changed"] == GENUINE["capture_date_changed"]
        published = _published_change(conn, data_dir, later[0])
        assert published["capture_date_changed"] == GENUINE["capture_date_changed"]


def test_a_row_update_that_raises_is_counted_and_the_pass_continues(conn, data_dir, monkeypatch):
    series = [_phantom_series_for(conn, data_dir, name) for name in ("Bend", "Salem")]
    real_update = db.update_diff
    calls = []

    def locked_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(db, "update_diff", locked_first)
    assert _main(data_dir, "--execute", "--no-publish-json") == 1
    # One row failed and kept its phantom, the other was repaired (in either order).
    assert sorted(_row(conn, d)["capture_date_changed"] for _, _, _, d in series) == [1, 3]


def test_execute_without_the_flag_then_with_it_heals_the_json(conn, data_dir):
    _, _, later, _, _ = _phantom_series(conn, data_dir)
    regenerate_run_json(conn, later[0], data_dir)
    assert _main(data_dir, "--execute", "--no-publish-json") == 0
    assert _published_change(conn, data_dir, later[0])["capture_date_changed"] == 3  # stale

    assert _main(data_dir, "--execute", "--no-publish-json", "--regenerate-json") == 0
    assert _published_change(conn, data_dir, later[0])["capture_date_changed"] == 1


def test_a_json_that_already_agrees_is_not_rebuilt(conn, data_dir, monkeypatch, capsys):
    _, _, later, _, _ = _phantom_series(conn, data_dir)
    assert _main(data_dir, "--execute", "--regenerate-json", "--no-publish-json") == 0
    name = conn.execute("SELECT json_filename FROM runs WHERE run_id = ?", (later[0],)).fetchone()[
        0
    ]
    mtime = os.stat(os.path.join(data_dir, name)).st_mtime_ns
    rebuilds = []
    monkeypatch.setattr(
        recompute_module, "regenerate_run_json", lambda *a, **k: rebuilds.append(a) or name
    )
    capsys.readouterr()

    assert _main(data_dir, "--execute", "--regenerate-json") == 0
    assert rebuilds == []
    assert os.stat(os.path.join(data_dir, name)).st_mtime_ns == mtime
    out = capsys.readouterr().out
    assert "Rebuilt 0 per-run JSON" in out
    assert "Regenerated aggregate" not in out  # nothing changed, nothing republished


def test_a_healed_json_alone_republishes_the_aggregate(conn, data_dir):
    """A pass that changed no row but rebuilt a JSON still rebuilds the
    aggregate, whose change block and age stats come from the same sources."""
    _, base, later, _, _ = _phantom_series(conn, data_dir)
    regenerate_run_json(conn, base[0], data_dir)
    assert _main(data_dir, "--execute", "--no-publish-json") == 0  # rows fixed, no JSON
    assert not os.path.exists(os.path.join(data_dir, "cities.json.gz"))
    assert _main(data_dir, "--execute", "--regenerate-json") == 0
    assert os.path.exists(os.path.join(data_dir, "cities.json.gz"))
