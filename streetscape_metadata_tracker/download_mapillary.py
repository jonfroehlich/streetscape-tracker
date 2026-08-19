"""
Mapillary metadata downloader (issue #89).

Unlike Google Street View — which only answers "what's the nearest pano to
point X?", forcing one API request per grid point — Mapillary publishes its
image metadata as z14 vector tiles: one request returns every image in a
~2.4 km (at the equator) map-tile square as compact protobuf, including the
image id, capture timestamp, position, and an is_pano flag. A whole city is
typically a few dozen tile requests.

Collection model (issue #89, extended by issue #116):
- ALL panos are kept — one CSV row per 360-degree image (is_pano true), with
  query_lat/query_lon set to the image's nearest point on the city's frozen
  sampling grid. Coverage rate (% of grid points with >= 1 pano) is therefore
  directly comparable to GSV, while raw pano counts are a census here vs a
  grid sample for GSV.
- Flat/perspective images (is_pano false) are no longer discarded (issue
  #116). A grid point covered ONLY by flat imagery — no pano — gets a single
  FLAT_ONLY row (carrying the nearest flat image as a representative, with a
  null capture_date) instead of ZERO_RESULTS, so any-imagery coverage can be
  reported alongside the GSV-comparable 360-degree coverage. Flat imagery at a
  point that also has a pano is not written as a row (the pano already covers
  it), but every in-grid flat image is tallied into the returned
  num_flat_images census magnitude.
- Grid points with neither a pano nor a flat image get a single ZERO_RESULTS
  row, as before.

The output CSV uses the exact same 9-column schema as the GSV downloader
(config.METADATA_DTYPES), so analysis, diffing, and the frontend consume
both providers' files identically.

No resume logic: a full city is seconds of tile fetches (vs hours of
per-point requests for GSV), so an interrupted run just restarts.
"""

import asyncio
import gzip
import logging
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import backoff
import geopy.distance
import mapbox_vector_tile
import numpy as np
import pandas as pd

from . import census
from .analysis import FLAT_ONLY, REQUEST_FAILED
from .census import dedupe_census, status_for_capture_dates
from .config import MAPILLARY_METADATA_DTYPES
from .download_common import (
    HOST_MAPILLARY_TILES,
    AsyncRateLimiter,
    DownloadError,
    HostBlockedError,
    generate_grid_arrays,
    grid_index_ranges,
    redact_credentials,
)
from .fileutils import load_city_csv_file
from .host_lock import host_lock
from .progress import progress

logger = logging.getLogger(__name__)

TILE_ZOOM = 14  # the only zoom level whose tiles carry per-image metadata
# The tiles CDN only accepts the token as an `?access_token=` query
# parameter — it rejects the `Authorization: OAuth <token>` header the Graph
# API uses (verified: header -> HTTP 403, query param -> 200). This matches
# how download_gsv.py carries `key=` in the URL. A URL-borne token would
# otherwise leak into logs via HTTP-client exceptions that stringify the
# full request URL, so every raised/logged error text must pass through
# download_common.redact_credentials (which scrubs `access_token=`).
TILE_URL_TEMPLATE = "https://tiles.mapillary.com/maps/vtp/mly1_computed_public/2/{z}/{x}/{y}"
IMAGE_LAYER = "image"

# Meters per degree of latitude (WGS84 mean). Kept for rough offset math in
# tests/estimates; the actual grid assignment uses the latitude-local series
# below (the mean constant mis-assigned edge panos by whole grid rows —
# ~0.7% error at the equator is +1 row at 2.5 km from center).
_M_PER_DEG_LAT = 111320.0


def _meters_per_degree(lat_deg):
    """
    (m_per_deg_lat, m_per_deg_lon) at a latitude, via the standard WGS84
    series expansion. Accepts scalars or numpy arrays. Matches the geodesic
    math that builds the grid to well under a meter over a city-sized area,
    so nearest-grid-point assignment can't drift by rows near the edges.
    """
    phi = np.radians(lat_deg)
    m_lat = (
        111132.92 - 559.82 * np.cos(2 * phi) + 1.175 * np.cos(4 * phi) - 0.0023 * np.cos(6 * phi)
    )
    m_lon = 111412.84 * np.cos(phi) - 93.5 * np.cos(3 * phi) + 0.118 * np.cos(5 * phi)
    return m_lat, m_lon


# ── Slippy-map tile math (stdlib only) ─────────────────────────────────────


