"""
Boundary audit logic (issue #91, step 1): compare each city's frozen search
rectangle against the OSM boundary Nominatim reports for it today.

Grid geometry is frozen at registration (see db.py), so a rectangle inferred
from a bad geocode — an administrative region, a township, a neighborhood —
is locked into every future run. This module classifies each city so a human
can review the flagged ones before the scheduler starts accumulating
quarterly history. Pure logic, no network: fetching lives in
scripts/audit_city_boundaries.py via geoutils.geocode_boundary_raw.

Deliberately avoids shapely (deferred to issue #91 step 3): bbox math is
interval arithmetic and polygon areas use a shoelace sum in a local
cos(lat)-scaled equirectangular frame, which is plenty for audit-grade
flagging.
"""

import logging
import math
from dataclasses import dataclass
from typing import Any

from geopy.distance import geodesic

from .db import CityRow

logger = logging.getLogger(__name__)

# (south, north, west, east) in degrees — Nominatim boundingbox order
BBox = tuple[float, float, float, float]

# Meters per degree of latitude / of longitude at the equator (WGS84 mean).
# Used for the frozen-rectangle inverse and shoelace scaling; geodesic() is
# used where we measure the OSM bbox itself.
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LON_EQUATOR = 111_320.0


@dataclass
class OsmBoundary:
    """Parsed Nominatim result: identity, bbox, and polygon area if any."""

    display_name: str
    osm_type: str | None  # node / way / relation
    place_class: str | None  # e.g. 'boundary', 'place'
    place_type: str | None  # e.g. 'administrative', 'city'
    lat: float
    lon: float
    bbox: BBox | None
    geometry_type: str | None  # Polygon / MultiPolygon / Point / ...
    polygon_area_m2: float | None


@dataclass
class Thresholds:
    """Audit cutoffs; both raw ratios are reported so a reviewer can re-sort
    with different values without re-fetching."""

    min_coverage: float = 0.75  # UNDER when frozen∩osm / osm bbox area < this
    over_area_ratio: float = 4.0  # OVER when frozen area / osm bbox area > this


@dataclass
class AuditResult:
    """One report row: verdict plus every metric behind it."""

    verdict: str  # OK | UNDER | OVER | WRONG_PLACE | NO_POLYGON |
    #               BBOX_SUSPECT | NOT_FOUND | NOT_FETCHED
    osm_display_name: str | None = None
    osm_type: str | None = None
    place_class: str | None = None
    place_type: str | None = None
    geometry_type: str | None = None
    center_in_osm_bbox: bool | None = None
    center_dist_km: float | None = None
    osm_bbox_width_m: float | None = None
    osm_bbox_height_m: float | None = None
    osm_bbox_area_km2: float | None = None
    osm_polygon_area_km2: float | None = None
    bbox_coverage_frac: float | None = None
    over_area_ratio: float | None = None
    suggested_center_lat: float | None = None
    suggested_center_lon: float | None = None
    suggested_width_m: int | None = None
    suggested_height_m: int | None = None


def parse_osm_result(raw: dict[str, Any]) -> OsmBoundary:
    """
    Parse a raw Nominatim JSON result into an OsmBoundary.

    Nominatim quirks handled here: boundingbox values are strings in
    [south, north, west, east] order, and both 'boundingbox' and 'geojson'
    may be absent.
    """
    bbox: BBox | None = None
    raw_bbox = raw.get("boundingbox")
    if raw_bbox is not None and len(raw_bbox) == 4:
        try:
            bbox = tuple(float(v) for v in raw_bbox)  # type: ignore[assignment]
        except (TypeError, ValueError):
            logger.warning(f"Unparseable boundingbox: {raw_bbox!r}")

    geometry = raw.get("geojson")
    geometry_type = geometry.get("type") if isinstance(geometry, dict) else None

    return OsmBoundary(
        display_name=raw.get("display_name", ""),
        osm_type=raw.get("osm_type"),
        place_class=raw.get("class"),
        place_type=raw.get("type"),
        lat=float(raw["lat"]),
        lon=float(raw["lon"]),
        bbox=bbox,
        geometry_type=geometry_type,
        polygon_area_m2=polygon_area_m2(geometry) if geometry else None,
    )


