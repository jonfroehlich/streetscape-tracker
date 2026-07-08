#!/usr/bin/env python3
"""
Register the worldwide sampling-frame cities in the catalog, freezing each
city's grid geometry — WITHOUT downloading any imagery.

This lets the boundary-audit workflow (scripts/audit_city_boundaries.py ->
build_boundary_review.py -> apply_decisions.py) vet the grids BEFORE the first
collection, which matters more for international cities (OSM boundary quality
varies). Only after vetting should a city be enabled in the scheduler.

Identity is pinned to the vendored GeoNames ASCII names (city asciiname +
admin-1 ASCII name + English country name), NOT the free-form geocoder
response. This keeps city_ids/filenames/URLs ASCII, comma-free, and consistent
with the existing (US) dataset — e.g. "sao-paulo--sao-paulo--brazil" rather than
the geocoder's "são-paulo--..." or a comma-mangled "bogota--bogota--capital-
district--colombia". Geometry (center + dimensions) still comes from geocoding;
this mirrors cli._resolve_geometry's new-city branch (helpers imported below) but
supplies the identity ourselves.

Reads the manifest produced by scripts/build_worldwide_frame.py. Idempotent —
already-registered cities are skipped, so it is safe to re-run and resumable via
``--limit``. No API keys needed (geocoding is rate-limited Nominatim).

Usage:
    python scripts/register_frame.py --dry-run          # preview (no geocoding)
    python scripts/register_frame.py --limit 5          # register first 5
    python scripts/register_frame.py                    # register all
"""

import argparse
import csv
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gsv_metadata_tracker import db, get_city_location_data, get_search_dimensions  # noqa: E402
from gsv_metadata_tracker.cli import _cap_dimensions, _resolve_center  # noqa: E402
from gsv_metadata_tracker.naming import sanitize_city_query_str  # noqa: E402
from gsv_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worldwide_frame.csv"
)


def load_frame(manifest_path):
    """Return the frame manifest rows (dicts), in order."""
    with open(manifest_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def register_city(conn, row, step):
    """
    Register one frame city with GeoNames ASCII identity + geocoded geometry.

    Mirrors cli._resolve_geometry's new-city branch (center from the OSM bbox,
    dimensions from the boundary, clamped to MAX_GRID_DIM_M) but pins the
    city/state/country names to the vendored GeoNames ASCII values so the
    canonical city_id is stable and ASCII. Returns the registered CityRow.
    """
    query = row["query_string"]
    loc = get_city_location_data(query)
    if loc is None:
        raise ValueError("could not geocode")
    center = _resolve_center(loc)
    if center is None:
        raise ValueError("no center from geocode")
    center_lat, center_lon = center

    grid_width, grid_height = get_search_dimensions(query, 1000, 1000)
    grid_width, grid_height = _cap_dimensions(grid_width, grid_height, query)

    # Drop a redundant admin component (admin == city, e.g. Sao Paulo state) so
    # the slug is "sao-paulo--brazil", matching query_string's own logic.
    state_name = row["admin"] or None
    if state_name and state_name.lower() == row["city"].lower():
        state_name = None

    city_id = db.register_city(
        conn,
        city_name=row["city"],
        state_name=state_name,
        state_code=None,
        country_name=row["country"],
        country_code=(row["iso2"] or None),
        center_lat=center_lat,
        center_lon=center_lon,
        grid_width_m=grid_width,
        grid_height_m=grid_height,
        step_m=step,
    )
    # Alias the query slug to the canonical id so `gsv_tracker.py "<query>"`
    # resolves without geocoding (usually identical, since query_string is built
    # from the same GeoNames ASCII names, but admin punctuation can differ).
    query_slug = sanitize_city_query_str(query)
    if query_slug != city_id:
        db.add_alias(conn, query_slug, city_id)
    conn.commit()
    return db.resolve_city(conn, city_id)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST,
        help="frame manifest CSV (default: worldwide_frame.csv)",
    )
    p.add_argument(
        "--step",
        type=int,
        default=20,
        help="grid step in meters for new registrations (default: 20)",
    )
    p.add_argument(
        "--limit", type=int, default=None, help="only process the first N cities (resume-friendly)"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="report registered/new without geocoding or writing"
    )
    p.add_argument(
        "--db-path", default=None, help="catalog path (default: the standard data-dir DB)"
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
    )

    frame = load_frame(args.manifest)
    if args.limit is not None:
        frame = frame[: args.limit]

    db_path = args.db_path or db.get_default_db_path(get_default_data_dir())
    conn = db.connect(db_path)

    already = new = failed = 0
    try:
        for i, row in enumerate(frame, 1):
            query = row["query_string"]
            prefix = f"[{i}/{len(frame)}] {query}"
            existing = db.resolve_city(conn, query)
            if existing is not None:
                print(f"{prefix} -> already registered as {existing.city_id}")
                already += 1
                continue
            if args.dry_run:
                print(f"{prefix} -> NEW (would geocode + register)")
                new += 1
                continue
            try:
                city_row = register_city(conn, row, args.step)
                print(
                    f"{prefix} -> registered {city_row.city_id} "
                    f"({city_row.grid_width_m:.0f}x{city_row.grid_height_m:.0f}m "
                    f"@ step {city_row.step_m}m)"
                )
                new += 1
            except Exception as e:
                logger.warning(f"{prefix} -> {e}; skipped")
                failed += 1
    finally:
        conn.close()

    print(f"\nDone. already-registered={already} newly-registered={new} failed={failed}")
    print(
        "Next: run the boundary-audit workflow on these cities before "
        "enabling them in the scheduler."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
