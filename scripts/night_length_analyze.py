#!/usr/bin/env python3
"""
How long does a night actually take, at one lane and at several? (issue #240)

`_run_city_channels` can run a city's host-disjoint channels concurrently
(`[schedule].max_concurrent_channels`). The claim being measured is narrow and
one-sided: the night gets SHORTER, and nothing else moves — same cities, same
per-channel request volume, no self-inflicted busy-skips. This script reads the
scheduler's own rotating logs and emits the before/after distribution, plus the
committed record beside docs/experiments/night-length.md.

    python scripts/night_length_analyze.py --logs-dir logs \
        --db-path data/streetscape_tracker.db --docs-dir docs/experiments

WHAT MAKES TWO NIGHTS COMPARABLE
--------------------------------
Elapsed hours alone are not a measurement of this change, because a night's
length is dominated by which cities came due. Three separations follow, and none
of them may be a silent filter:

  full      An unfiltered nightly run that worked its whole slate. The only
            population the headline distribution is taken over.
  filtered  `run-due --provider ...` — a catch-up over a subset of channels.
            Structurally shorter for a reason that has nothing to do with lanes.
  truncated Stopped early (deadline, SIGTERM, city cap, error). Its elapsed is
            a property of the cap, not of the work.

Every night lands in exactly one of those and all three counts are reported, so
"we compared 7 nights" can never be read off a run that quietly dropped 20.

Even inside `full`, the slate varies — so the record carries `hours_per_city`
beside `hours`, and the writeup must quote BOTH: hours is what an operator feels,
hours-per-city is what survives a night of unusually large cities. Neither is a
controlled experiment; this is observational data from production, which is why
the writeup states the confound rather than pretending it away.

THE KNOB COMES FROM THE LOG, NOT FROM MEMORY
--------------------------------------------
`cmd_run_due` logs `max_concurrent_channels=N` on its opening line precisely so a
night's setting is recoverable from the night's own record. A night whose start
line predates that logging is reported as `knob: null` and excluded from the
per-knob summary rather than assumed to be 1 — assuming would silently pool the
pre-#240 corpus into the control group, which is where the whole comparison is.

THE NEGATIVE CONTROLS MATTER AS MUCH AS THE HEADLINE
-----------------------------------------------------
`--db-path` reads `api_usage` for each night's date and reports per-channel
request totals beside the elapsed hours. That is the evidence for "volume did not
move": if a lane night spends more Mapillary tiles than a sequential one, the
change did something it was never supposed to do, and per docs/provider-access.md
that is stop-the-line rather than a footnote. Busy and blocked counts are carried
for the same reason — a busy-host skip (79/80) on a night with no manual run
means our own lanes raced for a per-IP host, i.e. a hole in the affinity gating.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime

# The script's own directory: `experiment_stats` is a sibling MODULE, not a
# package, and tests load this file by path and treat it as a library.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from experiment_stats import describe as _describe  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The committed record beside the writeup. write_docs_record is its ONLY
# producer — the repo rule is that a writeup's numbers trace to committed JSON
# and that JSON to committed code, so `generated_by` stays a runnable command.
DOCS_METRICS_NAME = "night-length_metrics.json"
DOCS_GENERATED_BY = (
    "scripts/night_length_analyze.py --logs-dir logs "
    "--db-path data/streetscape_tracker.db --docs-dir docs/experiments"
)

_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
# The opening line of a run-due. `max_concurrent_channels=N` is optional because
# nights collected before #240 do not carry it, and those must read as unknown.
_START_RE = re.compile(
    rf"^{_TS} - .* - INFO - (?P<due>\d+) cities due on (?P<date>\d{{4}}-\d{{2}}-\d{{2}})"
    r"(?P<filter>\s\[--provider [^\]]*\])?;"
    r"(?:.*?max_concurrent_channels=(?P<knob>\d+))?\s*$"
)
_DONE_RE = re.compile(
    rf"^{_TS} - .* - INFO - Done: run-due (?P<date>\d{{4}}-\d{{2}}-\d{{2}})"
    r"(?P<filter>\s\[--provider [^\]]*\])?: "
    r"(?P<succeeded>\d+)/(?P<attempted>\d+) runs succeeded across "
    r"(?P<cities>\d+) cities in (?P<hours>[\d.]+) h(?P<rest>.*)$"
)
_DEFERRED_RE = re.compile(r"(\d+) deferred for budget")
_BUSY_RE = re.compile(r"(\d+) channel\(s\) skipped")


def _hosts_unavailable(rest: str) -> bool:
    """Did a per-IP host refuse this machine during the night?

    Anchored to the blocked-host NOTE rather than searched for anywhere in the
    tail. `cmd_run_due` builds that tail by joining `; `-separated segments, and
    the blocked note — `"; ".join(labels) + " unavailable"` — is one of them;
    after it come the stop reason and the driving-plan error, both of which carry
    arbitrary exception text. A bare `"unavailable" in rest` therefore fires on a
    plan fetch that failed with `503 Service Unavailable`, and
    `nights_with_a_host_refusal` then reports a per-IP refusal that never
    happened — a false positive on the one signal docs/scheduler.md's rollout
    watch list says should drop the knob to 1 the same day.

    Structural rather than a list of host labels, deliberately: this reads
    HISTORICAL logs, and a label reworded since a night was written must not turn
    that night's refusal invisible.
    """
    return any(segment.strip().endswith(" unavailable") for segment in rest.split(";"))


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S,%f")


def describe(values: list[float]) -> dict:
    """n plus the shape, because a headline number alone is not a finding.

    Hours, so 4 decimals — about a third of a second, well under the resolution
    of a log line's own timestamps.
    """
    return _describe(values, digits=4)


def log_paths(logs_dir: str) -> list[str]:
    """Rotating scheduler logs, oldest first.

    ``TimedRotatingFileHandler`` suffixes rotated files with the date, so plain
    lexical order over the glob is chronological, with the live file last.
    """
    rotated = sorted(glob.glob(os.path.join(logs_dir, "streetscape_scheduler.log.*")))
    live = os.path.join(logs_dir, "streetscape_scheduler.log")
    return rotated + ([live] if os.path.exists(live) else [])


def parse_log(text: str, source: str = "") -> list[dict]:
    """One record per completed run-due in ONE log file's text.

    A night is only recorded when its ``Done:`` line is found, and its knob comes
    from the most recent start line *for the same date*. Anything else reads as
    ``knob: null`` — a start line that rotated out, or a pre-#240 night that
    never logged one. Assuming 1 there would silently pool the whole pre-#240
    corpus into the control group, which is exactly where the comparison lives.
    """
    nights: list[dict] = []
    pending: dict | None = None
    for line in text.splitlines():
        start = _START_RE.match(line)
        if start:
            pending = {
                "date": start.group("date"),
                "knob": int(start.group("knob")) if start.group("knob") else None,
                "due": int(start.group("due")),
                "started_at": start.group(1),
            }
            continue
        done = _DONE_RE.match(line)
        if not done:
            continue
        rest = done.group("rest")
        deferred = _DEFERRED_RE.search(rest)
        busy = _BUSY_RE.search(rest)
        hours = float(done.group("hours"))
        cities = int(done.group("cities"))
        if pending is not None and pending["date"] == done.group("date"):
            knob, due, started_at = pending["knob"], pending["due"], pending["started_at"]
        else:
            knob, due, started_at = None, None, None
        pending = None
        stopped = "stopped early" in rest
        filtered = bool(done.group("filter"))
        nights.append(
            {
                "date": done.group("date"),
                "knob": knob,
                "population": "filtered" if filtered else "truncated" if stopped else "full",
                "hours": round(hours, 4),
                # Reported alongside `hours`, never instead of it: hours is what
                # an operator feels, this is what survives a night of unusually
                # large cities. Null rather than a division by zero when a night
                # attempted nothing.
                "hours_per_city": round(hours / cities, 4) if cities else None,
                "cities": cities,
                "due": due,
                "succeeded": int(done.group("succeeded")),
                "attempted": int(done.group("attempted")),
                "deferred_budget": int(deferred.group(1)) if deferred else 0,
                "busy_channels": int(busy.group(1)) if busy else 0,
                "hosts_unavailable": _hosts_unavailable(rest),
                "stopped_early": stopped,
                "started_at": started_at,
                "finished_at": done.group(1),
                "log": source,
            }
        )
    return nights


def parse_nights(paths: list[str]) -> list[dict]:
    """Every completed run-due across the rotating logs, oldest first.

    Each file is parsed independently on purpose: pairing a ``Done:`` line in one
    file with a start line in another would silently span the retention boundary
    and attribute a night to a knob nobody has evidence it ran under.
    """
    nights: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            nights.extend(parse_log(fh.read(), source=os.path.basename(path)))
    return nights


def api_usage_by_date(db_path: str, dates: list[str]) -> dict:
    """Per-channel request totals for the given dates, from the catalog.

    The negative control: lanes compress wall clock and must leave volume alone.
    Read-only, and missing/unopenable catalogs return {} rather than failing the
    analysis — the elapsed distribution is still worth having without it, as long
    as its absence is visible in the record.
    """
    if not db_path or not os.path.exists(db_path):
        return {}
    usage: dict[str, dict[str, int]] = {}
    # `closing`, not a bare `with`: sqlite3's own context manager is a
    # TRANSACTION manager — it commits or rolls back on exit and leaves the
    # connection, its file handle and its WAL reader mark open until GC. This is
    # a read-only single-shot, so the practical cost is small, but the catalog is
    # the operational source of truth and this file is a pattern others copy.
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        for date_str in sorted(set(dates)):
            rows = conn.execute(
                "SELECT provider, requests FROM api_usage WHERE date = ?", (date_str,)
            ).fetchall()
            if rows:
                usage[date_str] = dict(rows)
    return usage


def summarize(nights: list[dict], usage: dict) -> dict:
    """Per-knob distributions over the comparable population, plus the controls."""
    full = [n for n in nights if n["population"] == "full" and n["knob"] is not None]
    by_knob: dict[str, dict] = {}
    for night in full:
        by_knob.setdefault(str(night["knob"]), {"hours": [], "per_city": [], "dates": []})
        by_knob[str(night["knob"])]["hours"].append(night["hours"])
        if night["hours_per_city"] is not None:
            by_knob[str(night["knob"])]["per_city"].append(night["hours_per_city"])
        by_knob[str(night["knob"])]["dates"].append(night["date"])

    summary = {
        knob: {
            "elapsed_hours": describe(data["hours"]),
            "hours_per_city": describe(data["per_city"]),
            "dates": sorted(data["dates"]),
            "requests_by_channel": _requests_for(data["dates"], usage),
            # A busy-host skip on a night with no manual run means our own lanes
            # raced for a per-IP host. Summed rather than averaged: the claim is
            # that it is ZERO, and a mean would hide one night's three.
            "busy_channel_skips": sum(n["busy_channels"] for n in full if str(n["knob"]) == knob),
            "nights_with_a_host_refusal": sum(
                1 for n in full if str(n["knob"]) == knob and n["hosts_unavailable"]
            ),
        }
        for knob, data in sorted(by_knob.items(), key=lambda kv: int(kv[0]))
    }
    return {
        "by_knob": summary,
        "population_counts": {
            "full": sum(1 for n in nights if n["population"] == "full"),
            "filtered": sum(1 for n in nights if n["population"] == "filtered"),
            "truncated": sum(1 for n in nights if n["population"] == "truncated"),
            "knob_unknown": sum(1 for n in nights if n["knob"] is None),
            "comparable": len(full),
        },
    }


def _requests_for(dates: list[str], usage: dict) -> dict:
    """Per-channel request totals across a knob's nights, with the n they cover.

    ``dates_with_usage`` is carried because a channel total over 5 days and the
    same total over 9 are different claims, and the catalog is optional here.

    DEDUPED, and the name says dates rather than nights for the same reason: a
    knob's ``dates`` list holds one entry per NIGHT, but ``api_usage`` is keyed
    by (date, provider) and is already a whole day's total. Two unfiltered
    run-due invocations on one date — a nightly plus a re-run after a crash,
    which is exactly what an incident night looks like — would otherwise add
    that day's row twice. The inflation lands on the lane side, and the claim
    being tested is "a lane night must not spend MORE than a sequential one", so
    a duplicate would read as the stop-the-line condition in
    docs/provider-access.md rather than as the double-count it is.

    Two nights on one date are still two observations of elapsed hours, so
    `describe` is right to count both; only the volume control has to dedupe.
    """
    totals: dict[str, int] = {}
    covered = 0
    for date_str in sorted(set(dates)):
        day = usage.get(date_str)
        if not day:
            continue
        covered += 1
        for provider, requests in day.items():
            totals[provider] = totals.get(provider, 0) + requests
    return {"dates_with_usage": covered, "total_requests": dict(sorted(totals.items()))}


def write_docs_record(path: str, nights: list[dict], summary: dict, usage: dict) -> None:
    record = {
        "_about": {
            "experiment": "night-length",
            "writeup": "docs/experiments/night-length.md",
            "generated_by": DOCS_GENERATED_BY,
            "note": (
                "Night wall-clock before and after concurrent channel lanes "
                "(issue #240), read out of logs/streetscape_scheduler.log*. "
                "`by_knob` covers only UNFILTERED nights that were not stopped "
                "early and whose start line records max_concurrent_channels; the "
                "other populations are counted, never dropped. Observational "
                "production data, not a controlled experiment: the slate varies "
                "night to night, which is why hours_per_city is reported beside "
                "hours. requests_by_channel is the negative control — lanes must "
                "compress wall clock and leave request volume alone. The source "
                "logs and catalog stay on the collecting host."
            ),
        },
        "summary": summary,
        "api_usage_by_date": usage,
        "nights": nights,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1, sort_keys=False)
        fh.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--logs-dir",
        default=os.path.join(_REPO_ROOT, "logs"),
        help="directory holding streetscape_scheduler.log*",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="catalog to read api_usage from (the volume control); omit to skip it",
    )
    parser.add_argument(
        "--docs-dir",
        default="",
        help=f"write {DOCS_METRICS_NAME} here (normally docs/experiments)",
    )
    args = parser.parse_args(argv)

    paths = log_paths(args.logs_dir)
    if not paths:
        print(f"No scheduler logs under {args.logs_dir}", file=sys.stderr)
        return 1
    nights = parse_nights(paths)
    usage = api_usage_by_date(args.db_path, [n["date"] for n in nights])
    summary = summarize(nights, usage)

    counts = summary["population_counts"]
    print(f"{len(nights)} run-due nights in {len(paths)} log file(s)")
    print(
        f"  comparable: {counts['comparable']}  "
        f"(full {counts['full']}, filtered {counts['filtered']}, "
        f"truncated {counts['truncated']}, knob unknown {counts['knob_unknown']})"
    )
    for knob, data in summary["by_knob"].items():
        hours = data["elapsed_hours"]
        per_city = data["hours_per_city"]
        print(
            f"  max_concurrent_channels={knob}: n={hours['n']}  "
            f"hours p50 {hours.get('p50')} (min {hours.get('min')}, "
            f"p95 {hours.get('p95')}, max {hours.get('max')})  "
            f"h/city p50 {per_city.get('p50')}  "
            f"busy skips {data['busy_channel_skips']}"
        )
        if data["requests_by_channel"]["dates_with_usage"]:
            print(f"      requests: {data['requests_by_channel']['total_requests']}")

    if args.docs_dir:
        out = os.path.join(args.docs_dir, DOCS_METRICS_NAME)
        write_docs_record(out, nights, summary, usage)
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
