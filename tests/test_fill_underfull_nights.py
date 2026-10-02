"""
Filling under-full nights (issue #404).

`run-due` collects only DUE cities; once a backlog drains, the due slate is a
small fraction of the night's cap and deadline. The fill phase spends that idle
capacity on EARLY refreshes of cities whose every default channel is at least
`[schedule].fill_min_days` old, stalest-first, and admits a city WHOLE or not
at all. These tests pin, in order:

* eligibility: the floor, the not-yet-due wall, membership, quarantine;
* the pure whole-city rule (`_fill_verdict`) and the host room it reads;
* the live night: fill only behind a due loop that ended on its own, never
  outranking a due city, held by a backlog, bounded by the cap, the budgets,
  the fill ceiling and the deadline, and composing with the opt-in hoist and
  the refresh reserve;
* the marker the series carries (`early_refreshes`, v18) and the Done line;
* the dry run, and the config loader's fail-closed posture.
"""

import os
from datetime import UTC, date, datetime, timedelta

import pytest

from streetscape_metadata_tracker import clock, db
from streetscape_metadata_tracker import scheduler as sched
from streetscape_metadata_tracker.scheduler import (
    ProviderConfig,
    SchedulerConfig,
    _fill_host_room,
    _fill_verdict,
    load_scheduler_config,
)

TODAY = date(2026, 10, 1)
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
# Seeded stamps sit at midnight so `julianday(today) - julianday(stamp)` is a
# whole number of days -- the same reading `get_due_cities` gives it.
MIDNIGHT = datetime(2026, 10, 1, tzinfo=UTC)
# The unpatched connect: `_run_night` replaces `db.connect` for the night, and a
# test that builds a second catalog after one night must not get the first back.
_REAL_CONNECT = db.connect
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Freeze the UTC clock so stamps written tonight sort AT the batch start.

    The backlog test compares ``last_attempt_at`` against the batch start, and
    the early-refresh marker compares ``last_success_at`` before and after; a
    real clock would make both depend on wall time.
    """
    monkeypatch.setattr(clock, "_utc_clock", lambda: NOW)


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


def _seed(conn, city_id, provider, days_ago, *, failures=0):
    """A channel whose last success (and attempt) was ``days_ago`` before NOW."""
    stamp = (MIDNIGHT - timedelta(days=days_ago)).isoformat()
    conn.execute(
        """INSERT INTO schedule_state
           (city_id, provider, day_of_cycle, last_attempt_at, last_success_at,
            consecutive_failures)
           VALUES (?, ?, 0, ?, ?, ?)
           ON CONFLICT(city_id, provider) DO UPDATE SET
             last_attempt_at = excluded.last_attempt_at,
             last_success_at = excluded.last_success_at,
             consecutive_failures = excluded.consecutive_failures""",
        (city_id, provider, stamp, stamp, failures),
    )
    conn.commit()


def _city(conn, name, ages, **kw):
    """Register a city and seed each ``{channel: days_ago}``."""
    cid = _register(conn, name, **kw)
    for provider, days in ages.items():
        _seed(conn, cid, provider, days)
    return cid


def _grid_cfg(**overrides):
    """gsv + mapillary only: no walk, so no frozen-network question."""
    providers = overrides.pop(
        "providers",
        {
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
        },
    )
    overrides.setdefault("fill_min_days", 30)
    # Hermetic on its own, not only through conftest: no driving-plan fetch.
    # (conftest also stubs the hook and puts a default config's data, backup and
    # log dirs under tmp_path.)
    overrides.setdefault("driving_plan", sched.DrivingPlanConfig(enabled=False))
    overrides.setdefault("max_cities_per_day", 40)
    overrides.setdefault("max_batch_hours", 12.0)
    return SchedulerConfig(providers=providers, publish_enabled=False, **overrides)


def _run_night(monkeypatch, conn, cfg, *, outcome=None, record_usage=True, **kwargs):
    """Drive cmd_run_due with a fake collector; return the (city_id, channel) launch order.

    ``record_usage`` writes what the child would have spent into ``api_usage``
    (and so ``host_usage``), which every budget assertion depends on.
    """
    ran = []
    outcome = outcome or (lambda city, provider: True)

    def fake_run(cfg, city, run_today, provider="gsv", request_cap=None, **_):
        if record_usage:
            spend = sched.estimate_requests(city, provider)
            if request_cap is not None:
                spend = min(spend, request_cap)
            db.add_api_usage(conn, run_today, spend, provider)
        ran.append((city.city_id, provider))
        return outcome(city, provider)

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    kwargs.setdefault("today", TODAY)
    rc = sched.cmd_run_due(cfg, **kwargs)
    return ran, rc


def _done_line(caplog):
    done = [r.message for r in caplog.records if r.message.startswith("Done: ")]
    assert len(done) == 1
    return done[0]


def _early_rows(conn):
    return sorted(
        (r["city_id"], r["channel"], r["run_date"], r["floor_days"])
        for r in conn.execute("SELECT * FROM early_refreshes")
    )


def _cities_in(ran):
    out = []
    for cid, _ in ran:
        if cid not in out:
            out.append(cid)
    return out


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def _candidates(conn, channels=("gsv", "mapillary"), floor=30):
    return db.get_fill_candidates(
        conn,
        today=TODAY,
        channels=list(channels),
        default_membership=sched.CHANNEL_DEFAULT_MEMBERSHIP,
        fill_min_days=floor,
        due_threshold_days=83,
    )


def test_fill_candidates_need_every_member_channel_past_the_floor_and_not_due(conn):
    old = _city(conn, "Old", {"gsv": 60, "mapillary": 50})
    older = _city(conn, "Older", {"gsv": 40, "mapillary": 70})
    _city(conn, "Fresh", {"gsv": 60, "mapillary": 29})  # one channel under the floor
    _city(conn, "Due", {"gsv": 60, "mapillary": 83})  # due: the due slate's, not the fill's
    _city(conn, "Never", {"gsv": 60})  # mapillary never collected -> due
    quarantined = _city(conn, "Quarantined", {"gsv": 60, "mapillary": 60})
    _seed(conn, quarantined, "mapillary", 60, failures=5)
    excluded = _city(conn, "Excluded", {"gsv": 45})
    db.set_channel_membership(conn, excluded, "mapillary", False, cycle_days=90)
    # ONE failure since the last success is enough: the fill never adds the
    # second, so it cannot be what quarantines a city that was never due.
    failing = _city(conn, "Failing", {"gsv": 60, "mapillary": 60})
    _seed(conn, failing, "gsv", 60, failures=1)
    disabled = _city(conn, "Disabled", {"gsv": 60, "mapillary": 60})
    conn.execute("UPDATE cities SET enabled = 0 WHERE city_id = ?", (disabled,))
    conn.commit()

    got = _candidates(conn)
    # Stalest-first by the city's OLDEST channel: Older (70) before Old (60)
    # before Excluded (45), which is eligible on the one channel it is a member of.
    assert [c.city_id for c, _ in got] == [older, old, excluded]
    prior = {c.city_id: p for c, p in got}
    assert set(prior[excluded]) == {"gsv"}
    assert set(prior[old]) == {"gsv", "mapillary"}


def test_the_floor_is_inclusive_and_only_the_floor_moves_eligibility(conn):
    at = _city(conn, "At", {"gsv": 30, "mapillary": 30})
    _city(conn, "Under", {"gsv": 30, "mapillary": 29.9})
    assert [c.city_id for c, _ in _candidates(conn, floor=30)] == [at]
    assert len(_candidates(conn, floor=20)) == 2
    assert _candidates(conn, floor=31) == []


def test_an_opt_in_enrolment_neither_qualifies_nor_disqualifies_a_fill_city(conn):
    """The fill reads DEFAULT-membership channels only (#248, #374): a fresh
    KartaView clock on an enrolled city does not hold back its gsv/mapillary
    refresh, and a KartaView-only city is never a candidate."""
    both = _city(conn, "Both", {"gsv": 50, "mapillary": 50, "kartaview": 1})
    db.set_channel_membership(conn, both, "kartaview", True, cycle_days=90)
    only = _city(conn, "OnlyKarta", {"kartaview": 60})
    db.set_channel_membership(conn, only, "gsv", False, cycle_days=90)
    db.set_channel_membership(conn, only, "mapillary", False, cycle_days=90)
    db.set_channel_membership(conn, only, "kartaview", True, cycle_days=90)
    got = _candidates(conn)
    assert [c.city_id for c, _ in got] == [both]
    assert set(got[0][1]) == {"gsv", "mapillary"}


# ---------------------------------------------------------------------------
# The whole-city rule
# ---------------------------------------------------------------------------


def test_a_city_is_declined_whole_when_one_channel_does_not_fit():
    """THE acceptance test: gsv fits, mapillary does not -> nothing runs.

    Mutation check: drop the per-channel loop in _fill_verdict (or test only
    the first channel) and this admits the city.
    """
    v = _fill_verdict(
        ["gsv", "mapillary"],
        est={"gsv": 100, "mapillary": 60},
        channel_room={"gsv": 1_000_000, "mapillary": 59},
        host_room={},
        need_s={"gsv": 60, "mapillary": 60},
        remaining_s=None,
    )
    assert not v.admit and v.blocker == "mapillary"
    # The order does not matter: the LAST channel failing declines it too.
    v = _fill_verdict(
        ["mapillary", "gsv"],
        est={"gsv": 100, "mapillary": 60},
        channel_room={"gsv": 99, "mapillary": 60},
        host_room={},
        need_s={"gsv": 60, "mapillary": 60},
        remaining_s=None,
    )
    assert not v.admit and v.blocker == "gsv"
    # Exactly fitting is admitted (<=, not <).
    v = _fill_verdict(
        ["gsv", "mapillary"],
        est={"gsv": 100, "mapillary": 60},
        channel_room={"gsv": 100, "mapillary": 60},
        host_room={},
        need_s={"gsv": None, "mapillary": None},
        remaining_s=0.0,
    )
    assert v.admit and v.blocker is None


def test_the_host_check_sums_both_mapillary_channels():
    """Each channel fits the host room alone; together they do not."""
    v = _fill_verdict(
        ["mapillary", "mapillary_streets"],
        est={"mapillary": 60, "mapillary_streets": 60},
        channel_room={"mapillary": 3_500, "mapillary_streets": 1_750},
        host_room={"mapillary_tiles": 100},
        need_s={"mapillary": None, "mapillary_streets": None},
        remaining_s=None,
    )
    assert not v.admit and v.blocker == "mapillary_tiles"


def test_the_deadline_check_sums_the_channels_needs():
    v = _fill_verdict(
        ["gsv", "mapillary"],
        est={"gsv": 1, "mapillary": 1},
        channel_room={"gsv": 10, "mapillary": 10},
        host_room={},
        need_s={"gsv": 600, "mapillary": 600},
        remaining_s=1_000.0,
    )
    assert not v.admit and v.blocker == "deadline"


def test_fill_host_room_is_the_smaller_of_budget_and_ceiling():
    cfg = SchedulerConfig(
        host_budgets={"mapillary_tiles": 3_000}, fill_host_ceilings={"mapillary_tiles": 2_260}
    )
    assert _fill_host_room(cfg, {"mapillary_tiles": 2_000}) == {"mapillary_tiles": 260}
    cfg = SchedulerConfig(
        host_budgets={"mapillary_tiles": 2_000}, fill_host_ceilings={"mapillary_tiles": 2_260}
    )
    assert _fill_host_room(cfg, {"mapillary_tiles": 1_900}) == {"mapillary_tiles": 100}
    # Neither bound: unbounded, i.e. absent.
    assert _fill_host_room(SchedulerConfig(), {"kartaview": 5}) == {}


# ---------------------------------------------------------------------------
# The live night
# ---------------------------------------------------------------------------


def test_an_under_full_night_refreshes_eligible_cities_stalest_first(conn, monkeypatch, caplog):
    caplog.set_level("INFO")
    a = _city(conn, "Alpha", {"gsv": 40, "mapillary": 40})
    b = _city(conn, "Bravo", {"gsv": 70, "mapillary": 35})
    c = _city(conn, "Charlie", {"gsv": 50, "mapillary": 50})
    _city(conn, "Recent", {"gsv": 10, "mapillary": 10})

    ran, rc = _run_night(monkeypatch, conn, _grid_cfg())

    assert rc == 0
    assert _cities_in(ran) == [b, c, a], "stalest-first by the oldest channel"
    assert sorted(ran) == sorted((x, p) for x in (a, b, c) for p in ("gsv", "mapillary"))
    assert _early_rows(conn) == sorted(
        (x, p, "2026-10-01", 30) for x in (a, b, c) for p in ("gsv", "mapillary")
    )
    done = _done_line(caplog)
    assert "across 3 cities (0 due, 3 fill)" in done
    # Bravo's channels sat on different days: the fill re-aligned it.
    assert "fill (early refresh, >= 30 d): 3 cities, 1 realigned, 6/6 runs, 6 early" in done
    assert "ended by candidates exhausted" in done


def test_a_fill_city_is_never_collected_on_a_subset_of_its_channels(conn, monkeypatch, caplog):
    """Acceptance: the big city's mapillary does not fit, so its gsv does not run
    either, while a smaller city behind it still fits and runs on both.

    Mutation check: make _fill_verdict skip its per-channel check and `big`
    runs gsv (and a capped mapillary) -- the un-pairing #404 exists to stop.
    """
    caplog.set_level("INFO")
    big = _city(conn, "Big", {"gsv": 60, "mapillary": 60}, width=40_000, height=40_000)
    small = _city(conn, "Small", {"gsv": 50, "mapillary": 50}, width=2_000, height=2_000)
    big_tiles = sched.estimate_requests(db.resolve_city(conn, big), "mapillary")
    small_tiles = sched.estimate_requests(db.resolve_city(conn, small), "mapillary")
    assert small_tiles < big_tiles
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=big_tiles - 1),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == [(small, "gsv"), (small, "mapillary")]
    assert {r[0] for r in _early_rows(conn)} == {small}
    done = _done_line(caplog)
    assert "declined 1 for mapillary" in done
    # A decline FOLLOWED by an admission did not end the fill.
    assert "ended by candidates exhausted" in done


def test_budget_drawn_down_by_earlier_fill_cities_declines_a_later_one(conn, monkeypatch, caplog):
    """The ledger is re-read per admission: two cities fit a budget that the
    third, though identical, no longer does -- and the Done line names it."""
    caplog.set_level("INFO")
    ids = [_city(conn, f"C{i}", {"gsv": 60 - i, "mapillary": 60 - i}) for i in range(3)]
    tiles = sched.estimate_requests(db.resolve_city(conn, ids[0]), "mapillary")
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=2 * tiles),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert _cities_in(ran) == ids[:2]
    assert db.get_api_usage(conn, TODAY, "mapillary") == 2 * tiles
    assert "ended by budget (mapillary)" in _done_line(caplog)


def test_the_fill_ceiling_binds_below_the_host_budget(conn, monkeypatch, caplog):
    """With 3,000 of host budget left the due slate could spend, the fill stops
    at the lower 2,260 ceiling it prices against."""
    caplog.set_level("INFO")
    cid = _city(conn, "Tiles", {"gsv": 60, "mapillary": 60})
    tiles = sched.estimate_requests(db.resolve_city(conn, cid), "mapillary")
    # Earlier in the 24 h window: under the 3,000 budget by more than this
    # city costs, but over the ceiling minus the city's price.
    db.add_api_usage(conn, TODAY - timedelta(days=1), 2_260 - tiles + 1, "mapillary")
    cfg = _grid_cfg(
        host_budgets={"mapillary_tiles": 3_000}, fill_host_ceilings={"mapillary_tiles": 2_260}
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == []
    assert "ended by budget (mapillary_tiles)" in _done_line(caplog)
    # And without the ceiling the same city fits the host budget.
    cfg = _grid_cfg(host_budgets={"mapillary_tiles": 3_000})
    caplog.clear()
    ran, _ = _run_night(monkeypatch, conn, cfg)
    assert _cities_in(ran) == [cid]


def test_the_fill_stops_at_the_city_cap(conn, monkeypatch, caplog):
    caplog.set_level("INFO")
    due = _register(conn, "Due")  # never collected: due on both channels
    old = [_city(conn, f"Old{i}", {"gsv": 70 - i, "mapillary": 70 - i}) for i in range(3)]

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(max_cities_per_day=3))

    # The due city first, then the two stalest fill cities fill the cap of 3.
    assert _cities_in(ran) == [due, old[0], old[1]]
    done = _done_line(caplog)
    assert "across 3 cities (1 due, 2 fill)" in done
    assert "ended by city cap (3)" in done


def test_the_fill_ends_at_the_deadline_and_says_so(conn, monkeypatch, caplog):
    """A night with room on every budget but not on the clock: the fill declines
    the city it cannot finish rather than launching it into a SIGKILL."""
    caplog.set_level("INFO")
    _city(conn, "Slow", {"gsv": 60, "mapillary": 60})
    # 0.25 h = 900 s: above the 600 s launch floor, below this city's need.
    cfg = _grid_cfg(max_batch_hours=0.25)

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == []
    assert "ended by deadline (0.25 h)" in _done_line(caplog)


def test_a_full_night_is_unchanged_and_the_fill_is_not_reached(tmp_path, monkeypatch, caplog):
    """Due cities fill the cap: the launch order is identical with the fill on
    and off, and nothing fill-eligible runs."""
    caplog.set_level("INFO")

    def night(label, **kw):
        c = _REAL_CONNECT(str(tmp_path / f"{label}.db"))
        for i in range(3):
            _register(c, f"Due{i}")
        _city(c, "Eligible", {"gsv": 60, "mapillary": 60})
        caplog.clear()
        ran, _ = _run_night(monkeypatch, c, _grid_cfg(max_cities_per_day=2, **kw))
        return ran, _done_line(caplog), _early_rows(c)

    ran_on, done_on, rows_on = night("on")
    ran_off, done_off, _ = night("off", fill_min_days=None)

    assert ran_on == ran_off
    assert rows_on == []
    assert "fill not reached (the due slate ended the night)" in done_on
    assert "fill" not in done_off


def test_a_due_city_always_runs_before_any_fill_city(conn, monkeypatch):
    """Fill never outranks due, even when the fill city is far staler on one
    channel -- and the due city, collected on gsv alone by the due path's own
    rule, is then RE-ALIGNED by the fill on mapillary (40 days, past the floor),
    behind the staler fill city."""
    fill = _city(conn, "Fill", {"gsv": 82, "mapillary": 82})
    due = _city(conn, "Due", {"gsv": 83, "mapillary": 40})

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg())

    # The due city, partly collected tonight, is FINISHED first (its fresh gsv
    # would be under the floor tomorrow); then the staler fill city.
    assert ran == [(due, "gsv"), (due, "mapillary"), (fill, "gsv"), (fill, "mapillary")]


def test_a_backlog_holds_the_whole_fill(conn, monkeypatch, caplog):
    """A due city's mapillary deferred for budget is a backlog: the fill adds no
    gsv run either, because a fill city must run every channel or none."""
    caplog.set_level("INFO")
    due = _register(conn, "Due")
    _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=1),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == [(due, "gsv")]
    assert "held: backlog (mapillary 1 due not attempted)" in _done_line(caplog)


def test_a_failed_due_channel_is_not_a_backlog(conn, monkeypatch):
    """A failure records an attempt: the night did not run out of capacity."""
    due = _register(conn, "Due")
    fill = _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(), outcome=lambda city, p: city.city_id != due)

    assert _cities_in(ran) == [due, fill]


def test_a_backlog_on_an_opt_in_channel_does_not_hold_the_fill(conn, monkeypatch, caplog):
    """KartaView is budget-bound on its own; that says nothing about gsv/mapillary."""
    caplog.set_level("INFO")
    karta = _city(conn, "Karta", {"gsv": 20, "mapillary": 20})
    db.set_channel_membership(conn, karta, "kartaview", True, cycle_days=90)
    fill = _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
            "kartaview": ProviderConfig(daily_request_budget=1),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == [(fill, "gsv"), (fill, "mapillary")], "kartaview deferred; the fill still ran"


def test_the_fill_composes_with_the_hoist_and_the_refresh_reserve(tmp_path, monkeypatch, caplog):
    """The two reservations reorder the DUE slate; the fill only appends.

    A stranded opt-in-only city (hoisted) and due refreshes (promoted) run in
    exactly the order they run with the fill off, and the fill cities follow.
    """
    caplog.set_level("INFO")

    def night(label, **kw):
        conn = _REAL_CONNECT(str(tmp_path / f"{label}.db"))
        # Due, never collected: the NULLS FIRST block.
        new = [_register(conn, f"New{i}") for i in range(3)]
        # Due refreshes (85 d): what refresh_slots promotes.
        ref = [_city(conn, f"Ref{i}", {"gsv": 85, "mapillary": 85}) for i in range(2)]
        # Stranded: due only on kartaview (gsv/mapillary fresh) -> the hoist.
        strand = _city(conn, "Strand", {"gsv": 5, "mapillary": 5})
        db.set_channel_membership(conn, strand, "kartaview", True, cycle_days=90)
        # Fill-eligible.
        fills = [_city(conn, f"Fill{i}", {"gsv": 60 - i, "mapillary": 60 - i}) for i in range(2)]
        caplog.clear()
        cfg = _grid_cfg(
            providers={
                "gsv": ProviderConfig(daily_request_budget=10_000_000),
                "mapillary": ProviderConfig(daily_request_budget=3_500),
                "kartaview": ProviderConfig(daily_request_budget=10_000),
            },
            max_cities_per_day=20,
            opt_in_cities_per_day=1,
            refresh_slots=2,
            **kw,
        )
        ran, _ = _run_night(monkeypatch, conn, cfg)
        opening = next(r.message for r in caplog.records if " cities due on " in r.message)
        return ran, opening, new, ref, strand, fills

    ran_off, opening_off, new, ref, strand, fills = night("off", fill_min_days=None)
    ran_on, opening_on, *_ = night("on")

    due_part = ran_on[: len(ran_off)]
    assert due_part == ran_off, "the due phase is byte-identical with the fill on"
    assert _cities_in(ran_off)[:3] == [strand, ref[0], ref[1]], "hoist, then the reserve"
    assert set(_cities_in(ran_off)) == {strand, *ref, *new}
    assert _cities_in(ran_on[len(ran_off) :]) == fills
    for clause in ("(2 promoted)", "hoisted=1"):
        assert clause in opening_off and clause in opening_on
    assert "fill_min_days=30" in opening_on and "fill_min_days" not in opening_off


@pytest.mark.parametrize(
    ("kwargs", "note"),
    [
        ({"limit": 5}, "fill off for --limit"),
        ({"requested_providers": ["gsv"]}, "fill off for --provider"),
        ({"requested_cities": ["eligible--oregon--united-states"]}, "fill off for --city"),
    ],
)
def test_an_operator_narrowed_run_never_fills(conn, monkeypatch, caplog, kwargs, note):
    caplog.set_level("INFO")
    _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(), **kwargs)

    assert ran == []
    assert note in _done_line(caplog)


def test_the_repo_default_has_no_fill_and_logs_nothing_about_it(conn, monkeypatch, caplog):
    caplog.set_level("INFO")
    _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(fill_min_days=None))

    assert ran == []
    assert not any("fill" in r.message.lower() for r in caplog.records)
    assert SchedulerConfig().fill_min_days is None


def test_a_failed_fill_channel_is_not_marked_an_early_refresh(conn, monkeypatch):
    cid = _city(conn, "Half", {"gsv": 60, "mapillary": 60})

    _run_night(monkeypatch, conn, _grid_cfg(), outcome=lambda city, p: p == "gsv")

    assert _early_rows(conn) == [(cid, "gsv", "2026-10-01", 30)]
    # One failure recorded -- and the city then leaves the fill (no failure
    # since the last success is part of eligibility), so the fill can never
    # stack the five that would quarantine a never-due city.
    fails = conn.execute(
        "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = ?",
        (cid, "mapillary"),
    ).fetchone()[0]
    assert fails == 1
    row = conn.execute(
        "SELECT prior_success_at FROM early_refreshes WHERE city_id = ?", (cid,)
    ).fetchone()
    assert row[0] == (MIDNIGHT - timedelta(days=60)).isoformat()


def test_a_walk_on_an_unfrozen_network_declines_its_city(conn, monkeypatch, caplog, data_dir):
    """The fill never adds Overpass traffic: a walk that would fetch its network
    declines the city whole; once the network is frozen the city is admitted."""
    caplog.set_level("INFO")
    cid = _city(conn, "Walk", {"gsv": 60, "gsv_streets": 60})
    cfg = _grid_cfg(
        data_dir=data_dir,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "gsv_streets": ProviderConfig(daily_request_budget=10_000_000),
        },
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)
    assert ran == []
    assert "ended by unfrozen street network" in _done_line(caplog)

    path = sched.network_cache_path(cid, data_dir, "drive")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("<graphml/>")
    caplog.clear()
    ran, _ = _run_night(monkeypatch, conn, cfg)
    assert ran == [(cid, "gsv"), (cid, "gsv_streets")]
    # The walk is marked too, with its network type (for a later street_walks join).
    rows = conn.execute(
        "SELECT channel, network_type FROM early_refreshes WHERE city_id = ? ORDER BY channel",
        (cid,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [("gsv", None), ("gsv_streets", "drive")]


def test_a_host_refused_tonight_declines_the_fill_city(conn, monkeypatch, caplog):
    """A refusal mid-due-phase latches the host; a fill city that needs it would
    be stood down at launch on that channel alone, so it is declined whole.

    The due city's refused mapillary is itself a backlog, so this drives the
    admission directly against a latched breaker."""
    cid = _city(conn, "Tiles", {"gsv": 60, "mapillary": 60})
    city = db.resolve_city(conn, cid)
    breaker = sched.HostBreaker()
    breaker.latched.add("mapillary_tiles")
    v = sched._fill_admission(
        _grid_cfg(), conn, TODAY, city, ["gsv", "mapillary"], 10_000.0, breaker
    )
    assert not v.admit and v.blocker == "host refused tonight"


def test_an_error_in_the_fill_still_publishes_and_reports_unhealthy(conn, monkeypatch, caplog):
    caplog.set_level("INFO")
    _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})

    def boom(*a, **k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(sched.db, "get_fill_candidates", boom)
    ran, rc = _run_night(monkeypatch, conn, _grid_cfg())

    assert ran == [] and rc != 0
    done = _done_line(caplog)
    assert "ended by unexpected error in the fill phase" in done
    assert "stopped early" in done


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_the_dry_run_previews_the_fill_through_the_same_rule(conn, monkeypatch, capsys):
    due = _register(conn, "Due")
    a = _city(conn, "Alpha", {"gsv": 70, "mapillary": 70})
    b = _city(conn, "Bravo", {"gsv": 60, "mapillary": 60}, width=40_000, height=40_000)
    c = _city(conn, "Charlie", {"gsv": 50, "mapillary": 50})
    tiles = sched.estimate_requests(db.resolve_city(conn, a), "mapillary")
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            # Room for the due city and two small cities, not the big one.
            "mapillary": ProviderConfig(daily_request_budget=3 * tiles),
        }
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    assert sched.cmd_run_due(cfg, dry_run=True, today=TODAY) == 0

    out = capsys.readouterr().out
    assert due in out
    fill = out.split("Would FILL", 1)[1]
    assert "fill_min_days=30; 3 eligible, up to 39 by the city cap" in fill
    assert a in fill and c in fill and b not in fill
    assert fill.index(a) < fill.index(c), "stalest-first"
    assert "Fill: 2 cities admitted; declined 1 for mapillary." in fill
    # A preview writes nothing.
    assert _early_rows(conn) == []


def test_the_dry_run_says_when_the_fill_is_not_reached_or_held(conn, monkeypatch, capsys):
    for i in range(2):
        _register(conn, f"Due{i}")
    _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    sched.cmd_run_due(_grid_cfg(max_cities_per_day=2), dry_run=True, today=TODAY)
    assert "Fill: not reached — 2 due cities fill the 2-city cap." in capsys.readouterr().out

    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=1),
        }
    )
    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    out = capsys.readouterr().out
    assert "Fill: holding mapillary — the due slate leaves mapillary 2 unfinished." in out
    assert "Fill: 0 cities admitted; declined 1 for backlog." in out


# ---------------------------------------------------------------------------
# Config, and the catalog
# ---------------------------------------------------------------------------


def _load(tmp_path, schedule_lines):
    path = tmp_path / "s.toml"
    path.write_text("[schedule]\n" + schedule_lines + "\n")
    return load_scheduler_config(str(path))


def test_the_loader_reads_the_fill_keys(tmp_path):
    cfg = _load(tmp_path, "fill_min_days = 30\nfill_host_ceilings = { mapillary_tiles = 2260 }")
    assert cfg.fill_min_days == 30
    assert cfg.fill_host_ceilings == {"mapillary_tiles": 2260}
    assert _load(tmp_path, "").fill_min_days is None


@pytest.mark.parametrize(
    "lines",
    [
        "fill_min_days = 0",
        "fill_min_days = -3",
        "fill_min_days = true",
        'fill_min_days = "30"',
        "fill_min_days = 30\nfill_host_ceilings = { overpass = 100 }",
        "fill_min_days = 30\nfill_host_ceilings = { mapillary_tiles = 0 }",
        "fill_min_days = 30\nfill_host_ceilings = { mapillary_tiles = true }",
        "fill_min_days = 30\nfill_host_ceilings = 2260",
    ],
)
def test_a_bad_fill_key_turns_the_fill_off_rather_than_unbounded(tmp_path, lines, caplog):
    """The fill only adds traffic, so fail-closed is OFF -- and a bad ceiling
    must not leave the fill running without the bound it was meant to have."""
    cfg = _load(tmp_path, lines)
    assert cfg.fill_min_days is None and cfg.fill_host_ceilings == {}
    assert any("fill" in r.message for r in caplog.records)


def test_makelab1_fills_at_30_days_under_the_measured_mapillary_ceiling():
    """Pinned with the host budget it must stay BELOW: 2,260 is the highest
    combined night #292 measured clean, and the due slate keeps the 3,000."""
    cfg = load_scheduler_config(os.path.join(_PROJECT_ROOT, "config", "scheduler.makelab1.toml"))
    assert cfg.fill_min_days == 30
    assert cfg.fill_host_ceilings == {"mapillary_tiles": 2_260}
    assert cfg.fill_host_ceilings["mapillary_tiles"] < cfg.host_budgets["mapillary_tiles"]
    assert cfg.fill_min_days < cfg.cycle_days - cfg.grace_days
    assert (
        load_scheduler_config(os.path.join(_PROJECT_ROOT, "config", "scheduler.toml")).fill_min_days
        is None
    ), "the repo default keeps today's behaviour"


def test_a_v17_catalog_gains_the_early_refreshes_table(tmp_path):
    path = str(tmp_path / "v17.db")
    conn = db.connect(path)
    conn.execute("DROP TABLE early_refreshes")
    conn.execute("DROP TABLE fill_attempts")
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 18
    cid = _register(conn, "Bend")
    db.record_early_refresh(
        conn, cid, "gsv", TODAY, prior_success_at="2026-08-01T00:00:00+00:00", floor_days=30
    )
    db.record_early_refresh(
        conn, cid, "gsv", TODAY, prior_success_at="2026-08-01T00:00:00+00:00", floor_days=30
    )
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} >= {
        "early_refreshes",
        "fill_attempts",
    }
    # COUNT, not the key set: a set cannot see a duplicate row, so it stays
    # green with the PRIMARY KEY dropped.
    assert conn.execute("SELECT COUNT(*) FROM early_refreshes").fetchone()[0] == 1
    assert db.get_early_refresh_keys(conn) == {(cid, "gsv", "2026-10-01")}
    conn.close()


