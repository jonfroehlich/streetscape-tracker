"""
Issue #225 follow-up: what does it actually COST to sweep a city's frozen grid?

    python scripts/kartaview_sweep_cost.py --cities                 # list candidates
    python scripts/kartaview_sweep_cost.py --city bend--oregon--united-states
    python scripts/kartaview_sweep_cost.py --sample default --docs-dir docs/experiments

This is the number that gates a production KartaView channel and it is the one
number kartaview-feasibility.md could not supply: **circles x pages to cover a
frozen grid bbox**. Mapillary's median city is 12 tile requests; until this is
measured, nothing about cadence, daily budget or city-set size can be sized.

WHAT THIS COSTS, AND WHY THE PLAN IS THE MEASUREMENT. A sweep's requests are

    requests = cells_visited + sum over leaves of (pages(total) - 1)

because every cell -- whether it ends up a leaf or gets subdivided -- costs one
page-1 request, and page 1 reports `totalFilteredItems` for that circle. So
planning the sweep IS paying its first half, and it predicts the second half
exactly. This script therefore issues page 1 only, never pages 2+: it measures
`cells_visited` for real and computes the page count from the totals it sees.

THREE MEASURED FACTS THIS DESIGN RESTS ON (2026-08-19, laptop, authenticated):

  1. Pagination is EXHAUSTIVE. Seattle r=400 ipp=200 returned pages 1-6 with zero
     id overlap between any pair, union == totalFilteredItems == 1004, page 7
     empty. So a truncated circle is PAGED, not subdivided, and page 1 prices it
     before you pay. The feasibility probe never incremented `page`, so this was
     untested -- and the whole cost model above depends on it.
  2. apiCode 690 is FLAKY, not a function of (radius, ipp). At the Singapore
     point r=1000 was refused at ipp=10 AND ipp=100, then answered at ipp=2000
     (n=2000, total=10903) -- a rung the study recorded as failing there. So a
     refusal is RETRIED before the cell is subdivided, and both counts are in the
     record.
  3. Circle count, not photo count, dominates a large sparse bbox. Seattle's
     frozen grid is 498 km2; paging its ~477k photos is ~375 requests at
     ipp=2000, but tiling it with r=400 circles is ~1556 cells. Hence the
     adaptive walk below starts LARGE and only shrinks where the server refuses.

SAMPLING, STATED RATHER THAN HIDDEN. A full plan of Singapore's 2528 km2 is
~1260 cells at the default start radius, i.e. ~1.3 h of paced requests for one
city. `--max-requests-per-city` bounds it, and when it bites the record carries
`plan_complete: false` plus `roots_probed` / `roots_total` so the extrapolation
is visible and checkable. Root cells are walked in a SEEDED SHUFFLE, not raster
order, so a truncated plan is a spatially unbiased sample rather than the top of
the map.

Same posture as the feasibility probe: laptop-only (a per-IP limit found from
makelab2 takes out the nightly batch), paced to the documented hourly ceiling,
and it writes only a derived metrics record -- never bulk pages, never data/.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import requests
from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kartaview_probe import (  # noqa: E402
    IPP_MAX,
    RADIUS_LADDER_M,
    REQUESTS_PER_HOUR_ANON,
    REQUESTS_PER_HOUR_AUTH,
    BackpressureError,
    HourlyRateLimiter,
    ProbeError,
    TransportError,
    _post_nearby,
    refuse_on_collection_host,
)

from streetscape_metadata_tracker import config as cfg  # noqa: E402
from streetscape_metadata_tracker import paths  # noqa: E402
from streetscape_metadata_tracker.download_mapillary import grid_bbox  # noqa: E402

logger = logging.getLogger("kartaview_sweep_cost")

DOCS_METRICS_NAME = "kartaview-sweep-cost_metrics.json"

# Start big and shrink only where refused (fact 3 in the docstring). 1000 m is
# the largest rung the feasibility ladder ever saw answered, so it is the
# largest radius with any evidence behind it; the record reports the depth-0
# refusal rate so a future run can justify raising it.
DEFAULT_START_RADIUS_M = 1000

# The smallest circle the sweep will ever ask for. The feasibility ladder's
# smallest rung (100 m) answered at every one of its 8 targets, so a cell that
# still refuses at the floor is a genuine defect, not backpressure.
#
# The guard belongs on the CHILDREN, not on the cell being split (see
# ``can_subdivide``): asking "is this cell above the floor?" happily turns a
# 125 m cell into four 63 m ones, i.e. it enforces a floor of RADIUS_FLOOR_M / 2
# and asks the server for radii no rung has ever tested.
RADIUS_FLOOR_M = 100

# Pages are cheap but deep paging is untested past 6 (fact 1 was measured to
# page 7). Beyond this a cell is subdivided instead, which trades one wasted
# page-1 for four shallower circles.
MAX_PAGES_PER_CELL = 10

# Fact 2: a refusal is retried before it is believed -- and retrying is 4x
# cheaper than subdividing (1 request vs 4, each of which may cascade), so
# the budget is generous rather than minimal. See _probe_cell.
DEFAULT_BACKPRESSURE_RETRIES = 3

# Probes per rung during calibration. A rung is accepted only if every one
# answers, so this trades requests for confidence that the radius holds
# across the bbox rather than at one lucky point.
DEFAULT_CALIBRATION_PROBES = 2

# The study set, in two halves that answer two different questions.
#
# The GEOMETRIC term -- how many cells a bbox needs -- is exactly computable
# from area, so it does not need measuring at every size. What needs measuring
# is the EMPIRICAL overhead on top of it: the retry rate, the share of cells
# that must be subdivided, and the pages per cell. So the set walks the
# catalog's area deciles (measured 2026-08-19 over 1,144 enabled cities: p5 1.0,
# p20 3.2, p35 7.9, p50 19.5, p65 55.6, p80 149.7, p90 353.1, p95 736.1, p99
# 2406.8 km2) to check the model holds across two orders of magnitude of area...
DECILE_SAMPLE = (
    "buck-grove--iowa--united-states",  # p5   1.0 km2
    "south-tucson--arizona--united-states",  # p20  3.2
    "emmitsburg--maryland--united-states",  # p35  7.9
    "ithaca--michigan--united-states",  # p50  19.5
    "horace--north-dakota--united-states",  # p65  55.6
    "attleboro--massachusetts--united-states",  # p80  149.7
    "chandler--arizona--united-states",  # p90  353.1
    "milwaukee--wisconsin--united-states",  # p95  736.1
    "las-vegas--nevada--united-states",  # p99  2406.8
)

# ...and a density half, because sweep cost per km2 is worst exactly where the
# imagery is richest. These are the registered cities in the regimes the
# feasibility study measured: SE-Asian Grab markets against North American
# community uploads. Bend is the control that fits a COMPLETE plan cheaply,
# which is what validates the extrapolation the truncated ones rely on.
DENSITY_SAMPLE = (
    "singapore--singapore",
    "manila--capital-district--philippines",
    "seattle--washington--united-states",
    "new-york--new-york--united-states",
    "bend--oregon--united-states",
)

# Resolved against the catalog at run time; anything not registered is skipped
# and NAMED in the record rather than dropped quietly.
SAMPLES = {
    "default": DECILE_SAMPLE + DENSITY_SAMPLE,
    "decile": DECILE_SAMPLE,
    "density": DENSITY_SAMPLE,
}

METERS_PER_DEG_LAT = 111_320.0


# ── Pure geometry and cost model (no network, no catalog) ──────────────────


@dataclass(frozen=True)
class Cell:
    """
    One square of the sweep, and the circle that covers it.

    A square of side ``size_m`` is exactly covered by its circumscribed circle,
    so ``radius_m = size_m * sqrt(2) / 2``. That is what makes the union of a
    lattice of cells cover the bbox with no gap -- and it is also where the
    sweep's redundancy comes from: circle area / cell area is pi/2 ~= 1.571, so
    a photo is seen ~1.6 times and the cross-cell dedup is not optional.
    """

    lat: float
    lon: float
    size_m: float
    depth: int = 0

    @property
    def radius_m(self) -> int:
        return int(round(self.size_m * math.sqrt(2) / 2))


def cells_for_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, cell_size_m: float
) -> list[Cell]:
    """
    Tile a bbox with square cells of side ``cell_size_m``, returning their centres.

    Equirectangular placement about the bbox's own mid-latitude. The lattice is
    a cost model, not a data structure the artifacts depend on, and each cell is
    covered by a circle 1.41x its own width -- so the sub-metre error this
    approximation carries over a 100 km bbox is many orders of magnitude inside
    the slack. The grid the CSV is keyed to still comes from
    ``download_common.generate_grid_points``' geodesic solve, untouched.
    """
    if cell_size_m <= 0:
        raise ValueError(f"cell_size_m must be positive, got {cell_size_m}")
    mid_lat = (min_lat + max_lat) / 2.0
    deg_lat = cell_size_m / METERS_PER_DEG_LAT
    deg_lon = cell_size_m / (METERS_PER_DEG_LAT * math.cos(math.radians(mid_lat)))
    n_y = max(1, math.ceil((max_lat - min_lat) / deg_lat))
    n_x = max(1, math.ceil((max_lon - min_lon) / deg_lon))
    return [
        Cell(
            lat=min_lat + (j + 0.5) * deg_lat,
            lon=min_lon + (i + 0.5) * deg_lon,
            size_m=cell_size_m,
        )
        for j in range(n_y)
        for i in range(n_x)
    ]


def subdivide(cell: Cell) -> list[Cell]:
    """Split one cell into the four half-size cells that exactly cover it."""
    half = cell.size_m / 2.0
    d_lat = (half / 2.0) / METERS_PER_DEG_LAT
    d_lon = (half / 2.0) / (METERS_PER_DEG_LAT * math.cos(math.radians(cell.lat)))
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
    the floor it was meant to enforce.
    """
    return subdivide(cell)[0].radius_m >= RADIUS_FLOOR_M


