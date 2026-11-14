"""The night's length: its bracket, the runtime clamp, and the tail's SIGTERM guard (issue #389).

`[schedule].max_batch_hours = 12` was inherited, not derived, and the only pin
on it compared it with the unit's `TimeoutStartSec` in whole hours with no tail
term, so 13.9 h passed. These tests define the bracket once, from the files:

- the UNIT line, `h + NIGHT_OVERRUN_S + NIGHT_MARGIN_S <= TimeoutStartSec`,
  pinned between the production TOML and the shipped unit, and enforced at
  runtime by `night_window_seconds` for an installed unit the repo cannot see;
- the start timeout's own backstop clear of the next fire;
- a daytime ops window, a policy figure kept here rather than in config;
- an ON-TIME night ending inside its UTC date, in both seasons.

And the tail: `TimeoutStartSec` sends SIGTERM, which mid-loop is the #206
wind-down but in `_finish_batch` used to kill the publish. It is now logged and
ignored there (`_tail_survives_sigterm`). The loop's own wind-down is pinned
elsewhere and not duplicated: tests/test_scheduler.py's
`test_sigterm_winds_the_batch_down_instead_of_killing_the_publish` and
`test_sigterm_stops_the_city_mid_channel_instead_of_finishing_its_channels`.

Every figure is read out of the unit files and the production TOML, so a change
on either side of a cross-file agreement fails here rather than on prod.
"""

import logging
import math
import os
import re
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from scripts import prefreeze_street_networks as pf  # noqa: E402
from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker import scheduler as sched  # noqa: E402
from streetscape_metadata_tracker.scheduler import (  # noqa: E402
    SchedulerConfig,
    load_scheduler_config,
    night_window_seconds,
)
from tests.test_prefreeze_unit import _env, _one, _parse_unit, _span_minutes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROD_CONFIG = ROOT / "config" / "scheduler.makelab1.toml"

# The daytime ops window production keeps between a night's latest end and the
# next 02:00 fire: deploys, hand catch-ups, `assess-city`, `import-bundle`, all
# of which take host locks a running night holds (busy exits 79/80/82/85).
# Deliberately a test constant and not a config key (issue #389): it is a
# policy bound on max_batch_hours, and nothing at runtime would read it. 6 h
# admits 12 h (11.15 h left) and 16 h (7.15 h), and makes 20 h (3.15 h) a
# deliberate edit of this line. The window is smaller than it looks for
# anything needing Overpass: the chained prefreeze holds that lock for a
# further ~1.5 to 3 h after the night (`--limit 40 --pause-s 120`).
OPS_WINDOW_HOURS = 6


@pytest.fixture(scope="module")
def units():
    return {
        "collect": _parse_unit("streetscape-tracker.service"),
        "nightly": _parse_unit("streetscape-tracker.timer"),
    }


@pytest.fixture(scope="module")
def prod_cfg():
    return load_scheduler_config(str(PROD_CONFIG))


def _start_timeout_s(units) -> float:
    return _span_minutes(_one(units["collect"], "Service", "TimeoutStartSec")) * 60


def _stop_timeout_s(units) -> float:
    return _span_minutes(_one(units["collect"], "Service", "TimeoutStopSec")) * 60


def _fire_slack_s() -> float:
    """RandomizedDelaySec + systemd's default AccuracySec, as the prefreeze
    script carries it; test_the_scripts_fire_slack_is_the_nightly_timers pins
    that constant to the timer file, so it is not re-derived here."""
    return pf.NIGHTLY_FIRE_SLACK.total_seconds()


# --- the bracket, pinned between the files ------------------------------------


def test_the_night_plus_its_overrun_and_margin_fits_the_units_start_timeout(units, prod_cfg):
    """The unit line of the bracket, at the production value. The pin it
    replaces (whole hours, no tail term) accepted 13.9 h under a 14 h unit."""
    need_s = prod_cfg.max_batch_hours * 3600 + sched.NIGHT_OVERRUN_S + sched.NIGHT_MARGIN_S
    assert need_s <= _start_timeout_s(units), (
        f"max_batch_hours = {prod_cfg.max_batch_hours} needs TimeoutStartSec >= "
        f"{need_s / 3600:.3f} h (the tail and in-flight allowance, then the margin), but the "
        f"unit has {_start_timeout_s(units) / 3600:g} h: raise the unit first, and read "
        "docs/scheduler.md's 'What a raise must settle first' before raising either"
    )


