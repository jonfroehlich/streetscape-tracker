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

What it does:

- A file is a CANDIDATE only if it is a regular file (never a symlink or a
  directory) at the top level of --data-dir whose name has the exact shape
  one of the two generators emits (`diff.diff_detail_match`). Anything else —
  run CSVs, per-run JSON, walk artifacts, a city whose slug happens to
  contain `_diff_` — is never touched.
- A candidate no `run_diffs.detail_filename` and no
  `street_walk_diffs.detail_filename` names is UNREFERENCED. One modified
  within the last --min-age-hours (default 24) is reported as TOO RECENT and
  never deleted; an older one is an ORPHAN, which --execute removes through
  the remover the collectors use (`fileutils.remove_stale_diff_detail`).
- The inverse — a row whose `detail_filename` is missing on disk — is
  reported as INFORMATION ONLY. It is never "fixed" here: the row is the
  record of a computed diff, and a missing file is re-created by re-diffing,
  not by editing the catalog to match the disk.

Why it is careful (the #402 review). Both collectors write a detail file
BEFORE committing the row that names it — deliberately: every published JSON
copies the row's `detail_filename` into a `diff_file` link, so a row naming a
file that is not there yet is the worse state. A diff being written while
this script runs is therefore briefly unreferenced, and four layered guards
keep it:

1. The directory is listed BEFORE the catalog is read, so a row committed in
   between counts as referenced.
2. The age window skips every recently written file, whoever wrote it — the
   nightly batch, a hand-run collector, `assess-city`, `import-bundle`. Every
   file this script exists for was stranded before #265 deployed, so the
   window costs nothing. `--min-age-hours 0` disables it, loudly.
3. Each name is re-checked against both tables immediately before its
   unlink.
4. `--execute` is refused while a `run-due` is in flight on this machine
   (`scheduler._run_due_in_flight`, the detector `prefreeze_street_networks.py`
   and `import-bundle` use). A dry run proceeds and says so.

The catalog is opened read-only by construction (`mode=rw` plus
`PRAGMA query_only`; never `db.connect`, which creates, migrates and switches
journal mode) and is refused — exit 64, nothing deleted — when it is missing,
is not a catalog of this code's schema version, catalogs no runs or walks, or (for
`--execute`) looks OLDER than the disk: any unreferenced candidate dated
after the newest run or walk the catalog knows means a restored backup or a
dev copy, against which every later diff would look orphaned.

Catalog/disk only: no API calls, no network. Dry run by default.
Exit codes: 0 done, 1 an --execute removal failed, 2 usage error, 64 refused.

NOTE (publishing): neither the nightly publish nor a plain
`./sync_data_to_server.sh` passes rsync `--delete`, so removing a file here
does NOT remove the copy already on the web server. The orphan names this
script prints are the list to remove there; see the script's closing message.

Usage:
    python scripts/sweep_orphan_diff_details.py                       # dry run (default)
    python scripts/sweep_orphan_diff_details.py --execute             # remove the orphans
    python scripts/sweep_orphan_diff_details.py --data-dir DIR --db-path PATH
    python scripts/sweep_orphan_diff_details.py --min-age-hours 72    # a wider window
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.diff import diff_detail_match  # noqa: E402
from streetscape_metadata_tracker.fileutils import remove_stale_diff_detail  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_metadata_tracker.scheduler import (  # noqa: E402
    USAGE_EXIT_CODE,
    _run_due_in_flight,
)

logger = logging.getLogger("sweep_orphan_diff_details")

DEFAULT_MIN_AGE_HOURS = 24.0


class CatalogRefused(Exception):
    """The catalog cannot be trusted to judge the disk; nothing was deleted."""


@dataclass
class SweepReport:
    """What one pass found. Every list is sorted, so two runs diff cleanly."""

    candidates: int = 0  # diff-detail-shaped regular files on disk
    orphans: list[str] = field(default_factory=list)  # unreferenced, older than the window
    too_recent: list[str] = field(default_factory=list)  # unreferenced, inside the window
    missing: list[str] = field(default_factory=list)  # referenced, not on disk
    newest_catalog_date: str | None = None  # newest run/walk date the catalog knows
    newer_than_catalog: list[str] = field(default_factory=list)  # unreferenced, to-date after it
    removed: list[str] = field(default_factory=list)  # orphans actually deleted
    rechecked: list[str] = field(default_factory=list)  # orphans a row claimed before unlink
    failed: list[str] = field(default_factory=list)  # orphans --execute could not delete