# ---------------------------------------------------------------------------
# Review round (PR #411)
# ---------------------------------------------------------------------------


def _outcome(exit_code):
    from streetscape_metadata_tracker.scheduler import CollectionOutcome

    return CollectionOutcome(False, f"exited {exit_code}", exit_code=exit_code)


def _freeze_network(data_dir, city_id):
    path = sched.network_cache_path(city_id, data_dir, "drive")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("<graphml/>")


def _live_checkpoints(monkeypatch, live, ages=None):
    """Fake `_sweep_checkpoint_progress`: a live checkpoint for each (city, channel) in ``live``.

    ``ages`` optionally gives a city's checkpoint age in seconds (default 1 h).
    """

    def progress(cfg, city, channel):
        if (city.city_id, channel) in live:
            age = (ages or {}).get(city.city_id, 3600.0)
            return {"age_s": age, "units_done": 1, "unit_count": 2, "unit_name": "tiles"}
        return None

    monkeypatch.setattr(sched, "_sweep_checkpoint_progress", progress)


def test_admission_asks_the_launch_plan_not_only_the_remainder(conn, monkeypatch, caplog, capsys):
    """Review item 1: a 1-tile census against a 4-request remainder fits
    ``est <= room`` but sits under the launch floor, so the launch path would
    SKIP mapillary after gsv ran. Admission now asks the same plan and declines
    the city; the dry run agrees.

    Killed by dropping the `_fill_launch_refusal` call from `_fill_judge`.
    """
    caplog.set_level("INFO")
    cid = _city(conn, "Tiny", {"gsv": 60, "mapillary": 60}, width=100, height=100)
    assert sched.estimate_requests(db.resolve_city(conn, cid), "mapillary") <= 4
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=4),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == []
    assert "declined 1 for mapillary" in _done_line(caplog)
    assert any("the launch would skip it" in r.message for r in caplog.records)

    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    assert "Fill: 0 cities admitted; declined 1 for mapillary." in capsys.readouterr().out


def test_a_launch_the_plan_would_cap_is_declined(conn, monkeypatch):
    """The plan's other refusal: no skip, but a cap under the price -- the
    clock's doing, since the budget terms are already checked above it. The
    city would pause un-paired, so it is declined. Driven through a stub plan
    because the deadline sum normally declines first; killed by dropping the
    `request_cap < est` test."""
    cid = _city(conn, "Capped", {"gsv": 60, "mapillary": 60})
    city = db.resolve_city(conn, cid)
    monkeypatch.setattr(
        sched,
        "_sweep_launch_plan",
        lambda *a, **k: sched.SweepLaunchPlan(60, 3, 3, None, "", ""),
    )
    v = sched._fill_launch_refusal(
        _grid_cfg(),
        conn,
        city,
        ["gsv", "mapillary"],
        est={"gsv": 100, "mapillary": 10},
        channel_room={"gsv": 10**9, "mapillary": 3_500},
        host_room={},
        remaining_s=10_000.0,
    )
    assert v is not None and not v.admit and v.blocker == "mapillary"
    assert "cap it at 3 of ~10" in v.message
    monkeypatch.setattr(
        sched,
        "_sweep_launch_plan",
        lambda *a, **k: sched.SweepLaunchPlan(60, 10, 10, None, "", ""),
    )
    assert (
        sched._fill_launch_refusal(
            _grid_cfg(),
            conn,
            city,
            ["gsv", "mapillary"],
            est={"gsv": 100, "mapillary": 10},
            channel_room={"gsv": 10**9, "mapillary": 3_500},
            host_room={},
            remaining_s=10_000.0,
        )
        is None
    ), "a cap equal to the price launches whole"


