#!/usr/bin/env python3
"""
Mapillary, night by night: the #444 Stage 1 read-out in one read-only command.

    python scripts/mapillary_night_report.py \
        --config config/scheduler.makelab1.toml --since 2026-10-22 [--json OUT]

Run on the catalog host. It makes NO provider request and writes nothing but
the optional ``--json`` file: it reads the catalog (opened read-only, and
refused unless it is exactly this code's schema version -- the 2026-10-18
outage was a script that migrated prod's catalog), the scheduler's rotating
log and the per-attempt child logs under ``[paths].log_dir``. It never lists
``data/``.

WHAT ONE ROW IS
---------------
One UTC night (the run date every runs/walks row and the ``Done:`` line carry):

- ``served`` -- ``api_usage`` for ``mapillary`` + ``mapillary_streets``: the
  PER-PROCESS ledger, i.e. what this IP actually asked the tile CDN that UTC
  day. ``runs.api_requests`` is NOT that figure: for a census provider the
  collector stores the crawl's CUMULATIVE ``api_requests_total`` there
  (``cli.py``), so a crawl paused on 10-22/23 and finished on 10-24 shows its
  whole sweep on the 10-24 row. The row's sum is printed beside, as
  ``sweep_total``, and never added to ``served``.
- grid runs and walks completed that night (``runs`` / ``street_walks``).
- from the nightly ``Done:`` line: rolling-pool deferrals, a standing hold
  (rest night, cooldown), a refusal or throttle and the launches skipped while
  latched, the presence window on the tile CDN and its gap to the previous
  ledgered request, the fill's outcome, the stop reason.
- tile statuses: every ``mapillary tile statuses:`` line in that night's
  ``collect_*_mapillary*_<date>.log`` files, summed. A night with no such line
  prints ``n/a``, never 0: no line is "not observed", and a 0 would read as
  "no 429s".
- the fill's room on the tile CDN, first and last, as the fill's decline lines
  quote it (the room is printed only when a city is declined for it, so a night
  that admitted every candidate has ``n/a`` here).
- the stranded tier: the hoist's ``N of M cities not due on gsv are inside
  tonight's cap`` line. That counts EVERY stranded kind (gsv-excluded, opt-in
  enrolments, Mapillary-only-due), so it is a proxy; the Mapillary-only-due
  figure below is the exact one.
- issue #375's two numbers: Mapillary channels deferred for the batch deadline
  (the ``Done:`` line's clause) and Mapillary channels cut by a wind-down (a
  child killed by the stop signal, or a channel not started after it).

THE MAPILLARY-ONLY-DUE POPULATION
---------------------------------
Cities ``db.get_due_cities`` returns for ``mapillary`` or ``mapillary_streets``
and not for ``gsv``, as of ``--as-of`` (default: today, UTC) -- the population
kind 2 of the bounded hoist drains (docs/scheduler.md). It is a SNAPSHOT, not a
per-night series: ``schedule_state`` keeps only each channel's newest clock, so
the catalog cannot say what was due on an earlier night. Priced cache-blind at
the grid census's tile count, once per city (both Mapillary channels read the
same census, and a paired walk is free from the #290 cache), so the total is an
upper bound. ``walk_clock_unpaired`` counts cities whose two Mapillary clocks
last succeeded on different dates -- the reading that answers whether 0-walk
nights are un-paired clocks or deferred walks.

THE STAGE 1 CLOCK
-----------------
docs/provider-access.md: Stage 1's three-week test starts only at the design
volume. ``design_nights`` counts active nights (``served`` > 0) that served at
least 75% of the ``[hosts.mapillary_tiles]`` rolling pool; the criterion is
>= 15 active nights, >= 4 of every 5 of them at the design point, 0
``host_refusals`` refusals, and 0 429/5xx in the summed tile statuses OF EVERY
ACTIVE NIGHT.

Presence is REPORTED per night (and its range over the design nights), never
gated. A first draft also required >= 2 h of presence, and that clause could
not be met under the config it was written for: at 40/min through one host
lock the 1,200 pool is ~30 min of fetching, the one full-pool night (10-22)
showed 1.1 h, and the 10 -> 20 reservation hoists the Mapillary-due cities to
the HEAD of the slate, which concentrates presence further. #444's treatment
(H1) is LESS presence, so a presence floor would also have counted only nights
that looked like the pre-treatment ones.

Absence of evidence is never a pass: an active night with no tile-status line
in its child logs is counted in ``nights_without_status``, and while any such
night is in the window the 429/5xx clause -- and so the criterion -- is
unknown, not met. Every crawl writes the line (download_mapillary's
``finally``, and the cache-hit path), so a missing one means pruned or
mis-pathed child logs. Start ``--since`` at the step's first night: nights
before the status line was deployed (2026-10-21) have none and keep the clock
from starting.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.night_length_analyze import log_paths  # noqa: E402
from scripts.sweep_orphan_diff_details import (  # noqa: E402
    CatalogRefused,
    open_catalog_readonly,
)
from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.download_common import (  # noqa: E402
    HOST_LABELS,
    HOST_MAPILLARY_TILES,
)
from streetscape_metadata_tracker.download_mapillary import (  # noqa: E402
    TILE_STATUS_CLASSES,
    TILE_STATUS_LINE_PREFIX,
)

MAPILLARY_CHANNELS = ("mapillary", "mapillary_streets")
# The Stage 1 "informative volume" criterion (docs/provider-access.md, #444).
DESIGN_POOL_SHARE = 0.75
DESIGN_MIN_ACTIVE_NIGHTS = 15
DESIGN_MIN_SHARE = 0.8  # >= 4 of every 5 active nights

_LABEL = re.escape(HOST_LABELS[HOST_MAPILLARY_TILES])
_TS = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)"
_DATE = r"\d{4}-\d{2}-\d{2}"

_DONE_RE = re.compile(
    rf"^{_TS} - .* - INFO - Done: run-due (?P<date>{_DATE})"
    r"(?P<filter>(?:\s\[--[a-z-]+ [^\]]*\])*): "
    r"(?P<succeeded>\d+)/(?P<attempted>\d+) runs succeeded across (?P<cities>\d+) cities"
    r"(?: \((?P<due_cities>\d+) due, (?P<fill_cities>\d+) fill\))?"
    r" in (?P<hours>[\d.]+) h(?P<rest>.*)$"
)
_START_RE = re.compile(
    rf"^{_TS} - .* - INFO - (?P<due>\d+) cities due on (?P<date>{_DATE})"
    r"(?P<filter>(?:\s\[--[a-z-]+ [^\]]*\])*);"
    r"(?: processing up to (?P<day_cap>\d+) )?"
)
_POOL_RE = re.compile(rf"(\d+) deferred for the rolling-24h budget of {_LABEL}")
_PRESENCE_RE = re.compile(
    rf"on {_LABEL} (\d\d:\d\d)-(\d\d:\d\d) UTC \(([\d.]+) h\)"
    r"(?:, ([\d.]+) h after its previous ledgered request)?"
)
_HELD_RE = re.compile(rf"HELD (?:[^;]*?){_LABEL}: (?P<why>[^;]*?), (?P<n>\d+) launch\(es\) held")
_ANY_LABEL = "|".join(re.escape(label) for label in sorted(HOST_LABELS.values()))
# One verdict group of the blocked clause: a run of host labels at a clause
# boundary ("; " between clauses, ", " between the clause's parts), then its
# verdict. The boundary keeps the pool clause's "budget of <label>" out of it.
_HOST_RUN_RE = re.compile(
    rf"(?:(?<=; )|(?<=, )|^)(?P<labels>(?:{_ANY_LABEL})(?:; (?:{_ANY_LABEL}))*) "
    r"(?P<verdict>unavailable|THROTTLED|refused this host then recovered on re-check)"
)
_SKIPPED_RE = re.compile(r"(\d+) launch\(es\) skipped while latched")
_DEADLINE_RE = re.compile(r"\d+ channel\(s\) deferred for the deadline \((?P<by>[^)]*)\)")
_STOPPED_RE = re.compile(r"; stopped early \((?P<why>[^)]*)\)")
_FILL_RE = re.compile(r"fill \(early refresh, >= \d+ d\): (?P<cities>\d+) cities")
_FILL_ENDED_RE = re.compile(r"; ended by (?P<why>[^;]+)")
_FILL_DECLINED_TILES_RE = re.compile(r"(?<![\d,])(\d[\d,]*) for mapillary_tiles")
_RESERVED_RE = re.compile(r"reserved ([\d,]+) on mapillary_tiles for tomorrow's due")
_HOLDING_RE = re.compile(r"holding (?:[^;]*?)mapillary \((\d+) due not attempted\)")
_STRANDED_RE = re.compile(
    rf"^{_TS} - .* - INFO - (?P<reached>\d+) of (?P<total>\d+) "
    r"(?:opt-in-only cities|cities not due on \w+) are inside tonight's (?P<cap>\d+)-city cap"
)
_FILL_ROOM_RE = re.compile(
    rf"^{_TS} - .* \[fill\]: declined — ~[\d,]+ req on {_LABEL} does not fit its fill room "
    r"\((?P<room>[\d,]+)\)"
)
_KILLED_RE = re.compile(
    r" - (?P<city>\S+) \[(?P<channel>mapillary(?:_streets)?)\]: child was killed by the stop signal"
)
_NOT_STARTED_RE = re.compile(
    r" - (?P<city>\S+): stop requested — not starting (?P<channels>[^.]*)\. Not counted"
)
_CHILD_LOG_RE = re.compile(
    rf"^collect_(?P<city>.+)_(?P<channel>mapillary|mapillary_streets)_(?P<date>{_DATE})\.log$"
)
_STATUS_RE = re.compile(
    re.escape(TILE_STATUS_LINE_PREFIX)
    + "".join(rf" {re.escape(k)}=(?P<s{i}>\d+)" for i, k in enumerate(TILE_STATUS_CLASSES))
    + r" noresp=(?P<noresp>\d+) requests=(?P<requests>\d+) retries=(?P<retries>\d+)"
)


def _int(text: str) -> int:
    return int(text.replace(",", ""))


@dataclass
class LogRun:
    """One ``run-due`` as its scheduler-log segment describes it."""

    date: str
    filtered: bool
    done: bool
    cities: int | None = None
    hours: float | None = None
    succeeded: int | None = None
    attempted: int | None = None
    pool_deferred: int = 0
    held: str | None = None
    held_launches: int = 0
    refused: bool = False
    throttled: bool = False
    skipped_latched: int = 0
    presence: str | None = None
    presence_h: float | None = None
    gap_h: float | None = None
    deadline_deferred: int = 0
    stopped: str | None = None
    fill: str | None = None
    fill_cities: int | None = None
    fill_declined_tiles: int = 0
    reserved_tiles: int | None = None
    holding_mapillary: int = 0
    stranded_reached: int | None = None
    stranded_total: int | None = None
    fill_rooms: list[int] = field(default_factory=list)
    winddown_cut: int = 0
    # From the start line: how many cities were due and how many the run said
    # it would process. A bare `run-due --limit N` leaves no [--...] mark, so
    # a cap below both the due count and max_cities_per_day is what tells it
    # from the nightly (build_report).
    due_count: int | None = None
    day_cap: int | None = None
    # A start line with no Done: line before the NEXT start line: the run
    # died (the 2026-10-18 shape), it is not in flight.
    crashed: bool = False


def parse_done_rest(run: LogRun, rest: str) -> None:
    """Fill ``run`` from a ``Done:`` line's clauses (everything after the hours)."""
    if m := _POOL_RE.search(rest):
        run.pool_deferred = int(m.group(1))
    if m := _PRESENCE_RE.search(rest):
        run.presence = f"{m.group(1)}-{m.group(2)}"
        run.presence_h = float(m.group(3))
        run.gap_h = float(m.group(4)) if m.group(4) else None
    if m := _HELD_RE.search(rest):
        run.held, run.held_launches = m.group("why"), int(m.group("n"))
    # The blocked clause joins every refused host's label with "; " and names
    # the verdict ONCE, after the last label (scheduler._blocked_summary_note),
    # so Mapillary's label -- which sorts first -- is followed by another
    # host's, not by the verdict, whenever two hosts refused the same night.
    # Read the whole label run, never one ";"-segment.
    for m in _HOST_RUN_RE.finditer(rest):
        if HOST_LABELS[HOST_MAPILLARY_TILES] not in m.group("labels"):
            continue
        if m.group("verdict") == "THROTTLED":
            run.throttled = True
        else:
            run.refused = True
    if m := _SKIPPED_RE.search(rest):
        run.skipped_latched = int(m.group(1))
    if m := _DEADLINE_RE.search(rest):
        for part in m.group("by").split(","):
            channel, _, n = part.strip().rpartition(" ")
            if channel in MAPILLARY_CHANNELS:
                run.deadline_deferred += int(n)
    if m := _STOPPED_RE.search(rest):
        run.stopped = m.group("why")
    if "fill not reached" in rest:
        run.fill = "not reached"
    elif m := _FILL_RE.search(rest):
        run.fill_cities = int(m.group("cities"))
        # The fill's clauses run from its header to its own "ended by"; the
        # night's later clauses (quarantine, stop reason) are not the fill's.
        fill_part = rest[m.start() :]
        ended = _FILL_ENDED_RE.search(fill_part)
        if ended:
            fill_part = fill_part[: ended.end()]
        run.fill = f"{run.fill_cities} cities" + (
            f", ended by {ended.group('why').strip()}" if ended else ""
        )
        if d := _FILL_DECLINED_TILES_RE.search(fill_part):
            run.fill_declined_tiles = _int(d.group(1))
        if r := _RESERVED_RE.search(fill_part):
            run.reserved_tiles = _int(r.group(1))
        if h := _HOLDING_RE.search(fill_part):
            run.holding_mapillary = int(h.group(1))


