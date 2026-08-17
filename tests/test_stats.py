"""
Statistics-calculation tests (analysis.py + the stat-shaped json_summarizer
helpers), extending tests/test_coverage.py and tests/test_run_guard.py.

Every expected value is hand-computed and written as a literal, with the
arithmetic in a comment — never derived by mirroring the implementation.
Ages divide day-deltas by 365.25, so capture dates are chosen so the span
crosses exactly span/4 leap days and the age comes out exact (e.g.
2022-01-15 → 2026-01-15 is 1461 days = 4.0 * 365.25 → exactly 4.0 years).
"""

from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import plan_match
from streetscape_metadata_tracker.analysis import (
    EARLIEST_PLAUSIBLE_CAPTURE,
    calculate_coverage_stats,
    calculate_pano_stats,
    calculate_run_stats,
    implausible_capture_date_count,
)
from streetscape_metadata_tracker.download_common import standardize_capture_date
from streetscape_metadata_tracker.json_summarizer import merge_capture_date_histograms
from tests.conftest import COLUMNS, make_city_df

RUN_DATE = date(2026, 1, 15)
NOW = pd.Timestamp(RUN_DATE)

# Capture dates whose ages at RUN_DATE are exact (span crosses span/4 leap days):
#   2022-01-15 → 2026-01-15 = 365+365+366+365 = 1461 d = 4.0 * 365.25 → 4.0 y
#   2018-01-15 → 2026-01-15 = 8*365 + 2 (Feb 2020, 2024) = 2922 d      → 8.0 y
#   2014-01-15 → 2026-01-15 = 12*365 + 3 (2016, 2020, 2024) = 4383 d   → 12.0 y
CAPTURE_4Y = "2022-01-15"
CAPTURE_8Y = "2018-01-15"
CAPTURE_12Y = "2014-01-15"


# ── AgeStats: hand-computed median/avg/stdev/percentiles ────────────────────


def test_age_stats_three_panos_hand_computed():
    """Ages 4.0, 8.0, 12.0 years: every AgeStats field checked by hand."""
    df = make_city_df([("a", CAPTURE_4Y), ("b", CAPTURE_8Y), ("c", CAPTURE_12Y)])
    ages = calculate_pano_stats(df, NOW).age_stats

    assert ages.count == 3
    # min/max of the capture dates, as Timestamp.isoformat()
    assert ages.oldest_pano_date == "2014-01-15T00:00:00"
    assert ages.newest_pano_date == "2022-01-15T00:00:00"
    # mean = (4 + 8 + 12) / 3 = 8.0; median = middle of {4, 8, 12} = 8.0
    assert ages.avg_pano_age_years == pytest.approx(8.0, abs=1e-9)
    assert ages.median_pano_age_years == pytest.approx(8.0, abs=1e-9)
    # SAMPLE stdev (ddof=1): sqrt(((4-8)^2 + 0 + (12-8)^2) / (3-1)) = sqrt(16) = 4.0.
    # The population stdev would be sqrt(32/3) = 3.2659... — must NOT match.
    assert ages.stdev_pano_age_years == pytest.approx(4.0, abs=1e-9)
    assert ages.stdev_pano_age_years != pytest.approx(3.2659863, abs=1e-3)
    # Linear-interpolated percentiles over sorted [4, 8, 12] (index = q * 2):
    #   p10: idx 0.2 → 4 + 0.2*(8-4) = 4.8      p25: idx 0.5 → 4 + 0.5*4 = 6.0
    #   p75: idx 1.5 → 8 + 0.5*(12-8) = 10.0    p90: idx 1.8 → 8 + 0.8*4 = 11.2
    pct = ages.age_percentiles_years
    assert pct["p10"] == pytest.approx(4.8, abs=1e-9)
    assert pct["p25"] == pytest.approx(6.0, abs=1e-9)
    assert pct["p75"] == pytest.approx(10.0, abs=1e-9)
    assert pct["p90"] == pytest.approx(11.2, abs=1e-9)


