#!/usr/bin/env python3
"""
One-time boundary re-registration (issue #91, part 2).

The frozen search grids were centered on the geocoder's reported point instead
of the midpoint of the OSM bounding box the grid dimensions are derived from
(fixed going forward in cli.py / geoutils.EnhancedLocation.bbox_center). This
left many cities sampling only part of their intended area. This script reads
the boundary audit report and corrects the catalog geometry for the flagged
cities.

Policy (per city, driven by the audit `verdict`):
  * UNDER, OSM bbox <= REVIEW_THRESHOLD on both axes -> RE-REGISTER: recenter on
    the OSM bbox midpoint and grow the grid to at least the OSM bbox (grow-only,
    so a grid is never shrunk below what was previously sampled). This makes the
    frozen rectangle cover ~100% of the OSM boundary.
  * UNDER, OSM bbox larger than REVIEW_THRESHOLD on either axis -> MANUAL REVIEW.
    These are huge administrative boundaries (e.g. Đà Nẵng's 194×153 km
    municipality) whose true urban extent is far smaller; a human picks the
    sample size.
  * WRONG_PLACE / NO_POLYGON / NOT_FOUND / BBOX_SUSPECT / OVER -> MANUAL REVIEW
    (geometry left untouched — a wrong OSM match must not be applied blindly).
  * OK -> skipped silently.

Grid geometry is otherwise immutable; this is the deliberate escape hatch
(db.update_city_geometry). Re-registration makes ZERO provider API calls — it
only edits the catalog. The correction resets run-to-run diff continuity, but
the flagged cities are baseline-only (no real diffs exist), so nothing is lost.

Idempotent: current geometry is read live from the DB, so a city already at its
target geometry is reported as no-change and re-running after --execute applies
nothing.

Usage:
    python scripts/reregister_boundaries.py                 # dry run (default)
    python scripts/reregister_boundaries.py --execute       # apply changes
    python scripts/reregister_boundaries.py --report PATH --db-path PATH
"""

import argparse
import csv
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.boundary_audit import (  # noqa: E402
    bbox_intersection_frac,
    frozen_rect_bounds,
)
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("reregister")

# Auto-resize (unattended) only up to this grid dimension; larger OSM boxes are
# routed to manual review rather than grown without a human look. Deliberately
# conservative — smaller than the recommendation ceiling below.
REVIEW_THRESHOLD_M = 30000

# Ceiling for the "best size" RECOMMENDATION written to the manual-review file.
# We bias BIGGER — recommend the full OSM bbox so an analysis can later clip to
# the always-smaller polygon — capping only the enormous administrative units
# (Đà Nẵng's 194×153 km) whose collection time is impractical. Kept at the
# historical 80 km this one-time #91 tool actually ran with; the going-forward
# registration ceiling (cli.MAX_GRID_DIM_M) has since dropped to 40 km
# (issue #166), enforced retroactively by scripts/cap_oversized_grids.py.
REC_CEILING_M = 80000

# Default locations, relative to the repo root.
DEFAULT_REPORT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "audit",
    "boundary_audit_report.csv",
)
PLAN_CSV = "reregister_plan.csv"
MANUAL_CSV = "manual_review.csv"

# Tolerances for "already at target geometry" (idempotency).
_CENTER_EPS_DEG = 1e-7

# Note stamped onto re-registered rows for the audit trail.
REGEOM_NOTE = "regeom #91: recenter on OSM bbox midpoint + grow to OSM bbox"


@dataclass
class Current:
    """A city's current frozen geometry, read live from the catalog."""

    center_lat: float
    center_lon: float
    width_m: int
    height_m: int
    step_m: int


