"""
Run-to-run diff engine for temporal GSV tracking.

Compares two collection runs of the same city and reports what changed:

- Pano level (primary): which pano_ids were added, removed, or persisted,
  and which persisted panos had their capture_date change.
- Grid-point level (only when the two runs sampled the same grid): how many
  query points gained or lost coverage (OK <-> ZERO_RESULTS transitions).

Grid geometry is frozen in the cities catalog (db.py), so post-migration
runs of the same city always align; pairs involving a pre-migration
baseline may not (geocoder drift), in which case grid-point stats are None.
"""

import gzip
import logging
import os
import re
from dataclasses import dataclass, field

import pandas as pd

from .analysis import PRESENT_STATUSES
from .fileutils import remove_stale_diff_detail
from .naming import DEFAULT_PROVIDER, KNOWN_PROVIDERS, STREETWALK_DIFF_FILENAME_RE

logger = logging.getLogger(__name__)

# Rounding used to key grid points; matches the precision the query
# coordinates survive a CSV round-trip with.
_COORD_DECIMALS = 6


@dataclass
class RunDiff:
    """Summary (and detail rows) of changes between two runs of a city."""

    panos_added: int
    panos_removed: int
    panos_persisted: int
    capture_date_changed: int
    grid_aligned: bool
    points_gained_coverage: int | None
    points_lost_coverage: int | None
    coverage_delta_pct: float | None
    # Long-form detail rows: change_type, pano_id, pano_lat, pano_lon,
    # old_capture_date, new_capture_date
    detail: pd.DataFrame = field(repr=False, default=None)

    @property
    def has_changes(self) -> bool:
        return (self.panos_added + self.panos_removed + self.capture_date_changed) > 0


def _unique_present_panos(df: pd.DataFrame) -> pd.DataFrame:
    """
    Present rows (OK or NO_DATE) deduplicated on pano_id, keeping the newest
    capture_date. NO_DATE panos exist — they just lack a usable date (common
    for Mapillary's bogus contributor timestamps) — so an OK<->NO_DATE flip
    must read as a capture-date change, not as a pano removed + added.
    """
    present = df[df["status"].isin(PRESENT_STATUSES)].copy()
    present = present.sort_values("capture_date", na_position="first")
    return present.drop_duplicates(subset=["pano_id"], keep="last")


def _capture_date_str(value) -> str | None:
    """capture_date as 'YYYY-MM-DD' or None (handles NaT and raw strings)."""
    if pd.isna(value):
        return None
    if isinstance(value, str):
        return value
    return pd.Timestamp(value).date().isoformat()


def _grid_keys(df: pd.DataFrame) -> pd.Index:
    return pd.MultiIndex.from_arrays(
        [
            df["query_lat"].round(_COORD_DECIMALS),
            df["query_lon"].round(_COORD_DECIMALS),
        ]
    )


def compute_run_diff(df_old: pd.DataFrame, df_new: pd.DataFrame) -> RunDiff:
    """
    Compare two runs of the same city.

    Args:
        df_old: DataFrame of the earlier run (load_city_csv_file format)
        df_new: DataFrame of the later run

    Returns:
        RunDiff with pano-level counts, grid-point coverage transitions
        (None when the grids don't align), and a detail DataFrame with one
        row per changed pano.
    """
    old_panos = _unique_present_panos(df_old).set_index("pano_id")
    new_panos = _unique_present_panos(df_new).set_index("pano_id")

    old_ids = set(old_panos.index)
    new_ids = set(new_panos.index)

    added_ids = sorted(new_ids - old_ids)
    removed_ids = sorted(old_ids - new_ids)
    persisted_ids = sorted(old_ids & new_ids)

    detail_rows = []
    for pano_id in added_ids:
        row = new_panos.loc[pano_id]
        detail_rows.append(
            {
                "change_type": "pano_added",
                "pano_id": pano_id,
                "pano_lat": row["pano_lat"],
                "pano_lon": row["pano_lon"],
                "old_capture_date": None,
                "new_capture_date": _capture_date_str(row["capture_date"]),
            }
        )
    for pano_id in removed_ids:
        row = old_panos.loc[pano_id]
        detail_rows.append(
            {
                "change_type": "pano_removed",
                "pano_id": pano_id,
                "pano_lat": row["pano_lat"],
                "pano_lon": row["pano_lon"],
                "old_capture_date": _capture_date_str(row["capture_date"]),
                "new_capture_date": None,
            }
        )

    capture_date_changed = 0
    for pano_id in persisted_ids:
        old_date = _capture_date_str(old_panos.loc[pano_id, "capture_date"])
        new_date = _capture_date_str(new_panos.loc[pano_id, "capture_date"])
        if old_date != new_date:
            capture_date_changed += 1
            row = new_panos.loc[pano_id]
            detail_rows.append(
                {
                    "change_type": "capture_date_changed",
                    "pano_id": pano_id,
                    "pano_lat": row["pano_lat"],
                    "pano_lon": row["pano_lon"],
                    "old_capture_date": old_date,
                    "new_capture_date": new_date,
                }
            )

    detail = pd.DataFrame(
        detail_rows,
        columns=[
            "change_type",
            "pano_id",
            "pano_lat",
            "pano_lon",
            "old_capture_date",
            "new_capture_date",
        ],
    )

    # Grid-point coverage transitions, only when both runs sampled the
    # exact same grid points. Compare unique point sets, never row counts:
    # Mapillary runs hold one row per pano, so row counts differ between
    # runs of the identical frozen grid.
    old_keys = _grid_keys(df_old)
    new_keys = _grid_keys(df_new)
    grid_aligned = set(old_keys) == set(new_keys)

    points_gained = points_lost = coverage_delta = None
    if grid_aligned:
        old_status = pd.Series(df_old["status"].values, index=old_keys)
        new_status = pd.Series(df_new["status"].values, index=new_keys)
        # Duplicated grid keys shouldn't happen; guard so align() can't explode
        old_status = old_status[~old_status.index.duplicated(keep="first")]
        new_status = new_status[~new_status.index.duplicated(keep="first")]
        new_status = new_status.reindex(old_status.index)

        # A point is covered if it holds present imagery (OK or NO_DATE),
        # matching analysis.calculate_coverage_stats.
        old_ok = old_status.isin(PRESENT_STATUSES)
        new_ok = new_status.isin(PRESENT_STATUSES)
        points_gained = int((~old_ok & new_ok).sum())
        points_lost = int((old_ok & ~new_ok).sum())

        n = len(old_status)
        coverage_delta = float((new_ok.sum() - old_ok.sum()) / n * 100) if n else 0.0
    else:
        logger.warning(
            "Query grids do not align between runs "
            f"({len(set(old_keys))} vs {len(set(new_keys))} unique points); "
            "skipping grid-point coverage transitions"
        )

    return RunDiff(
        panos_added=len(added_ids),
        panos_removed=len(removed_ids),
        panos_persisted=len(persisted_ids),
        capture_date_changed=capture_date_changed,
        grid_aligned=grid_aligned,
        points_gained_coverage=points_gained,
        points_lost_coverage=points_lost,
        coverage_delta_pct=coverage_delta,
        detail=detail,
    )


