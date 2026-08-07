#!/usr/bin/env python3
"""
Cap frozen grids that are too large for a single night's collection (issue #166).

A handful of cities have frozen grids so big they are effectively uncollectable,
and because the scheduler works a stalest-first queue serially, each one eats
hours that the rest of the frame never gets back. Sydney is 100x99 km — nearly
25M grid points. Cairo is 10.5M, which is more than the entire daily gsv budget,
so the scheduler has logged "exceeds the entire daily budget ... Skipping" every
night since it was registered and has never collected it once.

This clamps each dimension to ``--max-extent-m`` (keeping the existing center),
which bounds both the request count and the downloader's peak memory — the
Mapillary post-decode path costs roughly 1.67 GB per 1M grid points, so an
uncapped city is also an OOM risk (see #157).

Sampling a bounded window around the core is more useful than an unbounded box
that never completes at all.

SAFETY. Geometry is frozen on purpose: re-gridding a city starts a new,
non-comparable series because run-to-run diffs no longer align on an identical
rectangle. So this refuses any city with a real (non-baseline) dated run unless
``--include-collected`` is given. Cities that were never collected, or that have
only imported baseline runs, have no diff continuity to lose and are resized
freely.

SCOPE. Only *enabled* cities are swept by default — a disabled city costs the
scheduler nothing tonight. But a disabled city registered under the old 80 km
ceiling is a landmine: enabling it later re-creates exactly the #166 failure,
and the registration-time cap can't help because it only runs at registration.
``--include-disabled`` sweeps them too; the default run reports how many are
sitting there.

Nothing is ever deleted. This edits catalog rows only — existing run files stay
on disk and their published URLs keep working. Makes ZERO provider API calls.

Usage:
    # See what would change (default is a dry run):
    python scripts/cap_oversized_grids.py

    # Apply to the cities that lose nothing:
    python scripts/cap_oversized_grids.py --execute

    # Also re-grid cities that already have real dated runs (breaks their diff
    # continuity — the files remain, but new runs no longer diff against old):
    python scripts/cap_oversized_grids.py --include-collected --execute

    # Also cap disabled cities, so enabling one later can't reintroduce #166:
    python scripts/cap_oversized_grids.py --include-disabled --execute
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import cli, db  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("cap_oversized_grids")

# Default cap per side — the same ceiling cli.py applies to newly registered
# cities, so a city registered today can't need capping tomorrow. At the
# standard 20 m step this is ~4M grid points, which fits inside the production
# gsv daily budget (10M) with room for other cities, and is still roughly twice
# Seattle's grid — comfortably more than an urban core. Deliberately a
# *dimension* cap rather than a point cap so the resulting rectangle stays
# square-ish and legible on the map.
DEFAULT_MAX_EXTENT_M = cli.MAX_GRID_DIM_M

CAP_NOTE = "grid capped to {extent} m/side (scripts/cap_oversized_grids.py, issue #166)"


def grid_points(width_m: float, height_m: float, step_m: float) -> int:
    """Number of grid sample points for a rectangle at a given step."""
    return (int(width_m / step_m) + 1) * (int(height_m / step_m) + 1)


def find_oversized(conn, max_extent_m: float, include_disabled: bool = False) -> list[dict]:
    """
    Cities with either dimension over the cap, largest first.

    Enabled cities only unless ``include_disabled`` — a disabled city burns no
    collection time today, but it keeps its oversized geometry until someone
    enables it (see SCOPE above). Entries carry ``enabled`` so callers can
    report the two groups separately.

    Each entry also carries the proposed dimensions and the run history that
    decides whether resizing is free: ``real_runs`` (dated, non-baseline) is
    what makes a resize destructive to diff continuity.
    """
    rows = conn.execute(
        "SELECT city_id, display_name, center_lat, center_lon, "
        "grid_width_m, grid_height_m, step_m, enabled FROM cities"
    ).fetchall()

    oversized = []
    for row in rows:
        if not row["enabled"] and not include_disabled:
            continue
        if row["grid_width_m"] <= max_extent_m and row["grid_height_m"] <= max_extent_m:
            continue
        runs = db.get_runs_for_city(conn, row["city_id"], provider=None)
        real_runs = [r for r in runs if not r.is_baseline]
        oversized.append(
            {
                "city_id": row["city_id"],
                "display_name": row["display_name"],
                "center_lat": row["center_lat"],
                "center_lon": row["center_lon"],
                "step_m": row["step_m"],
                "enabled": bool(row["enabled"]),
                "old_width_m": row["grid_width_m"],
                "old_height_m": row["grid_height_m"],
                "new_width_m": min(row["grid_width_m"], max_extent_m),
                "new_height_m": min(row["grid_height_m"], max_extent_m),
                "baseline_runs": len(runs) - len(real_runs),
                "real_runs": len(real_runs),
            }
        )

    for c in oversized:
        c["old_points"] = grid_points(c["old_width_m"], c["old_height_m"], c["step_m"])
        c["new_points"] = grid_points(c["new_width_m"], c["new_height_m"], c["step_m"])
    oversized.sort(key=lambda c: c["old_points"], reverse=True)
    return oversized


def _report_untouched_disabled(disabled: list[dict], include_disabled: bool) -> None:
    """Warn about oversized *disabled* cities a default run is leaving behind.

    They cost nothing tonight, which is why they are skipped — but nothing else
    in the system will ever cap them, so enabling one is a live #166 relapse.
    """
    if not disabled or include_disabled:
        return
    print(
        f"\nNOTE: {len(disabled)} disabled cities are also over the cap and were not "
        f"listed. Enabling one reintroduces the oversized grid this script exists to "
        f"prevent (the registration-time cap only applies at registration). Re-run "
        f"with --include-disabled to cap them too:"
    )
    for c in sorted(disabled, key=lambda c: c["old_points"], reverse=True)[:10]:
        print(
            f"  {c['city_id'][:52]:52s} "
            f"{int(c['old_width_m']):>8,}x{int(c['old_height_m']):<9,} "
            f"{c['old_points']:>12,} points"
        )
    if len(disabled) > 10:
        print(f"  ... and {len(disabled) - 10} more")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--max-extent-m",
        type=float,
        default=DEFAULT_MAX_EXTENT_M,
        help=f"Cap each grid dimension at this many meters (default {DEFAULT_MAX_EXTENT_M:,})",
    )
    parser.add_argument(
        "--include-collected",
        action="store_true",
        help="Also resize cities with real dated runs, breaking their diff continuity",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Also resize disabled cities, so enabling one later can't reintroduce #166",
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--db-path", default=None, help="default: {data-dir}/streetscape_tracker.db"
    )
    parser.add_argument("--execute", action="store_true", help="Apply (default is a dry run)")
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.max_extent_m <= 0:
        parser.error("--max-extent-m must be positive")

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    conn = db.connect(db_path)

    # Always look at the whole catalog so a default run can still *report* the
    # disabled cities it is deliberately leaving alone.
    all_oversized = find_oversized(conn, args.max_extent_m, include_disabled=True)
    disabled = [c for c in all_oversized if not c["enabled"]]
    oversized = (
        all_oversized if args.include_disabled else [c for c in all_oversized if c["enabled"]]
    )

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== Cap oversized grids to {int(args.max_extent_m):,} m/side ({mode}) ===")
    print(f"Catalog: {db_path}")

    if not oversized:
        scope = "city" if args.include_disabled else "enabled city"
        print(f"\nNo {scope} exceeds the cap. Nothing to do.")
        _report_untouched_disabled(disabled, args.include_disabled)
        conn.close()
        return 0

    free = [c for c in oversized if c["real_runs"] == 0]
    collected = [c for c in oversized if c["real_runs"] > 0]

    print(f"\n{len(oversized)} oversized cities: {len(free)} free to resize, ")
    print(f"{len(collected)} with real dated runs (diff continuity at stake).\n")

    header = f"{'city_id':52s} {'old':>18s} {'points':>12s} -> {'points':>12s}  runs"
    print(header)
    print("-" * len(header))
    for c in oversized:
        runs = f"{c['baseline_runs']}b/{c['real_runs']}r"
        flag = "" if c["enabled"] else "  [DISABLED]"
        if c["real_runs"]:
            flag += "  [FORCED]" if args.include_collected else "  [SKIP]"
        print(
            f"{c['city_id'][:52]:52s} "
            f"{int(c['old_width_m']):>8,}x{int(c['old_height_m']):<9,} "
            f"{c['old_points']:>12,} -> {c['new_points']:>12,}  {runs}{flag}"
        )

    targets = oversized if args.include_collected else free
    total_before = sum(c["old_points"] for c in targets)
    total_after = sum(c["new_points"] for c in targets)
    print(
        f"\nWould resize {len(targets)} cities: "
        f"{total_before:,} -> {total_after:,} grid points "
        f"({total_after - total_before:+,})"
    )
    if collected and not args.include_collected:
        print(
            f"Skipping {len(collected)} cities with real dated runs. "
            f"Re-run with --include-collected to resize them too "
            f"(no files are deleted; only diff continuity is lost)."
        )
    _report_untouched_disabled(disabled, args.include_disabled)

    if not args.execute:
        print("\nDRY RUN — no catalog changes. Re-run with --execute to apply.")
        conn.close()
        return 0

    note = CAP_NOTE.format(extent=int(args.max_extent_m))
    for c in targets:
        db.update_city_geometry(
            conn,
            city_id=c["city_id"],
            center_lat=c["center_lat"],
            center_lon=c["center_lon"],
            grid_width_m=c["new_width_m"],
            grid_height_m=c["new_height_m"],
            notes=note,
        )
    conn.close()
    print(f"\nApplied to {len(targets)} cities.")
    print("Run files on disk are untouched; only the catalog geometry changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
