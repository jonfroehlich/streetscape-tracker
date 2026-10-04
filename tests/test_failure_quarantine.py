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
* a FILL failure that reaches the cap is a transition too (the after-snapshot
  is taken behind the fill, not behind the due loop);
* a raising quarantine snapshot -- before or after the night -- costs neither
  the collection nor the tail, and alerts as a failed check rather than as a
  transition nobody observed;
* the amnestied exit-code families (and a child killed by the SIGTERM
  wind-down) can never reach the cap, against a plain failure as the positive
  control that does;
* `scheduler status` marks the quarantined pair;
* `reset-failures`: dry run by default, `--execute` writes, and bad input
  exits 64 having written nothing.
"""

import logging
import os
import shlex
import signal
import sqlite3
from datetime import UTC, date, datetime, timedelta

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


def _night(monkeypatch, conn, cfg, run_one, today=TODAY, caplog=None, tail=None):
    """Drive one real ``cmd_run_due`` with a fake collector; return (rc, alerts, done).

    ``tail``, when given, is a list the night's tail steps append their names to
    (``aggregate``, ``manifest``, ``publish``), so a test can tell a night whose
    tail ran from one that died before it.
    """
    tail = [] if tail is None else tail
    monkeypatch.setattr(
        sched,
        "_run_one_city",
        lambda cfg, city, today, provider="gsv", **_: run_one(city, provider),
    )
    monkeypatch.setattr(sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    monkeypatch.setattr(sched, "generate_aggregate_v2", lambda c, d: tail.append("aggregate"))
    monkeypatch.setattr(
        sched, "generate_streetwalk_manifest", lambda c, d: tail.append("manifest") or {}
    )
    monkeypatch.setattr(sched, "_publish", lambda cfg, summary, **kw: tail.append("publish") or 0)
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


def test_reset_consecutive_failures_commits_what_another_connection_reads(conn, data_dir):
    """The reset is visible to a SECOND connection to the same file.

    Every other test reads back through the connection that wrote, which sees
    its own uncommitted transaction -- so a reset that never commits passes
    them all, and then vanishes when `reset-failures` exits.
    """
    cid = _register(conn, "Bend")
    _set_failures(conn, cid, "mapillary", CAP)

    assert db.reset_consecutive_failures(conn, cid, "mapillary") == CAP

    other = sqlite3.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    try:
        (n,) = other.execute(
            "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = ?",
            (cid, "mapillary"),
        ).fetchone()
    finally:
        other.close()
    assert n == 0


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
    assert "; quarantined: 1 (mapillary 1, 1 new tonight)" in done


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


def _killed_by_the_stop():
    """A child that dies of the SIGTERM stopping the batch, as systemd's
    control-group kill delivers it: the stop is requested while it runs, and it
    exits -15, which is in no amnestied exit-code family on its own."""
    os.kill(os.getpid(), signal.SIGTERM)
    return CollectionOutcome(False, "exited -15", exit_code=-15)


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
        # The #206 wind-down: the child fails BECAUSE the stop's SIGTERM reached
        # its cgroup, so the stop is credited rather than the city. A callable,
        # because the signal has to arrive while the child is in flight.
        pytest.param(_killed_by_the_stop, id="sigterm-wind-down"),
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
            lambda city, p: True if p == "gsv" else (outcome() if callable(outcome) else outcome),
            caplog=caplog,
        )

    if callable(outcome):
        # Not vacuous: the child really did land in the stop's amnesty branch.
        assert "child was killed by the stop signal" in caplog.text
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


def test_a_fill_failure_that_reaches_the_cap_is_reported_as_a_transition(conn, monkeypatch, caplog):
    """The after-snapshot sits behind the FILL (issue #404), not the due loop.

    The fill admits only a city with no failure since its last success on a
    default channel, so it adds at most one -- which reaches the cap only at
    `max_consecutive_failures = 1`, the setting this test runs at. Nothing is
    due (both channels succeeded 60 days ago: past the 30-day floor, short of
    the 83-day due wall), so the ONLY failure of the night is the fill's.
    Taking the snapshot right after `_run_city_loop` would miss it.
    """
    cid = _register(conn, "Bend")
    stamp = (datetime.combine(TODAY, datetime.min.time(), UTC) - timedelta(days=60)).isoformat()
    for provider in ("gsv", "mapillary"):
        conn.execute(
            """INSERT INTO schedule_state
               (city_id, provider, day_of_cycle, last_attempt_at, last_success_at,
                consecutive_failures)
               VALUES (?, ?, 0, ?, ?, 0)""",
            (cid, provider, stamp, stamp),
        )
    conn.commit()
    ran = []

    def run_one(city, p):
        ran.append(p)
        return True if p == "gsv" else _failing("the fill's tile host said 500")

    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, alerts, done = _night(
            monkeypatch,
            conn,
            _cfg(fill_min_days=30, max_consecutive_failures=1),
            run_one,
            caplog=caplog,
        )

    assert ran == ["gsv", "mapillary"], "the fill must be what ran the city"
    assert "1 fill" in done
    assert _failures(conn, cid, "mapillary") == 1
    assert rc == 1
    assert len(alerts) == 1
    subject, body = alerts[0]
    assert "1 QUARANTINED" in subject
    assert f"{cid} [mapillary]: 1 consecutive failure(s)" in body
    assert "; quarantined: 1 (mapillary 1, 1 new tonight)" in done


@pytest.mark.parametrize("failing_call", ["before", "after"])
def test_a_raising_quarantine_check_costs_neither_the_night_nor_its_tail(
    conn, monkeypatch, caplog, failing_call
):
    """Each snapshot raises in turn; the night still collects, publishes and alerts.

    BEFORE sits ahead of the pre-loop backup (a raise there was the whole
    night); AFTER sits between the loop and `_finish_batch` (a raise there was
    the aggregate, manifests, backup, publish and the alert). Either failure is
    named on the Done line and alerts on its own -- `failure_threshold` is 99 and
    every collection succeeds, so nothing else here sends mail.

    A standing pair already at the cap is the trap for the BEFORE case: with no
    before-set, diffing against an empty one would report it as new tonight
    and re-alert a pair that was alerted on when it entered quarantine.
    """
    cid = _register(conn, "Bend")
    standing = _register(conn, "Corvallis")
    _set_failures(conn, standing, "mapillary", CAP)
    real = sched._quarantined_pairs
    calls = []

    def flaky(cfg, c):
        calls.append(1)
        if len(calls) == (1 if failing_call == "before" else 2):
            raise RuntimeError("database disk image is malformed")
        return real(cfg, c)

    monkeypatch.setattr(sched, "_quarantined_pairs", flaky)
    backups = []
    real_backup = sched.catalog_backup.write_backup
    monkeypatch.setattr(
        sched.catalog_backup,
        "write_backup",
        lambda *a, **k: backups.append(1) or real_backup(*a, **k),
    )
    ran, tail = [], []

    def run_one(city, p):
        ran.append((city.city_id, p))
        return True

    with caplog.at_level(logging.INFO, logger="streetscape_scheduler"):
        rc, alerts, done = _night(
            monkeypatch,
            conn,
            _cfg(publish_enabled=True),
            run_one,
            caplog=caplog,
            tail=tail,
        )

    assert len(calls) == 2, "both snapshots were attempted"
    assert (cid, "gsv") in ran and (cid, "mapillary") in ran, "the night still collected"
    assert tail == ["aggregate", "manifest", "publish"], "and its tail still ran"
    assert len(backups) == 2, "both the pre-loop and the tail backup ran"
    assert done is not None
    assert (
        f"; quarantine check FAILED ({failing_call} the night: RuntimeError: "
        "database disk image is malformed)"
    ) in done
    assert rc == 1
    assert len(alerts) == 1
    subject, body = alerts[0]
    assert "QUARANTINE CHECK FAILED" in subject
    assert "quarantine check FAILED" in body
    # Never a transition read off a missing snapshot.
    assert "QUARANTINED" not in subject.replace("QUARANTINE CHECK FAILED", "")
    assert "new tonight" not in done
    if failing_call == "before":
        # The after-set is still known, so the standing count is still reported.
        assert "; quarantined: 1 (mapillary 1)" in done
    else:
        # The after-set is UNKNOWN, so no standing count -- not the before-set
        # re-reported as if it were tonight's (the standing pair makes that
        # substitution visible: it would print "quarantined: 1").
        assert "; quarantined:" not in done


def test_the_reset_command_survives_an_apostrophe_in_the_city_id():
    """The pasted command splits back into exactly the argv it was built from.

    A real city_id can carry an apostrophe; unquoted, the shell reads it as an
    open quote and the operator's paste fails or runs something else.
    """
    city_id = "coeur-d'alene--idaho--united-states"
    cfg = _cfg(config_path="/abs/my scheduler.toml")

    cmd = sched._reset_failures_command(cfg, city_id, "kartaview")

    assert shlex.split(cmd) == [
        "python",
        "-m",
        "streetscape_metadata_tracker.scheduler",
        "--config",
        "/abs/my scheduler.toml",
        "reset-failures",
        city_id,
        "--channel",
        "kartaview",
        "--execute",
    ]


@pytest.mark.parametrize("newly", [0, 1], ids=["standing-only", "with-new"])
def test_the_quarantine_clauses_hold_no_semicolon_past_their_leading_separator(monkeypatch, newly):
    """The `Done:` line is split on ";" (scripts/night_length_analyze.py).

    So each clause's leading "; " must be its only ";" -- in the count's
    parenthetical, and in a failed check's exception text, which is not ours.
    """
    rows = [
        {"city_id": "a", "provider": "kartaview"},
        {"city_id": "b", "provider": "kartaview"},
        {"city_id": "c", "provider": "panoramax"},
    ]
    note = sched._quarantine_summary_note(rows, rows[:newly])
    assert note.startswith("; quarantined: 3 (")
    assert note.count(";") == 1, note

    def boom(cfg, c):
        raise RuntimeError("host unavailable; retry later")

    monkeypatch.setattr(sched, "_quarantined_pairs", boom)
    rows_out, error = sched._quarantine_snapshot(_cfg(), None, "after")
    assert rows_out is None
    clause = f"; {error}"
    assert clause.count(";") == 1, clause
    assert "host unavailable, retry later" in clause


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
