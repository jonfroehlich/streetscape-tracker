#!/usr/bin/env python3
"""
Check nights against the Panoramax staged-raise gate (issue #405). Read-only.

Usage::

    python scripts/panoramax_gate_check.py --config config/scheduler.makelab1.toml
    python scripts/panoramax_gate_check.py --config config/scheduler.makelab1.toml \\
        --end 2026-10-09 --nights 10

Prints one line per night and the verdict for the current stage. It reads two
things and writes nothing: the night's logs under ``[paths].log_dir`` and the
``api_usage`` ledger in the catalog. It makes no network request of any kind.

THE RULE (docs/provider-access.md, "The staged raise", carries the reasoning
and the sources):

* A night QUALIFIES -- counts toward the seven -- only if it spent at least
  ``MIN_QUALIFYING_REQUESTS`` (1,000) Panoramax requests across both channels.
  A quiet night is no evidence either way and neither advances nor resets.
* REVERT (to the previous stage, at once) on any per-IP refusal: an
  ``exited 84`` child, or a Panoramax 403/429/redirect/error-page message.
  Also REVERT when retried 5xx exceed ``REVERT_RETRY_RATIO`` (10%) of the
  night's requests -- the per-client retry budget at which Google's SRE book
  stops retrying because retries have become load amplification -- or when a
  second HOLD night lands within ``HOLD_REVERT_WINDOW`` (7) nights of a first.
* HOLD (the clean streak resets to 0; the stage stays) when any tile GAVE UP
  on a 5xx, or retried 5xx exceed ``max(HOLD_MIN_RETRIES, HOLD_RETRY_RATIO x
  requests)`` = ``max(10, 1%)``.
* Otherwise the night is CLEAN, and seven consecutive clean qualifying nights
  ADVANCE the stage.

Exit status: 0 normally, 1 when the window ends in REVERT, and argparse's 2 on a
bad argument.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker.download_panoramax import TILE_RETRY_LOG_PHRASE  # noqa: E402

#: Both channels draw on the one per-IP host, so the gate reads their sum.
PANORAMAX_CHANNELS = ("panoramax", "panoramax_streets")
#: The number of consecutive clean qualifying nights that advance a stage.
CLEAN_NIGHTS_TO_ADVANCE = 7
#: Below this many Panoramax requests a night neither advances nor resets.
MIN_QUALIFYING_REQUESTS = 1_000
#: HOLD above max(HOLD_MIN_RETRIES, HOLD_RETRY_RATIO x requests) retried 5xx.
HOLD_MIN_RETRIES = 10
HOLD_RETRY_RATIO = 0.01
#: REVERT above this share of requests retried on a 5xx.
REVERT_RETRY_RATIO = 0.10
#: Two HOLD nights this close together REVERT.
HOLD_REVERT_WINDOW = 7
#: The scheduler's line for a child that exited with Panoramax's blocked code.
SCHEDULER_BLOCK_MARKER = "exited 84 ("
#: Leading text of every Panoramax HostBlockedError, as the collector words it.
#: tests/test_panoramax_gate_check.py derives each from the REAL exception, so
#: a reworded error fails a test rather than silently counting no blocks.
BLOCK_MESSAGE_MARKERS = (
    "Panoramax refused this host",
    "Panoramax redirected instead of serving a tile",
    "Panoramax served an error page",
)

_RETRY_5XX = re.compile(re.escape(TILE_RETRY_LOG_PHRASE) + r": HTTP 5\d\d on try")
_GIVEUP_5XX = re.compile(re.escape(TILE_RETRY_LOG_PHRASE) + r" gave up: HTTP 5\d\d")
_RETRY_ANY = re.compile(re.escape(TILE_RETRY_LOG_PHRASE) + r": ")


@dataclass(frozen=True)
class NightCounts:
    """What one night's logs and ledger say. Counts, not judgements."""

    night: date
    requests: int
    retried_5xx: int = 0
    gave_up_5xx: int = 0
    retried_other: int = 0
    blocks: int = 0


