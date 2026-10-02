#!/usr/bin/env python3
"""
Re-derive every road walk's stored stats from its snapshot CSV and its FROZEN
street network, under the CURRENT coverage definition (issue #262).

The walk twin of `scripts/recompute_run_stats.py`. Walk coverage is computed at
collection time into `street_walks` rows and the published
`*_coverage.json.gz` artifact, so a definition change (#257 made a `NO_DATE`
sample count as covered) leaves every older walk under the old definition --
and each series' next walk diff reporting a one-time phantom coverage delta.
This is the handle that removes the phantom instead of explaining it.

Why it cannot read the artifact. The coverage GeoJSON holds per-edge aggregates
ALREADY computed under the old definition; the dropped samples are not in it,
so no arithmetic on it recovers them (unlike the two `backfill_streetwalk_*`
scripts, which copy figures out of it). The recompute goes back to the two
inputs `collect.py` had -- the snapshot CSV, read through the same
`fileutils.load_city_csv_file` the collectors read it back with, and the frozen
GraphML -- and calls the collector's own functions: `generate_samples`,
`compute_streetwalk_coverage`, `build_streetwalk_geojson`. `spacing_m`,
`match_dist_m`, `run_date`, `provider` and `network_type` come off the row.

What makes a walk REFUSED (and its whole series skipped, untouched):

- **No frozen GraphML on disk.** The network is loaded with `ox.load_graphml`
  from `naming.network_cache_path` and from nowhere else. This script never
  calls `download_street_network.fetch_graph`, which falls through to Overpass
  on a cache miss -- the per-IP volunteer service that banned makelab2. A
  sweep over a few hundred walks must not make even one request.
- **The regenerated sample frame is not the CSV's.** `street_networks` is
  UNIQUE per (city, network_type), so a `--refresh` overwrote the network in
  place and the graph a walk was collected on may be gone. Rather than reason
  about `fetched_at`, the frame is validated directly: `quantize_coord` over the
  regenerated samples must give EXACTLY the CSV's key set -- same count, same
  keys, no duplicate CSV rows -- with `street_walks.sample_points` as the cheap
  pre-check. Scoring a walk against a frame it never observed would be silent.
- A missing snapshot CSV, a NULL `spacing_m`/`match_dist_m`/`coverage_filename`
  (nothing to reproduce the collection with, or nowhere to publish it), or any
  exception while loading or recomputing.

**Whole series in one pass.** Per (city, provider, network_type) every walk is
recomputed in memory first; one refusal skips the series, so a city's walk
history never mixes two definitions. A refused series is reported by name and
makes the pass exit 1.

What --execute writes, per series that moved:

1. With --regenerate-artifacts, each stale `*_coverage.json.gz` (written to a
   temp name and `os.replace`d, so a crash never leaves a truncated artifact).
2. The `street_walks` stat columns, in ONE transaction for the series:
   edges_total, edges_fully_covered, mean_edge_coverage,
   coverage_pct_by_length, coverage_pct_by_length_any, coverage_by_highway,
   length_km, length_km_covered, length_km_covered_any,
   median_covered_age_years.
3. With --regenerate-artifacts, every EXISTING `street_walk_diffs` row of the
   series whose recomputation disagrees with what is stored (counters,
   `from_walk_id`, detail pointer, detail file presence or content), re-diffed
   through the collector's own `walk_diff.compute_and_record_walk_diff`, which
   since #265 removes a detail file a no-changes diff no longer has. A walk
   with no diff row gets none: inventing a diff is the collector's job.
   Without the flag the artifacts on disk are still old-definition, which is
   what a diff reads, so stale diffs are reported and left alone.

Then `streetwalks.json.gz` is regenerated (its headline stats AND its `change`
block both read what this pass changed), guarded so a manifest failure reports
rather than making a committed repair look failed.

The snapshot CSV is never rewritten: it records what the provider said.

Idempotent: a series whose rows, artifacts and diffs already agree with the
recomputation is untouched, and a re-run heals a pass that crashed between
steps (rows and diffs are compared against the in-memory recomputation, not
against whether an artifact was just written).

Nothing is rsynced: publish afterwards (`scheduler regenerate-aggregate
--publish`, or `./sync_data_to_server.sh`). The publish never passes rsync
`--delete`, so a walk diff detail file REMOVED here stays on the web server
until removed there; the script lists those names.

Concurrency, the catalog, and exit status follow `recompute_run_diffs.py`:
--execute is refused while a `run-due` is in flight on this machine; a dry run
opens the catalog read-only (`open_catalog_readonly`) and writes nothing; a
missing, other-version or empty catalog is refused. Exit 0 when every series
was recomputed or left alone; 1 when a series was refused or a step failed; 2
for an argument error; 64 for a refusal (catalog, unknown --city, in flight).

Catalog/disk only: no API calls, no Overpass, no network of any kind.

Usage:
    python scripts/recompute_streetwalk_stats.py                       # dry run
    python scripts/recompute_streetwalk_stats.py --provider mapillary  # filter
    python scripts/recompute_streetwalk_stats.py --execute             # catalog only
    python scripts/recompute_streetwalk_stats.py --regenerate-artifacts --execute
"""

