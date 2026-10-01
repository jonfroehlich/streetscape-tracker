"""
Issue #406: screen Panoramax 360° imagery OUTSIDE the catalog, region by region.

    python scripts/panoramax_world_screen_collect.py              # dry run: print the tile plan
    python scripts/panoramax_world_screen_collect.py --execute    # 235 requests, ~10 min

This is the standing #316 screen (`panoramax_screen.hexes_from_tile`, the v2
`grid` layer at z6) pointed at whole regions instead of at the catalog's frozen
grids. Its question is "where outside the catalog is there 360° imagery worth
registering a city for?"; its answer is a list of H3 hexagons (measured RES 7 on
2026-10-01, not the res 6 earlier docs state), which
`panoramax_world_screen_analyze.py` maps onto GeoNames places offline.

PROVENANCE, stated so nobody mistakes this for the code that ran. The
2026-10-01 pass was collected by a research script kept beside its raw output
(`experiments/candidate-360-cities-2026-10-01/panoramax/screen_regions.py`,
gitignored with it). This file is that script made reproducible: the same
regions, zoom, request order, request cap and mean rate, and the same
`hexes.csv` / `requests.log` schema, with `s` measured the same way (the
request's own latency, from send to body read, never the pacer's sleep). The
tile plan is pinned by `tests/test_panoramax_world_screen.py` against the 235
tiles the 2026-10-01 log records. Differences, none of which touches the record:
the User-Agent is the study's; a 5xx or transport error is retried (the research
script never retried, and none occurred), with every attempt counted against the
cap and logged as `attempts`; and the pacer spaces request STARTS, where the
research script slept after each response, so a re-run approaches the 30/min
mean rather than the 22.4/min the 2026-10-01 log shows.

OUTPUT. Each run writes `requests.log` and `hexes.csv` into `<raw-dir>/panoramax/`,
the layout `panoramax_world_screen_analyze.py --raw-dir <raw-dir>` reads.
`<raw-dir>` defaults to `experiments/panoramax-world-screen-<UTC date>/` under
the repo root (not the cwd), and a `panoramax/` directory that already holds
anything is refused, dry run included -- an earlier version defaulted to the
2026-10-01 evidence directory, where `--execute` would have appended to its
only request log and overwritten its only hexes.csv.

PACING AND THE CAP. 30/min mean, CV 0.6 (#292), sequential, from a laptop
(`refuse_on_collection_host`). `MAX_REQUESTS` caps ATTEMPTS, retries included,
and is enforced before every send. A 403/429 stops the pass at once. A tile that
exhausts its retries, or the cap being reached, also stops it; in every stopped
case the log's last line is a `stop` record and the run exits nonzero, so the
analyzer (which refuses any record whose tiles are not exactly the plan) can
never mistake a partial screen for a complete one.

EMPTY TILES. At z6 the meta-catalog answered an empty tile with **204 No
Content**, not 404 (34 of 235 on 2026-10-01, and 0 × 404) -- the opposite of
what `download_panoramax._fetch_tile` documents for z15. Both are recorded as
empty here, and the log keeps the status so the two stay distinguishable.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kartaview_probe import refuse_on_collection_host  # noqa: E402

from streetscape_metadata_tracker.download_common import tiles_for_bbox  # noqa: E402
from streetscape_metadata_tracker.panoramax_screen import (  # noqa: E402
    SCREEN_URL_TEMPLATE,
    SCREEN_ZOOM,
    hexes_from_tile,
    merge_hexes,
)

# Screened regions as (min_lon, min_lat, max_lon, max_lat). Order matters: a
# tile two regions share is attributed to the FIRST, which is how the
# 2026-10-01 log labels it. New Zealand was dropped to stay under the cap.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "conus_scanada": (-125.5, 24.5, -66.0, 50.5),
    "europe": (-10.5, 35.5, 30.5, 60.5),
    "turkey": (26.0, 36.0, 45.0, 42.0),
    "taiwan": (119.5, 21.5, 122.5, 25.5),
    "japan": (129.5, 31.0, 142.5, 43.5),
    "se_australia": (138.0, -39.5, 154.0, -27.0),
    "s_america_cone": (-71.0, -41.0, -43.0, -20.0),
    "mexico": (-117.5, 14.0, -86.0, 32.5),
}

MAX_REQUESTS = 250
MAX_TRIES_PER_TILE = 3
DEFAULT_RPM = 30
DEFAULT_JITTER = 0.6
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEX_FIELDS = ["hex_id", "lon", "lat", "nb_pictures", "nb_360_pictures", "nb_flat_pictures", "date"]
LOGGED_HEADER_PREFIXES = (
    "x-rate",
    "ratelimit",
    "retry",
    "server",
    "cache",
    "x-cache",
    "cf-",
    "via",
    "age",
)


def default_raw_dir(today: str | None = None) -> str:
    """`<repo>/experiments/panoramax-world-screen-<UTC date>`: never an existing record."""
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    return os.path.join(REPO, "experiments", f"panoramax-world-screen-{today}")


def tile_dir(raw_dir: str) -> str:
    """Where a pass writes: `<raw_dir>/panoramax`, the analyzer's input layout."""
    return os.path.join(raw_dir, "panoramax")


