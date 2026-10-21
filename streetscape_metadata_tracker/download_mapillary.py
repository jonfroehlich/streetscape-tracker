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
for a refusal, because retrying during a block is reported to extend it
(docs/provider-access.md). The one in-process retry that exists is for a
429/503, and it waits first -- see the backpressure section (issue #444).

The caller supplies the checkpoint path and discards it once its artifact is
durable -- see checkpointing.py, and the tile-keyed reassembly contract in
the checkpoint section below.
"""

import asyncio
import email.utils
import json
import logging
import math
import os
import time
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
from . import clock

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
    SWEEP_UNIT_TILES,
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
    MAPILLARY_TILE_CONNECTION_LIMIT,
    SWEEP_STOP_BACKPRESSURE,
    SWEEP_STOP_CLOCK,
    SWEEP_STOP_PHRASES,
    SWEEP_STOP_REQUESTS,
    AsyncRateLimiter,
    DownloadError,
    HostBlockedError,
    HostThrottledError,
    SweepIncompleteError,
    crawl_budget_seconds,
    crawl_ceiling_needs_checkpoint,
    crawl_deadline_passed,
    redact_credentials,
    streetscape_user_agent,
)
from .download_common import assign_to_grid as assign_to_grid
from .download_common import grid_bbox as grid_bbox
from .download_common import lonlat_to_tile_frac as lonlat_to_tile_frac
from .download_common import points_in_tiles as common_points_in_tiles
from .download_common import tile_frac_to_lonlat as tile_frac_to_lonlat
from .download_common import tiles_for_bbox as common_tiles_for_bbox
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

# Who we are, on every tile request (issue #444, stage 4). Until #444 the census
# sent aiohttp's stock `Python/3.x aiohttp/3.x` -- the agent every scraper
# sends -- so the only thing tiles.mapillary.com could do with traffic it
# disliked was refuse the IP. Neither Mapillary's API documentation nor any
# forum.mapillary.com thread found (2026-10-17) asks for a User-Agent or says
# one is read, so this is a courtesy whose effect is UNKNOWN, not a fix: what it
# buys is that anyone at Mapillary reading their logs can write to a person.
#
# The SAME string the Panoramax collector sends, from the one builder
# (download_common.streetscape_user_agent): one project, one identity, and the
# contact address Jon chose in #405 rather than a second hand-written copy.
MAPILLARY_USER_AGENT = streetscape_user_agent(f"aiohttp/{aiohttp.__version__}")


# ── Slippy-map tile math (stdlib only) ─────────────────────────────────────


def tiles_for_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, zoom: int = TILE_ZOOM
) -> list[tuple[int, int]]:
    """
    All (x, y) z14 tile indices intersecting the bbox — Mapillary's default zoom
    over the shared :func:`download_common.tiles_for_bbox`.

    A one-line wrapper rather than a re-export, because the shared function
    takes the zoom as a REQUIRED argument (a lattice helper serving two
    providers at two zooms has no default to be right about) while ~20 call
    sites here — the collector, the road walk, the tests, the golden fixture —
    call this one with four arguments and mean z14.
    """
    return common_tiles_for_bbox(min_lon, min_lat, max_lon, max_lat, zoom)


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
    Mapillary's ``unmeasured_mask``: which grid points sit under a tile that
    never downloaded (issue #168). The shared
    :func:`download_common.points_in_tiles` at this provider's default zoom, for
    the same reason :func:`tiles_for_bbox` above is a wrapper.
    """
    return common_points_in_tiles(lats, lons, tiles, zoom)


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
#
# PUBLIC because the scheduler prices its launch floor from it (#318):
# a request cap that cannot fund ONE tile's worst-case attempt buys no
# committed progress, and spends a night plus a checkpoint-age day to
# record none. Read there rather than pinned at 5, so retuning the retry
# budget carries the floor with it -- the same rule KartaView's radius
# ladder already follows.
TILE_MAX_TRIES = 5
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

# Jitter fraction for the tile pacer (issue #292). Each request gap is a
# shifted exponential around the mean 60/DEFAULT_TILE_REQUESTS_PER_MINUTE: a
# floor of (1 - this) x the mean plus an exponential tail scaled to this, so the
# mean gap is unchanged and the coefficient of variation IS this number.
#
# Non-zero BY DEFAULT: a saturated token bucket emits tiles at an exact 1.000 s
# cadence over sequential z14 tiles from a datacenter IP, and after three per-IP
# blocks that regularity is the one property of our traffic never changed
# between restarts (rate and daily volume were both falsified, #286).
#
# 0.6 rather than something smaller because the point is to approach the CV of
# an organic client's Poisson arrivals (1.0), not merely to break gap EQUALITY:
# a scorer reading gap statistics is unmoved by a narrow, hard-bounded wobble.
# 0 restores the metronome.
DEFAULT_TILE_JITTER = 0.6

# Content types that are an error page rather than a tile (issue #199). A
# DENY-list, not an allow-list of protobuf types: if Mapillary ever relabels
# real tiles (say `application/vnd.mapbox-vector-tile`), an allow-list would
# reject every tile and halt all collection, whereas this only ever misreads a
# genuine tile if one is served as HTML or JSON.
_TILE_ERROR_CONTENT_TYPES = ("text/html", "application/json")


# ── Per-status response counts (issue #444, stage 2) ───────────────────────
#
# Until #444 `_fetch_tile` counted requests and nothing else, so whether the
# tile CDN ever answers 429 or 5xx BEFORE it starts redirecting to /login --
# the hypothesis that our retries walk us into the block -- was unobservable.
# Every HTTP response a crawl receives is now tallied by class and logged as
# ONE line per crawl, in a fixed key order so a log grep can read it
# (illustrative numbers):
#
#     mapillary tile statuses: 200=81 204=0 404=1 429=0 4xx=0 5xx=0 3xx=5
#     other=0 noresp=0 requests=87 retries=1 retry_after=[] city='Carlsbad, CA'
#
# (one line in the log; wrapped here). `3xx` is every redirect -- in practice
# the 302 -> /login refusal; `4xx` is every 4xx EXCEPT 404 and 429, which have
# their own keys; `noresp` is requests that got no HTTP status at all (a
# timeout or a reset connection), derived as `requests` minus the statuses so
# the two can never disagree; `retries` is attempts beyond a tile's first,
# whichever layer retried it. `retry_after` lists every Retry-After header
# value seen, verbatim, capped at TILE_RETRY_AFTER_LOG_CAP entries.

# The key order of the status line, and the classes `tile_status_class` returns.
TILE_STATUS_CLASSES = ("200", "204", "404", "429", "4xx", "5xx", "3xx", "other")
TILE_STATUS_LINE_PREFIX = "mapillary tile statuses:"
# Retry-After values kept for the status line. A storm that sent one per request
# would otherwise make the line as long as the crawl; the count is still exact.
TILE_RETRY_AFTER_LOG_CAP = 20


def tile_status_class(status: int) -> str:
    """
    The status-line key one HTTP status is counted under.

    >>> [tile_status_class(s) for s in (200, 302, 403, 404, 429, 503, 101)]
    ['200', '3xx', '4xx', '404', '429', '5xx', 'other']
    """
    if status in (200, 204, 404, 429):
        return str(status)
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


@dataclass
class TileStatusCounts:
    """One crawl's response tally; see the section comment above for the line."""

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TILE_STATUS_CLASSES, 0))
    retries: int = 0
    retry_after: list[str] = field(default_factory=list)
    retry_after_seen: int = 0

    def record(self, status: int, retry_after: str | None) -> None:
        """Count one HTTP response, and keep its Retry-After if it sent one."""
        self.counts[tile_status_class(status)] += 1
        if retry_after is not None:
            self.retry_after_seen += 1
            if len(self.retry_after) < TILE_RETRY_AFTER_LOG_CAP:
                # Bounded per value too: this is provider text going into a log.
                self.retry_after.append(redact_credentials(retry_after)[:64])

    def summary_line(self, city_name: str, requests: int) -> str:
        """The greppable per-crawl line. ``requests`` is THIS process's spend."""
        statuses = " ".join(f"{key}={self.counts[key]}" for key in TILE_STATUS_CLASSES)
        noresp = max(requests - sum(self.counts.values()), 0)
        retry_after = ",".join(self.retry_after)
        if self.retry_after_seen > len(self.retry_after):
            retry_after += f",+{self.retry_after_seen - len(self.retry_after)} more"
        return (
            f"{TILE_STATUS_LINE_PREFIX} {statuses} noresp={noresp} requests={requests} "
            f"retries={self.retries} retry_after=[{retry_after}] city={city_name!r}"
        )


