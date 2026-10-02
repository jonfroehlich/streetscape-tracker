#!/usr/bin/env python3
"""
Check nights against the Panoramax staged-raise gate (issue #405). Read-only.

Usage (on the host whose logs and catalog it reads)::

    python scripts/panoramax_gate_check.py --config config/scheduler.makelab1.toml \\
        --since 2026-10-03
    python scripts/panoramax_gate_check.py --config config/scheduler.makelab1.toml \\
        --since 2026-10-03 --end 2026-10-12 --log-tz America/Los_Angeles

``--since`` is REQUIRED: it is the first UTC night the CURRENT stage ran, i.e.
the first night after the stage's config was DEPLOYED. A PR merging is not a
deploy, so the repo cannot know that date; it is recorded in the stage table in
docs/provider-access.md when a stage goes live. Reading from before it would
let a previous stage's clean nights advance this one, and a revert that was
already acted on keep failing the check.

It reads, and writes nothing: the per-attempt child logs and the scheduler's
own log under ``[paths].log_dir``, and the ``api_usage`` ledger, which it opens
read-only. It makes no network request of any kind.

THE RULE (docs/provider-access.md, "The staged raise", carries the reasoning
and the sources). Per UTC night, over both channels' ``api_usage`` requests:

* REVERT (to the previous stage, at once) on any per-IP refusal -- an
  ``exited 84`` child, or a Panoramax 403/429/redirect/error-page message --
  on retried 5xx over ``REVERT_RETRY_RATIO`` (10%) of requests (and over 10),
  or on a second HOLD within ``HOLD_REVERT_WINDOW`` (7) nights of a first.
* HOLD (the clean streak resets; the stage stays) on any tile that GAVE UP on
  a 5xx, or retried 5xx over ``max(10, 1% of requests)``.
* UNKNOWN when the ledger shows a qualifying night (>= 1,000 requests) but no
  child log exists for it -- the night cannot be judged, so it resets the
  streak and is flagged rather than read as clean.
* PROVISIONAL for the current UTC date: its batch may still be running, so it
  is shown but never judged clean or held (a refusal still reverts).
* CLEAN otherwise, and it QUALIFIES only at >= 1,000 requests; a QUIET night
  (fewer) neither advances nor resets. Seven consecutive clean qualifying
  nights ADVANCE the stage.

Exit status: 0 when no night since ``--since`` reverts and none is unknown;
1 when ANY night since ``--since`` reverts (not only the last one -- a revert
is acted on by a config change and a new ``--since``); 3 when none reverts
but at least one is UNKNOWN; argparse's 2 on a bad argument.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
#: Exit status when no night reverts but at least one could not be judged.
UNKNOWN_EXIT_CODE = 3
#: The scheduler's line for a child that exited with Panoramax's blocked code,
#: as `_run_collection_subprocess` words it.
SCHEDULER_BLOCK_MARKER = "exited 84 ("
#: Leading text of every Panoramax HostBlockedError, as the collector words it.
#: tests/test_panoramax_gate_check.py derives each from the REAL exception, so
#: a reworded error fails a test rather than silently counting no blocks.
BLOCK_MESSAGE_MARKERS = (
    "Panoramax refused this host",
    "Panoramax redirected instead of serving a tile",
    "Panoramax served an error page",
)
SCHEDULER_LOG_NAME = "streetscape_scheduler.log"
#: A scheduler-log RECORD starts with `setup_logging`'s asctime, which carries
#: milliseconds ("2026-10-02 09:30:00,123 - "). Every other line is a
#: continuation of the record above it -- above all the `--- last 25 lines of
#: <child log> ---` block `_run_collection_subprocess` appends to a failed
#: child's ERROR, whose lines are the CHILD's (no milliseconds) and are already
#: counted from the child's own file. Counting them again double-counts.
_SCHEDULER_RECORD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - ")

_RETRY_5XX = re.compile(re.escape(TILE_RETRY_LOG_PHRASE) + r": HTTP 5\d\d on try")
_RETRY_ANY = re.compile(re.escape(TILE_RETRY_LOG_PHRASE) + r": ")
_GIVEUP_5XX = re.compile(re.escape(TILE_RETRY_LOG_PHRASE) + r" gave up: HTTP 5\d\d")
_GIVEUP_ANY = re.compile(re.escape(TILE_RETRY_LOG_PHRASE) + r" gave up: ")


@dataclass(frozen=True)
class NightCounts:
    """What one night's logs and ledger say. Counts, not judgements."""

    night: date
    requests: int
    retried_5xx: int = 0
    gave_up_5xx: int = 0
    retried_other: int = 0
    gave_up_other: int = 0
    blocks: int = 0
    #: How many per-attempt child logs were found for the night.
    child_logs: int = 1
    #: The current UTC date, whose batch may still be running.
    provisional: bool = False


@dataclass(frozen=True)
class NightVerdict:
    counts: NightCounts
    #: One of "REVERT", "HOLD", "UNKNOWN", "PROVISIONAL", "QUIET", "CLEAN".
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
    # `c.requests and`: with no ledger row there is no ratio to take, and
    # max(10, 0) would revert a night on the 10-retry floor alone; the HOLD arm
    # below still sees those retries.
    if c.requests and c.retried_5xx > max(HOLD_MIN_RETRIES, REVERT_RETRY_RATIO * c.requests):
        return NightVerdict(
            c,
            "REVERT",
            f"{c.retried_5xx} retried 5xx is over {REVERT_RETRY_RATIO:.0%} of "
            f"{c.requests:,} requests",
        )
    if c.provisional:
        return NightVerdict(c, "PROVISIONAL", "today (UTC): the batch may still be running")
    if c.child_logs == 0 and c.requests >= MIN_QUALIFYING_REQUESTS:
        return NightVerdict(
            c,
            "UNKNOWN",
            f"{c.requests:,} requests in the ledger but no collect_*_panoramax* log",
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
    #: One of "REVERT", "ADVANCE", "HOLD", "UNKNOWN", "CONTINUE".
    verdict: str
    reason: str
    unknown: int = 0


def judge_window(counts: list[NightCounts]) -> WindowVerdict:
    """Fold per-night verdicts, oldest first, into the stage decision.

    The streak counts CLEAN nights since the last HOLD, REVERT or UNKNOWN;
    QUIET and PROVISIONAL nights are skipped. Any REVERT night, or a second
    HOLD within HOLD_REVERT_WINDOW nights of the previous one, reverts the
    stage and is the verdict however many clean nights follow it.
    """
    nights = [judge_night(c) for c in sorted(counts, key=lambda c: c.night)]
    streak = 0
    revert: str | None = None
    last_hold: date | None = None
    latest = None
    unknown = 0
    for v in nights:
        if v.verdict == "REVERT":
            revert = revert or f"{v.counts.night}: {v.reason}"
            streak = 0
            latest = v.verdict
        elif v.verdict == "HOLD":
            if last_hold is not None and (v.counts.night - last_hold).days < HOLD_REVERT_WINDOW:
                revert = revert or (
                    f"{v.counts.night}: second HOLD within {HOLD_REVERT_WINDOW} nights "
                    f"of {last_hold}"
                )
            last_hold = v.counts.night
            streak = 0
            latest = v.verdict
        elif v.verdict == "UNKNOWN":
            unknown += 1
            streak = 0
            latest = v.verdict
        elif v.verdict == "CLEAN":
            streak += 1
            latest = v.verdict
    if revert:
        return WindowVerdict(nights, streak, "REVERT", revert, unknown)
    if streak >= CLEAN_NIGHTS_TO_ADVANCE:
        return WindowVerdict(
            nights, streak, "ADVANCE", f"{streak} consecutive clean qualifying nights", unknown
        )
    if latest == "HOLD":
        return WindowVerdict(nights, streak, "HOLD", "the latest judged night held", unknown)
    if latest == "UNKNOWN":
        return WindowVerdict(
            nights, streak, "UNKNOWN", "the latest judged night has no child log", unknown
        )
    return WindowVerdict(
        nights,
        streak,
        "CONTINUE",
        f"{streak} of {CLEAN_NIGHTS_TO_ADVANCE} clean qualifying nights",
        unknown,
    )


def _empty_counts() -> dict[str, int]:
    return {
        "retried_5xx": 0,
        "gave_up_5xx": 0,
        "retried_other": 0,
        "gave_up_other": 0,
        "blocks": 0,
    }


def count_log_lines(lines, counts: dict[str, int]) -> None:
    """Add log lines' retries, give-ups and refusals into ``counts``.

    A retry or give-up counts as 5xx only when its line names an HTTP 5xx
    status; a timeout, a connection error or a retried 4xx is "other",
    printed and never gated.
    """
    for line in lines:
        if _GIVEUP_5XX.search(line):
            counts["gave_up_5xx"] += 1
        elif _GIVEUP_ANY.search(line):
            counts["gave_up_other"] += 1
        elif _RETRY_5XX.search(line):
            counts["retried_5xx"] += 1
        elif _RETRY_ANY.search(line):
            counts["retried_other"] += 1
        if SCHEDULER_BLOCK_MARKER in line or any(m in line for m in BLOCK_MESSAGE_MARKERS):
            counts["blocks"] += 1


def local_to_utc_date(stamp: str, log_tz: ZoneInfo | None) -> date:
    """The UTC date of a host-LOCAL ``YYYY-MM-DD HH:MM:SS`` log timestamp.

    ``log_tz`` None means this process's own local zone, which is right when
    the script runs on the host that wrote the log (the documented use).

    >>> local_to_utc_date("2026-10-05 18:30:00", ZoneInfo("America/Los_Angeles"))
    datetime.date(2026, 10, 6)
    """
    naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    aware = naive.astimezone() if log_tz is None else naive.replace(tzinfo=log_tz)
    return aware.astimezone(UTC).date()


def scheduler_log_files(log_dir: Path, night: date) -> list[Path]:
    """The scheduler-log files that can hold records from UTC ``night``.

    The log rotates at LOCAL midnight into ``<name>.<local date>``, and a UTC
    night spans two local dates anywhere off UTC (in Pacific time, 17:00 the
    day before to 17:00 the same day), so the rotations for the local dates on
    either side are read as well as the live file; each record is then kept
    only if its own timestamp falls on ``night`` in UTC.
    """
    names = [f"{SCHEDULER_LOG_NAME}.{(night + timedelta(days=d)).isoformat()}" for d in (-1, 0, 1)]
    names.append(SCHEDULER_LOG_NAME)
    return [log_dir / n for n in names if (log_dir / n).exists()]


def scheduler_records_for(lines, night: date, log_tz: ZoneInfo | None):
    """The scheduler log's OWN record lines on UTC ``night``; continuations
    (a failed child's copied tail above all) are skipped."""
    for line in lines:
        m = _SCHEDULER_RECORD.match(line)
        if m and local_to_utc_date(m.group(1), log_tz) == night:
            yield line


def child_log_files(log_dir: Path, night: date) -> list[Path]:
    """Per-attempt child logs, named for the UTC run date, so every line counts."""
    return sorted(log_dir.glob(f"collect_*_panoramax*_{night.isoformat()}.log"))


def open_ledger(db_path: str) -> sqlite3.Connection:
    """The catalog, opened READ-ONLY: this script must never write to it."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def gather_night(
    log_dir: Path,
    conn: sqlite3.Connection,
    night: date,
    *,
    log_tz: ZoneInfo | None = None,
    today: date | None = None,
) -> NightCounts:
    counts = _empty_counts()
    children = child_log_files(log_dir, night)
    for path in children:
        with open(path, encoding="utf-8", errors="replace") as fh:
            count_log_lines(fh, counts)
    for path in scheduler_log_files(log_dir, night):
        with open(path, encoding="utf-8", errors="replace") as fh:
            count_log_lines(scheduler_records_for(fh, night, log_tz), counts)
    requests = 0
    for channel in PANORAMAX_CHANNELS:
        row = conn.execute(
            "SELECT requests FROM api_usage WHERE usage_date = ? AND provider = ?",
            (night.isoformat(), channel),
        ).fetchone()
        requests += row[0] if row else 0
    return NightCounts(
        night=night,
        requests=requests,
        child_logs=len(children),
        provisional=today is not None and night >= today,
        **counts,
    )


def format_report(window: WindowVerdict) -> str:
    rows = [
        f"{'night':<10}  {'requests':>8}  {'5xx retry':>9}  {'5xx gave up':>11}  "
        f"{'other retry':>11}  {'other gave up':>13}  {'refusals':>8}  {'logs':>4}  verdict"
    ]
    for v in window.nights:
        c = v.counts
        rows.append(
            f"{c.night.isoformat():<10}  {c.requests:>8,}  {c.retried_5xx:>9}  "
            f"{c.gave_up_5xx:>11}  {c.retried_other:>11}  {c.gave_up_other:>13}  "
            f"{c.blocks:>8}  {c.child_logs:>4}  {v.verdict} ({v.reason})"
        )
    if window.unknown:
        rows.append(
            f"WARNING: {window.unknown} night(s) UNKNOWN -- the ledger shows Panoramax "
            f"traffic that no child log accounts for. Find the logs before trusting "
            f"this window."
        )
    rows.append(f"Stage verdict: {window.verdict} -- {window.reason}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="config/scheduler.toml", help="scheduler TOML")
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        required=True,
        help="first UTC night of the CURRENT stage (its deploy date; see provider-access.md)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="last UTC night to read (default: yesterday, the last finished night)",
    )
    parser.add_argument(
        "--log-tz",
        default=None,
        help="IANA zone the scheduler log's timestamps are in (default: this host's local zone)",
    )
    args = parser.parse_args(argv)

    from streetscape_metadata_tracker.clock import snapshot_date_today
    from streetscape_metadata_tracker.scheduler import load_scheduler_config

    today = snapshot_date_today()
    end = args.end or today - timedelta(days=1)
    if end > today:
        parser.error(f"--end {end} is in the future (today is {today} UTC)")
    if args.since > end:
        parser.error(f"--since {args.since} is after --end {end}")
    log_tz = ZoneInfo(args.log_tz) if args.log_tz else None

    cfg = load_scheduler_config(args.config)
    nights = [args.since + timedelta(days=i) for i in range((end - args.since).days + 1)]
    conn = open_ledger(cfg.db_path)
    try:
        counts = [
            gather_night(Path(cfg.log_dir), conn, n, log_tz=log_tz, today=today) for n in nights
        ]
    finally:
        conn.close()
    window = judge_window(counts)
    print(format_report(window))
    if window.verdict == "REVERT":
        return 1
    return UNKNOWN_EXIT_CODE if window.unknown else 0


if __name__ == "__main__":
    sys.exit(main())