def test_a_paused_fill_crawl_is_resumed_first_by_the_next_nights_fill(conn, monkeypatch, caplog):
    """Review item 2: a fill crawl that pauses (exit 83) is not due on that
    channel, so nothing on the due path would resume it before its checkpoint
    expires. Night 1 says so (not "stays due"); night 2's fill resumes it FIRST,
    on the paused channel only, and marks it an early refresh against the
    success it still had.
    """
    from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE

    caplog.set_level("INFO")
    cid = _city(conn, "Paused", {"gsv": 60, "mapillary": 60})
    other = _city(conn, "Other", {"gsv": 40, "mapillary": 40})
    live: set = set()
    _live_checkpoints(monkeypatch, live)

    def night1(city, provider):
        if city.city_id == cid and provider == "mapillary":
            live.add((cid, "mapillary"))
            return _outcome(SWEEP_INCOMPLETE_EXIT_CODE)
        return True

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(max_cities_per_day=1), outcome=night1)
    assert ran == [(cid, "gsv"), (cid, "mapillary")]
    assert _early_rows(conn) == [(cid, "gsv", "2026-10-01", 30)]
    pause = [r.message for r in caplog.records if "sweep paused" in r.message]
    assert pause and "NOT due, so the next night's fill resumes it first" in pause[0]
    assert "stays due" not in pause[0]

    tomorrow = TODAY + timedelta(days=1)
    monkeypatch.setattr(clock, "_utc_clock", lambda: NOW + timedelta(days=1))
    caplog.clear()

    def night2(city, provider):
        live.discard((city.city_id, provider))
        return True

    ran, _ = _run_night(
        monkeypatch, conn, _grid_cfg(max_cities_per_day=2), outcome=night2, today=tomorrow
    )
    # The orphan first, before any new city -- and gsv re-run beside it (free
    # metadata), so the city's channels share the resume date (review 411c #6).
    assert ran[:2] == [(cid, "gsv"), (cid, "mapillary")], "the orphan leads, realigned"
    assert _cities_in(ran) == [cid, other]
    assert (cid, "gsv", "2026-10-02", 30) in _early_rows(conn)
    rows = _early_rows(conn)
    assert (cid, "mapillary", "2026-10-02", 30) in rows
    prior = conn.execute(
        "SELECT prior_success_at FROM early_refreshes WHERE city_id = ? AND channel = ?",
        (cid, "mapillary"),
    ).fetchone()[0]
    assert prior == (MIDNIGHT - timedelta(days=60)).isoformat()
    assert "(1 resuming a paused fill crawl)" in _done_line(caplog)


def test_a_paused_fill_crawl_is_resumed_even_while_a_backlog_holds_the_fill(
    conn, monkeypatch, caplog
):
    """Nothing else resumes it, so the hold (which waits on due work) does not."""
    caplog.set_level("INFO")
    cid = _city(conn, "Paused", {"gsv": 2, "mapillary": 60})
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(cid, "mapillary")})
    held = _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    due = _city(conn, "Due", {"gsv": 85, "mapillary": 20})
    # The due city's gsv does not fit what is left today (but would fit a
    # fresh budget), so it is deferred: a backlog that holds the refresh pass.
    db.add_api_usage(conn, TODAY, 990_000, "gsv")
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=1_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    # gsv's remainder cannot take a realigning gsv run, so the paused crawl is
    # resumed alone: the resume is what must not wait.
    assert ran == [(cid, "mapillary")]
    assert all(c not in (held, due) for c, _ in ran)
    assert any("resuming the paused crawl alone" in r.message for r in caplog.records)
    assert "held: backlog (gsv 1 due not attempted)" in _done_line(caplog)


def test_sigterm_mid_city_in_the_fill_stops_the_night(conn, monkeypatch, caplog):
    """Review items 2 and 9: a SIGTERM during a fill city's first channel stops
    the city (its second channel is not launched), the fill, and the night,
    which reports it -- and still reaches the tail."""
    import signal

    caplog.set_level("INFO")
    a = _city(conn, "Alpha", {"gsv": 70, "mapillary": 70})
    _city(conn, "Bravo", {"gsv": 60, "mapillary": 60})

    def outcome(city, provider):
        if city.city_id == a and provider == "gsv":
            os.kill(os.getpid(), signal.SIGTERM)
        return True

    ran, rc = _run_night(monkeypatch, conn, _grid_cfg(), outcome=outcome)

    assert ran == [(a, "gsv")]
    done = _done_line(caplog)
    assert "ended by received SIGTERM" in done
    assert "stopped early (received SIGTERM)" in done


def test_a_walk_stranded_in_the_fill_is_retried_then_named_without_a_dead_command(
    conn, monkeypatch, caplog, data_dir
):
    """Review item 3: the walk is refused after the fill city's grid landed. The
    fill's own retry pass asks again; still refused, it is reported as not
    lost, with the date its own clock makes it due, and WITHOUT the
    `run-due --city` command (a no-op for a walk that is not due)."""
    from streetscape_metadata_tracker.download_common import HOST_EXIT_CODES, HOST_OVERPASS

    caplog.set_level("INFO")
    cid = _city(conn, "Walk", {"gsv": 60, "gsv_streets": 60})
    _freeze_network(data_dir, cid)
    cfg = _grid_cfg(
        data_dir=data_dir,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "gsv_streets": ProviderConfig(daily_request_budget=10_000_000),
        },
    )
    ran, _ = _run_night(
        monkeypatch,
        conn,
        cfg,
        outcome=lambda city, p: (
            _outcome(HOST_EXIT_CODES[HOST_OVERPASS]) if p == "gsv_streets" else True
        ),
    )

    # The fill's launch, then `_retry_stranded_walks`' own two asks (#380's
    # rule: one retry, and one more after a refusal) -- the pass really ran.
    assert ran == [(cid, "gsv")] + [(cid, "gsv_streets")] * 3
    assert _early_rows(conn) == [(cid, "gsv", "2026-10-01", 30)]
    assert "(1 in the fill, not lost)" in _done_line(caplog)
    breaker = sched.HostBreaker()
    breaker.strand(cid, "gsv_streets")
    breaker.fill_stranded[(cid, "gsv_streets")] = "2026-10-24"
    note = sched._stranded_alert_note(cfg, breaker, TODAY)
    assert "stranded by the FILL phase" in note
    assert f"{cid} (gsv_streets: due on its own clock by 2026-10-24)" in note
    # No pasteable recovery command (it would be a no-op), and none of the due
    # paragraph's "~83 days" wording.
    assert "--provider" not in note and "~83 days" not in note


def test_the_fill_strandings_due_date_is_the_walks_own_wall(conn, monkeypatch, data_dir):
    from streetscape_metadata_tracker.download_common import HOST_EXIT_CODES, HOST_OVERPASS

    cid = _city(conn, "Walk", {"gsv": 60, "gsv_streets": 60})
    _freeze_network(data_dir, cid)
    cfg = _grid_cfg(
        data_dir=data_dir,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "gsv_streets": ProviderConfig(daily_request_budget=10_000_000),
        },
    )
    seen = {}
    real = sched._stranded_alert_note

    def spy(cfg, breaker, today):
        seen.update(breaker.fill_stranded)
        return real(cfg, breaker, today)

    monkeypatch.setattr(sched, "_stranded_alert_note", spy)
    _run_night(
        monkeypatch,
        conn,
        cfg,
        outcome=lambda city, p: (
            _outcome(HOST_EXIT_CODES[HOST_OVERPASS]) if p == "gsv_streets" else True
        ),
    )
    # 60 days before 2026-10-01, plus the 83-day wall.
    assert seen == {(cid, "gsv_streets"): "2026-10-24"}


def test_a_due_refresh_deferred_for_budget_holds_the_fill(conn, monkeypatch, caplog):
    """Review item 4: the hold read on a city ATTEMPTED before (85 days ago),
    not only a never-attempted one -- killed by dropping the `< since_iso` test."""
    caplog.set_level("INFO")
    due = _city(conn, "Refresh", {"gsv": 85, "mapillary": 85})
    _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=1),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == [(due, "gsv")]
    assert "held: backlog (mapillary 1 due not attempted)" in _done_line(caplog)


def test_a_due_pair_that_can_never_fit_does_not_hold_the_fill(conn, monkeypatch, caplog):
    """Review item 4: a due gsv grid priced over the WHOLE daily budget is
    skipped every night, so holding on it would switch the fill off for good.
    Killed by removing the `_never_fits_tonight` exclusion."""
    caplog.set_level("INFO")
    giant = _register(conn, "Giant", width=40_000, height=40_000)
    fill = _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    giant_points = sched.estimate_requests(db.resolve_city(conn, giant), "gsv")
    small_points = sched.estimate_requests(db.resolve_city(conn, fill), "gsv")
    budget = giant_points - 1
    assert small_points < budget
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=budget),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert (giant, "gsv") not in ran
    assert (fill, "gsv") in ran and (fill, "mapillary") in ran
    assert any("does not hold the fill" in r.message for r in caplog.records)


def test_a_host_that_latches_on_one_fill_city_declines_the_next(conn, monkeypatch, caplog):
    """Review item 7, live: the tile CDN refuses fill city 1's mapillary, and
    city 2 is declined whole rather than given a gsv-only refresh. Killed by
    handing `_fill_admission` a fresh `HostBreaker()`."""
    from streetscape_metadata_tracker.download_common import HOST_EXIT_CODES, HOST_MAPILLARY_TILES

    caplog.set_level("INFO")
    a = _city(conn, "Alpha", {"gsv": 70, "mapillary": 70})
    b = _city(conn, "Bravo", {"gsv": 60, "mapillary": 60})

    def outcome(city, provider):
        if provider == "mapillary":
            return _outcome(HOST_EXIT_CODES[HOST_MAPILLARY_TILES])
        return True

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(), outcome=outcome)

    assert ran == [(a, "gsv"), (a, "mapillary")]
    assert all(cid != b for cid, _ in ran)
    assert "declined 1 for host refused tonight" in _done_line(caplog)


def test_the_dry_run_draws_its_ledgers_down_across_fill_cities(conn, monkeypatch, capsys):
    """Review item 9: two identical fill cities, a mapillary budget for one --
    the second is declined in the preview. Killed by not subtracting an admitted
    city's price from `budget_left`."""
    a = _city(conn, "Alpha", {"gsv": 70, "mapillary": 70})
    b = _city(conn, "Bravo", {"gsv": 60, "mapillary": 60})
    tiles = sched.estimate_requests(db.resolve_city(conn, a), "mapillary")
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=tiles + tiles // 2),
        }
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    out = capsys.readouterr().out.split("Would FILL", 1)[1]
    assert a in out and b not in out
    assert "Fill: 1 cities admitted; declined 1 for mapillary." in out


