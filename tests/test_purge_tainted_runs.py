"""Tests for scripts/purge_tainted_runs.py: identify runs whose snapshots
contain retryable-status rows (the 2026-07-16 quota-throttling incident) or
whose snapshot file has gone missing, and purge their catalog rows + files
(and re-arm scheduling) so the same run date can be re-collected.
"""

import os
from datetime import date

import pytest

from scripts.purge_tainted_runs import count_tainted_rows, find_runs_to_purge, purge_run
from streetscape_metadata_tracker import db
from tests.conftest import make_city_df, write_city_csv_gz


def _set_schedule_state(conn, city_id, provider, last_success_at):
    conn.execute(
        "INSERT INTO schedule_state (city_id, provider, day_of_cycle, last_success_at) "
        "VALUES (?, ?, 0, ?)",
        (city_id, provider, last_success_at),
    )
    conn.commit()


@pytest.fixture
def catalog(conn, data_dir):
    """One city with a clean 07-15 run and a throttle-tainted 07-16 run,
    linked by a diff with a published detail file, plus a schedule_state row
    stamped as if the tainted run succeeded."""
    city_id = db.register_city(
        conn,
        city_name="Bend",
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

    clean_csv = f"{city_id}_width_1000_height_1000_step_20_2026-07-15.csv.gz"
    write_city_csv_gz(make_city_df([("p1", "2020-05-01")]), os.path.join(data_dir, clean_csv))
    clean_run = db.register_run(
        conn, city_id=city_id, run_date=date(2026, 7, 15), csv_filename=clean_csv, total_points=2
    )

    tainted_df = make_city_df([("p1", "2020-05-01")], n_empty=2)
    tainted_df.loc[tainted_df.index[-1], "status"] = "OVER_QUERY_LIMIT"
    tainted_csv = f"{city_id}_width_1000_height_1000_step_20_2026-07-16.csv.gz"
    tainted_json = tainted_csv.replace(".csv.gz", ".json.gz")
    write_city_csv_gz(tainted_df, os.path.join(data_dir, tainted_csv))
    with open(os.path.join(data_dir, tainted_json), "w") as f:
        f.write("{}")
    tainted_run = db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 16),
        csv_filename=tainted_csv,
        json_filename=tainted_json,
        total_points=3,
    )

    detail = f"{city_id}_diff_2026-07-15_to_2026-07-16.csv.gz"
    with open(os.path.join(data_dir, detail), "w") as f:
        f.write("x")
    db.record_diff(
        conn,
        city_id=city_id,
        from_run_id=clean_run,
        to_run_id=tainted_run,
        grid_aligned=True,
        panos_added=0,
        panos_removed=0,
        panos_persisted=1,
        capture_date_changed=0,
        points_gained_coverage=0,
        points_lost_coverage=0,
        coverage_delta_pct=0.0,
        detail_filename=detail,
    )
    _set_schedule_state(conn, city_id, "gsv", "2026-07-16")
    return conn, data_dir, city_id, clean_run, tainted_run


def test_count_tainted_rows(catalog):
    conn, data_dir, city_id, clean_run, tainted_run = catalog
    clean = conn.execute("SELECT csv_filename FROM runs WHERE run_id = ?", (clean_run,)).fetchone()
    bad = conn.execute("SELECT csv_filename FROM runs WHERE run_id = ?", (tainted_run,)).fetchone()
    assert count_tainted_rows(os.path.join(data_dir, clean["csv_filename"])) == 0
    assert count_tainted_rows(os.path.join(data_dir, bad["csv_filename"])) == 1
    # Missing file is a distinct signal (None), not "clean" (0)
    assert count_tainted_rows(os.path.join(data_dir, "does-not-exist.csv.gz")) is None


def test_unknown_error_counts_as_tainted(catalog):
    """#7: UNKNOWN_ERROR is retryable, so it taints a snapshot too."""
    conn, data_dir, city_id, *_ = catalog
    df = make_city_df([("p1", "2020-05-01")], n_empty=1)
    df.loc[df.index[-1], "status"] = "UNKNOWN_ERROR"
    path = os.path.join(data_dir, "ue.csv.gz")
    write_city_csv_gz(df, path)
    assert count_tainted_rows(path) == 1


def test_find_runs_to_purge_only_flags_the_throttled_date(catalog):
    conn, data_dir, *_ = catalog
    assert find_runs_to_purge(conn, data_dir, "2026-07-15", None) == []
    items = find_runs_to_purge(conn, data_dir, "2026-07-16", None)
    assert len(items) == 1
    assert items[0]["reason"] == "tainted"
    assert items[0]["tainted"] == 1


def test_missing_snapshot_is_flagged_as_orphan(catalog):
    """#10: a run row whose csv.gz was hand-deleted is an orphan to purge,
    not a clean run to leave in place."""
    conn, data_dir, city_id, clean_run, tainted_run = catalog
    bad = conn.execute("SELECT csv_filename FROM runs WHERE run_id = ?", (tainted_run,)).fetchone()
    os.remove(os.path.join(data_dir, bad["csv_filename"]))
    items = find_runs_to_purge(conn, data_dir, "2026-07-16", "gsv")
    assert len(items) == 1
    assert items[0]["reason"] == "missing-file"


def test_dry_run_deletes_nothing(catalog):
    conn, data_dir, city_id, clean_run, tainted_run = catalog
    [item] = find_runs_to_purge(conn, data_dir, "2026-07-16", "gsv")
    purge_run(conn, data_dir, item["run"], item["reason"], execute=False)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM run_diffs").fetchone()[0] == 1
    assert os.path.exists(os.path.join(data_dir, item["run"]["csv_filename"]))
    # schedule_state untouched on a dry run
    row = conn.execute(
        "SELECT last_success_at FROM schedule_state WHERE city_id = ? AND provider = 'gsv'",
        (city_id,),
    ).fetchone()
    assert row["last_success_at"] == "2026-07-16"


def test_purge_removes_rows_and_files_but_keeps_clean_run(catalog):
    conn, data_dir, city_id, clean_run, tainted_run = catalog
    [item] = find_runs_to_purge(conn, data_dir, "2026-07-16", "gsv")
    run = item["run"]
    purge_run(conn, data_dir, run, item["reason"], execute=True)

    # Tainted run gone: rows and every file (snapshot, json, diff detail)
    assert conn.execute("SELECT * FROM runs WHERE run_id = ?", (tainted_run,)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM run_diffs").fetchone()[0] == 0
    assert not os.path.exists(os.path.join(data_dir, run["csv_filename"]))
    assert not os.path.exists(os.path.join(data_dir, run["json_filename"]))
    assert not os.path.exists(
        os.path.join(data_dir, f"{city_id}_diff_2026-07-15_to_2026-07-16.csv.gz")
    )

    # Clean run and frozen city geometry untouched
    clean = conn.execute("SELECT * FROM runs WHERE run_id = ?", (clean_run,)).fetchone()
    assert clean is not None
    assert os.path.exists(os.path.join(data_dir, clean["csv_filename"]))
    assert db.resolve_city(conn, city_id) is not None

    # The same (city, provider, run_date) can now be re-registered
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 16),
        csv_filename=run["csv_filename"],
        total_points=3,
    )


def test_purge_clears_schedule_state(catalog):
    """#9: last_success_at is cleared so a forgotten re-collect leaves the
    city due again rather than silently skipped for a full cycle."""
    conn, data_dir, city_id, clean_run, tainted_run = catalog
    [item] = find_runs_to_purge(conn, data_dir, "2026-07-16", "gsv")
    purge_run(conn, data_dir, item["run"], item["reason"], execute=True)
    row = conn.execute(
        "SELECT last_success_at FROM schedule_state WHERE city_id = ? AND provider = 'gsv'",
        (city_id,),
    ).fetchone()
    assert row["last_success_at"] is None


