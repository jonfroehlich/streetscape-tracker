#!/usr/bin/env python3
"""
Purge runs whose snapshots are tainted by transient API failures (throttling)
so they can be re-collected cleanly.

Background (2026-07-16): before client-side rate limiting, a fast host could
exceed the GSV metadata per-minute quota mid-run; the throttled responses were
written into the immutable snapshot as final rows, where they read as "no
imagery" and corrupt coverage rates and every future diff. Runs are immutable
and UNIQUE(city_id, provider, run_date), so re-collecting the same run date
first requires deleting the catalog rows and the snapshot files.

For each run on --run-date (all providers unless --provider is given), the CSV
is scanned for rows carrying a retryable status (OVER_QUERY_LIMIT,
UNKNOWN_ERROR — analysis.RETRYABLE_STATUSES). A run is purged when it is either:

  * tainted     — its snapshot contains one or more retryable-status rows, or
  * missing     — its snapshot file is gone but the catalog row survives
                  (an orphan that would otherwise silently block re-collection
                  via the immutable-snapshot / same-date guards).

Purging a run removes, in this order (files first, so a failed delete cannot
orphan a published file behind a deleted catalog row):

1. the snapshot .csv.gz, its per-run .json.gz, any _failed_points.csv sidecar,
   and any leftover .downloading / .downloading.runlock / .downloading.lock
   checkpoint siblings for the SAME run date (so a same-date re-collect starts
   clean instead of resuming a pre-fix checkpoint)
2. run_diffs rows referencing the run, plus their published detail files
3. the runs row itself
4. schedule_state.last_success_at for the (city, provider) is CLEARED, so a
   forgotten re-collect leaves the city immediately due again instead of
   silently skipped for a full cycle with green scheduler status

The frozen city geometry, aliases, and api_usage ledger are untouched.
Re-collect afterwards with the SAME run date so every published filename is
overwritten in place, then regenerate the aggregate:

    python streetscape_tracker.py "<City>" --provider gsv --force \
        --run-date YYYY-MM-DD --download-dir DIR --db-path PATH
    python -m streetscape_metadata_tracker.scheduler regenerate-aggregate --publish

Usage:
    python scripts/purge_tainted_runs.py --run-date 2026-07-16            # dry run (default)
    python scripts/purge_tainted_runs.py --run-date 2026-07-16 --execute  # apply
    python scripts/purge_tainted_runs.py --run-date 2026-07-16 --provider gsv \
        --data-dir DIR --db-path PATH
"""

import argparse
import logging
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.analysis import RETRYABLE_STATUSES  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_metadata_tracker.scheduler import CHANNEL_DEFAULT_MEMBERSHIP  # noqa: E402

logger = logging.getLogger("purge_tainted_runs")


def count_tainted_rows(csv_gz_path: str) -> int | None:
    """
    Number of retryable-status rows in a run snapshot, or None if the file is
    missing (a distinct signal from 0, which means "present and clean").
    """
    if not os.path.exists(csv_gz_path):
        return None
    status = pd.read_csv(csv_gz_path, usecols=["status"], dtype=str)["status"]
    return int(status.isin(RETRYABLE_STATUSES).sum())


def _sibling_paths(data_dir: str, csv_filename: str) -> list[str]:
    """
    Every file the run owns besides the catalog rows: the snapshot, its JSON,
    the failed-points sidecar, and any leftover in-progress checkpoint siblings
    for the same run date. run_diffs detail files are added by the caller (it
    already queries them).
    """
    csv_path = os.path.join(data_dir, csv_filename)
    # download_gsv derives these from the .csv.gz path (see
    # download_gsv_metadata_async); mirror that derivation exactly.
    downloading = csv_path[: -len(".gz")] + ".downloading"  # ...csv.downloading
    return [
        csv_path,
        csv_path.replace(".csv.gz", ".json.gz"),
        csv_path.replace(".csv.gz", "_failed_points.csv"),
        downloading,
        downloading + ".runlock",
        downloading + ".lock",
    ]


def find_runs_to_purge(conn, data_dir: str, run_date: str, provider: str | None) -> list[dict]:
    """All runs on run_date that are tainted or whose snapshot file is missing."""
    sql = "SELECT * FROM runs WHERE run_date = ?"
    params: list = [run_date]
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    to_purge = []
    for run in conn.execute(sql + " ORDER BY run_id", params).fetchall():
        n = count_tainted_rows(os.path.join(data_dir, run["csv_filename"]))
        if n is None:
            to_purge.append({"run": run, "reason": "missing-file", "tainted": 0})
        elif n > 0:
            to_purge.append({"run": run, "reason": "tainted", "tainted": n})
    return to_purge


