"""Unit tests for fractional per-edge road-walk coverage (issue #99).

Builds edges + samples + a synthetic collected DataFrame by hand (no network)
and checks the fraction arithmetic, the sample-to-pano distance guard, the
official-Google filter, the PRESENT (OK + NO_DATE) coverage vocabulary
(issue #257), and JSON validity of the coverage artifact.
"""

import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from streetscape_street_analyzer import road_sampling as rs
from streetscape_street_analyzer import street_coverage as sc

LONG_EDGE = LineString([(-121.30, 44.05), (-121.30, 44.052)])
SHORT_EDGE = LineString([(-121.30, 44.052), (-121.30, 44.0525)])
RUN_DATE = "2026-07-08"


def _edges():
    return gpd.GeoDataFrame(
        {"edge_id": ["1_2", "2_3"], "highway": ["residential", "service"], "length": [222.0, 55.0]},
        geometry=[LONG_EDGE, SHORT_EDGE],
        crs="EPSG:4326",
    )


def _collected(
    samples,
    covered_pred,
    *,
    pano_offset=0.0,
    copyright_="© Google",
    date="2022-06-01",
    no_date_pred=None,
):
    """Synthesize a METADATA-shaped collected frame from the sample list.

    `covered_pred(row)` decides whether a sample gets an OK pano; `pano_offset`
    shifts the returned pano's latitude (to exercise the distance guard).
    `no_date_pred(row)` takes precedence and gives the sample a NO_DATE row —
    a located pano with an unusable capture date, which is imagery within reach
    of the street but carries no age.
    """
    rows = []
    for r in samples.itertuples():
        undated = bool(no_date_pred(r)) if no_date_pred else False
        cov = undated or covered_pred(r)
        rows.append(
            {
                "query_lat": r.lat,
                "query_lon": r.lon,
                "pano_lat": (r.lat + pano_offset) if cov else None,
                "pano_lon": r.lon if cov else None,
                "pano_id": ("p" if cov else None),
                "capture_date": (None if undated else (date if cov else None)),
                "copyright_info": (copyright_ if cov else None),
                "status": ("NO_DATE" if undated else ("OK" if cov else "ZERO_RESULTS")),
                "query_timestamp": "2026-07-08T00:00:00Z",
            }
        )
    return pd.DataFrame(rows)


def test_fractional_coverage_per_edge():
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)
    # Cover the first 8 of edge 1_2's 15 samples; none of edge 2_3.
    collected = _collected(samples, lambda r: r.edge_id == "1_2" and r.sample_idx < 8)
    out = sc.compute_streetwalk_coverage(edges, samples, collected, RUN_DATE, "gsv", 25.0)
    by_edge = out.set_index("edge_id")
    assert by_edge.loc["1_2", "total_samples"] == 15
    assert by_edge.loc["1_2", "covered_samples"] == 8
    assert round(by_edge.loc["1_2", "coverage_fraction"], 4) == round(8 / 15, 4)
    assert by_edge.loc["1_2", "covered"]
    assert by_edge.loc["2_3", "covered_samples"] == 0
    assert not by_edge.loc["2_3", "covered"]


def test_distance_guard_rejects_far_pano():
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)
    # Every sample gets an OK Google pano, but ~222 m north (>> 25 m threshold).
    collected = _collected(samples, lambda r: True, pano_offset=0.002)
    out = sc.compute_streetwalk_coverage(edges, samples, collected, RUN_DATE, "gsv", 25.0)
    assert int(out["covered_samples"].sum()) == 0


def test_non_google_copyright_excluded_for_gsv():
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)
    collected = _collected(samples, lambda r: True, copyright_="© Someone Else")
    out = sc.compute_streetwalk_coverage(edges, samples, collected, RUN_DATE, "gsv", 25.0)
    assert int(out["covered_samples"].sum()) == 0


def test_non_gsv_provider_keeps_any_ok_pano():
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)
    collected = _collected(samples, lambda r: True, copyright_=None)
    out = sc.compute_streetwalk_coverage(edges, samples, collected, RUN_DATE, "mapillary", 25.0)
    assert int(out["covered_samples"].sum()) == len(samples)


# ── NO_DATE counts as coverage (issue #257) ──────────────────────────────────
#
# `compute_streetwalk_coverage` filtered on status == "OK" long after 611bd53
# fixed the same bug in `select_pano_points`, so the road-walk half of the
# package disagreed with the grid-attribution half. A NO_DATE sample is a 360°
# pano within `match_dist_m` whose capture date the provider guard nulled: it
# covers, and contributes no age. KartaView makes that population large by
# construction (`shot_date >= date_added` → NULL), so the walk had to be
# corrected before its first artifact was ever written.


def _dated(r):
    """First 8 samples of the long edge get a dated OK pano."""
    return r.edge_id == "1_2" and r.sample_idx < 8


def _undated(r):
    """The next 4 get a located pano with no usable capture date."""
    return r.edge_id == "1_2" and 8 <= r.sample_idx < 12


