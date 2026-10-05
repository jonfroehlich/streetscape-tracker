"""
Mapillary ``quality_score`` as a distribution, not one median (issue #321).

Mapillary publishes a per-image ``quality_score`` -- "predicted visual quality
of the image in the range [0.0, 1.0]" -- on the tiles the census already
fetches, and every pano row has carried it since 2026-07-24. The study in
``docs/experiments/mapillary-image-quality.md`` measured what one median of it
can and cannot say, and this module is where the published numbers come from.

Two rules travel with every number this module produces:

* **It is a vendor's PREDICTION of visual quality, nothing more.** It does not
  mean "useful for a Sidewalk assessment".
* **It marks pedestrian capture DOWN.** Paired within-city, on-foot imagery
  scored below vehicle imagery in 84.5% of the 58 cities holding both, so a
  city ranked on this score alone is a city ranked against on-foot capture --
  often exactly the imagery a sidewalk assessment wants. The on-foot counts
  are therefore part of the block rather than a separate lookup, and the
  frontend never shows the quality figures without the on-foot share beside
  them.

Definitions are the study's (``scripts/mapillary_image_quality_collect.py``,
``CityAccumulator.finish``), so the published block reproduces the committed
per-city rows up to rounding: numpy's linear-interpolation percentiles over
the SCORED pano rows; the sequence-weighted cut takes one median per
``sequence_id`` and drops images with no sequence from that cut only; on-foot
counts are over every pano row whose ``on_foot`` is known.

COLUMNAR ONLY (issue #157's memory contract). This runs in the per-run
summarizer, the per-city memory high-water mark inside the nightly child
(measured 0.914 GiB per million CSV rows), so it takes one-dimensional numpy
arrays and never builds a frame, never loops per row and never ``.apply``s a
Python callable: the per-sequence medians are a ``lexsort`` plus two gathers.

Usage::

    block = quality_block(quality, sequence_codes, on_foot)
    if block is not None:
        meta["quality"] = block
"""

from __future__ import annotations

import csv
import gzip
from typing import Any

import numpy as np

QUALITY_COLUMN = "quality_score"

# Every column the block is built from. The backfill reads only these, so
# splicing a block into an old JSON costs four columns of a census, not all.
BLOCK_COLUMNS = ("status", QUALITY_COLUMN, "on_foot", "sequence_id")

# The two tail shares. The median compresses most cities into one 0.07-wide
# band (58.8% of 388 measured); these are where cities actually separate.
# The study script imports them from here, so the study and the published
# numbers cannot use two different cut-offs.
GOOD_THRESHOLD = 0.90
POOR_THRESHOLD = 0.60

# Published values are rounded like the median mapillary_meta always carried.
DECIMALS = 3

# on_foot encoding shared with the study: 1 on foot, 0 vehicle, -1 unknown.
FOOT_ON = 1
FOOT_VEHICLE = 0
FOOT_UNKNOWN = -1


def has_quality_column(csv_gz_path: str) -> bool:
    """Does this run CSV carry ``quality_score`` (the 2026-07-24 schema)?

    Reads the header line only -- deciding whether a census is worth loading
    must never cost loading it.
    """
    with gzip.open(csv_gz_path, "rt", encoding="utf-8", newline="") as fh:
        header = fh.readline()
    return QUALITY_COLUMN in next(csv.reader([header]), [])


def _round(value: float) -> float:
    return round(float(value), DECIMALS)


def _percentiles(values: np.ndarray, points: tuple[int, ...]) -> list[float | None]:
    """numpy's default linear interpolation, or Nones for an empty sample."""
    if values.size == 0:
        return [None] * len(points)
    return [_round(v) for v in np.percentile(values, points)]


def _median_or_none(values: np.ndarray) -> float | None:
    return _round(np.median(values)) if values.size else None


def _pct(numerator: int, denominator: int) -> float | None:
    return _round(100.0 * numerator / denominator) if denominator else None


def per_sequence_medians(quality: np.ndarray, codes: np.ndarray) -> np.ndarray:
    """One median per sequence code, columnar.

    ``quality`` must be finite and ``codes`` non-negative (the caller filters
    both). Sorting by (code, quality) lays each sequence out as one contiguous
    sorted run, so its median is the mean of the run's two middle elements --
    the same value ``groupby(...).median()`` returns, without a frame.

    Args:
        quality: Finite quality scores, one per image.
        codes: Sequence code per image, from ``pd.factorize``.

    Returns:
        float64 array, one median per distinct code, in code order.

    Example::

        >>> per_sequence_medians(np.array([0.9, 0.1, 0.5, 0.7]), np.array([1, 0, 0, 1]))
        array([0.3, 0.8])
    """
    if quality.size == 0:
        return np.empty(0, dtype="float64")
    order = np.lexsort((quality, codes))
    sorted_q = quality[order]
    sorted_codes = codes[order]
    # Group boundaries: the first index of each run of equal codes.
    starts = np.flatnonzero(np.r_[True, sorted_codes[1:] != sorted_codes[:-1]])
    counts = np.diff(np.r_[starts, sorted_codes.size])
    lo = sorted_q[starts + (counts - 1) // 2]
    hi = sorted_q[starts + counts // 2]
    return (lo + hi) / 2.0


def quality_block(
    quality: np.ndarray, sequence_codes: np.ndarray, on_foot: np.ndarray
) -> dict[str, Any] | None:
    """The published ``mapillary_meta.quality`` block for one run's pano census.

    All three arrays are aligned, one element per PANO row (status OK or
    NO_DATE) -- the caller applies that mask.

    Args:
        quality: float64, NaN where the row carries no score.
        sequence_codes: int, ``pd.factorize`` codes of ``sequence_id``, -1 for
            an image with no sequence.
        on_foot: int8 in the module's encoding (1 on foot, 0 vehicle, -1
            unknown).

    Returns:
        The block, or None when no pano is scored -- "not measured" must stay
        distinguishable from "measured zero", so the caller OMITS the key
        rather than writing zeros or nulls.
    """
    scored = np.isfinite(quality)
    q = quality[scored]
    if q.size == 0:
        return None

    p10, p25, p50, p75, p90 = _percentiles(q, (10, 25, 50, 75, 90))

    # Sequence-weighted: images with no sequence (code -1) drop out of THIS cut
    # only. Inventing one sequence per image would drag the sequence-weighted
    # numbers onto the image-weighted ones, which is the comparison being made.
    usable = scored & (sequence_codes >= 0)
    seq_medians = per_sequence_medians(quality[usable], sequence_codes[usable])
    sq25, sq50, sq75 = _percentiles(seq_medians, (25, 50, 75))

    n_foot_known = int((on_foot >= 0).sum())
    return {
        "n_scored": int(q.size),
        "p10": p10,
        "p25": p25,
        "p50": p50,
        "p75": p75,
        "p90": p90,
        "pct_ge_good": _pct(int((q >= GOOD_THRESHOLD).sum()), q.size),
        "pct_lt_poor": _pct(int((q < POOR_THRESHOLD).sum()), q.size),
        "n_sequences": int(seq_medians.size),
        "seq_p25": sq25,
        "seq_p50": sq50,
        "seq_p75": sq75,
        "p50_on_foot": _median_or_none(quality[scored & (on_foot == FOOT_ON)]),
        "p50_vehicle": _median_or_none(quality[scored & (on_foot == FOOT_VEHICLE)]),
        "n_on_foot": int((on_foot == FOOT_ON).sum()),
        "n_foot_known": n_foot_known,
    }
