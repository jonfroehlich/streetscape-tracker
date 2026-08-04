#!/usr/bin/env python3
"""
Backfill street_walks.coverage_by_highway from coverage artifacts on disk.

Background: schema v11 (issue #101) added a per-highway-bucket JSON column to
`street_walks`, populated by the collector at registration time. Walks
cataloged before v11 have NULL there even though every coverage GeoJSON has
carried the breakdown in its `properties.metadata.coverage_by_highway` since
the road-walk collector shipped — so the catalog can be brought current by
reading it back out of the artifacts, no recomputation and no API calls.

One-time and idempotent: only rows with a NULL column are candidates, so a
second run finds nothing to do. Rows whose artifact is missing from
--data-dir (or predates the breakdown key) are reported and left NULL rather
than guessed at.

Deliberately NOT folded into `scheduler reconcile-walks`: that is date-scoped
orphan *salvage* which mutates schedule_state, while this is a catalog-wide
UPDATE on rows that already exist.

Walk-to-walk diffs are not backfilled here — production has one walk per
(city, provider, network) so far, so there are no pairs to diff; the diff
machinery fills in on its own as second walks land.

Usage:
    python scripts/backfill_streetwalk_coverage.py                # dry run (default)
    python scripts/backfill_streetwalk_coverage.py --execute      # apply
    python scripts/backfill_streetwalk_coverage.py --data-dir DIR --db-path PATH --execute
"""

import argparse
import gzip
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("backfill_streetwalk_coverage")


def backfill(conn, data_dir: str, execute: bool = False) -> dict[str, int]:
    """
    Populate coverage_by_highway on every NULL row whose artifact carries it.

    Returns counts: updated (or would-update on a dry run), missing_artifact,
    unreadable (artifact present but not valid gzip/JSON), missing_key
    (artifact readable but carries no coverage_by_highway — pre-breakdown),
    and skipped_no_filename (row never cataloged a coverage artifact at all).
    """
    counts = {
        "updated": 0,
        "missing_artifact": 0,
        "unreadable": 0,
        "missing_key": 0,
        "skipped_no_filename": 0,
    }
    rows = conn.execute(
        """SELECT walk_id, city_id, provider, network_type, run_date, coverage_filename
           FROM street_walks WHERE coverage_by_highway IS NULL
           ORDER BY city_id, provider, network_type, run_date"""
    ).fetchall()

    for row in rows:
        label = f"{row['city_id']} [{row['provider']}/{row['network_type']}] {row['run_date']}"
        if not row["coverage_filename"]:
            logger.warning(f"{label}: no coverage artifact cataloged; leaving NULL")
            counts["skipped_no_filename"] += 1
            continue
        path = os.path.join(data_dir, row["coverage_filename"])
        if not os.path.exists(path):
            logger.warning(f"{label}: artifact missing from {data_dir}; leaving NULL")
            counts["missing_artifact"] += 1
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                geojson = json.load(fh)
        except (OSError, EOFError, ValueError) as e:
            logger.warning(
                f"{label}: could not read {row['coverage_filename']} ({e}); leaving NULL"
            )
            counts["unreadable"] += 1
            continue
        try:
            breakdown = geojson["properties"]["metadata"].get("coverage_by_highway")
        except (KeyError, TypeError, AttributeError):
            breakdown = None
        if not breakdown:
            logger.warning(f"{label}: artifact carries no coverage_by_highway; leaving NULL")
            counts["missing_key"] += 1
            continue

        logger.info(f"{label}: {len(breakdown)} highway buckets from {row['coverage_filename']}")
        if execute:
            conn.execute(
                "UPDATE street_walks SET coverage_by_highway = ? WHERE walk_id = ?",
                (json.dumps(breakdown), row["walk_id"]),
            )
        counts["updated"] += 1

    if execute:
        conn.commit()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--data-dir", default=None, help="Data directory (default: ./data)")
    parser.add_argument("--db-path", default=None, help="Catalog DB path (default: in data dir)")
    parser.add_argument(
        "--execute", action="store_true", help="Apply the updates (default: dry run)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data_dir = args.data_dir or get_default_data_dir()
    db_path = args.db_path or db.get_default_db_path(data_dir)
    conn = db.connect(db_path)
    try:
        mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to apply)"
        logger.info(f"{mode}\n")
        counts = backfill(conn, data_dir, execute=args.execute)
        verb = "Updated" if args.execute else "Would update"
        logger.info(
            f"\n{verb} {counts['updated']} walk(s); "
            f"{counts['missing_artifact']} missing artifact(s), "
            f"{counts['unreadable']} unreadable, "
            f"{counts['missing_key']} without a breakdown, "
            f"{counts['skipped_no_filename']} with no artifact cataloged."
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