def test_purge_removes_downloading_siblings(catalog):
    """#3: a leftover same-date checkpoint (and its locks) is removed so a
    re-collect can't resume a pre-fix .downloading file."""
    conn, data_dir, city_id, clean_run, tainted_run = catalog
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (tainted_run,)).fetchone()
    downloading = os.path.join(data_dir, run["csv_filename"][: -len(".gz")] + ".downloading")
    siblings = [downloading, downloading + ".runlock", downloading + ".lock"]
    for s in siblings:
        with open(s, "w") as f:
            f.write("stale")
    [item] = find_runs_to_purge(conn, data_dir, "2026-07-16", "gsv")
    purge_run(conn, data_dir, item["run"], item["reason"], execute=True)
    for s in siblings:
        assert not os.path.exists(s)


def test_the_re_arm_says_so_when_the_pair_is_a_channel_member(catalog, caplog):
    """The default-membership case: the re-arm really does re-arm, so the log
    states it plainly and nothing warns.

    Membership gates BEFORE staleness in get_due_cities (#248), so this branch
    is what makes "clearing last_success_at makes the city due again" a true
    sentence rather than an assumed one — and gsv defaults to member, which is
    why the four scheduled channels see no change here at all.
    """
    import logging

    conn, data_dir, city_id, clean_run, tainted_run = catalog
    [item] = find_runs_to_purge(conn, data_dir, "2026-07-16", "gsv")
    with caplog.at_level(logging.INFO, logger="purge_tainted_runs"):
        purge_run(conn, data_dir, item["run"], item["reason"], execute=True)

    assert "clear schedule_state.last_success_at for gsv" in caplog.text
    assert "re-arms nothing" not in caplog.text


def test_the_re_arm_warns_when_the_pair_is_not_a_channel_member(catalog, caplog):
    """The opt-in case, and the reason this branch exists: on a non-member pair
    the UPDATE below succeeds and re-arms NOTHING.

    `get_due_cities` COALESCEs membership before it ever looks at staleness, so
    a NULLed last_success_at on a city nobody enrolled leaves it exactly as
    undue as it was. Logging a re-arm that did not happen is the same failure
    the re-arm itself exists to prevent — a missed re-collect hiding as a green
    skip — so it warns and names the handle instead.
    """
    import logging

    conn, data_dir, city_id, clean_run, tainted_run = catalog
    # Retarget the tainted run onto the one opt-in channel, nobody enrolled.
    conn.execute("UPDATE runs SET provider = 'kartaview' WHERE run_id = ?", (tainted_run,))
    conn.commit()

    [item] = find_runs_to_purge(conn, data_dir, "2026-07-16", "kartaview")
    with caplog.at_level(logging.INFO, logger="purge_tainted_runs"):
        purge_run(conn, data_dir, item["run"], item["reason"], execute=True)

    assert "is not a member of kartaview" in caplog.text
    assert "enroll-city" in caplog.text, "the warning has to name the handle that fixes it"
    assert "clear schedule_state.last_success_at" not in caplog.text


def test_an_enrolled_pair_on_an_opt_in_channel_re_arms_normally(catalog, caplog):
    """Enrolment flips the branch above, which is what keeps the warning a
    statement about THIS pair rather than about the channel."""
    import logging

    from streetscape_metadata_tracker import db as sdb

    conn, data_dir, city_id, clean_run, tainted_run = catalog
    conn.execute("UPDATE runs SET provider = 'kartaview' WHERE run_id = ?", (tainted_run,))
    conn.commit()
    sdb.set_channel_membership(conn, city_id, "kartaview", True, cycle_days=90)

    [item] = find_runs_to_purge(conn, data_dir, "2026-07-16", "kartaview")
    with caplog.at_level(logging.INFO, logger="purge_tainted_runs"):
        purge_run(conn, data_dir, item["run"], item["reason"], execute=True)

    assert "clear schedule_state.last_success_at for kartaview" in caplog.text
    assert "re-arms nothing" not in caplog.text
