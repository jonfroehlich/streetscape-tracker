"""
CLI: road-walk street-coverage collection for a city (issue #99).

    python -m streetscape_street_analyzer.collect "Seattle, WA" \
        [--spacing 15] [--match-dist 25] [--network-type drive] \
        [--run-date YYYY-MM-DD] [--force] [--refresh] \
        [--connection-limit N] [--max-requests-per-minute R] \
        [--daily-budget N] [--estimate] [--data-dir DIR]

A SECOND collection modality alongside the grid downloader. It walks the city's
frozen OSM network (issue #103), samples on-street points every ``--spacing``
metres along each edge, and queries GSV for the nearest pano at each point,
yielding **fractional** per-edge coverage. Unlike the grid downloader it queries
only on-street points, and its association to streets is by construction.

The provider is GSV, but the request budget is isolated in its own key/channel
(``GMAPS_STREETS_API_KEY`` / the ``gsv_streets`` ``api_usage`` ledger, issue
#141) so a road-crawl can't exhaust the production grid collector's quota. It
reuses the exact rate-limiter + OVER_QUERY_LIMIT retry machinery of the grid
downloader via ``download_gsv.collect_points_async``.

Two dated artifacts are written next to the run (both published as ``*.gz``):
a raw sample snapshot ``..._streetwalk_sp{N}_{DATE}.csv.gz`` (METADATA schema,
one row per sampled location) and the derived per-edge coverage GeoJSON
``..._streetwalk_sp{N}_{DATE}_coverage.json.gz``.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import os
import sys
from datetime import UTC, date, datetime

from dotenv import find_dotenv, load_dotenv

from streetscape_metadata_tracker import config as cfg
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.analysis import detect_systemic_failure
from streetscape_metadata_tracker.config import load_config
from streetscape_metadata_tracker.download_common import DownloadError
from streetscape_metadata_tracker.download_gsv import collect_points_async
from streetscape_metadata_tracker.json_summarizer import generate_streetwalk_manifest
from streetscape_metadata_tracker.naming import (
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_metadata_tracker.paths import get_default_data_dir

from .download_street_network import fetch_street_edges
from .road_sampling import dedupe_query_points, generate_samples
from .street_coverage import (
    DEFAULT_MATCH_DIST_M,
    build_streetwalk_geojson,
    compute_streetwalk_coverage,
)

logger = logging.getLogger(__name__)

# Provider label stored in street_walks/for the coverage filter (imagery
# provider). Budget is metered under the separate 'gsv_streets' ledger channel.
PROVIDER = "gsv"
BUDGET_CHANNEL = "gsv_streets"
DEFAULT_SPACING_M = 15

# Defaults mirror config/scheduler.toml's [providers.gsv_streets] / [download]
# so a manual run paces like the (future) scheduled one. The gsv_streets key
# has its own ~30k/min GSV metadata quota; 24000 is ~80% client-side headroom.
DEFAULT_MAX_REQUESTS_PER_MINUTE = 24_000


def run_collect(args: argparse.Namespace) -> int:
    data_dir = args.data_dir
    db_path = db.get_default_db_path(data_dir)
    if not os.path.exists(db_path):
        logger.error("Catalog DB not found at %s", db_path)
        return 1

    conn = db.connect(db_path)
    try:
        city = db.resolve_city(conn, args.city)
        if city is None:
            logger.error("City not found in catalog: %s", args.city)
            return 1

        run_date = date.fromisoformat(args.run_date) if args.run_date else date.today()

        # Frozen network → edges (registers the #103 network row via conn).
        edges = fetch_street_edges(
            city, data_dir, refresh=args.refresh, network_type=args.network_type, conn=conn
        )
        samples = generate_samples(edges, args.spacing)
        query_points = dedupe_query_points(samples)
        logger.info(
            "%s: %d edges → %d samples → %d unique GSV queries (spacing %.1fm)",
            city.city_id,
            len(edges),
            len(samples),
            len(query_points),
            args.spacing,
        )

        if args.estimate:
            # Dry run: no key, no API calls — just report the work + cost.
            print(
                f"{city.city_id} [{args.network_type}]: {len(edges)} edges, "
                f"{len(samples)} samples, {len(query_points)} unique GSV queries "
                f"(spacing={args.spacing}m). No requests issued (--estimate)."
            )
            return 0

        if len(query_points) == 0:
            logger.error("No on-street sample points generated; nothing to collect.")
            return 1

        stem = generate_streetwalk_filename(
            city.city_id,
            city.grid_width_m,
            city.grid_height_m,
            city.step_m,
            args.spacing,
            run_date,
        )
        csv_name = stem + ".csv.gz"
        coverage_name = streetwalk_coverage_filename(csv_name)
        out_csv = os.path.join(data_dir, csv_name)
        out_coverage = os.path.join(data_dir, coverage_name)

        # Immutable-per-date snapshot: refuse to overwrite unless --force, which
        # clears the prior artifacts so this date can be re-collected.
        if os.path.exists(out_csv):
            if not args.force:
                logger.info(
                    "Streetwalk snapshot already exists for %s on %s; skipping "
                    "(use --force to re-collect, or a different --run-date).",
                    city.city_id,
                    run_date,
                )
                return 0
            logger.warning("--force: removing existing snapshot %s", out_csv)
            os.remove(out_csv)
            for stale in (out_csv[: -len(".gz")] + ".downloading", out_csv + ".rejected"):
                if os.path.exists(stale):
                    os.remove(stale)

        # Pre-flight budget guard against the isolated gsv_streets ledger.
        if args.daily_budget is not None:
            already = db.get_api_usage(conn, run_date, provider=BUDGET_CHANNEL)
            if already + len(query_points) > args.daily_budget:
                logger.error(
                    "gsv_streets daily budget %d would be exceeded: %d already spent "
                    "+ %d estimated queries. Aborting.",
                    args.daily_budget,
                    already,
                    len(query_points),
                )
                return 1

        # Load .env so GMAPS_STREETS_API_KEY is picked up the same way the grid
        # CLI loads GMAPS_API_KEY (cli.py). Done after the --estimate return so a
        # dry run needs no key at all.
        load_dotenv()
        cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
        config = load_config(BUDGET_CHANNEL)

        try:
            dict_results = asyncio.run(
                collect_points_async(
                    query_points,
                    config["api_key"],
                    out_csv,
                    city_label=city.display_name,
                    batch_size=args.batch_size,
                    connection_limit=args.connection_limit,
                    request_timeout=args.timeout,
                    max_retries=args.max_retries,
                    max_requests_per_minute=args.max_requests_per_minute,
                )
            )
        except DownloadError as e:
            # Failed crawls still spent real (billable) requests; record them so
            # a later budget check doesn't overspend the gsv_streets channel.
            spent = getattr(e, "api_requests", 0)
            if spent:
                db.add_api_usage(conn, run_date, spent, provider=BUDGET_CHANNEL)
                logger.warning("Recorded %d gsv_streets requests spent by the failed crawl", spent)
            logger.error("Collection failed: %s", e)
            return 1

        df = dict_results["df"]
        db.add_api_usage(conn, run_date, dict_results["api_requests"], provider=BUDGET_CHANNEL)

        # Reject a crawl dominated by credential/quota denials (cf. the grid
        # pipeline): it says nothing about coverage and must not be cataloged.
        failure_reason = detect_systemic_failure(df)
        if failure_reason:
            rejected_path = f"{out_csv}.rejected"
            os.replace(out_csv, rejected_path)
            logger.error(
                "Streetwalk run rejected, not cataloged: %s. Raw responses kept at %s",
                failure_reason,
                rejected_path,
            )
            return 1

        covered = compute_streetwalk_coverage(
            edges,
            samples,
            df,
            run_date.isoformat(),
            provider=PROVIDER,
            match_dist_m=args.match_dist,
        )
        geojson = build_streetwalk_geojson(
            covered,
            city_id=city.city_id,
            provider=PROVIDER,
            run_date=run_date.isoformat(),
            spacing_m=args.spacing,
            match_dist_m=args.match_dist,
            source_csv=csv_name,
        )
        with gzip.open(out_coverage, "wt", encoding="utf-8") as fh:
            json.dump(geojson, fh)

        totals = geojson["properties"]["metadata"]["totals"]
        db.register_street_walk(
            conn,
            city_id=city.city_id,
            run_date=run_date,
            csv_filename=csv_name,
            provider=PROVIDER,
            coverage_filename=coverage_name,
            network_type=args.network_type,
            spacing_m=args.spacing,
            match_dist_m=args.match_dist,
            sample_points=len(samples),
            edges_total=totals["edges"],
            edges_fully_covered=totals["edges_fully_covered"],
            mean_edge_coverage=totals["mean_edge_coverage"],
            coverage_pct_by_length=totals["coverage_pct_by_length"],
            api_requests=dict_results["api_requests"],
            started_at=dict_results.get("started_at"),
            finished_at=dict_results.get("finished_at") or datetime.now(UTC).isoformat(),
        )

        # Refresh the sidecar manifest so the city page can actually find this
        # artifact (issue #155). The collector is a manual CLI outside the
        # scheduler, and the manifest is only otherwise rebuilt by a nightly
        # run-due or an explicit regenerate-aggregate — without this a freshly
        # collected walk would publish but stay invisible until the next one of
        # those. Catalog-driven and cheap (one small query, no artifact reads).
        manifest = generate_streetwalk_manifest(conn, data_dir)

        logger.info(
            "Wrote %s and %s (manifest: %d walks)",
            out_csv,
            out_coverage,
            len(manifest["walks"]),
        )
        print(
            f"{city.city_id} [streetwalk {PROVIDER} {run_date}]: "
            f"{len(samples)} samples over {totals['edges']} edges "
            f"({dict_results['api_requests']} GSV queries); "
            f"mean edge coverage {totals['mean_edge_coverage']:.3f}, "
            f"{totals['coverage_pct_by_length']}% of street-km covered "
            f"({totals['edges_fully_covered']}/{totals['edges']} edges fully covered)"
        )
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Road-walk street-coverage collection (issue #99)."
    )
    parser.add_argument("city", help="City query or catalog slug (e.g. 'Seattle, WA')")
    parser.add_argument(
        "--spacing",
        # Integer metres only: the artifact filename encodes spacing as an
        # integer `sp{N}` token (naming.generate_streetwalk_filename), so a
        # fractional spacing would silently truncate (12.5 -> "sp12") and
        # misrepresent the run. Reject it at the CLI instead.
        type=int,
        default=DEFAULT_SPACING_M,
        help=f"Along-edge sample spacing in whole metres (default: {DEFAULT_SPACING_M})",
    )
    parser.add_argument(
        "--match-dist",
        type=float,
        default=DEFAULT_MATCH_DIST_M,
        help=f"Max sample-to-pano distance in metres (default: {DEFAULT_MATCH_DIST_M})",
    )
    parser.add_argument(
        "--network-type",
        default="drive",
        help="OSM network type to walk (default: drive; 'walk'/'all' are broader)",
    )
    parser.add_argument(
        "--run-date",
        default=None,
        help="Collection date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-collect this run-date, removing any existing snapshot first",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download (re-freeze) the OSM network instead of using the cache",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Report edge/sample/query counts and exit — no API key or requests needed",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--connection-limit", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--max-requests-per-minute",
        type=int,
        default=DEFAULT_MAX_REQUESTS_PER_MINUTE,
        help=(
            "Client-side pacing cap for the gsv_streets key "
            f"(default: {DEFAULT_MAX_REQUESTS_PER_MINUTE}); <= 0 disables pacing"
        ),
    )
    parser.add_argument(
        "--daily-budget",
        type=int,
        default=None,
        help="If set, abort when today's gsv_streets usage + estimated queries would exceed it",
    )
    parser.add_argument("--data-dir", default=get_default_data_dir(), help="Data directory")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return run_collect(args)


if __name__ == "__main__":
    sys.exit(main())
