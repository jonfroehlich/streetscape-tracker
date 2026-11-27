"""
The KartaView night allowance (issue #464): ``[hosts.kartaview].max_night_minutes``.

An optional bound on the minutes of KartaView launches one ``run-due`` charges
before FRESH crawls are held. Since issue #477 resumes of live checkpoints
count toward it (there is no reserve) and are never held themselves; a city
that is paid-for work on the host (a LIVE checkpoint, a cached census) is
exempt from a spent allowance, and a dead checkpoint is held as the fresh
crawl it is once the age-wall arm has judged it. A held launch
behaves like a #444 hold -- no attempt, no failure, no alert, no cap slot, the
pair stays due -- but is counted apart from the HELD clause, which the
Mapillary night report reads.

Pure logic: the collection seam is ``_run_one_city`` (faked), and the fake
child MOVES the frozen UTC clock by the minutes a launch should take, which is
what the allowance charges. Checkpoints and cached censuses are faked at their
two readers, ``_sweep_checkpoint_progress`` and ``channel_census_cache_marker``.
Each test's docstring names the one-line mutation of the production code that
turns it red.
"""

import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest

from streetscape_metadata_tracker import clock, db
from streetscape_metadata_tracker import scheduler as sched
from streetscape_metadata_tracker.download_common import HOST_KARTAVIEW
from streetscape_metadata_tracker.scheduler import load_scheduler_config

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_CONFIG = os.path.join(_PROJECT_ROOT, "config", "scheduler.makelab1.toml")

WEDNESDAY = date(2026, 10, 14)
THURSDAY = date(2026, 10, 15)
NIGHT = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)  # a Thursday, 02:00 PDT
KV_GRID = ("kartaview",)
KV_PAIR = ("kartaview", "kartaview_streets")
MAPILLARY_PAIR = ("mapillary", "mapillary_streets")
LABEL = "the KartaView API (api.kartaview.org)"


@pytest.fixture(autouse=True)
def _night_clock(frozen_utc_clock):
    """Every test starts at 02:00 Pacific on a Thursday, UTC clock frozen."""
    frozen_utc_clock(NIGHT)
    return frozen_utc_clock


def _cfg(
    tmp_path,
    minutes=None,
    *,
    channels=KV_GRID,
    mapillary=False,
    mapillary_rest=False,
    schedule="",
    kv_extra="",
):
    """gsv + the named KartaView channels (+ the Mapillary pair), THROUGH the loader."""
    path = tmp_path / "allowance.toml"
    text = (f"[schedule]\n{schedule}\n" if schedule else "") + (
        "[providers.gsv]\nenabled = true\ndaily_request_budget = 10000000\n\n"
    )
    if mapillary:
        text += "".join(
            f"[providers.{p}]\nenabled = true\ndaily_request_budget = 5000\n"
            "max_requests_per_minute = 40\njitter = 0.6\n\n"
            for p in MAPILLARY_PAIR
        )
    text += "".join(
        f"[providers.{p}]\nenabled = true\ndaily_request_budget = 100000\n"
        "max_requests_per_minute = 1.5\n\n"
        for p in channels
    )
    text += (
        f'[paths]\ndata_dir = "{tmp_path / "data"}"\nlog_dir = "{tmp_path / "logs"}"\n'
        f'backup_dir = "{tmp_path / "backups"}"\n\n'
    )
    if mapillary_rest:
        text += '[hosts.mapillary_tiles]\nrest_weekdays = ["Wed"]\n\n'
    if minutes is not None:
        text += f"[hosts.kartaview]\nmax_night_minutes = {minutes}\n{kv_extra}"
    path.write_text(text)
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


def _kv_only(conn, cfg, name, channels=KV_GRID):
    """A city due ONLY on ``channels``: its gsv run succeeded today."""
    cid = _register(conn, name)
    db.record_attempt(conn, cid, success=True, provider="gsv")
    for channel in channels:
        db.set_channel_membership(conn, cid, channel, True, cycle_days=cfg.cycle_days)
    return cid


def _live(monkeypatch, live):
    """Fake `_sweep_checkpoint_progress`: a live checkpoint for each (city_id, channel) in ``live``.

    ``live`` is kept by reference, so a fake child can clear an entry to model
    a sweep that completes tonight.
    """

    def progress(cfg, city, channel):
        if (city.city_id, channel) in live:
            return {"age_s": 3600.0, "units_done": 1, "unit_count": 2, "unit_name": "roots"}
        return None

    monkeypatch.setattr(sched, "_sweep_checkpoint_progress", progress)
    return live


def _cached(monkeypatch, cached):
    """Fake `channel_census_cache_marker`: a reusable census for each (city_id, channel)."""

    def marker(city, channel, *, max_age_s=None):
        if (city.city_id, channel) in cached:
            return {"fetched_by": "kartaview"}
        return None

    monkeypatch.setattr(sched, "channel_census_cache_marker", marker)
    return cached


def _night(monkeypatch, conn, cfg, *, minutes=None, outcome=None, today=THURSDAY, **kwargs):
    """``cmd_run_due`` with a fake child that moves the clock; returns ``(rc, launches, alerts)``.

    ``minutes(city_id, provider)`` is how long each launch takes; ``outcome``
    its result (default success).
    """
    launches = []
    alerts = []
    minutes = minutes or (lambda cid, provider: 0)
    outcome = outcome or (lambda cid, provider: True)
    lock = threading.Lock()

    def fake(cfg_, city, today_, provider="gsv", **kw):
        with lock:
            launches.append((city.city_id, provider))
            later = clock.utc_now() + timedelta(minutes=minutes(city.city_id, provider))
            clock._utc_clock = lambda: later
        return outcome(city.city_id, provider)

    monkeypatch.setattr(clock, "_utc_clock", clock._utc_clock)  # restored after the test
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


def _messages(caplog, needle, level=None):
    return [
        r.getMessage()
        for r in caplog.records
        if needle in r.getMessage() and (level is None or r.levelno == level)
    ]


def _failures(conn):
    return {
        (r["city_id"], r["provider"]): r["consecutive_failures"]
        for r in conn.execute("SELECT city_id, provider, consecutive_failures FROM schedule_state")
    }


def _city_channels(monkeypatch, conn, cfg, city_id, providers, breaker, *, minutes=0, outcome=None):
    """``_run_city_channels`` for ONE city with a clock-moving fake child.

    Returns ``(launches, counters)``; ``counters`` holds the per-call Counters.
    """
    launches = []
    outcome = outcome or (lambda provider: True)

    def fake(cfg_, city, today_, provider="gsv", **kw):
        launches.append(provider)
        later = clock.utc_now() + timedelta(minutes=minutes)
        clock._utc_clock = lambda: later
        return outcome(provider)

    monkeypatch.setattr(clock, "_utc_clock", clock._utc_clock)
    monkeypatch.setattr(sched, "_run_one_city", fake)
    counters = {
        name: Counter()
        for name in (
            "busy_hosts",
            "deferred_channels",
            "deferred_host_budget",
            "deadline_deferred",
            "ceiling_skipped",
        )
    }
    sched._run_city_channels(
        cfg,
        conn,
        db.resolve_city(conn, city_id),
        THURSDAY,
        list(providers),
        blocked_hosts=breaker,
        rejected_argv=sched.ArgvRejections(),
        batch_deadline=None,
        stop_requested=None,
        **counters,
    )
    return launches, counters


DEAD_AGE_S = sched.CHECKPOINT_MAX_AGE_S + 3600.0  # 7 days + 1 h: discarded by the child


def _aged(monkeypatch, ages):
    """Fake `_sweep_checkpoint_progress`: a kartaview checkpoint at 1/2 root cells per city.

    ``ages`` maps a city id to the checkpoint's ``age_s``; None is a store
    with no first-commit stamp (a KartaView v1 record, #272).
    """

    def progress(cfg, city, channel):
        if channel == "kartaview" and city.city_id in ages:
            return {
                "age_s": ages[city.city_id],
                "units_done": 1,
                "unit_count": 2,
                "unit_name": "root cells",
            }
        return None

    monkeypatch.setattr(sched, "_sweep_checkpoint_progress", progress)


