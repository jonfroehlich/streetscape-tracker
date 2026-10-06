#!/usr/bin/env python3
"""
Freeze the street networks of the next night's road-walk cities ahead of the
night (issue #341); in production, right after the previous night ends (#389).

A road walk on a FROZEN network never contacts Overpass: ``fetch_graph`` returns
the cached GraphML before it takes the Overpass host lock or probes ``/status``.
Only a city's first walk fetches one. So if the networks of tonight's walk
cities are frozen ahead of time, a mid-night Overpass refusal — the shape that
stranded 20 cities un-paired for ~83 days on 2026-09-13 and 09-15 — costs
nothing, because nothing in the night needs Overpass any more. This moves
existing fetches earlier in the day; it adds none.

What it does, in order:

1. Loads the SAME scheduler config the night will run under (``--config``), so
   the slate it predicts is the slate ``run-due`` would build: ``_collect_due``
   over the enabled channels, hoist and refresh reserve included, for the date
   the next nightly fire will use (``--date``, default the UTC date of the next
   02:00 Pacific fire, ``next_run_date``).
2. Keeps the cities inside the night's city cap that are due on at least one
   street channel and have NO frozen GraphML for that channel's configured
   ``network_type``. ``--nights N`` widens the window to N caps' worth of the
   stalest-first order — an approximation twice over: each night re-resolves
   its two reservations, and a city whose every channel is skipped (budget,
   breaker, busy lock) does not consume a cap slot, so a real night can reach
   past the first ``max_cities_per_day`` entries. The order beyond the cap is
   what those extra slots and the following night draw from, so ``--nights 2``
   covers both.
   Since 2026-09-25 prod's cap is being raised in stages toward a ceiling
   above what a night reaches, so the window grows past a night's worth and
   ``--limit`` is increasingly what bounds a pass. The plan is
   in slate order either way, so the first ``--limit`` cold networks are
   tonight's head -- but how many cities a deadline-governed night reaches is
   not yet measured, so ``--limit 40`` is an assumption to re-check.
3. Dry run by DEFAULT: prints the list and exits. With ``--execute`` it fetches
   them SERIALLY, one at a time, sleeping ``--pause-s`` between fetches, through
   the same host lock, ``/status`` pre-flight, retry policy and deadline every
   walk uses. Overpass's own slot pacing applies on top (osmnx sleeps off the
   wait the server advertises before each query).
   Before the FIRST fetch it also asks the night breaker's own fail-CLOSED
   question, ``download_common.overpass_serving`` (one tiny metered query,
   inside the Overpass host lock), and stops with exit 76 unless Overpass
   positively answers (issue #389). The walk's ``/status`` pre-flight is
   fail-OPEN by design, so on its own a ban that presents as a refused
   connection -- the 2026-08-14 shape -- would cost the first fetch its whole
   retry window before the pass stopped. Since #389 the pass starts minutes
   after a night that may have ended with Overpass latched, and the night's
   breaker lives in the night's process, so this probe is what carries that
   question across.

It stops at the first host-level refusal or busy lock and exits with that host's
code (76 blocked / 80 busy), exactly as a collection child does — a refused
Overpass is not something to keep asking. A city-specific failure (a bbox with
no drivable ways) is logged and the pass continues.

It refuses to run while a ``run-due`` is in flight on this machine unless
``--force`` — checked before EVERY fetch, not once, because a 30-city pass at
the default pause is well over an hour and a pass started too late is still
fetching when the 02:00 timer fires. The night and the pass would be two
Overpass talkers from one IP, which the host lock would serialize but which is
still the profile that earned the 2026-08-14 ban — and the walk that loses the
lock exits busy and strands its city for ~83 days (the very failure #341 is
about). Run a hand pass clear of the 02:00 fire and of the chained pass
(``systemctl --user is-active streetscape-prefreeze.service``).

The in-flight check only notices a night that has ALREADY started, after the
fetch in hand; so every ``--execute`` pass, hand or chained, also stops
launching fetches at ``fetch_cutoff()``: the last start whose worst-case fetch
(``OVERPASS_DEADLINE_S``) still ends ``NEXT_FIRE_CLEARANCE`` (1 h) before the
next 02:00 Pacific fire -- 00:45 Pacific. That is what bounds a pass chained
from a night that started late (a Persistent catch-up after a reboot, or a hand
``systemctl start``), which can otherwise end close enough to 02:00 that the
pass would still hold the Overpass lock then. Reaching the cutoff is a quiet
exit 0, not an alert: what it leaves cold the night fetches itself.

In production it is chained from ``streetscape-tracker.service``'s
``OnSuccess=``/``OnFailure=`` (issues #355, #389), so it starts when the night's
tail ends, at whatever hour that is, with ``--alert`` so a pass that does not finish -- a host
condition, a run-due in flight, a crash, a SIGTERM from the unit's timeout --
emails the ``[alerts]`` recipient instead of silently leaving tonight's networks
cold. A pass with nothing cold exits 0 and sends nothing, so the steady state is
a no-op rather than a daily mail. The unit file carries the pacing rationale.

``--all-enabled`` (issue #381) is the by-hand BACKLOG drain the slate mode cannot
be: the slate only ever sees cities due by the target date, so the long tail of
cold networks is otherwise frozen the night it is walked -- exactly when a
refusal strands it. It plans every ENABLED city that is a member of at least one
enabled street channel and has no frozen GraphML for that channel's
``network_type``, stalest-first (the order the scheduler would eventually walk
them) with ``city_id`` as the tiebreaker, so repeated passes make monotone
progress. Frozen networks are immutable (#103), so the backlog is a one-time
cost. Everything else -- serial and paced, the host lock and probe, the
in-flight refusal, the stop on a host condition -- is the same code path.
``--limit`` defaults to 20 in this mode, and ``--nights`` and ``--date`` are
refused beside it (neither means anything to a pass that ignores dueness). The
chained pass stays on the slate mode and runs after every night, so a drain's
fetches always add to its count.

Nothing is published and no imagery request is made. The catalog gains a
``street_networks`` row per frozen network, as a walk's own fetch would add.

Usage:
    # See what tonight's walks would fetch (default is a dry run):
    python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml

    # Freeze them, two minutes apart:
    python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --execute

    # Look two nights ahead, cap the pass at 30 fetches:
    python scripts/prefreeze_street_networks.py --config ... --nights 2 --limit 30 --execute

    # Drain the whole catalog's cold backlog, 20 networks per pass (#381):
    python scripts/prefreeze_street_networks.py --config ... --all-enabled --execute
"""

