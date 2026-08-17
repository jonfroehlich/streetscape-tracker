"""
Resolve a city query to a catalog row, registering it (with frozen grid
geometry) the first time we see it.

This is the one place a city enters the catalog interactively. It lived inside
cli.py until issue #215 needed it from the scheduler too: the road-walk
collector requires an already-registered city (collect.py refuses an unknown
one), so a "same-day answer for a new city" command has to be able to register
one without going through a full grid collection.

It is a separate module rather than an import of cli.py on purpose — cli.py
pulls the collectors (download_gsv, download_mapillary and hence
mapbox-vector-tile, pandas' full analysis path) into whatever imports it, and
the scheduler has no business loading any of that to register a city. This
module needs only db + geoutils.

Grid geometry is FROZEN at registration and never re-derived: every future run
samples the identical lattice, which is what makes run-to-run diffs meaningful
(see db.py). So the numbers this module picks are permanent, modulo the
catalog-only escape hatches (scripts/resize_city.py, scripts/cap_oversized_grids.py).
"""

import logging

from . import db
from .geoutils import get_city_location_data, get_search_dimensions
from .naming import sanitize_city_query_str

logger = logging.getLogger(__name__)

# Ceiling for auto-derived grid dimensions, per side (issue #166; supersedes
# the 80 km value from #91). At the standard 20 m step, 40 km/side is ~4M grid
# points — inside the production gsv daily budget (10M) with room for other
# cities, and roughly twice Seattle's grid, comfortably more than an urban
# core. Production data showed the old 80 km clamp still admitted grids no
# night can absorb (Cairo ~10.5M points was skipped as over-budget every night,
# and its ZERO_RESULTS fill alone OOMed the Mapillary tail — see #157/#166).
# scripts/cap_oversized_grids.py applies this same cap retroactively; keep the
# two in sync by importing this constant. NB the budget math above assumes the
# standard 20 m step — this is a *dimension* cap, so a finer --step re-inflates
# the point count (40 km/side at step 10 is ~16M points, over budget again).
# Override with explicit width/height for a genuinely larger area.
MAX_GRID_DIM_M = 40_000


class CityResolutionError(RuntimeError):
    """A city query could not be resolved to coordinates."""


def resolve_center(city_loc_data):
    """
    Grid center from a geocode result: the OSM bounding-box midpoint when
    available (correct — the grid dimensions are derived from that same bbox,
    so the sampled rectangle actually covers the boundary), else the geocoder's
    reported point as a fallback. Returns (lat, lng) or None.
    """
    if city_loc_data is None:
        return None
    center = city_loc_data.bbox_center
    if center is not None:
        return center
    return (city_loc_data.latitude, city_loc_data.longitude)


def cap_dimensions(grid_width, grid_height, city):
    """Clamp auto-derived grid dimensions to MAX_GRID_DIM_M, warning if clamped."""
    capped_w = min(grid_width, MAX_GRID_DIM_M)
    capped_h = min(grid_height, MAX_GRID_DIM_M)
    if capped_w < grid_width or capped_h < grid_height:
        logger.warning(
            f"Derived grid for '{city}' is {grid_width:.0f}x{grid_height:.0f}m; "
            f"clamping to {capped_w:.0f}x{capped_h:.0f}m (the OSM boundary is far "
            f"larger than a typical city sample). Use --width/--height to override."
        )
    return capped_w, capped_h


def resolve_or_register_city(
    conn,
    *,
    query: str,
    lat: float | None = None,
    lng: float | None = None,
    width: float | None = None,
    height: float | None = None,
    step: float = 20,
) -> tuple[db.CityRow, bool]:
    """
    Resolve a city query's identity and grid geometry, registering it if new.

    Registered cities reuse their frozen geometry from the catalog (zero
    geocoding calls) and any geometry override is ignored with a warning.
    Unknown cities are geocoded once, their grid inferred (or taken from the
    explicit lat/lng/width/height overrides), and registered so future runs
    align.

    Note ``width``/``height`` bypass MAX_GRID_DIM_M entirely — that is the
    documented override — and, given without ``lat``/``lng``, they are applied
    around the OSM bounding-box midpoint rather than downtown. Callers that
    can refuse that combination should (see scheduler's assess-city).

    Args:
        conn: open catalog connection (db.connect)
        query: the user's city query, e.g. "Newport, Kentucky"
        lat, lng: explicit grid center; both or neither
        width, height: explicit grid dimensions in meters; both or neither
        step: grid spacing in meters

    Returns:
        (city_row, newly_registered)

    Raises:
        CityResolutionError: the query could not be geocoded and no explicit
            center was supplied.
    """
    city_row = db.resolve_city(conn, query)
    if city_row is not None:
        overrides = [
            o for o, v in (("--lat/--lng", lat), ("--width/--height", width)) if v is not None
        ]
        if overrides:
            logger.warning(
                f"{' and '.join(overrides)} ignored: '{query}' is already "
                f"registered as {city_row.city_id} with frozen grid geometry "
                f"(center {city_row.center_lat:.5f},{city_row.center_lon:.5f}, "
                f"{city_row.grid_width_m}x{city_row.grid_height_m}m, "
                f"step {city_row.step_m}m). Changing geometry would break "
                f"run-to-run diffs."
            )
        return city_row, False

    # Unknown city: geocode once and register with frozen geometry
    city_loc_data = get_city_location_data(query)

    if lat is not None:
        center_lat, center_lng = lat, lng
        # logger, not print: this module is imported by the scheduler as well as
        # the CLI, and a library that writes to stdout puts its own diagnostics
        # in the middle of a report the caller is composing.
        logger.info(f"Using user-provided coordinates: {center_lat}, {center_lng}")
    elif city_loc_data:
        center_lat, center_lng = resolve_center(city_loc_data)
    else:
        raise CityResolutionError(
            f"Could not find coordinates for {query}. Provide them manually with --lat/--lng."
        )

    if width is not None:
        grid_width, grid_height = width, height
        logger.info(f"Using provided dimensions: {grid_width:.1f}m x {grid_height:.1f}m")
    else:
        grid_width, grid_height = get_search_dimensions(query, 1000, 1000)
        grid_width, grid_height = cap_dimensions(grid_width, grid_height, query)

    city_name = city_loc_data.city if city_loc_data else query.split(",")[0].strip()
    state_name = city_loc_data.state if city_loc_data else None
    state_code = city_loc_data.state_code if city_loc_data else None
    country_name = city_loc_data.country if city_loc_data else None
    country_code = city_loc_data.country_code if city_loc_data else None

    city_id = db.register_city(
        conn,
        city_name=city_name,
        state_name=state_name,
        state_code=state_code,
        country_name=country_name,
        country_code=country_code,
        center_lat=center_lat,
        center_lon=center_lng,
        grid_width_m=grid_width,
        grid_height_m=grid_height,
        step_m=step,
    )
    # Alias the user's query slug to the canonical id so future invocations
    # with the same query resolve without geocoding (and geocoder naming
    # drift can't re-register the city under a different id)
    query_slug = sanitize_city_query_str(query)
    if query_slug != city_id:
        db.add_alias(conn, query_slug, city_id)

    logger.info(f"Registered new city {city_id} with frozen geometry")
    return db.resolve_city(conn, city_id), True
