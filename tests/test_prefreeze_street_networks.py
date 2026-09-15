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