import argparse
import logging
import os
import signal
import socket
import sys
import time
import traceback
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import clock, db  # noqa: E402
from streetscape_metadata_tracker.alerting import send_alert  # noqa: E402
from streetscape_metadata_tracker.download_common import (  # noqa: E402
    HOST_BY_BUSY_EXIT_CODE,
    HOST_BY_EXIT_CODE,
    HOST_OVERPASS,
    DownloadError,
    HostBlockedError,
    HostUnavailableError,
    host_exit_code,
    overpass_serving,
)
from streetscape_metadata_tracker.host_lock import host_lock  # noqa: E402
from streetscape_metadata_tracker.naming import network_cache_path  # noqa: E402
from streetscape_metadata_tracker.scheduler import (  # noqa: E402
    CHANNEL_DEFAULT_MEMBERSHIP,
    DEFAULT_CONFIG_PATH,
    USAGE_EXIT_CODE,
    _collect_due,
    _opt_in_reservation,
    _run_due_in_flight,
    is_street_channel,
    load_scheduler_config,
)
from streetscape_street_analyzer.download_street_network import (  # noqa: E402
    OVERPASS_DEADLINE_S,
    fetch_graph,
)

logger = logging.getLogger("prefreeze_street_networks")

# Gap between two fetches. Overpass's usage policy is a daily count, and osmnx
# already sleeps off whatever slot wait the server advertises per query, so
# this is not a rate limiter -- it is what keeps a pass from presenting as a
# burst if the server-side wait happens to be zero. Two minutes puts a 27-city
# pass (the busiest real night measured in #341) at about an hour.
DEFAULT_PAUSE_S = 120

