import gzip
import hashlib
import json
import logging
import os
from dataclasses import asdict
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from tqdm import tqdm

from . import db, driving_plan, plan_match
from .analysis import (
    PRESENT_STATUSES,
    calculate_coverage_stats,
    calculate_pano_stats,
)
from .fileutils import get_list_of_city_csv_files, load_city_csv_file
from .geoutils import get_city_location_data, get_country_code, get_state_abbreviation
from .naming import parse_filename

logger = logging.getLogger(__name__)


def sanitize_for_json(obj: Any) -> Any:
    """
    Recursively replace NaN/Infinity float values with None so the result
    is valid strict JSON (json.dump with allow_nan=False would otherwise
    raise, and allow_nan=True emits literal NaN which JSON.parse rejects).
    """
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def _write_json_gz_atomic(path: str, payload: Any) -> None:
    """
    Write a ``.json.gz`` via a temp sibling + ``os.replace`` so a crash or
    concurrent reader (including the publish rsync, whose glob skips the
    ``.tmp`` name) never sees a truncated file.

    Creates the parent directory if it is missing. Every caller before the
    driving-plan summary ran only after a collection had already populated
    ``data_dir``, so the directory's existence was incidental rather than
    guaranteed; the plan summary regenerates even on a night that collected
    nothing, which is exactly the case a fresh deployment starts from.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = path + ".tmp"
    with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
        json.dump(sanitize_for_json(payload), f, indent=2, allow_nan=False)
    os.replace(tmp_path, path)


def find_missing_json_files(data_dir: str) -> list[str]:
    """
    Find all csv.gz files that don't have corresponding JSON.gz files.

    Args:
        data_dir: Directory to search for files

    Returns:
        List of paths to csv.gz files needing JSON metadata
    """
    csv_files = get_list_of_city_csv_files(data_dir)

    missing_json = []
    for csv_file in csv_files:
        json_file = csv_file.rsplit(".csv.gz", 1)[0] + ".json.gz"
        if not os.path.exists(json_file):
            missing_json.append(csv_file)

    return missing_json


def generate_missing_city_json_files(data_dir: str) -> None:
    """
    Generate missing JSON metadata files for all csv.gz files in directory.

    This is useful if a .json file was never created for a given city or if
    the .json file needs to be recreated due to changes in analysis code.
    """
    logger.info(f"Scanning {data_dir} for csv.gz files missing JSON metadata...")

    all_csv_files = get_list_of_city_csv_files(data_dir)
    missing_json_files = find_missing_json_files(data_dir)

    if not missing_json_files:
        file_text = "file" if len(all_csv_files) == 1 else "files"
        logger.info(
            f"Found {len(all_csv_files)} csv.gz {file_text}. All csv.gz files already have a corresponding .json metadata file."
        )
        return

    file_text = "file" if len(missing_json_files) == 1 else "files"
    logger.info(
        f"Found {len(missing_json_files)} of {len(all_csv_files)} {file_text} needing a .json metadata file."
    )

    cnt_generated_json_files = 0
    for csv_path in tqdm(missing_json_files, desc="Generating metadata .json files"):
        try:
            params = parse_filename(csv_path)
            city_query_str = params.city_query_str
            search_width = params.width_meters
            search_height = params.height_meters
            step = params.step_meters

            logger.debug(
                f"Parsed filename into city: {city_query_str}, width: {search_width}, height: {search_height}, step: {step}"
            )

            df = load_city_csv_file(csv_path)

            center_lat = float(df["query_lat"].mean())
            center_lon = float(df["query_lon"].mean())

            # Reverse geocode city name with lat,lng as hints
            city_loc_data = get_city_location_data(city_query_str, center_lat, center_lon)

            logger.debug(
                f"Generating .json metadata for {csv_path} at {city_loc_data.city}, {city_loc_data.state}, {city_loc_data.country}"
            )

            generate_city_metadata_summary_as_json(
                csv_gz_path=csv_path,
                df=df,
                city_name=city_loc_data.city,
                state_name=city_loc_data.state,
                country_name=city_loc_data.country,
                grid_width=search_width,
                grid_height=search_height,
                step_length=step,
            )

            logger.debug(
                f"Generated .json metadata for {csv_path} at {city_loc_data.city}, {city_loc_data.state}, {city_loc_data.country}"
            )
            cnt_generated_json_files += 1
        except Exception as e:
            logger.error(f"Error processing {csv_path}: {str(e)}")
            continue

    logger.info(f"Metadata generation completed for {cnt_generated_json_files} file(s).")


def _merge_histogram_into(histogram, accumulator, year_keys: bool) -> None:
    """
    Add one city's capture-date histogram into an accumulator dict.

    Accepts both the current {"counts": {...}} shape and the bare-dict shape
    of older per-run JSONs. Yearly histograms use int keys, daily use ISO
    date strings.
    """
    counts = histogram.get("counts", histogram) if isinstance(histogram, dict) else {}
    for key, count in counts.items():
        if year_keys:
            try:
                key = int(key)
            except (ValueError, TypeError) as e:
                logger.warning(f"Error converting year '{key}' to integer: {e}")
                continue
        accumulator[key] = accumulator.get(key, 0) + count


def merge_capture_date_histograms(cities_data: list[dict]) -> dict[str, dict[int | str, int]]:
    """
    Merge yearly and daily histograms from multiple cities' per-run JSONs.

    The google_panos section is optional (absent for non-GSV providers and
    merged only when present); cities missing the all_panos histograms
    (very old schema) are skipped with a warning.

    Returns:
        Dict with all_panos_yearly / google_panos_yearly / all_panos_daily /
        google_panos_daily merged histograms. Yearly histograms use integer
        years as keys, daily histograms use ISO date strings.
    """
    logger.debug(f"Merging capture date histograms for {len(cities_data)} cities")

    merged = {
        "all_panos_yearly": {},
        "google_panos_yearly": {},
        "all_panos_daily": {},
        "google_panos_daily": {},
    }

    skipped_cities = []
    for city_data in cities_data:
        city_name = f"{city_data.get('city', {}).get('name', 'unknown')}, {city_data.get('city', {}).get('state', {}).get('abbreviation', '??')}"
        logger.debug(f"Merging histograms for {city_name}")

        all_panos = city_data.get("all_panos", {})
        missing_fields = [
            f"all_panos.{f}"
            for f in ("histogram_of_capture_dates_by_year", "histogram_of_capture_dates")
            if f not in all_panos
        ]
        if missing_fields:
            logger.warning(f"Skipping {city_name}: missing fields: {', '.join(missing_fields)}")
            skipped_cities.append((city_name, missing_fields))
            continue

        _merge_histogram_into(
            all_panos["histogram_of_capture_dates_by_year"],
            merged["all_panos_yearly"],
            year_keys=True,
        )
        _merge_histogram_into(
            all_panos["histogram_of_capture_dates"], merged["all_panos_daily"], year_keys=False
        )

        google_panos = city_data.get("google_panos")
        if google_panos:
            _merge_histogram_into(
                google_panos.get("histogram_of_capture_dates_by_year", {}),
                merged["google_panos_yearly"],
                year_keys=True,
            )
            _merge_histogram_into(
                google_panos.get("histogram_of_capture_dates", {}),
                merged["google_panos_daily"],
                year_keys=False,
            )

    if skipped_cities:
        logger.warning(f"\n{'=' * 60}")
        logger.warning(f"Skipped {len(skipped_cities)} cities with outdated JSON schema:")
        for name, fields in skipped_cities:
            logger.warning(f"  {name}: missing {', '.join(fields)}")
        logger.warning("To fix: delete their .json.gz files and rerun generate_json.py")
        logger.warning(f"{'=' * 60}\n")

    return {key: dict(sorted(value.items())) for key, value in merged.items()}


def compute_mapillary_meta(df: pd.DataFrame) -> dict[str, Any] | None:
    """
    Lightweight summary of the free per-image Mapillary metadata (issue: capture
    all free tile metadata), for ranking candidate cities at a glance without
    re-parsing the CSV.

    Computed over the 360-degree pano census (rows with status OK or NO_DATE) —
    the subset relevant to Project Sidewalk viability:
        n_images            pano rows
        n_distinct_orgs     distinct non-null organization_id (systematic
                            city-wide programs: municipal fleets, scooter sweeps)
        pct_with_org        % of panos attributed to an organization
        pct_on_foot         % of panos captured on foot (vs vehicle)
        median_quality_score median Mapillary quality_score (0-1)

    Returns None for a legacy Mapillary file that predates the enriched schema
    (the extra columns are absent) or a run with no pano rows, so callers can
    simply omit the block.
    """
    if "organization_id" not in df.columns:
        return None
    panos = df[df["status"].isin(("OK", "NO_DATE"))]
    n = int(len(panos))
    if n == 0:
        return None

    org = panos["organization_id"]
    on_foot = panos["on_foot"]
    quality = panos["quality_score"]

    n_with_org = int(org.notna().sum())
    n_foot_known = int(on_foot.notna().sum())
    median_quality = quality.median()  # skips NA; NA if all missing

    return {
        "n_images": n,
        "n_distinct_orgs": int(org.nunique(dropna=True)),
        "pct_with_org": round(100.0 * n_with_org / n, 1),
        "pct_on_foot": (
            round(100.0 * int((on_foot == True).sum()) / n_foot_known, 1)  # noqa: E712
            if n_foot_known
            else None
        ),
        "median_quality_score": (
            None if pd.isna(median_quality) else round(float(median_quality), 3)
        ),
    }


def generate_city_metadata_summary_as_json(
    csv_gz_path: str,
    df: pd.DataFrame,
    city_name: str,
    state_name: str,
    country_name: str,
    grid_width: float,
    grid_height: float,
    step_length: float,
    force_recreate_file: bool = False,
    run_date: Any | None = None,
    is_baseline: bool = False,
    change_from_previous_run: dict[str, Any] | None = None,
    provider: str = "gsv",
) -> str:
    """
    Generate and save download statistics for an individual city run to a
    compressed JSON file (schema v2).

    Returns the .json.gz filename with path

    Args:
        csv_gz_path: Full path to the compressed CSV file (including filename)
        df: DataFrame containing the run data
        city_name: Name of the city
        state_name: Name of the state (if one exists)
        country_name: Name of the country
        grid_width: Width of search grid in meters
        grid_height: Height of search grid in meters
        step_length: Distance between sample points in meters
        force_recreate_file: forces the recreation of the .json file (defaults False)
        run_date: datetime.date of the collection run. Age statistics are
            computed relative to this date (so regeneration is deterministic
            and cross-run age comparisons are meaningful). When None, ages
            fall back to generation wall-clock time.
        is_baseline: True for legacy pre-temporal-tracking snapshots
        change_from_previous_run: summary dict of the diff vs the previous
            run (see cli.py), or None for a city's first run
        provider: imagery provider. GSV runs additionally get the
            'google_panos' block (the Google-copyright subset of all_panos);
            for other providers all rows are already provider imagery, so
            only 'all_panos' is emitted.
    """
    logger.debug(
        f"Generating metadata summary for {city_name}, {state_name}, {country_name} from {csv_gz_path}"
    )

    # Generate JSON.gz path by replacing .csv.gz extension with .json.gz
    json_filename_with_path = csv_gz_path.rsplit(".csv.gz", 1)[0] + ".json.gz"

    if os.path.exists(json_filename_with_path) and not force_recreate_file:
        logger.info(f"JSON.gz file already exists: {json_filename_with_path}; returning...")
        return json_filename_with_path

    # Calculate center coordinates from query points
    center_lat = float(df["query_lat"].mean())
    center_lon = float(df["query_lon"].mean())

    # Calculate ranges to verify grid dimensions
    diagonal_meters = np.sqrt(grid_width**2 + grid_height**2)

    # Calculate extents
    query_bounds = {
        "min_lat": float(df["query_lat"].min()),
        "max_lat": float(df["query_lat"].max()),
        "min_lon": float(df["query_lon"].min()),
        "max_lon": float(df["query_lon"].max()),
    }

    # Get start and end times from query_timestamp
    df["query_timestamp_converted"] = pd.to_datetime(df["query_timestamp"], errors="coerce")
    problematic_timestamps = df[df["query_timestamp_converted"].isna()]

    if len(problematic_timestamps) > 0:
        logger.warning(f"\nFound {len(problematic_timestamps)} problematic timestamps:")
        logger.warning("\nOriginal problematic values:")
        for idx, row in problematic_timestamps.iterrows():
            logger.warning(f"Row {idx}: {row['query_timestamp']}")
    else:
        logger.debug(f"All timestamps converted successfully in {csv_gz_path}!")

    start_time = df["query_timestamp_converted"].min()
    end_time = df["query_timestamp_converted"].max()

    try:
        duration = end_time - start_time
        duration_seconds = duration.total_seconds()
        logger.debug(f"Duration: {duration_seconds:.2f} seconds")
    except Exception as e:
        logger.error(f"Error calculating duration: {str(e)}")
        duration_seconds = None

    # Ages are pinned to run_date when known so the output is deterministic
    now = pd.Timestamp(run_date) if run_date is not None else pd.Timestamp.now()

    # Calculate all pano statistics. Archival GSV imports (issue #93) never
    # captured copyright_info; for those runs the Google subset is unknown,
    # so the google_panos block is omitted and a flag records why.
    all_pano_stats = calculate_pano_stats(df, now)
    gsv_copyright_available = True
    if provider == "gsv":
        present_rows = df[df["status"].isin(PRESENT_STATUSES)]
        gsv_copyright_available = len(present_rows) == 0 or bool(
            present_rows["copyright_info"].notna().any()
        )
    google_pano_stats = (
        calculate_pano_stats(df, now, google_only=True)
        if provider == "gsv" and gsv_copyright_available
        else None
    )

    # Calculate coverage statistics
    coverage_stats = calculate_coverage_stats(df)

    top_10_photographers = dict(
        sorted(
            all_pano_stats.photographer_stats.photographer_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
    )

    metadata = {
        "schema_version": 2,
        "provider": provider,
        "run": {
            "run_date": (run_date.isoformat() if run_date is not None else None),
            "is_baseline": is_baseline,
        },
        "change_from_previous_run": change_from_previous_run,
        "data_file": {
            "filename": os.path.basename(csv_gz_path),
            "format": "csv.gz",
            "rows": len(df),
            "size_bytes": os.path.getsize(csv_gz_path),
        },
        "city": {
            "name": city_name,
            "state": {"name": state_name, "code": get_state_abbreviation(state_name)},
            "country": {"name": country_name, "code": get_country_code(country_name)},
            "center": {"latitude": center_lat, "longitude": center_lon},
            "bounds": query_bounds,
        },
        "search_grid": {
            "width_meters": grid_width,
            "height_meters": grid_height,
            "step_length_meters": step_length,
            "diagonal_meters": diagonal_meters,
            # Unique query points, not len(df): Mapillary runs have one row
            # per pano, so several rows can share a grid point
            "total_search_points": int(df[["query_lat", "query_lon"]].drop_duplicates().shape[0]),
            "area_km2": (grid_width * grid_height) / 1_000_000,
        },
        "download": {
            "start_time": start_time.isoformat() if start_time is not None else None,
            "end_time": end_time.isoformat() if end_time is not None else None,
            "duration_seconds": duration_seconds,
        },
        "coverage": asdict(coverage_stats),
        "all_panos": {
            "duplicate_stats": asdict(all_pano_stats.duplicate_stats),
            "age_stats": asdict(all_pano_stats.age_stats),
            "histogram_of_capture_dates_by_year": asdict(all_pano_stats.yearly_distribution),
            "histogram_of_capture_dates": asdict(all_pano_stats.daily_distribution),
            "top_10_photographers": top_10_photographers,
        },
    }
    if provider == "gsv":
        metadata["copyright_info_available"] = gsv_copyright_available
    if google_pano_stats is not None:
        metadata["google_panos"] = {
            "duplicate_stats": asdict(google_pano_stats.duplicate_stats),
            "age_stats": asdict(google_pano_stats.age_stats),
            "histogram_of_capture_dates_by_year": asdict(google_pano_stats.yearly_distribution),
            "histogram_of_capture_dates": asdict(google_pano_stats.daily_distribution),
        }
    if provider == "mapillary":
        mapillary_meta = compute_mapillary_meta(df)
        if mapillary_meta is not None:
            metadata["mapillary_meta"] = mapillary_meta

    # Save compressed JSON (atomic; sanitized — NaN is not valid JSON)
    _write_json_gz_atomic(json_filename_with_path, metadata)

    logger.info(f"Saved compressed JSON to: {json_filename_with_path}")
    return json_filename_with_path


def regenerate_run_json(conn, run_id: int, data_dir: str) -> str | None:
    """
    Rebuild the per-run summary JSON for an already-cataloged run from its CSV
    and set ``runs.json_filename`` to match.

    Recovery path for a run whose row was committed (``register_run``) but whose
    pipeline tail was interrupted before the JSON was written — e.g. a scheduler
    subprocess SIGKILLed a few seconds after the download finished but during
    the diff/JSON step, leaving a valid run with ``json_filename = NULL`` that
    the aggregate then skips. The scheduler calls this to self-heal (see
    ``cmd_run_due``); it is also usable as a manual repair.

    Reuses the exact functions the live pipeline uses
    (``generate_city_metadata_summary_as_json``), so the output is identical to
    a normal run's JSON. The diff change-block is intentionally omitted
    (``change_from_previous_run=None``): it is cosmetic and self-heals on the
    city's next run, and recomputing it would re-load the previous run's CSV —
    the very cost that caused the interruption.

    Returns the JSON basename, or ``None`` if the run's CSV is missing on disk.
    """
    from datetime import date

    row = conn.execute(
        "SELECT run_id, city_id, provider, run_date, csv_filename, is_baseline "
        "FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        logger.warning(f"regenerate_run_json: run {run_id} not found")
        return None

    csv_path = os.path.join(data_dir, row["csv_filename"])
    if not os.path.exists(csv_path):
        logger.warning(f"regenerate_run_json: CSV missing, cannot rebuild: {csv_path}")
        return None

    city = db.resolve_city(conn, row["city_id"])
    if city is None:
        logger.warning(f"regenerate_run_json: city {row['city_id']} unresolvable")
        return None

    df = load_city_csv_file(csv_path)
    y, m, d = (int(x) for x in row["run_date"].split("-"))
    json_path = generate_city_metadata_summary_as_json(
        csv_path,
        df,
        city.city_name,
        city.state_name,
        city.country_name,
        city.grid_width_m,
        city.grid_height_m,
        city.step_m,
        force_recreate_file=True,
        run_date=date(y, m, d),
        is_baseline=bool(row["is_baseline"]),
        change_from_previous_run=None,
        provider=row["provider"],
    )
    basename = os.path.basename(json_path)
    db.update_run_json_filename(conn, run_id, basename)
    logger.info(f"regenerate_run_json: rebuilt {basename} for run {run_id}")
    return basename


def _load_city_json(json_path: str) -> dict[str, Any] | None:
    """Load a per-run city json.gz, returning None on any failure."""
    try:
        with gzip.open(json_path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading {json_path}: {e}")
        return None


def _build_provider_summary(runs, latest_json, data_dir, conn) -> dict[str, Any]:
    """
    Build one provider's {latest, runs, change} block for a city's
    aggregate record. `runs` is that provider's run series (oldest first)
    and `latest_json` the loaded per-run JSON of its newest run.
    """
    from . import db  # local import to keep module import order simple

    latest = runs[-1]
    csv_path = os.path.join(data_dir, latest.csv_filename)
    csv_size = os.path.getsize(csv_path) if os.path.exists(csv_path) else None

    panorama_counts = {
        "unique_panos": latest_json["all_panos"]["duplicate_stats"]["total_unique_panos"],
    }
    histograms_by_year = {
        "all_panos": latest_json["all_panos"]["histogram_of_capture_dates_by_year"],
    }
    latest_block = {
        "run_date": latest.run_date,
        "is_baseline": latest.is_baseline,
        "data_file": {
            "filename": latest.csv_filename,
            "size_bytes": csv_size,
        },
        "json_file": latest.json_filename,
        "search_area_km2": latest_json["search_grid"]["area_km2"],
        # The grid's size in sample points, and the geometry that produced it.
        # coverage_rate_percent is a share OF these points, so without the
        # denominator a reader cannot tell a 40% built from 1,681 points from a
        # 40% built from 2 million — the difference between a village and a
        # metro.
        #
        # Indexed, not `.get()`-guarded, exactly like search_area_km2 above:
        # all four keys are written by one dict literal in
        # generate_city_metadata_summary_as_json and have coexisted since the
        # file's earliest tracked form, so a search_grid missing one is a
        # corrupt JSON worth failing on, not a generation to tolerate. A guard
        # here would also emit {width: null, height: null, step: null} — a
        # truthy all-null block that no `if (rec.grid)` consumer can reject,
        # which is precisely what the absent-not-null convention exists to
        # avoid (see _trim_coverage_by_highway below).
        #
        # NOTE: this is the LATEST RUN's grid, not the city's current frozen
        # geometry — the two diverge for cities resized catalog-only by
        # scripts/cap_oversized_grids.py (#166) until their next collection.
        # That is the right pairing here: total_search_points is this run's
        # denominator, so it must travel with the geometry that produced it.
        "total_search_points": latest_json["search_grid"]["total_search_points"],
        "grid": {
            "width_meters": latest_json["search_grid"]["width_meters"],
            "height_meters": latest_json["search_grid"]["height_meters"],
            "step_length_meters": latest_json["search_grid"]["step_length_meters"],
        },
        # From the DB, not the per-run JSON: the DB holds the
        # points-with-pano coverage definition for every generation
        # (issue #90), while per-run JSONs written before the fix may
        # carry the briefly-used unique-pano rate until regenerated
        "coverage_rate_percent": latest.coverage_rate_pct,
        # Any-imagery (360° + flat) coverage, Mapillary-only signal (issue
        # #116). For GSV and pre-v7 runs this equals coverage_rate_percent;
        # NULL falls back to the 360° rate at read time in the frontend.
        "any_imagery_coverage_rate_percent": latest.any_imagery_coverage_rate_pct,
        "num_flat_images": latest.num_flat_images,
        "panorama_counts": panorama_counts,
        "all_panos_age_stats": latest_json["all_panos"]["age_stats"],
        "collection_info": latest_json["download"],
        "histogram_of_capture_dates_by_year": histograms_by_year,
    }
    # GSV runs carry the Google-copyright breakdown; other providers don't.
    # Archival GSV imports flag copyright_info_available=false instead and
    # omit google_panos (frontends fall back to all-pano stats).
    if "copyright_info_available" in latest_json:
        latest_block["copyright_info_available"] = latest_json["copyright_info_available"]
    google_panos = latest_json.get("google_panos")
    if google_panos:
        panorama_counts["unique_google_panos"] = google_panos["duplicate_stats"][
            "total_unique_panos"
        ]
        latest_block["google_panos_age_stats"] = google_panos["age_stats"]
        histograms_by_year["google_panos"] = google_panos["histogram_of_capture_dates_by_year"]

    # Change summary vs the previous run (None for the first run)
    change = None
    diff_row = db.get_diff_for_run(conn, latest.run_id)
    if diff_row is not None and len(runs) >= 2:
        change = {
            "from": runs[-2].run_date,
            "to": latest.run_date,
            "panos_added": diff_row["panos_added"],
            "panos_removed": diff_row["panos_removed"],
            "capture_date_changed": diff_row["capture_date_changed"],
            "coverage_delta_pct": diff_row["coverage_delta_pct"],
            "diff_file": diff_row["detail_filename"],
        }

    return {
        "latest": latest_block,
        "runs": [
            {
                "run_date": r.run_date,
                "is_baseline": r.is_baseline,
                "data_file": r.csv_filename,
                "json_file": r.json_filename,
                "unique_panos": r.unique_panos,
                "unique_google_panos": r.unique_google_panos,
                "coverage_rate_percent": r.coverage_rate_pct,
                "any_imagery_coverage_rate_percent": r.any_imagery_coverage_rate_pct,
                "median_pano_age_years": r.median_pano_age_years,
            }
            for r in runs
        ],
        "change": change,
    }


def generate_aggregate_v2(conn, data_dir: str) -> dict[str, Any]:
    """
    Generate the aggregate cities.json.gz (schema v3) from the SQLite
    catalog. (The function name predates the provider dimension; it is the
    catalog-driven successor to the legacy directory-scan aggregate.)

    One entry per city, grouped by provider:

        { "city_id": ..., "city": {...},
          "providers": {
              "gsv":       { "latest": {...}, "runs": [...], "change": {...} },
              "mapillary": { ... } } }

    Each provider block has a `latest` summary for the map display, a slim
    `runs[]` history, and a `change` block summarizing the diff between the
    provider's two most recent runs. Global capture-date histograms are
    keyed by provider and merge each city's LATEST run only (so re-running
    a city never double-counts).

    Args:
        conn: open catalog connection (db.connect)
        data_dir: directory holding the per-run json.gz files; the aggregate
            is written here as cities.json.gz

    Returns:
        The aggregate summary dict.
    """
    from . import db  # local import to keep module import order simple

    cities_out = []
    # Raw per-run JSON of each city's latest run per provider, for the merge
    latest_run_jsons_by_provider: dict[str, list[dict]] = {}

    for city in tqdm(db.get_all_cities(conn), desc="Aggregating cities", unit="city"):
        runs_by_provider: dict[str, list] = {}
        for run in db.get_runs_for_city(conn, city.city_id, provider=None):
            runs_by_provider.setdefault(run.provider, []).append(run)

        providers_out = {}
        city_block = None
        for provider in sorted(runs_by_provider, key=lambda p: p != "gsv"):
            runs = runs_by_provider[provider]
            latest = runs[-1]
            latest_json = None
            json_filename = latest.json_filename
            if not json_filename:
                # A crash between register_run and update_run_json_filename
                # leaves json_filename NULL even though the sibling file may
                # exist (or be regenerated later by
                # generate_missing_city_json_files). Fall back to the
                # derived name so one bad night doesn't drop the provider
                # from the aggregate forever.
                derived = latest.csv_filename.rsplit(".csv.gz", 1)[0] + ".json.gz"
                if os.path.exists(os.path.join(data_dir, derived)):
                    logger.warning(
                        f"{city.city_id} [{provider}]: run has no cataloged "
                        f"json_filename; using derived sibling {derived}"
                    )
                    json_filename = derived
            if json_filename:
                latest_json = _load_city_json(os.path.join(data_dir, json_filename))
            if latest_json is None:
                logger.warning(
                    f"Skipping {city.city_id} [{provider}]: missing/unreadable "
                    f"per-run JSON ({json_filename or latest.json_filename})"
                )
                continue
            providers_out[provider] = _build_provider_summary(runs, latest_json, data_dir, conn)
            latest_run_jsons_by_provider.setdefault(provider, []).append(latest_json)
            if city_block is None:  # gsv first, so GSV's city block wins
                city_block = latest_json["city"]

        if not providers_out:
            continue
        cities_out.append(
            {
                "city_id": city.city_id,
                "city": city_block,
                "providers": providers_out,
            }
        )

    merged_histograms = {
        provider: merge_capture_date_histograms(jsons)
        for provider, jsons in sorted(latest_run_jsons_by_provider.items())
    }

    summary = {
        "schema_version": 3,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "cities_count": len(cities_out),
        "histogram_of_capture_dates": merged_histograms,
        "cities": cities_out,
    }

    output_path = os.path.join(data_dir, "cities.json.gz")
    _write_json_gz_atomic(output_path, summary)
    logger.info(f"Wrote v3 aggregate for {len(cities_out)} cities to {output_path}")

    return summary


# Per-highway-class fields the streets page renders, in display order. The
# stored breakdown carries more (edges_sampled, edges_any_coverage,
# mean_edge_coverage, coverage_pct_by_count on the grid-attribution variant),
# none of which any consumer reads — and the manifest is fetched by every
# visitor, so it carries only what is used. The catalog keeps the full block.
#
# Extending this list is cheap and needs no re-collection: the catalog already
# holds every field, so `scheduler regenerate-aggregate --publish` rebuilds the
# manifest from it with no API calls. Trim aggressively here rather than
# publishing fields speculatively against an unwritten frontend.
_PUBLISHED_HIGHWAY_FIELDS = (
    "edges",
    "length_km",
    "length_km_covered",
    "length_km_covered_any",
    "coverage_pct_by_length",
    "coverage_pct_by_length_any",
    "median_covered_age_years",
)


def _trim_coverage_by_highway(raw: str | None) -> dict[str, dict[str, Any]] | None:
    """
    Parse the stored ``coverage_by_highway`` JSON down to the published fields.

    Returns None for a NULL column, unparseable JSON, or an empty breakdown, so
    the caller can omit the key entirely rather than publishing a null. Bucket
    ORDER is preserved: the artifact's key order is the road → service →
    non-motorized hierarchy (`_BUCKET_DISPLAY_ORDER` in
    streetscape_street_analyzer/street_coverage.py) and the frontend legend
    trusts it. Per-bucket fields absent from an older artifact are simply not
    emitted, the same "not measured" convention used elsewhere.

    Args:
        raw: the street_walks.coverage_by_highway TEXT value, or None.

    Returns:
        {bucket: {field: value}} in stored order, or None.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # A malformed breakdown is a bad row, not a bad manifest: the walk's
        # headline stats are still sound, so publish the entry without it.
        logger.warning("Skipping unparseable coverage_by_highway in streetwalk manifest")
        return None
    if not isinstance(parsed, dict):
        return None
    trimmed = {}
    for bucket, block in parsed.items():
        if not isinstance(block, dict):
            continue
        trimmed[bucket] = {f: block[f] for f in _PUBLISHED_HIGHWAY_FIELDS if f in block}
    return trimmed or None


