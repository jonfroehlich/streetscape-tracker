"""
Issue #316, phase 1: is there Panoramax imagery in the cities we actually track?

    python scripts/panoramax_feasibility.py --stage screen                  # 104 requests, whole catalog
    python scripts/panoramax_feasibility.py --stage measure --max-cities 60
    python scripts/panoramax_feasibility.py --stage detail --detail-cities 12
    python scripts/panoramax_feasibility.py --stage instances
    python scripts/panoramax_feasibility.py --analyze --docs-dir docs/experiments   # offline

Panoramax fits this project better than anything else on the candidate list
(`docs/imagery-providers.md`): one federated host, no credential, per-picture
metadata richer than any provider we collect, and a vector-tile census that
drops onto the seam `census.py` already is. Exactly one thing decides whether a
channel is worth building, and it was unmeasured: **coverage against our
largely-US catalog.** This script measures it and nothing else. There is no
collector here, no scheduler channel, no credential, and no write of any kind
to `data/`.

READ THIS BEFORE CHANGING THE INSTRUMENT. #316 proposed answering the question
with the federated `/api/search` over a stratified sample. Probing it first (the
standing rule) showed that cannot work, and the alternative is strictly better:

  1. `/api/search` CANNOT COUNT. It does not paginate -- `links` comes back
     empty at limit=1, limit=1000 and limit=10000 alike -- and carries no
     `numberMatched`. limit=100000 is a hard 400. So a bbox holding more
     pictures than `limit` is indistinguishable from one holding exactly
     `limit`, which is the one distinction a coverage study is made of. It is
     also enormous: 10,000 features is 75 MB, because every feature embeds a
     ~90-key EXIF blob. It stays in this study as a bounded METADATA sample
     (stage `instances`), never as a census.

  2. THE MAP TILES CAN, and there are two of them, serving different products:
     v1 `/api/map/{z}/{x}/{y}.mvt` has a `grid` layer at z0-z6 ONLY -- a 0.1
     degree lattice of points carrying nb_pictures / nb_360_pictures /
     nb_flat_pictures -- and a `pictures` layer (per-picture id, ts, type,
     account_id, sequences) at z15+. v2 `/api/map/2/{z}/{x}/{y}.mvt` replaces
     the coarse lattice with an H3 hexagon `grid` whose resolution tracks the
     zoom, res 11 at z14, carrying the same three counters.

  3. THE COST LADDER OVER OUR OWN FROZEN GRIDS makes the gate answerable over
     the WHOLE catalog rather than a sample (1,144 enabled cities, computed
     offline from `grid_bbox`, no network):

         instrument            zoom   distinct tiles   per city p50/p90/p95/max
         v1 grid (screen)      z6              104          1 /   1 /   1 /    2
         v2 H3 grid (measure)  z14          64,650         12 / 121 / 240 / 6,480
         v1 pictures (detail)  z15         236,808         35 / 462 / 900 / 25,418

     104 requests screen every city we track. That is the whole design: an
     almost-free stage that can PROVE ABSENCE, then exact measurement spent
     only where absence was not proven. z14's median of 12 is the Mapillary
     census median, as it must be -- same zoom.

  4. THE TILE `type` FIELD HAS NO ABSENT STATE; THE SEARCH `field_of_view` DOES.
     Summed over the federation at v1 z0: 119,362,642 pictures = 52,128,373
     360 + 67,234,269 flat + 0 unclassified. #316's "10-34% field absent" third
     state is a property of reading `pers:interior_orientation.field_of_view`
     out of EXIF in the SEARCH response, not of the imagery. Stage `instances`
     measures both against the same bbox so the writeup can say which.

WHY A ZERO SCREEN IS CONCLUSIVE AND A NON-ZERO ONE IS NOT. A 0.1 degree cell is
roughly 80 km2 and the median catalog city is 19.5 km2, so the screen sums cells
far larger than the city inside them: it is an UPPER BOUND. That asymmetry is
the point. An upper bound of zero means the city has no imagery, full stop; a
positive one means only "look closer", which is what stage `measure` is for.
Stage `measure` therefore also walks a seeded random CONTROL sample of
screened-zero cities, because an unverified screen is an assumption.

PACING, AND WHY IT IS THE CONSERVATIVE END. No rate limit is documented anywhere
found, including the OpenAPI spec, and no rate-limit headers come back. Per the
standing rule that is UNKNOWN, not unlimited. So: a modest default rate, gaps
jittered with the #292 shifted-exponential (`download_common.spaced_gap_seconds`
-- imported, never re-derived), and `refuse_on_collection_host()` so this can
never be the thing that finds Panoramax's limit with the nightly batch's IP.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import mapbox_vector_tile
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from experiment_stats import describe  # noqa: E402
from kartaview_probe import refuse_on_collection_host  # noqa: E402

from streetscape_metadata_tracker.download_common import (  # noqa: E402
    _unit_exponential,
    grid_bbox,
    spaced_gap_seconds,
)
from streetscape_metadata_tracker.download_mapillary import (  # noqa: E402
    lonlat_to_tile_frac,
    tile_frac_to_lonlat,
    tiles_for_bbox,
)

logger = logging.getLogger("panoramax_feasibility")

TOPIC = "panoramax-feasibility"
DOCS_METRICS_NAME = f"{TOPIC}_metrics.json"
DOCS_DIR_DEFAULT = "docs/experiments"
RAW_DIR_DEFAULT = os.path.join("experiments", TOPIC)
WRITEUP = f"docs/experiments/{TOPIC}.md"
ISSUE = "https://github.com/jonfroehlich/streetscape-tracker/issues/316"

API_BASE = "https://api.panoramax.xyz/api"
MAP_V1_URL = API_BASE + "/map/{z}/{x}/{y}.mvt"
MAP_V2_URL = API_BASE + "/map/2/{z}/{x}/{y}.mvt"
SEARCH_URL = API_BASE + "/search"
INSTANCES_URL = API_BASE + "/instances"
STATS_URL = API_BASE + "/stats"

# The three instruments, pinned as constants because each zoom is the ONLY one
# that serves its layer -- these are not tunables. z6 is the finest zoom with a
# v1 `grid`; z14 is the finest with a v2 H3 `grid` (and the zoom the Mapillary
# census already walks); z15 is the coarsest with a `pictures` layer.
SCREEN_ZOOM = 6
MEASURE_ZOOM = 14
DETAIL_ZOOM = 15

SCREEN_LAYER = "grid"
# Two endpoints serve a z6 grid and they DISAGREE; see stage_screen.
SCREEN_VARIANTS = {"v1_lattice": MAP_V1_URL, "v2_h3": MAP_V2_URL}
DEFAULT_SCREEN_VARIANT = "v2_h3"
MEASURE_LAYER = "grid"
DETAIL_LAYER = "pictures"

# The v1 z6 lattice's spacing, measured off the decoded tiles: anchors sit on a
# 0.1 degree graticule in BOTH axes (a geographic lattice, not a Mercator one).
SCREEN_CELL_DEG = 0.1

# Panoramax's own vocabulary for the two imagery types, from the tile layers.
# `equirectangular` is 360; anything else is flat. Never absent (see the module
# docstring, finding 4), which is why this is read as a two-state field.
TYPE_360 = "equirectangular"

# Conservative by construction: no published limit means no evidence, and this
# study is small enough that a slow pace costs an afternoon rather than a
# result. 30/min with jitter 0.6 gives a 1.2 s floor and a 2.0 s mean gap.
DEFAULT_RATE_PER_MINUTE = 30
DEFAULT_JITTER = 0.6

# Named so `docs_generated_by` can elide it when it was not overridden -- the
# mistake that once left `--seed` out of a study's provenance stamp.
DEFAULT_SEED = 316

DEFAULT_LEADERS = 20
DEFAULT_TYPICAL = 40
DEFAULT_CONTROLS = 20
DEFAULT_DETAIL_CITIES = 12
DEFAULT_MAX_TILES_PER_CITY = 200
DEFAULT_SEARCH_LIMIT = 500
# z15 tiles fetched per city to type the search sample's pictures. Four is
# enough to cover a contributor-concentrated sample and keeps the whole
# reconciliation under one city's measure cost.
DEFAULT_RECONCILE_TILES = 4

# The access probe's own constants. The datetime window is deliberately in the
# FUTURE relative to the imagery it is aimed at, so "honoured" and "ignored"
# give visibly different answers rather than merely different counts.
ACCESS_PROBE_LIMIT = 300
ACCESS_PROBE_DATETIME = "2026-01-01T00:00:00Z/.."
DEFAULT_TIMEOUT_S = 60

DOCS_RECORD_NOTE = (
    "Phase 1 of #316: read-only, no credential, no collector. Three instruments, and they are "
    "not interchangeable. `screen` sums the v1 z6 grid layer's 0.1-degree cells overlapping a "
    "city's frozen grid bbox, so every `screen_pictures_upper_bound` is an UPPER BOUND over an "
    "area much larger than the city -- a zero is conclusive, a positive number is not. `measure` "
    "sums the v2 z14 H3 grid layer's res-11 hexagons whose centre falls inside the bbox, which is "
    "the exact in-bbox count; hexes are deduped by H3 id across tiles and their centres are "
    "reconstructed from the union of the clipped pieces. `detail` reads the v1 z15 pictures layer "
    "per picture, and is the only stage that can report capture months, contributors and "
    "sequences; where `tiles_probed` is below `tiles_total` the city was truncated by "
    "--max-tiles-per-city and its counts are a seeded-shuffle sample of the bbox, NOT a total. "
    "`instances` is a bounded /api/search sample, capped by --search-limit and therefore never a "
    "count of anything; it exists to attribute pictures to source instances and to cross-tabulate "
    "the EXIF field of view against the same pictures' tile `type`, which has no absent state. "
    "`access` is not a measurement of coverage at all: it probes three silent /api/search "
    "behaviours (no pagination, `datetime` ignored, `filter=field_of_view=360` dropping EXIF-less "
    "pictures) and DERIVES each finding from the responses, so a re-run against a fixed Panoramax "
    "fails rather than leaving stale prose. "
    "Quote `measure` for counts, `detail` for dates and contributors, and neither for the other."
)


# ── Pure decoding and geometry: no network, no catalog ──────────────────────


def _tile_point_to_lonlat(px: float, py: float, tile_x: int, tile_y: int, zoom: int, extent: int):
    """One MVT point, in tile-local y-up coordinates, as (lon, lat)."""
    fx = tile_x + px / extent
    fy = tile_y + (1.0 - py / extent)
    return tile_frac_to_lonlat(fx, fy, zoom)


def snap_to_lattice(value: float, step: float = SCREEN_CELL_DEG) -> float:
    """
    Snap a decoded coordinate back onto the v1 grid's 0.1-degree graticule.

    MVT quantizes geometry to 1/`extent` of a tile -- about 0.0014 degrees at
    z6 -- so a lattice anchor decodes a fraction of a cell off its true value
    (-123.70 arrives as -123.7005615). Rounding to the lattice makes a cell's
    identity exact, which is what lets cells be deduped across the tile seams
    they straddle.
    """
    return round(value / step) * step


def screen_cells_from_tile(
    tile_bytes: bytes, tile_x: int, tile_y: int, zoom: int = SCREEN_ZOOM
) -> list[dict[str, Any]]:
    """
    The v1 `grid` layer of one z6 tile as lattice cells with their counters.

    Returns dicts with lon/lat (the snapped lattice anchor) and the three
    counters. An empty tile -- no imagery anywhere in ~5.6 degrees of
    longitude -- decodes to no layer at all, which is a real answer and not an
    error.
    """
    if not tile_bytes:
        return []
    decoded = mapbox_vector_tile.decode(tile_bytes)
    layer = decoded.get(SCREEN_LAYER)
    if not layer:
        return []
    extent = layer.get("extent", 4096)
    cells = []
    for feature in layer["features"]:
        geometry = feature.get("geometry", {})
        if geometry.get("type") != "Point":
            continue
        px, py = geometry["coordinates"]
        lon, lat = _tile_point_to_lonlat(px, py, tile_x, tile_y, zoom, extent)
        props = feature.get("properties", {})
        cells.append(
            {
                "lon": snap_to_lattice(lon),
                "lat": snap_to_lattice(lat),
                "nb_pictures": int(props.get("nb_pictures") or 0),
                "nb_360_pictures": int(props.get("nb_360_pictures") or 0),
                "nb_flat_pictures": int(props.get("nb_flat_pictures") or 0),
            }
        )
    return cells


def grow_bbox(
    bbox: tuple[float, float, float, float], margin_deg: float = SCREEN_CELL_DEG
) -> tuple[float, float, float, float]:
    """
    A bbox grown by the screen's safety margin, for enumerating z6 tiles.

    :func:`screen_cells_overlapping` accepts an anchor up to one cell outside
    the bbox, and a cell one bbox-width outside can live in the NEXT z6 tile --
    which, if tiles were enumerated from the bare bbox, would never be fetched.
    The margin would then silently disappear exactly at the tile seams: 108 of
    1,144 catalog cities sit within one cell of one, 49 of them screened zero,
    and for those the screen's "a zero is conclusive" claim would rest on cells
    nobody looked at. So the growth happens where the tiles are chosen, not
    only where the cells are filtered.

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


