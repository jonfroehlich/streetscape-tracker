"""Scheduler logic tests — pure logic only, no network or subprocesses."""

import contextlib
import dataclasses
import gzip
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import scheduler as _sched
from streetscape_metadata_tracker.alerting import AlertConfig
from streetscape_metadata_tracker.naming import (
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_metadata_tracker.scheduler import (
    ProviderConfig,
    ResourceGuardConfig,
    SchedulerConfig,
    SystemPressure,
    _reconcile_orphaned_run,
    _reconcile_orphaned_walk,
    build_parser,
    cmd_fetch_driving_plan,
    cmd_reconcile_walks,
    estimate_requests,
    load_scheduler_config,
    plan_connection_limit,
)
from tests.conftest import make_city_df, write_city_csv_gz

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The real nightly hooks, saved before the autouse stubs below replace them so
# the dedicated driving-plan / backup tests can exercise them.
_REAL_DRIVING_PLAN_HOOK = _sched._fetch_driving_plan_nightly
_REAL_WRITE_BACKUP = _sched.catalog_backup.write_backup
_REAL_BACKUP_HOOK = _sched._backup_catalog_nightly
_REAL_PLAN_SUMMARY = _sched.generate_driving_plan_summary


@pytest.fixture(autouse=True)
def _no_driving_plan_fetch(monkeypatch):
    """run-due snapshots the driving-plan feed before the city loop (issue
    #176). Stub the hook for every test so the suite stays hermetic — no
    network, no writes to the real archive/ dir; the driving-plan tests
    restore _REAL_DRIVING_PLAN_HOOK explicitly."""
    monkeypatch.setattr(_sched, "_fetch_driving_plan_nightly", lambda cfg, conn, today: None)


@pytest.fixture(autouse=True)
def _no_real_catalog_backup(monkeypatch):
    """
    run-due backs the catalog up before the city loop AND in the tail (issue
    #145). Stub it for every test, because SchedulerConfig's backup_dir defaults
    to <repo>/backups: without this, every test reaching either hook writes a
    real backup of its fixture catalog into the developer's working tree. That
    is not hypothetical — the tail backup predates this fixture and had been
    dropping fixture-sized files into the repo's logs/ for as long as it
    existed, where they were indistinguishable from a real catalog backup.

    The dedicated backup tests restore _REAL_WRITE_BACKUP and point backup_dir
    at tmp_path.
    """
    monkeypatch.setattr(
        _sched.catalog_backup,
        "write_backup",
        lambda conn, backup_dir, when, **kw: _sched.catalog_backup.BackupResult(
            ok=True, path=os.path.join(backup_dir, "stubbed.backup")
        ),
    )


@pytest.fixture(autouse=True)
def _no_driving_plan_summary(monkeypatch):
    """
    The tail regenerates driving_plan.json.gz UNCONDITIONALLY — deliberately
    not gated on `succeeded > 0`, since Google's feed changes on its own
    schedule and gating would leave the published plan stale on exactly the
    quiet nights. That means every run-due test reaches it, and
    SchedulerConfig's data_dir defaults to <repo>/data, so without this stub
    the suite writes a fixture-sized artifact into the developer's working
    tree — the same hazard _no_real_catalog_backup exists for, and the reason
    the writer now creates its parent directory rather than failing.

    The dedicated driving-plan tests restore _REAL_PLAN_SUMMARY and point
    data_dir at tmp_path.
    """
    monkeypatch.setattr(
        _sched, "generate_driving_plan_summary", lambda conn, data_dir: {"records": []}
    )


def _register(conn, name, width=5000, height=5000, step=20):
    return db.register_city(
        conn,
        city_name=name,
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=width,
        grid_height_m=height,
        step_m=step,
    )


def _stub_collection(
    sched,
    monkeypatch,
    conn,
    ran,
    *,
    outcome=None,
    record_usage=False,
    slept=None,
):
    """
    Stand in for one collection subprocess plus the batch tail.

    Every ``cmd_run_due`` test needs the same five substitutions, so they live
    here rather than being copied per test — the copies had already drifted (some
    simulated the ledger write, some did not), which is how a ``--limit`` test
    ended up unable to exercise the budget guard.

    - ``ran`` collects ``(city_id, provider)`` in the order they were launched.
    - ``outcome(city, provider) -> bool`` fakes per-channel success; default True.
    - ``record_usage`` writes the estimated requests to ``api_usage``, which is
      what the real pipeline does and what any budget assertion depends on.
    - ``slept`` collects ``sleep_between_cities_s`` calls, for the tests that care
      whether a capped run pauses after its last city.
    """
    outcome = outcome or (lambda city, provider: True)
    conn_ = conn

    def fake_run(
        cfg,
        city,
        run_today,
        provider="gsv",
        connection_limit=None,
        daily_budget=0,
        conn=None,
        remaining_s=None,
        **_,
    ):
        # The fixture connection from the CLOSURE, not the `conn` parameter: the
        # scheduler hands a lane worker `conn=None` on purpose, because
        # db.connect opens the catalog check_same_thread=True and only the main
        # thread may touch it (issue #240). The ledger write this fake stands in
        # for really is the child's own, and the child has its own handle.
        if record_usage:
            db.add_api_usage(conn_, run_today, sched.estimate_requests(city, provider), provider)
        ran.append((city.city_id, provider))
        return outcome(city, provider)

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(
        sched.time, "sleep", (lambda s: slept.append(s)) if slept is not None else (lambda s: None)
    )
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})


def test_run_one_city_command_defers_skip_policy_to_scheduler(conn, monkeypatch, tmp_path):
    """
    The scheduler already decided this city is due (cycle − grace), so the
    subprocess must run with --min-days-since-last-run 0: otherwise any
    config with cycle_days − grace_days ≤ the CLI default (80) makes every
    run "succeed" as a skip — stamping last_success_at while never
    collecting anything. The city name must also follow '--' so a display
    name starting with '-' can't be parsed as a flag.
    """
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend")
    city = db.resolve_city(conn, cid)

    captured = {}

    def fake_run(cmd, timeout=None, cwd=None, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    assert sched._run_one_city(
        SchedulerConfig(log_dir=str(tmp_path)), city, date(2026, 7, 1), "gsv"
    )

    cmd = captured["cmd"]
    i = cmd.index("--min-days-since-last-run")
    assert cmd[i + 1] == "0"
    # Client-side quota pacing must reach every subprocess.
    i = cmd.index("--max-requests-per-minute")
    assert cmd[i + 1] == "24000"
    assert cmd[cmd.index("--") + 1] == city.display_name
    assert cmd[-1] == city.display_name


def test_estimate_requests_matches_grid_math(conn):
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    assert estimate_requests(city) == 51 * 51  # (1000//20 + 1)^2


def test_estimate_requests_mapillary_counts_tiles(conn):
    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    tiles = estimate_requests(city, provider="mapillary")
    # A 5km grid is a handful of z14 tiles — three orders of magnitude
    # cheaper than GSV's per-point requests
    assert 4 <= tiles <= 25
    assert tiles < estimate_requests(city) / 100


def test_city_timeout_scales_with_grid_size(conn):
    """A huge GSV grid gets a timeout derived from points ÷ rate (so it is not
    SIGKILLed mid-run by the flat floor); a small city keeps the floor."""
    from streetscape_metadata_tracker.scheduler import (
        _ACHIEVED_RATE_FRACTION,
        _TIMEOUT_FIXED_SLACK_S,
        _TIMEOUT_HEADROOM,
        city_timeout_seconds,
    )

    cfg = SchedulerConfig(city_timeout_minutes=180, max_requests_per_minute=24_000)
    floor = 180 * 60

    small = db.resolve_city(conn, _register(conn, "Bend", width=1000, height=1000, step=20))
    assert city_timeout_seconds(cfg, small, "gsv") == floor  # 2601 pts, well under floor

    big = db.resolve_city(conn, _register(conn, "Metropolis", width=40000, height=40000, step=20))
    pts = estimate_requests(big)  # (40000//20 + 1)^2 = 4_004_001
    # The timeout budgets for the *achieved* rate, not the pacing ceiling: at
    # 24k/min the engine really sustains ~12k/min, so paced time roughly doubles
    # and must clear the flat floor with headroom for the diff/JSON tail.
    effective_rate = cfg.max_requests_per_minute * _ACHIEVED_RATE_FRACTION
    expected = int(pts / effective_rate * 60.0 * _TIMEOUT_HEADROOM + _TIMEOUT_FIXED_SLACK_S)
    assert city_timeout_seconds(cfg, big, "gsv") == expected
    assert expected > floor


def test_city_timeout_covers_observed_austin_download(conn):
    """Regression for the Austin timeout bug (#3599 investigation): at makelab2's
    real config (48k/min cap, ~24.6k achieved) an Austin-sized grid must derive a
    timeout well above the ~170-min download that used to eat the whole 180-min
    floor and get SIGKILLed during the diff/JSON tail."""
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    cfg = SchedulerConfig(city_timeout_minutes=180, max_requests_per_minute=48_000)
    austin = db.resolve_city(conn, _register(conn, "Austin", width=36189, height=46350, step=20))
    # Observed download was ~170 min; require comfortable margin over 240 min so
    # the whole pipeline (download + diff + JSON) fits.
    assert city_timeout_seconds(cfg, austin, "gsv") > 240 * 60


def test_city_timeout_floor_for_mapillary_and_disabled_pacing(conn):
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    big = db.resolve_city(conn, _register(conn, "Metropolis", width=40000, height=40000, step=20))
    floor = 180 * 60
    # A 40 km grid is ~550 z14 tiles: minutes even at the paced 60/min, so the
    # flat floor still covers it comfortably. (What does NOT is a grid several
    # times that — see the Anchorage-shaped case below.)
    assert city_timeout_seconds(SchedulerConfig(), big, "mapillary") == floor
    # No client-side pacing -> no basis to scale, keep the floor.
    assert city_timeout_seconds(SchedulerConfig(max_requests_per_minute=0), big, "gsv") == floor


def _register_at(conn, name, lat, lon, width, height, step=20):
    return db.register_city(
        conn,
        city_name=name,
        state_name=None,
        state_code=None,
        country_name="United States",
        country_code="US",
        center_lat=lat,
        center_lon=lon,
        grid_width_m=width,
        grid_height_m=height,
        step_m=step,
    )


@pytest.mark.parametrize("provider", ["mapillary", "mapillary_streets"])
def test_a_paced_tile_census_outgrows_the_flat_timeout(conn, provider):
    """Pacing (issue #198) made Mapillary wall-clock scale with tile count, so
    the timeout has to see it.

    Anchorage's frozen grid is 105 x 84 km at latitude 61, where a z14 tile is
    only ~1.2 km across: ~6,500 tiles, i.e. ~108 minutes of deliberate sleeping
    at 60/min before the decode/assignment/CSV tail even starts. The old flat
    180-minute floor left that a coin flip, and losing it costs the requests
    already spent AND counts a failure.

    Both channels, because they read the identical census — the road walk joins
    it onto sample points locally rather than issuing more requests.
    """
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    city = db.resolve_city(
        conn, _register_at(conn, "Anchorage", 61.2, -149.9, width=105588, height=83676)
    )
    cfg = SchedulerConfig(providers={provider: ProviderConfig(max_requests_per_minute=60)})
    floor = 180 * 60

    derived = city_timeout_seconds(cfg, city, provider)
    assert derived > floor, "a 6,500-tile census must not be squeezed into the flat floor"
    # And it must cover the pacing itself with room for the tail.
    paced_seconds = 6_480 / 60 * 60
    assert derived > paced_seconds


def test_a_paced_tile_census_uses_the_channels_own_rate(conn):
    """Halving the configured rate doubles the pacing, so the timeout must
    follow the channel's own figure rather than a constant."""
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    city = db.resolve_city(
        conn, _register_at(conn, "Anchorage", 61.2, -149.9, width=105588, height=83676)
    )
    fast = city_timeout_seconds(
        SchedulerConfig(providers={"mapillary": ProviderConfig(max_requests_per_minute=120)}),
        city,
        "mapillary",
    )
    slow = city_timeout_seconds(
        SchedulerConfig(providers={"mapillary": ProviderConfig(max_requests_per_minute=30)}),
        city,
        "mapillary",
    )
    assert slow > fast


def test_an_unpaced_mapillary_channel_keeps_the_flat_floor(conn):
    """0 disables pacing, which leaves nothing to derive a duration from."""
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    city = db.resolve_city(
        conn, _register_at(conn, "Anchorage", 61.2, -149.9, width=105588, height=83676)
    )
    cfg = SchedulerConfig(providers={"mapillary": ProviderConfig(max_requests_per_minute=0)})
    assert city_timeout_seconds(cfg, city, "mapillary") == 180 * 60


# ── KartaView: the sweep's own cost arms (issue #238) ──────────────────────
#
# The channel is still refused by UNWIRED_CHANNELS, so none of these run in a
# real night. They drive SchedulerConfig directly, which is what the config
# loader's refusal cannot reach and exactly how the Mapillary cases above work.


def _kv_cfg(rate=None, **overrides):
    """A config carrying a kartaview channel, bypassing the loader's refusal."""
    pc = ProviderConfig(enabled=True, daily_request_budget=20_000, max_requests_per_minute=rate)
    return SchedulerConfig(providers={"kartaview": pc}, **overrides)


def test_the_sweep_estimate_is_the_lattice_not_the_grid_formula(conn):
    """The fail-open arm this replaces: estimate_requests fell through to the
    GSV grid formula, pricing a bbox the sweep covers in a handful of circles
    as one request per 20 m grid point — wrong by three orders of magnitude,
    and wrong in BOTH directions since it also ignores the sweep's overhead."""
    from streetscape_metadata_tracker.download_kartaview import estimate_sweep_requests
    from streetscape_metadata_tracker.scheduler import _SWEEP_OVERHEAD_MULTIPLIER

    # Ithaca MI, the catalog's median city, sized to the study's 19.7 km2. The
    # study's own bbox is not square, so it lattices to 12 root circles where
    # this square one gives 16 — quote the fixture's number here, not the
    # study's, and note 16 is ALSO the study's observed cost for Ithaca, which
    # is a coincidence of this fixture and not agreement.
    city = db.resolve_city(conn, _register_at(conn, "Ithaca", 43.3, -84.6, 4440, 4440))

    lattice = estimate_sweep_requests(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    assert estimate_requests(city, "kartaview") == int(lattice * _SWEEP_OVERHEAD_MULTIPLIER)

    grid_points = estimate_requests(city, "gsv")
    assert grid_points > 40_000, "the geometry this used to be priced at"
    assert estimate_requests(city, "kartaview") < grid_points / 100


def test_the_sweep_estimate_carries_the_measured_overhead(conn):
    """The lattice is a FLOOR: it counts one page-1 per root circle and prices
    neither the extra pages, the backpressure retries nor the per-city
    calibration ladder. The study measured the real cost at a median 1.80x it
    (observed_over_root_cells.p50), and the guard has to carry that or it is
    systematically under on exactly the cities it exists to protect."""
    from streetscape_metadata_tracker.download_kartaview import estimate_sweep_requests
    from streetscape_metadata_tracker.scheduler import _SWEEP_OVERHEAD_MULTIPLIER

    # NOT observed_over_floor (1.54x): that is measured against a different
    # denominator — the study's floor counts cells PLUS pages 2+, where
    # estimate_sweep_requests counts cells alone.
    assert _SWEEP_OVERHEAD_MULTIPLIER == pytest.approx(1.80)

    # Milwaukee, the study's p95 city, sized to its 737.6 km2 — 400 root
    # circles as a square bbox, against 384 for the study's own shape. What is
    # being pinned is that 636 OBSERVED requests are covered; the bare lattice
    # (400) would not cover them and the multiplier is the whole difference.
    city = db.resolve_city(conn, _register_at(conn, "Milwaukee", 43.0, -87.9, 27160, 27160))
    assert estimate_requests(city, "kartaview") >= 636
    assert (
        estimate_sweep_requests(
            city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
        )
        < 636
    ), "the bare lattice must be the thing that falls short, or this pins nothing"


def test_a_prior_sweeps_observed_cost_beats_the_geometry(conn):
    """Radius is a 4x lever on the whole cost and the up-front lattice cannot
    see it: it must assume the default r=1000, while Singapore, New York and
    Manila all calibrate down to r=500. Nothing durable stores that radius —
    the checkpoint pins it for one sweep and cli.py discards it once the run is
    cataloged — but runs.api_requests already holds the sweep's OBSERVED total,
    which carries the radius, the pages, the retries and the ladder at once."""
    # Sized to the study's Singapore: 50.47 km square == 2,547 km2, which the
    # lattice covers in 1,296 circles at the default r=1000. The dimensions
    # matter — the ~4x this test is named for is only visible if the fixture
    # really is the city the study measured.
    cid = _register_at(conn, "Singapore", 1.35, 103.8, 50470, 50470)
    city = db.resolve_city(conn, cid)

    geometric = estimate_requests(city, "kartaview", conn=conn)
    # The ratio, not a round ceiling: 9,974 spent against ~2,332 estimated is
    # the r=1000-vs-r=500 lever itself, so pin it as such and let the test fail
    # if the geometry tier moves under it.
    assert 3.5 < 9_974 / geometric < 5.0, (
        f"r=1000 geometry should under-price this r=500 city ~4x; got {9_974 / geometric:.2f}x"
    )

    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 1),
        csv_filename="s.csv.gz",
        provider="kartaview",
        api_requests=9_974,  # the study's measured Singapore sweep
    )
    assert estimate_requests(city, "kartaview", conn=conn) == 9_974
    # Without a connection there is no prior run to read, so it falls back.
    assert estimate_requests(city, "kartaview") == geometric


def test_a_prior_sweeps_cost_never_outranks_a_grid_that_grew_under_it(conn):
    """The prior describes the bbox as it was THEN, and this is the one channel
    whose cost tracks bbox AREA directly.

    Frozen geometry is mutable through two documented escape hatches
    (`resize_city.py --force`, `cap_oversized_grids.py --include-collected`),
    and every other arm of estimate_requests recomputes from today's geometry on
    every call — so a prior-run tier that ignored a resize would go on pricing a
    bbox that no longer exists. Under-pricing is the direction that costs: it
    hands the child a timeout derived from the smaller sweep, which is the
    SIGKILL-with-no-ledger-row that #238 exists to prevent, reached from inside
    the fix rather than by the old fall-through.

    Pinned as the max() rather than as either tier, because the prior still has
    to WIN wherever the grid did not grow — that is the whole point of tier 1.
    """
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    cid = _register_at(conn, "Smallville", 40.0, -80.0, 4440, 4440)
    small = db.resolve_city(conn, cid)
    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 1),
        csv_filename="s.csv.gz",
        provider="kartaview",
        # Above this bbox's geometric tier, so tier 1 is genuinely in force.
        api_requests=60,
    )
    assert estimate_requests(small, "kartaview") < 60, "the premise: geometry is the lower one"
    assert estimate_requests(small, "kartaview", conn=conn) == 60

    # Same city, grid re-registered 100x larger in area.
    db.update_city_geometry(
        conn,
        city_id=cid,
        center_lat=40.0,
        center_lon=-80.0,
        grid_width_m=44_400,
        grid_height_m=44_400,
    )
    grown = db.resolve_city(conn, cid)

    geometry = estimate_requests(grown, "kartaview")
    assert geometry > 1_000, "a 100x bbox is worth three figures of circles"
    assert estimate_requests(grown, "kartaview", conn=conn) == geometry, (
        "the stale prior priced the OLD bbox and won, so the sweep is timed for a "
        "city 100x smaller than the one it will actually walk"
    )
    # And the consequence the estimate exists to prevent, end to end.
    assert city_timeout_seconds(_kv_cfg(), grown, "kartaview", conn=conn) > 180 * 60


def test_a_metro_sweep_outgrows_the_flat_timeout(conn):
    """The defect in #238: a sweep is paced at 16 req/min and SERIAL, so a
    metro is hours of deliberate waiting — Singapore's ~9,974 requests are
    ~10.4 h — and the flat 180-minute floor SIGKILLed it part-way through.
    That is worse than a plain failure twice over: a killed child records NO
    api_usage, so every request it already spent vanishes from the daily
    ledger, and it burns one of the five consecutive_failures that nothing but
    a success resets."""
    from streetscape_metadata_tracker.download_kartaview import (
        DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
    )
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    cid = _register_at(conn, "Singapore", 1.35, 103.8, 50470, 50470)
    city = db.resolve_city(conn, cid)
    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 1),
        csv_filename="s.csv.gz",
        provider="kartaview",
        api_requests=9_974,
    )
    floor = 180 * 60
    derived = city_timeout_seconds(_kv_cfg(), city, "kartaview", conn=conn)

    assert derived > floor, "a ~10 h sweep must not be squeezed into the flat floor"
    # Pin both walls rather than the number: it must cover the sweep's own
    # paced wall-clock, and the premise must be re-measured if the pace moves.
    paced_seconds = 9_974 / DEFAULT_SWEEP_REQUESTS_PER_MINUTE * 60
    assert paced_seconds / 3600 < 11, "re-measure this test's premise, not just the constant"
    assert derived > paced_seconds


def test_the_sweep_child_is_timed_with_the_catalog_handle_not_without_it(
    conn, monkeypatch, tmp_path
):
    """`_run_one_city`'s grid arm has to pass `conn` down, and for KartaView
    that is load-bearing rather than cosmetic.

    The street arm has always passed it and the grid arm never did, which was
    harmless while `conn` was read only by the `gsv_streets` estimate. The
    KartaView tier that reads a previous sweep's observed cost changes that:
    without the handle the estimate silently drops to default-radius geometry,
    which under-prices an r=500 metro roughly fourfold — and the only symptom
    would be a SIGKILL months later.

    Asserted end-to-end through `_run_one_city` rather than on
    `city_timeout_seconds` directly, because the seam that can break is the
    argument-passing, not the derivation.
    """
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register_at(conn, "Singapore", 1.35, 103.8, 50470, 50470)
    city = db.resolve_city(conn, cid)
    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 1),
        csv_filename="s.csv.gz",
        provider="kartaview",
        api_requests=9_974,
    )

    seen = {}

    def fake_run(cmd, timeout=None, cwd=None, **kwargs):
        seen["timeout"] = timeout

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    cfg = dataclasses.replace(_kv_cfg(), log_dir=str(tmp_path))
    sched._run_one_city(cfg, city, date(2026, 7, 2), "kartaview", conn=conn)

    geometry_only = sched.city_timeout_seconds(cfg, city, "kartaview")
    assert seen["timeout"] > geometry_only, (
        "the child was timed from default-radius geometry, so the catalog handle "
        "never reached the derivation"
    )
    assert seen["timeout"] == sched.city_timeout_seconds(cfg, city, "kartaview", conn=conn)


def test_a_median_city_sweep_keeps_the_flat_floor(conn):
    """The derivation never drops below the configured floor, so the median
    catalog city — 12 circles, under a minute of fetching — is unaffected."""
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    city = db.resolve_city(conn, _register_at(conn, "Ithaca", 43.3, -84.6, 4440, 4440))
    assert city_timeout_seconds(_kv_cfg(), city, "kartaview", conn=conn) == 180 * 60


def test_a_sweep_timeout_uses_the_channels_own_rate(conn):
    """Halving the configured pace doubles the waiting, so the timeout has to
    follow the channel's own figure rather than a constant."""
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    city = db.resolve_city(conn, _register_at(conn, "Vegas", 36.2, -115.1, 49130, 49130))
    fast = city_timeout_seconds(_kv_cfg(rate=32), city, "kartaview", conn=conn)
    slow = city_timeout_seconds(_kv_cfg(rate=8), city, "kartaview", conn=conn)
    assert slow > fast


def test_an_unpaced_sweep_channel_keeps_the_flat_floor(conn):
    """0 disables pacing, which leaves nothing to derive a duration from —
    `is None`, not falsy, exactly as the Mapillary arm reads it."""
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    city = db.resolve_city(conn, _register_at(conn, "Vegas", 36.2, -115.1, 49130, 49130))
    assert city_timeout_seconds(_kv_cfg(rate=0), city, "kartaview", conn=conn) == 180 * 60


def test_the_sweep_budgets_a_lower_achieved_rate_than_the_tile_census(conn):
    """Deliberately BELOW the Mapillary fraction, which is the opposite of the
    intuition that a serial walk tracks its limiter more closely.

    The tile census is concurrent, so per-request latency hides behind other
    requests in flight and the limiter binds. The sweep is serial by design, so
    its wall-clock per request is max(pacing_interval, latency) with nothing to
    overlap — and at 16/min the interval is only 3.75 s against a page carrying
    up to 2,000 photo records, so latency can be the binding term instead."""
    from streetscape_metadata_tracker.scheduler import (
        _SWEEP_ACHIEVED_RATE_FRACTION,
        _TILE_ACHIEVED_RATE_FRACTION,
    )

    assert _SWEEP_ACHIEVED_RATE_FRACTION < _TILE_ACHIEVED_RATE_FRACTION


def test_the_sweep_channel_is_ordered_by_decision_not_by_the_rank_fallback():
    """`rank.get(p, 99)` used to place kartaview last by accident. It still
    sorts last, but now because the table says so.

    Deliberately pinned as the POSITION and not as a rationale for it: four
    rationales for this ordering have been wrong, and docs/scheduler.md holds
    them and what each got wrong. The durable rule is "most expensive first,
    except where truncation is cheapest to absorb" — a multi-hour sweep would
    otherwise consume the deadline and leave its cheap siblings clamped to
    `_MIN_CLAMPED_TIMEOUT_S`, and it is the one channel #239 checkpoints, so it
    is the one that can absorb being cut short."""
    cfg = SchedulerConfig(
        providers={
            "kartaview": ProviderConfig(enabled=True),
            "gsv": ProviderConfig(enabled=True),
            "mapillary": ProviderConfig(enabled=True),
        }
    )
    assert cfg.enabled_providers() == ["gsv", "mapillary", "kartaview"]

    # And it is the TABLE saying so, not the fallback: a name the table really
    # does not know still sorts after kartaview. Under the old rank.get(p, 99)
    # the two would have tied at 99 and fallen back to alphabetical order,
    # putting "aardvark" first.
    cfg.providers["aardvark"] = ProviderConfig(enabled=True)
    assert cfg.enabled_providers()[-1] == "aardvark"


def _orphan_run(conn, data_dir, *, run_date=date(2026, 4, 15), write_csv=True):
    """A cataloged run with json_filename=NULL, mimicking a subprocess killed in
    the pipeline tail after register_run committed. When write_csv is False the
    CSV is absent (the run is unrecoverable)."""
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    csv_filename = "bend--oregon--united-states_width_1000_height_1000_step_20_2026-04-15.csv.gz"
    if write_csv:
        df = make_city_df([("p1", "2020-06-15"), ("p2", "2024-01-10")], run_date=run_date)
        write_city_csv_gz(df, os.path.join(data_dir, csv_filename))
    run_id = db.register_run(
        conn,
        city_id=cid,
        run_date=run_date,
        csv_filename=csv_filename,
        provider="gsv",
        json_filename=None,  # the defect: tail was killed before JSON
        total_points=3,
        status_ok=2,
    )
    return db.resolve_city(conn, cid), run_id, run_date


def test_reconcile_rebuilds_missing_json_for_cataloged_run(conn, data_dir):
    """A subprocess 'failure' that nonetheless cataloged a valid run is salvaged:
    the missing per-run JSON is rebuilt from the CSV and the run counts as a
    success (the Austin bug, automated)."""
    cfg = SchedulerConfig(data_dir=data_dir)
    city, run_id, run_date = _orphan_run(conn, data_dir)

    assert _reconcile_orphaned_run(conn, cfg, city, "gsv", run_date) is True

    row = conn.execute("SELECT json_filename FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["json_filename"]  # now populated
    assert os.path.exists(os.path.join(data_dir, row["json_filename"]))


def test_reconcile_no_row_is_genuine_failure(conn, data_dir):
    """No run row for (city, provider, today) → nothing to salvage; the caller
    must record a real failure."""
    cfg = SchedulerConfig(data_dir=data_dir)
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    assert _reconcile_orphaned_run(conn, cfg, city, "gsv", date(2026, 4, 15)) is False


def test_reconcile_missing_csv_is_genuine_failure(conn, data_dir):
    """A run row exists but its CSV is gone → cannot rebuild JSON, so it is a
    real failure rather than a false success."""
    cfg = SchedulerConfig(data_dir=data_dir)
    city, run_id, run_date = _orphan_run(conn, data_dir, write_csv=False)
    assert _reconcile_orphaned_run(conn, cfg, city, "gsv", run_date) is False
    row = conn.execute("SELECT json_filename FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert not row["json_filename"]  # still unrepaired


def test_config_defaults_when_file_missing(tmp_path):
    cfg = load_scheduler_config(str(tmp_path / "nope.toml"))
    assert cfg.cycle_days == 90 and cfg.batch_size == 100
    assert cfg.max_requests_per_minute == 24_000
    assert cfg.db_path.endswith("streetscape_tracker.db")
    # No [providers] config → gsv-only with the legacy budget
    assert cfg.enabled_providers() == ["gsv"]
    assert cfg.providers["gsv"].daily_request_budget == 10_000_000


def test_config_parses_toml(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text("""
[schedule]
cycle_days = 30
daily_request_budget = 1000
[download]
batch_size = 7
max_requests_per_minute = 48000
[publish]
enabled = true
""")
    cfg = load_scheduler_config(str(p))
    assert cfg.cycle_days == 30
    assert cfg.daily_request_budget == 1000
    assert cfg.batch_size == 7
    assert cfg.max_requests_per_minute == 48000
    assert cfg.publish_enabled
    # v1-style toml (no [providers]): gsv-only, legacy budget honored
    assert cfg.enabled_providers() == ["gsv"]
    assert cfg.providers["gsv"].daily_request_budget == 1000


def test_config_parses_provider_sections(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text("""
[schedule]
cycle_days = 30
[providers.gsv]
daily_request_budget = 99000
[providers.mapillary]
enabled = true
daily_request_budget = 5000
[providers.bogus]
daily_request_budget = 1
""")
    cfg = load_scheduler_config(str(p))
    assert cfg.enabled_providers() == ["gsv", "mapillary"]  # gsv always first
    assert cfg.providers["gsv"].daily_request_budget == 99_000
    assert cfg.providers["mapillary"].daily_request_budget == 5000
    assert "bogus" not in cfg.providers  # unknown providers are ignored


def test_config_rejects_an_unknown_network_type(tmp_path):
    """A bad network_type must not reach the collector's argparse choices.

    `collect --network-type` validates its argument, so an unknown value (a
    typo, or the osmnx-1.x name 'all_private') exits 2 on EVERY street run of
    EVERY due city, night after night, with nothing in the scheduler's output
    naming the config as the cause. Fall back to the default series instead.
    """
    p = tmp_path / "s.toml"
    p.write_text("""
[providers.gsv_streets]
network_type = "all_private"
[providers.mapillary_streets]
network_type = "all_public"
""")
    cfg = load_scheduler_config(str(p))
    assert cfg.providers["gsv_streets"].network_type == "drive"
    # A valid non-default type still comes through untouched.
    assert cfg.providers["mapillary_streets"].network_type == "all_public"


def test_config_provider_can_be_disabled(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text("""
[providers.gsv]
daily_request_budget = 99000
[providers.mapillary]
enabled = false
""")
    cfg = load_scheduler_config(str(p))
    assert cfg.enabled_providers() == ["gsv"]


def test_config_flag_accepted_on_either_side_of_subcommand():
    # --config is global; a prior systemd unit put it AFTER the subcommand, which
    # argparse rejected and would have failed the nightly service. It must now
    # parse on both sides. See build_parser / _add_global_flags.
    parser = build_parser()

    before = parser.parse_args(["--config", "/x.toml", "run-due", "--dry-run"])
    assert before.command == "run-due" and before.config == "/x.toml" and before.dry_run

    after = parser.parse_args(["run-due", "--config", "/x.toml", "--dry-run"])
    assert after.command == "run-due" and after.config == "/x.toml" and after.dry_run

    # Works for a plain subcommand after the flag too.
    assert parser.parse_args(["--config", "/y.toml", "status"]).config == "/y.toml"

    # Omitted entirely: SUPPRESS leaves the attr absent so main() falls back to None.
    assert getattr(parser.parse_args(["status"]), "config", None) is None


def test_regenerate_aggregate_parses_publish_flag():
    parser = build_parser()
    a = parser.parse_args(["regenerate-aggregate"])
    assert a.command == "regenerate-aggregate" and a.publish is False
    b = parser.parse_args(["--config", "/x.toml", "regenerate-aggregate", "--publish"])
    assert b.command == "regenerate-aggregate" and b.publish and b.config == "/x.toml"


def test_regenerate_aggregate_rebuilds_without_publish(conn, monkeypatch):
    """regenerate-aggregate rebuilds the aggregate and, without --publish,
    never touches the publish script."""
    from streetscape_metadata_tracker import scheduler as sched

    calls = {"agg": 0, "publish": 0}
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(
        sched,
        "generate_aggregate_v2",
        lambda c, d: calls.__setitem__("agg", calls["agg"] + 1) or {"cities_count": 3},
    )
    monkeypatch.setattr(
        sched,
        "generate_streetwalk_manifest",
        lambda c, d: calls.__setitem__("manifest", calls.get("manifest", 0) + 1) or {"walks": []},
    )
    monkeypatch.setattr(sched, "_publish", lambda cfg, ctx: calls.__setitem__("publish", 1) or 0)

    rc = sched.cmd_regenerate(SchedulerConfig(publish_enabled=False))
    # The streetwalk manifest is rebuilt alongside the aggregate: both are
    # catalog-derived indexes the frontend fetches, and regenerate-aggregate is
    # the documented recovery path after a manual/killed run (issue #155).
    assert rc == 0 and calls == {"agg": 1, "manifest": 1, "publish": 0}


def test_regenerate_aggregate_publishes_on_flag(conn, monkeypatch):
    """--publish runs the publish step even when [publish].enabled is false,
    and a publish failure surfaces as a nonzero exit."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: {"cities_count": 0})
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    published = []
    monkeypatch.setattr(sched, "_publish", lambda cfg, ctx: published.append(ctx) or 0)
    assert sched.cmd_regenerate(SchedulerConfig(publish_enabled=False), publish=True) == 0
    assert published  # publish ran despite publish_enabled=False

    monkeypatch.setattr(sched, "_publish", lambda cfg, ctx: 1)  # simulate rsync failure
    assert sched.cmd_regenerate(SchedulerConfig(publish_enabled=False), publish=True) == 1


def test_regenerate_aggregate_reports_a_failed_driving_plan_rebuild(conn, monkeypatch, capsys):
    """
    The driving-plan join is failure-guarded so an OSError there cannot cost the
    caller its publish (#167) — but rebuilding the published JSON is this
    command's whole job, so "two of three" must not exit 0 to a wrapper. It still
    publishes the two it did rebuild.
    """
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: {"cities_count": 3})
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    monkeypatch.setattr(
        sched,
        "generate_driving_plan_summary",
        lambda c, d: (_ for _ in ()).throw(OSError("disk full")),
    )
    published = []
    monkeypatch.setattr(sched, "_publish", lambda cfg, ctx: published.append(ctx) or 0)

    rc = sched.cmd_regenerate(SchedulerConfig(publish_enabled=False), publish=True)

    assert rc == 1
    assert published  # the two artifacts that DID rebuild still reach the site
    assert "driving_plan.json.gz NOT regenerated" in capsys.readouterr().out


def test_the_repo_default_config_declares_kartaview_as_an_opt_in_channel():
    """The flip, pinned in the file it actually lands in (issue #248).

    Two halves, and the second is the one that makes the first safe: the
    channel is declared, AND it is opt-in, so declaring it enrols nobody. A
    default-membership kartaview would put all 1,144 enabled cities in its
    nightly queue at ~186,000 requests a pass — the thing this whole issue
    exists to prevent — and nothing else in the config would say so.

    The budget is pinned exactly because it is not a round number: it clears
    the largest city the cost study measured (Singapore, ~9,974), and a city
    whose estimate exceeds the daily budget is skipped PERMANENTLY rather than
    deferred (issue #274).
    """
    from streetscape_metadata_tracker.scheduler import (
        DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
        is_opt_in_channel,
    )

    cfg = load_scheduler_config(os.path.join(_PROJECT_ROOT, "config", "scheduler.toml"))
    assert "kartaview" in cfg.enabled_providers()
    assert is_opt_in_channel("kartaview"), "declaring it must not enrol the catalog"
    assert cfg.providers["kartaview"].daily_request_budget == 10_000
    # _kartaview_timeout_seconds derives the per-city timeout from this rate,
    # so it is not only a pacing figure.
    assert cfg.providers["kartaview"].max_requests_per_minute == DEFAULT_SWEEP_REQUESTS_PER_MINUTE


def test_a_declared_kartaview_channel_still_collects_nothing_until_a_city_is_enrolled(
    conn, monkeypatch
):
    """The flip's whole safety property, end to end through a night.

    The config is enabled, the channel is priced, ranked and paced — and the
    nightly queue is empty, because membership defaults to off. This is what
    lets the repo default carry the block at all.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    ran = []
    _run_loop_with(
        monkeypatch,
        conn,
        _sweep_cfg(publish_enabled=False),
        lambda city, provider: ran.append(provider) or True,
        today=date(2026, 7, 2),
    )
    assert "kartaview" not in ran
    assert sched.db.count_channel_members(conn, "kartaview", False) == 0


def test_makelab1_production_config_is_wired():
    # Guard the checked-in production config the systemd unit points at.
    #
    # This file is what prod actually reads (config/scheduler.toml is the
    # annotated repo default and is NOT deployed), so enabling a channel in
    # scheduler.toml alone changes nothing in production — the two must be kept
    # in step deliberately, which is what this assertion is for.
    cfg = load_scheduler_config(os.path.join(_PROJECT_ROOT, "config", "scheduler.makelab1.toml"))
    # Order is canonical, not alphabetical: most expensive first, EXCEPT kartaview,
    # which ranks LAST because a multi-hour sweep absorbs deadline truncation most
    # cheaply (#238). Asserting the list therefore pins the launch order too.
    #
    # Both Mapillary channels are PAUSED as of 2026-08-28 (third per-IP block), so
    # they are configured-but-disabled and drop out of this list. Pinned as an
    # absence rather than deleted: resuming them must be a deliberate edit here.
    assert cfg.enabled_providers() == ["gsv", "gsv_streets", "kartaview"]
    for paused in ("mapillary", "mapillary_streets"):
        assert paused in cfg.providers, f"{paused} block must survive the pause"
        assert not cfg.providers[paused].enabled
    # The pause is about the mechanism, not the sizing — the budgets stay at the
    # values #241/#267 argued for so that resuming is one flag, not a re-derivation.
    assert cfg.providers["mapillary"].daily_request_budget == 1_750
    assert cfg.providers["mapillary_streets"].daily_request_budget == 1_750
    # kartaview was turned on in production on 2026-08-28, the separate deploy
    # decision this assertion previously withheld (#248). Enabling the CHANNEL
    # enrols nobody: it is the one opt-in channel, so the nightly queue is exactly
    # the cities `scheduler enroll-city` names, and the seed set is deliberately
    # two (Krabi, Yogyakarta) because a whole-catalog pass is ~186,000 requests.
    # The budget is a FLOOR to clear rather than a ceiling on the night's spend
    # (#273/#274) — a city whose estimate exceeds it is skipped permanently — so
    # this figure is pinned exactly, like the Mapillary ones below, and re-checked
    # before any metro is enrolled.
    assert cfg.providers["kartaview"].daily_request_budget == 10_000
    # Load-bearing beyond pacing: _kartaview_timeout_seconds derives every sweep's
    # per-city timeout from this rate, so lowering it shortens those timeouts too.
    assert cfg.providers["kartaview"].max_requests_per_minute == 16
    # Channel concurrency is OFF in production until both of #240's deploy gates
    # clear: resume for the Mapillary tile census (#256), because a stop now kills
    # N children at once and a killed census re-spends tiles into a per-IP ceiling
    # we have already been blocked by twice; and a console check that the two GSV
    # keys live in separate Cloud projects, since neither channel holds a per-IP
    # lock. Pinned here rather than left implicit so raising it is a deliberate
    # edit to this test and this file together — the same reason the Mapillary
    # budgets above are pinned exactly.
    assert cfg.max_concurrent_channels == 1
    # The street channels must keep their ISOLATED budgets: metered under their
    # own api_usage provider strings against separate keys, so a road crawl can
    # never eat the grid collectors' quota.
    assert cfg.providers["gsv_streets"].daily_request_budget == 3_000_000
    # Paced by the streets key's own quota, not [download]'s 48k grid pacing.
    assert cfg.providers["gsv_streets"].max_requests_per_minute == 24_000
    # The Mapillary budgets are NOT derived from the documented 50,000/day
    # per-app cap — both blocks matched that limit in no attribute (per IP not
    # per app, at 21% and 10% of it, 302 not 4xx). #214's bet that the 60/min
    # pace was the real protection was FALSIFIED on 2026-08-20 (issue #241:
    # blocked while obeying it exactly, at 5,013/day the day after 5,753 ran
    # clean), and only a rolling 2-3 day per-IP window fits both incidents
    # (2-day threshold in (7,061, 10,766]). Split EVENLY because both channels
    # read the identical z14 tile census, so the budgets deplete in lockstep
    # and a heavy slate defers the same cities on both channels rather than
    # un-pairing them. Pinned exactly, not bounded
    # loosely: this is the sort of number that drifts upwards one "just a bit
    # more" at a time, and the whole point is that a change to it is a
    # decision someone made on purpose.
    mly = cfg.providers["mapillary"].daily_request_budget
    mly_streets = cfg.providers["mapillary_streets"].daily_request_budget
    assert mly == 1_750
    assert mly_streets == 1_750
    assert mly + mly_streets == 3_500, (
        "the tile block is per IP, so the two channels' budgets SUM — the "
        "2026-08-22 cut keeps any 2-day total at <= 7,000, at or below the "
        "highest value ever observed clean (7,061), because only a rolling "
        "2-3 day window fits both blocks (issue #241, see CLAUDE.md)"
    )
    # The per-minute pace is still pinned — an unpaced burst (~370/min) is
    # confirmed harmful, and a constant peak rate is what made the two
    # incidents comparable — but per #241 it is NOT sufficient on its own.
    # Both channels draw on one per-IP rate, so both carry the same figure.
    assert cfg.providers["mapillary"].max_requests_per_minute == 60
    assert cfg.providers["mapillary_streets"].max_requests_per_minute == 60
    assert cfg.publish_enabled
    assert cfg.publish_script.endswith("sync_data_to_server.sh")
    # smtp transport (not "mail"): the local mailer is blocked by the systemd
    # sandbox, so alerts go straight to the campus relay (issue #144).
    assert cfg.alerts.enabled and cfg.alerts.transport == "smtp" and cfg.alerts.recipient
    assert cfg.alerts.smtp_host and cfg.alerts.smtp_from
    # Data/DB live on lab storage (makelab2), not in the web docroot.
    assert "/projects/makeabilitylab/streetscape-tracker" in cfg.db_path
    assert "/cse/web/" not in cfg.db_path and "/cse/web/" not in cfg.data_dir
    # Shared-host resource guard is active in production.
    assert cfg.resource_guard.enabled
    # Nightly driving-plan snapshot (issue #176): on, and archived on lab
    # storage OUTSIDE data/ so the publish rsync never sees it.
    assert cfg.driving_plan.enabled
    assert "/archive/gsv_driving_plan" in cfg.driving_plan.archive_dir
    assert not cfg.driving_plan.archive_dir.startswith(cfg.data_dir)
    # The batch deadline must stay strictly below the unit's TimeoutStartSec, or
    # systemd kills the loop first and the night publishes nothing (#167). These
    # two live in different files and only mean something together.
    unit = Path(_PROJECT_ROOT, "deploy", "systemd", "streetscape-tracker.service").read_text()
    unit_timeout_h = int(re.search(r"^TimeoutStartSec=(\d+)h", unit, re.M).group(1))
    assert cfg.max_batch_hours < unit_timeout_h, (
        f"max_batch_hours={cfg.max_batch_hours} must be under TimeoutStartSec={unit_timeout_h}h"
    )


def test_run_one_city_honors_connection_limit_override(conn, monkeypatch, tmp_path):
    """The resource guard lowers concurrency by passing a connection_limit
    override, which must reach the subprocess as --connection-limit."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend")
    city = db.resolve_city(conn, cid)
    captured = {}

    def fake_run(cmd, timeout=None, cwd=None, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    assert sched._run_one_city(
        SchedulerConfig(log_dir=str(tmp_path)), city, date(2026, 7, 1), "gsv", connection_limit=7
    )
    cmd = captured["cmd"]
    assert cmd[cmd.index("--connection-limit") + 1] == "7"


def test_plan_connection_limit_no_pressure_keeps_base():
    # No /proc data (non-Linux, read failure) → never throttle.
    assert plan_connection_limit(50, None, ResourceGuardConfig()) == (50, None)


def test_plan_connection_limit_disabled_is_noop():
    cfg = ResourceGuardConfig(enabled=False)
    starved = SystemPressure(load5=999.0, ncpu=8, mem_available_gb=0.1)
    assert plan_connection_limit(50, starved, cfg) == (50, None)


def test_plan_connection_limit_healthy_box_keeps_base():
    cfg = ResourceGuardConfig()
    healthy = SystemPressure(load5=1.0, ncpu=48, mem_available_gb=100.0)
    assert plan_connection_limit(50, healthy, cfg) == (50, None)


def test_plan_connection_limit_low_memory_drops_to_floor():
    cfg = ResourceGuardConfig(min_available_memory_gb=8.0, min_connection_limit=5)
    tight = SystemPressure(load5=0.0, ncpu=48, mem_available_gb=2.0)
    limit, reason = plan_connection_limit(50, tight, cfg)
    assert limit == 5
    assert "low memory" in reason


def test_plan_connection_limit_high_load_scales_proportionally():
    # ceiling = 0.9 * 10 = 9; load 18 is 2× over → half the base.
    cfg = ResourceGuardConfig(max_load_per_core=0.9, min_connection_limit=5)
    busy = SystemPressure(load5=18.0, ncpu=10, mem_available_gb=64.0)
    limit, reason = plan_connection_limit(50, busy, cfg)
    assert limit == 25  # int(50 * 9 / 18)
    assert "high load" in reason


def test_plan_connection_limit_never_below_floor():
    cfg = ResourceGuardConfig(min_connection_limit=5)
    extreme = SystemPressure(load5=10_000.0, ncpu=8, mem_available_gb=100.0)
    limit, _ = plan_connection_limit(50, extreme, cfg)
    assert limit == 5


def test_plan_connection_limit_no_reason_when_limit_unchanged():
    # base already <= floor: "throttling" can't lower it, so no reason/no-op log.
    cfg = ResourceGuardConfig(min_connection_limit=5)
    starved = SystemPressure(load5=9999.0, ncpu=8, mem_available_gb=0.1)
    assert plan_connection_limit(3, starved, cfg) == (3, None)


def test_read_system_pressure_returns_none_when_proc_unavailable(monkeypatch):
    import builtins

    from streetscape_metadata_tracker import scheduler as sched

    def boom(*a, **k):
        raise OSError("no /proc here")

    monkeypatch.setattr(builtins, "open", boom)
    assert sched.read_system_pressure() is None


def test_config_parses_resource_guard(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(
        "[resource_guard]\n"
        "enabled = true\n"
        "min_available_memory_gb = 12.0\n"
        "max_load_per_core = 0.5\n"
        "min_connection_limit = 3\n"
    )
    cfg = load_scheduler_config(str(p))
    assert cfg.resource_guard.enabled
    assert cfg.resource_guard.min_available_memory_gb == 12.0
    assert cfg.resource_guard.max_load_per_core == 0.5
    assert cfg.resource_guard.min_connection_limit == 3


def test_config_resource_guard_defaults_on(tmp_path):
    cfg = load_scheduler_config(str(tmp_path / "nope.toml"))
    assert cfg.resource_guard.enabled is True
    assert cfg.resource_guard.min_connection_limit == 5


def test_due_cities_stalest_first(conn):
    a = _register(conn, "Alpha")
    b = _register(conn, "Beta")
    c = _register(conn, "Gamma")
    db.assign_schedule(conn, 90)

    # Beta succeeded long ago; Gamma succeeded recently; Alpha never ran
    conn.execute(
        "UPDATE schedule_state SET last_success_at = '2025-01-01T00:00:00+00:00' WHERE city_id = ?",
        (b,),
    )
    conn.execute(
        "UPDATE schedule_state SET last_success_at = '2026-06-30T00:00:00+00:00' WHERE city_id = ?",
        (c,),
    )
    conn.commit()

    due = db.get_due_cities(
        conn,
        today=date(2026, 7, 2),
        cycle_days=90,
        grace_days=7,
        max_consecutive_failures=5,
        default_membership=True,
    )
    ids = [x.city_id for x in due]
    assert ids[0] == a  # never-run first (NULL last_success)
    assert ids[1] == b  # then stalest
    assert c not in ids  # fresh city not due


def test_disabled_city_never_due(conn):
    cid = _register(conn, "Alpha")
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE cities SET enabled = 0 WHERE city_id = ?", (cid,))
    conn.commit()
    due = db.get_due_cities(
        conn,
        today=date(2026, 7, 2),
        cycle_days=90,
        grace_days=7,
        max_consecutive_failures=5,
        default_membership=True,
    )
    assert due == []


def test_failure_cap_excludes_city(conn):
    cid = _register(conn, "Alpha")
    db.assign_schedule(conn, 90)
    for _ in range(5):
        db.record_attempt(conn, cid, success=False, error="x")
    due = db.get_due_cities(
        conn,
        today=date(2026, 7, 2),
        cycle_days=90,
        grace_days=7,
        max_consecutive_failures=5,
        default_membership=True,
    )
    assert due == []


def test_budget_ledger_defers_second_city_when_first_consumes_budget(conn, monkeypatch):
    """The remaining-budget check reads the LIVE api_usage ledger: after city
    A's run records its requests, city B (same size) no longer fits today and
    is deferred — not run over budget, and not marked as a failure."""
    from streetscape_metadata_tracker import scheduler as sched

    today = date(2026, 7, 2)
    # Each 1000x1000/20 city estimates (50+1)^2 = 2601 requests; a 4000
    # budget fits one such run but not two.
    a = _register(conn, "Alpha", width=1000, height=1000, step=20)
    b = _register(conn, "Beta", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")  # both due
    conn.commit()

    ran = []
    # record_usage: the real pipeline writes what it spent to api_usage, and the
    # remaining-budget check reads that ledger back.
    _stub_collection(sched, monkeypatch, conn, ran, record_usage=True)

    cfg = SchedulerConfig(daily_request_budget=4_000, publish_enabled=False)
    rc = sched.cmd_run_due(cfg, today=today)

    assert len(ran) == 1  # exactly one city fit the budget
    assert rc == 0  # a budget deferral is not a failure
    assert db.get_api_usage(conn, today, "gsv") == 2601  # B never spent requests
    # The deferred city is untouched: still due tomorrow, no failure recorded
    deferred = b if ran == [(a, "gsv")] else a
    row = conn.execute(
        "SELECT consecutive_failures, last_success_at FROM schedule_state "
        "WHERE city_id = ? AND provider = 'gsv'",
        (deferred,),
    ).fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_success_at"] is None


def test_oversized_city_does_not_starve_queue(conn, monkeypatch):
    """A city whose estimate exceeds the entire daily budget must be
    skipped (not break the loop), so smaller cities behind it still run.
    Regression: 82 real cities have grids too large for any daily budget;
    stalest-first ordering would otherwise block collection forever."""
    from streetscape_metadata_tracker import scheduler as sched

    huge = _register(conn, "Huge", width=200_000, height=200_000, step=20)
    small = _register(conn, "Small", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    # Make Huge the stalest (never run) — both are due
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)

    cfg = SchedulerConfig(daily_request_budget=10_000, publish_enabled=False)
    rc = sched.cmd_run_due(cfg, today=date(2026, 7, 2))

    ran_cities = [city_id for city_id, _ in ran]
    assert huge not in ran_cities  # skipped: never fits any budget
    assert small in ran_cities  # not starved by the huge city ahead of it
    assert rc == 0


def test_run_due_pairs_providers_per_city(conn, monkeypatch):
    """A city due for both providers runs both back-to-back with the same
    run date, each within its own budget ledger and failure tracking."""
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.scheduler import ProviderConfig

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)

    ran = []
    _stub_collection(
        sched, monkeypatch, conn, ran, outcome=lambda city, provider: provider == "gsv"
    )

    cfg = SchedulerConfig(
        publish_enabled=False,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000),
            "mapillary": ProviderConfig(daily_request_budget=1_000),
        },
    )
    rc = sched.cmd_run_due(cfg, today=date(2026, 7, 2))

    assert ran == [(cid, "gsv"), (cid, "mapillary")]  # paired, gsv first
    assert rc == 1  # the (simulated) mapillary failure surfaces in the exit code

    # Success/failure recorded independently per provider
    rows = {
        r["provider"]: r
        for r in conn.execute(
            "SELECT provider, last_success_at, consecutive_failures "
            "FROM schedule_state WHERE city_id = ?",
            (cid,),
        )
    }
    assert rows["gsv"]["last_success_at"] is not None
    assert rows["gsv"]["consecutive_failures"] == 0
    assert rows["mapillary"]["last_success_at"] is None
    assert rows["mapillary"]["consecutive_failures"] == 1


def test_run_due_provider_budgets_are_independent(conn, monkeypatch):
    """Exhausting one provider's budget must not block the other."""
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.scheduler import ProviderConfig

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    today = date(2026, 7, 2)  # pinned; must match the cmd_run_due call below
    # gsv's ledger is already full for today; mapillary's is untouched
    db.add_api_usage(conn, today, 10_000, provider="gsv")

    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)

    cfg = SchedulerConfig(
        publish_enabled=False,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000),
            "mapillary": ProviderConfig(daily_request_budget=1_000),
        },
    )
    sched.cmd_run_due(cfg, today=date(2026, 7, 2))

    assert ran == [(cid, "mapillary")]  # gsv deferred, mapillary still ran


# ---------------------------------------------------------------------------
# run-due --provider / --limit (issue #214): the on-demand catch-up path.
#
# The point of routing a bulk Mapillary catch-up through the scheduler rather
# than a script is that it inherits the budget ledger, the host lock, the
# breaker, failure counting and the publish tail — the detached script that had
# none of those is what got this host per-IP banned on 2026-08-14.
# ---------------------------------------------------------------------------


def _mly_cfg(**overrides):
    """gsv + mapillary both enabled, budgets generous enough not to interfere."""
    base = dict(
        publish_enabled=False,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=10_000),
        },
    )
    base.update(overrides)
    return SchedulerConfig(**base)


def test_run_due_parses_the_provider_filter():
    """--provider is repeatable and collects into args.providers; absent, it is
    None (which means "every enabled channel", not "no channels")."""
    args = build_parser().parse_args(
        ["run-due", "--provider", "mapillary", "--provider", "mapillary_streets", "--limit", "40"]
    )
    assert args.providers == ["mapillary", "mapillary_streets"]
    assert args.limit == 40
    assert build_parser().parse_args(["run-due"]).providers is None


def test_provider_filter_runs_only_the_named_channel(conn, monkeypatch):
    """The whole mechanism is _collect_due's filter: a channel left out of
    providers_for_city is never priced, budgeted or launched."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)

    rc = sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2), requested_providers=["mapillary"])

    assert ran == [(cid, "mapillary")]  # gsv enabled but not requested
    assert rc == 0
    # ...and gsv's schedule_state is untouched, so it stays due for the nightly
    # batch. A catch-up must not consume another channel's turn.
    row = conn.execute(
        "SELECT last_success_at FROM schedule_state WHERE city_id = ? AND provider = 'gsv'",
        (cid,),
    ).fetchone()
    assert row["last_success_at"] is None


def test_provider_filter_accepts_a_comma_list(conn, monkeypatch):
    """`--provider a,b` is what an operator types; it must mean the same as the
    repeated form, and the result keeps the canonical gsv-first ordering rather
    than the order given on the command line."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)

    sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2), requested_providers=["mapillary,gsv"])

    assert ran == [(cid, "gsv"), (cid, "mapillary")]


def test_provider_filter_rejects_an_unknown_channel(conn, monkeypatch):
    """A typo exits USAGE_EXIT_CODE WITHOUT opening the catalog. Returning (not
    raising) is deliberate: main()'s run-due branch emails an alert on an
    exception, and an operator typo is not a nightly crash."""
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)

    rc = sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2), requested_providers=["mapilary"])

    assert rc == sched.USAGE_EXIT_CODE
    assert connected == []


def test_provider_filter_rejects_a_disabled_channel(conn, monkeypatch):
    """The prod-shaped case: while the Mapillary channels are switched off after
    a per-IP block, `--provider mapillary` must say so rather than run a zero-due
    night — which would still fire the publish tail and read as a success."""
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)

    cfg = _mly_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(enabled=False, daily_request_budget=10_000),
        }
    )
    rc = sched.cmd_run_due(cfg, today=date(2026, 7, 2), requested_providers=["mapillary"])

    assert rc == sched.USAGE_EXIT_CODE
    assert connected == []


@pytest.mark.parametrize("value", ["", ",", "  ,  "])
def test_provider_filter_rejects_a_value_naming_no_channel(conn, monkeypatch, value):
    """
    `--provider ""` and `--provider ,` both parse (argparse takes any string) and
    both name nothing. They must be refused for the same reason a typo is: an
    empty channel list is a zero-due night that still runs the whole publish tail
    and exits 0.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)

    rc = sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2), requested_providers=[value])

    assert rc == sched.USAGE_EXIT_CODE
    assert connected == []


def test_select_providers_never_hands_back_a_none_sentinel():
    """
    A rejected channel must raise, not return None — because None means the
    OPPOSITE thing one call away: _collect_due's `providers` used to default to
    None-means-every-enabled-channel. Forwarding an error sentinel into that would
    turn `--provider mapilary` from a refusal into a full unfiltered night, GSV
    included. The signature check is half the guard: no fail-open default exists
    for a future caller to hit.
    """
    import inspect

    from streetscape_metadata_tracker import scheduler as sched

    with pytest.raises(sched._UsageError):
        sched._select_providers(_mly_cfg(), ["nope"])

    providers_param = inspect.signature(sched._collect_due).parameters["providers"]
    assert providers_param.default is inspect.Parameter.empty, (
        "_collect_due must require an explicit channel list; a None default "
        "silently means 'every channel' and fails open"
    )


def test_usage_exit_code_is_distinct_from_every_other_status():
    """
    Exit statuses are an interface for whatever wraps the on-demand catch-up, so
    the usage code has to be its own number. 2 is taken twice over (argparse's
    parse errors and main()'s unknown-subcommand catch-all), and 0/1 are the
    night's own verdicts.
    """
    from streetscape_metadata_tracker import download_common
    from streetscape_metadata_tracker import scheduler as sched

    assert sched.USAGE_EXIT_CODE == 64  # sysexits.h EX_USAGE
    assert sched.USAGE_EXIT_CODE not in {0, 1, 2}
    assert sched.USAGE_EXIT_CODE not in set(download_common.HOST_EXIT_CODES.values())
    assert sched.USAGE_EXIT_CODE not in set(download_common.HOST_BUSY_EXIT_CODES.values())


@pytest.mark.parametrize("bad", [0, -1])
def test_limit_below_one_is_refused_rather_than_collecting_nothing(conn, monkeypatch, bad):
    """
    `--limit 0`/`-1` used to make `processed >= max_cities` true on the first
    iteration: zero cities, no publish, exit 0. That is the same "a night that did
    nothing reads as a success" failure the channel check refuses, reached through
    the sibling flag — so it is refused before the catalog is even opened.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)

    rc = sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2), limit=bad)

    assert rc == sched.USAGE_EXIT_CODE
    assert connected == []


def test_limit_overrides_the_daily_city_cap(conn, monkeypatch):
    """An explicit --limit IS the cap for that run. Without this the config's
    max_cities_per_day silently wins and `--limit 40` quietly does 20, which
    leaves a Mapillary catch-up at the nightly cap's ~61 nights per pass rather
    than the ~5 the daily budget allows."""
    from streetscape_metadata_tracker import scheduler as sched

    for i in range(5):
        _register(conn, f"City{i}", width=1000, height=1000, step=20)
    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)

    rc = sched.cmd_run_due(
        _mly_cfg(max_cities_per_day=2),
        today=date(2026, 7, 2),
        limit=5,
        requested_providers=["mapillary"],
    )

    assert len(ran) == 5
    assert rc == 0


def test_limit_reaches_n_cities_even_when_candidates_are_skipped(conn, monkeypatch):
    """
    THE regression this flag exists to prevent, one layer down. `--limit N` used
    to also slice the *candidate* list to N — but the loop's cap counts cities it
    actually PROCESSED, and a candidate can be skipped without processing (budget
    guard, host breaker, busy lock). So the loop ran out of list below N and
    reported a clean night: `--limit 40` quietly doing 30 again.

    Here two of the five candidates can never fit any budget, so a pre-truncated
    list of 3 would collect 1. The loop must instead walk past them and reach 3.
    """
    from streetscape_metadata_tracker import scheduler as sched

    # Interleave unfittable cities among fittable ones, and make the unfittable
    # pair the stalest so stalest-first ordering puts them at the front.
    ids = []
    for i in range(5):
        oversized = i in (0, 1)
        ids.append(
            _register(
                conn,
                f"City{i}",
                width=400_000 if oversized else 1000,
                height=400_000 if oversized else 1000,
                step=20,
            )
        )
    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)

    rc = sched.cmd_run_due(
        _mly_cfg(max_cities_per_day=2),
        today=date(2026, 7, 2),
        limit=3,
        requested_providers=["mapillary"],
    )

    assert [city_id for city_id, _ in ran] == ids[2:5], (
        "the two over-budget candidates must be walked past, not counted against "
        "--limit; pre-slicing the candidate list is what made this collect 1"
    )
    assert rc == 0


def test_limit_still_respects_the_daily_request_budget(conn, monkeypatch):
    """
    The safety property that makes overriding the city cap acceptable: a widened
    run is still bounded by the channel's daily budget ledger, which is the whole
    reason a catch-up goes through the scheduler instead of a script.
    """
    from streetscape_metadata_tracker import scheduler as sched

    ids = [_register(conn, f"City{i}", width=40_000, height=40_000, step=20) for i in range(10)]
    ran = []
    # record_usage is what makes this test mean anything: without the ledger
    # write, `used + est > budget` never trips and the guard is never exercised.
    _stub_collection(sched, monkeypatch, conn, ran, record_usage=True)

    # Size the budget to fit exactly three of these cities. Read the estimate back
    # rather than hard-coding it: a z14 tile count depends on the grid's latitude,
    # so a literal would pin this test to the fixture's coordinates.
    per_city = sched.estimate_requests(db.resolve_city(conn, ids[0]), "mapillary", conn=conn)
    budget = 3 * per_city + per_city // 2
    cfg = _mly_cfg(
        max_cities_per_day=100,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=budget),
        },
    )
    rc = sched.cmd_run_due(
        cfg, today=date(2026, 7, 2), limit=100, requested_providers=["mapillary"]
    )

    assert len(ran) == 3, "--limit must not let a run spend past the daily budget"
    spent = db.get_api_usage(conn, date(2026, 7, 2), "mapillary")
    assert spent == 3 * per_city
    # The budget, not the city list, is what stopped it: a fourth city would not
    # have fit, and there were seven more candidates waiting.
    assert spent + per_city > budget
    assert len(ids) == 10
    assert rc == 0  # a budget deferral is not a failure
    # The budget-deferred cities recorded no failure, so they stay due and lead
    # tomorrow's stalest-first queue (the #208 posture, unchanged by --limit).
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM schedule_state WHERE provider = 'mapillary' "
            "AND consecutive_failures > 0"
        ).fetchone()[0]
        == 0
    )


def test_limit_below_the_cap_still_narrows(conn, monkeypatch):
    """The pre-#214 meaning of --limit is unchanged: it may narrow as well as
    widen, so `--limit 1` is still a one-city smoke test — and it must not pause
    for sleep_between_cities_s afterwards, now that the candidate list is no
    longer truncated to the limit."""
    from streetscape_metadata_tracker import scheduler as sched

    for i in range(5):
        _register(conn, f"City{i}", width=1000, height=1000, step=20)
    ran, slept = [], []
    _stub_collection(sched, monkeypatch, conn, ran, slept=slept)

    sched.cmd_run_due(
        _mly_cfg(max_cities_per_day=20),
        today=date(2026, 7, 2),
        limit=1,
        requested_providers=["mapillary"],
    )

    assert len(ran) == 1
    assert slept == [], "a capped run must not sleep after the last city it will run"


def test_the_summary_reports_elapsed_time_and_the_active_filter(conn, monkeypatch):
    """
    The summary is what the [alerts] email carries, so it is where an operator
    reads a night off. Two things have to be in it. The **filter**, or a
    single-channel catch-up is indistinguishable from a nightly run that collected
    one channel. And **elapsed time**, because the Mapillary block may have a
    sustained-load component and nothing else records time-under-load: peak rate
    is a config value and `api_usage` is a daily total, so without this the
    documented falsifier ("was it too much, or too long?") is unanswerable after
    the fact.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    _stub_collection(sched, monkeypatch, conn, [])
    seen = {}

    def capture(cfg, c, summary, *args, **kwargs):
        seen["summary"] = summary
        return 0

    monkeypatch.setattr(sched, "_finish_batch", capture)

    sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2), requested_providers=["mapillary"])

    assert "[--provider mapillary]" in seen["summary"]
    assert re.search(r"in \d+\.\d\d h", seen["summary"]), seen["summary"]


def test_a_filtered_run_desyncs_that_citys_paired_snapshots(conn, monkeypatch):
    """
    Documenting the cost of a single-channel catch-up, because nothing downstream
    will. `get_due_cities` derives dueness from `schedule_state.last_success_at`
    alone and never reads `day_of_cycle`, so a city's channels sharing a run date
    is a CONSEQUENCE of their clocks being in lockstep, not a constraint the
    scheduler maintains. Advancing one channel alone therefore un-pairs the city
    until the cadences happen to re-converge — which is precisely what catching a
    channel up means, and is why the run logs a warning saying so.
    """
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)
    today = date(2026, 7, 2)

    sched.cmd_run_due(_mly_cfg(), today=today, requested_providers=["mapillary"])

    assert ran == [(cid, "mapillary")]
    clocks = {
        r["provider"]: r["last_success_at"]
        for r in conn.execute(
            "SELECT provider, last_success_at FROM schedule_state WHERE city_id = ?", (cid,)
        )
    }
    assert clocks["mapillary"] is not None
    assert clocks["gsv"] is None
    # Concretely: gsv is still due while mapillary now is not. They will not land
    # on one run date again by themselves.
    still_due = {
        p: [
            c.city_id
            for c in db.get_due_cities(
                conn,
                today=today,
                cycle_days=90,
                grace_days=7,
                max_consecutive_failures=5,
                default_membership=True,
                provider=p,
            )
        ]
        for p in ("gsv", "mapillary")
    }
    assert still_due["gsv"] == [cid]
    assert still_due["mapillary"] == []


def test_run_due_without_a_filter_still_runs_every_enabled_channel(conn, monkeypatch):
    """The nightly path (no --provider, no --limit) is untouched by #214."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    ran = []
    _stub_collection(sched, monkeypatch, conn, ran)

    sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2))

    assert ran == [(cid, "gsv"), (cid, "mapillary")]