def generate_streetwalk_manifest(conn, data_dir: str) -> dict[str, Any]:
    """
    Build and write ``streetwalks.json.gz`` — a small sidecar index of the
    latest road-walk street-coverage artifact per (city, provider, network
    type), from the ``street_walks`` catalog table (issue #155).

    The city page needs this because the streetwalk coverage GeoJSON is NOT a
    sibling of the grid run file (its ``sp{N}`` spacing is a free parameter and
    its run-date differs from the grid run's), so the frontend cannot derive the
    artifact URL the way it derives ``_streets.json.gz``. The manifest maps
    (city_id, provider, network_type) → the exact ``coverage_filename`` plus
    headline stats.

    Kept deliberately separate from the aggregate ``cities.json.gz`` (schema v3):
    the aggregate contract stays untouched, and #102 later folds these stats in
    properly. Published automatically by the ``*.json.gz`` publish glob.

    ``schema_version`` stays 1 across the v12 additions (``length_km``,
    ``length_km_covered``, ``length_km_covered_any``,
    ``median_covered_age_years`` and the optional ``coverage_by_highway``
    block) because every one of them is additive — no existing key changed
    shape or meaning. Consumers must therefore treat a missing/NULL length as
    "this walk has no length cataloged", NOT as "this manifest predates
    lengths": a v1 manifest can legitimately contain both, since a walk
    cataloged before schema v12 reads NULL until
    ``scripts/backfill_streetwalk_length.py`` runs.

    Empty catalog → ``walks: []`` (the file is still written so the frontend
    fetch succeeds and simply renders no streetwalk overlays).

    Args:
        conn: open catalog connection (db.connect).
        data_dir: directory the manifest is written to (alongside cities.json.gz).

    Returns:
        The manifest dict.
    """
    from . import db  # local import to keep module import order simple

    walks = []
    for row in db.get_latest_street_walks_all(conn):
        pct = row["coverage_pct_by_length"]
        entry = {
            "city_id": row["city_id"],
            "provider": row["provider"],
            # Which OSM network was walked. 'drive' (motorized roads only)
            # is the original and still-scheduled series; 'all_public' adds
            # alleys, footways, park paths, cycleways and steps. A city can
            # have an entry for each, so consumers must select on this —
            # the frontend defaults to 'drive'.
            "network_type": row["network_type"],
            "coverage_filename": row["coverage_filename"],
            "run_date": row["run_date"],
            "spacing_m": row["spacing_m"],
            "match_dist_m": row["match_dist_m"],
            "coverage_pct_by_length": pct,
            # Any-imagery street coverage (360° + flat). NULL on walks
            # predating the column — deliberately NOT defaulted to `pct`, so
            # the streets page can show "no data" instead of implying the
            # flat footprint was measured.
            "coverage_pct_by_length_any": row["coverage_pct_by_length_any"],
            # street_walks has no uncovered column; derive it so the frontend
            # headline ("X% of street-km have no imagery") needs no math.
            "uncovered_pct_by_length": None if pct is None else round(100 - pct, 1),
            "edges": row["edges_total"],
            "edges_fully_covered": row["edges_fully_covered"],
            "mean_edge_coverage": row["mean_edge_coverage"],
            # Absolute street length (schema v12). The percentages above are
            # shares; these are the kilometres a deployment estimate or a paper
            # actually quotes, and no percentage recovers them without the
            # denominator. NULL on walks cataloged before v12 and not yet
            # backfilled (scripts/backfill_streetwalk_length.py).
            "length_km": row["length_km"],
            "length_km_covered": row["length_km_covered"],
            "length_km_covered_any": row["length_km_covered_any"],
            # Median age of the imagery covering this walk's streets. Not
            # derivable from the per-bucket medians below (a median of medians
            # is not the median), which is why it is stored and published.
            "median_covered_age_years": row["median_covered_age_years"],
        }
        # Per-highway-class breakdown (residential vs footway vs alley …), the
        # cut that answers "is there imagery where pedestrians actually walk".
        # Key ABSENT (not null) when the column is NULL, matching the `change`
        # block below: an additive key should not sprinkle nulls through every
        # entry that predates it. Trimmed to the fields the frontend renders —
        # the full block carries per-class edge counts and sampled/any-coverage
        # tallies that no consumer reads, and this file is fetched by every
        # visitor to the streets page.
        breakdown = _trim_coverage_by_highway(row["coverage_by_highway"])
        if breakdown:
            entry["coverage_by_highway"] = breakdown
        # "Since the last walk" change block (issue #101), from the walk-diff
        # catalog like the aggregate's change key. Key ABSENT (not null) when
        # no diff exists — most walks are still first walks, and an additive
        # manifest key shouldn't sprinkle nulls into every prior entry.
        diff_row = db.get_walk_diff_for_walk(conn, row["walk_id"])
        if diff_row is not None:
            entry["change"] = {
                "from": diff_row["from_run_date"],
                "to": row["run_date"],
                "edges_gained_coverage": diff_row["edges_gained_coverage"],
                "edges_lost_coverage": diff_row["edges_lost_coverage"],
                "coverage_pct_by_length_delta": diff_row["coverage_pct_by_length_delta"],
                "coverage_pct_by_length_any_delta": diff_row["coverage_pct_by_length_any_delta"],
                "nearest_pano_date_changed": diff_row["nearest_pano_date_changed"],
                "diff_file": diff_row["detail_filename"],
            }
        walks.append(entry)

    manifest = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "walks": walks,
    }

    output_path = os.path.join(data_dir, "streetwalks.json.gz")
    _write_json_gz_atomic(output_path, manifest)
    logger.info(f"Wrote streetwalk manifest for {len(walks)} walks to {output_path}")

    return manifest