def test_the_dry_run_draws_the_fill_ceiling_down_across_fill_cities(conn, monkeypatch, capsys):
    """Killed by leaving the preview's own spend out of the host room's `used`."""
    a = _city(conn, "Alpha", {"gsv": 70, "mapillary": 70})
    b = _city(conn, "Bravo", {"gsv": 60, "mapillary": 60})
    tiles = sched.estimate_requests(db.resolve_city(conn, a), "mapillary")
    cfg = _grid_cfg(fill_host_ceilings={"mapillary_tiles": tiles + tiles // 2})
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    out = capsys.readouterr().out.split("Would FILL", 1)[1]
    assert a in out and b not in out
    assert "Fill: 1 cities admitted; declined 1 for mapillary_tiles." in out


def test_a_non_default_floor_is_the_one_recorded(conn, monkeypatch):
    """Killed by recording a constant instead of `cfg.fill_min_days`."""
    cid = _city(conn, "Old", {"gsv": 50, "mapillary": 50})
    _city(conn, "Young", {"gsv": 40, "mapillary": 40})
    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(fill_min_days=45))
    assert _cities_in(ran) == [cid]
    assert {r[3] for r in _early_rows(conn)} == {45}


def _pair_cfg(data_dir, **kw):
    return _grid_cfg(
        data_dir=data_dir,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
            "mapillary_streets": ProviderConfig(daily_request_budget=kw.pop("walk_budget", 1_750)),
        },
        **kw,
    )


def test_the_mapillary_pair_runs_whole_through_the_fill(conn, monkeypatch, data_dir):
    cid = _city(conn, "Pair", {"gsv": 60, "mapillary": 60, "mapillary_streets": 60})
    _freeze_network(data_dir, cid)
    ran, _ = _run_night(monkeypatch, conn, _pair_cfg(data_dir))
    assert ran == [(cid, "gsv"), (cid, "mapillary"), (cid, "mapillary_streets")]
    assert [r[1] for r in _early_rows(conn)] == ["gsv", "mapillary", "mapillary_streets"]


def test_a_launch_time_budget_skip_inside_the_fill_is_counted(conn, monkeypatch, caplog, data_dir):
    """Admission priced the walk against its remainder, then something else
    spent that remainder before the walk launched: the launch floor-skips it,
    and the night counts the deferral. Killed by dropping the fill's
    `skipped_budget` fold."""
    caplog.set_level("INFO")
    cid = _city(conn, "Pair", {"gsv": 60, "mapillary": 60, "mapillary_streets": 60})
    _freeze_network(data_dir, cid)
    walk = sched.estimate_requests(db.resolve_city(conn, cid), "mapillary_streets")
    assert walk >= 7

    def outcome(city, provider):
        if provider == "mapillary":
            # Another spender draws the walk's daily ledger to 2 under budget.
            db.add_api_usage(conn, TODAY, walk - 2, "mapillary_streets")
        return True

    ran, _ = _run_night(monkeypatch, conn, _pair_cfg(data_dir, walk_budget=walk), outcome=outcome)
    assert ran == [(cid, "gsv"), (cid, "mapillary")]
    assert "1 deferred for budget" in _done_line(caplog)


def test_the_fill_runs_through_two_lanes(conn, monkeypatch, data_dir):
    """max_concurrent_channels=2: the fill city's channels run in lanes and are
    all marked. No ledger writes from the fake (lane workers have no catalog)."""
    cid = _city(conn, "Lanes", {"gsv": 60, "gsv_streets": 60, "mapillary": 60})
    _freeze_network(data_dir, cid)
    cfg = _grid_cfg(
        data_dir=data_dir,
        max_concurrent_channels=2,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "gsv_streets": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
        },
    )
    ran, _ = _run_night(monkeypatch, conn, cfg, record_usage=False)
    assert sorted(ran) == [(cid, "gsv"), (cid, "gsv_streets"), (cid, "mapillary")]
    assert sorted(r[1] for r in _early_rows(conn)) == ["gsv", "gsv_streets", "mapillary"]


# ---------------------------------------------------------------------------
# Jon's decisions (2026-10-02): tomorrow's due room, and alignment
# ---------------------------------------------------------------------------


def _opt_in_cfg(**budgets):
    providers = {
        "gsv": ProviderConfig(daily_request_budget=10_000_000),
        "mapillary": ProviderConfig(daily_request_budget=3_500),
        "kartaview": ProviderConfig(daily_request_budget=budgets.get("kartaview", 10_000)),
        "panoramax": ProviderConfig(daily_request_budget=budgets.get("panoramax", 4_000)),
    }
    return _grid_cfg(providers=providers)


def _tomorrow_city(conn, name, **kw):
    """Due TOMORROW on mapillary (82 days today), not fill-eligible tonight (gsv fresh)."""
    return _city(conn, name, {"gsv": 10, "mapillary": 82}, **kw)


def test_a_heavy_tomorrow_shrinks_tonights_fill(conn, monkeypatch, caplog):
    """The fill may not borrow tomorrow's due room: with tomorrow's mapillary
    demand reserved, a city that fits the bare ceiling no longer fits.
    Killed by dropping the reserve term from `_fill_host_room`."""
    caplog.set_level("INFO")
    fill = _city(conn, "Fill", {"gsv": 60, "mapillary": 60})
    tomorrow = _tomorrow_city(conn, "Tomorrow")
    f_tiles = sched.estimate_requests(db.resolve_city(conn, fill), "mapillary")
    t_tiles = sched.estimate_requests(db.resolve_city(conn, tomorrow), "mapillary")
    cfg = _grid_cfg(fill_host_ceilings={"mapillary_tiles": f_tiles + t_tiles - 1})

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == []
    done = _done_line(caplog)
    assert f"reserved {t_tiles:,} on mapillary_tiles for tomorrow's due (1 cities)" in done
    assert "ended by budget (mapillary_tiles)" in done


def test_an_empty_tomorrow_leaves_the_ceiling(conn, monkeypatch, caplog):
    """Nothing due tomorrow: the reserve is 0 and the ceiling admits exactly
    what it did before."""
    caplog.set_level("INFO")
    fill = _city(conn, "Fill", {"gsv": 60, "mapillary": 60})
    f_tiles = sched.estimate_requests(db.resolve_city(conn, fill), "mapillary")
    cfg = _grid_cfg(fill_host_ceilings={"mapillary_tiles": f_tiles})

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert _cities_in(ran) == [fill]
    assert "reserved 0 on mapillary_tiles for tomorrow's due (0 cities)" in _done_line(caplog)


def test_the_reserve_sums_grid_and_walk_on_one_host_key(conn):
    """One pool per HOST: a grid and a walk on the tile CDN are summed under
    `mapillary_tiles`; a walk whose grid sibling is due the same night is
    priced 0 (the grid's census is cached for it, #290) while an unpaired one
    pays its census; gsv (no rolling host) is not reserved at all; tonight's
    preview correction drops a city's due channels. Killed by keying the
    reserve on the channel, and by pricing a paired walk in full."""
    cid = _city(conn, "Tomorrow", {"gsv": 82, "mapillary": 82, "mapillary_streets": 82})
    city = db.resolve_city(conn, cid)
    lone = _city(conn, "Lone", {"gsv": 10, "mapillary": 10, "mapillary_streets": 82})
    lone_city = db.resolve_city(conn, lone)
    cfg = _grid_cfg(
        fill_host_ceilings={"mapillary_tiles": 2_260},
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
            "mapillary_streets": ProviderConfig(daily_request_budget=1_750),
        },
    )
    want = sched._channel_estimate(cfg, city, "mapillary", conn) + sched._channel_estimate(
        cfg, lone_city, "mapillary_streets", conn
    )
    providers = ["gsv", "mapillary", "mapillary_streets"]
    assert sched._tomorrow_due_reserve(cfg, conn, TODAY, providers) == {
        "mapillary_tiles": (want, 2)
    }
    assert sched._tomorrow_due_reserve(
        cfg,
        conn,
        TODAY,
        providers,
        running_tonight={
            cid: ["mapillary", "mapillary_streets"],
            lone: ["mapillary_streets"],
        },
    ) == {"mapillary_tiles": (0, 0)}
    # A day further out, nothing is due tomorrow either.
    assert sched._tomorrow_due_reserve(cfg, conn, TODAY - timedelta(days=1), providers) == {
        "mapillary_tiles": (0, 0)
    }


def test_the_reserve_is_cut_at_a_channels_daily_budget(conn):
    """Tomorrow cannot spend more on a channel than its daily budget."""
    _tomorrow_city(conn, "Tomorrow", width=40_000, height=40_000)
    cfg = _grid_cfg(
        fill_host_ceilings={"mapillary_tiles": 2_260},
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=5),
        },
    )
    assert sched._tomorrow_due_reserve(cfg, conn, TODAY, ["gsv", "mapillary"]) == {
        "mapillary_tiles": (5, 1)
    }


def test_the_fills_own_spend_counts_against_tomorrows_room(conn, monkeypatch, caplog):
    """cap - reserve is the fill's TOTAL: the second fill city does not fit
    what the first left. Killed by dropping the fill's own spend from the term."""
    caplog.set_level("INFO")
    a = _city(conn, "Alpha", {"gsv": 70, "mapillary": 70})
    _city(conn, "Bravo", {"gsv": 60, "mapillary": 60})
    tomorrow = _tomorrow_city(conn, "Tomorrow", width=40_000, height=40_000)
    f = sched.estimate_requests(db.resolve_city(conn, a), "mapillary")
    t = sched.estimate_requests(db.resolve_city(conn, tomorrow), "mapillary")
    assert t > f
    cfg = _grid_cfg(fill_host_ceilings={"mapillary_tiles": t + f + f // 2})

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert _cities_in(ran) == [a]
    assert "declined 1 for mapillary_tiles" in _done_line(caplog)


def test_the_dry_run_prints_and_applies_tomorrows_reserve(conn, monkeypatch, capsys):
    fill = _city(conn, "Fill", {"gsv": 60, "mapillary": 60})
    tomorrow = _tomorrow_city(conn, "Tomorrow")
    f_tiles = sched.estimate_requests(db.resolve_city(conn, fill), "mapillary")
    t_tiles = sched.estimate_requests(db.resolve_city(conn, tomorrow), "mapillary")
    cfg = _grid_cfg(fill_host_ceilings={"mapillary_tiles": f_tiles + t_tiles - 1})
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    out = capsys.readouterr().out
    assert f"Fill: reserved {t_tiles:,} on mapillary_tiles for tomorrow's due (1 cities)" in out
    assert "Fill: 0 cities admitted; declined 1 for mapillary_tiles." in out


def test_the_fill_runs_every_member_channel_opt_ins_included(conn, monkeypatch):
    """Jon: "every night we should try to get the same exact cities across all
    providers". An enrolled city is refreshed on gsv, mapillary AND its opt-in
    channels on one date; a city not enrolled is never run there. Killed by
    filling only the default-membership channels."""
    cid = _city(conn, "Enrolled", {"gsv": 60, "mapillary": 60, "kartaview": 60, "panoramax": 60})
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)
    db.set_channel_membership(conn, cid, "panoramax", True, cycle_days=90)
    other = _city(conn, "Plain", {"gsv": 50, "mapillary": 50})

    ran, _ = _run_night(monkeypatch, conn, _opt_in_cfg())

    assert ran == [
        (cid, "gsv"),
        (cid, "mapillary"),
        (cid, "kartaview"),
        (cid, "panoramax"),
        (other, "gsv"),
        (other, "mapillary"),
    ]
    assert sorted(r[1] for r in _early_rows(conn) if r[0] == cid) == [
        "gsv",
        "kartaview",
        "mapillary",
        "panoramax",
    ]


def test_an_opt_in_channel_that_does_not_fit_declines_the_whole_city(conn, monkeypatch, caplog):
    """Whole-city admission spans the opt-in channels too: KartaView over its
    daily budget means no gsv or mapillary run for that city either."""
    caplog.set_level("INFO")
    cid = _city(conn, "Enrolled", {"gsv": 60, "mapillary": 60, "kartaview": 60})
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)

    ran, _ = _run_night(monkeypatch, conn, _opt_in_cfg(kartaview=1))

    assert ran == []
    assert "declined 1 for kartaview" in _done_line(caplog)


def test_a_city_the_due_phase_ran_on_an_opt_in_channel_is_realigned(conn, monkeypatch, caplog):
    """A late KartaView enrolment: never collected there, so DUE and hoisted.
    The due phase runs kartaview; the fill then runs gsv and mapillary the same
    night, so all three share a date. Only the fill's runs are early refreshes.
    Killed by excluding tonight's due cities from the fill."""
    caplog.set_level("INFO")
    cid = _city(conn, "Late", {"gsv": 60, "mapillary": 60})
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)

    ran, _ = _run_night(monkeypatch, conn, _opt_in_cfg())

    assert ran == [(cid, "kartaview"), (cid, "gsv"), (cid, "mapillary")]
    assert [r[1] for r in _early_rows(conn)] == ["gsv", "mapillary"]
    dates = {
        r[0][:10]
        for r in conn.execute(
            "SELECT last_success_at FROM schedule_state WHERE city_id = ? "
            "AND provider IN ('gsv', 'mapillary', 'kartaview')",
            (cid,),
        )
    }
    assert dates == {"2026-10-01"}
    assert "1 realigned" in _done_line(caplog)


def test_misaligned_cities_go_first_among_equally_stale(conn):
    """Same oldest day; the city whose channels disagree goes first, although
    it sorts later by name. Killed by dropping the misalignment key."""
    aligned = _city(conn, "Aaa", {"gsv": 60, "mapillary": 60})
    misaligned = _city(conn, "Zzz", {"gsv": 60, "mapillary": 40})
    older = _city(conn, "Old", {"gsv": 70, "mapillary": 70})
    assert [c.city_id for c, _ in _candidates(conn)] == [older, misaligned, aligned]


def test_only_an_early_success_is_marked_an_early_refresh():
    cfg = _grid_cfg()
    assert sched._is_early(cfg, TODAY, (MIDNIGHT - timedelta(days=82)).isoformat())
    assert not sched._is_early(cfg, TODAY, (MIDNIGHT - timedelta(days=83)).isoformat())
    assert not sched._is_early(cfg, TODAY, None)
    # A naive legacy stamp is read as UTC.
    assert sched._is_early(cfg, TODAY, "2026-09-01T00:00:00")


# ---------------------------------------------------------------------------
# Review round 3 (PR #411, review-411c)
# ---------------------------------------------------------------------------


def test_a_due_city_whose_grid_needs_more_than_the_window_does_not_hold_the_fill(
    conn, monkeypatch, caplog
):
    """Review-411c #1 (probe P1): the #373 gate defers the giant's gsv AND, to
    keep the pair, its mapillary -- resumable, so it never looked never-fits on
    its own, and it held the fill every night. Killed by dropping the window
    arm, and by dropping the first-grid cascade."""
    caplog.set_level("INFO")
    giant = _register(conn, "Giant")
    fill = _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    real = sched.city_timeout_estimate_seconds

    def est(cfg, city, channel, conn=None, **k):
        if city.city_id == giant and channel == "gsv":
            return int(13 * 3600)
        return real(cfg, city, channel, conn=conn, **k)

    monkeypatch.setattr(sched, "city_timeout_estimate_seconds", est)
    ran, _ = _run_night(monkeypatch, conn, _grid_cfg())

    assert all(c != giant for c, _ in ran)
    assert (fill, "gsv") in ran and (fill, "mapillary") in ran
    assert any("deferred with its first grid channel gsv" in r.message for r in caplog.records)


def test_a_paused_fill_grid_resumes_with_the_walk_it_held_back(conn, monkeypatch, caplog, data_dir):
    """Review-411c #2 (probe P2): night 1 pauses mapillary, so its walk is
    deferred behind the sibling sweep; night 2's resume runs the walk too, and
    gsv beside them, so the whole city lands on one date."""
    from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE

    caplog.set_level("INFO")
    cid = _city(conn, "Pair", {"gsv": 60, "mapillary": 60, "mapillary_streets": 60})
    _freeze_network(data_dir, cid)
    live: set = set()
    _live_checkpoints(monkeypatch, live)

    def n1(city, p):
        if p == "mapillary":
            live.add((cid, "mapillary"))
            return _outcome(SWEEP_INCOMPLETE_EXIT_CODE)
        return True

    ran1, _ = _run_night(monkeypatch, conn, _pair_cfg(data_dir), outcome=n1)
    assert ran1 == [(cid, "gsv"), (cid, "mapillary")], "the walk was deferred behind it"
    monkeypatch.setattr(clock, "_utc_clock", lambda: NOW + timedelta(days=1))

    def n2(city, p):
        live.discard((city.city_id, p))
        return True

    ran2, _ = _run_night(
        monkeypatch,
        conn,
        # A one-city night: the walk must come back WITH the resume, not from a
        # later refresh of the same city (which a cap of 1 never reaches).
        _pair_cfg(data_dir, max_cities_per_day=1),
        outcome=n2,
        today=TODAY + timedelta(days=1),
    )
    assert ran2 == [(cid, "gsv"), (cid, "mapillary"), (cid, "mapillary_streets")]
    days = {
        r[0][:10]
        for r in conn.execute(
            "SELECT last_success_at FROM schedule_state WHERE city_id = ? AND provider IN "
            "('gsv', 'mapillary', 'mapillary_streets')",
            (cid,),
        )
    }
    assert days == {"2026-10-02"}, "one date across the city"


def test_the_dry_run_does_not_hold_on_a_due_pair_that_can_never_fit(conn, monkeypatch, capsys):
    """Review-411c #3: the preview's hold reads `_never_fits_tonight` as the
    night does, so a due gsv priced over the whole budget holds nothing."""
    giant = _register(conn, "Giant", width=40_000, height=40_000)
    fill = _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    budget = sched.estimate_requests(db.resolve_city(conn, giant), "gsv") - 1
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=budget),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
        }
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    out = capsys.readouterr().out
    assert "Fill: holding gsv" not in out
    assert fill in out.split("Would FILL", 1)[1]


def test_a_resumer_never_runs_a_failing_channel(conn, monkeypatch):
    """Review-411c #4 (probe P3): a quarantined (or once-failed) paused channel
    is not resumed. Killed by dropping the failure test in `_resumable_member`."""
    cid = _city(conn, "Q", {"gsv": 2, "mapillary": 60})
    _seed(conn, cid, "mapillary", 60, failures=1)
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(cid, "mapillary")})
    ran, _ = _run_night(monkeypatch, conn, _grid_cfg())
    assert (cid, "mapillary") not in ran


def test_an_orphan_with_no_success_at_all_is_still_resumed(conn, monkeypatch):
    """Review-411c #5 (probe P4): gsv FAILED and mapillary PAUSED, so no early
    refresh was written; the fill ATTEMPT is what finds the orphan. Killed by
    keying resumers on early_refreshes again."""
    from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE

    cid = _city(conn, "P", {"gsv": 60, "mapillary": 60})
    live: set = set()
    _live_checkpoints(monkeypatch, live)

    def n1(city, p):
        if p == "gsv":
            return _outcome(1)
        live.add((cid, "mapillary"))
        return _outcome(SWEEP_INCOMPLETE_EXIT_CODE)

    _run_night(monkeypatch, conn, _grid_cfg(), outcome=n1)
    assert db.get_early_refresh_keys(conn) == set()
    monkeypatch.setattr(clock, "_utc_clock", lambda: NOW + timedelta(days=1))
    ran2, _ = _run_night(monkeypatch, conn, _grid_cfg(), today=TODAY + timedelta(days=1))
    # gsv failed, so it is not realigned (no failure-carrying channel is run).
    assert ran2 == [(cid, "mapillary")]


def test_a_city_due_tonight_on_another_channel_still_has_its_orphan_resumed(conn, monkeypatch):
    """Review-411c #5: a city due tonight only on KartaView is not skipped
    whole -- its fill-paused mapillary is resumed (the due slate does not hold
    mapillary for it). Killed by excluding every due city."""
    cid = _city(conn, "Both", {"gsv": 2, "mapillary": 60})
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(cid, "mapillary")})
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
            "kartaview": ProviderConfig(daily_request_budget=10_000),
        }
    )
    ran, _ = _run_night(monkeypatch, conn, cfg)
    assert (cid, "kartaview") in ran and (cid, "mapillary") in ran


def test_a_channel_due_tonight_is_never_resumed_by_the_fill(conn, monkeypatch):
    """The other half: a checkpoint on a channel the due slate holds is the due
    path's to resume, so the fill does not launch it a second time."""
    cid = _city(conn, "Due", {"gsv": 2, "mapillary": 85})
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(cid, "mapillary")})
    ran, _ = _run_night(monkeypatch, conn, _grid_cfg())
    assert ran.count((cid, "mapillary")) == 1


def test_resumers_are_members_of_enabled_cities_only(conn, monkeypatch):
    """Killed by dropping the membership test, and by dropping the enabled test."""
    outsider = _city(conn, "Outsider", {"gsv": 2, "mapillary": 60, "kartaview": 60})
    off = _city(conn, "Off", {"gsv": 2, "mapillary": 60})
    conn.execute("UPDATE cities SET enabled = 0 WHERE city_id = ?", (off,))
    conn.commit()
    for cid in (outsider, off):
        db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(outsider, "kartaview"), (off, "mapillary")})
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
            "kartaview": ProviderConfig(daily_request_budget=10_000),
        }
    )
    ran, _ = _run_night(monkeypatch, conn, cfg)
    assert (outsider, "kartaview") not in ran
    assert all(c != off for c, _ in ran)


def test_the_checkpoint_nearest_its_age_wall_is_resumed_first(conn, monkeypatch):
    """With room for one city, the older checkpoint wins. Killed by ordering
    resumers youngest-first."""
    young = _city(conn, "Aaa", {"gsv": 2, "mapillary": 60})
    old = _city(conn, "Zzz", {"gsv": 2, "mapillary": 60})
    for cid in (young, old):
        db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(
        monkeypatch, {(young, "mapillary"), (old, "mapillary")}, ages={young: 3600.0, old: 86400.0}
    )
    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(max_cities_per_day=1))
    assert _cities_in(ran) == [old]


def test_the_launch_check_draws_the_host_and_the_clock_down_in_launch_order(conn, monkeypatch):
    """Review-411c #6/#9: each resumable channel is planned against what the
    city's EARLIER channels leave of the host room and of the clock. Killed by
    not drawing either down."""
    cid = _city(conn, "Pair", {"gsv": 60, "mapillary": 60, "mapillary_streets": 60})
    city = db.resolve_city(conn, cid)
    seen = []

    def plan(cfg, city, channel, conn, *, est, remaining, remaining_s, **_):
        seen.append((channel, remaining, remaining_s))
        return sched.SweepLaunchPlan(60, max(est, remaining), remaining, None, "", "")

    monkeypatch.setattr(sched, "_sweep_launch_plan", plan)
    v = sched._fill_launch_refusal(
        _grid_cfg(),
        conn,
        city,
        ["gsv", "mapillary", "mapillary_streets"],
        est={"gsv": 1_000, "mapillary": 40, "mapillary_streets": 30},
        channel_room={"gsv": 10**9, "mapillary": 3_500, "mapillary_streets": 1_750},
        host_room={"mapillary_tiles": 100},
        remaining_s=10_000.0,
        need_s={"gsv": 1_200, "mapillary": 800, "mapillary_streets": None},
    )
    assert v is None
    assert seen == [("mapillary", 100, 8_800.0), ("mapillary_streets", 60, 8_000.0)]


def test_the_fills_retry_pass_leaves_the_due_phases_strandings_alone(
    conn, monkeypatch, caplog, data_dir
):
    """Review-411c #7: one due-phase stranding (already retried by the due
    pass) and one fill stranding (a resume that realigned gsv_streets). The
    fill's pass asks only about its own -- without `only=` it reached for a
    city it never touched and the fill died. The subject counts only the due
    stranding; the fill's is "not lost". Killed by dropping `only=`, and by
    dropping the filter it feeds."""
    from streetscape_metadata_tracker.download_common import HOST_EXIT_CODES, HOST_OVERPASS

    caplog.set_level("INFO")
    due = _register(conn, "Due")
    resumed = _city(conn, "Resumed", {"gsv": 2, "gsv_streets": 2, "mapillary": 60})
    db.record_fill_attempt(conn, resumed, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(resumed, "mapillary")})
    for cid in (due, resumed):
        _freeze_network(data_dir, cid)
    cfg = _grid_cfg(
        data_dir=data_dir,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "gsv_streets": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
        },
    )
    subjects = []
    ran, _ = _run_night(
        monkeypatch,
        conn,
        cfg,
        outcome=lambda city, p: (
            _outcome(HOST_EXIT_CODES[HOST_OVERPASS]) if p == "gsv_streets" else True
        ),
    )
    assert (resumed, "gsv_streets") in ran
    done = _done_line(caplog)
    assert "unexpected error in the fill phase" not in done
    assert "2 city(ies) STRANDED un-walked (1 in the fill, not lost)" in done

    # The subject: one due stranding, not two.
    breaker = sched.HostBreaker()
    breaker.strand(due, "gsv_streets")
    breaker.strand(resumed, "gsv_streets")
    breaker.fill_stranded[(resumed, "gsv_streets")] = "2026-10-24"
    breaker.add(HOST_OVERPASS)
    breaker.latched.add(HOST_OVERPASS)
    monkeypatch.setattr(sched, "send_alert", lambda cfg, subject, body: subjects.append(subject))
    cfg.alerts.enabled = True
    sched._finish_batch(
        cfg, conn, "summary", succeeded=1, attempted=1, today=TODAY, blocked_hosts=breaker
    )
    assert subjects and "1 city(ies) STRANDED un-walked" in subjects[0]


def test_a_fill_crash_is_named_as_the_fills_in_the_subject(conn, monkeypatch, caplog):
    """Review-411c #10: the subject says FILL CRASHED, not LOOP CRASHED."""
    _city(conn, "Eligible", {"gsv": 60, "mapillary": 60})
    seen = {}

    def boom(*a, **k):
        raise RuntimeError("simulated")

    real = sched._finish_batch

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(sched.db, "get_fill_candidates", boom)
    monkeypatch.setattr(sched, "_finish_batch", spy)
    _run_night(monkeypatch, conn, _grid_cfg())
    assert seen["crashed"] == "FILL" and seen["errored"] is True


def test_a_walk_the_fill_did_not_hold_behind_a_sibling_checkpoint(
    conn, monkeypatch, caplog, data_dir
):
    """Review-411c #8 (M9): a due walk deferred behind its grid sibling's
    in-flight sweep does not hold its channel -- a fill city on that walk
    channel still runs. Killed by dropping the sibling arm."""
    from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE

    caplog.set_level("INFO")
    due = _city(conn, "Due", {"gsv": 10})  # due on both mapillary channels (never)
    fill = _city(conn, "Walker", {"gsv": 60, "mapillary_streets": 60})
    db.set_channel_membership(conn, fill, "mapillary", False, cycle_days=90)
    _freeze_network(data_dir, fill)
    live: set = set()
    _live_checkpoints(monkeypatch, live)

    def outcome(city, p):
        if city.city_id == due and p == "mapillary":
            live.add((due, "mapillary"))
            return _outcome(SWEEP_INCOMPLETE_EXIT_CODE)
        return True

    ran, _ = _run_night(monkeypatch, conn, _pair_cfg(data_dir), outcome=outcome)
    assert (due, "mapillary_streets") not in ran
    assert (fill, "mapillary_streets") in ran


def test_the_dry_run_counts_tonights_due_spend_against_the_host_budget(conn, monkeypatch, capsys):
    """The preview's host room reads its own simulated due spend: with a host
    budget the due city's mapillary leaves the fill city one tile short.
    Killed by leaving the preview's spend out of the host room's `used`."""
    due = _register(conn, "Due")
    fill = _city(conn, "Fill", {"gsv": 60, "mapillary": 60})
    d = sched.estimate_requests(db.resolve_city(conn, due), "mapillary")
    f = sched.estimate_requests(db.resolve_city(conn, fill), "mapillary")
    cfg = _grid_cfg(host_budgets={"mapillary_tiles": d + f - 1})
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    out = capsys.readouterr().out
    assert "Fill: 0 cities admitted; declined 1 for mapillary_tiles." in out


def test_the_dry_runs_reserve_assumes_tonights_due_slate_succeeds(conn, monkeypatch, capsys):
    """A city due tonight is not tomorrow's demand in the preview (it is
    assumed to run tonight). Killed by not handing the preview's reserve
    tonight's slate."""
    _register(conn, "Due")
    _city(conn, "Fill", {"gsv": 60, "mapillary": 60})
    cfg = _grid_cfg(fill_host_ceilings={"mapillary_tiles": 2_260})
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(cfg, dry_run=True, today=TODAY)
    assert "Fill: reserved 0 on mapillary_tiles for tomorrow's due (0 cities)" in (
        capsys.readouterr().out
    )


def test_the_reserve_is_cut_at_the_city_cap(conn):
    """Tomorrow cannot run more than max_cities_per_day cities on a channel:
    two due tomorrow at a cap of one reserve the stalest one only. Killed by
    not cutting the list."""
    older = _city(conn, "Older", {"gsv": 10, "mapillary": 82.5})
    _tomorrow_city(conn, "Newer")
    cfg = _grid_cfg(max_cities_per_day=1, fill_host_ceilings={"mapillary_tiles": 2_260})
    want = sched.estimate_requests(db.resolve_city(conn, older), "mapillary")
    assert sched._tomorrow_due_reserve(cfg, conn, TODAY, ["gsv", "mapillary"]) == {
        "mapillary_tiles": (want, 1)
    }


# ---------------------------------------------------------------------------
# Review round 4 (PR #411, 3118ac5..686ebff)
# ---------------------------------------------------------------------------


def _capture_caps(monkeypatch):
    """Record each launch's request cap: {(city_id, channel): request_cap}."""
    caps = {}
    real = sched._run_one_city

    def spy(cfg, city, run_today, provider="gsv", request_cap=None, **kw):
        caps[(city.city_id, provider)] = request_cap
        return real(cfg, city, run_today, provider=provider, request_cap=request_cap, **kw)

    return caps, spy


def test_a_partly_collected_city_is_finished_before_a_staler_one(conn, monkeypatch):
    """Review 411d #2 (P1): A is due on gsv+mapillary (85 d) and enrolled on
    KartaView at 40 d; B is aligned at 80 d. One fill slot: A's kartaview, so A
    ends the night aligned -- tomorrow its fresh gsv is under the floor and A
    is not a candidate again for a month. Killed by ranking staleness first."""
    a = _city(conn, "Aaa", {"gsv": 85, "mapillary": 85, "kartaview": 40})
    db.set_channel_membership(conn, a, "kartaview", True, cycle_days=90)
    b = _city(conn, "Bbb", {"gsv": 80, "mapillary": 80})
    cfg = _opt_in_cfg()
    cfg.max_cities_per_day = 2

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert (a, "kartaview") in ran
    assert all(c != b for c, _ in ran)


def test_the_dry_run_previews_finishing_a_partly_due_city(conn, monkeypatch, capsys):
    """Review 411d #8: the preview assumes tonight's due slate succeeds and so
    shows the fill finishing a due city on its other channels. Killed by
    excluding due cities from the preview again."""
    a = _city(conn, "Aaa", {"gsv": 85, "mapillary": 85, "kartaview": 40})
    db.set_channel_membership(conn, a, "kartaview", True, cycle_days=90)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(_opt_in_cfg(), dry_run=True, today=TODAY)
    fill = capsys.readouterr().out.split("Would FILL", 1)[1]
    assert a in fill and "kartaview" in fill
    assert "Fill: 1 cities admitted." in fill


def test_a_fill_launch_cap_reaches_the_child(conn, monkeypatch):
    """Review 411d #3 (P6): the cap the child receives is the FILL's room -- a
    12-tile ceiling caps mapillary at <= 12, not at the 3,500 daily (or 3,000
    rolling) remainder the due slate keeps; KartaView likewise at its fill
    ceiling, not its 10,000 budget. Killed by not handing the launch `fill_cap`."""
    cid = _city(
        conn, "Small", {"gsv": 60, "mapillary": 60, "kartaview": 60}, width=2_000, height=2_000
    )
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)
    city = db.resolve_city(conn, cid)
    tiles = sched.estimate_requests(city, "mapillary")
    cfg = _opt_in_cfg()
    karta = sched._channel_estimate(cfg, city, "kartaview", conn)
    # Above KartaView's 34-request launch floor, far below its 10,000 budget.
    ceiling = max(karta, 34) + 10
    cfg.fill_host_ceilings = {"mapillary_tiles": 12, "kartaview": ceiling}
    assert tiles <= 12
    caps = {}

    def fake_run(cfg, city, run_today, provider="gsv", request_cap=None, **_):
        caps[provider] = request_cap
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    sched.cmd_run_due(cfg, today=TODAY)

    assert caps["mapillary"] is not None and caps["mapillary"] <= 12
    assert caps["kartaview"] is not None and caps["kartaview"] <= ceiling


def test_a_paused_walk_alone_is_resumed_alone(conn, monkeypatch, data_dir):
    """Review 411d #4 (P7): only the WALK paused, so the resume does not re-run
    gsv beside it -- that would leave mapillary on the old date and split the
    pair. Killed by realigning whenever anything is paused."""
    cid = _city(conn, "Walk", {"gsv": 2, "mapillary": 2, "mapillary_streets": 60})
    _freeze_network(data_dir, cid)
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(cid, "mapillary_streets")})
    ran, _ = _run_night(monkeypatch, conn, _pair_cfg(data_dir))
    assert ran == [(cid, "mapillary_streets")]


def test_a_free_walk_extra_needs_a_frozen_network(conn, monkeypatch, data_dir):
    """The free realignment skips a walk whose network is not frozen (the fill
    sends Overpass nothing): gsv is re-run beside the resumed mapillary, the
    unfrozen gsv_streets is not. Killed by dropping the frozen check."""
    cid = _city(conn, "Grid", {"gsv": 2, "gsv_streets": 2, "mapillary": 60})
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _live_checkpoints(monkeypatch, {(cid, "mapillary")})
    cfg = _grid_cfg(
        data_dir=data_dir,
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "gsv_streets": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
        },
    )
    ran, _ = _run_night(monkeypatch, conn, cfg)
    assert ran == [(cid, "gsv"), (cid, "mapillary")]