# --all-enabled's --limit when none is given (issue #381). Overpass's guidance
# for an app that queries regularly is under ~100 queries a day
# (docs/provider-access.md), and the nightly walks plus the chained post-batch
# pass (up to 40) already spend part of that -- and that pass runs after every
# night, so a drain ALWAYS adds to its count. Measured on prod 2026-09-21 the
# backlog was 242 cold networks, so this drains it over ~12 passes, not one burst.
DEFAULT_ALL_ENABLED_LIMIT = 20

# The nightly timer's OnCalendar, restated: `*-*-* 02:00:00 America/Los_Angeles`
# in deploy/systemd/streetscape-tracker.timer. tests/test_prefreeze_unit.py pins
# the two together; a timer moved without these is a pass predicting the wrong
# night. Hour/minute ints rather than datetime.time: this module imports `time`.
NIGHTLY_FIRE_HOUR = 2
NIGHTLY_FIRE_MINUTE = 0
NIGHTLY_FIRE_ZONE = "America/Los_Angeles"

# How far clear of the next nightly fire the pass's LAST fetch must end (issue
# #389 review, F2). The chain starts the pass whenever a night ends, and a night
# that did not start at 02:00 -- a Persistent catch-up after a reboot, the #369
# watchdog's re-arm, a hand `systemctl start` -- can end late enough that a
# 6 h pass would still hold the Overpass lock at the next 02:00, where the
# night's first cold walks would exit busy (80) and strand. So no fetch is
# STARTED unless its worst case (OVERPASS_DEADLINE_S) ends this far before the
# fire; with the defaults the last launch is at 00:45 Pacific. The hour is the
# same clearance the unit's TimeoutStartSec is sized to leave on a 02:00 night.
NEXT_FIRE_CLEARANCE = timedelta(hours=1)


def next_nightly_fire(now: datetime | None = None) -> datetime:
    """
    The aware UTC instant of the next 02:00 Pacific nightly fire after ``now``.

    The nominal instant: the timer's 15-minute randomized delay only ever makes
    the real start later, so this is the earliest a night can begin. Computed in
    the fire's own zone, so a pass across a DST change gets the right offset.
    Same-zone datetime arithmetic is wall-clock arithmetic, which is what
    "02:00 tomorrow" means; the UTC conversion then resolves the offset.

    Example: at 2026-09-01 00:30 UTC (17:30 PDT on 08-31) this returns
    2026-09-01 09:00 UTC (02:00 PDT).
    """
    now = now if now is not None else clock.utc_now()
    local = now.astimezone(ZoneInfo(NIGHTLY_FIRE_ZONE))
    fire = local.replace(
        hour=NIGHTLY_FIRE_HOUR, minute=NIGHTLY_FIRE_MINUTE, second=0, microsecond=0
    )
    if fire <= local:
        fire += timedelta(days=1)
    return fire.astimezone(UTC)


def fetch_cutoff(now: datetime | None = None) -> datetime:
    """
    The last instant a fetch may START and still end clear of the next night.

    ``next_nightly_fire(now) - NEXT_FIRE_CLEARANCE - OVERPASS_DEADLINE_S``:
    a fetch launched at the cutoff ends, at its 900 s deadline, an hour before
    the next 02:00 (issue #389 review, F2). Computed ONCE per pass, from the
    pass's start: re-reading it per fetch would roll it to TOMORROW's fire
    the moment 02:00 passed, which is exactly when it must hold.

    Example: a pass starting at 2026-09-01 07:30 UTC (00:30 PDT) gets a cutoff
    of 07:45 UTC (00:45 PDT); one starting at 08:00 UTC (01:00 PDT) is already
    past its cutoff and fetches nothing.
    """
    return next_nightly_fire(now) - NEXT_FIRE_CLEARANCE - timedelta(seconds=OVERPASS_DEADLINE_S)


def next_run_date(now: datetime | None = None) -> date:
    """
    The catalog date the next nightly ``run-due`` will compute dueness for.

    ``cmd_run_due`` reads ``clock.snapshot_date_today()`` -- the UTC date -- when
    the 02:00 Pacific timer fires, so the right date is the UTC date of the NEXT
    02:00 Pacific instant after ``now``, not "tomorrow UTC": a pass that starts
    after midnight UTC (16:00 PST / 17:00 PDT) is on the fire's own UTC day
    already. The old formula was a day late there, and the error was not safe
    for the opt-in reservation's channel rotation (#348), whose start is the
    date's ordinal (issue #389). The 15-minute randomized delay never crosses a
    UTC date (09:00-10:15 UTC).

    Example: at 2026-09-01 00:30 UTC (17:30 PDT on 08-31) the next fire is
    2026-09-01 02:00 PDT = 09:00 UTC, so this returns ``date(2026, 9, 1)``.
    """
    return next_nightly_fire(now).date()


