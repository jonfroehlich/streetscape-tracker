"""
Panoramax metadata downloader (issue #316, phase 2).

Panoramax is a federated open imagery commons founded by IGN and OpenStreetMap
France: ~120 M pictures across 23 registered instances, harvested into ONE
meta-catalog at api.panoramax.xyz. It is the third census provider — every
picture in an area, rather than the nearest one to a query point — so it binds
to the shared seam in `census.py` exactly as Mapillary and KartaView do, and
this module is shaped on `download_mapillary` rather than `download_kartaview`
because its primitive is a concurrently-fetched vector tile rather than a
serial radius sweep.

READ THIS BEFORE CHANGING THE INSTRUMENT. Phase 1 measured the alternatives and
`docs/experiments/panoramax-feasibility.md` is the record; the three findings
that decide this module's shape are:

  1. `/api/search` CANNOT COUNT and is never the census. It does not paginate
     (`links` is empty at limit=1, 1000 and 10000 alike), reports no
     `numberMatched`, SILENTLY IGNORES its own `datetime` filter (measured over
     20 cities: 5,045 pictures that the requested windows should have excluded
     all came back), and embeds a ~90-key EXIF blob per feature, so 10,000
     features is 75 MB. An incremental "everything since the last run" fetch
     built on it would re-read the whole history and report it as new.
  2. THE PER-PICTURE INSTRUMENT IS THE v1 `pictures` LAYER, WHICH STARTS AT z15.
     One row per picture carrying `id`, `ts` (capture), `type`, `account_id` and
     `first_sequence`. z15 rather than Mapillary's z14 is not a tunable — it is
     the coarsest zoom that serves the layer at all — and it costs about 4x as
     many tiles for the same bbox (catalog p50 35 tiles/city against
     Mapillary's 12, p95 900).
  3. `type` HAS NO ABSENT STATE. Summed federation-wide, 119,362,642 pictures =
     52,128,373 `equirectangular` + 67,234,269 `flat` + 0 unclassified, and not
     one of the 1,345,143 pictures phase 1 read off this layer carried an absent
     type. The "10-34% field of view absent" third state in #316 is an artifact
     of reading EXIF out of the SEARCH response: every one of those pictures
     that could be looked up is `flat` in the tiles. So `is_pano` is total here,
     which is what lets the census schema declare it non-nullable `"bool"` (see
     `census.census_is_pano` for what the other two declarations cost).

Collection model — identical to Mapillary's, because #116's stratification is
provider-agnostic: every 360 picture is a census row assigned to its nearest
frozen grid point; a point covered ONLY by flat imagery becomes one FLAT_ONLY
row with a null capture date; a point with neither becomes ZERO_RESULTS. That
yields the two coverage numbers that are never conflated, and for Panoramax
reporting BOTH is not optional — the mix is a per-city property, from 99.5% 360
in Des Moines to 0% in Tulsa and Boise.

NO CREDENTIAL. Reads are unauthenticated, so there is no token in the URL, no
`.env` entry to fail fast on, and no per-channel key isolation to build. What
replaces the credential as the thing to be careful with is the host itself: one
volunteer-run meta-catalog absorbing all of our load however wide the federation
grows, publishing no rate limit of any kind. Pacing is therefore deliberately
half Mapillary's (see DEFAULT_TILE_REQUESTS_PER_MINUTE) and 403/429 is a stop
rather than a retry.

The census CHECKPOINTS and is PROMOTED into the shared cache exactly as
Mapillary's is; the tile-keyed reassembly contract, the fails-open posture and
the caller-discards rule are all `docs/census.md`'s and are preconditions for
reading the checkpoint section below rather than background.
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

# Aliased for the reason download_mapillary aliases it: `census` is also the
# name of the local DataFrame this module passes around.
from . import census as census_core
from .analysis import EARLIEST_PLAUSIBLE_CAPTURE
from .census import dedupe_census
from .checkpointing import (
    CHECKPOINT_MAX_AGE_S,
    PANORAMAX_CHECKPOINT_FORMAT_VERSION,
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
from .config import PANORAMAX_METADATA_DTYPES
from .download_common import (
    HOST_PANORAMAX,
    AsyncRateLimiter,
    DownloadError,
    HostBlockedError,
    SweepIncompleteError,
    grid_bbox,
    points_in_tiles,
    redact_credentials,
    tile_frac_to_lonlat,
)
from .download_common import tiles_for_bbox as common_tiles_for_bbox
from .host_lock import host_lock
from .progress import progress

logger = logging.getLogger(__name__)

API_BASE = "https://api.panoramax.xyz/api"
# The v1 map endpoint. There is also a v2 (`/api/map/2/{z}/{x}/{y}.mvt`) whose
# `grid` layer serves H3 hexagon COUNTS at every zoom; that is the screen and
# measure instrument (issue #316 phase 1, and the standing screen), not this
# one. Both endpoints serve a `pictures` layer at z15+ and phase 1 measured them
# agreeing to p50 1.000, so v1 is used here simply because it is the one the API
# root advertises.
TILE_URL_TEMPLATE = API_BASE + "/map/{z}/{x}/{y}.mvt"
# The COARSEST zoom that serves per-picture rows, hence the cheapest. Not a
# tunable: below z15 the `pictures` layer is absent entirely and a city would
# collect zero panos rather than fail.
TILE_ZOOM = 15
PICTURE_LAYER = "pictures"
# Panoramax's own vocabulary for the two imagery types. Anything else is flat;
# see finding 3 in the module docstring for why there is no third bucket, and
# `image_type` in the run schema for how a future third value stays visible
# rather than being silently folded in here.
TYPE_360 = "equirectangular"

# Sent on every request. Panoramax is volunteer-run infrastructure with no
# credential to identify us, so the User-Agent is the ONLY way an operator there
# can tell what this traffic is or who to contact about it — which matters more
# here than on a metered commercial CDN, not less.
USER_AGENT = (
    "streetscape-tracker/1.0 (+https://github.com/jonfroehlich/streetscape-tracker; "
    "street-level imagery coverage research)"
)


# ── Slippy-map tile math ───────────────────────────────────────────────────


def tiles_for_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, zoom: int = TILE_ZOOM
) -> list[tuple[int, int]]:
    """
    All (x, y) z15 tile indices intersecting the bbox — this provider's default
    zoom over the shared :func:`download_common.tiles_for_bbox`.

    Note the zoom, which is the one number a reader coming from
    ``download_mapillary`` will assume: Panoramax's per-picture layer starts at
    z15, so the same bbox is ~4x the tiles.
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
    Number of z15 tile requests a run will make — the scheduler's cost hook, and
    the one this channel's per-city timeout is derived from.

    EXACT rather than estimated, and free: the lattice is counted from the
    frozen geometry with no network call and no imagery-dependent term, so
    unlike KartaView's sweep estimate there is no observed-versus-geometric
    correction to apply. Measured over the 20 richest cities phase 1 found:
    p50 414 tiles, p90 2,400, max 3,132 (~104 minutes at the shipped pace).
    """
    return len(
        tiles_for_bbox(*grid_bbox(center_lat, center_lon, grid_width, grid_height, step_length))
    )


def _points_in_tiles(
    lats: np.ndarray, lons: np.ndarray, tiles: list[tuple[int, int]], zoom: int = TILE_ZOOM
) -> np.ndarray:
    """This provider's ``unmeasured_mask``: grid points under an undownloaded tile."""
    return points_in_tiles(lats, lons, tiles, zoom)


