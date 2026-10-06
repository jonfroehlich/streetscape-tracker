"""
The #405 stage gate as one command (`scripts/panoramax_gate_check.py`).

Two halves, pinned separately because each can be wrong while the other is
right: the RULE (pure functions over counts, every boundary asserted on both
sides) and the READING. The reading is tested on lines produced by the REAL
writers -- the collector's retry handlers and `HostBlockedError` messages, and
the scheduler's own `_run_collection_subprocess`, tail copy included -- so a
reworded message or a re-shaped log fails here instead of silently counting
the wrong thing, which is the failure a grep-shaped gate is most prone to.
"""

import logging
import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import scripts.panoramax_gate_check as gate
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import download_panoramax as dp
from streetscape_metadata_tracker import scheduler as sched
from streetscape_metadata_tracker.download_common import HostBlockedError
from tests.test_panoramax import _FakeTileResponse, _FakeTileSession, _fetch

NIGHT = date(2026, 10, 2)
PACIFIC = ZoneInfo("America/Los_Angeles")
#: What `setup_logging` (the scheduler) writes: asctime WITH milliseconds.
_SCHEDULER_FORMAT = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
#: What cli.py / collect.py (the children) write: no milliseconds.
_CHILD_FORMAT = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)


def _counts(night=NIGHT, requests=8_000, **kw):
    return gate.NightCounts(night=night, requests=requests, **kw)


# ── The rule, night by night ───────────────────────────────────────────────


def test_the_hold_threshold_is_ten_or_one_percent_whichever_is_larger():
    assert gate.hold_threshold(0) == 10
    assert gate.hold_threshold(999) == 10
    assert gate.hold_threshold(1_000) == 10
    assert gate.hold_threshold(1_100) == 11
    assert gate.hold_threshold(16_000) == 160


def test_the_constants_are_the_documented_rule():
    """docs/provider-access.md quotes these numbers; moving one is a decision."""
    assert gate.CLEAN_NIGHTS_TO_ADVANCE == 7
    assert gate.MIN_QUALIFYING_REQUESTS == 1_000
    assert (gate.HOLD_MIN_RETRIES, gate.HOLD_RETRY_RATIO) == (10, 0.01)
    assert gate.REVERT_RETRY_RATIO == 0.10
    assert gate.HOLD_REVERT_WINDOW == 7
    assert gate.UNKNOWN_EXIT_CODE == 3
    assert gate.PANORAMAX_CHANNELS == ("panoramax", "panoramax_streets")


@pytest.mark.parametrize(
    ("kw", "verdict"),
    [
        (dict(blocks=1), "REVERT"),
        (dict(blocks=1, requests=0), "REVERT"),
        (dict(blocks=1, provisional=True), "REVERT"),  # a refusal is never provisional
        (dict(retried_5xx=800), "HOLD"),  # exactly 10% of 8,000 is not over it
        (dict(retried_5xx=801), "REVERT"),
        (dict(retried_5xx=801, provisional=True), "REVERT"),
        (dict(gave_up_5xx=1), "HOLD"),
        (dict(gave_up_other=3), "CLEAN"),  # a timeout give-up is reported, not gated
        (dict(retried_5xx=80), "CLEAN"),  # exactly 1% of 8,000
        (dict(retried_5xx=81), "HOLD"),
        (dict(retried_5xx=81, provisional=True), "PROVISIONAL"),
        (dict(retried_other=500), "CLEAN"),  # timeouts and 4xx are reported, not gated
        (dict(requests=999, retried_5xx=10), "QUIET"),
        (dict(requests=999, retried_5xx=11), "HOLD"),  # a quiet night can still hold
        (dict(requests=1_000, retried_5xx=10), "CLEAN"),
        (dict(requests=500, retried_5xx=51), "REVERT"),  # over max(10, 10% of 500)
        # No ledger row: no ratio to take, so the 10-retry floor alone must not
        # revert -- but those retries still HOLD.
        (dict(requests=0, retried_5xx=11), "HOLD"),
        # A qualifying ledger night with no child log cannot be judged ...
        (dict(child_logs=0), "UNKNOWN"),
        (dict(child_logs=0, requests=1_000), "UNKNOWN"),
        # ... but a sub-qualifying one (the weekly screen alone is 113) is QUIET.
        (dict(child_logs=0, requests=113), "QUIET"),
    ],
)
def test_each_boundary_of_the_night_rule(kw, verdict):
    assert gate.judge_night(_counts(**kw)).verdict == verdict