def _kv_estimates(monkeypatch, estimates, default):
    """Fake the kartaview estimate per city id (``default`` otherwise); other channels real."""
    real = sched._channel_estimate
    monkeypatch.setattr(
        sched,
        "_channel_estimate",
        lambda cfg_, city, provider, conn_=None, **kw: (
            estimates.get(city.city_id, default)
            if provider == "kartaview"
            else real(cfg_, city, provider, conn_, **kw)
        ),
    )


def _spent_breaker(cfg, minutes):
    """A breaker whose KartaView allowance (``minutes``) fresh crawls have already spent."""
    holds = sched._host_holds(cfg, THURSDAY)
    holds.charge(HOST_KARTAVIEW, minutes * 60, fresh=True)
    assert holds.allowance(HOST_KARTAVIEW) is not None, "premise: the allowance is spent"
    return sched.HostBreaker(holds=holds)


# ── config ───────────────────────────────────────────────────────────────


def test_a_night_allowance_loads_for_the_kartaview_host(tmp_path):
    """Mutation: `_host_settings` never stores the key (`night_minutes[token] = ...` deleted)."""
    cfg = _cfg(tmp_path, 360)
    assert cfg.host_budget_errors == []
    assert cfg.host_night_minutes == {HOST_KARTAVIEW: 360.0}
    assert sched._host_holds(cfg, THURSDAY).usage(HOST_KARTAVIEW).allowance_s == 360 * 60


@pytest.mark.parametrize(
    "block",
    [
        "[hosts.kartaview]\nmax_night_minutes = 0\n",
        "[hosts.kartaview]\nmax_night_minutes = -5\n",
        "[hosts.kartaview]\nmax_night_minutes = true\n",
        '[hosts.kartaview]\nmax_night_minutes = "360"\n',
        "[hosts.kartaview]\nmax_night_minutes = 1441\n",
        "[hosts.mapillary_tiles]\nmax_night_minutes = 360\n",
        "[hosts.panoramax]\nmax_night_minutes = 360\n",
    ],
    ids=["zero", "negative", "bool", "string", "over-a-day", "tile-cdn", "panoramax"],
)
def test_an_invalid_night_allowance_fails_closed(conn, monkeypatch, tmp_path, caplog, block):
    """Dropping a bad allowance would run the unbounded night (fail-OPEN), so it
    is recorded and run-due/assess-city exit 64 before connecting.
    Mutation: accept `<= 0` (zero, negative) or drop the `_ALLOWANCE_HOSTS` test
    (tile-cdn, panoramax); drop the `_config_refusals` gate from either command."""
    path = tmp_path / "bad.toml"
    path.write_text(
        "[providers.gsv]\nenabled = true\n\n"
        f'[paths]\ndata_dir = "{tmp_path / "data"}"\nlog_dir = "{tmp_path / "logs"}"\n'
        f'backup_dir = "{tmp_path / "backups"}"\n\n' + block
    )
    cfg = load_scheduler_config(str(path))
    assert cfg.host_night_minutes == {}
    assert len(cfg.host_budget_errors) == 1
    assert "max_night_minutes" in cfg.host_budget_errors[0]
    connected = []
    monkeypatch.setattr(sched.db, "connect", lambda path: connected.append(path) or conn)
    # assess-city refuses on the same list (review round 3, finding 8), before
    # it geocodes, registers or connects: one ERROR per command names the key.
    caplog.clear()  # the loader's own ERROR
    with caplog.at_level(logging.ERROR, logger="streetscape_scheduler"):
        assert sched.cmd_run_due(cfg, today=THURSDAY) == sched.USAGE_EXIT_CODE
        assert sched.cmd_assess_city(cfg, "Albany, Oregon", today=THURSDAY) == sched.USAGE_EXIT_CODE
    assert len(_messages(caplog, "max_night_minutes", logging.ERROR)) == 2
    assert connected == []


def test_makelab1_ships_the_allowance_commented_out():
    """The PR ships the mechanism only: prod leaves the key UNSET, and the
    commented-out line carries the derivation for the operator who sets it.
    Mutation: uncomment `max_night_minutes` in config/scheduler.makelab1.toml."""
    cfg = load_scheduler_config(PROD_CONFIG)
    assert cfg.host_budget_errors == []
    assert cfg.host_night_minutes == {}
    assert sched._host_holds(cfg, THURSDAY).allowance_hosts() == set()
    with open(PROD_CONFIG) as fh:
        text = fh.read()
    assert "# [hosts.kartaview]" in text
    assert "# max_night_minutes = " in text


# ── unset is today's behaviour ──────────────────────────────────────────


def test_an_unset_allowance_probes_nothing_and_leaves_the_night_as_it_was(
    conn, monkeypatch, tmp_path, caplog
):
    """With the key unset the launch path reads no checkpoint and no census
    cache it did not read before #464. A Mapillary rest night holds the pair;
    gsv runs. On origin/main this night never calls either probe for a census
    channel (gsv is not resumable, the Mapillary pair is held before pricing),
    so both are patched to RAISE for any census channel, and the launches and
    the Done line's hold clause are the pre-change values, spelled out.
    Mutation: compute the paid-for set unconditionally in `_city_holds`
    (`paid = {h for c in channels for h in CHANNEL_HOSTS[c] if
    _paid_for_on_host(cfg, city, channels, h)}` before the short-circuit)."""

    def boom(*args, **kwargs):
        raise AssertionError("a probe the unset night must not make")

    def marker(city, channel, *, max_age_s=None):
        if channel != "gsv":
            boom()
        return None

    monkeypatch.setattr(sched, "_sweep_checkpoint_progress", boom)
    monkeypatch.setattr(sched, "channel_census_cache_marker", marker)
    cfg = _cfg(tmp_path, None, channels=(), mapillary=True, mapillary_rest=True)
    a = _register(conn, "Albany")
    b = _register(conn, "Bend")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(monkeypatch, conn, cfg, today=WEDNESDAY)
    assert rc == 0 and alerts == []
    assert launches == [(a, "gsv"), (b, "gsv")]
    done = _done_line(caplog)
    assert "2/2 runs succeeded across 2 cities" in done
    assert (
        "HELD Mapillary's tile CDN (tiles.mapillary.com): rest night (Wed), 4 launch(es) held"
        in (done)
    )
    assert "ALLOWANCE" not in done
    # The fill's decline line for a held host is #444's text, byte for byte:
    # with no allowance configured it never names one (review 1, finding 2).
    holds = sched._host_holds(cfg, WEDNESDAY)
    city = db.resolve_city(conn, a)
    refusal = sched._fill_prebudget_refusal(
        cfg, city, ["mapillary"], set(), sched._city_holds(cfg, city, ["mapillary"], holds)[0]
    )
    assert refusal is not None
    assert refusal.message == (
        "Mapillary's tile CDN (tiles.mapillary.com) is held tonight (mapillary): "
        "rest night or cooldown"
    )