def test_single_pano_age_stats():
    """1 pano: avg == median == its age, stdev None (sample stdev of n=1 is
    undefined — NaN under ddof=1 — and must surface as None, not NaN/0),
    every percentile equals the single age, oldest == newest."""
    df = make_city_df([("solo", CAPTURE_4Y)])
    ages = calculate_pano_stats(df, NOW).age_stats

    assert ages.count == 1
    assert ages.avg_pano_age_years == pytest.approx(4.0, abs=1e-9)
    assert ages.median_pano_age_years == pytest.approx(4.0, abs=1e-9)
    assert ages.stdev_pano_age_years is None
    assert ages.oldest_pano_date == ages.newest_pano_date == "2022-01-15T00:00:00"
    for p in ("p10", "p25", "p75", "p90"):
        assert ages.age_percentiles_years[p] == pytest.approx(4.0, abs=1e-9)


def test_single_pano_distance_stdev_is_zero_not_none():
    """DistanceStats deliberately uses 0.0 (not None) for the n=1 stdev —
    the opposite convention from AgeStats — so pin both here.

    conftest offsets each pano 0.0001° in lat and lon from its query point:
    dist = sqrt(0.0001^2 + 0.0001^2) * 111000 = 0.0001 * 1.4142136 * 111000
         = 15.6978 m.
    """
    df = make_city_df([("solo", CAPTURE_4Y)])
    dist = calculate_coverage_stats(df).pano_distance_stats

    assert dist is not None
    assert dist.stdev_meters == 0.0
    assert dist.min_meters == pytest.approx(15.6978, abs=1e-3)
    assert dist.max_meters == pytest.approx(15.6978, abs=1e-3)
    assert dist.avg_meters == pytest.approx(15.6978, abs=1e-3)
    assert dist.median_meters == pytest.approx(15.6978, abs=1e-3)


def test_zero_pano_run_all_stats_none_not_nan():
    """A run that found no imagery at all: age fields are None (never NaN,
    never 0), distributions empty, counts zero, coverage exactly 0.0."""
    df = make_city_df([], n_empty=3)  # 3 grid points, all ZERO_RESULTS

    results = calculate_pano_stats(df, NOW)
    ages = results.age_stats
    assert ages.count == 0
    assert ages.oldest_pano_date is None
    assert ages.newest_pano_date is None
    assert ages.avg_pano_age_years is None
    assert ages.median_pano_age_years is None
    assert ages.stdev_pano_age_years is None
    assert ages.age_percentiles_years is None  # None, not a dict of Nones
    assert results.yearly_distribution.counts == {}
    assert results.daily_distribution.counts == {}
    dup = results.duplicate_stats
    assert dup.total_unique_panos == 0
    assert dup.total_pano_references == 0
    assert dup.most_referenced_count == 0
    assert dup.average_references_per_pano == 0

    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["total_points"] == 3
    assert stats["status_zero_results"] == 3
    assert stats["unique_panos"] == 0
    assert stats["unique_google_panos"] == 0  # known-empty, not unknown
    assert stats["coverage_rate_pct"] == 0.0
    assert stats["oldest_capture_date"] is None
    assert stats["newest_capture_date"] is None
    assert stats["median_pano_age_years"] is None


# ── Date math: run_date pinning, leap years, fractional ages ────────────────


def test_ages_pinned_to_run_date_not_wall_clock():
    """The same frame under two run_dates one calendar year apart must shift
    every age by exactly 365/365.25 (2026 has no leap day) — and repeated
    calls with the same run_date must be byte-identical. If any stat leaked
    datetime.now(), the exact 4.0/8.0 medians asserted elsewhere in this
    file (valid only relative to run_date) would already fail."""
    df = make_city_df([("a", CAPTURE_4Y), ("b", CAPTURE_8Y)])

    stats_2026 = calculate_run_stats(df, date(2026, 1, 15))
    stats_2027 = calculate_run_stats(df, date(2027, 1, 15))
    # median at 2026-01-15: (4.0 + 8.0) / 2 = 6.0 exactly
    assert stats_2026["median_pano_age_years"] == pytest.approx(6.0, abs=1e-9)
    # 2026-01-15 → 2027-01-15 = 365 days = 365/365.25 = 0.9993155 years
    delta = stats_2027["median_pano_age_years"] - stats_2026["median_pano_age_years"]
    assert delta == pytest.approx(0.9993155, abs=1e-6)

    # Determinism: same inputs → identical output dict, run after run
    assert calculate_run_stats(df, date(2026, 1, 15)) == stats_2026