# ── Tile decoding ──────────────────────────────────────────────────────────


def pictures_from_tile(
    tile_bytes: bytes, tile_x: int, tile_y: int, zoom: int = TILE_ZOOM
) -> list[dict[str, Any]]:
    """
    The `pictures` layer of one tile, one dict per picture.

    This is the only layer with per-picture rows, and therefore the only way to
    get capture DATES rather than counts. Keeps `id`, lon/lat, the capture
    timestamp `ts`, the imagery `type` VERBATIM, the contributor `account_id`
    and the owning sequence, plus the derived `is_pano` the census schema reads.

    ``type`` is carried through unchanged as well as reduced to ``is_pano``,
    deliberately: a run file records what the provider said, so a third `type`
    value Panoramax has never yet served would appear in the data as itself
    rather than being silently counted as flat by this function.

    Shared with ``scripts/panoramax_feasibility.py``, which imports it rather
    than keeping its own copy — the same rule that keeps the pacing formula in
    one place. A decoder rewritten beside its caller is how the study and the
    collector would come to disagree about what a picture is.

    Args:
        tile_bytes: the raw MVT body. Empty bytes decode to nothing.
        tile_x, tile_y: the tile's indices, needed to place its local
            coordinates back on the globe.
        zoom: the zoom those indices are at.
    """
    if not tile_bytes:
        return []
    decoded = mapbox_vector_tile.decode(tile_bytes)
    layer = decoded.get(PICTURE_LAYER)
    if not layer:
        return []
    extent = layer.get("extent", 4096)

    pictures = []
    for feature in layer["features"]:
        geometry = feature.get("geometry", {})
        if geometry.get("type") != "Point":
            continue
        props = feature.get("properties", {})
        # THE PROPERTY ONLY -- deliberately no fall back to the MVT feature id,
        # which download_mapillary's decoder does have. An MVT feature id is
        # TILE-LOCAL (mapbox_vector_tile numbers features 0, 1, 2 ... per tile),
        # so falling back to it would mint an id that collides with a different
        # picture in every other tile -- and `dedupe_census` factorizes on `id`,
        # so those collisions would silently collapse distinct pictures into one
        # across the whole city. A picture the layer does not name is dropped.
        picture_id = props.get("id")
        if picture_id is None:
            continue
        px, py = geometry["coordinates"]
        # decode() returns y-up tile-local coords; convert to global fractions.
        lon, lat = tile_frac_to_lonlat(tile_x + px / extent, tile_y + (1 - py / extent), zoom)
        image_type = props.get("type")
        account_id = props.get("account_id")
        sequence_id = props.get("first_sequence")
        pictures.append(
            {
                "id": str(picture_id),
                "lon": lon,
                "lat": lat,
                "ts": props.get("ts"),
                "type": image_type,
                "image_type": None if image_type is None else str(image_type),
                "is_pano": image_type == TYPE_360,
                "account_id": None if account_id is None else str(account_id),
                "sequence_id": None if sequence_id is None else str(sequence_id),
            }
        )
    return pictures


# The earliest date this provider could plausibly have imagery for. Read from
# analysis.py rather than spelled here so the collector's decode-time floor and
# every reader's cannot drift apart -- both existing census providers do the
# same. See analysis.EARLIEST_PLAUSIBLE_CAPTURE for why it is 2004 rather than
# Panoramax's own 2022 founding.
_EARLIEST_CAPTURE = EARLIEST_PLAUSIBLE_CAPTURE["panoramax"]
# The same floor as an aware Timestamp. Both the scalar and the vectorized rule
# compare TIMESTAMPS rather than calling `.dt.date`: on a column that parsed to
# all-NaT, `.dt.date` stays datetime64 instead of becoming objects and the
# comparison raises `InvalidComparison` -- i.e. a city whose every timestamp was
# unusable would fail the run rather than record it as undated.
_EARLIEST_CAPTURE_TS = pd.Timestamp(_EARLIEST_CAPTURE, tz=UTC)


def ts_to_iso_date(ts) -> str:
    """
    A tile capture timestamp -> 'YYYY-MM-DD' (UTC), or '' when unusable.

    Panoramax serves `ts` as a string, and phase 1 saw at least three shapes of
    it: '2025-11-02 00:24:37+00', an ISO 'T'/'Z' form, and a fractional-second
    form. It also serves a genuine SENTINEL — two Paris pictures dated
    1970-01-01, i.e. the Unix epoch — which is this provider's known analogue of
    Mapillary's epoch-zero device clocks and is what the floor below drops.

    Scalar reference implementation for :func:`ts_to_iso_dates`, which is what
    the collection paths actually call; a test pins the two together
    element-wise, so the rules live here in readable form and are stated once.
    """
    if not ts:
        return ""
    parsed = pd.to_datetime(pd.Series([ts]), format="ISO8601", utc=True, errors="coerce")[0]
    if pd.isna(parsed):
        return ""
    if parsed < _EARLIEST_CAPTURE_TS or parsed > pd.Timestamp.now(tz=UTC):
        return ""
    return parsed.date().isoformat()


def ts_to_iso_dates(values) -> pd.Series:
    """
    Vectorized :func:`ts_to_iso_date` over a column of tile timestamps.

    ``format="ISO8601"`` IS LOAD-BEARING and must not be dropped as redundant.
    Left to infer, pandas locks onto ONE format from the first non-null value
    and coerces every value at another precision to NaT — silently, since
    ``errors="coerce"`` is what makes a genuinely bad value survivable. That is
    #226 from one direction and KartaView's mixed-precision pages from another,
    and Panoramax mixes precisions too: a fractional-second `ts` beside a
    whole-second one nulls one of the two, whichever came second.

    Args:
        values: array-like of `ts` strings, nulls allowed.

    Returns:
        A str Series of 'YYYY-MM-DD' / '' values, aligned to the input.
    """
    ts = pd.to_datetime(
        pd.Series(values, dtype="object").reset_index(drop=True),
        format="ISO8601",
        utc=True,
        errors="coerce",
    )
    usable = ts.notna() & ts.ge(_EARLIEST_CAPTURE_TS) & ts.le(pd.Timestamp.now(tz=UTC))
    # Mask BEFORE formatting, exactly as the Mapillary parser does: pandas
    # represents timestamps Python's datetime cannot and then refuses to
    # strftime them, and those are the values `usable` has already rejected.
    return ts.where(usable).dt.strftime("%Y-%m-%d").fillna("").astype(str)


