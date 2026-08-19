"""
Provider-agnostic census machinery: columnar build, dedup, and row emission.

A *census* provider returns every image in an area rather than the nearest one
to a query point (Mapillary's vector tiles, KartaView's radius sweep), so the
pipeline from "decoded records" to "METADATA-schema CSV rows" is the same shape
for both. This module holds that shape exactly once.

WHY THIS IS ITS OWN MODULE, AND NOT COPIED PER PROVIDER. Issue #157 established
three properties here that are contracts rather than implementation details, and
each one is invisible in a code review of a second copy:

  1. **The census is columnar.** Colorado Springs is 6.5M features and Detroit
     19M; a list of per-image dicts costs ~0.74 GB per 1M images and the
     row-wise pipeline built three more full-census structures on top of it,
     which is what pushed both Mapillary channels into permanent reclaim under
     a 4 GB cgroup and made them look hung. Every step here is array work on
     positional indices.
  2. **The dedup is TWO rules, not one** (see :func:`dedupe_census`).
  3. **The written CSV is byte-identical** to the row-wise output it replaced,
     pinned by ``tests/fixtures/mapillary_golden_run.csv`` -- a run file is an
     immutable dated snapshot that ``diff.py`` compares against its predecessor,
     so a formatting or ordering drift shows up as a large phantom diff in every
     census city with no way to tell it from real imagery churn.

A drifted second copy is exactly the streetwalk-provider-token class of bug: it
works until it silently doesn't. So a provider supplies its *schema* (census
dtypes, output dtypes) and its *columns* (the copyright convention and its own
extras), and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Columns every provider's row builder fills itself, so a provider's extra
# columns are "everything else". Named once here rather than repeated as a
# tuple literal at each build site.
QUERY_COLUMNS = ("query_lat", "query_lon", "query_timestamp", "status")


def census_column(records: list[dict[str, Any]], column: str, dtype):
    """
    Build one census column, so that a single dirty value can't cost a request.

    ``pd.array(..., dtype="Int64"/"boolean")`` is a SAFE cast: a contributor
    device clock reporting a timestamp outside int64's range, or a non-integral
    one, raises rather than coercing (verified: 10**25 -> OverflowError,
    42.5 -> TypeError). That exception would be raised while a single tile or
    circle is being decoded -- i.e. before any of the capture-date guards run --
    so the caller would score that whole fetch as failed, discarding every other
    image in it (one z14 tile has been observed carrying 2.1M features) and, on
    a small city, pushing the run straight past its failed-fetch threshold. The
    row-wise census kept such a value untouched and let the date parser turn it
    into NO_DATE.

    So: the vectorized cast whenever it works, and a per-value pass only for a
    batch that actually holds something unusable. That fallback is a Python loop
    over one fetch's records and is slow -- and still far cheaper than dropping
    it.
    """
    values = [r[column] for r in records]
    try:
        return pd.array(values, dtype=dtype)
    except (TypeError, ValueError, OverflowError) as e:
        logger.warning(
            f"Unusable {column} value(s) in a batch ({e}); coercing the bad "
            f"entries to null rather than failing the whole batch"
        )
    coerced = []
    for value in values:
        try:
            coerced.append(pd.array([value], dtype=dtype)[0])
        except (TypeError, ValueError, OverflowError):
            coerced.append(None)
    return pd.array(coerced, dtype=dtype)


def records_to_census(records: list[dict[str, Any]], dtypes: Mapping[str, Any]) -> pd.DataFrame:
    """
    Turn one fetch's decoded records into a columnar census frame.

    Called per tile / per circle, immediately after decoding, so one fetch's
    dicts are freed before the next is decoded rather than every fetch's
    surviving until the whole city has downloaded.

    Args:
        records: decoded image dicts, each carrying every key in ``dtypes``.
        dtypes: the provider's census schema, ``{column: pandas dtype}``.

    Returns:
        A DataFrame with the ``dtypes`` columns, one row per image.
    """
    if not records:
        return pd.DataFrame({c: pd.Series(dtype=d) for c, d in dtypes.items()})
    return pd.DataFrame(
        {column: census_column(records, column, dtype) for column, dtype in dtypes.items()}
    )


def concat_census(frames: list[pd.DataFrame], dtypes: Mapping[str, Any]) -> pd.DataFrame:
    """Combine per-fetch census frames into one, preserving fetch order."""
    frames = [f for f in frames if len(f)]
    if not frames:
        return records_to_census([], dtypes)
    return pd.concat(frames, ignore_index=True)


def dedupe_census(census: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse cross-fetch duplicate image ids, exactly as the row-wise census did.

    Every census provider sees an image more than once: Mapillary encodes tiles
    with a render buffer, and a KartaView sweep covers each square with its
    circumscribed circle, so it re-sees ~pi/2 of everything. The row-wise form
    deduped with ``images_by_id[record["id"]] = record``, and a dict is TWO
    rules, not one: a repeated id takes the **last** copy's values, but keeps
    the position of its **first** appearance (assigning to an existing key
    overwrites the value without reordering the key).

    Both halves matter and pandas has no single call for the pair:

    * Values -- the two copies carry coordinates quantized to their own fetch's
      extent, so preferring the other one can shift an edge image to a
      neighbouring grid point and surface as a phantom change in the next
      run-to-run diff.
    * Order -- a run file is an immutable dated snapshot, so its row order is
      part of what must not drift. ``drop_duplicates(keep="last")`` gets the
      values right and the order wrong: given fetches ``[B, A]`` and ``[B]`` it
      yields ``[A, B]`` where the dict yielded ``[B, A]``. Duplicates are
      ubiquitous, so that reorders essentially every real city.

    Args:
        census: the concatenated per-fetch census, in fetch order.

    Returns:
        The deduped census, re-indexed from 0.
    """
    # factorize numbers the ids in order of FIRST appearance, so `codes` is
    # already the dict's key order; the scatter below then overwrites each
    # code's slot with every later position it occurs at, leaving the LAST.
    # (NumPy specifies last-wins for repeated indices in a plain assignment.)
    codes, uniques = pd.factorize(census["id"])
    if len(uniques) == len(census):  # no duplicates: skip the copy entirely
        return census
    last_position = np.empty(len(uniques), dtype=np.int64)
    last_position[codes] = np.arange(len(codes), dtype=np.int64)
    return census.take(last_position).reset_index(drop=True)


def status_for_capture_dates(capture_dates) -> np.ndarray:
    """
    Per-row OK / NO_DATE from already-parsed capture dates.

    Mirrors GSV's convention: an image whose contributor timestamp is unusable
    still proves coverage, so it is present-but-NO_DATE rather than a row
    quietly carrying a bogus date into the dated statistics. Shared by every
    grid downloader and road-walk collector so one provider's status vocabulary
    can't drift from another's.

    Args:
        capture_dates: array-like of 'YYYY-MM-DD'/'' from the provider's
            vectorized date parser.
    """
    return np.where(np.asarray(capture_dates) != "", "OK", "NO_DATE")


def build_image_rows(
    census: pd.DataFrame,
    image_positions: np.ndarray,
    query_lat,
    query_lon,
    query_timestamp: str,
    status,
    capture_date,
    *,
    dtypes: Mapping[str, Any],
    image_columns: Callable[[pd.DataFrame], Mapping[str, Any]],
) -> pd.DataFrame:
    """
    METADATA-schema rows for query locations matched to census images.

    Shared by every grid downloader (query location = a frozen grid point) and
    every road-walk collector (query location = an on-street sample point), and
    across providers. The shared 9-column core is built here; everything a
    provider adds -- including ``copyright_info``, whose convention differs per
    provider -- comes from ``image_columns``.

    Args:
        census: the columnar census.
        image_positions: positional index into ``census``, one per output row.
        query_lat/query_lon: the queried location per output row.
        query_timestamp: run-level ISO timestamp, identical on every row.
        status: per-row status string (or a scalar applied to every row).
        capture_date: per-row 'YYYY-MM-DD'/'' (or a scalar), already filtered by
            the provider's date rules. FLAT_ONLY rows pass None: a flat image is
            a coverage-presence marker, and a null date keeps contributor flat
            timestamps out of every dated statistic.
        dtypes: the provider's OUTPUT schema; its key order is the CSV's column
            order and is part of the byte-identical contract.
        image_columns: called with the taken sub-frame, returns the provider's
            own columns (``copyright_info`` plus its extras) as arrays.

    Returns:
        A DataFrame with the ``dtypes`` columns, in order.
    """
    picked = census.take(image_positions)
    return pd.DataFrame(
        {
            "query_lat": query_lat,
            "query_lon": query_lon,
            "query_timestamp": query_timestamp,
            "pano_lat": picked["lat"].to_numpy(),
            "pano_lon": picked["lon"].to_numpy(),
            "pano_id": picked["id"].to_numpy(dtype=object),
            "capture_date": capture_date,
            "status": status,
            **image_columns(picked),
        },
        columns=list(dtypes.keys()),
    )


def build_empty_rows(
    query_lat, query_lon, query_timestamp: str, status, *, dtypes: Mapping[str, Any]
) -> pd.DataFrame:
    """
    Rows for query locations with no imagery -- the ZERO_RESULTS fill, plus the
    REQUEST_FAILED variant for points under a fetch that never landed.

    Built column-wise: at a 4M-point grid the equivalent list of per-point dicts
    was the single largest allocation in the pipeline (issue #157). The row
    count comes from ``query_lat`` rather than a separate argument -- the two
    can only ever disagree by caller error, and the disagreement would surface
    as a length mismatch raised from inside the DataFrame constructor.
    """
    n = len(query_lat)
    return pd.DataFrame(
        {
            "query_lat": query_lat,
            "query_lon": query_lon,
            "query_timestamp": query_timestamp,
            "status": status,
            # No image at this location -> every image-derived column is null.
            # np.full rather than [None] * n: at Cairo's ~10.5M grid points the
            # Python list is ~84 MB of pure transient, per column, on the
            # allocation this function exists to keep small.
            **{c: np.full(n, None, dtype=object) for c in dtypes if c not in QUERY_COLUMNS},
        },
        columns=list(dtypes.keys()),
    )