# ── The rule over a window ─────────────────────────────────────────────────


def _nights(*verdict_kws, start=NIGHT):
    return [_counts(night=start + timedelta(days=i), **kw) for i, kw in enumerate(verdict_kws)]


CLEAN = {}
QUIET = dict(requests=10)
HOLD = dict(gave_up_5xx=1)
UNKNOWN = dict(child_logs=0)


def test_seven_clean_qualifying_nights_advance_and_six_do_not():
    assert gate.judge_window(_nights(*[CLEAN] * 7)).verdict == "ADVANCE"
    six = gate.judge_window(_nights(*[CLEAN] * 6))
    assert (six.verdict, six.streak) == ("CONTINUE", 6)


def test_quiet_and_provisional_nights_neither_advance_nor_reset_the_streak():
    provisional = dict(provisional=True, retried_5xx=500)
    w = gate.judge_window(_nights(*[CLEAN] * 3, QUIET, provisional, *[CLEAN] * 4))
    assert (w.verdict, w.streak) == ("ADVANCE", 7)
    assert gate.judge_window(_nights(*[QUIET] * 9)).streak == 0


def test_a_hold_resets_the_streak():
    w = gate.judge_window(_nights(*[CLEAN] * 6, HOLD, *[CLEAN] * 6))
    assert (w.verdict, w.streak) == ("CONTINUE", 6)
    w = gate.judge_window(_nights(*[CLEAN] * 6, HOLD))
    assert (w.verdict, w.streak) == ("HOLD", 0)


def test_an_unknown_night_resets_the_streak_and_is_counted():
    w = gate.judge_window(_nights(*[CLEAN] * 6, UNKNOWN, *[CLEAN] * 6))
    assert (w.verdict, w.streak, w.unknown) == ("CONTINUE", 6, 1)
    w = gate.judge_window(_nights(*[CLEAN] * 6, UNKNOWN))
    assert (w.verdict, w.unknown) == ("UNKNOWN", 1)


def test_two_holds_within_seven_nights_revert_and_further_apart_do_not():
    assert gate.judge_window(_nights(HOLD, *[CLEAN] * 5, HOLD)).verdict == "REVERT"
    apart = gate.judge_window(_nights(HOLD, *[CLEAN] * 6, HOLD))
    assert apart.verdict == "HOLD"


def test_a_revert_anywhere_in_the_window_wins_over_a_later_clean_run():
    w = gate.judge_window(_nights(dict(blocks=1), *[CLEAN] * 8))
    assert w.verdict == "REVERT"
    assert str(NIGHT) in w.reason


# ── Reading the lines the collector actually writes ────────────────────────


def _record(fn, details, caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=dp.logger.name):
        fn(details)
    (record,) = caplog.records
    return record


class _Status(Exception):
    def __init__(self, status):
        self.status = status


def _counted(lines):
    counts = gate._empty_counts()
    gate.count_log_lines(lines, counts)
    return counts