def _require_overpass_serving() -> None:
    """
    Raise ``HostBlockedError`` unless Overpass POSITIVELY serves this host now.

    The pass's one fail-CLOSED gate (issue #389), asked once, before the first
    fetch. ``download_common.overpass_serving`` is the night breaker's own reset
    test: True only for an interpreter 200 that echoes this call's nonce, so a
    refused connection, a timeout, a 429/403/406/5xx or an odd body all stop the
    pass. That is the opposite of the walk's ``/status`` pre-flight, which is
    fail-open by design -- and the 2026-08-14 ban presented as a refused TCP
    connection, which that pre-flight lets through into a whole retry window.

    Why it is needed at all: since #389 the pass is chained to the END of the
    night, so it starts minutes after a night that may have ended with Overpass
    latched, instead of hours later. The breaker is in-memory in the night's
    process and is gone by then; this re-asks its question, once a pass.

    Inside the Overpass host lock, so the probe is never a second concurrent
    talker beside a local walk (a held lock raises ``HostBusyError``, exit 80).
    """
    with host_lock(HOST_OVERPASS):
        if not overpass_serving():
            raise HostBlockedError(
                "Overpass did not positively answer the fail-closed probe "
                "(download_common.overpass_serving: refused, timed out, throttled or an "
                "unexpected body); stopping before the first fetch rather than paying its "
                "retry window against a host that may be refusing this IP (issue #389).",
                host=HOST_OVERPASS,
            )


def plan_prefreeze(
    conn, cfg, today: date, *, nights: int
) -> list[tuple[db.CityRow, str, list[str]]]:
    """
    (city, network_type, channels) for every cold network the next ``nights``
    nights' walks would fetch, in the order the night would reach them.

    Reuses ``_collect_due`` rather than ``get_due_cities`` so the prediction
    carries the night's real ordering — the bounded opt-in hoist (#248/#282)
    and the refresh reserve (#308) — instead of the raw stalest-first list that
    both reorder. Read-only: nothing here writes the catalog.
    """
    providers = cfg.enabled_providers()
    street = [p for p in providers if is_street_channel(p)]
    if not street:
        return []
    window = cfg.max_cities_per_day * max(1, nights)
    slate = _collect_due(
        conn,
        cfg,
        today,
        providers,
        max_opt_in=_opt_in_reservation(cfg, window),
        max_cities=window,
    )
    planned: list[tuple[db.CityRow, str, list[str]]] = []
    seen: set[tuple[str, str]] = set()
    for city in slate.cities[:window]:
        for channel in slate.providers_for_city[city.city_id]:
            if not is_street_channel(channel):
                continue
            network_type = cfg.providers[channel].network_type
            key = (city.city_id, network_type)
            if key in seen:
                # Two channels walking the same network type share one GraphML.
                for entry in planned:
                    if (entry[0].city_id, entry[1]) == key:
                        entry[2].append(channel)
                continue
            if os.path.exists(network_cache_path(city.city_id, cfg.data_dir, network_type)):
                continue
            seen.add(key)
            planned.append((city, network_type, [channel]))
    return planned