def parse_scheduler_log(lines) -> list[LogRun]:
    """Every ``run-due`` in the scheduler log, in order, one segment each.

    A run's events (the stranded line, fill declines, wind-down cuts) are logged
    BEFORE its ``Done:`` line -- the stranded line even before its start line --
    so a segment is everything since the previous ``Done:``, and it belongs to
    the run that ``Done:`` closes. A second start line before any ``Done:``
    means the first run died: it is closed there as ``done=False``,
    ``crashed=True``, and a stranded line seen after its start (which can only
    be the NEXT run's, since that line precedes its own start) moves with the
    next run. A trailing segment with no ``Done:`` is a run still in flight:
    dated from its start line and marked ``done=False``; with no start line
    either, it is dropped.
    """
    runs: list[LogRun] = []
    pending = LogRun(date="", filtered=False, done=False)
    carried: tuple[int, int] | None = None  # a stranded line seen after a start
    for line in lines:
        if m := _STRANDED_RE.match(line):
            reached, total = int(m.group("reached")), int(m.group("total"))
            if pending.date:
                carried = (reached, total)
            else:
                pending.stranded_reached, pending.stranded_total = reached, total
            continue
        if m := _FILL_ROOM_RE.match(line):
            pending.fill_rooms.append(_int(m.group("room")))
            continue
        if _KILLED_RE.search(line):
            pending.winddown_cut += 1
            continue
        if m := _NOT_STARTED_RE.search(line):
            pending.winddown_cut += sum(
                1 for ch in m.group("channels").split(", ") if ch.strip() in MAPILLARY_CHANNELS
            )
            continue
        if m := _START_RE.match(line):
            if pending.date:
                pending.crashed = True
                runs.append(pending)
                pending = LogRun(date="", filtered=False, done=False)
                if carried:
                    pending.stranded_reached, pending.stranded_total = carried
            carried = None
            pending.date, pending.filtered = m.group("date"), bool(m.group("filter"))
            pending.due_count = int(m.group("due"))
            pending.day_cap = int(m.group("day_cap")) if m.group("day_cap") else None
            continue
        if m := _DONE_RE.match(line):
            run = pending
            run.date, run.filtered, run.done = m.group("date"), bool(m.group("filter")), True
            run.cities = int(m.group("cities"))
            run.hours = float(m.group("hours"))
            run.succeeded = int(m.group("succeeded"))
            run.attempted = int(m.group("attempted"))
            parse_done_rest(run, m.group("rest"))
            runs.append(run)
            pending = LogRun(date="", filtered=False, done=False)
            if carried:
                # Logged after this run's start, so it was the next run's.
                pending.stranded_reached, pending.stranded_total = carried
                carried = None
    if pending.date:
        runs.append(pending)
    return runs