# ── Backpressure on 429/503 (issue #444, stage 4) ──────────────────────────
#
# Until #444 a 429 or 503 was an ordinary ClientResponseError, which `@backoff`
# retried up to TILE_MAX_TRIES times on a one-to-a-few-second exponential, at
# whatever pace the crawl was running, ignoring any Retry-After -- i.e. it
# answered "slow down" by asking again almost at once, from the host whose
# forum reports say retrying INTO a block extends it. The rule now:
#
# 1. A 429 or 503 is NOT retried by `@backoff`. `_fetch_tile` raises
#    TileBackpressureError, which is deliberately neither an aiohttp.ClientError
#    (backoff's retry set) nor a DownloadError (#205's whole-city abort).
# 2. The crawl's pacer is HELD -- no tile request goes out, from any socket --
#    for `max(Retry-After, TILE_BACKPRESSURE_MIN_WAIT_S)`. A Retry-After longer
#    than TILE_BACKPRESSURE_MAX_WAIT_S is not slept on at all: the crawl
#    pauses instead (3), because retrying before the time the server named
#    would disobey it, and a sleep that long belongs to the next night rather
#    than to this one's host lock. Nor is a hold that would end past the
#    crawl's wall-clock budget (`deadline_monotonic`), which the scheduler sets
#    600 s before its SIGKILL: sleeping into the kill would count a failure.
#    Below the strike limit that crawl stops as the CLOCK's pause (exit 83),
#    as if its budget had run out now -- one 429 near the end of a crawl says
#    no more about the host than one anywhere else (round-3 review of #444).
# 3. The pace is halved for the REST OF THIS CRAWL (once, on the first such
#    response). The limiter is built per crawl, so this cannot reach another
#    crawl or another process; see AsyncRateLimiter.slow_down.
# 4. The tile is then retried, through the same in-semaphore cap and deadline
#    checks as a fresh tile, so `max_requests` + its reservation still bound
#    the crawl and no attempt is counted twice.
# 5. The TILE_BACKPRESSURE_PAUSE_AFTER-th standing strike in one crawl
#    stops it on the request cap's path -- turned-away tiles uncommitted, the
#    checkpoint kept, raised before the settle loop -- and the crawl's pacer is
#    closed so no request still waiting for its turn goes out. It raises
#    HostThrottledError (exit 87), committed tiles or not, so the scheduler
#    skips this host for the rest of the night without counting a failure. It
#    is NOT a refusal: a 302 -> /login is still the HostBlockedError (exit 75)
#    it always was, and wins if both happen in one crawl.
# 6. One tile is given up as an ordinary failed tile after
#    TILE_BACKPRESSURE_TILE_TRIES backpressure responses, so a tile stuck on
#    503 cannot pause its city night after night. Only a tile's FIRST response
#    can be a strike, and a strike stops counting once another tile is served
#    from a later pacer generation unless its own retry was served (rounds 2
#    and 3 of the review of #444), so neither can several -- except three in a
#    row in request order, which nothing in one crawl tells from a storm. The
#    checkpoint is the evidence from outside the crawl: every tile refused and
#    not since served is recorded (`TileCheckpoint.stuck`), and a resume asks
#    for those LAST, charging no strike (round 4). Without it a resume, whose
#    `todo` has lost the served tiles between them, lined up stuck tiles from
#    anywhere in a capped night as three in a row, and paused every night.
#    Left: three stuck tiles that stop a crawl before it has served a single
#    tile, since a crawl that commits nothing has no checkpoint to record in.
#
# Both statuses, because a CDN shedding load answers 503 and one metering a
# client answers 429; neither has ever been observed from tiles.mapillary.com
# (nothing counted them before the status line above), so this is built ahead
# of the evidence, on the cheap side of an unknown.
TILE_BACKPRESSURE_STATUSES = (429, 503)
# The redirects `_fetch_tile` reads as the per-IP block (302 -> /login), named so
# `host_probe.classify_mapillary` reads the same set: 304 Not Modified is not
# one, so a probe must never call it REFUSED.
TILE_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
TILE_BACKPRESSURE_MIN_WAIT_S = 60.0
# 15 minutes. Two reasons, one external and one ours. The only cooldown ever
# reported for a Mapillary tile block is "the script sleeps for 15 minutes",
# found by trial and error by a forum user (thread 5821) -- a hint, not a
# contract. And a crawl holds in at most PAUSE_AFTER - 1 = 2 episodes before
# the third pauses it, each lasting the longest wait asked inside it, so it
# holds the machine-wide tile lock for about 30 minutes of sleeping -- not
# exactly, since a same-episode response can extend a hold by up to one request
# timeout -- far inside prod's 180-minute per-city timeout floor. A hold that would run past
# the crawl's own deadline is not slept at all (see on_backpressure).
TILE_BACKPRESSURE_MAX_WAIT_S = 900.0
# Counted in EPISODES, not responses (review of #444): a 429/503 answering a
# request that was sent before the latest hold began belongs to that hold's
# episode -- one throttle window can answer several in-flight requests at once
# -- so it adds no strike. It may still EXTEND the hold, to its own wait.
# Nor is a tile's own repeated response a strike, only its first (round 2).
# And a strike whose tile is still retrying or was given up as stuck stops
# COUNTING as soon as another tile is served from a request sent after it, in
# a later pacer generation, because a host serving other tiles is not the one
# refusing (round 3; see `strikes` in _fetch_city_images). A strike whose own
# retry is served is a CONFIRMED throttle and always counts. A storm serves
# nothing between its refusals, so it still pauses at its third episode -- and
# so, by the same token, do three stuck tiles in a row in request order, which
# no rule that reads one crawl can tell from a storm. The checkpoint's record
# of them (round 4, rule 6 above) is what lets the NEXT night tell.
TILE_BACKPRESSURE_PAUSE_AFTER = 3
# Backpressure responses ONE TILE may draw before it is given up as an ordinary
# failed tile -- REQUEST_FAILED under MAX_FAILED_TILE_FRACTION (#168), exactly
# what `@backoff` exhausting it meant before #444. Strictly below
# PAUSE_AFTER, so a single tile that always answers 503 can never pause its
# crawl on its own: without this it spent all three strikes, the crawl paused
# (exit 83, amnestied without bound), and the next night hit the same tile
# first, so the city never completed. Two such tiles did the same until round
# 2 of the review counted strikes on first responses only.
TILE_BACKPRESSURE_TILE_TRIES = 2
TILE_BACKPRESSURE_SLOWDOWN = 2.0

