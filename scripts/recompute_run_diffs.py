#!/usr/bin/env python3
"""
Re-derive existing `run_diffs` rows from the two runs' CSVs, under the CURRENT
reader and diff definitions, and make each published detail file match.

The diff twin of `scripts/recompute_run_stats.py`. That script re-derives a
run's stats from its one CSV; a diff is a comparison of TWO artifacts, so it
needs both CSVs and its published detail file rewritten together, and until
this script nothing could do that.

Why it exists (issue #245). Before #244/#226, `fileutils.load_city_csv_file`
parsed `capture_date` with a strict '%Y-%m-%d', so every date in a legacy
MONTH-precision baseline CSV ('2022-09') became NaT. `diff.compute_run_diff`
compares the two sides as strings, so the baseline side was None for every
pano and the collector recorded a capture-date change for EVERY persisted pano.
Two production rows carry that phantom (Amsterdam 71,233 of 71,233 persisted;
Auckland 82,156 of 82,204), along with their published detail CSVs and the
change blocks built from them. The reader is fixed; this is the repair handle.
It is also the operation issue #394 asks for once a coverage definition moves:
`--provider gsv --execute --regenerate-json` re-diffs every GSV series.

What it does, per selected row, exactly as the collector does
(`cli._compute_and_record_diff`):

- SKIPS the row, untouched and reported, when either run's CSV is missing on
  disk or `naming.same_grid_geometry(from_csv, to_csv)` is false (the
  collector's own gate; a cross-geometry comparison is meaningless, and such a
  row is never deleted or "fixed" here).
- Loads both CSVs with `fileutils.load_city_csv_file` and calls
  `diff.compute_run_diff`.
- Leaves the row ENTIRELY untouched (`computed_at` included) when every
  recomputed counter equals the stored one, the stored `detail_filename` is the
  one the result implies, and the detail file's presence on disk matches
  `has_changes`. A second pass is therefore a no-op. The file's CONTENT is not
  compared: it is written before the row is updated, from the same diff, so a
  row whose numbers match was produced together with its file.
- Otherwise, under --execute, (over)writes or removes the detail file through
  `diff.sync_diff_detail` (the collector's own helper, issue #265: a detail
  file exists exactly when the diff has changes), then UPDATEs the row in place
  with `db.update_diff`, which keeps its `diff_id`. `db.record_diff` would not
  do: its INSERT OR REPLACE mints a new `diff_id`, and the published change
  blocks pick the MAX `diff_id` per `to_run`, so re-recording an older
  comparison would change which baseline the site advertises. A stored
  `detail_filename` that differs from the deterministic name (nothing writes
  one) is removed too, and reported.

Scope. Existing `run_diffs` rows only: it never invents a diff for a pair that
has none (a run diffed for the first time is the collector's job, and a pair
the geometry gate skipped has no row by design). --provider and --city select
WHOLE (city, provider) series, processed in run-date order, so a series cannot
end up half under one definition; --diff-id narrows to named rows and is the
operator's explicit choice to recompute part of a series.

Publishing. Under --regenerate-json each CHANGED row's `to_run` per-run JSON
is rebuilt with `json_summarizer.regenerate_run_json`, which replays the
repaired row into its `change_from_previous_run` block. Two other published
artifacts read `run_diffs` and are rebuilt whenever a row changed (skip with
--no-publish-json), the same way `recompute_run_stats.py`'s tail does:
`cities.json.gz` (its per-provider `change` block, `db.get_diff_for_run`) and
`driving_plan.json.gz` (its observation `change` block, `db.get_latest_runs_all`).
The streetwalk manifest reads `street_walk_diffs`, not `run_diffs`, so it is not
rebuilt. Nothing is rsynced: publish afterwards (`scheduler regenerate-aggregate
--publish`, or `./sync_data_to_server.sh`). The publish never passes rsync
`--delete`, so a detail file REMOVED here stays on the web server until removed
there; the script lists those names.

Exit status: 0 when every selected row was recomputed or skipped; 1 when any row
FAILED (an exception while loading or diffing, or a detail file that could not
be removed); 2 for a usage error (an unknown --city or --provider, or a
--db-path that does not exist, which is refused BEFORE connecting because
`db.connect` would create an empty catalog there). Skipped rows (missing CSV,
geometry mismatch) are reported and counted but do not fail the pass: they are
rows this script must not touch, not errors.

Memory. A census provider's CSV is millions of rows (issue #157) and each row
loads two of them. The two known repairs (#245's phantom diffs, #394's re-diff)
are GSV-only, so pass --provider gsv; without it the pass also walks every
Mapillary/KartaView/Panoramax series.

Catalog/disk only: no API calls, no network. Dry run by default; the dry run
performs the full recomputation and prints what WOULD change (old -> new per
counter), writing nothing — no catalog write, no file written or removed, no
JSON.

Usage:
    python scripts/recompute_run_diffs.py --provider gsv                    # dry run
    python scripts/recompute_run_diffs.py --provider gsv \\
        --city amsterdam--north-holland--netherlands --city auckland--auckland--new-zealand
    python scripts/recompute_run_diffs.py --provider gsv --city ... \\
        --execute --regenerate-json                                          # #245's prod repair
    python scripts/recompute_run_diffs.py --diff-id 41 --diff-id 50         # named rows only
    python scripts/recompute_run_diffs.py --data-dir DIR --db-path PATH
"""

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.diff import (  # noqa: E402
    RunDiff,
    compute_run_diff,
    generate_diff_filename,
    sync_diff_detail,
)
from streetscape_metadata_tracker.fileutils import (  # noqa: E402
    load_city_csv_file,
    remove_stale_diff_detail,
)
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    generate_aggregate_v2,
    generate_driving_plan_summary,
    regenerate_run_json,
)
from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS, same_grid_geometry  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("recompute_run_diffs")

