"""
Walk-to-walk diff engine for road-walk street coverage (issue #101).

The street analogue of diff.py: compares two road-walk coverage GeoJSONs of
the same (city, provider, network_type) series per ``edge_id`` — the stable
unordered OSM node pair, playing the role pano_id plays in grid diffs — and
reports what changed: edges that gained or lost imagery coverage, fractional
coverage shifts, and nearest-pano capture-date changes. This is the temporal
payoff of the street-coverage work: "the imagery was refreshed, but did
coverage actually improve at the next capture?"

Sample points are deterministic from the frozen GraphML, so two walks of the
same network at the same spacing observe the identical sample frame and their
per-edge fractions are directly comparable. A changed spacing or match
distance changes the frame/threshold and nothing survives the comparison, so
the orchestrator skips such pairs entirely (the same_grid_geometry gate
semantics of the grid pipeline). A ``--refresh``'d OSM network shrinks the
shared edge set instead: the diff proceeds over the intersection, and
one-sided edges are counted as added/removed but never as coverage gained or
lost (network churn is not imagery churn).

Lives in the tracker package, not streetscape_street_analyzer, because the
scheduler's walk-salvage path needs it too and the dependency direction is
strictly analyzer -> tracker; it is deliberately pure json/pandas so the heavy
geo stack stays out of the core import graph.
"""

import gzip
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from . import db
from .naming import generate_streetwalk_diff_filename

logger = logging.getLogger(__name__)

# Detail CSV column order — a published contract, like diff.py's detail.
DETAIL_COLUMNS = [
    "edge_id",
    "change_type",
    "highway",
    "length_m",
    "old_coverage_fraction",
    "new_coverage_fraction",
    "old_coverage_fraction_any",
    "new_coverage_fraction_any",
    "old_nearest_pano_date",
    "new_nearest_pano_date",
]


@dataclass
class WalkDiff:
    """Summary (and detail rows) of changes between two walks of a city.

    The headline counters are independent by design: an edge that gained
    coverage AND changed its nearest-pano date increments both counters, so
    each answers its own question. Only the detail CSV collapses to one row
    per changed edge, labeled by the most specific change_type.
    """

    edges_aligned: int
    edges_added: int  # edge_ids only in the new artifact (network refresh)
    edges_removed: int  # edge_ids only in the old artifact
    edges_gained_coverage: int  # aligned, any-coverage 0 -> >0
    edges_lost_coverage: int  # aligned, any-coverage >0 -> 0
    coverage_fraction_changed: int  # aligned, either fraction differs (superset of gained/lost)
    nearest_pano_date_changed: int  # aligned, nearest_pano_date differs (None-safe)
    edges_fully_covered_delta: int | None
    coverage_pct_by_length_delta: float | None
    coverage_pct_by_length_any_delta: float | None
    detail: pd.DataFrame = field(repr=False, default=None)

    @property
    def has_changes(self) -> bool:
        return (
            self.edges_added
            + self.edges_removed
            + self.coverage_fraction_changed
            + self.nearest_pano_date_changed
        ) > 0


