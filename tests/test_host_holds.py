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
    alerts = kwargs.pop("alerts", None)
    monkeypatch.setattr(
        sched, "send_alert", lambda *a, **k: alerts.append(a) if alerts is not None else None
    )
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
    alerts = []
    cfg = _cfg(tmp_path, "refusal_cooldown_h = 36\n")
    cfg.alerts.enabled = True
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches = _refusing_night(monkeypatch, conn, cfg, exit_code=75, alerts=alerts)
    assert [p for _c, p in launches].count("gsv") == 2
    assert "Could not record the mapillary_tiles refusal" in caplog.text
    done = _done_line(caplog)
    assert "stopped early" not in done
    lost = (
        "refusal(s) NOT RECORDED in host_refusals (1 of Mapillary's tile CDN "
        "(tiles.mapillary.com)): the next night's refusal cooldown cannot see them"
    )
    assert f"; {lost}" in done
    # The fix it names holds the channels by hand; a probe would ask the
    # possibly still-blocking host again (#444 review). A refusal the probe
    # meets IS recorded, so the note must not claim it never arms a cooldown.
    assert "systemctl --user disable --now streetscape-tracker.timer" in done
    assert "record one with `probe-host`" not in done
    assert "never a cooldown" not in done
    assert "does arm the cooldown" in done
    # Surfaced in the alert too: the night alerts anyway, because of the refusal.
    assert len(alerts) == 1
    _cfg_arg, _subject, body = alerts[0]
    assert "Refusal(s) NOT RECORDED in host_refusals (1 of Mapillary's tile CDN" in body
    # The alert's own paragraph carries the advice too, not only the summary
    # line quoted above it (the capital R is the paragraph's sentence).
    paragraph = body.split("Refusal(s) NOT RECORDED", 1)[1]
    assert "by hand" in paragraph and "does arm the cooldown" in paragraph
    assert rc != 0


def test_a_lost_refusal_of_a_host_with_no_cooldown_gets_no_hold_advice(
    conn, monkeypatch, tmp_path, caplog
):
    """Round-2 review of #444: "hold until the cooldown would have ended" means
    nothing for a host with no refusal_cooldown_h (Overpass 76, KartaView 81,
    Panoramax 84, or a tile CDN configured without one): the lost row changes
    nothing the next night does. The clause says only that it was lost."""

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sched.db, "record_host_verdict", broken)
    _register(conn, "Bend")
    alerts = []
    cfg = _cfg(tmp_path)
    assert HOST_MAPILLARY_TILES not in cfg.host_refusal_cooldown_h
    cfg.alerts.enabled = True
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _refusing_night(monkeypatch, conn, cfg, exit_code=75, alerts=alerts)
    done = _done_line(caplog)
    assert "; refusal(s) NOT RECORDED in host_refusals (1 of Mapillary's tile CDN" in done
    assert "cooldown" not in done.split("NOT RECORDED", 1)[1]
    assert "by hand" not in done
    _cfg_arg, _subject, body = alerts[0]
    assert "Refusal(s) NOT RECORDED in host_refusals" in body
    assert "by hand" not in body


def test_a_recorded_refusal_says_nothing_about_lost_records(conn, monkeypatch, tmp_path, caplog):
    """The clause appears only when a write failed."""
    _register(conn, "Bend")
    alerts = []
    cfg = _cfg(tmp_path)
    cfg.alerts.enabled = True
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _refusing_night(monkeypatch, conn, cfg, exit_code=75, alerts=alerts)
    assert "NOT RECORDED" not in _done_line(caplog)
    assert len(alerts) == 1 and "NOT RECORDED" not in alerts[0][2]


# ── a THROTTLED child (exit 87, issue #444 review) ────────────────────────

THROTTLED = 87


def _failures(conn):
    return {
        (r["city_id"], r["provider"]): r["consecutive_failures"]
        for r in conn.execute(
            "SELECT city_id, provider, consecutive_failures FROM schedule_state "
            "WHERE provider IN ('mapillary', 'mapillary_streets')"
        )
    }


def test_the_throttled_code_is_the_collectors():
    from streetscape_metadata_tracker.download_common import HOST_THROTTLED_EXIT_CODES

    assert HOST_THROTTLED_EXIT_CODES == {HOST_MAPILLARY_TILES: THROTTLED}