def read_scheduler_logs(log_dir: str) -> list[LogRun]:
    """Parse the rotating scheduler logs, oldest first, as one stream."""
    lines: list[str] = []
    for path in log_paths(log_dir):
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines.extend(fh.read().splitlines())
    return parse_scheduler_log(lines)


def tile_statuses_by_night(log_dir: str) -> dict[str, dict]:
    """Summed ``mapillary tile statuses:`` counts per run date, from the child logs.

    Keyed by the DATE IN THE FILENAME (the run date), not the line's local
    timestamp. ``logs`` counts the Mapillary child logs for that date, and
    ``crawls`` the status lines found in them; a date with logs but no status
    line is returned with ``crawls == 0`` so the caller prints ``n/a``.
    """
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(log_dir, "collect_*_mapillary*_*.log")):
        m = _CHILD_LOG_RE.match(os.path.basename(path))
        if not m:
            continue
        night = out.setdefault(
            m.group("date"),
            {"logs": 0, "crawls": 0, "requests": 0, "retries": 0, "noresp": 0}
            | dict.fromkeys(TILE_STATUS_CLASSES, 0),
        )
        night["logs"] += 1
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = _STATUS_RE.search(line)
                if not s:
                    continue
                night["crawls"] += 1
                for i, key in enumerate(TILE_STATUS_CLASSES):
                    night[key] += int(s.group(f"s{i}"))
                for key in ("requests", "retries", "noresp"):
                    night[key] += int(s.group(key))
    return out