def load_streetwalk_coverage(path: str) -> dict:
    """Load a gzipped road-walk coverage GeoJSON FeatureCollection."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _edge_frame(fc: dict) -> pd.DataFrame:
    """Per-edge properties of a coverage FeatureCollection, indexed by edge_id.

    coverage_fraction_any falls back to coverage_fraction when the artifact
    predates the any-imagery split (#116) — the two are equal by construction
    for GSV, and an older Mapillary artifact simply didn't measure flats.
    Duplicated edge_ids shouldn't happen (the id is the network's edge key);
    guard with keep='first' like diff.py does for grid keys.
    """
    rows = []
    for feature in fc.get("features", []):
        props = feature.get("properties", {})
        edge_id = props.get("edge_id")
        if edge_id is None:
            continue
        fraction = props.get("coverage_fraction")
        rows.append(
            {
                "edge_id": edge_id,
                "highway": props.get("highway"),
                "length_m": props.get("length_m"),
                "coverage_fraction": fraction,
                "coverage_fraction_any": props.get("coverage_fraction_any", fraction),
                "nearest_pano_date": props.get("nearest_pano_date"),
            }
        )
    frame = pd.DataFrame(
        rows,
        columns=[
            "edge_id",
            "highway",
            "length_m",
            "coverage_fraction",
            "coverage_fraction_any",
            "nearest_pano_date",
        ],
    ).set_index("edge_id")
    return frame[~frame.index.duplicated(keep="first")]


def _date_str(value) -> str | None:
    """nearest_pano_date as a string or None (normalizes NaN from pandas)."""
    if value is None or pd.isna(value):
        return None
    return str(value)


def _totals_delta(fc_old: dict, fc_new: dict, key: str) -> float | None:
    """new - old of a metadata.totals value, or None when either is absent.

    Absent means "not measured" (e.g. coverage_pct_by_length_any on a pre-#116
    artifact) — never treated as zero.
    """
    old_val = fc_old.get("properties", {}).get("metadata", {}).get("totals", {}).get(key)
    new_val = fc_new.get("properties", {}).get("metadata", {}).get("totals", {}).get(key)
    if old_val is None or new_val is None:
        return None
    # Totals are published at 1 decimal; keep the delta on the same scale.
    return round(new_val - old_val, 1)


def compute_walk_diff(fc_old: dict, fc_new: dict) -> WalkDiff:
    """
    Compare two road-walk coverage FeatureCollections of the same series.

    Pure function over the artifacts; knows nothing about the catalog. Exact
    ``!=`` on fractions is deliberate: under an identical sample frame each
    fraction is covered/total of the same integer counts, so equality is
    deterministic — any difference is a real change in what the provider
    returned, not float noise.

    Returns a WalkDiff with per-edge transition counts, totals deltas taken
    from the artifacts' metadata, and a detail DataFrame with one row per
    changed edge (DETAIL_COLUMNS order).
    """
    old_edges = _edge_frame(fc_old)
    new_edges = _edge_frame(fc_new)

    old_ids = set(old_edges.index)
    new_ids = set(new_edges.index)
    added_ids = sorted(new_ids - old_ids)
    removed_ids = sorted(old_ids - new_ids)
    aligned_ids = sorted(old_ids & new_ids)

    if added_ids or removed_ids:
        logger.warning(
            f"Edge sets differ between walks ({len(old_ids)} vs {len(new_ids)} edges, "
            f"{len(aligned_ids)} shared) — the OSM network changed between walks; "
            "one-sided edges are reported as added/removed, not as coverage changes"
        )

    old_aligned = old_edges.loc[aligned_ids]
    new_aligned = new_edges.loc[aligned_ids]

    old_any = old_aligned["coverage_fraction_any"].astype(float)
    new_any = new_aligned["coverage_fraction_any"].astype(float)
    old_frac = old_aligned["coverage_fraction"].astype(float)
    new_frac = new_aligned["coverage_fraction"].astype(float)

    gained_mask = (old_any == 0) & (new_any > 0)
    lost_mask = (old_any > 0) & (new_any == 0)
    fraction_changed_mask = (old_frac != new_frac) | (old_any != new_any)

    old_dates = old_aligned["nearest_pano_date"].map(_date_str)
    new_dates = new_aligned["nearest_pano_date"].map(_date_str)
    date_changed_mask = old_dates.fillna("") != new_dates.fillna("")

    detail_rows = []
    for edge_id in added_ids:
        row = new_edges.loc[edge_id]
        detail_rows.append(
            {
                "edge_id": edge_id,
                "change_type": "edge_added",
                "highway": row["highway"],
                "length_m": row["length_m"],
                "old_coverage_fraction": None,
                "new_coverage_fraction": row["coverage_fraction"],
                "old_coverage_fraction_any": None,
                "new_coverage_fraction_any": row["coverage_fraction_any"],
                "old_nearest_pano_date": None,
                "new_nearest_pano_date": _date_str(row["nearest_pano_date"]),
            }
        )
    for edge_id in removed_ids:
        row = old_edges.loc[edge_id]
        detail_rows.append(
            {
                "edge_id": edge_id,
                "change_type": "edge_removed",
                "highway": row["highway"],
                "length_m": row["length_m"],
                "old_coverage_fraction": row["coverage_fraction"],
                "new_coverage_fraction": None,
                "old_coverage_fraction_any": row["coverage_fraction_any"],
                "new_coverage_fraction_any": None,
                "old_nearest_pano_date": _date_str(row["nearest_pano_date"]),
                "new_nearest_pano_date": None,
            }
        )

    changed_mask = fraction_changed_mask | date_changed_mask
    for edge_id in old_aligned.index[changed_mask]:
        if gained_mask.loc[edge_id]:
            change_type = "gained_coverage"
        elif lost_mask.loc[edge_id]:
            change_type = "lost_coverage"
        elif fraction_changed_mask.loc[edge_id]:
            change_type = "coverage_changed"
        else:
            change_type = "pano_date_changed"
        new_row = new_aligned.loc[edge_id]
        old_row = old_aligned.loc[edge_id]
        detail_rows.append(
            {
                "edge_id": edge_id,
                "change_type": change_type,
                "highway": new_row["highway"],
                "length_m": new_row["length_m"],
                "old_coverage_fraction": old_row["coverage_fraction"],
                "new_coverage_fraction": new_row["coverage_fraction"],
                "old_coverage_fraction_any": old_row["coverage_fraction_any"],
                "new_coverage_fraction_any": new_row["coverage_fraction_any"],
                "old_nearest_pano_date": _date_str(old_row["nearest_pano_date"]),
                "new_nearest_pano_date": _date_str(new_row["nearest_pano_date"]),
            }
        )

    detail = pd.DataFrame(detail_rows, columns=DETAIL_COLUMNS)

    # Fully-covered counts over each FULL artifact (not just aligned edges),
    # matching summarize_streetwalk_coverage's definition, so the delta agrees
    # with the difference of the two cataloged edges_fully_covered values.
    fully_covered_delta = int(
        (new_edges["coverage_fraction"].astype(float) >= 1.0).sum()
        - (old_edges["coverage_fraction"].astype(float) >= 1.0).sum()
    )

    return WalkDiff(
        edges_aligned=len(aligned_ids),
        edges_added=len(added_ids),
        edges_removed=len(removed_ids),
        edges_gained_coverage=int(gained_mask.sum()),
        edges_lost_coverage=int(lost_mask.sum()),
        coverage_fraction_changed=int(fraction_changed_mask.sum()),
        nearest_pano_date_changed=int(date_changed_mask.sum()),
        edges_fully_covered_delta=fully_covered_delta,
        coverage_pct_by_length_delta=_totals_delta(fc_old, fc_new, "coverage_pct_by_length"),
        coverage_pct_by_length_any_delta=_totals_delta(
            fc_old, fc_new, "coverage_pct_by_length_any"
        ),
        detail=detail,
    )


def write_walk_diff_detail(diff: WalkDiff, output_path: str) -> None:
    """Write the diff's detail rows as a gzipped CSV."""
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as f:
        diff.detail.to_csv(f, index=False)
    logger.info(f"Wrote walk diff detail ({len(diff.detail)} rows) to {output_path}")