def pages_for_total(total_filtered_items: int | None, ipp: int) -> int:
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


def sweep_requests(cells_visited: int, leaf_totals: list[int | None], ipp: int) -> int:
    """
    Total requests a real sweep would issue, given a completed plan.

    ``cells_visited`` already includes page 1 of every cell -- internal nodes
    that got subdivided included, because their page-1 was spent before the
    refusal was known -- so only pages 2+ of the leaves are added here.
    """
    extra = sum(pages_for_total(t, ipp) - 1 for t in leaf_totals)
    return cells_visited + extra


def redundancy_factor() -> float:
    """Circle area over cell area: how many times the sweep sees each photo."""
    return math.pi / 2.0


# ── Per-city radius calibration ────────────────────────────────────────────


def calibration_points(
    bbox: tuple[float, float, float, float], n: int
) -> list[tuple[float, float]]:
    """
    Spread ``n`` sample points across a bbox: the centre first, then corners.

    Deterministic, so a re-run calibrates identically. Centre-first because a
    one-point calibration should look at the middle of the city rather than at
    a corner that may be open water.
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
    return candidates[:n]


def calibrate_radius(
    session: requests.Session,
    limiter: HourlyRateLimiter,
    bbox: tuple[float, float, float, float],
    *,
    ipp: int,
    access_token: str | None,
    probes_per_rung: int,
    retries: int,
) -> tuple[int | None, list[dict], int]:
    """
    Find the largest radius this city's server will actually answer.

    THE REASON THIS EXISTS. The working radius is a property of the LOCATION,
    not of how much imagery is there, and it varies by at least 4x across the
    catalog. Measured 2026-08-19: Ithaca MI answered r=1000 at 12 of 12 cells;
    Bend OR answered r=1000 at 4 of 8 first tries and 8 of 8 with one retry; and
    Horace ND -- which holds NO imagery at all -- refused r=1000 on 10 of 10
    attempts across two page sizes while answering r=250 on 4 of 4. Page size
    does not predict it either: Horace refused r=1000 identically at ipp=200 and
    ipp=2000, and Singapore refused r=1000 at ipp=10 and ipp=100 while answering
    it at ipp=2000.

    So a per-cell adaptive descent pays that discovery cost at EVERY cell: a
    root that will never answer costs its retries plus a 1 + 4 + 16 cascade to
    the floor, and it pays that again for each of the city's other roots. One
    calibration up front costs at most ``len(RADIUS_LADDER_M) * probes_per_rung``
    requests for the whole city.

    A rung is accepted only if EVERY probe on it answers, because one lucky
    point would set a radius the rest of the city then re-discovers the hard
    way.

    Returns:
        ``(radius or None, trace, requests_spent)``. ``None`` means no rung
        answered anywhere -- which is NOT the same as "no imagery here", and the
        caller must not record it as a cheap city.
    """
    points = calibration_points(bbox, probes_per_rung)
    trace: list[dict] = []
    spent = 0
    for radius in RADIUS_LADDER_M:
        outcomes = []
        for lat, lon in points:
            cell = Cell(lat=lat, lon=lon, size_m=radius * math.sqrt(2))
            _, cost, outcome = _probe_cell(
                session, limiter, cell, ipp=ipp, access_token=access_token, retries=retries
            )
            spent += cost
            outcomes.append(outcome)
        answered = sum(o.startswith("ok") for o in outcomes)
        trace.append({"radius_m": radius, "probes": len(points), "answered": answered})
        logger.info(f"  calibrate r={radius}m: {answered}/{len(points)} answered")
        if answered == len(points):
            return radius, trace, spent
    return None, trace, spent


def _unreachable_city(
    city: dict,
    bbox: tuple[float, float, float, float],
    start_radius_m: int,
    trace: list[dict],
    spent: int,
) -> dict:
    """
    A city where NO rung answered anywhere.

    Recorded with null estimates rather than zero. Zero would sort to the front
    of the cost distribution and read as the cheapest city in the study, when
    what actually happened is that we never got an answer -- the same
    "refused is not empty" distinction the feasibility probe makes for
    ``max_working_radius_m: null``.
    """
    return {
        "city_id": city["city_id"],
        "bbox_area_km2": round(_bbox_area_km2(bbox), 1),
        "grid_width_m": city["grid_width_m"],
        "grid_height_m": city["grid_height_m"],
        "start_radius_m": start_radius_m,
        "calibrated_radius_m": None,
        "calibration": trace,
        "calibration_requests": spent,
        "reachable": False,
        "note": "no radius answered at any calibration point; NOT evidence of an empty city",
        "root_cells": None,
        "roots_probed": 0,
        "plan_complete": False,
        "requests_spent_planning": spent,
        "cells_visited": 0,
        "leaf_cells": 0,
        "subdivisions": 0,
        "refusals": 0,
        "retries_attempted": 0,
        "retries_cleared": 0,
        "floor_failures": 0,
        "broken_cells": 0,
        "deepest_refusal_radius_m": min(RADIUS_LADDER_M),
        "photos_seen_sum_over_cells": None,
        "photos_in_bbox_estimate": None,
        "sweep_requests_over_probed_roots": None,
        "sweep_requests_estimate": None,
    }


# ── The adaptive walk (this is the part that spends requests) ──────────────


def plan_city(
    session: requests.Session,
    limiter: HourlyRateLimiter,
    city: dict,
    *,
    ipp: int,
    start_radius_m: int,
    max_requests: int,
    access_token: str | None,
    seed: int,
    retries: int = DEFAULT_BACKPRESSURE_RETRIES,
    probes_per_rung: int = DEFAULT_CALIBRATION_PROBES,
) -> dict:
    """
    Calibrate this city's working radius, then walk its bbox at that radius.

    Two phases, because the working radius is a property of the location rather
    than of its imagery (see :func:`calibrate_radius`). Calibration costs at
    most a dozen requests for the whole city; discovering the same thing per
    cell costs a cascade per cell.

    Root cells are visited in a seeded shuffle so that a run stopped by
    ``max_requests`` has sampled the bbox uniformly rather than its northern
    strip. A cell that still refuses at the calibrated radius is retried and
    only then subdivided, and one that refuses all the way to
    ``RADIUS_FLOOR_M`` is recorded as a failure rather than silently costing
    nothing.
    """
    bbox = grid_bbox(
        city["center_lat"],
        city["center_lon"],
        city["grid_width_m"],
        city["grid_height_m"],
        city["step_m"],
    )
    calibrated_m, calibration_trace, calibration_spent = calibrate_radius(
        session,
        limiter,
        bbox,
        ipp=ipp,
        access_token=access_token,
        probes_per_rung=probes_per_rung,
        retries=retries,
    )
    # No rung answered anywhere: record it and spend nothing more. This is NOT
    # evidence of an empty city, so it must not be reported as a cheap one.
    if calibrated_m is None:
        return _unreachable_city(city, bbox, start_radius_m, calibration_trace, calibration_spent)

    cell_size = calibrated_m * math.sqrt(2)
    roots = cells_for_bbox(*bbox, cell_size)
    order = list(range(len(roots)))
    random.Random(seed).shuffle(order)

    spent = calibration_spent
    cells_visited = 0
    leaf_totals: list[int | None] = []
    refusals = subdivisions = retries_attempted = retries_cleared = floor_failures = 0
    broken_cells = 0
    roots_probed = 0
    deepest_refusal_radius_m: int | None = None

    for root_index in order:
        if spent >= max_requests:
            break
        roots_probed += 1
        stack = [roots[root_index]]
        while stack:
            if spent >= max_requests:
                break
            cell = stack.pop()
            total, cost, outcome = _probe_cell(
                session, limiter, cell, ipp=ipp, access_token=access_token, retries=retries
            )
            spent += cost
            cells_visited += 1
            retries_attempted += cost - 1
            if outcome == "ok_after_retry":
                retries_cleared += 1
            if outcome == "broken":
                # Not backpressure: the server failed to answer at all. Asking
                # it for four requests where it just failed to serve one is how
                # a struggling host gets pushed over, so this cell stops here.
                broken_cells += 1
                continue
            if outcome == "refused":
                refusals += 1
                deepest_refusal_radius_m = min(
                    cell.radius_m, deepest_refusal_radius_m or cell.radius_m
                )
                if not can_subdivide(cell):
                    floor_failures += 1
                    continue
                subdivisions += 1
                stack.extend(subdivide(cell))
                continue
            if pages_for_total(total, ipp) > MAX_PAGES_PER_CELL and can_subdivide(cell):
                subdivisions += 1
                stack.extend(subdivide(cell))
                continue
            leaf_totals.append(total)

    plan_complete = roots_probed == len(roots) and spent < max_requests
    scale = len(roots) / roots_probed if roots_probed else 0.0
    sampled_requests = sweep_requests(cells_visited, leaf_totals, ipp)
    seen = sum(t for t in leaf_totals if t)

    return {
        "city_id": city["city_id"],
        "bbox_area_km2": round(_bbox_area_km2(bbox), 1),
        "grid_width_m": city["grid_width_m"],
        "grid_height_m": city["grid_height_m"],
        "start_radius_m": start_radius_m,
        "calibrated_radius_m": calibrated_m,
        "reachable": True,
        "calibration": calibration_trace,
        "calibration_requests": calibration_spent,
        "root_cells": len(roots),
        "roots_probed": roots_probed,
        "plan_complete": plan_complete,
        "requests_spent_planning": spent,
        "cells_visited": cells_visited,
        "leaf_cells": len(leaf_totals),
        "subdivisions": subdivisions,
        "refusals": refusals,
        "retries_attempted": retries_attempted,
        "retries_cleared": retries_cleared,
        "floor_failures": floor_failures,
        "broken_cells": broken_cells,
        # The smallest radius that still got refused. If this sits well above
        # the floor the walk is converging; at or near the floor, the refusal
        # is not about how much was asked for -- Horace ND refuses an EMPTY
        # circle at r >= 250 and answers 0 photos at r=125.
        "deepest_refusal_radius_m": deepest_refusal_radius_m,
        # Sum over overlapping circles, so it double-counts by ~pi/2. Both the
        # raw sum and the de-overlapped estimate are kept: the first is what
        # the sweep pays to fetch, the second is what the city holds.
        "photos_seen_sum_over_cells": seen,
        "photos_in_bbox_estimate": int(round(seen / redundancy_factor() * scale)),
        "sweep_requests_over_probed_roots": sampled_requests,
        "sweep_requests_estimate": int(round(sampled_requests * scale)),
    }


def _probe_cell(
    session: requests.Session,
    limiter: HourlyRateLimiter,
    cell: Cell,
    *,
    ipp: int,
    access_token: str | None,
    retries: int,
) -> tuple[int | None, int, str]:
    """
    Page 1 of one cell. Returns ``(total, requests_spent, outcome)``.

    ``outcome`` is ``ok`` / ``ok_after_retry`` / ``refused`` / ``broken``.

    RETRYING IS FOUR TIMES CHEAPER THAN SUBDIVIDING, which is why the retry
    budget is generous and is the whole reason this is a function. A retry costs
    one request; a subdivision costs four, each of which may retry and subdivide
    in turn -- a cascade to the radius floor is 1 + 4 + 16 = 21 requests. Since
    apiCode 690 was measured flaky (Bend: 4 of 8 cells refused, 4 of 4 cleared on
    one retry), a cell that clears on any retry saves at least 20.

    ``broken`` is kept apart from ``refused`` deliberately. Only a
    BackpressureError means "you asked for too much", so only it may subdivide.
    Asking a server for four requests where it just failed to serve one is how
    the Mapillary block got extended (#198), not a fix for it.

    The two non-backpressure classes are then retried differently, because they
    are different facts. A ``TransportError`` -- reset, timeout, DNS -- is
    transient by nature and gets the same retry budget. A ``ResponseError`` is
    the server giving a definite answer we cannot use (a rejected token, an
    unparseable body); re-asking cannot change it, and a rejected credential
    retried at every cell is a great way to look like an attack.
    """
    for attempt in range(retries + 1):
        try:
            _, total = _post_nearby(
                session,
                limiter,
                cell.lat,
                cell.lon,
                cell.radius_m,
                page=1,
                ipp=ipp,
                access_token=access_token,
            )
        except BackpressureError as e:
            logger.debug(f"r={cell.radius_m}m @ {cell.lat:.4f},{cell.lon:.4f}: {e}")
            continue
        except TransportError as e:
            logger.debug(f"r={cell.radius_m}m @ {cell.lat:.4f},{cell.lon:.4f}: transport: {e}")
            if attempt == retries:
                return None, attempt + 1, "broken"
            continue
        except ProbeError as e:
            logger.warning(
                f"r={cell.radius_m}m @ {cell.lat:.4f},{cell.lon:.4f}: not backpressure and "
                f"not transient, NOT retrying and NOT subdividing: {e}"
            )
            return None, attempt + 1, "broken"
        return total, attempt + 1, ("ok_after_retry" if attempt else "ok")
    return None, retries + 1, "refused"


def _bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = (min_lat + max_lat) / 2.0
    h = (max_lat - min_lat) * METERS_PER_DEG_LAT
    w = (max_lon - min_lon) * METERS_PER_DEG_LAT * math.cos(math.radians(mid_lat))
    return w * h / 1e6


# ── Catalog access, record, CLI ────────────────────────────────────────────


def load_cities(city_ids: list[str]) -> tuple[list[dict], list[str]]:
    """Frozen geometry for the named cities. Returns (found, missing)."""
    db = os.path.join(paths.get_project_root(), "data", "streetscape_tracker.db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    found, missing = [], []
    for city_id in city_ids:
        row = conn.execute(
            "SELECT city_id, center_lat, center_lon, grid_width_m, grid_height_m, step_m "
            "FROM cities WHERE city_id = ?",
            (city_id,),
        ).fetchone()
        (found.append(dict(row)) if row else missing.append(city_id))
    conn.close()
    return found, missing


def docs_generated_by(args: argparse.Namespace) -> str:
    """The command that produced the record, spelled from the real arguments."""
    parts = ["scripts/kartaview_sweep_cost.py"]
    if args.city:
        for c in args.city:
            parts += ["--city", c]
    else:
        parts += ["--sample", str(args.sample)]
    if args.ipp != IPP_MAX:
        parts += ["--ipp", str(args.ipp)]
    if args.start_radius_m != DEFAULT_START_RADIUS_M:
        parts += ["--start-radius-m", str(args.start_radius_m)]
    if args.max_requests_per_city != DEFAULT_MAX_REQUESTS_PER_CITY:
        parts += ["--max-requests-per-city", str(args.max_requests_per_city)]
    parts += ["--docs-dir", str(args.docs_dir)]
    return " ".join(parts)


# 60 probes ~40-60 root cells, which is a usable sample of any bbox while
# keeping a 14-city run inside one paced hour. Small cities finish complete.
DEFAULT_MAX_REQUESTS_PER_CITY = 60


def summarize(cities: list[dict]) -> dict:
    """Distribution over the study set. CLAUDE.md: quote the shape, not a headline."""
    reachable = [c for c in cities if c.get("sweep_requests_estimate") is not None]
    if not reachable:
        return {"n": 0, "unreachable": len(cities)}
    est = sorted(c["sweep_requests_estimate"] for c in reachable)

    def pct(q: float) -> int:
        return est[min(len(est) - 1, int(q * (len(est) - 1)))]

    visited = sum(c["cells_visited"] for c in cities)
    radii = sorted(c["calibrated_radius_m"] for c in reachable)
    return {
        "n": len(est),
        "sweep_requests_estimate": {
            "min": est[0],
            "p50": pct(0.5),
            "p90": pct(0.9),
            "max": est[-1],
            "mean": int(round(sum(est) / len(est))),
        },
        "plans_complete": sum(1 for c in cities if c["plan_complete"]),
        "plans_truncated": sum(1 for c in reachable if not c["plan_complete"]),
        "unreachable": len(cities) - len(reachable),
        # The finding this study turns on: the working radius is a property
        # of the LOCATION, and it is what sets the cost.
        "calibrated_radius_m": {"min": radii[0], "max": radii[-1], "p50": radii[len(radii) // 2]},
        "refusal_rate_over_cells_visited": (
            round(sum(c["refusals"] for c in cities) / visited, 4) if visited else None
        ),
        "retries_cleared": sum(c["retries_cleared"] for c in cities),
        "retries_attempted": sum(c["retries_attempted"] for c in cities),
        "floor_failures": sum(c["floor_failures"] for c in cities),
        "broken_cells": sum(c.get("broken_cells", 0) for c in cities),
        "requests_spent_measuring": sum(c["requests_spent_planning"] for c in cities),
    }


def write_docs_record(cities, missing, args, authed) -> str:
    """Write the committed metrics record beside the writeup. Sole producer."""
    os.makedirs(args.docs_dir, exist_ok=True)
    path = os.path.join(args.docs_dir, DOCS_METRICS_NAME)
    payload = {
        "_about": {
            "experiment": "kartaview-sweep-cost",
            "writeup": "docs/experiments/kartaview-sweep-cost.md",
            "generated_by": docs_generated_by(args),
            "issue": 225,
            "probed_at_utc": datetime.now(UTC).isoformat(),
            "authenticated": authed,
            "rate_limit_used_per_hour": (
                REQUESTS_PER_HOUR_AUTH if authed else REQUESTS_PER_HOUR_ANON
            ),
            "ipp": args.ipp,
            "start_radius_m": args.start_radius_m,
            "max_requests_per_city": args.max_requests_per_city,
            "seed": args.seed,
            "cities_not_registered": list(missing),
            "note": DOCS_RECORD_NOTE,
        },
        "summary": summarize(cities),
        "cities": cities,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")
    return path


DOCS_RECORD_NOTE = (
    "Sweep cost for a KartaView census over each city's FROZEN GRID bbox. A sweep's "
    "requests are cells_visited + sum over leaves of (pages - 1); page 1 of every cell "
    "reports totalFilteredItems, so planning the sweep pays its first half and prices "
    "the second exactly. This run issued page 1 ONLY -- it never fetched pages 2+, so "
    "sweep_requests_* are measured for the cell half and computed for the page half. "
    "Cells are squares covered by their circumscribed circle, so the sweep sees each "
    "photo ~pi/2 times; photos_seen_sum_over_cells is what a sweep fetches and "
    "photos_in_bbox_estimate divides that out. Where plan_complete is false, "
    "max_requests_per_city stopped the walk and every *_estimate is scaled by "
    "root_cells / roots_probed -- roots are walked in a SEEDED SHUFFLE so a truncated "
    "plan is a uniform sample of the bbox rather than its northern strip. A refusal "
    "(HTTP 400, apiCode 690/408) is BACKPRESSURE: it is retried once and only then "
    "subdivided, because apiCode 690 was measured to be flaky rather than a function "
    "of (radius, ipp)."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KartaView sweep-cost measurement (issue #225).")
    p.add_argument("--city", action="append", help="city_id to plan (repeatable)")
    p.add_argument(
        "--sample",
        default="default",
        help=f"named study set ({', '.join(SAMPLES)})",
    )
    p.add_argument("--cities", action="store_true", help="list the default study set and exit")
    p.add_argument("--ipp", type=int, default=IPP_MAX, help=f"items per page (cap {IPP_MAX})")
    p.add_argument(
        "--start-radius-m",
        type=int,
        default=DEFAULT_START_RADIUS_M,
        help="radius of the top-level cells; the walk only ever shrinks from here",
    )
    p.add_argument(
        "--max-requests-per-city",
        type=int,
        default=DEFAULT_MAX_REQUESTS_PER_CITY,
        help="bound on the paced walk; when it bites, plan_complete is false and "
        "the estimates are scaled from the roots actually probed",
    )
    p.add_argument("--seed", type=int, default=225, help="root-cell shuffle seed")
    p.add_argument("--docs-dir", default="docs/experiments")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(message)s")

    if args.cities:
        for c in SAMPLES[args.sample]:
            print(c)
        return 0

    refuse_on_collection_host()

    if args.sample not in SAMPLES:
        logger.error(f"unknown sample {args.sample!r}; known: {', '.join(SAMPLES)}")
        return 64
    city_ids = args.city or list(SAMPLES[args.sample])
    cities, missing = load_cities(city_ids)
    if missing:
        logger.warning(f"not registered, skipped and named in the record: {', '.join(missing)}")
    if not cities:
        logger.error("no registered cities to plan")
        return 64

    load_dotenv()
    cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    token = os.environ.get("KARTAVIEW_ACCESS_TOKEN")
    rph = REQUESTS_PER_HOUR_AUTH if token else REQUESTS_PER_HOUR_ANON
    logger.info(
        f"planning {len(cities)} cities at {rph} req/h "
        f"({'authenticated' if token else 'anonymous'}), "
        f"<= {args.max_requests_per_city} requests each"
    )

    limiter = HourlyRateLimiter(rph)
    session = requests.Session()
    session.headers["User-Agent"] = (
        "streetscape-tracker sweep-cost (github.com/jonfroehlich/streetscape-tracker)"
    )

    results = []
    for city in cities:
        logger.info(f"--- {city['city_id']}")
        results.append(
            plan_city(
                session,
                limiter,
                city,
                ipp=args.ipp,
                start_radius_m=args.start_radius_m,
                max_requests=args.max_requests_per_city,
                access_token=token,
                seed=args.seed,
            )
        )
        r = results[-1]
        logger.info(
            f"{r['city_id']}: {r['bbox_area_km2']} km2, {r['roots_probed']}/{r['root_cells']} "
            f"roots, spent {r['requests_spent_planning']} -> sweep ~"
            f"{r['sweep_requests_estimate']} requests"
            f"{'' if r['plan_complete'] else ' (EXTRAPOLATED)'}"
        )

    # Write BEFORE printing: a formatting error in the table must not discard a
    # multi-hour paced run.
    path = write_docs_record(results, missing, args, bool(token))
    logger.info(f"wrote {path}")

    print(f"\n{'city':<44} {'km2':>8} {'roots':>12} {'spent':>7} {'sweep req':>10}")
    for r in results:
        flag = "" if r["plan_complete"] else "  <- extrapolated"
        print(
            f"{r['city_id']:<44} {r['bbox_area_km2']:>8.1f} "
            f"{r['roots_probed']:>5}/{r['root_cells']:<6} "
            f"{r['requests_spent_planning']:>7} {r['sweep_requests_estimate']:>10}{flag}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