def test_provider_filter_still_registers_stagger_for_every_channel(conn, monkeypatch):
    """assign_schedule runs over the FULL enabled set even under a filter: a
    Mapillary-only catch-up must not leave a newly registered city without a gsv
    stagger row, which would silently delay its first grid collection."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    _stub_collection(sched, monkeypatch, conn, [])

    sched.cmd_run_due(_mly_cfg(), today=date(2026, 7, 2), requested_providers=["mapillary"])

    providers = {
        r["provider"]
        for r in conn.execute("SELECT provider FROM schedule_state WHERE city_id = ?", (cid,))
    }
    assert providers == {"gsv", "mapillary"}


def test_run_due_refreshes_the_manifest_only_after_a_success(conn, monkeypatch):
    """
    The nightly rebuild of the catalog-derived indexes is gated on ≥1 successful
    city: a night where nothing collected leaves both `cities.json.gz` and
    `streetwalks.json.gz` untouched (no needless republish), and a night with a
    success refreshes both together (issue #155).
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")  # due
    conn.commit()

    calls = {"agg": 0, "manifest": 0}
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        sched, "generate_aggregate_v2", lambda c, d: calls.__setitem__("agg", calls["agg"] + 1)
    )
    monkeypatch.setattr(
        sched,
        "generate_streetwalk_manifest",
        lambda c, d: calls.__setitem__("manifest", calls["manifest"] + 1) or {"walks": []},
    )

    # A night where the city's run fails: neither index is rebuilt.
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            False
        ),
    )
    sched.cmd_run_due(SchedulerConfig(publish_enabled=False), today=date(2026, 7, 2))
    assert calls == {"agg": 0, "manifest": 0}

    # A night with a success: both, exactly once each.
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            True
        ),
    )
    sched.cmd_run_due(SchedulerConfig(publish_enabled=False), today=date(2026, 7, 3))
    assert calls == {"agg": 1, "manifest": 1}


def test_regenerate_writes_both_indexes_to_the_configured_data_dir(conn, data_dir, monkeypatch):
    """
    End-to-end (no stubbed generators): `regenerate-aggregate` must leave BOTH
    published indexes on disk in the configured data dir. This is the documented
    recovery path after a killed run, so a missing `streetwalks.json.gz` here
    means the city page silently loses every road-walk overlay.
    """
    import gzip
    import json

    from streetscape_metadata_tracker import scheduler as sched

    city_id = _register(conn, "Bend", width=1000, height=1000, step=20)
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 17),
        csv_filename="bend_streetwalk_sp15_2026-07-17.csv.gz",
        coverage_filename="bend_streetwalk_sp15_2026-07-17_coverage.json.gz",
        coverage_pct_by_length=88.0,
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    rc = sched.cmd_regenerate(SchedulerConfig(data_dir=data_dir, publish_enabled=False))

    assert rc == 0
    assert os.path.exists(os.path.join(data_dir, "cities.json.gz"))
    manifest_path = os.path.join(data_dir, "streetwalks.json.gz")
    assert os.path.exists(manifest_path)
    with gzip.open(manifest_path, "rt") as fh:
        walks = json.load(fh)["walks"]
    assert [w["city_id"] for w in walks] == [city_id]


# ── Street-coverage channels (issue #99) ────────────────────────────────────


def _street_cfg(**overrides):
    """A config with both street channels enabled alongside the grid ones."""
    from streetscape_metadata_tracker.scheduler import ProviderConfig

    providers = {
        "gsv": ProviderConfig(enabled=True, daily_request_budget=10_000_000),
        "gsv_streets": ProviderConfig(
            enabled=True, daily_request_budget=2_000_000, max_requests_per_minute=24_000
        ),
        "mapillary": ProviderConfig(enabled=True, daily_request_budget=40_000),
        "mapillary_streets": ProviderConfig(enabled=True, daily_request_budget=5_000),
    }
    return SchedulerConfig(providers=providers, **overrides)


def test_street_channels_parse_as_real_providers(tmp_path):
    """They used to be skipped outright by the config loader, which made the
    [providers.gsv_streets] block inert."""
    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text(
        "[providers.gsv]\nenabled = true\n\n"
        "[providers.gsv_streets]\nenabled = true\ndaily_request_budget = 2000000\n"
        "max_requests_per_minute = 24000\nspacing_m = 20\n\n"
        "[providers.mapillary_streets]\nenabled = true\ndaily_request_budget = 5000\n"
    )
    cfg = load_scheduler_config(str(cfg_path))
    assert set(cfg.enabled_providers()) == {"gsv", "gsv_streets", "mapillary_streets"}
    assert cfg.providers["gsv_streets"].daily_request_budget == 2_000_000
    assert cfg.providers["gsv_streets"].max_requests_per_minute == 24_000
    assert cfg.providers["gsv_streets"].spacing_m == 20


def test_enabled_providers_orders_expensive_channels_first():
    """The canonical order is stable, which is what other things are read against.

    Pinned for what the order actually decides rather than for a budget story:
    it is the submit order, so it picks which channels have FINISHED when a
    SIGTERM wind-down stops the city (#206 — a submit gate, but `KillMode`
    defaults to control-group, so a real `systemctl stop` takes the in-flight
    children with it), which claim a lane first when a city has more channels
    than lanes, and — the mechanical case for expensive channels leading — how
    much of the batch deadline each child's timeout clamp is allowed to see,
    since `remaining_s` is read fresh at every launch. That last one is pinned
    in its own right by
    test_the_deadline_is_a_submit_gate_and_every_lane_child_gets_its_own_remaining_s.

    It decides nothing about budgets, which are per-channel, and nothing about
    what a batch deadline DROPS, which is a between-cities check. docs/scheduler.md
    records why both of those read as established for a while, along with the two
    later rationales that were also wrong.
    """
    assert _street_cfg().enabled_providers() == [
        "gsv",
        "gsv_streets",
        "mapillary",
        "mapillary_streets",
    ]


def test_unknown_provider_still_warned_and_dropped(tmp_path):
    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text("[providers.gsv]\nenabled = true\n\n[providers.bogus]\nenabled = true\n")
    cfg = load_scheduler_config(str(cfg_path))
    assert "bogus" not in cfg.providers


# The refusal mechanism outlived the channel it was written for: kartaview is a
# real channel since #248, so these drive a SYNTHETIC entry via monkeypatch
# rather than being deleted. Keeping them is the point — the record/drop/
# don't-raise asymmetry is what the NEXT unwired channel inherits, and the four
# fail-open arms #225 phase 3b left behind are what it exists to catch.
_UNWIRED_FIXTURE = "gsv_streets"


@pytest.fixture
def unwired(monkeypatch):
    """Make a real, otherwise-runnable channel temporarily unwired."""
    from streetscape_metadata_tracker.scheduler import UNWIRED_CHANNELS

    monkeypatch.setitem(UNWIRED_CHANNELS, _UNWIRED_FIXTURE, "a synthetic test reason")
    return _UNWIRED_FIXTURE


def test_a_known_but_unwired_channel_is_recorded_and_dropped(tmp_path, unwired):
    """
    RECORDED and dropped, not silently skipped like the unknown name above and
    not raised either -- a three-way asymmetry, each arm load-bearing.

    #225 phase 3b put "kartaview" in KNOWN_PROVIDERS so the CLI could collect a
    city by hand, and this loader gates on that same tuple -- so
    [providers.kartaview] started PARSING while city_timeout_seconds,
    estimate_requests and enabled_providers' rank all stayed fail-open for it.
    Dropping the block keeps every check after this point from pricing or
    launching it; recording the error lets the channel-running commands refuse
    rather than quietly run a night AROUND a channel the config asks for; and
    NOT raising keeps backup-status and restore-backup -- the incident-time
    handles -- working under a config only run-due could ever act on.
    """
    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text(
        f"[providers.gsv]\nenabled = true\n\n[providers.{unwired}]\nenabled = true\n"
    )
    cfg = load_scheduler_config(str(cfg_path))
    assert unwired not in cfg.providers, "dropped, so nothing downstream can run it"
    assert "gsv" in cfg.providers, "the runnable channel beside it is untouched"
    assert unwired not in cfg.enabled_providers()
    assert len(cfg.unwired_channel_errors) == 1
    assert unwired in cfg.unwired_channel_errors[0]


def test_disabling_an_unwired_channel_does_not_make_it_acceptable(tmp_path, unwired):
    """
    `enabled = false` is not a way to keep the block around "for later".

    The refusal is about the block EXISTING, because the next person to flip
    that flag gets a channel the scheduler cannot run and no error saying so.
    """
    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text(f"[providers.{unwired}]\nenabled = false\n")
    cfg = load_scheduler_config(str(cfg_path))
    assert unwired not in cfg.providers
    assert cfg.unwired_channel_errors


def test_run_due_refuses_an_unwired_channel_before_opening_the_catalog(
    conn, monkeypatch, tmp_path, unwired
):
    """
    The channel-running half of the split. A night that silently ran AROUND a
    channel the config asks for would read as a success while collecting
    nothing on it -- the same shape as the unknown-channel refusal, one layer
    down. Refused with USAGE_EXIT_CODE before the catalog is opened.
    """
    from streetscape_metadata_tracker import scheduler as sched

    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text(
        f"[providers.gsv]\nenabled = true\n\n[providers.{unwired}]\nenabled = true\n"
    )
    cfg = load_scheduler_config(str(cfg_path))
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)

    assert sched.cmd_run_due(cfg, today=date(2026, 7, 2)) == sched.USAGE_EXIT_CODE
    assert connected == []


def test_assess_city_refuses_an_unwired_channel_before_geocoding(
    conn, monkeypatch, tmp_path, unwired
):
    """assess-city launches channels too, so it refuses on the same guard --
    before the catalog is opened and before the Nominatim pre-flight spends a
    request on a city that will not be collected.

    The fixture channel is in ASSESS_CHANNELS on purpose: otherwise this would
    pass on the "not an assess channel" refusal instead and stop testing the
    guard it names.
    """
    from streetscape_metadata_tracker import scheduler as sched

    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text(f"[providers.{unwired}]\nenabled = true\n")
    cfg = load_scheduler_config(str(cfg_path))
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)

    assert sched.cmd_assess_city(cfg, "Bend, OR") == sched.USAGE_EXIT_CODE
    assert connected == []


def test_backup_status_still_works_with_a_stray_unwired_block(conn, monkeypatch, tmp_path, unwired):
    """
    The read-only half of the split, and the reason the loader stopped raising:
    backup-status is an incident-time handle, and a load-time ValueError took
    it down over a config block it could never act on -- on exactly the kind of
    bad day it exists for.
    """
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text(
        f'[paths]\ndata_dir = "{tmp_path / "data"}"\n'
        f'backup_dir = "{tmp_path / "backups"}"\n\n'
        f"[providers.{unwired}]\nenabled = true\n"
    )
    cfg = load_scheduler_config(str(cfg_path))
    assert cfg.unwired_channel_errors, "the stray block must be what this test exercises"
    cfg.driving_plan.archive_dir = str(tmp_path / "archive" / "gsv_driving_plan")
    os.makedirs(cfg.driving_plan.archive_dir)
    sched.catalog_backup.write_backup(conn, cfg.backup_dir, date(2026, 8, 7), source_db=cfg.db_path)

    assert sched.cmd_backup_status(cfg) == 0, "read-only subcommands keep working"


def test_every_unwired_channel_is_a_real_provider_token(tmp_path):
    """
    A guard keyed on a name nothing can configure is a guard that never fires.

    If a token is removed from KNOWN_PROVIDERS (or misspelled here), the branch
    above becomes unreachable and the config it was written to refuse starts
    being silently dropped by the unknown-provider branch instead -- which reads
    as working, because the channel still does not run.
    """
    from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS
    from streetscape_metadata_tracker.scheduler import STREET_CHANNELS, UNWIRED_CHANNELS

    if not UNWIRED_CHANNELS:
        # `set() - ... == []` is vacuously true, and a guard that passes by
        # being empty is the one outcome worth refusing outright: it reads as
        # "checked" in a green run. Skipping says so out loud instead. Every
        # channel is wired since #248; this fires again for the next one.
        pytest.skip("UNWIRED_CHANNELS is empty — every known channel is wired")

    unreachable = sorted(set(UNWIRED_CHANNELS) - set(KNOWN_PROVIDERS) - set(STREET_CHANNELS))
    assert unreachable == [], f"UNWIRED_CHANNELS names nothing configurable: {unreachable}"


# ---------------------------------------------------------------------------
# Per-(city, channel) membership and the opt-in hoist (issue #248).
#
# Membership scopes a channel to an explicit set of cities; the hoist is what
# makes that set actually REACHED, because _collect_due's union is ordered by
# first appearance and _run_city_loop truncates at max_cities_per_day. The two
# halves are tested separately because either one alone is a channel that is
# configured and collects nothing.
# ---------------------------------------------------------------------------


def test_every_scheduled_channel_declares_its_default_membership():
    """Set EQUALITY, so a provider token cannot land without a membership decision.

    A `.get(p, True)`, or a set of opt-in names tested with `in`, would classify
    a NEWLY ADDED provider as "every enabled city, immediately" — which is
    exactly how #225 phase 3b created this bug: adding a token to
    KNOWN_PROVIDERS made a channel configurable and four fail-open arms then did
    the wrong thing silently. A missing entry must be a KeyError here.
    """
    from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS
    from streetscape_metadata_tracker.scheduler import (
        CHANNEL_DEFAULT_MEMBERSHIP,
        STREET_CHANNELS,
    )

    assert set(CHANNEL_DEFAULT_MEMBERSHIP) == set(KNOWN_PROVIDERS) | set(STREET_CHANNELS)
    with pytest.raises(KeyError):
        CHANNEL_DEFAULT_MEMBERSHIP["a_channel_nobody_decided_about"]


def test_get_due_cities_has_no_default_membership_default():
    """The fail-open direction here costs a 186-hour nightly queue.

    Same guard as _collect_due's `providers` and _run_city_loop's `max_cities`:
    a `= True` would make "the caller forgot" and "every enabled city" the same
    value, and the symptom is only visible as a night that never ends.
    """
    import inspect

    from streetscape_metadata_tracker import db as sdb

    param = inspect.signature(sdb.get_due_cities).parameters["default_membership"]
    assert param.default is inspect.Parameter.empty, (
        "get_due_cities must require an explicit default_membership; a True "
        "default fails open into the whole catalog"
    )
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_kartaview_is_the_only_opt_in_channel_today():
    """Pins the actual policy, not just its shape — the four scheduled channels
    are cheap enough per city that catalog-wide membership is right for them,
    and flipping one of them to opt-in would silently empty its nightly queue.
    """
    from streetscape_metadata_tracker.scheduler import CHANNEL_DEFAULT_MEMBERSHIP

    opt_in = sorted(c for c, default in CHANNEL_DEFAULT_MEMBERSHIP.items() if not default)
    assert opt_in == ["kartaview"]


def test_the_hoist_is_the_identity_permutation_without_an_opt_in_channel(conn):
    """PR A's inertness, asserted as element-wise list identity rather than
    "looks the same".

    With every CHANNEL_DEFAULT_MEMBERSHIP value True, `opt_in` is empty, every
    sort key is 1, and a stable sort on a constant key changes nothing. This
    pins that against the pre-change union order for a multi-city, multi-channel
    slate — the shape where a reorder would actually show.
    """
    from streetscape_metadata_tracker import scheduler as sched

    ids = [
        _register(conn, n, width=1000, height=1000, step=20)
        for n in ("Bend", "Corvallis", "Eugene")
    ]
    db.assign_schedule(conn, 90, providers=("gsv", "mapillary"))
    # Stagger the clocks so the order is a real stalest-first ordering rather
    # than the city_id tiebreak.
    for i, cid in enumerate(ids):
        conn.execute(
            "UPDATE schedule_state SET last_success_at = ? WHERE city_id = ? AND provider = 'gsv'",
            (f"2026-0{i + 1}-01T00:00:00+00:00", cid),
        )
    conn.commit()

    cfg = _mly_cfg()
    ordered, providers_for_city, hoisted = sched._collect_due(
        conn, cfg, date(2026, 7, 2), ["gsv", "mapillary"]
    )
    expected = [
        c.city_id
        for c in db.get_due_cities(
            conn,
            today=date(2026, 7, 2),
            cycle_days=cfg.cycle_days,
            grace_days=cfg.grace_days,
            max_consecutive_failures=cfg.max_consecutive_failures,
            default_membership=True,
            provider="gsv",
        )
    ]
    assert [c.city_id for c in ordered] == expected
    assert hoisted == 0
    assert all(providers_for_city[c] == ["gsv", "mapillary"] for c in expected)


def test_a_city_due_only_on_an_opt_in_channel_is_hoisted_ahead_of_the_gsv_block(conn):
    """Without this the channel is scoped but never REACHED.

    The union is ordered by first appearance, so gsv (rank 0) dictates city
    order and an opt-in channel only ever appends. A city whose gsv run
    succeeded — so gsv will not surface it for ~83 days — but whose sweep
    checkpointed sits at the tail of ~949 cities and is truncated by the
    20-city cap, every night. That is what makes docs/scheduler.md's "leads
    tomorrow's stalest-first queue" true of the channel's own list and false of
    the union, and what would spend #239's five-night budget over a whole cycle.
    """
    from streetscape_metadata_tracker import scheduler as sched

    for name in ("Bend", "Corvallis", "Eugene"):
        _register(conn, name, width=1000, height=1000, step=20)
    krabi = _register(conn, "Krabi", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90, providers=("gsv", "kartaview"))
    db.set_channel_membership(conn, krabi, "kartaview", True, cycle_days=90)
    # Krabi's gsv clock is fresh, so gsv does not surface it; its sweep is due.
    db.record_attempt(conn, krabi, success=True, provider="gsv")

    cfg = _sweep_cfg(publish_enabled=False)
    ordered, providers_for_city, hoisted = sched._collect_due(
        conn, cfg, date(2026, 7, 2), ["gsv", "kartaview"]
    )

    assert [c.city_id for c in ordered][0] == krabi
    assert providers_for_city[krabi] == ["kartaview"]
    assert hoisted == 1


def test_a_city_due_on_gsv_too_keeps_its_exact_union_position(conn):
    """`all`, not `any` — and the choice is the blast radius.

    A city due on gsv as well needs no hoist: the union already places it in
    gsv's stalest-first list and both channels run the same night, and below the
    city cap it is truncated on both channels TOGETHER, which pairs fine. An
    `any` key would hoist it anyway, displacing the stalest gsv-only city from a
    capped night every time a member city comes due — negligible at two seed
    cities, a queue-jump per member city per cycle once the set widens.
    """
    from streetscape_metadata_tracker import scheduler as sched

    ids = [_register(conn, n, width=1000, height=1000, step=20) for n in ("Bend", "Corvallis")]
    krabi = _register(conn, "Krabi", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90, providers=("gsv", "kartaview"))
    db.set_channel_membership(conn, krabi, "kartaview", True, cycle_days=90)
    # Every city is due on gsv, and Krabi is LAST in gsv's stalest-first order.
    for i, cid in enumerate([*ids, krabi]):
        conn.execute(
            "UPDATE schedule_state SET last_success_at = ? WHERE city_id = ? AND provider = 'gsv'",
            (f"2026-0{i + 1}-01T00:00:00+00:00", cid),
        )
    conn.commit()

    cfg = _sweep_cfg(publish_enabled=False)
    ordered, providers_for_city, hoisted = sched._collect_due(
        conn, cfg, date(2026, 7, 2), ["gsv", "kartaview"]
    )

    assert [c.city_id for c in ordered][-1] == krabi, "not hoisted: it is due on gsv too"
    assert providers_for_city[krabi] == ["gsv", "kartaview"]
    assert hoisted == 0


def test_a_paused_sweep_leads_the_next_nights_slate_within_the_city_cap(conn, monkeypatch):
    """The claim docs/scheduler.md makes, end to end through the loop.

    Not just "still returned by get_due_cities" — the amnesty already gives that
    (test_a_multi_night_sweep_is_never_quarantined_before_it_can_finish) — but
    that it REACHES _run_city_loop inside max_cities_per_day. Without the hoist
    a paused city sits behind every gsv-due city and the cap decides, so a
    checkpointed sweep would resume months later rather than tomorrow, and #239's
    five-night budget would be spent over a whole cycle having finished nothing.
    """

    for name in ("Bend", "Corvallis", "Eugene"):
        _register(conn, name, width=1000, height=1000, step=20)
    krabi = _register(conn, "Krabi", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90, providers=tuple(_street_cfg().enabled_providers()))
    db.set_channel_membership(conn, krabi, "kartaview", True, cycle_days=90)
    # Krabi is fresh on every default-membership channel — the stranded shape:
    # due ONLY on the opt-in channel, so `all(...)` holds and it hoists.
    for channel in _street_cfg().enabled_providers():
        db.record_attempt(conn, krabi, success=True, provider=channel)

    ran = []

    def run_one(city, provider):
        ran.append((city.city_id, provider))
        return _paused_sweep_outcome() if provider == "kartaview" else True

    # A cap of ONE city: whichever city heads the slate is the one the night
    # reaches, so this asserts ORDERING rather than mere presence.
    cfg = _sweep_cfg(publish_enabled=False, max_cities_per_day=1)
    _run_loop_with(monkeypatch, conn, cfg, run_one, today=date(2026, 7, 2))

    assert ran[0] == (krabi, "kartaview"), "the paused sweep leads the slate"
    # And the pause did not consume the cap: the amnesty `continue`s BEFORE
    # `attempted` is counted, so `processed` never incremented and the night
    # went on to the next city. Pinned here because that placement is what
    # keeps a checkpointed sweep from costing a capped night its whole slate —
    # move the amnesty below the counter and this line goes red.
    assert any(cid != krabi for cid, _ in ran)


def test_a_sweep_killed_by_its_timeout_leads_the_next_slate_but_costs_the_night_a_slot(
    conn, monkeypatch
):
    """The arm a nightly batch actually takes today, which the pause case above
    does NOT cover.

    `_run_one_city` passes no `--kartaview-max-requests` (#273), and
    `download_kartaview` sets its `stop_reason` only from that guard — so exit
    83, its amnesty, and its "consumes no slot" property are all unreachable
    from run-due for now. What actually ends an unfinished sweep is a timeout
    SIGKILL, and it differs in both halves: it counts a consecutive_failure,
    and it DOES consume a city-cap slot, because the failure path increments
    `attempted`.

    The half the hoist is for survives either way: nothing stamps
    last_success_at, so the city is still due ONLY on the opt-in channel and
    still leads tomorrow's slate instead of falling to the tail of the union.
    Pinned so that if #273 lands and flips which arm is common, this stays the
    record of what the other one does.
    """
    for name in ("Bend", "Corvallis", "Eugene"):
        _register(conn, name, width=1000, height=1000, step=20)
    krabi = _register(conn, "Krabi", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90, providers=tuple(_street_cfg().enabled_providers()))
    db.set_channel_membership(conn, krabi, "kartaview", True, cycle_days=90)
    for channel in _street_cfg().enabled_providers():
        db.record_attempt(conn, krabi, success=True, provider=channel)

    ran = []

    def run_one(city, provider):
        ran.append((city.city_id, provider))
        # A SIGKILL surfaces as a plain failure carrying no exit code at all:
        # nothing here can tell it from a sweep that made no progress, which is
        # the whole reason it is charged a failure.
        return False if provider == "kartaview" else True

    cfg = _sweep_cfg(publish_enabled=False, max_cities_per_day=1)
    _run_loop_with(monkeypatch, conn, cfg, run_one, today=date(2026, 7, 2))

    assert ran[0] == (krabi, "kartaview"), "the killed sweep still leads the slate"
    # And unlike the amnestied pause, it COST the night its only slot.
    assert [cid for cid, _ in ran] == [krabi]

    row = conn.execute(
        "SELECT consecutive_failures, last_success_at FROM schedule_state "
        "WHERE city_id = ? AND provider = 'kartaview'",
        (krabi,),
    ).fetchone()
    assert row["consecutive_failures"] == 1
    assert row["last_success_at"] is None

    ordered, providers_for_city, hoisted = _sched._collect_due(
        conn, cfg, date(2026, 7, 3), list(cfg.enabled_providers())
    )
    assert ordered[0].city_id == krabi, "still hoisted tomorrow, which is what bounds it at five"
    assert providers_for_city[krabi] == ["kartaview"]
    assert hoisted == 1


def test_a_single_opt_in_channel_run_reports_no_hoist(conn):
    """`--provider kartaview` makes EVERY due city opt-in-only, so every sort
    key is 0 and the reorder is the identity permutation — the same trivial
    case as no opt-in channel configured, reached from the other end.

    Counting it would put `hoisted=<the whole slate>` on the opening line of
    every catch-up, which reads as "N cities were moved ahead of something"
    when nothing was moved and there is nothing to be ahead of.
    """
    krabi = _register(conn, "Krabi", width=1000, height=1000, step=20)
    bend = _register(conn, "Bend", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90, providers=("gsv", "kartaview"))
    for cid in (krabi, bend):
        db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)

    cfg = _sweep_cfg(publish_enabled=False)
    ordered, providers_for_city, hoisted = _sched._collect_due(
        conn, cfg, date(2026, 7, 2), ["kartaview"]
    )
    assert {c.city_id for c in ordered} == {krabi, bend}, "both are still collected"
    assert all(providers_for_city[c.city_id] == ["kartaview"] for c in ordered)
    assert hoisted == 0


def test_the_night_warns_when_the_opt_in_cities_fill_the_city_cap(conn, monkeypatch, caplog):
    """The hoist is deliberately unbounded (#282), so this alert is the only
    guard until reserved slots land.

    Once the opt-in-only cities alone fill `max_cities_per_day`, every
    default-membership channel collects NOTHING that night — and the arithmetic
    that produces a night of zero gsv would otherwise be recoverable only by
    subtracting two numbers on an INFO line. The warning names the starved
    channels, so the night's record says what it did and not only what it
    reordered.
    """
    import logging

    enrolled = [_register(conn, n, width=1000, height=1000, step=20) for n in ("Krabi", "Hue")]
    bend = _register(conn, "Bend", width=1000, height=1000, step=20)
    channels = _street_cfg().enabled_providers()
    db.assign_schedule(conn, 90, providers=tuple(channels))
    # Enrolled AND fresh on every default-membership channel: the stranded
    # shape, so both hoist and the cap is exactly their count.
    for cid in enrolled:
        db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)
        for channel in channels:
            db.record_attempt(conn, cid, success=True, provider=channel)

    ran = []
    cfg = _sweep_cfg(publish_enabled=False, max_cities_per_day=len(enrolled))
    with caplog.at_level(logging.WARNING, logger="streetscape_scheduler"):
        _run_loop_with(
            monkeypatch,
            conn,
            cfg,
            lambda c, p: ran.append((c.city_id, p)) or True,
            today=date(2026, 7, 2),
        )

    assert f"fill the city cap ({len(enrolled)})" in caplog.text
    assert "gsv" in caplog.text and "nothing tonight" in caplog.text
    # The warning is not merely pessimistic: Bend really did not collect.
    assert {cid for cid, _ in ran} == set(enrolled)
    assert bend not in {cid for cid, _ in ran}


def test_the_hoist_count_is_on_the_nights_own_record(conn, monkeypatch, caplog):
    """Same reason as max_concurrent_channels: which cities a capped night
    reached must be recoverable from the night's own log, not from an
    operator's memory of when a city was enrolled. Absent entirely when nothing
    was hoisted, so nightly lines stay byte-identical to today's.
    """
    import logging

    krabi = _register(conn, "Krabi", width=1000, height=1000, step=20)
    # A second city, due on gsv, so the hoist has something to be AHEAD of: the
    # count means "moved past N cities", and an all-opt-in slate is the identity
    # permutation with nothing to report (see
    # test_a_single_opt_in_channel_run_reports_no_hoist).
    _register(conn, "Bend", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90, providers=tuple(_street_cfg().enabled_providers()))
    db.set_channel_membership(conn, krabi, "kartaview", True, cycle_days=90)
    for channel in _street_cfg().enabled_providers():
        db.record_attempt(conn, krabi, success=True, provider=channel)

    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _run_loop_with(
            monkeypatch,
            conn,
            _sweep_cfg(publish_enabled=False),
            lambda c, p: True,
            today=date(2026, 7, 2),
        )
    assert "hoisted=1 opt-in-only cities" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _run_loop_with(monkeypatch, conn, _mly_cfg(), lambda c, p: True, today=date(2026, 7, 2))
    assert "hoisted=" not in caplog.text


# ── enroll-city (issue #248) ────────────────────────────────────────────────


def _enroll_cfg(tmp_path, conn, monkeypatch, **overrides):
    from streetscape_metadata_tracker import scheduler as sched

    cfg = _sweep_cfg(publish_enabled=False, **overrides)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    return cfg


def test_enroll_city_enrolls_and_reports_the_cost_before_it_is_spent(
    conn, monkeypatch, tmp_path, capsys
):
    """ "I typed a slug" has to become "I saw the number".

    Risk 1 of this design is that a hoisted opt-in city can consume essentially
    a whole night, and the mitigation is keeping the enrolled set to cities
    whose estimate is well under one — a decision only possible if the estimate
    is printed at enrolment time.
    """
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Krabi", width=10000, height=10000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)

    assert sched.cmd_enroll_city(cfg, cid, channel="kartaview") == 0
    out = capsys.readouterr().out
    assert "-> MEMBER" in out
    assert "requests" in out and "km" in out
    assert "1 of 1 enabled cities opted in" in out
    assert db.get_channel_membership(conn, cid, "kartaview") == 1


def test_enroll_city_refuses_a_default_membership_channel(conn, monkeypatch, tmp_path):
    """Per-city exclusion on gsv already has a handle: cities.enabled. A second,
    less visible way to disable a city is how two operators end up disagreeing
    about why it stopped collecting."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)

    assert sched.cmd_enroll_city(cfg, cid, channel="gsv") == sched.USAGE_EXIT_CODE
    assert sched.cmd_enroll_city(cfg, cid, channel="nope") == sched.USAGE_EXIT_CODE
    assert conn.execute("SELECT COUNT(*) FROM schedule_state").fetchone()[0] == 0


def test_enroll_city_refuses_an_unresolvable_or_disabled_city(conn, monkeypatch, tmp_path):
    """Both are the silent zero-row success this command exists to prevent: a
    typo'd slug matches nothing, and a disabled city can never be due on ANY
    channel because get_due_cities still requires cities.enabled = 1."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)

    assert sched.cmd_enroll_city(cfg, "krabi", channel="kartaview") == sched.USAGE_EXIT_CODE

    conn.execute("UPDATE cities SET enabled = 0 WHERE city_id = ?", (cid,))
    conn.commit()
    assert sched.cmd_enroll_city(cfg, cid, channel="kartaview") == sched.USAGE_EXIT_CODE
    assert db.get_channel_membership(conn, cid, "kartaview") is None


def test_enroll_city_remove_and_clear_are_kept_apart(conn, monkeypatch, tmp_path):
    """Indistinguishable to dueness today, deliberately kept apart anyway: an
    explicit 0 persists as an exclusion if this channel's default membership
    ever flips to True (the plausible end-state of "widen after"), while NULL
    flips with it. Collapsing them would put that out of reach of everything but
    hand-SQL."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Krabi", width=1000, height=1000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)

    sched.cmd_enroll_city(cfg, cid, channel="kartaview")
    assert sched.cmd_enroll_city(cfg, cid, channel="kartaview", remove=True) == 0
    assert db.get_channel_membership(conn, cid, "kartaview") == 0
    assert sched.cmd_enroll_city(cfg, cid, channel="kartaview", clear=True) == 0
    assert db.get_channel_membership(conn, cid, "kartaview") is None


def test_enroll_city_works_before_the_config_block_exists_and_says_so(
    conn, monkeypatch, tmp_path, capsys
):
    """It deliberately does NOT refuse on unwired_channel_errors: enrolment has
    to work BEFORE the channel is runnable or the rollout order is impossible.
    "Nothing collected overnight" is the EXPECTED outcome at that point, so it
    is stated rather than discovered."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Krabi", width=1000, height=1000, step=20)
    cfg = _mly_cfg(publish_enabled=False)  # no kartaview channel configured at all
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    assert sched.cmd_enroll_city(cfg, cid, channel="kartaview") == 0
    assert "NOTE" in capsys.readouterr().out
    assert db.get_channel_membership(conn, cid, "kartaview") == 1


def test_enroll_city_list_reports_the_channels_members(conn, monkeypatch, tmp_path, capsys):
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Krabi", width=1000, height=1000, step=20)
    _register(conn, "Bend", width=1000, height=1000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)
    sched.cmd_enroll_city(cfg, cid, channel="kartaview")
    capsys.readouterr()

    assert sched.cmd_enroll_city(cfg, None, channel="kartaview", list_only=True) == 0
    out = capsys.readouterr().out
    assert cid in out
    assert "1 of 2 enabled cities opted in" in out