def catalog_nights(conn, since: str, until: str) -> dict[str, dict]:
    """Per-night served, grid runs and walks from the catalog (read-only)."""
    nights: dict[str, dict] = {}

    def night(d: str) -> dict:
        return nights.setdefault(
            d, {"served": 0, "served_by": {}, "grid_runs": 0, "sweep_total": 0, "walks": 0}
        )

    marks = ",".join("?" * len(MAPILLARY_CHANNELS))
    for d, provider, requests in conn.execute(
        f"SELECT usage_date, provider, requests FROM api_usage "
        f"WHERE provider IN ({marks}) AND usage_date BETWEEN ? AND ?",
        (*MAPILLARY_CHANNELS, since, until),
    ):
        n = night(d)
        n["served"] += requests or 0
        n["served_by"][provider] = requests or 0
    for d, count, total in conn.execute(
        "SELECT run_date, COUNT(*), SUM(COALESCE(api_requests, 0)) FROM runs "
        "WHERE provider = 'mapillary' AND run_date BETWEEN ? AND ? GROUP BY run_date",
        (since, until),
    ):
        n = night(d)
        n["grid_runs"], n["sweep_total"] = count, total or 0
    for d, count in conn.execute(
        "SELECT run_date, COUNT(*) FROM street_walks "
        "WHERE provider = 'mapillary' AND run_date BETWEEN ? AND ? GROUP BY run_date",
        (since, until),
    ):
        night(d)["walks"] = count
    return nights


