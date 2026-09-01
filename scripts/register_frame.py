#!/usr/bin/env python3
"""
Register the cities in a frame manifest, freezing each city's grid geometry
— WITHOUT downloading any imagery.

This lets the boundary-audit workflow (scripts/audit_city_boundaries.py ->
build_boundary_review.py -> apply_decisions.py) vet the grids BEFORE the first
collection, which matters more for international cities (OSM boundary quality
varies). Only after vetting should a city be enabled in the scheduler.

Identity is pinned to the vendored GeoNames ASCII names (city asciiname +
admin-1 ASCII name + English country name), NOT the free-form geocoder
response. This keeps city_ids/filenames/URLs ASCII, comma-free, and consistent
with the existing (US) dataset — e.g. "sao-paulo--brazil" rather than the
geocoder's "são-paulo--..." or a comma-mangled "bogota--bogota--capital-
district--colombia". Geometry (center + dimensions) still comes from geocoding;
this mirrors cli._resolve_geometry's new-city branch (helpers imported below)
but supplies the identity ourselves.

Reads a manifest in the format scripts/build_worldwide_frame.py writes: the
worldwide sampling frame by default, or a purposive list such as
mapillary_360_cities.csv via --manifest (docs/worldwide_sampling.md).
Identify the batch in the catalog with --notes-label. Idempotent —
already-registered cities are skipped, so it is safe to re-run and resumable
via ``--limit``. Makes ZERO provider API calls (geocoding is rate-limited
Nominatim, no keys needed). Dry-run by default; pass --execute to write.

Safety rails (issue #110):
  * **Overlap reuse.** A frame city whose GeoNames coordinates fall within
    ``--overlap-km`` (default 25) of an already-registered city — under ANY
    slug, e.g. the geocoder-derived "são-paulo--são-paulo--brazil" — is treated
    as that existing city: its frame slugs are aliased to the existing city_id
    and no duplicate row (or duplicate collection series) is created.
  * **Registered disabled.** New cities are written with ``enabled=0`` so the
    scheduler cannot pick them up before boundary vetting; flip to enabled
    after the audit workflow accepts the grid.
  * **Center guard + query fallback.** A geocode attempt whose center lands
    more than ``--max-center-km`` (default 50) from the GeoNames coordinates is
    distrusted (big non-US metros can geocode to a province centroid —
    Ho Chi Minh City once landed ~100 km off). When the manifest query fails to
    geocode or trips the guard, a bare "City, Country" query is tried next
    (GeoNames admin-1 names don't always match Nominatim's). If nothing passes,
    the city is NOT registered; re-run with ``--center-from-geonames`` to use
    the GeoNames coordinates as the center instead, or fix by hand.

Usage:
    python scripts/register_frame.py                    # dry-run preview (default)
    python scripts/register_frame.py --execute --limit 5
    python scripts/register_frame.py --execute
"""

import argparse
import csv
import logging
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_worldwide_frame import effective_admin  # noqa: E402
from streetscape_metadata_tracker import (  # noqa: E402
    db,
    get_city_location_data,
    get_search_dimensions,
)
from streetscape_metadata_tracker.city_registration import (  # noqa: E402
    cap_dimensions as _cap_dimensions,
)
from streetscape_metadata_tracker.city_registration import (  # noqa: E402
    resolve_center as _resolve_center,
)
from streetscape_metadata_tracker.naming import sanitize_city_query_str  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worldwide_frame.csv"
)

DEFAULT_NOTES_LABEL = "worldwide frame"
DEFAULT_OVERLAP_KM = 25.0
DEFAULT_MAX_CENTER_KM = 50.0


