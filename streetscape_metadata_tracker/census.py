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

import gzip
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import geopy
import numpy as np
import pandas as pd

from .analysis import FLAT_ONLY, REQUEST_FAILED
from .download_common import (
    assign_to_grid,
    generate_grid_arrays,
    grid_bbox,
    grid_index_ranges,
)
from .fileutils import load_city_csv_file

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
    #
    # use_na_sentinel=False is load-bearing, not tidiness. By default factorize
    # gives every null id the sentinel code -1, and `last_position[-1] = ...`
    # is a NEGATIVE index: it writes the null row's position into the slot
    # belonging to the LAST unique id, so that image is not merely dropped, it
    # is REPLACED by the null row's coordinates and capture date. Given
    # [A, B, <NA>] the result was [A, <NA>] -- B silently gone, a row with no
    # pano_id in its place, in an immutable dated snapshot that diff.py then
    # reads as one pano removed and another added at a different grid point.
    # With the flag, a null id is simply its own key, which is exactly what the
    # `images_by_id[record["id"]] = record` dict this reproduces would do
    # (None is a perfectly good dict key). Mapillary cannot reach this --
    # decode_image_features skips a feature with no id -- but a census provider
    # that does not filter its ids can, so the seam has to be safe on its own.
    codes, uniques = pd.factorize(census["id"], use_na_sentinel=False)
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


def _check_image_columns(
    core: Mapping[str, Any], provider: Mapping[str, Any], dtypes: Mapping[str, Any]
) -> None:
    """
    Refuse a provider binding that does not exactly fill its own output schema.

    ``pd.DataFrame(..., columns=list(dtypes))`` selects and reorders, which is
    what makes the output column order the schema's order -- but it is also
    silent in all three directions a binding can be wrong, and every one of
    them publishes rather than raising:

    * a column declared in ``dtypes`` that the binding never supplies is
      created as all-null. #225 publishes ``date_added`` as its own column
      precisely so it can never be confused with ``shot_date``; an all-null
      ``date_added`` in an immutable dated snapshot destroys exactly the
      provenance that column exists to keep, and nothing raises.
    * a key the binding supplies that is NOT in ``dtypes`` -- a typo -- is
      dropped, so the intended column is all-null by the rule above and the
      typo leaves no trace.
    * a key that collides with the shared core silently WINS, because the
      binding is splatted last. A binding returning ``pano_lat`` would quietly
      replace the census's own coordinate.

    A provider seam whose contract is unenforced is a copy waiting to happen,
    which is the thing this module exists to prevent, so the contract is
    checked once per call (a set comparison against a handful of names, not
    per-row work) and stated as an error rather than discovered in a CSV.
    """
    collisions = sorted(set(core) & set(provider))
    if collisions:
        raise ValueError(
            f"image_columns returned {collisions}, which the shared core already "
            f"fills; a provider column cannot silently replace a core one"
        )
    supplied, declared = set(core) | set(provider), set(dtypes)
    if supplied != declared:
        missing, unexpected = sorted(declared - supplied), sorted(supplied - declared)
        raise ValueError(
            f"image_columns does not match the output schema: "
            f"missing {missing or 'nothing'}, unexpected {unexpected or 'nothing'}"
        )


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
    core = {
        "query_lat": query_lat,
        "query_lon": query_lon,
        "query_timestamp": query_timestamp,
        "pano_lat": picked["lat"].to_numpy(),
        "pano_lon": picked["lon"].to_numpy(),
        "pano_id": picked["id"].to_numpy(dtype=object),
        "capture_date": capture_date,
        "status": status,
    }
    provider = image_columns(picked)
    _check_image_columns(core, provider, dtypes)
    return pd.DataFrame({**core, **provider}, columns=list(dtypes.keys()))


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


# ── The grid run: a census becomes a METADATA-schema CSV ───────────────────


@dataclass(frozen=True, eq=False)
class CensusGrid:
    """
    A city's frozen sampling lattice, built once per run.

    Built BEFORE the fetch (``bbox`` is what bounds it) and consumed AFTER it
    (``lats``/``lons`` become the query columns of every output row), so the
    caller builds one and threads it through rather than deriving it twice: at
    Cairo's ~10.5M points the coordinate arrays are ~170 MB and the pure-Python
    geodesic solve that produces them ran ~13 minutes before a single tile was
    fetched (issue #157).

    ``eq=False`` because the fields are arrays: the generated ``__eq__`` would
    return an array rather than a bool, and the generated ``__hash__`` would
    raise. Nothing needs either.
    """

    lats: np.ndarray
    lons: np.ndarray
    center_lat: float
    center_lon: float
    step_length: float
    width_steps: int
    height_steps: int
    bbox: tuple[float, float, float, float]
    # Enough of grid_index_ranges to turn an (i, j) back into a position.
    i_min: int
    j_min: int
    n_j: int

    @property
    def num_points(self) -> int:
        return len(self.lats)

    def ordinals(self, i, j):
        """
        Position of grid index ``(i, j)`` in generation order; scalars or arrays.

        The grid is a regular lattice, so a point's position is arithmetic and
        needs no lookup table -- the ``{(i, j): position}`` dict this replaces
        cost ~4.5 GB at Cairo scale against an 8 GB cgroup (issue #157).
        """
        return (i - self.i_min) * self.n_j + (j - self.j_min)


def build_grid(
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float,
) -> CensusGrid:
    """
    Build the frozen lattice for a city, plus the bbox that bounds a fetch of it.

    Grid geometry is frozen per city, so every future run re-derives these same
    coordinates and its diffs align on an identical rectangle. Shared by every
    census provider so one provider's lattice can never drift from another's.
    """
    width_steps = int(grid_width / step_length)
    height_steps = int(grid_height / step_length)
    lats, lons, _, _ = generate_grid_arrays(
        geopy.Point(center_lat, center_lon), width_steps, height_steps, step_length
    )
    i_values, j_values = grid_index_ranges(width_steps, height_steps)
    return CensusGrid(
        lats=lats,
        lons=lons,
        center_lat=center_lat,
        center_lon=center_lon,
        step_length=step_length,
        width_steps=width_steps,
        height_steps=height_steps,
        bbox=grid_bbox(center_lat, center_lon, grid_width, grid_height, step_length),
        i_min=i_values[0],
        j_min=j_values[0],
        n_j=len(j_values),
    )


def write_census_grid_run(
    fetched: dict[str, Any],
    grid: CensusGrid,
    output_csv_gz_path: str,
    query_timestamp: str,
    *,
    capture_dates_for: Callable[[pd.DataFrame, np.ndarray], Any],
    image_columns: Callable[[pd.DataFrame], Mapping[str, Any]],
    dtypes: Mapping[str, Any],
    unmeasured_mask: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
    unmeasured_desc: str = "",
) -> dict[str, Any]:
    """
    Turn a fetched census into a run CSV: the back half of every grid collector.

    Assigns every image to its nearest grid point, then writes three populations
    in one pass -- 360 panos, FLAT_ONLY markers for points that hold only flat
    imagery (issue #116), and the ZERO_RESULTS/REQUEST_FAILED fill for points
    nothing covered.

    THE CENSUS IS POPPED OUT OF ``fetched``, and the caller must not keep its own
    reference. That is not a style choice: this function drops the census as soon
    as the last frame is built, and a caller-side name would pin the whole thing
    (19M rows at Detroit) alive through both CSV writes, defeating the release
    entirely. Passing the fetch-result dict rather than the frame is what makes
    this function the sole owner. A test pins it.

    Args:
        fetched: the provider's fetch result; its ``census`` key is consumed.
        grid: the city's frozen lattice, from :func:`build_grid`.
        output_csv_gz_path: destination; the caller validates and creates it.
        query_timestamp: run-level ISO timestamp, identical on every row.
        capture_dates_for: ``(census, positions) -> array`` of 'YYYY-MM-DD'/'',
            applying the provider's own date rules. Takes positions rather than
            a taken sub-frame so a provider can index the one or two columns it
            needs instead of materializing the whole census again.
        image_columns: the provider's own output columns; see
            :func:`build_image_rows`.
        dtypes: the provider's OUTPUT schema; its key order is the CSV's column
            order and part of the byte-identical contract.
        unmeasured_mask: ``(lats, lons) -> bool mask`` marking query points whose
            area never downloaded, or None when the fetch was complete.
        unmeasured_desc: operator-facing noun phrase for what failed (e.g.
            "3 undownloaded tile(s)"), logged with the resulting point count.

    Returns:
        Dict with the read-back ``df`` and the four population counts.
    """
    census = fetched.pop("census")
    is_pano = census["is_pano"].to_numpy()

    # Nearest-grid-point assignment for the WHOLE census at once. Everything
    # from here to the CSV write is array work on positional indices into the
    # census: the row-wise form built an (image, (i, j)) tuple pair and then a
    # 16-key dict per image, which is ~1.4 GB per million images and is what
    # made a census-heavy city unschedulable (issue #157).
    i_idx, j_idx, in_grid = assign_to_grid(
        census["lat"].to_numpy(),
        census["lon"].to_numpy(),
        grid.center_lat,
        grid.center_lon,
        grid.width_steps,
        grid.height_steps,
        grid.step_length,
    )
    ordinals = grid.ordinals(i_idx, j_idx)
    del i_idx, j_idx

    # Positions of the in-grid panos, in census order -- the order the row-wise
    # loop visited them in, and therefore the row order of the output file.
    pano_positions = np.flatnonzero(in_grid & is_pano)
    pano_ordinals = ordinals[pano_positions]
    capture_dates = capture_dates_for(census, pano_positions)
    covered_df = build_image_rows(
        census,
        pano_positions,
        grid.lats[pano_ordinals],
        grid.lons[pano_ordinals],
        query_timestamp,
        status_for_capture_dates(capture_dates),
        capture_dates,
        dtypes=dtypes,
        image_columns=image_columns,
    )
    del capture_dates

    # Flat imagery (issue #116): tally every in-grid flat image for the census
    # magnitude, and keep one representative per grid point so a flat-only
    # point (a point with flats but no pano) can be written as a single
    # FLAT_ONLY marker row.
    flat_positions = np.flatnonzero(in_grid & ~is_pano)
    num_flat_images = len(flat_positions)
    # return_index gives the FIRST occurrence of each ordinal, matching the
    # dict.setdefault this replaces -- the earliest flat in census order stays
    # the representative for its point.
    _, first_of_point = np.unique(ordinals[flat_positions], return_index=True)
    flat_positions = flat_positions[np.sort(first_of_point)]
    # A pano already covers that point, so it is not flat-ONLY.
    flat_positions = flat_positions[
        ~np.isin(ordinals[flat_positions], pano_ordinals, assume_unique=False)
    ]
    flat_ordinals = ordinals[flat_positions]
    flat_only_df = build_image_rows(
        census,
        flat_positions,
        grid.lats[flat_ordinals],
        grid.lons[flat_ordinals],
        query_timestamp,
        FLAT_ONLY,
        # capture_date is deliberately null for FLAT_ONLY: this row is a
        # coverage-presence marker, and a null date keeps flat timestamps out
        # of every date/age/histogram path (which key on status == 'OK').
        None,
        dtypes=dtypes,
        image_columns=image_columns,
    )
    num_flat_only_points = len(flat_positions)
    # Deliberately NOT pd.concat'd onto covered_df. covered_df is one row per
    # in-grid pano -- 6.5M rows of mostly-object columns at Colorado Springs --
    # and concatenating copies all of it to append a frame that is at most one
    # row per grid point. The two are written to the gzip handle in sequence
    # instead, which is byte-identical, for the same reason the empty fill is
    # (see the write below).
    del census, is_pano, in_grid

    # Which grid points nothing covered, via a bitmap rather than np.setdiff1d:
    # setdiff1d sorts and uniques BOTH operands, including the
    # num_grid_points-long arange that is unique by construction -- O(n log n)
    # plus several int64 temporaries over the biggest array in the function.
    # This is O(n) and one byte per point, and needs no concatenate either.
    covered = np.zeros(grid.num_points, dtype=bool)
    covered[pano_ordinals] = True
    covered[flat_ordinals] = True
    empty_ordinals = np.flatnonzero(~covered)
    del covered, pano_ordinals, flat_ordinals, ordinals

    # The empty-grid-point fill, built COLUMN-WISE rather than as one dict per
    # point. This is the single biggest allocation in the whole pipeline: a
    # 16-key dict is 464 bytes, so Cairo's ~10.5M points cost ~4.9 GB of dicts
    # plus another ~6 GB when pandas turns them into an N x 16 object matrix --
    # on a cgroup capped at 8 GB (issue #157). As arrays the same fill is two
    # float columns and a handful of all-null ones.
    empty_df = build_empty_rows(
        grid.lats[empty_ordinals],
        grid.lons[empty_ordinals],
        query_timestamp,
        "ZERO_RESULTS",
        dtypes=dtypes,
    )
    # An uncovered point inside an area that never downloaded is UNKNOWN, not
    # empty. Left as ZERO_RESULTS it would be indistinguishable from genuine
    # no-imagery and would quietly understate coverage and pollute the
    # run-to-run diff, so it gets its own status -- the same shape as the GSV
    # downloader writing REQUEST_FAILED rows for its sub-threshold holes
    # (issue #168). REQUEST_FAILED counts toward neither 360 nor any-imagery
    # coverage, and is already one of analysis.SYSTEMIC_FAILURE_STATUSES.
    num_unmeasured_points = 0
    if unmeasured_mask is not None:
        in_unmeasured = unmeasured_mask(grid.lats[empty_ordinals], grid.lons[empty_ordinals])
        empty_df.loc[in_unmeasured, "status"] = REQUEST_FAILED
        num_unmeasured_points = int(in_unmeasured.sum())
        logger.warning(
            f"{num_unmeasured_points:,} grid points fall in {unmeasured_desc}; "
            f"written as {REQUEST_FAILED} rather than empty"
        )
    num_empty_points = len(empty_ordinals)
    del empty_ordinals

    # Stream straight into the gzip handle, and write the three frames in
    # sequence rather than pd.concat'ing them first. df.to_csv() with no path
    # built the ENTIRE csv as one Python str and then a second full copy as
    # bytes -- about 1.7 GB of pure duplication at Cairo scale, for a file we
    # are writing out anyway -- and each concat was another full copy of the
    # frames it joined. Appending with header=False is byte-identical to
    # concatenating: same columns, same order, panos then flat-only then empty.
    with gzip.open(output_csv_gz_path, "wt", encoding="utf-8", newline="") as f:
        covered_df.to_csv(f, index=False)
        if num_flat_only_points:
            flat_only_df.to_csv(f, index=False, header=False)
        if num_empty_points:
            empty_df.to_csv(f, index=False, header=False)
    del covered_df, flat_only_df, empty_df

    # Read back through the shared loader so dtypes match GSV runs exactly.
    df = load_city_csv_file(output_csv_gz_path, dtypes=dtypes)
    n_pano_rows = int(df["status"].isin(("OK", "NO_DATE")).sum())
    logger.info(
        f"Wrote {len(df)} rows ({n_pano_rows} pano rows, "
        f"{num_flat_only_points} flat-only points, {num_flat_images} flat images, "
        f"{num_empty_points} empty grid points) "
        f"to {output_csv_gz_path}"
    )
    return {
        "df": df,
        "num_pano_rows": n_pano_rows,
        "num_flat_images": num_flat_images,
        "num_flat_only_points": num_flat_only_points,
        "num_empty_points": num_empty_points,
        "num_unmeasured_points": num_unmeasured_points,
    }