# The run_diffs columns compute_run_diff owns, in report order. detail_filename
# is compared separately, since it is derived from has_changes, not counted.
COUNTER_COLUMNS = (
    "grid_aligned",
    "panos_added",
    "panos_removed",
    "panos_persisted",
    "capture_date_changed",
    "points_gained_coverage",
    "points_lost_coverage",
    "coverage_delta_pct",
)

_SELECT = """
    SELECT d.diff_id, d.city_id, d.from_run_id, d.to_run_id, d.detail_filename,
           d.computed_at, {counters},
           f.provider AS from_provider, f.run_date AS from_date, f.csv_filename AS from_csv,
           t.provider AS provider, t.run_date AS to_date, t.csv_filename AS to_csv
    FROM run_diffs d
    JOIN runs f ON f.run_id = d.from_run_id
    JOIN runs t ON t.run_id = d.to_run_id
""".format(counters=", ".join(f"d.{c}" for c in COUNTER_COLUMNS))


@dataclass
class Report:
    """What one pass found. Each list holds (diff_id, one-line description)."""

    scanned: int = 0
    unchanged: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    removed_files: list = field(default_factory=list)  # names to remove from the web server
    changed_to_run_ids: list = field(default_factory=list)


def _equalish(a, b) -> bool:
    """Stored vs recomputed, tolerant of float noise and None (as recompute_run_stats)."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-9)
    return a == b


def _recomputed_values(diff: RunDiff) -> dict:
    return {
        "grid_aligned": int(diff.grid_aligned),
        "panos_added": diff.panos_added,
        "panos_removed": diff.panos_removed,
        "panos_persisted": diff.panos_persisted,
        "capture_date_changed": diff.capture_date_changed,
        "points_gained_coverage": diff.points_gained_coverage,
        "points_lost_coverage": diff.points_lost_coverage,
        "coverage_delta_pct": diff.coverage_delta_pct,
    }


def select_rows(conn, providers, city_ids, diff_ids) -> list:
    """The run_diffs rows in scope, ordered so each (city, provider) series is
    processed oldest comparison first. Empty filters select everything."""
    clauses, params = [], []
    for column, values in (("t.provider", providers), ("d.city_id", city_ids)):
        if values:
            clauses.append(f"{column} IN ({', '.join('?' * len(values))})")
            params.extend(values)
    if diff_ids:
        clauses.append(f"d.diff_id IN ({', '.join('?' * len(diff_ids))})")
        params.extend(diff_ids)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"{_SELECT} {where} ORDER BY d.city_id, t.provider, t.run_date, f.run_date, d.diff_id",
        params,
    ).fetchall()


def recompute_row(conn, row, data_dir: str, execute: bool, report: Report) -> None:
    """Recompute one run_diffs row and, when ``execute``, apply it. Never raises
    for a per-row problem: it is reported and the pass continues."""
    label = (
        f"diff {row['diff_id']} {row['city_id']} [{row['provider']}] "
        f"{row['from_date']} -> {row['to_date']}"
    )
    from_path = os.path.join(data_dir, row["from_csv"])
    to_path = os.path.join(data_dir, row["to_csv"])

    # The collector's gates. A row failing one is left exactly as it is: its comparison cannot be re-derived, which says nothing about
    # whether the stored one is wrong.
    missing = [
        p for p in (row["from_csv"], row["to_csv"]) if not os.path.exists(os.path.join(data_dir, p))
    ]
    if missing:
        report.skipped.append((row["diff_id"], f"{label}: CSV missing ({', '.join(missing)})"))
        return
    if row["from_provider"] != row["provider"]:
        # Nothing writes such a row; a cross-provider pair is not a series diff.
        report.skipped.append(
            (row["diff_id"], f"{label}: from-run provider is {row['from_provider']}")
        )
        return
    if not same_grid_geometry(row["from_csv"], row["to_csv"]):
        report.skipped.append((row["diff_id"], f"{label}: grid geometry differs, not re-diffed"))
        return

    try:
        diff = compute_run_diff(load_city_csv_file(from_path), load_city_csv_file(to_path))
    except Exception as exc:  # one unreadable pair must not end the pass
        logger.exception(f"{label}: recompute failed")
        report.failed.append((row["diff_id"], f"{label}: {type(exc).__name__}: {exc}"))
        return

    detail_name = generate_diff_filename(
        row["city_id"], row["from_date"], row["to_date"], provider=row["provider"]
    )
    expected_detail = detail_name if diff.has_changes else None
    detail_on_disk = os.path.exists(os.path.join(data_dir, detail_name))
    stored_detail = row["detail_filename"]
    # A stored pointer that is not the deterministic name: nothing writes one,
    # but if it exists it is a published file this row vouched for.
    stray_detail = stored_detail if stored_detail not in (None, detail_name) else None

    new_values = _recomputed_values(diff)
    moved = {
        c: (row[c], new_values[c]) for c in COUNTER_COLUMNS if not _equalish(row[c], new_values[c])
    }
    if (
        not moved
        and stored_detail == expected_detail
        and detail_on_disk == diff.has_changes
        and stray_detail is None
    ):
        report.unchanged.append((row["diff_id"], label))
        return

    notes = [f"{c} {old} -> {new}" for c, (old, new) in moved.items()]
    if diff.has_changes:
        notes.append(f"detail file {'rewrite' if detail_on_disk else 'write'} {detail_name}")
    elif detail_on_disk:
        notes.append(f"detail file remove {detail_name}")
    if stored_detail != expected_detail:
        notes.append(f"detail_filename {stored_detail} -> {expected_detail}")
    if stray_detail is not None:
        notes.append(f"stray stored detail file remove {stray_detail}")
    line = f"{label}: {'; '.join(notes)}"

    if not execute:
        report.changed.append((row["diff_id"], line))
        return

    # File first, row second: a crash between the two leaves a row whose numbers
    # still disagree with the recomputation (or, when only the file was wrong, a
    # file that is now right), so the next pass redoes or skips it correctly.
    detail_filename = sync_diff_detail(diff, data_dir, detail_name)
    to_remove = []  # files this row must no longer have on disk
    if not diff.has_changes and detail_on_disk:
        to_remove.append(detail_name)  # sync_diff_detail just removed it
    if stray_detail is not None:
        remove_stale_diff_detail(data_dir, stray_detail)
        to_remove.append(stray_detail)
    # remove_stale_diff_detail never raises, so the disk is the only witness.
    still_there = [n for n in to_remove if os.path.exists(os.path.join(data_dir, n))]
    report.removed_files.extend(n for n in to_remove if n not in still_there)

    db.update_diff(
        conn,
        row["diff_id"],
        detail_filename=detail_filename,
        **{c: new_values[c] for c in COUNTER_COLUMNS},
    )
    report.changed.append((row["diff_id"], line))
    report.changed_to_run_ids.append(row["to_run_id"])
    if still_there:
        # The row is right (it records no file); the file is now an orphan the
        # sweep can find. Counted as a failure so the pass exits nonzero.
        report.failed.append(
            (row["diff_id"], f"{label}: could not remove {', '.join(still_there)}")
        )


def _parse_list(values) -> list[str]:
    """Repeatable AND comma-separated, as `run-due --provider` takes it."""
    out = []
    for value in values or ():
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--db-path", default=None, help="default: {data-dir}/streetscape_tracker.db"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Apply changes (default is a dry-run report)"
    )
    parser.add_argument(
        "--provider",
        action="append",
        help="Restrict to these providers' series (repeatable or comma-separated; "
        f"one of {', '.join(KNOWN_PROVIDERS)}). Use gsv for the known repairs: a "
        "census CSV is millions of rows and every row loads two.",
    )
    parser.add_argument(
        "--city",
        action="append",
        help="Restrict to these cities' series (repeatable; a city_id or a query "
        "db.resolve_city understands). Always the whole series.",
    )
    parser.add_argument(
        "--diff-id",
        action="append",
        type=int,
        help="Restrict to these run_diffs rows (repeatable). The one filter that "
        "can recompute PART of a series — an explicit choice.",
    )
    parser.add_argument(
        "--regenerate-json",
        action="store_true",
        help="Also rebuild the per-run JSON of each changed row's to-run, whose "
        "change block replays the row",
    )
    parser.add_argument(
        "--no-publish-json",
        action="store_true",
        help="Skip rebuilding cities.json.gz and driving_plan.json.gz after a "
        "pass that changed rows (catalog and detail files only)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    providers = _parse_list(args.provider)
    unknown = sorted(set(providers) - set(KNOWN_PROVIDERS))
    if unknown:
        parser.error(
            f"unknown --provider {', '.join(unknown)}; known: {', '.join(KNOWN_PROVIDERS)}"
        )

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    # Checked before db.connect, which would CREATE an empty catalog at a wrong
    # path and then report "nothing to recompute" as if that were an answer.
    if not os.path.exists(db_path):
        logger.error(f"No catalog at {db_path}; refusing to create one")
        return 2
    conn = db.connect(db_path)
    try:
        city_ids = []
        for query in args.city or ():
            city = db.resolve_city(conn, query)
            if city is None:
                parser.error(f"unknown --city {query!r}")
            city_ids.append(city.city_id)

        rows = select_rows(conn, providers, city_ids, args.diff_id or [])
        report = Report(scanned=len(rows))
        for row in rows:
            recompute_row(conn, row, args.data_dir, args.execute, report)

        mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to apply)"
        print(f"{mode}: {args.data_dir}")
        for _, line in report.changed:
            print(f"  {'changed' if args.execute else 'would change'}: {line}")
        for _, line in report.skipped:
            print(f"  skipped: {line}")
        for _, line in report.failed:
            print(f"  FAILED: {line}")
        print(
            f"\n{report.scanned} diffs scanned, {len(report.unchanged)} unchanged, "
            f"{len(report.changed)} {'changed' if args.execute else 'would change'}, "
            f"{len(report.skipped)} skipped, {len(report.failed)} failed"
        )

        if not args.execute:
            if report.changed:
                print("Dry run complete. Re-run with --execute to apply.")
                if not args.regenerate_json:
                    # Said BEFORE the execute, because afterwards it is too late
                    # to ask: a second pass finds nothing left to change, so it
                    # rebuilds no JSON either.
                    print(
                        "Without --regenerate-json the per-run JSONs of these rows' "
                        "to-runs keep their old change blocks; pass it WITH --execute."
                    )
            return 1 if report.failed else 0

        if args.regenerate_json:
            rebuilt = 0
            # A to-run is rebuilt once even if two of its rows changed.
            to_runs = list(dict.fromkeys(report.changed_to_run_ids))
            for run_id in to_runs:
                if regenerate_run_json(conn, run_id, args.data_dir) is None:
                    logger.warning(f"Could not rebuild the per-run JSON of run {run_id}")
                else:
                    rebuilt += 1
            print(f"Rebuilt {rebuilt} of {len(to_runs)} per-run JSON summaries.")
        elif report.changed:
            # A re-run cannot fix this (it finds nothing left to change), so name
            # the runs whose published change block is now stale.
            stale = ", ".join(str(r) for r in dict.fromkeys(report.changed_to_run_ids))
            print(
                "Per-run JSONs NOT rebuilt (no --regenerate-json): the change blocks of "
                f"run_id {stale} still hold the old numbers until each city's next "
                "collection, or json_summarizer.regenerate_run_json(conn, run_id, data_dir)."
            )
        if report.changed and not args.no_publish_json:
            generate_aggregate_v2(conn, args.data_dir)
            print(f"Regenerated aggregate: {os.path.join(args.data_dir, 'cities.json.gz')}")
            # Guarded like the scheduler's tail and recompute_run_stats.py: the
            # catalog repair is already committed and must not look failed.
            try:
                generate_driving_plan_summary(conn, args.data_dir)
                print(
                    "Regenerated driving plan: "
                    f"{os.path.join(args.data_dir, 'driving_plan.json.gz')}"
                )
            except Exception:
                logger.exception(
                    "Driving-plan summary failed; the diff repair is applied, but "
                    "driving.html keeps the old change block until "
                    "`scheduler regenerate-aggregate` or the next run-due rebuilds it"
                )
        if report.changed:
            print(
                "Nothing was published. Publish with `python -m "
                "streetscape_metadata_tracker.scheduler regenerate-aggregate --publish` "
                "or ./sync_data_to_server.sh."
            )
        if report.removed_files:
            print(
                "Removed detail files stay on the web server (the publish never passes "
                "rsync --delete); remove these there:"
            )
            for name in report.removed_files:
                print(f"  {name}")
        return 1 if report.failed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