def load_frame(manifest_path):
    """Return the frame manifest rows (dicts), in order."""
    with open(manifest_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two (lat, lon) points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def find_overlap(existing_cities, lat, lon, overlap_km):
    """
    The nearest already-registered city within ``overlap_km`` of (lat, lon), or
    None. Distance-only on purpose: the overlaps we must catch are the same
    physical city under a *different* slug (geocoder-derived, often non-ASCII),
    so name matching cannot be trusted.
    """
    best, best_km = None, overlap_km
    for city in existing_cities:
        d = _haversine_km(lat, lon, city.center_lat, city.center_lon)
        if d <= best_km:
            best, best_km = city, d
    return best


def frame_identity(row):
    """
    (city_name, state_name, country_name) for catalog identity, with the
    admin-1 dropped when it would just duplicate the city name (same rule as
    the query strings — see build_worldwide_frame.effective_admin).
    """
    return row["city"], effective_admin(row["city"], row["admin"] or None), row["country"]


def geocode_queries(row):
    """
    Query strings to try, most specific first: the manifest query, then a bare
    "City, Country". GeoNames admin-1 names sometimes don't match Nominatim's
    ("State of Vienna", "Matanzas Province"), which either kills the geocode
    outright or matches the wrong feature entirely.
    """
    query = row["query_string"]
    fallback = f"{row['city']}, {row['country']}"
    return [query] if fallback == query else [query, fallback]


def register_frame_city(conn, row, step, use_geonames_center, max_center_km, notes_label):
    """
    Register one manifest city with GeoNames ASCII identity + geocoded geometry.

    Mirrors cli._resolve_geometry's new-city branch (center from the OSM bbox,
    dimensions from the boundary, clamped to MAX_GRID_DIM_M) but pins the
    city/state/country names to the vendored GeoNames ASCII values so the
    canonical city_id is stable and ASCII. The city is registered disabled
    (kept out of the scheduler until boundary-vetted). Returns the registered
    CityRow.

    Tries each candidate from ``geocode_queries`` until one geocodes AND its
    center passes the GeoNames distance guard. Raises ValueError when nothing
    geocodes, or when every geocode lands more than ``max_center_km`` from the
    GeoNames coordinates (unless ``use_geonames_center``, which registers the
    first geocoded candidate with the GeoNames coordinates as center).
    """
    query = row["query_string"]
    geo_lat, geo_lon = float(row["lat"]), float(row["lon"])

    chosen = best = None  # (geocode_query, center_lat, center_lon, offset_km)
    for candidate in geocode_queries(row):
        loc = get_city_location_data(candidate)
        if loc is None:
            continue
        center = _resolve_center(loc)
        if center is None:
            continue
        offset_km = _haversine_km(center[0], center[1], geo_lat, geo_lon)
        if best is None:
            best = (candidate, center[0], center[1], offset_km)
        if offset_km <= max_center_km:
            chosen = (candidate, center[0], center[1], offset_km)
            break
        logger.warning(f"{candidate}: geocoded center {offset_km:.0f} km off GeoNames")

    if chosen is None and best is None:
        raise ValueError("could not geocode")
    if chosen is None:
        if not use_geonames_center:
            raise ValueError(
                f"geocoded center is {best[3]:.0f} km from the GeoNames "
                f"coordinates (> {max_center_km:.0f} km) — likely a province "
                f"centroid; re-run with --center-from-geonames or fix by hand"
            )
        logger.warning(f"{query}: using GeoNames coordinates ({geo_lat}, {geo_lon}) as center")
        chosen = (best[0], geo_lat, geo_lon, 0.0)

    geocode_query, center_lat, center_lon, _ = chosen
    if geocode_query != query:
        logger.info(f"{query}: geocoded via fallback query '{geocode_query}'")

    grid_width, grid_height = get_search_dimensions(geocode_query, 1000, 1000)
    grid_width, grid_height = _cap_dimensions(grid_width, grid_height, geocode_query)

    city_name, state_name, country_name = frame_identity(row)
    city_id = db.register_city(
        conn,
        city_name=city_name,
        state_name=state_name,
        state_code=None,
        country_name=country_name,
        country_code=(row["iso2"] or None),
        center_lat=center_lat,
        center_lon=center_lon,
        grid_width_m=grid_width,
        grid_height_m=grid_height,
        step_m=step,
        enabled=False,
        notes=f"{notes_label} (geonameid {row['geonameid']}); pending boundary vetting",
    )
    # Alias the query slug to the canonical id so `streetscape_tracker.py
    # "<query>"` resolves without geocoding (usually identical, since
    # query_string is built from the same GeoNames ASCII names, but admin
    # punctuation can differ).
    query_slug = sanitize_city_query_str(query)
    if query_slug != city_id:
        db.add_alias(conn, query_slug, city_id)
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
        "--execute",
        action="store_true",
        help="actually geocode and write to the catalog (default: dry-run preview)",
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
        "--overlap-km",
        type=float,
        default=DEFAULT_OVERLAP_KM,
        help="reuse an existing catalog city within this distance of the GeoNames "
        f"coordinates instead of registering a duplicate (default: {DEFAULT_OVERLAP_KM:.0f})",
    )
    p.add_argument(
        "--max-center-km",
        type=float,
        default=DEFAULT_MAX_CENTER_KM,
        help="reject a geocoded center farther than this from the GeoNames "
        f"coordinates (default: {DEFAULT_MAX_CENTER_KM:.0f})",
    )
    p.add_argument(
        "--center-from-geonames",
        action="store_true",
        help="when the geocoded center fails the --max-center-km guard, fall "
        "back to the GeoNames coordinates instead of skipping the city",
    )
    p.add_argument(
        "--notes-label",
        default=DEFAULT_NOTES_LABEL,
        help="batch label written into cities.notes, so a later reader can tell "
        "which manifest a city came from (default: %(default)s)",
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

    already = reused = new = failed = 0
    needs_review = []
    try:
        existing_cities = db.get_all_cities(conn)
        for i, row in enumerate(frame, 1):
            query = row["query_string"]
            prefix = f"[{i}/{len(frame)}] {query}"

            existing = db.resolve_city(conn, query)
            if existing is not None:
                print(f"{prefix} -> already registered as {existing.city_id}")
                already += 1
                continue

            overlap = find_overlap(
                existing_cities, float(row["lat"]), float(row["lon"]), args.overlap_km
            )
            if overlap is not None:
                km = _haversine_km(
                    float(row["lat"]), float(row["lon"]), overlap.center_lat, overlap.center_lon
                )
                if args.execute:
                    frame_slugs = {
                        sanitize_city_query_str(query),
                        db.derive_city_id(*frame_identity(row)),
                    }
                    for slug in sorted(frame_slugs - {overlap.city_id}):
                        db.add_alias(conn, slug, overlap.city_id)
                    print(f"{prefix} -> reused existing {overlap.city_id} ({km:.0f} km; aliased)")
                else:
                    print(
                        f"{prefix} -> REUSE existing {overlap.city_id} ({km:.0f} km; would alias)"
                    )
                reused += 1
                continue

            if not args.execute:
                print(f"{prefix} -> NEW (would geocode + register disabled)")
                new += 1
                continue
            try:
                city_row = register_frame_city(
                    conn,
                    row,
                    args.step,
                    args.center_from_geonames,
                    args.max_center_km,
                    args.notes_label,
                )
                print(
                    f"{prefix} -> registered {city_row.city_id} "
                    f"({city_row.grid_width_m:.0f}x{city_row.grid_height_m:.0f}m "
                    f"@ step {city_row.step_m}m, disabled until vetted)"
                )
                new += 1
                existing_cities.append(city_row)
            except Exception as e:
                logger.warning(f"{prefix} -> {e}; skipped")
                needs_review.append(query)
                failed += 1
    finally:
        conn.close()

    mode = "" if args.execute else " (dry run — nothing written; use --execute)"
    print(
        f"\nDone{mode}. already-registered={already} reused-existing={reused} "
        f"newly-registered={new} failed={failed}"
    )
    if needs_review:
        print("Needs manual review (not registered):")
        for query in needs_review:
            print(f"  - {query}")
    print(
        "Next: run the boundary-audit workflow on these cities, then set "
        "enabled=1 for the vetted ones."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