# Census columns, in decode order. Columnar rather than per-picture dicts for
# the reason issue #157 records, which binds harder here than on Mapillary: a
# leader city is 500,000+ pictures over a few thousand tiles.
#
# `is_pano` is non-nullable "bool" and that is a correctness choice, not a size
# one -- Panoramax's `type` has no absent state (module docstring, finding 3),
# so there is no null to represent, and the two wrong declarations fail in ways
# `census.census_is_pano` documents at length (an `object` column of Python
# bools marks the ENTIRE city FLAT_ONLY without raising).
_CENSUS_DTYPES = {
    "id": pd.StringDtype("pyarrow"),
    "lon": "float64",
    "lat": "float64",
    "ts": pd.StringDtype("pyarrow"),
    "image_type": pd.StringDtype("pyarrow"),
    "is_pano": "bool",
    "account_id": pd.StringDtype("pyarrow"),
    "sequence_id": pd.StringDtype("pyarrow"),
}


def records_to_census(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Panoramax's binding of :func:`census_core.records_to_census`."""
    return census_core.records_to_census(records, _CENSUS_DTYPES)


def concat_census(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Combine per-tile census frames into one, preserving tile order."""
    return census_core.concat_census(frames, _CENSUS_DTYPES)


def _panoramax_image_columns(picked: pd.DataFrame) -> dict[str, Any]:
    """
    Panoramax's own output columns: the copyright convention plus its extras.

    Handed to :func:`census_core.build_image_rows`, which fills the shared core.

    The copyright string carries the contributor account, matching Mapillary's
    `creator_id` and KartaView's `username` parity convention. It is NOT the
    per-picture licence: Panoramax publishes `license` and `geovisio:producer`
    only through `/api/search`, which the module docstring explains cannot be
    the census, so the honest thing to record from a tile is who took it.
    """
    account = picked["account_id"].astype("string")
    return {
        # Through the nullable string dtype rather than astype(str), so a
        # missing account stays missing instead of rendering "<NA>".
        "copyright_info": ("© Panoramax contributor " + account)
        .fillna("© Panoramax")
        .to_numpy(dtype=object),
        "account_id": account.to_numpy(dtype=object),
        "sequence_id": picked["sequence_id"].to_numpy(dtype=object),
        "image_type": picked["image_type"].to_numpy(dtype=object),
        "is_pano": picked["is_pano"].to_numpy(),
    }


def _panoramax_capture_dates(census_frame: pd.DataFrame, positions: np.ndarray) -> np.ndarray:
    """
    Capture dates for the census rows at ``positions``, per Panoramax's rules.

    Takes positions rather than a taken sub-frame so this indexes the ONE column
    it needs; see :func:`census_core.write_census_grid_run` (issue #157).
    """
    return ts_to_iso_dates(census_frame["ts"].to_numpy()[positions]).to_numpy()


def build_image_rows(
    census_frame: pd.DataFrame,
    image_positions: np.ndarray,
    query_lat,
    query_lon,
    query_timestamp: str,
    status,
    capture_date,
) -> pd.DataFrame:
    """PANORAMAX_METADATA_DTYPES rows for query locations matched to census images."""
    return census_core.build_image_rows(
        census_frame,
        image_positions,
        query_lat,
        query_lon,
        query_timestamp,
        status,
        capture_date,
        dtypes=PANORAMAX_METADATA_DTYPES,
        image_columns=_panoramax_image_columns,
    )


def build_empty_rows(query_lat, query_lon, query_timestamp: str, status) -> pd.DataFrame:
    """Rows for query locations with no imagery — ZERO_RESULTS and REQUEST_FAILED."""
    return census_core.build_empty_rows(
        query_lat, query_lon, query_timestamp, status, dtypes=PANORAMAX_METADATA_DTYPES
    )


# ── Download ───────────────────────────────────────────────────────────────


# Same posture and the same 2% as Mapillary's: tolerate a blip, refuse a hole.
# A fraction rather than a count for the reason recorded there — the threshold
# bounds the size of the unknown region in an immutable snapshot, it does not
# count requests.
MAX_FAILED_TILE_FRACTION = 0.02

# PUBLIC for the same reason Mapillary's is (#318): one tile's worst-case
# attempt is what a request cap has to be able to fund before launching is worth
# anything. Nothing prices a Panoramax launch floor yet -- the channel is in
# UNWIRED_CHANNELS -- but the constant is exported now so wiring it does not have
# to rediscover which number the floor comes from.
TILE_MAX_TRIES = 5
_TILE_MAX_TIME_S = 120

# Client-side pacing. NOTHING IS DOCUMENTED — no limit in the API docs, none in
# the OpenAPI spec, and no X-RateLimit-*/Retry-After header comes back — so per
# this repo's standing rule that is unknown rather than unlimited, and the
# conservative end is the right end for three reasons that all point the same
# way: api.panoramax.xyz is one volunteer-run meta-catalog taking all of our
# load; there is no credential, so we cannot even be identified and throttled
# individually before being blocked; and this host is reached from the same IP
# as the nightly batch, which is exactly how both Mapillary bans took out
# channels that had done nothing wrong.
#
# 30/min is phase 1's figure — half the Mapillary channels' configured rate
# against a host with strictly less published guidance, which is the intended
# direction of the asymmetry. It is not a measurement of anything Panoramax
# said; raising it is a volume change under the top-of-file rule in CLAUDE.md.
#
# What it costs, over the cities that would actually be enrolled rather than
# over the catalog: a p50 leader city is 414 z15 tiles (~14 min), p90 2,400
# (~80 min), max 3,132 (~104 min), against a 12 h batch deadline.
DEFAULT_TILE_REQUESTS_PER_MINUTE = 30

# Jitter fraction, the #292 shifted exponential. Non-zero by default for the
# reason the Mapillary channels are: after three per-IP blocks there, request
# REGULARITY is the property never varied between restarts, and a metronomic
# cadence from a datacenter IP is the one shape a rate-limiter's scorer reads
# most easily. Adopted here BEFORE any incident rather than after three.
DEFAULT_TILE_JITTER = 0.6

# An error page rather than a tile. A DENY-list, not an allow-list, for the
# reason #199 records: an allow-list would reject every tile the day the server
# relabels a real content type, halting collection entirely.
_TILE_ERROR_CONTENT_TYPES = ("text/html", "application/json")


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
    on_empty: Callable[[], None] | None = None,
) -> bytes:
    # Pacing and counting sit INSIDE the retried body (issue #198): this
    # function may issue up to TILE_MAX_TRIES requests, and taking one token in
    # the caller would let a retrying tile present five times the configured
    # rate — during a 5xx storm, i.e. when the host is least able to absorb it —
    # while under-reporting the same factor to the ledger.
    if rate_limiter is not None:
        await rate_limiter.acquire()
    if on_request is not None:
        on_request()
    # allow_redirects=False for the reason #199 made it load-bearing on
    # Mapillary: a host that answers a rate limit with a 302 to a login page
    # would otherwise deliver that page's perfectly good HTTP 200 to the
    # protobuf decoder, and a block would read as corrupt data.
    async with session.get(url, timeout=timeout, allow_redirects=False) as response:
        if response.status in (403, 429):
            # STOP, NEVER RETRY. There is no credential here, so 403 cannot be a
            # rejected token the way it is on Mapillary and KartaView — on this
            # host both of these mean the IP is refused, and retrying into a
            # refusal is reported to extend it.
            raise HostBlockedError(
                f"Panoramax refused this host (HTTP {response.status}). Reads are "
                f"unauthenticated, so this is a per-IP refusal rather than a "
                f"credential problem: stop, wait, and record what happened in "
                f"docs/provider-access.md before collecting again.",
                host=HOST_PANORAMAX,
            )
        if response.status in (301, 302, 303, 307, 308):
            location = redact_credentials(response.headers.get("Location", "(none)"))
            raise HostBlockedError(
                f"Panoramax redirected instead of serving a tile (HTTP "
                f"{response.status} → {location}). A redirect to a login or "
                f"error page means this host's IP is being refused; a redirect "
                f"anywhere else means the tile endpoint has moved and this code "
                f"needs updating.",
                host=HOST_PANORAMAX,
            )
        if response.status == 404:
            # A tile the host has nothing for — an ANSWER, not a failure, which
            # is the opposite of Mapillary's reading and is measured rather than
            # assumed: phase 1 saw 0 empty tiles across 3,321 z14 requests
            # including 20 cities that hold no imagery at all, because an empty
            # area comes back 200 with no layer. Counted rather than silent, so
            # the caller can refuse a run where EVERY tile 404s (see
            # _fetch_city_images) — which is what a moved endpoint looks like,
            # and would otherwise publish as "every pano in the city removed".
            if on_empty is not None:
                on_empty()
            return b""
        if response.status != 200:
            # 5xx raises ClientResponseError, which backoff retries.
            response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if any(bad in content_type.lower() for bad in _TILE_ERROR_CONTENT_TYPES):
            raise HostBlockedError(
                f"Panoramax served an error page instead of a vector tile "
                f"(HTTP 200, Content-Type: {content_type}). This is usually a "
                f"rate limit or a block on this host's IP, not a corrupt tile.",
                host=HOST_PANORAMAX,
            )
        return await response.read()


# ── The tile checkpoint on disk ────────────────────────────────────────────
#
# Layout and contract are Mapillary's, and deliberately so — the two are the
# same crawl shape, and `docs/census.md` states the rules once:
#
#     <checkpoint_path>/
#       state.json                the commit record; written LAST
#       tile-17462-24880.parquet  one committed tile's census rows
#
# PARTS ARE KEYED BY TILE (x, y), NOT BY FETCH ORDER, because tiles are fetched
# concurrently and completion order is nondeterministic; the reassembly order is
# RECOMPUTED from `tiles_for_bbox` rather than stored. That is the byte-identity
# mechanism, and without it `dedupe_census`'s first-position rule would resolve a
# border duplicate differently depending on which night fetched which copy —
# reading, in `diff.py`, as imagery churn indistinguishable from a real re-drive.
#
# ONLY SUCCESSFUL TILES ARE COMMITTED, so #168's tolerance keeps measuring
# against the FULL tile set. A ZERO-ROW TILE GETS A RECORD AND NO FILE, which
# matters more here than on Mapillary: at z15 most tiles over a real bbox are
# empty and a leader city is thousands of them.
#
# CHECKPOINTING FAILS OPEN. The trade differs from Mapillary's by degree rather
# than in kind — the worst city here is ~104 minutes rather than ~15 — but the
# conclusion is the same: a city must never fail over its own safety net, and
# what a lost checkpoint costs is a re-fetch, not an artifact.

CHECKPOINT_PART_TEMPLATE = "tile-{x}-{y}.parquet"

# Re-exposed under this module's own name; DECLARED in checkpointing.py beside
# the other providers' so the census-cache probe can tell whether an entry's
# commit record is one this loader reads without importing this module.
CHECKPOINT_FORMAT_VERSION = PANORAMAX_CHECKPOINT_FORMAT_VERSION


@dataclass
class TileCheckpoint:
    """Handle to an on-disk tile checkpoint, loaded or freshly opened."""

    path: str
    channel: str | None = None
    # What distinguishes two crawls of one city inside one channel -- a walk's
    # --network-type. None for a grid run, which has exactly one.
    variant: str | None = None
    # When the FIRST tile of this crawl was committed, carried forward by every
    # later write. The age cap is measured from here and never from a timestamp
    # that moves on writes committing no tile; see _commit_spend.
    created_at: str | None = None
    # (x, y) -> committed row count. Membership means "this tile is done"; the
    # count distinguishes a committed empty tile from a missing part.
    done: dict[tuple[int, int], int] = field(default_factory=dict)
    # Spend of the PREVIOUS invocations only.
    api_requests_before: int = 0
    # Latched by the first failed commit, so the warning is logged once and the
    # rest of the fetch runs uncheckpointed rather than retrying a directory
    # that has already proved unwritable.
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
    THIS lattice, ``(None, reason)`` otherwise. Raises nothing of its own.

    Factored out because a promoted cache entry IS a checkpoint directory that
    was moved, so it must be validated exactly as a resume is. What each caller
    adds differs: :func:`load_tile_checkpoint` adds the channel, the variant and
    the crawl's age; :func:`load_cached_census` adds the marker's own window and
    the one check a resume must NOT make, completeness.
    """
    if state["format_version"] != PANORAMAX_CHECKPOINT_FORMAT_VERSION:
        return None, (
            f"it is format v{state['format_version']}, this build writes "
            f"v{PANORAMAX_CHECKPOINT_FORMAT_VERSION}"
        )
    if not _bbox_matches(state["bbox"], bbox):
        return None, f"it covers bbox {state['bbox']}, this run uses {list(bbox)}"
    if int(state["zoom"]) != TILE_ZOOM:
        # The tile INDICES in every part name mean nothing without the zoom that
        # produced them, so this is checked even though only z15 is served.
        return None, f"it was fetched at z{state['zoom']}, this run uses z{TILE_ZOOM}"
    if int(state["tile_count"]) != len(tiles):
        return None, f"it covers {state['tile_count']} tiles, this run has {len(tiles)}"
    done = {(int(x), int(y)): int(rows) for x, y, rows in state["done_tiles"]}
    if not done.keys() <= set(tiles):
        return None, "it holds tiles this run's lattice does not contain"
    # Verify the parts from their FOOTERS — a seek to the end of each file —
    # rather than discovering a truncated one at reassembly, after the fetch has
    # already been paid for.
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

    NEVER RAISES: every failure degrades to "fetch every tile" with a warning. A
    checkpoint is not a comparison whose mismatch corrupts an artifact — the
    worst case of ignoring one is a re-spend, so refusing outright would cost a
    night to protect nothing.

    An unusable checkpoint is DELETED. Its parts are named for the tiles they
    hold, so a stale directory that is never resumed is also never overwritten.

    Args:
        path: the checkpoint directory. Need not exist.
        bbox: this run's frame. A different one means a different lattice.
        tiles: this run's tile list, from :func:`tiles_for_bbox`.
        channel: which api_usage channel this census meters into. The PATH
            already keys it, but the path is caller-built, so it is recorded and
            compared here too.
        variant: what separates two crawls of one city WITHIN one channel — a
            walk's ``--network-type``, None for a grid run. The half a wrong
            path cannot fix: two walks of one city agree on every geometric
            parameter and on the ledger.
    """

    def discard(reason: str) -> None:
        logger.warning(f"Ignoring the Panoramax tile checkpoint at {path}: {reason}")
        discard_checkpoint(path)

    state_path = _state_path(path)
    if not os.path.exists(state_path):
        return None  # the ordinary first-run case
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
        # MEASURED FROM created_at -- WHEN THE OLDEST ROW WAS FETCHED. Frozen
        # geometry never changes, so every other check still passes months later
        # and resuming would splice last quarter's rows into a snapshot dated
        # today. `updated_at` cannot carry this: _commit_spend rewrites the
        # record on a night that committed NO tile, and a host-blocked night
        # records no consecutive_failure, so a city would refresh its own clock
        # indefinitely. See checkpointing.CHECKPOINT_MAX_AGE_S.
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
        # Broad on purpose; see the NEVER RAISES note above.
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

    :func:`checkpointing.load_cached_store` — which owns the marker window, the
    ``run_date`` rule, the never-raise posture and the delete-what-is-refused
    contract — with this provider's two checks plugged in: the same
    geometric/footer cascade a resume makes, and COMPLETENESS, which a resume
    deliberately does NOT check and which is the whole difference between the
    two. A partial entry reused as a census would publish the missing tiles'
    grid points as genuine no-imagery — absence never observed — in an immutable
    dated snapshot.
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
        label="Panoramax census",
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
    write. Takes ``done`` rather than a checkpoint because it must also run when
    there is NO commit record: a process that died between its first
    ``to_parquet`` and its first ``state.json`` leaves a part that
    :func:`load_tile_checkpoint` returns too early to reach, and if that tile
    later commits as EMPTY the footer loop skips it on ``rows == 0`` and the
    file survives every later purge holding rows nothing will ever read.
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
        logger.warning(f"Could not tidy the Panoramax tile checkpoint at {path}: {e}")


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
    unusable checkpoint here is DELETED, so a directory created first would be
    the one the discard removes: the fresh handle would point at nothing, every
    commit would fail, ``degraded`` would latch on the first tile, and the city
    would fetch unprotected. That is the case the age cap produces — the first
    attempt after a multi-day block — so the one run that most needs protecting
    would be the one running without it.
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
    # process that died before its first commit record is still here.
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

    The ordering IS the commit point. ``state.json`` is written last and
    atomically, so a part that exists without being counted never happened —
    :func:`_purge_checkpoint_debris` sweeps it and the tile is refetched.

    Best effort, and it LATCHES: the first failure warns once and turns
    checkpointing off for the rest of the run.
    """
    if cp.degraded:
        return
    try:
        if len(frame):
            part = _tile_part_path(cp.path, x, y)
            tmp = f"{part}.tmp"
            frame.to_parquet(tmp, index=False)
            # FSYNCED BEFORE THE RENAME, and the directory after it. Without
            # them the part-then-state ordering holds against a process crash
            # (where the page cache survives) but not against a power loss,
            # where the two renames may reach the disk in either order — losing
            # the WHOLE checkpoint rather than the last tile.
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
    """
    created_at = cp.created_at or datetime.now(UTC).isoformat()
    state = {
        "format_version": PANORAMAX_CHECKPOINT_FORMAT_VERSION,
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

    Without this the crawl total on the catalog row under-reports a night that
    ended badly: the requests a block refused are counted into ``api_usage``
    deliberately, but would die with the process.

    Skipped when nothing was committed, so a city refused at request 1 leaves no
    directory behind. THIS IS THE WRITE THAT MUST NOT AGE A CHECKPOINT —
    ``created_at`` is carried forward instead; see :func:`_write_checkpoint_state`.
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
    with, because that order is the whole byte-identity mechanism.

    Failed tiles are INHERITED from the marker rather than re-probed: the reuser
    is republishing the same observation, so the same grid points read
    REQUEST_FAILED in both artifacts. The one reader for whom that is wrong —
    the crawl's OWN channel re-finalizing after a tail crash — never reaches
    here, because :func:`checkpointing.reconcile_cache_hit` hands such an entry
    back to its checkpoint first.
    """
    store, marker = cached
    fetched_by = marker.get("fetched_by")
    crawl_started_at = marker.get("crawl_started_at")
    # WARNING, not INFO: a collection that issues no request is otherwise
    # indistinguishable from a real one in its artifact, and this line is what
    # an operator reading the per-attempt log has to tell them apart.
    logger.warning(
        f"REUSING the Panoramax census fetched by {fetched_by} (crawl started "
        f"{crawl_started_at}) for {city_name}: 0 tile requests"
    )
    results = [_checkpoint_frame_for_tile(store, x, y) for (x, y) in tiles if (x, y) in store.done]
    raw_feature_count = sum(len(r) for r in results)
    census = concat_census(results)
    # The same release the fetch path makes before the dedup copy (issue #157).
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
        # columns: the accounting all providers must agree on, from one place.
        **reused_census_provenance(marker, channel=checkpoint_channel, variant=checkpoint_variant),
    }


async def fetch_city_images_async(
    city_name: str,
    bbox: tuple[float, float, float, float],
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    max_requests: int | None = None,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    checkpoint_variant: str | None = None,
    census_cache: CensusCache | None = None,
) -> dict[str, Any]:
    """
    Fetch a city's Panoramax tile census, serialized against other processes.

    Every Panoramax request in the repo passes through here, which is what makes
    this the one place the machine-wide host lock has to be taken. The
    ``AsyncRateLimiter`` bounds THIS process; the lock supplies the other half by
    ensuring no second process is pacing itself at the same time (issue #208).

    See :func:`_fetch_city_images` for the arguments and return value.

    Raises:
        HostBusyError: another process on this machine is already talking to
            Panoramax. Raised before any request is issued.
    """
    # The lock hold covers the CHECKPOINT as well as the requests, which is why
    # the checkpoint needs no lock of its own.
    with host_lock(HOST_PANORAMAX):
        try:
            return await _fetch_city_images(
                city_name,
                bbox,
                connection_limit=connection_limit,
                request_timeout=request_timeout,
                max_requests_per_minute=max_requests_per_minute,
                jitter=jitter,
                max_requests=max_requests,
                checkpoint_path=checkpoint_path,
                checkpoint_channel=checkpoint_channel,
                checkpoint_variant=checkpoint_variant,
                census_cache=census_cache,
            )
        except BaseException:
            # A city that failed before committing anything would otherwise
            # leave an empty directory behind on every attempt. os.rmdir refuses
            # a non-empty one, so a real checkpoint is never touched.
            _remove_empty_checkpoint_dir(checkpoint_path)
            raise


async def _fetch_city_images(
    city_name: str,
    bbox: tuple[float, float, float, float],
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    max_requests: int | None = None,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    checkpoint_variant: str | None = None,
    census_cache: CensusCache | None = None,
) -> dict[str, Any]:
    """
    Fetch and dedupe every Panoramax picture in a bbox from the z15 tiles.

    Extracted from :func:`download_panoramax_metadata_async` so a future
    road-walk collector can share the same fetch and decode: the two would differ
    only in what they assign pictures TO afterwards.

    Args:
        city_name: label for logging/progress only.
        bbox: (min_lon, min_lat, max_lon, max_lat), e.g. from grid_bbox.
        connection_limit: max concurrent tile fetches.
        request_timeout: per-request timeout in seconds.
        max_requests_per_minute: client-side pacing cap. <= 0 disables pacing.
        jitter: the #292 gap coefficient of variation.
        checkpoint_path: directory to resume from and commit into, or None for
            fetch-everything. Built by the caller, because only the caller knows
            the channel — see :func:`checkpointing.checkpoint_path_for`.
        checkpoint_channel: the api_usage channel this census meters into,
            recorded so a checkpoint cannot be resumed under a different one.
        checkpoint_variant: what separates two crawls of one city within that
            channel — a walk's ``--network-type``, None for a grid run.
        census_cache: the shared per-(provider, city, bbox) cache entry and how
            this caller may use it (issue #290), also caller-built. Given one, a
            COMPLETE census another consumer already paid for is reused here for
            zero requests, and a census this call completes is PROMOTED into it
            on the way out.

    Returns:
        Dict with ``census``, ``api_requests`` (THIS call's spend, for the
        additive daily ledger), ``api_requests_total`` (the whole crawl's, for
        the catalog row), ``tiles``, ``raw_feature_count``, ``num_images``,
        ``num_panos``, ``failed_tiles``, ``checkpoint_path`` (None after a
        promotion — the directory MOVED) and the census provenance
        (``census_fetched_by`` / ``census_fetched_at`` / ``census_reused``).

    Raises:
        DownloadError: on a refusal or transport failure, carrying
            ``api_requests`` so the caller can still record what it spent.
    """
    if max_requests is not None and checkpoint_path is None:
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
        # programming error and never for an operator.
        raise ValueError(
            "max_requests needs a checkpoint_path: a capped crawl stops part-way, "
            "and with nothing to resume from that discards everything it spent."
        )
    tiles = tiles_for_bbox(*bbox)
    logger.info(
        f"Fetching Panoramax metadata for {city_name}: {len(tiles)} z{TILE_ZOOM} "
        f"tiles covering bbox {tuple(round(v, 4) for v in bbox)}"
    )

    # THE CACHE IS CONSULTED BEFORE THE CHECKPOINT IS OPENED (issue #290). The
    # two answer different questions: a checkpoint is THIS crawl's unfinished
    # work, a cache entry is a COMPLETE observation somebody already paid for
    # over the identical lattice. If a usable entry exists there is nothing left
    # to fetch, so opening a checkpoint first would create a directory, resume a
    # partial crawl and re-request tiles the answer for is already on disk. A hit
    # is then RECONCILED with whatever sits at checkpoint_path. This whole block
    # runs inside the host lock.
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
    empty_tiles = 0
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    semaphore = asyncio.Semaphore(connection_limit)
    rate_limiter = AsyncRateLimiter(max_requests_per_minute, jitter=jitter)
    if max_requests_per_minute <= 0:
        logger.info("Tile pacing DISABLED (max_requests_per_minute <= 0)")
    elif jitter > 0:
        # Logged as the gap DISTRIBUTION, not as a rate: the rate is what the
        # metronome also said, and the shape is the property being chosen.
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
        desc=f"Downloading Panoramax tiles for {city_name}",
        unit="tile",
        logger=logger,
    )

    def count_request() -> None:
        nonlocal api_requests
        api_requests += 1

    def count_empty() -> None:
        nonlocal empty_tiles
        empty_tiles += 1

    def interrupted(err: DownloadError) -> DownloadError:
        """
        Stamp an interrupted census's two counters and persist what it spent.

        Every path that leaves this function without a census owes the caller
        the same three things, so it is written once. The counters are
        deliberately different numbers: ``api_requests`` is THIS process's spend,
        for the additive (date, provider) ledger, and the total is the whole
        crawl's, for the operator and the catalog row.
        """
        total = _census_requests_total(checkpoint, api_requests)
        err.api_requests = api_requests
        err.api_requests_total = total
        _commit_spend(checkpoint, bbox=bbox, tiles=tiles, api_requests_total=total)
        return err

    # First whole-city condition seen: a refusal or an error page. Every
    # remaining tile would fail identically, so stop issuing them (issue #205).
    fatal: DownloadError | None = None

    # The cap tripped: stop DISPATCHING, and pause rather than fail (issue
    # #318). A SEPARATE flag from `fatal` rather than a second meaning for it,
    # because the two say opposite things about the tiles they skip. `fatal`
    # means every remaining tile would fail identically, so the city is over;
    # this means every remaining tile is perfectly fetchable and we are simply
    # out of budget for tonight, so they are owed to tomorrow. Folding them
    # together would make one of the two exit codes wrong whichever way it went.
    capped = False

    async def fetch_one(x: int, y: int) -> pd.DataFrame:
        nonlocal fatal, capped
        url = TILE_URL_TEMPLATE.format(z=TILE_ZOOM, x=x, y=y)
        # Per-tile, alongside the whole-city counter: the commit below needs to
        # know whether THIS tile 404ed, not how many did.
        answered_404 = False

        def note_empty() -> None:
            nonlocal answered_404
            answered_404 = True
            count_empty()

        async with semaphore:
            # The abort check belongs HERE, inside the semaphore: gather starts
            # every task at once and each runs to its first suspension point, so
            # a check above this line would be evaluated by all N tasks before
            # any response came back. Behind the semaphore, tasks resume a few at
            # a time, see the flag, and return without taking a token.
            if fatal is not None:
                # Keep the progress bar honest: a city that stopped at request 1
                # must not read like a city that hung at tile 3.
                progress_bar.update(1)
                return records_to_census([])
            if max_requests is not None and api_requests >= max_requests:
                # THE CAP IS CHECKED HERE FOR THE REASON THE ABORT ABOVE IS, and
                # it inherits the same bound: tasks already past this line
                # finish, and each may spend up to TILE_MAX_TRIES requests, so
                # the overshoot is at most connection_limit * TILE_MAX_TRIES
                # (25 at the grid defaults) rather than the whole city. That is
                # deliberate and documented on the CLI flag: stopping requests
                # already in flight would mean cancelling a paced, retrying
                # fetch mid-attempt, which buys ~25 requests and costs the
                # guarantee that every request we made was counted.
                #
                # Returning an empty census WITHOUT committing is what makes
                # this resumable: an uncommitted tile is not in `done`, so the
                # next invocation's `todo` still holds it. The empty frame never
                # reaches the census -- the raise below happens before the
                # settle loop, precisely so a skipped tile cannot be read as a
                # tile observed to be empty.
                capped = True
                progress_bar.update(1)
                return records_to_census([])
            try:
                # Pacing/counting happen inside _fetch_tile, per retried attempt.
                tile_bytes = await _fetch_tile(
                    session, url, timeout, rate_limiter, count_request, note_empty
                )
            except DownloadError as e:
                # ONLY DownloadError trips the abort. Per-tile failures (a 5xx
                # that exhausted its retries, a decode error) must still fan out
                # to every tile — that is #168's guarantee that one bad tile
                # cannot discard a city.
                fatal = fatal or e
                raise
        progress_bar.update(1)
        # Convert to columns HERE, not after the gather: asyncio.gather holds
        # every tile's result until the last one lands, so returning dicts would
        # keep the entire city's per-picture dicts alive at once (issue #157).
        frame = records_to_census(pictures_from_tile(tile_bytes, x, y))
        if checkpoint is not None and not answered_404:
            # Synchronous, inside the coroutine: the loop is single-threaded, so
            # this is atomic with respect to every other tile's commit, and the
            # host lock rules out another process. Only a SUCCESSFUL tile gets
            # here — a failure raised above, and stays refetchable.
            #
            # A 404 is deliberately NOT committed, even though this run reads it
            # as an empty tile. The moved-endpoint guard below is evidence that
            # only exists per invocation — it asks whether everything REQUESTED
            # answered 404 — so committing one spends that evidence: the run
            # that correctly refuses would leave a checkpoint in which every
            # tile is recorded fetched-and-empty, and the next invocation would
            # find nothing to do, skip the guard, and re-finalize the city from
            # disk as a genuine ZERO_RESULTS snapshot for 0 requests (then
            # promote that into the shared #290 cache). Leaving it uncommitted
            # re-asks the question every night, which is the only honest thing
            # to do with a tile whose meaning a single response cannot settle.
            # The cost is paid by the case measured at zero in 3,321 phase-1
            # requests: a city with a scattered 404 never completes its
            # checkpoint, so it re-fetches that tile on a resume and its cache
            # entry is refused (and deleted) on the next read.
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
        # return_exceptions: one bad tile out of thousands must not discard the
        # whole city (issue #168).
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            settled = await asyncio.gather(
                *(fetch_one(x, y) for x, y in todo), return_exceptions=True
            )
    except DownloadError as e:
        # Called for its side effects, then a BARE re-raise: it is the same
        # exception object, and `raise ... from e` would chain it to itself.
        interrupted(e)
        raise
    except (TimeoutError, aiohttp.ClientError) as e:
        error = DownloadError(f"Panoramax tile download failed: {redact_credentials(e)}")
        raise interrupted(error) from e
    finally:
        progress_bar.close()

    # This block wins over the loop's DownloadError arm below, which is dead on
    # current code: `fetch_one` assigns `fatal` before it re-raises, so such an
    # error is always ALSO in `settled`, and raising it here reports the error
    # that actually caused the abort rather than whichever one sits earliest in
    # tile order. Both are kept, and what the pair guards is a future edit that
    # makes `fetch_one` swallow that error — then every aborted tile becomes an
    # empty SUCCESS, `failed_tiles` is empty, and a 0-pano census registers,
    # publishes and diffs as "every pano in the city removed".
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
    if capped:
        committed = len(checkpoint.done) if checkpoint is not None else 0
        detail = (
            f"Panoramax tile census for {city_name} stopped at its "
            f"{max_requests:,}-request cap with {committed}/{len(tiles)} tiles fetched; "
            f"{api_requests} requests spent this process, "
            f"{_census_requests_total(checkpoint, api_requests)} in total."
        )
        if checkpoint is None or checkpoint.degraded:
            # NOT a pause, because there is nothing to resume FROM: no
            # checkpoint at all (an unwritable directory --
            # `_open_tile_checkpoint` fails open), or one whose commits latched
            # off mid-crawl. A plain DownloadError takes none of the exit-83
            # amnesty and counts a real failure, which is the honest answer:
            # calling this progress would tell an operator to re-run a command
            # that spends the same requests and stops in the same place,
            # forever. The caller is refused a cap without a checkpoint PATH at
            # all (see the guard above); this is the runtime half of the same
            # rule, and it is why that guard is not enough on its own.
            raise interrupted(
                DownloadError(f"{detail} Nothing is checkpointed, so nothing can be resumed.")
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
            )
        )

    fetched: dict[tuple[int, int], pd.DataFrame] = {}
    failed_tiles: list[tuple[int, int]] = []
    first_error: BaseException | None = None
    # `todo`, not `tiles`: a resumed run only attempted the missing ones.
    for (x, y), outcome in zip(todo, settled, strict=True):
        if isinstance(outcome, BaseException):
            # Unreachable on current code (the `fatal` block above raises
            # first); kept as the belt to that braces.
            if isinstance(outcome, DownloadError):
                raise interrupted(outcome)
            failed_tiles.append((x, y))
            first_error = first_error or outcome
        else:
            fetched[(x, y)] = outcome

    # EVERY TILE ANSWERED 404 — refuse rather than publish a city as empty.
    # A 404 is an ordinary "nothing here" on this host (see _fetch_tile), and a
    # genuinely empty area answers 200 with no layer, so a whole lattice of them
    # is what a MOVED OR RENAMED ENDPOINT looks like, not what an empty city
    # looks like. Without this the run would finalize 0 panos, and against the
    # previous snapshot `diff.py` would report every pano in the city removed.
    #
    # Bounded by `len(todo) >= 2`, and it is `todo` rather than `tiles` because
    # a RESUMED run only asks for what it is missing -- a moved endpoint has to
    # be caught there too, or a resume would assemble the census from committed
    # tiles alone and publish a partial city as a complete one. Two is the bar
    # because a single 404 is a hole worth one tile while two, against a
    # measured baseline of zero in 3,321 requests, is a moved endpoint.
    if len(todo) >= 2 and empty_tiles == len(todo):
        error = DownloadError(
            f"Every one of the {len(todo)} Panoramax tiles requested for {city_name} "
            f"answered HTTP 404. An empty area answers 200 with no picture layer, so "
            f"this means the tile endpoint ({TILE_URL_TEMPLATE}) has moved or been "
            f"renamed — refusing to finalize a snapshot claiming the city holds no "
            f"imagery."
        )
        raise interrupted(error)

    if failed_tiles:
        # Denominator is the FULL tile set, not this invocation's share: the
        # tolerance asks what fraction of the city is unmeasured, and a tile a
        # previous night already fetched is measured.
        failed_fraction = len(failed_tiles) / len(tiles)
        detail = f"{len(failed_tiles)}/{len(tiles)} tiles failed: {redact_credentials(first_error)}"
        if failed_fraction > MAX_FAILED_TILE_FRACTION:
            error = DownloadError(
                f"Panoramax tile download failed: {detail} "
                f"({failed_fraction:.1%} > {MAX_FAILED_TILE_FRACTION:.0%} tolerated); "
                f"refusing to finalize an incomplete snapshot"
            )
            raise interrupted(error) from first_error
        logger.warning(f"Continuing with {detail}; affected grid points marked REQUEST_FAILED")

    # REASSEMBLE IN TILE ORDER, taking each tile from this run if it was fetched
    # now and from its part file otherwise. This is the whole byte-identity
    # mechanism: `gather` preserves argument order, so an uninterrupted run
    # produces exactly this sequence, and concat + dedupe therefore see
    # positionally identical input however the work was split across nights.
    results = []
    for tile in tiles:
        if tile in fetched:
            # pop, so the only surviving reference is the one in `results`.
            results.append(fetched.pop(tile))
        elif tile in done:
            results.append(_checkpoint_frame_for_tile(checkpoint, *tile))
        # else: it failed this run and no earlier one committed it. Already in
        # failed_tiles, and the caller marks its points REQUEST_FAILED.

    raw_feature_count = sum(len(r) for r in results)
    census = concat_census(results)
    # ALL THREE names have to go before the dedup copy: they hold references to
    # the same per-tile frames, so dropping fewer frees nothing (issue #157).
    del results, settled, fetched
    census = dedupe_census(census)

    # PROMOTION IS THE LAST STATEMENT BEFORE THE RETURN, and that placement is
    # the safety argument (issue #290). Everything above it can raise, and a
    # raise must leave the checkpoint exactly where the caller expects to resume
    # from; here, nothing between the move and the return can fail.
    #
    # `not degraded` is the completeness guarantee: a checkpoint whose commits
    # latched off is missing tiles it never recorded, and an incomplete entry
    # reused as a census would publish absence that was never observed.
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
                "panoramax",
                # RECORDED, never keyed: the entry is reusable by any channel,
                # and this is what lets the catalog say who actually paid.
                fetched_by=checkpoint.channel,
                fetched_variant=checkpoint.variant,
                crawl_started_at=checkpoint.created_at,
                api_requests_total=total,
                failed=[[int(x), int(y)] for x, y in failed_tiles],
            ),
        )

    return {
        "census": census,
        "api_requests": api_requests,
        "api_requests_total": total,
        "checkpoint_path": None if promoted else (checkpoint.path if checkpoint else None),
        "tiles": len(tiles),
        "raw_feature_count": raw_feature_count,
        # Summarized HERE, where the census is already in hand: the caller may
        # not bind it to a local (write_census_grid_run pops and releases it).
        "num_images": len(census),
        "num_panos": int(census_core.census_is_pano(census).sum()),
        "failed_tiles": failed_tiles,
        "census_fetched_by": checkpoint_channel,
        "census_fetched_at": checkpoint.created_at if checkpoint else None,
        "census_reused": False,
    }


