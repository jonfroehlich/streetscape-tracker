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
    REQUESTS_PER_HOUR_ANON,
    REQUESTS_PER_HOUR_AUTH,
    HourlyRateLimiter,
    ProbeError,
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

# Fact 2: a refusal is retried before it is believed.
BACKPRESSURE_RETRIES = 1

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
) -> dict:
    """
    Walk one city's bbox adaptively, spending one page-1 request per cell.

    Root cells are visited in a seeded shuffle so that a run stopped by
    ``max_requests`` has sampled the bbox uniformly rather than its northern
    strip. Refusals are retried before a cell is subdivided (docstring fact 2),
    and a cell that still refuses at ``RADIUS_FLOOR_M`` is recorded as a failure
    rather than silently costing nothing.
    """
    bbox = grid_bbox(
        city["center_lat"],
        city["center_lon"],
        city["grid_width_m"],
        city["grid_height_m"],
        city["step_m"],
    )
    cell_size = start_radius_m * math.sqrt(2)
    roots = cells_for_bbox(*bbox, cell_size)
    order = list(range(len(roots)))
    random.Random(seed).shuffle(order)

    spent = 0
    cells_visited = 0
    leaf_totals: list[int | None] = []
    refusals = subdivisions = retries_attempted = retries_cleared = floor_failures = 0
    roots_probed = 0

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
                session, limiter, cell, ipp=ipp, access_token=access_token
            )
            spent += cost
            cells_visited += 1
            retries_attempted += cost - 1
            if outcome == "ok_after_retry":
                retries_cleared += 1
            if outcome == "refused":
                refusals += 1
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
) -> tuple[int | None, int, str]:
    """
    Page 1 of one cell. Returns ``(total, requests_spent, outcome)``.

    ``outcome`` is one of ``ok`` / ``ok_after_retry`` / ``refused``. The retry is
    the whole reason this is a function: a 690 that clears on a second ask would
    otherwise subdivide a cell into four that did not need subdividing, which
    inflates every number this script exists to measure.
    """
    for attempt in range(BACKPRESSURE_RETRIES + 1):
        try:
            items, total = _post_nearby(
                session,
                limiter,
                cell.lat,
                cell.lon,
                cell.radius_m,
                page=1,
                ipp=ipp,
                access_token=access_token,
            )
        except ProbeError as e:
            logger.debug(f"r={cell.radius_m}m @ {cell.lat:.4f},{cell.lon:.4f}: {e}")
            continue
        return total, attempt + 1, ("ok_after_retry" if attempt else "ok")
    return None, BACKPRESSURE_RETRIES + 1, "refused"


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
    if not cities:
        return {"n": 0}
    est = sorted(c["sweep_requests_estimate"] for c in cities)

    def pct(q: float) -> int:
        return est[min(len(est) - 1, int(q * (len(est) - 1)))]

    visited = sum(c["cells_visited"] for c in cities)
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
        "plans_truncated": sum(1 for c in cities if not c["plan_complete"]),
        "refusal_rate_over_cells_visited": (
            round(sum(c["refusals"] for c in cities) / visited, 4) if visited else None
        ),
        "retries_cleared": sum(c["retries_cleared"] for c in cities),
        "retries_attempted": sum(c["retries_attempted"] for c in cities),
        "floor_failures": sum(c["floor_failures"] for c in cities),
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