def host_refusal_rows(conn, since: str) -> list[dict]:
    """``host_refusals`` rows for the tile CDN since ``since`` (both verdicts)."""
    rows = conn.execute(
        "SELECT recorded_at, verdict, source, channel, city_id, exit_code, requests_before "
        "FROM host_refusals WHERE host = ? AND recorded_at >= ? ORDER BY recorded_at",
        (HOST_MAPILLARY_TILES, since),
    ).fetchall()
    return [dict(r) for r in rows]


def mapillary_only_due(conn, cfg, as_of: date) -> dict:
    """The Mapillary-only-due population as of ``as_of`` (see the module docstring)."""
    from streetscape_metadata_tracker.scheduler import (
        CHANNEL_DEFAULT_MEMBERSHIP,
        estimate_requests,
    )

    def due(channel: str) -> dict[str, db.CityRow]:
        return {
            c.city_id: c
            for c in db.get_due_cities(
                conn,
                today=as_of,
                cycle_days=cfg.cycle_days,
                grace_days=cfg.grace_days,
                max_consecutive_failures=cfg.max_consecutive_failures,
                default_membership=CHANNEL_DEFAULT_MEMBERSHIP[channel],
                provider=channel,
            )
        }

    gsv = due("gsv")
    grid, walk = due("mapillary"), due("mapillary_streets")
    population = {cid: c for cid, c in (grid | walk).items() if cid not in gsv}
    paired_due = {cid for cid in (grid | walk) if cid in gsv}
    priced = sum(estimate_requests(c, "mapillary") for c in population.values())
    unpaired = conn.execute(
        """SELECT COUNT(*) FROM schedule_state g
           JOIN schedule_state w ON w.city_id = g.city_id
           JOIN cities c ON c.city_id = g.city_id
           WHERE g.provider = 'mapillary' AND w.provider = 'mapillary_streets'
             AND c.enabled = 1
             AND g.last_success_at IS NOT NULL AND w.last_success_at IS NOT NULL
             AND substr(g.last_success_at, 1, 10) <> substr(w.last_success_at, 1, 10)"""
    ).fetchone()[0]
    return {
        "as_of": as_of.isoformat(),
        "cities": len(population),
        "grid_due": sum(1 for cid in population if cid in grid),
        "walk_only_due": sum(1 for cid in population if cid not in grid),
        "priced_requests_upper_bound": priced,
        "also_due_on_gsv": len(paired_due),
        "walk_clock_unpaired": unpaired,
        "city_ids": sorted(population),
    }


