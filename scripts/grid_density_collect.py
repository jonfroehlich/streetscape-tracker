"""
Issue #106 experiment collection: one aligned 5 m GSV metadata sweep per study area.

    python scripts/grid_density_collect.py --estimate --area all     # no key needed
    python scripts/grid_density_collect.py --area adrian             # ~26k queries
    python scripts/grid_density_collect.py --area all                # ~348k queries

Queries ONLY the 5 m lattice (the 20/10 m and road-clipped variants are derived
offline by grid_density_analyze.py), on the isolated GMAPS_STREETS_API_KEY /
``gsv_streets`` ledger so the experiment can't touch the production grid quota.
Plumbing mirrors streetscape_street_analyzer/collect.py; the request engine is
the grid downloader's hardened ``collect_points_async`` (rate limiting,
OVER_QUERY_LIMIT retries, ``.downloading`` resume, gzip finalize).

Snapshots land in experiments/grid-density/ (gitignored, NEVER under data/ — the rsync
publisher globs any *.csv.gz there). An existing final snapshot is skipped
(immutable; delete manually to re-collect); an interrupted one resumes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date

from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopy  # noqa: E402
from grid_density_common import (  # noqa: E402
    CLIP_DIST_M_DEFAULT,
    DEFAULT_OUT_DIR,
    FINE_STEP_M,
    STUDY_AREAS,
    fine_index_ranges,
    generate_lattice,
    index_ranges_20m,
    lattice_frame,
    road_clip_mask,
    snapshot_csv_path,
    variant_masks,
)

from streetscape_metadata_tracker import config as cfg  # noqa: E402
from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.analysis import (  # noqa: E402
    calculate_coverage_stats,
    detect_systemic_failure,
)
from streetscape_metadata_tracker.config import load_config  # noqa: E402
from streetscape_metadata_tracker.download_common import DownloadError  # noqa: E402
from streetscape_metadata_tracker.download_gsv import collect_points_async  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_street_analyzer.download_street_network import fetch_street_edges  # noqa: E402

logger = logging.getLogger(__name__)

BUDGET_CHANNEL = "gsv_streets"
DEFAULT_MAX_REQUESTS_PER_MINUTE = 24_000


def collect_area(args: argparse.Namespace, conn, area_key: str) -> int:
    area = STUDY_AREAS[area_key]
    city = db.resolve_city(conn, area.city_id)
    if city is None:
        logger.error("City not found in catalog: %s", area.city_id)
        return 1

    i20, j20 = index_ranges_20m(city, area)
    i5, j5 = fine_index_ranges(i20, j20, substep=round(city.step_m / FINE_STEP_M))
    origin = geopy.Point(city.center_lat, city.center_lon)
    points = generate_lattice(origin, i5, j5, FINE_STEP_M)
    n20 = len(i20) * len(j20)
    n10 = ((len(i5) + 1) // 2) * ((len(j5) + 1) // 2)

    if args.estimate:
        # Dry run: no key, no API calls. Road-clipped count needs only the
        # cached GraphML (zero network for all three experiment cities).
        lattice = lattice_frame(points)
        edges = fetch_street_edges(city, args.data_dir, conn=None)
        n_road = int(road_clip_mask(lattice, edges, args.clip_dist).sum())
        print(
            f"{area.key} ({city.city_id}): step20={n20} step10={n10} "
            f"step5={len(points)} road-clipped-5m={n_road} "
            f"(queries issued = step5 only). No requests issued (--estimate)."
        )
        return 0

    out_csv = snapshot_csv_path(args.out_dir, area)
    if os.path.exists(out_csv):
        logger.info("Experiment snapshot already collected, skipping: %s", out_csv)
        return 0

    # Pre-flight budget guard against the isolated gsv_streets ledger.
    today = date.today()
    if args.daily_budget is not None:
        already = db.get_api_usage(conn, today, provider=BUDGET_CHANNEL)
        if already + len(points) > args.daily_budget:
            logger.error(
                "gsv_streets daily budget %d would be exceeded: %d spent + %d planned",
                args.daily_budget,
                already,
                len(points),
            )
            return 1

    load_dotenv()
    cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    config = load_config(BUDGET_CHANNEL)

    try:
        results = asyncio.run(
            collect_points_async(
                points,
                config["api_key"],
                out_csv,
                city_label=f"{city.display_name} [grid-density {area.key}]",
                batch_size=args.batch_size,
                connection_limit=args.connection_limit,
                request_timeout=args.timeout,
                max_retries=args.max_retries,
                max_requests_per_minute=args.max_requests_per_minute,
            )
        )
    except DownloadError as e:
        # A failed crawl still spent real requests; keep the ledger honest.
        spent = getattr(e, "api_requests", 0)
        if spent:
            db.add_api_usage(conn, today, spent, provider=BUDGET_CHANNEL)
            logger.warning("Recorded %d gsv_streets requests spent by the failed crawl", spent)
        logger.error("Collection failed (checkpoint kept; rerun resumes): %s", e)
        return 1

    df = results["df"]
    db.add_api_usage(conn, today, results["api_requests"], provider=BUDGET_CHANNEL)

    failure_reason = detect_systemic_failure(df)
    if failure_reason:
        rejected = f"{out_csv}.rejected"
        os.replace(out_csv, rejected)
        logger.error(
            "Experiment snapshot rejected: %s. Raw responses kept at %s", failure_reason, rejected
        )
        return 1

    stats = calculate_coverage_stats(df)
    masks = variant_masks(lattice_frame(points)["i"], lattice_frame(points)["j"])
    print(
        f"{area.key}: {len(df)} points collected ({results['api_requests']} requests "
        f"this session), 5 m coverage {stats.coverage_rate:.1f}% -> {out_csv} "
        f"[derived variants: step20={int(masks['step20'].sum())}, "
        f"step10={int(masks['step10'].sum())}]"
    )
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue #106 grid-density experiment collection.")
    parser.add_argument(
        "--area",
        default="all",
        choices=[*STUDY_AREAS, "all"],
        help="Study area (default: all, cheapest first)",
    )
    parser.add_argument("--estimate", action="store_true", help="Report counts and exit (no key)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument("--clip-dist", type=float, default=CLIP_DIST_M_DEFAULT)
    parser.add_argument("--daily-budget", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--connection-limit", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--max-requests-per-minute", type=int, default=DEFAULT_MAX_REQUESTS_PER_MINUTE
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db_path = db.get_default_db_path(args.data_dir)
    if not os.path.exists(db_path):
        logger.error("Catalog DB not found at %s", db_path)
        return 1

    # Cheapest first, so a key/quota problem surfaces after ~26k queries.
    area_keys = list(STUDY_AREAS) if args.area == "all" else [args.area]
    conn = db.connect(db_path)
    try:
        for key in area_keys:
            rc = collect_area(args, conn, key)
            if rc != 0:
                return rc
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
