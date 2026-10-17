"""
Standing per-IP host holds (issue #444): rest nights, and the plumbing every
later hold shares.

A HOLD keeps a host's channels from launching without anything having refused
us tonight. Unlike the breaker (issues #208, #341) it never trips, alerts,
records a failure or changes the exit status, and the cities stay due.

Pure logic: the collection seam is ``_run_one_city`` (faked), and the tail is
stubbed, so ``cmd_run_due`` runs for real down to the launch gate.
"""

import logging
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import scheduler as sched
from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES
from streetscape_metadata_tracker.scheduler import load_scheduler_config

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_CONFIG = os.path.join(_PROJECT_ROOT, "config", "scheduler.makelab1.toml")

WEDNESDAY = date(2026, 10, 14)
THURSDAY = date(2026, 10, 15)
MAPILLARY_PAIR = ("mapillary", "mapillary_streets")


def _cfg(tmp_path, hosts_block='rest_weekdays = ["Wed"]\n', *, fill=False):
    """gsv + the Mapillary pair, loaded THROUGH the loader, with a [hosts] block."""
    path = tmp_path / "holds.toml"
    path.write_text(
        ("[schedule]\nfill_min_days = 30\n\n" if fill else "")
        + "[providers.gsv]\nenabled = true\ndaily_request_budget = 10000000\n\n"
        "[providers.mapillary]\nenabled = true\ndaily_request_budget = 5000\n"
        "max_requests_per_minute = 40\njitter = 0.6\n\n"
        "[providers.mapillary_streets]\nenabled = true\ndaily_request_budget = 5000\n"
        "max_requests_per_minute = 40\njitter = 0.6\n\n"
        f'[paths]\ndata_dir = "{tmp_path / "data"}"\nlog_dir = "{tmp_path / "logs"}"\n'
        f'backup_dir = "{tmp_path / "backups"}"\n\n'
        + (f"[hosts.mapillary_tiles]\n{hosts_block}" if hosts_block is not None else "")
    )
    return load_scheduler_config(str(path))


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
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )


def _night(monkeypatch, conn, cfg, *, today=WEDNESDAY, **kwargs):
    """``cmd_run_due`` with a fake child; returns ``(exit, launches, alerts)``."""
    launches = []
    alerts = []

    def fake(cfg_, city, today_, provider="gsv", **kw):
        launches.append((city.city_id, provider))
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary, **kw: 0)
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: alerts.append(a))
    rc = sched.cmd_run_due(cfg, today=today, **kwargs)
    return rc, launches, alerts


def _done_line(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Done: ")]
    assert len(lines) == 1, lines
    return lines[0]


# ── config ───────────────────────────────────────────────────────────────


def test_rest_weekdays_load_canonical_and_in_weekday_order(tmp_path):
    cfg = _cfg(tmp_path, 'rest_weekdays = ["sun", "WED"]\nrolling_24h_request_budget = 900\n')
    assert cfg.host_budget_errors == []
    assert cfg.host_rest_weekdays == {HOST_MAPILLARY_TILES: ("Wed", "Sun")}
    assert cfg.host_budgets == {HOST_MAPILLARY_TILES: 900}


def test_a_rest_only_section_loads_without_a_budget(tmp_path):
    """Since #444 a [hosts.*] section may hold rest_weekdays alone."""
    cfg = _cfg(tmp_path, 'rest_weekdays = ["Wed"]\n')
    assert cfg.host_budget_errors == []
    assert cfg.host_budgets == {}
    assert cfg.host_rest_weekdays == {HOST_MAPILLARY_TILES: ("Wed",)}


def test_an_empty_rest_list_means_no_rest_night(tmp_path):
    cfg = _cfg(tmp_path, "rest_weekdays = []\n")
    assert cfg.host_budget_errors == []
    assert cfg.host_rest_weekdays == {}


@pytest.mark.parametrize(
    "block",
    [
        'rest_weekdays = "Wed"\n',
        'rest_weekdays = ["Wednesday"]\n',
        'rest_weekdays = ["Wed", "wed"]\n',
        "rest_weekdays = [3]\n",
        'rest_weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]\n',
        # A valid budget beside a bad rest list: the WHOLE section is refused,
        # so the budget must not load either.
        'rolling_24h_request_budget = 1200\nrest_weekdays = ["Wedn"]\n',
    ],
    ids=["string", "full-name", "duplicate", "integer", "every-day", "beside-a-budget"],
)
def test_an_invalid_rest_list_fails_closed(conn, monkeypatch, tmp_path, caplog, block):
    """Dropping a bad rest list would run the host on the night it was meant to
    rest (fail-OPEN), so it is recorded and the channel-running commands refuse
    -- the same posture as an invalid [hosts.*] budget (#385)."""
    with caplog.at_level(logging.ERROR, logger="streetscape_scheduler"):
        cfg = _cfg(tmp_path, block)
    assert cfg.host_rest_weekdays == {}
    assert cfg.host_budgets == {}
    assert len(cfg.host_budget_errors) == 1
    assert "rest_weekdays" in cfg.host_budget_errors[0]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == (
        cfg.host_budget_errors
    )
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)
    assert sched.cmd_run_due(cfg, today=WEDNESDAY) == sched.USAGE_EXIT_CODE
    assert sched.cmd_assess_city(cfg, "Bend, OR") == sched.USAGE_EXIT_CODE
    assert connected == []


def test_makelab1_rests_the_tile_cdn_on_wednesday_and_sunday():
    """Pinned exactly, with the pass-through: the PROD config's rest list is the
    one `_host_holds` reads, so a Wednesday and a Sunday hold the tile CDN and
    a Thursday does not."""
    cfg = load_scheduler_config(PROD_CONFIG)
    assert cfg.host_budget_errors == []
    assert cfg.host_rest_weekdays == {HOST_MAPILLARY_TILES: ("Wed", "Sun")}
    assert sched._host_holds(cfg, WEDNESDAY).active(HOST_MAPILLARY_TILES).detail == "Wed"
    assert sched._host_holds(cfg, date(2026, 10, 18)).active(HOST_MAPILLARY_TILES) is not None
    assert sched._host_holds(cfg, THURSDAY).active(HOST_MAPILLARY_TILES) is None
    # Nothing else rests: GSV, KartaView and Panoramax run every night.
    assert sched._host_holds(cfg, WEDNESDAY).hosts() == {HOST_MAPILLARY_TILES}


# ── the night ────────────────────────────────────────────────────────────


def test_a_rest_night_holds_only_the_hosts_channels_and_is_not_a_failure(
    conn, monkeypatch, tmp_path, caplog
):
    """The core contract: gsv runs, the Mapillary pair launches nothing, and
    nothing about the night reads as a refusal or a failure."""
    a = _register(conn, "Bend")
    b = _register(conn, "Corvallis")
    cfg = _cfg(tmp_path)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(monkeypatch, conn, cfg)

    assert rc == 0
    assert alerts == []
    assert sorted(launches) == [(a, "gsv"), (b, "gsv")]
    # The held pairs recorded no attempt, so the cities stay due and no
    # failure counter moved.
    for cid in (a, b):
        for channel in MAPILLARY_PAIR:
            row = conn.execute(
                "SELECT last_attempt_at, consecutive_failures FROM schedule_state "
                "WHERE city_id = ? AND provider = ?",
                (cid, channel),
            ).fetchone()
            assert row["last_attempt_at"] is None
            assert row["consecutive_failures"] == 0
    done = _done_line(caplog)
    assert "HELD Mapillary's tile CDN (tiles.mapillary.com): rest night (Wed)" in done
    assert "4 launch(es) held, cities stay due" in done
    assert "unavailable" not in done and "refused" not in done
    assert "holding mapillary, mapillary_streets for this run" in caplog.text


def test_a_non_rest_night_launches_the_pair(conn, monkeypatch, tmp_path, caplog):
    cid = _register(conn, "Bend")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(monkeypatch, conn, _cfg(tmp_path), today=THURSDAY)
    assert rc == 0
    assert sorted(launches) == [(cid, "gsv"), (cid, "mapillary"), (cid, "mapillary_streets")]
    assert "HELD" not in _done_line(caplog)


def test_a_rest_night_with_nothing_due_on_the_host_still_reaches_the_done_line(
    conn, monkeypatch, tmp_path, caplog
):
    """A rest is never invisible: named even when it held no launch."""
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(monkeypatch, conn, _cfg(tmp_path))
    assert rc == 0 and launches == []
    assert "rest night (Wed), 0 launch(es) held" in _done_line(caplog)


def test_the_limit_catch_up_honours_the_rest_night(conn, monkeypatch, tmp_path, caplog):
    """`run-due --provider mapillary --limit N`, the supported catch-up path."""
    _register(conn, "Bend")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(
            monkeypatch, conn, _cfg(tmp_path), requested_providers=["mapillary"], limit=5
        )
    assert rc == 0 and alerts == []
    assert launches == []
    assert "rest night (Wed), 1 launch(es) held" in _done_line(caplog)