# ── Driving plan × observed imagery (issue #176 payoff) ────────────────────

# The feed's own caveat, reproduced on the page so no reader mistakes an absent
# row for a guarantee. Google's wording, not ours.
_PLAN_DISCLAIMER = (
    "Google notes that listed regions “may include smaller cities and towns "
    "within driving distance”, so absence from this plan is not a guarantee "
    "that an area will not be driven."
)

# How many plan revisions the artifact publishes. The archive gains roughly one
# a week and this file is fetched by every visitor, so the log is a recent
# history rather than a complete one; the catalog keeps every snapshot.
_MAX_REVISIONS = 26


def _clean(value: Any) -> Any:
    """Strip surrounding whitespace from a feed string, leaving None alone."""
    return value.strip() if isinstance(value, str) else value


def _record_id(key: tuple) -> str:
    """
    A stable, unique id for a feed record.

    The table sorts with a tiebreak on the row's id, so plan-area rows need one
    that survives regeneration — an ordinal would shift every time Google adds
    a record. Derived from the record's own identity, so it changes only when
    the record does, and prefixed to keep it obviously distinct from a
    ``city_id`` in a URL or a bug report.
    """
    digest = hashlib.sha1("\x1f".join(str(part) for part in key).encode("utf-8")).hexdigest()
    return f"plan:{digest[:12]}"