def test_a_throttled_child_latches_the_host_for_the_night_and_writes_no_refusal(
    conn, monkeypatch, tmp_path, caplog
):
    """87 trips the breaker like a refusal -- one Mapillary launch, then both
    Mapillary channels skipped for every later city -- but writes NO
    host_refusals row, records no failure, and still alerts and exits nonzero."""
    _register(conn, "Bend")
    _register(conn, "Corvallis")
    alerts = []
    cfg = _cfg(tmp_path, None)
    cfg.alerts.enabled = True
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches = _refusing_night(monkeypatch, conn, cfg, exit_code=THROTTLED, alerts=alerts)

    assert rc != 0
    providers = [p for _c, p in launches]
    assert providers.count("mapillary") == 1
    assert "mapillary_streets" not in providers
    assert providers.count("gsv") == 2
    assert _refusal_rows(conn) == []
    assert set(_failures(conn).values()) <= {0}
    done = _done_line(caplog)
    assert "THROTTLED" in done and "unavailable" not in done
    assert len(alerts) == 1
    _cfg_arg, subject, body = alerts[0]
    assert "1 host(s) THROTTLED" in subject
    assert "UNAVAILABLE" not in subject
    assert "THROTTLED this host" in body
    assert "was refused by" not in body


@pytest.mark.parametrize("exit_code,counted", [(THROTTLED, False), (1, True)])
def test_a_throttled_night_quarantines_nothing_however_often_it_repeats(
    conn, monkeypatch, tmp_path, exit_code, counted
):
    """Amnestied: nights of a 429 storm must never quarantine a Mapillary pair
    (#421). The exit-1 control proves the harness records a real failure."""
    a = _register(conn, "Bend")
    cfg = _cfg(tmp_path, None)
    nights = 3
    for day in range(nights):
        _refusing_night(
            monkeypatch, conn, cfg, exit_code=exit_code, today=THURSDAY + timedelta(days=day)
        )
    assert _failures(conn).get((a, "mapillary"), 0) == (nights if counted else 0)


@pytest.mark.parametrize("exit_code", [THROTTLED, 75], ids=["throttled-87", "refused-75"])
def test_a_throttled_or_refused_pair_stays_due(conn, monkeypatch, tmp_path, exit_code):
    """Round-2 review of #444: the amnesty is only half the contract -- the
    pair must also stay DUE, so it leads the next night's queue and resumes
    its checkpoint. Nothing pinned that: recording a success before the
    branch's `continue` survived the whole suite. 75 is pinned beside 87,
    since its branch had the same gap."""
    a = _register(conn, "Bend")
    cfg = _cfg(tmp_path, None)
    _refusing_night(monkeypatch, conn, cfg, exit_code=exit_code)

    row = conn.execute(
        "SELECT last_success_at FROM schedule_state WHERE city_id = ? AND provider = 'mapillary'",
        (a,),
    ).fetchone()
    assert row is None or row["last_success_at"] is None
    due = db.get_due_cities(
        conn,
        today=THURSDAY + timedelta(days=1),
        cycle_days=cfg.cycle_days,
        grace_days=cfg.grace_days,
        max_consecutive_failures=cfg.max_consecutive_failures,
        default_membership=sched.CHANNEL_DEFAULT_MEMBERSHIP["mapillary"],
        provider="mapillary",
    )
    assert a in [c.city_id for c in due]


def test_a_throttle_arms_no_refusal_cooldown(conn, monkeypatch, tmp_path, frozen_utc_clock):
    """The next night is not held: a 429 is inconclusive, so only a real
    refusal (a host_refusals `refused` row) may start the 36 h cooldown."""
    frozen_utc_clock(datetime(2026, 10, 15, 9, 30, tzinfo=UTC))
    _register(conn, "Bend")
    cfg = _cfg(tmp_path, "refusal_cooldown_h = 36\n")
    _refusing_night(monkeypatch, conn, cfg, exit_code=THROTTLED)
    assert db.newest_host_verdict(conn, HOST_MAPILLARY_TILES, db.HOST_REFUSED) is None
    frozen_utc_clock(datetime(2026, 10, 16, 2, 0, tzinfo=UTC))
    assert sched._host_holds(cfg, date(2026, 10, 16), conn).active(HOST_MAPILLARY_TILES) is None