def test_ignore_host_holds_launches_and_says_so(conn, monkeypatch, tmp_path, caplog):
    cid = _register(conn, "Bend")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(
            monkeypatch,
            conn,
            _cfg(tmp_path),
            requested_providers=["mapillary"],
            limit=5,
            ignore_host_holds=True,
        )
    assert rc == 0
    assert launches == [(cid, "mapillary")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(m.startswith("--ignore-host-holds") and "rest night (Wed)" in m for m in warnings), (
        warnings
    )


def test_the_override_flag_reaches_cmd_run_due(monkeypatch, tmp_path):
    monkeypatch.setattr(sched.sys, "argv", ["scheduler", "run-due", "--ignore-host-holds"])
    monkeypatch.setattr(sched, "load_scheduler_config", lambda path: _cfg(tmp_path))
    monkeypatch.setattr(sched, "setup_logging", lambda cfg, verbose=False: None)
    seen = {}
    monkeypatch.setattr(sched, "cmd_run_due", lambda cfg, **kw: seen.update(kw) or 0)
    assert sched.main() == 0
    assert seen["ignore_host_holds"] is True


def test_the_rest_night_is_read_on_the_utc_snapshot_date(
    conn, monkeypatch, tmp_path, frozen_utc_clock
):
    """00:30 UTC Wednesday is still Tuesday evening in Pacific time. The run is
    dated Wednesday (#347), so it is a Wednesday rest night -- whatever the
    host's local calendar says."""
    frozen_utc_clock(datetime(2026, 10, 14, 0, 30, tzinfo=UTC))
    _register(conn, "Bend")
    rc, launches, _ = _night(monkeypatch, conn, _cfg(tmp_path), today=None)
    assert rc == 0
    assert [p for _c, p in launches] == ["gsv"]


def test_the_dry_run_shows_the_hold(conn, monkeypatch, tmp_path, capsys):
    cid = _register(conn, "Bend")
    rc, launches, _ = _night(monkeypatch, conn, _cfg(tmp_path), dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0 and launches == []
    assert "HELD tonight — Mapillary's tile CDN (tiles.mapillary.com): rest night (Wed)" in out
    held_lines = [ln for ln in out.splitlines() if cid in ln and "HELD (rest night)" in ln]
    assert len(held_lines) == 2, out


def test_status_names_the_rest_weekdays_and_whether_today_is_one(
    conn, monkeypatch, tmp_path, capsys, frozen_utc_clock
):
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    cfg = _cfg(tmp_path, 'rest_weekdays = ["Wed", "Sun"]\n')
    frozen_utc_clock(datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
    sched.cmd_status(cfg)
    assert (
        "Mapillary's tile CDN (tiles.mapillary.com) rests on Wed, Sun (UTC snapshot date); "
        "today (2026-10-14, Wed) is a REST night."
    ) in capsys.readouterr().out
    frozen_utc_clock(datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
    sched.cmd_status(cfg)
    assert "today (2026-10-15, Thu) is not a rest night." in capsys.readouterr().out


# ── the fill ─────────────────────────────────────────────────────────────


def test_the_fill_declines_a_city_whose_channel_is_on_a_held_host(conn, tmp_path):
    """Admitted whole or not at all: the launch gate would hold the Mapillary
    pair, so a fill city that runs them would land without them."""
    cfg = _cfg(tmp_path, fill=True)
    city = db.resolve_city(conn, _register(conn, "Bend"))
    verdict = sched._fill_prebudget_refusal(
        cfg, city, ["gsv", "mapillary"], latched=set(), held_hosts={HOST_MAPILLARY_TILES}
    )
    assert verdict is not None and not verdict.admit
    assert verdict.blocker == sched._FILL_BLOCKER_HOST_HELD
    assert sched._fill_prebudget_refusal(cfg, city, ["gsv"], set(), {HOST_MAPILLARY_TILES}) is None


def test_the_live_fill_is_handed_the_runs_holds(conn, monkeypatch, tmp_path):
    """The pass-through: `_fill_admission` asks the breaker's holds, not an empty set."""
    cfg = _cfg(tmp_path, fill=True)
    city = db.resolve_city(conn, _register(conn, "Bend"))
    seen = {}
    real = sched._fill_judge

    def spy(*a, **kw):
        seen["held_hosts"] = kw.get("held_hosts")
        return real(*a, **kw)

    monkeypatch.setattr(sched, "_fill_judge", spy)
    breaker = sched.HostBreaker(holds=sched._host_holds(cfg, WEDNESDAY))
    verdict = sched._fill_admission(cfg, conn, WEDNESDAY, city, ["gsv", "mapillary"], None, breaker)
    assert seen["held_hosts"] == {HOST_MAPILLARY_TILES}
    assert verdict.blocker == sched._FILL_BLOCKER_HOST_HELD


# ── the breaker is untouched ─────────────────────────────────────────────


def test_a_hold_never_enters_the_breakers_set():
    """`bool(breaker)` is the alert and the exit status; a hold must not reach it."""
    breaker = sched.HostBreaker(holds=sched.HostHolds(rest={HOST_MAPILLARY_TILES: "Wed"}))
    assert breaker.holding({HOST_MAPILLARY_TILES}) == {
        HOST_MAPILLARY_TILES: sched.HostHold(HOST_MAPILLARY_TILES, sched.HOLD_REST, "Wed")
    }
    assert not breaker
    assert breaker.latched == set()
    assert breaker.blocking({HOST_MAPILLARY_TILES}) == set()


# ── persisted refusals (host_refusals, schema v22) ───────────────────────


def _refusing_night(monkeypatch, conn, cfg, *, exit_code, today=THURSDAY, **kwargs):
    """A night whose Mapillary children all exit ``exit_code``."""
    launches = []

    def fake(cfg_, city, today_, provider="gsv", **kw):
        launches.append((city.city_id, provider))
        if provider in MAPILLARY_PAIR:
            return sched.CollectionOutcome(False, f"exited {exit_code} (stub)", exit_code=exit_code)
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary, **kw: 0)
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    return sched.cmd_run_due(cfg, today=today, **kwargs), launches


def _refusal_rows(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT host, verdict, source, channel, city_id, exit_code, detail, requests_before "
            "FROM host_refusals ORDER BY id"
        )
    ]


def test_a_refusal_the_scheduler_classifies_is_persisted(
    conn, monkeypatch, tmp_path, frozen_utc_clock
):
    """Exactly one row per classified refusal -- the refused child's -- with
    the host's prior-24 h ledger; the breaker's skips that follow add none."""
    now = frozen_utc_clock(datetime(2026, 10, 15, 9, 30, tzinfo=UTC))
    conn.execute(
        "INSERT INTO host_usage VALUES (?, 'mapillary_tiles', 'mapillary', 1631)",
        (datetime(2026, 10, 15, 2, 0, tzinfo=UTC).isoformat(),),
    )
    a = _register(conn, "Bend")
    _register(conn, "Corvallis")
    rc, launches = _refusing_night(monkeypatch, conn, _cfg(tmp_path), exit_code=75)

    assert rc != 0  # a refusal is still loud: that is the breaker's, unchanged
    assert [p for _c, p in launches].count("mapillary") == 1
    assert _refusal_rows(conn) == [
        {
            "host": HOST_MAPILLARY_TILES,
            "verdict": "refused",
            "source": "run-due",
            "channel": "mapillary",
            "city_id": a,
            "exit_code": 75,
            "detail": "exited 75 (stub)",
            "requests_before": 1631,
        }
    ]
    row = db.newest_host_verdict(conn, HOST_MAPILLARY_TILES, db.HOST_REFUSED)
    assert row["recorded_at"] == now.isoformat()


def test_a_busy_host_is_not_a_refusal_and_writes_no_row(conn, monkeypatch, tmp_path):
    """79 is another local process holding the lock -- not the third party."""
    _register(conn, "Bend")
    _refusing_night(monkeypatch, conn, _cfg(tmp_path), exit_code=79)
    assert _refusal_rows(conn) == []


def test_a_failed_refusal_write_never_ends_the_night(conn, monkeypatch, tmp_path, caplog):
    """The breaker has the refusal already; losing the record must not cost the
    night's later cities, its publish or its alert."""

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sched.db, "record_host_verdict", broken)
    _register(conn, "Bend")
    _register(conn, "Corvallis")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches = _refusing_night(monkeypatch, conn, _cfg(tmp_path), exit_code=75)
    assert [p for _c, p in launches].count("gsv") == 2
    assert "Could not record the mapillary_tiles refusal" in caplog.text
    assert "stopped early" not in _done_line(caplog)
    assert rc != 0


def test_status_prints_the_last_refusal_and_clearance_per_host(
    conn, monkeypatch, tmp_path, capsys, frozen_utc_clock
):
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    refused_at = datetime(2026, 10, 17, 9, 3, tzinfo=UTC)
    db.record_host_verdict(
        conn,
        HOST_MAPILLARY_TILES,
        db.HOST_REFUSED,
        source="run-due",
        channel="mapillary",
        city_id="carlsbad",
        exit_code=75,
        at=refused_at,
    )
    frozen_utc_clock(datetime(2026, 10, 18, 15, 3, tzinfo=UTC))
    sched.cmd_status(_cfg(tmp_path))
    out = capsys.readouterr().out
    assert (
        "Mapillary's tile CDN (tiles.mapillary.com): last refused 2026-10-17T09:03:00+00:00 "
        "(run-due, carlsbad [mapillary], exit 75, 0 requests in the 24 h before), 30.0 h ago; "
        "no clearance recorded."
    ) in out


# ── the cross-night refusal cooldown ─────────────────────────────────────

_NIGHT_START = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)  # a Thursday, 02:00 PDT


def _refused(conn, hours_before, at=_NIGHT_START):
    db.record_host_verdict(
        conn,
        HOST_MAPILLARY_TILES,
        db.HOST_REFUSED,
        source="run-due",
        channel="mapillary",
        exit_code=75,
        at=at - timedelta(hours=hours_before),
    )


@pytest.mark.parametrize("value,hours", [("36", 36.0), ("0.5", 0.5), ("168", 168.0)])
def test_a_refusal_cooldown_loads(tmp_path, value, hours):
    cfg = _cfg(tmp_path, f"refusal_cooldown_h = {value}\n")
    assert cfg.host_budget_errors == []
    assert cfg.host_refusal_cooldown_h == {HOST_MAPILLARY_TILES: hours}


@pytest.mark.parametrize("value", ["0", "-1", "169", "true", '"36"', "[36]"])
def test_an_invalid_refusal_cooldown_fails_closed(conn, monkeypatch, tmp_path, value):
    """Dropping it would let the nightly re-probe the blocked IP it exists to
    leave alone, so it refuses the channel-running commands like a bad budget."""
    cfg = _cfg(tmp_path, f"refusal_cooldown_h = {value}\n")
    assert cfg.host_refusal_cooldown_h == {}
    assert len(cfg.host_budget_errors) == 1
    assert "refusal_cooldown_h" in cfg.host_budget_errors[0]
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    assert sched.cmd_run_due(cfg, today=THURSDAY) == sched.USAGE_EXIT_CODE