def _capture_year_counts(block: Any) -> dict[str, Any] | None:
    """
    The year → count map out of a per-run JSON's capture-year histogram,
    tolerating both shapes the archive contains.

    Per-run JSONs come in two generations and BOTH are on disk:

        newer:  {"histogram_of_capture_dates_by_year": {"counts": {"2019": 12}}}
        older:  {"histogram_of_capture_dates_by_year": {"2008": 3, "2012": 566}}

    Reading only the nested form silently dropped the capture history for 178
    of 1,144 catalogued cities (15%) — invisible in tests and fixtures, which
    are all written in the current shape, and only apparent when the page was
    rendered against the real archive. Files are immutable dated snapshots, so
    the old shape is never going away and must be read, not migrated.

    Disambiguated by the ``counts`` key, which cannot collide with a year.
    """
    if not isinstance(block, dict):
        return None
    counts = block.get("counts") if "counts" in block else block
    return counts if isinstance(counts, dict) else None


def _compact_capture_years(counts: dict[str, Any] | None) -> list[Any] | None:
    """
    A capture-year histogram as ``[first_year, [counts…]]``, or None.

    The per-run JSON stores ``{"2018": 1, "2020": 1, "2024": 1}``. Published
    verbatim for 1,214 cities that repeats a four-character year key thousands
    of times; the dense form names the first year once and lets position carry
    the rest, with explicit zeros for years that saw no capture (which are
    themselves meaningful — a gap between drives).

    Source is deliberately the per-run JSON's ``google_panos`` block, which is
    already filtered to official ``© Google`` imagery. That makes the sparkline
    *more* trustworthy than the ``newest_capture_date`` column beside it, since
    issue #213's corrupt third-party EXIF only contaminates the ``all_panos``
    path.
    """
    if not counts:
        return None
    years = []
    for raw, count in counts.items():
        try:
            years.append((int(raw), int(count)))
        except (TypeError, ValueError):
            continue
    if not years:
        return None
    years.sort()
    first, last = years[0][0], years[-1][0]
    # A corrupt year (issue #213 territory) would otherwise stretch the array to
    # hundreds of buckets; clamp to a span that can only be real imagery.
    if last - first > 40:
        return None
    dense = [0] * (last - first + 1)
    for year, count in years:
        dense[year - first] += count
    return [first, dense]