def bbox_dims_m(bbox: BBox) -> tuple[float, float]:
    """
    (width_m, height_m) of a (south, north, west, east) bbox, measured
    geodesically — width along the middle latitude, matching
    get_search_dimensions and the archival importer.
    """
    south, north, west, east = bbox
    mid_lat = (south + north) / 2
    width = geodesic((mid_lat, west), (mid_lat, east)).meters
    height = geodesic((south, west), (north, west)).meters
    return width, height


def frozen_rect_bounds(
    center_lat: float, center_lon: float, width_m: float, height_m: float
) -> BBox:
    """
    The frozen search rectangle as a (south, north, west, east) bbox —
    the inverse of the meters-from-degrees conversion, good to well under
    a percent at city scale.
    """
    half_h_deg = (height_m / 2) / _M_PER_DEG_LAT
    m_per_deg_lon = _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(center_lat))
    half_w_deg = (width_m / 2) / m_per_deg_lon
    return (
        center_lat - half_h_deg,
        center_lat + half_h_deg,
        center_lon - half_w_deg,
        center_lon + half_w_deg,
    )


def bbox_intersection_frac(rect: BBox, target: BBox) -> float:
    """
    Fraction of `target`'s area covered by `rect`, in cos(lat)-scaled
    degree space. 1.0 = fully covered, 0.0 = disjoint (or degenerate
    target).
    """
    s1, n1, w1, e1 = rect
    s2, n2, w2, e2 = target
    lat_overlap = max(0.0, min(n1, n2) - max(s1, s2))
    lon_overlap = max(0.0, min(e1, e2) - max(w1, w2))
    target_lat = n2 - s2
    target_lon = e2 - w2
    if target_lat <= 0 or target_lon <= 0:
        return 0.0
    # The cos(lat) scale factor is common to numerator and denominator at
    # city scale, so plain degree products suffice for the ratio
    return (lat_overlap * lon_overlap) / (target_lat * target_lon)


def _ring_area_m2(ring: list[list[float]]) -> float:
    """
    Unsigned shoelace area of one GeoJSON ring ([lon, lat] pairs) in a
    local equirectangular frame anchored at the ring's mean latitude.
    """
    if len(ring) < 3:
        return 0.0
    mean_lat = sum(pt[1] for pt in ring) / len(ring)
    kx = _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(mean_lat))
    ky = _M_PER_DEG_LAT
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(ring, ring[1:] + ring[:1], strict=False):
        total += (lon1 * kx) * (lat2 * ky) - (lon2 * kx) * (lat1 * ky)
    return abs(total) / 2


def polygon_area_m2(geometry: dict[str, Any] | None) -> float | None:
    """
    Area of a GeoJSON Polygon (exterior minus holes) or MultiPolygon
    (summed), in square meters. None for Point/LineString/anything else —
    callers treat that as "no polygon available".
    """
    if not isinstance(geometry, dict):
        return None
    gtype = geometry.get("type")
    if gtype == "Polygon":
        rings = geometry.get("coordinates", [])
        if not rings:
            return None
        area = _ring_area_m2(rings[0])
        for hole in rings[1:]:
            area -= _ring_area_m2(hole)
        return max(area, 0.0)
    if gtype == "MultiPolygon":
        areas = [
            polygon_area_m2({"type": "Polygon", "coordinates": rings})
            for rings in geometry.get("coordinates", [])
        ]
        return sum(a for a in areas if a is not None) or None
    return None


def _intersect(p1: list[float], p2: list[float], axis: int, val: float) -> list[float]:
    """The point on segment p1->p2 where coordinate[axis] == val (linear)."""
    other = 1 - axis
    d = p2[axis] - p1[axis]
    t = 0.0 if d == 0 else (val - p1[axis]) / d
    pt = [0.0, 0.0]
    pt[axis] = val
    pt[other] = p1[other] + t * (p2[other] - p1[other])
    return pt