def test_makelab1_cools_the_tile_cdn_for_36_hours_and_it_reaches_the_gate(conn, frozen_utc_clock):
    """The pass-through: prod's 36 h is the figure the holds are built from."""
    cfg = load_scheduler_config(PROD_CONFIG)
    assert cfg.host_refusal_cooldown_h == {HOST_MAPILLARY_TILES: 36.0}
    _refused(conn, 20)
    frozen_utc_clock(_NIGHT_START)
    hold = sched._host_holds(cfg, THURSDAY, conn).active(HOST_MAPILLARY_TILES)
    assert hold is not None and hold.reason == sched.HOLD_COOLDOWN
    assert "until 2026-10-16 01:00 UTC (36 h after the refusal recorded 2026-10-14 13:00 UTC)" in (
        hold.describe()
    )
    frozen_utc_clock(_NIGHT_START + timedelta(hours=16, seconds=1))
    assert sched._host_holds(cfg, THURSDAY, conn).active(HOST_MAPILLARY_TILES) is None


def test_a_night_inside_the_cooldown_holds_the_host_and_is_not_a_refusal(
    conn, monkeypatch, tmp_path, caplog, frozen_utc_clock
):
    """The 10-17 shape: refused ~20 h before the 02:00 fire. Nothing is sent to
    the host, nothing alerts, the exit is clean, no new refusal row is written,
    and the cities stay due."""
    _refused(conn, 20)
    frozen_utc_clock(_NIGHT_START)
    a = _register(conn, "Bend")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(
            monkeypatch, conn, _cfg(tmp_path, "refusal_cooldown_h = 36\n"), today=THURSDAY
        )
    assert rc == 0 and alerts == []
    assert launches == [(a, "gsv")]
    assert conn.execute("SELECT COUNT(*) FROM host_refusals").fetchone()[0] == 1
    for channel in MAPILLARY_PAIR:
        row = conn.execute(
            "SELECT last_attempt_at FROM schedule_state WHERE city_id = ? AND provider = ?",
            (a, channel),
        ).fetchone()
        assert row["last_attempt_at"] is None
    done = _done_line(caplog)
    assert (
        "HELD Mapillary's tile CDN (tiles.mapillary.com): cooling down until 2026-10-16 01:00 UTC"
        in done
    )
    assert "2 launch(es) held" in done
    assert "cooling down until 2026-10-16 01:00 UTC" in caplog.text


