"""End-to-end test for scripts/recompute_run_stats.py (the v3 stats backfill).

Runs the real script as a subprocess against a fixture catalog: a run whose
stored stats predate the v3 "NO_DATE counts as present imagery" definition
gets rewritten to match analysis.calculate_run_stats, and a second dry run
confirms idempotence. This is the harness the next stats-definition bump
(v4) will rely on.
"""

import gzip
import json
import os
import subprocess
import sys
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.analysis import calculate_run_stats
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from tests.conftest import COLUMNS, make_city_df, write_city_csv_gz

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_PROJECT_ROOT, "scripts", "recompute_run_stats.py")


def _run_script(data_dir, *extra):
    return subprocess.run(
        [sys.executable, _SCRIPT, "--data-dir", data_dir, "--no-publish-json", *extra],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_recompute_rewrites_stale_stats_then_is_idempotent(conn, data_dir):
    run_date = date(2026, 4, 15)
    cid = db.register_city(
        conn,
        city_name="Bend",
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

    # Two dated panos + one ZERO_RESULTS point + one NO_DATE pano (the row
    # class whose accounting changed in v3)
    df = make_city_df([("p1", "2020-06-15"), ("p2", "2024-01-10")], run_date=run_date, n_empty=1)
    ts = df.iloc[0]["query_timestamp"]
    no_date_row = pd.DataFrame(
        [[44.9, -121.0, ts, 44.9001, -121.0001, "p_nodate", None, "© Google", "NO_DATE"]],
        columns=COLUMNS,
    )
    df = pd.concat([df, no_date_row], ignore_index=True)

    csv_name = "bend--oregon--united-states_width_1000_height_1000_step_20_2026-04-15.csv.gz"
    csv_path = os.path.join(data_dir, csv_name)
    write_city_csv_gz(df, csv_path)

    # Stored stats simulate the pre-v3 catalog: NO_DATE folded into
    # status_other, its pano missing from every total
    db.register_run(
        conn,
        city_id=cid,
        run_date=run_date,
        csv_filename=csv_name,
        total_points=4,
        status_ok=2,
        status_no_date=0,
        status_zero_results=1,
        status_other=1,
        unique_panos=2,
        unique_google_panos=2,
        coverage_rate_pct=50.0,
    )

    result = _run_script(data_dir, "--execute")
    assert result.returncode == 0, result.stderr

    expected = calculate_run_stats(load_city_csv_file(csv_path), run_date, provider="gsv")
    row = conn.execute(
        """SELECT total_points, status_ok, status_no_date, status_other,
                  unique_panos, unique_google_panos, coverage_rate_pct
           FROM runs WHERE city_id = ?""",
        (cid,),
    ).fetchone()
    assert row["status_no_date"] == expected["status_no_date"] == 1
    assert row["status_other"] == expected["status_other"] == 0
    assert row["unique_panos"] == expected["unique_panos"] == 3  # NO_DATE pano now counted
    assert row["coverage_rate_pct"] == expected["coverage_rate_pct"]
    assert row["total_points"] == expected["total_points"]

    # Second pass: nothing left to change
    rerun = _run_script(data_dir)
    assert rerun.returncode == 0, rerun.stderr
    assert "0 would change" in rerun.stdout


def test_recompute_repairs_impossible_capture_dates_and_rebuilds_json(conn, data_dir):
    """Issue #213's repair, end to end: a run whose CSV holds a pano dated 2611
    gets its catalog columns narrowed to what can be true, and — only under
    --regenerate-json — its PUBLISHED per-run JSON rebuilt, since that file
    carries the same corrupt value in its all_panos block and capture-year
    histogram and the city page reads it directly.

    The JSON repair is keyed on the CSV, not on whether the catalog changed:
    the second pass below finds nothing left to update and must still rebuild.

    The SCAN for affected runs is gated on the same flag, not just the rebuild:
    it is a second dedup + date-parse pass over every CSV in the catalog, and
    this loop is already the whole cost of the script.
    """
    run_date = date(2026, 4, 15)
    cid = db.register_city(
        conn,
        city_name="Bend",
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
    df = make_city_df(
        [("good", "2020-06-15"), ("corrupt", "2611-09-01")], run_date=run_date, n_empty=1
    )
    csv_name = "bend--oregon--united-states_width_1000_height_1000_step_20_2026-04-15.csv.gz"
    csv_path = os.path.join(data_dir, csv_name)
    write_city_csv_gz(df, csv_path)
    db.register_run(
        conn,
        city_id=cid,
        run_date=run_date,
        csv_filename=csv_name,
        oldest_capture_date="2020-06-15T00:00:00",
        newest_capture_date="2611-09-01T00:00:00",
        median_pano_age_years=-292.0,
    )
    json_path = csv_path.replace(".csv.gz", ".json.gz")

    # Pass 1: catalog only. Without the flag the published JSON is neither
    # inspected nor rebuilt, and the output says so rather than staying silent.
    result = _run_script(data_dir, "--execute")
    assert result.returncode == 0, result.stderr
    assert "Per-run JSONs not inspected" in result.stdout
    assert not os.path.exists(json_path)

    row = conn.execute(
        "SELECT oldest_capture_date, newest_capture_date, median_pano_age_years, json_filename "
        "FROM runs WHERE city_id = ?",
        (cid,),
    ).fetchone()
    assert row["newest_capture_date"] == "2020-06-15T00:00:00"
    assert row["oldest_capture_date"] == "2020-06-15T00:00:00"
    assert row["median_pano_age_years"] > 0  # 2611 made it negative
    assert row["json_filename"] is None

    # Pass 2: nothing left to recompute, but the JSON still needs rebuilding.
    result = _run_script(data_dir, "--execute", "--regenerate-json")
    assert result.returncode == 0, result.stderr
    assert "0 would change" in result.stdout
    assert "Rebuilt 1 of 1" in result.stdout

    with gzip.open(json_path, "rt", encoding="utf-8") as fh:
        published = json.load(fh)
    ages = published["all_panos"]["age_stats"]
    assert ages["newest_pano_date"] == "2020-06-15T00:00:00"
    assert published["all_panos"]["histogram_of_capture_dates_by_year"]["counts"] == {"2020": 1}
    # The corrupt pano is still IMAGERY — only its date was unusable
    assert published["all_panos"]["duplicate_stats"]["total_unique_panos"] == 2
    row = conn.execute("SELECT json_filename FROM runs WHERE city_id = ?", (cid,)).fetchone()
    assert row["json_filename"] == os.path.basename(json_path)

    # --provider scopes the scan; this catalog holds no Mapillary runs
    scoped = _run_script(data_dir, "--provider", "mapillary")
    assert scoped.returncode == 0, scoped.stderr
    assert "0 runs scanned" in scoped.stdout


def test_regenerated_json_keeps_the_change_block(conn, data_dir):
    """A rebuilt per-run JSON replays its change block from the catalog.

    regenerate_run_json's original caller only ever fires on runs with NO json
    at all (scheduler self-heal), where dropping the block is free. Issue #213's
    repair points it at runs whose JSON is complete and merely holds an
    impossible date, and there a dropped block is a regression the operator
    cannot undo before the city's next collection: city.js renders the
    "Since <date>: +N new / −N removed" panel straight from it, and falls back
    to CONSTRUCTING the diff detail filename from run history when it is absent
    — so a pair that legitimately published no detail file starts 404ing.
    """
    cid = db.register_city(
        conn,
        city_name="Bend",
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
    stem = "bend--oregon--united-states_width_1000_height_1000_step_20"
    run_ids = []
    for run_date, panos in (
        (date(2026, 1, 15), [("good", "2020-06-15")]),
        # The later run is the one carrying the corrupt date, i.e. the one the
        # repair rebuilds — and the one whose change block must survive.
        (date(2026, 4, 15), [("good", "2020-06-15"), ("corrupt", "2611-09-01")]),
    ):
        csv_name = f"{stem}_{run_date.isoformat()}.csv.gz"
        write_city_csv_gz(
            make_city_df(panos, run_date=run_date, n_empty=1),
            os.path.join(data_dir, csv_name),
        )
        run_ids.append(db.register_run(conn, city_id=cid, run_date=run_date, csv_filename=csv_name))
    db.record_diff(
        conn,
        city_id=cid,
        from_run_id=run_ids[0],
        to_run_id=run_ids[1],
        grid_aligned=True,
        panos_added=7,
        panos_removed=2,
        panos_persisted=11,
        capture_date_changed=3,
        points_gained_coverage=5,
        points_lost_coverage=1,
        coverage_delta_pct=1.25,
        detail_filename="bend--oregon--united-states_diff_2026-01-15_to_2026-04-15.csv.gz",
    )

    result = _run_script(data_dir, "--execute", "--regenerate-json")
    assert result.returncode == 0, result.stderr
    # BOTH runs are rebuilt, not only the one holding the corrupt date: their
    # catalog rows were registered with no stats at all, so issue #226's second
    # trigger — this pass MOVES the capture-date columns — fires for each. The
    # property under test is the change block surviving a rebuild, which the
    # later run still exercises.
    assert "Rebuilt 2 of 2" in result.stdout

    json_path = os.path.join(data_dir, f"{stem}_2026-04-15.json.gz")
    with gzip.open(json_path, "rt", encoding="utf-8") as fh:
        published = json.load(fh)
    change = published["change_from_previous_run"]
    assert change is not None, "the rebuild dropped the change block"
    assert change["from_run_date"] == "2026-01-15"
    assert change["panos_added"] == 7
    assert change["panos_removed"] == 2
    assert change["capture_date_changed"] == 3
    assert change["coverage_delta_pct"] == 1.25
    assert change["grid_aligned"] is True
    # diff_file is what stops city.js constructing (and 404ing on) a name.
    assert change["diff_file"].endswith("_diff_2026-01-15_to_2026-04-15.csv.gz")

    # The FIRST run has no diff to replay, and must publish no block rather
    # than a fabricated one — "nothing to compare against" is not "no change".
    first_json = os.path.join(data_dir, f"{stem}_2026-01-15.json.gz")
    if os.path.exists(first_json):
        with gzip.open(first_json, "rt", encoding="utf-8") as fh:
            assert json.load(fh)["change_from_previous_run"] is None


def test_recompute_republishes_the_driving_plan(conn, data_dir):
    """The repaired catalog columns reach the page that actually reads them.

    cities.json.gz takes its age stats from the per-run JSONs; the columns this
    script repairs (newest_capture_date, median_pano_age_years) are read
    DIRECTLY off `runs` by exactly one published artifact —
    generate_driving_plan_summary, which powers driving.html. Publishing only
    the aggregate would leave the driving page showing the pre-repair dates and
    the driven_unplanned verdicts derived from them.
    """
    cid = db.register_city(
        conn,
        city_name="Bend",
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
    run_date = date(2026, 4, 15)
    csv_name = "bend--oregon--united-states_width_1000_height_1000_step_20_2026-04-15.csv.gz"
    write_city_csv_gz(
        make_city_df([("good", "2020-06-15"), ("corrupt", "2611-09-01")], run_date=run_date),
        os.path.join(data_dir, csv_name),
    )
    db.register_run(
        conn,
        city_id=cid,
        run_date=run_date,
        csv_filename=csv_name,
        newest_capture_date="2611-09-01T00:00:00",
        median_pano_age_years=-292.0,
    )

    # NOTE: no --no-publish-json, unlike _run_script's default.
    result = subprocess.run(
        [sys.executable, _SCRIPT, "--data-dir", data_dir, "--execute"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert os.path.exists(os.path.join(data_dir, "cities.json.gz"))

    plan_path = os.path.join(data_dir, "driving_plan.json.gz")
    assert os.path.exists(plan_path), "driving_plan.json.gz was not republished"
    with gzip.open(plan_path, "rt", encoding="utf-8") as fh:
        plan = json.load(fh)
    observed = next(c for c in plan["cities"] if c["city_id"] == cid)["observed"]["gsv"]
    assert observed["newest_capture"] == "2020-06-15"
    # The median is derived from the same panos as the max, so the two travel
    # together: blanking one while rendering "-292.0 yrs" beside it would leave
    # the page contradicting itself.
    assert observed["median_pano_age_years"] > 0


def test_recompute_skips_runs_with_missing_csv(conn, data_dir):
    cid = db.register_city(
        conn,
        city_name="Ghost",
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
    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 4, 15),
        csv_filename="ghost--never-written.csv.gz",
        unique_panos=7,
    )

    result = _run_script(data_dir, "--execute")
    assert result.returncode == 0, result.stderr
    assert "1 skipped (missing CSV)" in result.stdout
    # The stored values survive untouched
    row = conn.execute("SELECT unique_panos FROM runs WHERE city_id = ?", (cid,)).fetchone()
    assert row["unique_panos"] == 7


def test_moved_capture_dates_rebuild_the_json_without_an_impossible_date(conn, data_dir):
    """Issue #226's repair: a run whose date columns MOVE gets its JSON rebuilt.

    The legacy pre-2026 runs carry month-precision capture dates, which the
    loader used to coerce to NaT — so their catalog columns AND their published
    per-run JSON were both computed with no dates at all, while every pano count
    came out perfect.

    What this pins is that the JSON rebuild does not depend on the run ALSO
    holding an impossible date. #213's trigger fires only on that, and whether
    an affected run happens to carry one is luck: measured across the 8 affected
    runs in one catalog, Lagos, Nakuru and La Piedad carry none. This fixture is
    that shape — every date plausible — and it must still be rebuilt, because
    the site's age display reads the per-run JSON rather than `runs`
    (json_summarizer._build_provider_summary takes all_panos_age_stats and the
    capture-year histogram straight from it).
    """
    run_date = date(2026, 4, 15)
    cid = db.register_city(
        conn,
        city_name="Bend",
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
    # Month precision, and every date comfortably inside the plausible window
    df = make_city_df([("p1", "2022-09"), ("p2", "2024-03")], run_date=run_date, n_empty=1)
    csv_name = "bend--oregon--united-states_width_1000_height_1000_step_20_2026-04-15.csv.gz"
    csv_path = os.path.join(data_dir, csv_name)
    write_city_csv_gz(df, csv_path)
    # The catalog as the broken loader left it: counts right, dates absent
    db.register_run(
        conn,
        city_id=cid,
        run_date=run_date,
        csv_filename=csv_name,
        unique_panos=2,
        unique_google_panos=2,
        oldest_capture_date=None,
        newest_capture_date=None,
        median_pano_age_years=None,
    )
    json_path = csv_path.replace(".csv.gz", ".json.gz")

    result = _run_script(data_dir, "--execute", "--regenerate-json")
    assert result.returncode == 0, result.stderr
    # Reported as a moved date, NOT as an impossible one — the two reasons are
    # counted separately so this cannot pass for the wrong reason.
    assert "0 hold an impossible capture date" in result.stdout
    assert "1 have capture-date columns this pass moves" in result.stdout
    assert "Rebuilt 1 of 1" in result.stdout

    row = conn.execute(
        "SELECT oldest_capture_date, newest_capture_date, median_pano_age_years "
        "FROM runs WHERE city_id = ?",
        (cid,),
    ).fetchone()
    assert row["oldest_capture_date"] == "2022-09-01T00:00:00"  # pinned to the 1st
    assert row["newest_capture_date"] == "2024-03-01T00:00:00"
    assert row["median_pano_age_years"] is not None

    with gzip.open(json_path, "rt", encoding="utf-8") as fh:
        published = json.load(fh)
    ages = published["all_panos"]["age_stats"]
    assert ages["oldest_pano_date"] == "2022-09-01T00:00:00"
    assert ages["newest_pano_date"] == "2024-03-01T00:00:00"
    assert ages["median_pano_age_years"] is not None
    assert published["all_panos"]["histogram_of_capture_dates_by_year"]["counts"] == {
        "2022": 1,
        "2024": 1,
    }


@pytest.mark.parametrize(
    "column, wrong_value, rebuilt",
    [
        ("oldest_capture_date", "2021-01-01T00:00:00", True),
        ("newest_capture_date", "2025-12-01T00:00:00", True),
        ("median_pano_age_years", 99.0, True),
        # Control: a NON-date column moving must NOT drag the JSON along. The
        # per-run JSON's age blocks are what trigger (2) exists to repair, and
        # #213's narrowing moves a pano/coverage stat for nearly every gsv run
        # — so a trigger that fired on any change at all would rebuild the
        # whole series every pass, for runs whose published dates are fine.
        ("unique_panos", 999, False),
    ],
)
def test_each_date_column_independently_triggers_the_json_rebuild(
    conn, data_dir, column, wrong_value, rebuilt
):
    """One stale column at a time — the case a realistic fixture cannot produce.

    scripts/recompute_run_stats.DATE_COLUMNS names three columns and the
    trigger is `any(c in changed for c in DATE_COLUMNS)`, so a misspelled or
    dropped entry is not an error: it is a condition that can never be true,
    and the rebuild silently stops happening for that column forever. Every
    natural fixture hides that, because oldest, newest and median are a min, a
    max and a median of ONE population and move together — so this test builds
    the unnatural case directly, registering a catalog row that is correct in
    every column but one.

    The module also refuses to import if DATE_COLUMNS drifts out of
    STAT_COLUMNS; the two guards catch different mistakes (a name that is not a
    stat column at all, versus a stat column quietly dropped from the trigger).
    """
    run_date = date(2026, 4, 15)
    cid = db.register_city(
        conn,
        city_name="Bend",
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
    # Every date plausible, so trigger (1) — "the CSV holds an impossible
    # capture date" — stays silent and cannot mask the column under test.
    df = make_city_df([("p1", "2020-06-15"), ("p2", "2024-01-10")], run_date=run_date, n_empty=1)
    csv_name = "bend--oregon--united-states_width_1000_height_1000_step_20_2026-04-15.csv.gz"
    csv_path = os.path.join(data_dir, csv_name)
    write_city_csv_gz(df, csv_path)

    # Register the run with the CORRECT stats, then spoil exactly one column.
    truth = calculate_run_stats(load_city_csv_file(csv_path), run_date, provider="gsv")
    stored = {**truth, column: wrong_value}
    db.register_run(conn, city_id=cid, run_date=run_date, csv_filename=csv_name, **stored)

    result = _run_script(data_dir, "--execute", "--regenerate-json")
    assert result.returncode == 0, result.stderr
    if rebuilt:
        # Reported as a MOVED date, never as an impossible one: the two reasons
        # are counted separately precisely so this cannot pass for the wrong
        # one, and this fixture's dates are all plausible.
        assert "0 hold an impossible capture date" in result.stdout
        assert "1 have capture-date columns this pass moves" in result.stdout
        assert "Rebuilt 1 of 1" in result.stdout
        assert os.path.exists(csv_path.replace(".csv.gz", ".json.gz"))
    else:
        assert "Rebuilt 0 of 0" in result.stdout
        assert not os.path.exists(csv_path.replace(".csv.gz", ".json.gz"))
    # Either way the catalog itself is repaired — the trigger governs the
    # PUBLISHED file, never whether the stats pass does its job.
    row = conn.execute("SELECT * FROM runs WHERE city_id = ?", (cid,)).fetchone()
    assert row[column] == truth[column]
