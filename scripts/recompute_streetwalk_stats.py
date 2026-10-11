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

What it can repair, and what it cannot. It re-applies whatever
`compute_streetwalk_coverage` does to the CSV's rows (the PRESENT vocabulary,
the GSV copyright gate, the match distance). For a CENSUS walk (Mapillary,
KartaView, Panoramax) the CSV is itself a derived join -- the census rows were
already reduced to one row per sample, with the provider's date rule already
applied -- so a future change to a census-side date or status rule cannot be
repaired here; that needs the census, which is not kept.

What makes a walk REFUSED (and its whole series skipped, untouched), counted
per reason in the summary:

- **missing GraphML.** The network is loaded with `ox.load_graphml` from
  `naming.network_cache_path` and from nowhere else. This script never calls
  `download_street_network.fetch_graph`, which falls through to Overpass on a
  cache miss -- the per-IP volunteer service that banned makelab2. A sweep over
  a few hundred walks must not make even one request.
- **sample count mismatch / sample key mismatch.** `street_networks` is UNIQUE
  per (city, network_type), so a `--refresh` overwrote the network in place and
  the graph a walk was collected on may be gone. Rather than reason about
  `fetched_at`, the frame is validated directly. `street_walks.sample_points`
  is the cheap pre-check; then the regenerated edges' `edge_id`s must be
  exactly the stored artifact's (a refresh can keep the sample count and even
  the coordinates while renumbering the OSM nodes, and the walk diff keys on
  `edge_id`), and every regenerated sample must lie within COORD_TOLERANCE_DEG
  of exactly one of the CSV's unique query locations, and every one of those
  locations must be hit. A tolerance, not exact 9-decimal keys, so the check
  cannot refuse a series over a sub-ULP difference with a message that reads
  like a refreshed network. Since issue #425 `fileutils.load_city_csv_file`
  reads coordinates correctly rounded (`float_precision="round_trip"`), so on
  every CSV the collectors write the tolerance is never needed: the CSV text is
  `repr` of the sample float and parses back to it exactly. A sample that
  matches only within the tolerance is still accepted and COUNTED (`n_noise`),
  exactly as the collector accepts it -- `compute_streetwalk_coverage`'s exact
  key join scores it uncovered in both -- but the report flags it, because
  under the round-trip loader a nonzero count means the CSV's text is not the
  sample's own repr. Duplicate CSV rows (one location twice) are accepted and
  counted, exactly as the collector's own `drop_duplicates(keep="first")`
  accepts them.
- **NULL column / missing CSV / recompute error.** A NULL
  `spacing_m`/`match_dist_m`/`coverage_filename` (nothing to reproduce the
  collection with, or nowhere to publish it), a missing snapshot CSV, or any
  exception while loading or recomputing.

**Whole series in one pass.** Per (city, provider, network_type) every walk is
recomputed in memory first; one refusal skips the series, so a city's walk
history never mixes two definitions. A refused series is reported by name and
makes the pass exit 1. A series that spans a network refresh is therefore
refused whole, even though its post-refresh walks alone could be repaired.

What --execute writes, per series that moved:

1. Each stale `*_coverage.json.gz` (written to a temp name and `os.replace`d,
   so a crash never leaves a truncated artifact).
2. The `street_walks` stat columns, in ONE transaction for the series:
   edges_total, edges_fully_covered, mean_edge_coverage,
   coverage_pct_by_length, coverage_pct_by_length_any, coverage_by_highway,
   length_km, length_km_covered, length_km_covered_any,
   median_covered_age_years.
3. Every walk diff that disagrees with a diff of the recomputed artifacts --
   an existing row whose counters, `from_walk_id`, detail pointer or detail
   file (presence or content) differ, a row the orchestrator would now remove
   (no same-frame predecessor), and a MISSING row for a walk that has a
   same-frame predecessor -- re-diffed through the collector's own
   `walk_diff.compute_and_record_walk_diff`. Since #265 that removes a detail
   file a no-changes diff no longer has.

`--catalog-only` writes step 2 alone. That leaves the site MIXED: the catalog
and manifest say one definition, the published per-edge artifacts and change
blocks the other -- and each series' next nightly walk diffs against the OLD
artifact on disk, which re-creates the very phantom delta this tool exists to
remove. The report says so whenever the flag is used.

