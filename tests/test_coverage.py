"""
Coverage-definition tests (issue #90).

coverage_rate must be grid-point coverage — points with >= 1 pano / total
points — not unique-panos / points. The unique-based rate is a density
proxy: it collapses as the sampling step shrinks (duplicate snaps) and
made every city's coverage appear to plummet vs the published site.
"""

import math
import pathlib
import re
from datetime import date

import numpy as np
import pandas as pd
import pytest

from streetscape_metadata_tracker.analysis import (
    GSV_QUERY_RADIUS_M,
    OUT_OF_RADIUS,
    apply_query_radius,
    calculate_coverage_stats,
    calculate_pano_stats,
    calculate_run_stats,
    detect_systemic_failure,
    out_of_radius_count,
)
from streetscape_metadata_tracker.checkpointing import CENSUS_PROVIDERS
from streetscape_metadata_tracker.geoutils import EARTH_RADIUS_M, haversine_m
from tests.conftest import (
    COLUMNS,
    make_city_df,
    make_kartaview_city_df,
    make_mapillary_city_df,
    make_panoramax_city_df,
)


class TestCalculateCoverageStats:
    def test_gsv_duplicate_panos_do_not_reduce_coverage(self):
        # 3 OK points but only 2 unique panos (adjacent points snapped to
        # the same pano) + 2 empty points: coverage is 3/5, not 2/5
        df = make_city_df(
            [("a", "2020-01-01"), ("a", "2020-01-01"), ("b", "2021-01-01")], n_empty=2
        )
        cov = calculate_coverage_stats(df)
        assert cov.num_points_with_panos == 3
        assert cov.num_points_without_panos == 2
        assert cov.num_points_with_errors == 0
        assert cov.num_points_with_unique_pano_ids == 2  # separate metric
        assert abs(cov.coverage_rate - 60.0) < 1e-9

    def test_error_rows_count_as_error_points(self):
        # A NO_DATE-only point holds present-but-dateless imagery, so it
        # counts as covered; only genuine errors (REQUEST_DENIED) are errors.
        ts = "2026-01-15T12:00:00+00:00"
        df = pd.DataFrame(
            [
                (44.000, -121.0, ts, 44.0001, -121.0001, "a", "2020-01-01", "© Google", "OK"),
                (44.001, -121.0, ts, None, None, None, None, None, "ZERO_RESULTS"),
                (44.002, -121.0, ts, None, None, None, None, None, "REQUEST_DENIED"),
                (44.003, -121.0, ts, 44.0031, -121.0031, "d", None, "© Google", "NO_DATE"),
            ],
            columns=COLUMNS,
        )
        cov = calculate_coverage_stats(df)
        assert cov.num_points_with_panos == 2  # OK + NO_DATE
        assert cov.num_points_without_panos == 1  # ZERO_RESULTS
        assert cov.num_points_with_errors == 1  # REQUEST_DENIED
        assert cov.num_points_with_unique_pano_ids == 2  # dateless pano counts
        assert abs(cov.coverage_rate - 50.0) < 1e-9

    def test_mapillary_census_rows_count_grid_points_once(self):
        # 6 panos on 2 grid points (3 rows each) + 1 empty point:
        # coverage is 2/3 points, while unique panos stays a census count
        df = make_mapillary_city_df([(f"m{i}", "2022-01-01") for i in range(6)], panos_per_point=3)
        cov = calculate_coverage_stats(df)
        assert cov.num_points_with_panos == 2
        assert cov.num_points_without_panos == 1
        assert cov.num_points_with_unique_pano_ids == 6
        assert abs(cov.coverage_rate - 100 * 2 / 3) < 1e-9

    def test_mapillary_point_with_both_ok_and_no_date_rows_is_covered(self):
        # A grid point holding an OK pano and a NO_DATE pano is covered,
        # not an error point
        ts = "2026-01-15T12:00:00+00:00"
        df = pd.DataFrame(
            [
                (44.0, -121.0, ts, 44.0001, -121.0001, "m1", "2022-01-01", "© contributor", "OK"),
                (44.0, -121.0, ts, 44.0002, -121.0002, "m2", None, "© contributor", "NO_DATE"),
                (44.001, -121.0, ts, None, None, None, None, None, "ZERO_RESULTS"),
            ],
            columns=COLUMNS,
        )
        cov = calculate_coverage_stats(df)
        assert cov.num_points_with_panos == 1
        assert cov.num_points_without_panos == 1
        assert cov.num_points_with_errors == 0
        assert abs(cov.coverage_rate - 50.0) < 1e-9

    def test_empty_frame_is_zero_coverage(self):
        df = pd.DataFrame([], columns=COLUMNS)
        cov = calculate_coverage_stats(df)
        assert cov.coverage_rate == 0
        assert cov.num_points_with_panos == 0