def refuse_nonempty_dir(raw_dir: str) -> None:
    """Exit with an error if the pass's output dir already holds anything.

    A run never touches a previous record.
    """
    out = tile_dir(raw_dir)
    if os.path.isdir(out) and os.listdir(out):
        raise SystemExit(
            f"Refusing to write into {out!r}: it already holds files, and a run "
            f"appends to requests.log and rewrites hexes.csv. Pass a fresh --raw-dir."
        )


def plan_tiles(
    regions: dict[str, tuple[float, float, float, float]] = REGIONS, zoom: int = SCREEN_ZOOM
) -> dict[tuple[int, int], str]:
    """Every z`zoom` tile touching any region, mapped to the first region that claims it.

    Example::

        >>> plan = plan_tiles()
        >>> len(plan)
        235
    """
    tiles: dict[tuple[int, int], str] = {}
    for name, bbox in regions.items():
        for tile in tiles_for_bbox(*bbox, zoom):
            tiles.setdefault(tile, name)
    return tiles


def region_counts(plan: dict[tuple[int, int], str]) -> dict[str, int]:
    """Tiles per region, in `REGIONS` order (the shape the run printed)."""
    counts: dict[str, int] = {}
    for region in plan.values():
        counts[region] = counts.get(region, 0) + 1
    return counts


def hex_rows(merged: dict[str, dict[str, Any]]) -> list[list[Any]]:
    """One CSV row per merged hexagon: its bbox centre, the three counters and `date`."""
    rows = []
    for hex_id, h in merged.items():
        lon = (h["min_lon"] + h["max_lon"]) / 2
        if lon > 180:
            lon -= 360
        lat = (h["min_lat"] + h["max_lat"]) / 2
        rows.append(
            [
                hex_id,
                round(lon, 4),
                round(lat, 4),
                h["nb_pictures"],
                h["nb_360_pictures"],
                h["nb_flat_pictures"],
                h["date"],
            ]
        )
    return rows