def open_catalog_readonly(db_path: str) -> sqlite3.Connection:
    """
    Open the catalog for reading only, or raise CatalogRefused.

    ``mode=rw`` (which, unlike a plain path, never creates a missing file)
    plus ``PRAGMA query_only``, rather than ``mode=ro``: measured on a
    WAL-mode catalog with no other connection open, a ``mode=ro`` reader
    creates ``-wal``/``-shm`` and cannot remove them on close, leaving
    sidecars that outlive their database's last writer; this form leaves the
    file byte-identical and no sidecars behind, and still sees each commit a
    concurrent writer makes. Never ``immutable=1``, which is only safe for a
    file nobody is writing — the nightly batch may be.
    """
    if not os.path.isfile(db_path):
        raise CatalogRefused(f"no catalog at {db_path}")
    uri = Path(db_path).resolve().as_uri() + "?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise CatalogRefused(f"cannot open {db_path} ({exc})") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != db.SCHEMA_VERSION:
            raise CatalogRefused(
                f"{db_path} is schema version {version}, not this code's {db.SCHEMA_VERSION} "
                "(not a catalog, or one this code would have to migrate; the sweep never "
                "writes the catalog, so it refuses rather than migrating)"
            )
        if newest_catalog_date(conn) is None:
            raise CatalogRefused(f"{db_path} catalogs no runs or walks (wrong --db-path?)")
    except sqlite3.Error as exc:
        conn.close()
        raise CatalogRefused(f"{db_path} is not a readable catalog ({exc})") from exc
    except CatalogRefused:
        conn.close()
        raise
    return conn


def scan_diff_details(data_dir: str) -> dict[str, float]:
    """
    Every diff-detail-shaped REGULAR file at the top level of ``data_dir``,
    mapped to its mtime. One ``os.scandir`` pass, no recursion (diff details
    are written nowhere else). ``follow_symlinks=False`` is deliberate: a
    symlink is not an artifact a collector wrote, so it is never a candidate,
    whatever it points at.
    """
    found: dict[str, float] = {}
    with os.scandir(data_dir) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and diff_detail_match(entry.name):
                found[entry.name] = entry.stat(follow_symlinks=False).st_mtime
    return found


def referenced_detail_files(conn) -> set[str]:
    """Every detail filename a diff row of either family points at."""
    names: set[str] = set()
    for table in ("run_diffs", "street_walk_diffs"):
        rows = conn.execute(
            f"SELECT detail_filename FROM {table} WHERE detail_filename IS NOT NULL"
        ).fetchall()
        names.update(row["detail_filename"] for row in rows)
    return names


def is_referenced(conn, name: str) -> bool:
    """Whether a diff row of either family names ``name`` NOW — the re-check
    made immediately before each unlink."""
    for table in ("run_diffs", "street_walk_diffs"):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE detail_filename = ? LIMIT 1", (name,)
        ).fetchone():
            return True
    return False


def newest_catalog_date(conn) -> str | None:
    """The newest run or walk date the catalog knows (ISO, so it orders as a string)."""
    return conn.execute(
        "SELECT MAX(run_date) FROM (SELECT run_date FROM runs "
        "UNION ALL SELECT run_date FROM street_walks)"
    ).fetchone()[0]


