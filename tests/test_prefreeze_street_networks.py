"""Tests for scripts/prefreeze_street_networks.py (issue #341).

The post-batch pass that freezes the next night's walk networks so a mid-night
Overpass refusal has nothing left to strand. Pinned: dry-run by default, the
slate is the night's own (`_collect_due`, not the raw due list), only COLD
networks are fetched and keyed on each channel's network_type, fetches are
serial and paced, a host condition stops the pass with that host's exit code
while a city-specific failure does not, a run-due in flight refuses it, the
first fetch waits on a fail-CLOSED Overpass probe (#389), and no fetch starts
past the cutoff that keeps a late pass clear of the next 02:00 fire (#389, F2).

No network: `fetch_graph` is replaced by a recorder that writes the GraphML
path it would have frozen, and `overpass_serving` answers True unless a test
says otherwise.
"""

import contextlib
import os
import sys
from datetime import UTC, date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from scripts import prefreeze_street_networks as pf  # noqa: E402
from streetscape_metadata_tracker import clock, db  # noqa: E402
from streetscape_metadata_tracker.download_common import (  # noqa: E402
    HOST_OVERPASS,
    DownloadError,
    HostBlockedError,
    HostBusyError,
)
from streetscape_metadata_tracker.naming import network_cache_path  # noqa: E402
from streetscape_metadata_tracker.scheduler import (  # noqa: E402
    USAGE_EXIT_CODE,
    ProviderConfig,
    SchedulerConfig,
)

TODAY = date(2026, 9, 16)
# 14:45 PDT on TODAY: where the chain lands after a 12 h night, and far from the
# cutoff, so a pass under test is never cut short by the hour the suite runs at.
DAYTIME_UTC = datetime(2026, 9, 16, 21, 45, tzinfo=UTC)


def _register(conn, name):
    return db.register_city(
        conn,
        city_name=name,
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


def _cfg(data_dir, **overrides):
    base = dict(
        providers={
            "gsv": ProviderConfig(enabled=True, daily_request_budget=10_000_000),
            "gsv_streets": ProviderConfig(enabled=True, daily_request_budget=2_000_000),
            "mapillary": ProviderConfig(enabled=True, daily_request_budget=40_000),
            "mapillary_streets": ProviderConfig(enabled=True, daily_request_budget=5_000),
        },
        data_dir=data_dir,
        db_path=db.get_default_db_path(data_dir),
        max_cities_per_day=2,
    )
    base.update(overrides)
    return SchedulerConfig(**base)


@pytest.fixture(autouse=True)
def _overpass_serving(monkeypatch):
    """The fail-closed probe (#389) answers "serving" unless a test says not.

    Left real it could never answer True here -- the suite blocks DNS, and a
    fail-closed probe reads that as "not serving" -- so every --execute test
    would stop before its first fetch. Returns the list of calls."""
    calls = []

    def serving(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(pf, "overpass_serving", serving)
    return calls


@pytest.fixture(autouse=True)
def _daytime_clock(monkeypatch):
    """Freeze the clock at DAYTIME_UTC: `main` computes its fetch cutoff from the
    real clock (#389, F2), so without this every --execute test would fetch
    nothing when the suite happens to run between 00:45 and 02:16 Pacific."""
    monkeypatch.setattr(clock, "_utc_clock", lambda: DAYTIME_UTC)


@pytest.fixture
def three_cities(conn):
    """Alpha, Beta, Gamma: all enabled, never collected, so all due and in that order."""
    return [_register(conn, n) for n in ("Alpha", "Beta", "Gamma")]


class _Fetcher:
    """Stands in for fetch_graph: records calls, freezes the file, can fail."""

    def __init__(self, failures=None):
        self.calls = []
        self.retry_policies = []
        self.failures = dict(failures or {})

    def __call__(self, city, data_dir, *, network_type, conn, overpass_retry):
        self.calls.append((city.city_id, network_type))
        self.retry_policies.append(overpass_retry)
        error = self.failures.pop(city.city_id, None)
        if error is not None:
            raise error
        path = network_cache_path(city.city_id, data_dir, network_type)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return object()


def _run(monkeypatch, cfg, *args, fetcher=None, in_flight=None):
    fetcher = fetcher or _Fetcher()
    slept = []
    monkeypatch.setattr(pf, "load_scheduler_config", lambda path: cfg)
    monkeypatch.setattr(pf, "fetch_graph", fetcher)
    monkeypatch.setattr(
        pf, "_run_due_in_flight", in_flight if callable(in_flight) else (lambda: in_flight)
    )
    monkeypatch.setattr(pf.time, "sleep", lambda s: slept.append(s))
    args = [str(a) for a in args]
    # --all-enabled refuses --date (#381), so it gets none: passing one would
    # make every all-enabled test exit 64 for the wrong reason, and a test
    # asserting 64 (the in-flight refusal, --nights) would pass vacuously.
    date_args = [] if "--all-enabled" in args else ["--date", TODAY.isoformat()]
    rc = pf.main([*date_args, *args])
    return rc, fetcher, slept


def test_dry_run_is_the_default_and_fetches_nothing(three_cities, data_dir, monkeypatch, capsys):
    rc, fetcher, slept = _run(monkeypatch, _cfg(data_dir))
    assert rc == 0
    assert fetcher.calls == []
    assert slept == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    # The night's cap is 2 cities, so the third is outside tonight's window.
    assert three_cities[0] in out and three_cities[1] in out
    assert three_cities[2] not in out
    assert "Would freeze 2 cold street network(s)" in out


def test_execute_fetches_only_the_cold_networks_serially_and_paced(
    three_cities, data_dir, monkeypatch
):
    alpha, beta, gamma = three_cities
    # Alpha is already frozen: nothing to do for it.
    path = network_cache_path(alpha, data_dir, "drive")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()

    rc, fetcher, slept = _run(monkeypatch, _cfg(data_dir), "--execute", "--pause-s", "7")
    assert rc == 0
    assert fetcher.calls == [(beta, "drive")]
    assert slept == [], "one fetch, no pause"

    # And with nothing frozen: both cities of the cap, in slate order, one
    # pause BETWEEN them (never a trailing one).
    os.remove(path)
    os.remove(network_cache_path(beta, data_dir, "drive"))
    rc, fetcher, slept = _run(monkeypatch, _cfg(data_dir), "--execute", "--pause-s", "7")
    assert rc == 0
    assert fetcher.calls == [(alpha, "drive"), (beta, "drive")]
    assert slept == [7.0]
    assert os.path.exists(network_cache_path(beta, data_dir, "drive"))


def test_nights_widens_the_window_and_limit_caps_the_pass(three_cities, data_dir, monkeypatch):
    alpha, beta, gamma = three_cities
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute", "--nights", "2")
    assert rc == 0
    assert fetcher.calls == [(alpha, "drive"), (beta, "drive"), (gamma, "drive")]

    for cid in three_cities:
        os.remove(network_cache_path(cid, data_dir, "drive"))
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute", "--nights", "2", "--limit", "1")
    assert rc == 0
    assert fetcher.calls == [(alpha, "drive")]


def test_the_pass_fetches_with_the_configured_overpass_retry_policy(
    three_cities, data_dir, monkeypatch
):
    """The pre-freeze is the same talker to the same host as a nightly walk,
    earlier, so it must run the same [overpass] window (issue #357) -- pinned at
    a NON-default policy, which a hardcoded default could not satisfy."""
    from streetscape_metadata_tracker.overpass_retry import OverpassRetryPolicy

    configured = OverpassRetryPolicy(max_attempts=2, initial_wait_s=45, window_s=300)
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir, overpass_retry=configured), "--execute")
    assert rc == 0
    assert len(fetcher.retry_policies) == 2
    assert all(policy == configured for policy in fetcher.retry_policies)


def test_each_channels_network_type_is_frozen_separately(three_cities, data_dir, monkeypatch):
    """Two channels walking the same type share one GraphML; a channel on a
    different type needs its own, and a frozen 'drive' says nothing about it."""
    alpha, beta, _ = three_cities
    cfg = _cfg(data_dir, max_cities_per_day=1)
    cfg.providers["mapillary_streets"] = ProviderConfig(
        enabled=True, daily_request_budget=5_000, network_type="all_public"
    )
    rc, fetcher, _ = _run(monkeypatch, cfg, "--execute")
    assert rc == 0
    assert fetcher.calls == [(alpha, "drive"), (alpha, "all_public")]

    planned = pf.plan_prefreeze(db.connect(cfg.db_path), cfg, TODAY, nights=1)
    assert planned == [], "both now frozen"


def test_a_city_not_due_tonight_is_not_fetched(three_cities, conn, data_dir, monkeypatch):
    """The slate is the night's own, not 'every city without a network'."""
    alpha, beta, gamma = three_cities
    db.assign_schedule(conn, 90)
    for provider in ("gsv", "gsv_streets", "mapillary", "mapillary_streets"):
        db.record_attempt(conn, alpha, success=True, provider=provider)
    conn.commit()

    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute")
    assert rc == 0
    assert fetcher.calls == [(beta, "drive"), (gamma, "drive")]


def test_a_host_condition_stops_the_pass_with_that_hosts_exit_code(
    three_cities, data_dir, monkeypatch
):
    alpha, beta, _ = three_cities
    blocked = HostBlockedError("Overpass refused this host", host=HOST_OVERPASS)
    rc, fetcher, _ = _run(
        monkeypatch, _cfg(data_dir), "--execute", fetcher=_Fetcher({alpha: blocked})
    )
    assert rc == 76
    assert fetcher.calls == [(alpha, "drive")], "asking a refusing host again is the retry hazard"

    busy = HostBusyError("another process holds the Overpass lock", host=HOST_OVERPASS)
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute", fetcher=_Fetcher({alpha: busy}))
    assert rc == 80
    assert fetcher.calls == [(alpha, "drive")]


def test_a_city_specific_failure_does_not_stop_the_pass(
    three_cities, data_dir, monkeypatch, capsys
):
    alpha, beta, _ = three_cities
    roadless = DownloadError("no drivable ways in this bbox")
    rc, fetcher, _ = _run(
        monkeypatch, _cfg(data_dir), "--execute", fetcher=_Fetcher({alpha: roadless})
    )
    assert rc == 0
    assert fetcher.calls == [(alpha, "drive"), (beta, "drive")]
    assert "1 city(ies) had no usable network" in capsys.readouterr().out


def test_a_run_due_in_flight_refuses_the_pass_unless_forced(three_cities, data_dir, monkeypatch):
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute", in_flight="pid 4242: run-due")
    assert rc == USAGE_EXIT_CODE
    assert fetcher.calls == []

    rc, fetcher, _ = _run(
        monkeypatch, _cfg(data_dir), "--execute", "--force", in_flight="pid 4242: run-due"
    )
    assert rc == 0
    assert len(fetcher.calls) == 2

    # A dry run never needs to ask: it touches nothing.
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), in_flight="pid 4242: run-due")
    assert rc == 0


def test_a_run_due_that_starts_mid_pass_stops_it(three_cities, data_dir, monkeypatch, capsys):
    """The guard is asked before EVERY fetch, not once: a pass is long and the
    timer does not wait for it, and the walk that then loses the Overpass lock
    exits busy and strands its city -- the failure #341 is about."""
    alpha, beta, _ = three_cities
    answers = iter([None, "pid 4242: run-due"])
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute", in_flight=lambda: next(answers))
    assert rc == USAGE_EXIT_CODE
    assert fetcher.calls == [(alpha, "drive")], "the fetch in hand finished; the next never started"
    assert "stopped early: a run-due started" in capsys.readouterr().out


def test_no_street_channel_means_nothing_to_freeze(three_cities, data_dir, monkeypatch, capsys):
    cfg = _cfg(data_dir, providers={"gsv": ProviderConfig(enabled=True)})
    rc, fetcher, _ = _run(monkeypatch, cfg, "--execute")
    assert rc == 0
    assert fetcher.calls == []
    assert "No street channel is enabled" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [("--nights", "0"), ("--limit", "0"), ("--pause-s", "-1")])
