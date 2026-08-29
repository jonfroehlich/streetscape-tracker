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
BBOX AREA, not by how much imagery it holds.

QUOTE THE OBSERVED COST, NOT THE FLOOR. :func:`estimate_sweep_requests` counts
the LATTICE -- circles, not requests -- and the study measured it 1.54x too low
in aggregate (19,173 against 29,589), because the floor prices neither the
backpressure retries nor the calibration ladder. The two columns, per the cost
study: the median catalog city is 12 circles and cost 16 requests (58 s), the
p95 is 384 circles and cost 636, and Singapore is 5,130 circles and cost ~9,974
(~10.4 h at the pace above). Budget against the observed figure with the study's
median 1.80x overhead applied to the cell count; use the bare lattice count only
where the thing being described really is the lattice.

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

CHECKPOINTING (issue #239). A sweep is HOURS at that pace -- Singapore is
~9,974 requests, i.e. ~10.4 h -- so an interruption that discarded it would be
the most expensive failure in the repo. ``fetch_city_images_async`` therefore
takes a ``checkpoint_path`` and commits what it has answered as it goes, so a
SIGKILL, a ``systemctl stop``, an OOM or a crash in the caller's tail means
"resume tomorrow" rather than "lose the night". The last of those four is why
the caller, not this module, calls :func:`discard_checkpoint`: the census is
returned as a DataFrame and the artifact is written afterwards, so a checkpoint
deleted before returning would cover every interruption except the one that
happens after it. The immutable dated-snapshot contract is untouched: a partial
sweep is never a run, and becomes one only when the lattice is complete, dated
on the day it completes. See the checkpoint section below -- the file format,
the fetch-order contract, the commit cadence and the age bound are all
constraints rather than choices.

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
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import census as census_core
from .analysis import EARLIEST_PLAUSIBLE_CAPTURE
from .checkpointing import (
    CENSUS_CACHE_FORMAT_VERSION,
    CENSUS_REUSE_MAX_AGE_S,
    load_census_cache_marker,
    promote_checkpoint_to_cache,
)
from .checkpointing import (
    CHECKPOINT_DIR_ENV as CHECKPOINT_DIR_ENV,
)
from .checkpointing import (
    CHECKPOINT_MAX_AGE_S as CHECKPOINT_MAX_AGE_S,
)
from .checkpointing import (
    CHECKPOINT_STATE_FILENAME as CHECKPOINT_STATE_FILENAME,
)
from .checkpointing import (
    _bbox_matches as _bbox_matches,
)
from .checkpointing import (
    _fsync_dir as _fsync_dir,
)
from .checkpointing import (
    _remove_empty_checkpoint_dir as _remove_empty_checkpoint_dir,
)
from .checkpointing import (
    _state_path as _state_path,
)
from .checkpointing import (
    checkpoint_dir as checkpoint_dir,
)
from .checkpointing import (
    checkpoint_path_for as checkpoint_path_for,
)
from .checkpointing import (
    discard_checkpoint as discard_checkpoint,
)
from .config import KARTAVIEW_METADATA_DTYPES
from .download_common import (
    HOST_KARTAVIEW,
    AsyncRateLimiter,
    DownloadError,
    HostBlockedError,
    grid_bbox,
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

# ── Checkpointing (issue #239) ─────────────────────────────────────────────
#
# A sweep is HOURS of paced fetching -- Singapore is ~9,974 requests, i.e. ~10.4 h
# at the rate above -- and until this existed, any interruption discarded every
# request it had already paid for. A SIGKILL from the scheduler's per-city
# timeout, a `systemctl stop`, an OOM, a crash in the caller's tail: all of them
# started the next attempt back at cell zero.
#
# EVERY SINGAPORE FIGURE IN THIS SECTION IS `sweep_requests_observed`, NEVER THE
# GEOMETRIC FLOOR. The cost study's floor column reads 7,329 for the same city and
# is the number docs/census.md says not to quote, because it prices neither the
# backpressure retries nor the calibration ladder (the study's own overhead ratio
# is 1.54x). Singapore is 5,130 root cells / 9,974 requests / ~10.4 h; see
# docs/experiments/kartaview-sweep-cost.md, "Finding 3".
#
# READ THIS BEFORE "SIMPLIFYING" THE COMMIT INTO THE `finally`. The periodic
# in-sweep commit IS the feature. `cli.py` installs no SIGTERM handler, and
# Python's default disposition terminates the process without unwinding, so
# `finally` does NOT run on the most common deliberate interruption -- nor on a
# SIGKILL from a timeout, nor on an OOM kill. The `finally`-commit is a bonus
# that catches a host block and stray exceptions; it is not the mechanism.
#
# The commit cadence is measured in REQUESTS, not in root cells, because a root
# does not cost a fixed amount: one that answers cleanly is a single request
# (~3.75 s at 16/min), while one that cascades to the radius floor is
# 1 + 4 + 16 + 64 = 85 cells at up to `retries + 1` attempts each -- ~340
# requests, ~21 minutes. Flushing per root would therefore have both a WORSE
# worst case and one part file per ROOT -- 5,130 of them for Singapore, which is
# its root-cell count and not its request count; the two differ by ~2x and it is
# the cells that would each get a file.
# v2 (issue #272): the commit record gained `created_at`, and the age cap moved
# from `updated_at` onto it. A v1 record cannot be read forward -- it has no
# first-commit stamp at all, and adopting `updated_at` in its place would be
# exactly the bug the bump exists to fix -- so an old checkpoint is discarded by
# the existing format-mismatch arm and its sweep restarts. That costs at most one
# in-flight city, once, on the build that ships this.
CHECKPOINT_FORMAT_VERSION = 2
CHECKPOINT_PART_TEMPLATE = "part-{index:05d}.parquet"

# Requests between commits. 32 is two minutes at the shipped pace, which bounds
# lost work at `interval + one root's worst case` (the check is only evaluated at
# a root boundary) and keeps the part count at ~0 for the median catalog city
# (16 requests), ~20 at the p95 (636) and ~310 for Singapore (9,974).
#
# Note what a SMALL value means, since it is the opposite of the natural reading:
# the cadence test is `api_requests - requests_at_last_commit >= interval` and no
# root costs zero requests, so anything <= 1 -- including 0 and any negative --
# commits at every root boundary, i.e. the per-root flushing the paragraph above
# argues against. It is not a way to disable checkpointing; that is
# `checkpoint_path=None`.
DEFAULT_CHECKPOINT_REQUEST_INTERVAL = 32


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
    # The latitude whose cos() priced this cell's LON EXTENT in degrees. The
    # lattice is laid out at the bbox's mid-latitude, so a cell's share of it
    # is size_m / cos(mid_lat) degrees wide -- NOT size_m / cos(cell.lat),
    # which on the equator-ward rows of a tall bbox is a few metres narrower
    # and left an unmasked sliver between adjacent failed cells (a grid point
    # there published ZERO_RESULTS, absence never observed). Roots record the
    # lattice's mid-latitude and subdivide passes it down, so descendants tile
    # exactly the share their root owns; None (hand-built cells, checkpoints
    # from before this field) falls back to `lat`, the historical behaviour.
    lon_extent_lat: float | None = None

    @property
    def radius_m(self) -> int:
        return int(round(self.size_m * math.sqrt(2) / 2))

    @property
    def extent_lat(self) -> float:
        """The latitude this cell's lon extent is priced at (see above)."""
        return self.lat if self.lon_extent_lat is None else self.lon_extent_lat


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
            lon_extent_lat=mid_lat,
        )
        for j in range(n_y)
        for i in range(n_x)
    ]


