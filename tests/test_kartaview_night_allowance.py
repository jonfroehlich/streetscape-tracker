"""
The KartaView night allowance (issue #464): ``[hosts.kartaview].max_night_minutes``.

An optional bound on the minutes one ``run-due`` spends starting FRESH
KartaView crawls. Tonight's resumes of live checkpoints are reserved out of it
first and never held; a city that is paid-for work on the host (a live
checkpoint, a cached census) is exempt from a spent allowance. A held launch
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
    assert sched._host_holds(cfg, THURSDAY).fresh_allowance_s(HOST_KARTAVIEW) == 360 * 60


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
def test_an_invalid_night_allowance_fails_closed(conn, monkeypatch, tmp_path, block):
    """Dropping a bad allowance would run the unbounded night (fail-OPEN), so it
    is recorded and run-due/assess-city exit 64 before connecting.
    Mutation: accept `<= 0` (zero, negative) or drop the `_ALLOWANCE_HOSTS` test
    (tile-cdn, panoramax)."""
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
    assert sched.cmd_run_due(cfg, today=THURSDAY) == sched.USAGE_EXIT_CODE
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


def test_resumes_are_reserved_out_of_the_allowance_first(conn, monkeypatch, tmp_path, caplog):
    """30 min at 1.5/min, one resumer (Bend): ~24.7 min reserved, ~5.3 left for
    fresh crawls. Every launch takes 25 min. Bend runs paid; one fresh city
    runs and spends the 5.3; the other fresh city is left. Holds whatever order
    the hoist gives the three (all three fit its derived reservation of 5).
    Mutation: skip `_reserve_allowance_resumes` in cmd_run_due (fresh then has
    30, and both fresh cities run)."""
    cfg = _cfg(tmp_path, 30)
    a = _kv_only(conn, cfg, "Albany")
    b = _kv_only(conn, cfg, "Bend")
    c = _kv_only(conn, cfg, "Corvallis")
    _live(monkeypatch, {(b, "kartaview")})
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 25)
    assert rc == 0
    assert (b, "kartaview") in launches
    fresh = [cid for cid in (a, c) if (cid, "kartaview") in launches]
    assert len(fresh) == 1, launches
    assert "1 city(ies) resume a live checkpoint tonight (~25 min reserved); 5 min left" in (
        caplog.text
    )


def test_a_resume_does_not_spend_the_fresh_allowance(conn, monkeypatch, tmp_path, caplog):
    """60 min, one resumer (Albany) reserved ~24.7, so ~35.3 for fresh crawls;
    25 min a launch. Albany leads the slate (asserted: registered first, and no
    gsv-due city to order against), so if its 25 min were charged as fresh the
    first fresh city would spend the rest. Charged as paid, two fresh cities run and the third
    is left. Mutation: `start_charge(provider, fresh=True)` for every launch."""
    cfg = _cfg(tmp_path, 60)
    a = _kv_only(conn, cfg, "Albany")
    fresh = [_kv_only(conn, cfg, name) for name in ("Bend", "Corvallis", "Dalles")]
    _live(monkeypatch, {(a, "kartaview")})
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, launches, _ = _night(monkeypatch, conn, cfg, minutes=lambda cid, p: 25)
    assert rc == 0
    assert launches[0] == (a, "kartaview"), "premise: the resumer leads the slate"
    assert sum((cid, "kartaview") in launches for cid in fresh) == 2, launches
    assert "1 launch(es) left for a later night" in _done_line(caplog)


def test_an_unhoisted_resumer_still_runs_when_the_reserve_fills_the_allowance(
    conn, monkeypatch, tmp_path, caplog
):
    """A 10-minute allowance against two resumers (~49 min reserved): fresh
    crawls are held from the start. At --limit 4 the opt-in reservation is 1,
    and the second resumer sits behind the fresh city in the slate (asserted
    from the log order); it is exempt and runs, and the fresh city is left.
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
    assert "fresh crawls are held from the start" in caplog.text
    assert (r1, "kartaview") in launches and (r2, "kartaview") in launches
    assert (f, "kartaview") not in launches
    # The fully reserved allowance is never announced as a whole-run hold.
    assert _messages(caplog, "for this run", logging.WARNING) == []
    assert "2 city(ies) exempt as paid-for work" in _done_line(caplog)


def test_the_done_clause_carries_the_numbers_and_never_the_held_token(
    conn, monkeypatch, tmp_path, caplog
):
    """40 min, Albany resumes (25 min, ~24.7 reserved), Bend fresh (31 min),
    Corvallis left. The clause states measured launch minutes, the fresh share,
    the paid share, the reserve and the count; it never says HELD, and the
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
        f"; ALLOWANCE {LABEL}: 56 of 40 min in launches (fresh 31 of 15 min over 1 "
        f"launch(es); resumes 25 min over 1; 1 resume(s) 25 min reserved), 1 launch(es) "
        f"left for a later night, 0 city(ies) exempt as paid-for work, cities stay due"
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
    outranks a spent allowance. The allowance here is spent by its own reserve
    (10 min against one resumer's ~24.7), so its hold is in force too. Were the
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
    holds.reserve(HOST_KARTAVIEW, sched._allowance_launch_s(cfg, HOST_KARTAVIEW), 1)
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
    assert "1 city(ies) exempt as paid-for work" in sched._allowance_summary_note(breaker)


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
    assert "fresh 31 of 30 min over 1 launch(es)" in _done_line(caplog)


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
    city's kartaview row HELD (night allowance spent). Mutation: drop the
    preview's `holds.charge(...)` call."""
    cfg = _cfg(tmp_path, 1)
    kv = [_kv_only(conn, cfg, name) for name in ("Albany", "Bend", "Corvallis")]
    d = _register(conn, "Dalles")
    db.set_channel_membership(conn, d, "kartaview", True, cycle_days=cfg.cycle_days)
    rc, launches, _ = _night(monkeypatch, conn, cfg, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0 and launches == []
    assert f"  ALLOWANCE — {LABEL}: 1 min of launches; 0 city(ies) resume" in out
    rows = [ln.split() for ln in out.splitlines() if ln.startswith("  ") and "--" in ln]
    assert [r[0] for r in rows if r[0] in kv] == [kv[0]], out
    assert [r[:2] for r in rows if r[0] == d] == [[d, "gsv"], [d, "kartaview"]], out
    assert [ln for ln in out.splitlines() if "HELD (night allowance spent)" in ln] == [
        ln for ln in out.splitlines() if ln.startswith(f"  {d}") and "kartaview" in ln
    ]
    assert "(2 due city(ies) HELD on every channel passed over" in out


def test_status_names_the_night_allowance(conn, tmp_path):
    """Mutation: delete the `host_night_minutes` loop in `_host_hold_status_lines`."""
    lines = sched._host_hold_status_lines(_cfg(tmp_path, 360), conn, THURSDAY)
    assert lines == [
        f"{LABEL} night allowance: 360 min of launches per run-due for fresh crawls, "
        f"tonight's resumes reserved first; live checkpoints and cached censuses are never "
        f"held (issue #464). Usage is on each night's Done: line."
    ]
    assert sched._host_hold_status_lines(_cfg(tmp_path, None), conn, THURSDAY) == []


def test_ignore_host_holds_lifts_the_night_allowance(conn, monkeypatch, tmp_path, caplog):
    """A 10-minute allowance against one resumer's ~25-minute reserve holds
    fresh crawls from the start; `--ignore-host-holds` lifts it, every city
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