def build_report(conn, cfg, *, since: str, until: str, as_of: date, log_dir: str) -> dict:
    """Everything the table prints, as one JSON-able dict."""
    catalog = catalog_nights(conn, since, until)
    statuses = tile_statuses_by_night(log_dir)
    log_runs = read_scheduler_logs(log_dir)
    pool = (cfg.host_budgets or {}).get(HOST_MAPILLARY_TILES)

    dates = set(catalog)
    dates |= {d for d in statuses if since <= d <= until}
    dates |= {r.date for r in log_runs if since <= r.date <= until}
    nights = []
    for d in sorted(dates):
        on_date = [r for r in log_runs if r.date == d]
        nightly = [r for r in on_date if not r.filtered and not _narrowed(r, cfg)]
        # The FIRST full run of the date is the 02:00 nightly; a later
        # unfiltered one is a hand run, and so is anything narrowed.
        run = nightly[0] if nightly else None
        hand = [r for r in on_date if r is not run]
        cat = catalog.get(d, {"served": 0, "served_by": {}, "grid_runs": 0, "sweep_total": 0})
        st = statuses.get(d)
        tiles = st if st and st["crawls"] else None
        nights.append(
            {
                "date": d,
                "served": cat["served"],
                "served_by": cat["served_by"],
                "grid_runs": cat["grid_runs"],
                "walks": cat.get("walks", 0),
                "sweep_total": cat["sweep_total"],
                "mapillary_child_logs": st["logs"] if st else 0,
                "tile_statuses": tiles,
                "nightly_run": asdict(run) if run else None,
                "hand_runs": len(hand),
                "ended_by": _ended_by(run, cfg),
                "fill_room_first": run.fill_rooms[0] if run and run.fill_rooms else None,
                "fill_room_last": run.fill_rooms[-1] if run and run.fill_rooms else None,
            }
        )

    refusals = host_refusal_rows(conn, since)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "since": since,
        "until": until,
        "pool": pool,
        "nights": nights,
        "host_refusals": refusals,
        "mapillary_only_due": mapillary_only_due(conn, cfg, as_of),
        "stage1_clock": _stage1_clock(nights, pool, refusals),
        "issue_375": {
            "mapillary_deadline_deferred": sum(
                (n["nightly_run"] or {}).get("deadline_deferred", 0) for n in nights
            ),
            "mapillary_winddown_cut": sum(
                (n["nightly_run"] or {}).get("winddown_cut", 0) for n in nights
            ),
        },
    }