def _observation_block(run: Any, today: date) -> dict[str, Any]:
    """
    One provider's observed imagery for a city, as the page renders it.

    ``newest_capture`` is filtered through ``plan_match.plausible_capture_date``
    rather than copied: the catalog's capture-date columns are computed over
    every pano in a run, including third-party photospheres whose EXIF can be
    corrupt, so 21 production runs currently read as captured in 2611-2612 and
    75 as captured before Street View existed. Publishing those would put an
    absurd date on the page and manufacture a ``driven_unplanned`` verdict —
    the exact claim this page exists to make — out of a typo.
    """
    newest = plan_match.plausible_capture_date(run["newest_capture_date"], today)
    block: dict[str, Any] = {
        "run_date": run["run_date"],
        # The city page is addressed by run filename (city.html?file=…), not by
        # city_id, so a row cannot link out without carrying it.
        "csv_filename": run["csv_filename"],
        "coverage_rate_pct": run["coverage_rate_pct"],
        "any_imagery_coverage_rate_pct": run["any_imagery_coverage_rate_pct"],
        "newest_capture": newest.isoformat() if newest else None,
        "median_pano_age_years": run["median_pano_age_years"],
    }
    # Years since the newest capture — the "who is overdue" sort. Derived here
    # rather than in the browser so the page and any downstream analysis agree
    # on the arithmetic.
    if newest is not None:
        block["years_since_newest_capture"] = round((today - newest).days / 365.25, 2)
    # gsv only: the official-Google pano count is the drive-imagery magnitude.
    # NULL for other providers by construction, so omit rather than publish a
    # null that reads as "measured, and zero".
    if run["unique_google_panos"] is not None:
        block["google_panos"] = run["unique_google_panos"]
    # "Did the imagery actually refresh since last time?" Absent (not null)
    # when this run has no diff — a first run has not failed to change, it has
    # nothing to be compared against.
    if run["diff_from_run_date"] is not None:
        block["change"] = {
            "from": run["diff_from_run_date"],
            "capture_date_changed": run["capture_date_changed"],
            "coverage_delta_pct": run["coverage_delta_pct"],
            "panos_added": run["panos_added"],
            "panos_removed": run["panos_removed"],
        }
    return block