def test_the_tail_allowance_is_the_units_stop_timeout(units):
    """One stand-in for the tail: the number a wind-down's tail must already fit."""
    assert sched.NIGHT_TAIL_ALLOWANCE_S == _stop_timeout_s(units)


def test_the_unit_tells_the_scheduler_its_own_start_timeout(units):
    """The runtime clamp reads the unit's TimeoutStartSec from this variable,
    because nothing inside the sandbox can read the installed unit. A raise of
    one without the other would clamp every night to the old figure, or let a
    night outgrow the new one."""
    env = _env(units["collect"])
    assert sched.UNIT_TIMEOUT_ENV_VAR in env, "the unit no longer states its start timeout"
    assert float(env[sched.UNIT_TIMEOUT_ENV_VAR]) == _start_timeout_s(units)


def test_the_start_timeouts_kill_lands_clear_of_the_next_fire(units):
    """TimeoutStartSec is the backstop for a hung night: SIGTERM, then SIGKILL
    after TimeoutStopSec. On an on-time night that kill must land an hour before
    the next 02:00, or a hung night still holds the unit when the timer fires
    (and the start of an active oneshot is a no-op, so that night is lost). A
    catch-up start is later, and its own arithmetic is in docs/scheduler.md."""
    kill_s = _fire_slack_s() + _start_timeout_s(units) + _stop_timeout_s(units)
    assert kill_s <= 23 * 3600, (
        f"a hung on-time night is SIGKILLed {kill_s / 3600:.2f} h after 02:00"
    )


def test_prod_keeps_its_daytime_ops_window(prod_cfg):
    """Between the night's latest end and the next fire stays at least
    OPS_WINDOW_HOURS: the derivation the production TOML's comment states."""
    left_s = 24 * 3600 - _fire_slack_s() - prod_cfg.max_batch_hours * 3600 - sched.NIGHT_OVERRUN_S
    assert left_s >= OPS_WINDOW_HOURS * 3600, (
        f"max_batch_hours = {prod_cfg.max_batch_hours} leaves {left_s / 3600:.2f} h of daytime "
        f"ops window, under the {OPS_WINDOW_HOURS} h this test keeps; moving it is a decision, "
        "not a side effect of a raise"
    )


@pytest.mark.parametrize("season", [date(2027, 1, 15), date(2027, 7, 15)], ids=["PST", "PDT"])
def test_an_on_time_night_ends_inside_its_utc_date(units, prod_cfg, season):
    """The snapshot date is UTC and read once at the night's start, and the
    census cache refuses an entry completed on a LATER UTC date than its
    consumer's run date (checkpointing.load_census_cache_marker). So a night's
    last child must end before UTC midnight, or a grid crawl that finishes after
    it is refused by its own paired walk, which re-crawls the same tiles
    against the same per-IP host.

    ON-TIME only: the 02:00 fire, plus its slack. A catch-up start (Persistent=,
    the 08:30 watchdog re-arm, a hand start) crosses the line at 12 h already;
    that is an existing defect recorded in docs/scheduler.md, not this pin.
    Both seasons, because PST is the tight one (02:16 PST is 10:16 UTC)."""
    spec = _one(units["nightly"], "Timer", "OnCalendar")
    m = re.fullmatch(r"\*-\*-\* (\d\d):(\d\d):(\d\d) (\S+)", spec)
    assert m, spec
    fire = datetime(
        season.year,
        season.month,
        season.day,
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3)),
        tzinfo=ZoneInfo(m.group(4)),
    )
    last_child_end = fire + timedelta(
        seconds=_fire_slack_s() + prod_cfg.max_batch_hours * 3600 + sched._MIN_CLAMPED_TIMEOUT_S
    )
    start_utc = (fire + timedelta(seconds=_fire_slack_s())).astimezone(ZoneInfo("UTC"))
    end_utc = last_child_end.astimezone(ZoneInfo("UTC"))
    assert end_utc.date() == start_utc.date(), (
        f"an on-time night starting {start_utc:%H:%M} UTC ends its last child at "
        f"{end_utc:%Y-%m-%d %H:%M} UTC, past its UTC date, at max_batch_hours = "
        f"{prod_cfg.max_batch_hours}"
    )


# --- night_window_seconds, the runtime clamp ----------------------------------


