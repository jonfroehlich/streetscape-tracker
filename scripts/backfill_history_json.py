#!/usr/bin/env python3
"""
Write the capture-history summary JSON beside every harvested `_gsv_history_` CSV.

Background: issue #109 gave each history harvest (issue #2) a sibling summary,
`{history_base}.json.gz`, which the aggregate points at and the city page
renders.
`scripts/harvest_gsv_history.py` now writes it at harvest time, but a harvest
cataloged before that has only its CSV -- and the aggregate deliberately
reads, never builds, the summary (the nightly tail must not grow a CSV pass),
so such a harvest stays invisible on the site until this script summarizes it.

Catalog/disk-only: it reads `history_harvests` rows and their CSVs, contacts no
endpoint, and writes no catalog row.
That is also why it does NOT take the run-due in-flight guard the other repair
scripts take: that guard protects the catalog and a night's diffs, and this
touches neither -- each summary is written atomically (temp file +
`os.replace`), so a publish running beside it ships either the old file or
the new one, never half of one.

Dry run by default. Every harvest row is reported as one of:
  * up to date  -- its summary exists (rewrite it with --force);
  * would write / written;
  * missing CSV -- the cataloged CSV is not in --data-dir; reported, never
    guessed at, and the script then exits 1.

A --data-dir that is not a directory, or a catalog that does not exist, is
refused with exit 2 before anything is opened: `db.connect` would otherwise
create an empty catalog and the script would report "0 rows", which reads as
"no harvests, nothing to do" (#436 review).

Then run `python -m streetscape_metadata_tracker.scheduler regenerate-aggregate
--publish` to surface the summaries on the site.

Usage:
    python scripts/backfill_history_json.py                       # dry run (default)
    python scripts/backfill_history_json.py --execute             # write missing summaries
    python scripts/backfill_history_json.py --execute --force     # rewrite every summary
    python scripts/backfill_history_json.py --city "Seattle, WA" --execute
    python scripts/backfill_history_json.py --data-dir DIR --db-path PATH --execute
"""

import argparse
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.fileutils import load_history_csv_file  # noqa: E402
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    _history_json_filename,
    generate_history_summary_as_json,
)
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Write the capture-history summary JSON beside every harvested "
        "_gsv_history_ CSV (issue #109). Dry run unless --execute."
    )
    p.add_argument(
        "--data-dir",
        default=get_default_data_dir(),
        help="Directory holding the harvest csv.gz files (default: ./data)",
    )
    p.add_argument(
        "--db-path",
        default=None,
        help="Catalog DB path (default: <data-dir>/streetscape_tracker.db)",
    )
    p.add_argument("--city", default=None, help="Limit to one city (query or slug)")
    p.add_argument("--execute", action="store_true", help="Write files (default: dry run)")
    p.add_argument("--force", action="store_true", help="Rewrite summaries that already exist")
    p.add_argument("--verbose", action="store_true", help="Debug logging")
    return p.parse_args(argv)


def backfill(conn, data_dir: str, *, city_id=None, execute=False, force=False) -> dict[str, int]:
    """
    Summarize every harvest row (or one city's) that lacks a summary.

    Returns counts: ``written`` (would-write on a dry run), ``up_to_date``,
    ``missing_csv``.
    """
    counts = {"written": 0, "up_to_date": 0, "missing_csv": 0}
    verb = "wrote" if execute else "would write"
    for row in db.get_history_harvests(conn, city_id):
        label = f"{row['city_id']} [{row['provider']}] {row['harvest_date']}"
        csv_path = os.path.join(data_dir, row["csv_filename"])
        json_path = os.path.join(data_dir, _history_json_filename(row["csv_filename"]))
        if not os.path.exists(csv_path):
            print(f"{label}: MISSING CSV {row['csv_filename']} in {data_dir}; skipped")
            counts["missing_csv"] += 1
            continue
        if os.path.exists(json_path) and not force:
            print(f"{label}: up to date ({os.path.basename(json_path)})")
            counts["up_to_date"] += 1
            continue
        if execute:
            generate_history_summary_as_json(
                csv_path,
                load_history_csv_file(csv_path),
                city_id=row["city_id"],
                harvest_date=date.fromisoformat(row["harvest_date"]),
                provider=row["provider"],
                grid_points_queried=row["grid_points_queried"],
                api_requests=row["api_requests"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                force_recreate_file=True,
            )
        print(f"{label}: {verb} {os.path.basename(json_path)}")
        counts["written"] += 1
    return counts


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    # Refuse a missing --data-dir or catalog BEFORE db.connect, which would
    # create both (directory and a fresh empty schema) and then report
    # "0 rows" -- the exact output the deploy note reads as "no harvests,
    # nothing to do". A mistyped prod path must fail loudly, and a dry run
    # must never write a file.
    if not os.path.isdir(args.data_dir):
        print(f"Refusing: --data-dir {args.data_dir!r} is not a directory.", file=sys.stderr)
        return 2
    if not os.path.isfile(db_path):
        print(
            f"Refusing: no catalog at {db_path!r} (wrong --data-dir or --db-path?).",
            file=sys.stderr,
        )
        return 2
    conn = db.connect(db_path)
    try:
        city_id = None
        if args.city:
            city = db.resolve_city(conn, args.city)
            if city is None:
                print(f"City {args.city!r} is not registered in the catalog.", file=sys.stderr)
                return 2
            city_id = city.city_id
        counts = backfill(
            conn, args.data_dir, city_id=city_id, execute=args.execute, force=args.force
        )
    finally:
        conn.close()

    total = sum(counts.values())
    written_label = "written" if args.execute else "would write"
    print(
        f"{total} rows: {counts['written']} {written_label}, "
        f"{counts['up_to_date']} up to date, {counts['missing_csv']} missing CSV"
    )
    if not args.execute and counts["written"]:
        print("Dry run: nothing was written. Re-run with --execute.")
    if args.execute and counts["written"]:
        print(
            "Then run `python -m streetscape_metadata_tracker.scheduler "
            "regenerate-aggregate --publish` to surface them."
        )
    return 1 if counts["missing_csv"] else 0


if __name__ == "__main__":
    sys.exit(main())