def subdivide(cell: Cell) -> list[Cell]:
    """
    Split one cell into the four half-size cells that exactly cover it.

    "Cover it" means the cell's share of the LATTICE, so the lon step is
    priced at the parent's ``extent_lat`` and the children inherit it: by
    induction every descendant tiles exactly the share its root owns, instead
    of a share re-priced at each generation's own latitude -- which on a tall
    bbox drifted a few metres from the lattice and out of the failed-cell
    mask.
    """
    half = cell.size_m / 2.0
    extent_lat = cell.extent_lat
    d_lat = (half / 2.0) / _METERS_PER_DEG_LAT
    d_lon = (half / 2.0) / (_METERS_PER_DEG_LAT * math.cos(math.radians(extent_lat)))
    return [
        Cell(cell.lat + sy * d_lat, cell.lon + sx * d_lon, half, cell.depth + 1, extent_lat)
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


def _kartaview_capture_dates(census_frame: pd.DataFrame, positions: np.ndarray) -> np.ndarray:
    """
    Capture dates for the census rows at ``positions``, per KartaView's rules.

    Handed to :func:`census_core.write_census_grid_run`. Takes positions rather
    than a taken sub-frame so this indexes the TWO columns it needs -- a
    whole-frame ``.take()`` here would materialize every column of a
    multi-million-row census a second time (issue #157).

    Per-column ``Series.take(positions)``, not ``.to_numpy()[positions]``: the
    census columns are Arrow-backed strings (~a byte per character), and
    ``.to_numpy()`` converts the ENTIRE column to a numpy object array of
    Python strings -- tens of bytes per value, for every row in the census --
    before the positional index throws almost all of it away. ``take`` selects
    first, inside Arrow.

    Two columns rather than Mapillary's one because the rule needs both: a
    ``shot_date`` at or after its ``date_added`` is the upload timestamp being
    served as a capture date, and :func:`shot_dates_to_iso_dates` rejects it
    (that function resets both series' indexes, so the taken positions do not
    survive as labels). They are never merged into a fallback -- see
    :func:`shot_date_to_iso_date`.
    """
    return shot_dates_to_iso_dates(
        census_frame["shot_date"].take(positions),
        census_frame["date_added"].take(positions),
    ).to_numpy()


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


# ── Grid assignment ────────────────────────────────────────────────────────


def _points_in_cells(lats: np.ndarray, lons: np.ndarray, cells: list[Cell]) -> np.ndarray:
    """
    Boolean mask of which (lat, lon) points fall inside any of ``cells``.

    Used to attribute unmeasured cells back to the grid points they cover, so
    those points become REQUEST_FAILED rather than ZERO_RESULTS -- the same job
    ``download_mapillary._points_in_tiles`` does for undownloaded tiles (#168).
    Erring toward "unknown" is the point: recording an unswept point as empty
    publishes an absence we never observed into an immutable dated snapshot.

    Tested against each cell's own SQUARE, not its circumscribed circle. The
    circle is what the request covered and is 1.57x the area, so masking with it
    would mark points in neighbouring cells -- which were measured, by their own
    request -- as unknown. The square is the cell's share of the lattice.

    Deliberately a loop over cells rather than one packed lookup: subdivision
    means cells are NOT one size (that is the whole difference from the tile
    case), and the list is bounded by MAX_FAILED_AREA_FRACTION anyway.
    """
    if len(lats) == 0:
        return np.zeros(0, dtype=bool)
    mask = np.zeros(len(lats), dtype=bool)
    for cell in cells:
        half_lat = (cell.size_m / 2.0) / _METERS_PER_DEG_LAT
        # extent_lat, not cell.lat: the cell's share of the lattice was priced
        # at the lattice's mid-latitude, and re-pricing it here at the cell's
        # own row left an unmasked sliver between adjacent failed cells on the
        # equator-ward rows of a tall bbox (~2 m wide at 47 degN over 40 km).
        half_lon = (cell.size_m / 2.0) / (
            _METERS_PER_DEG_LAT * math.cos(math.radians(cell.extent_lat))
        )
        # Wrapped difference, so a cell beside the antimeridian compares against
        # points on the other side of it rather than against a ~360 deg gap.
        d_lon = ((lons - cell.lon + 180.0) % 360.0) - 180.0
        mask |= (np.abs(lats - cell.lat) <= half_lat) & (np.abs(d_lon) <= half_lon)
    return mask


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


class CredentialRejectedError(ResponseError):
    """
    The server rejected the token itself: HTTP 401/403.

    The one ResponseError that fails the SWEEP rather than the cell. Every
    other definite answer is a property of one query -- an unparseable body at
    cell N says nothing about cell N+1 -- but a dead token answers identically
    everywhere, so recording it per cell re-asks a rejected credential at
    every remaining cell of every page (a full lattice's worth of requests to
    learn what request one already said, which is also a good way to look like
    an attack). ``_probe_cell`` re-raises it instead of returning ``broken``;
    the checkpoint's finally-commit keeps the spend.

    Still a ResponseError and still NOT host-typed, per the class above: the
    token is scoped to the CHANNEL, so a sibling channel's cities keep running.
    """


class SweepIncompleteError(DownloadError):
    """
    The sweep stopped with root cells unvisited, and CHECKPOINTED them (#239).

    Nothing is finalized -- a partial census must never be published as a dated
    snapshot, because an immutable dated file holding 60% of a city diffs
    against its predecessor as "every pano in the rest of the city removed".
    What is different from every other failure here is that the spend survives:
    the answered cells are on disk and the next attempt resumes from them.

    Deliberately NOT a ``HostUnavailableError``, and the distinction is not
    academic. ``host_exit_code`` maps those to 81 for KartaView, which the
    scheduler turns into a night-level breaker skipping every remaining
    KartaView city -- correct for a refusal, which is a property of the machine,
    and wrong for this, which is a property of THIS city's budget. The next
    city's sweep is unaffected and should run.

    This is why ``download_gsv_history``'s ``HarvestIncompleteError``, which
    subclasses its blocked error, is the wrong precedent to copy: that harvester
    is a manual script the scheduler never runs, so nothing reads its type as a
    host verdict.

    Carries ``api_requests`` (this process's spend, for the ledger) and
    ``api_requests_total`` (the whole sweep's, for the operator), attached by
    the caller's ``spent`` helper.
    """

    def __init__(
        self, message: str, *, checkpoint_path: str, roots_done: int, root_count: int
    ) -> None:
        super().__init__(message)
        self.checkpoint_path = checkpoint_path
        self.roots_done = roots_done
        self.root_count = root_count


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
                raise CredentialRejectedError(
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
    budget_exhausted: Callable[[], bool] | None = None,
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
        CredentialRejectedError: the token was rejected. Propagates from the
            FIRST probe rather than after the ladder -- a dead token answers
            identically at every rung, so the remaining probes could only
            re-learn it -- and its message sends the operator to the .env.
        DownloadError: ``budget_exhausted`` (the sweep's ``max_requests``
            guard, asked before every probe) returned True. Raised rather
            than returned as None, because None means "no rung answers in
            this bbox" and both the log line and the caller's refusal would
            blame the city for a budget the operator set. Nothing is swept
            and nothing is checkpointed at this point, so the message says
            so instead of pointing at a resume.
        ResponseError: the server gave a definite, unusable non-credential
            answer at every probe (an unparseable body, an HTTP error that is
            neither backpressure nor transport). Surfaced as itself rather
            than folded into the None above, because "no radius answers in
            this bbox" is a property of the LOCATION and sends the operator to
            look at the city; a broken endpoint is not the same fact and the
            message the caller prints must not claim the wrong one.
    """
    points = calibration_points(bbox, probes_per_rung)
    saw_only_broken = True
    for radius in RADIUS_LADDER_M:
        answered = 0
        for lat, lon in points:
            if budget_exhausted is not None and budget_exhausted():
                # The ladder is fixed overhead the runaway guard used to skip:
                # max_requests=3 spent up to 30 requests here before the first
                # root was ever asked, in the parameter the scheduler uses to
                # hand a channel the night's REMAINING budget.
                raise DownloadError(
                    f"The request budget ran out during radius calibration "
                    f"(while probing r={radius} m); nothing was swept and nothing is "
                    f"checkpointed -- re-run with a larger budget"
                )
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
        # A rejected credential never reaches this: CredentialRejectedError
        # propagates from the first probe. What lands here is the endpoint
        # answering definite garbage -- unparseable bodies, item-less envelopes.
        raise ResponseError(
            "KartaView gave no usable answer at any radius or any calibration point "
            "(every probe a definite error rather than backpressure); this is the "
            "endpoint, not the city's geometry"
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
    giving a definite answer we cannot use (an unparseable body, an HTTP error
    that is neither backpressure nor transport); re-asking cannot change it, so
    the cell is recorded broken and never retried. Its
    :class:`CredentialRejectedError` subclass is the exception and PROPAGATES:
    a dead token answers identically at every cell, so it fails the sweep
    rather than the cell.

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
        except CredentialRejectedError:
            # Propagated, not recorded: a dead token answers identically at
            # every cell, so "broken" here would re-ask it at every remaining
            # cell of every page. The sweep stops now, the checkpoint's
            # finally-commit keeps the spend, and the message sends the
            # operator to the .env rather than to the city.
            raise
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


# ── The checkpoint on disk ─────────────────────────────────────────────────
#
# Layout, one directory per (city, grid geometry):
#
#     <checkpoint_path>/
#       state.json            the commit record; written LAST
#       part-00000.parquet    census rows, _CENSUS_DTYPES, in FETCH ORDER
#       part-00001.parquet
#
# THE PARTS ARE PARQUET, NOT CSV, and that is load-bearing rather than a taste
# call. Every string column here is a PROVIDER-SUPPLIED value -- a contributor's
# chosen username, an OSM way id, a sequence id, an org code -- and pandas'
# default `na_values` claims `NA`, `null`, `None`, `nan`, `N/A` and several more.
# Measured on this schema: a username of "NA", a way_id of "null" and a
# sequence_id of "None" all come back from `read_csv` as `<NA>`. So a CSV
# checkpoint would make a RESUMED run publish DIFFERENT rows than an
# uninterrupted one -- `_kartaview_image_columns` would attribute that photo to
# "© KartaView" instead of "© KartaView contributor NA", and its way_id would be
# gone -- in a repo whose census output is pinned byte-for-byte by a golden
# fixture. (`keep_default_na=False` trades the bug for its mirror image: every
# genuine null becomes the empty string, and the same attribution splits the
# other way.) Parquet round-trips all thirteen columns exactly, `""`-vs-`<NA>`
# included, and an empty frame's dtypes with them.
#
# THE COMMIT RECORD IS A COUNT, NOT A LIST. Committed parts are `[0, parts)`;
# anything at or beyond that index is a torn write from a crash and is deleted on
# load. A growing list re-serialized at every commit would be O(n^2) bytes for
# nothing, and "is this name in the list" is a harder thing to reason about than
# an integer. `state.json` is written last and atomically, so it is the commit
# point: a part that exists without being counted never happened.
#
# The rules that are NOT about what is being crawled -- where checkpoints go,
# why the path is date-free, why the channel is part of its key, and why the
# CALLER discards it once its artifact is durable -- moved to `checkpointing.py`
# when Mapillary grew a checkpoint of its own (#256). Read them there; they are
# preconditions for this file, not background.
#


@dataclass
class SweepCheckpoint:
    """Handle to an on-disk checkpoint directory, loaded or freshly opened."""

    path: str
    radius_m: int
    channel: str | None = None
    # When the FIRST commit of this sweep landed, carried forward by every later
    # one. The age cap is measured from here rather than from `updated_at`
    # (issue #272), for the reason Mapillary's checkpoint already records: a
    # night that commits NO progress still rewrites the record (the finally
    # commit runs on a host block, a rejected credential, a budget stop), and a
    # host-blocked night deliberately records no `consecutive_failures`, so the
    # same stalest city is re-attempted the next night and the next. Aged from
    # `updated_at`, such a city could refresh its own clock forever and splice
    # rows fetched last quarter into a snapshot dated today -- which is the one
    # way a checkpoint produces a WRONG artifact rather than wasted work.
    created_at: str | None = None
    roots_done: int = 0
    parts: int = 0
    census_rows: int = 0
    cells_visited: int = 0
    raw_photo_count: int = 0
    api_requests_total: int = 0
    failed_cells: list[Cell] = field(default_factory=list)


def _cell_to_dict(cell: Cell) -> dict[str, Any]:
    return {
        "lat": cell.lat,
        "lon": cell.lon,
        "size_m": cell.size_m,
        "depth": cell.depth,
        "lon_extent_lat": cell.lon_extent_lat,
    }


def _cell_from_dict(record: dict[str, Any]) -> Cell:
    # .get(): a checkpoint from before lon_extent_lat existed reads as None,
    # which every consumer treats as the historical cell.lat behaviour.
    extent = record.get("lon_extent_lat")
    return Cell(
        lat=float(record["lat"]),
        lon=float(record["lon"]),
        size_m=float(record["size_m"]),
        depth=int(record["depth"]),
        lon_extent_lat=None if extent is None else float(extent),
    )


def _part_path(path: str, index: int) -> str:
    return os.path.join(path, CHECKPOINT_PART_TEMPLATE.format(index=index))


def _validate_sweep_store(
    path: str,
    state: dict,
    *,
    bbox: tuple[float, float, float, float],
    ipp: int,
    requested_radius_m: int | None,
) -> tuple[SweepCheckpoint | None, str | None]:
    """
    The geometric/footer cascade every reader of a sweep store makes.

    ``(cp, None)`` when the directory holds what its commit record claims for
    THIS lattice, ``(None, reason)`` otherwise. Raises nothing of its own; a
    malformed record surfaces as an exception the callers' broad handler turns
    into a reason, which is the never-raise posture they both keep.

    Factored out because a promoted cache entry (issue #290) IS a checkpoint
    directory that was moved, so it must be validated exactly as a resume is.
    What each caller adds on top is what differs: :func:`load_checkpoint` adds
    the channel and the age of the sweep and then purges torn parts;
    :func:`load_cached_sweep` adds the marker's own window and the one check a
    resume must NOT make, completeness.

    The returned handle carries no ``created_at``; each caller stamps that from
    the record itself, because only ``load_checkpoint`` continues the sweep and
    so needs to carry the origin forward.
    """
    if state["format_version"] != CHECKPOINT_FORMAT_VERSION:
        return None, (
            f"it is format v{state['format_version']}, this build writes "
            f"v{CHECKPOINT_FORMAT_VERSION}"
        )
    if not _bbox_matches(state["bbox"], bbox):
        return None, f"it was swept over bbox {state['bbox']}, this run uses {list(bbox)}"
    if int(state["ipp"]) != ipp:
        return None, f"it was swept at ipp={state['ipp']}, this run uses ipp={ipp}"
    radius_m = int(state["radius_m"])
    if requested_radius_m is not None and requested_radius_m != radius_m:
        return None, (
            f"it was swept at r={radius_m} m and this run was asked for "
            f"r={requested_radius_m} m; an explicit radius wins"
        )
    root_count = int(state["root_count"])
    if len(cells_for_bbox(*bbox, radius_m * math.sqrt(2))) != root_count:
        # Catches a change to cells_for_bbox itself. The module docstring
        # notes that correcting the equirectangular cos(mid_lat) shortfall
        # "would move every city's cell count"; this is what makes such a
        # change re-sweep rather than silently resume onto a lattice whose
        # indices no longer mean what the checkpoint recorded.
        return None, f"the lattice no longer has {root_count} root cells"
    cp = SweepCheckpoint(
        path=path,
        radius_m=radius_m,
        roots_done=int(state["roots_done"]),
        parts=int(state["parts"]),
        census_rows=int(state["census_rows"]),
        cells_visited=int(state["cells_visited"]),
        raw_photo_count=int(state["raw_photo_count"]),
        api_requests_total=int(state["api_requests_total"]),
        failed_cells=[_cell_from_dict(c) for c in state["failed_cells"]],
    )
    if not 0 <= cp.roots_done <= root_count or cp.parts < 0:
        return None, f"its counters are out of range ({cp.roots_done}/{root_count}, {cp.parts})"
    # Verify the parts from their FOOTERS -- a seek to the end of each file,
    # costing nothing -- rather than discovering a truncated one at finalize,
    # after the night has already been paid for.
    rows = 0
    for index in range(cp.parts):
        part = _part_path(path, index)
        if not os.path.exists(part):
            return None, f"committed part {os.path.basename(part)} is missing"
        rows += pq.ParquetFile(part).metadata.num_rows
    if rows != cp.census_rows:
        return None, f"its parts hold {rows} rows where the commit record says {cp.census_rows}"
    return cp, None


def load_checkpoint(
    path: str,
    *,
    bbox: tuple[float, float, float, float],
    ipp: int,
    requested_radius_m: int | None,
    channel: str | None = None,
) -> SweepCheckpoint | None:
    """
    Resume state for this sweep, or None if there is nothing usable here.

    NEVER RAISES. Every failure degrades to "sweep from the beginning" with a
    warning, following :func:`download_gsv.get_processed_points` and
    :func:`download_gsv_history._load_checkpoint`. A checkpoint is not a
    comparison whose mismatch would corrupt an artifact -- the worst case of
    ignoring one is wasted work, so the walk-diff ``same_grid_geometry`` posture
    of refusing outright would cost a night to protect nothing.

    Args:
        path: the checkpoint directory. Need not exist.
        bbox: this sweep's frame. A different one means a different lattice.
        ipp: this sweep's page size. It prices ``pages_for_total``, so a change
            changes the walk even at an identical radius.
        requested_radius_m: the caller's explicit radius, or None to adopt the
            checkpoint's. An explicit value that CONTRADICTS the stored one
            discards the checkpoint rather than being silently overridden.
        channel: which api_usage channel this sweep meters into. The PATH
            already keys the channel (checkpoint_path_for), but the path is
            caller-built: a directory moved by hand, or a future caller
            deriving the path wrong, would pass every geometric check here and
            resume a sweep whose spend belongs to a different ledger. The
            state file records what it was written as, and a mismatch --
            including a checkpoint from before this field existed -- discards.

    Returns:
        A :class:`SweepCheckpoint` with its uncommitted parts already swept
        away, or None.
    """

    def discard(reason: str) -> None:
        logger.warning(f"Ignoring the KartaView checkpoint at {path}: {reason}")

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
        cp, reason = _validate_sweep_store(
            path, state, bbox=bbox, ipp=ipp, requested_radius_m=requested_radius_m
        )
        if reason is not None:
            discard(reason)
            return None
        cp.channel = channel
        cp.created_at = state["created_at"]
        # MEASURED FROM created_at -- WHEN THE FIRST COMMIT LANDED -- NOT FROM
        # updated_at (issue #272). The one guard here that protects an ARTIFACT
        # rather than a night's work: frozen geometry never changes, so every
        # other check above still passes months later, and resuming would splice
        # last quarter's rows into a snapshot dated today. `updated_at` cannot
        # carry it, because the finally-commit rewrites the record on nights
        # that swept NOTHING -- a host block, a rejected credential, a budget
        # stop at request 1 -- and a host-blocked night records no
        # `consecutive_failures`, so the same stalest city is re-attempted
        # nightly and would refresh its own clock indefinitely. See
        # CHECKPOINT_MAX_AGE_S.
        age_s = (datetime.now(UTC) - datetime.fromisoformat(cp.created_at)).total_seconds()
        if age_s > CHECKPOINT_MAX_AGE_S:
            discard(
                f"its first commit landed {age_s / 86400:.1f} days ago, past the "
                f"{CHECKPOINT_MAX_AGE_S / 86400:.0f}-day limit; its rows would be spliced "
                f"into a snapshot dated today"
            )
            return None
        root_count = len(cells_for_bbox(*bbox, cp.radius_m * math.sqrt(2)))
    except Exception as e:
        # Broad on purpose; see the NEVER RAISES note above. A checkpoint that
        # cannot be read must cost a re-sweep, never a night.
        discard(f"{type(e).__name__}: {e}")
        return None

    try:
        _purge_uncommitted_parts(cp)
    except Exception as e:
        # The one call that sat OUTSIDE the catch-all, and it hits the
        # filesystem too: purging a torn part is an os.remove, so a read-only
        # checkpoint directory raised PermissionError straight through the
        # NEVER RAISES contract -- out of load_checkpoint, out of the sweep's
        # own DownloadError arms (it fires before the try), and into cli.py as
        # a bare traceback with exit 2. Degrade to a fresh sweep like every
        # other failure here; resuming WITHOUT the purge is not an option,
        # since the debris would sit under the next commit's part name.
        discard(f"cannot purge its uncommitted parts: {type(e).__name__}: {e}")
        return None
    if cp.roots_done == root_count:
        # Louder than the partial case, because this one finalizes without
        # re-sweeping and so cannot be told from a fresh collection by its
        # artifact alone. It is the intended recovery from a caller that died
        # before its artifact was durable -- and it is also what a caller that
        # simply forgot to `discard_checkpoint` looks like, so say which sweep's
        # answers are about to be republished.
        retry_note = (
            f" after re-probing its {len(cp.failed_cells)} failed cell(s)"
            if cp.failed_cells
            else " without issuing a request"
        )
        logger.warning(
            f"The KartaView checkpoint at {path} is COMPLETE ({root_count} root cells, "
            f"first committed {age_s / 3600:.1f} h ago): finalizing from disk{retry_note}. "
            f"This is the recovery path for a caller that died before "
            f"its artifact was durable; if that is not what happened, the previous run "
            f"failed to call discard_checkpoint()."
        )
    else:
        logger.info(
            f"Resuming a KartaView sweep from {path}: {cp.roots_done}/{root_count} root cells "
            f"done at r={cp.radius_m} m, {cp.census_rows} census rows on disk, "
            f"{cp.api_requests_total} requests already spent"
        )
    return cp


def load_cached_sweep(
    cache_path: str,
    *,
    bbox: tuple[float, float, float, float],
    ipp: int,
    requested_radius_m: int | None,
) -> tuple[SweepCheckpoint, dict] | None:
    """
    A COMPLETE sweep another consumer already paid for, or None (issue #290).

    This is the one that matters most for KartaView: a sweep is HOURS (Singapore
    ~10.4 h at the paced 16/min), so a grid run and the #258 road walk of one
    city sweeping the identical frozen bbox on the same night is not a doubled
    cost in the abstract -- it is a second overnight against a host that meters
    by IP.

    Never raises and DELETES an entry it refuses, like Mapillary's loader and
    unlike :func:`load_checkpoint` (which leaves an unusable checkpoint in place
    because its parts are indexed by fetch order and would be overwritten
    anyway). A cache entry is reachable by every consumer, so one that will never
    validate has to go rather than be re-read by each of them in turn.

    Three things are checked, in order: the marker's reuse window, the same
    geometric/footer cascade a resume makes (:func:`_validate_sweep_store`), and
    COMPLETENESS -- ``roots_done == root_count``. That last is the difference
    between a checkpoint and a cache entry: a partial sweep is legitimate
    progress, but reused as a census it would publish the unvisited cells' query
    points as genuine no-imagery, which is absence never observed.

    ``failed_cells`` in a complete entry are allowed (the fetcher's own
    ``MAX_FAILED_AREA_FRACTION`` guard already passed on them) and are INHERITED
    RATHER THAN RE-PROBED. The reuser is publishing the same observation, so the
    same query points read REQUEST_FAILED in both artifacts. That differs
    deliberately from a same-channel resume, which re-probes them because a
    refusal is time-varying -- a resume is continuing one crawl, while this is
    republishing a finished one.
    """

    def discard(reason: str) -> None:
        logger.warning(f"Ignoring the cached KartaView sweep at {cache_path}: {reason}")
        discard_checkpoint(cache_path)

    marker = load_census_cache_marker(cache_path, max_age_s=CENSUS_REUSE_MAX_AGE_S)
    if marker is None:
        return None  # no entry, or one the marker check already removed
    state_path = _state_path(cache_path)
    if not os.path.exists(state_path):
        discard("it has a marker but no commit record")
        return None
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        cp, reason = _validate_sweep_store(
            cache_path, state, bbox=bbox, ipp=ipp, requested_radius_m=requested_radius_m
        )
        if reason is not None:
            discard(reason)
            return None
        cp.created_at = state.get("created_at")
        root_count = int(state["root_count"])
        if cp.roots_done != root_count:
            discard(
                f"it stopped at {cp.roots_done} of {root_count} root cells; only a "
                f"COMPLETE sweep is reusable"
            )
            return None
    except Exception as e:
        # Broad on purpose, exactly as above.
        discard(f"{type(e).__name__}: {e}")
        return None
    return cp, marker


def _purge_uncommitted_parts(cp: SweepCheckpoint) -> None:
    """
    Delete part files at or beyond the commit record, and any staging leftovers.

    Those are torn writes: the process died between writing a part and counting
    it. Removing them keeps the next commit's index free, so a part is never
    written under a name that already holds someone else's bytes.

    The ``.tmp`` siblings are the other half of the same crash. A commit that
    died between ``to_parquet`` and ``os.replace`` leaves one behind, and while
    it is harmless -- the next commit truncates the same name -- a sweep that
    never completes would otherwise accumulate one per interrupted commit in a
    directory that already holds a partial census.
    """
    index = cp.parts
    while os.path.exists(part := _part_path(cp.path, index)):
        os.remove(part)
        index += 1
    for name in os.listdir(cp.path):
        if name.endswith(".tmp"):
            os.remove(os.path.join(cp.path, name))


def _commit_checkpoint(
    cp: SweepCheckpoint,
    frames: list[pd.DataFrame],
    *,
    roots_done: int,
    failed_cells: list[Cell],
    cells_visited: int,
    raw_photo_count: int,
    api_requests_total: int,
    bbox: tuple[float, float, float, float],
    ipp: int,
    root_count: int,
) -> None:
    """
    Make everything swept so far durable, as of the last completed root boundary.

    Ordering is the whole mechanism: the part is written, fsynced and renamed
    into place FIRST, and only then does ``state.json`` -- itself written to a
    sibling and ``os.replace``d -- count it. So a crash anywhere leaves either
    the previous consistent state or this one, never a half of either.

    THE DIRECTORY IS FSYNCED AFTER EACH RENAME, and the state file before its
    own. Without that the ordering above holds only against a PROCESS crash --
    where the page cache survives and the file fsync buys nothing anyway -- and
    not against a power loss, where the two renames may reach the disk in either
    order. The failure that leaves would not be a wrong artifact (a state file
    naming a part that is not there is caught by ``load_checkpoint``'s existence
    and footer checks) but it would discard the WHOLE checkpoint rather than the
    last interval, which for a multi-night city is the loss this exists to
    prevent. Four fsyncs per commit, i.e. four per 32 paced requests, is not a
    cost worth reasoning about.

    ``frames`` holds only the rows since the last commit; the caller clears it
    afterwards, which is what keeps the sweep's memory bounded by one interval
    rather than by the whole census.
    """
    # Stamped by the FIRST commit of the sweep and carried forward by every
    # later one, including the ones that commit no progress (issue #272). It is
    # what the age cap is measured against, so it must describe the oldest row
    # this checkpoint holds rather than the last time anything touched the file.
    # `updated_at` still moves, for an operator reading the directory.
    created_at = cp.created_at or datetime.now(UTC).isoformat()
    frame = concat_census(frames)
    if len(frame):
        tmp = _part_path(cp.path, cp.parts) + ".tmp"
        frame.to_parquet(tmp, index=False)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, _part_path(cp.path, cp.parts))
        _fsync_dir(cp.path)
        cp.parts += 1
        cp.census_rows += len(frame)
    cp.roots_done = roots_done
    cp.failed_cells = list(failed_cells)
    cp.cells_visited = cells_visited
    cp.raw_photo_count = raw_photo_count
    cp.api_requests_total = api_requests_total
    state = {
        "format_version": CHECKPOINT_FORMAT_VERSION,
        "bbox": list(bbox),
        "radius_m": cp.radius_m,
        "channel": cp.channel,
        "ipp": ipp,
        "root_count": root_count,
        "roots_done": cp.roots_done,
        "parts": cp.parts,
        "census_rows": cp.census_rows,
        "cells_visited": cp.cells_visited,
        "raw_photo_count": cp.raw_photo_count,
        "api_requests_total": cp.api_requests_total,
        "failed_cells": [_cell_to_dict(c) for c in cp.failed_cells],
        "created_at": created_at,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    tmp_state = _state_path(cp.path) + ".tmp"
    with open(tmp_state, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_state, _state_path(cp.path))
    _fsync_dir(cp.path)
    # Only after the record naming it is durable, so a crash cannot leave the
    # in-memory stamp claiming an origin no file records.
    cp.created_at = created_at


def _checkpoint_frames(cp: SweepCheckpoint) -> list[pd.DataFrame]:
    """
    The committed parts, read back in index order.

    Index order is FETCH order, and that is not cosmetic:
    :func:`census.dedupe_census` keeps a repeated image id at the position of
    its FIRST appearance while taking its LAST values, so reading the parts in
    any other order -- a directory glob's, say -- would reorder the published
    CSV of essentially every real city, since the sweep re-sees ~pi/2 of
    everything by construction.
    """
    return [pd.read_parquet(_part_path(cp.path, index)) for index in range(cp.parts)]


async def download_kartaview_metadata_async(
    city_name: str,
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float,
    access_token: str,
    output_csv_gz_path: str,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_requests_per_minute: int = DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
    max_requests: int | None = None,
    radius_m: int | None = None,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    cache_path: str | None = None,
    reuse_census: bool = True,
) -> dict[str, Any]:
    """
    Sweep a city's KartaView census and write it as a run csv.gz.

    Same calling convention as ``download_gsv_metadata_async`` and
    ``download_mapillary_metadata_async``: the caller decides the output
    filename, because skip policy and dated naming live in the CLI/scheduler
    layer rather than here.

    The shape is the second census provider's, which is the point of #237's
    seam -- preamble, grid, this provider's own fetch, then the shared tail.
    Everything specific to KartaView is in the three bindings handed to
    :func:`census_core.write_census_grid_run`.

    Returns:
        Dict with:
            df: DataFrame containing the metadata (KARTAVIEW_METADATA_DTYPES)
            filename_with_path: the written .csv.gz path
            api_requests: sweep requests issued BY THIS PROCESS, for the ledger
            api_requests_total: the whole sweep's spend across resumes
            num_flat_images: census magnitude of flat (PLANE) imagery (#116)
            started_at / finished_at: UTC ISO 8601 timestamps
            checkpoint_path: the still-live checkpoint directory (or None). The
                CALLER discards it, and only after the runs row is committed:
                until then a crash leaves an uncataloged CSV whose orphan-guard
                remedy is "delete it and re-run", which must resume from the
                checkpoint rather than re-pay the sweep. None too when the sweep
                was promoted into the shared census cache (issue #290), because
                the directory has moved.
            census_fetched_by / census_fetched_at: which channel paid for this
                census and when it observed KartaView, for the ``runs`` row

    Raises:
        SweepIncompleteError: propagated unchanged. Nothing is written and the
            checkpoint is NOT discarded -- that is the whole point of it.
    """
    started_at = datetime.now(UTC).isoformat()

    # Checked before a single request is issued, though write_census_grid_run
    # re-checks as it takes ownership of the write: one implementation, called
    # at the point where failing is free.
    census_core.prepare_output_path(output_csv_gz_path)

    # Built before the fetch (its bbox bounds the sweep lattice) and consumed
    # after it, so it is derived once and threaded through.
    grid = census_core.build_grid(center_lat, center_lon, grid_width, grid_height, step_length)

    fetched = await fetch_city_images_async(
        city_name,
        grid.bbox,
        access_token,
        radius_m=radius_m,
        request_timeout=request_timeout,
        max_requests_per_minute=max_requests_per_minute,
        max_requests=max_requests,
        checkpoint_path=checkpoint_path,
        checkpoint_channel=checkpoint_channel,
        cache_path=cache_path,
        reuse_census=reuse_census,
    )
    # WHEN THE PROVIDER WAS OBSERVED, not when this process started, and only
    # when the census was REUSED (issue #290) -- see the identical rule in
    # download_mapillary_metadata_async for why a fresh sweep keeps started_at.
    query_timestamp = (
        fetched.get("census_fetched_at") or started_at
        if fetched.get("census_reused")
        else started_at
    )
    api_requests = fetched["api_requests"]
    api_requests_total = fetched["api_requests_total"]
    failed_cells = fetched.get("failed_cells") or []
    checkpoint_path_used = fetched.get("checkpoint_path")
    try:
        # Counted by the fetch, not recomputed here: binding the census to a
        # local would pin the whole thing alive through both CSV writes and
        # defeat the tail's release, and re-reading it through the dict would
        # cost a second full pass for a log line (issue #157).
        num_images = fetched["num_images"]
        num_panos = fetched["num_panos"]
        logger.info(
            f"Swept {fetched['raw_photo_count']} photo rows "
            f"({num_images} unique: {num_panos} panos, {num_images - num_panos} flat) "
            f"from {fetched['cells_visited']} cells at r={fetched['radius_m']} m"
        )

        written = census_core.write_census_grid_run(
            fetched,
            grid,
            output_csv_gz_path,
            query_timestamp,
            capture_dates_for=_kartaview_capture_dates,
            image_columns=_kartaview_image_columns,
            dtypes=KARTAVIEW_METADATA_DTYPES,
            # A cell nothing came back for leaves its grid points UNKNOWN rather
            # than empty; a clean sweep passes None and pays nothing. The sweep
            # refuses to finalize at all past MAX_FAILED_AREA_FRACTION, so this
            # only ever describes a small remainder.
            unmeasured_mask=(
                (lambda lats, lons: _points_in_cells(lats, lons, failed_cells))
                if failed_cells
                else None
            ),
            unmeasured_desc=f"{len(failed_cells)} unmeasured cell(s)",
        )
    except Exception as e:
        # The sweep's spend is real even when this tail dies (ENOSPC on the
        # gzip write, a read-back failure), and these failures are not
        # DownloadErrors, so without the attributes the caller's failure-path
        # ledger write records nothing — and because the checkpoint survives
        # and the resume re-finalizes for ~0 new requests, the spend would
        # never land in ANY api_usage row (PR #251 review).
        e.api_requests = api_requests
        e.api_requests_total = api_requests_total
        raise

    # The checkpoint is NOT discarded here, deliberately — not even now that
    # the CSV is on disk. The caller still has to write the stats, the `runs`
    # row, the JSON and the diff, and a crash between this return and
    # `register_run` leaves an uncataloged CSV whose orphan-guard remedy is
    # "delete it and re-run" — which must re-finalize from the checkpoint for
    # ~0 requests, not re-pay a multi-night sweep (PR #251 review). The path is
    # returned so the CLI can discard_checkpoint() once the runs row is
    # committed; see that function's docstring for the contract.

    return {
        "checkpoint_path": checkpoint_path_used,
        "census_fetched_by": fetched.get("census_fetched_by"),
        "census_fetched_at": fetched.get("census_fetched_at"),
        "census_reused": bool(fetched.get("census_reused")),
        "df": written["df"],
        "filename_with_path": output_csv_gz_path,
        # This process's spend. The ledger is additive and keyed by (date,
        # provider), so handing it the cumulative figure would charge a resumed
        # sweep's earlier nights against today's budget gate.
        "api_requests": api_requests,
        "api_requests_total": api_requests_total,
        # Census magnitude of flat imagery (issue #116): every in-grid PLANE
        # image, including those at points that also hold a SPHERE pano. Not
        # reconstructable from the CSV (flat-only points collapse to one
        # FLAT_ONLY row), so it is threaded to the catalog separately.
        "num_flat_images": written["num_flat_images"],
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }


def _reuse_cached_sweep(
    cached: tuple[SweepCheckpoint, dict],
    *,
    city_name: str,
    checkpoint_channel: str | None,
) -> dict[str, Any]:
    """
    Assemble a census from the shared cache. Zero requests (issue #290).

    Parts are read in INDEX order, which is FETCH order, because that is what
    :func:`census.dedupe_census` reads as first-appearance position -- the sweep
    re-sees ~pi/2 of everything by construction, so any other order would
    reshuffle the published CSV of essentially every real city. It is the same
    argument :func:`_checkpoint_frames` documents for a resume, and the reason
    this reuses that function rather than globbing the directory.

    THE TWO REQUEST COUNTERS SPLIT DIFFERENTLY HERE:

    * ``api_requests`` is 0 unconditionally -- this process issued none, and the
      daily ledger is additive and keyed by (date, provider), so anything else
      would charge one channel's spend against another's budget gate.
    * ``api_requests_total`` is the sweep's cost ONLY when the reuser is the same
      channel that paid it, which is #239's re-finalize rather than a reuse: a
      caller that died before its artifact was durable, coming back to write the
      row for a sweep it did pay for. A DIFFERENT channel records 0, and the
      provenance columns are what make that 0 explicable.

    ``cells_visited``, ``raw_photo_count`` and ``radius_m`` come off the stored
    record, because they describe the CENSUS rather than the process that
    fetched it -- the same rule a resume follows. ``failed_cells`` are inherited
    rather than re-probed; see :func:`load_cached_sweep`.
    """
    cp, marker = cached
    fetched_by = marker.get("fetched_by")
    crawl_started_at = marker.get("crawl_started_at") or marker.get("completed_at")
    # WARNING, not INFO, for the reason the COMPLETE-checkpoint notice is: a
    # collection that issues no request is indistinguishable from a real one by
    # its artifact alone, so the log has to say which sweep's answers are being
    # republished.
    logger.warning(
        f"REUSING the KartaView census fetched by {fetched_by} (crawl started "
        f"{crawl_started_at}) for {city_name}: 0 sweep requests"
    )
    frames = _checkpoint_frames(cp)
    census = concat_census(frames)
    # The #157 release: the per-part frames must not survive into dedupe's
    # allocations.
    frames.clear()
    census = census_core.dedupe_census(census)
    same_crawl = fetched_by == checkpoint_channel
    return {
        "census": census,
        "api_requests": 0,
        "api_requests_total": int(marker.get("api_requests_total") or 0) if same_crawl else 0,
        "cells": cp.roots_done,
        "cells_visited": cp.cells_visited,
        "radius_m": cp.radius_m,
        "raw_photo_count": cp.raw_photo_count,
        "num_images": len(census),
        "num_panos": int(census_core.census_is_pano(census).sum()),
        "failed_cells": [_cell_from_dict(c) for c in marker.get("failed") or []],
        # The entry is shared, so it is nobody's to discard.
        "checkpoint_path": None,
        "census_fetched_by": fetched_by,
        "census_fetched_at": crawl_started_at,
        "census_reused": True,
    }


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
    checkpoint_path: str | None = None,
    checkpoint_request_interval: int = DEFAULT_CHECKPOINT_REQUEST_INTERVAL,
    checkpoint_channel: str | None = None,
    cache_path: str | None = None,
    reuse_census: bool = True,
) -> dict[str, Any]:
    """
    Fetch a city's KartaView census, serialized against other processes.

    Every KartaView request in the repo passes through here -- the grid run and
    the road walk both -- which is what makes this the one place the
    machine-wide lock has to be taken. The documented ceiling is per key, but
    nothing published says the ENFORCED one is; both of this project's prior
    bans were on undocumented per-IP limits, and a per-IP limit is a property of
    the machine that no per-process limiter can honour alone (#208).

    That hold covers the CHECKPOINT too, and is why it needs no lock of its own:
    every read and write of the directory happens inside this ``with``, so a
    second process on this machine is refused before it can open the file a
    resume is reading.

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
            checkpoint_path=checkpoint_path,
            checkpoint_request_interval=checkpoint_request_interval,
            checkpoint_channel=checkpoint_channel,
            cache_path=cache_path,
            reuse_census=reuse_census,
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
    checkpoint_path: str | None = None,
    checkpoint_request_interval: int = DEFAULT_CHECKPOINT_REQUEST_INTERVAL,
    checkpoint_channel: str | None = None,
    cache_path: str | None = None,
    reuse_census: bool = True,
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
        max_requests: runaway guard, not a sampling knob. WITHOUT a checkpoint a
            sweep that hits it leaves the rest of the bbox UNMEASURED, which all
            but guarantees the failed-area check below refuses to finalize the
            snapshot -- which is the point: a partial census must not be
            published as a dated one. WITH a checkpoint the unvisited cells are
            not unmeasured, they are tomorrow's work, so the same trip raises
            :class:`SweepIncompleteError` instead and publishes nothing either
            way. That is what lets the scheduler hand this the night's remaining
            budget without the figure being a cliff that destroys the spend.
        retries: backpressure/transport retries before a cell is subdivided.
        calibration_probes: points per rung; a rung passes only if all answer.
        checkpoint_path: directory to checkpoint into, so an interrupted sweep
            resumes instead of starting over (#239). None disables it, and that
            path is byte-for-byte the behaviour that shipped before this
            existed. THE PATH MUST BE DATE-FREE and must live outside ``data/``;
            see the checkpoint section above for why both are contracts rather
            than preferences. One directory per (city, grid geometry, CHANNEL) --
            the channel is not optional, because a road walk sweeps the same
            frozen bbox at the same ipp and radius and would otherwise resume
            the grid run's checkpoint. A mismatched bbox, ipp or radius, or a
            checkpoint older than :data:`CHECKPOINT_MAX_AGE_S`, discards it and
            sweeps afresh rather than resuming onto a lattice it does not
            describe. ON A CLEAN SWEEP THE DIRECTORY SURVIVES: it is the
            caller's to :func:`discard_checkpoint` once the dated artifact is
            durable, which is what makes a crash in that tail recoverable.
        checkpoint_request_interval: requests between commits. Clamped to >= 1.
        checkpoint_channel: the api_usage channel this sweep meters into,
            recorded in the commit record and required to match on resume. The
            checkpoint PATH already keys the channel, but the path is
            caller-built; this is the half the state file can enforce itself.
        cache_path: the shared per-(provider, city, bbox) cache entry (issue
            #290), also caller-built -- see
            :func:`checkpointing.census_cache_path_for`. Given one, a COMPLETE
            sweep another consumer already paid for is reused here for zero
            requests, and a sweep this call completes is PROMOTED into it on the
            way out. None keeps the historical behaviour exactly. It matters
            more here than for Mapillary: a sweep is hours, so the grid run and
            the #258 walk of one city would otherwise be two overnights against
            a host that meters by IP.
        reuse_census: False re-sweeps even when the cache holds a usable entry,
            and replaces it. The ``--refetch-census`` escape hatch.

    Returns:
        Dict with ``census`` (the deduped columnar census), ``api_requests``,
        ``api_requests_total``, ``cells`` (root cells), ``cells_visited`` (roots
        plus every subdivision), ``radius_m`` (what the sweep tiled at),
        ``raw_photo_count`` (pre-dedupe), ``num_images`` and ``num_panos``
        (post-dedupe totals, summarized here because the caller cannot count
        them without pinning the census -- see below), ``failed_cells`` and
        ``checkpoint_path`` (echoed back, or None). On a clean sweep that path
        still exists and is the CALLER'S to :func:`discard_checkpoint` once its
        artifact is durable; see that function for why deleting it here would
        forfeit the caller-tail half of #239.

        THE TWO REQUEST COUNTS ARE NOT THE SAME NUMBER and the asymmetry is
        deliberate. ``api_requests`` is what THIS PROCESS spent; it is what
        ``cli.py`` feeds to ``db.add_api_usage``, which is additive and keyed by
        (date, provider), so a resumed night reporting the whole sweep would
        charge last night's requests against tonight's budget gate and
        eventually skip cities that fit. ``api_requests_total`` is the sweep's
        cumulative spend, for the operator and for the ``runs`` row. (Note this
        is the opposite of ``download_gsv_history``'s checkpoint, which DOES
        carry its count over -- correctly, because its caller writes
        ``db.record_harvest`` and never touches the daily ledger.)

        ``cells_visited`` and ``raw_photo_count`` stay CUMULATIVE across a
        resume, because they describe the census rather than the process that
        fetched it. ``failed_cells`` is carried too, but through a RETRY PASS
        rather than verbatim: a resume re-probes every carried cell before the
        unvisited roots (a refusal is time-varying -- fact 2), so a cell stays
        failed only by refusing again, and a crash mid-pass keeps the
        not-yet-re-probed tail failed in the checkpoint.

    Raises:
        SweepIncompleteError: the sweep stopped with roots unvisited and
            checkpointed them. Nothing is finalized; re-run with the same
            checkpoint path to continue.
        DownloadError: on a rejected credential, a city where no radius answers
            anywhere, or a sweep that left more than
            :data:`MAX_FAILED_AREA_FRACTION` of the bbox unmeasured. Carries
            ``api_requests`` so the caller can still record what it spent.
        HostBlockedError: KartaView is refusing this host. Raised at the FIRST
            refusal rather than after the whole bbox has been paid for (#205).
    """
    api_requests = 0
    prior_requests = 0

    def count_request() -> None:
        nonlocal api_requests
        api_requests += 1

    def spent(error: DownloadError) -> DownloadError:
        """Attach the spend so the caller can still write the ledger row."""
        # api_requests is THIS process's, because that is the attribute the
        # caller hands to the additive daily ledger. The cumulative figure rides
        # alongside rather than replacing it; see the Returns note above.
        error.api_requests = api_requests
        error.api_requests_total = prior_requests + api_requests
        return error

    def over_budget() -> bool:
        """Has the runaway guard tripped? Asked everywhere a request is issued."""
        return max_requests is not None and api_requests >= max_requests

    def unvisited(cells: list[Cell]) -> None:
        """
        Record cells a budget stop never reached.

        WITHOUT a checkpoint they are unmeasured area, and the failed-area guard
        below must refuse to finalize: a census covering 60% of a city, written
        as an immutable dated snapshot, diffs against its predecessor as "every
        pano in the other 40% removed".

        WITH one they are simply tomorrow's work -- on disk, unvisited, and
        picked up by the resume. Calling them failed there would be actively
        wrong twice over: it would poison the resumed sweep's guard with cells
        nothing was ever wrong with, and it would make the guard refuse a
        snapshot that is about to be completed.
        """
        if cp is None:
            failed_cells.extend(cells)

    def durable_failed() -> list[Cell]:
        """
        The failed set as it must be RECORDED, not as this session has seen it.

        A cell carried failed from a prior session leaves the set only at the
        moment it is actually re-swept (``retry_pos`` advances past it), so a
        commit taken mid-retry-pass -- or the finally-commit after a crash
        there -- keeps every not-yet-re-probed cell failed. Initializing
        ``failed_cells`` from the carried set instead would read the same on
        the clean path and silently LOSE the tail on this one: the un-retried
        cells' grid points would publish as ZERO_RESULTS, absence never
        observed.
        """
        return retry_queue[retry_pos:] + failed_cells

    # Clamped ONCE, here, so the page arithmetic and the wire agree. _post_nearby
    # sends min(ipp, IPP_MAX) because the server caps it there, but
    # pages_for_total was priced from the caller's value -- so ipp=8000 asked
    # for one page of a circle holding 8,000 photos, got 2,000, and recorded no
    # failed cell and no warning. 6,000 photos silently absent from a snapshot
    # that publishes as complete.
    if ipp > IPP_MAX:
        logger.warning(f"ipp={ipp} exceeds the server cap; using {IPP_MAX}")
        ipp = IPP_MAX

    # Normalized, NOT defended against: any value <= 1 means "commit at every
    # root boundary", because the cadence test is
    # `api_requests - requests_at_last_commit >= interval` and no root costs
    # zero requests. So 0 and -5 are not a way to disable checkpointing, they
    # are the tightest cadence there is -- and that is a legitimate thing for a
    # caller to want (the tests sweep at 2 and 3). The clamp only keeps a
    # nonsense value from reading as one, since `>= -5` and `>= 1` are the same
    # predicate here. Turning checkpointing OFF is checkpoint_path=None.
    checkpoint_request_interval = max(1, checkpoint_request_interval)

    # `resumed` is what was already on disk (None on a first night or a
    # checkpoint that does not describe this sweep); `cp` is the handle the loop
    # commits through, and cannot be built until the radius is resolved, since
    # the radius is part of what the checkpoint pins.
    # THE CACHE IS CONSULTED BEFORE ANYTHING ELSE -- before the checkpoint
    # directory is created and, crucially, before calibration (issue #290).
    # Calibration alone is up to 30 requests, so a hit checked any later would
    # not actually be free. Inside the host lock (fetch_city_images_async), so
    # no second process can be mid-promotion into the entry read here.
    if reuse_census and cache_path is not None:
        cached = load_cached_sweep(cache_path, bbox=bbox, ipp=ipp, requested_radius_m=radius_m)
        if cached is not None:
            return _reuse_cached_sweep(
                cached,
                city_name=city_name,
                checkpoint_channel=checkpoint_channel,
            )

    resumed: SweepCheckpoint | None = None
    cp: SweepCheckpoint | None = None
    if checkpoint_path is not None:
        try:
            os.makedirs(checkpoint_path, exist_ok=True)
        except OSError as e:
            # Asked BEFORE the first request, deliberately. An unwritable
            # checkpoint discovered ten hours in is the exact failure this
            # feature exists to prevent, arriving from the inside.
            raise spent(
                DownloadError(f"Cannot use the KartaView checkpoint at {checkpoint_path}: {e}")
            ) from e
        resumed = load_checkpoint(
            checkpoint_path,
            bbox=bbox,
            ipp=ipp,
            requested_radius_m=radius_m,
            channel=checkpoint_channel,
        )

    limiter = AsyncRateLimiter(max_requests_per_minute)
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    logger.info(
        f"Pacing KartaView requests at {max_requests_per_minute}/min"
        if max_requests_per_minute > 0
        else "KartaView pacing DISABLED (max_requests_per_minute <= 0)"
    )

    # `frames` holds only the rows SINCE THE LAST COMMIT once a checkpoint is in
    # play; everything committed lives on disk and is read back at finalize. The
    # rest are cumulative across resumes, because they describe the census.
    frames: list[pd.DataFrame] = []
    # Cells prior sessions recorded as failed are RE-PROBED, not carried forward
    # unasked: a refusal is time-varying (fact 2 -- Horace refused r=1000 on 0/6
    # attempts and answered it 2/2 forty-five minutes later), so yesterday's
    # dead cell is often today's clean answer, and carrying it blindly would let
    # one bad hour permanently punch REQUEST_FAILED holes through every later
    # resume -- or trip the area guard on a sweep about to complete. They live
    # in `retry_queue` until each is actually re-swept; `failed_cells` is this
    # SESSION's failures only, append-only so the rewind marks stay valid, and
    # the recorded set is always durable_failed() -- the two joined.
    retry_queue: list[Cell] = list(resumed.failed_cells) if resumed else []
    retry_pos = 0
    failed_cells: list[Cell] = []
    raw_photo_count = resumed.raw_photo_count if resumed else 0
    cells_visited = resumed.cells_visited if resumed else 0
    prior_requests = resumed.api_requests_total if resumed else 0
    start_index = resumed.roots_done if resumed else 0
    roots_done = start_index
    requests_at_last_commit = 0
    stop_reason: str | None = None
    # Did the LAST commit -- the one in the sweep's `finally` -- actually land?
    # The whole promotion predicate turns on this (issue #290). That commit is
    # deliberately best-effort: a checkpoint that cannot be written must never
    # be what fails a sweep, so its failure is logged and swallowed. The cost of
    # that is exactly what a cache entry must not inherit -- an on-disk store
    # that LAGS the in-memory census, missing the last interval's rows while
    # `state.json` still reads complete enough to pass every geometric check a
    # reuser makes.
    final_commit_ok = False

    try:
        async with aiohttp.ClientSession() as session:
            if resumed is not None:
                # PINNED, not re-measured. A refusal is a transient (fact 2 --
                # Horace refused r=1000 on 0/6 attempts and answered it 2/2
                # forty-five minutes later), so a resume that re-calibrated
                # could land on a different rung and re-tile the bbox mid-sweep.
                # The lattice has to be stable across resumes or `roots_done`
                # indexes into a different list of cells than it was recorded
                # against. Skipping the ladder also saves up to 30 requests a
                # night, which is a whole median city.
                radius_m = resumed.radius_m
            elif radius_m is None:
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
                    budget_exhausted=over_budget,
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

            if checkpoint_path is not None:
                # The radius is settled, so the checkpoint can be opened against
                # the lattice it will actually describe.
                cp = resumed or SweepCheckpoint(
                    path=checkpoint_path, radius_m=radius_m, channel=checkpoint_channel
                )

            roots = cells_for_bbox(*bbox, radius_m * math.sqrt(2))
            logger.info(
                f"Sweeping KartaView for {city_name}: {len(roots)} cells at r={radius_m} m "
                f"covering bbox {tuple(round(v, 4) for v in bbox)}"
            )

            async def _sweep_subtree(subtree_root: Cell) -> bool:
                """
                Sweep one cell depth-first, descending wherever it refuses.

                Returns False when the request budget ran out mid-subtree (the
                caller stops the sweep; ``unvisited`` has already recorded
                whatever the stop never reached) and True otherwise --
                including when parts of the subtree ended as failed cells,
                which are recorded rather than raised. One body shared by the
                root loop and the retry pass, so the two walks cannot drift.
                """
                nonlocal cells_visited, raw_photo_count
                stack = [subtree_root]
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
                        unvisited(stack)
                        return False
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
                            unvisited([cell])
                            unvisited(stack)
                            return False
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
                            # as a failed cell". (The credential half now
                            # propagates before it can get here -- see
                            # CredentialRejectedError.)
                            failed_cells.append(cell)
                            break
                        raw_photo_count += len(items)
                        frames.append(records_to_census(decode_photo_items(items)))
                return True

            def maybe_commit() -> None:
                """Commit at the cadence, at a boundary the marks were just taken at."""
                nonlocal mark_frames, requests_at_last_commit
                if (
                    cp is not None
                    and api_requests - requests_at_last_commit >= checkpoint_request_interval
                ):
                    _commit_checkpoint(
                        cp,
                        frames,
                        roots_done=roots_done,
                        failed_cells=durable_failed(),
                        cells_visited=cells_visited,
                        raw_photo_count=raw_photo_count,
                        api_requests_total=prior_requests + api_requests,
                        bbox=bbox,
                        ipp=ipp,
                        root_count=len(roots),
                    )
                    frames.clear()
                    mark_frames = 0
                    requests_at_last_commit = api_requests

            progress_bar = progress(
                total=len(roots),
                # Seeded, so a resumed night's bar and its once-a-minute log
                # line read against the whole city rather than restarting at 0%.
                initial=start_index,
                desc=(
                    f"Sweeping KartaView circles for {city_name}"
                    if resumed is None
                    else f"Resuming KartaView sweep for {city_name} "
                    f"at root {start_index}/{len(roots)}"
                ),
                unit="cell",
                # Paced at ~16 requests/min, so a large city is hours of
                # deliberately slow fetching under the scheduler's redirected
                # log. A healthy run has to print, or "hung" and "slow" are
                # indistinguishable after a SIGKILL (issue #157).
                logger=logger,
            )
            retry_bar = None
            # The accumulators as of the last completed boundary -- a root's,
            # or a retried cell's -- so a stop landing mid-cell can roll back
            # to one. Everything a commit writes is taken at a mark, never
            # mid-cell.
            mark_frames = 0
            mark_failed = len(failed_cells)
            mark_visited, mark_photos = cells_visited, raw_photo_count
            mark_retry = retry_pos
            try:
                # ---- Retry pass: cells prior sessions recorded as failed ----
                # Walked BEFORE the unvisited roots, deliberately: these are
                # the cells whose age argues hardest for asking again, and a
                # budget that runs out tonight should run out on the roots the
                # resume will reach anyway, not on the holes it would carry
                # forever. Each cell leaves the durable failed set only when
                # it is actually re-swept (durable_failed), so a stop or crash
                # mid-pass keeps the tail failed rather than losing it; one
                # that fails AGAIN is re-recorded by the subtree body.
                if retry_queue:
                    retry_bar = progress(
                        total=len(retry_queue),
                        desc=(
                            f"Re-probing {len(retry_queue)} previously failed "
                            f"cell(s) for {city_name}"
                        ),
                        unit="cell",
                        logger=logger,
                    )
                    while retry_pos < len(retry_queue):
                        if not await _sweep_subtree(retry_queue[retry_pos]):
                            logger.warning(
                                f"Stopped after {api_requests} requests (max_requests="
                                f"{max_requests}); {len(retry_queue) - retry_pos} of "
                                f"{len(retry_queue)} previously failed cell(s) not yet "
                                f"re-probed -- they stay failed in the checkpoint"
                            )
                            stop_reason = (
                                f"the {max_requests}-request budget ran out re-probing "
                                f"previously failed cells"
                            )
                            break
                        retry_pos += 1
                        retry_bar.update(1)
                        mark_frames, mark_failed = len(frames), len(failed_cells)
                        mark_visited, mark_photos = cells_visited, raw_photo_count
                        mark_retry = retry_pos
                        maybe_commit()

                if stop_reason is None:
                    # range(), not enumerate() + continue: skipping already-swept
                    # roots through the loop body would evaluate over_budget() on
                    # each of them, so a resume with a small budget could "stop"
                    # before asking anything.
                    for index in range(start_index, len(roots)):
                        root = roots[index]
                        if over_budget():
                            unvisited(roots[index:])
                            logger.warning(
                                f"Stopped after {api_requests} requests (max_requests="
                                f"{max_requests}); {len(roots) - index} of {len(roots)} cells "
                                f"never visited"
                            )
                            stop_reason = f"the {max_requests}-request budget ran out"
                            break
                        if not await _sweep_subtree(root):
                            unvisited(roots[index + 1 :])
                            logger.warning(
                                f"Stopped mid-cell after {api_requests} requests (max_requests="
                                f"{max_requests}); {len(roots) - index - 1} of {len(roots)} root "
                                f"cells never visited"
                            )
                            stop_reason = f"the {max_requests}-request budget ran out mid-cell"
                            if cp is None:
                                # Uncheckpointed, this root is counted the way it
                                # always was; the rewind below is what replaces it.
                                progress_bar.update(1)
                            break
                        progress_bar.update(1)
                        roots_done = index + 1
                        mark_frames, mark_failed = len(frames), len(failed_cells)
                        mark_visited, mark_photos = cells_visited, raw_photo_count
                        maybe_commit()
            finally:
                # THE COMMIT GOES FIRST AND THE BAR IS CLOSED AFTER. Anything
                # that can raise between entering this block and committing
                # would cost the segment this block exists to save, and closing
                # a progress bar is exactly the kind of thing that raises on a
                # dead output stream -- the failure mode `progress()` was
                # written for (#167). Nothing below it can throw the commit
                # away.
                if cp is not None:
                    # REWIND TO THE LAST COMPLETED BOUNDARY -- a root's, or a
                    # retried cell's. A commit always writes the sweep as of
                    # one, and this is where that invariant is actually
                    # enforced -- for the budget stop above, for a host block,
                    # for a transport fault, for a bug. It is a no-op on the
                    # clean path, since the marks are taken at each boundary. A
                    # root interrupted between its pages has photos in hand,
                    # but a paged circle is not exhaustive until its last page,
                    # so committing those rows against a `roots_done` that
                    # excludes the root would leave the resume free to sweep it
                    # again -- the census would carry its early pages twice and
                    # every counter describing it would drift. Enforcing it
                    # here rather than per stop-path is also what lets the DFS
                    # stack stay un-persisted. `retry_pos` rewinds with the
                    # rest: a half-re-probed cell must commit as still failed.
                    del frames[mark_frames:]
                    del failed_cells[mark_failed:]
                    cells_visited = mark_visited
                    raw_photo_count = mark_photos
                    retry_pos = mark_retry
                    # The bonus half, not the mechanism: this catches a host
                    # block, a raising responder and the clean end of the loop.
                    # It does NOT catch a SIGTERM or a SIGKILL -- neither runs a
                    # finally -- which is why the periodic commit above exists
                    # and must not be "simplified" into this one.
                    try:
                        _commit_checkpoint(
                            cp,
                            frames,
                            roots_done=roots_done,
                            failed_cells=durable_failed(),
                            cells_visited=cells_visited,
                            raw_photo_count=raw_photo_count,
                            api_requests_total=prior_requests + api_requests,
                            bbox=bbox,
                            ipp=ipp,
                            root_count=len(roots),
                        )
                        frames.clear()
                        requests_at_last_commit = api_requests
                        final_commit_ok = True
                    except Exception as e:
                        # Best effort, the _write_owner posture: a checkpoint
                        # that cannot be written must never be what fails a
                        # sweep. The cost is re-paying this segment, not a
                        # wrong artifact -- and swallowing it here also means
                        # it cannot mask an in-flight exception.
                        #
                        # BROAD ON PURPOSE, and not the OSError this used to
                        # catch. A commit runs concat_census, to_parquet and
                        # json.dump, and their failures are not all OSError --
                        # pyarrow's ArrowIOError is, but ArrowInvalid is a
                        # ValueError. This finally sits on the re-raise path of
                        # HostBlockedError, so anything escaping it would
                        # replace a host block with a serialization error: no
                        # exit 81, no night-level breaker, and a scheduler that
                        # keeps asking a host which is refusing this IP -- the
                        # exact outcome #205/#208 exist to prevent, arriving
                        # through the error-handling path.
                        logger.error(
                            f"Could not commit the KartaView checkpoint at {cp.path}: "
                            f"{type(e).__name__}: {e}; "
                            f"{api_requests - requests_at_last_commit} requests will be re-paid"
                        )
                progress_bar.close()
                if retry_bar is not None:
                    retry_bar.close()
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
    finally:
        # Only ever fires when `cp` was never opened -- a rejected credential, a
        # host block during calibration, a bbox where no rung answers. `os.rmdir`
        # refuses a non-empty directory, so a real checkpoint is untouched.
        _remove_empty_checkpoint_dir(checkpoint_path)

    if stop_reason is not None and cp is not None:
        # Without a checkpoint the same stop falls through to the area guard
        # below, because `unvisited` put those cells in failed_cells: unmeasured
        # area, refuse. Nothing is finalized either way -- the difference is that
        # here the spend survives to be continued.
        raise spent(
            SweepIncompleteError(
                f"KartaView sweep for {city_name} stopped after {roots_done} of {len(roots)} "
                f"root cells ({stop_reason}); {api_requests} requests spent this process, "
                f"{prior_requests + api_requests} in total. Progress is checkpointed at "
                f"{cp.path}; re-running with the same checkpoint path continues it. Nothing "
                f"is finalized: a partial census must never be published as a dated snapshot.",
                checkpoint_path=cp.path,
                roots_done=roots_done,
                root_count=len(roots),
            )
        )

    # From here on the recorded set IS the session set: a sweep that got this
    # far either had no checkpoint (empty retry queue) or walked its whole
    # retry queue, so the rebinding is exact -- and it keeps the guard, the
    # caller's REQUEST_FAILED masking and the checkpoint reading one list.
    failed_cells = durable_failed()
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
            # Named so the operator knows what a retry costs and that the
            # reset is manual. Re-running with the checkpoint re-probes ONLY
            # the failed cells -- a refusal is time-varying, so asking again
            # genuinely can answer differently -- and everything already
            # answered stays paid for.
            resume_note = (
                ""
                if cp is None
                else (
                    f" Progress is checkpointed at {cp.path}; re-running with it re-probes "
                    f"just the {len(failed_cells)} failed cell(s), or delete that directory "
                    f"to force a fresh sweep."
                )
            )
            raise spent(
                DownloadError(
                    f"KartaView sweep failed: {detail} "
                    f"(> {MAX_FAILED_AREA_FRACTION:.0%} tolerated); refusing to "
                    f"finalize an incomplete snapshot.{resume_note}"
                )
            )
        # Under the threshold the run continues, but the caller must mark the
        # affected query points REQUEST_FAILED rather than let them look like
        # genuine no-imagery -- the same shape as an undownloaded Mapillary tile.
        logger.warning(f"Continuing with {detail}; affected query points marked REQUEST_FAILED")

    # How many rows the finally-commit could NOT write. Read before the splice
    # below, which mixes the on-disk parts into the same list; see the promotion
    # predicate for why a nonzero value forbids promotion.
    uncommitted_frames = len(frames)
    if cp is not None:
        # Committed parts FIRST, then whatever the finally-commit could not
        # write: index order is fetch order, which dedupe_census reads as
        # first-appearance position. Any other order reshuffles the published
        # CSV of every city, since the sweep re-sees ~pi/2 of everything.
        frames = _checkpoint_frames(cp) + frames
    census = concat_census(frames)
    # clear(), not `del`: the release matters the same either way (#157 -- the
    # per-part frames must not survive into dedupe's allocations), but `frames`
    # is now also a cell variable of _sweep_subtree/maybe_commit and deleting a
    # closed-over name reads as undefined to the linter.
    frames.clear()
    census = census_core.dedupe_census(census)

    # PROMOTION INTO THE SHARED CACHE (issue #290), the last thing before the
    # logging and the return, so it cannot coexist with a raise: every path that
    # refuses to finalize -- SweepIncompleteError, the failed-area guard, a host
    # block, a rejected credential -- has already left above, and the checkpoint
    # they leave behind is the caller's to resume from.
    #
    # THE PREDICATE IS FOUR-PART BECAUSE THE finally-COMMIT IS BEST-EFFORT. Its
    # failure is swallowed (a checkpoint must never fail a sweep that
    # succeeded), so the store on disk can legitimately lag the census in
    # memory -- and an entry that is complete by its own counters while missing
    # the last interval's rows is the one thing a reuser cannot detect. So:
    #
    #   final_commit_ok  the last commit actually landed;
    #   uncommitted      it wrote everything (frames were empty by then);
    #   roots_done       the lattice was swept to the end, not stopped short;
    #   failed sets      what it RECORDED as failed is what this session ended
    #                    with, so the reuser inherits the same holes we publish.
    #
    # Anything short of all four falls through to the caller's ordinary discard,
    # costing a future consumer a re-sweep and costing this run nothing.
    promoted = False
    if (
        cache_path
        and cp is not None
        and final_commit_ok
        and uncommitted_frames == 0
        and cp.roots_done == len(roots)
        and cp.failed_cells == failed_cells
    ):
        promoted = promote_checkpoint_to_cache(
            cp.path,
            cache_path,
            {
                "format_version": CENSUS_CACHE_FORMAT_VERSION,
                "provider": "kartaview",
                # RECORDED, never keyed -- the entry is reusable by any channel.
                "fetched_by": cp.channel,
                "fetched_variant": None,
                "crawl_started_at": cp.created_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "api_requests_total": prior_requests + api_requests,
                "failed": [_cell_to_dict(c) for c in failed_cells],
            },
        )

    # The checkpoint is NOT discarded here. It is the caller's, and it must
    # survive until the dated artifact is durable -- everything that writes one
    # happens after this returns, so a delete on this line would guarantee that
    # a crash in that tail re-pays the whole sweep, which is one of the four
    # interruptions #239 exists to cover. See discard_checkpoint().

    logger.info(
        f"Swept {cells_visited} cells ({len(roots)} roots at r={radius_m} m) for {city_name}: "
        f"{raw_photo_count} photo rows, {len(census)} unique, {api_requests} requests"
        + (f" this process, {prior_requests + api_requests} in total" if prior_requests else "")
    )
    return {
        "census": census,
        # THIS process's spend, for the additive daily ledger; see the Returns
        # docstring for why the cumulative figure must not take its place.
        "api_requests": api_requests,
        "api_requests_total": prior_requests + api_requests,
        "cells": len(roots),
        "cells_visited": cells_visited,
        "radius_m": radius_m,
        "raw_photo_count": raw_photo_count,
        # Summarized HERE, not by the caller, and that is a memory contract
        # rather than a convenience: write_census_grid_run POPS the census so it
        # can drop the frame before the CSV writes, so a caller counting these
        # itself would have to bind the census to a local and would pin every
        # row alive across both writes (issue #157). Mapillary's fetch
        # pre-counts its equivalents for exactly this reason.
        "num_images": len(census),
        "num_panos": int(census_core.census_is_pano(census).sum()),
        # Cells nothing came back for. Empty on a clean sweep; the caller
        # attributes the query points inside them to REQUEST_FAILED.
        "failed_cells": failed_cells,
        # None after a promotion: the directory MOVED into the cache, so the
        # caller's discard_checkpoint must not chase it.
        "checkpoint_path": None if promoted else checkpoint_path,
        # Provenance for the catalog. A fresh sweep names ITS OWN channel and
        # the instant its first commit landed, so a later reuser and the fetcher
        # agree on when KartaView was observed.
        "census_fetched_by": checkpoint_channel,
        "census_fetched_at": cp.created_at if cp is not None else None,
        "census_reused": False,
    }