def _plan_block(summary: plan_match.PlanSummary, tier: str) -> dict[str, Any]:
    """Google's published plan for one city, reduced to what the page shows."""
    block: dict[str, Any] = {
        "match_tier": tier,
        "entry_count": summary.entry_count,
        "active_count": summary.active_count,
        "window_start": summary.window_start,
        "window_end": summary.window_end,
    }
    # Marks a window whose bounds came from the day-first reading of a dirty
    # raw value rather than the feed's own ISO date. Absent when every bound
    # was clean, so the page only has to render the caveat where it applies.
    if summary.approximate:
        block["window_approximate"] = True
    # A handful of district names, enough to show WHY a city matched without
    # shipping all 91 counties of Idaho to every visitor.
    if summary.districts:
        block["districts"] = summary.districts[:8]
        if len(summary.districts) > 8:
            block["districts_total"] = len(summary.districts)
    return block


def generate_driving_plan_summary(conn, data_dir: str) -> dict[str, Any]:
    """
    Build and write ``driving_plan.json.gz`` — Google's published driving plan
    joined against the imagery we have actually observed (issue #176).

    A normal run records where imagery IS; the plan records where Google says
    it is GOING. Neither is trustworthy alone, which is the whole point: the
    feed's Israeli rows all read ``publish=No`` with 2018-19 windows, while our
    own runs record captures in 2023-09 and 2023-10. Google drove Israel four
    years after the feed said the campaign closed and never revised it. So a
    ``closed`` or ``not_listed`` verdict must never be read as "not driven" —
    the page says so, and ``driven_unplanned`` names that case outright.

    **This is the one artifact derived from a third party's content.** The raw
    feed stays unpublished in ``archive/gsv_driving_plan/``; what ships here is
    a join keyed by our own cities, carrying only the plan fields the verdict
    rests on, with attribution and the source URL. See the module docstring of
    ``driving_plan.py`` for why the raw archive stays out of ``data/``.

    Two collections, because the page answers two questions:

    * ``cities`` — one per tracked city (~1,214): "when does MY city get
      driven, and has it been?" Both ``plan`` and ``observed`` are
      absent-not-null, so an unlisted or never-collected city carries no key
      rather than a block of nulls no ``if (rec.plan)`` consumer can reject.
    * ``records`` — one per feed record (~3,715), NOT per exploded district
      (~11,765): "where is Google driving that we do not track?" The table
      chassis renders every matching row on each keystroke with no
      virtualization, and 11,765 is far past anything it runs at today.

    Empty catalog → empty collections, with the file still written so the
    frontend fetch succeeds.

    Args:
        conn: open catalog connection (db.connect).
        data_dir: directory the artifact is written to (alongside cities.json.gz).

    Returns:
        The summary dict.
    """
    from . import db  # local import to keep module import order simple

    today = date.today()
    entries = db.get_active_driving_plans(conn, country=None)
    index = plan_match.build_index(entries)

    runs_by_city: dict[str, dict[str, Any]] = {}
    for run in db.get_latest_runs_all(conn):
        runs_by_city.setdefault(run["city_id"], {})[run["provider"]] = run

    cities_out = []
    matched_city_ids: dict[tuple, list[str]] = {}
    for city in db.get_all_cities(conn):
        tier, matched = plan_match.match_city(city, index)
        summary = plan_match.summarize_entries(matched) if matched else None

        runs = runs_by_city.get(city.city_id, {})
        gsv_run = runs.get("gsv")
        newest = (
            plan_match.plausible_capture_date(gsv_run["newest_capture_date"], today)
            if gsv_run is not None
            else None
        )

        record: dict[str, Any] = {
            "city_id": city.city_id,
            "display_name": city.display_name,
            "city_name": city.city_name,
            "state_name": city.state_name,
            "country_name": city.country_name,
            "enabled": city.enabled,
            "verdict": plan_match.classify(summary, newest, today),
        }
        if summary is not None and tier is not None:
            record["plan"] = _plan_block(summary, tier)
        observed = {
            provider: _observation_block(run, today)
            for provider, run in sorted(runs.items(), key=lambda kv: kv[0] != "gsv")
        }
        if observed:
            record["observed"] = observed

        # When was this city actually driven, not just how old is the median?
        # A city with 2019 + 2022 + 2024 imagery and one with a single 2021
        # pass can share a median and mean entirely different things. Read from
        # the latest gsv run's per-run JSON — the same files generate_aggregate_v2
        # reads in this pipeline run, so the cost is already paid — and absent
        # (not null) when the run has no JSON or no dated official imagery.
        if gsv_run is not None and gsv_run["json_filename"]:
            run_json = _load_city_json(os.path.join(data_dir, gsv_run["json_filename"]))
            years = _compact_capture_years(
                _capture_year_counts(
                    ((run_json or {}).get("google_panos") or {}).get(
                        "histogram_of_capture_dates_by_year"
                    )
                )
            )
            if years:
                record["capture_years"] = years

        cities_out.append(record)

        # Remember the reverse direction so a plan record can advertise which
        # of our cities it covers — that is the "we should be collecting here"
        # signal, and it is only computable while the match is in hand. Keyed
        # by the record's identity rather than the row object, so the two
        # passes agree without depending on the entry list staying alive.
        #
        # A country-tier match is deliberately excluded. It means only "this
        # city's country appears somewhere in the plan", which is enough to
        # show the city a plan block (labelled `country`, so the weakness is
        # visible) but NOT enough to claim a particular record covers it —
        # otherwise Salem, Oregon lands in Idaho's matched list purely because
        # both are in the United States, and every "no tracked city" record
        # in a country we collect anywhere silently disappears from the
        # collection-target list.
        if tier in ("manual", "region", "district"):
            for entry in matched:
                matched_city_ids.setdefault(plan_match.record_key(entry), []).append(city.city_id)

    # Regroup the exploded entries back into feed records. The catalog stores
    # one row per (record, district) because the feed comma-joins districts;
    # everything except `district` is shared by a record's rows.
    grouped: dict[tuple, dict[str, Any]] = {}
    entries_by_record: dict[tuple, list[Any]] = {}
    for entry in entries:
        key = plan_match.record_key(entry)
        entries_by_record.setdefault(key, []).append(entry)
        plan_record = grouped.get(key)
        if plan_record is None:
            start, start_approx = plan_match.entry_date(entry, "date_start", "date_start_raw")
            end, end_approx = plan_match.entry_date(entry, "date_end", "date_end_raw")
            plan_record = {
                # Stable across regenerations so an area row can be deep-linked
                # and can act as the table's sort tiebreak, the way city_id
                # does for a city row.
                "record_id": _record_id(key),
                # Trimmed for display: the feed ships at least one value with a
                # leading space (" Leningrad region"), which sorts a row to the
                # very top of an alphabetical table for no reason a reader can
                # see. Matching already folds whitespace, so this is a display
                # fix only and does not change which cities match.
                "country": _clean(entry["country"]),
                "country_matched": plan_match.normalize_country(entry["country"]),
                "region": _clean(entry["region"]),
                "publish": entry["publish"],
                "window_start": start,
                "window_end": end,
                "districts": [],
                "matched_city_ids": sorted(set(matched_city_ids.get(key, ()))),
            }
            if start_approx or end_approx:
                plan_record["window_approximate"] = True
            grouped[key] = plan_record
        if entry["district"]:
            plan_record["districts"].append(_clean(entry["district"]))

    records_out = []
    for key, plan_record in grouped.items():
        plan_record["district_count"] = len(plan_record["districts"])
        plan_record["matched_city_count"] = len(plan_record["matched_city_ids"])
        # A record with no tracked city becomes a row in its own right on the
        # Driving page, so it needs the same verdict vocabulary the city rows
        # use — computed here, through the same `classify`, rather than
        # reimplemented in JavaScript where the two would drift. There is no
        # imagery to weigh against it by definition, so the verdict can only
        # ever be a plan status (never drive_confirmed / driven_unplanned).
        plan_record["verdict"] = plan_match.classify(
            plan_match.summarize_entries(entries_by_record[key]), None, today
        )
        records_out.append(plan_record)

    # What Google actually changed, revision by revision. Entries exist only
    # for content-changed snapshots, so consecutive members of this list are
    # exactly the comparable pairs — no gap to reason about, no need to
    # reconstruct an unchanged fetch. Capped: the archive grows one revision a
    # week and every visitor fetches this file.
    changed = db.get_changed_driving_plan_snapshots(conn, limit=_MAX_REVISIONS + 1)
    revisions = []
    for newer, older in zip(changed, changed[1:]):
        diff = plan_match.diff_snapshots(
            db.get_driving_plan_entries(conn, older["snapshot_id"]),
            db.get_driving_plan_entries(conn, newer["snapshot_id"]),
        )
        revisions.append({"from": older["fetch_date"], "to": newer["fetch_date"], **diff})

    history = db.get_driving_plan_history(conn)
    plan_meta = {
        "source_url": driving_plan.FEED_URL,
        "disclaimer": _PLAN_DISCLAIMER,
        "fetch_count": (history["fetch_count"] if history else 0) or 0,
        "change_count": (history["change_count"] if history else 0) or 0,
        "first_fetch": history["first_fetch"] if history else None,
        "latest_fetch": history["latest_fetch"] if history else None,
        "latest_change": history["latest_change"] if history else None,
    }

    summary_doc = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "plan": plan_meta,
        "cities": cities_out,
        "records": records_out,
        "revisions": revisions,
    }

    output_path = os.path.join(data_dir, "driving_plan.json.gz")
    _write_json_gz_atomic(output_path, summary_doc)
    logger.info(
        f"Wrote driving-plan summary for {len(cities_out)} cities and "
        f"{len(records_out)} plan records to {output_path}"
    )

    return summary_doc
