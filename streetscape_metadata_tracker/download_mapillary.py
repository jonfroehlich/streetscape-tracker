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

The tile census CHECKPOINTS (issue #256). It did not always, and the reason
it does now is not that cities got slower: at 60 tiles/min (issue #198) the
catalog's worst city is ~15 minutes, but each channel's daily budget is 1,750
tile requests against a per-IP limit that has blocked this host twice, so a
re-spent census is not merely slow -- it is charged against the same rolling
window the block is drawn from. An interrupted city now resumes for its
MISSING tiles only, and tiles fetched before a block survive to the next
night. Resume is strictly next-invocation: no in-process retry is added
anywhere, because retrying during a block is reported to extend it
(docs/provider-access.md).

The caller supplies the checkpoint path and discards it once its artifact is
durable -- see checkpointing.py, and the tile-keyed reassembly contract in
the checkpoint section below.
"""

import asyncio
import json
import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import aiohttp
import backoff
import mapbox_vector_tile
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# Aliased, because `census` is also the name of the local DataFrame this
# module passes around (see fetch_city_images_async). Importing the module
# as `census` would work today only because those locals live in other
# functions -- and would break the first time someone called a census
# helper from one of them, with a DataFrame AttributeError.
from . import census as census_core

# A name imported below in the redundant `X as X` form is a RE-EXPORT this
# module does not itself use (the form is what tells ruff that, rather than a
# per-line noqa). Each one used to be DEFINED here and has since moved -- the
# grid geodesy down to download_common, so a second census provider can reach it
# without importing this module, and the census row/status helpers into
# census.py. The aliases keep every existing `download_mapillary.X` call site
# working: the street analyzer's collect_mapillary, and ~20 test references.
from .analysis import FLAT_ONLY as FLAT_ONLY
from .census import dedupe_census
from .census import status_for_capture_dates as status_for_capture_dates
from .checkpointing import (
    CHECKPOINT_MAX_AGE_S,
    MAPILLARY_CHECKPOINT_FORMAT_VERSION,
    CensusCache,
    _bbox_matches,
    _fsync_dir,
    _remove_empty_checkpoint_dir,
    _state_path,
    _write_json_durable,
    census_cache_marker,
    discard_checkpoint,
    load_cached_store,
    observation_timestamp,
    promote_checkpoint_to_cache,
    reconcile_cache_hit,
    reused_census_provenance,
)
from .config import MAPILLARY_METADATA_DTYPES
from .download_common import _M_PER_DEG_LAT as _M_PER_DEG_LAT
from .download_common import (
    HOST_MAPILLARY_TILES,
    AsyncRateLimiter,
    DownloadError,
    HostBlockedError,
    redact_credentials,
)
from .download_common import assign_to_grid as assign_to_grid
from .download_common import grid_bbox as grid_bbox
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

    Mapillary's binding of :func:`census_core.records_to_census`.

    Args:
        records: decoded image dicts from :func:`decode_image_features`.

    Returns:
        A DataFrame with the :data:`_CENSUS_DTYPES` columns, one row per image.
    """
    return census_core.records_to_census(records, _CENSUS_DTYPES)


def concat_census(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Combine per-tile census frames into one, preserving tile order."""
    return census_core.concat_census(frames, _CENSUS_DTYPES)


def _mapillary_image_columns(picked: pd.DataFrame) -> dict[str, Any]:
    """
    Mapillary's own output columns: the copyright convention plus its extras.

    Handed to :func:`census_core.build_image_rows`, which fills the shared core.
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


def _mapillary_capture_dates(census_frame: pd.DataFrame, positions: np.ndarray) -> np.ndarray:
    """
    Capture dates for the census rows at ``positions``, per Mapillary's rules.

    Handed to :func:`census_core.write_census_grid_run`. Takes positions rather
    than a taken sub-frame so this indexes the ONE column it needs -- a full
    ``.take()`` here would materialize every column of a multi-million-row
    census a second time (issue #157).
    """
    return captured_at_to_iso_dates(census_frame["captured_at_ms"].to_numpy()[positions]).to_numpy()


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
    Mapillary's binding of :func:`census_core.build_image_rows`; see there for the
    argument contract.
    """
    return census_core.build_image_rows(
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

    Mapillary's binding of :func:`census_core.build_empty_rows`.
    """
    return census_core.build_empty_rows(
        query_lat, query_lon, query_timestamp, status, dtypes=MAPILLARY_METADATA_DTYPES
    )


# ── Grid assignment ────────────────────────────────────────────────────────


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
# distribution has a long tail — the largest grid in the catalog is Moscow at
# 870 tiles (~15 min), and before #166 capped oversized grids Anchorage's
# 105x84 km frame alone was ~6,480 tiles (~108 min); it is 575 now. Either way
# the tail is why scheduler.city_timeout_seconds derives a Mapillary timeout
# from the tile count instead of using the flat floor. Re-measure rather than
# trusting these figures — grid re-registration moves them, and an earlier copy
# of this comment outlived its own numbers (docs/provider-access.md).
#
# Per-process, like the GSV limiter: N concurrent collections present N times
# this rate to the CDN. Do not run Mapillary collections in parallel.
DEFAULT_TILE_REQUESTS_PER_MINUTE = 60

# Jitter fraction for the tile pacer (issue #292): request gaps vary uniformly
# by ±this around 60/DEFAULT_TILE_REQUESTS_PER_MINUTE. Non-zero BY DEFAULT: a
# saturated token bucket emits tiles at an exact 1.000 s cadence over
# sequential z14 tiles from a datacenter IP, and after three per-IP blocks that
# regularity is the one property of our traffic never changed between restarts
# (rate and daily volume were both falsified, #286). 0 restores the metronome.
DEFAULT_TILE_JITTER = 0.6

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


# ── The tile checkpoint on disk (issue #256) ───────────────────────────────
#
# Layout, one directory per (city, grid geometry, channel) — the naming rules
# and the caller-discards contract live in `checkpointing.py` and are
# preconditions for this section, not background:
#
#     <checkpoint_path>/
#       state.json                the commit record; written LAST
#       tile-2621-6335.parquet    one committed tile's census rows
#
# PARTS ARE KEYED BY TILE (x, y), NOT BY FETCH ORDER. That is the one real
# divergence from KartaView's checkpoint, and it is forced: KartaView visits its
# cells in a deterministic order, so it can number parts 0..n and replay them in
# that order, while this module fetches tiles CONCURRENTLY (`asyncio.gather`
# behind a semaphore) and completion order is nondeterministic. A fetch-order
# index here would make a resumed census depend on which tiles happened to land
# first before the interruption.
#
# What replaces it stores nothing: `tiles_for_bbox` is pure, so the reassembly
# order is RECOMPUTED — walk the tile list and take each tile's frame from this
# run if it was fetched now, else from its part file. `gather` already preserves
# argument order, so an uninterrupted run and a resumed one hand `concat_census`
# positionally identical input. That is what keeps `dedupe_census`'s
# first-position/last-value rule on a border duplicate from depending on which
# night fetched which copy — and with it the byte-for-byte golden fixture, and
# `diff.py`'s freedom from phantom imagery churn in every Mapillary city.
#
# ONLY SUCCESSFUL TILES ARE COMMITTED. A tile that 404s or times out is left out
# of the record and re-requested next invocation, so #168's tolerance keeps
# measuring failures against the FULL tile set rather than against whatever this
# process happened to attempt. One tile is one paced request, so committing per
# tile is at most one small write per second at the shipped 60/min — and it is
# what makes #205's fail-fast salvage automatic, since everything fetched before
# the fatal is already durable by the time it re-raises.
#
# A ZERO-ROW TILE GETS A RECORD AND NO FILE. Most tiles over a real bbox are
# empty, and writing a part for each would mean 870 files for Moscow to say
# nothing. The record's row count is what tells "empty tile, already fetched"
# from "not fetched yet".
#
# CHECKPOINTING FAILS OPEN, which is a deliberate divergence from KartaView's
# fail-fast. There it is right: ten hours of paid-for crawl is worth more than
# the night that loses it. Here the worst city in the catalog is ~15 minutes, so
# the same trade goes the other way — an unwritable directory or a failing
# commit logs one warning and the fetch carries on unprotected, because a city
# must never fail over its own safety net.

# MAPILLARY_CHECKPOINT_FORMAT_VERSION is declared in checkpointing.py (imported
# above) beside KartaView's, so the census cache probe can tell whether an
# entry's commit record is one this loader reads without importing this module.
CHECKPOINT_PART_TEMPLATE = "tile-{x}-{y}.parquet"


@dataclass
class TileCheckpoint:
    """Handle to an on-disk tile checkpoint, loaded or freshly opened."""

    path: str
    channel: str | None = None
    # What distinguishes two crawls of one city inside one channel -- a walk's
    # --network-type. None for a grid run, which has exactly one.
    variant: str | None = None
    # When the FIRST tile of this crawl was committed, carried forward across
    # every later write. The age cap is measured from here rather than from
    # `updated_at`, because `updated_at` advances on writes that commit no tile
    # (see _commit_spend) and would otherwise let a nightly-refused city hold a
    # months-old crawl under the cap forever.
    created_at: str | None = None
    # (x, y) -> committed row count. Membership means "this tile is done"; the
    # count is what distinguishes a committed empty tile from a missing part.
    done: dict[tuple[int, int], int] = field(default_factory=dict)
    # Spend of the PREVIOUS invocations only. This process adds its own on top,
    # and the sum is what reaches the catalog row (never the daily ledger).
    api_requests_before: int = 0
    # Latched by the first failed commit, so the warning is logged once per run
    # and the rest of the fetch runs uncheckpointed rather than retrying a
    # directory that has already proved unwritable.
    degraded: bool = False


def _tile_part_path(path: str, x: int, y: int) -> str:
    return os.path.join(path, CHECKPOINT_PART_TEMPLATE.format(x=x, y=y))


def _validate_tile_store(
    path: str,
    state: dict,
    *,
    bbox: tuple[float, float, float, float],
    tiles: list[tuple[int, int]],
) -> tuple[dict[tuple[int, int], int] | None, str | None]:
    """
    The geometric/footer cascade every reader of a tile store makes.

    ``(done, None)`` when the directory holds what its commit record claims for
    THIS lattice, ``(None, reason)`` otherwise. Raises nothing of its own; a
    malformed record surfaces as an exception the callers' broad handler turns
    into a reason, which is the never-raise posture they both keep.

    Factored out because a promoted cache entry (issue #290) IS a checkpoint
    directory that was moved, so it must be validated exactly as a resume is --
    and a second copy of a cascade this long is how the two would come to
    disagree about, say, what a zero-row tile means. What each caller adds on
    top is what differs: :func:`load_tile_checkpoint` adds the channel, the
    variant and the age of the crawl; :func:`load_cached_census` adds the
    marker's own window and the one check a resume must NOT make, completeness.

    Args:
        path: the directory holding ``state.json`` and the parts.
        state: its parsed commit record.
        bbox: this run's frame. A different one means a different lattice.
        tiles: this run's tile list, from :func:`tiles_for_bbox`.
    """
    if state["format_version"] != MAPILLARY_CHECKPOINT_FORMAT_VERSION:
        return None, (
            f"it is format v{state['format_version']}, this build writes "
            f"v{MAPILLARY_CHECKPOINT_FORMAT_VERSION}"
        )
    if not _bbox_matches(state["bbox"], bbox):
        return None, f"it covers bbox {state['bbox']}, this run uses {list(bbox)}"
    if int(state["zoom"]) != TILE_ZOOM:
        # Only z14 carries per-image metadata today, so this cannot fire on
        # current builds. It is here because the tile INDICES in every part
        # name mean nothing without the zoom that produced them.
        return None, f"it was fetched at z{state['zoom']}, this run uses z{TILE_ZOOM}"
    if int(state["tile_count"]) != len(tiles):
        # Catches a change to tiles_for_bbox itself, which would leave the
        # stored tile indices describing a lattice this run does not have.
        return None, f"it covers {state['tile_count']} tiles, this run has {len(tiles)}"
    done = {(int(x), int(y)): int(rows) for x, y, rows in state["done_tiles"]}
    if not done.keys() <= set(tiles):
        return None, "it holds tiles this run's lattice does not contain"
    # Verify the parts from their FOOTERS — a seek to the end of each file,
    # costing nothing — rather than discovering a truncated one at
    # reassembly, after the fetch has already been paid for.
    rows_on_disk = 0
    for (x, y), rows in done.items():
        if rows == 0:
            continue  # committed empty tile; no part by design
        part = _tile_part_path(path, x, y)
        if not os.path.exists(part):
            return None, f"committed part {os.path.basename(part)} is missing"
        found = pq.ParquetFile(part).metadata.num_rows
        if found != rows:
            return None, (
                f"part {os.path.basename(part)} holds {found} rows where the "
                f"commit record says {rows}"
            )
        rows_on_disk += found
    if rows_on_disk != int(state["census_rows"]):
        return None, (
            f"its parts hold {rows_on_disk} rows where the commit record says "
            f"{state['census_rows']}"
        )
    return done, None


def load_tile_checkpoint(
    path: str,
    *,
    bbox: tuple[float, float, float, float],
    tiles: list[tuple[int, int]],
    channel: str | None = None,
    variant: str | None = None,
) -> TileCheckpoint | None:
    """
    Resume state for this census, or None if there is nothing usable here.

    NEVER RAISES, following :func:`download_kartaview.load_checkpoint` and
    :func:`download_gsv.get_processed_points`: every failure degrades to
    "fetch every tile" with a warning. A checkpoint is not a comparison whose
    mismatch corrupts an artifact — the worst case of ignoring one is a
    re-spend, so refusing outright would cost a night to protect nothing.

    Unlike KartaView's, an unusable checkpoint here is DELETED rather than left
    in place. Its parts are named for the tiles they hold, so a stale directory
    that is never resumed is also never overwritten, and would otherwise sit
    there until the age cap swept it.

    Args:
        path: the checkpoint directory. Need not exist.
        bbox: this run's frame. A different one means a different lattice.
        tiles: this run's tile list, from :func:`tiles_for_bbox`.
        channel: which api_usage channel this census meters into. The PATH
            already keys the channel, but the path is caller-built: a directory
            moved by hand, or a future caller deriving it wrong, would pass
            every geometric check here and resume a census whose spend belongs
            to another ledger — under another credential, for this provider.
        variant: what separates two crawls of one city WITHIN one channel — a
            walk's --network-type, None for a grid run. Same argument as the
            channel one level down, and it is the half a wrong path cannot fix:
            two walks of one city agree on the ledger, the credential and every
            geometric parameter, so nothing else here would refuse them.
    """

    def discard(reason: str) -> None:
        logger.warning(f"Ignoring the Mapillary tile checkpoint at {path}: {reason}")
        discard_checkpoint(path)

    state_path = _state_path(path)
    if not os.path.exists(state_path):
        return None  # the ordinary first-run case; not worth a line of log
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("channel") != channel:
            discard(
                f"it belongs to the {state.get('channel')!r} channel and this run is "
                f"{channel!r}; the two meter into different api_usage ledgers"
            )
            return None
        if state.get("variant") != variant:
            discard(
                f"it belongs to the {state.get('variant')!r} crawl of this channel and "
                f"this run is {variant!r}; resuming it would price this crawl with "
                f"another one's requests"
            )
            return None
        # MEASURED FROM created_at -- WHEN THE OLDEST ROW WAS FETCHED -- NOT FROM
        # updated_at. This is the one guard here that protects an ARTIFACT rather
        # than a night's work (frozen geometry never changes, so every other
        # check still passes months later and resuming would splice last
        # quarter's rows into a snapshot dated today), and `updated_at` cannot
        # carry it: _commit_spend rewrites the record on a failure that committed
        # NO tile, and a host-blocked night deliberately records no
        # `consecutive_failures`, so the same stalest city is re-attempted every
        # night and would refresh its own clock indefinitely. Ageing from the
        # first commit bounds what a single dated snapshot can span, which is
        # what the cap is actually for. See checkpointing.CHECKPOINT_MAX_AGE_S.
        age_s = (datetime.now(UTC) - datetime.fromisoformat(state["created_at"])).total_seconds()
        if age_s > CHECKPOINT_MAX_AGE_S:
            discard(
                f"its first tile was committed {age_s / 86400:.1f} days ago, past the "
                f"{CHECKPOINT_MAX_AGE_S / 86400:.0f}-day limit; its rows would be spliced "
                f"into a snapshot dated today"
            )
            return None
        done, reason = _validate_tile_store(path, state, bbox=bbox, tiles=tiles)
        if reason is not None:
            discard(reason)
            return None
        cp = TileCheckpoint(
            path=path,
            channel=channel,
            variant=variant,
            created_at=state["created_at"],
            done=done,
            api_requests_before=int(state["api_requests_total"]),
        )
    except Exception as e:
        # Broad on purpose; see the NEVER RAISES note above. A checkpoint that
        # cannot be read must cost a re-fetch, never a city.
        discard(f"{type(e).__name__}: {e}")
        return None

    _purge_checkpoint_debris(cp.path, cp.done)
    return cp


def load_cached_census(
    cache_path: str,
    *,
    bbox: tuple[float, float, float, float],
    tiles: list[tuple[int, int]],
    run_date: date | None = None,
) -> tuple[TileCheckpoint, dict] | None:
    """
    A COMPLETE census another consumer already paid for, or None (issue #290).

    :func:`checkpointing.load_cached_store` -- which owns the marker window, the
    ``run_date`` rule, the never-raise posture and the delete-what-is-refused
    contract -- with this provider's two checks plugged in: the same
    geometric/footer cascade a resume makes (:func:`_validate_tile_store`), and
    COMPLETENESS, which a resume deliberately does NOT check and which is the
    whole difference between the two. ``done`` plus the tiles the fetcher
    recorded as FAILED must be the entire lattice; a partial entry reused as a
    census would publish the missing tiles' grid points as genuine no-imagery,
    absence never observed, in an immutable dated snapshot.

    Returns ``(store, marker)``: a :class:`TileCheckpoint` handle whose ``done``
    is what the entry holds (so :func:`_checkpoint_frame_for_tile` reads it back
    unchanged), and the provenance record naming who paid and when.
    """

    def validate(state: dict) -> tuple[dict[tuple[int, int], int] | None, str | None]:
        return _validate_tile_store(cache_path, state, bbox=bbox, tiles=tiles)

    def is_complete(done: dict[tuple[int, int], int], state: dict, marker: dict) -> str | None:
        failed = {(int(x), int(y)) for x, y in marker.get("failed") or []}
        if done.keys() | failed == set(tiles):
            return None
        missing = len(set(tiles) - done.keys() - failed)
        return (
            f"it covers {len(done)} fetched + {len(failed)} failed of {len(tiles)} tiles, "
            f"leaving {missing} never observed; only a COMPLETE census is reusable"
        )

    loaded = load_cached_store(
        cache_path,
        label="Mapillary census",
        run_date=run_date,
        validate=validate,
        is_complete=is_complete,
    )
    if loaded is None:
        return None
    done, marker = loaded
    return TileCheckpoint(path=cache_path, done=done), marker


def _purge_checkpoint_debris(path: str, done: dict[tuple[int, int], int]) -> None:
    """
    Delete part files nothing committed, and any staging leftovers.

    A part written for a tile that never reached the commit record is a torn
    write: the process died between ``to_parquet`` and ``state.json``. Under
    KartaView's fetch-order indices those names are reused and MUST be cleared;
    here they would simply be overwritten by the tile's next attempt, so this is
    housekeeping rather than correctness — a census that never completes would
    otherwise accumulate them in a directory that already holds a partial one.
    Best effort by the same argument as the rest of this file's checkpointing.

    Takes ``done`` rather than a checkpoint because it must also run when there
    is NO commit record: a process that died between its first ``to_parquet``
    and its first ``state.json`` leaves a part that :func:`load_tile_checkpoint`
    returns too early to reach. That orphan is not merely untidy — if the tile
    later commits as EMPTY, ``done`` claims it, the footer loop skips it on
    ``rows == 0``, and the file survives every later purge holding rows nothing
    will ever read.

    Args:
        path: the checkpoint directory. Need not exist.
        done: the committed tiles, or ``{}`` when nothing is committed yet.
    """
    if not os.path.isdir(path):
        return
    try:
        for name in os.listdir(path):
            if name.endswith(".tmp"):
                os.remove(os.path.join(path, name))
                continue
            if not name.startswith("tile-") or not name.endswith(".parquet"):
                continue
            try:
                _, x, y = name[: -len(".parquet")].split("-")
                committed = (int(x), int(y)) in done
            except ValueError:
                committed = False
            if not committed:
                os.remove(os.path.join(path, name))
    except OSError as e:
        logger.warning(f"Could not tidy the Mapillary tile checkpoint at {path}: {e}")


def _open_tile_checkpoint(
    path: str | None,
    *,
    bbox: tuple[float, float, float, float],
    tiles: list[tuple[int, int]],
    channel: str | None,
    variant: str | None = None,
) -> TileCheckpoint | None:
    """
    Prepare the checkpoint directory and load any resumable state.

    THE LOAD RUNS BEFORE THE ``makedirs``, AND THAT ORDER IS LOAD-BEARING. An
    unusable checkpoint here is DELETED rather than left in place (see
    :func:`load_tile_checkpoint`), so a directory created first would be the one
    the discard removes — and the fresh handle returned below would point at
    nothing, every commit would fail, ``degraded`` would latch on the first
    tile, and the city would fetch with no checkpoint at all. That is the case
    the age cap produces, i.e. the first attempt after a multi-day block: the
    one run that most needs protecting would be the one running without it.
    Creating afterwards still happens before the first request, which is the
    property that actually matters — an unwritable path must fail in a second
    rather than fifteen minutes in.

    Returns None when the caller passed no path (checkpointing off, the
    byte-for-byte historical behaviour) or when the directory cannot be created
    — see the FAILS OPEN paragraph above.
    """
    if path is None:
        return None
    resumed = load_tile_checkpoint(path, bbox=bbox, tiles=tiles, channel=channel, variant=variant)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.warning(f"Could not open a tile checkpoint at {path}; fetching unprotected: {e}")
        return None
    if resumed is not None:
        return resumed
    # Nothing to resume, so nothing has swept this directory: a part left by a
    # process that died before its first commit record is still here, and
    # load_tile_checkpoint returns at the missing state.json before it can look.
    _purge_checkpoint_debris(path, {})
    return TileCheckpoint(path=path, channel=channel, variant=variant)


def _commit_tile(
    cp: TileCheckpoint,
    x: int,
    y: int,
    frame: pd.DataFrame,
    *,
    bbox: tuple[float, float, float, float],
    tiles: list[tuple[int, int]],
    api_requests_total: int,
) -> None:
    """
    Make one fetched tile durable: its part first, then the commit record.

    The ordering is the commit point. ``state.json`` is written last and
    atomically, so a part that exists without being counted never happened —
    :func:`_purge_checkpoint_debris` sweeps it and the tile is simply refetched.

    Best effort, and it LATCHES: the first failure warns once and turns
    checkpointing off for the rest of the run. A Mapillary census is short
    enough that losing the safety net costs less than failing a city over it.
    """
    if cp.degraded:
        return
    try:
        if len(frame):
            part = _tile_part_path(cp.path, x, y)
            tmp = f"{part}.tmp"
            frame.to_parquet(tmp, index=False)
            # FSYNCED BEFORE THE RENAME, and the directory after it, exactly as
            # download_kartaview._commit_checkpoint does and for the same
            # reason: without them the part-then-state ordering below holds
            # against a PROCESS crash (where the page cache survives and the
            # fsync buys nothing) but not against a power loss, where the two
            # renames may reach the disk in either order. What that leaves is
            # not a wrong artifact -- load_tile_checkpoint's footer check
            # catches a part shorter than its record -- but it discards the
            # WHOLE checkpoint rather than the last tile, which on makelab2's
            # ZFS is the loss this exists to prevent. Four fsyncs per tile at
            # 60 tiles/min is not a cost worth reasoning about.
            with open(tmp, "rb+") as f:
                os.fsync(f.fileno())
            os.replace(tmp, part)
            _fsync_dir(cp.path)
        done = dict(cp.done)
        done[(x, y)] = len(frame)
        _write_checkpoint_state(
            cp, done, bbox=bbox, tiles=tiles, api_requests_total=api_requests_total
        )
        # Only after the record is durable, so an in-memory `done` can never
        # claim a tile the next invocation would not find.
        cp.done = done
    except Exception as e:
        cp.degraded = True
        logger.warning(
            f"Could not checkpoint tile ({x}, {y}) at {cp.path}; continuing "
            f"unprotected for the rest of this city: {e}"
        )


def _write_checkpoint_state(
    cp: TileCheckpoint,
    done: dict[tuple[int, int], int],
    *,
    bbox: tuple[float, float, float, float],
    tiles: list[tuple[int, int]],
    api_requests_total: int,
) -> None:
    """
    Write the commit record atomically. Raises; callers decide what that costs.

    ``created_at`` is stamped by the FIRST write of a crawl and carried forward
    by every later one, including the ones that commit no tile. It is what the
    age cap is measured against, so it must describe the oldest row this
    checkpoint holds rather than the last time anything touched the file.
    ``updated_at`` still moves, for an operator reading the directory.
    """
    created_at = cp.created_at or datetime.now(UTC).isoformat()
    state = {
        "format_version": MAPILLARY_CHECKPOINT_FORMAT_VERSION,
        "bbox": list(bbox),
        "zoom": TILE_ZOOM,
        "channel": cp.channel,
        "variant": cp.variant,
        "tile_count": len(tiles),
        "done_tiles": [[tx, ty, rows] for (tx, ty), rows in done.items()],
        "census_rows": sum(done.values()),
        "api_requests_total": api_requests_total,
        "created_at": created_at,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _write_json_durable(_state_path(cp.path), state)
    # Only after the record naming it is durable, so a crash cannot leave the
    # in-memory stamp claiming an origin no file records.
    cp.created_at = created_at


def _commit_spend(
    cp: TileCheckpoint | None,
    *,
    bbox: tuple[float, float, float, float],
    tiles: list[tuple[int, int]],
    api_requests_total: int,
) -> None:
    """
    Persist spend that happened AFTER the last committed tile.

    Without this the crawl total on the catalog row silently under-reports a
    night that ended badly: the requests a block refused are counted into
    ``api_usage`` (deliberately — one token, one increment, one request) but
    would die with the process, so a resumed run's row would price the city
    below what it actually cost. Bounded by #205's fail-fast at
    ``connection_limit`` requests, and therefore small — but it is exactly the
    process-vs-crawl distinction this pair of counters exists to keep straight.

    Skipped when nothing was committed: there is no crawl to resume, the
    exception carries the spend to the ledger, and writing state here would
    leave a directory behind that ``_remove_empty_checkpoint_dir`` should take.

    THIS IS THE WRITE THAT MUST NOT AGE A CHECKPOINT. It runs on a night that
    committed no tile — a city refused at request 1 — and a host-blocked night
    records no ``consecutive_failures``, so the same stalest city is re-attempted
    the next night and the next. If the age cap were measured from the timestamp
    this write moves, a city could refresh its own clock forever and hold rows
    from any distance in the past under the limit. ``created_at`` is carried
    forward instead; see :func:`_write_checkpoint_state`.
    """
    if cp is None or cp.degraded or not cp.done:
        return
    try:
        _write_checkpoint_state(
            cp, cp.done, bbox=bbox, tiles=tiles, api_requests_total=api_requests_total
        )
    except Exception as e:  # pragma: no cover - same fail-open posture as _commit_tile
        logger.warning(f"Could not record the interrupted spend at {cp.path}: {e}")


def _census_requests_total(cp: TileCheckpoint | None, api_requests: int) -> int:
    """This census's spend across every invocation, checkpointed or not."""
    return (cp.api_requests_before if cp else 0) + api_requests


def _checkpoint_frame_for_tile(cp: TileCheckpoint, x: int, y: int) -> pd.DataFrame:
    """Read one committed tile back. An empty tile has a record but no file."""
    if cp.done[(x, y)] == 0:
        return records_to_census([])
    return pd.read_parquet(_tile_part_path(cp.path, x, y))


def _reuse_cached_census(
    cached: tuple[TileCheckpoint, dict],
    *,
    city_name: str,
    tiles: list[tuple[int, int]],
    checkpoint_channel: str | None,
    checkpoint_variant: str | None,
) -> dict[str, Any]:
    """
    Assemble a census from the shared cache. Zero requests (issue #290).

    Reassembly walks ``tiles`` IN TILE ORDER, the same loop the fetch path ends
    with, because that order is the whole byte-identity mechanism:
    ``dedupe_census`` keeps a repeated image id at the position of its FIRST
    appearance, and a border image lands in two tiles. Reading the parts in any
    other order (a directory glob's, say) would resolve those duplicates
    differently and the reused census would differ from the fetched one in every
    city that straddles a tile edge.

    The request counters and the provenance come from
    :func:`checkpointing.reused_census_provenance`, the one rule for both
    providers: ``api_requests`` is 0 unconditionally, and ``api_requests_total``
    is the crawl's cost only for the (channel, variant) that paid it.

    Failed tiles are inherited from the marker rather than re-probed. The reuser
    is publishing the SAME observation, so the same grid points read
    REQUEST_FAILED in both artifacts -- which is the honest answer, and the
    alternative (a handful of fresh requests for those tiles) would mix two
    moments into one dated snapshot for no gain. The one reader for whom that is
    wrong -- the crawl's OWN channel re-finalizing after a tail crash, which a
    resume would re-probe -- never reaches here:
    :func:`checkpointing.reconcile_cache_hit` hands such an entry back to its
    checkpoint first.
    """
    store, marker = cached
    fetched_by = marker.get("fetched_by")
    crawl_started_at = marker.get("crawl_started_at")
    # WARNING, not INFO: a collection that issues no request is otherwise
    # indistinguishable from a real one in its artifact, and this line is what
    # an operator reading logs/collect_{city}_{channel}_{date}.log has to tell
    # them apart -- the same argument the COMPLETE-checkpoint notice makes.
    logger.warning(
        f"REUSING the Mapillary census fetched by {fetched_by} (crawl started "
        f"{crawl_started_at}) for {city_name}: 0 tile requests"
    )
    results = [_checkpoint_frame_for_tile(store, x, y) for (x, y) in tiles if (x, y) in store.done]
    raw_feature_count = sum(len(r) for r in results)
    census = concat_census(results)
    # The same release the fetch path makes before dedupe copies: `results`
    # holds a reference to every part frame, and a big city's census must not be
    # resident twice through dedupe_census (issue #157).
    del results
    census = dedupe_census(census)
    return {
        "census": census,
        "tiles": len(tiles),
        "raw_feature_count": raw_feature_count,
        "num_images": len(census),
        "num_panos": int(census_core.census_is_pano(census).sum()),
        "failed_tiles": [(int(x), int(y)) for x, y in marker.get("failed") or []],
        # api_requests, api_requests_total, checkpoint_path and the provenance
        # columns: the accounting both providers must agree on, from one place.
        **reused_census_provenance(marker, channel=checkpoint_channel, variant=checkpoint_variant),
    }


async def fetch_city_images_async(
    city_name: str,
    bbox: tuple[float, float, float, float],
    access_token: str,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    checkpoint_variant: str | None = None,
    census_cache: CensusCache | None = None,
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
    # The lock hold covers the CHECKPOINT as well as the requests, which is why
    # the checkpoint needs no lock of its own: only one process on this machine
    # is inside this block, so its commits cannot race another's (issue #239
    # makes the same argument for the same host).
    with host_lock(HOST_MAPILLARY_TILES):
        try:
            return await _fetch_city_images(
                city_name,
                bbox,
                access_token,
                connection_limit=connection_limit,
                request_timeout=request_timeout,
                max_requests_per_minute=max_requests_per_minute,
                jitter=jitter,
                checkpoint_path=checkpoint_path,
                checkpoint_channel=checkpoint_channel,
                checkpoint_variant=checkpoint_variant,
                census_cache=census_cache,
            )
        except BaseException:
            # A city that failed before committing anything -- a rejected token,
            # a block on request 1 -- would otherwise leave an empty directory
            # behind on every attempt. os.rmdir refuses a non-empty one, so a
            # real checkpoint is never touched.
            _remove_empty_checkpoint_dir(checkpoint_path)
            raise


async def _fetch_city_images(
    city_name: str,
    bbox: tuple[float, float, float, float],
    access_token: str,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    checkpoint_variant: str | None = None,
    census_cache: CensusCache | None = None,
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
        checkpoint_path: directory to resume from and commit into, or None
            for the historical fetch-everything behaviour. Built by the
            caller, because only the caller knows the channel — see
            :func:`checkpointing.checkpoint_path_for`.
        checkpoint_channel: the api_usage channel this census meters into,
            recorded in the commit record so a checkpoint cannot be resumed
            under a different one.
        checkpoint_variant: what separates two crawls of one city within that
            channel — a walk's ``--network-type``, None for a grid run. Also
            recorded, for the same reason.
        census_cache: the shared per-(provider, city, bbox) cache entry and how
            this caller may use it (issue #290), also caller-built — see
            :func:`checkpointing.crawl_store_for`. Given one, a COMPLETE census
            another consumer already paid for is reused here for zero requests
            (unless its ``reuse`` is False — the ``--refetch-census`` escape
            hatch — or the entry postdates its ``run_date``), and a census this
            call completes is PROMOTED into it on the way out, refetch included.
            None keeps the historical behaviour exactly.

    Returns:
        Dict with ``census`` (the deduped columnar census — see
        :func:`records_to_census`), ``api_requests`` (tiles fetched BY THIS
        CALL, which is what the additive daily ledger wants), ``tiles``
        (tile count), ``raw_feature_count`` (pre-dedupe),
        ``api_requests_total`` (the whole census's spend across resumes,
        which is what the catalog row wants) and ``checkpoint_path``.

        Plus the census's PROVENANCE: ``census_fetched_by`` (the channel whose
        credential and ledger paid for it), ``census_fetched_at`` (when the
        crawl's first tile was committed, i.e. when Mapillary was actually
        observed) and ``census_reused`` (True only when this call read the
        cache rather than the network). The first two land in the catalog's
        ``census_fetched_by``/``census_fetched_at``, which are what make an
        ``api_requests`` of 0 on a fully collected city explicable.

        ``checkpoint_path`` is None after a successful promotion: the directory
        has MOVED, so the caller's ``discard_checkpoint`` must not chase it.

    Raises:
        DownloadError: on a rejected token or tile transport failure, carrying
            ``api_requests`` so the caller can still record what it spent.
    """
    tiles = tiles_for_bbox(*bbox)
    logger.info(
        f"Fetching Mapillary metadata for {city_name}: {len(tiles)} z{TILE_ZOOM} "
        f"tiles covering bbox {tuple(round(v, 4) for v in bbox)}"
    )

    # THE CACHE IS CONSULTED BEFORE THE CHECKPOINT IS OPENED (issue #290). The
    # two answer different questions and the order follows from that: a
    # checkpoint is THIS crawl's unfinished work, a cache entry is a COMPLETE
    # observation somebody already paid for over the identical lattice. If a
    # usable entry exists there is nothing left to fetch, so opening a
    # checkpoint first would create a directory, resume a partial crawl and
    # re-request tiles the answer for is already on disk.
    #
    # A hit is then RECONCILED with whatever sits at checkpoint_path
    # (reconcile_cache_hit): a checkpoint newer than the entry is resumed
    # instead, an older one is discarded as superseded, and an entry this very
    # crawl paid for that still has failed tiles is handed back to the
    # checkpoint so the resume below re-probes them.
    #
    # This whole block runs inside the host lock (fetch_city_images_async), so no
    # second process on this machine can be promoting into, or reading, the
    # entry being read here.
    if census_cache is not None and census_cache.reuse:
        cached = load_cached_census(
            census_cache.path, bbox=bbox, tiles=tiles, run_date=census_cache.run_date
        )
        if cached is not None and reconcile_cache_hit(
            cached[1],
            cache_path=census_cache.path,
            checkpoint_path=checkpoint_path,
            channel=checkpoint_channel,
            variant=checkpoint_variant,
        ):
            return _reuse_cached_census(
                cached,
                city_name=city_name,
                tiles=tiles,
                checkpoint_channel=checkpoint_channel,
                checkpoint_variant=checkpoint_variant,
            )

    checkpoint = _open_tile_checkpoint(
        checkpoint_path,
        bbox=bbox,
        tiles=tiles,
        channel=checkpoint_channel,
        variant=checkpoint_variant,
    )
    done = checkpoint.done if checkpoint else {}
    todo = [tile for tile in tiles if tile not in done]
    if done and todo:
        logger.warning(
            f"Resuming {city_name} from the checkpoint at {checkpoint.path}: "
            f"{len(done)}/{len(tiles)} tiles already fetched for "
            f"{checkpoint.api_requests_before:,} requests; {len(todo)} to go"
        )
    elif done and not todo:
        # Recovers the crash-after-fetch-before-catalog case for ~0 requests.
        # Loud, because the other way to arrive here is a checkpoint the caller
        # forgot to discard, and then a zero-request 'collection' would look
        # like a real one.
        logger.warning(
            f"The checkpoint at {checkpoint.path} is COMPLETE: all {len(tiles)} tiles "
            f"were fetched by an earlier invocation, so this one issues ZERO requests "
            f"and re-finalizes from disk. If that is not what you meant, remove the "
            f"directory and re-run."
        )

    api_requests = 0
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    semaphore = asyncio.Semaphore(connection_limit)
    # Bounds the aggregate rate regardless of connection_limit — the semaphore
    # caps concurrency, which on a fast link still meant ~5 tiles/s (~300/min)
    # from a single city before this (issue #198).
    rate_limiter = AsyncRateLimiter(max_requests_per_minute, jitter=jitter)
    if max_requests_per_minute <= 0:
        logger.info("Tile pacing DISABLED (max_requests_per_minute <= 0)")
    elif jitter > 0:
        mean_gap_s = 60.0 / max_requests_per_minute
        logger.info(
            f"Pacing tile requests at a mean {max_requests_per_minute}/min, jittered "
            f"\u00b1{jitter:.0%} (gaps {mean_gap_s * (1 - jitter):.2f}\u2013"
            f"{mean_gap_s * (1 + jitter):.2f} s, issue #292)"
        )
    else:
        logger.info(f"Pacing tile requests at {max_requests_per_minute}/min")
    progress_bar = progress(
        total=len(todo),
        desc=f"Downloading Mapillary tiles for {city_name}",
        unit="tile",
        # Paced at 60 tiles/min (issue #198), so a large city is tens of minutes
        # of deliberately slow fetching under the scheduler's redirected log.
        logger=logger,
    )

    def count_request() -> None:
        nonlocal api_requests
        api_requests += 1

    def interrupted(err: DownloadError) -> DownloadError:
        """
        Stamp an interrupted census's two counters and persist what it spent.

        FIVE PATHS LEAVE THIS FUNCTION WITHOUT A CENSUS — a session-level error,
        a transport failure, the #205 abort, a per-tile DownloadError that
        reached the settle loop, and the over-tolerance refusal — and every one
        of them owes the caller the same three things. Written once because the
        last time they were five copies, one of them was silently unreachable
        and still got extended; the next edit to what an interruption records
        would have had five places to reach and no way to notice a miss.

        The counters are deliberately different numbers: `api_requests` is THIS
        process's spend, for the additive (date, provider) ledger, and the total
        is the whole crawl's, for the operator and the catalog row.
        """
        total = _census_requests_total(checkpoint, api_requests)
        err.api_requests = api_requests
        err.api_requests_total = total
        _commit_spend(checkpoint, bbox=bbox, tiles=tiles, api_requests_total=total)
        return err

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
        frame = records_to_census(decode_image_features(tile_bytes, x, y))
        if checkpoint is not None:
            # Synchronous, inside the coroutine: the loop is single-threaded, so
            # this is atomic with respect to every other tile's commit, and the
            # host lock rules out another process. Only a SUCCESSFUL tile gets
            # here — a failure raised above, and stays refetchable.
            _commit_tile(
                checkpoint,
                x,
                y,
                frame,
                bbox=bbox,
                tiles=tiles,
                api_requests_total=checkpoint.api_requests_before + api_requests,
            )
        return frame

    try:
        # Token rides in each tile URL as ?access_token= — see TILE_URL_TEMPLATE
        # comment (the tiles CDN 403s the Authorization header).
        #
        # return_exceptions: one bad tile out of hundreds used to discard the
        # whole city, for both the grid run and the road walk (issue #168). A
        # transient 404 is worth one tile, not a city.
        async with aiohttp.ClientSession() as session:
            settled = await asyncio.gather(
                *(fetch_one(x, y) for x, y in todo), return_exceptions=True
            )
    except DownloadError as e:
        # e.g. the rejected-token error from _fetch_tile; attach the spent
        # request count so the caller can still record it in the ledger. The
        # ledger wants THIS call's spend (it is additive and keyed by date), so
        # api_requests is the one that must never become the cumulative figure;
        # the total rides along for the operator and for parity with the
        # success path.
        # Called for its side effects, then a BARE re-raise: it is the same
        # exception object, and `raise ... from e` would chain it to itself.
        interrupted(e)
        raise
    except (TimeoutError, aiohttp.ClientError) as e:
        error = DownloadError(f"Mapillary tile download failed: {redact_credentials(e)}")
        raise interrupted(error) from e
    finally:
        progress_bar.close()

    # THIS BLOCK WINS, AND THE LOOP'S DownloadError ARM BELOW IS THE ONE THAT IS
    # DEAD TODAY — the reverse of what this comment used to claim. `fetch_one`
    # assigns `fatal` for every DownloadError before it re-raises, so such an
    # error is always ALSO in `settled`; raising it here rather than there is
    # what reports the error that actually caused the abort, instead of
    # whichever DownloadError happens to sit earliest in tile order.
    #
    # Both are kept, and what the pair guards is a future edit that makes
    # `fetch_one` swallow or wrap that error — then every aborted tile becomes
    # an empty SUCCESS (they return an empty census, not an exception),
    # `failed_tiles` is empty, `detect_systemic_failure` doesn't reject (it only
    # looks for REQUEST_DENIED/OVER_QUERY_LIMIT), and a 0-pano census registers,
    # publishes and diffs as "every pano in the city removed" — against an
    # immutable dated snapshot.
    if fatal is not None:
        raise interrupted(fatal)

    fetched: dict[tuple[int, int], pd.DataFrame] = {}
    failed_tiles: list[tuple[int, int]] = []
    first_error: BaseException | None = None
    # `todo`, not `tiles`: a resumed run only attempted the missing ones, and a
    # tile already in the checkpoint is a success that happened on an earlier
    # night. `strict=True` is what keeps that from drifting.
    for (x, y), outcome in zip(todo, settled, strict=True):
        if isinstance(outcome, BaseException):
            # A bad token is a whole-city condition, not a per-tile one: every
            # remaining tile would fail the same way, so don't dress it up as
            # partial coverage.
            # Unreachable on current code (the `fatal` block above raises
            # first); kept as the belt to that braces. See its comment.
            if isinstance(outcome, DownloadError):
                raise interrupted(outcome)
            failed_tiles.append((x, y))
            first_error = first_error or outcome
        else:
            fetched[(x, y)] = outcome

    if failed_tiles:
        # Denominator is the FULL tile set, not this invocation's share: the
        # tolerance asks what fraction of the city is unmeasured, and a tile a
        # previous night already fetched is measured.
        failed_fraction = len(failed_tiles) / len(tiles)
        detail = f"{len(failed_tiles)}/{len(tiles)} tiles failed: {redact_credentials(first_error)}"
        if failed_fraction > MAX_FAILED_TILE_FRACTION:
            error = DownloadError(
                f"Mapillary tile download failed: {detail} "
                f"({failed_fraction:.1%} > {MAX_FAILED_TILE_FRACTION:.0%} tolerated); "
                f"refusing to finalize an incomplete snapshot"
            )
            raise interrupted(error) from first_error
        # Under the threshold the run continues, but the caller must mark the
        # affected grid points REQUEST_FAILED rather than let them look like
        # genuine no-imagery — see download_mapillary_metadata_async.
        logger.warning(f"Continuing with {detail}; affected grid points marked REQUEST_FAILED")

    # REASSEMBLE IN TILE ORDER, taking each tile from this run if it was fetched
    # now and from its part file otherwise. This is the whole byte-identity
    # mechanism: `gather` preserves argument order, so an uninterrupted run
    # produces exactly this sequence, and concat + dedupe therefore see
    # positionally identical input however the work was split across nights.
    # See the tile-checkpoint section above.
    results = []
    for tile in tiles:
        if tile in fetched:
            # pop, so the only surviving reference is the one in `results` --
            # otherwise this loop would double the resident census (issue #157).
            results.append(fetched.pop(tile))
        elif tile in done:
            results.append(_checkpoint_frame_for_tile(checkpoint, *tile))
        # else: it failed this run and no earlier one committed it. Already in
        # failed_tiles, and the caller marks its points REQUEST_FAILED.

    raw_feature_count = sum(len(r) for r in results)
    census = concat_census(results)
    # ALL THREE names have to go before the dedup copy: `settled`, `fetched` and
    # `results` hold references to the same per-tile frames, so dropping fewer
    # frees nothing and leaves a third full census resident through
    # dedupe_census.
    del results, settled, fetched
    census = dedupe_census(census)

    # PROMOTION IS THE LAST STATEMENT BEFORE THE RETURN, and that placement is
    # the safety argument (issue #290). Everything above it can raise -- the
    # fatal abort, the per-tile tolerance, the reassembly, concat, dedupe -- and
    # a raise must leave the checkpoint exactly where the caller expects to
    # resume from. Here, nothing between the move and the return can fail, so a
    # moved directory and a propagating exception cannot coexist.
    #
    # `not degraded` is the completeness guarantee. A checkpoint whose commits
    # latched off is missing tiles it never recorded, so `done` no longer
    # describes what is on disk -- and an incomplete entry reused as a census
    # would publish absence that was never observed. `created_at is None` is the
    # same condition from the other side: nothing was ever committed.
    #
    # It happens under the host lock, so no other process can be reading the
    # entry while `os.replace` swaps it.
    promoted = False
    total = _census_requests_total(checkpoint, api_requests)
    if (
        census_cache is not None
        and checkpoint is not None
        and not checkpoint.degraded
        and checkpoint.created_at is not None
    ):
        promoted = promote_checkpoint_to_cache(
            checkpoint.path,
            census_cache.path,
            census_cache_marker(
                "mapillary",
                # RECORDED, never keyed: the entry is reusable by any channel,
                # and this is what lets the catalog say who actually paid.
                fetched_by=checkpoint.channel,
                fetched_variant=checkpoint.variant,
                crawl_started_at=checkpoint.created_at,
                api_requests_total=total,
                # Inherited verbatim by a cross-channel reuser; the crawl's own
                # channel re-probes them instead (reconcile_cache_hit).
                failed=[[int(x), int(y)] for x, y in failed_tiles],
            ),
        )

    return {
        "census": census,
        # THIS CALL's spend, for the additive (date, provider) ledger. A
        # resumed night that reported the whole census here would charge the
        # earlier night's tiles against today's budget gate (issue #239 got
        # this backwards once).
        "api_requests": api_requests,
        # The whole census's spend across every invocation, for the catalog
        # row, which describes the collection rather than the process.
        "api_requests_total": total,
        # None after a promotion: the directory MOVED into the cache, so the
        # caller's discard_checkpoint would be chasing a path that is gone (a
        # no-op, but a misleading one -- and a future caller that read it as
        # "still mine" could delete the shared entry).
        "checkpoint_path": None if promoted else (checkpoint.path if checkpoint else None),
        "tiles": len(tiles),
        "raw_feature_count": raw_feature_count,
        # Summarized HERE, where the census is already in hand: the caller may
        # not bind it to a local (write_census_grid_run pops and releases it),
        # so counting there would mean a second full pass over the column --
        # 19M rows at Detroit, on the path whose whole justification is #157's
        # memory and time budget.
        "num_images": len(census),
        "num_panos": int(census_core.census_is_pano(census).sum()),
        # (x, y) of tiles that never came back. Empty on a clean run.
        "failed_tiles": failed_tiles,
        # Provenance for the catalog. A fresh fetch names ITS OWN channel and
        # the instant its first tile was committed -- so a later reuser and the
        # fetcher agree on when Mapillary was observed, which is the whole point
        # of storing it rather than deriving it from the run date.
        "census_fetched_by": checkpoint_channel,
        "census_fetched_at": checkpoint.created_at if checkpoint else None,
        "census_reused": False,
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
    jitter: float = DEFAULT_TILE_JITTER,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    census_cache: CensusCache | None = None,
) -> dict[str, Any]:
    """
    Fetch Mapillary pano metadata for a city and write it as a run csv.gz.

    Same calling convention as download_gsv_metadata_async: the caller
    decides the output filename (skip policy and dated naming live in the
    CLI/scheduler layer, not here).

    The checkpoint is the caller's to DISCARD, once the run row is committed.
    Nothing here removes it: this function returns after writing the CSV, and
    the caller still has stats, the runs row, the JSON and the diff to do — a
    delete issued here would guarantee that a crash in that tail costs the
    whole census again, which is one of the interruptions #256 exists to cover.

    Returns:
        Dict with:
            df: DataFrame containing the metadata (METADATA_DTYPES schema)
            filename_with_path: the written .csv.gz path
            api_requests: number of tile requests issued this call
            api_requests_total: the census's spend across every invocation
            checkpoint_path: the checkpoint to discard, or None (None too when
                the census was promoted into the shared cache, issue #290)
            census_fetched_by / census_fetched_at: which channel paid for this
                census and when it observed Mapillary, for the ``runs`` row
            started_at / finished_at: UTC ISO 8601 timestamps
    """
    started_at = datetime.now(UTC).isoformat()

    # Checked before a single tile is fetched, though write_census_grid_run
    # re-checks as it takes ownership of the write: one implementation, called
    # at the point where failing is free.
    census_core.prepare_output_path(output_csv_gz_path)

    # Built before the fetch (its bbox bounds the tile set) and consumed after
    # it, so it is derived once and threaded through -- see census_core.build_grid.
    grid = census_core.build_grid(center_lat, center_lon, grid_width, grid_height, step_length)

    fetched = await fetch_city_images_async(
        city_name,
        grid.bbox,
        access_token,
        connection_limit=connection_limit,
        request_timeout=request_timeout,
        max_requests_per_minute=max_requests_per_minute,
        jitter=jitter,
        checkpoint_path=checkpoint_path,
        checkpoint_channel=checkpoint_channel,
        census_cache=census_cache,
    )
    # A reused census is stamped with when the provider was observed, a fresh
    # one with this process's clock; see checkpointing.observation_timestamp.
    query_timestamp = observation_timestamp(fetched, started_at)
    api_requests = fetched["api_requests"]
    api_requests_total = fetched["api_requests_total"]
    failed_tiles = fetched.get("failed_tiles") or []
    # Counted by the fetch, not recomputed here: binding the census to a local
    # would pin the whole thing (19M rows at Detroit) alive through both CSV
    # writes, defeating the tail's release, and re-reading it through the dict
    # would cost a second full pass for a log line (issue #157).
    num_images = fetched["num_images"]
    num_panos = fetched["num_panos"]
    logger.info(
        f"Decoded {fetched['raw_feature_count']} features "
        f"({num_images} unique: {num_panos} panos, {num_images - num_panos} flat) "
        f"from {fetched['tiles']} tiles"
    )

    # The tail is wrapped because the checkpoint changes what a crash HERE
    # costs. Without one, a failure after the fetch loses the spend with the
    # process and the caller records what the exception carries. With one, the
    # next invocation re-finalizes from disk for ~0 requests — so a tail failure
    # that carried no spend would land this census's tiles in no ledger, ever
    # (the same gap PR #251 closed for KartaView).
    try:
        written = census_core.write_census_grid_run(
            fetched,
            grid,
            output_csv_gz_path,
            query_timestamp,
            capture_dates_for=_mapillary_capture_dates,
            image_columns=_mapillary_image_columns,
            dtypes=MAPILLARY_METADATA_DTYPES,
            # A tile that never downloaded leaves its grid points UNKNOWN rather
            # than empty (issue #168); a clean fetch passes None and pays nothing.
            unmeasured_mask=(
                (lambda lats, lons: _points_in_tiles(lats, lons, failed_tiles))
                if failed_tiles
                else None
            ),
            unmeasured_desc=f"{len(failed_tiles)} undownloaded tile(s)",
        )
    except BaseException as e:
        e.api_requests = api_requests
        e.api_requests_total = api_requests_total
        raise

    return {
        "df": written["df"],
        "filename_with_path": output_csv_gz_path,
        "api_requests": api_requests,
        "api_requests_total": api_requests_total,
        "checkpoint_path": fetched.get("checkpoint_path"),
        "census_fetched_by": fetched.get("census_fetched_by"),
        "census_fetched_at": fetched.get("census_fetched_at"),
        "census_reused": bool(fetched.get("census_reused")),
        # Census magnitude of flat imagery (issue #116): every in-grid flat
        # image, including those at points that also hold a pano. Not
        # reconstructable from the CSV (flat-only points collapse to one
        # FLAT_ONLY row), so it is threaded to the catalog separately.
        "num_flat_images": written["num_flat_images"],
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }
