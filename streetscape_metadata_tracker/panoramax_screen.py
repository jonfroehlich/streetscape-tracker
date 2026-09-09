"""
The standing Panoramax growth screen: is there imagery in a city, yet? (#316)

Panoramax is the one provider we track whose deployments are NEW. Boise's
entire 54,592-picture corpus was three months old when phase 1 measured it and
Lincoln's ten months, so unlike GSV there is no archive to backfill: a temporal
series exists only if we are already watching when the imagery lands. That is
what this module is — not a collector, and not a sampler of the collector's
work, but a cheap standing instrument that re-asks "does this city hold
anything at all?" over the WHOLE catalog on a weekly cadence.

WHY IT IS NEARLY FREE, AND WHY THAT IS THE WHOLE DESIGN. The screen reads the
v2 map endpoint's `grid` layer at **z6**, where one tile spans ~5.6 degrees of
longitude and carries H3 hexagons with per-hexagon picture counters. The 1,144
enabled cities touch **113 distinct z6 tiles** between them, so one pass over
the entire catalog costs 113 requests — against 64,650 tiles for an exact z14
measure and 236,808 for the z15 census the collector runs. Cities share tiles;
that sharing is the saving.

A ZERO IS CONCLUSIVE AND A POSITIVE ONE IS NOT. A res-6 hexagon is roughly
36 km2 and the median catalog city is 19.5 km2, so a city's screen sums
hexagons LARGER than the city inside them: every number here is an UPPER
BOUND. That asymmetry is the point and it is the only reason 113 requests can
answer anything — an upper bound of zero means the city holds no imagery, full
stop, while a positive one means only "look closer", which is what the
collector (or `scripts/panoramax_feasibility.py --stage measure`) is for. Every
name in this module says `upper_bound` for that reason.

THE INSTRUMENT IS v2, NOT v1, AND THAT WAS MEASURED. The obvious screen is v1's
`grid` — a 0.1-degree lattice, and the layer the API root's `xyz` link points
at. It is **lossy**: over three z6 tiles it reported 2.5%, 7.9% and 23.9% fewer
pictures than v2's H3 grid over the identical extent, and it omits whole
populated cells rather than under-counting populated ones. A lossy screen is not
a slightly worse screen here, it is a broken one: the design rests on a zero
being conclusive. Phase 1 found this with a control group and moved the default;
the v1 decoders stay in the feasibility script, which is the only thing that
still needs them for that comparison.

WHERE THE DECODERS LIVE. `hexes_from_tile` and friends were phase 1's, in
`scripts/panoramax_feasibility.py`. They move HERE, and the script imports them,
for the same reason the collector's per-picture decoder moved and the script
imports that: a decoder rewritten beside its second caller is how a study and
the instrument it justifies come to disagree about what they counted. This
module owns the v2 `grid` layer; `download_panoramax` owns the v1 `pictures`
layer; neither reimplements the other.

PACING AND REFUSALS ARE THE COLLECTOR'S, IMPORTED. Same host, same absence of
any documented limit, same per-IP exposure — so the screen paces at the same
30/min with the same #292 jitter, takes the same machine-wide
`host_lock(HOST_PANORAMAX)`, and reads HTTP status through the collector's
`_fetch_tile`, which is the single place in the repo that says what a 403, a
redirect, a 404 or an HTML body means on this host. 113 requests a week is a
rounding error against a night of collection, and it still goes through the
lock: the lock is not about volume, it is about two processes pacing
independently into one volunteer-run meta-catalog.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

import aiohttp
import mapbox_vector_tile

from .download_common import (
    HOST_PANORAMAX,
    AsyncRateLimiter,
    DownloadError,
    grid_bbox,
    redact_credentials,
    tile_frac_to_lonlat,
)
from .download_common import tiles_for_bbox as common_tiles_for_bbox

# `_fetch_tile` is imported under its private name deliberately, rather than
# copied. It is where this repo decides that a 403 or 429 is a per-IP refusal
# (there is no credential, so it cannot be a rejected token), that a redirect is
# a block or a moved endpoint, that a 404 is an EMPTY TILE rather than a failure,
# and that an HTML body behind a 200 is an error page. Those readings are host
# properties, not collector properties: a second copy here would be a second
# place to get them wrong, and the screen is the instrument whose wrong answer is
# hardest to notice.
from .download_panoramax import (
    API_BASE,
    DEFAULT_TILE_JITTER,
    DEFAULT_TILE_REQUESTS_PER_MINUTE,
    USER_AGENT,
    _fetch_tile,
)
from .host_lock import host_lock
from .progress import progress

logger = logging.getLogger(__name__)

# The v2 map endpoint. v1's `grid` layer exists at z0-z6 too and is what the API
# root advertises; see the module docstring for why it is not used.
SCREEN_URL_TEMPLATE = API_BASE + "/map/2/{z}/{x}/{y}.mvt"

# The screen zoom. Not a tunable: 113 tiles for the whole catalog is a property
# of z6, and z5 would fold cities into hexagons coarse enough that a zero stops
# meaning anything about a city.
SCREEN_ZOOM = 6

# The layer both zooms serve. `pictures` (the collector's) starts at z15.
SCREEN_LAYER = "grid"

# The exact-measure zoom, where v2's hexagons are res 11 (~25 m across) and the
# centre-assignment approximation in `hexes_in_bbox` becomes noise. Bounded and
# manual only: all 414 screened-positive cities is ~51,000 tiles, about 28 h.
MEASURE_ZOOM = 14

# How far the bbox is grown before z6 tiles are enumerated.
#
# A hexagon is returned CLIPPED to the tile that carries it, and `merge_hexes`
# reconstructs the whole hexagon by unioning those pieces — so a hexagon
# overlapping the city but straddling a z6 tile seam is only correctly extended,
# and correctly counted, if BOTH tiles were fetched. A res-6 hexagon is about
# 7 km across, comfortably inside this margin. 108 of 1,144 catalog cities sit
# within one margin of a seam, 49 of them screening zero, so without this the
# "a zero is conclusive" claim would rest at those cities on tiles nobody read.
#
# Numerically equal to the feasibility script's v1 lattice cell size, and that
# is a coincidence of two different arguments landing on 0.1 degrees; the two
# constants are deliberately not shared.
SCREEN_MARGIN_DEG = 0.1

# Per-request timeout. A z6 tile of the v2 grid is small (the whole catalog's
# 113 came back in well under a minute of transfer), so this is generous.
SCREEN_REQUEST_TIMEOUT_S = 60


@dataclass(frozen=True)
class ScreenTarget:
    """One city to screen: identity for the row, geometry for the tiles.

    A plain value rather than a `db.CityRow`, so this module reads no catalog
    and can be tested without one — the same split every `download_*` module has.
    Build it with :func:`target_from_city`.
    """

    city_id: str
    display_name: str
    country_name: str | None
    bbox: tuple[float, float, float, float]


def target_from_city(city) -> ScreenTarget:
    """A :class:`ScreenTarget` from a ``db.CityRow``, on its FROZEN geometry.

    Frozen is the point, and it is the same point the collectors rest on: the
    screen has to describe the identical rectangle the GSV, Mapillary and
    Panoramax runs describe, or "Panoramax arrived in this city" is a claim
    about a different city than the coverage numbers beside it.
    """
    return ScreenTarget(
        city_id=city.city_id,
        display_name=city.display_name,
        country_name=city.country_name,
        bbox=grid_bbox(
            city.center_lat,
            city.center_lon,
            city.grid_width_m,
            city.grid_height_m,
            city.step_m,
        ),
    )


# ── Pure decoding and geometry: no network, no catalog ──────────────────────


def _tile_point_to_lonlat(px: float, py: float, tile_x: int, tile_y: int, zoom: int, extent: int):
    """One MVT point, in tile-local y-up coordinates, as (lon, lat)."""
    fx = tile_x + px / extent
    fy = tile_y + (1.0 - py / extent)
    return tile_frac_to_lonlat(fx, fy, zoom)


def _rings(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Coordinate rings of a Polygon or MultiPolygon, ignoring anything else."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [list(ring) for ring in coords]
    if kind == "MultiPolygon":
        return [list(ring) for polygon in coords for ring in polygon]
    return []


def hexes_from_tile(
    tile_bytes: bytes, tile_x: int, tile_y: int, zoom: int
) -> dict[str, dict[str, Any]]:
    """
    The v2 `grid` layer of one tile, keyed by H3 cell id.

    ``zoom`` is REQUIRED, on the same reasoning that made it required on the
    shared ``tiles_for_bbox``: this layer is read at two zooms that differ by a
    factor of 256 in cell size, the decode is pure arithmetic, and a wrong zoom
    produces plausible coordinates on the far side of the planet rather than an
    error. A default is the shape of that bug.

    Each hexagon carries the three counters for the WHOLE hexagon, not for the
    part of it inside this tile: verified across four adjacent z14 tiles, a hex
    appearing in more than one carries an identical `nb_pictures` in each. So
    the counters must be deduped by id -- 582 features over those four tiles
    were 483 distinct hexes -- and summing them per tile would over-count every
    hex on a tile seam.

    The geometry, on the other hand, IS clipped to the tile, so this returns
    the vertex bounding box rather than a centre. :func:`merge_hexes` unions
    those boxes across tiles, which reconstructs the full hexagon's extent from
    its pieces.
    """
    if not tile_bytes:
        return {}
    decoded = mapbox_vector_tile.decode(tile_bytes)
    layer = decoded.get(SCREEN_LAYER)
    if not layer:
        return {}
    extent = layer.get("extent", 4096)
    out: dict[str, dict[str, Any]] = {}
    for feature in layer["features"]:
        props = feature.get("properties", {})
        hex_id = props.get("id")
        if hex_id is None:
            continue
        lons, lats = [], []
        for ring in _rings(feature.get("geometry", {})):
            for px, py in ring:
                lon, lat = _tile_point_to_lonlat(px, py, tile_x, tile_y, zoom, extent)
                lons.append(lon)
                lats.append(lat)
        if not lons:
            continue
        out[str(hex_id)] = {
            "min_lon": min(lons),
            "max_lon": max(lons),
            "min_lat": min(lats),
            "max_lat": max(lats),
            "nb_pictures": int(props.get("nb_pictures") or 0),
            "nb_360_pictures": int(props.get("nb_360_pictures") or 0),
            "nb_flat_pictures": int(props.get("nb_flat_pictures") or 0),
            "date": props.get("date"),
        }
    return out


def merge_hexes(
    accumulated: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """
    Fold one tile's hexes into the running set, unioning clipped geometry.

    Counters are taken once per id -- as the MAX across sightings, never the
    sum. Under the measured contract (whole-hex figures repeated verbatim in
    every tile the hex touches) max and first-seen are the same number and
    the sum double-counts every seam hex. Max is chosen over first-seen
    because of what each does if the contract is ever wrong: were a tile to
    carry only its own piece's count, first-seen could record a ZERO for a hex
    whose pictures all sit in the other tile, and the screen would then call a
    covered city empty -- the one failure the design cannot tolerate -- while
    max degrades to a lower bound that is zero only when every piece is zero.
    The vertex box is unioned, so a hex split across two tiles ends up with
    the extent of the complete hexagon and therefore its true centre. Mutates
    and returns `accumulated`.
    """
    for hex_id, hexagon in new.items():
        seen = accumulated.get(hex_id)
        if seen is None:
            accumulated[hex_id] = dict(hexagon)
            continue
        for counter in ("nb_pictures", "nb_360_pictures", "nb_flat_pictures"):
            seen[counter] = max(seen[counter], hexagon[counter])
        seen["min_lon"] = min(seen["min_lon"], hexagon["min_lon"])
        seen["max_lon"] = max(seen["max_lon"], hexagon["max_lon"])
        seen["min_lat"] = min(seen["min_lat"], hexagon["min_lat"])
        seen["max_lat"] = max(seen["max_lat"], hexagon["max_lat"])
    return accumulated


def hexes_in_bbox(
    accumulated: dict[str, dict[str, Any]], bbox: tuple[float, float, float, float]
) -> list[dict[str, Any]]:
    """
    The hexes whose centre falls inside `bbox`, in sorted-id order.

    A res-11 hexagon is about 2,150 m2 -- roughly 25 m across -- against city
    bboxes measured in kilometres, so assigning a whole hex by its centre
    rather than clipping it to the bbox is an approximation worth naming and
    not worth removing. Sorted so the raw artifact is stable across runs.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    inside = []
    for hex_id in sorted(accumulated):
        hexagon = accumulated[hex_id]
        lon = (hexagon["min_lon"] + hexagon["max_lon"]) / 2.0
        lat = (hexagon["min_lat"] + hexagon["max_lat"]) / 2.0
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            inside.append({"id": hex_id, "lon": lon, "lat": lat, **hexagon})
    return inside