def test_a_walk_lost_to_a_throttle_strands_and_the_alert_says_throttled(
    conn, monkeypatch, tmp_path, caplog
):
    """The grid run lands, the walk exits 87: stranded exactly as for a refused
    host, named as a throttle -- and the end-of-night retry launches no second
    child into the latched host."""
    a = _register(conn, "Bend")
    launches = []

    def fake(cfg_, city, today_, provider="gsv", **kw):
        launches.append((city.city_id, provider))
        if provider == "mapillary_streets":
            return sched.CollectionOutcome(False, "exited 87 (stub)", exit_code=THROTTLED)
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary, **kw: 0)
    alerts = []
    monkeypatch.setattr(sched, "send_alert", lambda *a_, **k: alerts.append(a_))
    cfg = _cfg(tmp_path, None)
    cfg.alerts.enabled = True
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc = sched.cmd_run_due(cfg, today=THURSDAY)

    assert rc != 0
    assert launches.count((a, "mapillary_streets")) == 1
    assert "1 city(ies) STRANDED un-walked" in _done_line(caplog)
    _cfg_arg, subject, body = alerts[0]
    assert f"{a} (mapillary_streets: a throttled host (HTTP 429/503))" in body
    assert _refusal_rows(conn) == []


@pytest.mark.parametrize("days_old,warned", [(6, True), (1, False)])
def test_a_throttled_checkpoint_near_the_age_wall_is_warned_about(
    conn, monkeypatch, tmp_path, caplog, days_old, warned
):
    """Round-2 review of #444: the 87 branch keeps the checkpoint exactly as
    the 83 branch does, so repeated throttle stops can age it into the 7-day
    CHECKPOINT_MAX_AGE_S wall; it now gives the 83 branch's warning the night
    before. The fresh case is the control."""
    from tests.test_scheduler import _write_sweep_checkpoint

    a = _register(conn, "Bend")
    city = db.resolve_city(conn, a)
    monkeypatch.setenv("STREETSCAPE_CHECKPOINT_DIR", str(tmp_path / "cp"))
    _write_sweep_checkpoint(city, "mapillary", roots_done=3, root_count=800, days_old=days_old)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _refusing_night(monkeypatch, conn, _cfg(tmp_path, None), exit_code=THROTTLED)
    age_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "not finishing at this nightly budget" in r.getMessage()
    ]
    assert len(age_warnings) == (1 if warned else 0), age_warnings
    if warned:
        assert age_warnings[0].startswith(f"{a} [mapillary]:")


def test_a_throttle_latches_without_a_recheck_and_is_never_recovered():
    breaker = sched.HostBreaker(cooldown_s=0)
    breaker.throttle(HOST_MAPILLARY_TILES)
    assert breaker and breaker.throttled_only({HOST_MAPILLARY_TILES})
    assert breaker.blocking({HOST_MAPILLARY_TILES}) == {HOST_MAPILLARY_TILES}
    assert breaker.rechecks[HOST_MAPILLARY_TILES] == 0
    assert breaker.recovered == set()
    # A refusal on the same night is named as a refusal, not a throttle.
    breaker.add(HOST_MAPILLARY_TILES)
    assert not breaker.throttled_only({HOST_MAPILLARY_TILES})


def test_the_child_reason_names_a_throttle(tmp_path, monkeypatch):
    """`_run_collection_subprocess` turns 87 into words, never "unavailable"."""
    cfg = _cfg(tmp_path, None)

    class _Done:
        returncode = THROTTLED

    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: _Done())
    row = type("C", (), {"city_id": "bend--oregon--united-states"})()
    outcome = sched._run_collection_subprocess(cfg, ["true"], 60, row, "mapillary", THURSDAY)
    assert outcome.exit_code == THROTTLED and not outcome
    assert "THROTTLED this host" in outcome.reason
    assert "unavailable" not in outcome.reason


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


# ── scheduler probe-host ─────────────────────────────────────────────────

_TOKEN = "MLY|probe-token-SECRET"