def test_enroll_city_list_refuses_a_write_flag_and_accepts_any_channel(
    conn, monkeypatch, tmp_path, capsys
):
    """`--list` is read-only, so it is scoped the OPPOSITE way from the write
    path on one axis and stricter on the other.

    Stricter: argparse's mutually exclusive group covers --remove/--clear but
    not --list, and `list_only` short-circuits the write path — so `--list
    --remove` would otherwise be accepted, ignored and exit 0. That is the
    silent no-op this whole command exists to prevent, shipped by the command
    itself.

    Looser: refusing to LIST a default-membership channel would be a refusal
    with no hazard behind it. The answer there is "every enabled city", which
    is true and occasionally the thing being checked. Only WRITING to one is
    refused, because per-city exclusion there is cities.enabled.
    """
    cid = _register(conn, "Krabi", width=1000, height=1000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)

    for flag in ("remove", "clear"):
        assert (
            _sched.cmd_enroll_city(cfg, None, channel="kartaview", list_only=True, **{flag: True})
            == _sched.USAGE_EXIT_CODE
        )

    capsys.readouterr()
    assert _sched.cmd_enroll_city(cfg, None, channel="gsv", list_only=True) == 0
    out = capsys.readouterr().out
    assert cid in out, "every enabled city is a member of a default-membership channel"
    assert "1 of 1 enabled cities opted in" in out

    # Writing to one is still refused, and an unknown channel on either path.
    assert _sched.cmd_enroll_city(cfg, cid, channel="gsv") == _sched.USAGE_EXIT_CODE
    assert (
        _sched.cmd_enroll_city(cfg, None, channel="nope", list_only=True) == _sched.USAGE_EXIT_CODE
    )


def test_the_enrolment_cost_note_says_when_it_is_the_geometry_tier(
    conn, monkeypatch, tmp_path, capsys
):
    """The estimate an operator enrols against is a FLOOR, and nothing
    downstream absorbs the miss.

    estimate_kartaview_requests' tier 2 must assume the default r=1000 because
    the working radius is a property of the location; an r=500 metro costs ~4x
    it. No city has a cataloged KartaView run, so tier 2 is what every
    enrolment prints today — and the daily budget guard is a pre-flight check
    against this same number while the child is handed no request cap at all
    (#273), which makes the operator reading this line the last gate.
    """
    cid = _register(conn, "Krabi", width=10000, height=10000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)

    assert _sched.cmd_enroll_city(cfg, cid, channel="kartaview") == 0
    assert "GEOMETRY estimate" in capsys.readouterr().out

    # A prior sweep is the observed tier, and the caveat goes away with it.
    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 6, 1),
        csv_filename="x.csv.gz",
        total_points=1,
        provider="kartaview",
        api_requests=5_000,
    )
    assert _sched.cmd_enroll_city(cfg, cid, channel="kartaview") == 0
    out = capsys.readouterr().out
    assert "GEOMETRY estimate" not in out
    assert "~5,000 requests" in out, "and the observed number is what it prices"


def test_status_marks_a_non_member_rather_than_printing_enabled_yes(
    conn, monkeypatch, tmp_path, capsys
):
    """`c.enabled` alone would print `yes` for a non-member whose DUE column
    stays permanently blank, which reads as "the scheduler is broken" rather
    than "this city is not enrolled in this channel" (issue #248, risk 3)."""
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    cfg = _enroll_cfg(tmp_path, conn, monkeypatch)
    db.assign_schedule(conn, 90, providers=tuple(cfg.enabled_providers()))

    assert sched.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "not enrolled" in out
    assert "kartaview: 0 of 1 enabled cities opted in" in out


def test_street_estimate_prefers_a_prior_walk_and_rescales_for_spacing(conn):
    from streetscape_metadata_tracker.scheduler import estimate_street_samples

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    db.register_street_walk(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 1),
        csv_filename="w.csv.gz",
        spacing_m=15.0,
        sample_points=1000,
    )
    # Same spacing → the exact prior count.
    assert estimate_street_samples(conn, city, 15) == 1000
    # Halving the spacing doubles the samples along a fixed network length.
    assert estimate_street_samples(conn, city, 30) == 500


def test_street_estimate_falls_back_to_frozen_network_then_area(conn):
    from streetscape_metadata_tracker.scheduler import estimate_street_samples

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)

    # No walk, no network → the area proxy (25 km² * 7.45 km/km² / 15 m).
    area_only = estimate_street_samples(conn, city, 15)
    assert 10_000 < area_only < 14_000

    conn.execute(
        """INSERT INTO street_networks (city_id, network_type, graphml_filename,
           node_count, edge_count, fetched_at)
           VALUES (?, 'drive', 'x.graphml', 100, 1000, '2026-07-01T00:00:00+00:00')""",
        (cid,),
    )
    conn.commit()
    # A frozen network is more specific than the area proxy, so it wins.
    assert estimate_street_samples(conn, city, 15) == int(1000 * 4.2)


def test_street_estimate_does_not_reuse_another_network_types_walk(conn):
    """A drive walk says nothing about how much work an all_public walk is.

    Reusing it would badly under-budget the night (all_public adds every
    footway, path, cycleway, alley and driveway), so each step of the estimate
    filters on network type. Falling through to the area proxy is scaled UP for
    a broad network, deliberately over-estimating so the guard defers rather
    than overruns.
    """
    from streetscape_metadata_tracker.scheduler import estimate_street_samples

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    db.register_street_walk(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 1),
        csv_filename="w_drive.csv.gz",
        network_type="drive",
        spacing_m=15.0,
        sample_points=1000,
    )
    conn.execute(
        """INSERT INTO street_networks (city_id, network_type, graphml_filename,
           node_count, edge_count, fetched_at)
           VALUES (?, 'drive', 'x.graphml', 100, 1000, '2026-07-01T00:00:00+00:00')""",
        (cid,),
    )
    conn.commit()

    assert estimate_street_samples(conn, city, 15, "drive") == 1000
    broad = estimate_street_samples(conn, city, 15, "all_public")
    assert broad != 1000  # neither the drive walk nor the drive network leaked
    assert broad > 1000  # and the broad fallback over-estimates, never under

    # Once the city has its OWN broad walk, that exact count takes over.
    db.register_street_walk(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 2),
        csv_filename="w_broad.csv.gz",
        network_type="all_public",
        spacing_m=15.0,
        sample_points=2600,
    )
    assert estimate_street_samples(conn, city, 15, "all_public") == 2600
    assert estimate_street_samples(conn, city, 15, "drive") == 1000


def test_street_channel_passes_its_network_type_to_the_collector(conn):
    """Each network type is its own series, so the configured type must reach
    the collector — otherwise the channel silently walks 'drive' forever."""
    from streetscape_metadata_tracker.scheduler import ProviderConfig, _street_collect_cmd

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = SchedulerConfig(providers={"gsv_streets": ProviderConfig(network_type="all_public")})
    cmd = _street_collect_cmd(cfg, city, date(2026, 7, 8), "gsv_streets", 10, 100)
    assert "--network-type" in cmd
    assert cmd[cmd.index("--network-type") + 1] == "all_public"

    # Default config still walks the drive network.
    default_cmd = _street_collect_cmd(
        SchedulerConfig(providers={"gsv_streets": ProviderConfig()}),
        city,
        date(2026, 7, 8),
        "gsv_streets",
        10,
        100,
    )
    assert default_cmd[default_cmd.index("--network-type") + 1] == "drive"


def test_mapillary_street_estimate_is_tiles_not_samples(conn):
    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    # The whole reason Mapillary streets can be scheduled everywhere: its cost
    # is the tile census, identical to the grid provider's and unrelated to
    # how many sample points get scored.
    assert estimate_requests(city, "mapillary_streets", conn=conn) == estimate_requests(
        city, "mapillary"
    )
    assert estimate_requests(city, "gsv_streets", conn=conn) > estimate_requests(
        city, "mapillary_streets", conn=conn
    )


def test_street_timeout_scales_like_gsv_not_the_flat_floor(conn):
    """A 247k-sample city (Seattle) must not inherit the flat floor and get
    SIGKILLed 20 minutes into its crawl."""
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    cfg = _street_cfg(city_timeout_minutes=180, max_requests_per_minute=24_000)
    floor = 180 * 60
    cid = _register(conn, "Metropolis", width=40000, height=40000, step=20)
    city = db.resolve_city(conn, cid)
    db.register_street_walk(
        conn,
        city_id=cid,
        run_date=date(2026, 7, 1),
        csv_filename="w.csv.gz",
        spacing_m=15.0,
        sample_points=2_000_000,
    )
    assert city_timeout_seconds(cfg, city, "gsv_streets", conn=conn) > floor
    # Mapillary streets reads a handful of tiles — the floor is plenty.
    assert city_timeout_seconds(cfg, city, "mapillary_streets", conn=conn) == floor


def test_street_channel_dispatches_to_the_road_walk_collector(conn, monkeypatch, tmp_path):
    """The scheduler must run the road-walk CLI for a street channel, with the
    imagery provider, the isolated daily budget, and the catalog path."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    captured = {}

    def fake_run(cmd, timeout=None, cwd=None, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    cfg = _street_cfg(db_path="/tmp/x.db", data_dir="/tmp/data", log_dir=str(tmp_path))
    assert sched._run_one_city(
        cfg, city, date(2026, 7, 1), "gsv_streets", daily_budget=12345, conn=conn
    )

    cmd = captured["cmd"]
    assert "streetscape_street_analyzer.collect" in cmd
    assert cmd[cmd.index("--provider") + 1] == "gsv"  # the imagery provider
    assert cmd[cmd.index("--daily-budget") + 1] == "12345"
    assert cmd[cmd.index("--db-path") + 1] == "/tmp/x.db"
    assert cmd[cmd.index("--spacing") + 1] == "15"
    assert cmd[cmd.index("--run-date") + 1] == "2026-07-01"
    assert cmd[cmd.index("--") + 1] == city.display_name


def test_mapillary_street_dispatch_omits_per_minute_pacing(conn, monkeypatch, tmp_path):
    """Pacing is meaningless for a tile census; passing it would imply the
    collector meters per-request like the GSV arm."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    captured = {}

    def fake_run(cmd, timeout=None, cwd=None, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    assert sched._run_one_city(
        _street_cfg(log_dir=str(tmp_path)), city, date(2026, 7, 1), "mapillary_streets", conn=conn
    )
    cmd = captured["cmd"]
    assert cmd[cmd.index("--provider") + 1] == "mapillary"
    assert "--max-requests-per-minute" not in cmd


def test_street_dispatch_passes_the_full_budget_not_the_remainder(conn, monkeypatch, data_dir):
    """
    ``--daily-budget`` is the channel's whole ceiling, because the collector
    re-reads the same api_usage ledger and subtracts today's spend itself.

    Passing ``budget - used`` made the child's guard ``2*used + est > budget``,
    so once a street channel was ~half spent every remaining city aborted with
    exit 1 — which the scheduler counts as a real failure, driving the city
    toward max_consecutive_failures and out of the due set entirely.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    cfg = _street_cfg(data_dir=data_dir, publish_enabled=False)
    budget = cfg.providers["gsv_streets"].daily_request_budget
    today = date(2026, 7, 2)
    # More than half of it already spent — the regime where the old arithmetic
    # started rejecting cities that comfortably fit.
    already = int(budget * 0.6)
    db.add_api_usage(conn, today, already, provider="gsv_streets")

    seen = {}
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            seen.setdefault(provider, daily_budget) is not None or True
        ),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    sched.cmd_run_due(cfg, today=today)

    assert seen["gsv_streets"] == budget
    assert seen["gsv_streets"] != budget - already  # the old, doubled-counting value


def test_street_channels_share_the_grid_stagger_day(conn):
    """Paired snapshots: a city's street walk lands the same night as its grid
    run, because day_of_cycle is hashed from city_id alone."""
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90, providers=("gsv", "gsv_streets", "mapillary_streets"))
    days = {
        r["provider"]: r["day_of_cycle"]
        for r in conn.execute(
            "SELECT provider, day_of_cycle FROM schedule_state WHERE city_id = ?", (cid,)
        ).fetchall()
    }
    assert len(set(days.values())) == 1
    assert set(days) == {"gsv", "gsv_streets", "mapillary_streets"}


WALK_DATE = date(2026, 7, 28)


def _orphan_walk(
    conn,
    data_dir,
    *,
    provider="gsv",
    network_type="drive",
    spacing=15,
    samples=4,
    with_network_type_key=True,
    corrupt=False,
    write_csv=True,
    coverage_by_highway=None,
    lengths=None,
):
    """A finished road walk with artifacts on disk but no catalog row — the
    Berlin shape: the crawl completed and both files were written, then the
    subprocess died before register_street_walk.

    ``with_network_type_key=False`` reproduces an artifact written before the
    network-type series existed (it carries no such key at all)."""
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    stem = generate_streetwalk_filename(
        city.city_id,
        city.grid_width_m,
        city.grid_height_m,
        city.step_m,
        spacing,
        WALK_DATE,
        provider=provider,
        network_type=network_type,
    )
    csv_name = stem + ".csv.gz"
    coverage_name = streetwalk_coverage_filename(csv_name)

    if write_csv:
        with gzip.open(os.path.join(data_dir, csv_name), "wt", encoding="utf-8") as fh:
            fh.write("query_lat,query_lon,status\n")
            for i in range(samples):
                fh.write(f"44.0{i},-121.0{i},OK\n")

    if corrupt:
        with gzip.open(os.path.join(data_dir, coverage_name), "wt", encoding="utf-8") as fh:
            fh.write('{"type": "FeatureCollection", "featur')  # truncated mid-write
    else:
        metadata = {
            "schema_version": 1,
            "kind": "streetwalk_coverage",
            "city_id": city.city_id,
            "provider": provider,
            "run_date": WALK_DATE.isoformat(),
            "spacing_m": spacing,
            "match_dist_m": 25.0,
            "source_csv": csv_name,
            "totals": {
                "edges": 120,
                "edges_fully_covered": 100,
                "mean_edge_coverage": 0.82,
                "coverage_pct_by_length": 82.2,
                "coverage_pct_by_length_any": 85.0,
            },
        }
        # Left out by default: the base totals block above reproduces an
        # artifact written BEFORE the v12 length keys existed, which the
        # salvage path must tolerate. Pass `lengths` for a current artifact.
        if lengths is not None:
            metadata["totals"].update(lengths)
        if with_network_type_key:
            metadata["network_type"] = network_type
        if coverage_by_highway is not None:
            metadata["coverage_by_highway"] = coverage_by_highway
        with gzip.open(os.path.join(data_dir, coverage_name), "wt", encoding="utf-8") as fh:
            json.dump(
                {"type": "FeatureCollection", "features": [], "properties": {"metadata": metadata}},
                fh,
            )

    return city, csv_name, coverage_name


def _walk_row(conn, city_id, provider="gsv", network_type="drive"):
    return conn.execute(
        "SELECT * FROM street_walks WHERE city_id = ? AND provider = ? "
        "AND network_type = ? AND run_date = ?",
        (city_id, provider, network_type, WALK_DATE.isoformat()),
    ).fetchone()


def test_reconcile_orphaned_walk_catalogs_a_finished_crawl(conn, data_dir, monkeypatch):
    """The Berlin regression: a walk that crawled, wrote both artifacts, then died
    before register_street_walk must be salvaged from those artifacts rather than
    re-crawled at full cost next cycle."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    city, csv_name, coverage_name = _orphan_walk(conn, data_dir, samples=4)
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True

    row = _walk_row(conn, city.city_id)
    assert row is not None
    assert row["csv_filename"] == csv_name
    assert row["coverage_filename"] == coverage_name
    assert row["edges_total"] == 120
    assert row["coverage_pct_by_length"] == 82.2
    assert row["coverage_pct_by_length_any"] == 85.0
    assert row["match_dist_m"] == 25.0
    # sample_points is not in the artifact; it comes from the snapshot's rows,
    # and for GSV that row count is also the request count (one request each).
    assert row["sample_points"] == 4
    assert row["api_requests"] == 4


def test_reconcile_orphaned_walk_salvages_the_v12_lengths(conn, data_dir, monkeypatch):
    """A salvaged walk carries its street kilometres too. Without this the
    streets page would show the walk with a blank length forever: the backfill
    script only targets rows whose artifact it can still find, and nothing else
    ever revisits a salvaged row."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    city, _, _ = _orphan_walk(
        conn,
        data_dir,
        lengths={
            "length_km": 382.3,
            "length_km_covered": 314.25,
            "length_km_covered_any": 325.0,
            "median_covered_age_years": 1.75,
        },
    )
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True

    row = _walk_row(conn, city.city_id)
    assert row["length_km"] == 382.3
    assert row["length_km_covered"] == 314.25
    assert row["length_km_covered_any"] == 325.0
    assert row["median_covered_age_years"] == 1.75


def test_reconcile_orphaned_walk_tolerates_an_artifact_without_lengths(conn, data_dir, monkeypatch):
    """The salvage path reads whatever artifact is on disk, including ones
    written before the v12 keys existed. Their absence must leave NULLs, not
    raise — a KeyError here would turn a recoverable walk into a re-crawl at
    full API cost, which is the whole thing the salvage exists to prevent."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    city, _, _ = _orphan_walk(conn, data_dir)  # no `lengths` → pre-v12 artifact
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True

    row = _walk_row(conn, city.city_id)
    assert row["coverage_pct_by_length"] == 82.2  # the walk is still cataloged
    assert row["length_km"] is None
    assert row["median_covered_age_years"] is None


def test_reconcile_orphaned_walk_refreshes_the_manifest(conn, data_dir, monkeypatch):
    """A salvaged walk that isn't advertised is invisible: the city page finds
    streetwalk artifacts only through the sidecar manifest."""
    from streetscape_metadata_tracker import scheduler as sched

    regenerated = []
    monkeypatch.setattr(
        sched,
        "generate_streetwalk_manifest",
        lambda c, d: regenerated.append(d) or {"walks": []},
    )
    city, _, _ = _orphan_walk(conn, data_dir)
    cfg = _street_cfg(data_dir=data_dir)
    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE)
    assert regenerated == [cfg.data_dir]


def test_reconcile_orphaned_walk_without_artifacts_is_a_genuine_failure(conn, data_dir):
    """Nothing on disk → nothing to salvage; the caller records a real failure."""
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is False
    assert _walk_row(conn, city.city_id) is None


def test_reconcile_orphaned_walk_rejects_a_corrupt_artifact(conn, data_dir):
    """A truncated coverage file says nothing about coverage and must not become
    a catalog row — that would publish a walk with no numbers behind it."""
    city, _, _ = _orphan_walk(conn, data_dir, corrupt=True)
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is False
    assert _walk_row(conn, city.city_id) is None


def test_reconcile_orphaned_walk_takes_network_type_from_the_channel(conn, data_dir, monkeypatch):
    """Walks written before the network-type series carry no network_type key
    (Berlin's does not), so the channel's configured type is authoritative —
    and it is part of the street_walks key, so getting it wrong mis-files the row."""
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.scheduler import ProviderConfig

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    city, csv_name, _ = _orphan_walk(
        conn, data_dir, network_type="all_public", with_network_type_key=False
    )
    cfg = _street_cfg(data_dir=data_dir)
    cfg.providers["gsv_streets"] = ProviderConfig(network_type="all_public")

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True

    row = _walk_row(conn, city.city_id, network_type="all_public")
    assert row is not None
    assert "allpublic" in row["csv_filename"]  # the artifact it actually salvaged