def _clip_ring_to_bbox(ring: list[list[float]], bbox: BBox) -> list[list[float]]:
    """
    Sutherland-Hodgman clip of a [lon, lat] ring against a (south, north,
    west, east) axis-aligned rectangle. Returns an open ring (no repeated
    closing vertex); fewer than 3 points means the ring fell outside.
    """
    south, north, west, east = bbox
    # Drop an explicit closing vertex; the area/clip math treats rings as closed.
    poly = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    # (axis, threshold, keep_greater_equal): west, east, south, north edges.
    edges = [(0, west, True), (0, east, False), (1, south, True), (1, north, False)]
    for axis, val, keep_ge in edges:
        if not poly:
            break
        clipped: list[list[float]] = []
        for i in range(len(poly)):
            cur, prev = poly[i], poly[i - 1]
            cur_in = cur[axis] >= val if keep_ge else cur[axis] <= val
            prev_in = prev[axis] >= val if keep_ge else prev[axis] <= val
            if cur_in:
                if not prev_in:
                    clipped.append(_intersect(prev, cur, axis, val))
                clipped.append(cur)
            elif prev_in:
                clipped.append(_intersect(prev, cur, axis, val))
        poly = clipped
    return poly


def _clipped_polygon_area_m2(geometry: dict[str, Any], bbox: BBox) -> float:
    """Area (m²) of the part of a GeoJSON polygon inside `bbox`."""
    gtype = geometry.get("type")
    if gtype == "Polygon":
        rings = geometry.get("coordinates", [])
        if not rings:
            return 0.0
        area = _ring_area_m2(_clip_ring_to_bbox(rings[0], bbox))
        for hole in rings[1:]:
            area -= _ring_area_m2(_clip_ring_to_bbox(hole, bbox))
        return max(area, 0.0)
    if gtype == "MultiPolygon":
        return sum(
            _clipped_polygon_area_m2({"type": "Polygon", "coordinates": rings}, bbox)
            for rings in geometry.get("coordinates", [])
        )
    return 0.0


def rect_polygon_coverage(geometry: dict[str, Any] | None, bbox: BBox | None) -> float | None:
    """
    Fraction of a GeoJSON boundary polygon's area that falls inside a
    (south, north, west, east) rectangle — "how much of the real city does
    this search grid actually cover?", the metric the audit's bbox ratios
    only proxy. None when there is no polygon or its area is degenerate.

    Uses the same shoelace-in-a-local-equirectangular-frame math as
    polygon_area_m2 (no shapely); good to well under a percent at city scale.
    """
    if not isinstance(geometry, dict) or bbox is None:
        return None
    total = polygon_area_m2(geometry)
    if not total or total <= 0:
        return None
    inside = _clipped_polygon_area_m2(geometry, bbox)
    return max(0.0, min(1.0, inside / total))


def rect_in_boundary_frac(geometry: dict[str, Any] | None, bbox: BBox | None) -> float | None:
    """
    Fraction of the SEARCH RECTANGLE that lies inside a GeoJSON boundary
    polygon — the reciprocal of rect_polygon_coverage, and the one that says
    whether the imagery numbers a run produces are actually about this place.

    The two answer different questions and both matter. Coverage asks "does the
    grid capture the whole city?"; this asks "is what the grid samples the city
    at all?". Northern Kentucky (2026-07-30) is the case study: county
    rectangles whose in-boundary fraction was only 49-69%, where the remainder
    was largely Cincinnati — whose dense recent GSV would have silently
    flattered a coverage number quoted to a prospective deployment partner.

    Uses the same shoelace-in-a-local-equirectangular-frame math as
    polygon_area_m2 (no shapely). The rectangle's area is measured in a frame
    anchored at its own mean latitude and the clipped polygon in one anchored
    at the clipped ring's, which differ negligibly at city scale — the same
    tolerance the rest of this module documents.

    Args:
        geometry: Nominatim's ``geojson`` for the place (see
            geoutils.geocode_boundary_raw), or None.
        bbox: the frozen search rectangle as (south, north, west, east) —
            boundary_audit.frozen_rect_bounds produces it from catalog geometry.

    Returns:
        Fraction in [0, 1], or None when there is no usable polygon (Nominatim
        answers with a Point for many places) or the rectangle is degenerate.
    """
    if not isinstance(geometry, dict) or bbox is None:
        return None
    # Rejects Point/LineString and zero-area multipolygons before any clipping.
    if not polygon_area_m2(geometry):
        return None
    south, north, west, east = bbox
    rect_area = _ring_area_m2(
        [[west, south], [east, south], [east, north], [west, north]],
    )
    if rect_area <= 0:
        return None
    inside = _clipped_polygon_area_m2(geometry, bbox)
    return max(0.0, min(1.0, inside / rect_area))