# The hold's sleep, looked up at CALL time so a test can replace it (conftest
# makes it instant suite-wide; tests/test_mapillary_backpressure.py records it).
_backpressure_sleep = asyncio.sleep


class TileBackpressureError(Exception):
    """
    The tile CDN answered 429 or 503: slow down, then retry (issue #444).

    Raised by :func:`_fetch_tile` and handled by the crawl, never by
    ``@backoff``: it is deliberately NOT an ``aiohttp.ClientError`` (which
    backoff would retry at once) and NOT a ``DownloadError`` (which would trip
    #205's whole-city abort -- this is a request to wait, not a refusal).
    """

    def __init__(self, status: int, retry_after: str | None):
        super().__init__(f"Mapillary tile CDN answered HTTP {status} (Retry-After: {retry_after})")
        self.status = status
        self.retry_after = retry_after


# A Retry-After is clamped to this before any arithmetic. Far past
# TILE_BACKPRESSURE_MAX_WAIT_S, so a clamped value still reads as "longer than
# we will wait" and pauses the crawl; it only keeps the number finite.
_RETRY_AFTER_CAP_S = 10**9


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """
    A Retry-After header as seconds from now, or None when absent or unreadable.

    Both forms RFC 9110 sec. 10.2.3 allows: delay-seconds (a non-negative
    integer) and an HTTP-date. A date in the past is 0, not negative.

    >>> retry_after_seconds("120")
    120.0
    >>> retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT",
    ...                     now=datetime(2015, 10, 21, 7, 27, tzinfo=UTC))
    60.0
    >>> retry_after_seconds("soon") is None
    True
    """
    if value is None:
        return None
    text = value.strip()
    # ASCII digits only: str.isdigit() also accepts "²" and other Unicode
    # digits that int() refuses. A value this function cannot read is None,
    # never an exception -- the caller then waits the 60 s floor.
    if text.isascii() and text.isdigit():
        # Compared as a STRING length first: int() refuses more than 4,300
        # digits (ValueError) and float() overflows past ~308. Anything longer
        # than the cap's own digits is past the ceiling anyway.
        if len(text) > len(str(_RETRY_AFTER_CAP_S)):
            return float(_RETRY_AFTER_CAP_S)
        return float(min(int(text), _RETRY_AFTER_CAP_S))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:  # pragma: no cover - older Pythons returned None
        return None
    if when.tzinfo is None:
        # RFC 9110: an HTTP-date is always GMT.
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return max((when - now).total_seconds(), 0.0)


def backpressure_wait_seconds(retry_after_s: float | None) -> float | None:
    """
    How long to hold the crawl after one 429/503, or None to pause it instead.

    ``max(Retry-After, TILE_BACKPRESSURE_MIN_WAIT_S)``; None when the server
    asked for longer than TILE_BACKPRESSURE_MAX_WAIT_S (see rule 2 above).

    >>> backpressure_wait_seconds(None), backpressure_wait_seconds(5.0)
    (60.0, 60.0)
    >>> backpressure_wait_seconds(300.0), backpressure_wait_seconds(3600.0)
    (300.0, None)
    """
    if retry_after_s is not None and retry_after_s > TILE_BACKPRESSURE_MAX_WAIT_S:
        return None
    return max(retry_after_s or 0.0, TILE_BACKPRESSURE_MIN_WAIT_S)


class _CrawlPaused(Exception):
    """
    Raised by a closed :class:`_BackpressurePacer` instead of releasing a request.

    The crawl decided to pause while this request was waiting for its turn, so
    it must not go out (issue #444 review): the tile is turned away uncommitted,
    exactly as one the pause check refuses. Not an aiohttp.ClientError, so
    ``@backoff`` does not retry it, and raised BEFORE ``on_request``, so no
    request is counted for it -- none was sent.
    """