def hexes_overlapping_bbox(
    accumulated: dict[str, dict[str, Any]], bbox: tuple[float, float, float, float]
) -> list[dict[str, Any]]:
    """
    Hexes whose extent INTERSECTS the bbox, in sorted-id order.

    The screen and the measure stage select hexes differently on purpose.
    :func:`hexes_in_bbox` assigns a res-11 hexagon by its centre because at 25 m
    across the difference is noise. A screen hexagon is res 6 -- about 36 km2 --
    and a city bbox is often smaller than one, so centre-based selection would
    miss the very hex the city sits inside. Overlap is the only selection that
    keeps the screen an upper bound.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    out = []
    for hex_id in sorted(accumulated):
        hexagon = accumulated[hex_id]
        if (
            hexagon["min_lon"] <= max_lon
            and hexagon["max_lon"] >= min_lon
            and hexagon["min_lat"] <= max_lat
            and hexagon["max_lat"] >= min_lat
        ):
            out.append({"id": hex_id, **hexagon})
    return out


def grow_bbox(
    bbox: tuple[float, float, float, float], margin_deg: float = SCREEN_MARGIN_DEG
) -> tuple[float, float, float, float]:
    """
    A bbox grown by the screen's safety margin, for enumerating z6 tiles.

    The cells a city may count and the tiles fetched for it must be chosen from
    the SAME rectangle, or the margin is real in one place and imaginary in the
    other -- see :data:`SCREEN_MARGIN_DEG` for what the margin buys.

    Deliberately unclamped in longitude: `tiles_for_bbox` already handles the
    antimeridian wrap, and clamping here would reintroduce the gap at 180.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        min_lon - margin_deg,
        max(-90.0, min_lat - margin_deg),
        max_lon + margin_deg,
        min(90.0, max_lat + margin_deg),
    )