def purge_run(conn, data_dir: str, run, reason: str, execute: bool) -> None:
    """Delete one run's files then its catalog rows (or just narrate)."""
    run_id = run["run_id"]
    verb = "DELETE" if execute else "would delete"

    diffs = conn.execute(
        "SELECT diff_id, detail_filename FROM run_diffs WHERE from_run_id = ? OR to_run_id = ?",
        (run_id, run_id),
    ).fetchall()

    files = _sibling_paths(data_dir, run["csv_filename"])
    files.extend(
        os.path.join(data_dir, d["detail_filename"]) for d in diffs if d["detail_filename"]
    )

    # Files FIRST: if a delete fails (NFS hiccup, held by rsync) the catalog
    # row still points at the surviving files, so the operator can rerun the
    # purge — the reverse order would leave an uncataloged file that keeps
    # publishing and blocks re-collection behind the immutable-snapshot guard.
    csv_path = os.path.join(data_dir, run["csv_filename"])
    json_path = csv_path.replace(".csv.gz", ".json.gz")
    for path in files:
        if os.path.exists(path):
            logger.info(f"  {verb} file {path}")
            if execute:
                os.remove(path)
        elif path in (csv_path, json_path) and reason != "missing-file":
            # The snapshot/JSON are expected to exist for a tainted run.
            logger.warning(f"  expected file already missing: {path}")

    for d in diffs:
        logger.info(f"  {verb} run_diffs row diff_id={d['diff_id']}")
    logger.info(f"  {verb} runs row run_id={run_id}")
    # Membership gates BEFORE staleness in get_due_cities (issue #248), so on a
    # non-member pair the re-arm below re-arms nothing. Say so rather than
    # logging a re-arm that did not happen — the whole point of the re-arm is
    # that a missed re-collect must not hide as a green skip.
    member = db.get_channel_membership(conn, run["city_id"], run["provider"])
    # Indexed, never .get(..., True): a provider token with no membership
    # decision must be a KeyError here rather than a silent "member".
    is_member = CHANNEL_DEFAULT_MEMBERSHIP[run["provider"]] if member is None else bool(member)
    if is_member:
        logger.info(f"  {verb} clear schedule_state.last_success_at for {run['provider']}")
    else:
        logger.warning(
            f"  {run['city_id']} is not a member of {run['provider']}, so clearing "
            f"last_success_at re-arms nothing: it will not become due until it is "
            f"enrolled (`scheduler enroll-city ... --channel {run['provider']}`)."
        )
    if execute:
        conn.execute(
            "DELETE FROM run_diffs WHERE from_run_id = ? OR to_run_id = ?", (run_id, run_id)
        )
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        # Re-arm scheduling: NULL last_success_at makes the city due again
        # (get_due_cities orders NULLS FIRST), so a missed re-collect can't
        # hide as a green skip for a whole cycle.
        conn.execute(
            "UPDATE schedule_state SET last_success_at = NULL WHERE city_id = ? AND provider = ?",
            (run["city_id"], run["provider"]),
        )
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--run-date", required=True, help="Run date to scan (YYYY-MM-DD)")
    parser.add_argument("--provider", default=None, help="Restrict to one provider (default: all)")
    parser.add_argument("--data-dir", default=None, help="Data directory (default: ./data)")
    parser.add_argument("--db-path", default=None, help="Catalog DB path (default: in data dir)")
    parser.add_argument(
        "--execute", action="store_true", help="Apply the deletions (default: dry run)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data_dir = args.data_dir or get_default_data_dir()
    db_path = args.db_path or db.get_default_db_path(data_dir)
    conn = db.connect(db_path)
    try:
        items = find_runs_to_purge(conn, data_dir, args.run_date, args.provider)
        if not items:
            logger.info(f"No tainted or orphaned runs on {args.run_date}. Nothing to do.")
            return 0

        mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to apply)"
        logger.info(f"{mode}: {len(items)} run(s) to purge on {args.run_date}\n")
        purged = []
        for item in items:
            run = item["run"]
            if item["reason"] == "tainted":
                pct = 100.0 * item["tainted"] / run["total_points"] if run["total_points"] else 0.0
                logger.info(
                    f"{run['city_id']} [{run['provider']}]: "
                    f"{item['tainted']:,} retryable-status rows "
                    f"of {run['total_points']:,} points ({pct:.1f}%)"
                )
            else:
                logger.info(
                    f"{run['city_id']} [{run['provider']}]: snapshot file MISSING "
                    f"(orphan catalog row blocking re-collection)"
                )
            purge_run(conn, data_dir, run, item["reason"], args.execute)
            purged.append((run["city_id"], run["provider"]))
            logger.info("")

        if args.execute:
            logger.info("Purged and re-armed scheduling. Re-collect each city below with the")
            logger.info(f"SAME --run-date {args.run_date} and --force, then")
            logger.info("regenerate-aggregate --publish:\n")
            for city_id, prov in purged:
                logger.info(f"  {city_id} [{prov}]")
        else:
            logger.info("Pending re-collection (SAME run date, --force) once executed:\n")
            for city_id, prov in purged:
                logger.info(f"  {city_id} [{prov}]")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