class _Fetch:
    """An injected fetch primitive: records every URL, answers one canned response."""

    def __init__(self, status=200, headers=None, error=None):
        self.urls = []
        self.status = status
        self.headers = (
            headers if headers is not None else {"Content-Type": "application/x-protobuf"}
        )
        self.error = error

    def __call__(self, url):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.status, self.headers


@pytest.fixture
def probe_env(conn, monkeypatch, tmp_path, frozen_utc_clock):
    """The catalog, a frozen clock and a stubbed token for probe-host."""
    from streetscape_metadata_tracker import host_probe

    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    loader, url_for, classify = host_probe.PROBES[HOST_MAPILLARY_TILES]
    monkeypatch.setitem(
        host_probe.PROBES, HOST_MAPILLARY_TILES, (lambda: _TOKEN, url_for, classify)
    )
    frozen_utc_clock(_NIGHT_START)
    return _cfg(tmp_path, "refusal_cooldown_h = 36\n")


def _verdicts(conn):
    return [
        (r["verdict"], r["source"], r["detail"])
        for r in conn.execute("SELECT * FROM host_refusals ORDER BY id")
    ]


def test_probe_host_sends_exactly_one_tile_request_and_records_a_clearance(conn, probe_env, capsys):
    fetch = _Fetch()
    rc = sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch)
    assert rc == 0
    assert len(fetch.urls) == 1
    assert fetch.urls[0] == (
        "https://tiles.mapillary.com/maps/vtp/mly1_computed_public/2/14/2581/5921"
        f"?access_token={_TOKEN}"
    )
    assert _verdicts(conn) == [("cleared", "probe-host", "HTTP 200 tile (application/x-protobuf)")]
    # The one request is ledgered like any other, so the host window sees it.
    assert db.get_api_usage(conn, _NIGHT_START.date(), "mapillary") == 1
    assert "CLEARED" in capsys.readouterr().out


def test_a_redirect_is_a_refusal_and_the_token_never_reaches_the_record(conn, probe_env):
    fetch = _Fetch(
        302,
        {"Location": f"https://www.mapillary.com/login/?next=x%3Faccess_token%3D{_TOKEN}"},
    )
    rc = sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch)
    assert rc == 75
    ((verdict, source, detail),) = _verdicts(conn)
    assert (verdict, source) == ("refused", "probe-host")
    assert detail.startswith("HTTP 302 -> https://www.mapillary.com/login/")
    assert "SECRET" not in detail


def test_a_probe_refusal_starts_the_cooldown_a_night_then_honours(
    conn, monkeypatch, probe_env, frozen_utc_clock
):
    """The pass-through from probe-host's row to the next night's gate."""
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=_Fetch(302, {})) == 75
    frozen_utc_clock(_NIGHT_START + timedelta(hours=20))
    a = _register(conn, "Bend")
    rc, launches, _ = _night(monkeypatch, conn, probe_env, today=THURSDAY)
    assert rc == 0
    assert launches == [(a, "gsv")]


@pytest.mark.parametrize("status", [*range(300, 309), 429, 503])
def test_the_probe_reads_redirects_and_backpressure_as_the_collector_does(status):
    """S4 of the #444 review: the probe imports the collector's two status
    sets rather than restating them, and agrees with `_fetch_tile` itself on
    every 3xx and on 429/503 -- so a 304, which the collector does not treat
    as the block, is never a REFUSED probe."""
    import asyncio

    from streetscape_metadata_tracker import download_mapillary as dm
    from streetscape_metadata_tracker import host_probe
    from streetscape_metadata_tracker.download_common import HostBlockedError

    result = host_probe.classify_mapillary(status, {"Location": "https://x/login"})

    class _Resp:
        def __init__(self):
            self.status = status
            self.headers = {"Location": "https://x/login"}

        async def read(self):
            return b""

        def raise_for_status(self):
            raise RuntimeError(f"HTTP {status}")

    class _Session:
        def get(self, url, **kwargs):
            class _Ctx:
                async def __aenter__(self_inner):
                    return _Resp()

                async def __aexit__(self_inner, *exc):
                    return False

            return _Ctx()

    async def fetch():
        return await dm._fetch_tile.__wrapped__(_Session(), "https://t/1", None)

    try:
        asyncio.run(fetch())
        collector = "served"
    except HostBlockedError:
        collector = "refused"
    except dm.TileBackpressureError:
        collector = "backpressure"
    except Exception:  # noqa: BLE001 - any other per-tile failure
        collector = "other"

    assert (result.verdict == db.HOST_REFUSED) == (collector == "refused"), (status, collector)
    assert (status in dm.TILE_REDIRECT_STATUSES) == (collector == "refused")
    assert (status in dm.TILE_BACKPRESSURE_STATUSES) == (collector == "backpressure")
    if collector == "backpressure":
        assert result.verdict is None and "throttled" in result.detail
    if status == 304:
        assert result.verdict != db.HOST_REFUSED


