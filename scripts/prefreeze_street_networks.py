#!/usr/bin/env python3
"""
Freeze the street networks of the next night's road-walk cities during the day
(issue #341).

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
   the next timer fire will use (``--date``, default tomorrow UTC).
2. Keeps the cities inside the night's city cap that are due on at least one
   street channel and have NO frozen GraphML for that channel's configured
   ``network_type``. ``--nights N`` widens the window to N caps' worth of the
   stalest-first order — an approximation twice over: each night re-resolves
   its two reservations, and a city whose every channel is skipped (budget,
   breaker, busy lock) does not consume a cap slot, so a real night can reach
   past the first ``max_cities_per_day`` entries. The order beyond the cap is
   what those extra slots and the following night draw from, so ``--nights 2``
   covers both.
3. Dry run by DEFAULT: prints the list and exits. With ``--execute`` it fetches
   them SERIALLY, one at a time, sleeping ``--pause-s`` between fetches, through
   the same host lock, ``/status`` pre-flight, retry policy and deadline every
   walk uses. Overpass's own slot pacing applies on top (osmnx sleeps off the
   wait the server advertises before each query).

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
about). Run it in the daytime, well clear of the timer.

Nothing is published and no imagery request is made. The catalog gains a
``street_networks`` row per frozen network, as a walk's own fetch would add.

Usage:
    # See what tonight's walks would fetch (default is a dry run):
    python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml

    # Freeze them, two minutes apart:
    python scripts/prefreeze_street_networks.py --config config/scheduler.makelab1.toml --execute

    # Look two nights ahead, cap the pass at 30 fetches:
    python scripts/prefreeze_street_networks.py --config ... --nights 2 --limit 30 --execute
"""

import argparse
import logging
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.download_common import (  # noqa: E402
    DownloadError,
    HostUnavailableError,
    host_exit_code,
)
from streetscape_metadata_tracker.naming import network_cache_path  # noqa: E402
from streetscape_metadata_tracker.scheduler import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    USAGE_EXIT_CODE,
    _collect_due,
    _opt_in_reservation,
    _run_due_in_flight,
    is_street_channel,
    load_scheduler_config,
)
from streetscape_street_analyzer.download_street_network import fetch_graph  # noqa: E402

logger = logging.getLogger("prefreeze_street_networks")

# Gap between two fetches. Overpass's usage policy is a daily count, and osmnx
# already sleeps off whatever slot wait the server advertises per query, so
# this is not a rate limiter -- it is what keeps a pass from presenting as a
# burst if the server-side wait happens to be zero. Two minutes puts a 27-city
# pass (the busiest real night measured in #341) at about an hour.
DEFAULT_PAUSE_S = 120


def next_run_date() -> date:
    """
    The catalog date the next nightly ``run-due`` will compute dueness for.

    ``cmd_run_due`` reads ``datetime.now(UTC).date()`` at 02:00 Pacific, which
    is 09:00 or 10:00 UTC the NEXT UTC day for a pass run during a Pacific
    afternoon. Tomorrow UTC is right for that case and one day late for a
    pass run after midnight UTC, and the error is on the safe side: dueness is
    monotone in the date, so a later date can only ADD cities at the staleness
    threshold, never drop one the night will actually reach.
    """
    return datetime.now(UTC).date() + timedelta(days=1)


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


def run_prefreeze(
    conn, cfg, planned, *, pause_s: float, force: bool = False
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
    the timer does not wait for it.
    """
    frozen = failed = 0
    for index, (city, network_type, channels) in enumerate(planned):
        if index:
            time.sleep(pause_s)
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
            fetch_graph(city, cfg.data_dir, network_type=network_type, conn=conn)
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
        help="Catalog date to compute dueness for (YYYY-MM-DD; default: tomorrow UTC, which is what the next 02:00 Pacific timer fire reads)",
    )
    p.add_argument(
        "--nights",
        type=int,
        default=1,
        help="How many nights' worth of the city cap to look ahead (default 1)",
    )
    p.add_argument("--limit", type=int, help="Freeze at most N networks this pass")
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
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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

    conn = db.connect(cfg.db_path)
    try:
        planned = plan_prefreeze(conn, cfg, today, nights=args.nights)
        if args.limit is not None:
            planned = planned[: args.limit]

        street = [p for p in cfg.enabled_providers() if is_street_channel(p)]
        if not street:
            print("No street channel is enabled in this config; nothing to freeze.")
            return 0
        print(
            f"{'Would freeze' if not args.execute else 'Freezing'} {len(planned)} cold street "
            f"network(s) for the walk slate of {today} (channels {', '.join(street)}, "
            f"{args.nights} night(s) of the {cfg.max_cities_per_day}-city cap):"
        )
        for city, network_type, channels in planned:
            print(f"  {city.city_id:60s} {network_type:12s} {', '.join(channels)}")
        if not planned:
            print("Every walk in that window already has a frozen network.")
            return 0
        if not args.execute:
            print("DRY RUN — nothing fetched. Re-run with --execute to freeze them.")
            return 0

        frozen, failed, stop_code = run_prefreeze(
            conn, cfg, planned, pause_s=args.pause_s, force=args.force
        )
        print(
            f"Froze {frozen} of {len(planned)} network(s)"
            + (f"; {failed} city(ies) had no usable network" if failed else "")
            + (
                "; stopped early: a run-due started on this machine"
                if stop_code == USAGE_EXIT_CODE
                else "; stopped early on a host condition"
                if stop_code is not None
                else ""
            )
        )
        return stop_code if stop_code is not None else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
