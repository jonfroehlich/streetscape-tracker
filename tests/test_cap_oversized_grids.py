"""Tests for scripts/cap_oversized_grids.py (issue #166)."""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from scripts.cap_oversized_grids import find_oversized, grid_points, main  # noqa: E402
from streetscape_metadata_tracker import db  # noqa: E402


@pytest.fixture
def db_path(data_dir):
    return os.path.join(data_dir, "streetscape_tracker.db")


def run_cli(monkeypatch, db_path, *args):
    """Invoke the script's main() with argv, returning its exit code.

    The script opens (and closes) its OWN connection from --db-path, so the
    test's `conn` fixture stays usable afterwards.
    """
    monkeypatch.setattr(
        "sys.argv", ["cap_oversized_grids.py", *[str(a) for a in args], "--db-path", db_path]
    )
    return main()


def _register(conn, name, width, height, step=20):
    return db.register_city(
        conn,
        city_name=name,
        state_name="Somewhere",
        state_code="SW",
        country_name="Testland",
        country_code="TL",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=width,
        grid_height_m=height,
        step_m=step,
    )


def _add_run(conn, city_id, *, is_baseline, run_date="2026-07-01"):
    db.register_run(
        conn,
        city_id=city_id,
        provider="gsv",
        run_date=date.fromisoformat(run_date),
        csv_filename=f"{city_id}_{run_date}.csv.gz",
        is_baseline=is_baseline,
        total_points=2,
    )


def _geometry(conn, city_id):
    row = conn.execute(
        "SELECT grid_width_m, grid_height_m, center_lat, center_lon, notes "
        "FROM cities WHERE city_id = ?",
        (city_id,),
    ).fetchone()
    return row


def test_only_cities_over_the_cap_are_selected(conn):
    small = _register(conn, "Small", width=10_000, height=10_000)
    tall = _register(conn, "Tall", width=10_000, height=60_000)
    huge = _register(conn, "Huge", width=100_000, height=99_000)

    found = {c["city_id"] for c in find_oversized(conn, 40_000)}

    assert small not in found, "a city inside the cap must be left alone"
    assert tall in found, "exceeding EITHER dimension counts as oversized"
    assert huge in found


def test_cap_clamps_each_dimension_independently_and_keeps_the_center(conn):
    _register(conn, "Tall", width=10_000, height=60_000)

    (c,) = find_oversized(conn, 40_000)

    # The dimension already under the cap is untouched — this is a clamp, not a
    # rescale to a square.
    assert c["new_width_m"] == 10_000
    assert c["new_height_m"] == 40_000
    assert (c["center_lat"], c["center_lon"]) == (44.0, -121.0)
    assert c["old_points"] == grid_points(10_000, 60_000, 20)
    assert c["new_points"] == grid_points(10_000, 40_000, 20)
    assert c["new_points"] < c["old_points"]


def test_largest_first(conn):
    _register(conn, "Big", width=50_000, height=50_000)
    _register(conn, "Biggest", width=100_000, height=100_000)
    _register(conn, "Medium", width=45_000, height=41_000)

    order = [c["old_points"] for c in find_oversized(conn, 40_000)]

    assert order == sorted(order, reverse=True), "worst offenders must come first"
    assert len(order) == 3


def test_dry_run_is_the_default_and_changes_nothing(conn, db_path, monkeypatch, capsys):
    city_id = _register(conn, "Huge", width=100_000, height=100_000)

    assert run_cli(monkeypatch, db_path) == 0

    assert _geometry(conn, city_id)["grid_width_m"] == 100_000
    assert "DRY RUN" in capsys.readouterr().out


def test_execute_resizes_a_never_collected_city(conn, db_path, monkeypatch):
    city_id = _register(conn, "Huge", width=100_000, height=99_000)

    assert run_cli(monkeypatch, db_path, "--execute") == 0

    row = _geometry(conn, city_id)
    assert row["grid_width_m"] == 40_000
    assert row["grid_height_m"] == 40_000
    # The audit trail has to say WHY the frozen geometry moved.
    assert "issue #166" in row["notes"]