def test_bad_flags_exit_usage(three_cities, data_dir, monkeypatch, flags):
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute", *flags)
    assert rc == USAGE_EXIT_CODE
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "instant, expected",
    [
        # 17:30 PDT on 08-31 (conftest's EVENING_UTC): the case "tomorrow UTC"
        # got WRONG (09-02). The next fire is 09-01 02:00 PDT = 09:00 UTC.
        (datetime(2026, 9, 1, 0, 30, tzinfo=UTC), date(2026, 9, 1)),
        # 14:45 PDT -- where the chain lands after a 12 h night; agrees with the old formula.
        (datetime(2026, 9, 1, 21, 45, tzinfo=UTC), date(2026, 9, 2)),
        # 18:45 PDT -- where the chain lands after a 16 h night; the old formula said 09-03.
        (datetime(2026, 9, 2, 1, 45, tzinfo=UTC), date(2026, 9, 2)),
        # 01:30 PST, 30 min before the fire: tonight's fire is today's.
        (datetime(2026, 1, 15, 9, 30, tzinfo=UTC), date(2026, 1, 15)),
        # 02:30 PST, 30 min after the fire: the next one is tomorrow's.
        (datetime(2026, 1, 15, 10, 30, tzinfo=UTC), date(2026, 1, 16)),
        # 02:30 PDT, after the fire: the only hour of a summer day where a fixed
        # UTC-8 reading (01:30, before the fire) answers differently (review N1).
        (datetime(2026, 7, 15, 9, 30, tzinfo=UTC), date(2026, 7, 16)),
        # Inside the timer's slack (L1: 15 min RandomizedDelaySec + 1 min default
        # AccuracySec): at 02:05 and 02:15:30 PDT tonight's night may not have
        # started, so its fire is still the next one; at 02:17 the slack has run
        # out and the next fire is tomorrow's.
        (datetime(2026, 9, 1, 9, 5, tzinfo=UTC), date(2026, 9, 1)),
        (datetime(2026, 9, 1, 9, 15, 30, tzinfo=UTC), date(2026, 9, 1)),
        (datetime(2026, 9, 1, 9, 17, tzinfo=UTC), date(2026, 9, 2)),
        # Spring forward (2026-03-08, 02:00 PST -> 03:00 PDT). 01:59 PST, before
        # the fire; 03:00 PDT, the instant the nonexistent 02:00 resolves to, so
        # still pending; 03:17 PDT, past the slack. (zoneinfo's fold=0 reading of
        # the nonexistent 02:00; systemd's own handling is unverified.)
        (datetime(2026, 3, 8, 9, 59, tzinfo=UTC), date(2026, 3, 8)),
        (datetime(2026, 3, 8, 10, 0, tzinfo=UTC), date(2026, 3, 8)),
        (datetime(2026, 3, 8, 10, 17, tzinfo=UTC), date(2026, 3, 9)),
        # Fall back (2026-11-01, 02:00 PDT -> 01:00 PST). The SECOND 01:30 (PST),
        # still before the fire; 02:00 PST, the fire itself, pending; 02:17 PST.
        (datetime(2026, 11, 1, 9, 30, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 1, 10, 0, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 1, 10, 17, tzinfo=UTC), date(2026, 11, 2)),
    ],
    ids=[
        "17:30-PDT",
        "14:45-PDT",
        "18:45-PDT",
        "01:30-PST",
        "02:30-PST",
        "02:30-PDT",
        "02:05-PDT-in-slack",
        "02:15:30-PDT-in-slack",
        "02:17-PDT-past-slack",
        "spring-01:59-PST",
        "spring-03:00-PDT",
        "spring-03:17-PDT",
        "fall-second-01:30-PST",
        "fall-02:00-PST",
        "fall-02:17-PST",
    ],
)
def test_the_default_date_is_the_next_fires_utc_date(
    pacific_local_zone, frozen_utc_clock, instant, expected
):
    """cmd_run_due reads the UTC date when the 02:00 Pacific timer fires, so
    the pass predicts the UTC date of the NEXT such fire (#389) -- not
    "tomorrow UTC", which is a day late for a pass after midnight UTC, the
    normal case once the chained pass follows a night longer than ~14 h.

    Literals under a frozen clock (#347), never the same expression on both
    sides; the local zone is pinned to Pacific so a local-calendar read shows."""
    frozen_utc_clock(instant)
    assert pf.next_run_date() == expected
    # The explicit-argument form answers the same, so the clock is the only input.
    assert pf.next_run_date(instant) == expected


# ── The cutoff: no fetch starts that could overlap the next night (#389, F2) ──
#
# The chain starts the pass whenever a night ends, and a night that did not
# start at 02:00 (a Persistent catch-up after a reboot, a hand start) can end
# close enough to the next 02:00 that a pass would still hold the Overpass lock.