def test_no_date_sample_counts_toward_coverage():
    """A NO_DATE pano in range covers its edge, in the 360° and the _any numbers alike."""
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)

    before = sc.compute_streetwalk_coverage(
        edges, samples, _collected(samples, _dated), RUN_DATE, "gsv", 25.0
    )
    after = sc.compute_streetwalk_coverage(
        edges,
        samples,
        _collected(samples, _dated, no_date_pred=_undated),
        RUN_DATE,
        "gsv",
        25.0,
    )

    b, a = before.set_index("edge_id"), after.set_index("edge_id")
    assert b.loc["1_2", "covered_samples"] == 8
    assert a.loc["1_2", "covered_samples"] == 12
    assert a.loc["1_2", "total_samples"] == 15  # denominator unmoved
    assert round(a.loc["1_2", "coverage_fraction"], 4) == round(12 / 15, 4)
    # covered_sample_any ors in covered_sample, so it moves with it.
    assert a.loc["1_2", "covered_samples_any"] == 12
    assert round(a.loc["1_2", "coverage_fraction_any"], 4) == round(12 / 15, 4)

    b_tot = sc.summarize_streetwalk_coverage(before)["totals"]
    a_tot = sc.summarize_streetwalk_coverage(after)["totals"]
    assert a_tot["length_km_covered"] > b_tot["length_km_covered"]
    assert a_tot["coverage_pct_by_length"] > b_tot["coverage_pct_by_length"]
    assert a_tot["coverage_pct_by_length_any"] > b_tot["coverage_pct_by_length_any"]


def test_no_date_sample_contributes_coverage_but_no_age():
    """
    NO_DATE rows carry a null capture_date, so they must never reach a dated
    statistic: adding them moves coverage while leaving `nearest_pano_date` and
    `median_covered_age_years` exactly where they were. That is the grid
    convention — PRESENT_STATUSES cover, only the dated subset ages — and it is
    what lets the fix land without disturbing the age series.
    """
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)

    before = sc.compute_streetwalk_coverage(
        edges, samples, _collected(samples, _dated), RUN_DATE, "gsv", 25.0
    ).set_index("edge_id")
    after = sc.compute_streetwalk_coverage(
        edges,
        samples,
        _collected(samples, _dated, no_date_pred=_undated),
        RUN_DATE,
        "gsv",
        25.0,
    ).set_index("edge_id")

    assert after.loc["1_2", "covered_samples"] > before.loc["1_2", "covered_samples"]
    assert after.loc["1_2", "nearest_pano_date"] == before.loc["1_2", "nearest_pano_date"]
    assert (
        after.loc["1_2", "median_covered_age_years"]
        == before.loc["1_2", "median_covered_age_years"]
    )
    assert (
        before.loc["1_2", "median_covered_age_years"] is not None
    )  # a real value, not None == None

    # Same at the summary level: coverage_by_highway's age is the covered-edge
    # median, and the undated samples must not have entered it.
    b_res = sc.summarize_streetwalk_coverage(before.reset_index())["totals"]
    a_res = sc.summarize_streetwalk_coverage(after.reset_index())["totals"]
    assert a_res["median_covered_age_years"] == b_res["median_covered_age_years"]


def test_gsv_no_date_still_gated_by_official_copyright():
    """
    Counting NO_DATE must not widen the GSV official-imagery gate: an undated
    third-party pano is still third-party. The exact `© Google` match is the
    only difference between the two halves below.
    """
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)

    def _every(r):
        return True

    third_party = sc.compute_streetwalk_coverage(
        edges,
        samples,
        _collected(samples, _every, no_date_pred=_every, copyright_="© Someone Else"),
        RUN_DATE,
        "gsv",
        25.0,
    )
    assert int(third_party["covered_samples"].sum()) == 0

    official = sc.compute_streetwalk_coverage(
        edges,
        samples,
        _collected(samples, _every, no_date_pred=_every, copyright_="© Google"),
        RUN_DATE,
        "gsv",
        25.0,
    )
    assert int(official["covered_samples"].sum()) == len(samples)
    # Covered everywhere, aged nowhere.
    assert official["median_covered_age_years"].isna().all()
    assert official["nearest_pano_date"].isna().all()


def test_geojson_is_strictly_valid_and_carries_metadata():
    edges = _edges()
    samples = rs.generate_samples(edges, spacing_m=15.0)
    collected = _collected(samples, lambda r: r.edge_id == "1_2" and r.sample_idx < 8)
    out = sc.compute_streetwalk_coverage(edges, samples, collected, RUN_DATE, "gsv", 25.0)
    gj = sc.build_streetwalk_geojson(
        out,
        city_id="bend--or",
        provider="gsv",
        run_date=RUN_DATE,
        spacing_m=15.0,
        match_dist_m=25.0,
        source_csv="x.csv.gz",
    )
    # allow_nan=False raises on any NaN — uncovered edges must serialize None.
    json.dumps(gj, allow_nan=False)
    meta = gj["properties"]["metadata"]
    assert meta["kind"] == "streetwalk_coverage"
    assert meta["spacing_m"] == 15.0
    totals = meta["totals"]
    assert totals["edges"] == 2
    assert totals["edges_fully_covered"] == 0
    assert 0.0 < totals["mean_edge_coverage"] < 1.0
    # One uncovered edge feature must have null date/age (not NaN).
    uncovered = [f for f in gj["features"] if f["properties"]["edge_id"] == "2_3"][0]
    assert uncovered["properties"]["nearest_pano_date"] is None
    assert uncovered["properties"]["median_covered_age_years"] is None


def test_empty_edges_yield_zero_edge_frame():
    edges = _edges().iloc[0:0]
    samples = rs.generate_samples(_edges(), spacing_m=15.0).iloc[0:0]
    out = sc.compute_streetwalk_coverage(edges, samples, pd.DataFrame(), RUN_DATE, "gsv", 25.0)
    assert len(out) == 0
    assert "coverage_fraction" in out.columns