@dataclass(frozen=True)
class NightVerdict:
    counts: NightCounts
    #: One of "REVERT", "HOLD", "CLEAN", "QUIET".
    verdict: str
    reason: str


def hold_threshold(requests: int) -> int:
    """Retried 5xx a night may carry and still be clean: max(10, 1%).

    >>> hold_threshold(500), hold_threshold(1_000), hold_threshold(16_000)
    (10, 10, 160)
    """
    return max(HOLD_MIN_RETRIES, math.floor(HOLD_RETRY_RATIO * requests))


def judge_night(c: NightCounts) -> NightVerdict:
    """The per-night half of the rule, before the window is considered."""
    if c.blocks:
        return NightVerdict(c, "REVERT", f"{c.blocks} per-IP refusal(s) (exit 84 / 403 / 429)")
    if c.requests and c.retried_5xx > max(HOLD_MIN_RETRIES, REVERT_RETRY_RATIO * c.requests):
        return NightVerdict(
            c,
            "REVERT",
            f"{c.retried_5xx} retried 5xx is over {REVERT_RETRY_RATIO:.0%} of "
            f"{c.requests:,} requests",
        )
    if c.gave_up_5xx:
        return NightVerdict(c, "HOLD", f"{c.gave_up_5xx} tile(s) gave up on a 5xx")
    limit = hold_threshold(c.requests)
    if c.retried_5xx > limit:
        return NightVerdict(c, "HOLD", f"{c.retried_5xx} retried 5xx > {limit}")
    if c.requests < MIN_QUALIFYING_REQUESTS:
        return NightVerdict(
            c, "QUIET", f"{c.requests:,} requests < {MIN_QUALIFYING_REQUESTS:,}: no evidence"
        )
    return NightVerdict(c, "CLEAN", f"{c.retried_5xx} retried 5xx <= {limit}")


@dataclass(frozen=True)
class WindowVerdict:
    nights: list[NightVerdict]
    #: Consecutive CLEAN qualifying nights at the end of the window.
    streak: int
    #: One of "REVERT", "ADVANCE", "HOLD", "CONTINUE".
    verdict: str
    reason: str


def judge_window(counts: list[NightCounts]) -> WindowVerdict:
    """Fold per-night verdicts, oldest first, into the stage decision.

    The streak counts CLEAN nights since the last HOLD or REVERT; QUIET nights
    are skipped. Any REVERT night, or a second HOLD within HOLD_REVERT_WINDOW
    nights of the previous one, reverts the stage and is the verdict however
    many clean nights follow it -- a revert is a config change an operator
    makes, so the window that saw one is never read as advancing.
    """
    nights = [judge_night(c) for c in sorted(counts, key=lambda c: c.night)]
    streak = 0
    revert: str | None = None
    last_hold: date | None = None
    held = False
    for v in nights:
        if v.verdict == "REVERT":
            revert = revert or f"{v.counts.night}: {v.reason}"
            streak = 0
        elif v.verdict == "HOLD":
            if last_hold is not None and (v.counts.night - last_hold).days < HOLD_REVERT_WINDOW:
                revert = revert or (
                    f"{v.counts.night}: second HOLD within {HOLD_REVERT_WINDOW} nights "
                    f"of {last_hold}"
                )
            last_hold = v.counts.night
            held = True
            streak = 0
        elif v.verdict == "CLEAN":
            streak += 1
            held = False
    if revert:
        return WindowVerdict(nights, streak, "REVERT", revert)
    if streak >= CLEAN_NIGHTS_TO_ADVANCE:
        return WindowVerdict(
            nights, streak, "ADVANCE", f"{streak} consecutive clean qualifying nights"
        )
    if held:
        return WindowVerdict(nights, streak, "HOLD", "the latest qualifying night held")
    return WindowVerdict(
        nights,
        streak,
        "CONTINUE",
        f"{streak} of {CLEAN_NIGHTS_TO_ADVANCE} clean qualifying nights",
    )


def count_log_lines(lines, counts: dict[str, int]) -> None:
    """Add one log's retry, give-up and refusal lines into ``counts``."""
    for line in lines:
        if _GIVEUP_5XX.search(line):
            counts["gave_up_5xx"] += 1
        elif _RETRY_5XX.search(line):
            counts["retried_5xx"] += 1
        elif _RETRY_ANY.search(line):
            counts["retried_other"] += 1
        if SCHEDULER_BLOCK_MARKER in line or any(m in line for m in BLOCK_MESSAGE_MARKERS):
            counts["blocks"] += 1


def night_logs(log_dir: Path, night: date) -> list[tuple[Path, bool]]:
    """The files holding one night's Panoramax traffic, each with whether its
    lines must be filtered to that date.

    Per-attempt child logs are named for the night, so every line counts. The
    scheduler's own log (the weekly screen's retries, and every child's
    ``exited 84`` line) rotates at local midnight, so both its dated rotation
    and the live file are read, keeping only lines stamped with ``night``.
    """
    stamp = night.isoformat()
    files = [(p, False) for p in sorted(log_dir.glob(f"collect_*_panoramax*_{stamp}.log"))]
    for name in (f"streetscape_scheduler.log.{stamp}", "streetscape_scheduler.log"):
        path = log_dir / name
        if path.exists():
            files.append((path, True))
    return files


def gather_night(log_dir: Path, conn: sqlite3.Connection, night: date) -> NightCounts:
    counts = {"retried_5xx": 0, "gave_up_5xx": 0, "retried_other": 0, "blocks": 0}
    stamp = night.isoformat()
    for path, filter_by_date in night_logs(log_dir, night):
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = (ln for ln in fh if not filter_by_date or ln.startswith(stamp))
            count_log_lines(lines, counts)
    requests = 0
    for channel in PANORAMAX_CHANNELS:
        row = conn.execute(
            "SELECT requests FROM api_usage WHERE usage_date = ? AND provider = ?",
            (stamp, channel),
        ).fetchone()
        requests += row[0] if row else 0
    return NightCounts(night=night, requests=requests, **counts)


def format_report(window: WindowVerdict) -> str:
    rows = [
        f"{'night':<10}  {'requests':>8}  {'5xx retry':>9}  {'5xx gave up':>11}  "
        f"{'other retry':>11}  {'refusals':>8}  verdict"
    ]
    for v in window.nights:
        c = v.counts
        rows.append(
            f"{c.night.isoformat():<10}  {c.requests:>8,}  {c.retried_5xx:>9}  "
            f"{c.gave_up_5xx:>11}  {c.retried_other:>11}  {c.blocks:>8}  "
            f"{v.verdict} ({v.reason})"
        )
    rows.append(f"Stage verdict: {window.verdict} -- {window.reason}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="config/scheduler.toml", help="scheduler TOML")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="last night (UTC)")
    parser.add_argument("--nights", type=int, default=14, help="nights to read, ending at --end")
    args = parser.parse_args(argv)
    if args.nights < 1:
        parser.error("--nights must be at least 1")

    from streetscape_metadata_tracker.clock import snapshot_date_today
    from streetscape_metadata_tracker.scheduler import load_scheduler_config

    cfg = load_scheduler_config(args.config)
    end = args.end or snapshot_date_today()
    nights = [end - timedelta(days=i) for i in range(args.nights - 1, -1, -1)]
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    try:
        counts = [gather_night(Path(cfg.log_dir), conn, n) for n in nights]
    finally:
        conn.close()
    window = judge_window(counts)
    print(format_report(window))
    return 1 if window.verdict == "REVERT" else 0


if __name__ == "__main__":
    sys.exit(main())
