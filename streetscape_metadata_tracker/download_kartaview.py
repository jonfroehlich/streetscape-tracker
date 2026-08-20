"""
KartaView census collection: a paginated radius sweep over a city's frozen bbox.

THE API SHAPE IS NEITHER MAPILLARY'S NOR GSV'S, and everything here follows from
that. There is no bulk metadata endpoint: the coverage tiles
(``/2.0/sequence/tiles/{x}/{y}/{z}.png``) carry geometry only, their
``.json``/``.geojson`` variants return empty at every tile tried (including the
official docs' own example), and any unconstrained ``/2.0/photo/?lat=&lng=&radius=``
answers ``apiCode 408 "Query timeout"``. The one reliable spatial path is
``POST /1.0/list/nearby-photos/`` in RADIUS mode -- bbox mode errors or returns
zero in the southern hemisphere, i.e. at the Grab fleet cities that are the whole
reason to add this provider. So a census is a sweep of overlapping circles, and
this module is that sweep. See docs/experiments/kartaview-feasibility.md.

A sweep returns EVERY photo in a circle, so KartaView is a census like Mapillary
rather than a per-grid-point sample like GSV: the machinery from decoded records
to METADATA-schema rows is ``census.py``'s, bound here to KartaView's schema and
columns. 360-degree imagery is ``projection == "SPHERE"`` and flat imagery is
``"PLANE"``, which is issue #116's ``is_pano`` split for free.

WHAT THE SWEEP COSTS, MEASURED (docs/experiments/kartaview-sweep-cost.md, 14
cities / 638 requests). The cost is one geometric term -- ``root_cells`` tracks
``bbox_area / (2 r^2)`` to within 10% above ~350 km2 -- so a city is budgeted by
BBOX AREA, not by how much imagery it holds. The median catalog city is ~12
requests, the same as Mapillary's median tile count; the p95 is 384 and Singapore
is ~7,300. :func:`estimate_sweep_requests` is that number, and it is exact enough
to schedule against.

THREE MEASURED FACTS DECIDE THIS DESIGN. Two of them contradict the feasibility
study that preceded them, so read them before changing a radius, a page size or a
retry budget:

  1. ``/1.0/list/nearby-photos/`` pagination is EXHAUSTIVE (Seattle r=400
     ipp=200: pages 1-6, zero id overlap between any pair, union ==
     ``totalFilteredItems``, page 7 empty). A truncated circle is therefore
     PAGED, not subdivided -- and page 1 reports the circle's total, so it
     prices the rest of the circle before we pay for it.
  2. ``apiCode 690`` is FLAKY, not a function of (radius, ipp). Horace ND -- a
     bbox holding NO imagery at all -- refused r=1000 on 0/6 attempts at
     ipp=2000 and 0/4 at ipp=200, answered r=250 on 4/4, and then answered
     r=1000 on 2/2 some 45 minutes later. So a refusal is a transient to retry,
     not a measurement of anything. Retrying is 4x cheaper than subdividing (one
     request against four, each of which may cascade: 1 + 4 + 16 = 21 to the
     floor), and 88 of 174 retries cleared during the study.
  3. The working radius is a property of the LOCATION and varies by 4x across
     the catalog, uncorrelated with density -- Seattle held r=1000 at a higher
     measured photo density than either New York or Manila, both of which
     calibrated down to r=500. So it is measured once per city
     (:func:`calibrate_radius`, at most 30 requests at the defaults) rather than
     rediscovered at every cell, which would pay a cascade per cell.

HTTP 400 IS BACKPRESSURE, NOT A MALFORMED REQUEST. The server signals overload
with HTTP 400 carrying ``apiCode`` 690 or 408, and the remedy is to ask for less
-- the opposite of the usual 4xx reading, and the easiest thing here to get
wrong. Only backpressure may subdivide: asking a server for four requests where
it just failed to serve one is the shape of the Mapillary block (#198), not a fix
for it.

PACING. KartaView documents 100 requests/hour anonymous and 1,000 authenticated
(the FAQ is reachable only by scraping ``kartaview.org/main.*.js`` -- the docs are
a JS SPA -- and is corroborated by Bellingcat's toolkit entry), returns NO
``X-RateLimit-*`` or ``Retry-After`` headers of any kind, and did not enforce
either figure when measured (130 consecutive requests, zero 429s). A client
therefore cannot observe its own budget, which is exactly CLAUDE.md's corollary:
treat what you cannot find documented as unknown rather than unlimited, and pace
to the published number regardless of the headroom you can see. The sweep is
SERIAL -- one request in flight -- and paced by
:data:`DEFAULT_SWEEP_REQUESTS_PER_MINUTE`; concurrency would buy nothing anyway,
since the limiter is the bottleneck and the walk is adaptive (what to ask next
depends on the last answer).

The whole sweep also takes the machine-wide :data:`HOST_KARTAVIEW` lock, because
the documented limit is per API key but nothing tells us the enforced one is:
both of this project's prior per-IP bans (Mapillary tiles #198, Overpass #209)
were on limits no document described, and a per-IP limit is a property of the
machine that no per-process limiter can honour alone (issue #208).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import numpy as np
import pandas as pd

from . import census as census_core
from .analysis import EARLIEST_PLAUSIBLE_CAPTURE
from .config import KARTAVIEW_METADATA_DTYPES
from .download_common import (
    HOST_KARTAVIEW,
    AsyncRateLimiter,
    DownloadError,
    HostBlockedError,
    redact_credentials,
)
from .host_lock import host_lock
from .progress import progress

logger = logging.getLogger(__name__)

# The only reliable spatial endpoint (see the module docstring). Radius mode.
NEARBY_PHOTOS_URL = "https://kartaview.org/1.0/list/nearby-photos/"

# Documented hourly ceilings. The token is what makes a scheduled channel
# possible at all: at 100/hour the p95 city is 3.8 hours and Singapore is 73.
REQUESTS_PER_HOUR_ANON = 100
REQUESTS_PER_HOUR_AUTH = 1000

# Server-side page cap, from Grab's own JOSM plugin config
# (resources/kartaview_service.properties: nearbyPhotos.maxItems=2000).
#
# We ask for the cap. It is a trade rather than a free win -- a 2,000-row page is
# a heavier query, so the backpressure ceiling drops -- but the alternative is
# strictly worse: `ipp` is a CLIENT flag, and the feasibility study's 200-row
# default is what made it conclude that most of its targets could never be
# measured at all. At the cap the same 48 requests reached a complete sample at
# every one of its 8 targets.
IPP_MAX = 2000

# apiCode values that mean "you asked for too much" rather than "you asked
# wrongly". Both arrive inside an HTTP 400.
BACKPRESSURE_API_CODES = frozenset({408, 690})

# Radii to try when calibrating, largest first. Every rung has been answered
# somewhere in the measured record, and the smallest was answered at all 8
# feasibility targets.
RADIUS_LADDER_M = (1000, 500, 400, 300, 200, 100)

# The radius a sweep uses when a city has no calibrated one yet. It is the
# largest rung with any evidence behind it, and calibration only ever moves
# down from here.
DEFAULT_START_RADIUS_M = 1000

# The smallest circle the sweep will ever ask for. A cell that still refuses at
# the floor is a genuine defect rather than backpressure, and is recorded as a
# failed cell instead of being split further.
#
# The guard belongs on the CHILDREN, not on the cell being split (see
# :func:`can_subdivide`): asking "is this cell above the floor?" happily turns a
# 125 m cell into four 63 m ones, i.e. it enforces a floor of RADIUS_FLOOR_M / 2
# and asks the server for radii no rung has ever tested.
RADIUS_FLOOR_M = 100

# Deep paging is untested past page 7 (fact 1 was measured to there), so a cell
# that would need more than this is subdivided instead -- four shallower circles
# for the price of one wasted page-1.
MAX_PAGES_PER_CELL = 10

# A refusal is retried before it is believed (fact 2). Generous rather than
# minimal, because a retry costs one request and a subdivision costs four.
DEFAULT_BACKPRESSURE_RETRIES = 3

# Probes per rung during calibration. A rung is accepted only if EVERY probe on
# it answers, since one lucky point would set a radius the rest of the city then
# rediscovers the hard way -- which is the cost calibration exists to avoid.
DEFAULT_CALIBRATION_PROBES = 2

# Pacing, in the per-minute units AsyncRateLimiter speaks. 16/min is 960/hour,
# just under the documented authenticated ceiling, and the limiter's burst
# capacity is ~1 second's worth, so no hour can exceed it by more than a single
# request. Deliberately expressed as a rate rather than an hourly budget: the
# enforcement window is undocumented, so spreading the requests evenly is the
# conservative reading of an hourly figure (a burst of 1,000 followed by 59
# minutes of silence satisfies the same published number).
DEFAULT_SWEEP_REQUESTS_PER_MINUTE = 16

# Fraction of the bbox that may end up unmeasured before the sweep refuses to
# finalize. AREA rather than a cell count, because subdivision means cells are
# not one size: what this bounds is the size of the unknown region in a snapshot
# that is immutable once published. Mirrors download_mapillary's
# MAX_FAILED_TILE_FRACTION and download_gsv's MAX_FAILED_POINT_FRACTION:
# tolerate a blip, refuse a hole.
MAX_FAILED_AREA_FRACTION = 0.02

# Per-request timeout. Higher than the tile CDN's 30 s: this endpoint is a
# database query against a service whose own tracker carries an open MySQL
# collation error inside findNearbyPhotos, and a 2,000-row page is its heaviest
# documented shape.
DEFAULT_REQUEST_TIMEOUT_S = 60.0

_METERS_PER_DEG_LAT = 111_320.0


# ── Sweep geometry and the cost model (pure, no network) ───────────────────


@dataclass(frozen=True)
class Cell:
    """
    One square of the sweep, and the circle that covers it.

    A square of side ``size_m`` is exactly covered by its circumscribed circle,
    so ``radius_m = size_m * sqrt(2) / 2``. That is what makes the union of a
    lattice of cells cover the bbox with no gap -- and it is also where the
    sweep's redundancy comes from: circle area over cell area is pi/2 ~= 1.571,
    so the sweep re-sees each photo about 1.6 times and ``census.dedupe_census``
    is load-bearing rather than defensive.
    """

    lat: float
    lon: float
    size_m: float
    depth: int = 0

    @property
    def radius_m(self) -> int:
        return int(round(self.size_m * math.sqrt(2) / 2))


def _lon_span_deg(min_lon: float, max_lon: float) -> float:
    """
    East-west extent of a bbox in degrees, unwrapping an antimeridian crossing.

    geopy normalizes longitudes to +/-180, so a bbox straddling the antimeridian
    comes back with ``min_lon > max_lon`` (Suva and Taveuni in Fiji, and every
    other city within ~half a grid of 180 deg). ``max_lon - min_lon`` is then
    NEGATIVE, which is the shape that silently produced a near-empty sweep.
    """
    span = max_lon - min_lon
    return span + 360.0 if span < 0 else span


def _wrap_lon(lon: float) -> float:
    """Fold a longitude produced by unwrapped arithmetic back into +/-180."""
    return ((lon + 180.0) % 360.0) - 180.0


def cells_for_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, cell_size_m: float
) -> list[Cell]:
    """
    Tile a bbox with square cells of side ``cell_size_m``, returning their centres.

    Equirectangular placement about the bbox's own mid-latitude. The lattice is
    a fetch plan, not a data structure any artifact depends on, and each cell is
    covered by a circle 1.41x its own width. The grid the CSV is keyed to still
    comes from ``download_common.generate_grid_points``' geodesic solve,
    untouched.

    ANTIMERIDIAN. A bbox that crosses it arrives wrapped (``min_lon > max_lon``)
    and the naive ``max_lon - min_lon`` is negative, so ``ceil`` went negative
    and ``max(1, ...)`` collapsed the whole city to a single column of cells.
    This is not hypothetical and it is not new: ``download_mapillary``'s
    ``tiles_for_bbox`` carries the same fix and names Suva, Fiji as the case.
    Measured here before the fix, Taveuni FJ on an ordinary 40x40 km grid
    planned **29 cells where it needs 841** -- 3.4% of the city -- and, because
    ``_bbox_area_m2`` had the mirror-image bug and reported the wrap as 1.5
    million km2, even every one of those 29 failing computed as 0.004%
    unmeasured, three orders of magnitude under MAX_FAILED_AREA_FRACTION. So it
    did not fail: it returned a 97%-empty census as a clean success, which
    publishes and diffs as "every pano in the city removed".

    The residual approximation is the equirectangular ``cos(mid_lat)``: cells on
    the equator-ward half of a tall bbox are slightly wider in metres than
    ``cell_size_m``, so their circumscribed circle falls fractionally short at
    the corner -- about 1.7 m at a 40 km grid and 47 deg N, growing to ~7 m at
    100 km and 61 deg. Grids are capped at 40 km (#166), the shortfall is at the
    four corners only, and neighbouring circles overlap heavily everywhere else,
    so this is left as measured rather than fixed: correcting it would move
    every city's cell count and invalidate the committed cost record for a
    couple of metres of corner.
    """
    if cell_size_m <= 0:
        raise ValueError(f"cell_size_m must be positive, got {cell_size_m}")
    mid_lat = (min_lat + max_lat) / 2.0
    deg_lat = cell_size_m / _METERS_PER_DEG_LAT
    deg_lon = cell_size_m / (_METERS_PER_DEG_LAT * math.cos(math.radians(mid_lat)))
    n_y = max(1, math.ceil((max_lat - min_lat) / deg_lat))
    n_x = max(1, math.ceil(_lon_span_deg(min_lon, max_lon) / deg_lon))
    return [
        Cell(
            lat=min_lat + (j + 0.5) * deg_lat,
            lon=_wrap_lon(min_lon + (i + 0.5) * deg_lon),
            size_m=cell_size_m,
        )
        for j in range(n_y)
        for i in range(n_x)
    ]


def subdivide(cell: Cell) -> list[Cell]:
    """Split one cell into the four half-size cells that exactly cover it."""
    half = cell.size_m / 2.0
    d_lat = (half / 2.0) / _METERS_PER_DEG_LAT
    d_lon = (half / 2.0) / (_METERS_PER_DEG_LAT * math.cos(math.radians(cell.lat)))
    return [
        Cell(cell.lat + sy * d_lat, cell.lon + sx * d_lon, half, cell.depth + 1)
        for sy in (-1, 1)
        for sx in (-1, 1)
    ]


def can_subdivide(cell: Cell) -> bool:
    """
    Would splitting this cell produce children at or above the radius floor?

    Asked of the CHILDREN deliberately. The natural spelling -- "is this cell
    above the floor?" -- lets a 125 m cell split into four 63 m ones, halving
    the floor it was meant to enforce and asking the server for radii no rung
    has ever tested.
    """
    return subdivide(cell)[0].radius_m >= RADIUS_FLOOR_M


def pages_for_total(total_filtered_items: int | None, ipp: int = IPP_MAX) -> int:
    """
    Pages needed to exhaust one circle, given page 1's ``totalFilteredItems``.

    Page 1 is always paid, so an empty circle costs 1 page, not 0. An unknown
    total (an unparseable body) is priced as 1 rather than 0 -- pricing an
    unknown at zero is how a cost model quietly under-budgets the exact cities
    that broke it.
    """
    if total_filtered_items is None:
        return 1
    return max(1, math.ceil(total_filtered_items / ipp))


def estimate_sweep_requests(
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float = 20,
    radius_m: int = DEFAULT_START_RADIUS_M,
) -> int:
    """
    Requests a sweep of this city's grid bbox will make -- the KartaView
    analogue of :func:`download_mapillary.estimate_tile_count`.

    This is the number that gates the channel, and it is a FLOOR: it counts one
    page-1 per root cell and nothing else. Measured over 14 cities, seven cost
    exactly this and the rest carry overhead from two unrelated causes -- extra
    pages where imagery is dense, and refusal cascades where it is not (the
    worst case in the study, 6x, was a bbox holding no imagery at all).

    Args:
        radius_m: the city's calibrated radius if it has one, else the default.
            A city that calibrates to 500 m costs about 4x this estimate, which
            is why the previous run's calibrated value is worth storing.
    """
    # Imported here rather than at module scope: grid_bbox is pure geodesy that
    # happens to live in the Mapillary module, and a top-level import would make
    # every KartaView import pull in mapbox_vector_tile.
    from .download_mapillary import grid_bbox

    bbox = grid_bbox(center_lat, center_lon, grid_width, grid_height, step_length)
    return len(cells_for_bbox(*bbox, radius_m * math.sqrt(2)))


# ── Capture dates: two timestamps, and only one of them is a capture ───────


# The loose contributor-archive floor, not a fleet floor. KartaView (as
# OpenStreetCam) launched in 2016 and Grab's KartaCam fleet is far newer, but
# the imagery is overwhelmingly community dashcam footage and contributors
# upload genuinely old recordings -- the same reason Mapillary's floor is 2004
# rather than its 2013 founding. Stated here as the decode-time rule and mirrored
# in analysis.EARLIEST_PLAUSIBLE_CAPTURE, which a test pins to this constant.
EARLIEST_CAPTURE_DATE = EARLIEST_PLAUSIBLE_CAPTURE["kartaview"]

# Slack on the "nothing is captured after we observed it" ceiling. `shot_date`
# is a naive local timestamp with no zone, so a photo taken today in UTC+14
# legitimately reads as tomorrow in UTC; one day covers the whole inhabited
# range of offsets and cannot admit anything meaningfully wrong. The ceiling is
# mostly redundant anyway -- see :func:`shot_date_to_iso_date`, where
# `date_added` does the real work.
_FUTURE_SLACK = timedelta(days=1)


def _scalar_timestamp(value: str) -> pd.Timestamp:
    """
    One timestamp string -> naive ``pd.Timestamp``, or ``NaT`` if unusable.

    The scalar counterpart of :func:`_to_naive_datetime`, and it has to strip
    the zone for the same reason: an offset makes the Timestamp tz-aware, and
    comparing that to the naive ``EARLIEST_CAPTURE_DATE`` raises TypeError
    rather than returning False. ``pd.Timestamp`` raises a family of errors on
    garbage (ValueError, TypeError, OverflowError, and dateutil's ParserError
    which is a ValueError), so all of them mean the same thing here: unusable.
    """
    try:
        parsed = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return pd.NaT
    if parsed is not pd.NaT and parsed.tzinfo is not None:
        parsed = parsed.tz_convert("UTC").tz_localize(None)
    return parsed


def shot_date_to_iso_date(shot_date: str | None, date_added: str | None) -> str:
    """
    KartaView's two timestamps -> 'YYYY-MM-DD' capture date, or '' when unusable.

    ``shot_date`` is contributor EXIF and ``date_added`` is server-side upload
    time. They are NEVER merged: a ``shot_date or date_added`` fallback would
    file Krabi's undated 2025 bulk upload beside Seattle's genuine 2025 capture
    year, indistinguishably, in a project whose entire subject is when imagery
    was captured. ``date_added`` is published as its own column instead.

    THE INVARIANT: a photo cannot be captured after it was uploaded, so
    ``shot_date >= date_added`` is rejected outright. That is measured, not
    defensive -- v2 serves an ingest timestamp as the capture date for Grab's
    2025-11-19 open-360 ingest, audited at 10 of 48 sequences and 5,665 photos,
    every one of them ``SPHERE`` / ``KartaCam2`` / uploader ``OpenStreetView``
    (docs/experiments/kartaview-shotdate-audit_metrics.json). A collector
    reading that cannot detect it by null-checking, because what it is handed is
    a non-null, entirely plausible timestamp. This endpoint is v1, which reports
    those photos as null today, so the invariant is what stops the same defect
    arriving through this door -- via a backfill, or a v2 migration -- rather
    than a guard against a value we currently see. It costs nothing.

    ``>=`` and not ``>``: 3 of the 10 audited sequences read ``shotDate ==
    dateAdded`` to the second (Langkawi 11616157: ``2025-11-19 11:18:29`` on
    both), so a strict ``>`` is exactly the near-miss guard that lets the bad
    data through. The price is dropping the date of a photo genuinely uploaded
    in the same second it was taken; that photo still counts as coverage, as
    NO_DATE. A null is honest and can be handled downstream; a wrong date that
    looks right cannot (#213).

    Scalar reference implementation for :func:`shot_dates_to_iso_dates`, which is
    what the collector actually calls -- a test pins the two together
    element-wise, so the rules live here in readable form and are stated once.
    """
    if not shot_date:
        return ""
    shot = _scalar_timestamp(shot_date)
    if pd.isna(shot):
        return ""
    if date_added:
        added = _scalar_timestamp(date_added)
        if pd.notna(added) and shot >= added:
            return ""
    if shot < pd.Timestamp(EARLIEST_CAPTURE_DATE):
        return ""
    if shot > pd.Timestamp(datetime.now(UTC).replace(tzinfo=None)) + _FUTURE_SLACK:
        return ""
    return shot.date().isoformat()


def _to_naive_datetime(values) -> pd.Series:
    """
    Parse a column of KartaView timestamps to naive (zone-less) datetimes.

    ``format="ISO8601"`` rather than inference: KartaView mixes precisions
    inside ONE page -- ``shot_date`` arrives as "2025-09-01 17:57:05.000" and
    ``date_added`` as "2025-09-20 21:08:37" -- and pandas infers a single format
    from the first non-null value, so with ``errors="coerce"`` every value at
    the other precision silently becomes NaT. Measured: a page whose first row
    carried milliseconds nulled the capture date of every row that did not.
    That is issue #226's failure arriving from a second direction -- the values
    are fine, the parse throws them away, and nothing raises.

    ``utc=True`` then ``tz_localize(None)`` so the result is always naive: see
    the caller for why an offset appearing in a future response would otherwise
    raise past the point of no return.
    """
    parsed = pd.to_datetime(
        pd.Series(values, dtype=object), format="ISO8601", errors="coerce", utc=True
    )
    return parsed.dt.tz_localize(None)


def shot_dates_to_iso_dates(shot_dates, dates_added) -> pd.Series:
    """
    Vectorized :func:`shot_date_to_iso_date` over two columns of timestamps.

    Same rules, same '' for anything unusable, applied to a whole census at once
    -- a census is millions of images (issue #157), and the scalar form is both
    a Python-level loop and a per-image object.

    Args:
        shot_dates: array-like of contributor EXIF timestamps, nulls allowed.
        dates_added: array-like of server upload timestamps, nulls allowed,
            aligned to ``shot_dates``.

    Returns:
        A str Series of 'YYYY-MM-DD' / '' values, aligned to the input.
    """
    # Both the ISO8601 pin (#226's mixed-precision trap) and the naive-UTC
    # normalization (a future offset would otherwise raise past the point where
    # the sweep has already been paid for) live in _to_naive_datetime.
    shot = _to_naive_datetime(shot_dates)
    added = _to_naive_datetime(dates_added)
    shot = shot.reset_index(drop=True)
    added = added.reset_index(drop=True)
    ceiling = pd.Timestamp(datetime.now(UTC).replace(tzinfo=None)) + _FUTURE_SLACK
    usable = (
        shot.notna()
        & shot.ge(pd.Timestamp(EARLIEST_CAPTURE_DATE))
        & shot.le(ceiling)
        # An absent upload time cannot falsify the invariant, so it does not
        # veto the capture date -- `added.isna()` is a pass, not a failure.
        & (added.isna() | shot.lt(added))
    )
    # Mask BEFORE formatting, exactly as the Mapillary parser does: pandas holds
    # timestamps far outside Python's datetime range quite happily and then
    # refuses to strftime them, and those are precisely the values `usable` has
    # already rejected.
    return shot.where(usable).dt.strftime("%Y-%m-%d").fillna("").astype(str)


# ── Census schema and the census.py bindings ───────────────────────────────


# Census columns, in decode order. Held COLUMN-WISE for the reasons in
# census.py: a list of per-image dicts costs ~0.74 GB per million images, and a
# dense sweep returns millions. Arrow-backed strings hold an id in about a byte
# per character rather than a ~57-byte Python str plus a pointer.
#
# `shot_date` and `date_added` are carried as strings rather than parsed here:
# the parse is one vectorized pass over the finished census
# (:func:`shot_dates_to_iso_dates`), and both raw values are published, so
# reducing them at decode would destroy the provenance the date rules exist to
# preserve.
_CENSUS_DTYPES = {
    "id": pd.StringDtype("pyarrow"),
    "lon": "float64",
    "lat": "float64",
    "shot_date": pd.StringDtype("pyarrow"),
    "date_added": pd.StringDtype("pyarrow"),
    "is_pano": "bool",
    "username": pd.StringDtype("pyarrow"),
    "sequence_id": pd.StringDtype("pyarrow"),
    "sequence_index": "Int64",
    "field_of_view": "float64",
    "compass_angle": "float64",
    "org_code": pd.StringDtype("pyarrow"),
    "way_id": pd.StringDtype("pyarrow"),
}


def _as_float(value: Any) -> float | None:
    """Provider strings -> float, with an unusable value becoming null."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decode_photo_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    One page of ``nearby-photos`` rows -> census records.

    EVERY field here is free: the v1 bulk row carries the projection, the drive
    id and index, the contributor, the field of view, the heading, the
    publisher code and the OSM way the photo snapped to, with no second request.
    That is the one respect in which this API is better than Mapillary's, so the
    posture is the same as MAPILLARY_EXTRA_DTYPES': capture what the response
    already contains rather than re-fetching it later.

    Two decisions worth stating:

    * ``is_pano`` is ``projection == "SPHERE"``, which is issue #116's split
      exactly. The projection is authoritative and free; ``field_of_view`` is
      published beside it rather than used to derive the flag.
    * The published position is ``lat``/``lng``, the camera's own GPS, NOT
      ``match_lat``/``match_lng``, which is where KartaView snapped the photo
      onto an OSM way. Publishing the snapped position would put every photo on
      a road by construction and quietly inflate street coverage -- the measure
      #99's road walk exists to make honest. ``way_id`` records the snap target
      itself, which is the useful half.
    """
    records = []
    for item in items:
        lat, lon = _as_float(item.get("lat")), _as_float(item.get("lng"))
        if lat is None or lon is None:
            # No position, no census row: it can be assigned to no grid point
            # and no sample point, so it would be an unusable row rather than a
            # missing one.
            continue
        image_id = item.get("id")
        if image_id is None:
            # Same rule for the id, and for a sharper reason than tidiness:
            # `id` is what census.dedupe_census keys on and what becomes
            # `pano_id`, which diff.py compares run to run. An id-less row can
            # be neither deduped nor diffed, so it is dropped here exactly as
            # download_mapillary.decode_image_features drops a feature with no
            # id. (census.dedupe_census is independently safe against nulls
            # now, but the census should not carry them in the first place.)
            continue
        sequence_index = item.get("sequence_index")
        records.append(
            {
                "id": str(image_id),
                "lon": lon,
                "lat": lat,
                "shot_date": item.get("shot_date") or None,
                "date_added": item.get("date_added") or None,
                "is_pano": (item.get("projection") or "").upper() == "SPHERE",
                "username": item.get("username") or None,
                "sequence_id": (
                    str(item.get("sequence_id")) if item.get("sequence_id") is not None else None
                ),
                # Int64 rather than the raw string: it is a position in a drive,
                # and pano-spacing analysis orders by it.
                "sequence_index": (
                    int(sequence_index)
                    if isinstance(sequence_index, (int, str)) and str(sequence_index).isdigit()
                    else None
                ),
                "field_of_view": _as_float(item.get("field_of_view")),
                # KartaView calls it `heading`; the column is named for what
                # Mapillary calls the same measurement, so the two providers'
                # runs answer a bearing question with one column name (#97).
                "compass_angle": _as_float(item.get("heading")),
                "org_code": item.get("orgCode") or None,
                "way_id": str(item.get("way_id")) if item.get("way_id") is not None else None,
            }
        )
    return records


def records_to_census(records: list[dict[str, Any]]) -> pd.DataFrame:
    """
    Turn one page's decoded records into a columnar census frame.

    Called per page, immediately after :func:`decode_photo_items`, so a page's
    dicts are freed before the next page is decoded rather than every page's
    surviving until the whole city has downloaded.

    KartaView's binding of :func:`census.records_to_census`.
    """
    return census_core.records_to_census(records, _CENSUS_DTYPES)


def concat_census(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Combine per-page census frames into one, preserving fetch order."""
    return census_core.concat_census(frames, _CENSUS_DTYPES)


def _kartaview_image_columns(picked: pd.DataFrame) -> dict[str, Any]:
    """
    KartaView's own output columns: the copyright convention plus its extras.

    Handed to :func:`census.build_image_rows`, which fills the shared core.
    """
    # The contributor is published both as its own column and inside the
    # copyright string, for parity with GSV's "(c) <photographer>" and
    # Mapillary's "(c) Mapillary contributor <id>". Going through the nullable
    # string dtype rather than astype(str) keeps a missing username missing
    # instead of rendering it "<NA>".
    #
    # The imagery is CC BY-SA 4.0, so this column is an attribution requirement
    # and not merely a provenance note -- unlike GSV, where it is the filter
    # that separates official drives from third-party photospheres. Nothing
    # downstream should filter KartaView rows on it.
    username = picked["username"].astype("string")
    return {
        "copyright_info": ("© KartaView contributor " + username)
        .fillna("© KartaView")
        .to_numpy(dtype=object),
        "username": username.to_numpy(dtype=object),
        "sequence_id": picked["sequence_id"].to_numpy(dtype=object),
        "sequence_index": picked["sequence_index"].astype(object).to_numpy(),
        "is_pano": picked["is_pano"].to_numpy(),
        "field_of_view": picked["field_of_view"].to_numpy(),
        "compass_angle": picked["compass_angle"].to_numpy(),
        # Upload time, published beside a capture_date that may be null BECAUSE
        # of it (see shot_date_to_iso_date). Keeping it is what makes the
        # invariant auditable after the fact, and what a future measurement of
        # the capture-to-upload lag would be built from.
        "date_added": picked["date_added"].to_numpy(dtype=object),
        "org_code": picked["org_code"].to_numpy(dtype=object),
        "way_id": picked["way_id"].to_numpy(dtype=object),
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
    KARTAVIEW_METADATA_DTYPES rows for query locations matched to census images.

    Shared by the grid downloader (query location = a frozen grid point) and the
    road-walk collector (query location = an on-street sample point).
    KartaView's binding of :func:`census.build_image_rows`; see there for the
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
        dtypes=KARTAVIEW_METADATA_DTYPES,
        image_columns=_kartaview_image_columns,
    )


def build_empty_rows(query_lat, query_lon, query_timestamp: str, status) -> pd.DataFrame:
    """
    Rows for query locations with no imagery -- the ZERO_RESULTS fill, plus the
    REQUEST_FAILED variant for points under a cell that never came back.

    KartaView's binding of :func:`census.build_empty_rows`.
    """
    return census_core.build_empty_rows(
        query_lat, query_lon, query_timestamp, status, dtypes=KARTAVIEW_METADATA_DTYPES
    )


# ── The request, and what its failures mean ────────────────────────────────


class BackpressureError(DownloadError):
    """
    The server declined the QUERY: HTTP 400 carrying apiCode 690 or 408.

    The remedy is to ask for less -- retry, then shrink the radius. Kept
    distinct from the two below because that remedy is exactly wrong for them:
    subdividing a circle after a timeout asks a struggling server for four
    requests where it just failed to serve one, which is the shape of the
    Mapillary incident (#198) rather than a fix for it.
    """


class TransportError(DownloadError):
    """The request never got an answer: connection reset, timeout, DNS."""


class ResponseError(DownloadError):
    """
    An answer arrived that we cannot use: a rejected credential, an unparseable
    body, an HTTP error that is not backpressure.

    Deliberately NOT a HostBlockedError, following the Mapillary precedent
    (#208): a 401/403 is scoped to the CREDENTIAL, and a channel split gives
    different channels different tokens, so typing it host-wide would let one
    channel's bad key skip another channel's cities for the whole night.
    """


async def _post_nearby(
    session: aiohttp.ClientSession,
    limiter: AsyncRateLimiter,
    count_request,
    lat: float,
    lon: float,
    radius_m: int,
    *,
    page: int = 1,
    ipp: int = IPP_MAX,
    access_token: str | None = None,
    timeout: aiohttp.ClientTimeout | None = None,
) -> tuple[list[dict], int | None]:
    """
    One radius-mode nearby-photos call.

    Pacing and request counting happen HERE, inside the body the caller retries,
    so a retried request takes its own token and is counted once: one token, one
    ledger increment, one HTTP request (#198/#203). Taking either in the caller
    lets a retrying cell present up to ``retries``x the configured rate during
    exactly a backpressure storm, and under-report the same factor to the daily
    ledger.

    Returns:
        ``(items, total_filtered_items)``. The total is None when the server sent
        something we could not parse, which is deliberately distinct from 0.

    Raises:
        BackpressureError: the query was too heavy; retry, then subdivide.
        HostBlockedError: the service is refusing this HOST -- a redirect to a
            login page, an HTML error page, or a 429. All three are properties
            of the machine rather than of this circle, so the caller stops.
        TransportError: no answer arrived.
        ResponseError: an answer arrived that we cannot use.
    """
    await limiter.acquire()
    count_request()
    params = {"access_token": access_token} if access_token else None
    data = {"lat": lat, "lng": lon, "radius": radius_m, "page": page, "ipp": min(ipp, IPP_MAX)}
    try:
        # allow_redirects=False: Mapillary's block manifested as a 302 to a login
        # page whose FOLLOWED body was a 200 text/html, which reached the decoder
        # and read as corrupt data (#199). A redirect here is named, not chased.
        async with session.post(
            NEARBY_PHOTOS_URL,
            data=data,
            params=params,
            timeout=timeout,
            allow_redirects=False,
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            # 401/403 is decided FIRST, before the content-type test below, and
            # the order is the whole point (the Mapillary precedent checks it
            # first for the same reason). kartaview.org serves a JS single-page
            # app from the very host this API lives on, so the natural shape of
            # a rejected or expired token is an HTML login page -- and so is a
            # load balancer's 502/503. Reading content-type first typed both as
            # a host refusal, which routes into #208's night-level breaker:
            # every remaining KartaView city skipped for the night, no failure
            # recorded, and an unconditional "host UNAVAILABLE" alert sending
            # the operator after a ban that never happened. A credential is
            # scoped to the CHANNEL, not the machine.
            if resp.status in (401, 403):
                raise ResponseError(
                    f"KartaView rejected the credential (HTTP {resp.status}, {content_type or '?'})."
                    " Check KARTAVIEW_ACCESS_TOKEN; this is scoped to the token, not to this host."
                )
            if 300 <= resp.status < 400:
                raise HostBlockedError(
                    f"KartaView redirected the request (HTTP {resp.status} -> "
                    f"{redact_credentials(resp.headers.get('Location', '?'))}); "
                    f"treating this host as refused",
                    HOST_KARTAVIEW,
                )
            # 5xx before the content-type test, and transient rather than
            # definite. An overloaded upstream answers with its load balancer's
            # HTML error page essentially always, so the check below would have
            # called an ordinary 502 a host refusal and skipped every remaining
            # KartaView city for the night. Typed as transport, it takes the
            # retry budget _probe_cell already gives a timeout or a reset, and
            # if it persists the cell is recorded as unmeasured -- never
            # subdivided, because a struggling server must not be asked for
            # four requests where it just failed to serve one (#198).
            if resp.status >= 500:
                raise TransportError(
                    f"KartaView server error (HTTP {resp.status}, {content_type or '?'})"
                )
            if "text/html" in content_type.lower():
                raise HostBlockedError(
                    f"KartaView served an HTML page (HTTP {resp.status}, {content_type}) "
                    f"where JSON was expected; treating this host as refused",
                    HOST_KARTAVIEW,
                )
            if resp.status == 429:
                raise HostBlockedError(
                    "KartaView returned HTTP 429 (rate limited); the documented "
                    "ceiling is hourly and per key, but nothing published says the "
                    "enforced one is, so this host stops asking",
                    HOST_KARTAVIEW,
                )
            status_code = resp.status
            body_text = await resp.text()
    except (TimeoutError, aiohttp.ClientError) as e:
        raise TransportError(
            f"transport failure: {type(e).__name__}: {redact_credentials(e)}"
        ) from e

    try:
        body = json.loads(body_text)
    except ValueError as e:
        raise ResponseError(f"non-JSON body (HTTP {status_code}, {content_type or '?'})") from e

    # A body that parsed but is not an object (a bare list, string or null) is
    # an answer we cannot use, not a crash: `.get` on it raises AttributeError,
    # which is neither DownloadError nor a transport error, so it would escape
    # _probe_cell and _fetch_city_images WITHOUT the api_requests the caller
    # needs to write its ledger row.
    if not isinstance(body, dict):
        raise ResponseError(
            f"body is {type(body).__name__}, not a JSON object (HTTP {status_code})"
        )

    status = body.get("status") or {}
    if not isinstance(status, dict):
        status = {}
    try:
        api_code = int(status.get("apiCode"))
    except (TypeError, ValueError):
        api_code = None

    # Checked BEFORE the status code, because backpressure ARRIVES as an HTTP
    # 400: reading the status first would file every overloaded query as a
    # permanent error and never retry or subdivide it.
    if api_code in BACKPRESSURE_API_CODES:
        raise BackpressureError(
            f"backpressure: apiCode {api_code} ({status.get('apiMessage', '')!r}) "
            f"at radius {radius_m} m"
        )
    if status_code >= 400:
        raise ResponseError(f"HTTP {status_code}, apiCode {api_code}, body keys {sorted(body)}")

    # The v1 envelope has varied across deployments; accept both shapes rather
    # than KeyError on a server that answered perfectly well.
    items = body.get("currentPageItems")
    if items is None:
        items = (body.get("osv") or {}).get("currentPageItems")
    if items is None:
        raise ResponseError(f"no currentPageItems in body (keys: {sorted(body)})")

    total = body.get("totalFilteredItems")
    if total is None:
        total = (body.get("osv") or {}).get("totalFilteredItems")
    # MEASURED 2026-08-18: the API returns this as a LIST HOLDING A STRING --
    # `['737']`, not `737`. A bare int() raises on that, and a fallback to
    # len(items) would silently report the page size as the circle's total: 5
    # instead of 737. Everything downstream is built on this number -- it is how
    # a circle is priced and how the sweep decides to page -- so unwrap before
    # coercing, and report an unusable value as unknown rather than as a count
    # we did not measure.
    if isinstance(total, (list, tuple)):
        total = total[0] if total else None
    try:
        total = int(total)
    except (TypeError, ValueError):
        logger.warning(f"unparseable totalFilteredItems {total!r}; treating as unknown")
        total = None

    return list(items), total


# ── Per-city radius calibration ────────────────────────────────────────────


def calibration_points(
    bbox: tuple[float, float, float, float], n: int
) -> list[tuple[float, float]]:
    """
    Spread ``n`` sample points across a bbox: the centre first, then inset corners.

    Deterministic, so a re-run calibrates identically. Centre-first because a
    one-point calibration should look at the middle of the city rather than at a
    corner that may be open water.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat, mid_lon = (min_lat + max_lat) / 2, (min_lon + max_lon) / 2
    # Inset the corners by a quarter so they sample the city, not its edge.
    qy, qx = (max_lat - min_lat) / 4, (max_lon - min_lon) / 4
    candidates = [
        (mid_lat, mid_lon),
        (mid_lat - qy, mid_lon - qx),
        (mid_lat + qy, mid_lon + qx),
        (mid_lat - qy, mid_lon + qx),
        (mid_lat + qy, mid_lon - qx),
    ]
    if n < 1:
        # 0 is the natural spelling of "don't calibrate", and it fails OPEN:
        # `answered == len(points)` is 0 == 0, so the first rung is accepted
        # having asked nothing -- and that rung is r=1000, which four of the
        # study's fourteen cities could not hold. Refuse instead; the way to
        # skip calibration is to pass radius_m.
        raise ValueError(f"calibration needs at least one probe, got {n}")
    return candidates[:n]


async def calibrate_radius(
    session: aiohttp.ClientSession,
    limiter: AsyncRateLimiter,
    count_request,
    bbox: tuple[float, float, float, float],
    *,
    ipp: int,
    access_token: str | None,
    probes_per_rung: int,
    retries: int,
    timeout: aiohttp.ClientTimeout | None = None,
) -> int | None:
    """
    Find the largest radius this city's server will actually answer.

    THE REASON THIS EXISTS. The working radius is a property of the LOCATION
    rather than of how much imagery is there, and it varies by at least 4x
    across the catalog (measured 2026-08-19: Ithaca MI answered r=1000 at 12 of
    12 cells; Horace ND, which holds no imagery at all, refused r=1000 on 10 of
    10 attempts across two page sizes while answering r=250 on 4 of 4). Density
    does not predict it -- Seattle held r=1000 at a higher photo density than
    New York or Manila, both of which calibrated down to 500.

    Discovering it per cell instead pays the cascade at EVERY cell: a root that
    will not answer costs its retries plus 1 + 4 + 16 to the floor, and pays it
    again for each of the city's other roots. One calibration up front is paid
    once for the whole city.

    COST. A rung is accepted only if EVERY probe on it answers, so the moment
    one probe fails the rung is already lost and the remaining probes on it are
    pure waste -- hence the break. With that, the bound is
    ``len(RADIUS_LADDER_M) * (probes_per_rung + retries)`` requests, because a
    rung costs either ``probes_per_rung`` answers or one probe's full retry
    budget: 6 * (2 + 3) = **30** at the defaults, worst case, for a city where
    nothing answers anywhere. The docstring here, the module docstring and
    CLAUDE.md all previously said "at most 12" -- that was
    ``rungs * probes_per_rung``, which silently assumed every probe costs one
    request when a refused probe costs ``retries + 1``. Measured before the
    break: 48. The number matters because it is fixed overhead against a median
    city of 12 requests, and a scheduler that derives a timeout from the
    estimate does not count it at all.

    Returns:
        The calibrated radius, or None when no rung answered anywhere -- which
        is NOT the same as "no imagery here" and must never be recorded as an
        empty city.

    Raises:
        ResponseError: the server gave a definite, unusable answer at every
            probe -- overwhelmingly a rejected credential. Surfaced as itself
            rather than folded into the None above, because "no radius answers
            in this bbox" is a property of the LOCATION and sends the operator
            to look at the city; a 401 is a property of the token and sends
            them to the .env. They are not the same fact and the message the
            caller prints must not claim the wrong one.
    """
    points = calibration_points(bbox, probes_per_rung)
    saw_only_broken = True
    for radius in RADIUS_LADDER_M:
        answered = 0
        for lat, lon in points:
            _, _, outcome = await _probe_cell(
                session,
                limiter,
                count_request,
                Cell(lat=lat, lon=lon, size_m=radius * math.sqrt(2)),
                ipp=ipp,
                access_token=access_token,
                retries=retries,
                timeout=timeout,
            )
            if outcome != "broken":
                saw_only_broken = False
            if not outcome.startswith("ok"):
                # The rung needs every probe, so it is already lost.
                break
            answered += 1
        logger.info(f"  calibrate r={radius} m: {answered}/{len(points)} answered")
        if answered == len(points):
            return radius
    if saw_only_broken:
        raise ResponseError(
            "KartaView gave no usable answer at any radius or any calibration point "
            "(every probe a definite error rather than backpressure); this is the "
            "credential or the endpoint, not the city's geometry"
        )
    return None


async def _probe_cell(
    session: aiohttp.ClientSession,
    limiter: AsyncRateLimiter,
    count_request,
    cell: Cell,
    *,
    page: int = 1,
    ipp: int,
    access_token: str | None,
    retries: int,
    timeout: aiohttp.ClientTimeout | None = None,
) -> tuple[list[dict], int | None, str]:
    """
    One page of one cell. Returns ``(items, total, outcome)``.

    ``outcome`` is ``ok`` / ``ok_after_retry`` / ``refused`` / ``broken``.

    RETRYING IS FOUR TIMES CHEAPER THAN SUBDIVIDING, which is why the retry
    budget is generous and why this is a function rather than a loop inline. A
    retry costs one request; a subdivision costs four, each of which may retry
    and subdivide in turn -- a cascade to the radius floor is 1 + 4 + 16 = 21
    requests. apiCode 690 was measured flaky, so a cell that clears on any retry
    saves at least 20 (88 of 174 retries cleared during the sweep study).

    ``broken`` is kept apart from ``refused`` deliberately. Only a
    BackpressureError means "you asked for too much", so only it may subdivide.

    The two non-backpressure classes are then retried differently, because they
    are different facts. A TransportError -- reset, timeout, DNS -- is transient
    by nature and gets the same retry budget. A ResponseError is the server
    giving a definite answer we cannot use (a rejected token, an unparseable
    body); re-asking cannot change it, and a rejected credential re-asked at
    every cell of every city is a good way to look like an attack.

    A HostBlockedError is not caught at all: it is a property of the machine, so
    the sweep stops rather than working through its remaining cells to learn
    what the first refusal already said (#205).
    """
    for attempt in range(retries + 1):
        try:
            items, total = await _post_nearby(
                session,
                limiter,
                count_request,
                cell.lat,
                cell.lon,
                cell.radius_m,
                page=page,
                ipp=ipp,
                access_token=access_token,
                timeout=timeout,
            )
        except BackpressureError as e:
            logger.debug(f"r={cell.radius_m} m @ {cell.lat:.4f},{cell.lon:.4f}: {e}")
            continue
        except TransportError as e:
            logger.debug(f"r={cell.radius_m} m @ {cell.lat:.4f},{cell.lon:.4f}: transport: {e}")
            if attempt == retries:
                return [], None, "broken"
            continue
        except ResponseError as e:
            logger.warning(
                f"r={cell.radius_m} m @ {cell.lat:.4f},{cell.lon:.4f}: neither backpressure "
                f"nor transient, NOT retrying and NOT subdividing: {e}"
            )
            return [], None, "broken"
        return items, total, ("ok_after_retry" if attempt else "ok")
    return [], None, "refused"


# ── The sweep ──────────────────────────────────────────────────────────────


def _bbox_area_m2(bbox: tuple[float, float, float, float]) -> float:
    """
    Bbox area, the denominator of the unmeasured-fraction guard.

    Uses the same antimeridian unwrap as :func:`cells_for_bbox`. Taking the raw
    ``max_lon - min_lon`` here was the second half of the wrap bug: on a wrapped
    bbox it is about -359.6 deg, and the ``abs()`` below turned that into an
    area of ~1.5 million km2, so ANY number of failed cells divided down to
    nothing and the failed-area guard could not fire.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = (min_lat + max_lat) / 2.0
    height = (max_lat - min_lat) * _METERS_PER_DEG_LAT
    width = _lon_span_deg(min_lon, max_lon) * _METERS_PER_DEG_LAT * math.cos(math.radians(mid_lat))
    return abs(width * height)


async def fetch_city_images_async(
    city_name: str,
    bbox: tuple[float, float, float, float],
    access_token: str | None = None,
    *,
    ipp: int = IPP_MAX,
    radius_m: int | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_requests_per_minute: int = DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
    max_requests: int | None = None,
    retries: int = DEFAULT_BACKPRESSURE_RETRIES,
    calibration_probes: int = DEFAULT_CALIBRATION_PROBES,
) -> dict[str, Any]:
    """
    Fetch a city's KartaView census, serialized against other processes.

    Every KartaView request in the repo passes through here -- the grid run and
    the road walk both -- which is what makes this the one place the
    machine-wide lock has to be taken. The documented ceiling is per key, but
    nothing published says the ENFORCED one is; both of this project's prior
    bans were on undocumented per-IP limits, and a per-IP limit is a property of
    the machine that no per-process limiter can honour alone (#208).

    See :func:`_fetch_city_images` for the arguments and return value.

    Raises:
        HostBusyError: another process on this machine is already sweeping.
            Raised before any request is issued.
    """
    with host_lock(HOST_KARTAVIEW):
        return await _fetch_city_images(
            city_name,
            bbox,
            access_token,
            ipp=ipp,
            radius_m=radius_m,
            request_timeout=request_timeout,
            max_requests_per_minute=max_requests_per_minute,
            max_requests=max_requests,
            retries=retries,
            calibration_probes=calibration_probes,
        )


async def _fetch_city_images(
    city_name: str,
    bbox: tuple[float, float, float, float],
    access_token: str | None = None,
    *,
    ipp: int = IPP_MAX,
    radius_m: int | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_requests_per_minute: int = DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
    max_requests: int | None = None,
    retries: int = DEFAULT_BACKPRESSURE_RETRIES,
    calibration_probes: int = DEFAULT_CALIBRATION_PROBES,
) -> dict[str, Any]:
    """
    Sweep a bbox with overlapping circles and return every KartaView photo in it.

    Extracted from the grid path the same way Mapillary's tile fetch was, so the
    road-walk collector shares this exact fetch and decode: the two differ only
    in what they assign images TO afterwards -- a regular grid lattice for a
    run, on-street sample points for a walk.

    The walk is SERIAL and depth-first: calibrate the radius once, tile the bbox
    at it, and descend only where a cell refuses. Serial because pacing is the
    bottleneck at any sane rate, because the next question genuinely depends on
    the last answer, and because fanning out into a server that answers HTTP 400
    under load is the shape of the incident this project already had (#198).

    Args:
        city_name: label for logging/progress only.
        bbox: (min_lon, min_lat, max_lon, max_lat), e.g. from grid_bbox.
        access_token: KartaView token; None sweeps anonymously at a tenth of the
            hourly ceiling, which no cadence can live on (see load_config).
        ipp: items per page; the server caps it at IPP_MAX and so do we.
        radius_m: skip calibration and tile at this radius. Pass a previous
            run's calibrated value; leave None to measure it.
        request_timeout: per-request timeout in seconds.
        max_requests_per_minute: client-side pacing. <= 0 disables it (tests).
        max_requests: runaway guard, not a sampling knob. A sweep that hits it
            leaves the rest of the bbox UNMEASURED, which all but guarantees the
            failed-area check below refuses to finalize the snapshot -- which is
            the point: a partial census must not be published as a dated one.
        retries: backpressure/transport retries before a cell is subdivided.
        calibration_probes: points per rung; a rung passes only if all answer.

    Returns:
        Dict with ``census`` (the deduped columnar census), ``api_requests``
        (every request issued, calibration included), ``cells`` (root cells),
        ``cells_visited`` (roots plus every subdivision), ``radius_m`` (what the
        sweep tiled at), ``raw_photo_count`` (pre-dedupe) and ``failed_cells``.

    Raises:
        DownloadError: on a rejected credential, a city where no radius answers
            anywhere, or a sweep that left more than
            :data:`MAX_FAILED_AREA_FRACTION` of the bbox unmeasured. Carries
            ``api_requests`` so the caller can still record what it spent.
        HostBlockedError: KartaView is refusing this host. Raised at the FIRST
            refusal rather than after the whole bbox has been paid for (#205).
    """
    api_requests = 0

    def count_request() -> None:
        nonlocal api_requests
        api_requests += 1

    def spent(error: DownloadError) -> DownloadError:
        """Attach the spend so the caller can still write the ledger row."""
        error.api_requests = api_requests
        return error

    def over_budget() -> bool:
        """Has the runaway guard tripped? Asked everywhere a request is issued."""
        return max_requests is not None and api_requests >= max_requests

    # Clamped ONCE, here, so the page arithmetic and the wire agree. _post_nearby
    # sends min(ipp, IPP_MAX) because the server caps it there, but
    # pages_for_total was priced from the caller's value -- so ipp=8000 asked
    # for one page of a circle holding 8,000 photos, got 2,000, and recorded no
    # failed cell and no warning. 6,000 photos silently absent from a snapshot
    # that publishes as complete.
    if ipp > IPP_MAX:
        logger.warning(f"ipp={ipp} exceeds the server cap; using {IPP_MAX}")
        ipp = IPP_MAX

    limiter = AsyncRateLimiter(max_requests_per_minute)
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    logger.info(
        f"Pacing KartaView requests at {max_requests_per_minute}/min"
        if max_requests_per_minute > 0
        else "KartaView pacing DISABLED (max_requests_per_minute <= 0)"
    )

    frames: list[pd.DataFrame] = []
    failed_cells: list[Cell] = []
    raw_photo_count = cells_visited = 0

    try:
        async with aiohttp.ClientSession() as session:
            if radius_m is None:
                logger.info(f"Calibrating KartaView sweep radius for {city_name}")
                radius_m = await calibrate_radius(
                    session,
                    limiter,
                    count_request,
                    bbox,
                    ipp=ipp,
                    access_token=access_token,
                    probes_per_rung=calibration_probes,
                    retries=retries,
                    timeout=timeout,
                )
            if radius_m is None:
                # NOT a host condition, deliberately. A host block shows up as a
                # redirect, an HTML page or a 429 and is typed where it happens;
                # every rung refusing at every point in ONE bbox is a property
                # of that location (Horace ND refused r >= 250 while holding no
                # imagery at all), and typing it host-wide would let one such
                # city skip every other city's KartaView channel for the night.
                # It is also NOT an empty city: refused and empty are different
                # facts, and conflating them publishes a 0-pano census.
                raise spent(
                    DownloadError(
                        f"KartaView answered no radius at any calibration point for "
                        f"{city_name} (tried {RADIUS_LADDER_M} m at {calibration_probes} "
                        f"point(s) each); refusing to treat a refusal as an empty city"
                    )
                )

            roots = cells_for_bbox(*bbox, radius_m * math.sqrt(2))
            logger.info(
                f"Sweeping KartaView for {city_name}: {len(roots)} cells at r={radius_m} m "
                f"covering bbox {tuple(round(v, 4) for v in bbox)}"
            )
            progress_bar = progress(
                total=len(roots),
                desc=f"Sweeping KartaView circles for {city_name}",
                unit="cell",
                # Paced at ~16 requests/min, so a large city is hours of
                # deliberately slow fetching under the scheduler's redirected
                # log. A healthy run has to print, or "hung" and "slow" are
                # indistinguishable after a SIGKILL (issue #157).
                logger=logger,
            )
            budget_stop = False
            try:
                for index, root in enumerate(roots):
                    if over_budget():
                        # Everything not yet visited is unmeasured, not empty.
                        failed_cells.extend(roots[index:])
                        logger.warning(
                            f"Stopped after {api_requests} requests (max_requests="
                            f"{max_requests}); {len(roots) - index} of {len(roots)} cells "
                            f"never visited"
                        )
                        break
                    stack = [root]
                    while stack:
                        # The guard is checked HERE, and again in the page loop
                        # below, not only at the root boundary. Checked only
                        # there it bounded nothing: one root can cascade to the
                        # radius floor (1 + 4 + 16 + 64 = 85 cells, each up to
                        # retries + 1 attempts and MAX_PAGES_PER_CELL pages) and
                        # a page count comes from a SERVER-supplied total, so a
                        # single root could spend thousands of requests without
                        # the loop ever asking again. Measured before this fix:
                        # max_requests=5 issued 500 requests. The scheduler
                        # hands sibling channels the remaining daily budget in
                        # exactly this parameter, so the overrun would be spent
                        # against a per-IP-metered host.
                        if over_budget():
                            failed_cells.extend(stack)
                            budget_stop = True
                            break
                        cell = stack.pop()
                        cells_visited += 1
                        items, total, outcome = await _probe_cell(
                            session,
                            limiter,
                            count_request,
                            cell,
                            ipp=ipp,
                            access_token=access_token,
                            retries=retries,
                            timeout=timeout,
                        )
                        if outcome == "broken":
                            failed_cells.append(cell)
                            continue
                        if outcome == "refused":
                            if not can_subdivide(cell):
                                # Below the floor a refusal is a defect rather
                                # than backpressure: the smallest rung answered
                                # at every target the feasibility study probed.
                                failed_cells.append(cell)
                                continue
                            stack.extend(subdivide(cell))
                            continue

                        raw_photo_count += len(items)
                        # Converted to columns HERE, per page, so one page's
                        # dicts are freed before the next is decoded rather than
                        # the whole city's surviving to the end (#157).
                        frames.append(records_to_census(decode_photo_items(items)))

                        pages = pages_for_total(total, ipp)
                        if pages > MAX_PAGES_PER_CELL:
                            # Deep paging is untested past page 7; four
                            # shallower circles cost less risk than one long
                            # descent. Page 1 is kept rather than discarded --
                            # it is already paid for, and the children's
                            # overlap is deduped by id anyway.
                            if can_subdivide(cell):
                                stack.extend(subdivide(cell))
                                continue
                            # At the floor there are no shallower circles to
                            # fall back to, and the `and can_subdivide(cell)`
                            # this replaces made the cap a NO-OP there: control
                            # fell through and paged to a server-supplied total
                            # with no ceiling at all. A 100 m circle claiming a
                            # million items paged 500 times at 16/min. A cell
                            # we cannot exhaust is unmeasured area, which is
                            # what failed_cells means -- "refuse a hole"
                            # rather than silently accept a truncated circle.
                            logger.warning(
                                f"r={cell.radius_m} m @ {cell.lat:.4f},{cell.lon:.4f} needs "
                                f"{pages} pages at the radius floor and cannot be split; "
                                f"recording it as unmeasured"
                            )
                            failed_cells.append(cell)
                            continue

                        for page in range(2, pages + 1):
                            if over_budget():
                                failed_cells.append(cell)
                                budget_stop = True
                                break
                            items, _, outcome = await _probe_cell(
                                session,
                                limiter,
                                count_request,
                                cell,
                                page=page,
                                ipp=ipp,
                                access_token=access_token,
                                retries=retries,
                                timeout=timeout,
                            )
                            if outcome == "refused":
                                # Backpressure, and ONLY backpressure, may
                                # subdivide: a partially paged circle is not
                                # exhaustive, so the area is re-covered as four
                                # smaller ones rather than accepted short.
                                if can_subdivide(cell):
                                    stack.extend(subdivide(cell))
                                else:
                                    failed_cells.append(cell)
                                break
                            if outcome == "broken":
                                # A transport fault or a definite unusable
                                # answer is NOT backpressure, so it must not
                                # fan out -- asking the server for four
                                # requests where it just failed to serve one is
                                # #198's shape, not a fix for it. This branch
                                # used to share the `refused` path and so
                                # cascaded all the way to the floor: measured,
                                # a rejected credential on page 2 of one root
                                # cost 42 requests and a single TCP reset cost
                                # 105, while the module's own docstrings
                                # promised "asked exactly once" and "recorded
                                # as a failed cell".
                                failed_cells.append(cell)
                                break
                            raw_photo_count += len(items)
                            frames.append(records_to_census(decode_photo_items(items)))
                    if budget_stop:
                        failed_cells.extend(roots[index + 1 :])
                        logger.warning(
                            f"Stopped mid-cell after {api_requests} requests (max_requests="
                            f"{max_requests}); {len(roots) - index - 1} of {len(roots)} root "
                            f"cells never visited"
                        )
                        progress_bar.update(1)
                        break
                    progress_bar.update(1)
            finally:
                progress_bar.close()
    except HostBlockedError as e:
        # Nothing else in the bbox can answer differently, so the sweep stops
        # here rather than paying for the rest of the city to learn what the
        # first refusal already said (#205). Serial fetching makes that exact:
        # the spend is the requests issued up to and including the refusal.
        raise spent(e) from None
    except DownloadError as e:
        # Every exit carries the spend, including one raised deeper (a rejected
        # credential surfaced by calibrate_radius). Without it the caller writes
        # no api_usage row for requests that were genuinely issued.
        raise spent(e) from None
    except (TimeoutError, aiohttp.ClientError) as e:
        raise spent(DownloadError(f"KartaView sweep failed: {redact_credentials(e)}")) from e

    if failed_cells:
        # Clamped at 1.0 for the message only. The lattice deliberately
        # over-covers -- ceil() in both axes, and each cell is a square the bbox
        # edge cuts through -- so summed cell area exceeds bbox area, and a
        # small city whose every cell failed reported "161% unmeasured". The
        # over-count is in the safe direction (it can only refuse too eagerly,
        # never too late), so the guard keeps the raw ratio and only the printed
        # figure is capped.
        unmeasured = min(
            1.0, sum(c.size_m**2 for c in failed_cells) / max(_bbox_area_m2(bbox), 1.0)
        )
        detail = (
            f"{len(failed_cells)} cell(s) never answered, leaving {unmeasured:.1%} of "
            f"{city_name}'s bbox unmeasured"
        )
        if unmeasured > MAX_FAILED_AREA_FRACTION:
            raise spent(
                DownloadError(
                    f"KartaView sweep failed: {detail} "
                    f"(> {MAX_FAILED_AREA_FRACTION:.0%} tolerated); refusing to "
                    f"finalize an incomplete snapshot"
                )
            )
        # Under the threshold the run continues, but the caller must mark the
        # affected query points REQUEST_FAILED rather than let them look like
        # genuine no-imagery -- the same shape as an undownloaded Mapillary tile.
        logger.warning(f"Continuing with {detail}; affected query points marked REQUEST_FAILED")

    census = concat_census(frames)
    del frames
    census = census_core.dedupe_census(census)

    logger.info(
        f"Swept {cells_visited} cells ({len(roots)} roots at r={radius_m} m) for {city_name}: "
        f"{raw_photo_count} photo rows, {len(census)} unique, {api_requests} requests"
    )
    return {
        "census": census,
        "api_requests": api_requests,
        "cells": len(roots),
        "cells_visited": cells_visited,
        "radius_m": radius_m,
        "raw_photo_count": raw_photo_count,
        # Cells nothing came back for. Empty on a clean sweep; the caller
        # attributes the query points inside them to REQUEST_FAILED.
        "failed_cells": failed_cells,
    }
