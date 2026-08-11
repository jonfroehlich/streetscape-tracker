#!/usr/bin/env python3
"""
Backfill the street_walks absolute-length columns from coverage artifacts on disk.

Background: schema v12 added `length_km`, `length_km_covered`,
`length_km_covered_any` and `median_covered_age_years` to `street_walks`,
populated by the collector at registration time. Walks cataloged before v12
have NULL there even though every coverage GeoJSON has carried the figures in
its `properties.metadata.totals` since the road-walk collector shipped — so the
catalog can be brought current by reading them back out of the artifacts, with
no recomputation and no API calls.

The sibling of `backfill_streetwalk_coverage.py` (schema v11) and deliberately
shaped the same way: one-time, idempotent, and candidate rows are only those
with a NULL `length_km`, so a second run finds nothing to do. Rows whose
artifact is missing from --data-dir (or predates the totals) are reported and
left NULL rather than guessed at.

`length_km` alone gates candidacy. The other three are legitimately NULL on
rows that do have a length: a pre-#116 artifact carries no any-imagery length,
and `median_covered_age_years` is null whenever nothing was covered or no
covered edge carried a capture date. Keying on those would make such rows
permanent candidates that never resolve.

Cross-check: each artifact's lengths are verified against the percentage
already stored on the row (100 * covered / total ≈ coverage_pct_by_length). A
mismatch means the row was matched to the wrong artifact, which is worth
failing loudly over — the lengths and the percentages are published side by
side and must tell one story.

Usage:
    python scripts/backfill_streetwalk_length.py                # dry run (default)
    python scripts/backfill_streetwalk_length.py --execute      # apply
    python scripts/backfill_streetwalk_length.py --data-dir DIR --db-path PATH --execute
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

logger = logging.getLogger("backfill_streetwalk_length")

# Both sides of the comparison are pre-rounded in the artifact — the percentage
# to 1 dp, the lengths to 3 — so recomputing the percentage from the lengths
# cannot reproduce it exactly. This tolerance admits that rounding and nothing
# wider: a wrong-artifact match is off by whole percentage points, not tenths.
_PCT_TOLERANCE_PP = 0.15


def _percent_mismatch(totals: dict, stored_pct: float | None) -> float | None:
    """
    Absolute gap in percentage points between the percentage recomputed from
    the artifact's lengths and the one already cataloged, or None when the
    comparison cannot be made (no stored percentage, or a zero-length network).
    """
    if stored_pct is None:
        return None
    length_km = totals.get("length_km")
    covered_km = totals.get("length_km_covered")
    if not length_km or covered_km is None:
        return None
    return abs(100.0 * covered_km / length_km - stored_pct)


def backfill(conn, data_dir: str, execute: bool = False) -> dict[str, int]:
    """
    Populate the v12 length columns on every row with a NULL `length_km` whose
    artifact carries them.

    Returns counts: updated (or would-update on a dry run), missing_artifact,
    unreadable (artifact present but not valid gzip/JSON), missing_key
    (artifact readable but carries no length totals), skipped_no_filename (row
    never cataloged a coverage artifact at all), and mismatched (artifact
    lengths disagree with the percentage already on the row — never written).
    """
    counts = {
        "updated": 0,
        "missing_artifact": 0,
        "unreadable": 0,
        "missing_key": 0,
        "skipped_no_filename": 0,
        "mismatched": 0,
    }
    rows = conn.execute(
        """SELECT walk_id, city_id, provider, network_type, run_date,
                  coverage_filename, coverage_pct_by_length
           FROM street_walks WHERE length_km IS NULL
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
            totals = geojson["properties"]["metadata"].get("totals") or {}
        except (KeyError, TypeError, AttributeError):
            totals = {}
        if totals.get("length_km") is None:
            logger.warning(f"{label}: artifact carries no length totals; leaving NULL")
            counts["missing_key"] += 1
            continue

        gap = _percent_mismatch(totals, row["coverage_pct_by_length"])
        if gap is not None and gap > _PCT_TOLERANCE_PP:
            logger.error(
                f"{label}: {row['coverage_filename']} implies "
                f"{100.0 * totals['length_km_covered'] / totals['length_km']:.1f}% covered but "
                f"the catalog says {row['coverage_pct_by_length']:.1f}% "
                f"({gap:.1f} pp apart) — wrong artifact? leaving NULL"
            )
            counts["mismatched"] += 1
            continue

        logger.info(
            f"{label}: {totals['length_km']:,.1f} km network, "
            f"{totals['length_km_covered']:,.1f} km covered "
            f"(from {row['coverage_filename']})"
        )
        if execute:
            conn.execute(
                """UPDATE street_walks
                   SET length_km = ?, length_km_covered = ?,
                       length_km_covered_any = ?, median_covered_age_years = ?
                   WHERE walk_id = ?""",
                (
                    totals["length_km"],
                    totals.get("length_km_covered"),
                    totals.get("length_km_covered_any"),
                    totals.get("median_covered_age_years"),
                    row["walk_id"],
                ),
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
            f"{counts['missing_key']} without length totals, "
            f"{counts['skipped_no_filename']} with no artifact cataloged, "
            f"{counts['mismatched']} disagreeing with the cataloged percentage."
        )
        # A mismatch is a data-integrity finding, not a routine skip: surface it
        # in the exit status so a scripted rollout stops rather than proceeding
        # to publish lengths that contradict the percentages beside them.
        if counts["mismatched"]:
            logger.error(
                f"\n{counts['mismatched']} walk(s) disagreed with their cataloged "
                f"coverage percentage; investigate before publishing."
            )
            return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