@pytest.mark.parametrize(
    "instant, expected",
    [
        # 14:45 PDT: tomorrow's 02:00 PDT (09:00 UTC) less 1 h less 900 s.
        (datetime(2026, 9, 1, 21, 45, tzinfo=UTC), datetime(2026, 9, 2, 7, 45, tzinfo=UTC)),
        # 00:30 PDT: TONIGHT's fire, so the cutoff is 15 min away.
        (datetime(2026, 9, 1, 7, 30, tzinfo=UTC), datetime(2026, 9, 1, 7, 45, tzinfo=UTC)),
        # 01:00 PDT: the cutoff (00:45) is already behind it.
        (datetime(2026, 9, 1, 8, 0, tzinfo=UTC), datetime(2026, 9, 1, 7, 45, tzinfo=UTC)),
        # 23:00 PST in winter: 02:00 PST is 10:00 UTC, so the cutoff is 08:45 UTC.
        (datetime(2026, 1, 15, 7, 0, tzinfo=UTC), datetime(2026, 1, 15, 8, 45, tzinfo=UTC)),
        # 02:05 PDT, a night ending inside the timer's slack (L1): tonight's fire
        # is still pending, so the cutoff is tonight's 00:45 -- already past.
        (datetime(2026, 9, 1, 9, 5, tzinfo=UTC), datetime(2026, 9, 1, 7, 45, tzinfo=UTC)),
        # 02:15:30 PDT: past the 15 min delay but inside the 1 min accuracy, still tonight's.
        (datetime(2026, 9, 1, 9, 15, 30, tzinfo=UTC), datetime(2026, 9, 1, 7, 45, tzinfo=UTC)),
        # 02:17 PDT, past the slack: tomorrow's.
        (datetime(2026, 9, 1, 9, 17, tzinfo=UTC), datetime(2026, 9, 2, 7, 45, tzinfo=UTC)),
        # Fall-back night: 02:00 PST is 10:00 UTC, so the cutoff is 01:45 PDT.
        (datetime(2026, 10, 31, 22, 0, tzinfo=UTC), datetime(2026, 11, 1, 8, 45, tzinfo=UTC)),
    ],
    ids=[
        "14:45-PDT",
        "00:30-PDT",
        "01:00-PDT",
        "23:00-PST",
        "02:05-PDT",
        "02:15:30-PDT",
        "02:17-PDT",
        "fall-back",
    ],
)
def test_the_fetch_cutoff_is_the_next_fire_less_the_clearance_and_a_fetch(instant, expected):
    """Literals, never the same expression on both sides: the cutoff is the next
    02:00 Pacific less NEXT_FIRE_CLEARANCE (1 h) less one worst-case fetch
    (OVERPASS_DEADLINE_S, 900 s) -- normally 00:45 Pacific -- and tonight's fire
    stays the next one until the timer's randomized delay has run out."""
    assert pf.fetch_cutoff(instant) == expected


def _clock_advanced_by_sleep(monkeypatch, start, step):
    """A clock that starts at ``start`` and moves ``step`` per ``time.sleep``
    (the inter-fetch pause), so a pass can cross its cutoff mid-pass."""
    now = [start]
    monkeypatch.setattr(clock, "_utc_clock", lambda: now[0])

    def sleep(seconds):
        now[0] += step

    return sleep


def test_a_pass_that_starts_past_the_cutoff_fetches_nothing_and_does_not_alert(
    three_cities, data_dir, monkeypatch, capsys, _overpass_serving
):
    """A pass chained from a catch-up night that ended at 01:00 Pacific: one
    fetch now could hold the Overpass lock at 02:00, where the night's first
    cold walks would exit busy and strand. So nothing is fetched, no probe is
    spent, and it is a quiet 0 -- the night fetches those networks itself."""
    monkeypatch.setattr(clock, "_utc_clock", lambda: datetime(2026, 9, 16, 8, 0, tzinfo=UTC))
    rc, fetcher, alerts = _run_alerting(monkeypatch, _cfg(data_dir), "--execute", "--alert")
    assert rc == 0
    assert fetcher.calls == []
    assert _overpass_serving == [], "a pass past its cutoff owes no probe"
    assert alerts.sent == [], "reaching the cutoff is not a failure"
    assert "Froze 0 of 2 network(s); stopped at the cutoff (2026-09-16 07:45 UTC)" in (
        capsys.readouterr().out
    )


def test_a_pass_chained_from_a_night_ending_inside_the_fire_delay_fetches_nothing(
    three_cities, data_dir, monkeypatch, _overpass_serving
):
    """A late night that ends at 02:05 PDT chains a pass while tonight's timer
    can still start the night until ~02:16 (RandomizedDelaySec + AccuracySec). Planning against
    TOMORROW's fire would fetch straight into that night (#389 final review,
    L1); tonight's fire is still pending, so the pass is past its cutoff."""
    monkeypatch.setattr(clock, "_utc_clock", lambda: datetime(2026, 9, 16, 9, 5, tzinfo=UTC))
    rc, fetcher, alerts = _run_alerting(monkeypatch, _cfg(data_dir), "--execute", "--alert")
    assert rc == 0
    assert fetcher.calls == []
    assert _overpass_serving == []
    assert alerts.sent == []


def test_a_pass_that_crosses_the_cutoff_stops_before_the_next_fetch(
    three_cities, data_dir, monkeypatch
):
    """Started at 00:40 PDT, five minutes before the cutoff; the 120 s pause is
    made to advance the clock 10 min, to 00:50 -- past the cutoff, still an hour
    and more before the fire. The first fetch runs; the second never starts."""
    sleep = _clock_advanced_by_sleep(
        monkeypatch, datetime(2026, 9, 16, 7, 40, tzinfo=UTC), timedelta(minutes=10)
    )
    monkeypatch.setattr(pf.time, "sleep", sleep)
    fetcher = _Fetcher()
    monkeypatch.setattr(pf, "load_scheduler_config", lambda path: _cfg(data_dir))
    monkeypatch.setattr(pf, "fetch_graph", fetcher)
    monkeypatch.setattr(pf, "_run_due_in_flight", lambda: None)
    rc = pf.main(["--date", TODAY.isoformat(), "--execute"])
    assert rc == 0
    assert fetcher.calls == [(three_cities[0], "drive")]


def test_the_cutoff_is_fixed_at_the_passs_start_not_reread_per_fetch(
    three_cities, data_dir, monkeypatch
):
    """Started at 00:40 PDT, the pause jumps the clock to 02:30 PDT -- past the
    fire. Re-reading the cutoff then would find TOMORROW's 02:00 and fetch on,
    beside the night that just started; held from the start, it stops."""
    sleep = _clock_advanced_by_sleep(
        monkeypatch, datetime(2026, 9, 16, 7, 40, tzinfo=UTC), timedelta(hours=1, minutes=50)
    )
    monkeypatch.setattr(pf.time, "sleep", sleep)
    fetcher = _Fetcher()
    monkeypatch.setattr(pf, "load_scheduler_config", lambda path: _cfg(data_dir))
    monkeypatch.setattr(pf, "fetch_graph", fetcher)
    monkeypatch.setattr(pf, "_run_due_in_flight", lambda: None)
    rc = pf.main(["--date", TODAY.isoformat(), "--execute"])
    assert rc == 0
    assert fetcher.calls == [(three_cities[0], "drive")]