Then `streetwalks.json.gz` is regenerated (its headline stats AND its `change`
block both read what this pass changed), guarded so a manifest failure reports
rather than making a committed repair look failed. It is regenerated on EVERY
--execute pass, not only one that wrote, so a re-run heals a manifest that an
interrupted pass never reached -- except after a run-due was found mid-pass:
the batch writes the manifest itself through the same fixed `.tmp` name, and
its tail rebuilds it from the catalog this pass already updated.

The snapshot CSV is never rewritten: it records what the provider said.

Idempotent, and a re-run heals an interrupted pass. A series whose rows,
artifacts and diffs already agree with the recomputation is untouched. Rows and
diffs are compared against the in-memory recomputation, never against whether
this pass wrote something, so a crash after the artifacts but before the rows
is finished by the next pass. `compute_and_record_walk_diff` deletes a walk's
diff row (committed) before it writes the new one, so a re-diff that fails in
between leaves NO row; the next pass sees a same-frame predecessor with no row
and records it.

Nothing is rsynced: publish afterwards (`scheduler regenerate-aggregate
--publish`, or `./sync_data_to_server.sh`). The publish never passes rsync
`--delete`, so a walk diff detail file REMOVED here stays on the web server
until removed there; the script lists those names.

Concurrency. --execute is refused while a `run-due` is in flight on this
machine, and that is re-checked before each series' writes (the pass stops at
the first series that finds one, and leaves the manifest to the batch); each series' walk ids are also re-selected
before writing and the series is abandoned if they changed. Neither can see a
manual `collect` or `assess-city`, which are not `run-due`: do not run one
alongside this. A dry run opens the catalog read-only (`open_catalog_readonly`)
and writes nothing; a missing, other-version or empty catalog is refused.

Exit 0 when every series was recomputed or left alone; 1 when a series was
refused, abandoned or a step failed; 2 for an argument error; 64 for a refusal
(catalog, unknown --city, run-due in flight at the start).

Catalog/disk only: no API calls, no Overpass, no network of any kind.

Usage:
    python scripts/recompute_streetwalk_stats.py                       # dry run
    python scripts/recompute_streetwalk_stats.py --provider mapillary  # filter
    python scripts/recompute_streetwalk_stats.py --execute             # apply
    python scripts/recompute_streetwalk_stats.py --execute --catalog-only
"""

import argparse
import gzip
import json
import logging
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import numpy as np
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

# How far a regenerated sample may sit from its CSV query location and still be
# the same point: ~1 mm. Float noise is ~1e-14 deg; the closest two distinct
# sample locations can be is set by --spacing (metres, so ~1e-5 deg).
COORD_TOLERANCE_DEG = 1e-8
# Hash-grid cell for the tolerance search; must exceed 2 * COORD_TOLERANCE_DEG
# so a neighbour within tolerance is always in one of the 9 surrounding cells.
_CELL_DEG = 1e-6

# Refusal reasons, in summary order.
MISSING_GRAPHML = "missing GraphML"
COUNT_MISMATCH = "sample count mismatch"
KEY_MISMATCH = "sample key mismatch"
EDGE_MISMATCH = "edge id mismatch"
NULL_COLUMN = "NULL column"
MISSING_CSV = "missing CSV"
RECOMPUTE_ERROR = "recompute error"
REASONS = (
    MISSING_GRAPHML,
    COUNT_MISMATCH,
    KEY_MISMATCH,
    EDGE_MISMATCH,
    NULL_COLUMN,
    MISSING_CSV,
    RECOMPUTE_ERROR,
)


class WalkRefused(Exception):
    """This walk cannot be recomputed faithfully; its whole series is skipped."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class RunDueStarted(Exception):
    """A run-due began during an --execute pass; stop before the next write."""


@dataclass
class Report:
    """What one pass found. Each list holds one-line descriptions."""

    series_scanned: int = 0
    walks_scanned: int = 0
    unchanged_series: int = 0
    changed_walks: list = field(default_factory=list)
    changed_diffs: list = field(default_factory=list)
    left_stale: list = field(default_factory=list)  # what --catalog-only did not write
    refused: list = field(default_factory=list)
    refusal_reasons: Counter = field(default_factory=Counter)
    failed: list = field(default_factory=list)
    # Tolerance-only matches (unexpected since #425) and duplicate rows, one note each.
    notes: list = field(default_factory=list)
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
            MISSING_GRAPHML,
            f"no frozen {network_type} network at {path}; refusing rather than "
            "fetching it from Overpass",
        )
    return graph_to_edges(ox.load_graphml(path))