def test_an_unset_allowance_with_kartaview_enabled_leaves_the_night_as_it_was(
    conn, monkeypatch, tmp_path, caplog
):
    """Review round 3, finding 8: the unset night WITH both KartaView channels
    enabled -- the shape prod runs -- spelled out as origin/main runs it.
    Albany resumes a live grid checkpoint (its walk defers behind it), Bend is
    a fresh pair, Corvallis's walk reuses a cached census, Dalles is gsv-only;
    every KartaView launch takes 31 minutes, which would spend any allowance
    under the 4 h this night runs. Every allowance helper (#464, #477) is
    patched to RAISE, and the launches, the `Done:` line, the failure counts
    and the absence of any allowance log line are the pre-change values (the
    literals were checked against an origin/main tree before #464 landed).
    The patches are strict (no `raising=False`), so renaming a helper fails
    here loudly instead of leaving a patch that guards nothing.
    Mutation: key `_allowance_charge`'s early return on the channel's hosts
    alone (`if not set(CHANNEL_HOSTS.get(provider, ())) & {HOST_KARTAVIEW}:`),
    so an unset run classes each KartaView launch (`_allowance_checkpoint`
    then raises); or call `_note_night_allowances` without its caller's
    `has_allowance()` gate (the patch raises). `_city_holds` is gated twice
    (`has_allowance()` at the caller and inside), and
    `_stamped_dead_checkpoint` is asked only of a channel a spent allowance
    holds, so no one-line mutation of either reaches this night."""

    def boom(*args, **kwargs):
        raise AssertionError("a #464 helper the unset night must not reach")

    for name in (
        "_paid_for_on_host",
        "_allowance_checkpoint",
        "_stamped_dead_checkpoint",
        "_note_night_allowances",
    ):
        monkeypatch.setattr(sched, name, boom)
    monkeypatch.setattr(sched.HostBreaker, "start_charge", boom)
    cfg = _cfg(tmp_path, None, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", KV_PAIR)
    b = _kv_only(conn, cfg, "Bend", KV_PAIR)
    c = _kv_only(conn, cfg, "Corvallis", ("kartaview_streets",))
    d = _register(conn, "Dalles")
    _live(monkeypatch, {(a, "kartaview")})
    _cached(monkeypatch, {(c, "kartaview_streets")})
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(
            monkeypatch, conn, cfg, minutes=lambda cid, p: 31 if p.startswith("kartaview") else 0
        )
    assert rc == 0 and alerts == []
    assert launches == [
        (a, "kartaview"),
        (b, "kartaview"),
        (b, "kartaview_streets"),
        (c, "kartaview_streets"),
        (d, "gsv"),
    ], launches
    done = re.sub(r" in \d+\.\d+ h;", " in T h;", _done_line(caplog))
    assert done == (
        "Done: run-due 2026-10-15: 5/5 runs succeeded across 4 cities in T h; "
        "1 walk(s) deferred behind a paused sibling sweep (kartaview_streets); "
        "on the KartaView API (api.kartaview.org) 09:00-11:04 UTC (2.1 h)"
    ), done
    assert "ALLOWANCE" not in done and "left for a later night" not in done
    assert _messages(caplog, "night allowance") == []
    assert _messages(caplog, "#464") == []
    assert set(_failures(conn).values()) == {0}


def test_the_fill_names_a_spent_allowance_only_where_it_is_the_reason(conn, monkeypatch, tmp_path):
    """A KartaView host held by its spent allowance is declined as
    "night allowance spent"; the same Mapillary rest night on the same run
    still reads "rest night or cooldown". Mutation: `_fill_held_reason`
    returning one fixed string for both."""
    _live(monkeypatch, set())
    _cached(monkeypatch, set())
    cfg = _cfg(tmp_path, 30, mapillary=True, mapillary_rest=True)
    city = db.resolve_city(conn, _register(conn, "Albany"))
    holds = sched._host_holds(cfg, WEDNESDAY)
    holds.charge(HOST_KARTAVIEW, 30 * 60, fresh=True)
    reading = sched._city_holds(cfg, city, ["mapillary", "kartaview"], holds)[0]
    kv = sched._fill_prebudget_refusal(cfg, city, ["kartaview"], set(), reading)
    mly = sched._fill_prebudget_refusal(cfg, city, ["mapillary"], set(), reading)
    assert kv is not None and kv.message.endswith("(kartaview): night allowance spent")
    assert mly is not None and mly.message.endswith("(mapillary): rest night or cooldown")


# ── the night ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("minutes", [30, None], ids=["allowance", "unset-control"])
def test_a_spent_allowance_leaves_fresh_crawls_due_and_takes_no_slot(
    conn, monkeypatch, tmp_path, caplog, minutes
):
    """A and B are due only on kartaview, C only on gsv; each KartaView launch
    takes 31 min against a 30-minute allowance. No --limit, and the derived
    hoist reservation (5) holds both A and B, so B's position does not depend
    on the cap. B is left for a later night: not launched, no attempt, no
    failure, no alert, still due, and no city counted for it. The unset control
    launches B. Mutation: drop `or self.allowance(host)` from `HostHolds.active`."""
    cfg = _cfg(tmp_path, minutes)
    a = _kv_only(conn, cfg, "Albany")
    b = _kv_only(conn, cfg, "Bend")
    c = _register(conn, "Corvallis")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(
            monkeypatch, conn, cfg, minutes=lambda cid, p: 31 if p == "kartaview" else 0
        )
    assert rc == 0 and alerts == []
    assert (a, "kartaview") in launches and (c, "gsv") in launches
    done = _done_line(caplog)
    if minutes is None:
        assert (b, "kartaview") in launches
        assert "ALLOWANCE" not in done
        return
    assert (b, "kartaview") not in launches
    assert "2/2 runs succeeded across 2 cities" in done
    assert "1 launch(es) left for a later night" in done
    assert len(_messages(caplog, f"{b} [kartaview]: left for a later night")) == 1
    assert len(_messages(caplog, "crawls are left for a later night", logging.WARNING)) == 1
    assert set(_failures(conn).values()) == {0}
    row = conn.execute(
        "SELECT last_attempt_at FROM schedule_state WHERE city_id = ? AND provider = 'kartaview'",
        (b,),
    ).fetchone()
    assert row is None or row["last_attempt_at"] is None
    due = db.get_due_cities(
        conn,
        today=THURSDAY + timedelta(days=1),
        cycle_days=cfg.cycle_days,
        grace_days=cfg.grace_days,
        max_consecutive_failures=cfg.max_consecutive_failures,
        default_membership=sched.CHANNEL_DEFAULT_MEMBERSHIP["kartaview"],
        provider="kartaview",
    )
    assert b in [city.city_id for city in due]


def test_a_spent_allowance_is_never_counted_as_attempted(conn, monkeypatch, tmp_path, caplog):
    """A two-city night: A (KartaView-only, spends the allowance), B
    (KartaView-only, held) and C (gsv-only). `opt_in_cities_per_day = 2`
    hoists both A and B ahead of C (asserted), so C needs the slot B must not
    take. Mutation: `attempted += 1` before the allowance branch's `continue`."""
    cfg = _cfg(tmp_path, 30, schedule="max_cities_per_day = 2\nopt_in_cities_per_day = 2")
    a = _kv_only(conn, cfg, "Albany")
    b = _kv_only(conn, cfg, "Bend")
    c = _register(conn, "Corvallis")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _alerts = _night(
            monkeypatch,
            conn,
            cfg,
            minutes=lambda cid, p: 31 if p == "kartaview" else 0,
        )
    assert rc == 0
    assert launches == [(a, "kartaview"), (c, "gsv")], launches
    assert "hoisted=2 opt-in-only cities" in caplog.text, "premise: B is ahead of C"
    assert f"{b} [kartaview]: left for a later night" in caplog.text


def test_an_allowance_spent_mid_city_never_splits_the_grid_from_its_walk(
    conn, monkeypatch, tmp_path
):
    """The hold is read once per city: A's grid spends the allowance (31 of
    30 min) and A's walk still launches; B, on the same breaker, launches
    nothing. Mutation: `blocked_hosts.holding(wanted)` (per-launch read) in
    place of `holding(wanted, city_holds)`."""
    _live(monkeypatch, set())
    cfg = _cfg(tmp_path, 30, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", KV_PAIR)
    b = _kv_only(conn, cfg, "Bend", KV_PAIR)
    breaker = sched.HostBreaker(holds=sched._host_holds(cfg, THURSDAY))
    launches, _ = _city_channels(monkeypatch, conn, cfg, a, KV_PAIR, breaker, minutes=31)
    assert launches == ["kartaview", "kartaview_streets"]
    assert breaker.holds.allowance(HOST_KARTAVIEW) is not None
    launches, _ = _city_channels(monkeypatch, conn, cfg, b, KV_PAIR, breaker, minutes=31)
    assert launches == []
    assert breaker.allowance_skipped[HOST_KARTAVIEW] == 2


def test_a_resumed_grid_that_completes_is_followed_by_its_free_walk(conn, monkeypatch, tmp_path):
    """Paid-for work, pairing arm: the allowance is spent, A resumes a live
    grid checkpoint, the fake grid child completes (clears the checkpoint,
    promotes the census) and the walk then collects from the cache. Both
    launch; the grid is charged as paid and the cached walk not at all.
    Mutation: `_city_holds` returns `current` without dropping the exempt hosts."""
    cfg = _cfg(tmp_path, 30, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", KV_PAIR)
    live = _live(monkeypatch, {(a, "kartaview")})
    cached = _cached(monkeypatch, set())
    breaker = _spent_breaker(cfg, 30)

    def complete(provider):
        if provider == "kartaview":
            live.discard((a, "kartaview"))
            cached.add((a, "kartaview_streets"))
        return True

    launches, counters = _city_channels(
        monkeypatch, conn, cfg, a, KV_PAIR, breaker, minutes=20, outcome=complete
    )
    assert launches == ["kartaview", "kartaview_streets"]
    usage = breaker.holds.usage(HOST_KARTAVIEW)
    assert (usage.paid_n, usage.fresh_n) == (1, 1), "grid paid; the pre-spend is the one fresh"
    assert usage.paid_s == 20 * 60
    assert breaker.allowance_exempt == {HOST_KARTAVIEW: {a}}
    assert counters["deferred_channels"] == Counter()


def test_a_resumed_grid_that_pauses_still_defers_its_walk(conn, monkeypatch, tmp_path):
    """The pause arm of the same city: the grid exits 83 with its checkpoint
    still live, so the walk defers behind it (#274), exactly as without an
    allowance. Mutation: as above (the grid is then held and nothing launches)."""
    cfg = _cfg(tmp_path, 30, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", KV_PAIR)
    _live(monkeypatch, {(a, "kartaview")})
    _cached(monkeypatch, set())
    breaker = _spent_breaker(cfg, 30)
    paused = sched.CollectionOutcome(False, "exited 83 (stub)", exit_code=83)
    launches, counters = _city_channels(
        monkeypatch, conn, cfg, a, KV_PAIR, breaker, outcome=lambda p: paused
    )
    assert launches == ["kartaview"]
    assert counters["deferred_channels"] == Counter({"kartaview_streets": 1})


@pytest.mark.parametrize("cached_walk", [True, False], ids=["cached-census", "control-neither"])
def test_a_cached_walk_is_exempt_and_a_fresh_one_is_not(conn, monkeypatch, tmp_path, cached_walk):
    """A city due only on the walk whose census is cached collects for 0
    requests past a spent allowance (the stranded-walk retry's case); with
    neither a checkpoint nor a cache it is held. Mutation: drop the
    `channel_census_cache_marker` test from `_paid_for_on_host`."""
    cfg = _cfg(tmp_path, 30, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", ("kartaview_streets",))
    _live(monkeypatch, set())
    _cached(monkeypatch, {(a, "kartaview_streets")} if cached_walk else set())
    breaker = _spent_breaker(cfg, 30)
    launches, _ = _city_channels(monkeypatch, conn, cfg, a, ["kartaview_streets"], breaker)
    assert launches == (["kartaview_streets"] if cached_walk else [])
    # A cached census is not charged at all: only the pre-spend is on the books.
    usage = breaker.holds.usage(HOST_KARTAVIEW)
    assert (usage.fresh_n, usage.paid_n) == (1, 0)


def test_an_unfinishable_city_is_skipped_as_unfinishable_never_held(
    conn, monkeypatch, tmp_path, caplog
):
    """A fresh crawl over the 222-request wall is the #456 skip's (its WARNING
    names the levers, `ceiling_skipped` counts it), not "left for a later
    night": no night can run it. Mutation: drop the `_city_channel_unfinishable`
    test from the allowance branch."""
    cfg = _cfg(tmp_path, 30)
    a = _kv_only(conn, cfg, "Albany")
    _live(monkeypatch, set())
    real = sched._channel_estimate
    monkeypatch.setattr(
        sched,
        "_channel_estimate",
        lambda cfg_, city, provider, conn_=None, **kw: (
            5_000 if provider == "kartaview" else real(cfg_, city, provider, conn_, **kw)
        ),
    )
    breaker = _spent_breaker(cfg, 30)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        launches, counters = _city_channels(monkeypatch, conn, cfg, a, KV_GRID, breaker)
    assert launches == []
    assert counters["ceiling_skipped"] == Counter({"kartaview": 1})
    assert breaker.allowance_skipped[HOST_KARTAVIEW] == 0
    assert len(_messages(caplog, f"{a} [kartaview]", logging.WARNING)) == 1
    assert "left for a later night" not in caplog.text


def test_the_opening_line_is_silent_on_a_run_that_touches_no_kartaview_channel(
    conn, monkeypatch, tmp_path, caplog
):
    """Review 2, nit (kept from the reserve it was written for, issue #477): a
    `--provider mapillary` hand run neither counts nor logs the KartaView
    allowance; a run with kartaview does, and counts slate resumers as cities,
    not channels (a city with live checkpoints on BOTH channels is one).
    Mutations: drop `& run_hosts` from `_note_night_allowances`' host loop;
    count channels (`sum(... for c in ...)`) in place of `any(...)`."""
    cfg = _cfg(tmp_path, 120, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", KV_PAIR)
    _live(monkeypatch, {(a, "kartaview"), (a, "kartaview_streets")})
    city = db.resolve_city(conn, a)
    holds = sched._host_holds(cfg, THURSDAY)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        assert sched._note_night_allowances(cfg, holds, [city], {a: list(KV_PAIR)}, ["gsv"]) == {}
        assert _messages(caplog, "night allowance") == []
        found = sched._note_night_allowances(
            cfg, holds, [city], {a: list(KV_PAIR)}, ["gsv", *KV_PAIR]
        )
    assert found == {HOST_KARTAVIEW: 1}
    line = _messages(caplog, f"{LABEL} night allowance 120 min of launches, resumes included")
    assert len(line) == 1
    assert "1 slate city(ies) resume a live checkpoint there" in line[0]


@pytest.mark.parametrize(
    ("minutes", "warned"), [(720, True), (719, False)], ids=["the-window", "under-it"]
)
def test_an_allowance_as_long_as_the_batch_window_is_warned_as_bounding_nothing(
    conn, tmp_path, caplog, minutes, warned
):
    """Review round 3, finding 5: the loader accepts up to a day (a units-typo
    guard), but a run launches for at most `max_batch_hours`, so an allowance
    at least that long can never be spent. Accepted, and warned once per run
    in the allowance's opening line (`_note_night_allowances`, which took the
    warning over from the reserve in issue #477). Mutation: drop the
    warning's condition (`if False:`)."""
    cfg = _cfg(tmp_path, minutes, schedule="max_batch_hours = 12")
    assert cfg.host_budget_errors == []
    holds = sched._host_holds(cfg, THURSDAY)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        sched._note_night_allowances(cfg, holds, [], {}, ["kartaview"])
    found = _messages(caplog, "so it bounds nothing", logging.WARNING)
    assert len(found) == (1 if warned else 0)
    if warned:
        assert f"{LABEL} night allowance 720 min is at least the 12 h batch window" in found[0]


@pytest.mark.parametrize("est", [400, 20], ids=["unfinishable", "fits"])
@pytest.mark.parametrize(
    "age_s",
    [6.5 * 86400, sched.CHECKPOINT_MAX_AGE_S + 3600.0],
    ids=["inside-the-wall-margin", "past-the-discard"],
)
def test_a_checkpoint_at_the_age_wall_reaches_the_age_wall_arm(
    conn, monkeypatch, tmp_path, caplog, age_s, est
):
    """#468 review round 3, finding 1: a spent allowance never keeps a
    checkpoint from the launch plan's age-wall arm, which records the failure
    that feeds the alert and the quarantine. Held before the arm, a
    checkpoint the arm would refuse is "left for a later night" every night
    the allowance is spent, and nothing alerts.
    Checkpoint at 1/2 roots, 6.5 days (inside the arm's one-night margin, not
    yet discarded) or 7 days + 1 h (past `CHECKPOINT_MAX_AGE_S`, discarded by
    the child). Unfinishable (~200 requests left against one 60-minute launch
    at 1.5/min): at BOTH ages the arm refuses it and records a
    consecutive_failure. Fits (~10 left): at 6.5 days the city is exempt and
    resumes, charged as PAID. Past the discard (issue #477) the checkpoint is
    dead, so the city is NOT exempt: it falls through to the arm, which
    refuses the unfinishable one, and the one that fits is then held as the
    fresh crawl it is (was: launched, charged fresh).
    Mutations: drop `_stamped_dead_checkpoint` from the held branch's
    fallthrough -- past-the-discard/unfinishable is held, no failure; restore
    `_paid_for_on_host`'s any-readable-checkpoint test -- both past-the-discard
    arms are exempt again (fits launches)."""
    cfg = _cfg(tmp_path, 30)
    a = _kv_only(conn, cfg, "Albany")
    _cached(monkeypatch, set())
    monkeypatch.setattr(
        sched,
        "_sweep_checkpoint_progress",
        lambda cfg_, city, channel: (
            {"age_s": age_s, "units_done": 1, "unit_count": 2, "unit_name": "root cells"}
            if channel == "kartaview"
            else None
        ),
    )
    real = sched._channel_estimate
    monkeypatch.setattr(
        sched,
        "_channel_estimate",
        lambda cfg_, city, provider, conn_=None, **kw: (
            est if provider == "kartaview" else real(cfg_, city, provider, conn_, **kw)
        ),
    )
    breaker = _spent_breaker(cfg, 30)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        launches, _ = _city_channels(monkeypatch, conn, cfg, a, KV_GRID, breaker, minutes=25)
    live = age_s < sched.CHECKPOINT_MAX_AGE_S
    assert breaker.allowance_exempt == ({HOST_KARTAVIEW: {a}} if live else {})
    usage = breaker.holds.usage(HOST_KARTAVIEW)
    if est == 400:
        assert launches == []
        assert _failures(conn)[(a, "kartaview")] == 1
        assert len(_messages(caplog, "refusing to resume", logging.WARNING)) == 1
        assert breaker.allowance_skipped[HOST_KARTAVIEW] == 0
        assert "left for a later night" not in caplog.text
        assert (usage.fresh_n, usage.paid_n) == (1, 0)  # the pre-spend alone
        return
    assert _failures(conn)[(a, "kartaview")] == 0
    if live:
        assert launches == ["kartaview"]
        assert breaker.allowance_skipped[HOST_KARTAVIEW] == 0
        assert "left for a later night" not in caplog.text
        assert (usage.fresh_n, usage.paid_n) == (1, 1)
    else:
        assert launches == []
        assert breaker.allowance_skipped[HOST_KARTAVIEW] == 1
        assert len(_messages(caplog, f"{a} [kartaview]: left for a later night")) == 1
        assert (usage.fresh_n, usage.paid_n) == (1, 0)


def test_a_resume_spends_the_allowance_with_the_fresh_crawls(conn, monkeypatch, tmp_path, caplog):
    """Issue #477 (rewritten from #464's `..._does_not_spend_the_fresh_allowance`,
    whose contract this inverts): resume minutes COUNT toward the hold. 60 min,
    one resumer (Albany) leading the slate (asserted), three fresh cities, 25
    min a launch. Albany 25 + Bend 25 + Corvallis 25 = 75 >= 60 after
    Corvallis, so exactly two fresh cities run and Dalles is left.
    Mutations, one per direction: the hold comparing `fresh_s` alone (or a
    resume not charged at all, `_allowance_charge` returning None for it) lets
    all three fresh cities run; any reserve carved out of the 60 (a resume's
    ~24.7 min) holds Corvallis too, so only one runs."""
    cfg = _cfg(tmp_path, 60)
    a = _kv_only(conn, cfg, "Albany")
    fresh = [_kv_only(conn, cfg, name) for name in ("Bend", "Corvallis", "Dalles")]
    _live(monkeypatch, {(a, "kartaview")})
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 25)
    assert rc == 0
    assert launches[0] == (a, "kartaview"), "premise: the resumer leads the slate"
    assert [cid for cid in fresh if (cid, "kartaview") in launches] == fresh[:2], launches
    done = _done_line(caplog)
    assert "75 of 60 min in launches, 15 min over the allowance" in done
    assert "1 launch(es) left for a later night" in done


def test_an_unhoisted_resumer_still_runs_behind_a_held_fresh_city(
    conn, monkeypatch, tmp_path, caplog
):
    """Rewritten for issue #477 (was `..._when_the_reserve_fills_the_allowance`):
    an exempt resumer BEHIND a held fresh city still runs. A 10-minute
    allowance; at --limit 4 the opt-in reservation is 1, so the first resumer
    (Albany) leads, then the fresh city (Bend), then the second resumer
    (Corvallis), asserted from the log order. Albany's 25-minute resume spends
    the allowance, Bend is left, and Corvallis is exempt and runs.
    Mutation: `_city_holds` returns `current` without dropping the exempt hosts."""
    cfg = _cfg(tmp_path, 10)
    r1 = _kv_only(conn, cfg, "Albany")
    f = _kv_only(conn, cfg, "Bend")
    r2 = _kv_only(conn, cfg, "Corvallis")
    _live(monkeypatch, {(r1, "kartaview"), (r2, "kartaview")})
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 25, limit=4)
    assert rc == 0
    assert "opt_in_cities_per_day=1 reserved" in caplog.text
    left = caplog.text.index(f"{f} [kartaview]: left for a later night")
    assert caplog.text.index(f"{r2}: {LABEL} night allowance is spent") > left, (
        "premise: the second resumer comes after the fresh city"
    )
    assert launches == [(r1, "kartaview"), (r2, "kartaview")], launches
    # A spent allowance is never announced as a whole-run hold.
    assert _messages(caplog, "for this run", logging.WARNING) == []
    assert "1 city(ies) exempt with a checkpoint or cached census there" in _done_line(caplog)


def _resumers_spend_the_night(conn, monkeypatch, tmp_path, caplog):
    """Issue #477's resume-first night: 40 min; Albany and Bend resume live
    checkpoints first (25 min each, 50 >= 40 on Bend's PAID charge),
    Corvallis is fresh, Dalles resumes after it. Union order, asserted."""
    cfg = _cfg(tmp_path, 40)
    a, b, c, d = (_kv_only(conn, cfg, n) for n in ("Albany", "Bend", "Corvallis", "Dalles"))
    _live(monkeypatch, {(a, "kartaview"), (b, "kartaview"), (d, "kartaview")})
    _cached(monkeypatch, set())
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 25)
    assert rc == 0 and alerts == []
    return (a, b, c, d), launches


def test_resumes_that_run_first_spend_the_allowance_and_a_later_resumer_still_runs(
    conn, monkeypatch, tmp_path, caplog
):
    """Issue #477, a whole night: two resumes use the 40 minutes up, so the
    fresh city behind them is held, and the resumer behind THAT still runs
    (exempt). Mutations: the hold comparing `fresh_s` alone (Corvallis
    launches); `_paid_for_on_host` never exempting (Dalles is held)."""
    (a, b, c, d), launches = _resumers_spend_the_night(conn, monkeypatch, tmp_path, caplog)
    assert launches == [(a, "kartaview"), (b, "kartaview"), (d, "kartaview")], launches
    assert len(_messages(caplog, f"{c} [kartaview]: left for a later night")) == 1
    done = _done_line(caplog)
    assert "75 of 40 min in launches, 35 min over the allowance" in done
    assert "(fresh 0 min over 0 launch(es); resumes 75 min over 3)" in done
    assert "1 launch(es) left for a later night, 1 city(ies) exempt" in done
    assert set(_failures(conn).values()) == {0}


def test_a_paid_charge_that_crosses_the_allowance_logs_the_crossing_once(
    conn, monkeypatch, tmp_path, caplog
):
    """Issue #477: the same night, where a RESUME (Bend's) is the charge that
    spends the allowance. The crossing WARNING is logged once, on that charge,
    and not again on Dalles's later one. Mutations: `charge` returning
    `fresh and not was_spent and ...` (no WARNING at all); dropping
    `not was_spent` (a second WARNING on Dalles)."""
    _resumers_spend_the_night(conn, monkeypatch, tmp_path, caplog)
    found = _messages(caplog, "crawls are left for a later night", logging.WARNING)
    assert len(found) == 1, found
    assert f"{LABEL}: night allowance spent (launches used 50 of 40 min" in found[0]


def test_the_done_clause_reports_the_minutes_over_the_allowance(
    conn, monkeypatch, tmp_path, caplog
):
    """Issue #477: 50 min; Albany resumes (20), Bend fresh (30) spends it
    EXACTLY (50 of 50), Corvallis resumes after it (25, exempt), Dalles fresh
    is left. "Over the allowance" is the 25 minutes Corvallis ran past it.
    Mutations: computing it from `fresh_s` alone (0), or as `paid_s` (45)."""
    cfg = _cfg(tmp_path, 50)
    a, b, c, d = (_kv_only(conn, cfg, n) for n in ("Albany", "Bend", "Corvallis", "Dalles"))
    _live(monkeypatch, {(a, "kartaview"), (c, "kartaview")})
    _cached(monkeypatch, set())
    times = {a: 20, b: 30, c: 25}
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: times.get(cid, 31))
    assert rc == 0
    assert launches == [(a, "kartaview"), (b, "kartaview"), (c, "kartaview")], launches
    assert (
        f"; ALLOWANCE {LABEL}: 75 of 50 min in launches, 25 min over the allowance (fresh "
        f"30 min over 1 launch(es); resumes 45 min over 2), 1 launch(es) left for a later "
        f"night, 1 city(ies) exempt with a checkpoint or cached census there, cities stay due"
    ) in _done_line(caplog)


@pytest.mark.parametrize("spent", [True, False], ids=["spent", "not-spent"])
def test_a_dead_checkpoint_that_fits_is_held_as_the_fresh_crawl_it_is(
    conn, monkeypatch, tmp_path, caplog, spent
):
    """Issue #477, a whole night: Bend's checkpoint is 7 days + 1 h old, so the
    child would crawl from zero, and its ~10 remaining requests fit one
    launch. Albany (fresh, no checkpoint) runs first: 31 min spends the 30,
    5 min does not. Spent: Bend falls through to the launch plan, which lets
    it through, and is then HELD as the fresh crawl it is (was: launched past
    the spent allowance) -- no attempt, no failure. Not spent: it launches and
    is charged FRESH. Mutations: `_paid_for_on_host` taking any readable
    checkpoint again (spent: Bend launches); `_allowance_charge` testing
    `progress is not None` (not spent: Bend charged as a resume)."""
    cfg = _cfg(tmp_path, 30)
    a = _kv_only(conn, cfg, "Albany")
    b = _kv_only(conn, cfg, "Bend")
    _aged(monkeypatch, {b: DEAD_AGE_S})
    _cached(monkeypatch, set())
    _kv_estimates(monkeypatch, {}, 20)
    times = {a: 31 if spent else 5, b: 25}
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: times[cid])
    assert rc == 0 and alerts == []
    assert set(_failures(conn).values()) == {0}
    done = _done_line(caplog)
    if spent:
        assert launches == [(a, "kartaview")], launches
        assert (
            len(_messages(caplog, f"{b} [kartaview]: left for a later night — night allowance"))
            == 1
        )
        assert "1 launch(es) left for a later night, 0 city(ies) exempt" in done
        return
    assert launches == [(a, "kartaview"), (b, "kartaview")], launches
    assert "(fresh 30 min over 2 launch(es); resumes 0 min over 0)" in done


@pytest.mark.parametrize("spent", [True, False], ids=["spent", "not-spent"])
def test_a_held_dead_checkpoint_never_logs_that_it_is_launching_capped(
    conn, monkeypatch, tmp_path, caplog, spent
):
    """Round-1 review, finding 3: Bend's checkpoint is 7 days + 1 h old, half
    done, ~60 requests estimated, so the age-wall arm lets it through (~30
    left fit tonight) and the plan says it is "launching capped" (60 is over
    one launch's cap). Spent, the allowance then holds it, and only "left for
    a later night" may be logged: the plan's line would claim a launch that
    never happens. Not spent, it launches and the same line IS logged, which
    is what keeps the spent arm from passing on a message that never exists.
    Mutation: drop `and not allowance_fallthrough` from the plan-message log
    in `_run_city_channels` (spent: both lines are logged)."""
    cfg = _cfg(tmp_path, 30)
    a = _kv_only(conn, cfg, "Albany")
    b = _kv_only(conn, cfg, "Bend")
    _aged(monkeypatch, {b: DEAD_AGE_S})
    _cached(monkeypatch, set())
    _kv_estimates(monkeypatch, {b: 60}, 20)
    times = {a: 31 if spent else 5, b: 25}
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: times[cid])
    assert rc == 0 and alerts == []
    assert set(_failures(conn).values()) == {0}
    capped = _messages(caplog, f"{b} [kartaview]: ~60 estimated requests exceeds")
    left = _messages(caplog, f"{b} [kartaview]: left for a later night")
    if spent:
        assert launches == [(a, "kartaview")], launches
        assert len(left) == 1
        assert capped == []
        return
    assert launches == [(a, "kartaview"), (b, "kartaview")], launches
    assert left == []
    assert len(capped) == 1 and "launching capped" in capped[0]