def test_reconcile_orphaned_walk_leaves_mapillary_requests_null(conn, data_dir, monkeypatch):
    """Mapillary's cost is a z14 tile census independent of the sample count, and
    the artifacts don't record it. NULL means 'not measured' — writing the sample
    count there would invent a cost the walk never paid."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    city, _, _ = _orphan_walk(conn, data_dir, provider="mapillary", samples=6)
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "mapillary_streets", WALK_DATE) is True

    row = _walk_row(conn, city.city_id, provider="mapillary")
    assert row["sample_points"] == 6
    assert row["api_requests"] is None


def test_reconcile_salvages_coverage_by_highway(conn, data_dir, monkeypatch):
    """The per-bucket breakdown rides along in the artifact metadata (issue
    #101), so the salvage catalogs it like every other stat."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    breakdown = {"residential": {"edges": 80, "coverage_pct_by_length": 84.0}}
    city, _, _ = _orphan_walk(conn, data_dir, coverage_by_highway=breakdown)
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True
    row = _walk_row(conn, city.city_id)
    assert json.loads(row["coverage_by_highway"]) == breakdown


def test_reconcile_tolerates_artifact_without_breakdown(conn, data_dir, monkeypatch):
    """A pre-#101 artifact carries no coverage_by_highway key; the column stays
    NULL and the salvage still succeeds — a missing breakdown must never cost
    a fully-paid-for crawl its catalog row."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    city, _, _ = _orphan_walk(conn, data_dir)  # default: no breakdown key
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True
    assert _walk_row(conn, city.city_id)["coverage_by_highway"] is None


def test_reconcile_computes_diff_when_previous_walk_exists(conn, data_dir, monkeypatch):
    """The salvage path is exactly where the collect-side diff was lost (the
    crash landed in the tail), so reconcile computes it too (issue #101)."""
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.naming import generate_streetwalk_filename as gen_walk

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    city, _, _ = _orphan_walk(conn, data_dir)

    # A prior cataloged walk of the same series and sample frame, with its
    # artifact on disk (empty feature list — the diff still records a row).
    prev_date = date(2026, 5, 1)
    prev_stem = gen_walk(
        city.city_id, city.grid_width_m, city.grid_height_m, city.step_m, 15, prev_date
    )
    prev_csv = prev_stem + ".csv.gz"
    prev_coverage = streetwalk_coverage_filename(prev_csv)
    with gzip.open(os.path.join(data_dir, prev_coverage), "wt", encoding="utf-8") as fh:
        json.dump(
            {
                "type": "FeatureCollection",
                "features": [],
                "properties": {"metadata": {"totals": {"coverage_pct_by_length": 80.0}}},
            },
            fh,
        )
    prev_walk_id = db.register_street_walk(
        conn,
        city_id=city.city_id,
        run_date=prev_date,
        csv_filename=prev_csv,
        coverage_filename=prev_coverage,
        spacing_m=15.0,
        match_dist_m=25.0,
    )
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True

    salvaged = _walk_row(conn, city.city_id)
    diff_row = db.get_walk_diff_for_walk(conn, salvaged["walk_id"])
    assert diff_row is not None
    assert diff_row["from_walk_id"] == prev_walk_id
    assert diff_row["from_run_date"] == prev_date.isoformat()


def test_reconcile_diff_failure_still_salvages(conn, data_dir, monkeypatch):
    """A diff bug must never turn a successful salvage into a recorded failure
    — that would re-crawl the city at full cost, defeating the salvage."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    def boom(*a, **k):
        raise RuntimeError("diff exploded")

    monkeypatch.setattr(sched, "compute_and_record_walk_diff", boom)
    city, _, _ = _orphan_walk(conn, data_dir)
    cfg = _street_cfg(data_dir=data_dir)

    assert _reconcile_orphaned_walk(conn, cfg, city, "gsv_streets", WALK_DATE) is True
    assert _walk_row(conn, city.city_id) is not None


def test_run_due_salvages_a_finished_walk_instead_of_recording_failure(conn, monkeypatch, data_dir):
    """End-to-end: the collector subprocess reports failure but left a complete
    walk on disk. run-due must catalog it and count the channel a success, so the
    city isn't re-walked at full cost when it next comes due."""
    from streetscape_metadata_tracker import scheduler as sched

    city, _, _ = _orphan_walk(conn, data_dir)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            False
        ),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    sched.cmd_run_due(_street_cfg(data_dir=data_dir, publish_enabled=False), today=WALK_DATE)

    assert _walk_row(conn, city.city_id) is not None
    state = conn.execute(
        "SELECT last_success_at, consecutive_failures FROM schedule_state "
        "WHERE city_id = ? AND provider = 'gsv_streets'",
        (city.city_id,),
    ).fetchone()
    assert state["last_success_at"] is not None
    assert state["consecutive_failures"] == 0
    # The grid channel genuinely failed and must still be recorded as such.
    grid = conn.execute(
        "SELECT last_success_at FROM schedule_state WHERE city_id = ? AND provider = 'gsv'",
        (city.city_id,),
    ).fetchone()
    assert grid["last_success_at"] is None


def test_reconcile_walks_command_finds_and_catalogs_orphans(conn, monkeypatch, data_dir, capsys):
    """The operator handle for orphans run-due can't catch — a walk from the
    manual CLI, or one whose scheduler process itself died."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": [1]})
    city, _, coverage_name = _orphan_walk(conn, data_dir)
    cfg = _street_cfg(data_dir=data_dir)

    assert cmd_reconcile_walks(cfg, target_date=WALK_DATE, dry_run=True) == 0
    assert coverage_name in capsys.readouterr().out
    assert _walk_row(conn, city.city_id) is None  # dry run wrote nothing

    assert cmd_reconcile_walks(cfg, target_date=WALK_DATE) == 0
    assert _walk_row(conn, city.city_id) is not None

    # The salvage must also clear the recorded failure, or it is cosmetic: the
    # city would stay due and be re-crawled anyway, which is the whole cost the
    # reconcile exists to avoid.
    state = conn.execute(
        "SELECT last_success_at, consecutive_failures FROM schedule_state "
        "WHERE city_id = ? AND provider = 'gsv_streets'",
        (city.city_id,),
    ).fetchone()
    assert state["last_success_at"] is not None
    assert state["consecutive_failures"] == 0

    # Idempotent: a second pass sees the row and reports nothing to do.
    assert cmd_reconcile_walks(cfg, target_date=WALK_DATE) == 0
    assert "No orphaned walks found" in capsys.readouterr().out


def test_street_channel_failure_is_not_reconciled_as_an_orphan_run(conn, monkeypatch, data_dir):
    """Street channels write street_walks rows, not `runs` rows, so the
    orphaned-run salvage path must not be consulted for them (it would look up
    a run that can never exist and could mask a real failure). They get their own
    artifact-based salvage instead — see _reconcile_orphaned_walk."""
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Bend", width=1000, height=1000, step=20)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    called = []
    monkeypatch.setattr(
        sched,
        "_reconcile_orphaned_run",
        lambda *a, **k: called.append(a[3]) or False,
    )
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            False
        ),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    sched.cmd_run_due(_street_cfg(data_dir=data_dir, publish_enabled=False), today=date(2026, 7, 2))

    assert "gsv" in called
    assert "gsv_streets" not in called
    assert "mapillary_streets" not in called


def test_failed_collection_captures_child_output_and_surfaces_the_tail(conn, tmp_path, caplog):
    """A child's traceback used to be thrown away: the collectors log to stderr,
    the scheduler inherited it, and under systemd that goes to a journal the
    service account cannot read — so every 'collection failed' line lost its
    cause. The output must land in a per-attempt log AND its tail must reach the
    scheduler log, which is what the [alerts] email actually sends."""
    import logging
    import sys

    from streetscape_metadata_tracker import scheduler as sched

    cfg = SchedulerConfig(log_dir=str(tmp_path))
    city = db.resolve_city(conn, _register(conn, "Bend"))
    cmd = [sys.executable, "-c", "import sys; print('TRACEBACK MARKER'); sys.exit(3)"]

    with caplog.at_level(logging.ERROR):
        ok = sched._run_collection_subprocess(cfg, cmd, 60, city, "gsv", date(2026, 7, 1))

    assert not ok, "a nonzero exit is a failed collection"
    # The reason rides back to the caller so it can reach schedule_state.
    assert "exited 3" in ok.reason and "collect_" in ok.reason
    log_path = tmp_path / f"collect_{city.city_id}_gsv_2026-07-01.log"
    assert "TRACEBACK MARKER" in log_path.read_text()
    assert "exited 3" in caplog.text
    assert str(log_path) in caplog.text  # operator is told where the full log is
    assert "TRACEBACK MARKER" in caplog.text  # and the cause travels to the alert


def test_collection_log_appends_rather_than_truncating_a_retry(conn, tmp_path):
    """Re-running the same city/channel/day must add to the record, not destroy
    the failure being diagnosed."""
    import sys

    from streetscape_metadata_tracker import scheduler as sched

    cfg = SchedulerConfig(log_dir=str(tmp_path))
    city = db.resolve_city(conn, _register(conn, "Bend"))
    today = date(2026, 7, 1)

    for marker in ("FIRST ATTEMPT", "SECOND ATTEMPT"):
        sched._run_collection_subprocess(
            cfg, [sys.executable, "-c", f"print('{marker}')"], 60, city, "gsv", today
        )

    text = (tmp_path / f"collect_{city.city_id}_gsv_2026-07-01.log").read_text()
    assert "FIRST ATTEMPT" in text and "SECOND ATTEMPT" in text


# ── Batch deadline / always-publish (issue #167) ───────────────────────────


def _publishing_cfg(**overrides):
    """A config whose tail is fully observable: publish on, alerts off."""
    base = dict(
        daily_request_budget=10_000_000,
        publish_enabled=True,
        max_cities_per_day=20,
    )
    base.update(overrides)
    return SchedulerConfig(**base)


def _stub_tail(monkeypatch, sched, conn, published):
    """Stub the batch tail so a test can assert it ran, without touching disk."""
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: published.append("aggregate"))
    monkeypatch.setattr(
        sched, "generate_streetwalk_manifest", lambda c, d: published.append("manifest") or {}
    )
    monkeypatch.setattr(
        sched, "_publish", lambda cfg, summary, **kw: published.append("publish") or 0
    )


class _WorkClock:
    """A fake ``time.monotonic`` that advances when WORK happens, not when read.

    The obvious spelling — an iterator stepping on every read — makes the
    batch's elapsed time a function of how many times the scheduler happens to
    call ``time.monotonic()``, which is not a contract anyone signed up to
    keep. #240 moved those reads (the deadline is now priced once per LAUNCHED
    channel rather than once per channel considered), and a read-counting clock
    would have failed for that reason alone while the deadline itself behaved
    perfectly.

    Advancing inside the fake collection instead ties the clock to the thing the
    deadline is actually about — time spent collecting — so these tests keep
    meaning what they say the next time a read moves.

    Never combine one with ``max_concurrent_channels > 1`` unless a single
    thread owns every advance: with real lanes the workers run concurrently and
    a hand-advanced clock stops being deterministic. The lane tests that do need
    a clock advance it from the launch path, which is main-thread by design.
    """

    def __init__(self, per_unit_s: float = 3600.0):
        self.t = 0.0
        self.per_unit_s = per_unit_s

    def __call__(self) -> float:
        return self.t

    def work(self, units: float = 1.0) -> None:
        self.t += self.per_unit_s * units


def test_batch_deadline_stops_the_loop_but_still_publishes(conn, monkeypatch):
    """The whole point of the deadline: a night that runs long stops STARTING
    cities and still regenerates + publishes. Before this, systemd's
    TimeoutStartSec SIGKILLed the loop and every city already collected stayed
    invisible on the public site (issue #167)."""
    from streetscape_metadata_tracker import scheduler as sched

    for name in ("Alpha", "Beta", "Gamma"):
        _register(conn, name, width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    ran, published = [], []
    clock = _WorkClock()
    monkeypatch.setattr(sched.time, "monotonic", clock)

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        ran.append(city.city_id)
        clock.work()  # each city "takes" an hour of the batch's wall clock
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    _stub_tail(monkeypatch, sched, conn, published)

    # 2 h of budget: the deadline bites well before the 3 due cities are done.
    rc = sched.cmd_run_due(_publishing_cfg(max_batch_hours=2), today=date(2026, 7, 2))

    assert len(ran) < 3, "deadline should have stopped the loop early"
    assert published == ["aggregate", "manifest", "publish"], (
        "a deadline-stopped night must still publish what it collected"
    )
    assert rc == 0, "stopping at the deadline is not a failure"


def test_sigterm_winds_the_batch_down_instead_of_killing_the_publish(conn, monkeypatch):
    """systemd stops the unit with SIGTERM. Under the default handler that lands
    mid-loop and the night publishes nothing; the handler turns it into a stop
    request checked between cities.

    This pins the OUTER (city-boundary) half and the handler restoration. Its
    inner-loop counterpart — a stop landing between a city's own channels — is
    test_sigterm_stops_the_city_mid_channel_… below (issue #206); the two are
    not duplicates, and this one cannot see that bug because _publishing_cfg is
    gsv-only."""
    from streetscape_metadata_tracker import scheduler as sched

    for name in ("Alpha", "Beta", "Gamma"):
        _register(conn, name, width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    ran, published = [], []

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        ran.append(city.city_id)
        os.kill(os.getpid(), signal.SIGTERM)  # as systemd would, mid-batch
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    _stub_tail(monkeypatch, sched, conn, published)

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert len(ran) == 1, "the loop should stop after the city that saw SIGTERM"
    assert published == ["aggregate", "manifest", "publish"]
    assert rc == 0
    # The handler must be uninstalled again, or a later SIGTERM is swallowed.
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler)


# ── SIGTERM reaches a city's CHANNELS, not just its boundary (issue #206) ────
#
# None of these stub time.monotonic, deliberately. The rule, restated after #240
# moved the reads: stub the clock only with a work-advanced one (_WorkClock
# above), never with anything that steps on a read, and never at all in a test
# that runs more than one lane. _run_city_channels reads the clock once per
# LAUNCHED channel, so a per-read iterator would blow the batch deadline inside
# the first city and stop the loop for the wrong reason — the test would pass
# while asserting nothing about SIGTERM. max_batch_hours defaults to 10 h
# against the real clock, which is ample.


def _sigterm_cfg(**overrides):
    """A four-channel config whose publish tail is observable.

    _publishing_cfg is gsv-only, which is exactly why the pre-#206 SIGTERM test
    could not see the inner-loop defect: with one channel per city the inner loop
    never has a second provider to launch.
    """
    return _street_cfg(publish_enabled=True, max_cities_per_day=20, **overrides)


def test_sigterm_stops_the_city_mid_channel_instead_of_finishing_its_channels(
    conn, monkeypatch, caplog
):
    """A stop must not launch the rest of the in-flight city's channels.

    Before #206 it did: `sigterm_seen` was checked only in _run_city_loop's outer
    `for city in due`, so a stop during a city's first channel still ran the
    other three. With Mapillary enabled those are fired straight into a live
    per-IP tile block (#205) — the exact thing an operator typing `stop` is
    usually trying to prevent.

    The child here RETURNS before the signal reaches it, which is the uncommon
    shape: it exercises the top-of-loop exit. The common one — the child dying
    of the same cgroup SIGTERM — is
    test_a_child_killed_by_the_stop_… below, and both must name the channels
    they declined.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    for name in ("Alpha", "Beta"):
        _register(conn, name, width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    ran, published = [], []

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        ran.append((city.city_id, provider))
        if provider == "gsv":
            os.kill(os.getpid(), signal.SIGTERM)  # as systemd would, mid-city
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    _stub_tail(monkeypatch, sched, conn, published)

    with caplog.at_level(logging.INFO):
        rc = sched.cmd_run_due(_sigterm_cfg(), today=date(2026, 7, 2))

    assert [p for _, p in ran] == ["gsv"], (
        "the stop must land before the city's next channel is launched"
    )
    assert not [p for _, p in ran if p.startswith("mapillary")], (
        "a stop must never launch a Mapillary channel into a live tile block"
    )
    assert len({c for c, _ in ran}) == 1, "and no further city may be started"
    assert published == ["aggregate", "manifest", "publish"], (
        "a stopped night must still publish what it collected"
    )
    assert rc == 0, "an operator-requested stop is not an unhealthy night"
    assert "not starting gsv_streets, mapillary, mapillary_streets" in caplog.text, (
        "a wind-down must NAME the channels it declined: they are the ones an "
        "operator stopped the batch to keep away from a live tile block, and "
        "'nothing was collected' and 'three channels were declined' are "
        "different facts"
    )


def test_a_stop_skips_the_inter_city_sleep(conn, monkeypatch):
    """60 s of a 30-minute stop window (TimeoutStopSec) spent waiting to notice a
    flag that is already set — and PEP 475 makes time.sleep RESUME after the
    handler runs rather than returning early, so the whole interval is burned.
    """
    from streetscape_metadata_tracker import scheduler as sched

    for name in ("Alpha", "Beta"):
        _register(conn, name, width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    slept = []

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    _stub_tail(monkeypatch, sched, conn, [])
    # After _stub_tail, which no-ops sleep wholesale.
    monkeypatch.setattr(sched.time, "sleep", lambda s: slept.append(s))

    sched.cmd_run_due(_sigterm_cfg(), today=date(2026, 7, 2))

    assert slept == [], "a wind-down must not pause between cities it will never start"


def test_a_stop_on_the_last_due_city_still_reports_the_night_was_cut_short(
    conn, monkeypatch, caplog
):
    """The outer check only runs at the TOP of an iteration, so a stop during the
    LAST due city would fall out of the `for` normally and summarize as a
    complete night — while that city's remaining channels went uncollected. A
    night that quietly did not collect is the shape of failure #145 exists to
    make impossible.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    _stub_tail(monkeypatch, sched, conn, [])

    with caplog.at_level(logging.INFO):
        rc = sched.cmd_run_due(_sigterm_cfg(), today=date(2026, 7, 2))

    assert f"Stopped early: {sched._STOP_REASON_SIGTERM}" in caplog.text
    assert f"stopped early ({sched._STOP_REASON_SIGTERM})" in caplog.text, (
        "the Done: summary is what the [alerts] email carries; it must say the night was cut short"
    )
    assert rc == 0


def test_a_child_killed_by_the_stop_is_not_recorded_as_the_citys_failure(conn, monkeypatch, caplog):
    """KillMode defaults to control-group, so the stop that ends the batch also
    kills the in-flight child — the 2026-08-13 log shows it as `exited -15`, a
    code in neither HOST_BY_EXIT_CODE nor HOST_BY_BUSY_EXIT_CODE, i.e. an
    ordinary collection failure.

    Charging that to the city is wrong twice over, and both halves are the
    argument the blocked/busy branches next door already make (#208): it burns
    one of five `consecutive_failures` that ONLY a success ever resets, and it
    makes attempted > succeeded, so every deliberate `systemctl stop` would email
    a failure alert and end the unit red.

    This is also the exit a real stop TAKES — the child dies before the loop can
    come back around to the top-of-loop check — so it is the one that has to
    name the declined channels. While that message lived only at the top of the
    loop, the entire operator-visible record of a stopped four-channel night was
    "child was killed by the stop signal" and nothing about the three channels
    silently dropped with it.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    alerts = []

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        # What _run_collection_subprocess builds for a cgroup-killed child.
        return sched.CollectionOutcome(False, "exited -15 (see collect_alpha_gsv.log)", -15)

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: alerts.append(a))
    _stub_tail(monkeypatch, sched, conn, [])

    with caplog.at_level(logging.INFO):
        rc = sched.cmd_run_due(_sigterm_cfg(), today=date(2026, 7, 2))

    assert rc == 0, "a child we killed on purpose is not an unhealthy night"
    assert alerts == [], "stopping the batch must not email a failure alert"
    assert "not starting gsv_streets, mapillary, mapillary_streets" in caplog.text, (
        "the exit a real stop takes must name the declined channels too — the "
        "killed child is reported on its own line, so this lists what comes "
        "AFTER it, not including it"
    )
    assert "not starting gsv," not in caplog.text, (
        "the killed channel was started; listing it as declined would misreport "
        "where the batch actually stopped"
    )
    failures = conn.execute(
        "SELECT consecutive_failures FROM schedule_state WHERE provider = 'gsv'"
    ).fetchone()[0]
    assert failures == 0, (
        "nothing resets consecutive_failures except a success, so a handful of "
        "stops would quarantine the city for a whole cycle"
    )


def test_a_stop_on_a_citys_last_channel_declines_nothing_out_loud(caplog):
    """The killed-child exit passes `providers[i + 1:]`, which is EMPTY when the
    stop lands on a city's last channel. A wind-down that logged "not starting"
    with an empty list would read as work skipped that never existed — the
    inverse of the bug this message exists to fix, and just as misleading in an
    incident.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    with caplog.at_level(logging.INFO):
        sched._log_stop_declined("alpha", [])
    assert "not starting" not in caplog.text

    with caplog.at_level(logging.INFO):
        sched._log_stop_declined("alpha", ["mapillary_streets"])
    assert "alpha: stop requested — not starting mapillary_streets" in caplog.text


def test_run_city_channels_requires_an_explicit_stop_signal():
    """Mirrors the _collect_due guard: no fail-open default may exist for a
    future caller to inherit. `stop_requested=None` means "nothing can ask this
    to stop", which is right for an operator's foreground run and wrong for a
    batch — and the difference is invisible until someone types `systemctl stop`.

    `batch_deadline` is asserted alongside it because its own docstring makes
    exactly this argument (#214/#167) while nothing pinned it.
    """
    import inspect

    from streetscape_metadata_tracker import scheduler as sched

    params = inspect.signature(sched._run_city_channels).parameters
    for name in ("stop_requested", "batch_deadline"):
        assert params[name].default is inspect.Parameter.empty, (
            f"_run_city_channels must require an explicit {name}; a None default "
            f"silently disables the guard and fails open"
        )


def test_loop_error_still_publishes_but_reports_an_unhealthy_night(conn, monkeypatch):
    """Publishing what was collected must not silence a real bug: the batch
    still exits nonzero and alerts."""
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    published, alerts = [], []

    def boom(cfg, city, today, provider="gsv", **kwargs):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(sched, "_run_one_city", boom)
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: alerts.append(a))
    _stub_tail(monkeypatch, sched, conn, published)

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert rc == 1, "a crashed loop is an unhealthy night even though it published"
    assert alerts, "the operator must still be told"


def test_city_timeout_is_clamped_to_the_remaining_batch_budget(conn):
    """A big city started just inside the deadline would otherwise run its full
    derived timeout and blow straight through it."""
    from streetscape_metadata_tracker import scheduler as sched

    city = db.resolve_city(conn, _register(conn, "Huge", width=60_000, height=60_000, step=20))
    cfg = SchedulerConfig()

    unclamped = sched.city_timeout_seconds(cfg, city, "gsv")
    clamped = sched.city_timeout_seconds(cfg, city, "gsv", remaining_s=1800)

    assert unclamped > 1800, "this city is meant to want more than the remainder"
    assert clamped == 1800
    # Never clamp below the floor that lets a child reach its first request.
    assert sched.city_timeout_seconds(cfg, city, "gsv", remaining_s=1) == 300


def test_last_error_records_the_real_cause_not_a_generic_string(conn, tmp_path, monkeypatch):
    """Every failure in the catalog used to read "subprocess failed on <date>",
    so a bad night had to be re-derived from daily-rotated logs the next
    morning. The recorded cause must name what actually happened and which log
    to read (issue #169)."""
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda *a, **k: sched.CollectionOutcome(
            False, "timed out after 180 minutes (see collect_alpha_mapillary_2026-07-02.log)"
        ),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    monkeypatch.setattr(sched, "_reconcile_orphaned_run", lambda *a, **k: False)

    sched.cmd_run_due(
        SchedulerConfig(publish_enabled=False, log_dir=str(tmp_path)), today=date(2026, 7, 2)
    )

    recorded = conn.execute(
        "SELECT last_error FROM schedule_state WHERE provider = 'gsv'"
    ).fetchone()["last_error"]
    assert "timed out after 180 minutes" in recorded
    assert "collect_alpha_mapillary_2026-07-02.log" in recorded, "must name the log to read"
    assert "subprocess failed on" not in recorded


def test_a_plain_bool_from_run_one_city_still_works(conn, tmp_path, monkeypatch):
    """CollectionOutcome is deliberately bool-compatible; a caller returning a
    bare False must still record a failure rather than crash."""
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    monkeypatch.setattr(sched, "_run_one_city", lambda *a, **k: False)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    monkeypatch.setattr(sched, "_reconcile_orphaned_run", lambda *a, **k: False)

    sched.cmd_run_due(
        SchedulerConfig(publish_enabled=False, log_dir=str(tmp_path)), today=date(2026, 7, 2)
    )

    row = conn.execute(
        "SELECT consecutive_failures, last_error FROM schedule_state WHERE provider = 'gsv'"
    ).fetchone()
    assert row["consecutive_failures"] == 1
    assert "subprocess failed on 2026-07-02" in row["last_error"]  # the fallback


# ── Driving-plan feed snapshots (issue #176) ───────────────────────────────


_PLAN_RECORD = {
    "country": "United States",
    "code": "US",
    "svspc": "SV",
    "region": "Kentucky",
    "districts": "Jefferson, Bullitt",
    "publish": "Yes",
    "datestart": "2026-02-02T08:00:00.000Z",
    "dateend": "2026-12-31T08:00:00.000Z",
}


def test_config_parses_driving_plan_section(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(
        '[driving_plan]\nenabled = false\narchive_dir = "/somewhere/plans"\n'
        'url = "https://example.com/feed.json"\ntimeout_s = 10.0\n'
    )
    cfg = load_scheduler_config(str(p))
    assert not cfg.driving_plan.enabled
    assert cfg.driving_plan.archive_dir == "/somewhere/plans"
    assert cfg.driving_plan.url == "https://example.com/feed.json"
    assert cfg.driving_plan.timeout_s == 10.0


def test_config_driving_plan_defaults_when_section_missing(tmp_path):
    """No [driving_plan] section means ON with the repo-local archive dir, so
    prod picks the feature up on deploy without a config edit."""
    p = tmp_path / "s.toml"
    p.write_text("[schedule]\ncycle_days = 90\n")
    cfg = load_scheduler_config(str(p))
    assert cfg.driving_plan.enabled
    assert cfg.driving_plan.archive_dir.endswith(os.path.join("archive", "gsv_driving_plan"))


def test_run_due_snapshots_driving_plan_even_on_a_zero_due_night(conn, monkeypatch):
    """The hook sits BEFORE the city loop, so a night with nothing due still
    archives the feed — the whole point of a daily observation series."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "_fetch_driving_plan_nightly", _REAL_DRIVING_PLAN_HOOK)
    ingested = []
    monkeypatch.setattr(
        sched.driving_plan,
        "ingest",
        lambda c, **kw: ingested.append(kw["fetch_date"]) or _ingest_result(),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    rc = sched.cmd_run_due(SchedulerConfig(publish_enabled=False), today=date(2026, 7, 2))

    assert ingested == [date(2026, 7, 2)]
    assert rc == 0


def _ingest_result(**overrides):
    from streetscape_metadata_tracker.driving_plan import IngestResult

    base = dict(
        snapshot_id=1,
        fetch_date="2026-07-02",
        skipped=False,
        changed=False,
        record_count=1,
        entry_count=0,
    )
    base.update(overrides)
    return IngestResult(**base)


def test_driving_plan_failure_never_stops_the_night_but_does_report_it(conn, monkeypatch):
    """The feed is an undocumented asset with no uptime contract, so it breaking
    must not cost a night of collection (the issue #167 posture) — the loop runs,
    the indexes rebuild, the publish happens.

    It IS reported, though, and that is the half this test used to have
    backwards. Google overwrites the feed in place, so a night we fail to
    snapshot is a revision that no later run can recover — strictly worse than
    the plan *summary* rebuild beside it, which is regenerable from the catalog
    whenever we like and which already alerted and exited nonzero. Leaving the
    fetch silent and green meant a week of blocked fetches was a week of clean
    nights and seven snapshots gone for good.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    monkeypatch.setattr(sched, "_fetch_driving_plan_nightly", _REAL_DRIVING_PLAN_HOOK)

    def boom(c, **kw):
        raise RuntimeError("feed gone")

    monkeypatch.setattr(sched.driving_plan, "ingest", boom)
    ran, published = [], []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **kw: ran.append(city.city_id) or True,
    )
    _stub_tail(monkeypatch, sched, conn, published)

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert len(ran) == 1, "the city loop must still run"
    assert published == ["aggregate", "manifest", "publish"], (
        "a missed snapshot must never withhold what the night collected"
    )
    assert rc == 1, "a permanently unrecoverable miss is an unhealthy night"
    assert len(alerts) == 1
    assert "DRIVING-PLAN FETCH FAILED" in alerts[0][0], (
        "the subject is often all that gets read; it has to name this"
    )
    assert "feed gone" in alerts[0][1], "the alert must carry the cause"


def test_a_failing_plan_summary_does_not_take_down_the_rest_of_the_tail(conn, monkeypatch):
    """
    The tail's plan-summary regeneration sits AHEAD of the tail catalog backup
    and the publish, and — unlike the aggregate and the manifest — is ungated,
    so it runs on every night including quiet ones. It also touches up to
    ~1,200 per-run JSONs on disk, which is real exposure to an OSError. Before
    the guard, one of those would have cost a completely healthy night its
    backup AND its publish: issue #167's exact failure mode, paid for the
    least important artifact in the tail.

    "Least important" bounds the BLAST RADIUS, not the reporting: the failure
    also alerts and exits nonzero. Logging and continuing silently — the
    original behavior — let a permanently broken plan page rot while every night
    still reported a clean success, which is the observability half of #145's
    lesson.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    def boom(c, data_dir):
        raise OSError("data_dir vanished mid-tail")

    monkeypatch.setattr(sched, "generate_driving_plan_summary", boom)

    backups = []
    monkeypatch.setattr(
        sched.catalog_backup,
        "write_backup",
        lambda conn, backup_dir, when, **kw: (
            backups.append(when)
            or sched.catalog_backup.BackupResult(
                ok=True, path=os.path.join(backup_dir, "stubbed.backup")
            )
        ),
    )

    ran, published = [], []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **kw: ran.append(city.city_id) or True,
    )
    _stub_tail(monkeypatch, sched, conn, published)

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert len(ran) == 1
    assert published == ["aggregate", "manifest", "publish"], (
        "a stale plan page costs a day; an unpublished night costs the runs"
    )
    assert len(backups) == 2, "both the pre-flight and the TAIL backup must still happen"
    assert rc == 1, "a failed index is reported even though it cost the tail nothing"
    assert len(alerts) == 1
    assert "1 published index(es) FAILED" in alerts[0][0]
    assert "driving-plan summary failed" in alerts[0][1], "the alert must name WHICH index broke"
    assert "data_dir vanished mid-tail" in alerts[0][1]


def test_driving_plan_hook_respects_dry_run_and_enabled_flag(conn, monkeypatch, capsys):
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "_fetch_driving_plan_nightly", _REAL_DRIVING_PLAN_HOOK)
    ingested = []
    monkeypatch.setattr(
        sched.driving_plan, "ingest", lambda c, **kw: ingested.append(1) or _ingest_result()
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    # Dry run: announced but not fetched.
    rc = sched.cmd_run_due(SchedulerConfig(), dry_run=True, today=date(2026, 7, 2))
    assert rc == 0 and not ingested
    assert "driving-plan" in capsys.readouterr().out

    # Disabled: not fetched, not announced.
    cfg = SchedulerConfig()
    cfg.driving_plan.enabled = False
    rc = sched.cmd_run_due(cfg, today=date(2026, 7, 2))
    assert rc == 0 and not ingested


def test_cmd_fetch_driving_plan_backfills_from_file(conn, monkeypatch, tmp_path, capsys):
    """--from-file + --date ingests an already-saved raw feed — the handle for
    cataloging the manual snapshot kept on prod before this existed."""
    from streetscape_metadata_tracker import scheduler as sched

    saved = tmp_path / "data.json"
    saved.write_text(json.dumps([_PLAN_RECORD]))
    archive = tmp_path / "archive"
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    cfg = SchedulerConfig()
    cfg.driving_plan.archive_dir = str(archive)

    rc = cmd_fetch_driving_plan(cfg, from_file=str(saved), target_date=date(2026, 7, 31))

    assert rc == 0
    assert "CHANGED" in capsys.readouterr().out
    row = conn.execute("SELECT * FROM driving_plan_snapshots").fetchone()
    assert row["fetch_date"] == "2026-07-31" and row["changed"] == 1
    assert (archive / "gsv_driving_plan_2026-07-31.json.gz").exists()
    districts = {r["district"] for r in conn.execute("SELECT district FROM driving_plan_entries")}
    assert districts == {"Jefferson", "Bullitt"}


# ───────────────────── catalog backup wiring (issue #145) ─────────────────────


def _real_backup_cfg(tmp_path, **overrides):
    """A config whose backup hooks write real files, into tmp_path."""
    base = dict(publish_enabled=False, backup_dir=str(tmp_path / "backups"))
    base.update(overrides)
    return SchedulerConfig(**base)


def test_backup_runs_before_the_city_loop(conn, monkeypatch, tmp_path):
    """
    The ordering IS the feature. _finish_batch runs after any loop-level failure
    (errored loop, deadline, SIGTERM — #167) but NOT after a SIGKILL, which is
    the documented OOM mode on the Mapillary post-decode path (#157). A
    tail-only backup is therefore missing on exactly the nights something went
    badly wrong, so the night's copy has to exist before the loop starts.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)

    order = []
    monkeypatch.setattr(
        sched,
        "_backup_catalog_nightly",
        lambda cfg, c, today: order.append("backup") or _REAL_BACKUP_HOOK(cfg, c, today),
    )
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **kw: order.append("city") or True,
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})

    rc = sched.cmd_run_due(_real_backup_cfg(tmp_path), today=date(2026, 7, 2))

    assert rc == 0
    assert order[0] == "backup", f"backup must precede the city loop, got {order}"
    assert "city" in order
    assert (tmp_path / "backups" / "streetscape_tracker.db.2026-07-02.backup").exists()


def test_backup_happens_on_a_zero_due_night(conn, monkeypatch, tmp_path):
    """No cities due still means the catalog changed yesterday and is worth a
    dated copy — and the tail's aggregate step is skipped on such nights, so a
    tail-only backup would silently not happen."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    rc = sched.cmd_run_due(_real_backup_cfg(tmp_path), today=date(2026, 7, 2))

    assert rc == 0
    assert (tmp_path / "backups" / "streetscape_tracker.db.2026-07-02.backup").exists()


def test_driving_plan_summary_is_regenerated_on_a_zero_due_night(conn, monkeypatch, tmp_path):
    """
    The tail's aggregate and streetwalk manifest are gated on `succeeded > 0`
    because they describe the runs the night collected. The driving-plan
    summary must NOT be: it describes Google's feed, which the pre-loop hook
    refreshes independently and which changes roughly weekly regardless of
    whether any city was due. Gating it would leave the published plan stale on
    exactly the quiet nights, which are most of them.
    """
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched, "generate_driving_plan_summary", _REAL_PLAN_SUMMARY)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    data_dir = tmp_path / "data"
    rc = sched.cmd_run_due(
        _real_backup_cfg(tmp_path, data_dir=str(data_dir)), today=date(2026, 7, 2)
    )

    assert rc == 0
    artifact = data_dir / "driving_plan.json.gz"
    assert artifact.exists(), "a zero-due night must still refresh the published plan"
    with gzip.open(artifact, "rt", encoding="utf-8") as f:
        assert json.load(f)["schema_version"] == 1


def test_a_failed_backup_makes_the_night_unhealthy_but_still_publishes(conn, monkeypatch):
    """
    Two halves of the #145 lesson at once. A backup that fails silently is how
    /projects/makeabilitylab went unbacked-up for months, so it must alert and
    turn the unit red even when every city landed. But it must NOT withhold
    publishing — that is the #167 posture: never hide what the night collected.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    monkeypatch.setattr(
        sched.catalog_backup,
        "write_backup",
        lambda c, d, w, **kw: sched.catalog_backup.BackupResult(ok=False, error="disk full"),
    )
    ran, published = [], []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **kw: ran.append(city.city_id) or True,
    )
    _stub_tail(monkeypatch, sched, conn, published)

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert len(ran) == 1, "the city loop must still run"
    assert published == ["aggregate", "manifest", "publish"], (
        "a backup failure must not withhold what the night collected"
    )
    assert rc == 1, "a failed backup is an unhealthy night"
    assert len(alerts) == 1
    assert "CATALOG BACKUP FAILED" in alerts[0][0]
    assert "disk full" in alerts[0][1]


def test_a_failed_aggregate_still_backs_up_and_publishes(conn, monkeypatch):
    """
    The aggregate rebuild is the FIRST statement of the tail, and until this
    guard it was unguarded — so a crash there skipped the streetwalk manifest,
    the plan summary, the tail catalog backup AND the publish. That happened on
    2026-08-17: a manual `run-due ... | tail -40` whose pipe reader had gone away
    collected 10/10 cities, then died on a BrokenPipeError out of the aggregate's
    progress bar and published none of them. #167's failure mode, relocated from
    the city loop into the tail.

    Continuing is safe because every index is written via
    _write_json_gz_atomic, so a failed rebuild leaves the PREVIOUS good file in
    place: the publish ships a stale-but-valid index, never a truncated one. A
    stale index costs a day and one `regenerate-aggregate`; an unpublished night
    costs every artifact the night collected plus its backup.

    The manifest must still be attempted — separate file, separate reader
    (streets.html) — which is why each rebuild is wrapped individually.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    backups = []
    monkeypatch.setattr(
        sched.catalog_backup,
        "write_backup",
        lambda conn, backup_dir, when, **kw: (
            backups.append(when)
            or sched.catalog_backup.BackupResult(
                ok=True, path=os.path.join(backup_dir, "stubbed.backup")
            )
        ),
    )

    ran, published = [], []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **kw: ran.append(city.city_id) or True,
    )
    _stub_tail(monkeypatch, sched, conn, published)

    def boom(c, data_dir):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sched, "generate_aggregate_v2", boom)

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert len(ran) == 1, "the city loop must still run"
    assert published == ["manifest", "publish"], (
        "a broken aggregate must cost neither the manifest nor the publish"
    )
    assert len(backups) == 2, "both the pre-flight and the TAIL backup must still happen"
    assert rc == 1, "a failed index is an unhealthy night"
    assert len(alerts) == 1
    assert "1 published index(es) FAILED" in alerts[0][0]
    assert "aggregate index failed" in alerts[0][1]
    assert "BrokenPipeError" in alerts[0][1], "the alert must carry the cause, not just the label"


def test_backup_failure_alerts_even_below_the_failure_threshold(conn, monkeypatch):
    """The collection-failure threshold exists so one flaky city doesn't page
    every night. Backups get no such grace: there is no such thing as an
    acceptable number of nights without one."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(
        sched.catalog_backup,
        "write_backup",
        lambda c, d, w, **kw: sched.catalog_backup.BackupResult(ok=False, error="io error"),
    )
    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append(subj))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    # Zero attempted, zero failed — nothing here would alert on its own.
    rc = sched._finish_batch(
        _publishing_cfg(alerts=AlertConfig(enabled=True, recipient="x@y", failure_threshold=99)),
        conn,
        "summary",
        succeeded=0,
        attempted=0,
        today=date(2026, 7, 2),
        backup_error="catalog backup failed: io error",
    )

    assert rc == 1
    assert alerts and "CATALOG BACKUP FAILED" in alerts[0]


def test_tail_backup_captures_the_nights_runs(conn, monkeypatch, tmp_path):
    """Why the tail backs up a second time: the pre-flight copy proves a copy
    EXISTS, but it predates every run the night registered."""
    from streetscape_metadata_tracker import catalog_backup as cb
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")
    cfg = _real_backup_cfg(tmp_path)

    # Pre-flight copy of an empty-ish catalog.
    assert sched._backup_catalog_nightly(cfg, conn, date(2026, 7, 2)) is None
    before = cb.read_status(cfg.backup_dir)["row_counts"].get("cities", 0)

    # The "night" registers a city, then the tail runs.
    _register(conn, "Alpha", width=1000, height=1000, step=20)
    sched._finish_batch(cfg, conn, "summary", succeeded=1, attempted=1, today=date(2026, 7, 2))

    after = cb.read_status(cfg.backup_dir)["row_counts"].get("cities", 0)
    assert after == before + 1
    assert len(cb.list_backups(cfg.backup_dir)) == 1, "same date replaces, never accumulates"


def test_dry_run_announces_the_backup_without_writing_one(conn, monkeypatch, tmp_path, capsys):
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    cfg = _real_backup_cfg(tmp_path)

    rc = sched.cmd_run_due(cfg, dry_run=True, today=date(2026, 7, 2))

    assert rc == 0
    assert "back up the catalog" in capsys.readouterr().out
    assert not os.path.isdir(cfg.backup_dir), "a dry run must not write a backup"