def test_a_night_past_the_cooldown_launches(conn, monkeypatch, tmp_path, frozen_utc_clock):
    _refused(conn, 37)
    frozen_utc_clock(_NIGHT_START)
    a = _register(conn, "Bend")
    rc, launches, _ = _night(
        monkeypatch, conn, _cfg(tmp_path, "refusal_cooldown_h = 36\n"), today=THURSDAY
    )
    assert rc == 0
    assert sorted(launches) == [(a, "gsv"), (a, "mapillary"), (a, "mapillary_streets")]


def test_no_cooldown_configured_is_todays_behaviour(conn, monkeypatch, tmp_path, frozen_utc_clock):
    """Unset means no cooldown: a host refused an hour ago is asked again."""
    _refused(conn, 1)
    frozen_utc_clock(_NIGHT_START)
    a = _register(conn, "Bend")
    rc, launches, _ = _night(monkeypatch, conn, _cfg(tmp_path, None), today=THURSDAY)
    assert sorted(launches) == [(a, "gsv"), (a, "mapillary"), (a, "mapillary_streets")]


def test_a_clearance_does_not_shorten_the_cooldown(conn, tmp_path, frozen_utc_clock):
    """Only the newest REFUSAL counts; one positive probe is not licence for volume."""
    _refused(conn, 20)
    db.record_host_verdict(
        conn,
        HOST_MAPILLARY_TILES,
        db.HOST_CLEARED,
        source="probe-host",
        at=_NIGHT_START - timedelta(hours=2),
    )
    frozen_utc_clock(_NIGHT_START)
    cfg = _cfg(tmp_path, "refusal_cooldown_h = 36\n")
    assert sched._host_holds(cfg, THURSDAY, conn).active(HOST_MAPILLARY_TILES) is not None


def test_the_cooldown_ends_mid_night_and_the_host_resumes(
    conn, monkeypatch, tmp_path, frozen_utc_clock
):
    """Read against the wall clock at each launch: a night that starts 35.5 h
    after the refusal holds the first city's pair and runs the second's once
    the 36 h have passed."""
    _refused(conn, 35.5)
    frozen_utc_clock(_NIGHT_START)
    first = _register(conn, "Bend")
    second = _register(conn, "Corvallis")
    launches = []
    elapsed = {"h": 0.0}

    def fake(cfg_, city, today_, provider="gsv", **kw):
        launches.append((city.city_id, provider))
        if provider == "gsv":  # each gsv run takes 20 min of wall clock
            elapsed["h"] += 1 / 3
            frozen_utc_clock(_NIGHT_START + timedelta(hours=elapsed["h"]))
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary, **kw: 0)
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    cfg = _cfg(tmp_path, "refusal_cooldown_h = 36\n")
    assert sched.cmd_run_due(cfg, today=THURSDAY) == 0

    by_city = {cid: [p for c, p in launches if c == cid] for cid in (first, second)}
    ran_first = launches[0][0]
    ran_second = second if ran_first == first else first
    assert by_city[ran_first] == ["gsv"]
    assert by_city[ran_second] == ["gsv", "mapillary", "mapillary_streets"]


def test_status_says_whether_the_host_is_cooling_down(
    conn, monkeypatch, tmp_path, capsys, frozen_utc_clock
):
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    cfg = _cfg(tmp_path, "refusal_cooldown_h = 36\n")
    _refused(conn, 20)
    frozen_utc_clock(_NIGHT_START)
    sched.cmd_status(cfg)
    assert (
        "Mapillary's tile CDN (tiles.mapillary.com) refusal cooldown 36 h: cooling down until "
        "2026-10-16 01:00 UTC"
    ) in capsys.readouterr().out
    frozen_utc_clock(_NIGHT_START + timedelta(hours=17))
    sched.cmd_status(cfg)
    assert "refusal cooldown 36 h: not cooling down." in capsys.readouterr().out