def plan_prefreeze_all_enabled(conn, cfg) -> list[tuple[db.CityRow, str, list[str]]]:
    """
    (city, network_type, channels) for EVERY enabled city's cold walk network,
    stalest-first (issue #381).

    A (city, network_type) is planned when the city is a member of at least
    one enabled street channel walking that type -- a network no channel will
    ever walk is an Overpass request that buys nothing -- and its GraphML is
    not on disk. The channels' types come from config, never a literal.

    Membership and ``last_success_at`` are read through
    ``get_due_cities_with_last_success`` with its staleness gate opened
    (threshold 0 against the far-future date), rather than through a second
    copy of its membership clause: that clause is the part that fails open when
    a copy is forgotten. Its quarantine gate is KEPT at
    ``cfg.max_consecutive_failures``: a quarantined channel never walks the
    city until an operator intervenes, so freezing for it buys nothing -- and a
    city whose fetch always fails (a bbox with no drivable ways writes no
    GraphML) ends up quarantined, so without the gate it was re-planned at the
    head of every pass forever (PR #382 review).

    Order: CLEAN networks first -- one where some member channel walking it has
    ``consecutive_failures > 0`` goes behind every clean one, so a city that
    keeps failing, but is not yet quarantined, cannot hold the head of the
    queue while ``--limit 1`` passes re-ask it. Within each tier, staleness is
    the OLDEST ``last_success_at`` among the member channels walking it, and
    any never-walked channel makes it never-walked (NULLS FIRST, as the
    scheduler orders); then ``city_id``. Read-only.
    """
    street = [p for p in cfg.enabled_providers() if is_street_channel(p)]
    failing = {
        (row["city_id"], row["provider"])
        for row in conn.execute(
            "SELECT city_id, provider FROM schedule_state WHERE consecutive_failures > 0"
        )
    }
    entries: dict[tuple[str, str], tuple[db.CityRow, list[str], list[str | None], list[bool]]] = {}
    for channel in street:
        network_type = cfg.providers[channel].network_type
        members = db.get_due_cities_with_last_success(
            conn,
            today=date.max,
            cycle_days=0,
            grace_days=0,
            max_consecutive_failures=cfg.max_consecutive_failures,
            default_membership=CHANNEL_DEFAULT_MEMBERSHIP[channel],
            provider=channel,
        )
        for city, last_success_at in members:
            key = (city.city_id, network_type)
            if key not in entries:
                entries[key] = (city, [], [], [])
            entries[key][1].append(channel)
            entries[key][2].append(last_success_at)
            entries[key][3].append((city.city_id, channel) in failing)

    def staleness(item):
        (city_id, network_type), (_, _, successes, failures) = item
        oldest = None if None in successes else min(successes)
        # Clean first, then NULLS FIRST, then oldest, then city_id so reruns are monotone.
        return (any(failures), oldest is not None, oldest or "", city_id, network_type)

    return [
        (city, network_type, channels)
        for (city_id, network_type), (city, channels, _, _) in sorted(
            entries.items(), key=staleness
        )
        if not os.path.exists(network_cache_path(city_id, cfg.data_dir, network_type))
    ]


