"""Scheduler logic tests — pure logic only, no network or subprocesses."""

import dataclasses
import gzip
import json
import os
import re
import signal
import time
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


def test_makelab1_production_config_is_wired():
    # Guard the checked-in production config the systemd unit points at.
    #
    # This file is what prod actually reads (config/scheduler.toml is the
    # annotated repo default and is NOT deployed), so enabling a channel in
    # scheduler.toml alone changes nothing in production — the two must be kept
    # in step deliberately, which is what this assertion is for.
    cfg = load_scheduler_config(os.path.join(_PROJECT_ROOT, "config", "scheduler.makelab1.toml"))
    assert cfg.enabled_providers() == ["gsv", "gsv_streets", "mapillary", "mapillary_streets"]
    # The street channels must keep their ISOLATED budgets: metered under their
    # own api_usage provider strings against separate keys, so a road crawl can
    # never eat the grid collectors' quota.
    assert cfg.providers["gsv_streets"].daily_request_budget == 3_000_000
    # Paced by the streets key's own quota, not [download]'s 48k grid pacing.
    assert cfg.providers["gsv_streets"].max_requests_per_minute == 24_000
    # Mapillary's 50k/day application cap is shared with the grid channel.
    mly = cfg.providers["mapillary"].daily_request_budget
    mly_streets = cfg.providers["mapillary_streets"].daily_request_budget
    assert mly + mly_streets <= 50_000, "combined Mapillary budgets exceed the daily app cap"
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
        conn, today=date(2026, 7, 2), cycle_days=90, grace_days=7, max_consecutive_failures=5
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
        conn, today=date(2026, 7, 2), cycle_days=90, grace_days=7, max_consecutive_failures=5
    )
    assert due == []


def test_failure_cap_excludes_city(conn):
    cid = _register(conn, "Alpha")
    db.assign_schedule(conn, 90)
    for _ in range(5):
        db.record_attempt(conn, cid, success=False, error="x")
    due = db.get_due_cities(
        conn, today=date(2026, 7, 2), cycle_days=90, grace_days=7, max_consecutive_failures=5
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

    def fake_run(
        cfg,
        city,
        run_today,
        provider="gsv",
        connection_limit=None,
        daily_budget=0,
        conn=None,
        remaining_s=None,
    ):
        # Simulate the real pipeline's ledger write for the requests spent
        db.add_api_usage(conn, run_today, sched.estimate_requests(city, provider), provider)
        ran.append(city.city_id)
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    cfg = SchedulerConfig(daily_request_budget=4_000, publish_enabled=False)
    rc = sched.cmd_run_due(cfg, today=today)

    assert len(ran) == 1  # exactly one city fit the budget
    assert rc == 0  # a budget deferral is not a failure
    assert db.get_api_usage(conn, today, "gsv") == 2601  # B never spent requests
    # The deferred city is untouched: still due tomorrow, no failure recorded
    deferred = b if ran == [a] else a
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
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
            ran.append(city.city_id) or True
        ),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    cfg = SchedulerConfig(daily_request_budget=10_000, publish_enabled=False)
    rc = sched.cmd_run_due(cfg, today=date(2026, 7, 2))

    assert huge not in ran  # skipped: never fits any budget
    assert small in ran  # not starved by the huge city ahead of it
    assert rc == 0


def test_run_due_pairs_providers_per_city(conn, monkeypatch):
    """A city due for both providers runs both back-to-back with the same
    run date, each within its own budget ledger and failure tracking."""
    from streetscape_metadata_tracker import scheduler as sched
    from streetscape_metadata_tracker.scheduler import ProviderConfig

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)

    ran = []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
            ran.append((city.city_id, provider)) or (provider == "gsv")
        ),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

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
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
            ran.append((city.city_id, provider)) or True
        ),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})

    cfg = SchedulerConfig(
        publish_enabled=False,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000),
            "mapillary": ProviderConfig(daily_request_budget=1_000),
        },
    )
    sched.cmd_run_due(cfg, today=date(2026, 7, 2))

    assert ran == [(cid, "mapillary")]  # gsv deferred, mapillary still ran


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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
            False
        ),
    )
    sched.cmd_run_due(SchedulerConfig(publish_enabled=False), today=date(2026, 7, 2))
    assert calls == {"agg": 0, "manifest": 0}

    # A night with a success: both, exactly once each.
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
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
    """A city's channels run back-to-back inside one night's budgets, so the
    series that can actually exhaust a budget must claim it first."""
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
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
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary: published.append("publish") or 0)


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
    # Each city "takes" an hour of the batch's wall clock.
    clock = iter(range(0, 100_000, 3600))
    monkeypatch.setattr(sched.time, "monotonic", lambda: next(clock))

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        ran.append(city.city_id)
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
    request checked between cities."""
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


def test_driving_plan_failure_never_fails_the_night(conn, monkeypatch):
    """The feed is an undocumented asset with no uptime contract; it breaking
    must not cost a night of collection (the issue #167 posture)."""
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

    rc = sched.cmd_run_due(_publishing_cfg(), today=date(2026, 7, 2))

    assert len(ran) == 1, "the city loop must still run"
    assert published == ["aggregate", "manifest", "publish"]
    assert rc == 0, "a plan-fetch failure alone is not an unhealthy night"


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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
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
        provider="mapillary",
    )
    assert cid in {c.city_id for c in due}


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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None, remaining_s=None: (
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