class TestCatalogedCoverageRate:
    def test_run_stats_store_point_coverage_for_gsv(self):
        df = make_city_df(
            [("a", "2020-01-01"), ("a", "2020-01-01"), ("b", "2021-01-01")], n_empty=2
        )
        stats = calculate_run_stats(df, date(2026, 1, 15))
        # For GSV (one row per point) with no NO_DATE panos this equals
        # (status_ok + status_no_date) / total_points == status_ok / total.
        assert abs(stats["coverage_rate_pct"] - 60.0) < 1e-9
        assert (
            abs(
                stats["coverage_rate_pct"]
                - 100.0 * (stats["status_ok"] + stats["status_no_date"]) / stats["total_points"]
            )
            < 1e-9
        )

    def test_run_stats_store_point_coverage_for_mapillary(self):
        df = make_mapillary_city_df([(f"m{i}", "2022-01-01") for i in range(6)], panos_per_point=3)
        stats = calculate_run_stats(df, date(2026, 1, 15), provider="mapillary")
        # Rows are per-pano here, so the point rate must NOT be the row
        # rate (6 OK rows / 7 rows)
        assert stats["status_ok"] == 6 and stats["total_points"] == 7
        assert abs(stats["coverage_rate_pct"] - 100 * 2 / 3) < 1e-9


_CENSUS_BUILDERS = {
    "kartaview": make_kartaview_city_df,
    "mapillary": make_mapillary_city_df,
    "panoramax": make_panoramax_city_df,
}


class TestGridPointsAreNotRows:
    """Issue #289: ``total_points`` and the ``status_*`` buckets are ROW counts,
    and ``total_grid_points`` is the grid size.

    The census fixture is 6 images at 3 per point (2 covered points), 2 empty
    points and 1 flat-only point: 9 rows on 5 grid points. The expected 5 is
    written down rather than re-derived with the code's own dedupe, so the test
    cannot agree with a wrong implementation by construction.
    """

    def _census_df(self, provider):
        return _CENSUS_BUILDERS[provider](
            [(f"{provider[0]}{i}", "2022-01-01") for i in range(6)],
            panos_per_point=3,
            n_empty=2,
            n_flat_only=1,
        )

    def test_every_census_provider_has_a_builder_here(self):
        # A census provider added later must join the parametrization below,
        # or its grid size goes unpinned while the suite stays green.
        assert set(_CENSUS_BUILDERS) == set(CENSUS_PROVIDERS)

    @pytest.mark.parametrize("provider", sorted(_CENSUS_BUILDERS))
    def test_census_total_grid_points_is_the_distinct_point_count(self, provider):
        df = self._census_df(provider)
        stats = calculate_run_stats(df, date(2026, 1, 15), provider=provider)
        assert stats["total_grid_points"] == 5
        # The row counts keep their meaning (option 2 of #289): images + empty
        # points, and status_ok counts IMAGES, not covered points.
        assert stats["total_points"] == len(df) == 9
        assert stats["status_ok"] == 6
        assert stats["total_grid_points"] != stats["total_points"]

    @pytest.mark.parametrize("provider", sorted(_CENSUS_BUILDERS))
    def test_census_stored_coverage_uses_the_grid_point_denominator(self, provider):
        stats = calculate_run_stats(self._census_df(provider), date(2026, 1, 15), provider=provider)
        # 2 covered of 5 points -- and NOT a row rate (6 / 9), which is the
        # plausible-looking wrong answer the issue measured at 3-5x.
        assert stats["coverage_rate_pct"] == pytest.approx(100.0 * 2 / 5)
        assert stats["coverage_rate_pct"] == pytest.approx(100.0 * 2 / stats["total_grid_points"])
        assert stats["any_imagery_coverage_rate_pct"] == pytest.approx(100.0 * 3 / 5)

    def test_gsv_total_grid_points_equals_total_points(self):
        # GSV writes one row per grid point, so the two counts agree -- and the
        # duplicate pano id "a" (two points snapped to one pano) must not merge
        # two POINTS.
        df = make_city_df(
            [("a", "2020-01-01"), ("a", "2020-01-01"), ("b", "2021-01-01")], n_empty=2
        )
        stats = calculate_run_stats(df, date(2026, 1, 15), provider="gsv")
        assert stats["total_grid_points"] == stats["total_points"] == 5
        assert stats["coverage_rate_pct"] == pytest.approx(100.0 * 3 / 5)
        assert stats["coverage_rate_pct"] == pytest.approx(100.0 * 3 / stats["total_grid_points"])