def _spacing_arg(spacing_m: float):
    """The value collect.py had: `--spacing` is argparse type=int, so an
    integral stored REAL goes back as an int (the artifact records it as typed)."""
    value = float(spacing_m)
    return int(value) if value.is_integer() else value


def _cells(lat: float, lon: float) -> tuple[int, int]:
    return (math.floor(lat / _CELL_DEG), math.floor(lon / _CELL_DEG))


def match_frame(samples, df):
    """
    Validate the regenerated ``samples`` against the CSV's unique query locations
    within COORD_TOLERANCE_DEG, and return ``(n_noise, n_dup)``.

    The CSV's unique locations are taken exactly as ``compute_streetwalk_coverage``
    takes them -- one per 9-decimal key, first row wins -- so ``n_dup`` counts the
    rows it would drop too. Every sample must hit exactly one location within the
    tolerance and every location must be hit; otherwise WalkRefused.
    ``n_noise`` counts samples whose exact 9-decimal key matched no CSV location
    (0 on any CSV the collectors wrote, since #425's round-trip loader). The
    samples are NOT moved onto the CSV's coordinates: the scorer's exact join
    misses them in the collector and so must miss them here (module docstring).
    """
    keys = [quantize_coord(la, lo) for la, lo in zip(df["query_lat"], df["query_lon"], strict=True)]
    first = {}
    for i, key in enumerate(keys):
        first.setdefault(key, i)
    n_dup = len(keys) - len(first)
    qlat = df["query_lat"].to_numpy(dtype=float)
    qlon = df["query_lon"].to_numpy(dtype=float)
    loc_lat = np.array([qlat[i] for i in first.values()], dtype=float)
    loc_lon = np.array([qlon[i] for i in first.values()], dtype=float)
    key_to_loc = {key: n for n, key in enumerate(first)}

    grid: dict = {}
    for n, (la, lo) in enumerate(zip(loc_lat, loc_lon, strict=True)):
        grid.setdefault(_cells(la, lo), []).append(n)

    def near(la, lo, tol):
        ci, cj = _cells(la, lo)
        return [
            n
            for di in (-1, 0, 1)
            for dj in (-1, 0, 1)
            for n in grid.get((ci + di, cj + dj), ())
            if abs(loc_lat[n] - la) <= tol and abs(loc_lon[n] - lo) <= tol
        ]

    # Two CSV locations closer than twice the tolerance would make a sample's
    # match ambiguous; nothing the collector writes is that close.
    crowded = sum(
        1
        for la, lo in zip(loc_lat, loc_lon, strict=True)
        if len(near(la, lo, 2 * COORD_TOLERANCE_DEG)) > 1
    )
    if crowded:
        raise WalkRefused(
            KEY_MISMATCH,
            f"sample key mismatch: {crowded} CSV locations lie within "
            f"{2 * COORD_TOLERANCE_DEG:g} deg of another, so samples cannot be matched",
        )

    # Python floats, as the scorer's own `zip` over a Series yields them:
    # round() on an np.float64 takes numpy's path, which can land a half-way
    # value on the other side of quantize_coord's boundary.
    s_lat = samples["lat"].astype(float).tolist()
    s_lon = samples["lon"].astype(float).tolist()
    assigned = np.empty(len(samples), dtype=np.int64)
    unmatched = n_noise = 0
    for i, (la, lo) in enumerate(zip(s_lat, s_lon, strict=True)):
        n = key_to_loc.get(quantize_coord(la, lo))
        if n is None:
            hits = near(la, lo, COORD_TOLERANCE_DEG)
            if len(hits) != 1:
                unmatched += 1
                assigned[i] = -1
                continue
            n = hits[0]
            n_noise += 1
        assigned[i] = n
    hit = set(assigned[assigned >= 0].tolist())
    unhit = len(first) - len(hit)
    if unmatched or unhit:
        raise WalkRefused(
            KEY_MISMATCH,
            f"sample key mismatch: {unmatched} of {len(samples)} regenerated samples "
            f"match no CSV location within {COORD_TOLERANCE_DEG:g} deg, and "
            f"{unhit} of the CSV's {len(first)} locations match no sample",
        )
    return n_noise, n_dup