def test_leap_year_alters_one_calendar_year_age():
    """One calendar year is NOT always 1.0 'years' (365.25-day years):
    2023-07-01 → 2024-07-01 spans Feb 29 2024 = 366 d = 366/365.25 = 1.0020534,
    2024-07-01 → 2025-07-01 spans no leap day = 365 d = 365/365.25 = 0.9993155.
    """
    df = make_city_df([("a", "2023-07-01")])
    stats = calculate_run_stats(df, date(2024, 7, 1))
    assert stats["median_pano_age_years"] == pytest.approx(1.0020534, abs=1e-6)

    df = make_city_df([("a", "2024-07-01")])
    stats = calculate_run_stats(df, date(2025, 7, 1))
    assert stats["median_pano_age_years"] == pytest.approx(0.9993155, abs=1e-6)


def test_pano_captured_on_run_date_has_age_zero():
    """A pano captured on the run date is exactly 0.0 years old (dates and
    run_date both resolve to midnight — no intra-day wall-clock component)."""
    df = make_city_df([("fresh", "2026-01-15")])
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["median_pano_age_years"] == 0.0


def test_fractional_age_92_days():
    """2025-10-15 → 2026-01-15 = 16 (Oct) + 30 (Nov) + 31 (Dec) + 15 (Jan)
    = 92 days = 92/365.25 = 0.2518823 years."""
    df = make_city_df([("a", "2025-10-15")])
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["median_pano_age_years"] == pytest.approx(0.2518823, abs=1e-6)


# ── Reduced-precision capture dates (standardize_capture_date) ──────────────


def test_standardize_capture_date_leap_and_invalid_calendar_boundaries():
    """Calendar-validity boundaries beyond the basic format cases already in
    test_download_common: real leap day kept, impossible dates rejected
    outright (never silently coerced to a nearby valid date)."""
    assert standardize_capture_date("2024-02-29") == "2024-02-29"  # 2024 is leap
    assert standardize_capture_date("2023-02-29") is None  # 2023 is not
    assert standardize_capture_date("2024-02-30") is None  # never a real day
    assert standardize_capture_date("2024-13") is None  # month 13
    assert standardize_capture_date("2024-00") is None  # month 0


def test_year_precision_capture_date_age_integration():
    """A year-only provider date is pinned to Jan 1, and the age math then
    treats it like any full date: '2022' → 2022-01-01, and
    2022-01-01 → 2026-01-01 = 365+365+366+365 = 1461 d = exactly 4.0 y."""
    normalized = standardize_capture_date("2022")
    assert normalized == "2022-01-01"
    df = make_city_df([("y", normalized)])
    stats = calculate_run_stats(df, date(2026, 1, 1))
    assert stats["median_pano_age_years"] == pytest.approx(4.0, abs=1e-9)


def test_month_precision_capture_date_age_integration():
    """A month-only date is pinned to the 1st; captured 'this month' on a
    run dated the 1st means age exactly 0.0."""
    normalized = standardize_capture_date("2024-05")
    assert normalized == "2024-05-01"
    df = make_city_df([("m", normalized)])
    stats = calculate_run_stats(df, date(2024, 5, 1))
    assert stats["median_pano_age_years"] == 0.0


# ── Run stats: status breakdown, duplicates, copyright subsets ──────────────


def _row(lat, pano_id, capture, copyright_info, status):
    ts = "2026-01-15T12:00:00+00:00"
    if status in ("OK", "NO_DATE"):
        return (lat, -121.0, ts, lat + 0.0001, -120.9999, pano_id, capture, copyright_info, status)
    return (lat, -121.0, ts, None, None, None, None, None, status)


