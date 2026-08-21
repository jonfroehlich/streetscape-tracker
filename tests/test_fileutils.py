"""
The run-CSV loader's capture-date contract (issue #226).

fileutils.load_city_csv_file is the upstream gate every date-derived statistic
sits behind — analysis.dated_unique_panos, the per-run JSON's age blocks and
histograms, diff.py's capture-date comparison — so what it can READ bounds what
any of them can compute. It parsed with a strict '%Y-%m-%d', and the legacy
pre-2026 runs carry MONTH precision and are never rewritten, so every date in
them coerced to NaT while the pano counts stayed perfect. That is the failure
mode these tests exist for: a catalog row that looks fully populated and
internally consistent, with NULL oldest/newest/median.
"""

import os
from datetime import date

import pandas as pd

from streetscape_metadata_tracker.analysis import calculate_run_stats
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from tests.conftest import make_city_df, write_city_csv_gz

RUN_DATE = date(2026, 1, 15)


def _load(data_dir, panos, name="run.csv.gz"):
    """Write a synthetic run of (pano_id, capture_date_str) and load it back."""
    path = os.path.join(data_dir, name)
    write_city_csv_gz(make_city_df(panos, run_date=RUN_DATE), path)
    return load_city_csv_file(path)


def test_month_precision_dates_survive_the_load(data_dir):
    """The regression: a legacy run's YYYY-MM dates must reach the stats.

    Pinned to the 1st, matching download_common.standardize_capture_date — GSV
    publishes month precision and the pipeline has always resolved it that way,
    so the loader agreeing is what keeps one run's dates comparable to the next.
    """
    df = _load(data_dir, [("p1", "2022-09"), ("p2", "2024-03")])

    assert df["capture_date"].notna().sum() == 2
    assert list(df["capture_date"].dropna()) == [
        pd.Timestamp("2022-09-01"),
        pd.Timestamp("2024-03-01"),
    ]


def test_month_precision_run_reports_dates_not_nulls(data_dir):
    """End to end through calculate_run_stats, which is what the catalog stores.

    The pano counts are asserted BESIDE the dates deliberately: they were always
    correct, and that is exactly why the bug stayed invisible for so long. A
    test that only checked the dates would not show that the two disagree.
    """
    df = _load(data_dir, [("p1", "2022-09"), ("p2", "2024-03")])
    stats = calculate_run_stats(df, RUN_DATE, provider="gsv")

    assert stats["oldest_capture_date"] == "2022-09-01T00:00:00"
    assert stats["newest_capture_date"] == "2024-03-01T00:00:00"
    assert stats["median_pano_age_years"] is not None
    assert stats["unique_panos"] == 2
    assert stats["unique_google_panos"] == 2


def test_mixed_precision_parses_both_in_either_order(data_dir):
    """Both precisions in one file must BOTH parse, whichever comes first.

    This is what pins format="ISO8601" against the format-free
    pd.to_datetime(errors="coerce") the issue originally suggested: with no
    format, pandas infers ONE from the first non-null value and silently coerces
    everything at another precision to NaT. Measured on pandas 3.0:

        ["2022-09-15", "2022-09"] -> [2022-09-15, NaT]
        ["2022-09", "2022-09-15"] -> [2022-09-01, NaT]

    So a one-way test passes by luck — the assertion has to be made in both
    orderings, or it only ever exercises whichever half the inference picked.
    """
    day_first = _load(data_dir, [("p1", "2022-09-15"), ("p2", "2023-04")], name="a.csv.gz")
    month_first = _load(data_dir, [("p1", "2023-04"), ("p2", "2022-09-15")], name="b.csv.gz")

    assert list(day_first["capture_date"].dropna()) == [
        pd.Timestamp("2022-09-15"),
        pd.Timestamp("2023-04-01"),
    ]
    assert list(month_first["capture_date"].dropna()) == [
        pd.Timestamp("2023-04-01"),
        pd.Timestamp("2022-09-15"),
    ]


def test_year_precision_pins_to_january_first(data_dir):
    """standardize_capture_date resolves YYYY to Jan 1; the loader must agree."""
    df = _load(data_dir, [("p1", "2019")])

    assert list(df["capture_date"].dropna()) == [pd.Timestamp("2019-01-01")]


def test_day_precision_is_unchanged(data_dir):
    """The overwhelmingly common case must read exactly as it always did."""
    df = _load(data_dir, [("p1", "2022-09-15"), ("p2", "2024-03-02")])

    assert list(df["capture_date"].dropna()) == [
        pd.Timestamp("2022-09-15"),
        pd.Timestamp("2024-03-02"),
    ]


def test_unreadable_dates_coerce_rather_than_raise(data_dir):
    """errors="coerce" is kept: a garbage date drops one pano's date, not a run.

    Widening the accepted formats must not turn an unparseable value into an
    exception — a run CSV is an immutable dated snapshot, and refusing to load
    one because a single row is malformed would take out every statistic in it
    rather than the one date that is actually unusable. Same reasoning as
    analysis._dated_unique's own coerce.
    """
    df = _load(data_dir, [("p1", "not-a-date"), ("p2", "2022-09"), ("p3", "2022-13")])

    # "2022-13" is a well-formed shape with an impossible month, so it exercises
    # the parser rather than the regex-shaped rejection "not-a-date" gets.
    assert df["capture_date"].notna().sum() == 1
    assert list(df["capture_date"].dropna()) == [pd.Timestamp("2022-09-01")]


def test_absent_dates_stay_nat(data_dir):
    """A ZERO_RESULTS point and an explicit null both carry no date."""
    df = _load(data_dir, [("p1", "2022-09"), ("p2", None)])

    # 3 rows: two panos plus make_city_df's trailing ZERO_RESULTS point
    assert len(df) == 3
    assert df["capture_date"].isna().sum() == 2