@dataclass
class Decision:
    """The action to take for one audited city."""

    city_id: str
    display_name: str
    verdict: str
    action: str  # RESIZE | DEFER | NOCHANGE | SKIP
    reason: str
    old_center_lat: float | None = None
    old_center_lon: float | None = None
    old_width_m: int | None = None
    old_height_m: int | None = None
    new_center_lat: float | None = None
    new_center_lon: float | None = None
    new_width_m: int | None = None
    new_height_m: int | None = None
    old_points: int | None = None
    new_points: int | None = None
    coverage_before: float | None = None
    coverage_after: float | None = None
    # Recommendation for the manual-review cases (DEFER) — advisory only.
    rec_center_lat: float | None = None
    rec_center_lon: float | None = None
    rec_width_m: int | None = None
    rec_height_m: int | None = None
    rec_basis: str | None = None


# A frozen grid within this fraction of the OSM bbox area is "already the right
# size, just off-center" — recommend a recenter rather than a resize.
_OFFSET_RATIO = 0.9


def _grid_points(width_m: float, height_m: float, step_m: float) -> int:
    """Number of grid sample points for a rectangle at a given step."""
    return (int(width_m / step_m) + 1) * (int(height_m / step_m) + 1)


def _num(row: dict, key: str) -> float | None:
    """Parse a possibly-blank/NaN CSV cell to float, or None."""
    val = row.get(key, "")
    if val is None or val == "":
        return None
    try:
        f = float(val)
    except ValueError:
        return None
    return None if math.isnan(f) else f


def recommend(row: dict, current: Current, ceiling_m: float = REC_CEILING_M) -> dict:
    """
    Advisory "best size" for a deferred (manual-review) city.

    Bias BIGGER: recommend the full OSM bounding box (so a later analysis can
    clip to the always-smaller polygon), per-axis capped only at ``ceiling_m``
    for the enormous administrative units whose collection time is impractical.
    Never shrinks below the current grid. Advisory only — never auto-applied.
    Returns rec_* fields for the Decision.
    """
    verdict = row["verdict"]
    osm_w, osm_h = _num(row, "osm_bbox_width_m"), _num(row, "osm_bbox_height_m")
    sug_lat, sug_lon = _num(row, "suggested_center_lat"), _num(row, "suggested_center_lon")
    over = _num(row, "over_area_ratio")
    poly_area = _num(row, "osm_polygon_area_km2")
    center_dist = _num(row, "center_dist_km")
    have_bbox = None not in (osm_w, osm_h, sug_lat, sug_lon)

    # Default: leave geometry as-is until a human confirms.
    rc_lat, rc_lon = current.center_lat, current.center_lon
    rw, rh = current.width_m, current.height_m
    basis = "keep current geometry; verify manually"

    if verdict == "OVER":
        # Frozen grid is larger than the OSM bbox — but bigger is safe (clip to
        # the polygon later), so keep it. Do NOT shrink.
        basis = "keep current (oversized is safe — clip to polygon later; do not shrink)"

    elif verdict == "UNDER" and have_bbox:
        if over is not None and over >= _OFFSET_RATIO:
            # Grid already ≈ OSM bbox size: just off-center. A recenter alone
            # fixes coverage — safe to apply, no resize needed.
            rc_lat, rc_lon = sug_lat, sug_lon
            basis = "recenter only — grid already ≈ OSM bbox (safe to apply as-is)"
        else:
            # Undersized grid inside a larger boundary: grow to the full OSM
            # bbox (grow-only), per-axis capped at the practical ceiling.
            rw = int(min(ceiling_m, max(current.width_m, osm_w)))
            rh = int(min(ceiling_m, max(current.height_m, osm_h)))
            if center_dist is not None and center_dist <= 5:
                rc_lat, rc_lon = sug_lat, sug_lon
            capped = " (capped)" if (osm_w > ceiling_m or osm_h > ceiling_m) else ""
            poly = f", polygon ≈{poly_area:.0f}km²" if poly_area else ""
            basis = (
                f"grow to full OSM bbox {osm_w / 1000:.0f}x{osm_h / 1000:.0f}km"
                f"{poly} -> {rw / 1000:.0f}x{rh / 1000:.0f}km{capped}; verify center"
            )

    elif verdict == "WRONG_PLACE":
        cand = int(min(ceiling_m, max(osm_w or 0, osm_h or 0))) if have_bbox else current.width_m
        basis = (
            f"verify geocode first — frozen center is OUTSIDE the OSM bbox; "
            f"if the match is correct, ~{cand / 1000:.0f}km on the OSM bbox"
        )

    elif verdict == "NO_POLYGON" and have_bbox:
        # Recenter + grow to the full bbox (capped); bigger is safe.
        rc_lat, rc_lon = sug_lat, sug_lon
        rw = int(min(ceiling_m, max(current.width_m, osm_w)))
        rh = int(min(ceiling_m, max(current.height_m, osm_h)))
        basis = "no polygon, but a usable bbox — recenter + grow to it"

    # NOT_FOUND / BBOX_SUSPECT / no-bbox cases fall through to "keep current".
    return dict(
        rec_center_lat=round(rc_lat, 6),
        rec_center_lon=round(rc_lon, 6),
        rec_width_m=rw,
        rec_height_m=rh,
        rec_basis=basis,
    )