def _narrowed(run: LogRun, cfg) -> bool:
    """A bare ``--limit N`` run: its start line's cap is below both the due
    count and ``max_cities_per_day``, which only ``--limit`` does (``--provider``
    and ``--city`` carry their own ``[--...]`` mark)."""
    if run.day_cap is None or run.due_count is None:
        return False
    return run.day_cap < min(run.due_count, cfg.max_cities_per_day)


def _ended_by(run: LogRun | None, cfg) -> str | None:
    if run is None:
        return None
    if run.crashed:
        return "crashed (no Done line; a later run started)"
    if not run.done:
        return "in flight (no Done line yet)"
    if run.stopped:
        return run.stopped
    if run.fill and "ended by" in run.fill:
        return "fill: " + run.fill.split("ended by ", 1)[1]
    if run.cities is not None and run.cities >= cfg.max_cities_per_day:
        return f"city cap ({cfg.max_cities_per_day})"
    return "due slate worked through"


def _stage1_clock(nights: list[dict], pool: int | None, refusals: list[dict]) -> dict:
    """Has Stage 1's three-week clock started? (docs/provider-access.md, #444)."""
    active = [n for n in nights if n["served"] > 0]
    design = [n for n in active if pool and n["served"] >= DESIGN_POOL_SHARE * pool]
    presences = [
        (n["nightly_run"] or {}).get("presence_h")
        for n in design
        if (n["nightly_run"] or {}).get("presence_h") is not None
    ]
    seen = [n["tile_statuses"] for n in nights if n["tile_statuses"]]
    seen_warning = sum(s["429"] + s["5xx"] for s in seen) if seen else None
    # An active night with no status line is UNKNOWN, never 0: the sum over
    # the nights that have one says nothing about the nights that do not.
    without_status = sum(1 for n in active if not n["tile_statuses"])
    warning = None if without_status else seen_warning
    refused = sum(1 for r in refusals if r["verdict"] == "refused")
    met = (
        len(active) >= DESIGN_MIN_ACTIVE_NIGHTS
        and len(design) >= DESIGN_MIN_SHARE * len(active)
        and refused == 0
        and warning == 0
    )
    return {
        "active_nights": len(active),
        "design_nights": len(design),
        "design_presence_h": [min(presences), max(presences)] if presences else None,
        "nights_without_status": without_status,
        "refusals": refused,
        "status_429_5xx": warning,
        "status_429_5xx_seen": seen_warning,
        "status_3xx": sum(s["3xx"] for s in seen) if seen else None,
        "criterion_met": met,
    }


def _fmt(value, width: int = 0) -> str:
    text = "n/a" if value is None else f"{value:,}" if isinstance(value, int) else str(value)
    return text.rjust(width) if width else text


def _presence_range(span: list[float] | None) -> str:
    return "n/a" if not span else f"{span[0]:.1f}-{span[1]:.1f} h"


