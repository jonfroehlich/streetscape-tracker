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

The same reader must hand back the coordinates on disk, too (issue #425): the
road-walk scorer keys samples to rows on a 9-decimal rounding, so a parse one
ULP off its own text scores a sample uncovered.
"""

import os
from datetime import date

import pandas as pd

from streetscape_metadata_tracker.analysis import OUT_OF_RADIUS, calculate_run_stats
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.naming import generate_run_filename, generate_streetwalk_filename
from tests.conftest import make_city_df, make_mapillary_city_df, write_city_csv_gz

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

    Scope, because the loader docstring now states it too: this covers every
    shape a provider has ever written. It is NOT "to_datetime can no longer
    raise" — a timezone-aware value beside a naive one raises through
    errors="coerce" under format="ISO8601". No writer in the repo can emit one
    (standardize_capture_date returns YYYY-MM-DD or None; both census decoders
    strftime the same), so it is documented rather than guarded.
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


# --- issue #367: the loader is the query-radius seam ------------------------


def _with_far_pano(df):
    """Move the first pano ~1.1 km north of its query point (0.01 degrees)."""
    df = df.copy()
    df.loc[0, "pano_lat"] = float(df.loc[0, "query_lat"]) + 0.01
    return df


def _run_path(data_dir, provider):
    name = generate_run_filename("bend--oregon--united-states", 100, 100, 20, RUN_DATE, provider)
    return os.path.join(data_dir, name + ".csv.gz")


def test_a_gsv_run_loads_its_far_pano_as_out_of_radius(data_dir):
    """The default read applies the rule; raw=True returns the file as written,
    which still records what Google said."""
    path = _run_path(data_dir, "gsv")
    write_city_csv_gz(
        _with_far_pano(make_city_df([("far", "2024-01-01"), ("near", "2024-01-01")])), path
    )

    loaded = load_city_csv_file(path)
    assert loaded["status"].tolist() == [OUT_OF_RADIUS, "OK", "ZERO_RESULTS"]
    assert loaded["query_distance_m"].iloc[0] > 1000

    raw = load_city_csv_file(path, raw=True)
    assert raw["status"].tolist() == ["OK", "OK", "ZERO_RESULTS"]
    assert "query_distance_m" not in raw.columns


def test_a_census_run_is_never_filtered_by_the_loader(data_dir):
    """Gated on the file's OWN provider token: a Mapillary census assigns
    panos to points from exact tile geometry, so a far one is not an overshoot."""
    path = _run_path(data_dir, "mapillary")
    write_city_csv_gz(_with_far_pano(make_mapillary_city_df([("m1", "2024-01-01")])), path)

    loaded = load_city_csv_file(path)
    assert loaded["status"].iloc[0] == "OK"
    assert "query_distance_m" not in loaded.columns


def test_a_gsv_road_walk_is_never_filtered_by_the_loader(data_dir):
    """A gsv road walk carries the gsv provider (no token) but is not a GRID
    run: its sample-to-pano distance is bounded by the walk's own match
    distance, so the gate reads the file KIND as well as the provider."""
    name = generate_streetwalk_filename(
        "bend--oregon--united-states", 100, 100, 20, 15, RUN_DATE, provider="gsv"
    )
    path = os.path.join(data_dir, name + ".csv.gz")
    write_city_csv_gz(_with_far_pano(make_city_df([("far", "2024-01-01")])), path)

    loaded = load_city_csv_file(path)
    assert loaded["status"].iloc[0] == "OK"
    assert "query_distance_m" not in loaded.columns


# --- issue #425: coordinates load as their own text --------------------------

# A longitude from a real walk CSV (Seattle, dev catalog): pandas' default C
# parser returns the float one ULP below float() of this text, and that lands
# round(x, 9) on -122.268117222 instead of -122.268117223 -- a different
# quantize_coord key, so the sample would miss its own row (issue #425).
BOUNDARY_LON_TEXT = "-122.26811722250001"
BOUNDARY_LAT = 47.67595068398769


def test_a_coordinate_the_default_parser_misplaces_loads_as_its_own_text(data_dir):
    """The loader is round-trip: a loaded coordinate equals float() of its text,
    so the 9-decimal key every scorer uses is the key of what was written."""
    exact = float(BOUNDARY_LON_TEXT)
    assert repr(exact) == BOUNDARY_LON_TEXT  # the writer's repr IS this text
    path = os.path.join(data_dir, "walk.csv.gz")
    write_city_csv_gz(
        make_city_df([("p1", "2024-01-01")], run_date=RUN_DATE, grid_origin=(BOUNDARY_LAT, exact)),
        path,
    )
    # The premise, re-measured: the default parser gets this text wrong. If this
    # assertion ever fails, pandas fixed its parser; retire the premise and keep
    # the contract below.
    default = pd.read_csv(path, usecols=["query_lon"])["query_lon"].iloc[0]
    assert default != exact, "pandas' default C parser now round-trips this text"
    assert round(float(default), 9) != round(exact, 9)

    for raw in (False, True):
        loaded = load_city_csv_file(path, raw=raw)
        lat, lon = loaded["query_lat"].tolist()[0], loaded["query_lon"].tolist()[0]
        assert lon == exact
        assert lat == BOUNDARY_LAT
        assert (round(lat, 9), round(lon, 9)) == (round(BOUNDARY_LAT, 9), round(exact, 9))