def test_run_stats_status_breakdown_with_errors():
    """5 grid points, one per status flavor: the four status counters must
    partition total_points, and error rows join neither coverage nor panos."""
    df = pd.DataFrame(
        [
            _row(44.000, "a", CAPTURE_4Y, "© Google", "OK"),
            _row(44.001, "b", None, "© Google", "NO_DATE"),
            _row(44.002, None, None, None, "ZERO_RESULTS"),
            _row(44.003, None, None, None, "REQUEST_DENIED"),
            _row(44.004, None, None, None, "ERROR"),
        ],
        columns=COLUMNS,
    )
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["total_points"] == 5
    assert stats["status_ok"] == 1
    assert stats["status_no_date"] == 1
    assert stats["status_zero_results"] == 1
    assert stats["status_other"] == 2  # REQUEST_DENIED + ERROR
    assert stats["unique_panos"] == 2  # a + b; error rows carry no pano
    # coverage: 2 covered points / 5 total = 40%
    assert stats["coverage_rate_pct"] == pytest.approx(40.0, abs=1e-9)
    # age from the single dated pano: 2022-01-15 → 2026-01-15 = exactly 4.0 y
    assert stats["median_pano_age_years"] == pytest.approx(4.0, abs=1e-9)

    cov = calculate_coverage_stats(df)
    assert cov.num_points_with_errors == 2


def test_duplicate_pano_counted_once_in_ages_and_counts():
    """Pano 'a' snapped from two adjacent grid points must contribute its age
    ONCE: dedup gives ages {8.0, 4.0} → mean = (8+4)/2 = 6.0, not the
    reference-weighted (8+8+4)/3 = 6.67."""
    df = make_city_df([("a", CAPTURE_8Y), ("a", CAPTURE_8Y), ("b", CAPTURE_4Y)])

    results = calculate_pano_stats(df, NOW)
    ages = results.age_stats
    assert ages.count == 2
    assert ages.avg_pano_age_years == pytest.approx(6.0, abs=1e-9)
    assert ages.median_pano_age_years == pytest.approx(6.0, abs=1e-9)
    # sample stdev of {8, 4}: sqrt(((8-6)^2 + (4-6)^2) / 1) = sqrt(8) = 2.8284271
    assert ages.stdev_pano_age_years == pytest.approx(2.8284271, abs=1e-6)
    # capture-year histogram also deduplicated: one 2018, one 2022
    assert results.yearly_distribution.counts == {2018: 1, 2022: 1}

    dup = results.duplicate_stats
    assert dup.total_unique_panos == 2
    assert dup.total_pano_references == 3
    assert dup.duplicate_reference_count == 1  # 3 refs - 2 unique
    assert dup.most_referenced_count == 2  # 'a' seen from 2 points
    assert dup.panos_with_multiple_refs == 1  # just 'a'
    assert dup.average_references_per_pano == pytest.approx(1.5, abs=1e-9)  # 3/2

    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["unique_panos"] == 2
    assert stats["unique_google_panos"] == 2  # 'a' google-counted once too
    assert stats["median_pano_age_years"] == pytest.approx(6.0, abs=1e-9)


def test_all_no_date_run_counts_panos_but_ages_are_none():
    """A run whose every pano is dateless: pano/coverage totals populated,
    every date-derived stat None (there is no dated subset at all)."""
    df = pd.DataFrame(
        [
            _row(44.000, "nd1", None, "© Google", "NO_DATE"),
            _row(44.001, "nd2", None, "© Google", "NO_DATE"),
            _row(44.002, None, None, None, "ZERO_RESULTS"),
        ],
        columns=COLUMNS,
    )
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["status_no_date"] == 2
    assert stats["unique_panos"] == 2
    assert stats["unique_google_panos"] == 2
    # 2 covered points / 3 = 66.667%
    assert stats["coverage_rate_pct"] == pytest.approx(100 * 2 / 3, abs=1e-9)
    assert stats["oldest_capture_date"] is None
    assert stats["newest_capture_date"] is None
    assert stats["median_pano_age_years"] is None

    results = calculate_pano_stats(df, NOW)
    assert results.duplicate_stats.total_unique_panos == 2  # headline count kept
    assert results.age_stats.count == 0  # dated subset is empty
    assert results.age_stats.median_pano_age_years is None
    assert results.yearly_distribution.counts == {}


