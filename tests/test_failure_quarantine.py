"""
The failure quarantine, made visible (issue #421).

`get_due_cities` drops a (city, channel) once `consecutive_failures` reaches
`[schedule].max_consecutive_failures`, and only a success resets it -- which a
pair that is never attempted again cannot have. So nights 1-5 of a failing
channel each alerted, and night 6 onward was silent. These tests pin, in order:

* `db.get_quarantined`: the gates it shares with dueness (enabled, member) and
  the one it reads (the cap), and `db.reset_consecutive_failures`;
* the live night: the TRANSITION alerts once (as a subject part of the night
  email, past `failure_threshold`), a later night does not re-alert, and the
  `Done:` line carries the standing count only while it is nonzero;
* the amnestied exit-code families can never reach the cap, against a plain
  failure as the positive control that does;
* `scheduler status` marks the quarantined pair;
* `reset-failures`: dry run by default, `--execute` writes, and bad input
  exits 64 having written nothing.
"""

import logging
from datetime import date

import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import scheduler as sched
from streetscape_metadata_tracker.alerting import AlertConfig
from streetscape_metadata_tracker.download_common import (
    ARGV_REJECTED_EXIT_CODE,
    HOST_BUSY_EXIT_CODES,
    HOST_EXIT_CODES,
    HOST_MAPILLARY_TILES,
    SWEEP_INCOMPLETE_EXIT_CODE,
)
from streetscape_metadata_tracker.scheduler import (
    CHANNEL_DEFAULT_MEMBERSHIP,
    USAGE_EXIT_CODE,
    CollectionOutcome,
    ProviderConfig,
    SchedulerConfig,
)

TODAY = date(2026, 7, 2)
CAP = 5  # SchedulerConfig's default max_consecutive_failures, and prod's


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


def _cfg(**overrides):
    """gsv + mapillary, alerts on, and a threshold no ordinary night reaches.

    The threshold is the point: at 99 a plain failed collection sends nothing,
    so any email these tests see came from a condition that alerts on its own.
    """
    providers = {
        "gsv": ProviderConfig(enabled=True, daily_request_budget=10_000_000),
        "mapillary": ProviderConfig(enabled=True, daily_request_budget=40_000),
    }
    overrides.setdefault("alerts", AlertConfig(enabled=True, failure_threshold=99))
    overrides.setdefault("publish_enabled", False)
    return SchedulerConfig(providers=providers, **overrides)


def _set_failures(conn, city_id, provider, n, error="boom"):
    """A pair with ``n`` consecutive failures and no success, so it is due."""
    conn.execute(
        """INSERT INTO schedule_state
           (city_id, provider, day_of_cycle, consecutive_failures, last_error)
           VALUES (?, ?, 0, ?, ?)
           ON CONFLICT(city_id, provider) DO UPDATE SET
             consecutive_failures = excluded.consecutive_failures,
             last_error = excluded.last_error""",
        (city_id, provider, n, error),
    )
    conn.commit()


def _failures(conn, city_id, provider):
    row = conn.execute(
        "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = ?",
        (city_id, provider),
    ).fetchone()
    return None if row is None else row["consecutive_failures"]


def _night(monkeypatch, conn, cfg, run_one, today=TODAY, caplog=None):
    """Drive one real ``cmd_run_due`` with a fake collector; return (rc, alerts, done)."""
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **_: run_one(city, provider),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: None)
    monkeypatch.setattr(sched, "generate_streetwalk_manifest", lambda c, d: {})
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary, **kw: 0)
    alerts = []
    monkeypatch.setattr(
        sched, "send_alert", lambda c, subject, body: alerts.append((subject, body))
    )
    if caplog is not None:
        caplog.clear()
    rc = sched.cmd_run_due(cfg, today=today)
    done = None
    if caplog is not None:
        done = next(
            (r.getMessage() for r in caplog.records if r.getMessage().startswith("Done: ")), None
        )
    return rc, alerts, done


def _failing(reason="HTTP 500 from the tile host"):
    return CollectionOutcome(False, reason, exit_code=1)


# ── db.get_quarantined / db.reset_consecutive_failures ──────────────────────


def _quarantined_ids(conn, channels=("gsv", "mapillary", "kartaview")):
    return {
        (r["city_id"], r["provider"])
        for r in db.get_quarantined(
            conn,
            channels=channels,
            default_membership=CHANNEL_DEFAULT_MEMBERSHIP,
            max_consecutive_failures=CAP,
        )
    }


def test_get_quarantined_is_exactly_the_pairs_dueness_drops_for_failing(conn):
    """The cap is `>=`, and the enabled/member gates match `get_due_cities`'.

    Each row here differs from a quarantined one in exactly one respect, so a
    `>` for `>=`, a dropped `enabled` gate or a dropped membership gate each
    lets one of them through.
    """
    at_cap = _register(conn, "Bend")
    under = _register(conn, "Corvallis")
    disabled = _register(conn, "Eugene")
    non_member = _register(conn, "Salem")
    over = _register(conn, "Medford")
    _set_failures(conn, at_cap, "gsv", CAP)
    _set_failures(conn, under, "gsv", CAP - 1)
    _set_failures(conn, disabled, "gsv", CAP)
    conn.execute("UPDATE cities SET enabled = 0 WHERE city_id = ?", (disabled,))
    # kartaview is opt-in: a NULL member there is a non-member.
    _set_failures(conn, non_member, "kartaview", CAP)
    # ...and an explicitly enrolled one IS counted.
    _set_failures(conn, over, "kartaview", CAP + 3)
    conn.execute(
        "UPDATE schedule_state SET member = 1 WHERE city_id = ? AND provider = 'kartaview'",
        (over,),
    )
    conn.commit()

    assert _quarantined_ids(conn) == {(at_cap, "gsv"), (over, "kartaview")}
    # And the channel list scopes it: a channel not asked about is not reported.
    assert _quarantined_ids(conn, channels=("gsv",)) == {(at_cap, "gsv")}


def test_reset_consecutive_failures_zeroes_only_the_counter(conn):
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "gsv", CAP, error="the cause")
    _set_failures(conn, cid, "mapillary", 2)

    assert db.reset_consecutive_failures(conn, cid, "gsv") == CAP
    row = conn.execute(
        "SELECT consecutive_failures, last_error, last_success_at FROM schedule_state "
        "WHERE city_id = ? AND provider = 'gsv'",
        (cid,),
    ).fetchone()
    assert row["consecutive_failures"] == 0
    # Kept: the only record of why it was quarantined.
    assert row["last_error"] == "the cause"
    # A reset is not a success.
    assert row["last_success_at"] is None
    # The sibling channel is untouched.
    assert _failures(conn, cid, "mapillary") == 2
    # Nothing to reset is reported as 0, not as a write.
    assert db.reset_consecutive_failures(conn, cid, "gsv") == 0
    assert db.reset_consecutive_failures(conn, cid, "kartaview") == 0


# ── The live night ───────────────────────────────────────────────────────────


def test_the_night_a_pair_reaches_the_cap_alerts_once_naming_it_and_its_fix(
    conn, monkeypatch, caplog
):
    """The fifth failure is the transition: one subject part, one paragraph.

    `failure_threshold` is 99, so without the quarantine part this night sends
    nothing at all -- which is what makes the alert's presence meaningful.
    """
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP - 1)
    cfg = _cfg(config_path="/abs/scheduler.makelab1.toml")

    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, alerts, done = _night(
            monkeypatch,
            conn,
            cfg,
            lambda city, p: True if p == "gsv" else _failing("tile host said 500"),
            caplog=caplog,
        )

    assert _failures(conn, cid, "mapillary") == CAP
    assert rc == 1
    assert len(alerts) == 1
    subject, body = alerts[0]
    assert "1 QUARANTINED" in subject
    assert f"{cid} [mapillary]: {CAP} consecutive failure(s)" in body
    assert "tile host said 500" in body
    assert (
        "python -m streetscape_metadata_tracker.scheduler --config "
        f"/abs/scheduler.makelab1.toml reset-failures {cid} --channel mapillary --execute"
    ) in body
    assert "; quarantined: 1 (mapillary 1; 1 new tonight)" in done


def test_a_later_night_does_not_re_alert_but_keeps_counting(conn, monkeypatch, caplog):
    """The pair is no longer attempted, so nothing fails -- and nothing alerts.

    `count >= max` as the alert condition would email this night and every
    night after it; the Done line is where the standing set lives instead.
    """
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP)
    ran = []

    def run_one(city, p):
        ran.append(p)
        return True

    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, alerts, done = _night(monkeypatch, conn, _cfg(), run_one, caplog=caplog)

    assert "mapillary" not in ran, "a quarantined pair is not attempted"
    assert ran == ["gsv"]
    assert rc == 0
    assert alerts == []
    assert "; quarantined: 1 (mapillary 1)" in done
    assert "new tonight" not in done


def test_the_done_line_says_nothing_about_quarantine_when_there_is_none(conn, monkeypatch, caplog):
    """Present only when nonzero: a clean night's line is unchanged."""
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP - 1)
    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _rc, _alerts, done = _night(monkeypatch, conn, _cfg(), lambda c, p: True, caplog=caplog)
    assert done is not None
    assert "quarantined" not in done


def test_a_pair_already_over_the_cap_that_fails_again_is_not_a_new_transition():
    """The transition is a set difference, never a count, so a pair that some
    path outside the due list pushed from 5 to 6 does not alert a second time."""
    before = [{"city_id": "a", "provider": "gsv"}, {"city_id": "b", "provider": "mapillary"}]
    after = [
        {"city_id": "a", "provider": "gsv"},
        {"city_id": "b", "provider": "gsv"},
    ]
    assert sched._newly_quarantined(before, after) == [{"city_id": "b", "provider": "gsv"}]
    assert sched._newly_quarantined(after, after) == []


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(
            CollectionOutcome(False, "blocked", exit_code=HOST_EXIT_CODES[HOST_MAPILLARY_TILES]),
            id="blocked",
        ),
        pytest.param(
            CollectionOutcome(False, "busy", exit_code=HOST_BUSY_EXIT_CODES[HOST_MAPILLARY_TILES]),
            id="busy",
        ),
        pytest.param(
            CollectionOutcome(False, "paused", exit_code=SWEEP_INCOMPLETE_EXIT_CODE),
            id="crawl-incomplete",
        ),
        pytest.param(
            CollectionOutcome(False, "argv", exit_code=ARGV_REJECTED_EXIT_CODE),
            id="argv-rejected",
        ),
    ],
)
def test_an_amnestied_outcome_never_quarantines_a_pair_one_failure_from_the_cap(
    conn, monkeypatch, caplog, outcome
):
    """One failure short of the cap, an amnestied exit leaves it short.

    The exit-code families that record no `consecutive_failure` (CLAUDE.md's
    table) are exactly the ones that cannot be the city's fault, so none of them
    may ever be what quarantines it -- or the quarantine alert would fire for a
    host block. The positive control below runs the same night with a plain
    failure and does quarantine, so this cannot pass by never counting at all.
    """
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP - 1)

    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        _rc, alerts, done = _night(
            monkeypatch,
            conn,
            _cfg(),
            lambda city, p: True if p == "gsv" else outcome,
            caplog=caplog,
        )

    assert _failures(conn, cid, "mapillary") == CAP - 1
    assert not any("QUARANTINED" in subject for subject, _body in alerts)
    assert "quarantined" not in done


def test_the_positive_control_a_plain_failure_one_from_the_cap_does_quarantine(conn, monkeypatch):
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP - 1)
    _rc, alerts, _done = _night(
        monkeypatch, conn, _cfg(), lambda city, p: True if p == "gsv" else _failing()
    )
    assert _failures(conn, cid, "mapillary") == CAP
    assert any("QUARANTINED" in subject for subject, _body in alerts)


# ── status ───────────────────────────────────────────────────────────────────


def test_status_marks_the_quarantined_pair_and_counts_them(conn, monkeypatch, capsys):
    quarantined = _register(conn, "Bend")
    failing = _register(conn, "Corvallis")
    _set_failures(conn, quarantined, "mapillary", CAP)
    _set_failures(conn, failing, "mapillary", 1)
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)

    assert sched.cmd_status(_cfg()) == 0
    out = capsys.readouterr().out
    lines = {
        line.split()[0]: line
        for line in out.splitlines()
        if line.split() and line.split()[0] in (quarantined, failing) and "boom" in line
    }
    assert "QUARANTINED" in lines[quarantined]
    assert "QUARANTINED" not in lines[failing]
    assert "1 pair(s) QUARANTINED at [schedule].max_consecutive_failures = 5" in out


# ── reset-failures ───────────────────────────────────────────────────────────


def _reset(monkeypatch, conn, city, channel, execute=False):
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    return sched.cmd_reset_failures(_cfg(), city, channel=channel, execute=execute)


def test_reset_failures_is_a_dry_run_without_execute(conn, monkeypatch, capsys):
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP, error="the cause")

    assert _reset(monkeypatch, conn, cid, "mapillary") == 0
    out = capsys.readouterr().out
    assert _failures(conn, cid, "mapillary") == CAP, "a preview writes nothing"
    assert "QUARANTINED" in out
    assert "the cause" in out
    assert "DRY RUN" in out


def test_reset_failures_execute_clears_the_quarantine_and_the_pair_is_due_again(conn, monkeypatch):
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP)
    due_kw = dict(
        today=TODAY,
        cycle_days=90,
        grace_days=7,
        max_consecutive_failures=CAP,
        default_membership=True,
        provider="mapillary",
    )
    assert cid not in {c.city_id for c in db.get_due_cities(conn, **due_kw)}

    assert _reset(monkeypatch, conn, cid, "mapillary", execute=True) == 0

    assert _failures(conn, cid, "mapillary") == 0
    assert cid in {c.city_id for c in db.get_due_cities(conn, **due_kw)}


@pytest.mark.parametrize(
    ("city", "channel", "failures", "says"),
    [
        pytest.param("bend", "no-such-channel", CAP, "unknown channel", id="unknown-channel"),
        pytest.param("no-such-city", "mapillary", CAP, "no such city", id="unknown-city"),
        pytest.param(None, "mapillary", CAP, "CITY is required", id="no-city"),
        pytest.param("bend", "mapillary", 0, "nothing to reset", id="nothing-to-reset"),
        pytest.param("bend", "gsv", CAP, "nothing to reset", id="no-row-on-that-channel"),
    ],
)
def test_reset_failures_bad_input_exits_64_and_writes_nothing(
    conn, monkeypatch, caplog, city, channel, failures, says
):
    """Each refusal names its own cause.

    The message is asserted, not just the status: an unknown channel would
    otherwise ALSO exit 64 as "nothing to reset" (no row matches it), telling
    an operator who typo'd the channel that the pair is healthy.
    """
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", failures)
    query = cid if city == "bend" else city

    with caplog.at_level(logging.ERROR, logger="streetscape_scheduler"):
        assert _reset(monkeypatch, conn, query, channel, execute=True) == USAGE_EXIT_CODE
    assert _failures(conn, cid, "mapillary") == failures
    assert says in caplog.text


def test_reset_failures_is_wired_into_the_cli(monkeypatch):
    """The parser and main() both reach cmd_reset_failures with --execute intact."""
    seen = {}
    monkeypatch.setattr(
        sched,
        "cmd_reset_failures",
        lambda cfg, city, *, channel, execute: (
            seen.update(city=city, channel=channel, execute=execute) or 0
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["scheduler", "reset-failures", "bend", "--channel", "kartaview", "--execute"],
    )
    assert sched.main() == 0
    assert seen == {"city": "bend", "channel": "kartaview", "execute": True}