def test_a_nightly_under_a_unit_too_short_for_the_window_is_shortened():
    window, note = night_window_seconds(20, is_nightly=True, unit_timeout_env="64800")
    assert window == 64800 - sched.NIGHT_OVERRUN_S - sched.NIGHT_MARGIN_S
    assert note is not None
    assert "max_batch_hours = 20 h" in note and "64800" in note


def test_a_unit_longer_than_the_legacy_one_is_believed():
    """An 18 h unit holds a 16 h night; ignoring the variable would clamp it to 14 h's."""
    assert night_window_seconds(16, is_nightly=True, unit_timeout_env="64800") == (57600.0, None)


@pytest.mark.parametrize("value", [None, "", "abc", "60", "nan", "inf", "-inf"])
def test_a_nightly_without_a_usable_variable_is_held_to_the_legacy_unit(value):
    """Not set, junk, too small to leave a window, or not finite (`float`
    accepts nan and inf, and every comparison with NaN is false): all read as
    an installed copy older than the variable, i.e. 14 h."""
    window, note = night_window_seconds(16, is_nightly=True, unit_timeout_env=value)
    assert (
        window == sched.LEGACY_UNIT_TIMEOUT_START_S - sched.NIGHT_OVERRUN_S - sched.NIGHT_MARGIN_S
    )
    assert note is not None and "14 h" in note


def test_a_window_the_unit_can_hold_is_left_alone():
    assert night_window_seconds(12, is_nightly=True, unit_timeout_env="50400") == (43200.0, None)
    exact_h = (50400 - sched.NIGHT_OVERRUN_S - sched.NIGHT_MARGIN_S) / 3600
    window, note = night_window_seconds(exact_h, is_nightly=True, unit_timeout_env="50400")
    assert note is None and window == exact_h * 3600


@pytest.mark.parametrize("value", [None, "60", "50400"])
def test_a_hand_run_is_never_shortened(value):
    """A hand run has no systemd start timeout, whatever its environment says."""
    assert night_window_seconds(20, is_nightly=False, unit_timeout_env=value, pre_loop_s=3600) == (
        72000.0,
        None,
    )


def test_time_spent_before_the_loop_comes_out_of_the_window():
    """TimeoutStartSec counts from the unit's start, so ten minutes of catalog
    migration before the loop's clock starts are ten minutes the loop lacks."""
    hours = 12.3  # fits 14 h with 420 s to spare
    assert night_window_seconds(hours, is_nightly=True, unit_timeout_env="50400")[1] is None
    window, note = night_window_seconds(
        hours, is_nightly=True, unit_timeout_env="50400", pre_loop_s=600
    )
    assert window == 50400 - sched.NIGHT_OVERRUN_S - sched.NIGHT_MARGIN_S - 600
    # The unit holds 12.3 h; only the slow start broke it, so the note names the
    # elapsed time and does NOT send the operator to the unit (issue #389 review).
    assert note is not None and "600 s (10.0 min) passed before the city loop" in note
    assert "fits the installed unit" in note and "The unit needs no change" in note
    assert "Fix the unit" not in note and "daemon-reload" not in note


def test_a_window_the_unit_cannot_hold_even_without_a_pre_loop_blames_the_unit():
    """16 h under 14 h would not fit with no pre-loop at all: the unit (or the
    hours) is the fix, and the elapsed pre-loop time is still named."""
    window, note = night_window_seconds(
        16, is_nightly=True, unit_timeout_env="50400", pre_loop_s=600
    )
    assert window == 50400 - sched.NIGHT_OVERRUN_S - sched.NIGHT_MARGIN_S - 600
    assert note is not None and "does not fit the installed unit" in note
    assert "It would not fit even with no time before the loop" in note
    assert "Fix the unit" in note and "600 s (10.0 min) passed before the city loop" in note
    assert "needs no change" not in note


# --- the clamp, end to end through cmd_run_due --------------------------------