def test_the_reader_classifies_the_lines_the_real_handlers_write(caplog):
    def line(fn, exc, tries=1):
        details = {"exception": exc, "tries": tries, "wait": 0.5}
        return _CHILD_FORMAT.format(_record(fn, details, caplog))

    counts = _counted(
        [
            line(dp._log_tile_retry, _Status(503)),
            line(dp._log_tile_retry, _Status(503)),
            line(dp._log_tile_retry, _Status(400)),  # a retried 4xx is not a 5xx
            line(dp._log_tile_retry, TimeoutError()),
            line(dp._log_tile_giveup, _Status(502), 5),
            line(dp._log_tile_giveup, TimeoutError(), 5),  # must not HOLD
            "unrelated",
        ]
    )
    assert counts == {
        "retried_5xx": 2,
        "gave_up_5xx": 1,
        "retried_other": 2,
        "gave_up_other": 1,
        "blocks": 0,
    }


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (403, None),
        (429, None),
        (302, {"Location": "https://example.test/login"}),
        (200, {"Content-Type": "text/html"}),
    ],
)
def test_every_real_refusal_message_is_counted_as_a_block(status, headers):
    with pytest.raises(HostBlockedError) as excinfo:
        _fetch(_FakeTileSession(_FakeTileResponse(status, headers)))
    message = str(excinfo.value)
    assert _counted([f"2026-10-02 09:00:00 - x - ERROR - {message}"])["blocks"] == 1, message


# ── Reading what the SCHEDULER actually writes ─────────────────────────────


_CITY = db.CityRow(
    city_id="des-moines--ia",
    display_name="Des Moines, IA",
    city_name="Des Moines",
    state_name="Iowa",
    state_code="IA",
    country_name="United States",
    country_code="US",
    center_lat=41.59,
    center_lon=-93.6,
    grid_width_m=1000,
    grid_height_m=1000,
    step_m=20,
    created_at="2026-01-01T00:00:00+00:00",
    enabled=True,
    notes=None,
)


def _scheduler_night(tmp_path, monkeypatch, caplog, exit_code, retries):
    """Run the REAL `_run_collection_subprocess` for a fake Panoramax child that
    logs `retries` retried 503s and exits `exit_code`, with the scheduler log
    captured to a file in `setup_logging`'s format. Returns (log_dir, night)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    retry_line = _CHILD_FORMAT.format(
        _record(dp._log_tile_retry, {"exception": _Status(503), "tries": 1, "wait": 0.5}, caplog)
    )

    def child(*a, stdout=None, **k):
        for _ in range(retries):
            stdout.write(retry_line + "\n")
        return subprocess.CompletedProcess(a, exit_code)

    monkeypatch.setattr(subprocess, "run", child)
    handler = logging.FileHandler(log_dir / gate.SCHEDULER_LOG_NAME, encoding="utf-8")
    handler.setFormatter(_SCHEDULER_FORMAT)
    sched.logger.addHandler(handler)
    night = datetime.now(UTC).date()
    try:
        cfg = sched.SchedulerConfig(log_dir=str(log_dir))
        sched._run_collection_subprocess(cfg, ["x"], 60, _CITY, "panoramax", night)
    finally:
        sched.logger.removeHandler(handler)
        handler.close()
    return log_dir, night


@pytest.fixture
def empty_ledger(tmp_path):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE api_usage (usage_date TEXT, provider TEXT, requests INTEGER)")
    yield conn
    conn.close()


def test_a_failed_childs_tail_copy_is_not_counted_twice(
    tmp_path, monkeypatch, caplog, empty_ledger
):
    """`_run_collection_subprocess` copies a failed child's last 25 lines into
    the scheduler log, and those lines carry a date stamp of their own -- a
    reader that kept every dated line counted each retry twice (8 -> 16)."""
    log_dir, night = _scheduler_night(tmp_path, monkeypatch, caplog, exit_code=83, retries=8)
    scheduler_text = (log_dir / gate.SCHEDULER_LOG_NAME).read_text()
    assert "--- last 25 lines of" in scheduler_text, "the real tail copy did not happen"
    assert scheduler_text.count(dp.TILE_RETRY_LOG_PHRASE) == 8, "precondition: copied"
    c = gate.gather_night(log_dir, empty_ledger, night)
    assert (c.retried_5xx, c.child_logs, c.blocks) == (8, 1, 0)


def test_the_schedulers_real_exit_84_line_is_a_block(tmp_path, monkeypatch, caplog, empty_ledger):
    log_dir, night = _scheduler_night(tmp_path, monkeypatch, caplog, exit_code=84, retries=0)
    assert gate.gather_night(log_dir, empty_ledger, night).blocks == 1


# ── Time zones: the scheduler log is host-LOCAL, the ledger is UTC ─────────


def test_a_local_timestamp_is_judged_by_its_utc_date():
    # The weekly screen fires Monday 23:00 Pacific, which is Tuesday in UTC --
    # the date `_record_screen_spend` charges it to.
    assert gate.local_to_utc_date("2026-10-05 23:00:00", PACIFIC) == date(2026, 10, 6)
    assert gate.local_to_utc_date("2026-10-05 16:59:59", PACIFIC) == date(2026, 10, 5)
    assert gate.local_to_utc_date("2026-10-05 16:59:59", ZoneInfo("UTC")) == date(2026, 10, 5)


def _sched_line(local_stamp, message):
    return f"{local_stamp},123 - streetscape_metadata_tracker.x - WARNING - {message}\n"


def test_a_utc_night_reads_both_local_rotations_it_spans(tmp_path, empty_ledger):
    """UTC 2026-10-06 is Pacific 10-05 17:00 .. 10-06 17:00: half of it lives
    in the rotation named for 10-05, half in the one named for 10-06."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    retry = f"{dp.TILE_RETRY_LOG_PHRASE}: HTTP 503 on try 1 of 5, waiting 0.5s"
    (log_dir / f"{gate.SCHEDULER_LOG_NAME}.2026-10-05").write_text(
        _sched_line("2026-10-05 16:59:59", retry)  # UTC 10-05: another night
        + _sched_line("2026-10-05 18:00:00", retry)  # UTC 10-06: the screen
    )
    (log_dir / f"{gate.SCHEDULER_LOG_NAME}.2026-10-06").write_text(
        _sched_line("2026-10-06 02:30:00", retry)  # UTC 10-06
        + _sched_line("2026-10-06 17:00:00", retry)  # UTC 10-07: another night
    )
    c = gate.gather_night(log_dir, empty_ledger, date(2026, 10, 6), log_tz=PACIFIC)
    assert c.retried_5xx == 2
    utc = gate.gather_night(log_dir, empty_ledger, date(2026, 10, 6), log_tz=ZoneInfo("UTC"))
    assert utc.retried_5xx == 2  # 10-06 02:30 and 17:00 read as UTC


def test_the_rotated_file_alone_is_read(tmp_path, empty_ledger):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / f"{gate.SCHEDULER_LOG_NAME}.{NIGHT.isoformat()}").write_text(
        _sched_line(f"{NIGHT.isoformat()} 09:30:00", "city [panoramax]: exited 84 (x)")
    )
    assert gate.gather_night(log_dir, empty_ledger, NIGHT, log_tz=ZoneInfo("UTC")).blocks == 1


# ── One night, end to end over a log directory and a ledger ────────────────


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE api_usage (usage_date TEXT, provider TEXT, requests INTEGER, "
        "PRIMARY KEY (usage_date, provider))"
    )
    conn.executemany(
        "INSERT INTO api_usage VALUES (?, ?, ?)",
        [
            (NIGHT.isoformat(), "panoramax", 6_000),
            (NIGHT.isoformat(), "panoramax_streets", 2_000),
            (NIGHT.isoformat(), "mapillary", 99_999),  # another host: never read
            ((NIGHT - timedelta(days=1)).isoformat(), "panoramax", 5),
        ],
    )
    conn.commit()
    yield path, conn
    conn.close()


def _child_retry(stamp, status=503):
    return f"{stamp} 09:10:00 - dp - WARNING - {dp.TILE_RETRY_LOG_PHRASE}: HTTP {status} on try 1 of 5\n"


def test_gather_night_reads_both_channels_and_only_that_night(tmp_path, ledger):
    _path, conn = ledger
    logs = tmp_path / "logs"
    logs.mkdir()
    stamp = NIGHT.isoformat()
    yesterday = (NIGHT - timedelta(days=1)).isoformat()
    (logs / f"collect_des-moines_panoramax_{stamp}.log").write_text(_child_retry(stamp))
    (logs / f"collect_ames_panoramax_streets_{stamp}.log").write_text(_child_retry(stamp, 502))
    # Another night's child log, and another provider's, are never read.
    (logs / f"collect_ames_panoramax_{yesterday}.log").write_text(_child_retry(yesterday))
    (logs / f"collect_ames_mapillary_{stamp}.log").write_text(_child_retry(stamp))
    c = gate.gather_night(logs, conn, NIGHT, log_tz=ZoneInfo("UTC"))
    assert (c.requests, c.retried_5xx, c.child_logs) == (8_000, 2, 2)


def test_a_ledger_night_with_no_child_log_is_unknown(tmp_path, ledger):
    _path, conn = ledger
    logs = tmp_path / "logs"
    logs.mkdir()
    c = gate.gather_night(logs, conn, NIGHT)
    assert (c.requests, c.child_logs) == (8_000, 0)
    assert gate.judge_night(c).verdict == "UNKNOWN"


def test_today_is_provisional(tmp_path, ledger):
    _path, conn = ledger
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"collect_x_panoramax_{NIGHT.isoformat()}.log").write_text("")
    assert gate.gather_night(logs, conn, NIGHT, today=NIGHT).provisional
    assert not gate.gather_night(logs, conn, NIGHT, today=NIGHT + timedelta(days=1)).provisional


def test_the_ledger_is_opened_read_only(ledger):
    path, _conn = ledger
    ro = gate.open_ledger(str(path))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("INSERT INTO api_usage VALUES ('2026-01-01', 'panoramax', 1)")
    finally:
        ro.close()


# ── The command ────────────────────────────────────────────────────────────


def _config(tmp_path, db_path, logs):
    config = tmp_path / "s.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{tmp_path}"\ndb_path = "{db_path}"\nlog_dir = "{logs}"\n'
    )
    return str(config)


@pytest.fixture
def frozen_today(monkeypatch):
    from streetscape_metadata_tracker import clock

    today = NIGHT + timedelta(days=1)
    monkeypatch.setattr(clock, "snapshot_date_today", lambda: today)
    return today


def test_main_exits_one_on_a_revert_since_the_stage_start(tmp_path, ledger, capsys, frozen_today):
    db_path, conn = ledger
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"collect_x_panoramax_{NIGHT.isoformat()}.log").write_text(
        f"{NIGHT.isoformat()} 09:30:00 - x - ERROR - Panoramax refused this host (HTTP 403)\n"
    )
    config = _config(tmp_path, db_path, logs)
    rc = gate.main(["--config", config, "--since", (NIGHT - timedelta(days=1)).isoformat()])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Stage verdict: REVERT" in out
    # The default end is YESTERDAY (UTC): today's batch may still be running.
    assert NIGHT.isoformat() in out and frozen_today.isoformat() not in out
    # A --since AFTER the revert does not keep failing on it.
    assert gate.main(["--config", config, "--since", frozen_today.isoformat(), "--end",
                      frozen_today.isoformat()]) == 0  # fmt: skip


def test_main_exits_three_when_a_night_is_unknown(tmp_path, ledger, capsys, frozen_today):
    db_path, _conn = ledger
    logs = tmp_path / "logs"
    logs.mkdir()
    rc = gate.main(["--config", _config(tmp_path, db_path, logs), "--since", NIGHT.isoformat()])
    out = capsys.readouterr().out
    assert rc == gate.UNKNOWN_EXIT_CODE
    assert "WARNING: 1 night(s) UNKNOWN" in out


def test_main_refuses_a_future_end_and_a_since_after_end(tmp_path, ledger, frozen_today):
    db_path, _conn = ledger
    config = _config(tmp_path, db_path, tmp_path)
    future = (frozen_today + timedelta(days=1)).isoformat()
    with pytest.raises(SystemExit) as excinfo:
        gate.main(["--config", config, "--since", NIGHT.isoformat(), "--end", future])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        gate.main(["--config", config, "--since", frozen_today.isoformat(), "--end", "2026-01-01"])
    with pytest.raises(SystemExit):
        gate.main(["--config", config])  # --since is required