# ── The fail-closed Overpass probe before the first fetch (issue #389) ────────
#
# The chained pass starts minutes after a night that may have ended with
# Overpass refusing this IP, and the night's breaker died with its process.


def test_a_negative_probe_stops_the_pass_before_any_fetch(
    three_cities, data_dir, monkeypatch, _overpass_serving
):
    """Fail-CLOSED: anything but a positive answer is exit 76 and NO fetch --
    the walk's own /status pre-flight is fail-open, so without this a ban that
    presents as a refused connection costs the first fetch a whole retry window."""
    asked = []
    monkeypatch.setattr(pf, "overpass_serving", lambda *a, **k: asked.append(1) or False)
    rc, fetcher, alerts = _run_alerting(monkeypatch, _cfg(data_dir), "--execute", "--alert")
    assert rc == 76
    assert fetcher.calls == [], "a fetch after a negative probe is the retry hazard"
    assert asked == [1], "asked exactly once: a refusing host is not asked again"
    [(subject, body)] = alerts.sent
    assert "overpass REFUSED" in subject
    assert "2 planned network(s) still cold" in body


def test_the_probe_is_asked_once_per_pass_inside_the_overpass_lock(
    three_cities, data_dir, monkeypatch, _overpass_serving
):
    """Once, not per fetch (it is a metered query, and every later fetch has
    the walk's own pre-flight); and inside host_lock(HOST_OVERPASS), so it is
    never a second concurrent talker beside a local walk."""
    held = []

    @contextlib.contextmanager
    def recording_lock(host):
        held.append(host)
        try:
            yield
        finally:
            held.remove(host)

    def serving(*args, **kwargs):
        assert held == [HOST_OVERPASS], "the probe ran outside the Overpass host lock"
        _overpass_serving.append((args, kwargs))
        return True

    monkeypatch.setattr(pf, "host_lock", recording_lock)
    monkeypatch.setattr(pf, "overpass_serving", serving)
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute")
    assert rc == 0
    assert len(fetcher.calls) == 2
    assert len(_overpass_serving) == 1


def test_a_busy_overpass_lock_stops_the_probe_as_busy(
    three_cities, data_dir, monkeypatch, _overpass_serving
):
    """A local walk holding the lock is exit 80, and the probe is never sent."""

    @contextlib.contextmanager
    def busy_lock(host):
        raise HostBusyError("another process holds the Overpass lock", host=host)
        yield  # pragma: no cover

    monkeypatch.setattr(pf, "host_lock", busy_lock)
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute")
    assert rc == 80
    assert fetcher.calls == []
    assert _overpass_serving == []


def test_the_probe_is_not_spent_when_nothing_will_be_fetched(
    three_cities, data_dir, monkeypatch, _overpass_serving
):
    """A dry run, a run-due in flight, and an all-frozen window send nothing:
    the probe is a metered query, owed only by a pass about to fetch."""
    rc, _, _ = _run(monkeypatch, _cfg(data_dir))
    assert rc == 0
    rc, _, _ = _run(monkeypatch, _cfg(data_dir), "--execute", in_flight="pid 4242: run-due")
    assert rc == USAGE_EXIT_CODE
    assert _overpass_serving == []

    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute")
    assert rc == 0 and len(fetcher.calls) == 2
    _overpass_serving.clear()
    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--execute")
    assert rc == 0 and fetcher.calls == []
    assert _overpass_serving == [], "nothing cold, so nothing to ask"


# ── --alert: a pass that does not finish is never silent (issue #355) ─────────
#
# The timer runs this with nobody watching, and a pass that dies quietly leaves
# tonight's networks cold -- the exposure the timer exists to remove. So every
# way a pass can end early mails, and a pass that finishes does not.


class _Alerts:
    def __init__(self):
        self.sent = []
        self.configs = []

    def __call__(self, alert_cfg, subject, body):
        # The config is recorded, not ignored: `send_alert(None, ...)` at the
        # call site is a silently disabled alert in production, and a fake that
        # drops its first argument cannot tell the difference.
        self.configs.append(alert_cfg)
        self.sent.append((subject, body))
        return True


def _run_alerting(monkeypatch, cfg, *args, **kwargs):
    alerts = _Alerts()
    monkeypatch.setattr(pf, "send_alert", alerts)
    rc, fetcher, slept = _run(monkeypatch, cfg, *args, **kwargs)
    return rc, fetcher, alerts


def test_a_host_refusal_alerts_with_the_networks_it_left_cold(three_cities, data_dir, monkeypatch):
    alpha, beta, _ = three_cities
    cfg = _cfg(data_dir)
    blocked = HostBlockedError("Overpass refused this host", host=HOST_OVERPASS)
    rc, _, alerts = _run_alerting(
        monkeypatch, cfg, "--execute", "--alert", fetcher=_Fetcher({alpha: blocked})
    )
    assert rc == 76, "--alert must not change the exit status: the unit still goes red"
    assert len(alerts.sent) == 1
    subject, body = alerts.sent[0]
    assert "overpass REFUSED this host (exit 76)" in subject
    # The body names what is still exposed tonight -- both cities, since the
    # refusal came on the first fetch -- not merely that something went wrong.
    assert "2 planned network(s) still cold" in body
    assert f"  {alpha} drive" in body and f"  {beta} drive" in body
    # And carries the pass's own printed account.
    assert "Freezing 2 cold street network(s)" in body
    # The alert goes out under THIS run's [alerts] config. Passing anything else
    # -- None, a default AlertConfig -- disables the mail in production while
    # every assertion above still passes.
    assert alerts.configs == [cfg.alerts] and alerts.configs[0] is cfg.alerts


def test_without_alert_a_host_refusal_sends_nothing(three_cities, data_dir, monkeypatch):
    alpha, _, _ = three_cities
    blocked = HostBlockedError("Overpass refused this host", host=HOST_OVERPASS)
    rc, _, alerts = _run_alerting(
        monkeypatch, _cfg(data_dir), "--execute", fetcher=_Fetcher({alpha: blocked})
    )
    assert rc == 76
    assert alerts.sent == [], "a hand-run pass reports on the terminal, not by email"


def test_a_busy_lock_and_a_run_due_in_flight_alert_with_their_own_reasons(
    three_cities, data_dir, monkeypatch
):
    alpha, beta, _ = three_cities
    busy = HostBusyError("another process holds the Overpass lock", host=HOST_OVERPASS)
    rc, _, alerts = _run_alerting(
        monkeypatch, _cfg(data_dir), "--execute", "--alert", fetcher=_Fetcher({alpha: busy})
    )
    assert rc == 80
    assert [s for s, _ in alerts.sent] == [
        f"street-network prefreeze STOPPED: another local process holds the overpass lock "
        f"(exit 80) on {pf.socket.gethostname()}"
    ]

    # A run-due appearing after the first fetch: only the SECOND network is cold.
    answers = iter([None, "pid 4242: run-due"])
    rc, _, alerts = _run_alerting(
        monkeypatch,
        _cfg(data_dir),
        "--execute",
        "--alert",
        in_flight=lambda: next(answers),
    )
    assert rc == USAGE_EXIT_CODE
    assert len(alerts.sent) == 1
    subject, body = alerts.sent[0]
    assert "STOPPED: a run-due is in flight" in subject
    assert "1 planned network(s) still cold" in body
    assert f"  {beta} drive" in body and f"  {alpha} drive" not in body


def test_a_pass_that_finishes_sends_nothing(three_cities, data_dir, monkeypatch):
    """Including the steady state, where nothing is cold: a no-op, never a daily mail."""
    rc, fetcher, alerts = _run_alerting(monkeypatch, _cfg(data_dir), "--execute", "--alert")
    assert rc == 0 and len(fetcher.calls) == 2
    assert alerts.sent == []

    # Now everything in the window is frozen: nothing to fetch, nothing to say.
    rc, fetcher, alerts = _run_alerting(monkeypatch, _cfg(data_dir), "--execute", "--alert")
    assert rc == 0 and fetcher.calls == []
    assert alerts.sent == []

    # A city-specific failure is not a failed pass either (the night would fail
    # that city the same way; nothing about Overpass is exposed).
    for cid in three_cities:
        path = network_cache_path(cid, data_dir, "drive")
        if os.path.exists(path):
            os.remove(path)
    roadless = DownloadError("no drivable ways in this bbox")
    rc, _, alerts = _run_alerting(
        monkeypatch,
        _cfg(data_dir),
        "--execute",
        "--alert",
        fetcher=_Fetcher({three_cities[0]: roadless}),
    )
    assert rc == 0
    assert alerts.sent == []


def test_a_crash_alerts_with_the_traceback_and_still_raises(three_cities, data_dir, monkeypatch):
    alpha, _, _ = three_cities
    boom = RuntimeError("osmnx changed a signature")
    alerts = _Alerts()
    monkeypatch.setattr(pf, "send_alert", alerts)
    with pytest.raises(RuntimeError, match="osmnx changed a signature"):
        _run(monkeypatch, _cfg(data_dir), "--execute", "--alert", fetcher=_Fetcher({alpha: boom}))
    assert len(alerts.sent) == 1
    subject, body = alerts.sent[0]
    assert "CRASHED" in subject
    assert "RuntimeError: osmnx changed a signature" in body
    # A traceback does not answer "is tonight exposed?", so the crash alert names
    # the still-cold networks too, exactly as the host-stop and SIGTERM ones do.
    beta = three_cities[1]
    assert "2 planned network(s) still cold" in body
    assert f"  {alpha} drive" in body and f"  {beta} drive" in body

    # Without --alert the crash is just a crash.
    alerts.sent.clear()
    with pytest.raises(RuntimeError):
        _run(monkeypatch, _cfg(data_dir), "--execute", fetcher=_Fetcher({alpha: boom}))
    assert alerts.sent == []


class _SigtermFetcher(_Fetcher):
    """Delivers SIGTERM on the first fetch by calling the script's handler if
    it is the one installed -- the way systemd's TimeoutStartSec would, without
    risking the default disposition (or some other handler) acting on pytest
    itself when it is not."""

    def __init__(self):
        super().__init__()
        self.handlers = []

    def __call__(self, city, data_dir, *, network_type, conn, overpass_retry):
        # Records the call itself, because when the handler IS installed it
        # raises here and the base's own recording is never reached.
        self.calls.append((city.city_id, network_type))
        handler = pf.signal.getsignal(pf.signal.SIGTERM)
        self.handlers.append(handler)
        if handler is pf._raise_terminated:
            handler(pf.signal.SIGTERM, None)
        # `overpass_retry` is forwarded rather than dropped: the pass hands
        # `fetch_graph` the [overpass] retry policy (issue #357), so a stub
        # that does not take it fails with a TypeError that looks like a
        # signal-handling bug.
        return super().__call__(
            city,
            data_dir,
            network_type=network_type,
            conn=conn,
            overpass_retry=overpass_retry,
        )


def test_a_sigterm_mid_pass_alerts_and_restores_the_previous_handler(
    three_cities, data_dir, monkeypatch
):
    """TimeoutStartSec ends a slow pass with SIGTERM; under --alert that must
    mail rather than vanish, and must not leave the handler installed."""
    alpha, beta, _ = three_cities
    before = pf.signal.getsignal(pf.signal.SIGTERM)
    fetcher = _SigtermFetcher()
    rc, _, alerts = _run_alerting(
        monkeypatch, _cfg(data_dir), "--execute", "--alert", fetcher=fetcher
    )
    assert rc == pf.TERMINATED_EXIT_CODE == 143
    assert fetcher.calls == [(alpha, "drive")], "nothing is fetched after the signal"
    assert len(alerts.sent) == 1
    subject, body = alerts.sent[0]
    assert "KILLED by SIGTERM" in subject
    assert "2 planned network(s) still cold" in body and f"  {beta} drive" in body
    assert pf.signal.getsignal(pf.signal.SIGTERM) == before

    # Without --alert nothing is installed: a SIGTERM keeps its ordinary meaning.
    fetcher = _SigtermFetcher()
    rc, _, alerts = _run_alerting(monkeypatch, _cfg(data_dir), "--execute", fetcher=fetcher)
    assert fetcher.handlers[0] == before
    assert alerts.sent == []


def test_terminated_is_not_an_exception_a_library_can_swallow():
    """A broad `except Exception` in osmnx or tenacity must not eat the signal
    and keep fetching past the unit's timeout."""
    assert not issubclass(pf.Terminated, Exception)
    assert issubclass(pf.Terminated, BaseException)


# ── --all-enabled: drain the whole catalog's cold backlog (issue #381) ────────
#
# The slate mode only ever sees cities DUE by the target date, so a cold city
# the night will not reach is frozen only the night it is walked -- exactly
# when a refusal strands it. This mode plans every enabled member city instead.


def _freeze(data_dir, city_id, network_type="drive"):
    path = network_cache_path(city_id, data_dir, network_type)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


def test_all_enabled_selects_enabled_cold_cities_past_the_nights_window(
    three_cities, conn, data_dir, monkeypatch
):
    """Past the cap AND not due tonight, both of which the slate mode drops;
    a disabled city and an already-frozen one are skipped."""
    alpha, beta, gamma = three_cities
    delta = _register(conn, "Delta")
    epsilon = _register(conn, "Epsilon")
    db.set_city_enabled(conn, delta, False)
    _freeze(data_dir, epsilon)
    # Alpha was walked yesterday on every channel, so no night will reach it
    # for ~83 days -- but its network is cold, which is what this mode is for.
    db.assign_schedule(conn, 90)
    for provider in ("gsv", "gsv_streets", "mapillary", "mapillary_streets"):
        db.record_attempt(conn, alpha, success=True, provider=provider)
    conn.commit()

    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir, max_cities_per_day=1), "--execute")
    assert fetcher.calls == [(beta, "drive")], "the slate mode sees one due city"

    os.remove(network_cache_path(beta, data_dir, "drive"))
    rc, fetcher, _ = _run(
        monkeypatch, _cfg(data_dir, max_cities_per_day=1), "--all-enabled", "--execute"
    )
    assert rc == 0
    # Never-walked first (city_id order), then Alpha, whose walk is the freshest.
    assert fetcher.calls == [(beta, "drive"), (gamma, "drive"), (alpha, "drive")]


def test_all_enabled_limit_truncates_in_staleness_order(three_cities, conn, data_dir, monkeypatch):
    """Stalest-first, NOT city_id order: a city walked long ago outranks one
    walked recently, and the never-walked outrank both."""
    alpha, beta, gamma = three_cities
    db.assign_schedule(conn, 90)
    for city_id, when in ((alpha, "2026-09-01T00:00:00"), (beta, "2026-03-01T00:00:00")):
        for provider in ("gsv_streets", "mapillary_streets"):
            db.record_attempt(conn, city_id, success=True, provider=provider)
            conn.execute(
                "UPDATE schedule_state SET last_success_at = ? WHERE city_id = ? AND provider = ?",
                (when, city_id, provider),
            )
    conn.commit()

    planned = pf.plan_prefreeze_all_enabled(conn, _cfg(data_dir))
    assert [(c.city_id, t) for c, t, _ in planned] == [
        (gamma, "drive"),
        (beta, "drive"),
        (alpha, "drive"),
    ]
    assert planned[0][2] == ["gsv_streets", "mapillary_streets"], "one GraphML, both channels"

    rc, fetcher, _ = _run(monkeypatch, _cfg(data_dir), "--all-enabled", "--limit", "2", "--execute")
    assert rc == 0
    assert fetcher.calls == [(gamma, "drive"), (beta, "drive")]


def _set_walked(conn, city_id, provider, when):
    """Record a success on this channel and backdate it to ``when``."""
    db.record_attempt(conn, city_id, success=True, provider=provider)
    conn.execute(
        "UPDATE schedule_state SET last_success_at = ? WHERE city_id = ? AND provider = ?",
        (when, city_id, provider),
    )
    conn.commit()


def _fail(conn, city_id, provider, times):
    for _ in range(times):
        db.record_attempt(conn, city_id, success=False, error="no network", provider=provider)


def test_all_enabled_staleness_is_the_oldest_channel_and_any_never_walked_wins(
    three_cities, conn, data_dir
):
    """Channels with DIFFERENT timestamps, which is what tells min from max:
    Alpha (Jan / Sep) is older than Beta (May / May) by its OLDEST walk and
    younger by its newest; Gamma, walked on one channel and never on the
    other, is never-walked, as is Delta, which sorts ahead of it by city_id."""
    alpha, beta, gamma = three_cities
    delta = _register(conn, "Delta")
    _set_walked(conn, alpha, "gsv_streets", "2026-01-01T00:00:00")
    _set_walked(conn, alpha, "mapillary_streets", "2026-09-01T00:00:00")
    _set_walked(conn, beta, "gsv_streets", "2026-05-01T00:00:00")
    _set_walked(conn, beta, "mapillary_streets", "2026-05-01T00:00:00")
    _set_walked(conn, gamma, "mapillary_streets", "2026-02-01T00:00:00")

    planned = pf.plan_prefreeze_all_enabled(conn, _cfg(data_dir))
    assert [c.city_id for c, _, _ in planned] == [delta, gamma, alpha, beta]


def test_all_enabled_skips_a_channel_quarantined_at_the_failure_cap(three_cities, conn, data_dir):
    """A quarantined channel never walks the city until an operator
    intervenes, so its network buys nothing tonight. Quarantine is per
    CHANNEL: a city still walked by another channel is still planned, for it."""
    alpha, beta, gamma = three_cities
    cfg = _cfg(data_dir)
    for provider in ("gsv_streets", "mapillary_streets"):
        _fail(conn, alpha, provider, cfg.max_consecutive_failures)
    _fail(conn, beta, "gsv_streets", cfg.max_consecutive_failures)

    planned = pf.plan_prefreeze_all_enabled(conn, cfg)
    assert [(c.city_id, ch) for c, _, ch in planned] == [
        (beta, ["mapillary_streets"]),
        (gamma, ["gsv_streets", "mapillary_streets"]),
    ]


