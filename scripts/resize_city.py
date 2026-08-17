#!/usr/bin/env python3
"""
Manually resize a single city's frozen search grid.

Grid geometry is normally immutable so run-to-run diffs align on an identical
rectangle. This is the deliberate escape hatch (``db.update_city_geometry``)
for the case the bulk boundary re-registration (issue #91) can't fix: a town
whose OSM "populated place" bounding box is a tiny point-box, so growing *to
that bbox* still undersamples the visible street network (e.g. Browning, MT at
795x1001 m). Here a human eyeballs the map and picks a generous size.

Safety: changing geometry resets diff continuity, so this refuses any city that
already has a real (non-baseline) dated run unless ``--force`` is given — a
baseline-only city has no diffs to lose, so resizing it is free. Bias BIGGER:
oversampling is free (GSV metadata has no quota) and clips to the polygon later;
undersampling loses coverage permanently.

Makes ZERO provider API calls — it only edits the catalog. After resizing,
preview with::

    python streetscape_tracker.py "Browning, MT" --check-boundary

then collect when the rectangle looks right.

Usage:
    # Explicit dimensions, keep the current center:
    python scripts/resize_city.py "Browning, MT" --width 2500 --height 2500

    # Recenter on the fresh OSM bbox midpoint (one geocode) while resizing:
    python scripts/resize_city.py "Browning, MT" --width 2500 --height 2500 --recenter-osm

    # Recenter on explicit coordinates:
    python scripts/resize_city.py "Browning, MT" --width 2500 --height 2500 \
        --center-lat 48.560 --center-lon -113.014

    python scripts/resize_city.py "Browning, MT" --width 2500 --height 2500 --execute
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("resize_city")

RESIZE_NOTE = "manual resize (scripts/resize_city.py)"


def _grid_points(width_m: float, height_m: float, step_m: float) -> int:
    """Number of grid sample points for a rectangle at a given step."""
    return (int(width_m / step_m) + 1) * (int(height_m / step_m) + 1)


def _resolve_recenter(city_query: str, lat: float, lon: float) -> tuple[float, float]:
    """
    OSM bbox midpoint for ``city_query`` (a single geocode), falling back to the
    current center on any failure. Uses the same resolve_center logic as a real
    collection run so the recentered grid matches what the pipeline would pick.
    """
    from streetscape_metadata_tracker.city_registration import resolve_center
    from streetscape_metadata_tracker.geoutils import get_city_location_data

    loc = get_city_location_data(city_query, lat, lon)
    center = resolve_center(loc)
    if center is None:
        logger.warning("Geocode for '%s' returned no center; keeping current center.", city_query)
        return lat, lon
    return center


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("city", help="City query or canonical city_id (resolved via the catalog)")
    parser.add_argument("--width", type=float, required=True, help="New grid width in meters")
    parser.add_argument("--height", type=float, required=True, help="New grid height in meters")
    recenter = parser.add_mutually_exclusive_group()
    recenter.add_argument(
        "--recenter-osm",
        action="store_true",
        help="Recenter on the OSM bbox midpoint (one geocode). Default: keep current center.",
    )
    recenter.add_argument("--center-lat", type=float, help="Recenter latitude (needs --center-lon)")
    parser.add_argument("--center-lon", type=float, help="Recenter longitude (with --center-lat)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Resize even if the city has real (non-baseline) runs — breaks diff continuity",
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

    if (args.center_lat is None) != (args.center_lon is None):
        parser.error("--center-lat and --center-lon must be given together")
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    conn = db.connect(db_path)

    city = db.resolve_city(conn, args.city)
    if city is None:
        print(f"ERROR: '{args.city}' is not registered in the catalog.", file=sys.stderr)
        conn.close()
        return 1

    # Diff-continuity guard: real (dated, non-baseline) runs must not be silently
    # orphaned onto a differently-shaped grid.
    real_runs = [
        r for r in db.get_runs_for_city(conn, city.city_id, provider=None) if not r.is_baseline
    ]
    if real_runs and not args.force:
        providers = ", ".join(sorted({r.provider for r in real_runs}))
        print(
            f"REFUSING: '{city.display_name}' has {len(real_runs)} real (non-baseline) run(s) "
            f"[{providers}]. Resizing breaks diff continuity. Re-run with --force if intended.",
            file=sys.stderr,
        )
        conn.close()
        return 2

    # New center.
    if args.recenter_osm:
        new_lat, new_lon = _resolve_recenter(args.city, city.center_lat, city.center_lon)
    elif args.center_lat is not None:
        new_lat, new_lon = args.center_lat, args.center_lon
    else:
        new_lat, new_lon = city.center_lat, city.center_lon

    old_pts = _grid_points(city.grid_width_m, city.grid_height_m, city.step_m)
    new_pts = _grid_points(args.width, args.height, city.step_m)

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== Resize {city.display_name} ({mode}) ===")
    print(f"Catalog: {db_path}")
    print(
        f"  center: ({city.center_lat:.6f}, {city.center_lon:.6f}) "
        f"-> ({new_lat:.6f}, {new_lon:.6f})"
    )
    print(
        f"  dims:   {city.grid_width_m}x{city.grid_height_m} m -> {int(args.width)}x{int(args.height)} m"
    )
    print(f"  points: {old_pts:,} -> {new_pts:,} ({new_pts - old_pts:+,})")
    baseline_ct = len(db.get_runs_for_city(conn, city.city_id, provider=None)) - len(real_runs)
    print(
        f"  runs:   {baseline_ct} baseline, {len(real_runs)} real{' (FORCED)' if real_runs else ''}"
    )

    if not args.execute:
        print("\nDRY RUN — no catalog changes. Re-run with --execute to apply.")
        print(f'Then preview: python streetscape_tracker.py "{args.city}" --check-boundary')
        conn.close()
        return 0

    db.update_city_geometry(
        conn,
        city_id=city.city_id,
        center_lat=new_lat,
        center_lon=new_lon,
        grid_width_m=args.width,
        grid_height_m=args.height,
        notes=RESIZE_NOTE,
    )
    conn.close()
    print("\nApplied.")
    print(f'Preview: python streetscape_tracker.py "{args.city}" --check-boundary')
    return 0


if __name__ == "__main__":
    sys.exit(main())
