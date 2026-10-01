#!/usr/bin/env python3
"""
Find, and with --execute remove, published diff detail files that no catalog row points at.

Background (issue #265): both diff families — grid runs (`run_diffs`) and road
walks (`street_walk_diffs`) — used to write their detail `.csv.gz` only when a
diff had changes and never delete one. A re-diff that came out with no
changes, or a walk re-diff that was skipped (a same-day re-collection at a
different `--spacing`), dropped the row's `detail_filename` and left the file
in `data/`, which is rsynced to a public web server. The collectors now keep
the file a function of the diff result; this script is the one-off repair for
files stranded before that, and the way to learn whether the bug ever bit.

What it does, in one `os.scandir` pass over the top level of --data-dir (diff
details are written there and nowhere else):

- A file is a CANDIDATE only if its name has the exact shape one of the two
  generators emits (`diff.DIFF_DETAIL_FILENAME_RE`,
  `naming.STREETWALK_DIFF_FILENAME_RE`). Anything else — run CSVs, per-run
  JSON, walk artifacts, a city whose slug happens to contain `_diff_` — is
  never touched.
- A candidate is an ORPHAN when no `run_diffs.detail_filename` and no
  `street_walk_diffs.detail_filename` names it. Orphans are listed; with
  --execute they are removed through the same helper the collectors use
  (`fileutils.remove_stale_diff_detail`), which logs and continues on failure.
- The inverse — a row whose `detail_filename` is missing on disk — is
  reported as INFORMATION ONLY. It is never "fixed" here: the row is the
  record of a computed diff, and a missing file is re-created by re-diffing,
  not by editing the catalog to match the disk.

Catalog/disk only: no API calls, no network. Dry run by default.

NOTE (publishing): neither the nightly publish nor a plain
`./sync_data_to_server.sh` passes rsync `--delete`, so removing a file here
does NOT remove the copy already on the web server. The orphan names this
script prints are the list to remove there; see the script's closing message.

Usage:
    python scripts/sweep_orphan_diff_details.py                       # dry run (default)
    python scripts/sweep_orphan_diff_details.py --execute             # remove the orphans
    python scripts/sweep_orphan_diff_details.py --data-dir DIR --db-path PATH
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.diff import is_diff_detail_filename  # noqa: E402
from streetscape_metadata_tracker.fileutils import remove_stale_diff_detail  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("sweep_orphan_diff_details")


@dataclass
class SweepReport:
    """What one pass found. Every list is sorted, so two runs diff cleanly."""

    candidates: int = 0  # diff-detail-shaped files on disk
    orphans: list[str] = field(default_factory=list)  # shaped, unreferenced
    missing: list[str] = field(default_factory=list)  # referenced, not on disk
    removed: list[str] = field(default_factory=list)  # orphans actually deleted
    failed: list[str] = field(default_factory=list)  # orphans --execute could not delete


def referenced_detail_files(conn) -> set[str]:
    """Every detail filename a diff row of either family points at."""
    names: set[str] = set()
    for table in ("run_diffs", "street_walk_diffs"):
        rows = conn.execute(
            f"SELECT detail_filename FROM {table} WHERE detail_filename IS NOT NULL"
        ).fetchall()
        names.update(row["detail_filename"] for row in rows)
    return names


def sweep(conn, data_dir: str, execute: bool) -> SweepReport:
    """Classify the diff detail files in ``data_dir`` against the catalog and,
    when ``execute``, remove the orphans."""
    referenced = referenced_detail_files(conn)
    on_disk: set[str] = set()
    with os.scandir(data_dir) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and is_diff_detail_filename(entry.name):
                on_disk.add(entry.name)

    report = SweepReport(candidates=len(on_disk))
    report.orphans = sorted(on_disk - referenced)
    # Checked against the disk rather than the shaped set: a row naming a file
    # in some other shape is still a row whose file is (or isn't) present.
    report.missing = sorted(
        name for name in referenced if not os.path.exists(os.path.join(data_dir, name))
    )
    if execute:
        for name in report.orphans:
            if remove_stale_diff_detail(data_dir, name):
                report.removed.append(name)
            else:
                report.failed.append(name)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--db-path", default=None, help="default: {data-dir}/streetscape_tracker.db"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Remove the orphans (default: dry run)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    # Both guards exist because "unreferenced" is judged against the catalog:
    # a wrong --db-path would make EVERY diff detail an orphan. db.connect
    # creates a missing file, so existence is checked before it can; and an
    # existing catalog holding no runs at all is refused as the same mistake.
    if not os.path.exists(db_path):
        logger.error(f"No catalog at {db_path}; refusing to judge {args.data_dir} against nothing")
        return 2
    conn = db.connect(db_path)
    try:
        if conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0:
            logger.error(f"{db_path} catalogs no runs; refusing (wrong --db-path?)")
            return 2
        report = sweep(conn, args.data_dir, args.execute)
    finally:
        conn.close()

    mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to remove)"
    logger.info(f"{mode}: {args.data_dir}")
    logger.info(
        f"{report.candidates} diff detail file(s) on disk, {len(report.orphans)} with no "
        "catalog row pointing at them"
    )
    for name in report.orphans:
        logger.info(f"  orphan: {name}")
    if report.missing:
        logger.info(
            f"{len(report.missing)} catalog row(s) name a detail file that is not on disk "
            "(information only; re-diff to recreate, never edit the row):"
        )
        for name in report.missing:
            logger.info(f"  missing: {name}")

    if args.execute:
        logger.info(f"Removed {len(report.removed)}, failed {len(report.failed)}.")
        if report.removed:
            logger.info(
                "The published copies are NOT removed by this: the publish rsync never "
                "passes --delete. Remove the names listed above from the web docroot."
            )
        return 1 if report.failed else 0
    if report.orphans:
        logger.info("Re-run with --execute to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