def generate_diff_filename(
    city_id: str, from_date: str, to_date: str, provider: str = "gsv"
) -> str:
    """
    Basename for a published diff detail file.

    GSV diffs keep the original tokenless form so published URLs are stable;
    other providers get a token after '_diff':
    ``{city_id}_diff_mapillary_{from}_to_{to}.csv.gz``.
    """
    provider_token = "" if provider == "gsv" else f"{provider}_"
    return f"{city_id}_diff_{provider_token}{from_date}_to_{to_date}.csv.gz"


# The exact shape generate_diff_filename emits, and nothing looser (issue
# #265): it is what scripts/sweep_orphan_diff_details.py uses to decide which
# files in data/ are diff details at all, so a match is a licence to delete an
# unreferenced file. Anchored on the '_diff_[provider_]DATE_to_DATE.csv.gz'
# TAIL, which no run, walk or JSON artifact ends with — a city slug that
# happens to contain '_diff_' cannot make a run CSV match. The provider token
# is the explicit non-default alternation, as the generator never writes
# 'gsv_'. Pinned against the generator's own output in tests/test_diff.py.
# Ends in \Z, never $: '$' also matches before a trailing newline, and a
# licence-to-delete pattern must not accept a name the generator cannot emit.
_DIFF_PROVIDER_ALT = "|".join(p for p in KNOWN_PROVIDERS if p != DEFAULT_PROVIDER)
DIFF_DETAIL_FILENAME_RE = re.compile(
    r"^(?P<slug>.+?)_diff_"
    rf"(?:(?P<provider>{_DIFF_PROVIDER_ALT})_)?"
    r"(?P<from_date>\d{4}-\d{2}-\d{2})_to_(?P<to_date>\d{4}-\d{2}-\d{2})\.csv\.gz\Z"
)


def diff_detail_match(filename: str) -> re.Match | None:
    """The full match of a grid OR walk diff detail basename against the shape
    its generator emits (``generate_diff_filename`` here,
    ``naming.generate_streetwalk_diff_filename``), or None. Both patterns
    capture ``from_date`` and ``to_date``."""
    return DIFF_DETAIL_FILENAME_RE.fullmatch(filename) or STREETWALK_DIFF_FILENAME_RE.fullmatch(
        filename
    )


def is_diff_detail_filename(filename: str) -> bool:
    """True for a grid OR walk diff detail basename (see ``diff_detail_match``)."""
    return diff_detail_match(filename) is not None


def write_diff_detail(diff: RunDiff, output_path: str) -> None:
    """Write the diff's detail rows as a gzipped CSV."""
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as f:
        diff.detail.to_csv(f, index=False)
    logger.info(f"Wrote diff detail ({len(diff.detail)} rows) to {output_path}")


def sync_diff_detail(diff: RunDiff, data_dir: str, detail_name: str) -> str | None:
    """
    Make the published detail file at ``detail_name`` match ``diff`` and return
    the ``detail_filename`` the ``run_diffs`` row should record.

    The one implementation of issue #265's rule for grid diffs — a detail file
    is a function of the diff result — shared by the collector
    (``cli._compute_and_record_diff``) and the repair handle
    (``scripts/recompute_run_diffs.py``, issue #245), so the two cannot drift
    apart on what a recomputed diff leaves on disk:

    - has changes: the file is (over)written from THIS diff and its name returned;
    - no changes: any file at the name is removed and ``None`` returned, since a
      surviving file would be published with no row pointing at it.

    ``detail_name`` always comes from :func:`generate_diff_filename`, never by
    hand. Removal goes through ``fileutils.remove_stale_diff_detail``, which
    never raises; a caller that must know whether the file is really gone checks
    the disk afterwards.

    Usage:
        name = generate_diff_filename(city_id, prev.run_date, run_date.isoformat(), provider=p)
        detail_filename = sync_diff_detail(diff, data_dir, name)
    """
    if diff.has_changes:
        write_diff_detail(diff, os.path.join(data_dir, detail_name))
        return detail_name
    remove_stale_diff_detail(data_dir, detail_name)
    return None