def compute_and_record_walk_diff(
    conn,
    *,
    data_dir: str,
    city_id: str,
    walk_id: int,
    run_date: date,
    provider: str,
    network_type: str,
    spacing_m: float | None,
    match_dist_m: float | None,
    fc_new: dict | None = None,
) -> dict | None:
    """
    Diff a just-cataloged walk against its predecessor and record the result —
    the walk analogue of cli._compute_and_record_diff, shared by collect.py
    and the scheduler's walk-salvage path.

    Safe to call right after register_street_walk: the predecessor lookup is
    strictly run_date < the new walk's date, so the new row never matches
    itself. Returns the JSON change block for the manifest, or None when there
    is nothing to diff: no previous walk (every first walk), a changed sample
    frame (spacing/match-dist mismatch — nothing survives that comparison, so
    like the grid's geometry gate the pair is skipped entirely and diffs
    resume once two same-frame walks exist), or a missing previous artifact.

    The DB row is recorded even when nothing changed — "diffed, no changes"
    and "never diffed" are different facts — but the detail file is only
    written when there are changes, mirroring the grid diff.

    Args:
        fc_new: the new walk's coverage FeatureCollection when the caller
            already holds it in memory (the collector does); loaded from the
            walk's cataloged coverage_filename otherwise.
    """
    prev = db.get_previous_street_walk(
        conn, city_id, run_date, provider=provider, network_type=network_type
    )
    if prev is None:
        return None

    prev_spacing = prev["spacing_m"]
    prev_match = prev["match_dist_m"]
    if (
        spacing_m is None
        or prev_spacing is None
        or float(prev_spacing) != float(spacing_m)
        or match_dist_m is None
        or prev_match is None
        or float(prev_match) != float(match_dist_m)
    ):
        logger.warning(
            f"{city_id}: walk sample frames differ from previous walk "
            f"(spacing {prev_spacing} -> {spacing_m}, match-dist {prev_match} -> "
            f"{match_dist_m}); skipping walk diff — diffs resume once two "
            "same-frame walks exist"
        )
        return None

    if not prev["coverage_filename"]:
        logger.warning(
            f"{city_id}: previous walk ({prev['run_date']}) has no coverage artifact "
            "cataloged; skipping walk diff"
        )
        return None
    prev_path = os.path.join(data_dir, prev["coverage_filename"])
    if not os.path.exists(prev_path):
        logger.warning(
            f"{city_id}: previous walk coverage artifact missing on disk "
            f"({prev['coverage_filename']}); skipping walk diff"
        )
        return None

    if fc_new is None:
        new_row = conn.execute(
            "SELECT coverage_filename FROM street_walks WHERE walk_id = ?", (walk_id,)
        ).fetchone()
        if new_row is None or not new_row["coverage_filename"]:
            logger.warning(f"{city_id}: walk {walk_id} has no coverage artifact; skipping diff")
            return None
        fc_new = load_streetwalk_coverage(os.path.join(data_dir, new_row["coverage_filename"]))
    fc_old = load_streetwalk_coverage(prev_path)

    diff = compute_walk_diff(fc_old, fc_new)

    detail_filename = None
    if diff.has_changes:
        detail_filename = generate_streetwalk_diff_filename(
            city_id,
            prev["run_date"],
            run_date.isoformat(),
            provider=provider,
            network_type=network_type,
        )
        write_walk_diff_detail(diff, os.path.join(data_dir, detail_filename))

    db.record_street_walk_diff(
        conn,
        city_id=city_id,
        from_walk_id=prev["walk_id"],
        to_walk_id=walk_id,
        edges_aligned=diff.edges_aligned,
        edges_added=diff.edges_added,
        edges_removed=diff.edges_removed,
        edges_gained_coverage=diff.edges_gained_coverage,
        edges_lost_coverage=diff.edges_lost_coverage,
        coverage_fraction_changed=diff.coverage_fraction_changed,
        nearest_pano_date_changed=diff.nearest_pano_date_changed,
        edges_fully_covered_delta=diff.edges_fully_covered_delta,
        coverage_pct_by_length_delta=diff.coverage_pct_by_length_delta,
        coverage_pct_by_length_any_delta=diff.coverage_pct_by_length_any_delta,
        detail_filename=detail_filename,
    )

    delta = diff.coverage_pct_by_length_delta
    logger.info(
        f"{city_id}: walk diff vs {prev['run_date']} — "
        f"{diff.edges_gained_coverage} edges gained / {diff.edges_lost_coverage} lost coverage, "
        f"{diff.nearest_pano_date_changed} pano-date changes"
        + (f", coverage {delta:+.1f} pp by length" if delta is not None else "")
    )

    return {
        "from": prev["run_date"],
        "to": run_date.isoformat(),
        "edges_gained_coverage": diff.edges_gained_coverage,
        "edges_lost_coverage": diff.edges_lost_coverage,
        "coverage_pct_by_length_delta": diff.coverage_pct_by_length_delta,
        "coverage_pct_by_length_any_delta": diff.coverage_pct_by_length_any_delta,
        "nearest_pano_date_changed": diff.nearest_pano_date_changed,
        "diff_file": detail_filename,
    }