def test_a_dead_checkpoint_that_cannot_finish_records_the_age_wall_failure(
    conn, monkeypatch, tmp_path, caplog
):
    """Issue #477, guarding #468 review round 3, finding 1, through a whole
    night: Albany spends the 30 minutes; Bend's checkpoint is 7 days + 1 h old
    with ~200 requests left, which one 60-minute launch cannot finish. Not
    exempt, but the held branch lets it fall through, so the age-wall arm
    refuses it and records the consecutive_failure that feeds the alert and
    the quarantine; it is never called "left for a later night". Mutation:
    drop `_stamped_dead_checkpoint` from the held branch's fallthrough (Bend is
    held silently, no failure)."""
    cfg = _cfg(tmp_path, 30)
    a = _kv_only(conn, cfg, "Albany")
    b = _kv_only(conn, cfg, "Bend")
    _aged(monkeypatch, {b: DEAD_AGE_S})
    _cached(monkeypatch, set())
    _kv_estimates(monkeypatch, {b: 400}, 20)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 31)
    assert launches == [(a, "kartaview")], launches
    assert _failures(conn)[(b, "kartaview")] == 1
    assert len(_messages(caplog, f"{b} [kartaview]", logging.WARNING)) == 1
    assert "refusing to resume" in _messages(caplog, f"{b} [kartaview]", logging.WARNING)[0]
    assert _messages(caplog, f"{b} [kartaview]: left for a later night") == []
    assert "0 launch(es) left for a later night, 0 city(ies) exempt" in _done_line(caplog)