def screen_tiles_for_city(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """The z6 tiles one city's screen reads, from its GROWN bbox."""
    return common_tiles_for_bbox(*grow_bbox(tuple(bbox)), SCREEN_ZOOM)


def plan_screen(
    targets: list[ScreenTarget],
) -> tuple[list[tuple[int, int]], dict[str, list[tuple[int, int]]]]:
    """
    What one screen pass will fetch: the deduped tile list, and each city's own.

    The dedup IS the instrument's economy — 1,144 cities resolve to 113 distinct
    z6 tiles because neighbours share them — so this is also what prices a
    ``--dry-run``. Sorted, so the plan and the log are reproducible.
    """
    wanted: set[tuple[int, int]] = set()
    per_city: dict[str, list[tuple[int, int]]] = {}
    for target in targets:
        tiles = screen_tiles_for_city(target.bbox)
        per_city[target.city_id] = tiles
        wanted.update(tiles)
    return sorted(wanted), per_city


def screen_row(
    target: ScreenTarget,
    by_tile: dict[tuple[int, int], dict[str, dict[str, Any]]],
    tiles: list[tuple[int, int]],
) -> dict[str, Any]:
    """One city's screen row, summed over the hexes overlapping its bbox."""
    accumulated: dict[str, dict[str, Any]] = {}
    for tile in tiles:
        merge_hexes(accumulated, by_tile.get(tile, {}))
    selected = hexes_overlapping_bbox(accumulated, tuple(target.bbox))
    return {
        "city_id": target.city_id,
        "display_name": target.display_name,
        "country_name": target.country_name,
        "tiles": len(tiles),
        "cells": len(selected),
        "pictures_upper_bound": sum(c["nb_pictures"] for c in selected),
        "pictures_360_upper_bound": sum(c["nb_360_pictures"] for c in selected),
        "pictures_flat_upper_bound": sum(c["nb_flat_pictures"] for c in selected),
    }


# ── The network pass ────────────────────────────────────────────────────────


async def _fetch_screen_tiles(
    tiles: list[tuple[int, int]],
    *,
    zoom: int,
    max_requests_per_minute: int,
    jitter: float,
    request_timeout: float,
    label: str,
) -> tuple[dict[tuple[int, int], dict[str, dict[str, Any]]], int, int]:
    """
    Fetch and decode `tiles` SEQUENTIALLY, returning (by_tile, requests, empties).

    Sequential on purpose, and it costs nothing worth having: at 113 tiles and
    30/min the pass takes under four minutes either way, while concurrency would
    buy a burst shape against a volunteer-run host for no operational gain. The
    collector fans out because a leader city is thousands of tiles; this does not
    because the whole catalog is 113.

    Every failure mode is `_fetch_tile`'s. A `HostBlockedError` (a refusal, a
    redirect, an error page) propagates immediately and unretried — the screen
    has nothing to salvage, and the one thing it must never do is convert a
    refusal into a catalog full of zeroes.
    """
    by_tile: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    requests_spent = 0
    empty_tiles = 0

    def count_request() -> None:
        nonlocal requests_spent
        requests_spent += 1

    def count_empty() -> None:
        nonlocal empty_tiles
        empty_tiles += 1

    rate_limiter = AsyncRateLimiter(max_requests_per_minute, jitter=jitter)
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    if max_requests_per_minute > 0 and jitter > 0:
        mean_gap_s = 60.0 / max_requests_per_minute
        logger.info(
            f"Pacing {label} at a mean {max_requests_per_minute}/min, exponentially "
            f"jittered (CV {jitter:.2f}; gaps floor {mean_gap_s * (1 - jitter):.2f} s, "
            f"mean {mean_gap_s:.2f} s, p99 "
            f"{mean_gap_s * ((1 - jitter) + jitter * math.log(100.0)):.2f} s — issue #292)"
        )
    progress_bar = progress(total=len(tiles), desc=label, unit="tile", logger=logger)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            for x, y in tiles:
                url = SCREEN_URL_TEMPLATE.format(z=zoom, x=x, y=y)
                try:
                    tile_bytes = await _fetch_tile(
                        session, url, timeout, rate_limiter, count_request, count_empty
                    )
                except DownloadError as exc:
                    # STAMP WHAT WAS SPENT ON THE WAY OUT, the same contract the
                    # collector's `interrupted` has: a pass refused halfway still
                    # SENT what it sent, and the caller charges the day's ledger
                    # from this attribute. Without it a refused screen reports
                    # zero spend, and the next process on this machine sees a
                    # budget that has forgotten the traffic that got us refused.
                    exc.api_requests = requests_spent
                    raise
                except (TimeoutError, aiohttp.ClientError) as exc:
                    # Unlike a city census, a screen has no per-tile tolerance to
                    # spend: one unread z6 tile is every city under ~5.6 degrees
                    # of longitude, and writing those cities a zero they were
                    # never measured at is the one outcome this instrument may
                    # not produce. So a tile that exhausted `_fetch_tile`'s
                    # retries ends the pass.
                    error = DownloadError(
                        f"Panoramax screen tile z{zoom}/{x}/{y} failed after retries: "
                        f"{redact_credentials(exc)}. Refusing to write a screen with an "
                        f"unread tile — every city under it would record an unmeasured zero."
                    )
                    error.api_requests = requests_spent
                    raise error from exc
                by_tile[(x, y)] = hexes_from_tile(tile_bytes, x, y, zoom)
                progress_bar.update(1)
    finally:
        progress_bar.close()
    return by_tile, requests_spent, empty_tiles


def _refuse_if_endpoint_moved(
    tiles: list[tuple[int, int]], empty_tiles: int, *, api_requests: int = 0
) -> None:
    """Refuse a pass in which every tile answered 404 — a moved endpoint.

    Same reading, and the same measured basis, as the collector's guard: on this
    host an empty area answers 200 with no layer, and phase 1 saw zero 404s in
    3,321 requests including 20 cities holding nothing. So an all-404 lattice is
    a renamed endpoint, and finalizing it would stamp the whole catalog with a
    zero on the day the URL changed.

    Bounded at two tiles for the same reason the collector's is: one 404 is a
    hole, two against a measured baseline of none is a moved endpoint.
    """
    if len(tiles) >= 2 and empty_tiles == len(tiles):
        error = DownloadError(
            f"Every one of the {len(tiles)} Panoramax screen tiles answered HTTP 404. "
            f"An empty area answers 200 with no grid layer, so this means the screen "
            f"endpoint ({SCREEN_URL_TEMPLATE}) has moved or been renamed — refusing to "
            f"record a screen claiming the whole catalog holds no imagery."
        )
        # Carried for the same reason the fetch stamps it: this refusal comes
        # AFTER the whole lattice was requested, so it is the most expensive
        # failure the screen has, and the ledger has to see it.
        error.api_requests = api_requests
        raise error


async def screen_targets_async(
    targets: list[ScreenTarget],
    *,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    request_timeout: float = SCREEN_REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    """
    One whole-catalog screen pass, serialized against every other Panoramax
    process on this machine.

    Returns a dict with ``rows`` (one per target, in the order given),
    ``tiles``, ``api_requests`` (ATTEMPTS, not planned tiles — a retried 5xx
    sends traffic the plan did not price) and ``empty_tiles``.

    Raises:
        HostBlockedError: the host refused this IP, or the endpoint moved.
        DownloadError: a tile could not be read after retries, or every tile
            answered 404.
        HostBusyError: another local process holds the Panoramax lock.
    """
    tiles, per_city = plan_screen(targets)
    logger.info(
        f"Screening {len(targets)} cities against Panoramax: {len(tiles)} distinct "
        f"z{SCREEN_ZOOM} tiles (cities share tiles — that sharing is the whole cost saving)"
    )
    with host_lock(HOST_PANORAMAX):
        by_tile, api_requests, empty_tiles = await _fetch_screen_tiles(
            tiles,
            zoom=SCREEN_ZOOM,
            max_requests_per_minute=max_requests_per_minute,
            jitter=jitter,
            request_timeout=request_timeout,
            label=f"Screening Panoramax z{SCREEN_ZOOM} tiles",
        )
    _refuse_if_endpoint_moved(tiles, empty_tiles, api_requests=api_requests)
    rows = [screen_row(target, by_tile, per_city[target.city_id]) for target in targets]
    return {
        "rows": rows,
        "tiles": len(tiles),
        "api_requests": api_requests,
        "empty_tiles": empty_tiles,
    }


async def measure_targets_async(
    targets: list[ScreenTarget],
    *,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    request_timeout: float = SCREEN_REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    """
    Exact hexagon counts at z14 for a BOUNDED set of cities — the follow-up to a
    positive screen, never a catalog-wide pass.

    The screen's upper bound answers "is there anything here"; this answers "how
    much", by reading the same counters at res 11 (~25 m hexagons) and selecting
    them by CENTRE rather than by overlap, where the difference against a city
    bbox is noise. It is manual and bounded because it is not cheap: all 414
    screened-positive cities would be ~51,000 tiles, about 28 hours at this pace.

    Nothing here is written to the catalog. The `provider_screen` table records
    upper bounds on one instrument, and mixing a second instrument's exact counts
    into the same rows would make the series mean two different things depending
    on which command last touched a city. This prints, and the collector is what
    turns a promising city into a dated snapshot.
    """
    tiles: set[tuple[int, int]] = set()
    per_city: dict[str, list[tuple[int, int]]] = {}
    for target in targets:
        city_tiles = common_tiles_for_bbox(*tuple(target.bbox), MEASURE_ZOOM)
        per_city[target.city_id] = city_tiles
        tiles.update(city_tiles)
    tile_list = sorted(tiles)
    with host_lock(HOST_PANORAMAX):
        by_tile, api_requests, empty_tiles = await _fetch_screen_tiles(
            tile_list,
            zoom=MEASURE_ZOOM,
            max_requests_per_minute=max_requests_per_minute,
            jitter=jitter,
            request_timeout=request_timeout,
            label=f"Measuring Panoramax z{MEASURE_ZOOM} tiles",
        )
    _refuse_if_endpoint_moved(tile_list, empty_tiles, api_requests=api_requests)

    rows = []
    for target in targets:
        accumulated: dict[str, dict[str, Any]] = {}
        for tile in per_city[target.city_id]:
            merge_hexes(accumulated, by_tile.get(tile, {}))
        selected = hexes_in_bbox(accumulated, tuple(target.bbox))
        rows.append(
            {
                "city_id": target.city_id,
                "display_name": target.display_name,
                "tiles": len(per_city[target.city_id]),
                "cells": len(selected),
                "pictures": sum(c["nb_pictures"] for c in selected),
                "pictures_360": sum(c["nb_360_pictures"] for c in selected),
                "pictures_flat": sum(c["nb_flat_pictures"] for c in selected),
            }
        )
    return {
        "rows": rows,
        "tiles": len(tile_list),
        "api_requests": api_requests,
        "empty_tiles": empty_tiles,
    }


def measure_tile_count(targets: list[ScreenTarget]) -> int:
    """Distinct z14 tiles an exact measure of `targets` would read — the price."""
    tiles: set[tuple[int, int]] = set()
    for target in targets:
        tiles.update(common_tiles_for_bbox(*tuple(target.bbox), MEASURE_ZOOM))
    return len(tiles)


def screen_targets(targets: list[ScreenTarget], **kwargs) -> dict[str, Any]:
    """Synchronous :func:`screen_targets_async`, for the scheduler's CLI."""
    return asyncio.run(screen_targets_async(targets, **kwargs))


def measure_targets(targets: list[ScreenTarget], **kwargs) -> dict[str, Any]:
    """Synchronous :func:`measure_targets_async`."""
    return asyncio.run(measure_targets_async(targets, **kwargs))
