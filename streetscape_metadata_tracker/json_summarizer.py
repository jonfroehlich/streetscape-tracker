import gzip
import json
import logging
import os
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd
from tqdm import tqdm

from . import db
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
    """
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


def generate_streetwalk_manifest(conn, data_dir: str) -> dict[str, Any]:
    """
    Build and write ``streetwalks.json.gz`` — a small sidecar index of the
    latest road-walk street-coverage artifact per (city, provider), from the
    ``street_walks`` catalog table (issue #155).

    The city page needs this because the streetwalk coverage GeoJSON is NOT a
    sibling of the grid run file (its ``sp{N}`` spacing is a free parameter and
    its run-date differs from the grid run's), so the frontend cannot derive the
    artifact URL the way it derives ``_streets.json.gz``. The manifest maps
    (city_id, provider) → the exact ``coverage_filename`` plus headline stats.

    Kept deliberately separate from the aggregate ``cities.json.gz`` (schema v3):
    the aggregate contract stays untouched, and #102 later folds these stats in
    properly. Published automatically by the ``*.json.gz`` publish glob.

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
        walks.append(
            {
                "city_id": row["city_id"],
                "provider": row["provider"],
                "coverage_filename": row["coverage_filename"],
                "run_date": row["run_date"],
                "spacing_m": row["spacing_m"],
                "match_dist_m": row["match_dist_m"],
                "coverage_pct_by_length": pct,
                # street_walks has no uncovered column; derive it so the frontend
                # headline ("X% of street-km have no imagery") needs no math.
                "uncovered_pct_by_length": None if pct is None else round(100 - pct, 1),
                "edges": row["edges_total"],
                "edges_fully_covered": row["edges_fully_covered"],
                "mean_edge_coverage": row["mean_edge_coverage"],
            }
        )

    manifest = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "walks": walks,
    }

    output_path = os.path.join(data_dir, "streetwalks.json.gz")
    _write_json_gz_atomic(output_path, manifest)
    logger.info(f"Wrote streetwalk manifest for {len(walks)} walks to {output_path}")

    return manifest
