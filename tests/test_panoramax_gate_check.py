"""
The #405 stage gate as one command (`scripts/panoramax_gate_check.py`).

Two halves, pinned separately because each can be wrong while the other is
right: the RULE (pure functions over counts, every boundary asserted on both
sides) and the READING (real log lines produced by the collector's own
handlers and exceptions, so a reworded message fails here instead of silently
counting nothing -- the failure a grep-based gate is most prone to).
"""

import logging
import sqlite3
from datetime import date, timedelta

import pytest

import scripts.panoramax_gate_check as gate
from streetscape_metadata_tracker import download_panoramax as dp
from streetscape_metadata_tracker.download_common import HostBlockedError
from tests.test_panoramax import _FakeTileResponse, _FakeTileSession, _fetch

NIGHT = date(2026, 10, 2)
_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


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
    assert gate.PANORAMAX_CHANNELS == ("panoramax", "panoramax_streets")


@pytest.mark.parametrize(
    ("kw", "verdict"),
    [
        (dict(blocks=1), "REVERT"),
        (dict(blocks=1, retried_5xx=0, requests=0), "REVERT"),
        (dict(retried_5xx=800), "HOLD"),  # exactly 10% of 8,000 is not over it
        (dict(retried_5xx=801), "REVERT"),
        (dict(gave_up_5xx=1), "HOLD"),
        (dict(retried_5xx=80), "CLEAN"),  # exactly 1% of 8,000
        (dict(retried_5xx=81), "HOLD"),
        (dict(retried_other=500), "CLEAN"),  # timeouts are reported, not gated
        (dict(requests=999, retried_5xx=10), "QUIET"),
        (dict(requests=999, retried_5xx=11), "HOLD"),  # a quiet night can still hold
        (dict(requests=1_000, retried_5xx=10), "CLEAN"),
        (dict(requests=500, retried_5xx=51), "REVERT"),  # over max(10, 10% of 500)
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


def test_seven_clean_qualifying_nights_advance_and_six_do_not():
    assert gate.judge_window(_nights(*[CLEAN] * 7)).verdict == "ADVANCE"
    six = gate.judge_window(_nights(*[CLEAN] * 6))
    assert (six.verdict, six.streak) == ("CONTINUE", 6)


def test_quiet_nights_neither_advance_nor_reset_the_streak():
    w = gate.judge_window(_nights(*[CLEAN] * 3, QUIET, QUIET, *[CLEAN] * 4))
    assert (w.verdict, w.streak) == ("ADVANCE", 7)
    assert gate.judge_window(_nights(*[QUIET] * 9)).streak == 0


def test_a_hold_resets_the_streak():
    w = gate.judge_window(_nights(*[CLEAN] * 6, HOLD, *[CLEAN] * 6))
    assert (w.verdict, w.streak) == ("CONTINUE", 6)
    w = gate.judge_window(_nights(*[CLEAN] * 6, HOLD))
    assert (w.verdict, w.streak) == ("HOLD", 0)


def test_two_holds_within_seven_nights_revert_and_further_apart_do_not():
    assert gate.judge_window(_nights(HOLD, *[CLEAN] * 5, HOLD)).verdict == "REVERT"
    apart = gate.judge_window(_nights(HOLD, *[CLEAN] * 6, HOLD))
    assert apart.verdict == "HOLD"


def test_a_revert_anywhere_in_the_window_wins_over_a_later_clean_run():
    w = gate.judge_window(_nights(dict(blocks=1), *[CLEAN] * 8))
    assert w.verdict == "REVERT"
    assert str(NIGHT) in w.reason


# ── Reading the logs the collector actually writes ─────────────────────────


def _formatted(record_fn, details, caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=dp.logger.name):
        record_fn(details)
    (record,) = caplog.records
    return logging.Formatter(_FORMAT).format(record)


def _blocked_message(status, headers=None):
    with pytest.raises(HostBlockedError) as excinfo:
        _fetch(_FakeTileSession(_FakeTileResponse(status, headers)))
    return str(excinfo.value)


class _Status(Exception):
    def __init__(self, status):
        self.status = status


def test_the_reader_counts_the_lines_the_real_handlers_write(caplog):
    retry_503 = _formatted(
        dp._log_tile_retry, {"exception": _Status(503), "tries": 1, "wait": 0.5}, caplog
    )
    retry_timeout = _formatted(
        dp._log_tile_retry, {"exception": TimeoutError(), "tries": 1, "wait": 0.5}, caplog
    )
    gave_up = _formatted(dp._log_tile_giveup, {"exception": _Status(502), "tries": 5}, caplog)
    counts = {"retried_5xx": 0, "gave_up_5xx": 0, "retried_other": 0, "blocks": 0}
    gate.count_log_lines([retry_503, retry_503, retry_timeout, gave_up, "unrelated"], counts)
    assert counts == {"retried_5xx": 2, "gave_up_5xx": 1, "retried_other": 1, "blocks": 0}


@pytest.mark.parametrize(
    ("status", "headers"),
    [(403, None), (429, None), (302, {"Location": "https://example.test/login"})],
)
def test_every_real_refusal_message_is_counted_as_a_block(status, headers):
    message = _blocked_message(status, headers)
    counts = {"retried_5xx": 0, "gave_up_5xx": 0, "retried_other": 0, "blocks": 0}
    gate.count_log_lines([f"2026-10-02 09:00:00 - x - ERROR - {message}"], counts)
    assert counts["blocks"] == 1, message


def test_an_error_page_refusal_is_counted_as_a_block():
    session = _FakeTileSession(_FakeTileResponse(200, {"Content-Type": "text/html"}))
    with pytest.raises(HostBlockedError) as excinfo:
        _fetch(session)
    counts = {"retried_5xx": 0, "gave_up_5xx": 0, "retried_other": 0, "blocks": 0}
    gate.count_log_lines([str(excinfo.value)], counts)
    assert counts["blocks"] == 1


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


def _retry_line(stamp, status=503):
    return (
        f"{stamp} 09:10:00 - dp - WARNING - {dp.TILE_RETRY_LOG_PHRASE}: HTTP {status} on try 1 of 5"
    )


def test_gather_night_reads_both_channels_and_only_that_nights_scheduler_lines(tmp_path, ledger):
    _path, conn = ledger
    logs = tmp_path / "logs"
    logs.mkdir()
    stamp = NIGHT.isoformat()
    yesterday = (NIGHT - timedelta(days=1)).isoformat()
    (logs / f"collect_des-moines_panoramax_{stamp}.log").write_text(_retry_line(stamp) + "\n")
    (logs / f"collect_ames_panoramax_streets_{stamp}.log").write_text(
        _retry_line(stamp, 502) + "\n"
    )
    # Another night's child log, and another provider's, are never read.
    (logs / f"collect_ames_panoramax_{yesterday}.log").write_text(_retry_line(yesterday) + "\n")
    (logs / f"collect_ames_mapillary_{stamp}.log").write_text(_retry_line(stamp) + "\n")
    # The live scheduler log spans midnight: only the night's own lines count.
    (logs / "streetscape_scheduler.log").write_text(
        f"{yesterday} 23:59:00 - s - WARNING - ... exited 84 (the Panoramax meta-catalog)\n"
        f"{stamp} 09:30:00 - s - WARNING - ... exited 84 (the Panoramax meta-catalog)\n"
        + _retry_line(stamp)
        + "\n"
    )
    c = gate.gather_night(logs, conn, NIGHT)
    assert (c.requests, c.retried_5xx, c.blocks) == (8_000, 3, 1)


def test_main_prints_the_report_and_exits_one_on_revert(tmp_path, ledger, capsys):
    db_path, _conn = ledger
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "streetscape_scheduler.log").write_text(
        f"{NIGHT.isoformat()} 09:30:00 - s - WARNING - city exited 84 (the Panoramax ...)\n"
    )
    config = tmp_path / "s.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{tmp_path}"\ndb_path = "{db_path}"\nlog_dir = "{logs}"\n'
    )
    rc = gate.main(["--config", str(config), "--end", NIGHT.isoformat(), "--nights", "2"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Stage verdict: REVERT" in out
    assert f"{NIGHT.isoformat()}     8,000" in out