@pytest.mark.parametrize(
    "status,headers,verdict,rc",
    [
        (429, {"Retry-After": "3600"}, None, 1),
        (503, {}, None, 1),
        (200, {"Content-Type": "text/html; charset=utf-8"}, "refused", 75),
        (301, {}, "refused", 75),
        (404, {}, "cleared", 0),
        (403, {}, None, 1),
        (401, {}, None, 1),
        (500, {}, None, 1),
    ],
)
def test_probe_host_classifies_each_answer(conn, probe_env, status, headers, verdict, rc):
    fetch = _Fetch(status, headers)
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == rc
    assert len(fetch.urls) == 1
    assert [v for v, _s, _d in _verdicts(conn)] == ([verdict] if verdict else [])


@pytest.mark.parametrize(
    "status,headers,said",
    [
        (
            429,
            {"Retry-After": "3600"},
            "HTTP 429: throttled (backpressure, not a refusal); Retry-After: 3600",
        ),
        (503, {}, "HTTP 503: throttled (backpressure, not a refusal); no Retry-After"),
    ],
)
def test_a_throttled_probe_records_nothing_and_says_so(
    conn, monkeypatch, probe_env, capsys, frozen_utc_clock, status, headers, said
):
    """The collector reads a 429/503 as backpressure -- it waits and slows,
    records no refusal and trips no breaker -- so the probe must not arm the
    36 h cooldown on one: exit 1, no row, and the next night is not held."""
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=_Fetch(status, headers)) == 1
    assert _verdicts(conn) == []
    out = capsys.readouterr().out
    assert f"INCONCLUSIVE — {said} (not recorded)." in out
    frozen_utc_clock(_NIGHT_START + timedelta(hours=20))
    a = _register(conn, "Bend")
    rc, launches, _ = _night(monkeypatch, conn, probe_env, today=THURSDAY)
    assert rc == 0
    assert sorted(launches) == [(a, "gsv"), (a, "mapillary"), (a, "mapillary_streets")]


def test_no_response_is_inconclusive_and_not_ledgered(conn, probe_env):
    fetch = _Fetch(error=ConnectionError("connection refused"))
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == 1
    assert _verdicts(conn) == []
    assert db.get_api_usage(conn, _NIGHT_START.date(), "mapillary") == 0


def test_probe_host_refuses_while_a_run_due_is_in_flight(conn, monkeypatch, probe_env):
    monkeypatch.setattr(sched, "_run_due_in_flight", lambda: "pid 4242: python -m ... run-due")
    fetch = _Fetch()
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == (
        sched.USAGE_EXIT_CODE
    )
    assert fetch.urls == [] and _verdicts(conn) == []


def test_probe_host_refuses_inside_the_cooldown_unless_forced(conn, probe_env, caplog):
    _refused(conn, 20)
    fetch = _Fetch()
    with caplog.at_level(logging.ERROR, logger="streetscape_scheduler"):
        rc = sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch)
    assert rc == sched.USAGE_EXIT_CODE
    assert fetch.urls == []
    assert "cooling down until 2026-10-16 01:00 UTC" in caplog.text
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch, force=True) == 0
    assert len(fetch.urls) == 1


# ── probe-host on a rest day (S5 of the #444 review) ──────────────────────

# 05:00 UTC Wednesday is 22:00 PDT TUESDAY, and 03:00 UTC Thursday is 20:00 PDT
# WEDNESDAY: the two instants where the UTC date and the Pacific one differ.
_WED_UTC_TUE_PACIFIC = datetime(2026, 10, 14, 5, 0, tzinfo=UTC)
_THU_UTC_WED_PACIFIC = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


