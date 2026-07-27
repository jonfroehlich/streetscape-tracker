#!/usr/bin/env python3
"""
Rename road-walk artifacts written before streetwalk filenames carried a
provider token, and repoint their catalog rows.

Background: `naming.generate_streetwalk_filename` originally took no provider,
so every walk of a city at a given spacing and run date produced the SAME
name regardless of which imagery provider was walked. That was harmless while
the collector was a manual GSV-only CLI, but the scheduler now runs a city's
`gsv_streets` and `mapillary_streets` channels back-to-back on one run date —
at which point the second collection would find the first's snapshot already on
disk and skip as a no-op. The generator now emits the run-filename convention
(`..._step_{S}[_{provider}]_streetwalk_sp{N}_{DATE}`, gsv tokenless), and this
script brings artifacts collected before that change into line.

Only non-GSV walks are affected: GSV is tokenless in both the old and the new
scheme, so every GSV walk ever published keeps its exact filename.

For each affected `street_walks` row this renames the snapshot `.csv.gz` and its
`_coverage.json.gz` sibling on disk, then updates `csv_filename` /
`coverage_filename` to match, and finally regenerates `streetwalks.json.gz` so
the published manifest advertises the new names. Files are renamed BEFORE the
catalog is updated, so a failure can never leave a row pointing at a name that
does not exist. A row whose snapshot is missing from --data-dir is reported and
skipped rather than repointed at a nonexistent artifact.

Idempotent: rows already carrying a provider token are left alone, so a second
run is a no-op and it is safe to run on a host that is already correct.

NOTE (publishing): sync_data_to_server.sh does not pass --delete by default, so
the old artifact names linger on the web server as unreferenced orphans until a
`./sync_data_to_server.sh --delete`. Nothing reads them once the manifest is
regenerated.

Usage:
    python scripts/repair_streetwalk_names.py                     # dry run (default)
    python scripts/repair_streetwalk_names.py --execute           # apply
    python scripts/repair_streetwalk_names.py --data-dir DIR --db-path PATH --execute
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    generate_streetwalk_manifest,
)
from streetscape_metadata_tracker.naming import (  # noqa: E402
    generate_streetwalk_filename,
    parse_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("repair_streetwalk_names")


def corrected_names(row) -> tuple[str, str] | None:
    """
    The (csv, coverage) filenames this walk should carry, or None if it already
    carries them.

    The geometry comes from parsing the EXISTING name rather than from the
    city's current row: the filename records the frozen grid as it was at
    collection time, and that is what the artifact on disk is actually named.
    """
    parsed = parse_streetwalk_filename(row["csv_filename"])
    if parsed.provider == row["provider"]:
        return None  # already tokenized (or a GSV walk, which needs no token)

    stem = generate_streetwalk_filename(
        row["city_id"],
        parsed.width_meters,
        parsed.height_meters,
        parsed.step_meters,
        parsed.spacing_meters,
        parsed.run_date,
        provider=row["provider"],
    )
    csv_name = stem + ".csv.gz"
    return csv_name, streetwalk_coverage_filename(csv_name)


def find_walks_to_repair(conn, data_dir: str) -> list[dict]:
    """
    Every street_walks row whose artifacts are misnamed, with the corrected
    names and whether the files are actually present to rename.
    """
    items = []
    rows = conn.execute(
        "SELECT * FROM street_walks ORDER BY city_id, provider, run_date"
    ).fetchall()
    for row in rows:
        if not row["csv_filename"]:
            continue
        try:
            names = corrected_names(row)
        except ValueError:
            # Not a parseable streetwalk name — hand-written or from a scheme
            # this script doesn't know. Leave it entirely alone.
            logger.warning(
                f"{row['city_id']} [{row['provider']}]: unparseable streetwalk "
                f"filename {row['csv_filename']!r}; skipping"
            )
            continue
        if names is None:
            continue
        new_csv, new_coverage = names
        items.append(
            {
                "row": row,
                "new_csv": new_csv,
                "new_coverage": new_coverage,
                "csv_present": os.path.exists(os.path.join(data_dir, row["csv_filename"])),
            }
        )
    return items


def repair_walk(conn, data_dir: str, item: dict, execute: bool) -> bool:
    """
    Rename one walk's artifacts and repoint its catalog row. Returns True when
    the row was (or would be) repaired.

    Files first, then the catalog: a failure partway can leave an artifact
    renamed ahead of its row, which the next run simply finishes, but never a
    row pointing at a name that isn't on disk.
    """
    row = item["row"]
    old_coverage = row["coverage_filename"] or streetwalk_coverage_filename(row["csv_filename"])
    pairs = [
        (row["csv_filename"], item["new_csv"]),
        (old_coverage, item["new_coverage"]),
    ]

    for old_name, new_name in pairs:
        old_path = os.path.join(data_dir, old_name)
        new_path = os.path.join(data_dir, new_name)
        if not os.path.exists(old_path):
            # The coverage sibling can legitimately be absent (some fixtures
            # never wrote one); the snapshot's absence is caught by the caller.
            logger.info(f"    (no file to rename: {old_name})")
            continue
        if os.path.exists(new_path):
            logger.warning(f"    target already exists, not overwriting: {new_name}")
            continue
        logger.info(f"    {old_name}\n      -> {new_name}")
        if execute:
            os.rename(old_path, new_path)

    if execute:
        conn.execute(
            "UPDATE street_walks SET csv_filename = ?, coverage_filename = ? WHERE walk_id = ?",
            (item["new_csv"], item["new_coverage"], row["walk_id"]),
        )
        conn.commit()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--data-dir", default=None, help="Data directory (default: ./data)")
    parser.add_argument("--db-path", default=None, help="Catalog DB path (default: in data dir)")
    parser.add_argument(
        "--execute", action="store_true", help="Apply the renames (default: dry run)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data_dir = args.data_dir or get_default_data_dir()
    db_path = args.db_path or db.get_default_db_path(data_dir)
    conn = db.connect(db_path)
    try:
        items = find_walks_to_repair(conn, data_dir)
        if not items:
            logger.info("Every street walk already carries its provider token. Nothing to do.")
            return 0

        mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to apply)"
        logger.info(f"{mode}: {len(items)} walk(s) to rename\n")

        repaired = skipped = 0
        for item in items:
            row = item["row"]
            logger.info(f"{row['city_id']} [{row['provider']}] {row['run_date']}:")
            if not item["csv_present"]:
                logger.warning(
                    f"    snapshot missing from {data_dir}; leaving the catalog row "
                    f"pointing at {row['csv_filename']}"
                )
                skipped += 1
                continue
            repair_walk(conn, data_dir, item, args.execute)
            repaired += 1

        logger.info("")
        if args.execute:
            manifest = generate_streetwalk_manifest(conn, data_dir)
            logger.info(
                f"Renamed {repaired} walk(s), skipped {skipped}; "
                f"regenerated streetwalks.json.gz ({len(manifest['walks'])} walks)."
            )
            logger.info(
                "Publish with ./sync_data_to_server.sh (add --delete to remove the "
                "old artifact names from the web server)."
            )
        else:
            logger.info(f"Would rename {repaired} walk(s), skip {skipped}. Re-run with --execute.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