def lonlat_to_tile_frac(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Fractional Web-Mercator tile coordinates (x, y; y from the top)."""
    n = 2**zoom
    fx = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    fy = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return fx, fy


def tile_frac_to_lonlat(fx: float, fy: float, zoom: int) -> tuple[float, float]:
    """Inverse of lonlat_to_tile_frac."""
    n = 2**zoom
    lon = fx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n))))
    return lon, lat


def tiles_for_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, zoom: int = TILE_ZOOM
) -> list[tuple[int, int]]:
    """
    All (x, y) tile indices at the given zoom intersecting the bbox.

    A bbox that crosses the antimeridian (min_lon > max_lon after geopy
    normalizes longitudes to ±180 — e.g. Suva, Fiji) wraps: it covers the
    x columns from min_lon to the right edge plus those from the left edge
    to max_lon. The naive single range was empty there, silently yielding
    a 0-tile (0-pano) run.
    """
    fx_min, fy_max = lonlat_to_tile_frac(min_lon, min_lat, zoom)  # y grows southward
    fx_max, fy_min = lonlat_to_tile_frac(max_lon, max_lat, zoom)
    n = 2**zoom
    if fx_min > fx_max:  # bbox crosses the antimeridian
        x_indices = [*range(max(0, int(fx_min)), n), *range(0, min(n - 1, int(fx_max)) + 1)]
    else:
        x_indices = list(range(max(0, int(fx_min)), min(n - 1, int(fx_max)) + 1))
    y_range = range(max(0, int(fy_min)), min(n - 1, int(fy_max)) + 1)
    return [(x, y) for x in x_indices for y in y_range]


def grid_bbox(
    center_lat: float, center_lon: float, grid_width: float, grid_height: float, step_length: float
) -> tuple[float, float, float, float]:
    """
    (min_lon, min_lat, max_lon, max_lat) covering the sampling grid plus a
    half-step margin, computed with the same geodesic math that builds the
    grid so the two always agree. The margin admits images that lie just
    outside the outermost grid points but are still nearest to them.
    """
    origin = geopy.Point(center_lat, center_lon)
    half_h = grid_height / 2 + step_length / 2
    half_w = grid_width / 2 + step_length / 2
    north = geopy.distance.distance(meters=half_h).destination(origin, 0)
    south = geopy.distance.distance(meters=half_h).destination(origin, 180)
    east = geopy.distance.distance(meters=half_w).destination(origin, 90)
    west = geopy.distance.distance(meters=half_w).destination(origin, 270)
    return west.longitude, south.latitude, east.longitude, north.latitude


def estimate_tile_count(
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float = 20,
) -> int:
    """
    Number of z14 tile requests a run will make — the Mapillary analogue of
    the scheduler's grid-point request estimate for GSV.
    """
    return len(
        tiles_for_bbox(*grid_bbox(center_lat, center_lon, grid_width, grid_height, step_length))
    )


# ── Tile decoding ──────────────────────────────────────────────────────────


def decode_image_features(
    tile_bytes: bytes, tile_x: int, tile_y: int, zoom: int = TILE_ZOOM
) -> list[dict[str, Any]]:
    """
    Extract image records from one raw vector tile.

    Returns dicts with: id (str), lon, lat, captured_at_ms (int or None),
    creator_id, is_pano (bool), plus the free per-image extras Mapillary
    publishes on the z14 image layer — organization_id, quality_score, on_foot
    (tile prop `foot`), compass_angle, sequence_id (see MAPILLARY_EXTRA_DTYPES).
    Both 360-degree panos and flat/perspective images are returned, tagged by
    is_pano — the caller keeps every pano as a census row (issue #89) but
    collapses flat-only grid points to a single FLAT_ONLY marker (issue #116).
    Dropping flats here (as the original #89 scope did) is what made a
    flat-covered point indistinguishable from ZERO_RESULTS.
    """
    decoded = mapbox_vector_tile.decode(tile_bytes)
    layer = decoded.get(IMAGE_LAYER)
    if not layer:
        return []
    extent = layer.get("extent", 4096)

    records = []
    for feature in layer["features"]:
        props = feature.get("properties", {})
        geometry = feature.get("geometry", {})
        if geometry.get("type") != "Point":
            continue
        px, py = geometry["coordinates"]
        # decode() returns y-up tile-local coords; convert to global fractions
        fx = tile_x + px / extent
        fy = tile_y + (1 - py / extent)
        lon, lat = tile_frac_to_lonlat(fx, fy, zoom)

        image_id = props.get("id", feature.get("id"))
        if image_id is None:
            continue
        captured_at = props.get("captured_at")
        organization_id = props.get("organization_id")
        records.append(
            {
                "id": str(image_id),
                "lon": lon,
                "lat": lat,
                "captured_at_ms": captured_at,
                "creator_id": props.get("creator_id"),
                "is_pano": bool(props.get("is_pano")),
                # Free per-image extras from the same tile (large int ids kept
                # as strings; `foot` is Mapillary's on_foot flag). None when a
                # tile omits the field (e.g. organization_id on individual
                # contributor imagery).
                "organization_id": (None if organization_id is None else str(organization_id)),
                "quality_score": props.get("quality_score"),
                "on_foot": props.get("foot"),
                "compass_angle": props.get("compass_angle"),
                "sequence_id": props.get("sequence_id"),
            }
        )
    return records


def captured_at_to_iso_date(captured_at_ms) -> str:
    """
    Unix epoch milliseconds -> 'YYYY-MM-DD' (UTC), or '' when missing or
    implausible. Mapillary timestamps come from contributor device clocks,
    so guard against epoch-zero and other bogus values (anything before
    Mapillary could plausibly have imagery, or in the future).

    Scalar reference implementation for :func:`captured_at_to_iso_dates`, which
    is what the collection paths actually call — a test pins the two together
    element-wise, so the rules live here in readable form and are stated once.
    """
    if not captured_at_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(captured_at_ms) / 1000, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return ""
    if dt.year < 2004 or dt > datetime.now(UTC):
        return ""
    return dt.date().isoformat()


def captured_at_to_iso_dates(captured_at_ms) -> pd.Series:
    """
    Vectorized :func:`captured_at_to_iso_date` over a column of epoch millis.

    Same rules, same '' for anything unusable — applied to a whole census at
    once. A census is millions of images (issue #157), and calling the scalar
    form per image was both a Python-level loop and a per-image object.

    Args:
        captured_at_ms: array-like of epoch milliseconds, nulls allowed.

    Returns:
        A str Series of 'YYYY-MM-DD' / '' values, aligned to the input.
    """
    ms = pd.Series(captured_at_ms, dtype="Int64").reset_index(drop=True)
    # errors="coerce" turns an unrepresentable value into NaT where the scalar
    # form raises ValueError/OSError/OverflowError — both end as ''.
    ts = pd.to_datetime(ms.astype("float64"), unit="ms", utc=True, errors="coerce")
    usable = ts.notna() & ms.fillna(0).ne(0) & ts.dt.year.ge(2004) & ts.le(datetime.now(UTC))
    # Mask BEFORE formatting, not after: pandas represents timestamps far
    # outside Python's datetime range quite happily (a device clock reporting
    # the year 318857 is a real thing in contributor metadata) and then refuses
    # to strftime them. Those are exactly the values `usable` has already
    # rejected, so blanking them first is both correct and what keeps this from
    # raising on a census that the scalar form would have handled row by row.
    return ts.where(usable).dt.strftime("%Y-%m-%d").fillna("").astype(str)


# Census columns, in decode order. The census is held COLUMN-WISE (a DataFrame)
# rather than as a list of per-image dicts: at Colorado Springs' 6.5M-feature
# census the dicts alone cost ~4.8 GB against an 8 GB cgroup, which is what
# killed both of its Mapillary channels every night (issue #157). Dtypes are
# chosen for size as well as correctness — Arrow-backed strings hold an id in
# about a byte per character instead of a ~57-byte Python str plus a pointer,
# and pyarrow is already a hard dependency.
_CENSUS_DTYPES = {
    "id": pd.StringDtype("pyarrow"),
    "lon": "float64",
    "lat": "float64",
    "captured_at_ms": "Int64",
    "creator_id": "Int64",
    "is_pano": "bool",
    "organization_id": pd.StringDtype("pyarrow"),
    "quality_score": "float64",
    "on_foot": "boolean",
    "compass_angle": "float64",
    "sequence_id": pd.StringDtype("pyarrow"),
}


def records_to_census(records: list[dict[str, Any]]) -> pd.DataFrame:
    """
    Turn one tile's decoded records into a columnar census frame.

    Called per tile, immediately after :func:`decode_image_features`, so a
    tile's dicts are freed before the next tile is decoded rather than every
    tile's surviving until the whole city has downloaded.

    Mapillary's binding of :func:`census.records_to_census`.

    Args:
        records: decoded image dicts from :func:`decode_image_features`.

    Returns:
        A DataFrame with the :data:`_CENSUS_DTYPES` columns, one row per image.
    """
    return census.records_to_census(records, _CENSUS_DTYPES)


def concat_census(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Combine per-tile census frames into one, preserving tile order."""
    return census.concat_census(frames, _CENSUS_DTYPES)


def _mapillary_image_columns(picked: pd.DataFrame) -> dict[str, Any]:
    """
    Mapillary's own output columns: the copyright convention plus its extras.

    Handed to :func:`census.build_image_rows`, which fills the shared core.
    """
    # As text once: the contributor id is published both as its own structured
    # column and (for parity with GSV's "© <photographer>") inside the
    # copyright string. Going through the nullable string dtype rather than
    # astype(str) keeps a missing id missing instead of rendering it "<NA>".
    creator = picked["creator_id"].astype("string")
    return {
        "copyright_info": ("© Mapillary contributor " + creator)
        .fillna("© Mapillary")
        .to_numpy(dtype=object),
        "creator_id": creator.to_numpy(dtype=object),
        "organization_id": picked["organization_id"].to_numpy(dtype=object),
        "sequence_id": picked["sequence_id"].to_numpy(dtype=object),
        "is_pano": picked["is_pano"].to_numpy(),
        "on_foot": picked["on_foot"].astype(object).to_numpy(),
        "quality_score": picked["quality_score"].to_numpy(),
        "compass_angle": picked["compass_angle"].to_numpy(),
    }


def build_image_rows(
    census_frame: pd.DataFrame,
    image_positions: np.ndarray,
    query_lat,
    query_lon,
    query_timestamp: str,
    status,
    capture_date,
) -> pd.DataFrame:
    """
    MAPILLARY_METADATA_DTYPES rows for query locations matched to census images.

    Shared by the grid downloader (query location = a frozen grid point) and
    the road-walk collector (query location = an on-street sample point).
    Mapillary's binding of :func:`census.build_image_rows`; see there for the
    argument contract.
    """
    return census.build_image_rows(
        census_frame,
        image_positions,
        query_lat,
        query_lon,
        query_timestamp,
        status,
        capture_date,
        dtypes=MAPILLARY_METADATA_DTYPES,
        image_columns=_mapillary_image_columns,
    )


def build_empty_rows(query_lat, query_lon, query_timestamp: str, status) -> pd.DataFrame:
    """
    Rows for query locations with no imagery — the ZERO_RESULTS fill, plus the
    REQUEST_FAILED variant for points under an undownloaded tile.

    Mapillary's binding of :func:`census.build_empty_rows`.
    """
    return census.build_empty_rows(
        query_lat, query_lon, query_timestamp, status, dtypes=MAPILLARY_METADATA_DTYPES
    )


# ── Grid assignment ────────────────────────────────────────────────────────


def assign_to_grid(
    image_lats: np.ndarray,
    image_lons: np.ndarray,
    center_lat: float,
    center_lon: float,
    width_steps: int,
    height_steps: int,
    step_length: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorized nearest-grid-point assignment.

    The grid is a regular lattice of step_length meters indexed by
    (i, j) = (north, east) offsets from the center (see
    generate_grid_points), so the nearest point is just a rounded division
    in a local equirectangular projection — no spatial index needed.

    Returns (i, j, in_grid) arrays; in_grid is False for images farther
    than half a step beyond the outermost grid points, which the caller
    drops.
    """
    # Latitude-local scales: the grid is built geodesically, so a global
    # mean m/° mis-assigns by whole rows near the grid edges. dy uses the
    # series at the center↔image midpoint latitude; dx uses each image's
    # own latitude (grid rows are constant-latitude, and their east-west
    # spacing shrinks with cos φ at THAT row, not at the center).
    m_lat_mid, _ = _meters_per_degree((image_lats + center_lat) / 2)
    _, m_lon_local = _meters_per_degree(image_lats)
    dy_m = (image_lats - center_lat) * m_lat_mid
    dx_m = (image_lons - center_lon) * m_lon_local
    i = np.rint(dy_m / step_length).astype(int)
    j = np.rint(dx_m / step_length).astype(int)

    # Replicate generate_grid_points' index ranges exactly (note: Python
    # floor division makes the ranges asymmetric for odd step counts).
    i_min, i_max = -height_steps // 2, height_steps // 2
    j_min, j_max = -width_steps // 2, width_steps // 2
    in_grid = (i >= i_min) & (i <= i_max) & (j >= j_min) & (j <= j_max)
    return i, j, in_grid


def _points_in_tiles(
    lats: np.ndarray, lons: np.ndarray, tiles: list[tuple[int, int]], zoom: int = TILE_ZOOM
) -> np.ndarray:
    """
    Boolean mask of which (lat, lon) points fall inside any of ``tiles``.

    Vectorized form of ``lonlat_to_tile_frac``; used to attribute undownloaded
    tiles back to the grid points they cover (issue #168). Deliberately ignores
    the tiles' render buffer: a point just outside a failed tile may in fact
    have been covered by a neighbour, and calling it "unknown" errs toward
    admitting we don't know rather than claiming empty.
    """
    if len(lats) == 0:
        return np.zeros(0, dtype=bool)
    n = 2**zoom
    fx = (lons + 180.0) / 360.0 * n
    fy = (1.0 - np.arcsinh(np.tan(np.radians(lats))) / np.pi) / 2.0 * n
    # One packed int per tile so membership is a single sorted-array lookup
    # instead of a Python loop over the (usually tiny) failed-tile list.
    keys = fx.astype(np.int64) * n + fy.astype(np.int64)
    failed_keys = np.array(sorted(x * n + y for x, y in tiles), dtype=np.int64)
    return np.isin(keys, failed_keys)


# ── Download ───────────────────────────────────────────────────────────────


# A city whose permanently-failed tiles exceed this fraction of the tile set is
# abandoned rather than finalized as an immutable snapshot. Mirrors
# download_gsv.MAX_FAILED_POINT_FRACTION: tolerate a blip, refuse a hole.
#
# It is a FRACTION rather than an absolute count on purpose, and the
# consequence is worth being explicit about: a big city (Chicago, 480 tiles)
# absorbs a stray 404 at 0.2%, while a small city (16 tiles) gets no tolerance
# at all, because there one tile is ~6% of its entire area. That asymmetry is
# the right way round — the threshold is bounding the size of the unknown
# region in a snapshot that is immutable once published, not counting requests.
# A small city that loses a tile simply fails and is retried on its next
# nightly slot, which costs a few tile requests.
MAX_FAILED_TILE_FRACTION = 0.02

# The tiles CDN can return a transient 404 for a tile that serves fine minutes
# later (observed on Chicago, 2026-07-29: z14/4196/6084 404'd through every
# retry, then returned 2.1M features the next day). Three tries inside a
# 60-second window was too thin a budget for that.
_TILE_MAX_TRIES = 5
_TILE_MAX_TIME_S = 120

# Client-side pacing for the tile CDN (issue #198). This bounds a limit
# Mapillary does not document and that is enforced PER IP, not per token: on
# 2026-08-12 a bulk collection sustaining ~370 tile requests/min got the host
# redirected to a login page for every tile, across BOTH of our Mapillary
# applications at once, at a total spend of 10,659 requests — about 21% of the
# 50,000/day per-application cap the config has always paced against. So the
# daily budget is not the binding constraint and a second token buys nothing;
# only rate does.
#
# 60/min is a deliberately conservative guess. The true ceiling is unknown (370
# is merely confirmed too high), and being wrong is expensive in a way slowness
# is not: the nightly scheduler shares the host IP, so a ban takes out its
# `mapillary` and `mapillary_streets` channels too.
#
# What pacing costs, measured over the enabled cities' frozen geometry rather
# than guessed: a median city is 12 z14 tiles and the mean is 59, so a 20-city
# night is ~1,200 tiles on the grid channel (~20 min at this rate) and ~2,400
# across both Mapillary channels (~40 min), against a 10 h batch deadline. The
# distribution has a long tail — Anchorage's 105x84 km grid alone is ~6,480
# tiles, ~108 min — which is why scheduler.city_timeout_seconds derives a
# Mapillary timeout from the tile count instead of using the flat floor.
#
# Per-process, like the GSV limiter: N concurrent collections present N times
# this rate to the CDN. Do not run Mapillary collections in parallel.
DEFAULT_TILE_REQUESTS_PER_MINUTE = 60

# Content types that are an error page rather than a tile (issue #199). A
# DENY-list, not an allow-list of protobuf types: if Mapillary ever relabels
# real tiles (say `application/vnd.mapbox-vector-tile`), an allow-list would
# reject every tile and halt all collection, whereas this only ever misreads a
# genuine tile if one is served as HTML or JSON.
_TILE_ERROR_CONTENT_TYPES = ("text/html", "application/json")


@backoff.on_exception(
    backoff.expo,
    (asyncio.TimeoutError, aiohttp.ClientError),
    max_tries=_TILE_MAX_TRIES,
    max_time=_TILE_MAX_TIME_S,
)
async def _fetch_tile(
    session: aiohttp.ClientSession,
    url: str,
    timeout: aiohttp.ClientTimeout,
    rate_limiter: AsyncRateLimiter | None = None,
    on_request: Callable[[], None] | None = None,
) -> bytes:
    # Pacing and counting sit INSIDE the retried body on purpose (issue #198).
    # This function may issue up to _TILE_MAX_TRIES requests; taking one token
    # in the caller would let a retrying tile present up to five times the
    # configured rate — during a 429/5xx storm, i.e. exactly when the CDN is
    # least willing to absorb it — and would under-report the same factor to
    # the api_usage ledger. One token, one counted request, one HTTP request.
    if rate_limiter is not None:
        await rate_limiter.acquire()
    if on_request is not None:
        on_request()
    # allow_redirects=False is load-bearing (issue #199). When the tile CDN
    # rate-limits a host it answers 302 → www.mapillary.com/login/, and aiohttp
    # follows redirects by default — so the login page's own perfectly good HTTP
    # 200 passed the status checks below and 58 bytes of HTML reached the
    # protobuf decoder. A host-wide block then read as `DecodeError: Error
    # parsing message with type 'vector_tile.tile'`, i.e. as corrupt data.
    async with session.get(url, timeout=timeout, allow_redirects=False) as response:
        if response.status in (401, 403):
            # Deliberately NOT a HostBlockedError: a rejected token is scoped to
            # the CREDENTIAL, and our two Mapillary channels hold different
            # tokens. Typing it host-wide would let one channel's bad key stop
            # the other channel — which is working fine — for the whole night.
            raise DownloadError(
                f"Mapillary rejected the access token (HTTP {response.status}). "
                "Check MAPILLARY_ACCESS_TOKEN."
            )
        if response.status in (301, 302, 303, 307, 308):
            # Whole-city (indeed whole-host) condition, so DownloadError: the
            # caller re-raises those immediately instead of counting them
            # against MAX_FAILED_TILE_FRACTION, since every remaining tile
            # would fail identically. Observed 2026-08-12 after sustaining
            # ~370 tile requests/min from one IP; the ban is per-IP, so tokens
            # and the Graph API keep working and only this host is refused.
            # The Location echoes the request URL, hence redact_credentials.
            location = redact_credentials(response.headers.get("Location", "(none)"))
            raise HostBlockedError(
                f"Mapillary tile CDN redirected instead of serving a tile (HTTP "
                f"{response.status} → {location}). A redirect to a login page "
                f"means this host's IP is rate-limited on tiles.mapillary.com — "
                f"the access token itself may still be valid (the Graph API and "
                f"other IPs are unaffected), so retry later and collect "
                f"Mapillary more slowly. A redirect anywhere else means the tile "
                f"endpoint has moved and this code needs updating.",
                host=HOST_MAPILLARY_TILES,
            )
        if response.status != 200:
            # 429/5xx raise ClientResponseError, which backoff retries
            response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if any(bad in content_type.lower() for bad in _TILE_ERROR_CONTENT_TYPES):
            raise HostBlockedError(
                f"Mapillary served an error page instead of a vector tile "
                f"(HTTP 200, Content-Type: {content_type}). This is usually a "
                f"rate limit or a block on this host's IP, not a corrupt tile.",
                host=HOST_MAPILLARY_TILES,
            )
        return await response.read()


async def fetch_city_images_async(
    city_name: str,
    bbox: tuple[float, float, float, float],
    access_token: str,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
) -> dict[str, Any]:
    """
    Fetch a city's Mapillary tile census, serialized against other processes.

    Every tile request in the repo passes through here — the grid run and the
    road walk both — which is what makes this the one place the machine-wide
    tile lock has to be taken. The CDN rate-limits per IP, so ``AsyncRateLimiter``
    bounding this process is only half the guarantee; the lock supplies the
    other half by ensuring no second process is pacing itself at the same time
    (issue #208).

    See :func:`_fetch_city_images` for the arguments and return value.

    Raises:
        HostBusyError: another process on this machine is already fetching
            tiles. Raised before any request is issued.
    """
    with host_lock(HOST_MAPILLARY_TILES):
        return await _fetch_city_images(
            city_name,
            bbox,
            access_token,
            connection_limit=connection_limit,
            request_timeout=request_timeout,
            max_requests_per_minute=max_requests_per_minute,
        )


async def _fetch_city_images(
    city_name: str,
    bbox: tuple[float, float, float, float],
    access_token: str,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
) -> dict[str, Any]:
    """
    Fetch and dedupe every Mapillary image in a bbox from the z14 vector tiles.

    Extracted from download_mapillary_metadata_async so the road-walk street
    collector (issue #99) can share the exact same tile fetch and decode: the
    two differ only in what they assign images TO afterwards — a regular grid
    lattice for a run, on-street sample points for a road walk. Mapillary is a
    tile census, not a per-point query API, so both callers pay the same
    handful of tile requests no matter how many points they score.

    Args:
        city_name: label for logging/progress only.
        bbox: (min_lon, min_lat, max_lon, max_lat), e.g. from grid_bbox.
        access_token: Mapillary client token (rides in the tile URL).
        connection_limit: max concurrent tile fetches.
        request_timeout: per-request timeout in seconds.
        max_requests_per_minute: client-side pacing cap for the tile CDN,
            which rate-limits per IP (issue #198). <= 0 disables pacing.

    Returns:
        Dict with ``census`` (the deduped columnar census — see
        :func:`records_to_census`), ``api_requests`` (tiles fetched), ``tiles``
        (tile count) and ``raw_feature_count`` (pre-dedupe).

    Raises:
        DownloadError: on a rejected token or tile transport failure, carrying
            ``api_requests`` so the caller can still record what it spent.
    """
    tiles = tiles_for_bbox(*bbox)
    logger.info(
        f"Fetching Mapillary metadata for {city_name}: {len(tiles)} z{TILE_ZOOM} "
        f"tiles covering bbox {tuple(round(v, 4) for v in bbox)}"
    )

    api_requests = 0
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    semaphore = asyncio.Semaphore(connection_limit)
    # Bounds the aggregate rate regardless of connection_limit — the semaphore
    # caps concurrency, which on a fast link still meant ~5 tiles/s (~300/min)
    # from a single city before this (issue #198).
    rate_limiter = AsyncRateLimiter(max_requests_per_minute)
    logger.info(
        f"Pacing tile requests at {max_requests_per_minute}/min"
        if max_requests_per_minute > 0
        else "Tile pacing DISABLED (max_requests_per_minute <= 0)"
    )
    progress_bar = progress(
        total=len(tiles),
        desc=f"Downloading Mapillary tiles for {city_name}",
        unit="tile",
        # Paced at 60 tiles/min (issue #198), so a large city is tens of minutes
        # of deliberately slow fetching under the scheduler's redirected log.
        logger=logger,
    )

    def count_request() -> None:
        nonlocal api_requests
        api_requests += 1

    # First whole-city condition seen: a rejected token, a host block, or an
    # error page. Every remaining tile would fail identically, so stop issuing
    # them (issue #205). Before this, `gather(return_exceptions=True)` had to
    # settle ALL tiles before the loop below could re-raise, so a blocked host
    # spent its ENTIRE tile count at the paced 60/min to learn what the first
    # response already said — Fresno: 210 requests over 3.5 minutes, twice a
    # night, into a CDN refusing every one.
    fatal: DownloadError | None = None

    async def fetch_one(x: int, y: int) -> pd.DataFrame:
        nonlocal fatal
        url = f"{TILE_URL_TEMPLATE.format(z=TILE_ZOOM, x=x, y=y)}?access_token={access_token}"
        async with semaphore:
            # The abort check belongs HERE, inside the semaphore, not at the top
            # of the coroutine: gather starts every task at once and each runs to
            # its first suspension point, so a check above this line would be
            # evaluated by all N tasks before any response has come back. Behind
            # the semaphore, tasks resume a few at a time as in-flight ones
            # drain, see the flag, and return without taking a rate-limiter
            # token or counting a request. Worst case is connection_limit
            # requests instead of the whole city.
            if fatal is not None:
                # Nothing was requested for this tile, so keep the progress bar
                # honest: a city that stopped at request 1 must not read like a
                # city that hung at tile 3. That line in
                # logs/collect_{city}_{channel}_{date}.log is how an operator
                # tells those two apart.
                progress_bar.update(1)
                return records_to_census([])
            try:
                # Pacing/counting happen inside _fetch_tile, per retried attempt.
                tile_bytes = await _fetch_tile(session, url, timeout, rate_limiter, count_request)
            except DownloadError as e:
                # ONLY DownloadError trips the abort. Per-tile failures
                # (aiohttp.ClientResponseError from a 404/429/5xx, or a decode
                # error) must still fan out to every tile — that is #168's
                # guarantee that one bad tile cannot discard a city, and it is
                # what the settle loop's MAX_FAILED_TILE_FRACTION tolerance is
                # for. Assigned before leaving the semaphore block, so no
                # waiting task can slip past the check above.
                fatal = fatal or e
                raise
        progress_bar.update(1)
        # Convert to columns HERE, not after the gather: asyncio.gather holds
        # every tile's result until the last one lands, so returning dicts kept
        # the entire city's per-image dicts alive at once — gigabytes on a big
        # census (issue #157). Converting per tile bounds that to one tile.
        return records_to_census(decode_image_features(tile_bytes, x, y))

    try:
        # Token rides in each tile URL as ?access_token= — see TILE_URL_TEMPLATE
        # comment (the tiles CDN 403s the Authorization header).
        #
        # return_exceptions: one bad tile out of hundreds used to discard the
        # whole city, for both the grid run and the road walk (issue #168). A
        # transient 404 is worth one tile, not a city.
        async with aiohttp.ClientSession() as session:
            settled = await asyncio.gather(
                *(fetch_one(x, y) for x, y in tiles), return_exceptions=True
            )
    except DownloadError as e:
        # e.g. the rejected-token error from _fetch_tile; attach the spent
        # request count so the caller can still record it in the ledger.
        e.api_requests = api_requests
        raise
    except (TimeoutError, aiohttp.ClientError) as e:
        error = DownloadError(f"Mapillary tile download failed: {redact_credentials(e)}")
        error.api_requests = api_requests
        raise error from e
    finally:
        progress_bar.close()

    # Belt and braces, and deliberately unreachable today: the task that set
    # `fatal` also re-raised, so its exception is in `settled` and the loop
    # below finds it. What this guards is a future edit that makes `fetch_one`
    # swallow or wrap that error — then every aborted tile becomes an empty
    # SUCCESS (they return an empty census, not an exception), `failed_tiles` is
    # empty, `detect_systemic_failure` doesn't reject (it only looks for
    # REQUEST_DENIED/OVER_QUERY_LIMIT), and a 0-pano census registers, publishes
    # and diffs as "every pano in the city removed" — against an immutable dated
    # snapshot. It also reports the error that actually caused the abort, rather
    # than whichever DownloadError happens to sit earliest in tile order.
    if fatal is not None:
        fatal.api_requests = api_requests
        raise fatal

    results = []
    failed_tiles: list[tuple[int, int]] = []
    first_error: BaseException | None = None
    for (x, y), outcome in zip(tiles, settled, strict=True):
        if isinstance(outcome, BaseException):
            # A bad token is a whole-city condition, not a per-tile one: every
            # remaining tile would fail the same way, so don't dress it up as
            # partial coverage.
            if isinstance(outcome, DownloadError):
                outcome.api_requests = api_requests
                raise outcome
            failed_tiles.append((x, y))
            first_error = first_error or outcome
        else:
            results.append(outcome)

    if failed_tiles:
        failed_fraction = len(failed_tiles) / len(tiles)
        detail = f"{len(failed_tiles)}/{len(tiles)} tiles failed: {redact_credentials(first_error)}"
        if failed_fraction > MAX_FAILED_TILE_FRACTION:
            error = DownloadError(
                f"Mapillary tile download failed: {detail} "
                f"({failed_fraction:.1%} > {MAX_FAILED_TILE_FRACTION:.0%} tolerated); "
                f"refusing to finalize an incomplete snapshot"
            )
            error.api_requests = api_requests
            raise error from first_error
        # Under the threshold the run continues, but the caller must mark the
        # affected grid points REQUEST_FAILED rather than let them look like
        # genuine no-imagery — see download_mapillary_metadata_async.
        logger.warning(f"Continuing with {detail}; affected grid points marked REQUEST_FAILED")

    raw_feature_count = sum(len(r) for r in results)
    census = concat_census(results)
    # BOTH names have to go before the dedup copy: `settled` and `results` hold
    # references to the same per-tile frames, so dropping either one alone frees
    # nothing and leaves a third full census resident through dedupe_census.
    del results, settled
    census = dedupe_census(census)

    return {
        "census": census,
        "api_requests": api_requests,
        "tiles": len(tiles),
        "raw_feature_count": raw_feature_count,
        # (x, y) of tiles that never came back. Empty on a clean run.
        "failed_tiles": failed_tiles,
    }


async def download_mapillary_metadata_async(
    city_name: str,
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float,
    access_token: str,
    output_csv_gz_path: str,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
) -> dict[str, Any]:
    """
    Fetch Mapillary pano metadata for a city and write it as a run csv.gz.

    Same calling convention as download_gsv_metadata_async: the caller
    decides the output filename (skip policy and dated naming live in the
    CLI/scheduler layer, not here).

    Returns:
        Dict with:
            df: DataFrame containing the metadata (METADATA_DTYPES schema)
            filename_with_path: the written .csv.gz path
            api_requests: number of tile requests issued this call
            started_at / finished_at: UTC ISO 8601 timestamps
    """
    started_at = datetime.now(UTC).isoformat()
    query_timestamp = started_at

    if not output_csv_gz_path.endswith(".csv.gz"):
        raise ValueError(f"output_csv_gz_path must end in .csv.gz, got: {output_csv_gz_path}")
    Path(os.path.dirname(os.path.abspath(output_csv_gz_path))).mkdir(parents=True, exist_ok=True)

    width_steps = int(grid_width / step_length)
    height_steps = int(grid_height / step_length)
    origin = geopy.Point(center_lat, center_lon)
    # Arrays, not a list of tuples and a dict keyed by (i, j): at Cairo's ~10.5M
    # points those two cost ~4.5 GB between them against an 8 GB cgroup, which
    # is most of why a big city looked like a hang here (issue #157). The grid
    # is a regular lattice, so a point's position is arithmetic — see
    # _grid_ordinal below — and needs no lookup table.
    grid_lats, grid_lons, _, _ = generate_grid_arrays(
        origin, width_steps, height_steps, step_length
    )
    i_values, j_values = grid_index_ranges(width_steps, height_steps)
    i_min, n_j, num_grid_points = i_values[0], len(j_values), len(grid_lats)

    def _grid_ordinal(i: np.ndarray | int, j: np.ndarray | int):
        """Position of grid index (i, j) in the generation order."""
        return (i - i_min) * n_j + (j - j_values[0])

    bbox = grid_bbox(center_lat, center_lon, grid_width, grid_height, step_length)

    fetched = await fetch_city_images_async(
        city_name,
        bbox,
        access_token,
        connection_limit=connection_limit,
        request_timeout=request_timeout,
        max_requests_per_minute=max_requests_per_minute,
    )
    # pop, not [] — `fetched` stays a live local until this function returns, so
    # indexing it would pin the whole census in memory straight through
    # build_empty_rows and both CSV writes, defeating the `del census` below.
    census = fetched.pop("census")
    api_requests = fetched["api_requests"]
    tiles = fetched["tiles"]
    results = fetched["raw_feature_count"]
    failed_tiles = fetched.get("failed_tiles") or []
    is_pano = census["is_pano"].to_numpy()
    num_panos = int(is_pano.sum())
    logger.info(
        f"Decoded {results} features "
        f"({len(census)} unique: {num_panos} panos, {len(census) - num_panos} flat) "
        f"from {tiles} tiles"
    )

    # Nearest-grid-point assignment for the WHOLE census at once. Everything
    # from here to the CSV write is array work on positional indices into the
    # census: the row-wise form built an (image, (i, j)) tuple pair and then a
    # 16-key dict per image, which is ~1.4 GB per million images and is what
    # made a census-heavy city unschedulable (issue #157).
    i_idx, j_idx, in_grid = assign_to_grid(
        census["lat"].to_numpy(),
        census["lon"].to_numpy(),
        center_lat,
        center_lon,
        width_steps,
        height_steps,
        step_length,
    )
    ordinals = _grid_ordinal(i_idx, j_idx)
    del i_idx, j_idx

    # Positions of the in-grid panos, in census order — the order the row-wise
    # loop visited them in, and therefore the row order of the output file.
    pano_positions = np.flatnonzero(in_grid & is_pano)
    pano_ordinals = ordinals[pano_positions]
    capture_dates = captured_at_to_iso_dates(
        census["captured_at_ms"].to_numpy()[pano_positions]
    ).to_numpy()
    covered_df = build_image_rows(
        census,
        pano_positions,
        grid_lats[pano_ordinals],
        grid_lons[pano_ordinals],
        query_timestamp,
        status_for_capture_dates(capture_dates),
        capture_dates,
    )
    del capture_dates

    # Flat imagery (issue #116): tally every in-grid flat image for the census
    # magnitude, and keep one representative per grid point so a flat-only
    # point (a point with flats but no pano) can be written as a single
    # FLAT_ONLY marker row.
    flat_positions = np.flatnonzero(in_grid & ~is_pano)
    num_flat_images = len(flat_positions)
    # return_index gives the FIRST occurrence of each ordinal, matching the
    # dict.setdefault this replaces — the earliest flat in census order stays
    # the representative for its point.
    _, first_of_point = np.unique(ordinals[flat_positions], return_index=True)
    flat_positions = flat_positions[np.sort(first_of_point)]
    # A pano already covers that point, so it is not flat-ONLY.
    flat_positions = flat_positions[
        ~np.isin(ordinals[flat_positions], pano_ordinals, assume_unique=False)
    ]
    flat_ordinals = ordinals[flat_positions]
    flat_only_df = build_image_rows(
        census,
        flat_positions,
        grid_lats[flat_ordinals],
        grid_lons[flat_ordinals],
        query_timestamp,
        FLAT_ONLY,
        # capture_date is deliberately null for FLAT_ONLY: this row is a
        # coverage-presence marker, and a null date keeps flat timestamps out
        # of every date/age/histogram path (which key on status == 'OK').
        None,
    )
    num_flat_only_points = len(flat_positions)
    # Deliberately NOT pd.concat'd onto covered_df. covered_df is one row per
    # in-grid pano — 6.5M rows of mostly-object columns at Colorado Springs —
    # and concatenating copies all of it to append a frame that is at most one
    # row per grid point. The two are written to the gzip handle in sequence
    # instead, which is byte-identical, for the same reason the empty fill is
    # (see the write below).
    del census, is_pano, in_grid

    # Which grid points nothing covered, via a bitmap rather than np.setdiff1d:
    # setdiff1d sorts and uniques BOTH operands, including the
    # num_grid_points-long arange that is unique by construction — O(n log n)
    # plus several int64 temporaries over the biggest array in the function.
    # This is O(n) and one byte per point, and needs no concatenate either.
    covered = np.zeros(num_grid_points, dtype=bool)
    covered[pano_ordinals] = True
    covered[flat_ordinals] = True
    empty_ordinals = np.flatnonzero(~covered)
    del covered, pano_ordinals, flat_ordinals, ordinals

    # The empty-grid-point fill, built COLUMN-WISE rather than as one dict per
    # point. This is the single biggest allocation in the whole pipeline: a
    # 16-key dict is 464 bytes, so Cairo's ~10.5M points cost ~4.9 GB of dicts
    # plus another ~6 GB when pandas turns them into an N x 16 object matrix —
    # on a cgroup capped at 8 GB (issue #157). As arrays the same fill is two
    # float columns and a handful of all-null ones.
    empty_df = build_empty_rows(
        grid_lats[empty_ordinals],
        grid_lons[empty_ordinals],
        query_timestamp,
        "ZERO_RESULTS",
    )
    # An uncovered point inside a tile that never downloaded is UNKNOWN, not
    # empty. Left as ZERO_RESULTS it would be indistinguishable from genuine
    # no-imagery and would quietly understate coverage and pollute the
    # run-to-run diff, so it gets its own status — the same shape as the GSV
    # downloader writing REQUEST_FAILED rows for its sub-threshold holes
    # (issue #168). REQUEST_FAILED counts toward neither 360° nor any-imagery
    # coverage, and is already one of analysis.SYSTEMIC_FAILURE_STATUSES.
    if failed_tiles:
        in_failed_tile = _points_in_tiles(
            grid_lats[empty_ordinals], grid_lons[empty_ordinals], failed_tiles
        )
        empty_df.loc[in_failed_tile, "status"] = REQUEST_FAILED
        num_failed_points = int(in_failed_tile.sum())
        logger.warning(
            f"{num_failed_points:,} grid points fall in {len(failed_tiles)} undownloaded "
            f"tile(s); written as {REQUEST_FAILED} rather than empty"
        )
    num_empty_points = len(empty_ordinals)
    del empty_ordinals

    # Stream straight into the gzip handle, and write the three frames in
    # sequence rather than pd.concat'ing them first. df.to_csv() with no path
    # built the ENTIRE csv as one Python str and then a second full copy as
    # bytes — about 1.7 GB of pure duplication at Cairo scale, for a file we
    # are writing out anyway — and each concat was another full copy of the
    # frames it joined. Appending with header=False is byte-identical to
    # concatenating: same columns, same order, panos then flat-only then empty.
    with gzip.open(output_csv_gz_path, "wt", encoding="utf-8", newline="") as f:
        covered_df.to_csv(f, index=False)
        if num_flat_only_points:
            flat_only_df.to_csv(f, index=False, header=False)
        if num_empty_points:
            empty_df.to_csv(f, index=False, header=False)
    del covered_df, flat_only_df, empty_df

    # Read back through the shared loader so dtypes match GSV runs exactly
    df = load_city_csv_file(output_csv_gz_path)
    n_pano_rows = int(df["status"].isin(("OK", "NO_DATE")).sum())
    logger.info(
        f"Wrote {len(df)} rows ({n_pano_rows} pano rows, "
        f"{num_flat_only_points} flat-only points, {num_flat_images} flat images, "
        f"{num_empty_points} empty grid points) "
        f"to {output_csv_gz_path}"
    )

    return {
        "df": df,
        "filename_with_path": output_csv_gz_path,
        "api_requests": api_requests,
        # Census magnitude of flat imagery (issue #116): every in-grid flat
        # image, including those at points that also hold a pano. Not
        # reconstructable from the CSV (flat-only points collapse to one
        # FLAT_ONLY row), so it is threaded to the catalog separately.
        "num_flat_images": num_flat_images,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }
