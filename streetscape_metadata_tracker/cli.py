"""
Command-line interface for the GSV Metadata Tracker tool.

Each invocation collects one dated snapshot ("run") of a city per imagery
provider — by default BOTH GSV and Mapillary, back-to-back with the same
run date, so the two series stay in sync (--provider gsv|mapillary
restricts to one). Runs are cataloged in a SQLite database (see db.py); a
city's grid geometry is frozen in the catalog at first registration so
that all future runs sample the exact same grid and run-to-run diffs are
meaningful.

Skip policy (per provider): if the provider's latest run is newer than
--min-days-since-last-run days, that provider is skipped without
downloading (use --force to override).

Concurrent API requests are controlled by two key parameters:

1. batch_size: How many API requests we prepare and queue up at once
   - Example: batch_size=200 means we prepare 200 requests in one batch
   - Larger batch sizes mean more memory usage but better throughput

2. connection_limit: Maximum number of concurrent connections to the API
   - Example: connection_limit=100 means only 100 requests can be in-flight at once
   - This helps prevent overwhelming the network or API
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, date, datetime

from dotenv import find_dotenv, load_dotenv

from . import (
    config,
    create_visualization_map,
    db,
    display_search_area,
    download_gsv_metadata_async,
    download_mapillary_metadata_async,
    get_city_location_data,
    get_search_dimensions,
    load_config,
    open_in_browser,
)
from .analysis import calculate_run_stats, detect_systemic_failure, print_df_summary
from .diff import compute_run_diff, generate_diff_filename, write_diff_detail
from .download_common import DownloadError
from .download_mapillary import DEFAULT_TILE_REQUESTS_PER_MINUTE
from .fileutils import load_city_csv_file
from .json_summarizer import (
    generate_aggregate_v2,
    generate_city_metadata_summary_as_json,
    generate_streetwalk_manifest,
)
from .naming import generate_run_filename, same_grid_geometry, sanitize_city_query_str
from .paths import get_default_data_dir, get_default_vis_dir

logger = logging.getLogger(__name__)

# Ceiling for auto-derived grid dimensions, per side (issue #166; supersedes
# the 80 km value from #91). At the standard 20 m step, 40 km/side is ~4M grid
# points — inside the production gsv daily budget (10M) with room for other
# cities, and roughly twice Seattle's grid, comfortably more than an urban
# core. Production data showed the old 80 km clamp still admitted grids no
# night can absorb (Cairo ~10.5M points was skipped as over-budget every night,
# and its ZERO_RESULTS fill alone OOMed the Mapillary tail — see #157/#166).
# scripts/cap_oversized_grids.py applies this same cap retroactively; keep the
# two in sync by importing this constant. NB the budget math above assumes the
# standard 20 m step — this is a *dimension* cap, so a finer --step re-inflates
# the point count (40 km/side at step 10 is ~16M points, over budget again).
# Override with --width/--height for a genuinely larger area.
MAX_GRID_DIM_M = 40_000


def _resolve_center(city_loc_data):
    """
    Grid center from a geocode result: the OSM bounding-box midpoint when
    available (correct — the grid dimensions are derived from that same bbox,
    so the sampled rectangle actually covers the boundary), else the geocoder's
    reported point as a fallback. Returns (lat, lng) or None.
    """
    if city_loc_data is None:
        return None
    center = city_loc_data.bbox_center
    if center is not None:
        return center
    return (city_loc_data.latitude, city_loc_data.longitude)


def _cap_dimensions(grid_width, grid_height, city):
    """Clamp auto-derived grid dimensions to MAX_GRID_DIM_M, warning if clamped."""
    capped_w = min(grid_width, MAX_GRID_DIM_M)
    capped_h = min(grid_height, MAX_GRID_DIM_M)
    if capped_w < grid_width or capped_h < grid_height:
        logger.warning(
            f"Derived grid for '{city}' is {grid_width:.0f}x{grid_height:.0f}m; "
            f"clamping to {capped_w:.0f}x{capped_h:.0f}m (the OSM boundary is far "
            f"larger than a typical city sample). Use --width/--height to override."
        )
    return capped_w, capped_h


def parse_args():
    """
    Parse and validate command line arguments.

    Supports two optional override mechanisms (used only when a city is not
    yet registered in the catalog; registered cities reuse their frozen
    geometry so grids align across runs):

    1. --lat / --lng: Override geocoded city center coordinates.
    2. --width / --height: Override inferred city boundary dimensions.

    For the Google Street View Static API (30,000 requests/minute limit):
    - Conservative: batch_size=100, connection_limit=50
    - Moderate: batch_size=200, connection_limit=100
    - Aggressive: batch_size=400, connection_limit=200

    Returns:
        argparse.Namespace: Parsed command line arguments
    """
    parser = argparse.ArgumentParser(
        description="GSV Metadata Tracker", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("city", help="City name to analyze")

    parser.add_argument(
        "--provider",
        choices=["both", "gsv", "mapillary"],
        default="both",
        help="Imagery provider(s) to collect. The default collects GSV then "
        "Mapillary back-to-back with the same run date so the two "
        "series stay in sync. Each provider keeps its own independent "
        "run series (dated files, diffs, skip policy) on the same "
        "frozen city grid.",
    )

    parser.add_argument(
        "--download-dir",
        type=str,
        help="Dir to save downloaded data (defaults to ./data)",
        default=get_default_data_dir(),
    )

    parser.add_argument(
        "--width",
        type=float,
        default=None,
        help="Search grid width in meters. Only used when the city is not "
        "yet registered; registered cities reuse frozen geometry. "
        "(Default if inference fails: 1000m)",
    )

    parser.add_argument(
        "--height", type=float, default=None, help="Search grid height in meters. See --width."
    )

    parser.add_argument(
        "--lat",
        type=float,
        default=None,
        help="Latitude of city center. Must be used with --lng. Only used "
        "when the city is not yet registered.",
    )

    parser.add_argument(
        "--lng", type=float, default=None, help="Longitude of city center. Must be used with --lat."
    )

    parser.add_argument(
        "--step",
        type=float,
        default=20,
        help="Step size in meters between sample points in the grid "
        "(only used when the city is not yet registered)",
    )

    # Temporal tracking / run policy
    run_group = parser.add_argument_group("Run Policy")
    run_group.add_argument(
        "--run-date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Date recorded for this run (default: today, UTC). Embedded in the output filename.",
    )
    run_group.add_argument(
        "--force", action="store_true", help="Collect even if a recent run exists"
    )
    run_group.add_argument(
        "--min-days-since-last-run",
        type=int,
        default=80,
        help="Skip (exit 0) if the latest run is newer than this many days",
    )
    run_group.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to the run catalog database (default: {download-dir}/streetscape_tracker.db)",
    )
    run_group.add_argument(
        "--no-publish-json",
        action="store_true",
        help="Skip regenerating the aggregate cities.json.gz (the per-run "
        "JSON is always written). Useful in batch/scheduler contexts "
        "that regenerate the aggregate once at the end.",
    )

    # Parameters controlling concurrent processing
    concurrency_group = parser.add_argument_group("Concurrency Control")
    concurrency_group.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="""Number of requests to prepare and queue at once.
             Should be >= connection-limit. Higher values use more memory
             but can be more efficient. API limit is 500/second.""",
    )

    concurrency_group.add_argument(
        "--connection-limit",
        type=int,
        default=50,
        help="""Maximum number of concurrent connections to the API.
             Controls how many requests are actually in-flight at once.
             Should be <= batch-size. Conservative values prevent overwhelming
             the network or API.""",
    )

    concurrency_group.add_argument(
        "--max-requests-per-minute",
        type=int,
        default=24_000,
        help="""Client-side cap on GSV metadata requests per minute (gsv
             provider only). Default 24,000 is 80%% of the API's default
             30,000/min project quota; scale with your granted quota.
             0 disables pacing. Exceeding the server-side quota returns
             OVER_QUERY_LIMIT responses, which burn retries and can abort
             the run.""",
    )

    concurrency_group.add_argument(
        "--mapillary-max-requests-per-minute",
        type=int,
        default=DEFAULT_TILE_REQUESTS_PER_MINUTE,
        help=f"""Client-side cap on Mapillary vector-tile requests per minute
             (mapillary provider only). Separate from the GSV cap above
             because the limit it respects is a different KIND: Mapillary's
             tile CDN rate-limits per IP rather than per token or project,
             and exceeding it blocks the whole host — every Mapillary channel
             at once, including the nightly scheduler's. Default
             {DEFAULT_TILE_REQUESTS_PER_MINUTE}; 0 disables pacing.""",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for each individual API request",
    )

    parser.add_argument(
        "--check-boundary",
        "--check-size",
        action="store_true",
        help="Generate visualization of search area without downloading data. "
        "Useful for verifying the area before starting a download.",
    )

    parser.add_argument(
        "--no-visual", action="store_true", help="Skip generating visualizations of the results"
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level for output messages",
    )

    args = parser.parse_args()

    if (args.lat is None) != (args.lng is None):
        parser.error("--lat and --lng must be used together")

    if (args.width is None) != (args.height is None):
        parser.error("--width and --height must be used together")

    if args.connection_limit > args.batch_size:
        parser.error("connection-limit cannot be larger than batch-size")

    return args


def _resolve_geometry(conn, args):
    """
    Resolve the city's identity and grid geometry.

    Registered cities reuse their frozen geometry from the catalog (zero
    geocoding calls). Unknown cities are geocoded once, their grid inferred
    (or taken from explicit --lat/--lng/--width/--height overrides), and
    registered so future runs align.

    Returns:
        (city_row: db.CityRow, newly_registered: bool)
    """
    city_row = db.resolve_city(conn, args.city)
    if city_row is not None:
        overrides = [
            o
            for o, v in (("--lat/--lng", args.lat), ("--width/--height", args.width))
            if v is not None
        ]
        if overrides:
            logger.warning(
                f"{' and '.join(overrides)} ignored: '{args.city}' is already "
                f"registered as {city_row.city_id} with frozen grid geometry "
                f"(center {city_row.center_lat:.5f},{city_row.center_lon:.5f}, "
                f"{city_row.grid_width_m}x{city_row.grid_height_m}m, "
                f"step {city_row.step_m}m). Changing geometry would break "
                f"run-to-run diffs."
            )
        return city_row, False

    # Unknown city: geocode once and register with frozen geometry
    city_loc_data = get_city_location_data(args.city)

    if args.lat is not None:
        center_lat, center_lng = args.lat, args.lng
        print(f"Using user-provided coordinates: {center_lat}, {center_lng}")
    elif city_loc_data:
        center_lat, center_lng = _resolve_center(city_loc_data)
    else:
        logger.error(
            f"Could not find coordinates for {args.city}. "
            f"Use --lat and --lng to provide them manually."
        )
        sys.exit(1)

    if args.width is not None:
        grid_width, grid_height = args.width, args.height
        print(f"Using provided dimensions: {grid_width:.1f}m x {grid_height:.1f}m")
    else:
        grid_width, grid_height = get_search_dimensions(args.city, 1000, 1000)
        grid_width, grid_height = _cap_dimensions(grid_width, grid_height, args.city)

    city_name = city_loc_data.city if city_loc_data else args.city.split(",")[0].strip()
    state_name = city_loc_data.state if city_loc_data else None
    state_code = city_loc_data.state_code if city_loc_data else None
    country_name = city_loc_data.country if city_loc_data else None
    country_code = city_loc_data.country_code if city_loc_data else None

    city_id = db.register_city(
        conn,
        city_name=city_name,
        state_name=state_name,
        state_code=state_code,
        country_name=country_name,
        country_code=country_code,
        center_lat=center_lat,
        center_lon=center_lng,
        grid_width_m=grid_width,
        grid_height_m=grid_height,
        step_m=args.step,
    )
    # Alias the user's query slug to the canonical id so future invocations
    # with the same query resolve without geocoding (and geocoder naming
    # drift can't re-register the city under a different id)
    query_slug = sanitize_city_query_str(args.city)
    if query_slug != city_id:
        db.add_alias(conn, query_slug, city_id)

    logger.info(f"Registered new city {city_id} with frozen geometry")
    return db.resolve_city(conn, city_id), True


def _compute_and_record_diff(
    conn, city_row, prev_run, run_id, run_date, df_new, download_dir, provider="gsv"
):
    """
    Diff the new run against the previous one of the same provider, persist
    the summary row (and detail csv.gz when there are changes), and return
    the JSON change block.
    """
    prev_csv_path = os.path.join(download_dir, prev_run.csv_filename)
    if not os.path.exists(prev_csv_path):
        logger.warning(f"Previous run file missing, skipping diff: {prev_csv_path}")
        return None

    df_old = load_city_csv_file(prev_csv_path)
    diff = compute_run_diff(df_old, df_new)

    detail_filename = None
    if diff.has_changes:
        detail_filename = generate_diff_filename(
            city_row.city_id, prev_run.run_date, run_date.isoformat(), provider=provider
        )
        write_diff_detail(diff, os.path.join(download_dir, detail_filename))

    db.record_diff(
        conn,
        city_id=city_row.city_id,
        from_run_id=prev_run.run_id,
        to_run_id=run_id,
        grid_aligned=diff.grid_aligned,
        panos_added=diff.panos_added,
        panos_removed=diff.panos_removed,
        panos_persisted=diff.panos_persisted,
        capture_date_changed=diff.capture_date_changed,
        points_gained_coverage=diff.points_gained_coverage,
        points_lost_coverage=diff.points_lost_coverage,
        coverage_delta_pct=diff.coverage_delta_pct,
        detail_filename=detail_filename,
    )

    print(f"\nChanges since {prev_run.run_date}:")
    print(f"  Panos added:          {diff.panos_added:,}")
    print(f"  Panos removed:        {diff.panos_removed:,}")
    print(f"  Capture date changed: {diff.capture_date_changed:,}")
    if diff.grid_aligned:
        print(f"  Coverage delta:       {diff.coverage_delta_pct:+.2f} pct points")
    else:
        print("  (query grids differ; coverage transitions not computed)")

    return {
        "from_run_date": prev_run.run_date,
        "panos_added": diff.panos_added,
        "panos_removed": diff.panos_removed,
        "capture_date_changed": diff.capture_date_changed,
        "coverage_delta_pct": diff.coverage_delta_pct,
        "grid_aligned": diff.grid_aligned,
        "diff_file": detail_filename,
    }


async def async_main():
    """
    Main async function that coordinates one GSV metadata collection run:
    resolve city (frozen geometry) -> skip policy -> download -> catalog run
    -> diff vs previous run -> per-run JSON -> aggregate JSON -> map.
    """
    # Load environment variables from .env file immediately
    load_dotenv()
    config.warn_if_credentials_world_readable(find_dotenv(usecwd=True))

    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    os.makedirs(args.download_dir, exist_ok=True)
    logging.info(f"Using download directory: {args.download_dir}")

    run_date = args.run_date or datetime.now(UTC).date()
    db_path = args.db_path or db.get_default_db_path(args.download_dir)

    providers = ["gsv", "mapillary"] if args.provider == "both" else [args.provider]

    try:
        # Fail fast: require every requested provider's credential before
        # any downloading, so a missing key can't leave the series unpaired
        configs = {provider: load_config(provider) for provider in providers}
        conn = db.connect(db_path)

        vis_path = get_default_vis_dir()
        os.makedirs(vis_path, exist_ok=True)

        # Boundary preview: geocode + visualize, but don't register or download
        if args.check_boundary:
            return _check_boundary(conn, args, vis_path)

        city_row, newly_registered = _resolve_geometry(conn, args)

        print(f"City: {city_row.display_name} ({city_row.city_id})")
        print(
            f"Grid: {city_row.grid_width_m}m x {city_row.grid_height_m}m, "
            f"step {city_row.step_m}m, centered at "
            f"{city_row.center_lat:.5f}, {city_row.center_lon:.5f}"
        )

        # Collect each provider in turn (same run_date, so series pair up).
        # One provider failing must not prevent the other from collecting.
        failed = []
        for provider in providers:
            try:
                await _collect_one_run(
                    conn, args, city_row, run_date, provider, configs[provider], vis_path
                )
            except Exception as e:
                logging.exception(f"{provider} collection failed: {e}")
                failed.append(provider)

        if not args.no_publish_json:
            generate_aggregate_v2(conn, args.download_dir)
            generate_streetwalk_manifest(conn, args.download_dir)

        if failed:
            print(f"FAILED: {', '.join(failed)} (see log for details)")
            return 1
        return 0

    except Exception as e:
        logging.exception(f"Error: {str(e)}")
        sys.exit(1)


async def _collect_one_run(conn, args, city_row, run_date, provider, config, vis_path) -> None:
    """
    Collect, catalog, diff, and summarize one (city, provider) run.
    Returns silently when the skip policy applies; raises on failure.
    """
    # Skip policy: honor --min-days-since-last-run unless --force.
    # Each provider is an independent run series.
    latest = db.get_latest_run(conn, city_row.city_id, provider)
    if latest is not None:
        days_since = (run_date - date.fromisoformat(latest.run_date)).days
        if latest.run_date == run_date.isoformat():
            print(
                f"A {provider} run already exists for {city_row.city_id} "
                f"on {run_date} ({latest.csv_filename}); nothing to do. "
                f"Use --run-date to record a different date."
            )
            return
        if not args.force and days_since < args.min_days_since_last_run:
            print(
                f"SKIP: latest {provider} run for {city_row.city_id} is "
                f"{days_since} days old ({latest.run_date}), newer than "
                f"--min-days-since-last-run={args.min_days_since_last_run}. "
                f"Use --force to collect anyway."
            )
            return

    # Dated, immutable output file for this run
    run_base = generate_run_filename(
        city_row.city_id,
        city_row.grid_width_m,
        city_row.grid_height_m,
        city_row.step_m,
        run_date,
        provider=provider,
    )
    output_csv_gz_path = os.path.join(args.download_dir, f"{run_base}.csv.gz")

    # Immutable-snapshot guard: an existing file with this run's name is
    # either a concurrent collection in flight or an orphan from a run that
    # crashed between file-write and cataloging. Overwriting is never safe
    # (the published file could diverge from the cataloged stats/diffs), so
    # refuse and let the operator delete a confirmed orphan.
    if os.path.exists(output_csv_gz_path):
        raise RuntimeError(
            f"{output_csv_gz_path} already exists but no {provider} run is "
            f"cataloged for {run_date}. Refusing to overwrite an existing "
            f"snapshot (concurrent run in progress, or orphan from a crashed "
            f"run — if confirmed orphan, delete it and re-run)."
        )

    logging.info(f"Collecting {provider} run {run_date} for {city_row.city_id}")

    try:
        if provider == "mapillary":
            dict_results = await download_mapillary_metadata_async(
                city_name=city_row.display_name,
                center_lat=city_row.center_lat,
                center_lon=city_row.center_lon,
                grid_width=city_row.grid_width_m,
                grid_height=city_row.grid_height_m,
                step_length=city_row.step_m,
                access_token=config["access_token"],
                output_csv_gz_path=output_csv_gz_path,
                request_timeout=args.timeout,
                max_requests_per_minute=args.mapillary_max_requests_per_minute,
            )
        else:
            logging.info(
                f"Using batch_size={args.batch_size}, connection_limit={args.connection_limit}, "
                f"max_requests_per_minute={args.max_requests_per_minute}"
            )
            dict_results = await download_gsv_metadata_async(
                city_name=city_row.display_name,
                center_lat=city_row.center_lat,
                center_lon=city_row.center_lon,
                grid_width=city_row.grid_width_m,
                grid_height=city_row.grid_height_m,
                step_length=city_row.step_m,
                api_key=config["api_key"],
                output_csv_gz_path=output_csv_gz_path,
                batch_size=args.batch_size,
                connection_limit=args.connection_limit,
                request_timeout=args.timeout,
                max_requests_per_minute=args.max_requests_per_minute,
            )
    except DownloadError as e:
        # Failed downloads still spent real (budgeted, possibly billable)
        # requests; record them so tomorrow's budget check doesn't overspend.
        spent = getattr(e, "api_requests", 0)
        if spent:
            db.add_api_usage(conn, run_date, spent, provider=provider)
            logger.warning(f"Recorded {spent} {provider} API requests spent by the failed download")
        raise
    df = dict_results["df"]

    # Record API usage in the daily per-provider budget ledger (the
    # requests were spent even if the run is rejected below)
    db.add_api_usage(conn, run_date, dict_results["api_requests"], provider=provider)

    # Refuse to catalog a run dominated by credential/quota denials: it
    # says nothing about the city, and once registered it would become the
    # diff baseline and get published. The raw responses are kept under a
    # .rejected suffix (excluded from the *.csv.gz publish glob).
    failure_reason = detect_systemic_failure(df)
    if failure_reason:
        rejected_path = f"{output_csv_gz_path}.rejected"
        os.replace(output_csv_gz_path, rejected_path)
        raise RuntimeError(
            f"{provider} run rejected, not cataloged: {failure_reason}. "
            f"Raw responses kept at {rejected_path}"
        )

    print(f"\nDownload Summary for {city_row.display_name} [{provider}]")
    print("=" * 50)
    print_df_summary(df, provider=provider)

    # Catalog the run
    stats = calculate_run_stats(df, run_date, provider=provider)
    duration = None
    started = dict_results.get("started_at")
    finished = dict_results.get("finished_at")
    if started and finished:
        duration = (
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds()
    run_id = db.register_run(
        conn,
        city_id=city_row.city_id,
        run_date=run_date,
        csv_filename=os.path.basename(output_csv_gz_path),
        provider=provider,
        started_at=started,
        finished_at=finished,
        duration_seconds=duration,
        api_requests=dict_results["api_requests"],
        # Flat-image census (issue #116) is a Mapillary downloader artifact,
        # not derivable from the CSV — GSV runs omit it (stored NULL).
        num_flat_images=dict_results.get("num_flat_images"),
        **stats,
    )

    # Diff against the previous run of the same provider, if any — but only
    # when both runs sampled the same grid geometry. Archival baselines
    # (issue #93) carry their own width/height/step, recoverable only from
    # the filename; a cross-geometry diff would compare different sampled
    # areas and produce meaningless added/removed counts.
    change_block = None
    prev_run = db.get_previous_run(conn, city_row.city_id, run_date, provider=provider)
    if prev_run is not None:
        new_csv_filename = os.path.basename(output_csv_gz_path)
        if same_grid_geometry(prev_run.csv_filename, new_csv_filename):
            change_block = _compute_and_record_diff(
                conn, city_row, prev_run, run_id, run_date, df, args.download_dir, provider=provider
            )
        else:
            logger.warning(
                f"Skipping diff for {city_row.city_id} [{provider}]: previous "
                f"run {prev_run.csv_filename} has different grid geometry than "
                f"{new_csv_filename}; diffs resume once two same-geometry runs "
                f"exist"
            )

    # Per-run summary JSON (schema v2, ages pinned to run_date)
    json_path = generate_city_metadata_summary_as_json(
        output_csv_gz_path,
        df,
        city_row.city_name,
        city_row.state_name,
        city_row.country_name,
        city_row.grid_width_m,
        city_row.grid_height_m,
        city_row.step_m,
        force_recreate_file=True,
        run_date=run_date,
        change_from_previous_run=change_block,
        provider=provider,
    )
    db.update_run_json_filename(conn, run_id, os.path.basename(json_path))

    if not args.no_visual:
        map_path = os.path.join(vis_path, f"{run_base}.html")
        map_obj = create_visualization_map(df, city_row.display_name, provider=provider)
        print(f"Saving map visualization to {map_path}")
        map_obj.save(map_path)
        logging.info(f"Map visualization saved to {map_path}")


def _check_boundary(conn, args, vis_path: str) -> int:
    """
    Preview the search area without registering or downloading.

    Uses the same identity and geometry a real run would: a registered city
    keeps its canonical city_id and frozen geometry (so the preview filename
    matches the run files exactly); an unknown city is geocoded and its
    canonical id derived the same way register_city would — without
    registering it.
    """
    from .naming import generate_base_filename

    city_row = db.resolve_city(conn, args.city)
    if city_row is not None:
        overrides = [
            o
            for o, v in (("--lat/--lng", args.lat), ("--width/--height", args.width))
            if v is not None
        ]
        if overrides:
            logger.warning(
                f"{' and '.join(overrides)} ignored: '{args.city}' is already "
                f"registered as {city_row.city_id} with frozen grid geometry"
            )
        print(f"'{args.city}' is registered as {city_row.city_id}; previewing its frozen geometry")
        city_id = city_row.city_id
        center_lat, center_lng = city_row.center_lat, city_row.center_lon
        grid_width, grid_height = city_row.grid_width_m, city_row.grid_height_m
        step = city_row.step_m
    else:
        city_loc_data = get_city_location_data(args.city)
        if args.lat is not None:
            center_lat, center_lng = args.lat, args.lng
        elif city_loc_data:
            center_lat, center_lng = _resolve_center(city_loc_data)
        else:
            logging.error(
                f"Could not find coordinates for {args.city}. "
                f"Use --lat and --lng to provide them manually."
            )
            return 1

        if args.width is not None:
            grid_width, grid_height = args.width, args.height
        else:
            grid_width, grid_height = get_search_dimensions(args.city, 1000, 1000)
            grid_width, grid_height = _cap_dimensions(grid_width, grid_height, args.city)
        step = args.step

        # Same canonical id register_city would derive, so the preview
        # filename matches the files an eventual run will produce
        if city_loc_data:
            city_id = db.derive_city_id(
                city_loc_data.city, city_loc_data.state, city_loc_data.country
            )
        else:
            city_id = db.derive_city_id(args.city, None, None)

    print(f"The search dimensions for {args.city} are {grid_width:.1f}m x {grid_height:.1f}m")

    base_name = generate_base_filename(city_id, grid_width, grid_height, step)
    boundary_vis_full_path = os.path.join(vis_path, f"{base_name}_search_boundary.html")

    search_area_map = display_search_area(
        # `step`, not args.step: a registered city previews its FROZEN step
        args.city,
        center_lat,
        center_lng,
        grid_width,
        grid_height,
        step,
    )
    search_area_map.save(boundary_vis_full_path)

    print(f"\nSearch area preview saved to: {boundary_vis_full_path}")
    print("Review the visualization and adjust parameters if needed.")
    print("\nTo download data with these parameters, run the same command without --check-boundary")

    success, error_msg = open_in_browser(boundary_vis_full_path)
    if not success:
        logging.warning(f"Could not automatically open visualization: {error_msg}")
        print(
            f"Please open {boundary_vis_full_path} in your web browser to view the visualization."
        )

    return 0


def main():
    """
    Entry point for the command line interface.

    Sets up the async environment and manages the lifecycle of:
    - Async event loop
    - Connection pools
    - Batch processing
    """

    # Windows and Unix-like systems handle async I/O differently at the OS level
    # Windows has two event loop implementations:
    # 1. ProactorEventLoop (default): Good for subprocess/pipes but can have issues with some async operations
    # 2. SelectorEventLoop: More compatible with networking operations like what we're doing
    # So, on Windows, we explicitly set the event loop to use SelectorEventLoop
    if sys.platform.startswith("win"):
        # Override default Windows event loop policy to ensure compatibility
        # with aiohttp's async networking operations
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        return asyncio.run(async_main()) or 0
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        logging.exception(f"Fatal error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