def sweep(
    conn,
    data_dir: str,
    execute: bool,
    *,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    now: float | None = None,
) -> SweepReport:
    """
    Classify the diff detail files in ``data_dir`` against the catalog and,
    when ``execute``, remove the orphans.

    Raises CatalogRefused, having deleted nothing, when ``execute`` and an
    unreferenced candidate is dated after the newest run/walk in the catalog.
    """
    # The disk FIRST, the catalog second: a file written and recorded between
    # the two reads is then seen with its row, never as an orphan.
    scanned = scan_diff_details(data_dir)
    referenced = referenced_detail_files(conn)

    report = SweepReport(candidates=len(scanned))
    # Checked against the disk rather than the shaped set: a row naming a file
    # in some other shape is still a row whose file is (or isn't) present.
    report.missing = sorted(
        name for name in referenced if not os.path.exists(os.path.join(data_dir, name))
    )
    cutoff = (time.time() if now is None else now) - min_age_hours * 3600
    unreferenced = sorted(set(scanned) - referenced)
    report.too_recent = [name for name in unreferenced if scanned[name] > cutoff]
    report.orphans = [name for name in unreferenced if scanned[name] <= cutoff]

    report.newest_catalog_date = newest_catalog_date(conn)
    report.newer_than_catalog = [
        name
        for name in unreferenced
        if report.newest_catalog_date is None
        or diff_detail_match(name)["to_date"] > report.newest_catalog_date
    ]
    if not execute:
        return report
    if report.newer_than_catalog:
        raise CatalogRefused(
            f"{len(report.newer_than_catalog)} unreferenced diff detail(s) are dated after "
            f"the newest run or walk this catalog knows ({report.newest_catalog_date}): it "
            "looks older than the disk (a restored backup or a dev copy?)"
        )
    for name in report.orphans:
        if is_referenced(conn, name):
            report.rechecked.append(name)
        elif remove_stale_diff_detail(data_dir, name):
            report.removed.append(name)
        else:
            report.failed.append(name)
    return report


def _log_names(label: str, names: list[str]) -> None:
    for name in names:
        logger.info(f"  {label}: {name}")


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
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=DEFAULT_MIN_AGE_HOURS,
        help="never delete a file modified more recently than this (default: %(default)s; "
        "0 disables the guard, and says so)",
    )
    args = parser.parse_args(argv)
    if args.min_age_hours < 0:
        parser.error("--min-age-hours cannot be negative")

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.min_age_hours == 0:
        logger.warning(
            "AGE GUARD DISABLED (--min-age-hours 0): a diff detail a collector is writing "
            "right now is protected only by the re-check before each unlink"
        )
    in_flight = _run_due_in_flight()
    if in_flight:
        if args.execute:
            logger.error(
                f"A run-due is in flight on this machine ({in_flight}); refusing --execute "
                "while it can be writing diffs. Wait for the night to finish."
            )
            return USAGE_EXIT_CODE
        logger.warning(f"A run-due is in flight on this machine ({in_flight}); dry run only.")

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    try:
        conn = open_catalog_readonly(db_path)
    except CatalogRefused as exc:
        logger.error(f"Refusing: {exc}")
        return USAGE_EXIT_CODE
    try:
        try:
            report = sweep(conn, args.data_dir, args.execute, min_age_hours=args.min_age_hours)
        except CatalogRefused as exc:
            logger.error(f"Refusing --execute: {exc}; nothing was deleted")
            dry = sweep(conn, args.data_dir, False, min_age_hours=args.min_age_hours)
            _log_names("dated after the catalog", dry.newer_than_catalog)
            return USAGE_EXIT_CODE
    finally:
        conn.close()

    mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to remove)"
    logger.info(f"{mode}: {args.data_dir} (age window {args.min_age_hours:g} h)")
    logger.info(
        f"{report.candidates} diff detail file(s) on disk; {len(report.orphans)} orphan(s) "
        f"with no catalog row, {len(report.too_recent)} unreferenced but too recent (skipped)"
    )
    _log_names("orphan", report.orphans)
    _log_names("too recent, skipped", report.too_recent)
    if report.newer_than_catalog:
        logger.warning(
            f"{len(report.newer_than_catalog)} unreferenced file(s) are dated after the newest "
            f"run or walk this catalog knows ({report.newest_catalog_date}); --execute would "
            "refuse. Is this the live catalog?"
        )
        _log_names("dated after the catalog", report.newer_than_catalog)
    if report.missing:
        logger.info(
            f"{len(report.missing)} catalog row(s) name a detail file that is not on disk "
            "(information only; re-diff to recreate, never edit the row):"
        )
        _log_names("missing", report.missing)

    if args.execute:
        logger.info(
            f"Removed {len(report.removed)}, kept {len(report.rechecked)} that a row claimed "
            f"at the re-check, failed {len(report.failed)}."
        )
        _log_names("kept, referenced at re-check", report.rechecked)
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
