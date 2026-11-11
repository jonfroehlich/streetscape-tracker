"""
Tests for the systemic-failure run guard (analysis.detect_systemic_failure)
and the provider-aware visualization filter (vis.create_visualization_map).

Motivated by a real incident: a malformed API key produced a run of 203k
REQUEST_DENIED rows that was silently cataloged with all-zero stats, and
Mapillary visualizations were always empty because the map builder filtered
every provider's rows to Google copyright.
"""

from datetime import date

import pandas as pd

from streetscape_metadata_tracker.analysis import (
    GOOGLE_COPYRIGHT,
    calculate_run_stats,
    detect_systemic_failure,
    is_google_copyright,
)


def _status_df(statuses):
    return pd.DataFrame({"status": statuses})


class TestDetectSystemicFailure:
    def test_all_request_denied_is_rejected(self):
        reason = detect_systemic_failure(_status_df(["REQUEST_DENIED"] * 500))
        assert reason is not None
        assert "REQUEST_DENIED=500" in reason

    def test_all_over_query_limit_is_rejected(self):
        reason = detect_systemic_failure(_status_df(["OVER_QUERY_LIMIT"] * 100))
        assert reason is not None
        assert "OVER_QUERY_LIMIT=100" in reason

    def test_mixed_denials_are_summed(self):
        statuses = ["REQUEST_DENIED"] * 60 + ["OVER_QUERY_LIMIT"] * 39 + ["OK"]
        reason = detect_systemic_failure(_status_df(statuses))
        assert reason is not None

    def test_healthy_run_passes(self):
        statuses = ["OK"] * 60 + ["ZERO_RESULTS"] * 39 + ["REQUEST_DENIED"]
        assert detect_systemic_failure(_status_df(statuses)) is None

    def test_all_zero_results_is_valid(self):
        # "No imagery anywhere" is a real answer (remote areas), not a failure
        assert detect_systemic_failure(_status_df(["ZERO_RESULTS"] * 1000)) is None

    def test_below_threshold_passes(self):
        statuses = ["REQUEST_DENIED"] * 94 + ["OK"] * 6
        assert detect_systemic_failure(_status_df(statuses)) is None

    def test_at_threshold_is_rejected(self):
        statuses = ["REQUEST_DENIED"] * 95 + ["OK"] * 5
        assert detect_systemic_failure(_status_df(statuses)) is not None

    def test_empty_df_passes(self):
        assert detect_systemic_failure(_status_df([])) is None

    def test_transient_errors_do_not_reject(self):
        # Per-point errors (timeouts etc.) are not credential failures
        assert detect_systemic_failure(_status_df(["ERROR"] * 100)) is None


class TestIsGoogleCopyright:
    """
    Official imagery is exactly '© Google'; substring matching is wrong
    because photographer names can contain 'Google' (found in real data:
    '© MIB 360 - Google Virtual Tours Agency' in the Philadelphia run).
    Must stay in sync with the exact match in www/js/city.js.
    """

    def test_exact_match_only(self):
        s = pd.Series(
            [
                "© Google",  # official, until 2026-10-20
                "© 2026 Google",  # official, from 2026-10-21 (Google stamps the year)
                "© MIB 360 - Google Virtual Tours Agency",  # third party (real)
                "© MIB 2023 - Google Virtual Tours Agency",  # third party with a year (real)
                "© IWorkForGoogleForFree",  # third party (real)
                "© A Google User",  # third party (real)
                "© Lucerna Google",  # third party (real)
                "© 26 Google",  # not a four-digit year
                "© 2026 Google ",  # trailing text is not the official form
                "© Google Loser",  # third party (real)
                "© google",  # not the official form
                "© Jane Photographer",
                None,  # archival: never recorded
            ]
        )
        assert list(is_google_copyright(s)) == [True, True] + [False] * 11

    def test_constant_matches_api_form(self):
        assert GOOGLE_COPYRIGHT == "© Google"

    def test_run_stats_use_exact_match(self):
        df = pd.DataFrame(
            {
                "status": ["OK"] * 3,
                "pano_id": ["a", "b", "c"],
                "capture_date": ["2024-01-01"] * 3,
                "query_lat": [44.5] * 3,
                "query_lon": [-123.2] * 3,
                "pano_lat": [44.5] * 3,
                "pano_lon": [-123.2] * 3,
                "copyright_info": [
                    "© Google",
                    "© MIB 360 - Google Virtual Tours Agency",
                    "© Google Loser",
                ],
            }
        )
        stats = calculate_run_stats(df, date(2026, 7, 6), provider="gsv")
        assert stats["unique_panos"] == 3
        assert stats["unique_google_panos"] == 1


def _pano_df(copyright_info):
    return pd.DataFrame(
        {
            "query_lat": [44.56, 44.57, 44.58],
            "query_lon": [-123.26, -123.27, -123.28],
            "query_timestamp": ["2026-07-06T00:00:00+00:00"] * 3,
            "pano_lat": [44.56, 44.57, 44.58],
            "pano_lon": [-123.26, -123.27, -123.28],
            "pano_id": ["a", "b", "c"],
            "capture_date": ["2021-03-26", "2022-05-01", "2023-08-15"],
            "copyright_info": [copyright_info] * 3,
            "status": ["OK"] * 3,
        }
    )


class TestVisualizationProviderFilter:
    def _marker_count(self, folium_map):
        """Count CircleMarkers anywhere in the map's child tree."""
        import folium

        count = 0
        stack = list(folium_map._children.values())
        while stack:
            child = stack.pop()
            if isinstance(child, folium.CircleMarker):
                count += 1
            stack.extend(child._children.values())
        return count

    def test_mapillary_rows_are_kept_for_mapillary_provider(self):
        from streetscape_metadata_tracker.vis import create_visualization_map

        df = _pano_df("© Mapillary contributor 12345")
        m = create_visualization_map(df, "Testville", provider="mapillary")
        assert self._marker_count(m) == 3

    def test_non_google_rows_are_dropped_for_gsv_provider(self):
        from streetscape_metadata_tracker.vis import create_visualization_map

        df = _pano_df("© Some Third Party")
        m = create_visualization_map(df, "Testville", provider="gsv")
        assert self._marker_count(m) == 0

    def test_google_named_photographer_dropped_for_gsv_provider(self):
        from streetscape_metadata_tracker.vis import create_visualization_map

        df = _pano_df("© MIB 360 - Google Virtual Tours Agency")
        m = create_visualization_map(df, "Testville", provider="gsv")
        assert self._marker_count(m) == 0

    def test_google_rows_are_kept_for_gsv_provider(self):
        from streetscape_metadata_tracker.vis import create_visualization_map

        df = _pano_df("© Google")
        m = create_visualization_map(df, "Testville", provider="gsv")
        assert self._marker_count(m) == 3

    def test_mapillary_viewer_links_in_popups(self):
        from streetscape_metadata_tracker.vis import create_visualization_map

        df = _pano_df("© Mapillary contributor 12345")
        m = create_visualization_map(df, "Testville", provider="mapillary")
        html = m.get_root().render()
        assert "mapillary.com/app/?pKey=" in html
        assert "View in Mapillary" in html