def _drive_one_city_night(conn, monkeypatch, cfg, *, on_aggregate=None, dry_run=False):
    """Run cmd_run_due over one due city with the collection and the publish
    stubbed; return (rc, the loop's window in seconds, the tail steps run)."""
    db.register_city(
        conn,
        city_name="Alpha",
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
    db.assign_schedule(conn, 90)
    conn.execute("UPDATE schedule_state SET last_success_at = NULL")
    conn.commit()

    seen: dict = {}
    tail: list[str] = []
    real_loop = sched._run_city_loop

    def spy_loop(cfg_, conn_, today_, due_, pfc_, batch_deadline, *args, **kwargs):
        seen["window_s"] = batch_deadline - time.monotonic()
        return real_loop(cfg_, conn_, today_, due_, pfc_, batch_deadline, *args, **kwargs)

    def aggregate(c, d):
        tail.append("aggregate")
        if on_aggregate:
            on_aggregate()

    monkeypatch.setattr(sched, "_run_city_loop", spy_loop)
    monkeypatch.setattr(
        sched, "_run_one_city", lambda cfg_, city, today, provider="gsv", **kw: True
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", aggregate)
    monkeypatch.setattr(
        sched, "generate_streetwalk_manifest", lambda c, d: tail.append("manifest") or {}
    )
    monkeypatch.setattr(sched, "_publish", lambda cfg_, summary, **kw: tail.append("publish") or 0)
    rc = sched.cmd_run_due(cfg, dry_run=dry_run, today=date(2026, 7, 2))
    return rc, seen.get("window_s"), tail


def test_a_shortened_night_alerts_and_runs_to_the_shorter_deadline(conn, monkeypatch, caplog):
    """A nightly under a pre-#389 unit (no variable: 14 h) configured for 16 h,
    ten minutes after its process started: the loop gets 14 h less the
    overrun, the margin AND those ten minutes, one alert says so, and the
    opening line records the window the night really ran under."""
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(sched, "_is_nightly_unit", lambda: True)
    monkeypatch.delenv(sched.UNIT_TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.setattr(sched, "_PROCESS_STARTED_MONOTONIC", time.monotonic() - 600)
    monkeypatch.setattr(sched, "send_alert", lambda a, s, b: alerts.append((s, b)) or True)
    cfg = SchedulerConfig(max_batch_hours=16, publish_enabled=True, max_cities_per_day=5)

    with caplog.at_level(logging.INFO):
        rc, window_s, tail = _drive_one_city_night(conn, monkeypatch, cfg)

    expected_s = (
        sched.LEGACY_UNIT_TIMEOUT_START_S - sched.NIGHT_OVERRUN_S - sched.NIGHT_MARGIN_S - 600
    )
    assert abs(window_s - expected_s) < 30, (window_s, expected_s)
    shortened = [s for s, _b in alerts if "night shortened" in s]
    assert len(shortened) == 1, alerts
    assert shortened[0].endswith("the installed unit cannot hold max_batch_hours")
    logged = re.search(r"; max_batch_hours=([\d.]+)", caplog.text)
    assert logged and math.isclose(float(logged.group(1)) * 3600, expected_s, abs_tol=30)
    assert rc == 0 and tail == ["aggregate", "manifest", "publish"]


def test_a_night_shortened_by_a_slow_start_does_not_blame_the_unit(conn, monkeypatch):
    """12.3 h fits the 14 h unit with 420 s to spare; a ten-minute start eats
    that, so the night is shortened, but the mail says it was the start, and
    does not send the operator to the unit or promise a VIOLATED timer-status."""
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(sched, "_is_nightly_unit", lambda: True)
    monkeypatch.setenv(sched.UNIT_TIMEOUT_ENV_VAR, "50400")
    monkeypatch.setattr(sched, "_PROCESS_STARTED_MONOTONIC", time.monotonic() - 600)
    monkeypatch.setattr(sched, "send_alert", lambda a, s, b: alerts.append((s, b)) or True)
    cfg = SchedulerConfig(max_batch_hours=12.3, publish_enabled=True, max_cities_per_day=5)

    rc, window_s, _tail = _drive_one_city_night(conn, monkeypatch, cfg)

    assert abs(window_s - (50400 - sched.NIGHT_OVERRUN_S - sched.NIGHT_MARGIN_S - 600)) < 30
    [(subject, body)] = [(s, b) for s, b in alerts if "night shortened" in s]
    assert subject.endswith("a slow start before the city loop, not the unit")
    assert "VIOLATED" not in body and "Fix the unit" not in body
    assert rc == 0


def test_a_dry_run_is_never_clamped_and_never_alerts(conn, monkeypatch):
    """`run-due --dry-run` under the nightly's own environment and a unit that
    cannot hold the window: a dry run plans, it does not run to a deadline, so
    it is never shortened and never mails (the `if not dry_run` guard)."""
    alerts: list[tuple[str, str]] = []
    fitted: list[float] = []
    monkeypatch.setattr(sched, "_is_nightly_unit", lambda: True)
    monkeypatch.delenv(sched.UNIT_TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.setattr(sched, "send_alert", lambda a, s, b: alerts.append((s, b)) or True)
    real_fit = sched._fit_night_to_unit
    monkeypatch.setattr(
        sched,
        "_fit_night_to_unit",
        lambda c, **kw: fitted.append(c.max_batch_hours) or real_fit(c, **kw),
    )
    cfg = SchedulerConfig(max_batch_hours=16, publish_enabled=True, max_cities_per_day=5)

    rc, _window_s, _tail = _drive_one_city_night(conn, monkeypatch, cfg, dry_run=True)

    assert rc == 0
    assert fitted == []
    assert [s for s, _b in alerts if "night shortened" in s] == []


# --- the tail survives a SIGTERM ----------------------------------------------


class _DefaultDisposition(Exception):
    """Stands in for the default SIGTERM disposition, which would end pytest."""


@pytest.fixture
def default_sigterm():
    """Install a handler that raises, as the default disposition would kill.

    Without the guard a SIGTERM in the tail reaches this handler and aborts the
    tail; with it, the guard's handler is the one installed. Restored after."""

    def die(signum, frame):
        raise _DefaultDisposition()

    previous = signal.signal(signal.SIGTERM, die)
    yield die
    signal.signal(signal.SIGTERM, previous)


def test_a_sigterm_during_the_tail_is_logged_once_and_the_tail_runs_on(default_sigterm, caplog):
    steps = []
    with caplog.at_level(logging.WARNING), sched._tail_survives_sigterm() as seen:
        steps.append("aggregate")
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)  # a second stop, or the timeout's after a stop
        steps.append("backup")
        steps.append("publish")
    assert steps == ["aggregate", "backup", "publish"]
    assert seen.is_set()
    assert caplog.text.count("SIGTERM received during the night's tail") == 1
    assert signal.getsignal(signal.SIGTERM) is default_sigterm


def test_the_tail_guard_restores_the_handler_when_a_tail_step_raises(default_sigterm):
    with pytest.raises(RuntimeError), sched._tail_survives_sigterm():
        raise RuntimeError("the tail backup raised")
    assert signal.getsignal(signal.SIGTERM) is default_sigterm


def test_a_sigterm_in_the_nights_tail_does_not_cost_the_publish(conn, monkeypatch, default_sigterm):
    """End to end: the signal arrives during the aggregate rebuild, the first
    tail step, as a TimeoutStartSec landing just after the deadline would; the
    manifest and the publish still run, and the handler the night started with
    is back afterwards. The loop's own SIGTERM handling is untouched: its tests
    are in tests/test_scheduler.py (named in this module's docstring)."""
    monkeypatch.setattr(sched, "_is_nightly_unit", lambda: True)
    cfg = SchedulerConfig(publish_enabled=True, max_cities_per_day=5)
    rc, _window_s, tail = _drive_one_city_night(
        conn, monkeypatch, cfg, on_aggregate=lambda: os.kill(os.getpid(), signal.SIGTERM)
    )
    assert tail == ["aggregate", "manifest", "publish"]
    assert rc == 0
    assert signal.getsignal(signal.SIGTERM) is default_sigterm


def test_a_hand_runs_tail_keeps_the_default_sigterm(conn, monkeypatch, default_sigterm, caplog):
    """A hand `run-due` from a shell has no systemd SIGKILL behind an ignored
    SIGTERM, so the guard is the nightly's only: an operator's `kill` during a
    hand run's tail still reaches the default disposition. (The stand-in
    disposition raises, and the tail reports a raising step rather than
    propagating, so what shows here is the aggregate step failing, the night
    unhealthy, and no guard WARNING; the real default would end the process.)"""
    monkeypatch.setattr(sched, "_is_nightly_unit", lambda: False)
    cfg = SchedulerConfig(publish_enabled=True, max_cities_per_day=5)
    with caplog.at_level(logging.WARNING):
        rc, _window_s, tail = _drive_one_city_night(
            conn, monkeypatch, cfg, on_aggregate=lambda: os.kill(os.getpid(), signal.SIGTERM)
        )
    assert tail[0] == "aggregate"
    assert "aggregate index failed" in caplog.text
    assert "SIGTERM received during the night's tail" not in caplog.text
    assert rc != 0
    assert signal.getsignal(signal.SIGTERM) is default_sigterm