def screen_cells_overlapping(
    cells: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    cell_deg: float = SCREEN_CELL_DEG,
) -> list[dict[str, Any]]:
    """
    Every lattice cell whose 0.1-degree square could overlap `bbox`.

    Whether an anchor is a cell's south-west corner or its centre is not
    documented and was not measured, so a cell qualifies when its anchor sits
    within ONE cell of the bbox on each axis. That is a strict superset under
    either convention, which is exactly what the screen needs: the sum it
    feeds is an upper bound, so being generous here can only ever turn a real
    zero into a false positive that stage `measure` then spends a dozen
    requests refuting -- never the reverse, which would be a city wrongly
    written off as empty.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return [
        c
        for c in cells
        if (min_lon - cell_deg) <= c["lon"] <= (max_lon + cell_deg)
        and (min_lat - cell_deg) <= c["lat"] <= (max_lat + cell_deg)
    ]


def hexes_from_tile(
    tile_bytes: bytes, tile_x: int, tile_y: int, zoom: int = MEASURE_ZOOM
) -> dict[str, dict[str, Any]]:
    """
    The v2 `grid` layer of one tile, keyed by H3 cell id.

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
    layer = decoded.get(MEASURE_LAYER)
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


def _rings(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Coordinate rings of a Polygon or MultiPolygon, ignoring anything else."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [list(ring) for ring in coords]
    if kind == "MultiPolygon":
        return [list(ring) for polygon in coords for ring in polygon]
    return []


def merge_hexes(
    accumulated: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """
    Fold one tile's hexes into the running set, unioning clipped geometry.

    Counters are taken once per id (they are whole-hex figures, so the second
    sighting adds nothing); the vertex box is unioned, so a hex split across
    two tiles ends up with the extent of the complete hexagon and therefore its
    true centre. Mutates and returns `accumulated`.
    """
    for hex_id, hexagon in new.items():
        seen = accumulated.get(hex_id)
        if seen is None:
            accumulated[hex_id] = dict(hexagon)
            continue
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


def pictures_from_tile(
    tile_bytes: bytes, tile_x: int, tile_y: int, zoom: int = DETAIL_ZOOM
) -> list[dict[str, Any]]:
    """
    The v1 `pictures` layer of one z15 tile, one dict per picture.

    This is the only layer with per-picture rows, and therefore the only way to
    get capture DATES rather than counts. Keeps id, lon/lat, the capture
    timestamp `ts`, the imagery `type`, the contributor `account_id` and the
    owning sequence.
    """
    if not tile_bytes:
        return []
    decoded = mapbox_vector_tile.decode(tile_bytes)
    layer = decoded.get(DETAIL_LAYER)
    if not layer:
        return []
    extent = layer.get("extent", 4096)
    pictures = []
    for feature in layer["features"]:
        geometry = feature.get("geometry", {})
        if geometry.get("type") != "Point":
            continue
        props = feature.get("properties", {})
        picture_id = props.get("id", feature.get("id"))
        if picture_id is None:
            continue
        px, py = geometry["coordinates"]
        lon, lat = _tile_point_to_lonlat(px, py, tile_x, tile_y, zoom, extent)
        pictures.append(
            {
                "id": str(picture_id),
                "lon": lon,
                "lat": lat,
                "ts": props.get("ts"),
                "type": props.get("type"),
                "account_id": props.get("account_id"),
                "sequence_id": props.get("first_sequence"),
            }
        )
    return pictures


def capture_month(ts: Any) -> str | None:
    """
    The YYYY-MM of a tile timestamp such as ``'2025-11-02 00:24:37+00'``.

    Deliberately a prefix slice rather than a parse: the tile layer's format is
    the provider's, we do not control it, and a study that counts DISTINCT
    months must not silently drop a row because a timezone suffix moved. A
    value too short to carry a month is None, which is counted as undated
    rather than dropped -- the #257 lesson that undated imagery arrives in
    batches and so must stay visible.
    """
    if not isinstance(ts, str) or len(ts) < 7:
        return None
    month = ts[:7]
    if len(month) != 7 or month[4] != "-":
        return None
    return month


def bbox_contains(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    """Is this point inside the bbox? (Inclusive on all four edges.)"""
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def shuffled_tiles(tiles: list[tuple[int, int]], seed: int) -> list[tuple[int, int]]:
    """
    Tile visit order: a seeded shuffle, never raster order.

    A city cut short by --max-tiles-per-city must be a spatially unbiased
    sample of its bbox rather than its northern strip, which is the same
    posture `kartaview_sweep_cost.py` takes for the same reason. Seeded, so a
    truncated city is reproducible.
    """
    order = list(tiles)
    random.Random(seed).shuffle(order)
    return order


# ── Pacing and transport ───────────────────────────────────────────────────


class BlockedError(RuntimeError):
    """The host refused us in a way that means stop, not retry."""


class SpacedRateLimiter:
    """
    Synchronous #292 pacer: jittered gaps at a fixed mean rate.

    The repo's jittered limiter (`download_common.AsyncRateLimiter`) is async
    and this study is a straight-line script, so the class is not reusable --
    but the GAP FORMULA is, and it is imported rather than rewritten. That
    matters more than it looks: `jitter` is a coefficient of variation, not a
    plus-or-minus range, and an independently rewritten "wobble" would quietly
    be a different distribution from the one production paces with.

    `time_func` and `draw_func` are injectable so the pacing can be tested
    without sleeping.
    """

    def __init__(
        self,
        max_per_minute: int,
        *,
        jitter: float = DEFAULT_JITTER,
        time_func=None,
        draw_func=None,
        sleep_func=None,
    ):
        if not 0.0 <= jitter < 1.0:
            raise ValueError(f"jitter must be in [0, 1), got {jitter!r}")
        self.enabled = max_per_minute > 0
        self.mean_gap_s = 60.0 / max_per_minute if self.enabled else 0.0
        self.jitter = jitter
        self._time = time_func or time.monotonic
        self._draw = draw_func or _unit_exponential
        self._sleep = sleep_func or time.sleep
        self._next_at: float | None = None

    def acquire(self) -> None:
        """Block until the next request may go out. The first never waits."""
        if not self.enabled:
            return
        now = self._time()
        if self._next_at is not None and self._next_at > now:
            self._sleep(self._next_at - now)
            now = self._time()
        # Drawn after the sleep, so each request's delay is fresh randomness.
        self._next_at = now + spaced_gap_seconds(self.mean_gap_s, self.jitter, self._draw)


class Fetcher:
    """
    Paced, counted GET with a small retry, and a hard stop on a refusal.

    Retries only what is plausibly transient (a timeout, a connection reset, a
    5xx). A 429 or a 403 is the shape of a per-IP limit rather than a blip, and
    since finding Panoramax's undocumented limit is emphatically NOT this
    study's question, it raises :class:`BlockedError` and the run ends with
    what it has measured so far written out.
    """

    def __init__(self, limiter: SpacedRateLimiter, timeout_s: int, retries: int = 3):
        self.limiter = limiter
        self.timeout_s = timeout_s
        self.retries = retries
        self.requests_spent = 0
        self.empty_tiles = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "streetscape-tracker/panoramax-feasibility (+https://github.com/jonfroehlich/streetscape-tracker)"
        )

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self.limiter.acquire()
            self.requests_spent += 1
            try:
                response = self.session.get(url, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(f"{url}: {exc} (attempt {attempt + 1}/{self.retries})")
                continue
            if response.status_code in (403, 429):
                raise BlockedError(
                    f"HTTP {response.status_code} from {url} -- treating this as a per-IP "
                    f"refusal and stopping. Do not retry into it; wait, and record what "
                    f"happened in docs/provider-access.md."
                )
            if response.status_code == 404:
                # A tile the host has nothing for. Counted, never silent: a
                # 404 that is really a URL mistake would otherwise read as a
                # city with no imagery, which is exactly the wrong answer for a
                # coverage study to produce confidently.
                self.empty_tiles += 1
                return response
            if response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                logger.warning(f"{url}: HTTP {response.status_code} (attempt {attempt + 1})")
                continue
            response.raise_for_status()
            return response
        raise RuntimeError(f"{url}: gave up after {self.retries} attempts ({last_error})")

    def get_tile(self, template: str, zoom: int, x: int, y: int) -> bytes:
        response = self.get(template.format(z=zoom, x=x, y=y))
        return b"" if response.status_code == 404 else response.content


# ── Catalog ────────────────────────────────────────────────────────────────


def load_cities(db_path: str) -> list[dict[str, Any]]:
    """
    Every enabled city with its FROZEN grid geometry, as plain dicts.

    Frozen is the point: this study asks about the same bboxes the collectors
    already use, so a Panoramax number is comparable with the GSV and Mapillary
    numbers for that city rather than describing a different rectangle.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT city_id, display_name, country_name, center_lat, center_lon, "
            "grid_width_m, grid_height_m, step_m FROM cities WHERE enabled = 1 "
            "ORDER BY city_id"
        ).fetchall()
    finally:
        connection.close()
    cities = []
    for row in rows:
        city = dict(row)
        city["bbox"] = list(
            grid_bbox(
                row["center_lat"],
                row["center_lon"],
                row["grid_width_m"],
                row["grid_height_m"],
                row["step_m"],
            )
        )
        cities.append(city)
    return cities


# ── Stages ─────────────────────────────────────────────────────────────────


def stage_screen(
    cities: list[dict[str, Any]], fetcher: Fetcher | None, variant: str = DEFAULT_SCREEN_VARIANT
) -> dict[str, Any]:
    """
    Whole-catalog screen at z6. One request per distinct z6 tile the catalog
    touches -- 113 for the current 1,144 cities -- because neighbouring cities
    share tiles.

    THE VARIANT MATTERS AND THE DEFAULT CHANGED. The obvious instrument is v1's
    `grid`, a 0.1-degree lattice, and it is what the API root's `xyz` link
    points at. It is **lossy**: over three z6 tiles it reported 2.5%, 7.9% and
    23.9% fewer pictures than v2's H3 grid over the identical extent, and it
    omits whole populated cells rather than under-counting populated ones (it
    happily reports cells holding a single picture, so this is not a low-count
    threshold). The control group is how that was found: a screened-ZERO city
    turned out to hold imagery, and the v1 lattice had returned no cell for it
    at all while v2 did.

    A lossy screen is not a slightly worse screen here, it is a broken one --
    the entire design rests on a zero being conclusive, and a lattice that can
    drop a populated cell cannot support that. So the default is `v2_h3`, and
    `v1_lattice` is kept only so the comparison stays reproducible.
    """
    wanted: set[tuple[int, int]] = set()
    per_city_tiles: dict[str, list[tuple[int, int]]] = {}
    for city in cities:
        # Grown, not bare: see grow_bbox. The cells a city may count and the
        # tiles fetched for it must be chosen from the SAME rectangle, or the
        # margin is real in one place and imaginary in the other.
        tiles = tiles_for_bbox(*grow_bbox(tuple(city["bbox"])), SCREEN_ZOOM)
        per_city_tiles[city["city_id"]] = tiles
        wanted.update(tiles)
    tile_list = sorted(wanted)

    if fetcher is None:  # --dry-run
        return {
            "variant": variant,
            "planned_requests": len(tile_list),
            "tiles": len(tile_list),
            "cities": [],
        }

    url = SCREEN_VARIANTS[variant]
    by_tile: dict[tuple[int, int], Any] = {}
    for index, (x, y) in enumerate(tile_list, start=1):
        raw = fetcher.get_tile(url, SCREEN_ZOOM, x, y)
        by_tile[(x, y)] = (
            screen_cells_from_tile(raw, x, y, SCREEN_ZOOM)
            if variant == "v1_lattice"
            else hexes_from_tile(raw, x, y, SCREEN_ZOOM)
        )
        logger.info(f"screen {index}/{len(tile_list)} z{SCREEN_ZOOM}/{x}/{y}: {len(raw)} bytes")

    rows = []
    for city in cities:
        bbox = tuple(city["bbox"])
        tiles = per_city_tiles[city["city_id"]]
        if variant == "v1_lattice":
            # Deduped by lattice anchor: a bbox can straddle a tile seam, and
            # the same cell then arrives once per tile.
            seen: dict[Any, dict[str, Any]] = {}
            for tile in tiles:
                for cell in screen_cells_overlapping(by_tile[tile], bbox):
                    seen[(round(cell["lon"], 4), round(cell["lat"], 4))] = cell
            selected = list(seen.values())
        else:
            accumulated: dict[str, dict[str, Any]] = {}
            for tile in tiles:
                merge_hexes(accumulated, by_tile[tile])
            selected = hexes_overlapping_bbox(accumulated, grow_bbox(bbox, 0.0))
        rows.append(
            {
                "city_id": city["city_id"],
                "display_name": city["display_name"],
                "country_name": city["country_name"],
                "bbox": city["bbox"],
                "z6_tiles": len(tiles),
                "cells": len(selected),
                "screen_pictures_upper_bound": sum(c["nb_pictures"] for c in selected),
                "screen_360_upper_bound": sum(c["nb_360_pictures"] for c in selected),
                "screen_flat_upper_bound": sum(c["nb_flat_pictures"] for c in selected),
            }
        )
    return {
        "variant": variant,
        "zoom": SCREEN_ZOOM,
        "cell_deg": SCREEN_CELL_DEG if variant == "v1_lattice" else None,
        "tiles": len(tile_list),
        "requests_spent": len(tile_list),
        "cities": rows,
    }


MEASURE_GROUPS = ("leaders", "typical", "controls")


def select_measure_set(
    screen: dict[str, Any], leaders: int, typical: int, controls: int, seed: int
) -> dict[str, list[str]]:
    """
    Which cities stage `measure` visits, in three groups that answer three
    different questions and must never be pooled.

    `leaders` -- the richest screened-positive cities -- answer "is there a
    real Panoramax deployment in ANY city we track", which is what decides
    whether a channel could ever be worth building.

    `typical` is a seeded UNIFORM random draw from the screened-positive
    cities, and it is the group the gate actually turns on. #316 phrases the
    gate as "the median tracked city", and a richest-first list cannot estimate
    a median of anything: it is the extreme tail by construction. A uniform
    draw from the positive stratum can, because the other stratum is known
    exactly (a screened-zero city has zero imagery, full stop) -- so the two
    strata compose into a statement about the whole catalog.

    `controls` are a seeded random draw from the screened-ZERO cities. They are
    not optional garnish: they are the only thing standing between "a zero
    screen is conclusive" as a measured property and as an assumption the study
    quietly rests all of its arithmetic on.

    Groups are disjoint -- a leader is removed from the pool before `typical`
    is drawn -- so a city is never measured twice and never counted in two
    distributions.
    """
    positives = [c for c in screen["cities"] if c["screen_pictures_upper_bound"] > 0]
    positives.sort(key=lambda c: (-c["screen_pictures_upper_bound"], c["city_id"]))
    leader_ids = [c["city_id"] for c in positives[:leaders]]

    remaining = sorted(c["city_id"] for c in positives[leaders:])
    typical_ids = random.Random(seed).sample(remaining, min(typical, len(remaining)))

    zeros = sorted(c["city_id"] for c in screen["cities"] if c["screen_pictures_upper_bound"] == 0)
    control_ids = random.Random(seed + 1).sample(zeros, min(controls, len(zeros)))

    return {
        "leaders": leader_ids,
        "typical": sorted(typical_ids),
        "controls": sorted(control_ids),
    }


def scale_to_bbox(counted: int, tiles_probed: int, tiles_total: int) -> int:
    """
    A truncated city's count scaled back up to its whole bbox.

    Tiles are visited in a seeded uniform shuffle, so the probed subset is a
    uniform random sample of the bbox and ``counted * total / probed`` is an
    unbiased estimate of the whole. This exists because truncation is not
    neutral: the cities that hit --max-tiles-per-city are the largest ones, so
    reporting their raw counts beside complete cities' would understate exactly
    the tail that decides the question, and silently.

    A complete city returns its own count unchanged, so the field is always
    populated and a reader never has to branch on `complete` to use it -- but
    `complete` is still recorded, because an estimate and a census are not the
    same number even when they are equal.
    """
    if tiles_probed <= 0 or tiles_probed >= tiles_total:
        return counted
    return int(round(counted * tiles_total / tiles_probed))


def reusable_measure_row(
    prior: dict[str, Any] | None, city: dict[str, Any], max_tiles: int, seed: int
) -> dict[str, Any] | None:
    """
    A prior row for this city that the current settings would reproduce exactly.

    `measure_city` is deterministic in (bbox, seed, max_tiles): the same tiles
    in the same seeded order, the same hexes, the same sum. So re-fetching a
    city already measured under identical settings spends requests against a
    host with no documented limit to re-learn a number we hold, which is the
    opposite of the pacing posture this study argues for everywhere else.

    What makes reuse safe is the GUARD, not the determinism. A row is reused
    only when the tile plan the current settings produce matches the plan the
    row recorded; change --seed, --max-tiles-per-city, or a city's frozen
    geometry and it is refetched rather than silently compared against a
    different subset of a different bbox. Reuse is never silent either: the row
    is stamped with the run that measured it and the payload counts how many
    rows came from where.

    Returns None when there is nothing safely reusable.
    """
    if prior is None:
        return None
    for group in MEASURE_GROUPS:
        for row in prior.get(group, []):
            if row["city_id"] != city["city_id"]:
                continue
            tiles = tiles_for_bbox(*tuple(city["bbox"]), MEASURE_ZOOM)
            probed = min(max_tiles, len(tiles))
            if row["tiles_total"] == len(tiles) and row["tiles_probed"] == probed:
                return {**row, "reused_from": prior.get("_measured_by", "an earlier run")}
            return None
    return None


def measure_city(
    city: dict[str, Any], fetcher: Fetcher, max_tiles: int, seed: int
) -> dict[str, Any]:
    """Exact in-bbox picture counts for one city off the v2 z14 H3 grid."""
    bbox = tuple(city["bbox"])
    tiles = tiles_for_bbox(*bbox, MEASURE_ZOOM)
    order = shuffled_tiles(tiles, seed)[:max_tiles]
    accumulated: dict[str, dict[str, Any]] = {}
    for x, y in order:
        raw = fetcher.get_tile(MAP_V2_URL, MEASURE_ZOOM, x, y)
        merge_hexes(accumulated, hexes_from_tile(raw, x, y, MEASURE_ZOOM))
    inside = hexes_in_bbox(accumulated, bbox)
    pictures = sum(h["nb_pictures"] for h in inside)
    return {
        "city_id": city["city_id"],
        "display_name": city["display_name"],
        "country_name": city["country_name"],
        "tiles_total": len(tiles),
        "tiles_probed": len(order),
        "complete": len(order) == len(tiles),
        "hexes": len(inside),
        "pictures": pictures,
        "pictures_360": sum(h["nb_360_pictures"] for h in inside),
        "pictures_flat": sum(h["nb_flat_pictures"] for h in inside),
        "pictures_scaled_to_bbox": scale_to_bbox(pictures, len(order), len(tiles)),
        "newest_hex_date": max((h["date"] for h in inside if h["date"]), default=None),
    }


def detail_city(
    city: dict[str, Any], fetcher: Fetcher, max_tiles: int, seed: int
) -> dict[str, Any]:
    """Per-picture detail for one city off the v1 z15 pictures layer."""
    bbox = tuple(city["bbox"])
    tiles = tiles_for_bbox(*bbox, DETAIL_ZOOM)
    order = shuffled_tiles(tiles, seed)[:max_tiles]
    pictures: dict[str, dict[str, Any]] = {}
    for x, y in order:
        raw = fetcher.get_tile(MAP_V1_URL, DETAIL_ZOOM, x, y)
        for picture in pictures_from_tile(raw, x, y, DETAIL_ZOOM):
            if bbox_contains(picture["lon"], picture["lat"], bbox):
                pictures[picture["id"]] = picture

    months = Counter()
    undated = 0
    for picture in pictures.values():
        month = capture_month(picture["ts"])
        if month is None:
            undated += 1
        else:
            months[month] += 1
    contributors = Counter(p["account_id"] for p in pictures.values())
    top_share = (
        contributors.most_common(1)[0][1] / len(pictures) if pictures and contributors else None
    )
    return {
        "city_id": city["city_id"],
        "display_name": city["display_name"],
        "country_name": city["country_name"],
        "tiles_total": len(tiles),
        "tiles_probed": len(order),
        "complete": len(order) == len(tiles),
        "pictures": len(pictures),
        "pictures_360": sum(1 for p in pictures.values() if p["type"] == TYPE_360),
        "pictures_flat": sum(
            1 for p in pictures.values() if p["type"] is not None and p["type"] != TYPE_360
        ),
        "pictures_type_absent": sum(1 for p in pictures.values() if p["type"] is None),
        "undated_pictures": undated,
        "distinct_capture_months": len(months),
        "capture_months": dict(sorted(months.items())),
        "distinct_contributors": len(contributors),
        "top_contributor_share": round(top_share, 4) if top_share is not None else None,
        "distinct_sequences": len(
            {p["sequence_id"] for p in pictures.values() if p["sequence_id"]}
        ),
    }


def stage_access(city: dict[str, Any], fetcher: Fetcher) -> dict[str, Any]:
    """
    Pin the three /api/search behaviours that phase 2 would design around.

    Each is a claim this study makes in prose, and prose is not evidence -- so
    each gets a recorded response rather than a sentence. All three are the
    silent kind of failure, which is the only reason they are worth four
    requests:

      * PAGINATION. If `links` is empty at every limit and no match count is
        reported, a bbox with more pictures than `limit` is indistinguishable
        from one holding exactly `limit`. A collector built on search would
        report a ceiling as a measurement.
      * THE `datetime` PARAMETER IS IGNORED. Not rejected, not empty -- the
        SAME rows come back. An incremental "everything since last run" fetch
        would silently re-read the whole history and report it as new.
      * `filter=field_of_view=360` DROPS THE EXIF-LESS PICTURES rather than
        treating them as unknown, so a 360-only search filter discards imagery
        the tile layer classifies perfectly well.
    """
    min_lon, min_lat, max_lon, max_lat = city["bbox"]
    bbox = f"{min_lon},{min_lat},{max_lon},{max_lat}"
    probes = {}

    def search(label, params):
        response = fetcher.get(SEARCH_URL, params={"bbox": bbox, **params})
        payload = response.json()
        features = payload.get("features", [])
        classes = Counter(
            fov_class(
                (f.get("properties", {}).get("pers:interior_orientation") or {}).get(
                    "field_of_view"
                )
            )
            for f in features
        )
        stamps = sorted(
            f.get("properties", {}).get("datetime")
            for f in features
            if f.get("properties", {}).get("datetime")
        )
        probes[label] = {
            "params": params,
            "status": response.status_code,
            "response_bytes": len(response.content),
            "features": len(features),
            "links": payload.get("links"),
            "reports_number_matched": "numberMatched" in payload,
            "fov_classes": dict(sorted(classes.items())),
            "datetime_span": [stamps[0], stamps[-1]] if stamps else None,
            "first_ids": [f.get("id") for f in features[:5]],
        }

    search("baseline", {"limit": ACCESS_PROBE_LIMIT})
    search("datetime_filtered", {"limit": ACCESS_PROBE_LIMIT, "datetime": ACCESS_PROBE_DATETIME})
    search("fov_360_filtered", {"limit": ACCESS_PROBE_LIMIT, "filter": "field_of_view=360"})

    baseline, filtered = probes["baseline"], probes["datetime_filtered"]
    return {
        "city_id": city["city_id"],
        "display_name": city["display_name"],
        "probes": probes,
        # The findings, computed rather than asserted, so a future re-run that
        # finds Panoramax has FIXED any of these fails loudly instead of
        # leaving three stale sentences in the writeup.
        "search_paginates": bool(baseline["links"]) or baseline["reports_number_matched"],
        "datetime_filter_honoured": baseline["first_ids"] != filtered["first_ids"],
        "fov_filter_drops_absent": probes["fov_360_filtered"]["fov_classes"].get("absent", 0) == 0
        and baseline["fov_classes"].get("absent", 0) > 0,
    }


def reconcile_fov_against_type(
    sampled: list[dict[str, Any]], fetcher: Fetcher, max_tiles: int
) -> dict[str, Any]:
    """
    Cross-tabulate each sampled picture's SEARCH field of view against the same
    picture's TILE type, by looking it up in the z15 pictures layer.

    This is the measurement that decides what #116 stratification means for
    Panoramax, and it has to be per picture rather than per total. The tile
    layer's federation-wide counts already show it never leaves a picture
    unclassified, but "the totals add up" cannot tell you WHICH class an
    EXIF-less picture lands in -- and #316 assumed the answer was "a third one".

    Tiles are chosen by picture position, capped at `max_tiles`, and a picture
    whose tile was not fetched is reported as `not_in_fetched_tiles` rather
    than dropped: an unlooked-at picture is not evidence of anything, and
    silently omitting it would inflate whichever cell of the table happened to
    be cheap to fill.
    """
    wanted: dict[tuple[int, int], None] = {}
    for picture in sampled:
        x, y = _tile_xy(picture["lon"], picture["lat"], DETAIL_ZOOM)
        if len(wanted) < max_tiles or (x, y) in wanted:
            wanted[(x, y)] = None
    types: dict[str, Any] = {}
    for x, y in wanted:
        raw = fetcher.get_tile(MAP_V1_URL, DETAIL_ZOOM, x, y)
        for picture in pictures_from_tile(raw, x, y, DETAIL_ZOOM):
            types[picture["id"]] = picture["type"]

    table = Counter()
    for picture in sampled:
        tile_type = types.get(picture["id"], "not_in_fetched_tiles")
        table[f"{picture['fov_class']}__{tile_type}"] += 1
    return {
        "tiles_fetched": len(wanted),
        "sampled": len(sampled),
        "table": dict(sorted(table.items())),
    }


def _tile_xy(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """The (x, y) tile a point falls in. Thin wrapper over the shared math."""
    fx, fy = lonlat_to_tile_frac(lon, lat, zoom)
    return int(fx), int(fy)


def fov_class(field_of_view: Any) -> str:
    """`360`, `flat` or `absent` -- the SEARCH response's three-state reading."""
    if field_of_view is None:
        return "absent"
    return "360" if field_of_view >= 360 else "flat"


def instances_city(
    city: dict[str, Any], fetcher: Fetcher, search_limit: int, reconcile_tiles: int
) -> dict[str, Any]:
    """
    A bounded /api/search sample of one city, for instance attribution and for
    the field-of-view reconciliation.

    This is a SAMPLE and can never be anything else: /api/search does not
    paginate and reports no match count, so `sampled` is capped at
    `search_limit` by construction and a city at the cap has an unknown
    remainder. It buys two things the tiles cannot: the `via` link naming the
    source instance, and `pers:interior_orientation.field_of_view`, whose
    absent rate here is what shows "absent" is a property of this endpoint's
    EXIF passthrough rather than of the imagery.
    """
    min_lon, min_lat, max_lon, max_lat = city["bbox"]
    response = fetcher.get(
        SEARCH_URL,
        params={"bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}", "limit": search_limit},
    )
    features = response.json().get("features", [])
    instances = Counter()
    producers = Counter()
    fov_counts = Counter()
    sampled = []
    for feature in features:
        for link in feature.get("links", []):
            if link.get("rel") == "via":
                instances[link.get("instance_name") or link.get("href")] += 1
                break
        properties = feature.get("properties", {})
        producers[properties.get("geovisio:producer")] += 1
        orientation = properties.get("pers:interior_orientation") or {}
        klass = fov_class(orientation.get("field_of_view"))
        fov_counts[klass] += 1
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if feature.get("id") and coordinates:
            sampled.append(
                {
                    "id": str(feature["id"]),
                    "lon": coordinates[0],
                    "lat": coordinates[1],
                    "fov_class": klass,
                }
            )

    return {
        "city_id": city["city_id"],
        "display_name": city["display_name"],
        "sampled": len(features),
        "at_search_limit": len(features) >= search_limit,
        "instances": dict(instances.most_common()),
        "producers": dict(producers.most_common(5)),
        "fov_360": fov_counts["360"],
        "fov_flat": fov_counts["flat"],
        "fov_absent": fov_counts["absent"],
        "reconciliation": reconcile_fov_against_type(sampled, fetcher, reconcile_tiles),
        "response_bytes": len(response.content),
    }


def federation_snapshot(fetcher: Fetcher) -> dict[str, Any]:
    """
    Two requests: the registered instances, and the federation's own totals.

    ``/api/stats`` is not advertised in the API root's link list -- it was found
    by a parallel study, not by enumerating the catalog -- and it is strictly
    better than deriving totals from a z0 tile: one small response instead of a
    1.9 MB one, and it carries per-instance contributor counts and captured
    kilometres that no tile layer exposes.
    """
    instances = fetcher.get(INSTANCES_URL).json().get("instances", [])
    stats = fetcher.get(STATS_URL).json()
    by_instance = stats.get("stats_by_instance", {})
    return {
        "registered_instances": len(instances),
        "generic_stats": stats.get("generic_stats"),
        "instances": sorted(
            (
                {
                    "name": instance.get("name"),
                    "url": instance.get("url"),
                    "last_successful_harvest": instance.get("last_succesful_harvest"),
                    "nb_pictures": (by_instance.get(instance.get("name")) or {}).get("nb_pictures"),
                    "nb_contributors": (by_instance.get(instance.get("name")) or {}).get(
                        "nb_contributors"
                    ),
                }
                for instance in instances
            ),
            key=lambda i: (-(i["nb_pictures"] or 0), i["name"] or ""),
        ),
    }


# ── Raw artifacts and the committed record ─────────────────────────────────


def raw_path(raw_dir: str, stage: str) -> str:
    return os.path.join(raw_dir, f"{stage}.json")


def write_raw(raw_dir: str, stage: str, payload: dict[str, Any]) -> str:
    """
    Persist one stage's raw output to the gitignored /experiments tree.

    Written BEFORE any summarizing or printing, so a formatting mistake cannot
    discard a paced run that took an afternoon -- the ordering discipline
    `kartaview_sweep_cost.py` settled on in place of atomic writes.
    """
    os.makedirs(raw_dir, exist_ok=True)
    path = raw_path(raw_dir, stage)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
    return path


def read_raw(raw_dir: str, stage: str) -> dict[str, Any] | None:
    """A stage's raw output, or None if it was never run (never a silent {})."""
    path = raw_path(raw_dir, stage)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def docs_generated_by(args: argparse.Namespace) -> str:
    """
    The canonical command that writes the committed record.

    Spelled from the REAL parsed values rather than a literal, so a record can
    never claim an invocation nobody ran; every argument that can move a number
    appears, and only defaults are elided. Do not reach for these with a
    `getattr` default -- a Namespace missing an argument is exactly how a seed
    goes unrecorded.
    """
    parts = ["python", "scripts/panoramax_feasibility.py", "--analyze"]
    if args.docs_dir != DOCS_DIR_DEFAULT:
        parts += ["--docs-dir", args.docs_dir]
    if args.raw_dir != RAW_DIR_DEFAULT:
        parts += ["--raw-dir", args.raw_dir]
    if args.catalog_label != CATALOG_LABEL_DEFAULT:
        parts += ["--catalog-label", args.catalog_label]
    return " ".join(parts)


def measured_by(args: argparse.Namespace, stage: str) -> str:
    """The invocation that spent the network requests for one stage."""
    parts = ["python", "scripts/panoramax_feasibility.py", "--stage", stage]
    if args.rate != DEFAULT_RATE_PER_MINUTE:
        parts += ["--rate", str(args.rate)]
    if args.jitter != DEFAULT_JITTER:
        parts += ["--jitter", str(args.jitter)]
    if args.seed != DEFAULT_SEED:
        parts += ["--seed", str(args.seed)]
    if stage == "screen" and args.screen_variant != DEFAULT_SCREEN_VARIANT:
        parts += ["--screen-variant", args.screen_variant]
    if stage == "measure":
        if args.leaders != DEFAULT_LEADERS:
            parts += ["--leaders", str(args.leaders)]
        if args.typical != DEFAULT_TYPICAL:
            parts += ["--typical", str(args.typical)]
        if args.controls != DEFAULT_CONTROLS:
            parts += ["--controls", str(args.controls)]
        if args.reuse_measured:
            parts += ["--reuse-measured"]
    if stage in ("measure", "detail") and args.max_tiles_per_city != DEFAULT_MAX_TILES_PER_CITY:
        parts += ["--max-tiles-per-city", str(args.max_tiles_per_city)]
    if stage == "detail" and args.detail_cities != DEFAULT_DETAIL_CITIES:
        parts += ["--detail-cities", str(args.detail_cities)]
    if stage == "instances":
        if args.search_limit != DEFAULT_SEARCH_LIMIT:
            parts += ["--search-limit", str(args.search_limit)]
        if args.reconcile_tiles != DEFAULT_RECONCILE_TILES:
            parts += ["--reconcile-tiles", str(args.reconcile_tiles)]
    return " ".join(parts)


CATALOG_LABEL_DEFAULT = "laptop"


def summarize_screen(screen: dict[str, Any]) -> dict[str, Any]:
    """The gate, in numbers: how much of the catalog the screen cannot rule out."""
    rows = screen["cities"]
    bounds = [float(row["screen_pictures_upper_bound"]) for row in rows]
    positive = [row for row in rows if row["screen_pictures_upper_bound"] > 0]
    by_country = Counter(row["country_name"] for row in positive)
    return {
        "cities_screened": len(rows),
        "cities_with_zero_upper_bound": len(rows) - len(positive),
        "cities_not_ruled_out": len(positive),
        "share_not_ruled_out": round(len(positive) / len(rows), 4) if rows else None,
        "upper_bound_distribution": describe(bounds),
        "not_ruled_out_by_country": dict(by_country.most_common()),
        "requests_spent": screen["requests_spent"],
    }


def summarize_measure(measure: dict[str, Any]) -> dict[str, Any]:
    """
    What the exact stage found, kept split by group and never pooled.

    The three groups are three questions (is there a deployment anywhere; what
    does a not-ruled-out city hold; does a zero screen hold up), and one
    distribution laid over all of them would answer none of them -- the
    leaders are the extreme tail by construction and would drag any pooled
    median toward a value no city has.
    """
    out: dict[str, Any] = {
        "requests_spent": measure["requests_spent"],
        "empty_tiles": measure.get("empty_tiles", 0),
        "cities_failed": measure.get("failed", []),
        "reused_rows": measure.get("reused_rows", 0),
    }
    for group in MEASURE_GROUPS:
        rows = measure[group]
        counts = [float(row["pictures"]) for row in rows]
        scaled = [float(row["pictures_scaled_to_bbox"]) for row in rows]
        with_any = [row for row in rows if row["pictures"] > 0]
        out[group] = {
            "n": len(rows),
            "cities_with_any_pictures": len(with_any),
            "pictures_total": sum(row["pictures"] for row in rows),
            "pictures_360_total": sum(row["pictures_360"] for row in rows),
            "pictures_flat_total": sum(row["pictures_flat"] for row in rows),
            "picture_count_distribution": describe(counts) if counts else None,
            "picture_count_scaled_distribution": describe(scaled) if scaled else None,
            "cities_truncated": sum(1 for row in rows if not row["complete"]),
        }
    return out


def summarize_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Dates and contributors, over the cities that had enough imagery to ask."""
    rows = detail["cities"]
    complete = [row for row in rows if row["complete"]]
    months = [float(row["distinct_capture_months"]) for row in rows]
    shares = [
        float(row["top_contributor_share"])
        for row in rows
        if row["top_contributor_share"] is not None
    ]
    total = sum(row["pictures"] for row in rows)
    return {
        "n": len(rows),
        "cities_complete": len(complete),
        "pictures_seen": total,
        "pictures_360": sum(row["pictures_360"] for row in rows),
        "pictures_flat": sum(row["pictures_flat"] for row in rows),
        "pictures_type_absent": sum(row["pictures_type_absent"] for row in rows),
        "undated_pictures": sum(row["undated_pictures"] for row in rows),
        "distinct_month_distribution": describe(months) if months else None,
        "top_contributor_share_distribution": describe(shares) if shares else None,
        "requests_spent": detail["requests_spent"],
    }


def summarize_instances(instances: dict[str, Any]) -> dict[str, Any]:
    """
    The search sample's own summary, including the reconciliation #316 needs.

    `fov_absent_share` is the headline: the same pictures the tile layer types
    without a single gap arrive here with a field of view missing this often,
    which is what makes "absent" an instrument artifact rather than a third
    imagery state.
    """
    rows = instances["cities"]
    sampled = sum(row["sampled"] for row in rows)
    absent = sum(row["fov_absent"] for row in rows)
    by_instance = Counter()
    for row in rows:
        by_instance.update(row["instances"])
    return {
        "n": len(rows),
        "pictures_sampled": sampled,
        "cities_at_search_limit": sum(1 for row in rows if row["at_search_limit"]),
        "fov_360": sum(row["fov_360"] for row in rows),
        "fov_flat": sum(row["fov_flat"] for row in rows),
        "fov_absent": absent,
        "fov_absent_share": round(absent / sampled, 4) if sampled else None,
        "instances_seen": dict(by_instance.most_common()),
        "requests_spent": instances["requests_spent"],
    }


def cross_check(measure: dict[str, Any] | None, detail: dict[str, Any] | None):
    """
    Do the two instruments agree on the cities they both measured completely?

    The z14 H3 grid reports counters the server aggregated; the z15 pictures
    layer is a row per picture that this script counts itself. They are
    independent paths to the same number, and nothing else in this study checks
    the aggregate layer at all -- if the hex counters meant something other
    than "pictures in this hexagon", every count here would be wrong in a way
    no internal consistency check could see.

    Only cities COMPLETE in both are compared: a truncated city's counts are a
    sample of a different subset in each stage, so a disagreement there would
    say nothing. Returns None when there is no such city.
    """
    if not measure or not detail:
        return None
    grid_counts = {
        row["city_id"]: row for group in MEASURE_GROUPS for row in measure[group] if row["complete"]
    }
    pairs = []
    for row in detail["cities"]:
        if not row["complete"] or row["city_id"] not in grid_counts:
            continue
        grid = grid_counts[row["city_id"]]["pictures"]
        pairs.append(
            {
                "city_id": row["city_id"],
                "z14_grid_pictures": grid,
                "z15_pictures_layer": row["pictures"],
                "ratio": round(row["pictures"] / grid, 4) if grid else None,
            }
        )
    if not pairs:
        return None
    ratios = [p["ratio"] for p in pairs if p["ratio"] is not None]
    return {
        "n": len(pairs),
        "ratio_distribution": describe(ratios) if ratios else None,
        "cities": sorted(pairs, key=lambda p: p["city_id"]),
    }


def summarize_access(access: dict[str, Any]) -> dict[str, Any]:
    """
    The three search behaviours, as an agreement count across cities.

    Reported as "N of M cities" rather than a bare boolean because a behaviour
    that held in one city and not another would be the interesting result, and
    a single flag would hide it.
    """
    rows = access["cities"]
    return {
        "n": len(rows),
        "cities_where_search_paginates": sum(1 for r in rows if r["search_paginates"]),
        "cities_where_datetime_filter_honoured": sum(
            1 for r in rows if r["datetime_filter_honoured"]
        ),
        "cities_where_fov_filter_drops_absent": sum(
            1 for r in rows if r["fov_filter_drops_absent"]
        ),
        "requests_spent": access["requests_spent"],
    }


def build_record(args: argparse.Namespace) -> dict[str, Any]:
    """
    Merge whatever stages have been run into the committed metrics record.

    A stage that was never run is recorded as an explicit null with the raw
    path it would have come from, so a reader can tell "not measured" from
    "measured zero" -- the distinction a silently omitted key destroys.
    """
    screen = read_raw(args.raw_dir, "screen")
    measure = read_raw(args.raw_dir, "measure")
    detail = read_raw(args.raw_dir, "detail")
    instances = read_raw(args.raw_dir, "instances")
    access = read_raw(args.raw_dir, "access")

    about = {
        "experiment": TOPIC,
        "writeup": WRITEUP,
        "issue": ISSUE,
        "generated_by": docs_generated_by(args),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "catalog_label": args.catalog_label,
        "api_base": API_BASE,
        "credential": None,
        "screen_zoom": SCREEN_ZOOM,
        "measure_zoom": MEASURE_ZOOM,
        "detail_zoom": DETAIL_ZOOM,
        "measured_by": {
            stage: raw["_measured_by"]
            for stage, raw in (
                ("screen", screen),
                ("measure", measure),
                ("detail", detail),
                ("instances", instances),
                ("access", access),
            )
            if raw is not None
        },
        "note": DOCS_RECORD_NOTE,
    }

    def block(raw, summarizer, key):
        if raw is None:
            return {"available": False, "source": raw_path(args.raw_dir, key)}
        return {"available": True, "summary": summarizer(raw), **{"detail": raw}}

    return {
        "_about": about,
        "federation": (instances or {}).get("federation"),
        "screen": block(screen, summarize_screen, "screen"),
        "measure": block(measure, summarize_measure, "measure"),
        "detail": block(detail, summarize_detail, "detail"),
        "instances": block(instances, summarize_instances, "instances"),
        "access": block(access, summarize_access, "access"),
        "cross_check": cross_check(measure, detail),
    }


def write_docs_record(record: dict[str, Any], docs_dir: str) -> str:
    """Sole producer of docs/experiments/panoramax-feasibility_metrics.json."""
    os.makedirs(docs_dir, exist_ok=True)
    path = os.path.join(docs_dir, DOCS_METRICS_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=False)
        handle.write("\n")
    return path


# ── CLI ────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Issue #316 phase 1: measure Panoramax coverage over our frozen grids.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--stage",
        choices=("screen", "measure", "detail", "instances", "access"),
        help="which measurement to run; omit with --analyze to work offline",
    )
    parser.add_argument("--analyze", action="store_true", help="rebuild the committed record")
    parser.add_argument("--db", default="data/streetscape_tracker.db")
    parser.add_argument(
        "--catalog-label",
        default=CATALOG_LABEL_DEFAULT,
        help="which catalog this ran against; a LABEL, never a path, so no machine's "
        "directory layout lands in a committed file",
    )
    parser.add_argument("--docs-dir", default=DOCS_DIR_DEFAULT)
    parser.add_argument("--raw-dir", default=RAW_DIR_DEFAULT)
    parser.add_argument(
        "--screen-variant", choices=tuple(SCREEN_VARIANTS), default=DEFAULT_SCREEN_VARIANT
    )
    parser.add_argument("--rate", type=int, default=DEFAULT_RATE_PER_MINUTE)
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--leaders", type=int, default=DEFAULT_LEADERS)
    parser.add_argument("--typical", type=int, default=DEFAULT_TYPICAL)
    parser.add_argument("--controls", type=int, default=DEFAULT_CONTROLS)
    parser.add_argument("--detail-cities", type=int, default=DEFAULT_DETAIL_CITIES)
    parser.add_argument("--max-tiles-per-city", type=int, default=DEFAULT_MAX_TILES_PER_CITY)
    parser.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--reconcile-tiles", type=int, default=DEFAULT_RECONCILE_TILES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--city", action="append", help="override the stage's city set (repeatable)"
    )
    parser.add_argument(
        "--reuse-measured",
        action="store_true",
        help="reuse rows from an existing measure.json whose tile plan the current settings "
        "reproduce exactly, instead of re-fetching them",
    )
    parser.add_argument("--dry-run", action="store_true", help="plan and price it, spend nothing")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _cities_by_id(cities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {city["city_id"]: city for city in cities}


def _measure_targets(args, cities, by_id) -> dict[str, list[str]]:
    """The grouped id lists stage `measure` will walk."""
    if args.city:
        return {"leaders": [c for c in args.city if c in by_id], "typical": [], "controls": []}
    screen = read_raw(args.raw_dir, "screen")
    if screen is None:
        raise SystemExit(
            f"--stage measure needs {raw_path(args.raw_dir, 'screen')}; run --stage screen first "
            f"(104 requests) or name cities with --city."
        )
    return select_measure_set(screen, args.leaders, args.typical, args.controls, args.seed)


def _detail_targets(args, by_id) -> list[str]:
    """The ids stage `detail` will walk: the richest measured cities."""
    if args.city:
        return [c for c in args.city if c in by_id]
    measure = read_raw(args.raw_dir, "measure")
    if measure is None:
        raise SystemExit(
            f"--stage detail needs {raw_path(args.raw_dir, 'measure')}; run --stage measure first "
            f"or name cities with --city."
        )
    measured = [row for group in MEASURE_GROUPS for row in measure[group]]
    ranked = sorted(measured, key=lambda row: (-row["pictures"], row["city_id"]))
    return [row["city_id"] for row in ranked if row["pictures"] > 0][: args.detail_cities]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.stage and not args.analyze:
        raise SystemExit("nothing to do: pass --stage <name>, --analyze, or both")

    if args.stage:
        # The guard is about not SPENDING requests from a collection host, so
        # an --analyze-only run is deliberately allowed to pass it.
        if not args.dry_run:
            refuse_on_collection_host()
        cities = load_cities(args.db)
        by_id = _cities_by_id(cities)
        limiter = SpacedRateLimiter(args.rate, jitter=args.jitter)
        fetcher = None if args.dry_run else Fetcher(limiter, args.timeout)

        try:
            payload = _run_stage(args, cities, by_id, fetcher)
        except BlockedError as exc:
            logger.error(str(exc))
            return 75
        if args.dry_run:
            print(json.dumps(payload, indent=2))
            return 0
        payload["_measured_by"] = measured_by(args, args.stage)
        path = write_raw(args.raw_dir, args.stage, payload)
        logger.info(f"wrote {path} ({fetcher.requests_spent} requests spent)")

    if args.analyze:
        record = build_record(args)
        path = write_docs_record(record, args.docs_dir)
        logger.info(f"wrote {path}")
        _print_summary(record)
    return 0


def _run_stage(args, cities, by_id, fetcher) -> dict[str, Any]:
    """Dispatch one stage; every branch returns the stage's raw payload."""
    if args.stage == "screen":
        return stage_screen(cities, fetcher, args.screen_variant)

    if args.stage == "measure":
        groups = _measure_targets(args, cities, by_id)
        prior = read_raw(args.raw_dir, "measure") if args.reuse_measured else None
        if args.dry_run:
            planned = {
                group: sum(
                    min(
                        args.max_tiles_per_city,
                        len(tiles_for_bbox(*by_id[c]["bbox"], MEASURE_ZOOM)),
                    )
                    for c in ids
                )
                for group, ids in groups.items()
            }
            return {**groups, "planned_requests": planned, "planned_total": sum(planned.values())}
        spent_before = fetcher.requests_spent
        out: dict[str, Any] = {group: [] for group in MEASURE_GROUPS}
        out["failed"] = []
        out["reused_rows"] = 0
        for group in MEASURE_GROUPS:
            ids = groups[group]
            for index, city_id in enumerate(ids, start=1):
                # A city that fails is RECORDED and skipped, never dropped: an
                # omitted city silently shrinks a denominator, and the group
                # sizes are what every estimate here rests on.
                reused = reusable_measure_row(
                    prior, by_id[city_id], args.max_tiles_per_city, args.seed
                )
                if reused is not None:
                    out[group].append(reused)
                    out["reused_rows"] += 1
                    continue
                try:
                    row = measure_city(by_id[city_id], fetcher, args.max_tiles_per_city, args.seed)
                except BlockedError:
                    raise
                except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                    logger.error(f"measure {group} {city_id} FAILED: {exc}")
                    out["failed"].append({"group": group, "city_id": city_id, "error": str(exc)})
                    continue
                out[group].append(row)
                logger.info(
                    f"measure {group} {index}/{len(ids)} {city_id}: "
                    f"{row['pictures']} pictures over {row['tiles_probed']} tiles"
                )
        out["requests_spent"] = fetcher.requests_spent - spent_before
        out["empty_tiles"] = fetcher.empty_tiles
        return out

    if args.stage == "detail":
        targets = _detail_targets(args, by_id)
        if args.dry_run:
            planned = sum(
                min(args.max_tiles_per_city, len(tiles_for_bbox(*by_id[c]["bbox"], DETAIL_ZOOM)))
                for c in targets
            )
            return {"cities": targets, "planned_requests": planned}
        spent_before = fetcher.requests_spent
        rows = []
        for index, city_id in enumerate(targets, start=1):
            row = detail_city(by_id[city_id], fetcher, args.max_tiles_per_city, args.seed)
            rows.append(row)
            logger.info(
                f"detail {index}/{len(targets)} {city_id}: {row['pictures']} pictures, "
                f"{row['distinct_capture_months']} months"
            )
        return {"cities": rows, "requests_spent": fetcher.requests_spent - spent_before}

    if args.stage == "instances":
        targets = args.city or _detail_targets(args, by_id)
        if args.dry_run:
            return {
                "cities": targets,
                "planned_requests": 2 + len(targets) * (1 + args.reconcile_tiles),
            }
        spent_before = fetcher.requests_spent
        federation = federation_snapshot(fetcher)
        rows = []
        for index, city_id in enumerate(targets, start=1):
            row = instances_city(by_id[city_id], fetcher, args.search_limit, args.reconcile_tiles)
            rows.append(row)
            logger.info(
                f"instances {index}/{len(targets)} {city_id}: {row['sampled']} sampled, "
                f"{len(row['instances'])} instances"
            )
        return {
            "federation": federation,
            "cities": rows,
            "requests_spent": fetcher.requests_spent - spent_before,
        }

    if args.stage == "access":
        targets = args.city or _detail_targets(args, by_id)
        if args.dry_run:
            return {"cities": targets, "planned_requests": 3 * len(targets)}
        spent_before = fetcher.requests_spent
        rows = [stage_access(by_id[city_id], fetcher) for city_id in targets]
        for row in rows:
            logger.info(
                f"access {row['city_id']}: paginates={row['search_paginates']} "
                f"datetime_honoured={row['datetime_filter_honoured']} "
                f"fov_filter_drops_absent={row['fov_filter_drops_absent']}"
            )
        return {"cities": rows, "requests_spent": fetcher.requests_spent - spent_before}

    raise AssertionError(f"unhandled stage {args.stage!r}")


def _print_summary(record: dict[str, Any]) -> None:
    """A short human read of the record; the file on disk is the artifact."""
    for key in ("screen", "measure", "detail", "instances", "access"):
        block = record[key]
        if not block.get("available"):
            print(f"{key:10s} not measured ({block['source']})")
            continue
        print(f"{key:10s} {json.dumps(block['summary'], sort_keys=False)[:400]}")


if __name__ == "__main__":
    sys.exit(main())