def render(report: dict) -> str:
    """The night table and its summary, as plain text."""
    out = [
        f"Mapillary nights {report['since']} .. {report['until']} "
        f"(rolling pool {_fmt(report['pool'])}; served = api_usage, per process)",
        "",
    ]
    header = (
        "night       served  runs/walks  sweep_tot  pool_def  hold/refusal                "
        "presence(gap)      tiles 200/429/5xx/3xx (crawls/logs)  fill                       "
        "room first->last  stranded  ended by"
    )
    out.append(header)
    for n in report["nights"]:
        r = n["nightly_run"] or {}
        hold = (
            "REFUSED"
            if r.get("refused")
            else "THROTTLED"
            if r.get("throttled")
            else f"held: {r['held']} ({r['held_launches']})"
            if r.get("held")
            else "-"
        )
        if r.get("skipped_latched"):
            hold += f", {r['skipped_latched']} skipped"
        gap = f"{r['gap_h']:.1f} h" if r.get("gap_h") is not None else "n/a"
        presence = f"{r['presence_h']:.1f} h ({gap})" if r.get("presence_h") else "n/a"
        st = n["tile_statuses"]
        tiles = (
            f"{st['200']}/{st['429']}/{st['5xx']}/{st['3xx']} ({st['crawls']}/{st['logs']})"
            if st
            else f"n/a (0/{n['mapillary_child_logs']})"
        )
        stranded = (
            f"{r['stranded_reached']}/{r['stranded_total']}"
            if r.get("stranded_total") is not None
            else "n/a"
        )
        out.append(
            f"{n['date']}  {_fmt(n['served'], 6)}  {n['grid_runs']:>4} / {n['walks']:<4}"
            f"  {_fmt(n['sweep_total'], 9)}  {_fmt(r.get('pool_deferred'), 8)}"
            f"  {hold[:26]:<26}  {presence:<17}  {tiles:<35}  {(r.get('fill') or 'n/a')[:25]:<25}"
            f"  {_fmt(n['fill_room_first']) + '->' + _fmt(n['fill_room_last']):<16}"
            f"  {stranded:>8}  {n['ended_by'] or 'no Done line'}"
            + (f"  [+{n['hand_runs']} hand run(s)]" if n["hand_runs"] else "")
        )
    pop = report["mapillary_only_due"]
    clock = report["stage1_clock"]
    out += [
        "",
        f"host_refusals (tile CDN) since {report['since']}: {len(report['host_refusals'])} row(s)",
        *(
            f"  {r['recorded_at']} {r['verdict']} {r['source']} {r['channel'] or ''} "
            f"{r['city_id'] or ''} requests_before={_fmt(r['requests_before'])}"
            for r in report["host_refusals"]
        ),
        f"Mapillary-only-due as of {pop['as_of']}: {pop['cities']} cities "
        f"({pop['grid_due']} grid-due, {pop['walk_only_due']} walk-only), priced "
        f"<= {pop['priced_requests_upper_bound']:,} requests; {pop['also_due_on_gsv']} more "
        f"are Mapillary-due AND gsv-due; {pop['walk_clock_unpaired']} cities' two Mapillary "
        f"clocks last succeeded on different dates",
        f"Stage 1 clock: {clock['design_nights']} of {clock['active_nights']} active nights at "
        f"the design point (>= {DESIGN_POOL_SHARE:.0%} of the pool; presence there "
        f"{_presence_range(clock['design_presence_h'])}, reported, not gated); "
        f"refusals {clock['refusals']}; 429/5xx {_fmt(clock['status_429_5xx'])}"
        + (
            f" (UNKNOWN: {clock['nights_without_status']} active night(s) with no status "
            f"line; {_fmt(clock['status_429_5xx_seen'])} on the rest)"
            if clock["nights_without_status"]
            else ""
        )
        + f"; 3xx {_fmt(clock['status_3xx'])}; criterion met: "
        f"{'yes' if clock['criterion_met'] else 'no'}",
        f"#375: Mapillary channels deferred for the deadline "
        f"{report['issue_375']['mapillary_deadline_deferred']}, cut by a wind-down "
        f"{report['issue_375']['mapillary_winddown_cut']}",
    ]
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--config", required=True, help="scheduler TOML (prod: scheduler.makelab1.toml)"
    )
    parser.add_argument(
        "--since", type=date.fromisoformat, help="first UTC night (default: 14 d back)"
    )
    parser.add_argument(
        "--until", type=date.fromisoformat, help="last UTC night (default: --as-of)"
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        help="date the due population is read at (default: today UTC)",
    )
    parser.add_argument("--json", metavar="OUT", help="also write the report as JSON")
    parser.add_argument("--db-path", help="override [paths].db_path")
    parser.add_argument("--log-dir", help="override [paths].log_dir")
    args = parser.parse_args(argv)

    from streetscape_metadata_tracker.scheduler import load_scheduler_config

    cfg = load_scheduler_config(args.config)
    as_of = args.as_of or datetime.now(UTC).date()
    since = (args.since or as_of - timedelta(days=14)).isoformat()
    until = (args.until or as_of).isoformat()
    db_path = args.db_path or cfg.db_path
    log_dir = args.log_dir or cfg.log_dir
    try:
        conn = open_catalog_readonly(db_path)
    except CatalogRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        report = build_report(conn, cfg, since=since, until=until, as_of=as_of, log_dir=log_dir)
    except sqlite3.Error as exc:
        print(f"refused: {db_path} could not be read ({exc})", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(render(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
