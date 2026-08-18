import asyncio
import gzip
import logging
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import backoff
import geopy.distance
import pandas as pd
from filelock import FileLock, Timeout

from .analysis import REQUEST_FAILED, RETRYABLE_STATUSES
from .config import METADATA_DTYPES
from .download_common import (
    AsyncRateLimiter,
    DownloadError,
    generate_grid_points,
    redact_credentials,
    standardize_capture_date,
)
from .fileutils import load_city_csv_file
from .progress import progress

logger = logging.getLogger(__name__)

__all__ = [
    "collect_points_async",
    "download_gsv_metadata_async",
    "fetch_gsv_pano_metadata_async",
]

# A run whose permanently-failed points exceed this fraction of the grid is
# aborted (checkpoint kept) instead of finalized as an immutable snapshot.
MAX_FAILED_POINT_FRACTION = 0.01


def create_helpful_permission_error(path: str) -> str:
    """Create a helpful error message for permission issues."""
    return (
        f"Permission denied when accessing: {path}\n"
        f"This typically occurs on Windows when:\n"
        f"1. The data directory is read-only\n"
        f"2. Another program has locked the directory\n"
        f"3. You need administrator privileges\n\n"
        f"To fix this:\n"
        f"- Run your terminal as administrator\n"
        f"- Check folder permissions in File Explorer\n"
        f"- Close any programs that might be accessing the directory\n"
        f"- Try setting the command line param download-dir to a different directory using:\n"
        f"  python streetscape_tracker.py CITY_NAME --download-dir NEW_DIRECTORY\n"
    )


@backoff.on_exception(
    backoff.expo, (asyncio.TimeoutError, aiohttp.ClientError), max_tries=3, max_time=60
)
async def fetch_gsv_pano_metadata_async(
    lat: float,
    lon: float,
    api_key: str,
    session: aiohttp.ClientSession,
    timeout: aiohttp.ClientTimeout,
    limiter: AsyncRateLimiter | None = None,
) -> dict[str, Any]:
    """
    Get the closest pano data from Google Street View API using aiohttp with retry logic.

    Args:
        lat: Latitude coordinate
        lon: Longitude coordinate
        api_key: Google Street View API key
        session: aiohttp ClientSession for making requests
        timeout: Request timeout settings
        limiter: Optional rate limiter; acquired before every attempt
            (including backoff retries) so retried requests are paced too

    Returns:
        Dict containing the API response

    Raises:
        DownloadError: If the request fails or returns invalid data after all retries
    """
    # Google requires the key as a query parameter, so exception/response
    # text touching this URL must be scrubbed with redact_credentials()
    # before it is logged or re-raised.
    url = f"https://maps.googleapis.com/maps/api/streetview/metadata?location={lat},{lon}&key={api_key}"
    if limiter is not None:
        await limiter.acquire()
    try:
        async with session.get(url, timeout=timeout) as response:
            if response.status != 200:
                raise DownloadError(
                    f"HTTP {response.status}: {redact_credentials(await response.text())}"
                )
            return await response.json()
    except (TimeoutError, aiohttp.ClientError) as e:
        logger.warning(
            f"Attempt failed for coordinates {lat},{lon}: {redact_credentials(e)}, retrying..."
        )
        raise  # Let backoff handle the retry
    except Exception as e:
        raise DownloadError(
            f"Error fetching data for coordinates {lat},{lon}: {redact_credentials(e)}"
        ) from e


def resume_point_key(lat: float, lon: float) -> tuple[float, float]:
    """
    Matching key for resume bookkeeping: coordinates rounded to 9 decimals
    (~0.1 mm — far finer than any grid step, far coarser than float noise).

    The checkpoint CSV round-trip can perturb a coordinate by a few ULP
    (pandas' fast CSV float parser does not exactly round-trip every
    17-digit decimal), so raw float equality against freshly regenerated
    grid points would treat some already-downloaded points as new and
    re-request them.
    """
    return (round(lat, 9), round(lon, 9))


# Checkpoint rows carrying one of these statuses are NOT terminal answers —
# a resume must re-request them, never treat them as done. This also disarms
# checkpoints written by the pre-rate-limit code (throttled points baked in as
# OVER_QUERY_LIMIT rows) so a same-date re-collect can't inherit that taint.
_NON_TERMINAL_STATUSES = frozenset(RETRYABLE_STATUSES) | {REQUEST_FAILED}


def get_processed_points(file_path: str) -> set:
    """
    Get set of already processed points from existing download file.

    Rows whose status is non-terminal (still-retryable throttling/transient
    statuses, or a REQUEST_FAILED hole) are excluded, so resuming a partial
    download always re-requests those points instead of accepting a failure
    row as a final answer.

    Args:
        file_path: Path to the intermediate download file

    Returns:
        Set of resume_point_key() tuples for processed points. Compare
        grid points via resume_point_key(), never by raw float equality.
    """
    if not os.path.exists(file_path):
        return set()

    try:
        df = pd.read_csv(file_path, dtype=METADATA_DTYPES)
        df = df[~df["status"].isin(_NON_TERMINAL_STATUSES)]
        return {resume_point_key(row["query_lat"], row["query_lon"]) for _, row in df.iterrows()}
    except Exception as e:
        logger.error(f"Error reading existing file: {str(e)}")
        return set()


async def process_batch_async(
    points: list[tuple[float, float, int, int]],
    api_key: str,
    progress_queue: asyncio.Queue,
    base_file_path: str,
    timeout: aiohttp.ClientTimeout,
    connection_limit: int,
    failed_points_queue: asyncio.Queue,
    limiter: AsyncRateLimiter | None = None,
) -> list[dict]:
    """
    Process a batch of points asynchronously and append results to the
    in-progress CSV under a file lock (so a second process on the same city
    can't interleave writes).
    """
    results = []
    lock_file = f"{base_file_path}.lock"

    try:
        # Create connection-limited session
        connector = aiohttp.TCPConnector(limit=connection_limit)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = []
            for lat, lon, _i, _j in points:
                task = fetch_gsv_pano_metadata_async(lat, lon, api_key, session, timeout, limiter)
                tasks.append(task)

            responses = await asyncio.gather(*tasks, return_exceptions=True)

            batch_results = []
            for (lat, lon, i, j), response in zip(points, responses, strict=False):
                if isinstance(response, Exception):
                    logger.error(
                        f"Error processing point ({lat}, {lon}): {redact_credentials(response)}"
                    )
                    # No body status came back (network/timeout). Carry the
                    # synthetic REQUEST_FAILED reason so a permanently-failed
                    # point can be written back as a failure row.
                    await failed_points_queue.put((lat, lon, i, j, REQUEST_FAILED))
                    continue

                # Get the current UTC datetime
                now_utc = datetime.now(UTC)

                # Format the datetime as ISO 8601
                query_timestamp = now_utc.isoformat()

                status = response["status"]

                # Retryable statuses (quota throttling as a normal HTTP 200
                # OVER_QUERY_LIMIT, or a transient UNKNOWN_ERROR) say nothing
                # about the grid point itself, so route them to the retry
                # queue rather than writing them as a final row now — a
                # throttle row written immediately reads as "no imagery" and
                # corrupts coverage stats and future diffs. If a point is
                # still retryable after every pass, it is written back as a
                # failure row at finalize time (grid stays complete) with its
                # true status preserved here.
                if status in RETRYABLE_STATUSES:
                    await failed_points_queue.put((lat, lon, i, j, status))
                    continue

                result = {
                    "query_lat": lat,
                    "query_lon": lon,
                    "query_timestamp": query_timestamp,
                    "pano_lat": None,
                    "pano_lon": None,
                    "pano_id": None,
                    "capture_date": None,
                    "copyright_info": None,
                    "status": status,
                }

                if status == "OK":
                    # I have found that capture_date can be formatted in a variety of formats like format='%Y-%m' (most commonly) or format='%Y-%m-%d'.
                    # So, we should standardize the data format to make it consistent and easier for others to use once archived in a file
                    capture_date_raw = response.get(
                        "date", None
                    )  # Get the raw capture date from the API response
                    capture_date_standardized = standardize_capture_date(capture_date_raw)

                    result.update(
                        {
                            "pano_lat": response["location"]["lat"],
                            "pano_lon": response["location"]["lng"],
                            "pano_id": response["pano_id"],
                            "copyright_info": response.get("copyright", None),
                            "capture_date": capture_date_standardized,
                        }
                    )
                    if not result["capture_date"]:
                        result["status"] = "NO_DATE"

                batch_results.append(result)
                await progress_queue.put(1)

        # Every point in this batch failed (all are queued for the retry
        # pass); an empty DataFrame would KeyError on astype and abort the
        # whole run for what is usually a transient network blip.
        if not batch_results:
            return results

        # Append batch results to the in-progress CSV under a file lock.
        # The lock FILE is never deleted: removing a path another process
        # may be locking on lets a third acquirer create a fresh lock and
        # interleave CSV writes — the exact corruption the lock prevents.
        # Stale lock files are harmless (excluded from publish).
        df_batch = pd.DataFrame(batch_results)  # First create the DataFrame
        df_batch = df_batch.astype(METADATA_DTYPES)  # Then apply the dtypes

        lock = FileLock(lock_file, timeout=10)
        try:
            with lock:
                if os.path.exists(base_file_path):
                    df_batch.to_csv(base_file_path, mode="a", header=False, index=False)
                else:
                    df_batch.to_csv(base_file_path, index=False)
        except PermissionError as e:
            raise PermissionError(
                "FileLock failure: " + create_helpful_permission_error(base_file_path)
            ) from e

        results.extend(batch_results)

    except Exception as e:
        raise DownloadError(f"Error processing batch: {str(e)}") from e

    return results


def _append_failure_rows(downloading_path: str, failed_points: list[tuple], lock_file: str) -> None:
    """
    Append one failure row per permanently-failed grid point to the in-progress
    CSV, so the finalized snapshot has a row for every grid point (run-to-run
    diff alignment depends on this — see download_gsv_metadata_async). Each row
    carries the point's true failure status (REQUEST_FAILED, OVER_QUERY_LIMIT,
    …) with all pano fields null, so it is excluded from coverage/pano stats
    but keeps the grid complete.

    Args:
        downloading_path: the in-progress .downloading CSV to append to
        failed_points: (lat, lon, i, j, status) tuples still failing after all
            retries
        lock_file: the same FileLock path process_batch_async writes under
    """
    now_utc = datetime.now(UTC).isoformat()
    rows = [
        {
            "query_lat": lat,
            "query_lon": lon,
            "query_timestamp": now_utc,
            "pano_lat": None,
            "pano_lon": None,
            "pano_id": None,
            "capture_date": None,
            "copyright_info": None,
            "status": reason,
        }
        for (lat, lon, _i, _j, reason) in failed_points
    ]
    df_fail = pd.DataFrame(rows).astype(METADATA_DTYPES)

    lock = FileLock(lock_file, timeout=10)
    with lock:
        if os.path.exists(downloading_path):
            df_fail.to_csv(downloading_path, mode="a", header=False, index=False)
        else:
            df_fail.to_csv(downloading_path, index=False)