def test_unique_google_panos_with_partially_null_copyright():
    """unique_google_panos is None only when copyright is entirely unrecorded;
    a MIX of null and real copyrights is a known subset — null rows simply
    compare False, so only the exact '© Google' pano counts: 1 of 2."""
    df = pd.DataFrame(
        [
            _row(44.000, "g", CAPTURE_4Y, "© Google", "OK"),
            _row(44.001, "u", CAPTURE_8Y, None, "OK"),
            _row(44.002, None, None, None, "ZERO_RESULTS"),
        ],
        columns=COLUMNS,
    )
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["unique_panos"] == 2
    assert stats["unique_google_panos"] == 1


def test_google_only_age_stats_exclude_third_party():
    """google_only recomputes the date stats over the '© Google' subset:
    all-panos mean = (4+8)/2 = 6.0, google-only mean = 4.0 (the 8-year pano
    is a third-party photographer whose name merely contains 'Google')."""
    df = pd.DataFrame(
        [
            _row(44.000, "g1", CAPTURE_4Y, "© Google", "OK"),
            _row(44.001, "t1", CAPTURE_8Y, "© MIB 360 - Google Virtual Tours Agency", "OK"),
            _row(44.002, None, None, None, "ZERO_RESULTS"),
        ],
        columns=COLUMNS,
    )
    all_stats = calculate_pano_stats(df, NOW)
    assert all_stats.age_stats.avg_pano_age_years == pytest.approx(6.0, abs=1e-9)
    assert all_stats.duplicate_stats.total_unique_panos == 2
    assert all_stats.yearly_distribution.counts == {2018: 1, 2022: 1}

    google_stats = calculate_pano_stats(df, NOW, google_only=True)
    assert google_stats.age_stats.count == 1
    assert google_stats.age_stats.avg_pano_age_years == pytest.approx(4.0, abs=1e-9)
    assert google_stats.duplicate_stats.total_unique_panos == 1
    assert google_stats.yearly_distribution.counts == {2022: 1}
    assert google_stats.photographer_stats.photographer_counts == {"© Google": 1}

    # The stored catalog columns take the same view for gsv (issue #213): the
    # third-party pano is the OLDEST imagery in this frame, so an unfiltered
    # column would report 2018 as the city's oldest capture and 6.0 as its
    # median age — neither of which describes any Google drive.
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["unique_panos"] == 2  # headline count still counts both
    assert stats["unique_google_panos"] == 1
    assert stats["oldest_capture_date"] == "2022-01-15T00:00:00"
    assert stats["newest_capture_date"] == "2022-01-15T00:00:00"
    assert stats["median_pano_age_years"] == pytest.approx(4.0, abs=1e-9)


def test_run_stats_age_columns_keep_every_pano_when_copyright_unrecorded():
    """Archival imports (issue #93) recorded no copyright at all, so their
    Google subset is unknown — the age columns then describe every pano rather
    than silently reporting an empty subset as 'no imagery'. Median of ages
    {4.0, 8.0} = 6.0, and the run is otherwise indistinguishable from the
    filtered case above."""
    df = make_city_df([("a", CAPTURE_4Y), ("b", CAPTURE_8Y)], copyright_info=None)

    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["unique_google_panos"] is None  # unknown, not zero
    assert stats["oldest_capture_date"] == "2018-01-15T00:00:00"
    assert stats["newest_capture_date"] == "2022-01-15T00:00:00"
    assert stats["median_pano_age_years"] == pytest.approx(6.0, abs=1e-9)


def test_mapillary_run_stats_unaffected_by_copyright_filter():
    """Mapillary has no copyright concept — every row is provider imagery — so
    the gsv-only '© Google' narrowing must not touch it. The same frame read as
    mapillary keeps both panos in the age columns (median 6.0) where gsv would
    keep neither (the copyright string is not '© Google')."""
    df = make_city_df([("a", CAPTURE_4Y), ("b", CAPTURE_8Y)], copyright_info="© contributor")

    stats = calculate_run_stats(df, RUN_DATE, provider="mapillary")
    assert stats["unique_google_panos"] is None
    assert stats["median_pano_age_years"] == pytest.approx(6.0, abs=1e-9)
    assert stats["oldest_capture_date"] == "2018-01-15T00:00:00"

    gsv_stats = calculate_run_stats(df, RUN_DATE)
    assert gsv_stats["unique_google_panos"] == 0
    assert gsv_stats["median_pano_age_years"] is None


# ── Impossible capture dates (issue #213) ───────────────────────────────────


def test_impossible_capture_dates_excluded_from_every_dated_stat():
    """A pano dated after the run that observed it, and one dated before Street
    View existed, are both dropped from every date-derived statistic — they are
    real rows in the CSV whose dates cannot be true (corrupt contributor EXIF).
    Left alone they own the min AND the max, so oldest/newest report 1970 and
    2611 for the whole city; the surviving 4y/8y panos give median 6.0."""
    df = pd.DataFrame(
        [
            _row(44.000, "ok1", CAPTURE_4Y, "© Google", "OK"),
            _row(44.001, "ok2", CAPTURE_8Y, "© Google", "OK"),
            _row(44.002, "future", "2611-09-01", "© Google", "OK"),
            _row(44.003, "ancient", "1970-08-01", "© Google", "OK"),
        ],
        columns=COLUMNS,
    )

    stats = calculate_run_stats(df, RUN_DATE)
    # Pano/coverage totals are NOT narrowed: the imagery exists, only its
    # claimed date is impossible.
    assert stats["unique_panos"] == 4
    assert stats["unique_google_panos"] == 4
    assert stats["status_ok"] == 4
    assert stats["oldest_capture_date"] == "2018-01-15T00:00:00"
    assert stats["newest_capture_date"] == "2022-01-15T00:00:00"
    assert stats["median_pano_age_years"] == pytest.approx(6.0, abs=1e-9)

    # Same narrowing on the per-run JSON path, which is a separate seam: the
    # histograms would otherwise publish 1970 and 2611 buckets.
    results = calculate_pano_stats(df, NOW)
    assert results.age_stats.count == 2
    assert results.age_stats.avg_pano_age_years == pytest.approx(6.0, abs=1e-9)
    assert results.age_stats.stdev_pano_age_years == pytest.approx(2.8284271, abs=1e-6)
    assert results.age_stats.age_percentiles_years["p90"] == pytest.approx(7.6, abs=1e-9)
    assert results.yearly_distribution.counts == {2018: 1, 2022: 1}
    assert results.daily_distribution.counts == {"2018-01-15": 1, "2022-01-15": 1}
    # ...while the headline pano count still sees all four
    assert results.duplicate_stats.total_unique_panos == 4


def test_earliest_plausible_capture_is_provider_specific():
    """2005 imagery is impossible for Street View (launched 2007) but ordinary
    for Mapillary, whose contributors upload genuinely old photographs — the
    same frame therefore ages differently per provider, and neither answer is a
    fallback for the other."""
    df = make_city_df([("old", "2005-06-01"), ("new", CAPTURE_4Y)], copyright_info=None)

    gsv = calculate_run_stats(df, RUN_DATE)
    assert gsv["oldest_capture_date"] == "2022-01-15T00:00:00"

    mapillary = calculate_run_stats(df, RUN_DATE, provider="mapillary")
    assert mapillary["oldest_capture_date"] == "2005-06-01T00:00:00"


def test_capture_date_on_run_date_survives_the_upper_bound():
    """The ceiling is inclusive: a pano captured ON the run date is ordinary
    (GSV's month-precision dates are pinned to the 1st, so they can only round
    toward the past), and dropping it would erase the freshest imagery of every
    city collected the day it was driven."""
    df = make_city_df([("fresh", RUN_DATE.isoformat())])
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["median_pano_age_years"] == 0.0
    assert stats["newest_capture_date"] == "2026-01-15T00:00:00"

    # One day past it is not ordinary — that pano was captured after the query
    # that saw it.
    df = make_city_df([("fresh", "2026-01-16"), ("real", CAPTURE_4Y)])
    stats = calculate_run_stats(df, RUN_DATE)
    assert stats["newest_capture_date"] == "2022-01-15T00:00:00"