import argparse
import gzip
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date

import osmnx as ox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.sweep_orphan_diff_details import (  # noqa: E402
    CatalogRefused,
    open_catalog_readonly,
)
from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    generate_streetwalk_manifest,
)
from streetscape_metadata_tracker.naming import (  # noqa: E402
    KNOWN_PROVIDERS,
    generate_streetwalk_diff_filename,
    network_cache_path,
)
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_metadata_tracker.scheduler import (  # noqa: E402
    USAGE_EXIT_CODE,
    _run_due_in_flight,
)
from streetscape_metadata_tracker.walk_diff import (  # noqa: E402
    compute_and_record_walk_diff,
    compute_walk_diff,
)

# graph_to_edges is the collector's own flattening; only the LOAD differs (the
# collector's fetch_graph may go to Overpass, this script never may).
from streetscape_street_analyzer.download_street_network import graph_to_edges  # noqa: E402
from streetscape_street_analyzer.road_sampling import (  # noqa: E402
    generate_samples,
    quantize_coord,
)
from streetscape_street_analyzer.street_coverage import (  # noqa: E402
    build_streetwalk_geojson,
    compute_streetwalk_coverage,
)

logger = logging.getLogger("recompute_streetwalk_stats")

# The street_walks columns collect.py writes from the artifact's summary, in
# report order. coverage_by_highway is stored as json.dumps of a dict.
STAT_COLUMNS = (
    "edges_total",
    "edges_fully_covered",
    "mean_edge_coverage",
    "coverage_pct_by_length",
    "coverage_pct_by_length_any",
    "coverage_by_highway",
    "length_km",
    "length_km_covered",
    "length_km_covered_any",
    "median_covered_age_years",
)

# The street_walk_diffs columns compute_walk_diff owns (detail_filename is
# compared separately: it is derived from has_changes, not counted).
DIFF_COLUMNS = (
    "edges_aligned",
    "edges_added",
    "edges_removed",
    "edges_gained_coverage",
    "edges_lost_coverage",
    "coverage_fraction_changed",
    "nearest_pano_date_changed",
    "edges_fully_covered_delta",
    "coverage_pct_by_length_delta",
    "coverage_pct_by_length_any_delta",
)


class WalkRefused(Exception):
    """This walk cannot be recomputed faithfully; its whole series is skipped."""


@dataclass
class Report:
    """What one pass found. Each list holds one-line descriptions."""

    series_scanned: int = 0
    walks_scanned: int = 0
    unchanged_series: int = 0
    changed_walks: list = field(default_factory=list)
    changed_diffs: list = field(default_factory=list)
    stale_diffs_left: list = field(default_factory=list)  # no --regenerate-artifacts
    refused: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    artifacts_written: int = 0
    rows_updated: int = 0
    removed_files: list = field(default_factory=list)


@dataclass
class Recomputed:
    """One walk's recomputation, held in memory until its whole series passes."""

    row: object
    geojson: dict  # JSON-normalized (lists, not tuples), as it reads back from disk
    stats: dict
    moved: dict  # column -> (stored, recomputed)
    artifact_stale: bool


def _equalish(a, b) -> bool:
    """Stored vs recomputed, tolerant of float noise and None (as recompute_run_stats)."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-9)
    return a == b


def _label(row) -> str:
    return f"{row['city_id']} [{row['provider']}/{row['network_type']}] {row['run_date']}"


def stats_from_geojson(geojson: dict) -> dict:
    """The street_walks stat columns, exactly as `collect.run_collect` maps them
    out of the artifact it just built (pinned against a real collection)."""
    meta = geojson["properties"]["metadata"]
    totals = meta["totals"]
    return {
        "edges_total": totals["edges"],
        "edges_fully_covered": totals["edges_fully_covered"],
        "mean_edge_coverage": totals["mean_edge_coverage"],
        "coverage_pct_by_length": totals["coverage_pct_by_length"],
        "coverage_pct_by_length_any": totals["coverage_pct_by_length_any"],
        "coverage_by_highway": json.dumps(meta["coverage_by_highway"]),
        "length_km": totals["length_km"],
        "length_km_covered": totals["length_km_covered"],
        "length_km_covered_any": totals["length_km_covered_any"],
        "median_covered_age_years": totals["median_covered_age_years"],
    }


def _stat_equal(column: str, stored, new) -> bool:
    if column == "coverage_by_highway":
        if stored is None:
            return False
        try:
            return json.loads(stored) == json.loads(new)
        except ValueError:
            return False  # unparseable stored value: rewrite it
    return _equalish(stored, new)


def load_frozen_edges(city_id: str, data_dir: str, network_type: str):
    """
    The city's frozen street network as the collector's edge frame, read from
    the GraphML cache ONLY. Raises WalkRefused when the cache is absent: this
    is the one place a repair tool could reach Overpass, and it must not.
    """
    path = network_cache_path(city_id, data_dir, network_type)
    if not os.path.isfile(path):
        raise WalkRefused(
            f"no frozen {network_type} network at {path}; refusing rather than "
            "fetching it from Overpass"
        )
    return graph_to_edges(ox.load_graphml(path))


def _spacing_arg(spacing_m: float):
    """The value collect.py had: `--spacing` is argparse type=int, so an
    integral stored REAL goes back as an int (the artifact records it as typed)."""
    value = float(spacing_m)
    return int(value) if value.is_integer() else value


def recompute_walk(row, edges, data_dir: str) -> Recomputed:
    """Recompute one walk in memory. Raises WalkRefused on any input mismatch."""
    for column in ("spacing_m", "match_dist_m", "coverage_filename"):
        if row[column] is None:
            raise WalkRefused(f"{column} is NULL; the collection cannot be reproduced")
    csv_path = os.path.join(data_dir, row["csv_filename"])
    if not os.path.isfile(csv_path):
        raise WalkRefused(f"snapshot CSV missing ({row['csv_filename']})")

    spacing = _spacing_arg(row["spacing_m"])
    match_dist = float(row["match_dist_m"])
    samples = generate_samples(edges, spacing)
    if row["sample_points"] is not None and int(row["sample_points"]) != len(samples):
        raise WalkRefused(
            f"the frozen network yields {len(samples)} samples at {spacing} m, the walk "
            f"recorded {row['sample_points']}: the network was refreshed since this walk"
        )

    df = load_city_csv_file(csv_path)
    sample_keys = {
        quantize_coord(la, lo) for la, lo in zip(samples["lat"], samples["lon"], strict=True)
    }
    csv_keys = [
        quantize_coord(la, lo) for la, lo in zip(df["query_lat"], df["query_lon"], strict=True)
    ]
    csv_key_set = set(csv_keys)
    if len(csv_keys) != len(csv_key_set) or csv_key_set != sample_keys:
        raise WalkRefused(
            f"sample frame mismatch: the CSV has {len(csv_keys)} rows over "
            f"{len(csv_key_set)} locations, the frozen network yields {len(sample_keys)}; "
            f"{len(sample_keys - csv_key_set)} regenerated locations are not in the CSV "
            f"and {len(csv_key_set - sample_keys)} CSV locations are not regenerated"
        )

    covered = compute_streetwalk_coverage(
        edges,
        samples,
        df,
        row["run_date"],
        provider=row["provider"],
        match_dist_m=match_dist,
    )
    geojson = build_streetwalk_geojson(
        covered,
        city_id=row["city_id"],
        provider=row["provider"],
        run_date=row["run_date"],
        spacing_m=spacing,
        match_dist_m=match_dist,
        source_csv=row["csv_filename"],
        network_type=row["network_type"],
    )
    # Normalized through JSON so it compares equal to what reads back off disk
    # (geometry coordinates are tuples in memory, lists in a loaded file).
    geojson = json.loads(json.dumps(geojson))
    stats = stats_from_geojson(geojson)
    moved = {c: (row[c], stats[c]) for c in STAT_COLUMNS if not _stat_equal(c, row[c], stats[c])}

    artifact_path = os.path.join(data_dir, row["coverage_filename"])
    try:
        with gzip.open(artifact_path, "rt", encoding="utf-8") as fh:
            artifact_stale = json.load(fh) != geojson
    except (OSError, EOFError, ValueError):
        artifact_stale = True  # missing or unreadable: rewrite it
    return Recomputed(row, geojson, stats, moved, artifact_stale)


def _detail_matches(path: str, detail) -> bool:
    """Whether a detail file holds exactly what write_walk_diff_detail would
    write. Compared decompressed: the gzip header carries a write time."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            return fh.read() == detail.to_csv(index=False)
    except (OSError, EOFError, UnicodeDecodeError):
        return False


def stale_diffs(conn, recomputed: list, data_dir: str) -> list:
    """
    The series' existing street_walk_diffs rows that disagree with a diff of the
    RECOMPUTED artifacts, as (to_walk Recomputed, stored row, description).

    The predecessor is the previous walk of the series, which is what
    `db.get_previous_street_walk` returns (the series is in run_date order and a
    date is unique within it). A row the orchestrator would DELETE (no
    predecessor, or a changed sample frame) is stale too.
    """
    out = []
    for i, cur in enumerate(recomputed):
        stored = conn.execute(
            "SELECT * FROM street_walk_diffs WHERE to_walk_id = ?", (cur.row["walk_id"],)
        ).fetchone()
        if stored is None:
            continue
        prev = recomputed[i - 1] if i > 0 else None
        same_frame = prev is not None and (
            float(prev.row["spacing_m"]) == float(cur.row["spacing_m"])
            and float(prev.row["match_dist_m"]) == float(cur.row["match_dist_m"])
        )
        if not same_frame:
            out.append((cur, stored, "no same-frame predecessor; the row would be removed"))
            continue
        diff = compute_walk_diff(prev.geojson, cur.geojson)
        notes = []
        if stored["from_walk_id"] != prev.row["walk_id"]:
            notes.append(f"from_walk_id {stored['from_walk_id']} -> {prev.row['walk_id']}")
        values = {c: getattr(diff, c) for c in DIFF_COLUMNS}
        notes.extend(
            f"{c} {stored[c]} -> {values[c]}"
            for c in DIFF_COLUMNS
            if not _equalish(stored[c], values[c])
        )
        name = generate_streetwalk_diff_filename(
            cur.row["city_id"],
            prev.row["run_date"],
            cur.row["run_date"],
            provider=cur.row["provider"],
            network_type=cur.row["network_type"],
        )
        expected = name if diff.has_changes else None
        if stored["detail_filename"] != expected:
            notes.append(f"detail_filename {stored['detail_filename']} -> {expected}")
        path = os.path.join(data_dir, name)
        on_disk = os.path.isfile(path)
        if diff.has_changes and not (on_disk and _detail_matches(path, diff.detail)):
            notes.append(f"detail file {'stale' if on_disk else 'missing'}, rewrite {name}")
        elif not diff.has_changes and on_disk:
            notes.append(f"detail file remove {name}")
        if notes:
            out.append((cur, stored, "; ".join(notes)))
    return out


def _walk_line(rec: Recomputed, regenerate: bool) -> str:
    notes = [
        f"{c} {old} -> {new}" for c, (old, new) in rec.moved.items() if c != "coverage_by_highway"
    ]
    if "coverage_by_highway" in rec.moved:
        notes.append("coverage_by_highway moved")
    if rec.artifact_stale:
        action = "rewrite" if regenerate else "NOT rewritten without --regenerate-artifacts"
        notes.append(f"artifact stale, {action} ({rec.row['coverage_filename']})")
    return f"{_label(rec.row)}: {'; '.join(notes)}"


def _write_artifact(path: str, geojson: dict) -> None:
    """Write beside the final name and rename in, so a crash never leaves a
    truncated artifact that the site (or a later diff) would read."""
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(geojson, fh)
    os.replace(tmp, path)