class _BackpressurePacer:
    """
    One crawl's pacer: the rate limiter, plus a gate a 429/503 can close.

    Built per crawl around that crawl's own limiter, so a hold or a slowdown
    lives exactly as long as the crawl does. ``acquire`` is what ``_fetch_tile``
    awaits before every request, so a hold stops EVERY socket, not just the one
    that was answered 429: with five connections, four other tiles would
    otherwise keep asking while the fifth waited.

    A request that was already queued on the limiter when a hold began is sent
    back to wait the hold out (the ``_holds`` generation check), so nothing
    slips out during a hold that was opened after it passed the gate.

    :meth:`close` is the pause's half of the same rule: once the crawl has
    decided to pause, every request still waiting here -- at the gate, or
    queued on the limiter -- raises :class:`_CrawlPaused` instead of going out.
    It also cuts short any hold in progress: nothing will be sent after it, so
    sleeping out the rest of a hold would only keep the host lock (a 302 seen
    mid-hold used to sleep up to 900 s before exiting 75).
    """

    def __init__(self, limiter: Any, clock: Callable[[], float] | None = None):
        """
        Args:
            limiter: this crawl's rate limiter; anything with ``acquire()``
                and ``slow_down(factor)``.
            clock: monotonic seconds, for the hold arithmetic. Defaults to the
                running loop's clock; injectable so a test can advance time
                with the hold's own sleep.
        """
        self._limiter = limiter
        self._clock = clock
        self._gate = asyncio.Lock()
        self._holds = 0
        self._hold_until = 0.0
        self.slowed = False
        self.closed = False
        # Set by close(); a hold in progress waits on it beside its sleep.
        self._closed_event = asyncio.Event()

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_running_loop().time()

    @property
    def generation(self) -> int:
        """Holds begun so far (plus one for a close). A request records it
        when it is sent; a 429 to a request from an older generation belongs
        to a hold that has already been opened."""
        return self._holds

    async def acquire(self) -> None:
        while True:
            if self.closed:
                raise _CrawlPaused()
            seen = self._holds
            async with self._gate:
                pass  # waits out any hold in progress
            await self._limiter.acquire()
            if self._holds == seen and not self.closed:
                return

    def close(self) -> None:
        """Release no further request from this crawl: it is pausing."""
        self.closed = True
        # A new generation, so a request mid-way through `acquire` loops back
        # and meets the closed check rather than returning.
        self._holds += 1
        self._closed_event.set()

    async def hold(self, seconds: float) -> None:
        """Keep every request of this crawl from going out for ``seconds``."""
        self._holds += 1
        # Overlapping holds extend to the LATEST end rather than queueing, so
        # two 429s seen together cost one wait, not two back to back -- and a
        # later, SHORTER hold can never cut short a longer one still queued.
        self._hold_until = max(self._hold_until, self._now() + seconds)
        async with self._gate:
            remaining = self._hold_until - self._now()
            if remaining > 0 and not self.closed:
                # The sleep races close(): whichever finishes first ends it,
                # and the other is cancelled (so is each on our own cancel).
                sleeper = asyncio.ensure_future(_backpressure_sleep(remaining))
                closer = asyncio.ensure_future(self._closed_event.wait())
                try:
                    await asyncio.wait({sleeper, closer}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    sleeper.cancel()
                    closer.cancel()

    def slow_down(self) -> None:
        """Halve this crawl's pace, once; later calls are no-ops."""
        if not self.slowed:
            self.slowed = True
            self._limiter.slow_down(TILE_BACKPRESSURE_SLOWDOWN)


@backoff.on_exception(
    backoff.expo,
    (asyncio.TimeoutError, aiohttp.ClientError),
    max_tries=TILE_MAX_TRIES,
    max_time=_TILE_MAX_TIME_S,
)
async def _fetch_tile(
    session: aiohttp.ClientSession,
    url: str,
    timeout: aiohttp.ClientTimeout,
    rate_limiter: AsyncRateLimiter | None = None,
    on_request: Callable[[], None] | None = None,
    on_response: Callable[[int, str | None], None] | None = None,
) -> bytes:
    # Pacing and counting sit INSIDE the retried body on purpose (issue #198).
    # This function may issue up to TILE_MAX_TRIES requests; taking one token
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
        # FIRST, before any branch below can raise: a refusal and a 429 are
        # exactly the responses the status line exists to count (issue #444).
        if on_response is not None:
            on_response(response.status, response.headers.get("Retry-After"))
        if response.status in (401, 403):
            # Deliberately NOT a HostBlockedError: a rejected token is scoped to
            # the CREDENTIAL, and our two Mapillary channels hold different
            # tokens. Typing it host-wide would let one channel's bad key stop
            # the other channel — which is working fine — for the whole night.
            raise DownloadError(
                f"Mapillary rejected the access token (HTTP {response.status}). "
                "Check MAPILLARY_ACCESS_TOKEN."
            )
        if response.status in TILE_REDIRECT_STATUSES:
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
        if response.status in TILE_BACKPRESSURE_STATUSES:
            # Handled by the crawl, never by @backoff (issue #444): see the
            # backpressure section above.
            raise TileBackpressureError(response.status, response.headers.get("Retry-After"))
        if response.status != 200:
            # Other 4xx/5xx raise ClientResponseError, which backoff retries
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
    # Tiles this crawl has seen answer 429/503 and has NOT since been served
    # (issue #444, rule 6) -- given up as stuck, or still pending when the
    # crawl stopped. A tile leaves the set once it is served. Advisory, never
    # part of the commit's integrity: a resume retries them LAST and lets none
    # of them charge a strike, because a failed tile is never committed -- so
    # without this record the served tiles that sat between them drop out of
    # `todo`, the stuck tiles meet at its head, and three of them read as a
    # storm (exit 87) every night with no progress (round-4 review of #444).
    # Pending tiles count too: three stuck tiles in a row at five sockets all
    # strike before any is given up, so the pause records no give-up at all.
    stuck: set[tuple[int, int]] = field(default_factory=set)
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
        age_s = (clock.utc_now() - datetime.fromisoformat(state["created_at"])).total_seconds()
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
        lattice = set(tiles)
        cp = TileCheckpoint(
            path=path,
            channel=channel,
            variant=variant,
            created_at=state["created_at"],
            done=done,
            api_requests_before=int(state["api_requests_total"]),
            # Absent from a record written before #444's round 4, which reads
            # as "none known". Filtered rather than validated: it is advice
            # about request ORDER, so a stale entry costs a strike's worth of
            # caution at most and must never cost the resume.
            stuck={
                (int(x), int(y))
                for x, y in state.get("stuck_tiles") or []
                if (int(x), int(y)) in lattice and (int(x), int(y)) not in done
            },
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
    created_at = cp.created_at or clock.utc_now().isoformat()
    state = {
        "format_version": MAPILLARY_CHECKPOINT_FORMAT_VERSION,
        "bbox": list(bbox),
        "zoom": TILE_ZOOM,
        "channel": cp.channel,
        "variant": cp.variant,
        "tile_count": len(tiles),
        "done_tiles": [[tx, ty, rows] for (tx, ty), rows in done.items()],
        # A tile served since it was refused is committed by now, and so no
        # longer suspect: this filter is what drops it from the record.
        "stuck_tiles": sorted([tx, ty] for tx, ty in cp.stuck if (tx, ty) not in done),
        "census_rows": sum(done.values()),
        "api_requests_total": api_requests_total,
        "created_at": created_at,
        "updated_at": clock.utc_now().isoformat(),
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
    connection_limit: int = MAPILLARY_TILE_CONNECTION_LIMIT,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    max_requests: int | None = None,
    deadline_monotonic: float | None = None,
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
                max_requests=max_requests,
                deadline_monotonic=deadline_monotonic,
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
    connection_limit: int = MAPILLARY_TILE_CONNECTION_LIMIT,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    max_requests: int | None = None,
    deadline_monotonic: float | None = None,
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
        max_requests: stop after this many requests, CHECKPOINT the unfetched
            tiles and raise :class:`SweepIncompleteError` (issue #318), or None
            to fetch every tile. A SOFT ceiling: requests already in flight when
            it trips are allowed to finish, so the overshoot is bounded by
            ``connection_limit * TILE_MAX_TRIES`` rather than by the size of the
            city. Requires ``checkpoint_path`` -- capped and uncheckpointed, a
            crawl discards everything it paid for.
        deadline_monotonic: an absolute ``time.monotonic()`` value at which to
            stop the same way (issue #344), or None for no clock stop. Checked
            beside ``max_requests`` at the same tile boundary and with the same
            soft-ceiling residue; whichever ceiling is reached first pauses the
            crawl, and the error names which. Requires ``checkpoint_path``, for
            the same reason.
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
        ValueError: ``max_requests`` or ``deadline_monotonic`` given without
            a ``checkpoint_path``. A caller bug rather than a runtime
            condition -- both silent fall-backs are wrong in a way nothing
            downstream could see, since ignoring the cap overspends a per-IP
            budget and honouring it burns one for nothing.
        SweepIncompleteError: the cap or the deadline was reached, with
            tiles unfetched and the checkpoint holds them. Progress, not
            breakage: it maps to exit 83, which the scheduler amnesties rather
            than counting a failure.
        HostThrottledError: the CDN's backpressure stopped the crawl (issue
            #444) -- its TILE_BACKPRESSURE_PAUSE_AFTER-th standing 429/503
            strike, or a Retry-After past the ceiling -- whether or not any
            tile is committed. (A hold that would outlast the deadline is the
            clock's SweepIncompleteError below that limit.) The checkpoint is kept; it
            maps to the throttled exit (87), which the scheduler amnesties and
            uses to skip this host for the rest of the night.
        DownloadError: on a rejected token or tile transport failure, carrying
            ``api_requests`` so the caller can still record what it spent.
    """
    unresumable = crawl_ceiling_needs_checkpoint(max_requests, deadline_monotonic, checkpoint_path)
    if unresumable is not None:
        # REFUSED HERE, before a single request, because the two arguments are
        # only meaningful together (issue #318). A cap says "spend this much
        # tonight and continue tomorrow", and without somewhere to commit to
        # there is no tomorrow -- the crawl would stop at the cap and throw
        # every tile it paid for away, nightly, forever, while the ledger showed
        # the spend and the catalog showed no run.
        #
        # A ValueError rather than a quiet fall-back to uncapped, because both
        # fall-backs are wrong in a way nothing downstream could see: ignoring
        # the cap silently overspends a per-IP budget, and honouring it silently
        # burns it. The only caller that passes a cap is the scheduler, which
        # always passes a checkpoint path with it, so this fires for a
        # programming error and never for an operator. The wall-clock deadline
        # (issue #344) is the same kind of stop and is refused the same way.
        raise ValueError(
            f"{unresumable} needs a checkpoint_path: a capped crawl stops part-way, "
            "and with nothing to resume from that discards everything it spent."
        )
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
            # Still one status line per crawl, so a grep over a night's logs
            # finds every census -- this one issued nothing.
            logger.info(TileStatusCounts().summary_line(city_name, 0))
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
    # Tiles an earlier night saw refused on 429/503 and never served go LAST
    # and charge no strike (issue #444, round-4 review). Request order only: reassembly walks
    # `tiles`, so the census is byte-identical whatever order `todo` is in.
    # Without this a resume -- whose `todo` has lost every served tile -- lines
    # the stuck tiles up at its head with nothing served between them, which is
    # a storm as one crawl sees it, and the city stops as 87 every night. Fresh
    # tiles go first so a REAL storm is still met, and paused on, by tiles with
    # no such history.
    prior_stuck = checkpoint.stuck.copy() if checkpoint else set()
    todo = [tile for tile in tiles if tile not in done and tile not in prior_stuck] + [
        tile for tile in tiles if tile in prior_stuck
    ]
    if done and todo:
        stuck_note = (
            f"; {len(prior_stuck)} tile(s) an earlier run saw refused on 429/503 and never "
            f"served are retried last and charge no backpressure strike"
            if prior_stuck
            else ""
        )
        logger.warning(
            f"Resuming {city_name} from the checkpoint at {checkpoint.path}: "
            f"{len(done)}/{len(tiles)} tiles already fetched for "
            f"{checkpoint.api_requests_before:,} requests; {len(todo)} to go{stuck_note}"
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
    # What every request actually awaits: this crawl's limiter behind a gate a
    # 429/503 can close (issue #444). Built here, per crawl, beside the limiter
    # it wraps -- which is what keeps a hold or a halved pace from outliving
    # this crawl.
    pacer = _BackpressurePacer(rate_limiter)
    if max_requests_per_minute <= 0:
        logger.info("Tile pacing DISABLED (max_requests_per_minute <= 0)")
    elif jitter > 0:
        # Logged as the gap DISTRIBUTION, not as a rate: "40/min" is what the
        # metronome also said, and the shape is the whole change under test. The
        # p99 is the shifted exponential's, m * ((1 - j) + j * ln(100)).
        mean_gap_s = 60.0 / max_requests_per_minute
        floor_s = mean_gap_s * (1 - jitter)
        p99_s = mean_gap_s * ((1 - jitter) + jitter * math.log(100.0))
        logger.info(
            f"Pacing tile requests at a mean {max_requests_per_minute}/min, "
            f"exponentially jittered (CV {jitter:.2f}; gaps floor {floor_s:.2f} s, "
            f"mean {mean_gap_s:.2f} s, p99 {p99_s:.2f} s, no ceiling — issue #292)"
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

    # Every HTTP response this crawl receives, by class (issue #444). Logged
    # once, after the gather, on every way out of it -- success, refusal, pause.
    statuses = TileStatusCounts()

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

    # The cap tripped: stop DISPATCHING, and pause rather than fail (issue
    # #318). A SEPARATE flag from `fatal` rather than a second meaning for it,
    # because the two say opposite things about the tiles they skip. `fatal`
    # means every remaining tile would fail identically, so the city is over;
    # this means every remaining tile is perfectly fetchable and we are simply
    # out of budget for tonight, so they are owed to tomorrow. Folding them
    # together would make one of the two exit codes wrong whichever way it went.
    capped = False
    # WHICH ceiling stopped the crawl, for the pause message (issue #344):
    # "requests" or "clock". Set by the first tile turned away, so a night that
    # runs into both reports the one it met first.
    stop_reason: str | None = None

    # Requests promised to tiles that are past the cap check but have not yet
    # counted one. THE CAP GATES ON `api_requests + reserved`, NOT ON
    # `api_requests` ALONE, and the difference is the whole ordinary overshoot
    # (issue #318 review). `count_request` fires inside `_fetch_tile`, AFTER
    # `rate_limiter.acquire()` — so at a paced 40/min every task the semaphore
    # admits clears a check reading a stale `api_requests`, then queues on the
    # limiter behind it. Measured on this code with a fetch that actually
    # awaits: cap 200 at connection_limit 50 spent 249. Reserving at the gate
    # makes that 200, because the (connection_limit - 1) tasks holding tokens
    # they have not spent are now visible to the tasks behind them.
    #
    # It cannot be reserved per ATTEMPT, only per tile: `_fetch_tile` is
    # `@backoff`-decorated and may spend up to TILE_MAX_TRIES. Those extra
    # attempts do land in `api_requests` as they happen, so they close the gate
    # on tiles not yet admitted; what they cannot do is un-admit a tile already
    # in flight. That residue is the honest remaining bound, below.
    reserved = 0

    # Backpressure (issue #444): the strikes this crawl's 429/503 responses
    # charged, whether the crawl has decided to pause on them, and why, for the
    # pause message.
    #
    # A strike is a tile's FIRST backpressure response that opened a new
    # episode, keyed by tile and stamped with the pacer generation its request
    # was sent under. Whether it still COUNTS toward the pause is decided live,
    # by `standing_strikes`, from what the CDN did after it (round-3 review of
    # #444):
    #   * CONFIRMED -- the tile was served on a later attempt. The CDN asked it
    #     to wait and then served it: a real throttle, so the strike stands.
    #   * PENDING (still retrying) or GIVEN UP (stuck) -- it stands only while
    #     no OTHER tile has been served from a request sent after it, i.e. in a
    #     later pacer generation. Once one has, the host is serving and this
    #     tile is the thing refusing, so the strike is (provisionally, for a
    #     pending one) withdrawn.
    # A storm serves nothing between its refusals, so all its strikes stand and
    # its third episode pauses. Withdrawing a given-up tile's strike only on a
    # success AFTER its give-up (round 2) was too late: give-up takes two holds,
    # and the hold's FIFO gate sends the next stuck tile's first request before
    # any success can land, so three stuck tiles two apart at five sockets paused
    # the city as 87 every night with no progress.
    strikes: dict[tuple[int, int], list] = {}  # tile -> [generation, state]
    # The highest pacer generation any successful request was sent under.
    served_generation = -1
    backpressure_paused = False
    backpressure_detail: str | None = None
    # A hold that would outlast the deadline turned the crawl into a clock
    # stop (exit 83) rather than a backpressure pause; see on_backpressure.
    clock_stopped = False

    def standing_strikes() -> int:
        """The strikes that still count toward TILE_BACKPRESSURE_PAUSE_AFTER."""
        return sum(
            1
            for generation, state in strikes.values()
            if state == "confirmed" or served_generation <= generation
        )

    def pause_for_backpressure(detail: str) -> None:
        """Stop the crawl as THROTTLED (exit 87): see the capped branch below."""
        nonlocal backpressure_paused, backpressure_detail, stop_reason
        backpressure_detail = detail
        backpressure_paused = True
        if stop_reason is None:
            stop_reason = SWEEP_STOP_BACKPRESSURE
        # Nothing more goes out from this crawl, including requests already
        # waiting on the pacer (review of #444: they used to be released).
        pacer.close()
        logger.warning(
            f"Mapillary tile CDN backpressure for {city_name}: {backpressure_detail}; "
            f"pausing the crawl, NOT a refusal: it ends as THROTTLED (exit 87), keeping "
            f"whatever tiles are committed for the next run to resume from"
        )

    def episodes_detail(count: int) -> str:
        return (
            f"{count} HTTP 429/503 episodes in one crawl "
            f"(the limit is {TILE_BACKPRESSURE_PAUSE_AFTER})"
        )

    def on_backpressure(
        e: TileBackpressureError, sent_generation: int, tile_first: bool, tile: tuple[int, int]
    ) -> float | None:
        """
        Record one 429/503: halve the pace, and say how long to hold.

        Returns the seconds to hold every request for, or None when there is
        to be no hold: either the crawl PAUSES as THROTTLED -- on the
        TILE_BACKPRESSURE_PAUSE_AFTER-th standing strike, or on a Retry-After
        past TILE_BACKPRESSURE_MAX_WAIT_S -- or it STOPS ON ITS CLOCK, because
        the wait would outlast the deadline, or it is already stopping
        (below), when nothing will be sent after the hold anyway.

        ``sent_generation`` is the pacer's generation when the answered request
        was sent. Older than the current one means a hold began after it went
        out: the same episode, so no strike (see TILE_BACKPRESSURE_PAUSE_AFTER).

        ``tile_first`` is whether this is the tile's FIRST backpressure
        response. Only a first one can be a strike (round-2 review of #444): a
        tile's own retry being refused again says something about that tile,
        not the host.
        """
        nonlocal capped, stop_reason, clock_stopped
        same_episode = sent_generation != pacer.generation
        strike = tile_first and not same_episode
        if strike:
            strikes[tile] = [sent_generation, "pending"]
        pacer.slow_down()
        if backpressure_paused or clock_stopped or fatal is not None:
            # A late answer from a tile that was in flight when the crawl
            # decided to stop, or after a whole-city error (a 302): counted,
            # never waited on, and no stop of its own -- `fatal` already ends
            # the crawl, and the refusal's exit 75 must win.
            return None
        wait = backpressure_wait_seconds(retry_after_seconds(e.retry_after))
        if wait is None:
            # Says something about the HOST whatever else is happening: the
            # server named a time past what this process will sleep.
            pause_for_backpressure(
                f"HTTP {e.status} with a Retry-After of {e.retry_after!r}, longer than the "
                f"{TILE_BACKPRESSURE_MAX_WAIT_S:.0f} s it will wait in-process"
            )
            return None
        standing = standing_strikes()
        if strike and standing >= TILE_BACKPRESSURE_PAUSE_AFTER:
            pause_for_backpressure(episodes_detail(standing))
            return None
        # Already stopping (round-2 review of #444): the cap or the clock has
        # turned a tile away (`capped`), or will turn this one away at its
        # next check. No retry is going out, so a hold would only sleep with
        # the machine-wide tile lock held, and the stop stays the cap's or
        # the clock's.
        stopping = (
            capped
            or crawl_deadline_passed(deadline_monotonic)
            or (max_requests is not None and api_requests + reserved >= max_requests)
        )
        if stopping:
            return None
        if deadline_monotonic is not None and time.monotonic() + wait >= deadline_monotonic:
            # A hold that would outlast the crawl's wall-clock budget is not
            # slept: the scheduler forwards that budget as its SIGKILL timeout
            # less a 600 s margin, and a 900 s hold begun inside the margin
            # would be killed mid-sleep and counted a failure. Below the strike
            # limit this is the CLOCK's stop, exactly as if the budget had run
            # out now (round-3 review of #444): one 429 near the end of a long
            # crawl says no more about the host than one 429 anywhere else,
            # which the crawl would wait out and continue -- it must not become
            # the 87 that skips the host for the rest of the night. Nor may the
            # retry go out without its wait, so the crawl stops admitting tiles
            # here, turned away uncommitted as at the deadline.
            clock_stopped = True
            capped = True
            if stop_reason is None:
                stop_reason = SWEEP_STOP_CLOCK
            pacer.close()
            logger.warning(
                f"Mapillary tile CDN answered HTTP {e.status} (Retry-After: {e.retry_after}) "
                f"for {city_name}, asking for a {wait:.0f} s wait that would outlast this "
                f"crawl's wall-clock budget: stopping on the clock now (exit 83), keeping "
                f"whatever tiles are committed (issue #444)"
            )
            return None
        if strike:
            logger.warning(
                f"Mapillary tile CDN answered HTTP {e.status} (Retry-After: {e.retry_after}) "
                f"for {city_name}: holding every tile request for {wait:.0f} s, then "
                f"continuing at HALF the pace for the rest of this crawl (backpressure "
                f"episode {standing}/{TILE_BACKPRESSURE_PAUSE_AFTER}; issue #444)"
            )
        # No strike -- the hold already open covers it, or it is a tile's own
        # retry -- still returns the wait: it may EXTEND an open hold if it
        # asked for longer (holds end at the latest requested end), costs
        # nothing if it did not, and otherwise holds the other tiles once more
        # before this one is given up.
        return wait

    def on_tile_served(tile: tuple[int, int], generation: int) -> None:
        """A tile was served from a request sent under pacer ``generation``."""
        nonlocal served_generation
        served_generation = max(served_generation, generation)
        record = strikes.get(tile)
        if record is None or record[1] != "pending":
            return
        # Refused, waited, then served: a real throttle episode, confirmed.
        record[1] = "confirmed"
        if backpressure_paused or clock_stopped or fatal is not None:
            return
        standing = standing_strikes()
        if standing >= TILE_BACKPRESSURE_PAUSE_AFTER:
            # A confirmation can only RAISE the count (a served tile withdraws
            # pending strikes, never confirmed ones), so this is the other
            # place the limit can be reached. This tile itself is served and
            # commits; every tile still to go is turned away.
            pause_for_backpressure(episodes_detail(standing))

    async def fetch_one(x: int, y: int) -> pd.DataFrame:
        nonlocal fatal, capped, reserved, stop_reason
        url = f"{TILE_URL_TEMPLATE.format(z=TILE_ZOOM, x=x, y=y)}?access_token={access_token}"
        attempts = 0
        # This tile's backpressure responses (TILE_BACKPRESSURE_TILE_TRIES), and
        # the pacer generation its latest attempt was sent under.
        tile_backpressure = 0
        sent_generation = pacer.generation

        def on_attempt() -> None:
            # One call per HTTP request, from inside _fetch_tile: the ledger's
            # count, plus the status line's retries (any attempt past a tile's
            # first, whichever layer retried it).
            nonlocal attempts, sent_generation
            count_request()
            sent_generation = pacer.generation
            attempts += 1
            if attempts > 1:
                statuses.retries += 1

        async with semaphore:
            # The abort check belongs HERE, inside the semaphore, not at the top
            # of the coroutine: gather starts every task at once and each runs to
            # its first suspension point, so a check above this line would be
            # evaluated by all N tasks before any response has come back. Behind
            # the semaphore, tasks resume a few at a time as in-flight ones
            # drain, see the flag, and return without taking a rate-limiter
            # token or counting a request. Worst case is connection_limit
            # requests instead of the whole city.
            # A LOOP only so a tile answered 429/503 can be retried (issue
            # #444), and it re-enters at the TOP on purpose: the retry is
            # re-checked against the abort, the cap (with its reservation) and
            # the deadline exactly as a fresh tile is, so backpressure can
            # never be the way a crawl spends past its cap.
            while True:
                if fatal is not None:
                    # Nothing was requested for this tile, so keep the progress bar
                    # honest: a city that stopped at request 1 must not read like a
                    # city that hung at tile 3. That line in
                    # logs/collect_{city}_{channel}_{date}.log is how an operator
                    # tells those two apart.
                    progress_bar.update(1)
                    return records_to_census([])
                over_cap = max_requests is not None and api_requests + reserved >= max_requests
                # The wall-clock budget (issue #344) is checked HERE, beside the cap,
                # and takes exactly its path below: a clock stop is a pause for the
                # same reason a cap is, and a tile already in flight finishes its
                # retries under it with the same residue the cap documents.
                # A backpressure pause (issue #444) takes this path too: the
                # crawl is not over, the CDN asked it to stop for now. So does a
                # clock stop decided early, by a hold that would outlast the
                # deadline (`clock_stopped`, which set its own reason).
                if (
                    backpressure_paused
                    or clock_stopped
                    or over_cap
                    or crawl_deadline_passed(deadline_monotonic)
                ):
                    # THE CAP IS CHECKED HERE FOR THE REASON THE ABORT ABOVE IS, and
                    # it inherits that check's shape but not its bound. A tile
                    # admitted here reserves its request before releasing control,
                    # so the cap is not overshot at all in the ordinary case; what
                    # remains is RETRIES BY TILES ALREADY IN FLIGHT, at most
                    # connection_limit * (TILE_MAX_TRIES - 1). This function serves
                    # TWO callers, and since #361 both hold the same 5 sockets --
                    # download_common.MAPILLARY_TILE_CONNECTION_LIMIT, this
                    # module's default and the walk's ceiling at once -- so the
                    # residue is 20 on either:
                    #   GRID  (cli.py): cli.py forwards --connection-limit only on
                    #     its gsv arm, so this census takes this module's own
                    #     default. Not "the argparse default of 5" -- cli.py's
                    #     --connection-limit default is 50; the 5 arrives because
                    #     the argument is never forwarded.
                    #   WALK  (collect.py): the scheduler hands it
                    #     min(its per-child share, WALK_CONNECTION_LIMITS["mapillary"])
                    #     = min(50 in prod, 5). Until #361 that ceiling was 50, so a
                    #     capped walk could overshoot by 200.
                    #
                    # It is a soft ceiling on purpose: stopping requests already in
                    # flight would mean cancelling a paced, retrying fetch
                    # mid-attempt, which costs the guarantee that every request we
                    # made was counted. What is NOT deliberate is a soft ceiling
                    # that misses by a connection_limit on every capped night.
                    #
                    # Returning an empty census WITHOUT committing is what makes
                    # this resumable: an uncommitted tile is not in `done`, so the
                    # next invocation's `todo` still holds it. The empty frame never
                    # reaches the census -- the raise below happens before the
                    # settle loop, precisely so a skipped tile cannot be read as a
                    # tile observed to be empty.
                    capped = True
                    if stop_reason is None:
                        # The cap is tested first, so a tile that finds BOTH
                        # ceilings tripped at once reports "requests": a tie
                        # resolves to the request cap, as in the KartaView sweep.
                        # (A backpressure pause set its own reason already.)
                        stop_reason = SWEEP_STOP_REQUESTS if over_cap else SWEEP_STOP_CLOCK
                    progress_bar.update(1)
                    return records_to_census([])
                # Reserved BEFORE the await, released after it: there is no
                # suspension point between the check above and this line, so the
                # loop cannot interleave another task into the gap.
                reserved += 1
                slow_down_please: TileBackpressureError | None = None
                turned_away = False
                try:
                    # Pacing/counting happen inside _fetch_tile, per retried attempt.
                    tile_bytes = await _fetch_tile(
                        session, url, timeout, pacer, on_attempt, statuses.record
                    )
                except TileBackpressureError as e:
                    # Already counted (one request, one status) inside
                    # _fetch_tile. Handled below, after the reservation is
                    # released, so a hold never pins a reservation open.
                    slow_down_please = e
                except _CrawlPaused:
                    # Waited on the pacer and the crawl paused meanwhile: no
                    # request was sent or counted for this attempt.
                    turned_away = True
                except DownloadError as e:
                    # ONLY DownloadError trips the abort. Per-tile failures
                    # (aiohttp.ClientResponseError from a 404/5xx, or a decode
                    # error) must still fan out to every tile — that is #168's
                    # guarantee that one bad tile cannot discard a city, and it is
                    # what the settle loop's MAX_FAILED_TILE_FRACTION tolerance is
                    # for. Assigned before leaving the semaphore block, so no
                    # waiting task can slip past the check above.
                    fatal = fatal or e
                    # And nothing more goes out: requests already waiting on
                    # the pacer would otherwise still be sent (3-4 after a 302,
                    # measured), and a hold in progress would be slept out
                    # holding the host lock. Turned away, they set `capped`,
                    # but the `fatal` raise after the gather runs first.
                    pacer.close()
                    raise
                finally:
                    # Every exit from the fetch, exception included: a reservation
                    # that leaked would shrink the cap for the rest of the crawl and
                    # eventually close the gate on tiles the budget still covers.
                    # The requests it stood for are in `api_requests` by now,
                    # whether the attempt succeeded, failed or exhausted its
                    # retries, so releasing it double-counts nothing.
                    reserved -= 1
                if turned_away:
                    # The pause check's own outcome: uncommitted, owed to the
                    # resume, and never past the raise before the settle loop.
                    capped = True
                    progress_bar.update(1)
                    return records_to_census([])
                if slow_down_please is None:
                    # Served: a confirmation of this tile's own strike, if it
                    # charged one, and a withdrawal of every pending or stuck
                    # strike older than this request (see `strikes`).
                    on_tile_served((x, y), sent_generation)
                    break
                if checkpoint is not None:
                    # Remembered for a resume until it is served -- durable
                    # with the next commit record, a tile's or `interrupted`'s
                    # spend (see TileCheckpoint.stuck).
                    checkpoint.stuck.add((x, y))
                wait = on_backpressure(
                    slow_down_please,
                    sent_generation,
                    # A tile an earlier night already saw refused and never
                    # served may refuse on its own, so even its first response
                    # here says nothing about the host (round-4 review of #444).
                    tile_first=tile_backpressure == 0 and (x, y) not in prior_stuck,
                    tile=(x, y),
                )
                if wait is not None:
                    await pacer.hold(wait)
                tile_backpressure += 1
                if wait is not None and tile_backpressure >= TILE_BACKPRESSURE_TILE_TRIES:
                    # Given up as an ordinary failed tile, AFTER the hold (the
                    # hold protects the other tiles, not this one): it settles
                    # as an exception, so #168's tolerance judges it.
                    logger.warning(
                        f"Mapillary tile ({x}, {y}) of {city_name} answered HTTP "
                        f"{slow_down_please.status} {tile_backpressure} times; giving it up "
                        f"as a failed tile (issue #444)"
                    )
                    if (x, y) in strikes:
                        # Its strike stands only until another tile is served
                        # from a later generation: see `standing_strikes`.
                        strikes[(x, y)][1] = "given_up"
                    raise slow_down_please
                # Back to the top: retried if the checks there still admit it,
                # or turned away as a pause (issue #444).
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
        async with aiohttp.ClientSession(headers={"User-Agent": MAPILLARY_USER_AGENT}) as session:
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
        # HERE, so a refused, failed or paused crawl logs its statuses too:
        # every request is settled by now, and every raise below is after it.
        logger.info(statuses.summary_line(city_name, api_requests))

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

    # BEFORE THE SETTLE LOOP, and that is the whole safety argument (issue
    # #318). A tile skipped at the cap returned an EMPTY census rather than an
    # exception, so from here on it is indistinguishable from a tile the server
    # answered with no imagery: it would land in `fetched`, reassemble into the
    # census, and publish absence that was never observed -- as an immutable
    # dated snapshot, diffing against its predecessor as "every pano in the rest
    # of the city removed". Nothing below this line can tell the two apart, so
    # nothing below this line ever sees a capped crawl.
    #
    # WHICH ALSO MEANS the settle loop's MAX_FAILED_TILE_FRACTION check does not
    # run on a capped night -- worth stating, because "nothing below this line"
    # reads as a pure safety property and it is a trade. Here it costs nothing:
    # the only night that is NOT capped is one that attempted every tile still
    # outstanding, so that night's `failed_tiles` is the whole city's unmeasured
    # set and the tolerance sees it in full. The same sentence is NOT free in
    # download_panoramax, whose moved-endpoint guard sits in the same stretch
    # with no such final night to fall back on -- see the note there.
    if capped:
        committed = len(checkpoint.done) if checkpoint is not None else 0
        resumable = checkpoint is not None and not checkpoint.degraded and bool(checkpoint.done)
        if backpressure_paused:
            # EVERY backpressure stop is THROTTLED (exit 87), committed tiles or
            # not (issue #444). Before, a stop with nothing committed took the
            # plain-DownloadError arm below, which counts a consecutive_failure
            # -- so a few nights of a 429 storm would quarantine every Mapillary
            # pair (#421) -- and nothing latched the host, so the next city
            # walked into the same 429s at full pace. Tested on the flag, not on
            # `stop_reason`: a tile turned away at the cap a moment before the
            # pause still leaves the CDN asking us to slow down.
            #
            # The checkpoint is kept exactly as for exit 83 (`interrupted`
            # commits the spend; nothing here discards it). What differs by
            # case is only what the message may claim: with nothing committed
            # there is no progress, so it must not say "resume".
            phrase = SWEEP_STOP_PHRASES[SWEEP_STOP_BACKPRESSURE]
            head = (
                f"Mapillary tile census for {city_name} {phrase}: {backpressure_detail}. "
                f"{committed}/{len(tiles)} tiles fetched; {api_requests} requests spent "
                f"this process, {_census_requests_total(checkpoint, api_requests)} in total. "
            )
            if resumable:
                tail = f"Progress is checkpointed at {checkpoint.path}; the next run resumes it."
            else:
                tail = "No tile is committed, so the next run starts from the first tile."
            raise interrupted(
                HostThrottledError(
                    f"{head}{tail} Not a refusal (that is a redirect to /login, exit 75): "
                    f"the CDN asked for a slower pace, so collect no more Mapillary from this "
                    f"host tonight. Nothing is finalized.",
                    host=HOST_MAPILLARY_TILES,
                    checkpoint_path=checkpoint.path if resumable else None,
                    units_done=committed,
                    unit_count=len(tiles),
                )
            )
        if stop_reason == SWEEP_STOP_CLOCK:
            ceiling = f"{crawl_budget_seconds(deadline_monotonic):,}-second wall-clock budget"
        else:
            ceiling = f"{max_requests:,}-request cap"
        detail = (
            f"Mapillary tile census for {city_name} stopped at its "
            f"{ceiling} with {committed}/{len(tiles)} tiles fetched; "
            f"{api_requests} requests spent this process, "
            f"{_census_requests_total(checkpoint, api_requests)} in total."
        )
        if not resumable:
            # NOT a pause, because there is nothing to resume FROM, in any of
            # THREE ways. No checkpoint at all (an unwritable directory --
            # `_open_tile_checkpoint` fails open); one whose commits latched off
            # mid-crawl; or one that is live and healthy and has committed
            # NOTHING.
            #
            # That third one is the subtle member and it is not hypothetical:
            # `_commit_spend` returns early on an empty `done`, deliberately, so
            # a crawl that commits no tile writes no `state.json` at all. A cap
            # near the launch floor whose in-flight tiles all fail transiently
            # -- a 404 storm, a partial block -- reaches exactly that state, and
            # calling it a pause would raise "progress is checkpointed at ...,
            # re-run to continue" over an empty directory. Exit 83 is amnestied,
            # so the next night starts from tile 0 and does the same, forever,
            # with no failure counted and nothing for an alert to see.
            #
            # A plain DownloadError takes none of the exit-83 amnesty and counts
            # a real failure, which is the honest answer: calling this progress
            # would tell an operator to re-run a command that spends the same
            # requests and stops in the same place. The caller is refused a cap
            # without a checkpoint PATH at all (see the guard above); these are
            # the runtime halves of the same rule, and they are why that guard
            # is not enough on its own.
            raise interrupted(
                DownloadError(f"{detail} No tile is checkpointed, so nothing can be resumed.")
            )
        raise interrupted(
            SweepIncompleteError(
                f"{detail} Progress is checkpointed at {checkpoint.path}; re-running with "
                f"the same checkpoint path continues it. Nothing is finalized: a partial "
                f"census must never be published as a dated snapshot.",
                checkpoint_path=checkpoint.path,
                units_done=committed,
                unit_count=len(tiles),
                unit_name=SWEEP_UNIT_TILES,
                stopped_by=stop_reason,
            )
        )

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
        # EVERY TILE ACCOUNTED FOR -- fetched by some night, or recorded failed
        # (issue #318). This term was absent while it could not be false: the
        # only ways to reach this line with tiles missing all raised above it,
        # so the position of the promotion block WAS the completeness argument.
        # A request cap makes that argument depend on one `if capped: raise`
        # staying above this line forever. It is the same rule the READER
        # already applies in `is_complete`, so an entry promoted without it
        # would be refused and deleted on first use -- silently costing a whole
        # crawl rather than failing anywhere visible.
        and checkpoint.done.keys() | set(failed_tiles) == set(tiles)
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
    connection_limit: int = MAPILLARY_TILE_CONNECTION_LIMIT,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    max_requests: int | None = None,
    deadline_monotonic: float | None = None,
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
        max_requests=max_requests,
        deadline_monotonic=deadline_monotonic,
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
