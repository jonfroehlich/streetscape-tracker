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
        (r["city_id"], r["provider"], r["run_date"], r["floor_days"])
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
        max_consecutive_failures=5,
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
    assert "fill (early refresh, >= 30 d): 3 cities, 6/6 runs, 6 early refresh(es)" in done
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
    assert "declined 1 for mapillary" in _done_line(caplog)


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
    """Fill never outranks due, even when the fill city is far staler on one channel."""
    fill = _city(conn, "Fill", {"gsv": 82, "mapillary": 82})
    due = _city(conn, "Due", {"gsv": 83, "mapillary": 40})

    ran, _ = _run_night(monkeypatch, conn, _grid_cfg())

    # The due city runs on its due channel only (the due path's own rule);
    # the fill city follows, on both.
    assert ran == [(due, "gsv"), (fill, "gsv"), (fill, "mapillary")]


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


def test_the_fill_never_runs_an_opt_in_channel(conn, monkeypatch):
    """An enrolled city whose KartaView clock is also old is refreshed on its
    default channels only -- enrolment stays explicit (#248, #374)."""
    cid = _city(conn, "Enrolled", {"gsv": 60, "mapillary": 60, "kartaview": 60})
    db.set_channel_membership(conn, cid, "kartaview", True, cycle_days=90)
    cfg = _grid_cfg(
        providers={
            "gsv": ProviderConfig(daily_request_budget=10_000_000),
            "mapillary": ProviderConfig(daily_request_budget=3_500),
            "kartaview": ProviderConfig(daily_request_budget=10_000),
        }
    )

    ran, _ = _run_night(monkeypatch, conn, cfg)

    assert ran == [(cid, "gsv"), (cid, "mapillary")]


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
    assert "ended by unexpected error in the city loop" in done
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
    assert "Fill: held — the due slate leaves mapillary 2 unfinished." in capsys.readouterr().out


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
    assert db.get_early_refresh_keys(conn) == {(cid, "gsv", "2026-10-01")}
    conn.close()