@pytest.fixture
def pacific_tz():
    """Run with the machine's local zone set to Pacific, as on makelab2, so a
    probe reading a LOCAL date reads the other weekday and fails these tests.

    Restores only TZ, and re-runs tzset after restoring it. Not monkeypatch:
    the zone must be reset with tzset AFTER the variable is restored, and
    ``monkeypatch.undo()`` there would also undo every other patch the test
    made, out of order with the fixtures that made them."""
    import time as _time

    saved = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    _time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        _time.tzset()


@pytest.fixture
def rest_probe_env(probe_env, tmp_path):
    return _cfg(tmp_path, 'refusal_cooldown_h = 36\nrest_weekdays = ["Wed"]\n')


def test_probe_host_refuses_on_a_rest_day_read_in_utc(
    conn, rest_probe_env, frozen_utc_clock, pacific_tz, caplog
):
    frozen_utc_clock(_WED_UTC_TUE_PACIFIC)
    fetch = _Fetch()
    with caplog.at_level(logging.ERROR, logger="streetscape_scheduler"):
        rc = sched.cmd_probe_host(rest_probe_env, HOST_MAPILLARY_TILES, fetch=fetch)
    assert rc == sched.USAGE_EXIT_CODE
    assert fetch.urls == [] and _verdicts(conn) == []
    assert db.get_api_usage(conn, date(2026, 10, 14), provider="mapillary") == 0
    assert "rest night (Wed)" in caplog.text
    assert "zero presence" in caplog.text and "Stage 1" in caplog.text
    assert "--force skips the rest-day and cooldown checks" in caplog.text