def test_config_parses_backup_dir(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[paths]\nbackup_dir = "/srv/backups"\n')
    assert load_scheduler_config(str(p)).backup_dir == "/srv/backups"


def test_backup_dir_defaults_beside_the_project_not_inside_data(tmp_path):
    """It must not live under data_dir: the publish rsync walks data/ and would
    ship catalog backups to the public web server."""
    p = tmp_path / "s.toml"
    p.write_text("[schedule]\ncycle_days = 90\n")
    cfg = load_scheduler_config(str(p))
    assert cfg.backup_dir.endswith("backups")
    assert not cfg.backup_dir.startswith(cfg.data_dir)


def test_cmd_backup_status_reports_and_exits_nonzero_when_missing(
    conn, monkeypatch, tmp_path, capsys
):
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    cfg = _real_backup_cfg(tmp_path, data_dir=str(tmp_path / "data"))
    cfg.driving_plan.archive_dir = str(tmp_path / "archive" / "gsv_driving_plan")

    # No backups yet → unhealthy.
    assert sched.cmd_backup_status(cfg) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out or "EMPTY" in out

    # After a real backup → healthy, and the inventory is reported.
    os.makedirs(cfg.driving_plan.archive_dir)
    with open(
        os.path.join(cfg.driving_plan.archive_dir, "gsv_driving_plan_2026-08-01.json.gz"), "wb"
    ) as f:
        f.write(b"x" * 64)
    sched.catalog_backup.write_backup(conn, cfg.backup_dir, date(2026, 8, 7), source_db=cfg.db_path)

    assert sched.cmd_backup_status(cfg) == 0
    out = capsys.readouterr().out
    assert "streetscape_tracker.db.2026-08-07.backup" in out
    assert "driving-plan archive" in out
    assert "1 files" in out


def test_backup_status_exits_nonzero_when_the_newest_copy_is_stale(
    conn, monkeypatch, tmp_path, capsys
):
    """
    A monitor check has to catch "nothing has run in weeks", not just "the last
    thing we tried failed". The newest copy is never pruned, so an abandoned
    scheduler leaves one ancient file next to an ok status — #145's shape.
    """
    from streetscape_metadata_tracker import catalog_backup as cb
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    cfg = _real_backup_cfg(tmp_path, data_dir=str(tmp_path / "data"))
    cfg.driving_plan.archive_dir = str(tmp_path / "archive")
    result = sched.catalog_backup.write_backup(conn, cfg.backup_dir, date(2026, 8, 7))

    assert sched.cmd_backup_status(cfg) == 0
    capsys.readouterr()

    # Nothing changes but the file's age: same file, same successful status.
    old = time.time() - (cb.STALE_AFTER_HOURS + 2) * 3600
    os.utime(result.path, (old, old))

    assert sched.cmd_backup_status(cfg) == 1
    out = capsys.readouterr().out
    assert "STALE" in out
    assert "last attempt" in out and "FAILED" not in out


def test_backup_status_alert_fires_only_when_unhealthy(conn, monkeypatch, tmp_path, capsys):
    """
    The out-of-band monitor (issue #193). --alert must be silent on a healthy
    day — a daily mail nobody needs is a daily mail nobody reads — and must not
    change the exit status, which is what systemd and any external check use.
    """
    from streetscape_metadata_tracker import catalog_backup as cb
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(sched, "send_alert", lambda a, s, b: sent.append((s, b)) or True)

    cfg = _real_backup_cfg(tmp_path, data_dir=str(tmp_path / "data"))
    cfg.driving_plan.archive_dir = str(tmp_path / "archive")
    result = sched.catalog_backup.write_backup(conn, cfg.backup_dir, date(2026, 8, 7))

    assert sched.cmd_backup_status(cfg, alert=True) == 0
    assert sent == [], "a healthy check must not email"
    capsys.readouterr()

    # Age the copy past the staleness gate — the abandoned-scheduler shape.
    old = time.time() - (cb.STALE_AFTER_HOURS + 2) * 3600
    os.utime(result.path, (old, old))

    assert sched.cmd_backup_status(cfg, alert=True) == 1, "--alert must not change the exit status"
    assert len(sent) == 1
    subject, body = sent[0]
    # The subject has to carry the verdict: on a monitor mail, the subject line
    # is often the only part read, and "stale" (is the scheduler running?) and
    # "the copy failed" (why?) call for different first moves.
    assert "STALE" in subject
    # The body is the report itself, not a scheduler log tail.
    assert "Catalog backups:" in body and "STALE" in body


def test_backup_status_alert_names_the_reason_and_stays_opt_in(conn, monkeypatch, tmp_path, capsys):
    """Each unhealthy shape gets its own subject, and the default (no --alert)
    never sends — the plain CLI stays a plain CLI."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    sent: list[str] = []
    monkeypatch.setattr(sched, "send_alert", lambda a, s, b: sent.append(s) or True)

    cfg = _real_backup_cfg(tmp_path, data_dir=str(tmp_path / "data"))
    cfg.driving_plan.archive_dir = str(tmp_path / "archive")

    # Unhealthy, but not asked to alert: silent.
    assert sched.cmd_backup_status(cfg) == 1
    assert sent == []

    # No backups at all.
    assert sched.cmd_backup_status(cfg, alert=True) == 1
    assert "NO BACKUPS" in sent[-1]

    # A recorded failure, with a copy present: a different question entirely.
    sched.catalog_backup.write_backup(conn, cfg.backup_dir, date(2026, 8, 7))
    status_path = os.path.join(cfg.backup_dir, "backup_status.json")
    st = json.loads(Path(status_path).read_text())
    st["ok"] = False
    st["error"] = "integrity_check failed: page 3 is never used"
    Path(status_path).write_text(json.dumps(st))

    assert sched.cmd_backup_status(cfg, alert=True) == 1
    assert "last attempt FAILED" in sent[-1]
    capsys.readouterr()


def test_backup_check_unit_matches_the_collection_unit(tmp_path):
    """
    The monitor is only a monitor if it runs on the host that writes the
    backups and against the production config. These live in three files
    (two units + the toml) and only mean anything together — the same
    cross-file agreement the max_batch_hours/TimeoutStartSec test pins.
    """
    unit_dir = Path(_PROJECT_ROOT, "deploy", "systemd")
    check = (unit_dir / "streetscape-backup-check.service").read_text()
    collect = (unit_dir / "streetscape-tracker.service").read_text()
    timer = (unit_dir / "streetscape-backup-check.timer").read_text()

    # Same host pin: $HOME is shared NFS across both boxes, so without this the
    # check could report on a machine that never writes a backup.
    host = re.search(r"^ConditionHost=(.+)$", collect, re.M).group(1)
    assert re.search(rf"^ConditionHost={re.escape(host)}$", check, re.M), (
        "the check must be pinned to the same host as the collection unit"
    )
    # Same interpreter and same production config as the collection unit.
    for fragment in (".venv-makelab2/bin/python", "config/scheduler.makelab1.toml"):
        assert fragment in check and fragment in collect

    # Assert against the ExecStart line itself — the file's prose mentions
    # backup-status too, and a comment is not what systemd runs.
    exec_line = re.search(r"^ExecStart=(.+)$", check, re.M).group(1)
    # It must actually pass --alert; without it the timer runs a check whose
    # only output goes to a log nobody reads, which is the bug being fixed.
    assert "backup-status --alert" in exec_line
    # --config is a global arg: argparse rejects it after the subcommand.
    assert exec_line.index("--config") < exec_line.index("backup-status")
    assert "[Install]" in timer and "OnCalendar=" in timer


def _assert_unit_quotes(unit, rendered, constant):
    """Pin the two copies of a measurement to each other.

    The figures the resource directives are sized from live twice by design: as
    a constant here, which is what the test ENFORCES, and in the unit file's
    rationale block, which is what the next person re-sizing a directive
    actually READS. Nothing else stops those drifting, and the drift is silent
    and bad in a specific way — the cap ends up justified by one number and
    enforced against another, so an operator argues from a figure no test holds
    anyone to. Cheap to assert, so assert it.

    It is a SUBSTRING check, so it only pins the constant as tightly as the
    prose is unambiguous: if the unit spells a near-miss of the same quantity
    the same way — the unrounded product beside the rounded floor, both suffixed
    `GiB` — then lowering the constant to that near-miss still passes. Which is
    why the unit writes the product bare (`extrapolates to 15.1`) and reserves
    the unit suffix for the figure the caps are actually sized from. Keep it
    that way rather than making this matcher cleverer.
    """
    assert rendered in unit, (
        f"{constant} renders as {rendered!r}, which no longer appears in "
        f"deploy/systemd/streetscape-tracker.service. Update BOTH copies — the "
        f"constant is what this suite enforces, the unit's prose is what the "
        f"next operator reads before changing the directive."
    )


# Worst aggregate + streetwalk-manifest rebuild measured on prod: 7m15s on the
# 19-city night of 2026-08-18 (a small night is ~1.6 s). A named constant rather
# than a literal because it is a MEASUREMENT — the unit file quotes the same
# figure, and the next re-sizing of TimeoutStopSec has to argue from a number
# with a date on it. Re-measure from the scheduler log's own timestamps; the
# publish half of the tail is now the `Published in N.N s` line (issue #206).
_MEASURED_TAIL_AGGREGATE_S = 435


def test_stop_timeout_covers_the_publish_tail_it_waits_for():
    """
    `systemctl stop` is a wind-down that still runs the tail, so the unit's stop
    timeout and the tail's own bounds live in different files and only mean
    something together — the same cross-file agreement the
    max_batch_hours/TimeoutStartSec test pins. Without the directive systemd
    applies 90 s: measured 2026-08-13, SIGTERM 06:23:28 -> SIGKILL 06:24:58, no
    tail, and the night's runs left unpublished (issue #206).

    Asserted against the DIRECTIVE line, not the prose around it — a comment is
    not what systemd runs.

    It ALSO pins the unit's prose to `_MEASURED_TAIL_AGGREGATE_S` via
    `_assert_unit_quotes`, which the test name does not advertise: that constant
    has no other test, and a failure here can therefore be about the quoted
    figure rather than about TimeoutStopSec itself. The assertion message says
    which.
    """
    from streetscape_metadata_tracker import catalog_backup
    from streetscape_metadata_tracker import scheduler as sched

    unit = Path(_PROJECT_ROOT, "deploy", "systemd", "streetscape-tracker.service").read_text()
    # Parse systemd's suffixes rather than hard-coding `min`: the TimeoutStartSec
    # assertion above requires `\d+h` and would false-fail on a bare-seconds or
    # `30min` spelling. A bare integer is seconds in systemd.
    m = re.search(r"^TimeoutStopSec=(\d+)(s|min|h)?\s*$", unit, re.M)
    assert m, "the unit must set TimeoutStopSec explicitly; systemd's default is 90 s (#206)"
    stop_s = int(m.group(1)) * {None: 1, "s": 1, "min": 60, "h": 3600}[m.group(2)]
    _assert_unit_quotes(
        unit,
        f"{_MEASURED_TAIL_AGGREGATE_S // 60}m{_MEASURED_TAIL_AGGREGATE_S % 60:02d}s",
        "_MEASURED_TAIL_AGGREGATE_S",
    )
    _assert_unit_quotes(
        unit, f"PUBLISH_TIMEOUT_S ({sched.PUBLISH_TIMEOUT_S / 60:.0f} min)", "PUBLISH_TIMEOUT_S"
    )

    # The floor is the SUM of the tail's large known terms, not the largest of
    # them. Asserting only `> BACKUP_TIMEOUT_S` accepted 11min — which the very
    # sentence justifying that bound rules out, since the aggregate runs BEFORE
    # the backup and neither term substitutes for the other.
    #
    # PUBLISH_TIMEOUT_S is the third term (issue #230) and belongs in the sum for
    # exactly that reason: the publish runs AFTER both. A bound that merely sits
    # below TimeoutStopSec on its own — the weaker condition #230 asks for —
    # still lets the wind-down be SIGKILLed partway through backup + aggregate +
    # publish, which is the pre-#230 outcome reached one step later. Sizing the
    # publish bound and sizing this directive are one decision, so they are one
    # assertion, and it can fail from either side: see the message.
    floor_s = catalog_backup.BACKUP_TIMEOUT_S + _MEASURED_TAIL_AGGREGATE_S + sched.PUBLISH_TIMEOUT_S
    publish_ceiling_s = stop_s - catalog_backup.BACKUP_TIMEOUT_S - _MEASURED_TAIL_AGGREGATE_S
    assert stop_s > floor_s, (
        f"TimeoutStopSec={stop_s:.0f}s cannot survive its own tail: the catalog "
        f"backup is hard-bounded at {catalog_backup.BACKUP_TIMEOUT_S:.0f}s, "
        f"aggregate+manifest measured {_MEASURED_TAIL_AGGREGATE_S:.0f}s on the "
        f"19-city night of 2026-08-18, and the publish is bounded at "
        f"PUBLISH_TIMEOUT_S={sched.PUBLISH_TIMEOUT_S:.0f}s, so anything at or "
        f"below {floor_s:.0f}s SIGKILLs the wind-down partway through the very "
        f"steps this timeout exists to reach. This is why #206's suggested 10min "
        f"would have been wrong — and 11min would have been too. The other way "
        f"to break it is from the publish side: at this TimeoutStopSec, "
        f"PUBLISH_TIMEOUT_S cannot exceed ~{publish_ceiling_s:.0f}s, because a "
        f"publish bound that is itself SIGKILLed before it can report is the "
        f"pre-#230 behaviour under a different name."
    )
    cfg = load_scheduler_config(os.path.join(_PROJECT_ROOT, "config", "scheduler.makelab1.toml"))
    assert stop_s < cfg.max_batch_hours * 3600, (
        "a stop timeout at or above the batch's own deadline makes `systemctl "
        "stop` and host shutdown hang for longer than letting the night finish"
    )


# Extrapolated peak RSS of the largest city we track, in GiB. Both inputs are
# traceable, which matters because this figure is a floor the caps are sized
# against: the per-run JSON tail measured 4.81 GiB on Ho Chi Minh City's 5.26M-row
# census (2026-08-18), i.e. ~0.914 GiB per million rows, and the largest census
# in the catalog is Detroit's 16,569,307 rows —
#
#   sqlite3 data/streetscape_tracker.db \
#     "SELECT city_id, run_date, MAX(total_points) FROM runs WHERE provider='mapillary';"
#
# (runs.total_points IS the CSV row count for a Mapillary census: one row per
# pano plus the ZERO_RESULTS/FLAT_ONLY fill, and it sums the status columns
# exactly). 16.569M x 0.914 = 15.1 GiB, rounded UP to 15.3 here — a floor should
# err high, and the slope is one city's.
#
# WHY THE QUERY FILTERS ON PROVIDER, since dropping the filter finds a bigger
# number and the exclusion should be an argument rather than an omission: the
# slope is a property of the CSV row count, not of the provider, and
# analysis.calculate_run_stats writes total_points as len(df) for every one. Run
# it without the WHERE and the top row is juneau--alaska--united-states, gsv,
# 2025-01-08, 39,346,564 rows — 2.4x Detroit. Those are is_baseline=1 archival
# imports (issue #93) on pre-#166 geometry, and no future night re-collects at
# that size: the 40 km cap bounds a fresh gsv run at (40000/20)^2 = 4M points,
# hence 4M rows, hence ~3.7 GiB. A census has no such bound — its rows are
# imagery, not lattice points — which is why the largest live workload is a
# Mapillary one and why the filter belongs there.
#
# Also worth re-running on PROD rather than a dev checkout before trusting it as
# a catalog-wide maximum: a laptop catalog may hold only a handful of Mapillary
# runs, in which case the query returns the largest of those and not the largest
# we collect.
#
# A named constant for the same reason _MEASURED_TAIL_AGGREGATE_S is one: the
# unit file quotes this figure and the next re-sizing has to argue from it. NOTE
# it is an EXTRAPOLATION from one city's slope, not a measurement; replace it
# with a real MemoryPeak the first night a big city runs to completion.
_EXTRAPOLATED_LARGEST_CITY_PEAK_GIB = 15.3


def test_memory_high_is_a_throttle_below_the_hard_limit_and_clears_the_worst_city():
    """
    The two memory caps do different jobs and only mean something together.
    MemoryHigh is a THROTTLE: crossing it costs hours of silent reclaim that end
    in the scheduler's 180-min city timeout SIGKILLing a child that printed
    nothing (measured 2026-08-18, issue #157). MemoryMax is a hard limit, whose
    breach is a fast OOM kill an operator can actually read.

    So the hard limit must clear the largest city's peak no matter what, and IF
    a soft brake is configured it has to sit in the band where it is useful:
    strictly below MemoryMax (at or above it the brake is INERT — it never
    engages and every overrun becomes the hard kill) and above the largest
    city's peak (below it, the biggest nights throttle into the 180-min city
    timeout — the 2026-08-18 failure).

    "IF" is load-bearing: the unit file offers "no MemoryHigh at all" as a valid
    answer to a big-city hang, so this must not be the test that forbids the fix
    its own subject recommends. Absent — and systemd's `infinity` spelling of
    the same thing — is accepted, and the MemoryMax floor below is then the only
    thing standing between a big city and an OOM kill, which is why that
    assertion is unconditional rather than part of the MemoryHigh branch.

    Asserted against the directive lines, not the prose around them.
    """
    unit = Path(_PROJECT_ROOT, "deploy", "systemd", "streetscape-tracker.service").read_text()
    floor = _EXTRAPOLATED_LARGEST_CITY_PEAK_GIB * 2**30
    _assert_unit_quotes(
        unit, f"{_EXTRAPOLATED_LARGEST_CITY_PEAK_GIB} GiB", "_EXTRAPOLATED_LARGEST_CITY_PEAK_GIB"
    )

    def _raw(directive):
        # LAST match, not the first: systemd is last-wins for a repeated
        # directive, so a duplicated MemoryHigh= line would otherwise be
        # validated at the copy the kernel ignores — this test passing on a
        # value that is not in force is the one way it could mislead.
        found = re.findall(rf"^{directive}=(\S+)\s*$", unit, re.M)
        return found[-1] if found else None

    def _bytes(directive, value):
        # Decimal sizes are accepted because systemd accepts them and they are
        # still comparable. PERCENTAGES are not, and that is a requirement
        # rather than a parser limitation: the floor below is an absolute
        # measurement in GiB, so a percentage would silently re-scale both caps
        # with the host's RAM and make the comparison meaningless on any box but
        # the one it was written for. Say so, rather than failing as "cannot
        # interpret that spelling".
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)", value)
        assert m, (
            f"{directive}={value} must be an absolute byte size (e.g. 20G). This "
            f"test compares the caps against a measured GiB floor, so a "
            f"percentage-of-RAM spelling cannot be checked and would mean a "
            f"different cap on every host the unit is copied to."
        )
        return (
            float(m.group(1)) * {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}[m.group(2)]
        )

    hard_raw = _raw("MemoryMax")
    assert hard_raw and hard_raw != "infinity", (
        "the unit must set MemoryMax to a real ceiling: it is the only cap whose "
        "breach is a fast, legible OOM kill rather than silent reclaim"
    )
    hard = _bytes("MemoryMax", hard_raw)
    assert hard > floor, (
        f"MemoryMax={hard / 2**30:.1f}G is under the largest city's "
        f"~{_EXTRAPOLATED_LARGEST_CITY_PEAK_GIB}GiB extrapolated peak, so the "
        f"biggest nights are OOM-killed outright (issue #157)."
    )

    high_raw = _raw("MemoryHigh")
    if high_raw is None or high_raw == "infinity":
        return  # No soft brake by choice — see the docstring.
    high = _bytes("MemoryHigh", high_raw)

    assert high < hard, (
        f"MemoryHigh={high / 2**30:.1f}G is not below MemoryMax={hard / 2**30:.1f}G: "
        f"the soft brake can never engage, so every overrun skips the throttle and "
        f"becomes an OOM kill. If that is genuinely wanted, DROP MemoryHigh (or set "
        f"it to `infinity`) rather than raising it to meet MemoryMax, so the intent "
        f"is visible in the unit."
    )
    assert high > floor, (
        f"MemoryHigh={high / 2**30:.1f}G is under the largest city's "
        f"~{_EXTRAPOLATED_LARGEST_CITY_PEAK_GIB}GiB extrapolated peak, so the "
        f"biggest nights throttle into the 180-min city timeout instead of "
        f"finishing — the 2026-08-18 failure, which is what raising this cap "
        f"exists to prevent (issues #157/#206). If a real measurement lands above "
        f"MemoryMax={hard / 2**30:.1f}G, both caps have to move, not just this one."
    )


def test_restore_backup_subcommand_restores_and_then_refuses(conn, monkeypatch, tmp_path, capsys):
    """The incident-time handle. It must work, and it must refuse the second
    time — restoring onto a catalog that is already there would destroy the
    thing being recovered."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.catalog_backup, "write_backup", _REAL_WRITE_BACKUP)
    cfg = _real_backup_cfg(tmp_path)
    result = sched.catalog_backup.write_backup(conn, cfg.backup_dir, date(2026, 8, 7))
    dest = str(tmp_path / "restored" / "streetscape_tracker.db")

    assert sched.cmd_restore_backup(cfg, result.path, dest) == 0
    assert os.path.exists(dest)

    rc = sched.cmd_restore_backup(cfg, result.path, dest)
    assert rc == 1
    assert "Restore refused" in capsys.readouterr().out


def test_alert_subject_names_both_the_backup_and_the_collection_failures(conn, monkeypatch):
    """A subject line is often all that gets read; a failed backup must not mask
    a failed night, or vice versa."""
    from streetscape_metadata_tracker import scheduler as sched

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append(subj))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    sched._finish_batch(
        _publishing_cfg(),
        conn,
        "summary",
        succeeded=0,
        attempted=2,
        today=date(2026, 7, 2),
        errored=True,
        backup_error="catalog backup failed: io error",
    )

    assert len(alerts) == 1
    assert "CATALOG BACKUP FAILED" in alerts[0]
    assert "2 failed collection(s)" in alerts[0]


def test_backup_status_subcommand_is_wired(capsys):
    args = build_parser().parse_args(["backup-status"])
    assert args.command == "backup-status"


def test_restore_backup_subcommand_is_wired():
    args = build_parser().parse_args(["restore-backup", "/b/x.backup", "--to", "/tmp/d.db"])
    assert (args.command, args.backup_path, args.dest) == (
        "restore-backup",
        "/b/x.backup",
        "/tmp/d.db",
    )


# ── Mapillary tile pacing reaches the children (issue #198) ────────────────


def _grid_cmd(monkeypatch, tmp_path, conn, provider, cfg):
    """Capture the argv the scheduler hands a grid subprocess."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    captured = {}

    def fake_run(cmd, timeout=None, cwd=None, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    cfg = dataclasses.replace(cfg, log_dir=str(tmp_path))
    assert sched._run_one_city(cfg, city, date(2026, 7, 1), provider)
    return captured["cmd"], city


def test_mapillary_grid_child_gets_the_tile_pace_not_the_gsv_one(conn, monkeypatch, tmp_path):
    """The --max-requests-per-minute already on the grid command is a GSV number
    ([download], 48k on prod) and the CLI applies it only to the GSV path.
    Mapillary's cap is a different KIND of limit — per IP on the tile CDN — and
    orders of magnitude smaller, so it needs its own flag."""
    cfg = SchedulerConfig(providers={"mapillary": ProviderConfig(max_requests_per_minute=60)})
    cmd, city = _grid_cmd(monkeypatch, tmp_path, conn, "mapillary", cfg)

    assert cmd[cmd.index("--mapillary-max-requests-per-minute") + 1] == "60"
    # The GSV flag is still passed and still untouched — the CLI ignores it for
    # this provider, and nothing here should change GSV's behaviour.
    assert cmd[cmd.index("--max-requests-per-minute") + 1] == str(cfg.max_requests_per_minute)
    # '--' terminator must stay last so a display name is never read as a flag.
    assert cmd[cmd.index("--") + 1] == city.display_name
    assert cmd[-1] == city.display_name


def test_an_unset_mapillary_pace_leaves_the_cli_default_in_force(conn, monkeypatch, tmp_path):
    """Omitting the flag is correct: the CLI's own default is conservative. The
    wrong fallback would be [download].max_requests_per_minute, a GSV figure
    that would disable pacing in practice."""
    cfg = SchedulerConfig(providers={"mapillary": ProviderConfig()})
    cmd, _ = _grid_cmd(monkeypatch, tmp_path, conn, "mapillary", cfg)
    assert "--mapillary-max-requests-per-minute" not in cmd


def test_a_gsv_grid_child_never_gets_the_mapillary_flag(conn, monkeypatch, tmp_path):
    cfg = SchedulerConfig(providers={"mapillary": ProviderConfig(max_requests_per_minute=60)})
    cmd, _ = _grid_cmd(monkeypatch, tmp_path, conn, "gsv", cfg)
    assert "--mapillary-max-requests-per-minute" not in cmd


def test_a_sweep_child_gets_the_configured_pace_the_timeout_was_derived_from(
    conn, monkeypatch, tmp_path
):
    """Same reason as the Mapillary flag above, plus one specific to this
    channel: the KartaView timeout is DERIVED from the configured rate (#238),
    so a child left on its own default would be timed against a rate it never
    honoured — and the disagreement would only show up as a SIGKILL."""
    cfg = SchedulerConfig(providers={"kartaview": ProviderConfig(max_requests_per_minute=8)})
    cmd, _ = _grid_cmd(monkeypatch, tmp_path, conn, "kartaview", cfg)
    assert cmd[cmd.index("--kartaview-max-requests-per-minute") + 1] == "8"


def test_an_unset_sweep_pace_leaves_the_cli_default_in_force(conn, monkeypatch, tmp_path):
    """Omitting the flag is correct here too: the CLI's default is the same
    conservative 16/min the timeout derivation assumes, so the two agree."""
    cfg = SchedulerConfig(providers={"kartaview": ProviderConfig()})
    cmd, _ = _grid_cmd(monkeypatch, tmp_path, conn, "kartaview", cfg)
    assert "--kartaview-max-requests-per-minute" not in cmd


def test_a_gsv_grid_child_never_gets_the_sweep_flag(conn, monkeypatch, tmp_path):
    cfg = SchedulerConfig(providers={"kartaview": ProviderConfig(max_requests_per_minute=8)})
    cmd, _ = _grid_cmd(monkeypatch, tmp_path, conn, "gsv", cfg)
    assert "--kartaview-max-requests-per-minute" not in cmd


def test_an_explicit_zero_disables_gsv_street_pacing_instead_of_reverting(conn):
    """0 is documented as 'disables pacing'. A falsy `or` fallback silently
    promoted it to [download].max_requests_per_minute — a 24k/48k project
    figure, i.e. the opposite of what was asked for."""
    from streetscape_metadata_tracker.scheduler import _street_collect_cmd

    city = db.resolve_city(conn, _register(conn, "Bend", width=5000, height=5000, step=20))
    cfg = SchedulerConfig(providers={"gsv_streets": ProviderConfig(max_requests_per_minute=0)})

    cmd = _street_collect_cmd(cfg, city, date(2026, 7, 8), "gsv_streets", 10, 100)
    assert cmd[cmd.index("--max-requests-per-minute") + 1] == "0"


def test_an_unset_gsv_street_pace_falls_back_to_the_download_figure(conn):
    from streetscape_metadata_tracker.scheduler import _street_collect_cmd

    city = db.resolve_city(conn, _register(conn, "Bend", width=5000, height=5000, step=20))
    cfg = SchedulerConfig(providers={"gsv_streets": ProviderConfig()})

    cmd = _street_collect_cmd(cfg, city, date(2026, 7, 8), "gsv_streets", 10, 100)
    assert cmd[cmd.index("--max-requests-per-minute") + 1] == str(cfg.max_requests_per_minute)


def test_mapillary_street_child_gets_the_tile_pace(conn):
    """The walk and the grid share one per-IP budget, so the walk must not be
    the unpaced way in."""
    from streetscape_metadata_tracker.scheduler import _street_collect_cmd

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = SchedulerConfig(
        providers={"mapillary_streets": ProviderConfig(max_requests_per_minute=60)}
    )

    cmd = _street_collect_cmd(cfg, city, date(2026, 7, 8), "mapillary_streets", 10, 100)
    assert cmd[cmd.index("--mapillary-max-requests-per-minute") + 1] == "60"
    # gsv_streets' own pacing flag must not leak onto a Mapillary walk: its
    # value is a GSV-scale number the collector would apply to tile requests.
    assert "--max-requests-per-minute" not in cmd


# ---------------------------------------------------------------------------
# Per-IP host breaker (issue #208)
#
# A host that refuses this machine refuses it for every city, so the loop stops
# asking. The subtle requirement is that a breaker skip must NOT be recorded as
# a city failure: get_due_cities filters on consecutive_failures and nothing
# resets that counter except a success.
# ---------------------------------------------------------------------------


def _blocked_outcome(host):
    from streetscape_metadata_tracker.download_common import HOST_EXIT_CODES
    from streetscape_metadata_tracker.scheduler import CollectionOutcome

    return CollectionOutcome(
        False, f"exited {HOST_EXIT_CODES[host]} (blocked)", exit_code=HOST_EXIT_CODES[host]
    )


def _busy_outcome(host):
    from streetscape_metadata_tracker.download_common import HOST_BUSY_EXIT_CODES
    from streetscape_metadata_tracker.scheduler import CollectionOutcome

    return CollectionOutcome(
        False, f"exited {HOST_BUSY_EXIT_CODES[host]} (busy)", exit_code=HOST_BUSY_EXIT_CODES[host]
    )


def _run_loop_with(monkeypatch, conn, cfg, run_one, today=date(2026, 7, 2)):
    """Drive cmd_run_due with a fake _run_one_city, tail stubbed out."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            run_one(city, provider)
        ),
    )
    _stub_tail(monkeypatch, sched, conn, [])
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    return sched.cmd_run_due(cfg, today=today)


def test_a_blocked_host_skips_its_channels_for_the_rest_of_the_night(conn, monkeypatch):
    """One refusal is enough: asking again with the next city cannot produce a
    different answer, and every ask costs real requests into a blocked host."""
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    for name in ("Bend", "Corvallis", "Eugene"):
        _register(conn, name, width=1000, height=1000, step=20)

    ran = []

    def run_one(city, provider):
        ran.append((city.city_id, provider))
        if provider == "mapillary":
            return _blocked_outcome(HOST_MAPILLARY_TILES)
        return True

    _run_loop_with(monkeypatch, conn, _street_cfg(publish_enabled=False), run_one)

    mapillary_attempts = [p for _, p in ran if p == "mapillary"]
    assert len(mapillary_attempts) == 1, "must stop after the first refusal"
    # ...while the GSV channels, which do not use that host, run for every city.
    assert len([p for _, p in ran if p == "gsv"]) == 3


def test_a_blocked_host_is_not_recorded_as_a_city_failure(conn, monkeypatch):
    """
    The city did nothing wrong and we never reached its imagery. Recording a
    failure would burn one of its five consecutive_failures — and nothing in the
    codebase resets that counter except a success, so a run of blocked nights
    would quarantine the city for a whole cycle with no way back but hand SQL.
    """
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)

    def run_one(city, provider):
        return True if provider.startswith("gsv") else _blocked_outcome(HOST_MAPILLARY_TILES)

    _run_loop_with(monkeypatch, conn, _street_cfg(publish_enabled=False), run_one)

    rows = {
        r["provider"]: r
        for r in conn.execute(
            "SELECT provider, consecutive_failures, last_error FROM schedule_state "
            "WHERE city_id = ?",
            (cid,),
        )
    }
    for channel in ("mapillary", "mapillary_streets"):
        row = rows.get(channel)
        # Either no row at all, or a row that records no failure. Both are fine;
        # what must never happen is a counted failure.
        if row is not None:
            assert row["consecutive_failures"] == 0
            assert row["last_error"] is None


def test_repeated_blocked_nights_never_quarantine_a_city(conn, monkeypatch):
    """The regression this breaker exists to prevent, run end to end: five
    blocked nights in a row must leave the city as due as it started."""
    from streetscape_metadata_tracker import db as sdb
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    cfg = _street_cfg(publish_enabled=False)

    def run_one(city, provider):
        return _blocked_outcome(HOST_MAPILLARY_TILES) if provider == "mapillary" else True

    for day in range(5):
        _run_loop_with(
            monkeypatch, conn, cfg, run_one, today=date(2026, 7, 2) + timedelta(days=day)
        )

    row = conn.execute(
        "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = ?",
        (cid, "mapillary"),
    ).fetchone()
    assert row is None or row["consecutive_failures"] == 0

    # The real contract: it is still returned as due for that channel.
    due = sdb.get_due_cities(
        conn,
        today=date(2026, 7, 20),
        cycle_days=1,
        grace_days=0,
        max_consecutive_failures=5,
        default_membership=True,
        provider="mapillary",
    )
    assert cid in [c.city_id for c in due]


def test_only_the_channels_that_need_the_blocked_host_are_skipped(conn, monkeypatch):
    """Overpass blocks the two road-walk channels; the grid channels are
    untouched, because a grid run never asks OSM for anything."""
    from streetscape_metadata_tracker.download_common import HOST_OVERPASS

    for name in ("Bend", "Corvallis"):
        _register(conn, name, width=1000, height=1000, step=20)

    ran = []

    def run_one(city, provider):
        ran.append(provider)
        return _blocked_outcome(HOST_OVERPASS) if provider == "gsv_streets" else True

    _run_loop_with(monkeypatch, conn, _street_cfg(publish_enabled=False), run_one)

    assert ran.count("gsv_streets") == 1
    assert ran.count("mapillary_streets") == 0, "also needs Overpass for the network"
    # Grid channels are per-project (gsv) or per-IP on a DIFFERENT host
    # (mapillary tiles), so neither is affected by an Overpass block.
    assert ran.count("gsv") == 2
    assert ran.count("mapillary") == 2


def test_a_blocked_night_alerts_unconditionally_exits_nonzero_and_still_publishes(
    conn, monkeypatch
):
    """
    The breaker records no failure, so without an explicit signal a night that
    collected zero Mapillary would look like a clean success. It must alert
    regardless of failure_threshold — same posture as a failed catalog backup —
    while still publishing what the GSV channels did collect (#167).
    """
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES
    from streetscape_metadata_tracker.scheduler import AlertConfig

    _register(conn, "Bend", width=1000, height=1000, step=20)

    published, alerts = [], []

    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            _blocked_outcome(HOST_MAPILLARY_TILES) if provider == "mapillary" else True
        ),
    )
    _stub_tail(monkeypatch, sched, conn, published)
    monkeypatch.setattr(
        sched, "send_alert", lambda cfg, subject, body: alerts.append((subject, body))
    )

    cfg = _street_cfg(
        publish_enabled=True,
        # A threshold high enough that ordinary failure counting would stay
        # silent — the alert here must come from the block, not the threshold.
        alerts=AlertConfig(enabled=True, failure_threshold=99),
    )
    rc = sched.cmd_run_due(cfg, today=date(2026, 7, 2))

    assert rc == 1, "a blocked host makes the night unhealthy"
    assert "publish" in published, "still publishes what was collected (#167)"
    assert len(alerts) == 1
    subject, body = alerts[0]
    assert "UNAVAILABLE" in subject
    assert "Mapillary" in body
    # The operator needs to know the cities were not blamed, or the obvious
    # next move is to go hunting for a per-city problem that does not exist.
    assert "NO city was marked failed" in body


# ---------------------------------------------------------------------------
# A LOCALLY busy host is not a blocked one (issue #208)
#
# The two conditions have opposite lifetimes. A refusal is durable; a lock held
# by another process on this box ends when that process does. Escalating the
# second to the night-wide breaker would let a two-minute manual run cost the
# batch every Mapillary city of the night — while leaving it silent would hide
# a city that did not collect, which is the #145 failure mode.
# ---------------------------------------------------------------------------


def test_a_busy_host_skips_only_that_channel_of_that_city(conn, monkeypatch):
    """The next city's Mapillary channel must still be attempted: by then the
    other process may well have finished."""
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    for name in ("Bend", "Corvallis", "Eugene"):
        _register(conn, name, width=1000, height=1000, step=20)

    ran = []

    def run_one(city, provider):
        ran.append((city.city_id, provider))
        # Busy only for the first city that asks; the holder then "finishes".
        if provider == "mapillary" and len([p for _, p in ran if p == "mapillary"]) == 1:
            return _busy_outcome(HOST_MAPILLARY_TILES)
        return True

    _run_loop_with(monkeypatch, conn, _street_cfg(publish_enabled=False), run_one)

    assert len([p for _, p in ran if p == "mapillary"]) == 3, (
        "a busy lock must not trip the night-wide breaker — that is a blocked host"
    )


def test_a_busy_host_is_not_recorded_as_a_city_failure(conn, monkeypatch):
    """Same reasoning as a blocked host: we never reached the city's imagery, so
    burning one of its five consecutive_failures would be blaming the wrong
    thing — and nothing but a success ever resets that counter."""
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)

    def run_one(city, provider):
        return True if provider.startswith("gsv") else _busy_outcome(HOST_MAPILLARY_TILES)

    _run_loop_with(monkeypatch, conn, _street_cfg(publish_enabled=False), run_one)

    for channel in ("mapillary", "mapillary_streets"):
        row = conn.execute(
            "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = ?",
            (cid, channel),
        ).fetchone()
        assert row is None or row["consecutive_failures"] == 0


def test_repeated_busy_nights_never_quarantine_a_city(conn, monkeypatch):
    """The corollary that matters operationally: an operator who runs manual
    Mapillary work every night must not silently retire a city."""
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    cfg = _street_cfg(publish_enabled=False)

    for day in range(6):  # more than max_consecutive_failures (5)
        _run_loop_with(
            monkeypatch,
            conn,
            cfg,
            lambda city, provider: (
                True if provider.startswith("gsv") else _busy_outcome(HOST_MAPILLARY_TILES)
            ),
            today=date(2026, 7, 2) + timedelta(days=day),
        )

    due = db.get_due_cities(
        conn,
        today=date(2026, 7, 20),
        cycle_days=1,
        grace_days=0,
        max_consecutive_failures=5,
        default_membership=True,
        provider="mapillary",
    )
    assert cid in {c.city_id for c in due}


def _paused_sweep_outcome():
    from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE
    from streetscape_metadata_tracker.scheduler import CollectionOutcome

    return CollectionOutcome(
        False,
        f"exited {SWEEP_INCOMPLETE_EXIT_CODE} (paused)",
        exit_code=SWEEP_INCOMPLETE_EXIT_CODE,
    )


def _sweep_cfg(**overrides):
    """_street_cfg plus a kartaview channel, bypassing UNWIRED_CHANNELS' refusal
    the same way the timeout cases above do."""
    cfg = _street_cfg(**overrides)
    cfg.providers["kartaview"] = ProviderConfig(enabled=True, daily_request_budget=20_000)
    return cfg


def _enroll_kartaview(conn, cid):
    """Opt a city into the kartaview channel (issue #248).

    Required for every sweep case below, and not boilerplate: kartaview's
    default membership is False, so without this the channel is configured,
    priced and ranked but its nightly queue is EMPTY — which is the mechanism
    under test in the dueness tests and a silent no-op in the amnesty ones.
    """
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)


def test_a_checkpointed_pause_is_not_recorded_as_a_city_failure(conn, monkeypatch):
    """A sweep that stopped with roots unvisited and CHECKPOINTED them (#239) is
    progress, not breakage — the CLI says exactly that and gives it its own exit
    code so a wrapper cannot escalate a legitimately capped night.

    It has to take the host branches' amnesty for the host branches' reason:
    get_due_cities filters on `consecutive_failures < max_consecutive_failures`
    and NOTHING but a success resets it, so charging a pause would retire the
    city after five of them. Which is not a hypothetical for this channel — a
    metro sweep needs more nights than that by construction (Singapore is
    ~10.4 h of pacing against a 10 h max_batch_hours), so the failure counter
    would fire before the sweep it is meant to protect could ever finish."""
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    _enroll_kartaview(conn, cid)

    def run_one(city, provider):
        return _paused_sweep_outcome() if provider == "kartaview" else True

    _run_loop_with(monkeypatch, conn, _sweep_cfg(publish_enabled=False), run_one)

    row = conn.execute(
        "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = ?",
        (cid, "kartaview"),
    ).fetchone()
    assert row is None or row["consecutive_failures"] == 0


def test_a_multi_night_sweep_is_never_quarantined_before_it_can_finish(conn, monkeypatch):
    """The operational corollary, and the whole reason the amnesty exists: a
    sweep that legitimately takes more nights than max_consecutive_failures must
    still be due on the night it would have completed."""
    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    _enroll_kartaview(conn, cid)
    cfg = _sweep_cfg(publish_enabled=False)

    for day in range(8):  # comfortably past max_consecutive_failures (5)
        _run_loop_with(
            monkeypatch,
            conn,
            cfg,
            lambda city, provider: _paused_sweep_outcome() if provider == "kartaview" else True,
            today=date(2026, 7, 2) + timedelta(days=day),
        )

    due = db.get_due_cities(
        conn,
        today=date(2026, 7, 20),
        cycle_days=1,
        grace_days=0,
        max_consecutive_failures=5,
        # The channel's real default, not True: this asserts the city survives
        # the amnesty AND its enrolment, which is what a real night reads.
        default_membership=_sched.CHANNEL_DEFAULT_MEMBERSHIP["kartaview"],
        provider="kartaview",
    )
    assert cid in {c.city_id for c in due}