def process_series(
    conn, walks: list, edges_for, data_dir: str, execute: bool, regenerate: bool, report: Report
) -> bool:
    """
    Recompute one (city, provider, network_type) series and, under ``execute``,
    apply it. All-or-nothing: any refusal skips the series untouched. Returns
    whether anything was written.
    """
    first = walks[0]
    series = f"{first['city_id']} [{first['provider']}/{first['network_type']}]"
    report.walks_scanned += len(walks)

    recomputed = []
    for row in walks:
        try:
            edges = edges_for(row["city_id"], row["network_type"])
            recomputed.append(recompute_walk(row, edges, data_dir))
        except WalkRefused as exc:
            report.refused.append(
                f"{series}: {len(walks)} walk(s) skipped, because {row['run_date']} is "
                f"refused: {exc}"
            )
            return False
        except Exception as exc:  # one bad series must not end the sweep
            logger.exception(f"{_label(row)}: recompute failed")
            report.refused.append(
                f"{series}: {len(walks)} walk(s) skipped, because {row['run_date']} "
                f"failed: {type(exc).__name__}: {exc}"
            )
            return False

    diffs = stale_diffs(conn, recomputed, data_dir)
    moved_walks = [r for r in recomputed if r.moved or r.artifact_stale]
    if not moved_walks and not diffs:
        report.unchanged_series += 1
        return False
    report.changed_walks.extend(_walk_line(r, regenerate) for r in moved_walks)
    diff_lines = [f"{_label(cur.row)} diff: {note}" for cur, _, note in diffs]
    if not regenerate:
        # A diff reads the artifacts on disk, which this pass leaves old.
        report.stale_diffs_left.extend(diff_lines)
    else:
        report.changed_diffs.extend(diff_lines)
    if not execute:
        return False

    wrote = False
    if regenerate:
        for rec in recomputed:
            if rec.artifact_stale:
                wrote = True
                _write_artifact(os.path.join(data_dir, rec.row["coverage_filename"]), rec.geojson)
                report.artifacts_written += 1
    to_update = [r for r in recomputed if r.moved]
    if to_update:
        with conn:  # one transaction: the series' rows move together
            for rec in to_update:
                assignments = ", ".join(f"{c} = ?" for c in rec.moved)
                conn.execute(
                    f"UPDATE street_walks SET {assignments} WHERE walk_id = ?",
                    (*(rec.stats[c] for c in rec.moved), rec.row["walk_id"]),
                )
        report.rows_updated += len(to_update)
        wrote = True

    if regenerate:
        for cur, stored, _ in diffs:
            row = cur.row
            try:
                compute_and_record_walk_diff(
                    conn,
                    data_dir=data_dir,
                    city_id=row["city_id"],
                    walk_id=row["walk_id"],
                    run_date=date.fromisoformat(row["run_date"]),
                    provider=row["provider"],
                    network_type=row["network_type"],
                    spacing_m=float(row["spacing_m"]),
                    match_dist_m=float(row["match_dist_m"]),
                    fc_new=cur.geojson,
                )
            except Exception as exc:  # the stats repair above is committed
                logger.exception(f"{_label(row)}: re-diff failed")
                report.failed.append(f"{_label(row)} diff: {type(exc).__name__}: {exc}")
                continue
            wrote = True
            old = stored["detail_filename"]
            after = conn.execute(
                "SELECT detail_filename FROM street_walk_diffs WHERE to_walk_id = ?",
                (row["walk_id"],),
            ).fetchone()
            new = after["detail_filename"] if after is not None else None
            if old and old != new and not os.path.exists(os.path.join(data_dir, old)):
                report.removed_files.append(old)
    return wrote


def select_walks(conn, providers, city_ids) -> list:
    """Walks in scope, grouped by series (city, network_type, provider so one
    frozen network serves consecutive series) and in run_date order."""
    clauses, params = [], []
    for column, values in (("provider", providers), ("city_id", city_ids)):
        if values:
            clauses.append(f"{column} IN ({', '.join('?' * len(values))})")
            params.extend(values)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM street_walks {where} ORDER BY city_id, network_type, provider, run_date",
        params,
    ).fetchall()


def _group_series(rows) -> list[list]:
    groups, current, key = [], [], None
    for row in rows:
        k = (row["city_id"], row["network_type"], row["provider"])
        if k != key and current:
            groups.append(current)
            current = []
        key = k
        current.append(row)
    if current:
        groups.append(current)
    return groups


