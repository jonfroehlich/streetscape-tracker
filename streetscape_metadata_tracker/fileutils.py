import glob
import logging
import os
import platform
import subprocess
import webbrowser
from pathlib import Path

import pandas as pd

from .config import MAPILLARY_METADATA_DTYPES
from .paths import get_default_data_dir

logger = logging.getLogger(__name__)


def get_list_of_city_csv_files(data_dir=None) -> list[str]:
    if data_dir is None:
        data_dir = get_default_data_dir()

    csv_files = glob.glob(os.path.join(data_dir, "**/*.csv.gz"), recursive=True)
    return csv_files


def load_city_csv_file(csv_path: str) -> pd.DataFrame:
    """
    Read a CSV file into a DataFrame, automatically detecting if it's gzipped based on file extension.
    capture_date must be YYYY-MM-DD (the on-disk schema — standardize_capture_date
    normalizes month/year-precision dates at download time); any other format
    parses to NaT.

    Args:
        csv_path: Path to the CSV file (can be either .csv or .csv.gz)

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

        # Read CSV with query_timestamp as object type first. Use the full
        # Mapillary schema (core + Mapillary extras): pandas silently ignores
        # dtype keys for columns a file doesn't have, so GSV runs and legacy
        # Mapillary files (which lack the extra columns) load unchanged, while
        # enriched Mapillary runs get their extras coerced to the right dtypes.
        df = pd.read_csv(
            csv_path,
            dtype=MAPILLARY_METADATA_DTYPES,
            compression=compression,
        )

        # Convert query_timestamp (ISO 8601 with timezone)
        df["query_timestamp"] = pd.to_datetime(df["query_timestamp"], format="ISO8601")

        # Convert capture_date (YYYY-MM-DD)
        df["capture_date"] = pd.to_datetime(df["capture_date"], format="%Y-%m-%d", errors="coerce")

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