class TestNoDateCountsAsPresent:
    """A pano the provider returned but couldn't date (NO_DATE) is present
    imagery: it counts toward coverage and pano totals, never toward age
    stats, and is not an error point (schema v3)."""

    def _gsv_df_with_no_date(self):
        # 1 dated OK pano, 1 dateless NO_DATE pano (both © Google), 1 empty
        ts = "2026-01-15T12:00:00+00:00"
        return pd.DataFrame(
            [
                (44.000, -121.0, ts, 44.0001, -121.0001, "ok1", "2020-06-01", "© Google", "OK"),
                # Within the 50 m query radius (issue #367); at -121.0011 this
                # pano sat 88 m east and would read as OUT_OF_RADIUS.
                (44.001, -121.0, ts, 44.0011, -121.0001, "nd1", None, "© Google", "NO_DATE"),
                (44.002, -121.0, ts, None, None, None, None, None, "ZERO_RESULTS"),
            ],
            columns=COLUMNS,
        )

    def test_run_stats_counts_no_date_as_pano_and_coverage(self):
        stats = calculate_run_stats(self._gsv_df_with_no_date(), date(2026, 1, 15))
        assert stats["status_ok"] == 1
        assert stats["status_no_date"] == 1
        assert stats["status_zero_results"] == 1
        assert stats["status_other"] == 0
        assert stats["unique_panos"] == 2  # OK + NO_DATE both counted
        assert stats["unique_google_panos"] == 2  # both © Google
        assert abs(stats["coverage_rate_pct"] - 100 * 2 / 3) < 1e-9

    def test_no_date_pano_excluded_from_age_stats(self):
        stats = calculate_run_stats(self._gsv_df_with_no_date(), date(2026, 1, 15))
        # Age reflects only the single dated pano (2020-06-01); the dateless
        # pano must not skew or nullify it.
        assert stats["median_pano_age_years"] is not None
        assert stats["oldest_capture_date"] == stats["newest_capture_date"]
        assert stats["oldest_capture_date"].startswith("2020-06-01")

    def test_pano_stats_total_and_histogram(self):
        results = calculate_pano_stats(self._gsv_df_with_no_date(), pd.Timestamp("2026-01-15"))
        # Website headline pano count is sourced from here — must include the
        # dateless pano.
        assert results.duplicate_stats.total_unique_panos == 2
        # Capture-year histogram is date-based, so it sees only the OK pano.
        assert results.yearly_distribution.counts == {2020: 1}

    def test_point_with_only_no_date_is_covered_not_error(self):
        # A grid point whose sole pano is dateless is covered, not an error.
        ts = "2026-01-15T12:00:00+00:00"
        df = pd.DataFrame(
            [
                (44.000, -121.0, ts, 44.0001, -121.0001, "nd1", None, "© Google", "NO_DATE"),
                (44.001, -121.0, ts, None, None, None, None, None, "ZERO_RESULTS"),
            ],
            columns=COLUMNS,
        )
        cov = calculate_coverage_stats(df)
        assert cov.num_points_with_panos == 1
        assert cov.num_points_with_errors == 0
        assert cov.num_points_with_unique_pano_ids == 1
        assert abs(cov.coverage_rate - 50.0) < 1e-9

    def test_dateless_mapillary_pano_counts_in_census(self):
        # Mapillary census: a point with 2 panos, one of them dateless, still
        # contributes both panos to the unique count and is covered.
        ts = "2026-01-15T12:00:00+00:00"
        df = pd.DataFrame(
            [
                (
                    44.0,
                    -121.0,
                    ts,
                    44.0001,
                    -121.0001,
                    "m1",
                    "2022-01-01",
                    "© Mapillary contributor 1",
                    "OK",
                ),
                (
                    44.0,
                    -121.0,
                    ts,
                    44.0002,
                    -121.0002,
                    "m2",
                    None,
                    "© Mapillary contributor 2",
                    "NO_DATE",
                ),
            ],
            columns=COLUMNS,
        )
        stats = calculate_run_stats(df, date(2026, 1, 15), provider="mapillary")
        assert stats["unique_panos"] == 2
        assert stats["status_no_date"] == 1
        assert stats["unique_google_panos"] is None  # non-GSV
        assert abs(stats["coverage_rate_pct"] - 100.0) < 1e-9