def _edges_loader(data_dir: str):
    """load_frozen_edges with a one-entry cache: consecutive series of one
    (city, network_type) share a network, and a big one is worth not reparsing."""
    cache = {}

    def edges_for(city_id: str, network_type: str):
        key = (city_id, network_type)
        if key not in cache:
            cache.clear()
            try:
                cache[key] = load_frozen_edges(city_id, data_dir, network_type)
            except WalkRefused as exc:
                cache[key] = exc
        value = cache[key]
        if isinstance(value, WalkRefused):
            raise value
        return value

    return edges_for


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
        help="Restrict to these providers' walk series (repeatable or comma-separated; "
        f"one of {', '.join(KNOWN_PROVIDERS)})",
    )
    parser.add_argument(
        "--city",
        action="append",
        help="Restrict to these cities (repeatable; a city_id or a query db.resolve_city "
        "understands). Always whole series.",
    )
    parser.add_argument(
        "--regenerate-artifacts",
        action="store_true",
        help="Also rewrite each stale published *_coverage.json.gz and re-diff the "
        "street_walk_diffs rows that read them (without it: catalog rows only)",
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
                "while it can be writing walks, diffs and the manifest. Wait for the night "
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
        conn.close()
        conn = db.connect(db_path)  # validated above, so there is nothing to migrate
    try:
        city_ids = []
        for query in args.city or ():
            city = db.resolve_city(conn, query)
            if city is None:
                logger.error(f"Refusing: unknown --city {query!r}")
                return USAGE_EXIT_CODE
            city_ids.append(city.city_id)

        report = Report()
        edges_for = _edges_loader(args.data_dir)
        wrote = False
        for walks in _group_series(select_walks(conn, providers, city_ids)):
            report.series_scanned += 1
            wrote |= process_series(
                conn,
                walks,
                edges_for,
                args.data_dir,
                args.execute,
                args.regenerate_artifacts,
                report,
            )

        mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to apply)"
        verb = "changed" if args.execute else "would change"
        print(f"{mode}: {args.data_dir}")
        for line in report.changed_walks:
            print(f"  {verb}: {line}")
        for line in report.changed_diffs:
            print(f"  {verb}: {line}")
        for line in report.stale_diffs_left:
            print(f"  diff left stale (needs --regenerate-artifacts): {line}")
        for line in report.refused:
            print(f"  REFUSED: {line}")
        for line in report.failed:
            print(f"  FAILED: {line}")
        print(
            f"\n{report.series_scanned} series ({report.walks_scanned} walks) scanned, "
            f"{report.unchanged_series} unchanged, {len(report.changed_walks)} walks and "
            f"{len(report.changed_diffs)} diffs {verb}, {len(report.refused)} series refused, "
            f"{len(report.failed)} failed"
        )
        if not args.regenerate_artifacts and (report.changed_walks or report.stale_diffs_left):
            print(
                "Published artifacts and walk diffs not rewritten (no --regenerate-artifacts): "
                "the site keeps the old per-edge coverage and the old change blocks."
            )
        exit_code = 1 if (report.refused or report.failed) else 0
        if not args.execute:
            if report.changed_walks or report.changed_diffs or report.stale_diffs_left:
                print("Dry run complete. Re-run with --execute to apply.")
            return exit_code

        print(
            f"Updated {report.rows_updated} street_walks rows; wrote "
            f"{report.artifacts_written} coverage artifacts."
        )
        if wrote:
            # Guarded like the collector's own tail: the repair is committed.
            try:
                manifest = generate_streetwalk_manifest(conn, args.data_dir)
                print(f"Regenerated streetwalk manifest ({len(manifest['walks'])} walks).")
            except Exception:
                logger.exception(
                    "Streetwalk manifest failed; the repair is applied, but streets.html "
                    "keeps the old numbers until `scheduler regenerate-aggregate` or the "
                    "next run-due rebuilds it"
                )
                exit_code = 1
            print(
                "Nothing was published. Publish with `python -m "
                "streetscape_metadata_tracker.scheduler regenerate-aggregate --publish` "
                "or ./sync_data_to_server.sh."
            )
        if report.removed_files:
            print(
                "Removed walk diff detail files stay on the web server (the publish never "
                "passes rsync --delete); remove these there:"
            )
            for name in report.removed_files:
                print(f"  {name}")
        return exit_code
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