def test_a_sweep_killed_by_its_timeout_still_counts_a_failure(conn, monkeypatch):
    """The boundary of the amnesty, pinned so it is not read as wider than it is.

    A SIGKILLed child has NO exit code, so nothing here can tell a kill that
    checkpointed real progress from one that made none — and treating every
    timeout as progress would mean a genuinely stuck channel never trips the
    failure counter at all. So "a kill just resumes tomorrow" is true of the
    WORK and bounded at five nights by the SCHEDULE; the fix for a city whose
    clamped timeout cannot finish in five is #248's dueness work, not a wider
    amnesty here. See _kartaview_timeout_seconds."""
    from streetscape_metadata_tracker.scheduler import CollectionOutcome

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    _enroll_kartaview(conn, cid)

    def run_one(city, provider):
        if provider != "kartaview":
            return True
        return CollectionOutcome(False, "timed out after 180 minutes", exit_code=None)

    _run_loop_with(monkeypatch, conn, _sweep_cfg(publish_enabled=False), run_one)

    row = conn.execute(
        "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = ?",
        (cid, "kartaview"),
    ).fetchone()
    assert row["consecutive_failures"] == 1


def test_a_busy_night_alerts_and_exits_nonzero_but_says_it_was_local(conn, monkeypatch):
    """
    Loud, like a blocked night — a city that quietly did not collect is exactly
    what #145 says must be impossible. But the subject and body must NOT say the
    provider refused us: the operator's next move is to find the local process,
    not to wait out a ban.
    """
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES
    from streetscape_metadata_tracker.scheduler import AlertConfig

    _register(conn, "Bend", width=1000, height=1000, step=20)

    published, alerts = [], []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None, **_: (
            _busy_outcome(HOST_MAPILLARY_TILES) if provider == "mapillary" else True
        ),
    )
    _stub_tail(monkeypatch, sched, conn, published)
    monkeypatch.setattr(
        sched, "send_alert", lambda cfg, subject, body: alerts.append((subject, body))
    )

    rc = sched.cmd_run_due(
        _street_cfg(
            publish_enabled=True,
            alerts=AlertConfig(enabled=True, failure_threshold=99),
        ),
        today=date(2026, 7, 2),
    )

    assert rc == 1
    assert "publish" in published, "still publishes what was collected (#167)"
    assert len(alerts) == 1
    subject, body = alerts[0]
    assert "SKIPPED (host busy)" in subject
    assert "UNAVAILABLE" not in subject, "a busy lock is not the provider refusing us"
    assert "another process on this machine" in body
    assert "NO city was marked failed" in body


def test_the_subprocess_outcome_carries_the_childs_exit_code(monkeypatch, tmp_path):
    """
    The seam the whole breaker rests on: the child's message never crosses the
    process boundary, so if returncode is not copied onto the outcome, every
    host condition reads as an ordinary failure and the breaker is inert.
    """
    import subprocess

    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.download_common import (
        HOST_BUSY_EXIT_CODES,
        HOST_EXIT_CODES,
        HOST_MAPILLARY_TILES,
    )

    city = db.CityRow(
        city_id="bend--or",
        display_name="Bend, OR",
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
        created_at="2026-01-01T00:00:00+00:00",
        enabled=True,
        notes=None,
    )
    cfg = _street_cfg(publish_enabled=False)
    cfg.log_dir = str(tmp_path)

    for code in (
        HOST_EXIT_CODES[HOST_MAPILLARY_TILES],
        HOST_BUSY_EXIT_CODES[HOST_MAPILLARY_TILES],
        1,
    ):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, code=code, **k: subprocess.CompletedProcess(a, code)
        )
        outcome = sched._run_collection_subprocess(
            cfg, ["true"], 60, city, "mapillary", date(2026, 7, 2)
        )
        assert not outcome.ok
        assert outcome.exit_code == code

    # A timeout has no exit code to report — a SIGKILLed child carries none,
    # which is precisely why #209 bounds the Overpass hang rather than relying
    # on the scheduler's timeout to name it.
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="true", timeout=60)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert (
        sched._run_collection_subprocess(
            cfg, ["true"], 60, city, "mapillary", date(2026, 7, 2)
        ).exit_code
        is None
    )


def test_every_scheduled_channel_declares_its_per_ip_hosts():
    """
    CHANNEL_HOSTS is read with .get(provider, ()), so a channel added later
    would silently fail open — no breaker, no error, and the symptom is the
    pre-#208 behaviour on exactly the newest code path.
    """
    from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS
    from streetscape_metadata_tracker.scheduler import CHANNEL_HOSTS, STREET_CHANNELS

    assert set(CHANNEL_HOSTS) == set(KNOWN_PROVIDERS) | set(STREET_CHANNELS)


def test_a_mapillary_block_skips_both_mapillary_channels(conn, monkeypatch):
    """
    The grid channel and the road walk hit the SAME per-IP CDN, so one block
    takes out both (CHANNEL_HOSTS). Neither GSV channel is affected: gsv is
    metered per Google project, and gsv_streets uses Overpass.
    """
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    for name in ("Bend", "Corvallis"):
        _register(conn, name, width=1000, height=1000, step=20)

    ran = []

    def run_one(city, provider):
        ran.append(provider)
        return _blocked_outcome(HOST_MAPILLARY_TILES) if provider == "mapillary" else True

    _run_loop_with(monkeypatch, conn, _street_cfg(publish_enabled=False), run_one)

    assert ran.count("mapillary") == 1
    assert ran.count("mapillary_streets") == 0, "same CDN, same block"
    assert ran.count("gsv") == 2
    assert ran.count("gsv_streets") == 2


# ── the tail reports every condition, and the publish is not a crash surface ──


def _ok_backup(monkeypatch, sched):
    monkeypatch.setattr(
        sched.catalog_backup,
        "write_backup",
        lambda conn, backup_dir, when, **kw: sched.catalog_backup.BackupResult(
            ok=True, path=os.path.join(backup_dir, "stubbed.backup")
        ),
    )


def test_a_failed_index_and_a_failed_publish_arrive_in_ONE_complete_alert(conn, monkeypatch):
    """
    These two fail together far more often than separately — a vanished,
    unwritable or full data_dir breaks generate_aggregate_v2 AND makes
    sync_data_to_server.sh exit 1 — and the publish used to `return 1` above
    the alert block. So the operator got a bare "publish script FAILED" and
    never learned which index broke, or that the catalog backup went with it.

    _stub_tail hid this: it always returned 0 from _publish, so no existing test
    ever had both true at once.
    """
    from streetscape_metadata_tracker import scheduler as sched

    _register(conn, "Alpha", width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    _ok_backup(monkeypatch, sched)
    ran, published = [], []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **kw: ran.append(city.city_id) or True,
    )
    _stub_tail(monkeypatch, sched, conn, published)

    def boom(c, data_dir):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sched, "generate_aggregate_v2", boom)
    monkeypatch.setattr(
        sched, "_publish", lambda cfg, summary, **kw: published.append("publish") or 1
    )

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert "publish" in published, "the publish must still be ATTEMPTED (#167)"
    assert rc == 1
    assert len(alerts) == 1, "one email, not a partial one plus an early return"
    subject, body = alerts[0]
    assert "PUBLISH FAILED" in subject
    assert "1 published index(es) FAILED" in subject, (
        "the subject must name the index too — it is what the operator acts on"
    )
    assert "aggregate index failed" in body
    assert "No space left on device" in body, "the shared root cause must reach the body"


def _fake_publish_popen(returncode=0, write=None, hangs=False, dies_on_kill=True, seen=None):
    """A stand-in for the publish child, patched in at ``subprocess.Popen``.

    Patched at the real API rather than at a scheduler-side seam on purpose: the
    things these tests assert about the child — stdout redirected to a log file,
    ``start_new_session=True``, a BOUNDED wait — are properties of the call
    actually handed to the OS, and a helper-level fake would let any of the three
    be dropped without a test noticing.

    ``hangs`` makes the first wait time out, as a wedged rsync does;
    ``dies_on_kill=False`` additionally makes it survive SIGKILL, which is the
    uninterruptible-NFS case the reap grace exists for.
    """

    class _Proc:
        pid = 424242

        def __init__(self, cmd, **kwargs):
            self.cmd, self.returncode, self.killed = cmd, None, False
            if seen is not None:
                seen.update(kwargs)
                seen["cmd"] = cmd
                seen["waits"] = []
            if write and kwargs.get("stdout"):
                kwargs["stdout"].write(write)
                kwargs["stdout"].flush()

        def wait(self, timeout=None):
            if seen is not None:
                seen["waits"].append(timeout)
            if hangs and (not self.killed or not dies_on_kill):
                raise subprocess.TimeoutExpired(self.cmd, timeout)
            self.returncode = returncode
            return returncode

        def kill(self):
            self.killed = True

    return _Proc


def test_the_publish_child_never_inherits_a_dead_pipe(monkeypatch, tmp_path):
    """
    THE fix for 2026-08-17. Python ignores SIGPIPE only for itself; subprocess
    restores SIG_DFL in children. With stdio inherited, a `run-due ... |
    tail -40` whose reader has gone away means sync_data_to_server.sh (set -euo
    pipefail) takes SIGPIPE on its first echo — before any rsync — and the night
    publishes nothing. Every other guard in this file would have run, and the
    public site would still be stale.
    """
    from streetscape_metadata_tracker import scheduler as sched

    seen = {}

    monkeypatch.setattr(sched.subprocess, "Popen", _fake_publish_popen(seen=seen))
    cfg = _publishing_cfg(log_dir=str(tmp_path))

    assert sched._publish(cfg, "ctx") == 0
    assert seen["stdout"] is not None, "stdout must be redirected, never inherited"
    assert seen["stderr"] == subprocess.STDOUT
    assert seen["stdout"].name.startswith(str(tmp_path)), "…and it must go to a log file"
    assert len(list(tmp_path.glob("publish_*.log"))) == 1


def test_a_failed_publish_alert_carries_the_scripts_own_output(monkeypatch, tmp_path):
    """The publish log is the only place the rsync error text exists; an alert
    that omits it sends the operator back to the machine to find out why."""
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(
        sched.subprocess,
        "Popen",
        _fake_publish_popen(returncode=12, write="rsync: connection unexpectedly closed\n"),
    )
    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    rc = sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx")

    assert rc == 12
    assert len(alerts) == 1
    assert "rsync: connection unexpectedly closed" in alerts[0][1]

    alerts.clear()
    sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx", alert_on_failure=False)
    assert alerts == [], "the batch tail reports the failure itself, in one combined email"


def test_publish_logs_how_long_the_rsync_took(monkeypatch, tmp_path, caplog):
    """The rsync (~6,300 files) is the publish tail's largest component and was
    its only unmeasured one — everything else is either bounded in code
    (catalog_backup.BACKUP_TIMEOUT_S) or visible in the log's own timestamps.
    The tail is what the unit's TimeoutStopSec has to cover during a wind-down,
    so this line is the measurement any future re-sizing of that number has to
    be argued from (issue #206). An unasserted log line is one refactor from
    being dropped.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.subprocess, "Popen", _fake_publish_popen())
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    with caplog.at_level(logging.INFO):
        assert sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx") == 0
    assert re.search(r"Published in \d+\.\d+ s", caplog.text), (
        "a successful publish must record its duration"
    )

    # …and a failed one says how long it took to fail: 2 s (bad path, auth) and
    # 25 minutes (a stalled NFS transfer) are different incidents.
    caplog.clear()
    monkeypatch.setattr(sched.subprocess, "Popen", _fake_publish_popen(returncode=12))
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    with caplog.at_level(logging.INFO):
        assert sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx") == 12
    assert re.search(r"Publish script failed .* after \d+\.\d+ s", caplog.text)
    assert "Published in" not in caplog.text, "a failed publish must not report success"


def test_the_publish_is_bounded(monkeypatch, tmp_path):
    """The publish was the only unbounded subprocess.run in the scheduler, and
    it sits at the very END of the tail — so a hung rsync loses the publish for
    a night whose data is already collected (#167's shape by another route) and,
    since #206, can hold the whole 30-minute stop window by itself (issue #230).

    Asserted on the kwarg rather than on the wall clock: a test that actually
    waited PUBLISH_TIMEOUT_S out would take ten minutes.
    """
    from streetscape_metadata_tracker import scheduler as sched

    seen = {}

    monkeypatch.setattr(sched.subprocess, "Popen", _fake_publish_popen(seen=seen))
    assert sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx") == 0
    assert seen["waits"] == [sched.PUBLISH_TIMEOUT_S], (
        "the publish must be bounded; rsync sits on a half-open SSH connection "
        "or a stalled NFS mount indefinitely rather than erroring (#230)"
    )
    assert seen.get("start_new_session") is True, (
        "the child must lead its own process group, or the timeout's kill "
        "reaches only the shell and orphans the rsync — see "
        "test_a_hung_publish_kills_the_rsync_not_just_the_shell"
    )


# Every subprocess API that STARTS a child, not just the one the file happens to
# use today. `run` was the only spelling here when #230 landed, so a guard that
# named only `run` went green the moment _publish moved to `Popen` — while the
# bound it was guarding moved with it. The blocking wrappers take `timeout=`
# directly; `Popen` hands the caller the wait, so its bound lives on the
# `.wait(...)`/`.communicate(...)` that follows.
_BLOCKING_SUBPROCESS_CALLS = ("run", "call", "check_call", "check_output")


def _enclosing_functions(tree):
    """{node: enclosing ast.FunctionDef} for every Call in the module."""
    import ast

    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                owner.setdefault(node, fn)
    return owner


def test_every_subprocess_in_the_scheduler_is_bounded():
    """The rule generalises, so enforce it rather than trusting the next author.

    Two call sites is a rule for humans and the third reintroduces #230 — the
    same reasoning as the `progress()` and `host_lock` source-inspection guards.
    AST rather than a regex, so a bound spelled across wrapped lines still reads
    as one.

    Checks the whole child-starting API surface, not just `subprocess.run`. That
    is not hypothetical tidiness: #230's own fix moved the publish to `Popen`
    (the timeout's kill has to reach the process GROUP, since the wedged rsync is
    a grandchild of the `bash` we spawn), and a `run`-only guard would have gone
    green across exactly that change. For `Popen` the bound is on the wait, so
    that is what gets looked for.
    """
    import ast

    tree = ast.parse(
        Path(_PROJECT_ROOT, "streetscape_metadata_tracker", "scheduler.py").read_text()
    )
    owner = _enclosing_functions(tree)

    def _subprocess_attr(node):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return None
        value = node.func.value
        if isinstance(value, ast.Name) and value.id == "subprocess":
            return node.func.attr
        return None

    unbounded = []
    for node in ast.walk(tree):
        attr = _subprocess_attr(node)
        if attr in _BLOCKING_SUBPROCESS_CALLS:
            if not any(kw.arg == "timeout" for kw in node.keywords):
                unbounded.append(f"{attr} at line {node.lineno} (no timeout=)")
        elif attr == "Popen":
            # The bound is whatever waits on the handle, so look for a bounded
            # wait in the same function. Loose on purpose: this guard's job is to
            # notice an UNBOUNDED child being started, not to typecheck.
            fn = owner.get(node)
            waited = fn is not None and any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in ("wait", "communicate")
                and any(kw.arg == "timeout" for kw in inner.keywords)
                for inner in ast.walk(fn)
            )
            if not waited:
                unbounded.append(f"Popen at line {node.lineno} (no bounded wait/communicate)")

    assert not unbounded, (
        f"unbounded child in scheduler.py: {unbounded}. Every child this file "
        f"starts outlives a supervisor deadline if it hangs — which for the "
        f"publish meant a SIGKILL with nothing in the log but `Publishing via …` "
        f"(issues #218, #230)."
    )


def test_a_hung_publish_fails_and_alerts_instead_of_hanging(monkeypatch, tmp_path, caplog):
    """A TimeoutExpired is an ordinary publish failure — logged, alerted,
    nonzero — never an exception, because #167's rule is that the tail reports
    rather than propagates. Raising here would take down the alert that is the
    only remaining thing in the tail.

    The line carries the bound AND the real elapsed as two numbers: subprocess's
    post-kill wait() is unbounded, so a gap between them is a SIGKILL the kernel
    deferred (an uninterruptible NFS RPC on the --local path), which nothing
    else in the system would show.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.subprocess, "Popen", _fake_publish_popen(hangs=True))
    monkeypatch.setattr(sched.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(sched.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")
    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))

    with caplog.at_level(logging.INFO):
        rc = sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx")

    assert rc != 0, "a timed-out publish is a failed publish"
    assert re.search(r"Publish script failed \(timed out at \d+ s\) after \d+\.\d+ s", caplog.text)
    assert "Published in" not in caplog.text, "a timed-out publish must not report success"
    assert len(alerts) == 1
    assert "timed out" in alerts[0][1], (
        "the alert must say WHICH failure this was; `exited 1` would send the "
        "operator looking up an rsync exit code that never happened"
    )


