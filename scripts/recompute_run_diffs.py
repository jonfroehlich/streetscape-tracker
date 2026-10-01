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
  one the result implies, the detail file's presence on disk matches
  `has_changes`, and a present file's CONTENT is what this recomputation would
  write. The content is compared because a row written under an older reader
  can carry the same counts as the current one while its rows differ (a
  month-precision baseline whose panos were all replaced: no persisted pano, so
  no date comparison, but every removed pano's `old_capture_date` was blank).
- Otherwise, under --execute, (over)writes or removes the detail file through
  `diff.sync_diff_detail` (the collector's own helper, issue #265: a detail
  file exists exactly when the diff has changes), then UPDATEs the row in place
  with `db.update_diff`, which keeps its `diff_id` and refreshes `computed_at`.
  `db.record_diff` would not do: its INSERT OR REPLACE mints a new `diff_id`,
  and the published change blocks pick the MAX `diff_id` per `to_run`, so
  re-recording an older comparison would change which baseline the site
  advertises.

Deletion is guarded, because `data/` holds paid-for observations no later run
can backfill. The deterministic detail name is unique to its row by
construction (it encodes the city, provider and both run dates, and a run date
is UNIQUE within a series), so another row naming it means a corrupt pointer:
the row FAILS, untouched. A stored `detail_filename` that is NOT the
deterministic name (nothing writes one) is removed only when it is a bare,
diff-detail-shaped name (`diff.is_diff_detail_filename`) that no OTHER row of
either diff table names; otherwise the file is left alone, the row is still
repointed, and the pass reports it and exits 1.

Scope. Existing `run_diffs` rows only: it never invents a diff for a pair that
has none (a run diffed for the first time is the collector's job, and a pair
the geometry gate skipped has no row by design). --provider and --city select
WHOLE (city, provider) series; --diff-id narrows to named rows and is the
operator's explicit choice to recompute part of a series. Rows are processed
grouped by series, oldest comparison first, which only makes the report
readable: each row is independent, so what keeps a series under one definition
is the selection, and a row that fails or a pass that aborts leaves the series
half repaired until a re-run finishes it.

Publishing. Three published artifacts read `run_diffs`:

- the per-run JSON's `change_from_previous_run` block, which
  `json_summarizer.regenerate_run_json` replays from the row
  `db.get_diff_for_run` returns (the newest `diff_id` into that run);
- `cities.json.gz`'s per-provider `change` block (`db.get_diff_for_run`);
- `driving_plan.json.gz`'s observation `change` block (`db.get_latest_runs_all`).

With --regenerate-json, EVERY in-scope row's to-run JSON is compared with what
the replay would publish now, right after that row is handled, and rebuilt when
they disagree or the JSON is missing. So the per-run JSON is part of what
"unchanged" means: a re-run heals a JSON left stale by an aborted pass, or by an
--execute that forgot the flag, and a JSON that already agrees is not rewritten.
`cities.json.gz` and `driving_plan.json.gz` are rebuilt at the end of an
--execute pass that changed a row or rebuilt a JSON (skip with
--no-publish-json), the same way `recompute_run_stats.py`'s tail does. The
streetwalk manifest reads `street_walk_diffs`, not `run_diffs`, so it is not
rebuilt.

What a crashed pass leaves stale, and what heals it. Each row is committed
before the next is started, its file written before its row, and (with
--regenerate-json) its JSON rebuilt right after. A crash therefore leaves at
most one row half done, which a re-run redoes, and the two catalog-wide
artifacts unbuilt: re-run with the same arguments plus --execute
--regenerate-json, then `scheduler regenerate-aggregate`, since a re-run that
finds nothing left to change does not rebuild them.

Nothing is rsynced: publish afterwards (`scheduler regenerate-aggregate
--publish`, or `./sync_data_to_server.sh`). The publish never passes rsync
`--delete`, so a detail file REMOVED here stays on the web server until removed
there; the script lists those names.

Concurrency. --execute is refused while a `run-due` is in flight on this
machine (`scheduler._run_due_in_flight`, as the orphan sweep, `import-bundle`
and the prefreeze script do): the nightly tail writes `cities.json.gz` through
the same fixed temp name, and the two would contend for the catalog's write
lock. Run it in the daytime. A dry run proceeds and says a batch is running.

The catalog. A dry run opens it the orphan sweep's read-only way
(`open_catalog_readonly`: `mode=rw` plus `PRAGMA query_only`, never
`db.connect`, which switches journal mode and migrates), so it leaves the
catalog byte-identical with no `-wal`/`-shm` behind. Both modes REFUSE a
catalog that is missing, of another schema version, or holds no runs or walks,
rather than creating or migrating it; --execute then opens it with `db.connect`,
which at the current version migrates nothing.

Exit status: 0 when every selected row was recomputed, left alone or skipped;
1 when a row FAILED (an exception while loading, diffing, writing the file,
updating the row or rebuilding its JSON; a file that could not be removed; a
deterministic name another row claims) or a stray file was kept; 2 for an
argument error (argparse, or an unknown --provider); 64 for a refusal (the
catalog as above, an unknown --city, or --execute while `run-due` is in
flight). Skipped rows (missing CSV, geometry mismatch) are reported and counted
but do not fail the pass: they are rows this script must not touch, not errors.

Memory. A census provider's CSV is millions of rows (issue #157) and each row
loads two of them. The two known repairs (#245's phantom diffs, #394's re-diff)
are GSV-only, so pass --provider gsv; without it the pass also walks every
Mapillary/KartaView/Panoramax series.

Catalog/disk only: no API calls, no network. Dry run by default; the dry run
performs the full recomputation and prints what WOULD change (old -> new per
counter) and, with --regenerate-json, which per-run JSONs already disagree with
the catalog, writing nothing.

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
import gzip
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The orphan sweep owns the read-only catalog open and its refusal; reusing it
# keeps the two scripts' notion of "a catalog we may judge" identical.
from scripts.sweep_orphan_diff_details import (  # noqa: E402
    CatalogRefused,
    open_catalog_readonly,
)
from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.diff import (  # noqa: E402
    RunDiff,
    compute_run_diff,
    generate_diff_filename,
    is_diff_detail_filename,
    sync_diff_detail,
)
from streetscape_metadata_tracker.fileutils import (  # noqa: E402
    load_city_csv_file,
    remove_stale_diff_detail,
)

# _replay_change_block is the exact function regenerate_run_json publishes
# through, so comparing against it cannot drift from what a rebuild would write.
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    _replay_change_block,
    generate_aggregate_v2,
    generate_driving_plan_summary,
    regenerate_run_json,
    sanitize_for_json,
)
from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS, same_grid_geometry  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_metadata_tracker.scheduler import (  # noqa: E402
    USAGE_EXIT_CODE,
    _run_due_in_flight,
)

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
    """What one pass found. The row lists hold (diff_id, one-line description)."""

    scanned: int = 0
    unchanged: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    kept_strays: list = field(default_factory=list)  # stray pointers whose file was left
    removed_files: list = field(default_factory=list)  # names to remove from the web server
    json_stale: list = field(default_factory=list)  # to_run_ids whose JSON disagreed
    json_rebuilt: list = field(default_factory=list)  # to_run_ids rebuilt this pass


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


def _detail_matches(path: str, diff: RunDiff) -> bool:
    """Whether the detail file at ``path`` holds exactly what
    ``diff.write_diff_detail`` would write for ``diff``. Compared as the
    decompressed text, not the bytes: the gzip header carries a write time."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            on_disk = fh.read()
    except (OSError, EOFError, UnicodeDecodeError):
        return False  # unreadable is stale; the rewrite fixes it
    return on_disk == diff.detail.to_csv(index=False)


def _referenced_elsewhere(conn, name: str, diff_id: int) -> bool:
    """Whether any diff row OTHER than ``diff_id`` names ``name``, in either table."""
    if conn.execute(
        "SELECT 1 FROM run_diffs WHERE detail_filename = ? AND diff_id != ? LIMIT 1",
        (name, diff_id),
    ).fetchone():
        return True
    return (
        conn.execute(
            "SELECT 1 FROM street_walk_diffs WHERE detail_filename = ? LIMIT 1", (name,)
        ).fetchone()
        is not None
    )


def select_rows(conn, providers, city_ids, diff_ids) -> list:
    """The run_diffs rows in scope, grouped by (city, provider) series and oldest
    comparison first (for a readable report). Empty filters select everything."""
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


def heal_run_json(conn, to_run_id: int, data_dir: str, execute: bool, report: Report) -> None:
    """
    Compare a run's published per-run JSON change block with what
    ``regenerate_run_json`` would publish NOW, and (under ``execute``) rebuild
    it when they disagree or the JSON is missing.

    The expected block comes from the replay itself, so a run with two diff rows
    is compared against the row the replay picks (the newest ``diff_id``), not
    against whichever row the caller is holding. Raises on a failed rebuild; the
    caller's per-row handling counts it.
    """
    expected = sanitize_for_json(_replay_change_block(conn, to_run_id))
    row = conn.execute("SELECT json_filename FROM runs WHERE run_id = ?", (to_run_id,)).fetchone()
    published, present = None, False
    if row is not None and row["json_filename"]:
        path = os.path.join(data_dir, row["json_filename"])
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                published = json.load(fh).get("change_from_previous_run")
            present = True
        except (OSError, EOFError, ValueError):
            present = False  # missing or unreadable: rebuild
    if present and published == expected:
        return
    report.json_stale.append(to_run_id)
    if not execute:
        return
    if regenerate_run_json(conn, to_run_id, data_dir) is None:
        raise RuntimeError(f"could not rebuild the per-run JSON of run {to_run_id}")
    report.json_rebuilt.append(to_run_id)


def recompute_row(
    conn, row, data_dir: str, execute: bool, regenerate_json: bool, report: Report
) -> None:
    """Recompute one run_diffs row and, when ``execute``, apply it. Never raises
    for a per-row problem: every step after the gates is inside one handler, so
    a failure is reported and counted and the pass continues."""
    label = (
        f"diff {row['diff_id']} {row['city_id']} [{row['provider']}] "
        f"{row['from_date']} -> {row['to_date']}"
    )

    # The collector's gates. A row failing one is left exactly as it is: its
    # comparison cannot be re-derived, which says nothing about whether the
    # stored one is wrong.
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
        _recompute_and_apply(conn, row, label, data_dir, execute, report)
        if regenerate_json:
            heal_run_json(conn, row["to_run_id"], data_dir, execute, report)
    except Exception as exc:  # one bad row must not end a multi-hour pass
        logger.exception(f"{label}: failed")
        report.failed.append((row["diff_id"], f"{label}: {type(exc).__name__}: {exc}"))


def _recompute_and_apply(conn, row, label: str, data_dir: str, execute: bool, report: Report):
    diff_id = row["diff_id"]
    diff = compute_run_diff(
        load_city_csv_file(os.path.join(data_dir, row["from_csv"])),
        load_city_csv_file(os.path.join(data_dir, row["to_csv"])),
    )

    detail_name = generate_diff_filename(
        row["city_id"], row["from_date"], row["to_date"], provider=row["provider"]
    )
    if _referenced_elsewhere(conn, detail_name, diff_id):
        # Unique to this row by construction (city, provider and both run dates,
        # with run dates UNIQUE in a series), so another row naming it is a
        # corrupt pointer. Writing or removing the file would act on that row's
        # behalf; leave both alone.
        report.failed.append(
            (diff_id, f"{label}: {detail_name} is named by another diff row; left untouched")
        )
        return

    detail_path = os.path.join(data_dir, detail_name)
    expected_detail = detail_name if diff.has_changes else None
    detail_on_disk = os.path.isfile(detail_path)
    content_stale = diff.has_changes and detail_on_disk and not _detail_matches(detail_path, diff)
    stored_detail = row["detail_filename"]
    # A stored pointer that is not the deterministic name: nothing writes one.
    stray_detail = stored_detail if stored_detail not in (None, detail_name) else None

    new_values = _recomputed_values(diff)
    moved = {
        c: (row[c], new_values[c]) for c in COUNTER_COLUMNS if not _equalish(row[c], new_values[c])
    }
    if (
        not moved
        and stored_detail == expected_detail
        and detail_on_disk == diff.has_changes
        and not content_stale
        and stray_detail is None
    ):
        report.unchanged.append((diff_id, label))
        return

    notes = [f"{c} {old} -> {new}" for c, (old, new) in moved.items()]
    if diff.has_changes and not detail_on_disk:
        notes.append(f"detail file write {detail_name}")
    elif content_stale:
        notes.append(f"detail file content stale, rewrite {detail_name}")
    elif diff.has_changes:
        notes.append(f"detail file rewrite {detail_name}")
    elif detail_on_disk:
        notes.append(f"detail file remove {detail_name}")
    if stored_detail != expected_detail:
        notes.append(f"detail_filename {stored_detail} -> {expected_detail}")
    if stray_detail is not None:
        notes.append(f"stray stored detail {stray_detail}")
    line = f"{label}: {'; '.join(notes)}"

    if not execute:
        report.changed.append((diff_id, line))
        return

    # File first, row second: a crash between the two leaves a row whose numbers
    # still disagree with the recomputation (or, when only the file was wrong, a
    # file that is now right), so the next pass redoes or skips it correctly.
    detail_filename = sync_diff_detail(diff, data_dir, detail_name)
    to_remove = []  # files this row must no longer have on disk
    if not diff.has_changes and detail_on_disk:
        to_remove.append(detail_name)  # sync_diff_detail just removed it
    if stray_detail is not None:
        if (
            os.path.basename(stray_detail) == stray_detail
            and is_diff_detail_filename(stray_detail)
            and not _referenced_elsewhere(conn, stray_detail, diff_id)
        ):
            remove_stale_diff_detail(data_dir, stray_detail)
            to_remove.append(stray_detail)
        else:
            # Not ours to delete: a run CSV, another row's detail, a path. The
            # row is still repointed below; the file stays and the pass says so.
            report.kept_strays.append(
                (
                    diff_id,
                    f"{label}: stored detail_filename {stray_detail!r} is not a diff "
                    "detail of this row's alone; the row was repointed and the file LEFT",
                )
            )
    # remove_stale_diff_detail never raises, so the disk is the only witness.
    still_there = [n for n in to_remove if os.path.exists(os.path.join(data_dir, n))]
    report.removed_files.extend(n for n in to_remove if n not in still_there)

    db.update_diff(
        conn,
        diff_id,
        detail_filename=detail_filename,
        **{c: new_values[c] for c in COUNTER_COLUMNS},
    )
    report.changed.append((diff_id, line))
    if still_there:
        # The row is right (it records no file); the file is now an orphan the
        # sweep can find. Counted as a failure so the pass exits nonzero.
        report.failed.append((diff_id, f"{label}: could not remove {', '.join(still_there)}"))


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
        help="Also compare every in-scope row's to-run per-run JSON with the catalog "
        "and rebuild it when they disagree (heals a JSON an earlier pass left stale)",
    )
    parser.add_argument(
        "--no-publish-json",
        action="store_true",
        help="Skip rebuilding cities.json.gz and driving_plan.json.gz after a "
        "pass that changed something (catalog, detail files and per-run JSON only)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    providers = _parse_list(args.provider)
    unknown = sorted(set(providers) - set(KNOWN_PROVIDERS))
    if unknown:
        parser.error(
            f"unknown --provider {', '.join(unknown)}; known: {', '.join(KNOWN_PROVIDERS)}"
        )

    in_flight = _run_due_in_flight()
    if in_flight:
        if args.execute:
            logger.error(
                f"A run-due is in flight on this machine ({in_flight}); refusing --execute "
                "while it can be writing diffs and the published JSON. Wait for the night "
                "to finish."
            )
            return USAGE_EXIT_CODE
        logger.warning(f"A run-due is in flight on this machine ({in_flight}); dry run only.")

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    try:
        conn = open_catalog_readonly(db_path)
    except CatalogRefused as exc:
        logger.error(f"Refusing: {exc}")
        return USAGE_EXIT_CODE
    if args.execute:
        # Validated above (exists, this code's schema version, not empty), so
        # db.connect's init_schema has nothing to migrate.
        conn.close()
        conn = db.connect(db_path)
    try:
        city_ids = []
        for query in args.city or ():
            city = db.resolve_city(conn, query)
            if city is None:
                logger.error(f"Refusing: unknown --city {query!r}")
                return USAGE_EXIT_CODE
            city_ids.append(city.city_id)

        rows = select_rows(conn, providers, city_ids, args.diff_id or [])
        report = Report(scanned=len(rows))
        for row in rows:
            recompute_row(conn, row, args.data_dir, args.execute, args.regenerate_json, report)

        mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to apply)"
        verb = "changed" if args.execute else "would change"
        print(f"{mode}: {args.data_dir}")
        for _, line in report.changed:
            print(f"  {verb}: {line}")
        for _, line in report.skipped:
            print(f"  skipped: {line}")
        for _, line in report.kept_strays:
            print(f"  KEPT, NOT DELETED: {line}")
        for _, line in report.failed:
            print(f"  FAILED: {line}")
        print(
            f"\n{report.scanned} diffs scanned, {len(report.unchanged)} unchanged, "
            f"{len(report.changed)} {verb}, {len(report.skipped)} skipped, "
            f"{len(report.failed)} failed"
        )
        if args.regenerate_json:
            stale = sorted(set(report.json_stale))
            if args.execute:
                print(f"Rebuilt {len(set(report.json_rebuilt))} per-run JSON summaries.")
            else:
                print(
                    f"{len(stale)} per-run JSON(s) already disagree with the catalog and "
                    "would be rebuilt (rows this pass changes are rebuilt after them too)."
                )
        elif report.changed:
            print(
                "Per-run JSONs not checked (no --regenerate-json): the change blocks of "
                "the to-runs above keep their old numbers until a pass with "
                "--execute --regenerate-json heals them."
            )

        exit_code = 1 if (report.failed or report.kept_strays) else 0
        if not args.execute:
            if report.changed:
                print("Dry run complete. Re-run with --execute to apply.")
            return exit_code

        if (report.changed or report.json_rebuilt) and not args.no_publish_json:
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
        if report.changed or report.json_rebuilt:
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
        return exit_code
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