def test_all_enabled_limit_1_passes_move_past_a_city_whose_fetch_keeps_failing(
    conn, data_dir, monkeypatch, capsys
):
    """The PR #382 review's repro: a city whose bbox has no drivable ways fails
    every fetch and writes no GraphML, so it is cold forever. Failing but not
    yet quarantined, it goes behind every clean city, so repeated --limit 1
    passes make progress instead of re-asking it on every pass."""
    cities = [_register(conn, f"City{i:02d}") for i in range(4)]
    roadless = cities[0]
    for provider in ("gsv_streets", "mapillary_streets"):
        _fail(conn, roadless, provider, 2)

    def one_pass():
        fetcher = _Fetcher({roadless: DownloadError("no drivable ways in this bbox")})
        rc, fetcher, _ = _run(
            monkeypatch,
            _cfg(data_dir),
            "--all-enabled",
            "--limit",
            "1",
            "--execute",
            fetcher=fetcher,
        )
        return rc, fetcher.calls

    assert [one_pass() for _ in range(3)] == [
        (0, [(cities[1], "drive")]),
        (0, [(cities[2], "drive")]),
        (0, [(cities[3], "drive")]),
    ]
    capsys.readouterr()

    # Only the failing city is left: it is still asked (it is not quarantined,
    # and the night would ask it too), but a pass that froze nothing says so.
    assert one_pass() == (0, [(roadless, "drive")])
    out = capsys.readouterr().out
    assert "WARNING: every planned fetch failed (1 of 1); NOTHING was frozen" in out

    # And once the nights have quarantined it, no pass asks at all.
    for provider in ("gsv_streets", "mapillary_streets"):
        _fail(conn, roadless, provider, 3)
    assert one_pass() == (0, [])


def test_all_enabled_skips_a_city_no_enabled_street_channel_walks(
    three_cities, conn, data_dir, monkeypatch
):
    """A network no channel will walk is an Overpass request that buys nothing:
    Gamma is excluded from both walk channels, so it is not planned."""
    alpha, beta, gamma = three_cities
    for provider in ("gsv_streets", "mapillary_streets"):
        db.set_channel_membership(conn, gamma, provider, False, cycle_days=90)
    planned = pf.plan_prefreeze_all_enabled(conn, _cfg(data_dir))
    assert [c.city_id for c, _, _ in planned] == [alpha, beta]

    # On an OPT-IN walk channel a NULL member means "not enrolled", so only the
    # city an operator enrolled is planned -- the channel's default, never True.
    db.set_channel_membership(conn, beta, "kartaview_streets", True, cycle_days=90)
    conn.commit()
    cfg = _cfg(data_dir, providers={"kartaview_streets": ProviderConfig(enabled=True)})
    planned = pf.plan_prefreeze_all_enabled(conn, cfg)
    assert [(c.city_id, ch) for c, _, ch in planned] == [(beta, ["kartaview_streets"])]


def test_all_enabled_dry_run_fetches_nothing_and_defaults_limit_to_20(
    conn, data_dir, monkeypatch, capsys
):
    cities = [_register(conn, f"City{i:02d}") for i in range(22)]
    rc, fetcher, slept = _run(monkeypatch, _cfg(data_dir), "--all-enabled")
    assert rc == 0
    assert fetcher.calls == [] and slept == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Would freeze 20 cold street network(s) across every enabled city" in out
    assert "2 more cold network(s) are past --limit 20." in out
    assert cities[19] in out and cities[20] not in out


def test_all_enabled_and_nights_are_mutually_exclusive(three_cities, data_dir, monkeypatch):
    rc, fetcher, _ = _run(
        monkeypatch, _cfg(data_dir), "--all-enabled", "--nights", "1", "--execute"
    )
    assert rc == USAGE_EXIT_CODE
    assert fetcher.calls == []


def test_all_enabled_refuses_date(three_cities, data_dir, monkeypatch):
    """No date enters a staleness-ordered plan, so accepting one would promise
    a filter that is not applied."""
    fetcher = _Fetcher()
    monkeypatch.setattr(pf, "load_scheduler_config", lambda path: _cfg(data_dir))
    monkeypatch.setattr(pf, "fetch_graph", fetcher)
    monkeypatch.setattr(pf, "_run_due_in_flight", lambda: None)
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    argv = ["--all-enabled", "--execute"]
    assert pf.main(["--date", TODAY.isoformat(), *argv]) == USAGE_EXIT_CODE
    assert fetcher.calls == []
    # The control: the same invocation without --date runs.
    assert pf.main(argv) == 0
    assert len(fetcher.calls) == 3


def test_all_enabled_still_stops_on_a_run_due_and_a_host_condition(
    three_cities, data_dir, monkeypatch
):
    """The backlog drain shares the slate mode's guards, not a copy of them."""
    alpha, _, _ = three_cities
    rc, fetcher, _ = _run(
        monkeypatch, _cfg(data_dir), "--all-enabled", "--execute", in_flight="pid 4242: run-due"
    )
    assert rc == USAGE_EXIT_CODE
    assert fetcher.calls == []

    blocked = HostBlockedError("Overpass refused this host", host=HOST_OVERPASS)
    rc, fetcher, _ = _run(
        monkeypatch,
        _cfg(data_dir),
        "--all-enabled",
        "--execute",
        fetcher=_Fetcher({alpha: blocked}),
    )
    assert rc == 76
    assert fetcher.calls == [(alpha, "drive")]


def test_all_enabled_alert_names_the_mode_to_rerun(three_cities, data_dir, monkeypatch):
    alpha, _, _ = three_cities
    blocked = HostBlockedError("Overpass refused this host", host=HOST_OVERPASS)
    rc, _, alerts = _run_alerting(
        monkeypatch,
        _cfg(data_dir),
        "--all-enabled",
        "--execute",
        "--alert",
        fetcher=_Fetcher({alpha: blocked}),
    )
    assert rc == 76
    ((_, body),) = alerts.sent
    assert "--all-enabled --execute" in body and "--nights 2" not in body
