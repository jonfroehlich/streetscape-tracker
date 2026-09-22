"""Tests for scripts/prefreeze_street_networks.py (issue #341).

The daytime pass that freezes the next night's walk networks so a mid-night
Overpass refusal has nothing left to strand. Pinned: dry-run by default, the
slate is the night's own (`_collect_due`, not the raw due list), only COLD
networks are fetched and keyed on each channel's network_type, fetches are
serial and paced, a host condition stops the pass with that host's exit code
while a city-specific failure does not, and a run-due in flight refuses it.

No network: `fetch_graph` is replaced by a recorder that writes the GraphML
path it would have frozen.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from scripts import prefreeze_street_networks as pf  # noqa: E402
from streetscape_metadata_tracker import db  # noqa: E402
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


@pytest.fixture
def three_cities(conn):
    """Alpha, Beta, Gamma: all enabled, never collected, so all due and in that order."""
    return [_register(conn, n) for n in ("Alpha", "Beta", "Gamma")]


class _Fetcher:
    """Stands in for fetch_graph: records calls, freezes the file, can fail."""

    def __init__(self, failures=None):
        self.calls = []
        self.failures = dict(failures or {})

    def __call__(self, city, data_dir, *, network_type, conn):
        self.calls.append((city.city_id, network_type))
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
    rc = pf.main(["--date", TODAY.isoformat(), *[str(a) for a in args]])
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


def test_the_default_date_is_tomorrow_utc():
    """cmd_run_due reads the UTC date at 02:00 Pacific, i.e. the NEXT UTC day
    for a pass run in a Pacific afternoon. Later is the safe error: dueness is
    monotone in the date."""
    from datetime import UTC, datetime, timedelta

    assert pf.next_run_date() == datetime.now(UTC).date() + timedelta(days=1)


# ── --alert: a pass that does not finish is never silent (issue #355) ─────────
#
# The timer runs this with nobody watching, and a pass that dies quietly leaves
# tonight's networks cold -- the exposure the timer exists to remove. So every
# way a pass can end early mails, and a pass that finishes does not.


class _Alerts:
    def __init__(self):
        self.sent = []

    def __call__(self, alert_cfg, subject, body):
        self.sent.append((subject, body))
        return True


def _run_alerting(monkeypatch, cfg, *args, **kwargs):
    alerts = _Alerts()
    monkeypatch.setattr(pf, "send_alert", alerts)
    rc, fetcher, slept = _run(monkeypatch, cfg, *args, **kwargs)
    return rc, fetcher, alerts


def test_a_host_refusal_alerts_with_the_networks_it_left_cold(three_cities, data_dir, monkeypatch):
    alpha, beta, _ = three_cities
    blocked = HostBlockedError("Overpass refused this host", host=HOST_OVERPASS)
    rc, _, alerts = _run_alerting(
        monkeypatch, _cfg(data_dir), "--execute", "--alert", fetcher=_Fetcher({alpha: blocked})
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

    def __call__(self, city, data_dir, *, network_type, conn):
        self.calls.append((city.city_id, network_type))
        handler = pf.signal.getsignal(pf.signal.SIGTERM)
        self.handlers.append(handler)
        if handler is pf._raise_terminated:
            handler(pf.signal.SIGTERM, None)
        return super().__call__(city, data_dir, network_type=network_type, conn=conn)


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