class TestImageryTypeStratification:
    """Issue #116: FLAT_ONLY points widen any-imagery coverage but must not
    change the GSV-comparable 360° coverage_rate."""

    def test_flat_only_counts_toward_any_imagery_not_360(self):
        # 2 pano points + 1 flat-only point + 1 empty point = 4 total.
        # 360° coverage = 2/4 = 50%; any-imagery = 3/4 = 75%.
        df = make_mapillary_city_df(
            [("m1", "2021-01-01"), ("m2", "2022-01-01")], n_flat_only=1, n_empty=1
        )
        cov = calculate_coverage_stats(df)
        assert cov.num_points_with_panos == 2
        assert cov.num_points_with_any_imagery == 3
        assert cov.coverage_rate == 50.0
        assert cov.any_imagery_coverage_rate == 75.0
        # FLAT_ONLY is not an error, so it doesn't inflate the error bucket
        assert cov.num_points_with_errors == 0
        # ...and it doesn't count as a "point without panos" (that's ZERO_RESULTS)
        assert cov.num_points_without_panos == 1

    def test_gsv_any_imagery_equals_360(self):
        # GSV never emits FLAT_ONLY, so the two rates are identical there.
        df = make_city_df([("a", "2020-01-01"), ("b", "2021-01-01")], n_empty=2)
        cov = calculate_coverage_stats(df)
        assert cov.any_imagery_coverage_rate == cov.coverage_rate
        assert cov.num_points_with_any_imagery == cov.num_points_with_panos

    def test_run_stats_bucket_flat_only_separately(self):
        # status_flat_only is its own bucket, split out of status_other (like
        # NO_DATE in v3); any_imagery_coverage_rate_pct is exposed for the DB.
        df = make_mapillary_city_df(
            [("m1", "2021-01-01"), ("m2", "2022-01-01")], n_flat_only=2, n_empty=1
        )
        stats = calculate_run_stats(df, date(2026, 1, 15), provider="mapillary")
        assert stats["status_ok"] == 2
        assert stats["status_flat_only"] == 2
        assert stats["status_zero_results"] == 1
        assert stats["status_other"] == 0  # flat-only NOT folded into errors
        # 2 pano points / 5 total = 40%; any-imagery 4/5 = 80%
        assert stats["coverage_rate_pct"] == 40.0
        assert stats["any_imagery_coverage_rate_pct"] == 80.0
        # Flat-only rows are not panos: unique_panos counts only OK/NO_DATE
        assert stats["unique_panos"] == 2

    def test_flat_only_not_treated_as_systemic_failure(self):
        # A run that is all FLAT_ONLY is a valid "flat imagery everywhere, no
        # panos" answer, not a credential/quota failure — must not be rejected.
        df = make_mapillary_city_df([], n_flat_only=5, n_empty=0)
        assert detect_systemic_failure(df) is None


class TestPanoStatsCoverage:
    def test_google_only_summary_keeps_run_level_coverage(self):
        # The google_only filter drops ZERO_RESULTS rows (null copyright);
        # coverage must still describe the whole sampled grid
        df = make_city_df([("a", "2020-01-01"), ("b", "2021-01-01")], n_empty=1)
        results = calculate_pano_stats(df, pd.Timestamp("2026-01-15"), google_only=True)
        cov = results.coverage_stats
        assert cov.num_points_without_panos == 1
        assert abs(cov.coverage_rate - 100 * 2 / 3) < 1e-9


# --- issue #367: the GSV query radius ----------------------------------------