def test_baseline_only_city_resizes_freely(conn, db_path, monkeypatch):
    """An imported baseline has no prior run to diff against, so re-gridding it
    costs nothing."""
    city_id = _register(conn, "Huge", width=100_000, height=100_000)
    _add_run(conn, city_id, is_baseline=True)

    assert run_cli(monkeypatch, db_path, "--execute") == 0

    assert _geometry(conn, city_id)["grid_width_m"] == 40_000


def test_city_with_real_runs_is_skipped_unless_explicitly_included(conn, db_path, monkeypatch):
    """Resizing orphans that city's run-to-run diffs, so it must be opt-in."""
    city_id = _register(conn, "Huge", width=100_000, height=100_000)
    _add_run(conn, city_id, is_baseline=False)

    assert run_cli(monkeypatch, db_path, "--execute") == 0
    assert _geometry(conn, city_id)["grid_width_m"] == 100_000, "must not resize silently"

    assert run_cli(monkeypatch, db_path, "--execute", "--include-collected") == 0
    assert _geometry(conn, city_id)["grid_width_m"] == 40_000


def test_a_collected_city_does_not_block_the_free_ones(conn, db_path, monkeypatch):
    """The skip is per-city: one uncollectable-but-collected city must not stop
    the rest of the frame from being fixed."""
    collected = _register(conn, "Collected", width=100_000, height=100_000)
    _add_run(conn, collected, is_baseline=False)
    free = _register(conn, "Free", width=90_000, height=90_000)

    assert run_cli(monkeypatch, db_path, "--execute") == 0

    assert _geometry(conn, free)["grid_width_m"] == 40_000
    assert _geometry(conn, collected)["grid_width_m"] == 100_000


def test_capped_cairo_fits_the_production_gsv_daily_budget(conn):
    """The operational point of the cap: Cairo's real geometry estimates
    ~10,547,202 requests against a 10,000,000 budget, so the scheduler has
    skipped it every night and never collected it once."""
    city_id = _register(conn, "Cairo", width=66_453, height=63_475, step=20)

    (c,) = find_oversized(conn, 40_000)

    assert c["city_id"] == city_id
    assert c["old_points"] > 10_000_000, "reproduces the unschedulable grid"
    assert c["new_points"] < 10_000_000, "capped grid must fit the daily budget"


def test_disabled_cities_are_left_alone(conn):
    city_id = _register(conn, "Huge", width=100_000, height=100_000)
    conn.execute("UPDATE cities SET enabled = 0 WHERE city_id = ?", (city_id,))
    conn.commit()

    assert find_oversized(conn, 40_000) == []


def test_include_disabled_finds_the_landmine_cities(conn):
    """A disabled city costs nothing tonight, but nothing else ever caps it —
    the registration clamp runs only at registration — so enabling one later
    reintroduces #166. --include-disabled is the way to reach them."""
    disabled = _register(conn, "Dormant", width=100_000, height=100_000)
    enabled = _register(conn, "Active", width=90_000, height=90_000)
    conn.execute("UPDATE cities SET enabled = 0 WHERE city_id = ?", (disabled,))
    conn.commit()

    default_run = find_oversized(conn, 40_000)
    assert [c["city_id"] for c in default_run] == [enabled]

    swept = find_oversized(conn, 40_000, include_disabled=True)
    assert sorted(c["city_id"] for c in swept) == sorted([disabled, enabled])
    assert {c["city_id"]: c["enabled"] for c in swept} == {disabled: False, enabled: True}
    assert all(c["new_width_m"] == 40_000 for c in swept)


def test_default_cap_matches_registration_ceiling():
    """The retroactive cap and cli.py's registration-time clamp must agree, or a
    city registered today could need capping tomorrow.

    The first equality holds by construction (``DEFAULT_MAX_EXTENT_M`` *is*
    ``cli.MAX_GRID_DIM_M``) and only guards against someone re-hardcoding it.
    The load-bearing half is ``== 40_000``: a deliberate change-detector on a
    number the production budget depends on, so moving it is a decision rather
    than an edit. If policy really changes, update this line with the reason.
    """
    from scripts.cap_oversized_grids import DEFAULT_MAX_EXTENT_M
    from streetscape_metadata_tracker.cli import MAX_GRID_DIM_M

    assert DEFAULT_MAX_EXTENT_M == MAX_GRID_DIM_M == 40_000
