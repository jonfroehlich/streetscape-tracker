#!/usr/bin/env python3
"""
Crawl the GSV capture-date history for one work list (issues #2, #442).

Catalog-free on purpose: this runs on a dedicated host that holds nothing but
work lists written by scripts/build_gsv_history_worklist.py, and queries an
unpublished, IP-identified endpoint at a paced rate. Its outputs (raw bodies,
a queries ledger, per-sample history) are copied back and landed in the
catalog by a separate import step; the per-city `_gsv_history_` artifact and
its #109 summary are derived from them there.

Stopping rule (docs/capture-dates.md): ONE request in flight, 60/min mean with
jitter 0.6; the FIRST 429/403, the first HTML page served with a 200, 8
consecutive failed REQUESTS (transport failures, retries included, and 200s
whose body is not an answer), or failed requests above a quarter of the last
50 (--breaker-window, --breaker-share), ends the run and writes blocked.json
under --latch-dir -- by default the PARENT of --out-dir, so every work list
this host crawls under one results directory shares ONE latch, because a
refusal is per address, not per work list -- after which a restart is refused
until the cool-down (24 h) has passed. ONE crawl per host: crawl.lock beside
the latch is held for the whole run, and a second harvester exits 88.
--max-requests ends a run cleanly with exit 83 ("resume"), which is how a
timer runs daily tranches.

Exit codes: 0 pass complete; 83 incomplete, re-run to resume (cap reached, or
rows still failing); 86 refused by the endpoint, or a latch in force; 88
another harvester holds this host's crawl lock; 2 bad arguments, including a
work-list file that is missing or not a work list, or an --out-dir whose
files were written under another column set (appending would shift every
row: use a fresh out-dir, or rebuild them with reparse_gsv_history_raw.py).

Usage:
    python scripts/harvest_gsv_history.py --worklist W.worklist.csv.gz --pass 1 --out-dir results/
    python scripts/harvest_gsv_history.py --worklist W.worklist.csv.gz --pass 1 --out-dir results/ --max-requests 3000
    python scripts/harvest_gsv_history.py --worklist W.worklist.csv.gz --pass 1 --out-dir results/ --dry-run
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker.download_common import (  # noqa: E402
    SWEEP_INCOMPLETE_EXIT_CODE,
    jitter_fraction,
    positive_int,
)
from streetscape_metadata_tracker.download_gsv_history import (  # noqa: E402
    DEFAULT_BREAKER_LIMIT,
    DEFAULT_BREAKER_SHARE,
    DEFAULT_BREAKER_WINDOW,
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_JITTER,
    DEFAULT_RATE_PER_MINUTE,
    HISTORY_BLOCKED_EXIT_CODE,
    HISTORY_BUSY_EXIT_CODE,
    HarvestBlockedError,
    HarvestBusyError,
    HarvestIncompleteError,
    OutputShapeError,
    check_output_shapes,
    clear_latch,
    default_latch_dir,
    harvest_worklist_async,
    latch_active,
    load_worklist,
    output_paths,
    read_done_keys,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--worklist", required=True, help="a *.worklist.csv.gz from build_gsv_history_worklist.py"
    )
    p.add_argument("--pass", dest="pass_no", type=int, choices=(1, 2), required=True)
    p.add_argument(
        "--out-dir", required=True, help="where the three output files and the latch live"
    )
    p.add_argument("--rate-per-minute", type=float, default=DEFAULT_RATE_PER_MINUTE)
    p.add_argument("--jitter", type=jitter_fraction, default=DEFAULT_JITTER)
    p.add_argument(
        "--max-requests",
        type=positive_int,
        default=None,
        help="stop cleanly after N requests (exit 83)",
    )
    p.add_argument(
        "--breaker-limit",
        type=positive_int,
        default=DEFAULT_BREAKER_LIMIT,
        help="consecutive failed requests that end the run and write the latch",
    )
    p.add_argument(
        "--breaker-window",
        type=positive_int,
        default=DEFAULT_BREAKER_WINDOW,
        help="the last N requests the failure-share rule reads",
    )
    p.add_argument(
        "--breaker-share",
        type=jitter_fraction,
        default=DEFAULT_BREAKER_SHARE,
        help="failed share of the window above which the run latches",
    )
    p.add_argument(
        "--latch-dir",
        default=None,
        help="where blocked.json lives (default: the parent of --out-dir, one latch per host)",
    )
    p.add_argument("--cooldown-hours", type=float, default=DEFAULT_COOLDOWN_HOURS)
    p.add_argument("--clear-latch", action="store_true", help="remove a latch before starting")
    p.add_argument("--dry-run", action="store_true", help="report what would be queried and exit")
    p.add_argument("--verbose", action="store_true")
    return p


def worklist_stem(path: str) -> str:
    return os.path.basename(path).removesuffix(".worklist.csv.gz").removesuffix(".csv.gz")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.rate_per_minute <= 0:
        print("--rate-per-minute must be positive", file=sys.stderr)
        return 2
    os.makedirs(args.out_dir, exist_ok=True)
    latch_dir = args.latch_dir or default_latch_dir(args.out_dir)
    if args.clear_latch and clear_latch(latch_dir):
        print("latch cleared")
    latch = latch_active(latch_dir)
    if latch is not None:
        print(
            f"REFUSED: a block latch is in force ({latch.get('reason')}); "
            f"cool-down until {latch.get('cooldown_until')}. Use --clear-latch only once "
            "you have decided the cool-down has done its job.",
            file=sys.stderr,
        )
        return HISTORY_BLOCKED_EXIT_CODE

    if not os.path.isfile(args.worklist):
        print(f"{args.worklist}: no such work-list file", file=sys.stderr)
        return 2
    try:
        worklist = load_worklist(args.worklist)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    stem = worklist_stem(args.worklist)
    rows = int((worklist["pass"] == args.pass_no).sum())
    paths = output_paths(args.out_dir, stem, args.pass_no)
    try:
        check_output_shapes(paths)
        done = len(read_done_keys(paths["queries"]))
    except OutputShapeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        f"{stem} pass {args.pass_no}: {rows} rows, {done} already answered, "
        f"{rows - done} to query at {args.rate_per_minute:g}/min (jitter {args.jitter})"
        + (f", capped at {args.max_requests} requests" if args.max_requests else "")
    )
    if args.dry_run:
        return 0

    try:
        result = asyncio.run(
            harvest_worklist_async(
                worklist,
                args.out_dir,
                stem=stem,
                pass_no=args.pass_no,
                rate_per_minute=args.rate_per_minute,
                jitter=args.jitter,
                max_requests=args.max_requests,
                breaker_limit=args.breaker_limit,
                breaker_window=args.breaker_window,
                breaker_share=args.breaker_share,
                cooldown_hours=args.cooldown_hours,
                latch_dir=latch_dir,
            )
        )
    except HarvestBlockedError as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return HISTORY_BLOCKED_EXIT_CODE
    except HarvestBusyError as e:
        print(f"BUSY: {e}", file=sys.stderr)
        return HISTORY_BUSY_EXIT_CODE
    except OutputShapeError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    except HarvestIncompleteError as e:
        print(f"INCOMPLETE: {e} ({e.api_requests} requests this run)", file=sys.stderr)
        return SWEEP_INCOMPLETE_EXIT_CODE
    print(
        f"Done at {datetime.now(UTC).isoformat()}: {result['answered']} rows answered from "
        f"{result['api_requests']} requests; pass {args.pass_no} of {stem} is complete."
    )
    for name, path in result["paths"].items():
        print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