async def download_gsv_metadata_async(
    city_name: str,
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float,
    api_key: str,
    output_csv_gz_path: str,
    batch_size: int = 50,
    connection_limit: int = 50,
    request_timeout: float = 30.0,
    max_retries: int = 3,
    max_requests_per_minute: int = 24_000,
) -> dict[str, Any]:
    """
    Fetch GSV metadata for a city's frozen grid using async/await.

    Thin wrapper: it builds the grid sample points and delegates the actual
    collection (batching, rate limiting, quota-retry, resume, guards) to the
    provider-agnostic `collect_points_async` engine, which the street-coverage
    road-walk collector (issue #99) also drives — so both request paths share
    the exact same OVER_QUERY_LIMIT hardening.

    The caller decides the output filename (run-skip policy and dated naming
    live in the CLI/scheduler layer, not here). If a partial download exists
    for the same output path (a sibling .downloading file), it is resumed.

    Args:
        city_name: Name of the city (for logging/progress display only)
        center_lat: Center latitude
        center_lon: Center longitude
        grid_width: Width of search grid in meters
        grid_height: Height of search grid in meters
        step_length: Distance between sample points in meters
        api_key: Google Street View API key
        output_csv_gz_path: Full path of the .csv.gz file to write
        batch_size: Number of requests to prepare and queue at once
        connection_limit: Maximum number of concurrent connections to the API
        request_timeout: Timeout for each request in seconds
        max_retries: Maximum number of retry attempts for failed points
        max_requests_per_minute: Client-side pacing cap shared by all
            concurrent requests in this run. Default 24,000 is 80% of the
            GSV metadata API's default 30,000/min project quota; scale it
            with your granted quota. <= 0 disables pacing. Exceeding the
            server-side quota is not just slower — throttled points burn
            retries and can abort the run via the failed-point guard.

    Returns:
        Dict with:
            df: DataFrame containing the GSV metadata
            filename_with_path: the written .csv.gz path
            api_requests: number of API requests actually issued this call
            started_at / finished_at: UTC ISO 8601 timestamps
    """
    logger.info(
        f"Examining street view data for {city_name} centered at {center_lat},{center_lon}"
        + f" with a grid of {grid_width / 1000:.1f}km x {grid_height / 1000:.1f}km and step_length={step_length} meters"
    )

    # Calculate grid dimensions and generate all sample points up-front.
    width_steps = int(grid_width / step_length)
    height_steps = int(grid_height / step_length)
    origin = geopy.Point(center_lat, center_lon)
    all_points = generate_grid_points(origin, width_steps, height_steps, step_length)

    return await collect_points_async(
        all_points,
        api_key,
        output_csv_gz_path,
        city_label=city_name,
        batch_size=batch_size,
        connection_limit=connection_limit,
        request_timeout=request_timeout,
        max_retries=max_retries,
        max_requests_per_minute=max_requests_per_minute,
    )


async def collect_points_async(
    points: list[tuple[float, float, int, int]],
    api_key: str,
    output_csv_gz_path: str,
    *,
    city_label: str = "",
    batch_size: int = 50,
    connection_limit: int = 50,
    request_timeout: float = 30.0,
    max_retries: int = 3,
    max_requests_per_minute: int = 24_000,
) -> dict[str, Any]:
    """
    Query GSV pano metadata for an arbitrary set of points and write a snapshot.

    This is the provider-agnostic collection engine shared by the grid
    downloader (`download_gsv_metadata_async`) and the road-walk street
    collector (issue #99). It owns everything below the point generation: the
    per-run rate limiter, batched requests, the OVER_QUERY_LIMIT / quota-window
    retry passes, the failed-point finalize guard, `.downloading` resume, the
    immutable-snapshot + run-lock guards, and gzip finalize. Callers differ
    only in how they generate `points` (a grid lattice vs. on-street samples
    along OSM edges).

    Args:
        points: (lat, lon, a, b) tuples to query. `a, b` are opaque bookkeeping
            ints (grid i/j, or edge/sample indices) surfaced only in the
            `_failed_points.csv` diagnostic; output rows key on lat/lon.
        api_key: Google Street View API key.
        output_csv_gz_path: Full path of the .csv.gz snapshot to write.
        city_label: Human label for logs/progress only.
        batch_size, connection_limit, request_timeout, max_retries,
        max_requests_per_minute: see `download_gsv_metadata_async`.

    Returns:
        Same dict shape as `download_gsv_metadata_async` (df, filename_with_path,
        api_requests, started_at, finished_at).
    """
    start_time = time.time()
    started_at = datetime.now(UTC).isoformat()
    api_requests = 0

    logger.info(f"Using batch_size={batch_size}, connection_limit={connection_limit}")

    # Set up timeout
    timeout = aiohttp.ClientTimeout(total=request_timeout)

    # One limiter for the whole run: initial batches and retry passes share
    # the same token bucket, so the aggregate request rate stays under the
    # per-minute quota no matter how the work is scheduled.
    limiter = AsyncRateLimiter(max_requests_per_minute)
    if max_requests_per_minute > 0:
        logger.info(f"Client-side rate limit: {max_requests_per_minute} requests/minute")

    # Derive working file paths from the requested output path
    if not output_csv_gz_path.endswith(".csv.gz"):
        raise ValueError(f"output_csv_gz_path must end in .csv.gz, got: {output_csv_gz_path}")
    file_name_compressed_with_path = output_csv_gz_path
    file_name_with_path = output_csv_gz_path[: -len(".gz")]  # .csv
    file_name_downloading_with_path = file_name_with_path + ".downloading"
    failed_points_file = output_csv_gz_path[: -len(".csv.gz")] + "_failed_points.csv"

    Path(os.path.dirname(os.path.abspath(output_csv_gz_path))).mkdir(parents=True, exist_ok=True)

    # Immutable-snapshot guard: a completed run file is never overwritten.
    # A concurrent second run (e.g. scheduler + manual invocation of the
    # same city/date) would otherwise share the same .downloading file and
    # eventually clobber the registered snapshot with partial data.
    if os.path.exists(file_name_compressed_with_path):
        raise DownloadError(
            f"Output file already exists: {file_name_compressed_with_path}. "
            f"Runs are immutable snapshots; refusing to overwrite. "
            f"(Same city/provider/run-date already collected?)"
        )

    # Run-level mutual exclusion, held for the WHOLE run (the per-batch
    # lock only serializes individual appends). timeout=0: a second
    # process fails fast instead of queueing behind a multi-hour run.
    run_lock = FileLock(f"{file_name_downloading_with_path}.runlock", timeout=0)
    try:
        run_lock.acquire()
    except Timeout:
        raise DownloadError(
            f"Another process is already collecting {output_csv_gz_path} "
            f"(run lock held); refusing to run concurrently."
        ) from None

    try:
        all_points = points

        # Get already processed points
        processed_points = get_processed_points(file_name_downloading_with_path)

        # Filter out already processed points (quantized keys — see
        # resume_point_key; raw float equality re-requested ULP-perturbed
        # points after the checkpoint CSV round-trip)
        remaining_points = [
            point
            for point in all_points
            if resume_point_key(point[0], point[1]) not in processed_points
        ]

        if len(processed_points) > 0:
            logger.info(
                f"Found {len(processed_points)} already processed points. {len(remaining_points)} points remaining."
            )
        else:
            logger.info(
                f"No previous points processed. Processing all points ({len(remaining_points)} total)."
            )

        if len(remaining_points) == 0:
            logger.info("All points already processed.")
            if not os.path.exists(file_name_downloading_with_path):
                raise DownloadError(
                    "No points to download and no partial file exists "
                    f"(0 points supplied for {output_csv_gz_path})"
                )
            os.rename(file_name_downloading_with_path, file_name_with_path)
        else:
            # Initialize queues
            progress_queue = asyncio.Queue()
            failed_points_queue = asyncio.Queue()

            # Create progress bar
            progress_bar = progress(
                total=len(all_points),
                initial=len(processed_points),
                desc=f"Downloading GSV pano data{f' for {city_label}' if city_label else ''}",
                unit="point",
                # Headless under the scheduler (output goes to a per-attempt log
                # file), and long: a road walk is ~247k requests. Without the
                # tick this writes nothing between its first and last line, and
                # a SIGKILLed child becomes impossible to tell from a slow one.
                logger=logger,
            )

            # Process initial points in batches
            for i in range(0, len(remaining_points), batch_size):
                batch_points = remaining_points[i : i + batch_size]
                api_requests += len(batch_points)
                await process_batch_async(
                    batch_points,
                    api_key,
                    progress_queue,
                    file_name_downloading_with_path,
                    timeout,
                    connection_limit,
                    failed_points_queue,
                    limiter,
                )

                # Update progress bar
                while not progress_queue.empty():
                    await progress_queue.get()
                    progress_bar.update(1)

            # Process failed points with retries. A retryable API status
            # (OVER_QUERY_LIMIT/UNKNOWN_ERROR) means the provider's per-minute
            # quota window needs to reset before we re-request, so sleep
            # BEFORE the pass, not after — a retry fired inside the same
            # exhausted minute just burns an attempt and gets throttled again.
            # Plain network/timeout stragglers (REQUEST_FAILED) only need a
            # short backoff.
            retry_count = 0
            while not failed_points_queue.empty() and retry_count < max_retries:
                retry_count += 1

                # Drain the queue for this pass, preserving each point's
                # failure reason (needed if it stays failed to the very end).
                failed_points = []
                while not failed_points_queue.empty():
                    failed_points.append(await failed_points_queue.get())

                throttled = any(reason in RETRYABLE_STATUSES for *_coords, reason in failed_points)
                delay = (20 * retry_count) if throttled else (2 * retry_count)
                logger.info(
                    f"Retry attempt {retry_count} for {len(failed_points)} failed points; "
                    f"waiting {delay}s first "
                    f"({'quota window reset' if throttled else 'transient backoff'})"
                )
                await asyncio.sleep(delay)

                # process_batch_async takes 4-tuples; strip the reason.
                retry_batch = [(lat, lon, gi, gj) for (lat, lon, gi, gj, _r) in failed_points]
                for start in range(0, len(retry_batch), batch_size):
                    batch_points = retry_batch[start : start + batch_size]
                    api_requests += len(batch_points)
                    await process_batch_async(
                        batch_points,
                        api_key,
                        progress_queue,
                        file_name_downloading_with_path,
                        timeout,
                        connection_limit,
                        failed_points_queue,
                        limiter,
                    )

                while not progress_queue.empty():
                    await progress_queue.get()
                    progress_bar.update(1)

            # Collect any permanently failed points (still failing after every
            # retry pass), preserving each point's reason.
            remaining_failed = []
            while not failed_points_queue.empty():
                remaining_failed.append(await failed_points_queue.get())

            if remaining_failed:
                logger.error(
                    f"Failed to download data for {len(remaining_failed)} points after all retries"
                )
                with open(failed_points_file, "w") as f:
                    f.write("lat,lon,i,j,status\n")  # Write header
                    for lat, lon, i, j, reason in remaining_failed:
                        f.write(f"{lat},{lon},{i},{j},{reason}\n")

            progress_bar.close()

            # Refuse to finalize a run with too many holes (cf. the history
            # harvester's same guard). A heavily throttled/failed run would
            # otherwise register as a clean-looking snapshot that silently
            # lost coverage — invisible to detect_systemic_failure, poison for
            # run-to-run diffs. The .downloading checkpoint holds only terminal
            # rows (failed points are NOT written to it), so a same-date resume
            # re-requests exactly the points that failed.
            if len(remaining_failed) > MAX_FAILED_POINT_FRACTION * len(all_points):
                raise DownloadError(
                    f"{len(remaining_failed)} of {len(all_points)} grid points "
                    f"({len(remaining_failed) / len(all_points):.1%}) still failed after "
                    f"{max_retries} retries (> {MAX_FAILED_POINT_FRACTION:.0%} threshold); "
                    f"refusing to finalize an incomplete snapshot. Partial progress is "
                    f"checkpointed at {file_name_downloading_with_path}; re-running with the "
                    f"SAME --run-date resumes it (a new run date starts fresh — nothing "
                    f"resumes and the full request budget is re-spent)."
                )

            # Under the threshold: write the residual failures back as rows so
            # every grid point is present in the finalized snapshot. Diffs
            # require exact grid-key equality (diff.compute_run_diff); a missing
            # point flips grid_aligned to False and misreports panos sampled
            # only there as removed. Rows carry their true failure status, so
            # they stay out of coverage/pano stats and are findable by
            # scripts/purge_tainted_runs.py.
            if remaining_failed:
                _append_failure_rows(
                    file_name_downloading_with_path,
                    remaining_failed,
                    f"{file_name_downloading_with_path}.lock",
                )

            # Rename the downloading file to final csv
            os.rename(file_name_downloading_with_path, file_name_with_path)

        # Compress the final CSV file
        with open(file_name_with_path, "rb") as f_in:
            with gzip.open(file_name_compressed_with_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

        # Remove the uncompressed CSV file
        os.remove(file_name_with_path)

        # Read the final compressed file
        df = load_city_csv_file(file_name_compressed_with_path)

        end_time = time.time()
        elapsed_time = end_time - start_time
        logger.info(
            f"Downloaded {len(df)} rows in {elapsed_time:.2f} seconds "
            f"({api_requests} API requests this session)"
        )
        logger.info(f"Data compressed and saved to {file_name_compressed_with_path}")

        return {
            "df": df,
            "filename_with_path": file_name_compressed_with_path,
            "api_requests": api_requests,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
        }

    except Exception as e:
        # Attach the request count so the caller can still record spent
        # budget in the api_usage ledger — a failed multi-hour run can have
        # issued 100k+ real (billable) requests.
        error = DownloadError(f"Download failed: {redact_credentials(e)}")
        error.api_requests = api_requests
        raise error from e
    finally:
        run_lock.release()