def run_prefreeze(
    conn,
    cfg,
    planned,
    *,
    pause_s: float,
    force: bool = False,
    cutoff: datetime | None = None,
) -> tuple[int, int, int | None]:
    """
    Fetch each planned network in order, serially, ``pause_s`` apart.

    Returns ``(frozen, failed_cities, stop_code)``: ``stop_code`` is the exit
    code of what stopped the pass early -- a host condition's own code (76
    blocked / 80 busy), or ``USAGE_EXIT_CODE`` when a ``run-due`` appeared on
    this machine and ``force`` is False -- or None if it ran to the end. A
    city-specific ``DownloadError`` counts in ``failed_cities`` and does not
    stop the pass.

    The in-flight check runs before EVERY fetch (after the pause, so it sees
    the world the fetch will run in), not once up front: the pass is long and
    nothing ends it when a night starts.

    Before the FIRST fetch, after that check, Overpass must also pass the
    fail-closed probe (``_require_overpass_serving``); a negative answer stops
    the pass like any host refusal, with exit 76 and nothing fetched.

    ``cutoff`` (``fetch_cutoff()``, from ``main``) is the last instant a fetch
    may start: past it the pass stops QUIETLY -- ``stop_code`` None, no probe,
    no alert -- leaving the rest cold for the night's own walks to fetch, which
    is the pre-#341 behaviour rather than a failure (issue #389 review, F2).
    The caller tells a cutoff stop from a finished pass by
    ``frozen + failed < len(planned)``. None means no cutoff.
    """
    frozen = failed = 0
    for index, (city, network_type, channels) in enumerate(planned):
        if index:
            time.sleep(pause_s)
        if cutoff is not None and clock.utc_now() >= cutoff:
            logger.warning(
                "Past this pass's cutoff (%s UTC: the last start whose fetch ends %s before "
                "the next nightly fire); stopping with %d of %d frozen and the rest left for "
                "the night's own walks. Not a failure: the night started at an odd hour "
                "(a catch-up after a reboot, or a hand start), and a fetch now could still "
                "hold the Overpass lock at the next 02:00.",
                cutoff.strftime("%Y-%m-%d %H:%M"),
                NEXT_FIRE_CLEARANCE,
                frozen,
                len(planned),
            )
            break
        in_flight = _run_due_in_flight()
        if in_flight and not force:
            logger.error(
                "A run-due is in flight on this machine (%s); stopping the pass rather than "
                "being a second Overpass talker beside it (%d of %d frozen). Wait for the "
                "night to finish, or pass --force.",
                in_flight,
                frozen,
                len(planned),
            )
            return frozen, failed, USAGE_EXIT_CODE
        logger.info(
            "Freezing %s %s network (%d/%d) for %s",
            city.city_id,
            network_type,
            index + 1,
            len(planned),
            ", ".join(channels),
        )
        try:
            if index == 0:
                _require_overpass_serving()
            fetch_graph(
                city,
                cfg.data_dir,
                network_type=network_type,
                conn=conn,
                # The same [overpass] window a nightly walk uses (issue #357):
                # this pass is the same talker to the same host, earlier.
                overpass_retry=cfg.overpass_retry,
            )
        except HostUnavailableError as e:
            # Blocked or busy: the same exit code a collection child would
            # give, and the same reason to stop -- asking again cannot answer
            # differently, and asking a refusing host again is the retry hazard.
            logger.error("Stopping the pass: %s", e)
            return frozen, failed, host_exit_code(e)
        except DownloadError as e:
            # A bbox with no drivable ways is about this city, not the host.
            failed += 1
            logger.error("%s: no usable network, continuing: %s", city.city_id, e)
            continue
        frozen += 1
    return frozen, failed, None


# The exit status a SIGTERM'd process conventionally reports (128 + 15). Only
# returned under --alert, the one mode that catches the signal.
TERMINATED_EXIT_CODE = 128 + signal.SIGTERM


class Terminated(BaseException):
    """
    SIGTERM, raised in the main thread so ``--alert`` can report it (issue #355).

    Under the timer, SIGTERM is what ``TimeoutStartSec`` sends, and a pass that
    dies silently leaves the night's cold networks cold with nobody told -- the
    exposure the timer exists to remove. A BaseException, like
    KeyboardInterrupt, so a broad ``except Exception`` inside osmnx, requests or
    tenacity cannot swallow it and carry on fetching. Interrupting a fetch is
    safe: the GraphML is written to a ``.tmp`` and renamed in (#341), and the
    host lock is an flock the kernel releases.
    """


def _raise_terminated(signum, frame):
    raise Terminated(f"signal {signum}")


def _describe_stop(stop_code: int) -> str:
    """The alert subject's reason for a pass that stopped early."""
    if stop_code == USAGE_EXIT_CODE:
        return "STOPPED: a run-due is in flight"
    if stop_code in HOST_BY_EXIT_CODE:
        return f"STOPPED: {HOST_BY_EXIT_CODE[stop_code]} REFUSED this host (exit {stop_code})"
    if stop_code in HOST_BY_BUSY_EXIT_CODE:
        return (
            f"STOPPED: another local process holds the {HOST_BY_BUSY_EXIT_CODE[stop_code]} "
            f"lock (exit {stop_code})"
        )
    return f"STOPPED (exit {stop_code})"


def _still_cold(cfg, planned) -> list[str]:
    """One line per planned network that is still not frozen on disk."""
    return [
        f"  {city.city_id} {network_type} ({', '.join(channels)})"
        for city, network_type, channels in planned
        if not os.path.exists(network_cache_path(city.city_id, cfg.data_dir, network_type))
    ]