def test_probe_host_on_a_pacific_wednesday_that_is_a_utc_thursday_proceeds(
    conn, rest_probe_env, frozen_utc_clock, pacific_tz
):
    frozen_utc_clock(_THU_UTC_WED_PACIFIC)
    fetch = _Fetch()
    assert sched.cmd_probe_host(rest_probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == 0
    assert len(fetch.urls) == 1


def test_probe_host_on_a_non_rest_day_is_unaffected(conn, rest_probe_env):
    """probe_env's frozen instant is a Thursday: no rest, no cooldown."""
    fetch = _Fetch()
    assert sched.cmd_probe_host(rest_probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == 0
    assert len(fetch.urls) == 1


def test_force_probes_on_a_rest_day(conn, rest_probe_env, frozen_utc_clock, caplog):
    frozen_utc_clock(_WED_UTC_TUE_PACIFIC)
    fetch = _Fetch()
    with caplog.at_level(logging.WARNING, logger="streetscape_scheduler"):
        rc = sched.cmd_probe_host(rest_probe_env, HOST_MAPILLARY_TILES, fetch=fetch, force=True)
    assert rc == 0 and len(fetch.urls) == 1
    assert "probing on a rest day" in caplog.text


def test_force_never_skips_the_run_due_in_flight_check(
    conn, monkeypatch, rest_probe_env, frozen_utc_clock
):
    """On a rest day and inside a cooldown alike: --force overrides those two
    holds, never a probe beside a live night."""
    monkeypatch.setattr(sched, "_run_due_in_flight", lambda: "pid 4242: python -m ... run-due")
    for instant, refused in ((_WED_UTC_TUE_PACIFIC, False), (_NIGHT_START, True)):
        frozen_utc_clock(instant)
        if refused:
            _refused(conn, 20)
        fetch = _Fetch()
        rc = sched.cmd_probe_host(rest_probe_env, HOST_MAPILLARY_TILES, fetch=fetch, force=True)
        assert rc == sched.USAGE_EXIT_CODE
        assert fetch.urls == []


def test_the_cooldown_refusal_says_what_force_skips(conn, rest_probe_env, caplog):
    _refused(conn, 20)
    with caplog.at_level(logging.ERROR, logger="streetscape_scheduler"):
        rc = sched.cmd_probe_host(rest_probe_env, HOST_MAPILLARY_TILES, fetch=_Fetch())
    assert rc == sched.USAGE_EXIT_CODE
    assert "--force skips the rest-day and cooldown checks, never the run-due-in-flight" in (
        caplog.text
    )


def test_probe_host_refuses_while_another_process_holds_the_host_lock(conn, probe_env):
    """The lock every collection child takes: the busy code, and nothing sent.
    Held the way tests/test_host_lock.py holds it -- a second FileLock on the
    same path, which is another holder as far as flock is concerned."""
    from filelock import FileLock

    from streetscape_metadata_tracker import host_lock as hl

    fetch = _Fetch()
    other = FileLock(hl.lock_path(HOST_MAPILLARY_TILES))
    other.acquire()
    try:
        assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == 79
    finally:
        other.release()
    assert fetch.urls == [] and _verdicts(conn) == []
    # Free again: the probe takes the lock itself, and gives it back.
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == 0
    other.acquire(timeout=0)
    other.release()


def test_probe_host_refuses_an_unknown_host_and_a_missing_token(conn, monkeypatch, probe_env):
    from streetscape_metadata_tracker import host_probe

    fetch = _Fetch()
    assert sched.cmd_probe_host(probe_env, "overpass", fetch=fetch) == sched.USAGE_EXIT_CODE

    def no_token():
        raise ValueError("MAPILLARY_ACCESS_TOKEN not found")

    _loader, url_for, classify = host_probe.PROBES[HOST_MAPILLARY_TILES]
    monkeypatch.setitem(host_probe.PROBES, HOST_MAPILLARY_TILES, (no_token, url_for, classify))
    assert sched.cmd_probe_host(probe_env, HOST_MAPILLARY_TILES, fetch=fetch) == (
        sched.USAGE_EXIT_CODE
    )
    assert fetch.urls == []


def test_probe_host_is_wired_into_the_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sched.sys, "argv", ["scheduler", "probe-host", "mapillary_tiles", "--force"]
    )
    monkeypatch.setattr(sched, "load_scheduler_config", lambda path: _cfg(tmp_path))
    monkeypatch.setattr(sched, "setup_logging", lambda cfg, verbose=False: None)
    seen = {}
    monkeypatch.setattr(
        sched, "cmd_probe_host", lambda cfg, host, **kw: seen.update(kw, host=host) or 0
    )
    assert sched.main() == 0
    assert seen == {"host": "mapillary_tiles", "force": True}


def test_the_production_fetch_never_follows_a_redirect(monkeypatch):
    """allow_redirects=False is the whole verdict: following the 302 lands on
    the login page's own 200 (the collector's #199 bug)."""
    import requests

    from streetscape_metadata_tracker import host_probe

    seen = {}

    class _Response:
        status_code = 302
        headers = {"Location": "https://www.mapillary.com/login/"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_get(url, **kwargs):
        seen.update(kwargs, url=url)
        return _Response()

    monkeypatch.setattr(requests, "get", fake_get)
    assert host_probe.requests_fetch("https://t/x") == (302, _Response.headers)
    assert seen["allow_redirects"] is False
    assert seen["timeout"] == host_probe.PROBE_TIMEOUT_S
    # The probe must look like the collector it predicts (#444), not like
    # requests' default python-requests/<ver>.
    from streetscape_metadata_tracker.download_mapillary import MAPILLARY_USER_AGENT

    assert seen["headers"] == {"User-Agent": MAPILLARY_USER_AGENT}
    assert "sidewalk@cs.uw.edu" in seen["headers"]["User-Agent"]


def test_the_token_comes_from_the_existing_loader(monkeypatch):
    import dotenv

    from streetscape_metadata_tracker import config, host_probe

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(config, "warn_if_credentials_world_readable", lambda path: False)
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "MLY|from-env")
    assert host_probe.load_mapillary_token() == "MLY|from-env"
    monkeypatch.delenv("MAPILLARY_ACCESS_TOKEN")
    with pytest.raises(ValueError):
        host_probe.load_mapillary_token()


# ── per-host presence on the Done: line ──────────────────────────────────


def test_the_done_line_names_each_metered_hosts_presence_and_the_gap_before_it(
    conn, monkeypatch, tmp_path, caplog, frozen_utc_clock
):
    """First launch to last finish of any channel on the tile CDN, and the gap
    back to the newest ledger stamp from before the run. GSV has no per-IP
    host and Overpass is left out, so neither appears."""
    conn.execute(
        "INSERT INTO host_usage VALUES (?, 'mapillary_tiles', 'mapillary', 900)",
        ((_NIGHT_START - timedelta(hours=17, minutes=30)).isoformat(),),
    )
    conn.commit()
    frozen_utc_clock(_NIGHT_START)
    _register(conn, "Bend")
    elapsed = {"min": 0}

    def fake(cfg_, city, today_, provider="gsv", **kw):
        elapsed["min"] += 30 if provider != "gsv" else 6  # each child's wall clock
        frozen_utc_clock(_NIGHT_START + timedelta(minutes=elapsed["min"]))
        if provider in MAPILLARY_PAIR:
            # Tonight's own stamps, which the gap must NOT measure back to.
            db.add_api_usage(conn, today_, 10, provider)
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary, **kw: 0)
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        assert sched.cmd_run_due(_cfg(tmp_path, None), today=THURSDAY) == 0

    done = _done_line(caplog)
    # gsv 09:00-09:06, mapillary 09:06-09:36, mapillary_streets 09:36-10:06.
    assert (
        "; on Mapillary's tile CDN (tiles.mapillary.com) 09:06-10:06 UTC (1.0 h), "
        "17.6 h after its previous ledgered request"
    ) in done
    assert done.count("UTC (") == 1