# Module-level singleton so the default isn't a function call in the signature (B008).
_DEFAULT_THRESHOLDS = Thresholds()


def classify(
    city: CityRow, osm: OsmBoundary | None, thresholds: Thresholds = _DEFAULT_THRESHOLDS
) -> AuditResult:
    """
    Compare a city's frozen rectangle to its OSM boundary.

    Verdict ladder (first match wins):
      NOT_FOUND    — Nominatim had no result for the structured query
      BBOX_SUSPECT — bbox missing, inverted, or spanning >180° longitude
                     (antimeridian); size ratios would be meaningless
      WRONG_PLACE  — frozen center lies outside the OSM bbox entirely
                     (the Bancroft-SD-as-Le-Sueur-Township failure)
      NO_POLYGON   — point-only result; node bboxes are synthetic
                     (rank-based), so size verdicts aren't trusted
      UNDER        — frozen rectangle covers < min_coverage of the OSM
                     bbox (truncation risk; intersection-based, so an
                     offset center is caught even with matching dims)
      OVER         — frozen area > over_area_ratio × OSM bbox area
                     (wasted API budget)
      OK
    """
    if osm is None:
        return AuditResult(verdict="NOT_FOUND")

    result = AuditResult(
        verdict="OK",
        osm_display_name=osm.display_name,
        osm_type=osm.osm_type,
        place_class=osm.place_class,
        place_type=osm.place_type,
        geometry_type=osm.geometry_type,
        center_dist_km=geodesic((city.center_lat, city.center_lon), (osm.lat, osm.lon)).kilometers,
        osm_polygon_area_km2=(
            osm.polygon_area_m2 / 1e6 if osm.polygon_area_m2 is not None else None
        ),
    )

    bbox = osm.bbox
    if bbox is None or bbox[2] > bbox[3] or (bbox[3] - bbox[2]) > 180:
        result.verdict = "BBOX_SUSPECT"
        return result

    south, north, west, east = bbox
    osm_w, osm_h = bbox_dims_m(bbox)
    result.osm_bbox_width_m = osm_w
    result.osm_bbox_height_m = osm_h
    result.osm_bbox_area_km2 = (osm_w * osm_h) / 1e6
    result.suggested_center_lat = (south + north) / 2
    result.suggested_center_lon = (west + east) / 2
    result.suggested_width_m = math.ceil(osm_w)
    result.suggested_height_m = math.ceil(osm_h)
    result.center_in_osm_bbox = (
        south <= city.center_lat <= north and west <= city.center_lon <= east
    )

    frozen_rect = frozen_rect_bounds(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m
    )
    frozen_area_km2 = (city.grid_width_m * city.grid_height_m) / 1e6
    result.bbox_coverage_frac = bbox_intersection_frac(frozen_rect, bbox)
    if result.osm_bbox_area_km2 and result.osm_bbox_area_km2 > 0:
        result.over_area_ratio = frozen_area_km2 / result.osm_bbox_area_km2

    if not result.center_in_osm_bbox:
        result.verdict = "WRONG_PLACE"
    elif osm.geometry_type not in ("Polygon", "MultiPolygon"):
        result.verdict = "NO_POLYGON"
    elif result.bbox_coverage_frac < thresholds.min_coverage:
        result.verdict = "UNDER"
    elif result.over_area_ratio is not None and result.over_area_ratio > thresholds.over_area_ratio:
        result.verdict = "OVER"
    return result