def test_a_hung_publish_kills_the_rsync_not_just_the_shell(monkeypatch, tmp_path, caplog):
    """The bound is worth nothing if it kills the wrong process.

    `cmd` is ["bash", sync_data_to_server.sh], and that script runs rsync as an
    ordinary child with echoes after it — no implicit exec — so
    subprocess.run's timeout path (Popen.kill() -> os.kill(self.pid)) reaches
    only the SHELL. The wedged rsync would be reparented and keep going: still
    holding the half-open SSH connection or the stalled NFS mount, still
    appending to the per-day publish log after _tail_lines has read it, and
    still live when a later `regenerate-aggregate --publish` appends to that
    same file and starts a SECOND rsync into the same docroot.

    So the kill must reach the process GROUP, which is why the child is started
    with start_new_session=True. Asserted on killpg rather than on a real
    process tree: spawning one and racing it would make this test the flakiest
    in the file for no extra confidence.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    seen = {}
    killed = []
    monkeypatch.setattr(sched.subprocess, "Popen", _fake_publish_popen(hangs=True, seen=seen))
    monkeypatch.setattr(sched.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sched.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    with caplog.at_level(logging.INFO):
        assert sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx") != 0

    assert seen["start_new_session"] is True, (
        "without its own session the child stays in our process group and "
        "killpg would take the scheduler down with it"
    )
    assert killed == [(424242, signal.SIGKILL)], (
        "the timeout must SIGKILL the child's whole process group; killing the "
        "bash leader alone orphans the rsync that is actually wedged"
    )
    # And the reap that follows is bounded, unlike subprocess.run's.
    assert seen["waits"][-1] == sched._PUBLISH_REAP_GRACE_S


def test_the_publish_reap_is_bounded_when_the_kernel_refuses_the_kill(
    monkeypatch, tmp_path, caplog
):
    """The case no userspace bound can fix, and the one thing this code can still
    do about it: not wait.

    A --local child blocked in an uninterruptible NFS RPC defers SIGKILL until
    the mount answers — exactly as it would defer systemd's. subprocess.run's
    post-kill wait() is unbounded, so inheriting it would hand the tail back the
    very wait PUBLISH_TIMEOUT_S exists to end. The reap gives up after
    _PUBLISH_REAP_GRACE_S, says so, and lets the tail finish.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(
        sched.subprocess, "Popen", _fake_publish_popen(hangs=True, dies_on_kill=False)
    )
    monkeypatch.setattr(sched.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sched.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")

    with caplog.at_level(logging.INFO):
        assert sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx") != 0

    assert "did not die within" in caplog.text, (
        "a child that survives SIGKILL must be named, not silently abandoned — "
        "it is still holding the mount the next publish will want"
    )


def test_a_publish_interrupted_by_ctrl_c_still_kills_its_group(monkeypatch, tmp_path):
    """start_new_session takes the child OUT of the terminal's foreground process
    group, so Ctrl-C no longer reaches it — which would trade the timeout's
    orphan for an interactive one. Anything unwinding past the wait kills the
    group on the way out.
    """
    from streetscape_metadata_tracker import scheduler as sched

    class _Interrupting:
        pid = 424242

        def __init__(self, cmd, **kw):
            pass

        def wait(self, timeout=None):
            raise KeyboardInterrupt

        def kill(self):
            pass

    killed = []
    monkeypatch.setattr(sched.subprocess, "Popen", _Interrupting)
    monkeypatch.setattr(sched.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sched.os, "killpg", lambda pgid, sig: killed.append(pgid))

    with pytest.raises(KeyboardInterrupt):
        sched._publish(_publishing_cfg(log_dir=str(tmp_path)), "ctx")
    assert killed == [424242], "Ctrl-C must not leave the rsync running detached"


def test_the_batch_email_tail_survives_a_pasted_publish_failure(monkeypatch, tmp_path):
    """#218 put the publish log's tail INTO the scheduler log, and the batch email
    quotes that log's last N lines — so the fix can evict the context it exists
    to be read beside.

    A failed publish contributes _CHILD_LOG_TAIL_LINES + 2 lines and is the LAST
    thing a night writes, so at the old 40-line default it took 27 of 40 and
    pushed out which cities failed and which host refused us. The window is
    sized against what gets pasted into the log, not against the log's own
    narrative.
    """
    from streetscape_metadata_tracker import scheduler as sched

    assert sched._BATCH_LOG_TAIL_LINES >= 3 * (sched._CHILD_LOG_TAIL_LINES + 2) + 20, (
        "the batch email's window must hold a failed publish plus a couple of "
        "failed channels AND still show the night's own summary lines; sized "
        "below that, the most recent pasted block silently evicts the rest"
    )

    log = tmp_path / "streetscape_scheduler.log"
    log.write_text("".join(f"line {i}\n" for i in range(400)), encoding="utf-8")
    cfg = _publishing_cfg(log_dir=str(tmp_path))
    tail = sched._recent_log_tail(cfg, sched._BATCH_LOG_TAIL_LINES)
    assert len(tail.splitlines()) == sched._BATCH_LOG_TAIL_LINES


def test_a_failed_publish_reaches_the_batch_email_through_the_scheduler_log(monkeypatch, tmp_path):
    """The gap #218 named, pinned from the side that was broken.

    The nightly path calls _publish(alert_on_failure=False) so _finish_batch can
    send ONE combined email — and that email pastes _recent_log_tail and nothing
    else. While the script's output was copied only into _publish's own alert,
    every night that failed to publish reported a bare status and left the rsync
    error in a file on a host nobody reads.

    Asserted through _recent_log_tail rather than caplog, because the file is
    what the email actually quotes: a logger.error that never reached the handler
    would satisfy caplog and still lose the night's explanation.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(
        sched.subprocess,
        "Popen",
        _fake_publish_popen(returncode=12, write="rsync: connection unexpectedly closed\n"),
    )
    cfg = _publishing_cfg(log_dir=str(tmp_path))

    handler = logging.FileHandler(tmp_path / "streetscape_scheduler.log", encoding="utf-8")
    sched.logger.addHandler(handler)
    try:
        assert sched._publish(cfg, "ctx", alert_on_failure=False) == 12
    finally:
        sched.logger.removeHandler(handler)
        handler.close()

    assert "rsync: connection unexpectedly closed" in sched._recent_log_tail(cfg), (
        "the batch email quotes the scheduler log and nothing else, so the "
        "publish script's own output has to land THERE, not only in the alert "
        "the batch path suppresses (issue #218)"
    )


def test_a_loop_crash_still_names_itself_when_an_index_also_failed(conn, monkeypatch):
    """`errored` had no subject part, so a night that BOTH crashed in the city
    loop and failed an index reported only the index — and the crash, the more
    serious of the two, appeared nowhere in the line that actually gets read."""
    from streetscape_metadata_tracker import scheduler as sched

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")
    _ok_backup(monkeypatch, sched)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "generate_driving_plan_summary", lambda c, d: {})

    def boom(c, data_dir):
        raise RuntimeError("index broke")

    monkeypatch.setattr(sched, "generate_aggregate_v2", boom)

    rc = sched._finish_batch(
        _publishing_cfg(publish_enabled=False),
        conn,
        "summary line",
        succeeded=1,
        attempted=1,
        today=date(2026, 7, 2),
        errored=True,
    )

    assert rc == 1
    subject = alerts[0][0]
    assert "LOOP CRASHED" in subject
    assert "1 published index(es) FAILED" in subject
    assert "0 failed collection(s)" not in subject, "no zero-noise when a real condition exists"


def test_an_errored_night_alone_reads_cleanly(conn, monkeypatch):
    """The flip side: with nothing else wrong, the subject should be the crash,
    not the crash plus a meaningless '0 failed collection(s)'."""
    from streetscape_metadata_tracker import scheduler as sched

    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subj, body: alerts.append((subj, body)))
    monkeypatch.setattr(sched, "_recent_log_tail", lambda cfg, n=40: "")
    _ok_backup(monkeypatch, sched)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: {"cities_count": 0})
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    monkeypatch.setattr(sched, "generate_driving_plan_summary", lambda c, d: {"records": []})

    rc = sched._finish_batch(
        _publishing_cfg(publish_enabled=False),
        conn,
        "summary line",
        succeeded=1,
        attempted=1,
        today=date(2026, 7, 2),
        errored=True,
    )

    assert rc == 1
    assert alerts[0][0].startswith("LOOP CRASHED on ")


def test_regenerate_still_publishes_when_an_index_fails(conn, monkeypatch, capsys):
    """
    cmd_regenerate is what _tail_artifact's docstring prescribes as the recovery
    from a stale index, so it has to survive the conditions that make one stale.
    It used to call all three builders unguarded and then print BEFORE
    publishing — so a dead stdout, or one broken index, aborted the recovery
    before it recovered anything.
    """
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    def boom(c, data_dir):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sched, "generate_aggregate_v2", boom)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": [1, 2]})
    monkeypatch.setattr(sched, "generate_driving_plan_summary", lambda c, d: {"records": [1]})

    published = []
    monkeypatch.setattr(sched, "_publish", lambda cfg, ctx, **kw: published.append("publish") or 0)

    rc = sched.cmd_regenerate(_publishing_cfg(), publish=True)

    assert published == ["publish"], (
        "the two healthy indexes and the previous good aggregate must still ship"
    )
    assert rc == 1, "but the failure is reported, not swallowed"
    out = capsys.readouterr().out
    assert "cities.json.gz NOT regenerated" in out, (
        "the operator must see WHICH index did not rebuild"
    )
    assert "2 walks" in out, "…and the ones that did must still report their counts"


def test_emit_survives_a_reader_that_went_away():
    """cmd_regenerate's prints sit in front of its publish; a dead pipe there
    must not abort the recovery."""
    from streetscape_metadata_tracker import scheduler as sched

    class DeadStdout:
        def write(self, *a, **kw):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    with contextlib.redirect_stdout(DeadStdout()):
        sched._emit("this must not raise")


def test_exit_reports_the_status_we_computed_even_with_a_broken_stdout(monkeypatch):
    """
    CPython replaces the exit status with 120 when finalization's flush of
    sys.stdout fails — and setup_logging's StreamHandler(sys.stdout) guarantees
    there is buffered data to fail on. That silently clobbered the whole exit
    vocabulary (0/1, 64, 75/76, 79/80): a healthy piped night reported failure
    to systemd, and a wrapper could not read 64 as "bad argument" any more.
    """
    from streetscape_metadata_tracker import scheduler as sched

    class DeadStream:
        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

        def fileno(self):
            raise io.UnsupportedOperation("no fd here")

    monkeypatch.setattr(sched.sys, "stdout", DeadStream())
    monkeypatch.setattr(sched.sys, "stderr", DeadStream())

    with pytest.raises(SystemExit) as excinfo:
        sched._exit(sched.USAGE_EXIT_CODE)
    assert excinfo.value.code == sched.USAGE_EXIT_CODE, (
        "a broken pipe must not rewrite the status this module chose"
    )

    with pytest.raises(SystemExit) as excinfo:
        sched._exit(0)
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# Concurrent channel lanes (issue #240)
#
# One city's channels may run at once, up to [schedule].max_concurrent_channels
# — but never two that need the same per-IP third party. That is the whole
# safety argument: what Mapillary's tile CDN, Overpass and KartaView see from
# this machine is unchanged, and only the night's wall clock moves.
#
# Everything here is an ORDERING claim, which is why these tests read the
# start/end log a fake collection writes rather than timing anything. Every wait
# carries a timeout, so a lane scheduler that has stopped overlapping (or has
# started overlapping things it must not) fails red instead of hanging the suite.
# ---------------------------------------------------------------------------

# Generous enough that a loaded CI box never trips it, short enough that a real
# breakage is a failed test rather than a hung run.
_LANE_TIMEOUT_S = 10.0
# How long a lane test holds channels open while watching for one that must not
# have been launched. Absence is only observable by waiting, and this window is
# elapsed in full only when nothing went wrong — so keep it small enough to pay
# on every run and long enough that a main thread finishing one more pricing
# pass (a couple of catalog reads) lands well inside it.
_AFFINITY_PROBE_S = 0.5


class _LaneLog:
    """Thread-safe start/end record of the fake collections a city ran.

    ``events`` is ``("start" | "end", city_id, provider, seq)`` in the order the
    lanes actually reached those points. A monotonic ``seq`` taken under the
    lock rather than a timestamp: every question worth asking here is a
    happens-before question ("did these two overlap?", "was that one ever
    started at all?"), and a clock would only make the answers flaky.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.events: list[tuple[str, str, str, int]] = []

    def record(self, kind: str, city_id: str, provider: str) -> None:
        with self._lock:
            self.events.append((kind, city_id, provider, len(self.events) + 1))

    def seq(self, kind: str, provider: str) -> int:
        """The sequence number of the one ``kind`` event for ``provider``."""
        hits = [e[3] for e in self.events if e[0] == kind and e[2] == provider]
        assert len(hits) == 1, f"expected exactly one {kind!r} for {provider}, got {hits}"
        return hits[0]

    def starts(self) -> list[str]:
        return [e[2] for e in self.events if e[0] == "start"]

    def cities(self) -> list[str]:
        """City ids in the order their first event landed."""
        seen = []
        for _kind, city_id, _provider, _seq in self.events:
            if city_id not in seen:
                seen.append(city_id)
        return seen

    def peak_in_flight(self) -> int:
        peak = live = 0
        for kind, *_rest in self.events:
            live += 1 if kind == "start" else -1
            peak = max(peak, live)
        return peak


def _stub_lane_collection(sched, monkeypatch, *, outcome=None, gate=None):
    """Stand in for the collection subprocess in a lane test (issue #240).

    ``_stub_collection``'s fake is a single point in time; a lane test needs both
    edges, because what the lane scheduler has to prove is which channels were
    alive at the same moment.

    It never touches ``conn``, and that is the point rather than an omission: the
    scheduler hands a worker ``conn=None`` because ``db.connect`` opens the
    catalog ``check_same_thread=True``, so a fake that quietly reached for the
    fixture handle would go green over a design that could not work off-thread.

    ``gate(city_id, provider)`` runs between the two edges and is where a test
    puts a Barrier or an Event to force — or to forbid — an overlap.
    """
    log = _LaneLog()
    outcome = outcome or (lambda city_id, provider: True)

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        log.record("start", city.city_id, provider)
        try:
            if gate is not None:
                gate(city.city_id, provider)
            return outcome(city.city_id, provider)
        finally:
            log.record("end", city.city_id, provider)

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    return log


def _lane_city(conn, name="Bend"):
    """A registered city plus its frozen row, ready for _run_city_channels."""
    return db.resolve_city(conn, _register(conn, name, width=1000, height=1000, step=20))


def _run_channels(sched, cfg, conn, city, providers, **overrides):
    """Drive _run_city_channels directly, with the batch's arguments defaulted."""
    kwargs = dict(
        blocked_hosts=set(),
        busy_hosts=Counter(),
        batch_deadline=None,
        stop_requested=None,
    )
    kwargs.update(overrides)
    return sched._run_city_channels(cfg, conn, city, date(2026, 7, 2), providers, **kwargs)


def _no_salvage(sched, monkeypatch):
    """Neutralize orphan reconciliation for tests whose fakes report failure.

    Both reconcilers read the catalog and the data directory; a lane test is
    about scheduling, and letting a real salvage attempt run would make it
    depend on what happens to be on disk.
    """
    monkeypatch.setattr(sched, "_reconcile_orphaned_run", lambda *a, **k: False)
    monkeypatch.setattr(sched, "_reconcile_orphaned_walk", lambda *a, **k: False)


def test_host_disjoint_channels_of_one_city_genuinely_overlap_at_knob_2(conn, monkeypatch):
    """The premise of #240: two channels that share no per-IP host run at once.

    The barrier is the assertion — a scheduler that still runs channels
    back-to-back never gets a second party to it and fails on the timeout. gsv
    (no per-IP host) and mapillary (the tile CDN) are the disjoint pair.
    """
    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    barrier = threading.Barrier(2)
    log = _stub_lane_collection(
        sched, monkeypatch, gate=lambda city_id, provider: barrier.wait(_LANE_TIMEOUT_S)
    )

    attempted, succeeded, skipped = _run_channels(
        sched, _street_cfg(max_concurrent_channels=2), conn, city, ["gsv", "mapillary"]
    )

    assert (attempted, succeeded, skipped) == (2, 2, 0)
    assert log.peak_in_flight() == 2, "both channels must have been in flight together"


def test_no_more_than_max_concurrent_channels_are_ever_in_flight(conn, monkeypatch):
    """The knob is a cap, not a hint.

    gsv (no host), gsv_streets (Overpass) and mapillary (tiles) are pairwise
    host-disjoint, so affinity alone would let all three run together; at knob 2
    exactly two ever may. The barrier proves the floor (a sequential
    implementation cannot clear it), the peak proves the ceiling.
    """
    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    barrier = threading.Barrier(2)

    def gate(city_id, provider):
        # Only the first two launched wait; the third must not be able to join a
        # party of two, and would deadlock the run if it tried.
        if provider in ("gsv", "gsv_streets"):
            barrier.wait(_LANE_TIMEOUT_S)

    log = _stub_lane_collection(sched, monkeypatch, gate=gate)

    attempted, succeeded, _skipped = _run_channels(
        sched,
        _street_cfg(max_concurrent_channels=2),
        conn,
        city,
        ["gsv", "gsv_streets", "mapillary"],
    )

    assert (attempted, succeeded) == (3, 3)
    assert log.peak_in_flight() == 2, (
        "three host-disjoint channels at knob 2 must still peak at two in flight"
    )


def test_channels_sharing_a_host_never_overlap_even_at_knob_4(conn, monkeypatch):
    """The provider-facing invariant, and the reason #240 is safe to ship.

    mapillary_streets needs BOTH Overpass (gsv_streets' host) and the tile CDN
    (mapillary's), so however high the knob goes it must start only after both
    of those have finished. If it ever overlapped either, the tile CDN would see
    two talkers from this IP and the configured 60/min would silently become
    120/min — the exact shape of the 2026-08-12 block (see docs/provider-access.md).
    """
    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    trio = threading.Barrier(3)
    streets_started = threading.Event()
    overlapped_with: list[str] = []
    # Only the channels that actually SHARE a per-IP host with mapillary_streets
    # can be violated by it. gsv declares no host at all, so gsv running
    # alongside mapillary_streets is not the bug — it is the feature #240 exists
    # to deliver. Recording it as an overlap made this test fail on roughly one
    # run in eight: all three probe windows open together, so whenever gsv was
    # the last of the trio off the barrier, the other two could finish, be
    # classified, and let mapillary_streets start while gsv was still watching.
    shares_a_host_with_streets = {
        p
        for p in ("gsv", "gsv_streets", "mapillary")
        if set(sched.CHANNEL_HOSTS[p]) & set(sched.CHANNEL_HOSTS["mapillary_streets"])
    }
    assert shares_a_host_with_streets == {"gsv_streets", "mapillary"}, (
        "the host map changed; this test's premise (gsv is the disjoint one) needs re-deriving"
    )

    def gate(city_id, provider):
        if provider == "mapillary_streets":
            streets_started.set()
            return
        trio.wait(_LANE_TIMEOUT_S)
        # The three disjoint channels are now all alive. HOLD them there and
        # watch for the fourth, because the interesting failure does not
        # announce itself: with affinity removed, mapillary_streets is submitted
        # in the same pass, but it is a thread that does nothing, so it can
        # still be scheduled after these three have returned. Timing alone would
        # therefore let a scheduler with no affinity at all pass this test. The
        # window only elapses in full when the answer is the right one.
        if streets_started.wait(_AFFINITY_PROBE_S) and provider in shares_a_host_with_streets:
            overlapped_with.append(provider)

    log = _stub_lane_collection(sched, monkeypatch, gate=gate)

    # Submit order is a property of the PARENT, so read it there rather than
    # from the workers' start events: several lanes launched in one pass reach
    # their first line in whatever order the OS feels like. city_timeout_seconds
    # is called once per launched channel, on the launching thread, immediately
    # before the submit.
    submitted: list[str] = []

    def spy_timeout(cfg, city, provider, conn=None, remaining_s=None):
        submitted.append(provider)
        return 600

    monkeypatch.setattr(sched, "city_timeout_seconds", spy_timeout)

    attempted, succeeded, _skipped = _run_channels(
        sched,
        _street_cfg(max_concurrent_channels=4),
        conn,
        city,
        ["gsv", "gsv_streets", "mapillary", "mapillary_streets"],
    )

    assert (attempted, succeeded) == (4, 4)
    assert overlapped_with == [], (
        "mapillary_streets ran while a channel it shares a per-IP host with was "
        "still in flight — that is two talkers to one metered host from one "
        "process, which is the whole thing #240 must not do"
    )
    assert submitted == ["gsv", "gsv_streets", "mapillary", "mapillary_streets"], (
        "channels are submitted in canonical (most-expensive-first) order"
    )
    assert log.peak_in_flight() == 3, (
        "THESE four channels at knob 4 still peak at three: mapillary_streets "
        "shares a host with two of them, so the ceiling is host affinity and "
        "not the knob. The figure is a property of the channel SET, not a "
        "constant — kartaview shares its host with nothing (#248), so the five "
        "configured channels peak at four"
    )


def test_a_mapillary_block_at_knob_3_still_means_mapillary_streets_is_never_submitted(
    conn, monkeypatch, caplog
):
    """The breaker's read-then-act ordering has to survive a deferral.

    Sequentially this was trivial: mapillary ran, recorded the block, and
    mapillary_streets read `blocked_hosts` afterwards. With lanes the second
    channel is deferred rather than reached in order — and it stays deferred
    precisely BECAUSE it shares the blocked host, so it is still un-launched when
    the block is recorded and hits the breaker at its own submit. Re-verification
    of test_a_mapillary_block_skips_both_mapillary_channels at knob > 1.

    The staging is the test, and it is deliberate rather than incidental. At knob
    3 the first launch pass fills every lane, so mapillary_streets is held back
    by the LANE CAP, which an affinity bug would also respect — left there, this
    test passes with or without the gate. The window that actually distinguishes
    them is the launch pass triggered when a lane frees while mapillary is still
    in flight and still unclassified, so the test builds exactly that: gsv is let
    go first, the other two are held until gsv has been fully classified
    (``record_attempt`` is the seam, since that is the last thing classification
    does), and mapillary_streets is watched for during a probe window that only
    elapses in full when it was never submitted.

    Without this staging the outcome turned on which future ``wait`` happened to
    return first: measured at 4 failures in 6 runs with the affinity gate deleted,
    i.e. a mutation detector that missed a third of the time. It now fails every
    run — see docs/testing.md.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    city = _lane_city(conn)
    gsv_classified = threading.Event()
    held_pair = threading.Barrier(2)
    streets_started = threading.Event()
    overlapped_with: list[str] = []

    real_record_attempt = sched.db.record_attempt

    def spy_record_attempt(conn_, city_id, *args, provider=None, **kwargs):
        result = real_record_attempt(conn_, city_id, *args, provider=provider, **kwargs)
        if provider == "gsv":
            # Classification of gsv is COMPLETE. Whatever the lane scheduler does
            # with the freed lane, it does from here on, while the other two
            # channels are demonstrably still in flight.
            gsv_classified.set()
        return result

    monkeypatch.setattr(sched.db, "record_attempt", spy_record_attempt)

    def gate(city_id, provider):
        if provider == "gsv":
            return  # frees a lane immediately
        if provider == "mapillary_streets":
            streets_started.set()
            return
        # gsv_streets and mapillary: stay alive across the launch pass that the
        # freed lane triggers, then watch for the channel that must not appear.
        assert gsv_classified.wait(_LANE_TIMEOUT_S), "gsv was never classified"
        held_pair.wait(_LANE_TIMEOUT_S)
        if streets_started.wait(_AFFINITY_PROBE_S):
            overlapped_with.append(provider)

    log = _stub_lane_collection(
        sched,
        monkeypatch,
        gate=gate,
        outcome=lambda city_id, provider: (
            _blocked_outcome(HOST_MAPILLARY_TILES) if provider == "mapillary" else True
        ),
    )
    blocked: set[str] = set()

    with caplog.at_level(logging.INFO):
        attempted, succeeded, skipped = _run_channels(
            sched,
            _street_cfg(max_concurrent_channels=3),
            conn,
            city,
            ["gsv", "gsv_streets", "mapillary", "mapillary_streets"],
            blocked_hosts=blocked,
        )

    assert overlapped_with == [], (
        "mapillary_streets was submitted into the window where a lane had freed "
        "but mapillary's block was not yet recorded — the breaker cannot save a "
        "channel that is already running, which is why affinity has to hold it"
    )
    assert "mapillary_streets" not in log.starts(), (
        "a channel needing a host that refused us must never be launched"
    )
    assert blocked == {HOST_MAPILLARY_TILES}
    assert (attempted, succeeded, skipped) == (2, 2, 0), (
        "neither the blocked channel nor the one skipped behind it is an attempt"
    )
    assert "already refused this host" in caplog.text


def test_a_busy_exit_at_knob_3_skips_one_channel_without_poisoning_the_lanes(conn, monkeypatch):
    """Busy is not blocked, and the lanes must keep that distinction.

    A busy exit means another process on this machine holds the host — it ends
    when that process does — so it skips one channel and nothing else. Getting
    this wrong in the other direction would be worse than the sequential bug it
    mirrors: one manual run would cost the whole night's Mapillary work.
    """
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES

    city = _lane_city(conn)
    log = _stub_lane_collection(
        sched,
        monkeypatch,
        outcome=lambda city_id, provider: (
            _busy_outcome(HOST_MAPILLARY_TILES) if provider == "mapillary" else True
        ),
    )
    blocked: set[str] = set()
    busy: Counter[str] = Counter()

    attempted, succeeded, skipped = _run_channels(
        sched,
        _street_cfg(max_concurrent_channels=3),
        conn,
        city,
        ["gsv", "gsv_streets", "mapillary", "mapillary_streets"],
        blocked_hosts=blocked,
        busy_hosts=busy,
    )

    assert busy == Counter({HOST_MAPILLARY_TILES: 1})
    assert blocked == set(), "a busy local lock is not a provider refusal"
    assert "mapillary_streets" in log.starts(), (
        "the sibling of a busy channel is still worth running — the lock frees "
        "when the other local process finishes"
    )
    assert (attempted, succeeded, skipped) == (3, 3, 0)


def test_budget_skips_are_final_but_host_deferrals_relaunch(conn, monkeypatch):
    """The two ways a channel can fail to launch are not the same thing.

    A budget skip is a decision — priced once, logged, and never reconsidered
    for this city tonight. A host deferral is not a decision at all: the channel
    stays pending, silently, and launches the moment its sibling frees the host.
    Conflating them would either re-price skipped channels in a loop or drop
    deferred ones on the floor.
    """
    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    cfg = _street_cfg(max_concurrent_channels=3)
    # Priced above its entire daily budget, so mapillary can never fit tonight.
    cfg.providers["mapillary"] = ProviderConfig(enabled=True, daily_request_budget=0)

    priced: list[str] = []
    real_estimate = sched.estimate_requests
    monkeypatch.setattr(
        sched,
        "estimate_requests",
        lambda city, provider="gsv", **kw: (
            priced.append(provider) or real_estimate(city, provider, **kw)
        ),
    )
    barrier = threading.Barrier(2)

    def gate(city_id, provider):
        if provider in ("gsv", "gsv_streets"):
            barrier.wait(_LANE_TIMEOUT_S)

    log = _stub_lane_collection(sched, monkeypatch, gate=gate)

    attempted, succeeded, skipped = _run_channels(
        sched, cfg, conn, city, ["gsv", "gsv_streets", "mapillary", "mapillary_streets"]
    )

    assert "mapillary" not in log.starts()
    assert skipped == 1
    assert priced.count("mapillary") == 1, (
        "a channel skipped on budget must be priced once and then left alone; "
        "re-pricing it every pass is how a deferral loop turns into a spin"
    )
    assert (attempted, succeeded) == (3, 3)
    assert log.seq("start", "mapillary_streets") > log.seq("end", "gsv_streets"), (
        "the deferred channel waits for the Overpass sibling and then runs"
    )


def test_a_stop_lets_in_flight_lanes_drain_and_declines_the_pending_channels_by_name(
    conn, monkeypatch, caplog
):
    """A stop is a SUBMIT gate, not a kill.

    Work already in flight has been paid for whatever we do next, so it finishes
    and is credited; what a wind-down actually prevents is the channels nothing
    has been asked of yet — and with Mapillary enabled those are the ones that
    would otherwise fire into a live per-IP tile block (#205/#206). Naming them
    is the whole point of the message.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    stop = threading.Event()
    barrier = threading.Barrier(2)

    def gate(city_id, provider):
        barrier.wait(_LANE_TIMEOUT_S)  # both lanes are live before the stop lands
        stop.set()

    log = _stub_lane_collection(sched, monkeypatch, gate=gate)

    with caplog.at_level(logging.INFO):
        attempted, succeeded, skipped = _run_channels(
            sched,
            _street_cfg(max_concurrent_channels=2),
            conn,
            city,
            ["gsv", "gsv_streets", "mapillary", "mapillary_streets"],
            stop_requested=stop,
        )

    assert sorted(log.starts()) == ["gsv", "gsv_streets"]
    assert (attempted, succeeded, skipped) == (2, 2, 0), (
        "children that finished before the stop reached them are still credited"
    )
    assert f"{city.city_id}: stop requested — not starting mapillary, mapillary_streets" in (
        caplog.text
    )


def test_children_killed_together_by_the_stop_are_not_failures_for_any_channel(
    conn, monkeypatch, caplog
):
    """The unit's KillMode is control-group, so a `systemctl stop` reaches every
    in-flight child at once — N of them now, not one.

    Each dies with "exited -15", which is in neither host-exit table and reads as
    an ordinary collection failure. Charging even one to the city burns a
    `consecutive_failures` slot that only a success ever resets, and makes a
    deliberate stop end the unit red (issue #206). The amnesty therefore has to
    be applied per RESULT, not once at the exit.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    _no_salvage(sched, monkeypatch)
    recorded = []
    monkeypatch.setattr(sched.db, "record_attempt", lambda *a, **k: recorded.append((a, k)) or None)

    stop = threading.Event()
    barrier = threading.Barrier(2)

    def gate(city_id, provider):
        barrier.wait(_LANE_TIMEOUT_S)
        stop.set()

    _stub_lane_collection(
        sched,
        monkeypatch,
        gate=gate,
        outcome=lambda city_id, provider: sched.CollectionOutcome(
            False, f"exited -15 (see collect_{provider}.log)", -15
        ),
    )

    with caplog.at_level(logging.INFO):
        attempted, succeeded, skipped = _run_channels(
            sched,
            _street_cfg(max_concurrent_channels=2),
            conn,
            city,
            ["gsv", "gsv_streets", "mapillary", "mapillary_streets"],
            stop_requested=stop,
        )

    assert (attempted, succeeded, skipped) == (0, 0, 0), (
        "a channel we killed was never really attempted"
    )
    assert recorded == [], "a stop must not record a failure against any channel"
    assert caplog.text.count("child was killed by the stop signal") == 2
    assert "not starting mapillary, mapillary_streets" in caplog.text


def test_a_worker_error_drains_the_siblings_before_the_night_reports_unhealthy(conn, monkeypatch):
    """An unexpected exception in one lane must not discard its siblings' work.

    The siblings' collections are already paid for, so they are classified and
    recorded first; only then does the error propagate, which is what turns the
    night into _STOP_REASON_ERROR upstream — unhealthy, but still publishing
    what it collected (issue #167).
    """
    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    recorded = []
    monkeypatch.setattr(
        sched.db,
        "record_attempt",
        lambda conn_, city_id, success, provider=None, error=None: recorded.append(
            (provider, success)
        ),
    )
    barrier = threading.Barrier(2)

    def outcome(city_id, provider):
        if provider == "gsv":
            raise RuntimeError("something unexpected")
        return True

    _stub_lane_collection(
        sched,
        monkeypatch,
        gate=lambda city_id, provider: barrier.wait(_LANE_TIMEOUT_S),
        outcome=outcome,
    )

    with pytest.raises(RuntimeError, match="something unexpected"):
        _run_channels(
            sched, _street_cfg(max_concurrent_channels=2), conn, city, ["gsv", "gsv_streets"]
        )

    assert recorded == [("gsv_streets", True)], (
        "the sibling that finished must be credited before the error escapes"
    )


def test_a_MAIN_thread_error_also_drains_the_siblings_and_still_trips_the_breaker(
    conn, monkeypatch
):
    """The mirror of the worker-error case, and the one lanes actually created.

    Classification runs on the main thread and touches the catalog and the log,
    so it can raise (a sqlite error out of `record_attempt`, a salvage that
    throws, a `BrokenPipeError` out of `logger.error` — this file treats a dead
    output pipe as a live condition in four other places). Before #240 that could
    not lose anything: one channel was in flight and it was classified before the
    next started. With lanes, a raise there used to skip the rest of the drain,
    block in `pool.shutdown(wait=True)` for the siblings' FULL remaining runtime,
    and then discard their already-paid-for outcomes.

    The expensive half of that is invisible in a test; the cheap half is not.
    `blocked_hosts` is the assertion because it is the one that outlives the
    city: a host refusal dropped here means the night-level breaker never trips
    and every later city keeps firing at a host that already refused this IP.
    """
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.download_common import HOST_OVERPASS

    city = _lane_city(conn)
    # gsv is let go first and gsv_streets is held until gsv's classification has
    # actually raised, so the failure is guaranteed to land with a sibling still
    # in flight. Without that staging the two finish together, `wait` may hand
    # back gsv_streets first, and the test passes whether or not the drain works.
    gsv_classification_failed = threading.Event()

    def exploding_record_attempt(conn_, city_id, success, provider=None, error=None):
        if provider == "gsv":
            gsv_classification_failed.set()
            raise sqlite3.OperationalError("database is locked")

    def gate(city_id, provider):
        if provider == "gsv":
            return
        assert gsv_classification_failed.wait(_LANE_TIMEOUT_S), "gsv was never classified"

    monkeypatch.setattr(sched.db, "record_attempt", exploding_record_attempt)
    _stub_lane_collection(
        sched,
        monkeypatch,
        gate=gate,
        outcome=lambda city_id, provider: (
            True if provider == "gsv" else _blocked_outcome(HOST_OVERPASS)
        ),
    )
    blocked: set[str] = set()

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _run_channels(
            sched,
            _street_cfg(max_concurrent_channels=2),
            conn,
            city,
            ["gsv", "gsv_streets"],
            blocked_hosts=blocked,
        )

    assert blocked == {HOST_OVERPASS}, (
        "the sibling's host refusal was already paid for and must survive the "
        "main thread's own failure — otherwise the night-level breaker never "
        "trips and later cities keep hitting a host that refused this IP"
    )


def test_every_lane_exception_is_logged_even_though_only_one_is_re_raised(
    conn, monkeypatch, caplog
):
    """Two lanes can fail for unrelated reasons in one completion pass.

    Only one exception can propagate, so the other's cause exists nowhere but the
    log — and the [alerts] email carries only this log's tail. Stashing the first
    and silently dropping the rest recreates the "collection failed with no
    cause" hole the per-attempt child logs were added to close.
    """
    import logging

    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    barrier = threading.Barrier(2)

    def outcome(city_id, provider):
        raise RuntimeError(f"{provider} blew up")

    _stub_lane_collection(
        sched,
        monkeypatch,
        gate=lambda city_id, provider: barrier.wait(_LANE_TIMEOUT_S),
        outcome=outcome,
    )

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="blew up"):
        _run_channels(
            sched, _street_cfg(max_concurrent_channels=2), conn, city, ["gsv", "gsv_streets"]
        )

    assert "gsv blew up" in caplog.text and "gsv_streets blew up" in caplog.text, (
        "both causes must reach the log; only one of them can be re-raised"
    )


def test_the_deadline_is_a_submit_gate_and_every_lane_child_gets_its_own_remaining_s(
    conn, monkeypatch
):
    """Each child is priced against the shared deadline at ITS OWN submit.

    That is the correct reading with lanes: every in-flight child genuinely does
    have until the same deadline, so one clock read per LAUNCHED channel is not
    an approximation. Pricing them all off one read taken before the first launch
    would hand a late-launching channel a timeout it has no right to.

    The clock here is safe to stub despite the knob (see _WorkClock): every
    advance happens inside the recorder, which the launch pass calls on the main
    thread.
    """
    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    clock = _WorkClock(per_unit_s=1000.0)
    monkeypatch.setattr(sched.time, "monotonic", clock)

    priced = []
    main_thread = threading.get_ident()

    def fake_timeout(cfg, city, provider, conn=None, remaining_s=None):
        priced.append((provider, remaining_s, threading.get_ident()))
        clock.work()
        return 600

    monkeypatch.setattr(sched, "city_timeout_seconds", fake_timeout)
    _stub_lane_collection(sched, monkeypatch)

    _run_channels(
        sched,
        _street_cfg(max_concurrent_channels=3),
        conn,
        city,
        ["gsv", "gsv_streets", "mapillary", "mapillary_streets"],
        batch_deadline=10_000.0,
    )

    assert priced == [
        ("gsv", 10_000.0, main_thread),
        ("gsv_streets", 9_000.0, main_thread),
        ("mapillary", 8_000.0, main_thread),
        ("mapillary_streets", 7_000.0, main_thread),
    ], (
        "one clock read per launched channel, on the launching thread, each "
        "priced against what was left of the deadline when IT started"
    )


def test_budget_pricing_and_ledger_reads_stay_on_the_main_thread_in_submit_order(conn, monkeypatch):
    """A lane worker runs _run_one_city and nothing else.

    ``db.connect`` opens the catalog with ``check_same_thread=True``, so every
    read of it — the budget ledger included — has to happen on the thread that
    owns it. This is also what keeps the read-then-write budget guard honest:
    the reads are serialized by being on one thread, in submit order, so two
    channels cannot both see "under budget" and both spend.
    """
    from streetscape_metadata_tracker import scheduler as sched

    city = _lane_city(conn)
    main_thread = threading.get_ident()
    calls = []

    real_estimate = sched.estimate_requests
    real_usage = sched.db.get_api_usage

    def spy_estimate(city, provider="gsv", **kw):
        calls.append(("estimate", provider, threading.get_ident()))
        return real_estimate(city, provider, **kw)

    def spy_usage(conn_, day, provider):
        calls.append(("usage", provider, threading.get_ident()))
        return real_usage(conn_, day, provider)

    monkeypatch.setattr(sched, "estimate_requests", spy_estimate)
    monkeypatch.setattr(sched.db, "get_api_usage", spy_usage)
    _stub_lane_collection(sched, monkeypatch)

    _run_channels(
        sched,
        _street_cfg(max_concurrent_channels=3),
        conn,
        city,
        ["gsv", "gsv_streets", "mapillary", "mapillary_streets"],
    )

    assert {ident for _kind, _provider, ident in calls} == {main_thread}
    assert [provider for kind, provider, _ in calls if kind == "usage"] == [
        "gsv",
        "gsv_streets",
        "mapillary",
        "mapillary_streets",
    ], "the ledger is read once per channel, in submit order, on one thread"


def test_a_citys_lanes_drain_before_the_next_city_is_priced(conn, monkeypatch):
    """The city is the join point, and two properties depend on it.

    Paired snapshots: every channel of a city shares one run date, which only
    holds while the city loop stays sequential (Shape A). And budget ordering:
    the next city is priced against a ledger the previous city has finished
    writing to, rather than one still being written.
    """
    from streetscape_metadata_tracker import scheduler as sched

    for name in ("Alpha", "Beta"):
        _register(conn, name, width=1000, height=1000, step=20)
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    log = _stub_lane_collection(sched, monkeypatch)
    _stub_tail(monkeypatch, sched, conn, [])
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)

    sched.cmd_run_due(
        _street_cfg(max_concurrent_channels=3, publish_enabled=False), today=date(2026, 7, 2)
    )

    order = log.cities()
    assert len(order) == 2, "both cities should have been collected"
    first, second = order
    boundary = max(seq for _k, city_id, _p, seq in log.events if city_id == first)
    assert all(seq > boundary for _k, city_id, _p, seq in log.events if city_id == second), (
        "no channel of the second city may start before the first city is quiet"
    )


def test_knob_1_is_the_default_and_runs_the_channels_inline_and_in_order(
    conn, monkeypatch, tmp_path
):
    """The default must be the pre-#240 behaviour, to the thread.

    Not "a pool of size 1": inline on the calling thread. That is what makes the
    default byte-equivalent, and it is what lets anything substituting
    _run_one_city keep using the catalog handle — every existing test in this
    file depends on both.
    """
    from streetscape_metadata_tracker import scheduler as sched

    assert SchedulerConfig().max_concurrent_channels == 1
    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text("[schedule]\ncycle_days = 90\n")
    assert load_scheduler_config(str(cfg_path)).max_concurrent_channels == 1

    city = _lane_city(conn)
    threads = set()
    log = _stub_lane_collection(
        sched, monkeypatch, gate=lambda city_id, provider: threads.add(threading.get_ident())
    )

    attempted, succeeded, _skipped = _run_channels(
        sched, _street_cfg(), conn, city, ["gsv", "gsv_streets", "mapillary", "mapillary_streets"]
    )

    assert (attempted, succeeded) == (4, 4)
    assert threads == {threading.get_ident()}, "at one lane the channel body runs inline"
    assert log.peak_in_flight() == 1, "nothing overlaps at the default"
    assert [e[0] for e in log.events] == ["start", "end"] * 4
    assert log.starts() == ["gsv", "gsv_streets", "mapillary", "mapillary_streets"]


@pytest.mark.parametrize("value", ["0", "-2", "1.5", '"three"', "true"])
def test_max_concurrent_channels_rejects_a_nonsense_value_and_falls_back_to_1(
    tmp_path, value, caplog
):
    """Warn and fall back, never raise: a load-time error over one key of one
    section takes down every subcommand, including backup-status and
    restore-backup — the handles an operator needs during an incident.

    ``true`` is in the list because TOML booleans are Python ints, so a bare
    isinstance check would accept it as one lane and read as if it had been
    honoured.
    """
    import logging

    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text(f"[schedule]\nmax_concurrent_channels = {value}\n")

    with caplog.at_level(logging.WARNING):
        cfg = load_scheduler_config(str(cfg_path))

    assert cfg.max_concurrent_channels == 1
    assert "max_concurrent_channels" in caplog.text


# ── The shared census cache, at the scheduler seam (issue #290) ────────────


def _stamp_census_cache(city, provider, *, fetched_by, age_days=0):
    """Write the marker `census_cache_probe` reads. No parts: the probe is
    deliberately marker-only, so this is exactly what production hands it."""
    from streetscape_metadata_tracker.checkpointing import census_cache_path_for, frozen_bbox
    from tests.conftest import stamp_census_cache

    return stamp_census_cache(
        census_cache_path_for(provider, city.city_id, frozen_bbox(city)),
        provider,
        fetched_by=fetched_by,
        age_days=age_days,
        api_requests_total=41,
    )


def test_a_cached_census_prices_a_channel_at_zero(conn):
    """
    The gates this feeds are `est > budget` and `used + est > budget`. Without
    this the CHEAPEST channel of the night — a road walk whose census the grid
    run bought minutes earlier — is exactly the one a nearly-spent budget
    defers, and the pairing the cache exists to exploit never happens on the
    nights it helps most.
    """
    from streetscape_metadata_tracker.scheduler import _channel_estimate

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = SchedulerConfig(providers={"mapillary_streets": ProviderConfig()})

    assert _channel_estimate(cfg, city, "mapillary_streets", conn) > 0
    _stamp_census_cache(city, "mapillary", fetched_by="mapillary")
    assert _channel_estimate(cfg, city, "mapillary_streets", conn) == 0


def test_only_the_census_channels_read_the_cache(conn):
    """
    gsv and gsv_streets query per point; there is no census to share, so an
    entry for the same city must not silently price them at zero.
    """
    from streetscape_metadata_tracker.scheduler import _channel_estimate

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = SchedulerConfig(providers={p: ProviderConfig() for p in ("gsv", "gsv_streets")})
    _stamp_census_cache(city, "mapillary", fetched_by="mapillary")

    assert _channel_estimate(cfg, city, "gsv", conn) > 0
    assert _channel_estimate(cfg, city, "gsv_streets", conn) > 0


def test_both_mapillary_channels_and_kartaview_read_their_providers_entry(conn):
    """
    The channel -> provider mapping is the whole point: 'mapillary' and
    'mapillary_streets' are different ledgers reading ONE observation, and
    KartaView's grid run and the #258 walk will be the same.
    """
    from streetscape_metadata_tracker.scheduler import _channel_estimate

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = SchedulerConfig(
        providers={p: ProviderConfig() for p in ("mapillary", "mapillary_streets", "kartaview")}
    )

    _stamp_census_cache(city, "mapillary", fetched_by="mapillary_streets")
    assert _channel_estimate(cfg, city, "mapillary", conn) == 0
    assert _channel_estimate(cfg, city, "mapillary_streets", conn) == 0
    assert _channel_estimate(cfg, city, "kartaview", conn) > 0, "different provider, own entry"

    _stamp_census_cache(city, "kartaview", fetched_by="kartaview")
    assert _channel_estimate(cfg, city, "kartaview", conn) == 0


def test_an_expired_entry_prices_the_channel_at_full_cost(conn):
    """The window is what keeps a fortnight-old census out of a snapshot dated
    today, so it has to be priced as the fetch it will actually become."""
    from streetscape_metadata_tracker.scheduler import _channel_estimate

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = SchedulerConfig(providers={"mapillary_streets": ProviderConfig()})

    _stamp_census_cache(city, "mapillary", fetched_by="mapillary", age_days=9)
    assert _channel_estimate(cfg, city, "mapillary_streets", conn) > 0


def test_an_entry_that_would_expire_during_the_batch_is_priced_as_a_fetch(conn):
    """
    The probe runs at slate time and the child loads the entry up to
    max_batch_hours later. An entry the loader would refuse by then must not be
    priced at zero: the child would fetch at full cost with the budget gate
    already passed and no in-child request cap.
    """
    from streetscape_metadata_tracker.scheduler import _channel_estimate

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    # 6.8 days old: inside the 7-day consumer window, but not by a 10-hour batch.
    _stamp_census_cache(city, "mapillary", fetched_by="mapillary", age_days=6.8)

    loose = SchedulerConfig(providers={"mapillary_streets": ProviderConfig()}, max_batch_hours=1)
    assert _channel_estimate(loose, city, "mapillary_streets", conn) == 0
    tight = SchedulerConfig(providers={"mapillary_streets": ProviderConfig()}, max_batch_hours=10)
    assert _channel_estimate(tight, city, "mapillary_streets", conn) > 0


def test_a_cached_census_does_not_collapse_the_child_timeout(conn):
    """
    `estimate_requests` stays cache-BLIND on purpose, and this is why: it is
    also the input to the timeout derivations, and a 0 there would time a child
    against the flat floor. A reuse is fast, but a cache entry the collector
    then rejects means the child fetches for real — and a SIGKILL costs the
    requests already spent AND a consecutive_failure.
    """
    from streetscape_metadata_tracker.scheduler import city_timeout_seconds

    cid = _register(conn, "Metropolis", width=40000, height=40000, step=20)
    city = db.resolve_city(conn, cid)
    cfg = SchedulerConfig(
        city_timeout_minutes=1, providers={"mapillary": ProviderConfig(max_requests_per_minute=60)}
    )
    before = city_timeout_seconds(cfg, city, "mapillary", conn=conn)
    _stamp_census_cache(city, "mapillary", fetched_by="mapillary_streets")

    assert city_timeout_seconds(cfg, city, "mapillary", conn=conn) == before
    assert before > cfg.city_timeout_minutes * 60, "the derivation, not the floor"


def test_the_dry_run_names_a_cached_census(conn, monkeypatch, tmp_path, capsys):
    """
    A dry run has to price what the night will ACTUALLY spend. Reading the raw
    estimate would show an over-budget deferral for a channel the real run
    launches for nothing.
    """
    from streetscape_metadata_tracker import scheduler as sched

    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    _stamp_census_cache(city, "mapillary", fetched_by="mapillary")
    cfg = SchedulerConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "x.db"),
        backup_dir=str(tmp_path / "backups"),
        publish_enabled=False,
        providers={
            "mapillary": ProviderConfig(enabled=True),
            "mapillary_streets": ProviderConfig(enabled=True),
        },
    )

    assert sched.cmd_run_due(cfg, dry_run=True, today=date(2026, 7, 2)) == 0
    out = capsys.readouterr().out
    assert "cached census from mapillary" in out
    assert re.search(r"mapillary_streets\s+~\s*0 req", out), out


def test_the_tail_prunes_the_cache_and_says_how_many(conn, monkeypatch, tmp_path, caplog):
    """
    The cache is bounded by this and by nothing else: an entry is written for
    every census the night fetched and is not overwritten until that city comes
    round again, ~80 days later. In the tail beside the backup and the publish,
    where #167's rule is that no housekeeping step may cost the night's
    visibility.
    """
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=5000, height=5000, step=20)
    city = db.resolve_city(conn, cid)
    fresh = _stamp_census_cache(city, "mapillary", fetched_by="mapillary")
    stale = _stamp_census_cache(city, "kartaview", fetched_by="kartaview", age_days=9)
    cfg = SchedulerConfig(
        data_dir=str(tmp_path), backup_dir=str(tmp_path / "backups"), publish_enabled=False
    )

    with caplog.at_level("INFO"):
        sched._finish_batch(cfg, conn, "summary", succeeded=1, attempted=1, today=date(2026, 7, 2))

    assert os.path.isdir(fresh)
    assert not os.path.exists(stale)
    assert "Pruned 1 expired cached census" in caplog.text


def test_the_tail_survives_a_broken_cache_directory(conn, monkeypatch, tmp_path):
    """
    #167's posture: the prune is housekeeping, and the publish and the alert
    come after it. A filesystem it cannot read must cost a log line, never the
    night's visibility.
    """
    from streetscape_metadata_tracker import scheduler as sched

    def refuse(*a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr(sched.os, "listdir", refuse)
    cfg = SchedulerConfig(
        data_dir=str(tmp_path), backup_dir=str(tmp_path / "backups"), publish_enabled=False
    )
    assert sched._finish_batch(
        cfg, conn, "summary", succeeded=1, attempted=1, today=date(2026, 7, 2)
    ) in (0, 1)