# ── the dry run walks the queue the way the night does (#444 review) ─────


def _wholly_held_then_due(conn):
    """A: due only on the held Mapillary pair (its gsv succeeded today). B: due everywhere."""
    a = _register(conn, "Albany")
    b = _register(conn, "Bend")
    db.record_attempt(conn, a, success=True, provider="gsv")
    return a, b


def test_a_wholly_held_city_takes_no_slot_live_or_in_the_preview(
    conn, monkeypatch, tmp_path, capsys
):
    """At --limit 1 the night passes over a city whose every due channel is
    held and collects the next one; the preview must name the same city,
    not spend its one row on the held one."""
    a, b = _wholly_held_then_due(conn)
    cfg = _cfg(tmp_path)

    rc, launches, _ = _night(monkeypatch, conn, cfg, limit=1)
    assert rc == 0
    assert launches == [(b, "gsv")]

    conn.execute("DELETE FROM schedule_state WHERE city_id = ? AND provider = 'gsv'", (b,))
    conn.commit()
    capsys.readouterr()
    rc, launches, _ = _night(monkeypatch, conn, cfg, limit=1, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0 and launches == []
    rows = [
        ln.split() for ln in out.splitlines() if ln.startswith("  ") and ln.split()[0] in (a, b)
    ]
    assert [r[:2] for r in rows] == [[b, "gsv"], [b, "mapillary"], [b, "mapillary_streets"]], out
    assert a not in out
    assert "(1 due city(ies) HELD on every channel passed over" in out


def test_the_preview_counts_held_pairs_as_holding_the_fill(conn, monkeypatch, tmp_path, capsys):
    """A held due pair is unattempted, so -- the live rule -- it holds the fill
    on that channel; the preview must say so too (mutation M10 of the review)."""
    _register(conn, "Bend")
    cfg = _cfg(tmp_path, fill=True)
    rc, _launches, _ = _night(monkeypatch, conn, cfg, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        "Fill: holding mapillary, mapillary_streets — the due slate leaves mapillary 1 "
        "unfinished, mapillary_streets 1 unfinished."
    ) in out, out


def test_a_passed_over_citys_held_pairs_still_hold_the_fill(conn, monkeypatch, tmp_path, capsys):
    """A city the preview passes over (held on every due channel) is still
    unattempted due work, so it holds the fill on those channels, as it does live."""
    a = _register(conn, "Albany")
    db.record_attempt(conn, a, success=True, provider="gsv")
    rc, _launches, _ = _night(monkeypatch, conn, _cfg(tmp_path, fill=True), dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "(1 due city(ies) HELD on every channel passed over" in out
    assert (
        "Fill: holding mapillary, mapillary_streets — the due slate leaves mapillary 1 "
        "unfinished, mapillary_streets 1 unfinished."
    ) in out, out