def test_implausible_capture_date_count_reports_the_repairable_runs():
    """The count a repair pass keys on (scripts/recompute_run_stats.py) reads
    the same rule the stats do, so 'this run's published JSON is wrong' can
    never disagree with 'this run's stats dropped something'."""
    df = pd.DataFrame(
        [
            _row(44.000, "ok1", CAPTURE_4Y, "© Google", "OK"),
            _row(44.001, "future", "2611-09-01", "© Google", "OK"),
            _row(44.002, "ancient", "1970-08-01", "© Google", "OK"),
            _row(44.003, "nodate", None, "© Google", "NO_DATE"),
            _row(44.004, None, None, None, "ZERO_RESULTS"),
        ],
        columns=COLUMNS,
    )
    assert implausible_capture_date_count(df, NOW) == 2
    # A dateless pano is not an implausible one — it never claimed a date.
    assert implausible_capture_date_count(make_city_df([("a", CAPTURE_4Y)]), NOW) == 0


def test_plausibility_floor_agrees_with_the_driving_page_guard():
    """plan_match keeps its own literal so the pure-logic module stays
    stdlib-only; two copies of a rule drift, so pin them together here."""
    assert plan_match.EARLIEST_PLAUSIBLE_CAPTURE == EARLIEST_PLAUSIBLE_CAPTURE["gsv"]


# ── json_summarizer.merge_capture_date_histograms ───────────────────────────


def _city(all_yearly, all_daily, google_yearly=None, google_daily=None):
    data = {
        "all_panos": {
            "histogram_of_capture_dates_by_year": all_yearly,
            "histogram_of_capture_dates": all_daily,
        }
    }
    if google_yearly is not None:
        data["google_panos"] = {
            "histogram_of_capture_dates_by_year": google_yearly,
            "histogram_of_capture_dates": google_daily,
        }
    return data


def test_merge_histograms_sums_counts_and_accepts_both_shapes():
    """City A uses the current {'counts': {...}} shape, city B the bare-dict
    legacy shape and has no google_panos section (non-GSV): 2019=2, 2020=3+5=8,
    2021=7; yearly keys become sorted ints, daily keys stay ISO strings."""
    city_a = _city(
        {"counts": {"2019": 2, "2020": 3}},
        {"counts": {"2019-05-01": 2, "2020-06-01": 3}},
        google_yearly={"counts": {"2019": 1}},
        google_daily={"counts": {"2019-05-01": 1}},
    )
    city_b = _city(
        {"2021": 7, "2020": 5},
        {"2021-07-01": 7, "2020-06-01": 5},
    )
    merged = merge_capture_date_histograms([city_a, city_b])

    assert merged["all_panos_yearly"] == {2019: 2, 2020: 8, 2021: 7}
    assert list(merged["all_panos_yearly"]) == [2019, 2020, 2021]  # sorted
    assert merged["all_panos_daily"] == {"2019-05-01": 2, "2020-06-01": 8, "2021-07-01": 7}
    # google sections only merge from the city that has them
    assert merged["google_panos_yearly"] == {2019: 1}
    assert merged["google_panos_daily"] == {"2019-05-01": 1}


def test_merge_histograms_skips_city_missing_required_histograms():
    """A city missing either all_panos histogram is skipped WHOLE — its other
    histogram must not half-merge and skew the global totals."""
    incomplete = {"all_panos": {"histogram_of_capture_dates_by_year": {"2010": 9}}}
    complete = _city({"counts": {"2020": 1}}, {"counts": {"2020-06-01": 1}})
    merged = merge_capture_date_histograms([incomplete, complete])

    assert merged["all_panos_yearly"] == {2020: 1}  # no 2010 leak
    assert merged["all_panos_daily"] == {"2020-06-01": 1}


def test_merge_histograms_drops_unparseable_year_keys_only():
    """A malformed year key is dropped; the city's other keys still merge."""
    city = _city({"counts": {"unknown": 5, "2020": 1}}, {"counts": {}})
    merged = merge_capture_date_histograms([city])
    assert merged["all_panos_yearly"] == {2020: 1}


def test_merge_histograms_empty_input():
    merged = merge_capture_date_histograms([])
    assert merged == {
        "all_panos_yearly": {},
        "google_panos_yearly": {},
        "all_panos_daily": {},
        "google_panos_daily": {},
    }
