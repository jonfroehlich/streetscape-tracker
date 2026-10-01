"""
Issue #406: screen Panoramax 360° imagery OUTSIDE the catalog, region by region.

    python scripts/panoramax_world_screen_collect.py              # dry run: print the tile plan
    python scripts/panoramax_world_screen_collect.py --execute    # 235 requests, ~10 min

This is the standing #316 screen (`panoramax_screen.hexes_from_tile`, the v2
`grid` layer at z6) pointed at whole regions instead of at the catalog's frozen
grids. Its question is "where outside the catalog is there 360° imagery worth
registering a city for?"; its answer is a list of res-6 H3 hexagons, which
`panoramax_world_screen_analyze.py` maps onto GeoNames places offline.

PROVENANCE, stated so nobody mistakes this for the code that ran. The
2026-10-01 pass was collected by a research script kept beside its raw output
(`experiments/candidate-360-cities-2026-10-01/panoramax/screen_regions.py`,
gitignored with it). This file is that script made reproducible: the same
regions, zoom, request order, cap, pacing shape and output schema, but through
the study tooling the repo already has (`panoramax_feasibility.Fetcher`, the
#292 jittered pacer, the collection-host refusal). The tile plan is pinned by
`tests/test_panoramax_world_screen.py` against the 235 tiles the 2026-10-01
request log records. Three differences: the User-Agent is the study's rather
than the research one; `Fetcher` retries a 5xx up to three times where the
research script never retried (none occurred, so the record is unaffected); and
the shared pacer spaces request STARTS, where the research script slept after
each response, so a re-run approaches the 30/min mean rather than the 22.4/min
the 2026-10-01 log shows.

PACING. 30/min mean, CV 0.6, sequential, hard cap 250 requests, stop on the
first 403/429. That is the collector's own Panoramax rate, and the reasons it is
the conservative end are in `docs/provider-access.md`. Never run it on a
production collection host: the screen shares the per-IP Panoramax host with the
nightly channels.

EMPTY TILES. At z6 the meta-catalog answered an empty tile with **204 No
Content**, not 404 (34 of 235 on 2026-10-01, and 0 × 404) — the opposite of
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
DEFAULT_RPM = 30
DEFAULT_JITTER = 0.6
DEFAULT_RAW_DIR = os.path.join("experiments", "candidate-360-cities-2026-10-01", "panoramax")
HEX_FIELDS = ["hex_id", "lon", "lat", "nb_pictures", "nb_360_pictures", "nb_flat_pictures", "date"]


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


def collect(raw_dir: str, rpm: int, jitter: float) -> dict[str, int]:
    """Fetch the plan sequentially, appending to requests.log and writing hexes.csv."""
    import panoramax_feasibility as pf  # deferred: imports requests/protobuf

    plan = plan_tiles()
    if len(plan) > MAX_REQUESTS:
        raise SystemExit(f"plan is {len(plan)} tiles, over the {MAX_REQUESTS}-request cap")
    os.makedirs(raw_dir, exist_ok=True)
    fetcher = pf.Fetcher(pf.SpacedRateLimiter(rpm, jitter=jitter), timeout_s=60)
    merged: dict[str, dict[str, Any]] = {}
    counts = {"200": 0, "204": 0, "404": 0, "other": 0}
    with open(os.path.join(raw_dir, "requests.log"), "a") as log:
        for i, (x, y) in enumerate(sorted(plan)):
            url = SCREEN_URL_TEMPLATE.format(z=SCREEN_ZOOM, x=x, y=y)
            started = time.time()
            try:
                response = fetcher.get(url)
            except pf.BlockedError as exc:
                print(f"STOP: {exc}")
                break
            status = response.status_code
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower().startswith(
                    (
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
                )
            }
            record = {
                "i": i,
                "x": x,
                "y": y,
                "region": plan[(x, y)],
                "status": status,
                "bytes": len(response.content),
                "s": round(time.time() - started, 2),
                "hdrs": headers,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            log.write(json.dumps(record) + "\n")
            log.flush()
            key = str(status) if str(status) in counts else "other"
            counts[key] += 1
            if status == 200 and response.content:
                merge_hexes(merged, hexes_from_tile(response.content, x, y, SCREEN_ZOOM))
    with open(os.path.join(raw_dir, "hexes.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEX_FIELDS)
        writer.writerows(hex_rows(merged))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--execute", action="store_true", help="send the requests (default: dry run)"
    )
    parser.add_argument("--raw-dir", default=DEFAULT_RAW_DIR, help="gitignored output directory")
    parser.add_argument("--rpm", type=int, default=DEFAULT_RPM)
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER)
    args = parser.parse_args(argv)

    plan = plan_tiles()
    print(f"plan: {len(plan)} tiles {region_counts(plan)}")
    if not args.execute:
        return 0
    refuse_on_collection_host()
    print("done", collect(args.raw_dir, args.rpm, args.jitter))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
