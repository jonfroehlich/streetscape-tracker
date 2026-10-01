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
from datetime import date

import pandas as pd
import pytest

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


def test_an_unknown_city_or_provider_is_a_usage_error(conn, data_dir, three_stale_series):
    for args in (["--city", "Atlantis"], ["--provider", "bing"]):
        with pytest.raises(SystemExit) as exc:
            _main(data_dir, "--execute", "--no-publish-json", *args)
        assert exc.value.code == 2
    assert _repaired(conn, three_stale_series) == set()


def test_a_nonexistent_db_path_is_refused_and_not_created(data_dir, tmp_path):
    missing = tmp_path / "elsewhere" / "streetscape_tracker.db"
    missing.parent.mkdir()
    assert _main(data_dir, "--db-path", str(missing), "--execute") == 2
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