def recompute_walk(row, edges, data_dir: str, report: Report) -> Recomputed:
    """Recompute one walk in memory. Raises WalkRefused on any input mismatch."""
    for column in ("spacing_m", "match_dist_m", "coverage_filename"):
        if row[column] is None:
            raise WalkRefused(NULL_COLUMN, f"{column} is NULL; the collection cannot be reproduced")
    csv_path = os.path.join(data_dir, row["csv_filename"])
    if not os.path.isfile(csv_path):
        raise WalkRefused(MISSING_CSV, f"snapshot CSV missing ({row['csv_filename']})")

    spacing = _spacing_arg(row["spacing_m"])
    match_dist = float(row["match_dist_m"])
    samples = generate_samples(edges, spacing)
    if row["sample_points"] is not None and int(row["sample_points"]) != len(samples):
        raise WalkRefused(
            COUNT_MISMATCH,
            f"the frozen network yields {len(samples)} samples at {spacing} m, the walk "
            f"recorded {row['sample_points']}: the network was refreshed since this walk",
        )

    df = load_city_csv_file(csv_path)
    n_noise, n_dup = match_frame(samples, df)
    # Two notes, never one: duplicate rows are an accepted state (a resumed
    # collection can write a point twice), while a tolerance-only match is the
    # runbook's stop signal, so a duplicate-only walk must not print its text.
    if n_noise:
        report.notes.append(
            f"{_label(row)}: {n_noise} samples matched only within {COORD_TOLERANCE_DEG:g} deg "
            "(their 9-decimal key matches no CSV location, so the scorer's exact join scores "
            "them uncovered, here as in the collector; under the round-trip loader (#425) this "
            "should be 0 -- a nonzero count means this CSV's text is not the sample's own repr, "
            "or the regenerated samples differ from the collected ones)"
        )
    if n_dup:
        report.notes.append(
            f"{_label(row)}: {n_dup} duplicate CSV rows ignored as the collector ignores them"
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
            stored = json.load(fh)
    except (OSError, EOFError, ValueError):
        return Recomputed(row, geojson, stats, moved, True)  # missing or unreadable: rewrite it
    # The coordinates matching does not prove the EDGES are the walk's: a
    # refresh can renumber OSM nodes over identical geometry, and the walk diff
    # keys on edge_id, so a renumbered artifact would diff as every edge
    # removed and re-added.
    regenerated_ids = _edge_ids(geojson)
    stored_ids = _edge_ids(stored)
    if regenerated_ids != stored_ids:
        raise WalkRefused(
            EDGE_MISMATCH,
            f"edge id mismatch: the frozen network's {len(regenerated_ids)} edge ids are not "
            f"the stored artifact's {len(stored_ids)} ({len(regenerated_ids - stored_ids)} only "
            f"in the network, {len(stored_ids - regenerated_ids)} only in the artifact): the "
            "network was refreshed since this walk",
        )
    return Recomputed(row, geojson, stats, moved, stored != geojson)


def _edge_ids(geojson) -> set:
    """The edge_ids of a coverage FeatureCollection's features."""
    features = geojson.get("features") if isinstance(geojson, dict) else None
    return {(f.get("properties") or {}).get("edge_id") for f in features or ()}


def _detail_matches(path: str, detail) -> bool:
    """Whether a detail file holds exactly what write_walk_diff_detail would
    write. Compared decompressed: the gzip header carries a write time."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            return fh.read() == detail.to_csv(index=False)
    except (OSError, EOFError, UnicodeDecodeError):
        return False


def _same_frame(prev: Recomputed, cur: Recomputed) -> bool:
    """compute_and_record_walk_diff's own gate: same spacing AND match distance."""
    return float(prev.row["spacing_m"]) == float(cur.row["spacing_m"]) and float(
        prev.row["match_dist_m"]
    ) == float(cur.row["match_dist_m"])


def stale_diffs(conn, recomputed: list, data_dir: str) -> list:
    """
    The series' walk diffs that disagree with a diff of the RECOMPUTED artifacts,
    as (to_walk Recomputed, stored row or None, description, detail name). The
    detail name is the deterministic one the orchestrator writes or removes
    for a same-frame pair, else None.

    The predecessor is the previous walk of the series, which is what
    `db.get_previous_street_walk` returns (the series is in run_date order and a
    date is unique within it). Three shapes are stale: a stored row that
    differs, a stored row the orchestrator would now remove (no same-frame
    predecessor), and a MISSING row where a same-frame predecessor exists -- the
    last is what a failed re-diff leaves behind, so recording it is the heal.
    """
    out = []
    for i, cur in enumerate(recomputed):
        stored = conn.execute(
            "SELECT * FROM street_walk_diffs WHERE to_walk_id = ?", (cur.row["walk_id"],)
        ).fetchone()
        prev = recomputed[i - 1] if i > 0 else None
        if prev is None or not _same_frame(prev, cur):
            if stored is not None:
                out.append(
                    (cur, stored, "no same-frame predecessor; the row would be removed", None)
                )
            continue
        name = generate_streetwalk_diff_filename(
            cur.row["city_id"],
            prev.row["run_date"],
            cur.row["run_date"],
            provider=cur.row["provider"],
            network_type=cur.row["network_type"],
        )
        if stored is None:
            out.append((cur, None, f"no diff row against {prev.row['run_date']}; record one", name))
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
            out.append((cur, stored, "; ".join(notes), name))
    return out


def _walk_line(rec: Recomputed, catalog_only: bool) -> str | None:
    """What this pass writes for one walk, or None when it writes nothing."""
    notes = [
        f"{c} {old} -> {new}" for c, (old, new) in rec.moved.items() if c != "coverage_by_highway"
    ]
    if "coverage_by_highway" in rec.moved:
        notes.append("coverage_by_highway moved")
    if rec.artifact_stale and not catalog_only:
        notes.append(f"rewrite artifact {rec.row['coverage_filename']}")
    return f"{_label(rec.row)}: {'; '.join(notes)}" if notes else None


def _write_artifact(path: str, geojson: dict) -> None:
    """Write beside the final name and rename in, so a crash never leaves a
    truncated artifact that the site (or a later diff) would read."""
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(geojson, fh)
    os.replace(tmp, path)


def _series_where(row) -> tuple:
    return (
        "city_id = ? AND provider = ? AND network_type = ?",
        (row["city_id"], row["provider"], row["network_type"]),
    )


def process_series(
    conn, walks: list, edges_for, data_dir: str, execute: bool, catalog_only: bool, report: Report
) -> bool:
    """
    Recompute one (city, provider, network_type) series and, under ``execute``,
    apply it. All-or-nothing: any refusal skips the series untouched. Returns
    whether anything was written. Raises RunDueStarted when a batch began.
    """
    first = walks[0]
    series = f"{first['city_id']} [{first['provider']}/{first['network_type']}]"
    report.walks_scanned += len(walks)

    recomputed = []
    for row in walks:
        try:
            edges = edges_for(row["city_id"], row["network_type"])
            recomputed.append(recompute_walk(row, edges, data_dir, report))
        except Exception as exc:  # one bad series must not end the sweep
            if isinstance(exc, WalkRefused):
                reason, detail = exc.reason, str(exc)
            else:
                logger.exception(f"{_label(row)}: recompute failed")
                reason, detail = RECOMPUTE_ERROR, f"{type(exc).__name__}: {exc}"
            report.refusal_reasons[reason] += 1
            report.refused.append(
                f"{series}: {len(walks)} walk(s) skipped, because {row['run_date']} is "
                f"refused ({reason}): {detail}"
            )
            return False

    diffs = stale_diffs(conn, recomputed, data_dir)
    walk_lines = [line for r in recomputed if (line := _walk_line(r, catalog_only))]
    stale_artifacts = [r for r in recomputed if r.artifact_stale]
    if not walk_lines and not diffs and not stale_artifacts:
        report.unchanged_series += 1
        return False
    report.changed_walks.extend(walk_lines)
    diff_lines = [f"{_label(cur.row)} diff: {note}" for cur, _, note, _ in diffs]
    if catalog_only:
        report.left_stale.extend(
            f"{_label(r.row)}: artifact {r.row['coverage_filename']}" for r in stale_artifacts
        )
        report.left_stale.extend(diff_lines)
    else:
        report.changed_diffs.extend(diff_lines)
    if not execute:
        return False

    # The write phase. Re-check what the pass started from: a run-due that began
    # since, or a walk added or replaced in this series since it was selected.
    in_flight = _run_due_in_flight()
    if in_flight:
        raise RunDueStarted(f"a run-due started ({in_flight}); stopped before {series}")
    where, params = _series_where(first)
    now_ids = [
        r["walk_id"]
        for r in conn.execute(
            f"SELECT walk_id FROM street_walks WHERE {where} ORDER BY run_date", params
        )
    ]
    if now_ids != [r["walk_id"] for r in walks]:
        report.failed.append(f"{series}: its walks changed during the pass; abandoned unwritten")
        return False

    wrote = False
    if not catalog_only:
        for rec in stale_artifacts:
            _write_artifact(os.path.join(data_dir, rec.row["coverage_filename"]), rec.geojson)
            report.artifacts_written += 1
            wrote = True
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

    if catalog_only:
        return wrote
    for cur, stored, _, name in diffs:
        row = cur.row
        old = stored["detail_filename"] if stored is not None else None
        # A file at the deterministic name that no row points at (an orphan
        # from before #265) is removed by the no-changes branch too, and is as
        # published as a pointed-at one, so it is listed for the web server.
        existed = {n for n in (old, name) if n and os.path.exists(os.path.join(data_dir, n))}
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
            report.failed.append(
                f"{_label(row)} diff: {type(exc).__name__}: {exc} (a re-run records it)"
            )
            continue
        wrote = True
        after = conn.execute(
            "SELECT detail_filename FROM street_walk_diffs WHERE to_walk_id = ?",
            (row["walk_id"],),
        ).fetchone()
        new = after["detail_filename"] if after is not None else None
        gone = {n for n in existed if not os.path.exists(os.path.join(data_dir, n))}
        if old and old != new and not os.path.exists(os.path.join(data_dir, old)):
            gone.add(old)  # a pointer to a file already gone here may still be published
        report.removed_files.extend(sorted(gone))
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


CATALOG_ONLY_WARNING = (
    "--catalog-only: the published per-edge artifacts and the walk diffs were NOT "
    "rewritten, so the site now MIXES two definitions (catalog and manifest new, "
    "artifacts and change blocks old), and each series' next nightly walk diffs "
    "against the OLD artifact, re-creating the phantom delta. Re-run without "
    "--catalog-only to finish."
)


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
        "--catalog-only",
        action="store_true",
        help="Update the street_walks rows only: leave the published artifacts and the "
        "walk diffs as they are (leaves the site mixed; see the module docstring)",
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
        run_due_started = False
        for walks in _group_series(select_walks(conn, providers, city_ids)):
            report.series_scanned += 1
            try:
                process_series(
                    conn,
                    walks,
                    edges_for,
                    args.data_dir,
                    args.execute,
                    args.catalog_only,
                    report,
                )
            except RunDueStarted as exc:
                report.failed.append(f"{exc}; the remaining series were not processed")
                run_due_started = True
                break

        mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to apply)"
        verb = "changed" if args.execute else "would change"
        print(f"{mode}: {args.data_dir}")
        for line in report.changed_walks:
            print(f"  {verb}: {line}")
        for line in report.changed_diffs:
            print(f"  {verb}: {line}")
        for line in report.left_stale:
            print(f"  left stale (--catalog-only): {line}")
        for line in report.notes:
            print(f"  note: {line}")
        for line in report.refused:
            print(f"  REFUSED: {line}")
        for line in report.failed:
            print(f"  FAILED: {line}")
        reasons = ", ".join(
            f"{r} {report.refusal_reasons[r]}" for r in REASONS if report.refusal_reasons[r]
        )
        print(
            f"\n{report.series_scanned} series ({report.walks_scanned} walks) scanned, "
            f"{report.unchanged_series} unchanged, {len(report.changed_walks)} walks and "
            f"{len(report.changed_diffs)} diffs {verb}, {len(report.refused)} series refused"
            f"{f' ({reasons})' if reasons else ''}, {len(report.failed)} failed"
        )
        if args.catalog_only:
            print(CATALOG_ONLY_WARNING)
        exit_code = 1 if (report.refused or report.failed) else 0
        if not args.execute:
            if report.changed_walks or report.changed_diffs or report.left_stale:
                print("Dry run complete. Re-run with --execute to apply.")
            return exit_code

        print(
            f"Updated {report.rows_updated} street_walks rows; wrote "
            f"{report.artifacts_written} coverage artifacts."
        )
        if run_due_started:
            # The batch writes the manifest itself, through the same fixed
            # `.tmp` name, so a second writer now could interleave with it.
            # Its tail rebuilds it from the catalog this pass already updated.
            print(
                "Streetwalk manifest NOT regenerated: a run-due is running, and its tail "
                "rebuilds the manifest from the catalog this pass updated. If that night "
                "fails, run `scheduler regenerate-aggregate` once it has finished."
            )
        else:
            # Always, not only when this pass wrote: a pass that died after its
            # writes but before the manifest leaves nothing for a re-run to
            # write, and the manifest is healed only here.
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