def decide(row: dict, current: Current | None, threshold_m: float = REVIEW_THRESHOLD_M) -> Decision:
    """
    Pure policy: map one audit-report row + current geometry to a Decision.

    Reads the OSM suggestion from the report but the *current* geometry from
    the catalog (passed in), so the result is idempotent and network-free.
    """
    city_id = row["city_id"]
    name = row.get("display_name", city_id)
    verdict = row["verdict"]

    def d(action, reason, **kw):
        return Decision(
            city_id=city_id, display_name=name, verdict=verdict, action=action, reason=reason, **kw
        )

    def defer(reason):
        # Deferred cities carry both current geometry and an advisory
        # recommended geometry, for side-by-side human review.
        return d(
            "DEFER",
            reason,
            old_center_lat=current.center_lat,
            old_center_lon=current.center_lon,
            old_width_m=current.width_m,
            old_height_m=current.height_m,
            **recommend(row, current),
        )

    if current is None:
        return d("SKIP", "not found in catalog")
    if verdict == "OK":
        return d("SKIP", "coverage OK")
    if verdict != "UNDER":
        return defer(f"{verdict}: needs human review, geometry untouched")

    osm_w = _num(row, "osm_bbox_width_m")
    osm_h = _num(row, "osm_bbox_height_m")
    sug_lat = _num(row, "suggested_center_lat")
    sug_lon = _num(row, "suggested_center_lon")
    if None in (osm_w, osm_h, sug_lat, sug_lon):
        return defer("UNDER but OSM bbox/center missing in report")

    if osm_w > threshold_m or osm_h > threshold_m:
        return defer(
            f"OSM bbox {osm_w / 1000:.0f}x{osm_h / 1000:.0f}km exceeds "
            f"{threshold_m / 1000:.0f}km review threshold"
        )

    # Recenter on the OSM midpoint; grow the grid to at least the OSM bbox.
    new_w = int(max(current.width_m, osm_w))
    new_h = int(max(current.height_m, osm_h))

    # Reconstruct the OSM bbox (suggested center is its midpoint) to score
    # coverage before/after — an offline sanity check on the fix.
    osm_bbox = frozen_rect_bounds(sug_lat, sug_lon, osm_w, osm_h)
    cov_before = bbox_intersection_frac(
        frozen_rect_bounds(
            current.center_lat, current.center_lon, current.width_m, current.height_m
        ),
        osm_bbox,
    )
    cov_after = bbox_intersection_frac(frozen_rect_bounds(sug_lat, sug_lon, new_w, new_h), osm_bbox)

    common = dict(
        old_center_lat=current.center_lat,
        old_center_lon=current.center_lon,
        old_width_m=current.width_m,
        old_height_m=current.height_m,
        new_center_lat=sug_lat,
        new_center_lon=sug_lon,
        new_width_m=new_w,
        new_height_m=new_h,
        old_points=_grid_points(current.width_m, current.height_m, current.step_m),
        new_points=_grid_points(new_w, new_h, current.step_m),
        coverage_before=round(cov_before, 4),
        coverage_after=round(cov_after, 4),
    )

    already = (
        abs(current.center_lat - sug_lat) < _CENTER_EPS_DEG
        and abs(current.center_lon - sug_lon) < _CENTER_EPS_DEG
        and current.width_m == new_w
        and current.height_m == new_h
    )
    if already:
        return d("NOCHANGE", "already at target geometry", **common)
    return d("RESIZE", "recenter on OSM midpoint + grow to OSM bbox", **common)


def load_current_geometry(conn) -> dict:
    """Map city_id -> Current for every city in the catalog."""
    rows = conn.execute(
        "SELECT city_id, center_lat, center_lon, grid_width_m, grid_height_m, step_m FROM cities"
    ).fetchall()
    return {
        r["city_id"]: Current(
            r["center_lat"], r["center_lon"], r["grid_width_m"], r["grid_height_m"], r["step_m"]
        )
        for r in rows
    }


_PLAN_FIELDS = [
    "city_id",
    "display_name",
    "verdict",
    "reason",
    "old_center_lat",
    "old_center_lon",
    "old_width_m",
    "old_height_m",
    "new_center_lat",
    "new_center_lon",
    "new_width_m",
    "new_height_m",
    "old_points",
    "new_points",
    "coverage_before",
    "coverage_after",
]
_MANUAL_FIELDS = [
    "city_id",
    "display_name",
    "verdict",
    "reason",
    "old_center_lat",
    "old_center_lon",
    "old_width_m",
    "old_height_m",
    "rec_center_lat",
    "rec_center_lon",
    "rec_width_m",
    "rec_height_m",
    "rec_basis",
]


def _write_csv(path: str, fields: list, decisions: list) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for dec in decisions:
            w.writerow(asdict(dec))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--report",
        default=DEFAULT_REPORT,
        help="boundary audit report CSV (default: audit/boundary_audit_report.csv)",
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--db-path", default=None, help="default: {data-dir}/streetscape_tracker.db"
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="where to write the plan/manual-review CSVs (default: alongside the report)",
    )
    parser.add_argument(
        "--execute", action="store_true", help="Apply changes (default is a dry run)"
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.report))
    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== Boundary re-registration ({mode}) ===")
    print(f"Report:  {args.report}")
    print(f"Catalog: {db_path}\n")

    conn = db.connect(db_path)
    current_by_id = load_current_geometry(conn)

    with open(args.report, newline="") as f:
        decisions = [decide(row, current_by_id.get(row["city_id"])) for row in csv.DictReader(f)]

    resize = [d for d in decisions if d.action == "RESIZE"]
    defer = [d for d in decisions if d.action == "DEFER"]
    nochange = [d for d in decisions if d.action == "NOCHANGE"]

    plan_path = os.path.join(out_dir, PLAN_CSV)
    manual_path = os.path.join(out_dir, MANUAL_CSV)
    _write_csv(plan_path, _PLAN_FIELDS, resize + nochange)
    _write_csv(manual_path, _MANUAL_FIELDS, defer)

    point_delta = sum((d.new_points or 0) - (d.old_points or 0) for d in resize)
    print(f"Re-register (recenter + resize): {len(resize)}")
    print(f"Already at target (no change):   {len(nochange)}")
    print(f"Deferred to manual review:       {len(defer)}")
    print(
        f"Grid-point delta from resizes:   {point_delta:+,} "
        f"(one-time; free GSV metadata, ~{point_delta // 90:+,}/day over the 90-day cycle)"
    )
    print(f"\nPlan written to:          {plan_path}")
    print(f"Manual-review list:       {manual_path}")

    if not args.execute:
        print("\nDRY RUN — no catalog changes made. Re-run with --execute to apply.")
        conn.close()
        return 0

    for d in resize:
        db.update_city_geometry(
            conn,
            city_id=d.city_id,
            center_lat=d.new_center_lat,
            center_lon=d.new_center_lon,
            grid_width_m=d.new_width_m,
            grid_height_m=d.new_height_m,
            notes=REGEOM_NOTE,
        )
    conn.close()
    print(f"\nApplied {len(resize)} re-registrations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