@pytest.mark.parametrize("spent", [True, False], ids=["spent", "not-spent"])
def test_a_checkpoint_with_no_first_commit_stamp_is_held_not_crashed(
    conn, monkeypatch, tmp_path, caplog, spent
):
    """#468 review round 3, finding 1, and issue #477, a whole night:
    `sweep_progress` reports `age_s = None` for a store with no `created_at`
    (a KartaView v1 record, #272, which the child discards). The age test once
    read `progress.get("age_s", 0.0)`, None for a PRESENT key, and raised
    TypeError in the city loop. The child crawls it from zero and the
    age-wall arm cannot judge it, so it is not exempt and does not fall
    through: with the allowance spent it is held outright (was: exempt and
    launched); unspent, it launches and is charged FRESH.
    Mutations: drop `age_s is not None and` from `_allowance_checkpoint`
    (TypeError); `_paid_for_on_host` taking any readable checkpoint (spent:
    Bend launches)."""
    cfg = _cfg(tmp_path, 30)
    a = _kv_only(conn, cfg, "Albany")
    b = _kv_only(conn, cfg, "Bend")
    _aged(monkeypatch, {b: None})
    _cached(monkeypatch, set())
    _kv_estimates(monkeypatch, {}, 20)
    city = db.resolve_city(conn, b)
    assert sched._allowance_checkpoint(cfg, city, "kartaview") is False
    assert sched._stamped_dead_checkpoint(cfg, city, "kartaview") is False
    times = {a: 31 if spent else 5, b: 25}
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, alerts = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: times[cid])
    assert rc == 0 and alerts == []
    assert set(_failures(conn).values()) == {0}
    done = _done_line(caplog)
    if spent:
        assert launches == [(a, "kartaview")], launches
        assert len(_messages(caplog, f"{b} [kartaview]: left for a later night")) == 1
        assert "0 city(ies) exempt" in done
        return
    assert launches == [(a, "kartaview"), (b, "kartaview")], launches
    assert "(fresh 30 min over 2 launch(es); resumes 0 min over 0)" in done


