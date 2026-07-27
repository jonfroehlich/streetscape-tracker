"""Scheduler logic tests — pure logic only, no network or subprocesses."""

import os
from datetime import date

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.scheduler import (
    ResourceGuardConfig,
    SchedulerConfig,
    SystemPressure,
    _reconcile_orphaned_run,
    build_parser,
    estimate_requests,
    load_scheduler_config,
    plan_connection_limit,
)
from tests.conftest import make_city_df, write_city_csv_gz

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def test_run_one_city_command_defers_skip_policy_to_scheduler(conn, monkeypatch):
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

    def fake_run(cmd, timeout=None, cwd=None):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    assert sched._run_one_city(SchedulerConfig(), city, date(2026, 7, 1), "gsv")

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
    # Mapillary is fast bulk metadata — keep the flat floor regardless of grid.
    assert city_timeout_seconds(SchedulerConfig(), big, "mapillary") == floor
    # No client-side pacing -> no basis to scale, keep the floor.
    assert city_timeout_seconds(SchedulerConfig(max_requests_per_minute=0), big, "gsv") == floor


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
    assert cfg.providers["gsv_streets"].daily_request_budget == 2_000_000
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


def test_run_one_city_honors_connection_limit_override(conn, monkeypatch):
    """The resource guard lowers concurrency by passing a connection_limit
    override, which must reach the subprocess as --connection-limit."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend")
    city = db.resolve_city(conn, cid)
    captured = {}

    def fake_run(cmd, timeout=None, cwd=None):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    assert sched._run_one_city(SchedulerConfig(), city, date(2026, 7, 1), "gsv", connection_limit=7)
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
        cfg, city, run_today, provider="gsv", connection_limit=None, daily_budget=0, conn=None
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None: (
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None: (
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None: (
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None: (
            False
        ),
    )
    sched.cmd_run_due(SchedulerConfig(publish_enabled=False), today=date(2026, 7, 2))
    assert calls == {"agg": 0, "manifest": 0}

    # A night with a success: both, exactly once each.
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None: (
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


def test_street_channel_dispatches_to_the_road_walk_collector(conn, monkeypatch):
    """The scheduler must run the road-walk CLI for a street channel, with the
    imagery provider, the isolated daily budget, and the catalog path."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    captured = {}

    def fake_run(cmd, timeout=None, cwd=None):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    cfg = _street_cfg(db_path="/tmp/x.db", data_dir="/tmp/data")
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


def test_mapillary_street_dispatch_omits_per_minute_pacing(conn, monkeypatch):
    """Pacing is meaningless for a tile census; passing it would imply the
    collector meters per-request like the GSV arm."""
    from streetscape_metadata_tracker import scheduler as sched

    cid = _register(conn, "Bend", width=1000, height=1000, step=20)
    city = db.resolve_city(conn, cid)
    captured = {}

    def fake_run(cmd, timeout=None, cwd=None):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sched.subprocess, "run", fake_run)
    assert sched._run_one_city(
        _street_cfg(), city, date(2026, 7, 1), "mapillary_streets", conn=conn
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None: (
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


def test_street_channel_failure_is_not_reconciled_as_an_orphan_run(conn, monkeypatch, data_dir):
    """Street channels write street_walks rows, not `runs` rows, so the
    orphaned-run salvage path must not be consulted for them (it would look up
    a run that can never exist and could mask a real failure)."""
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
        lambda cfg, city, today, provider="gsv", connection_limit=None, daily_budget=0, conn=None: (
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
