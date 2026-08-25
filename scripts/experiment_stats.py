"""
Shared distribution summaries for the docs/experiments/ studies.

Every writeup in docs/experiments/ has to quote the DISTRIBUTION it summarizes —
percentiles and n, not a headline number — because the shape is usually the
finding. That made the same linear-interpolation percentile appear three times
(`night_length_analyze.py`, `publish_duration_analyze.py`,
`kartaview_sweep_cost.py`), differing only in rounding and in whether the
argument was a percent or a fraction.

Three copies of one formula is exactly the drift `experiment_style.py` exists to
prevent for the palette, and it is worse here: two writeups quoting p90s computed
by two implementations would be comparing numbers no one had checked agree. So
the formula lives in one place and the studies keep only what is a claim about
their own data.

Deliberately stdlib-only and dependency-free, matching the scripts that import
it — several of them run without numpy on purpose.
"""

from __future__ import annotations

import math

# The set every study reports. Fixed here so two writeups cannot quote different
# "standard" percentiles and read as though they used one ruler.
PERCENTILES = (25, 50, 75, 90, 95)


def percentile_fraction(values: list[float], q: float) -> float:
    """Linearly interpolated percentile -- i.e. the one that gives a real median.

    ``q`` is a FRACTION (0.0-1.0). This is the primitive; ``percentile`` below is
    the 0-100 spelling. Both exist because the studies are written in different
    units and neither reading should have to convert at every call site.

    The obvious spelling, ``values[int(q * (n - 1))]``, is a lower-index pick and
    is NOT a median on an even-sized sample: over the KartaView study's 14 cities
    it returned the 7th value, 210, where the median is 297. A study that exists
    to publish a distribution cannot mislabel its own middle, so this is numpy's
    default linear interpolation, written out rather than adding a dependency to
    stdlib-only scripts.

    ``pos = q * (n - 1)`` is grouped exactly as ``kartaview_sweep_cost.py`` wrote
    it, and that is load-bearing rather than stylistic: the algebraically equal
    ``(n - 1) * q`` differs by an ULP on some samples, and this repo pins study
    records byte-for-byte. Changing the grouping moves committed numbers.

    Sorts a copy, so callers may pass values in any order; passing an
    already-sorted list is free of surprises rather than merely tolerated.
    """
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def percentile(values: list[float], pct: float) -> float:
    """``percentile_fraction`` in the 0-100 spelling."""
    return percentile_fraction(values, pct / 100.0)


def describe(values: list[float], digits: int = 4) -> dict:
    """n plus min/p25/p50/p75/p90/p95/max -- never a bare median.

    ``{"n": 0}`` for an empty sample rather than raising: a study reports the
    populations it found, and "this cut is empty" is a result the record has to
    be able to carry.

    ``digits`` is the caller's, because it is a claim about their measurement's
    resolution — seconds of publish wall-clock and hours of night length do not
    round alike — and changing a study's rounding would silently move numbers a
    committed record already quotes.
    """
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    out = {"n": len(ordered), "min": round(ordered[0], digits)}
    for pct in PERCENTILES:
        out[f"p{pct}"] = round(percentile(ordered, pct), digits)
    out["max"] = round(ordered[-1], digits)
    return out