def test_the_dry_run_and_the_night_hold_the_same_cities(
    conn, monkeypatch, tmp_path, capsys, caplog
):
    """Issue #477: one slate, previewed and then run, with every launch's
    minutes in the night set to its requests at the configured 1.5/min (30
    requests = 20 min), so the preview charges what the night does. 50 min:
    Albany resumes (live), Bend and Corvallis are fresh (60 >= 50 after
    Corvallis), Dalles is fresh, Eugene has a dead checkpoint that fits,
    Florence a dead checkpoint the age wall refuses (400 requests).
    Both: Albany, Bend and Corvallis run; Dalles and Eugene are held and take
    no slot; Florence is the age-wall arm's (a recorded failure live, its skip
    label in the preview, never HELD). `opt_in_cities_per_day = 10` keeps the
    union order (no resumer-first choice). Mutations: the preview charging
    fresh crawls only (Dalles priced); dropping the preview's post-plan hold
    of a fallthrough (Eugene priced); dropping `_stamped_dead_checkpoint` from
    the preview's `_held_by` (Florence HELD)."""
    cfg = _cfg(tmp_path, 50, schedule="max_cities_per_day = 20\nopt_in_cities_per_day = 10")
    names = ("Albany", "Bend", "Corvallis", "Dalles", "Eugene", "Florence")
    a, b, c, d, e, f = (_kv_only(conn, cfg, n) for n in names)
    _aged(monkeypatch, {a: 3600.0, e: DEAD_AGE_S, f: DEAD_AGE_S})
    _cached(monkeypatch, set())
    _kv_estimates(monkeypatch, {f: 400}, 30)

    rc, launches, _ = _night(monkeypatch, conn, cfg, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0 and launches == []
    rows = {}
    for ln in out.splitlines():
        parts = ln.split()
        if ln.startswith("  ") and len(parts) > 2 and parts[0] in (a, b, c, d, e, f):
            rows[parts[0]] = ln
    priced = {cid for cid, ln in rows.items() if " req  ok" in ln}
    held = {cid for cid, ln in rows.items() if "HELD (night allowance spent)" in ln}
    assert priced == {a, b, c}, out
    assert held == {e}, out
    assert d not in rows, out
    assert f in rows and "HELD" not in rows[f] and " req  " in rows[f], out
    assert "(2 due city(ies) left for a later night by the spent night allowance" in out

    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 20)
    assert {cid for cid, _p in launches} == priced, launches
    left = {
        cid
        for cid in (a, b, c, d, e, f)
        if _messages(caplog, f"{cid} [kartaview]: left for a later night")
    }
    assert left == {d, e}
    assert _failures(conn)[(f, "kartaview")] == 1
    assert "60 of 50 min in launches, 10 min over the allowance" in _done_line(caplog)


def test_the_done_clause_carries_the_numbers_and_never_the_held_token(
    conn, monkeypatch, tmp_path, caplog
):
    """40 min, Albany resumes (25 min), Bend fresh (31 min) spends it at 56,
    Corvallis left. The clause states measured launch minutes, the minutes
    over the allowance (issue #477), the fresh and paid shares and the counts;
    it never says HELD, and the
    Mapillary night report's HELD regex reads none of it. `breaker.held` stays
    empty. Mutation: record allowance holds in `held` (`self.held.setdefault`
    for every hold in `holding`)."""
    from scripts import mapillary_night_report as report

    cfg = _cfg(tmp_path, 40)
    a = _kv_only(conn, cfg, "Albany")
    _kv_only(conn, cfg, "Bend")
    _kv_only(conn, cfg, "Corvallis")
    _live(monkeypatch, {(a, "kartaview")})
    times = {a: 25}
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _night(monkeypatch, conn, cfg, minutes=lambda cid, p: times.get(cid, 31))
    done = _done_line(caplog)
    assert (
        f"; ALLOWANCE {LABEL}: 56 of 40 min in launches, 16 min over the allowance (fresh "
        f"31 min over 1 launch(es); resumes 25 min over 1), 1 launch(es) left for a later "
        f"night, 0 city(ies) exempt with a checkpoint or cached census there, cities stay due"
    ) in done
    assert "HELD" not in done
    assert report._HELD_RE.search(done) is None


def test_a_rest_night_beside_a_spent_allowance_keeps_the_held_count_mapillarys(
    conn, monkeypatch, tmp_path, caplog
):
    """On a Mapillary rest night with the KartaView allowance also spent, the
    HELD clause's launch count is the Mapillary launches alone (two cities x two
    channels), so the #444 rest-night series is not corrupted. Mutation: count
    an allowance skip in `held_launches`."""
    from scripts import mapillary_night_report as report

    cfg = _cfg(tmp_path, 30, mapillary=True, mapillary_rest=True)
    a = _register(conn, "Albany")
    b = _register(conn, "Bend")
    for cid in (a, b):
        db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=cfg.cycle_days)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(
            monkeypatch,
            conn,
            cfg,
            today=WEDNESDAY,
            minutes=lambda cid, p: 31 if p == "kartaview" else 0,
        )
    assert rc == 0
    assert [p for _c, p in launches].count("kartaview") == 1
    done = _done_line(caplog)
    match = report._HELD_RE.search(done)
    assert match is not None and int(match["n"]) == 4, done
    assert "1 launch(es) left for a later night" in done


@pytest.mark.parametrize("hold", ["rest", "cooldown"])
def test_a_kartaview_rest_or_cooldown_outranks_its_spent_allowance(
    conn, monkeypatch, tmp_path, hold
):
    """Review 1, finding 1: on ONE host, a rest night or a refusal cooldown
    outranks a spent allowance. The allowance here is spent by an earlier
    charge (10 of 10 min), so its hold is in force too. Were the
    allowance reported first, `_city_holds` would exempt the live checkpoint and
    launch KartaView on its rest night or inside its cooldown. Nothing
    launches, the launch is counted as a #444 hold, and no city is exempt.
    Mutation: `HostHolds.active` returning `allowance or rest or cooldown`."""
    extra = 'rest_weekdays = ["Thu"]\n' if hold == "rest" else "refusal_cooldown_h = 36\n"
    cfg = _cfg(tmp_path, 10, kv_extra=extra)
    a = _kv_only(conn, cfg, "Albany")
    _live(monkeypatch, {(a, "kartaview")})
    _cached(monkeypatch, set())
    if hold == "cooldown":
        db.record_host_verdict(
            conn,
            HOST_KARTAVIEW,
            db.HOST_REFUSED,
            source="run-due",
            channel="kartaview",
            at=NIGHT - timedelta(hours=1),
        )
    holds = sched._host_holds(cfg, THURSDAY, conn)
    holds.charge(HOST_KARTAVIEW, 10 * 60, fresh=True)
    assert holds.allowance(HOST_KARTAVIEW) is not None, "premise: the allowance is spent"
    reason = sched.HOLD_REST if hold == "rest" else sched.HOLD_COOLDOWN
    assert holds.active(HOST_KARTAVIEW).reason == reason
    breaker = sched.HostBreaker(holds=holds)
    launches, _ = _city_channels(monkeypatch, conn, cfg, a, KV_GRID, breaker)
    assert launches == []
    assert breaker.held_launches == 1
    assert breaker.allowance_exempt == {}
    assert breaker.allowance_skipped[HOST_KARTAVIEW] == 0


def test_an_exempt_city_run_twice_is_counted_once(conn, monkeypatch, tmp_path):
    """Review 1, finding 5: the Done clause counts CITIES exempt as paid-for
    work, so a city the due loop ran and the #380 retry or the fill runs again
    is one city, not two. Mutation: a list (`setdefault(host, []).append`) in
    place of the set."""
    cfg = _cfg(tmp_path, 30, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", ("kartaview_streets",))
    _live(monkeypatch, set())
    _cached(monkeypatch, {(a, "kartaview_streets")})
    breaker = _spent_breaker(cfg, 30)
    for _ in range(2):
        launches, _ = _city_channels(monkeypatch, conn, cfg, a, ["kartaview_streets"], breaker)
        assert launches == ["kartaview_streets"]
    assert breaker.allowance_exempt == {HOST_KARTAVIEW: {a}}
    assert (
        "1 city(ies) exempt with a checkpoint or cached census there"
        in sched._allowance_summary_note(breaker)
    )


def test_a_launch_on_the_pool_path_is_charged(conn, monkeypatch, tmp_path, caplog):
    """Prod runs two lanes, so every launch goes through the pool and is charged
    when its future is classified on the main thread. Bend (KartaView-only)
    leads the slate and its 31-minute launch spends the 30; Albany's gsv still
    runs and its kartaview is left. Mutation: delete the `end_charge` call in
    the completion pass."""
    cfg = _cfg(tmp_path, 30, schedule="max_concurrent_channels = 2")
    assert cfg.max_concurrent_channels == 2
    a = _register(conn, "Albany")
    db.set_channel_membership(conn, a, "kartaview", True, cycle_days=cfg.cycle_days)
    b = _kv_only(conn, cfg, "Bend")
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(
            monkeypatch, conn, cfg, minutes=lambda cid, p: 31 if p == "kartaview" else 0
        )
    assert rc == 0
    assert launches[0] == (b, "kartaview"), "premise: the KartaView-only city leads"
    assert (a, "gsv") in launches
    assert (a, "kartaview") not in launches, launches
    assert "31 of 30 min in launches, 1 min over the allowance (fresh 31 min over 1 launch(es)" in (
        _done_line(caplog)
    )


def test_the_stranded_walk_retry_is_exempt_through_the_retry_pass(conn, monkeypatch, tmp_path):
    """#380: a KartaView walk stranded tonight had its grid land, so its census
    is cached; the end-of-night retry, driven through `_retry_stranded_walks`
    itself, collects it past a spent allowance. Mutation: drop the
    `channel_census_cache_marker` test from `_paid_for_on_host`."""
    cfg = _cfg(tmp_path, 30, channels=KV_PAIR)
    a = _kv_only(conn, cfg, "Albany", ("kartaview_streets",))
    _live(monkeypatch, set())
    _cached(monkeypatch, {(a, "kartaview_streets")})
    breaker = _spent_breaker(cfg, 30)
    breaker.strand(a, "kartaview_streets")
    launches = []
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg_, city, today_, provider="gsv", **kw: launches.append(provider) or True,
    )
    tally = Counter()
    stop = sched._retry_stranded_walks(
        cfg,
        conn,
        THURSDAY,
        {a: db.resolve_city(conn, a)},
        blocked_hosts=breaker,
        busy_hosts=Counter(),
        deferred_channels=Counter(),
        deferred_host_budget=Counter(),
        rejected_argv=sched.ArgvRejections(),
        deadline_deferred=Counter(),
        ceiling_skipped=Counter(),
        batch_deadline=time.monotonic() + 3600,
        sigterm_seen=threading.Event(),
        tally=tally,
    )
    assert stop is None
    assert launches == ["kartaview_streets"]
    assert breaker.stranded_recovered == 1


# ── the dry run, status, the override ────────────────────────────────────


def test_the_dry_run_holds_what_the_night_would_hold(conn, monkeypatch, tmp_path, capsys):
    """A 1-minute allowance: the preview charges its own first fresh row at the
    configured pace, then passes over the next two KartaView-only cities (held
    on every channel, so no row and no slot) and prints the gsv+kartaview
    city's kartaview row HELD (night allowance spent). Mutations: drop the
    preview's `holds.charge(...)` call; count every passed-over city under
    #444's HELD line (`if False:` for the allowance-only test)."""
    cfg = _cfg(tmp_path, 1)
    kv = [_kv_only(conn, cfg, name) for name in ("Albany", "Bend", "Corvallis")]
    d = _register(conn, "Dalles")
    db.set_channel_membership(conn, d, "kartaview", True, cycle_days=cfg.cycle_days)
    rc, launches, _ = _night(monkeypatch, conn, cfg, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0 and launches == []
    assert (
        f"  ALLOWANCE — {LABEL}: 1 min of launches, resumes included, before fresh crawls "
        f"are held; 0 slate city(ies) resume"
    ) in out
    rows = [ln.split() for ln in out.splitlines() if ln.startswith("  ") and "--" in ln]
    assert [r[0] for r in rows if r[0] in kv] == [kv[0]], out
    assert [r[:2] for r in rows if r[0] == d] == [[d, "gsv"], [d, "kartaview"]], out
    assert [ln for ln in out.splitlines() if "HELD (night allowance spent)" in ln] == [
        ln for ln in out.splitlines() if ln.startswith(f"  {d}") and "kartaview" in ln
    ]
    # Counted as #464's, never under #444's HELD pass-over (review round 3, finding 4).
    assert "(2 due city(ies) left for a later night by the spent night allowance" in out
    assert "HELD on every channel passed over" not in out


def test_the_dry_run_never_lists_a_spent_allowance_as_held_tonight(
    conn, monkeypatch, tmp_path, capsys
):
    """Review 2, nit: the preview's `HELD tonight` lines are #444's rest
    nights and cooldowns; the allowance has its own ALLOWANCE line. Since
    issue #477 no run starts with its allowance spent (there is no reserve),
    so the filter is defensive and `_host_holds` is wrapped here to hand the
    run an allowance already spent. Mutation: drop
    `hold.reason != HOLD_ALLOWANCE` from the `HELD tonight` filter."""
    cfg = _cfg(tmp_path, 10)
    r = _kv_only(conn, cfg, "Albany")
    _kv_only(conn, cfg, "Bend")
    _live(monkeypatch, {(r, "kartaview")})
    real = sched._host_holds

    def spent(cfg_, today, conn_=None, *, ignore=False):
        holds = real(cfg_, today, conn_, ignore=ignore)
        holds.charge(HOST_KARTAVIEW, 10 * 60, fresh=True)
        return holds

    monkeypatch.setattr(sched, "_host_holds", spent)
    rc, _launches, _ = _night(monkeypatch, conn, cfg, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert f"  ALLOWANCE — {LABEL}: 10 min of launches, resumes included" in out
    assert "1 slate city(ies) resume a live checkpoint there" in out
    assert "HELD tonight" not in out


def test_status_names_the_night_allowance(conn, tmp_path):
    """Mutation: delete the `host_night_minutes` loop in `_host_hold_status_lines`."""
    lines = sched._host_hold_status_lines(_cfg(tmp_path, 360), conn, THURSDAY)
    assert lines == [
        f"{LABEL} night allowance: 360 min of launches per run-due, resumes included, "
        f"before fresh crawls are held; live checkpoints and cached censuses are never "
        f"held (issues #464, #477). Usage is on each night's Done: line."
    ]
    assert sched._host_hold_status_lines(_cfg(tmp_path, None), conn, THURSDAY) == []


def test_ignore_host_holds_lifts_the_night_allowance(conn, monkeypatch, tmp_path, caplog):
    """A 10-minute allowance, spent by the resumer's 25-minute launch, holds
    the fresh city behind it; `--ignore-host-holds` lifts it, every city
    launches, and the WARNING names the allowance. The control arm (no
    override) leaves the fresh city. Mutation: `_host_holds(ignore=True)`
    returning an instance that keeps the allowances."""
    cfg = _cfg(tmp_path, 10)
    r = _kv_only(conn, cfg, "Albany")
    f = _kv_only(conn, cfg, "Bend")
    _live(monkeypatch, {(r, "kartaview")})
    rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 25)
    assert rc == 0 and (f, "kartaview") not in launches
    conn.execute("DELETE FROM schedule_state WHERE provider = 'kartaview' AND city_id = ?", (r,))
    db.set_channel_membership(conn, r, "kartaview", True, cycle_days=cfg.cycle_days)
    with caplog.at_level(logging.WARNING, logger="streetscape_scheduler"):
        rc, launches, _ = _night(
            monkeypatch, conn, cfg, minutes=lambda cid, p: 25, ignore_host_holds=True
        )
    assert rc == 0
    assert {(r, "kartaview"), (f, "kartaview")} <= set(launches)
    warning = _messages(caplog, "--ignore-host-holds", logging.WARNING)
    assert len(warning) == 1
    assert f"the night allowance ({LABEL} 10 min) is lifted too" in warning[0]
    # Review round 3, finding 2: the WARNING names the narrow form, since a full
    # run also lifts the tile CDN's rest night and refusal cooldown.
    assert "run only `--provider kartaview,kartaview_streets`, never a full run" in warning[0]
