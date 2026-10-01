import glob
import gzip
import logging
import os
import platform
import subprocess
import webbrowser
from pathlib import Path

import pandas as pd

from . import naming
from .analysis import apply_query_radius
from .config import MAPILLARY_METADATA_DTYPES, PROVIDER_RUN_DTYPES
from .paths import get_default_data_dir

logger = logging.getLogger(__name__)


def get_list_of_city_csv_files(data_dir=None) -> list[str]:
    if data_dir is None:
        data_dir = get_default_data_dir()

    csv_files = glob.glob(os.path.join(data_dir, "**/*.csv.gz"), recursive=True)
    return csv_files


def _resolve_run_path(csv_path: str) -> tuple[str | None, str | None]:
    """
    ``(kind, provider)`` from a run or road-walk artifact's own filename.

    ``kind`` is ``"run"`` for a grid run, ``"streetwalk"`` for a road walk, and
    both are None for a name the naming contract does not parse. The one place
    the loader asks "what is this file?", so the dtype schema and the
    query-radius gate (issue #367) can never resolve the same path differently.
    """
    for kind, parse in (
        ("run", naming.parse_filename),
        ("streetwalk", naming.parse_streetwalk_filename),
    ):
        try:
            return kind, parse(csv_path).provider
        except ValueError:
            continue
    return None, None


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
    _kind, provider = _resolve_run_path(csv_path)
    if provider is None:
        return MAPILLARY_METADATA_DTYPES
    return PROVIDER_RUN_DTYPES.get(provider, MAPILLARY_METADATA_DTYPES)


def load_city_csv_file(
    csv_path: str, dtypes: dict | None = None, *, raw: bool = False
) -> pd.DataFrame:
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
        raw: return exactly what is on disk. By default a GSV GRID RUN (the
            file's own name parses as a run with provider gsv) comes back
            through analysis.apply_query_radius (issue #367): a pano beyond
            analysis.GSV_QUERY_RADIUS_M of its query point reads as status
            OUT_OF_RADIUS and every row gains ``query_distance_m``. The CSV
            itself is never rewritten -- a run file records what the provider
            said -- so the rule repeats here, at the one reader everything
            goes through. Pass ``raw=True`` only where the provider's own
            answer is what matters: a caller that would write the frame back
            to a run file, or one (like the grid-attribution street analyzer)
            that judges a pano by its own position rather than by the query
            point that found it. Road-walk files and names the contract does
            not parse are never filtered; the walk bounds sample-to-pano
            distance itself.

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

        # The GSV query-radius rule (issue #367), gated on the file's OWN
        # resolution -- the same one that picked its dtype schema above -- so a
        # census run or a road walk is never touched however it is opened.
        if not raw:
            kind, provider = _resolve_run_path(csv_path)
            if kind == "run" and provider == "gsv":
                df = apply_query_radius(df, provider)

        return df

    except pd.errors.EmptyDataError as e:
        raise ValueError(f"The file {csv_path} is empty") from e
    except pd.errors.ParserError as e:
        raise ValueError(f"Error parsing file {csv_path}: {str(e)}") from e


def remove_stale_diff_detail(data_dir: str, filename: str | None) -> bool:
    """
    Delete a published diff detail file that no longer describes a recorded
    diff, returning True only when a file was actually removed (issue #265).

    The one rule behind it, shared by the grid diff (``cli._compute_and_record_diff``)
    and the walk diff (``walk_diff.compute_and_record_walk_diff``): **a diff
    detail file is a function of the diff result.** It exists exactly when a
    recorded diff with changes names it. Both families used to write the file
    only when a diff had changes and never delete one, so a re-diff that came
    out with no changes — or was skipped — dropped the row's pointer and left
    the file in ``data/``, which is rsynced to a public web server.

    Never raises, because every caller runs it after a paid-for crawl is
    already cataloged (the grid call site is not even failure-guarded):

    - ``None`` or an empty name is a no-op (a row that recorded no file);
    - a file that is already gone is the normal case, not an error;
    - any other ``OSError`` (permissions, a stale NFS handle) is logged as a
      warning and swallowed — a stranded file is a publishing blemish, a
      failed collection is a lost month;
    - a name with a path component is refused, logged, and nothing is
      deleted. These names come from our own catalog and generators, which
      never emit one, but the name is joined onto ``data_dir`` and an unlink
      is the one operation where trusting that blindly is not worth a line.

    Usage (the name always comes from a generator or a catalog row, never by hand):

        name = generate_diff_filename(city_id, prev.run_date, run_date.isoformat())
        if not diff.has_changes:
            remove_stale_diff_detail(data_dir, name)
    """
    if not filename:
        return False
    if os.path.basename(filename) != filename or filename in (os.curdir, os.pardir):
        logger.error(f"Refusing to remove diff detail {filename!r}: not a bare filename")
        return False
    path = os.path.join(data_dir, filename)
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            f"Could not remove stale diff detail {path} ({exc}); it stays on disk and "
            "published, unreferenced — scripts/sweep_orphan_diff_details.py finds it"
        )
        return False
    logger.info(f"Removed stale diff detail {path}")
    return True


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


def count_streetwalk_samples(csv_path: Path) -> int | None:
    """
    Number of sampled locations in a road-walk snapshot: its data rows.

    The walk writes exactly one row per on-street sample point, so the row count
    recovers ``sample_points`` — which the artifact itself does not carry and
    which ``estimate_street_samples`` prefers over every other precedence step
    when budgeting a later walk of the same city. Counted line-by-line rather
    than via pandas: the caller may be reconciling a multi-hundred-MB snapshot
    inside the scheduler's memory-capped cgroup, and only the count is wanted.

    Returns None if the snapshot is missing or unreadable.

    Shared by the orphan-walk salvage and the bundle importer (issue #330):
    both reconstruct a walk's row from artifacts on disk, and a second copy
    of this would let the two disagree about what a sample is.
    """
    try:
        with gzip.open(csv_path, "rt", encoding="utf-8") as fh:
            return max(sum(1 for _ in fh) - 1, 0)  # minus the header
    except (OSError, EOFError, UnicodeDecodeError) as e:
        logger.warning(f"Could not count samples in {csv_path.name}: {e}")
        return None