def test_a_city_due_tomorrow_is_credited_its_own_reserve(conn, monkeypatch, caplog):
    """Review 411d #5 (P2): an 82-day city is due tomorrow; refreshing it
    tonight REMOVES its own tomorrow demand, so it fits a ceiling of 1.5x its
    tiles. Killed by not crediting the judged city."""
    caplog.set_level("INFO")
    cid = _city(conn, "Eighty", {"gsv": 82, "mapillary": 82})
    t = sched.estimate_requests(db.resolve_city(conn, cid), "mapillary")
    ran, _ = _run_night(
        monkeypatch, conn, _grid_cfg(fill_host_ceilings={"mapillary_tiles": t + t // 2})
    )
    assert _cities_in(ran) == [cid]


def test_a_city_the_fill_pulled_forward_leaves_tomorrows_demand(conn, monkeypatch):
    """After an 82-day city is collected tonight its tomorrow demand is gone,
    which makes room for a second fill city. Killed by not crediting what the
    fill pulled forward."""
    x = _city(conn, "Xxx", {"gsv": 82, "mapillary": 82})
    y = _city(conn, "Yyy", {"gsv": 60, "mapillary": 60})
    z = _tomorrow_city(conn, "Zzz")
    t = sched.estimate_requests(db.resolve_city(conn, x), "mapillary")
    zt = sched.estimate_requests(db.resolve_city(conn, z), "mapillary")
    ran, _ = _run_night(
        monkeypatch, conn, _grid_cfg(fill_host_ceilings={"mapillary_tiles": 2 * t + zt})
    )
    assert _cities_in(ran) == [x, y]


def test_a_hosts_reserve_is_cut_at_its_cap(conn):
    """Tomorrow cannot spend more on a host than its cap."""
    _tomorrow_city(conn, "Big", width=40_000, height=40_000)
    cfg = _grid_cfg(fill_host_ceilings={"mapillary_tiles": 7})
    assert sched._tomorrow_due_reserve(cfg, conn, TODAY, ["gsv", "mapillary"]) == {
        "mapillary_tiles": (7, 1)
    }


def test_a_failing_opt_in_channel_does_not_freeze_the_city_out(conn, monkeypatch, caplog):
    """Review 411d #6: a failing KartaView is dropped from the run, the city is
    still refreshed on its other channels, and the night says why it is not
    aligned. A failing DEFAULT channel still keeps the city out (the
    candidates test). Killed by skipping the whole city again."""
    caplog.set_level("INFO")
    cid = _city(conn, "Karta", {"gsv": 60, "mapillary": 60, "kartaview": 60})
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)
    _seed(conn, cid, "kartaview", 60, failures=2)

    ran, _ = _run_night(monkeypatch, conn, _opt_in_cfg())

    assert ran == [(cid, "gsv"), (cid, "mapillary")]
    assert "realign blocked: kartaview failing (1)" in _done_line(caplog)


def test_a_never_collected_opt_in_channel_is_the_due_phases_not_the_fills(conn):
    """Review 411d #7: an opt-in channel never collected (or overdue) is DUE --
    the due phase and its bounded hoist catch it up, and the fill finishes the
    city's other channels the same night. As a candidate on its own it is not
    admissible."""
    never = _city(conn, "Never", {"gsv": 60, "mapillary": 60})
    db.set_channel_membership(conn, never, "panoramax", True, cycle_days=90)
    fresh = _city(conn, "Fresh", {"gsv": 60, "mapillary": 60, "kartaview": 10})
    db.set_channel_membership(conn, fresh, "kartaview", True, cycle_days=90)
    assert _candidates(conn, channels=("gsv", "mapillary", "kartaview", "panoramax")) == []


def test_a_paused_kartaview_due_sweep_holds_kartaview_visibly(conn, monkeypatch, caplog):
    """Review 411d #9: a due KartaView sweep that pauses holds the kartaview
    channel -- an enrolled city is declined -- while a city without KartaView
    is filled, and the Done line names the hold. Killed by dropping the
    held count from the report."""
    from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE

    caplog.set_level("INFO")
    due = _city(conn, "Due", {"gsv": 10, "mapillary": 10})
    db.set_channel_membership(conn, due, "kartaview", True, cycle_days=90)
    enrolled = _city(conn, "Enrolled", {"gsv": 60, "mapillary": 60, "kartaview": 60})
    db.set_channel_membership(conn, enrolled, "kartaview", True, cycle_days=90)
    plain = _city(conn, "Plain", {"gsv": 50, "mapillary": 50})

    def outcome(city, p):
        if city.city_id == due and p == "kartaview":
            return _outcome(SWEEP_INCOMPLETE_EXIT_CODE)
        return True

    ran, _ = _run_night(monkeypatch, conn, _opt_in_cfg(), outcome=outcome)
    assert all(c != enrolled for c, _ in ran)
    assert (plain, "gsv") in ran
    assert "holding kartaview (1 due not attempted)" in _done_line(caplog)


def test_the_fills_own_spend_excludes_what_was_already_in_the_window(conn, monkeypatch):
    """The fill's spend is measured from its BASELINE: spend already in the
    window when the fill began (yesterday, or tonight's due work) is not the
    fill's, and charging it against tomorrow's term declines a city that fits.
    Killed by ignoring the baseline (M15)."""
    x = _city(conn, "Xxx", {"gsv": 60, "mapillary": 60})
    z = _tomorrow_city(conn, "Zzz")
    t = sched.estimate_requests(db.resolve_city(conn, x), "mapillary")
    r = sched.estimate_requests(db.resolve_city(conn, z), "mapillary")
    db.add_api_usage(conn, TODAY - timedelta(days=1), r, "mapillary")  # in the window
    ran, _ = _run_night(monkeypatch, conn, _grid_cfg(fill_host_ceilings={"mapillary_tiles": r + t}))
    assert _cities_in(ran) == [x]


def test_old_fill_attempts_are_pruned(conn, monkeypatch):
    """`fill_attempts` keeps only what can still hold a live checkpoint."""
    cid = _city(conn, "Old", {"gsv": 10, "mapillary": 10})
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=30))
    db.record_fill_attempt(conn, cid, TODAY - timedelta(days=2))
    _run_night(monkeypatch, conn, _grid_cfg())
    days = [r[0] for r in conn.execute("SELECT run_date FROM fill_attempts")]
    assert days == [(TODAY - timedelta(days=2)).isoformat()]


def test_the_dry_run_credits_a_city_its_own_tomorrow(conn, monkeypatch, capsys):
    """The preview credits the judged city its own tomorrow demand, as the
    night does (P2): an 82-day city fits a 1.5x ceiling. Killed by not
    crediting it in the preview."""
    cid = _city(conn, "Eighty", {"gsv": 82, "mapillary": 82})
    t = sched.estimate_requests(db.resolve_city(conn, cid), "mapillary")
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    sched.cmd_run_due(
        _grid_cfg(fill_host_ceilings={"mapillary_tiles": t + t // 2}), dry_run=True, today=TODAY
    )
    assert "Fill: 1 cities admitted." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Review round 5 (PR #411, 411e)
# ---------------------------------------------------------------------------


def test_the_fills_walk_retry_is_a_fill_launch(conn, monkeypatch, data_dir):
    """Review 411e #2: the fill's stranded-walk retry launches through
    `_run_city_channels` with `fill=True` and the fill's `fill_cap`, so a
    retried mapillary walk is sized to the fill room, not the due budget.
    Killed by not threading either through."""
    from streetscape_metadata_tracker.download_common import HOST_EXIT_CODES, HOST_OVERPASS

    cid = _city(conn, "Pair", {"gsv": 60, "mapillary": 60, "mapillary_streets": 60})
    _freeze_network(data_dir, cid)
    calls = []
    real = sched._run_city_channels

    def spy(*a, **k):
        calls.append((a[4], k.get("fill"), k.get("fill_cap")))
        return real(*a, **k)

    monkeypatch.setattr(sched, "_run_city_channels", spy)

    def outcome(city, p):
        return _outcome(HOST_EXIT_CODES[HOST_OVERPASS]) if p == "mapillary_streets" else True

    cfg = _pair_cfg(data_dir, fill_host_ceilings={"mapillary_tiles": 400})
    _run_night(monkeypatch, conn, cfg, outcome=outcome)
    retries = [c for c in calls if c[0] == ["mapillary_streets"]]
    assert retries, "the fill retried its stranded walk"
    assert all(fill is True and cap is not None for _ch, fill, cap in retries)


def test_a_retried_fill_walk_is_capped_at_the_fill_room(conn, monkeypatch, data_dir):
    """The cap the retried walk's child receives is within the fill ceiling."""
    from streetscape_metadata_tracker.download_common import HOST_EXIT_CODES, HOST_OVERPASS

    cid = _city(conn, "Pair", {"gsv": 60, "mapillary": 60, "mapillary_streets": 60})
    _freeze_network(data_dir, cid)
    caps = []

    def fake_run(cfg, city, run_today, provider="gsv", request_cap=None, **_):
        if provider == "mapillary_streets":
            caps.append(request_cap)
            return _outcome(HOST_EXIT_CODES[HOST_OVERPASS])
        return True

    monkeypatch.setattr(sched, "_run_one_city", fake_run)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {"walks": []})
    monkeypatch.setattr(sched, "send_alert", lambda *a, **k: None)
    sched.cmd_run_due(_pair_cfg(data_dir, fill_host_ceilings={"mapillary_tiles": 400}), today=TODAY)
    assert len(caps) >= 2, "first launch plus the retry"
    assert all(c is not None and c <= 400 for c in caps), caps


def test_realigned_counts_only_a_city_that_ended_aligned(conn, monkeypatch, caplog):
    """Review 411e #3 (probe): gsv 60, mapillary 60, kartaview 40 FAILING --
    kartaview is dropped, the city ends on two dates, and the Done line must
    not call it realigned. Killed by counting misaligned-before alone."""
    caplog.set_level("INFO")
    cid = _city(conn, "Karta", {"gsv": 60, "mapillary": 60, "kartaview": 40})
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)
    _seed(conn, cid, "kartaview", 40, failures=1)

    ran, _ = _run_night(monkeypatch, conn, _opt_in_cfg())

    assert ran == [(cid, "gsv"), (cid, "mapillary")]
    done = _done_line(caplog)
    assert "realign blocked: kartaview failing (1)" in done
    assert "realigned" not in done


def test_a_failed_run_is_not_counted_realigned(conn, monkeypatch, caplog):
    caplog.set_level("INFO")
    _city(conn, "Mis", {"gsv": 70, "mapillary": 40})
    _run_night(monkeypatch, conn, _grid_cfg(), outcome=lambda city, p: p == "gsv")
    assert "realigned" not in _done_line(caplog)


def test_credits_let_the_next_city_into_tomorrows_window_and_reserve_it(conn):
    """Review 411e #4: the city cap is applied AFTER credits. At a cap of 1 with
    the stalest tomorrow city credited (the fill ran it), the NEXT one moves
    into tomorrow's window and must be reserved. Killed by cutting first."""
    a = _city(conn, "Aaa", {"gsv": 10, "mapillary": 82.5})
    b = _tomorrow_city(conn, "Bbb")
    cfg = _grid_cfg(max_cities_per_day=1, fill_host_ceilings={"mapillary_tiles": 2_260})
    want = sched.estimate_requests(db.resolve_city(conn, b), "mapillary")
    assert sched._tomorrow_due_reserve(
        cfg, conn, TODAY, ["gsv", "mapillary"], credit={(a, "mapillary")}
    ) == {"mapillary_tiles": (want, 1)}


def test_a_paired_walk_pays_when_its_grid_will_not_run_tomorrow(conn):
    """Review 411e #5: the walk is free only when its grid sibling survives
    tomorrow's grid budget cut. Two cities due tomorrow on both channels, a
    grid budget for the stalest one only: that one's walk is 0, the other's
    walk pays its census. Killed by pricing every paired walk at 0."""
    a = _city(conn, "Aaa", {"gsv": 10, "mapillary": 82.5, "mapillary_streets": 82.5})
    b = _city(conn, "Bbb", {"gsv": 10, "mapillary": 82, "mapillary_streets": 82})
    city_a, city_b = db.resolve_city(conn, a), db.resolve_city(conn, b)
    grid_a = sched.estimate_requests(city_a, "mapillary")
    cfg = _grid_cfg(
        fill_host_ceilings={"mapillary_tiles": 100_000},
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=grid_a),
            "mapillary_streets": ProviderConfig(daily_request_budget=100_000),
        },
    )
    walk_b = sched._channel_estimate(cfg, city_b, "mapillary_streets", conn)
    assert walk_b > 0
    got = sched._tomorrow_due_reserve(cfg, conn, TODAY, ["gsv", "mapillary", "mapillary_streets"])
    assert got == {"mapillary_tiles": (grid_a + walk_b, 2)}