class Stop(Exception):
    """End the pass; `reason` goes into the log's final `stop` record."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def run_pass(
    plan: dict[tuple[int, int], str],
    get: Callable[[str], Any],
    acquire: Callable[[], None],
    log,
    *,
    max_requests: int = MAX_REQUESTS,
    max_tries: int = MAX_TRIES_PER_TILE,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch every planned tile in sorted order; return (merged hexes, summary).

    `get(url)` returns a response with `status_code`, `content` and `headers`,
    or raises on a transport error; `acquire()` blocks for the pacer. Both are
    injected so the cap and the stop paths are testable without a network.
    `s` is timed around `get` alone, so it is the request's latency and never
    the pacer's sleep.
    """
    merged: dict[str, dict[str, Any]] = {}
    sent = 0
    summary: dict[str, Any] = {"complete": False, "requests": 0, "tiles": 0}
    try:
        if len(plan) > max_requests:
            raise Stop(f"plan is {len(plan)} tiles, over the {max_requests}-request cap")
        for i, (x, y) in enumerate(sorted(plan)):
            url = SCREEN_URL_TEMPLATE.format(z=SCREEN_ZOOM, x=x, y=y)
            response, latency, attempts, error = None, 0.0, 0, None
            while attempts < max_tries:
                if sent >= max_requests:
                    raise Stop(f"request cap {max_requests} reached at tile {i} ({x},{y})")
                acquire()
                sent += 1
                attempts += 1
                started = clock()
                try:
                    response = get(url)
                    error = None
                except Exception as exc:  # transport error: retry within the tile's budget
                    response, error = None, repr(exc)
                latency = clock() - started
                if response is not None and response.status_code < 500:
                    break
            status = response.status_code if response is not None else "EXC"
            record = {
                "i": i,
                "x": x,
                "y": y,
                "region": plan[(x, y)],
                "status": status,
                "bytes": len(response.content) if response is not None else 0,
                "s": round(latency, 2),
                "attempts": attempts,
                "hdrs": {
                    k: v
                    for k, v in (response.headers.items() if response is not None else [])
                    if k.lower().startswith(LOGGED_HEADER_PREFIXES)
                },
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if error:
                record["error"] = error
            log.write(json.dumps(record) + "\n")
            log.flush()
            summary["tiles"] = i + 1
            if status in (403, 429):
                raise Stop(f"HTTP {status}: per-IP refusal; do not retry into it")
            if status == "EXC" or (isinstance(status, int) and status >= 500):
                raise Stop(f"tile ({x},{y}) failed after {attempts} attempts: {error or status}")
            if status == 200 and response.content:
                merge_hexes(merged, hexes_from_tile(response.content, x, y, SCREEN_ZOOM))
        summary["complete"] = True
    except Stop as stop:
        log.write(json.dumps({"stop": stop.reason, "requests": sent}) + "\n")
        log.flush()
        summary["stop"] = stop.reason
    summary["requests"] = sent
    return merged, summary


def collect(raw_dir: str, rpm: int, jitter: float) -> dict[str, Any]:
    """One paced pass into a fresh `<raw_dir>/panoramax/`: requests.log, then hexes.csv."""
    import panoramax_feasibility as pf  # deferred: imports requests/protobuf
    import requests

    refuse_nonempty_dir(raw_dir)
    out = tile_dir(raw_dir)
    os.makedirs(out, exist_ok=True)
    limiter = pf.SpacedRateLimiter(rpm, jitter=jitter)
    session = requests.Session()
    session.headers["User-Agent"] = (
        "streetscape-tracker/panoramax-world-screen (+https://github.com/jonfroehlich/streetscape-tracker)"
    )
    with open(os.path.join(out, "requests.log"), "a") as log:
        merged, summary = run_pass(
            plan_tiles(), lambda url: session.get(url, timeout=60), limiter.acquire, log
        )
    with open(os.path.join(out, "hexes.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEX_FIELDS)
        writer.writerows(hex_rows(merged))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--execute", action="store_true", help="send the requests (default: dry run)"
    )
    parser.add_argument(
        "--raw-dir",
        default=None,
        help="fresh, gitignored output directory (default: experiments/panoramax-world-screen-<UTC date>)",
    )
    parser.add_argument("--rpm", type=int, default=DEFAULT_RPM)
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER)
    args = parser.parse_args(argv)
    raw_dir = args.raw_dir or default_raw_dir()

    plan = plan_tiles()
    print(f"plan: {len(plan)} tiles {region_counts(plan)} -> {raw_dir}")
    refuse_nonempty_dir(raw_dir)
    if not args.execute:
        return 0
    refuse_on_collection_host()
    summary = collect(raw_dir, args.rpm, args.jitter)
    print("done", summary)
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