async def download_panoramax_metadata_async(
    city_name: str,
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float,
    output_csv_gz_path: str,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    max_requests: int | None = None,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    census_cache: CensusCache | None = None,
) -> dict[str, Any]:
    """
    Fetch Panoramax picture metadata for a city and write it as a run csv.gz.

    Same calling convention as the other providers' downloaders MINUS the
    credential: reads here are unauthenticated, so there is no ``access_token``
    parameter and nothing for the CLI's fail-fast credential check to find.

    The checkpoint is the caller's to DISCARD, once the run row is committed.
    Nothing here removes it: this function returns after writing the CSV and the
    caller still has stats, the runs row, the JSON and the diff to do, so a
    delete issued here would guarantee that a crash in that tail costs the whole
    census again.

    Returns:
        Dict with:
            df: DataFrame containing the metadata (PANORAMAX_METADATA_DTYPES)
            filename_with_path: the written .csv.gz path
            api_requests: number of tile requests issued this call
            api_requests_total: the census's spend across every invocation
            checkpoint_path: the checkpoint to discard, or None (None too when
                the census was promoted into the shared cache, issue #290)
            census_fetched_by / census_fetched_at: which channel paid for this
                census and when it observed Panoramax, for the ``runs`` row
            num_flat_images: the flat census magnitude (issue #116)
            started_at / finished_at: UTC ISO 8601 timestamps
    """
    started_at = datetime.now(UTC).isoformat()

    # Checked before a single tile is fetched, though write_census_grid_run
    # re-checks as it takes ownership of the write: one implementation, called
    # at the point where failing is free.
    census_core.prepare_output_path(output_csv_gz_path)

    # Built before the fetch (its bbox bounds the tile set) and consumed after.
    grid = census_core.build_grid(center_lat, center_lon, grid_width, grid_height, step_length)

    fetched = await fetch_city_images_async(
        city_name,
        grid.bbox,
        connection_limit=connection_limit,
        request_timeout=request_timeout,
        max_requests_per_minute=max_requests_per_minute,
        jitter=jitter,
        max_requests=max_requests,
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
    # would pin the whole thing alive through both CSV writes (issue #157).
    num_images = fetched["num_images"]
    num_panos = fetched["num_panos"]
    logger.info(
        f"Decoded {fetched['raw_feature_count']} features "
        f"({num_images} unique: {num_panos} panos, {num_images - num_panos} flat) "
        f"from {fetched['tiles']} tiles"
    )

    # The tail is wrapped because the checkpoint changes what a crash HERE
    # costs: with one, the next invocation re-finalizes from disk for ~0
    # requests, so a tail failure that carried no spend would land this census's
    # tiles in no ledger, ever.
    try:
        written = census_core.write_census_grid_run(
            fetched,
            grid,
            output_csv_gz_path,
            query_timestamp,
            capture_dates_for=_panoramax_capture_dates,
            image_columns=_panoramax_image_columns,
            dtypes=PANORAMAX_METADATA_DTYPES,
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
        # picture, including those at points that also hold a pano. Not
        # reconstructable from the CSV (flat-only points collapse to one
        # FLAT_ONLY row), so it is threaded to the catalog separately.
        "num_flat_images": written["num_flat_images"],
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }
