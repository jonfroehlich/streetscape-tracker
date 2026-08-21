import glob
import logging
import os
import platform
import subprocess
import webbrowser
from pathlib import Path

import pandas as pd

from . import naming
from .config import MAPILLARY_METADATA_DTYPES, PROVIDER_RUN_DTYPES
from .paths import get_default_data_dir

logger = logging.getLogger(__name__)


def get_list_of_city_csv_files(data_dir=None) -> list[str]:
    if data_dir is None:
        data_dir = get_default_data_dir()

    csv_files = glob.glob(os.path.join(data_dir, "**/*.csv.gz"), recursive=True)
    return csv_files


def dtypes_for_run_path(csv_path: str) -> dict:
    """
    The run schema a CSV should be read with, derived from its own filename.

    A run CSV is self-describing only through its name -- the provider token
    after ``_step_{S}`` (absent = gsv). That matters because pandas INFERS any
    column the dtype mapping omits, so reading one census provider's run with
    another's schema is silent corruption rather than an error: a nullable
    Int64 sequence index becomes float64 and a numeric-looking string way_id
    becomes a float, differently depending on which module opened the file.

    Falls back to the Mapillary schema for any name the naming contract does
    not parse (fixtures, ad-hoc exports). That is the historical default and is
    a superset of the shared core, so pandas ignores the keys such a file lacks
    and legacy/GSV reads are unchanged.

    Args:
        csv_path: path or bare filename of a run or road-walk snapshot CSV.
    """
    for parse in (naming.parse_filename, naming.parse_streetwalk_filename):
        try:
            provider = parse(csv_path).provider
        except ValueError:
            continue
        return PROVIDER_RUN_DTYPES.get(provider, MAPILLARY_METADATA_DTYPES)
    return MAPILLARY_METADATA_DTYPES


def load_city_csv_file(csv_path: str, dtypes: dict | None = None) -> pd.DataFrame:
    """
    Read a CSV file into a DataFrame, automatically detecting if it's gzipped based on file extension.
    capture_date accepts any ISO 8601 date — day, month or year precision —
    with reduced precision pinned to the 1st, matching standardize_capture_date;
    anything else parses to NaT (issue #226). One shape is an exception to
    "anything else parses to NaT" and raises instead: a timezone-AWARE value
    beside a naive one in the same column ("Mixed timezones detected", which
    errors="coerce" does not suppress). Nothing can write that today —
    standardize_capture_date returns YYYY-MM-DD or None, and both census
    decoders strftime("%Y-%m-%d") — so it is stated rather than guarded.

    Args:
        csv_path: Path to the CSV file (can be either .csv or .csv.gz)
        dtypes: Column dtypes to coerce. Defaults to the schema named by the
            file's OWN provider token (:func:`dtypes_for_run_path`), because
            pandas ignores dtype keys a file lacks but INFERS any column the
            mapping omits -- so reading one census provider's run with
            another's schema silently turns a nullable-Int64 sequence index
            into float64 and a numeric-looking string id into a float. Pass a
            schema explicitly only when the caller already knows the provider
            and the path may not carry a parseable name.

    Returns:
        pd.DataFrame: Loaded and processed DataFrame

    Raises:
        ValueError: If the file extension is neither .csv nor .csv.gz
        FileNotFoundError: If the specified file doesn't exist
    """
    logger.debug(f"Loading CSV file: {csv_path}")

    file_path = Path(csv_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {csv_path}")

    # Determine compression based on file extension
    if file_path.suffix == ".gz" or str(file_path).endswith(".csv.gz"):
        compression = "gzip"
    elif file_path.suffix == ".csv":
        compression = None
    else:
        raise ValueError(
            f"Unsupported file format. Expected .csv or .csv.gz, got: {file_path.suffix}"
        )

    try:
        logger.debug(f"Reading CSV file with compression: {compression}")

        # Read CSV with query_timestamp as object type first. With no schema
        # given, the file's own provider token picks one: pandas silently
        # ignores dtype keys for columns a file doesn't have, so GSV runs and
        # legacy files load unchanged, while each census provider's extras are
        # coerced by ITS schema rather than by whichever module opened the file.
        df = pd.read_csv(
            csv_path,
            dtype=dtypes_for_run_path(csv_path) if dtypes is None else dtypes,
            compression=compression,
        )

        # Convert query_timestamp (ISO 8601 with timezone)
        df["query_timestamp"] = pd.to_datetime(df["query_timestamp"], format="ISO8601")

        # Convert capture_date. This is the upstream gate every date-derived
        # statistic sits behind — analysis.dated_unique_panos, the per-run JSON's
        # age blocks and histograms, diff.py's capture-date comparison — so its
        # parse has to be at least as permissive as what is actually on disk.
        # A strict "%Y-%m-%d" was not: the legacy pre-2026 downloader wrote
        # MONTH-precision dates and those run files are never rewritten, so
        # every date in them coerced to NaT while the pano counts stayed
        # perfect, leaving catalog rows that looked fully populated and
        # internally consistent with NULL oldest/newest/median (issue #226).
        #
        # The format is PINNED rather than inferred, and that is the load-bearing
        # part: a format-free to_datetime(errors="coerce") reads ONE format off
        # the first non-null value and silently NaTs everything at another
        # precision, so a file mixing 2022-09 and 2022-09-15 loses one of the two
        # populations depending on which happens to come first. "ISO8601" accepts
        # every generation at once and pins reduced precision to the 1st, the
        # same convention standardize_capture_date applies at download time (and
        # download_kartaview pins the same way, for the same reason).
        #
        # errors="coerce" is what keeps ONE malformed row from taking out a whole
        # immutable dated snapshot, and it covers every shape a provider has ever
        # written -- but state its one hole rather than implying it has none: a
        # timezone-aware value beside a naive one raises "Mixed timezones
        # detected" THROUGH errors="coerce" (measured on pandas 3.0). The old
        # "%Y-%m-%d" coerced such a value to NaT instead, so this is a real if
        # unreachable narrowing: no writer in the repo can emit an offset here.
        # Left unguarded deliberately -- utc=True would silently SHIFT the naive
        # values rather than preserve them, which is a worse answer than a loud
        # failure on a file that cannot currently exist.
        df["capture_date"] = pd.to_datetime(df["capture_date"], format="ISO8601", errors="coerce")

        logger.debug(f"Loaded {len(df)} rows from {csv_path}")
        logger.debug(f"The DataFrame has columns: {df.columns} with dtypes: {df.dtypes}")

        # Print out dtypes to verify
        logger.debug("\nDataFrame dtypes after conversion:")
        for col, dtype in df.dtypes.items():
            logger.debug(f"  {col:15} {dtype}")

        return df

    except pd.errors.EmptyDataError as e:
        raise ValueError(f"The file {csv_path} is empty") from e
    except pd.errors.ParserError as e:
        raise ValueError(f"Error parsing file {csv_path}: {str(e)}") from e


def try_open_with_system_command(file_path: str) -> bool:
    """
    Attempt to open file using system-specific commands as fallback.

    Args:
        file_path: Path to the file to open

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        system = platform.system().lower()
        if system == "darwin":  # macOS
            subprocess.run(["open", file_path], check=True)
        elif system == "windows":
            subprocess.run(["start", file_path], shell=True, check=True)
        elif system == "linux":
            subprocess.run(["xdg-open", file_path], check=True)
        else:
            return False
        return True
    except subprocess.SubprocessError:
        return False


def open_in_browser(file_path: str) -> tuple[bool, str | None]:
    """
    Open a file in the default web browser with error handling and fallback options.

    Args:
        file_path: Path to the file to open

    Returns:
        Tuple[bool, Optional[str]]: (Success status, Error message if any)
    """
    path = Path(file_path).resolve()

    if not path.exists():
        return False, f"File not found: {file_path}"

    try:
        # Convert to proper file URI based on platform
        if platform.system() == "Windows":
            uri = path.as_uri()
        else:
            uri = f"file://{path}"

        # Try primary method: webbrowser module
        if webbrowser.open(uri, new=2):
            return True, None

        # First fallback: Try specific browsers
        for browser in ["google-chrome", "firefox", "safari", "edge"]:
            try:
                browser_ctrl = webbrowser.get(browser)
                if browser_ctrl.open(uri, new=2):
                    return True, None
            except webbrowser.Error:
                continue

        # Second fallback: system-specific commands
        if try_open_with_system_command(str(path)):
            return True, None

        return False, "Failed to open browser using all available methods"

    except Exception as e:
        return False, f"Error opening browser: {str(e)}"