def _alert(
    cfg, what: str, report: list[str], extra: list[str], *, mode: str = "--nights 2"
) -> None:
    """Email ``what`` plus the pass's own output through ``[alerts]``. Never raises."""
    body = [
        "The post-batch street-network prefreeze pass (issues #341, #355, #389) did not finish.",
        "Every network it left cold is one Overpass refusal tonight away from stranding",
        "its city's walk for ~83 days. Re-run it by hand once the cause is cleared:",
        f"  scripts/prefreeze_street_networks.py --config <prod.toml> {mode} --execute",
        "",
        *report,
        *extra,
    ]
    send_alert(
        cfg.alerts, f"street-network prefreeze {what} on {socket.gethostname()}", "\n".join(body)
    )


def _mode(args) -> str:
    """The planning flag a hand re-run should repeat, for the alert body."""
    return "--all-enabled" if args.all_enabled else "--nights 2"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Scheduler TOML (production: config/scheduler.makelab1.toml)",
    )
    p.add_argument(
        "--date",
        help="Catalog date to compute dueness for (YYYY-MM-DD; default: the UTC date the next 02:00 Pacific nightly fire reads)",
    )
    p.add_argument(
        "--nights",
        type=int,
        help="How many nights' worth of the city cap to look ahead (default 1)",
    )
    p.add_argument(
        "--all-enabled",
        action="store_true",
        help=(
            "Plan every enabled city's cold walk network, stalest-first, instead of "
            "the next nights' slate: a by-hand backlog drain (issue #381). Refused "
            "with --nights or --date"
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        help=(
            "Freeze at most N networks this pass "
            f"(default: no limit, or {DEFAULT_ALL_ENABLED_LIMIT} with --all-enabled)"
        ),
    )
    p.add_argument(
        "--pause-s",
        type=float,
        default=DEFAULT_PAUSE_S,
        help=f"Seconds to wait between fetches (default {DEFAULT_PAUSE_S})",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually fetch (default is a dry run that only lists)",
    )
    p.add_argument(
        "--force", action="store_true", help="Run even if a run-due is in flight on this machine"
    )
    p.add_argument(
        "--alert",
        action="store_true",
        help=(
            "Email the [alerts] recipient when a pass does not finish: a host "
            "condition, a run-due in flight, a crash, or a SIGTERM (issue #355). "
            "Silent when it finishes, including when nothing was cold. The exit "
            "status is unchanged, so a systemd unit still goes red."
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.all_enabled and args.nights is not None:
        logger.error("--all-enabled and --nights are mutually exclusive")
        return USAGE_EXIT_CODE
    if args.all_enabled and args.date is not None:
        # The plan is staleness-ordered over the whole catalog; no date enters it,
        # so accepting one would promise a filter that is not applied.
        logger.error("--all-enabled and --date are mutually exclusive")
        return USAGE_EXIT_CODE
    if args.nights is None:
        args.nights = 1
    if args.all_enabled and args.limit is None:
        args.limit = DEFAULT_ALL_ENABLED_LIMIT
    if args.nights < 1:
        logger.error("--nights must be at least 1 (got %d)", args.nights)
        return USAGE_EXIT_CODE
    if args.limit is not None and args.limit < 1:
        logger.error("--limit must be at least 1 (got %d)", args.limit)
        return USAGE_EXIT_CODE
    if args.pause_s < 0:
        logger.error("--pause-s must not be negative (got %g)", args.pause_s)
        return USAGE_EXIT_CODE

    cfg = load_scheduler_config(args.config)
    today = date.fromisoformat(args.date) if args.date else next_run_date()
    # From the pass's START, once: see fetch_cutoff. Independent of --date, which
    # picks the slate; the cutoff is about the real clock and the real next fire.
    cutoff = fetch_cutoff()

    # Everything the pass prints, so an alert carries the pass's own account
    # rather than a pointer to a log somebody has to go and find.
    report: list[str] = []
    planned: list = []
    previous_handler = None
    try:
        if args.alert:
            previous_handler = signal.signal(signal.SIGTERM, _raise_terminated)
        return _run_pass(args, cfg, today, report, planned, cutoff)
    except Terminated:
        # Only reachable under --alert: that is the only mode that installs the handler.
        logger.error("Terminated by SIGTERM mid-pass (TimeoutStartSec, or a stop).")
        still = _still_cold(cfg, planned)
        _alert(
            cfg,
            "KILLED by SIGTERM",
            report,
            ["", f"Killed with {len(still)} planned network(s) still cold:", *still],
            mode=_mode(args),
        )
        return TERMINATED_EXIT_CODE
    except Exception:
        if args.alert:
            # The still-cold list belongs here as much as on a host stop: what
            # the reader has to decide is whether tonight is exposed, and a
            # traceback alone does not answer that.
            still = _still_cold(cfg, planned)
            _alert(
                cfg,
                "CRASHED",
                report,
                [
                    "",
                    f"{len(still)} planned network(s) still cold:",
                    *still,
                    "",
                    traceback.format_exc(),
                ],
                mode=_mode(args),
            )
        raise
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


def _run_pass(args, cfg, today: date, report: list[str], planned: list, cutoff: datetime) -> int:
    """The pass itself; fills ``report`` and ``planned`` in for ``main``'s alerts."""

    def say(line: str) -> None:
        print(line)
        report.append(line)

    conn = db.connect(cfg.db_path)
    try:
        if args.all_enabled:
            full = plan_prefreeze_all_enabled(conn, cfg)
        else:
            full = plan_prefreeze(conn, cfg, today, nights=args.nights)
        planned.extend(full[: args.limit] if args.limit is not None else full)

        street = [p for p in cfg.enabled_providers() if is_street_channel(p)]
        if not street:
            say("No street channel is enabled in this config; nothing to freeze.")
            return 0
        scope = (
            "across every enabled city, stalest-first"
            if args.all_enabled
            else f"for the walk slate of {today}"
        )
        window = (
            f"{len(full)} cold in all"
            if args.all_enabled
            else f"{args.nights} night(s) of the {cfg.max_cities_per_day}-city cap"
        )
        say(
            f"{'Would freeze' if not args.execute else 'Freezing'} {len(planned)} cold street "
            f"network(s) {scope} (channels {', '.join(street)}, {window}):"
        )
        for city, network_type, channels in planned:
            say(f"  {city.city_id:60s} {network_type:12s} {', '.join(channels)}")
        if len(full) > len(planned):
            say(f"{len(full) - len(planned)} more cold network(s) are past --limit {args.limit}.")
        if not planned:
            # The steady state once the backlog is frozen: a no-op, never an alert.
            say(
                "Every enabled city's walk network is already frozen."
                if args.all_enabled
                else "Every walk in that window already has a frozen network."
            )
            return 0
        if not args.execute:
            say("DRY RUN — nothing fetched. Re-run with --execute to freeze them.")
            return 0

        frozen, failed, stop_code = run_prefreeze(
            conn, cfg, planned, pause_s=args.pause_s, force=args.force, cutoff=cutoff
        )
        # run_prefreeze stops early with no stop_code ONLY at the cutoff.
        cut_short = stop_code is None and frozen + failed < len(planned)
        if stop_code is None and not cut_short and frozen == 0 and failed:
            # Not a failed pass by this script's conventions (a city-specific
            # failure never is, and the night would fail those cities the same
            # way), so the exit stays 0 -- but "Froze 0" must not read as a clean
            # pass: every fetch spent an Overpass query and bought nothing.
            say(
                f"WARNING: every planned fetch failed ({failed} of {len(planned)}); NOTHING "
                "was frozen. Those cities have no usable network for Overpass to return, "
                "and each attempt still spent a query against the daily budget."
            )
        say(
            f"Froze {frozen} of {len(planned)} network(s)"
            + (f"; {failed} city(ies) had no usable network" if failed else "")
            + (
                "; stopped early: a run-due started on this machine"
                if stop_code == USAGE_EXIT_CODE
                else "; stopped early on a host condition"
                if stop_code is not None
                else (
                    f"; stopped at the cutoff ({cutoff:%Y-%m-%d %H:%M} UTC) so as not to "
                    "overlap the next night -- the rest are left for its own walks"
                )
                if cut_short
                else ""
            )
        )
        if stop_code is not None and args.alert:
            still = _still_cold(cfg, planned)
            _alert(
                cfg,
                _describe_stop(stop_code),
                report,
                ["", f"{len(still)} planned network(s) still cold:", *still],
                mode=_mode(args),
            )
        return stop_code if stop_code is not None else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