# Metres per degree along a meridian under the same sphere haversine_m uses, so
# a pano placed `m / _M_PER_DEG` degrees north of its query point is exactly
# (to float precision) m metres away.
_M_PER_DEG = EARTH_RADIUS_M * math.pi / 180


def _gsv_frame(pano_offsets_m, statuses=None, n_empty=1):
    """A GSV run with one grid point per offset: the pano sits that many metres
    due north of its query point. None as an offset means no pano coordinates
    (a row whose distance cannot be measured). Plus ``n_empty`` ZERO_RESULTS
    points."""
    ts = "2026-01-15T12:00:00+00:00"
    statuses = statuses or ["OK"] * len(pano_offsets_m)
    rows = []
    for i, (m, status) in enumerate(zip(pano_offsets_m, statuses, strict=True)):
        qlat = 44.0 + i * 0.001
        plat = None if m is None else qlat + m / _M_PER_DEG
        plon = None if m is None else -121.0
        date_ = "2024-06-01" if status == "OK" else None
        rows.append((qlat, -121.0, ts, plat, plon, f"p{i}", date_, "© Google", status))
    for j in range(n_empty):
        qlat = 44.0 + (len(pano_offsets_m) + j) * 0.001
        rows.append((qlat, -121.0, ts, None, None, None, None, None, "ZERO_RESULTS"))
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["capture_date"] = pd.to_datetime(df["capture_date"])
    return df


class TestQueryRadius:
    def test_far_panos_are_uncovered_answers_not_errors(self):
        # 10 m and 49 m are within 50 m; 51 m and 3,000 km (issue #367's
        # continental outliers) are not. 5 points: 4 with a pano + 1 empty.
        df = apply_query_radius(_gsv_frame([10, 49, 51, 3_000_000]))

        assert df["status"].tolist() == [
            "OK",
            "OK",
            OUT_OF_RADIUS,
            OUT_OF_RADIUS,
            "ZERO_RESULTS",
        ]
        cov = calculate_coverage_stats(df)
        assert cov.num_points_with_panos == 2
        assert cov.coverage_rate == 40.0  # 2 of 5, not 4 of 5
        assert cov.num_points_out_of_radius == 2
        assert cov.num_points_with_errors == 0  # a real answer, not an error
        assert cov.num_points_without_panos == 1
        assert cov.num_points_with_unique_pano_ids == 2
        # The distance stats describe only what still counts, in real metres
        assert cov.pano_distance_stats.max_meters == pytest.approx(49.0, abs=1e-6)

    def test_run_stats_split_out_of_radius_from_status_other(self):
        df = apply_query_radius(_gsv_frame([10, 49, 51, 3_000_000]))
        stats = calculate_run_stats(df, date(2026, 1, 15), provider="gsv")

        assert stats["status_ok"] == 2
        assert stats["status_out_of_radius"] == 2
        assert stats["status_other"] == 0
        assert stats["unique_panos"] == 2
        assert stats["unique_google_panos"] == 2
        assert stats["coverage_rate_pct"] == 40.0
        assert stats["query_radius_m"] == GSV_QUERY_RADIUS_M == 50.0

    def test_run_stats_apply_the_rule_to_a_raw_frame_themselves(self):
        # query_radius_m = 50.0 is a MEASUREMENT: a raw gsv frame handed
        # straight to calculate_run_stats, with no loader in between, is
        # counted under the rule it reports.
        stats = calculate_run_stats(_gsv_frame([10, 3_000_000]), date(2026, 1, 15))
        assert stats["status_out_of_radius"] == 1
        assert stats["status_ok"] == 1
        assert stats["coverage_rate_pct"] == pytest.approx(100 / 3)

    def test_a_gsv_frame_without_coordinates_is_returned_with_a_warning(self, caplog):
        df = _gsv_frame([3_000_000]).drop(columns=["pano_lat"])
        with caplog.at_level("WARNING", logger="streetscape_metadata_tracker.analysis"):
            out = apply_query_radius(df)
        assert out is df
        assert "lacks pano_lat" in caplog.text

    def test_query_radius_m_is_null_for_a_census_provider(self):
        df = make_mapillary_city_df([("m1", "2024-06-01")])
        assert (
            calculate_run_stats(df, date(2026, 1, 15), provider="mapillary")["query_radius_m"]
            is None
        )

    def test_a_pano_exactly_at_the_radius_is_within_it(self):
        # Strictly greater-than: find a pano latitude whose float distance is
        # EXACTLY 50.0 on this platform's libm, rather than trusting one.
        lat = 50.0 / _M_PER_DEG
        for _ in range(64):
            if haversine_m(0.0, 0.0, lat, 0.0) == GSV_QUERY_RADIUS_M:
                break
            lat = np.nextafter(lat, 1.0 if haversine_m(0.0, 0.0, lat, 0.0) < 50 else 0.0)
        else:
            pytest.fail("no float latitude lands exactly on 50.0 m")
        df = pd.DataFrame(
            [(0.0, 0.0, "2026-01-15T12:00:00+00:00", lat, 0.0, "edge", None, "© Google", "OK")],
            columns=COLUMNS,
        )
        out = apply_query_radius(df)
        assert out["query_distance_m"].iloc[0] == 50.0
        assert out["status"].iloc[0] == "OK"

    def test_a_missing_pano_coordinate_never_reclassifies(self):
        df = apply_query_radius(_gsv_frame([None, 3_000_000]))
        assert df["status"].tolist() == ["OK", OUT_OF_RADIUS, "ZERO_RESULTS"]
        assert pd.isna(df["query_distance_m"].iloc[0])

    def test_a_far_no_date_pano_is_reclassified_too(self):
        # NO_DATE is present imagery, so the radius applies to it exactly as to OK
        df = apply_query_radius(_gsv_frame([20, 500], statuses=["NO_DATE", "NO_DATE"]))
        assert df["status"].tolist() == ["NO_DATE", OUT_OF_RADIUS, "ZERO_RESULTS"]

    def test_the_seam_is_idempotent(self):
        once = apply_query_radius(_gsv_frame([10, 51]))
        twice = apply_query_radius(once)
        pd.testing.assert_frame_equal(once, twice)

    def test_a_census_provider_frame_is_returned_unchanged(self):
        # Mapillary assigns panos to points from exact tile geometry; a pano
        # far from the query point is not an overshoot there.
        df = _gsv_frame([3_000_000])
        out = apply_query_radius(df, provider="mapillary")
        assert out is df
        assert out["status"].tolist() == ["OK", "ZERO_RESULTS"]
        assert out_of_radius_count(df, "mapillary") == 0

    def test_out_of_radius_count_agrees_on_raw_and_loaded_frames(self):
        raw = _gsv_frame([10, 51, 3_000_000])
        assert out_of_radius_count(raw) == 2
        assert out_of_radius_count(apply_query_radius(raw)) == 2

    def test_distance_stats_are_great_circle_not_planar(self):
        # 1 degree of longitude at 60 N is ~55.6 km; the planar
        # sqrt(dlat^2 + dlon^2) * 111000 this replaced said 111 km.
        assert haversine_m(60.0, 0.0, 60.0, 1.0) == pytest.approx(55_597, abs=1)
        df = pd.DataFrame(
            [
                (
                    60.0,
                    0.0,
                    "2026-01-15T12:00:00+00:00",
                    60.0,
                    1.0,
                    "e",
                    "2024-06-01",
                    "© Google",
                    "OK",
                )
            ],
            columns=COLUMNS,
        )
        # Straight into the stats, no seam: DistanceStats computes the distance
        # itself when query_distance_m is absent.
        dist = calculate_coverage_stats(df).pano_distance_stats
        assert dist.max_meters == pytest.approx(55_597, abs=1)

    def test_the_js_query_radius_agrees_with_python(self):
        """The browser drops the same far panos the published stats do, only
        while its copy of the constant and the sphere match this one. Read out
        of the source, as test_the_js_run_filename_regex_agrees_with_python
        does -- there is no Node in the fast suite."""
        js = (
            pathlib.Path(__file__).resolve().parent.parent / "www" / "js" / "streetscape-utils.js"
        ).read_text(encoding="utf-8")
        radius = re.search(r"^const GSV_QUERY_RADIUS_M = ([0-9.]+);$", js, re.MULTILINE)
        earth = re.search(r"^const EARTH_RADIUS_M = ([0-9.]+);$", js, re.MULTILINE)
        assert radius and earth, "streetscape-utils.js no longer declares the #367 constants"
        assert float(radius.group(1)) == GSV_QUERY_RADIUS_M
        assert float(earth.group(1)) == EARTH_RADIUS_M
