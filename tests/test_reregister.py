"""
Boundary re-registration policy (scripts/reregister_boundaries.py, issue #91).

Exercises the pure decision function over the four branches (offset->recenter,
undersized->grow, huge-bbox->defer, wrong-place->defer) plus idempotency. No
network, no DB — decide() takes the report row and current geometry directly.
"""

import importlib.util
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_script(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PROJECT_ROOT, "scripts", f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rr = _load_script("reregister_boundaries")


def row(verdict, *, osm_w=None, osm_h=None, sug_lat=None, sug_lon=None, city_id="c", name="City"):
    return {
        "city_id": city_id,
        "display_name": name,
        "verdict": verdict,
        "osm_bbox_width_m": "" if osm_w is None else str(osm_w),
        "osm_bbox_height_m": "" if osm_h is None else str(osm_h),
        "suggested_center_lat": "" if sug_lat is None else str(sug_lat),
        "suggested_center_lon": "" if sug_lon is None else str(sug_lon),
    }


def cur(lat, lon, w, h, step=20, manually_resized=False):
    return rr.Current(
        center_lat=lat,
        center_lon=lon,
        width_m=w,
        height_m=h,
        step_m=step,
        manually_resized=manually_resized,
    )


def test_offset_city_recenters_without_shrinking():
    # Right size (~= OSM bbox) but off-center: recenter, size essentially kept.
    d = rr.decide(
        row("UNDER", osm_w=2000, osm_h=1700, sug_lat=46.44, sug_lon=-96.73),
        cur(46.45, -96.72, 2000, 1700),
    )
    assert d.action == "RESIZE"
    assert (d.new_center_lat, d.new_center_lon) == (46.44, -96.73)
    assert d.new_width_m == 2000 and d.new_height_m == 1700  # grow-only, unchanged
    assert d.coverage_after > 0.99 > d.coverage_before


def test_degenerate_grid_grows_to_osm_bbox():
    # Gary-style 1-point grid grows to the (modest) OSM bbox.
    d = rr.decide(
        row("UNDER", osm_w=17600, osm_h=14226, sug_lat=41.6, sug_lon=-87.34),
        cur(41.6, -87.34, 8, 11),
    )
    assert d.action == "RESIZE"
    assert d.new_width_m == 17600 and d.new_height_m == 14226
    assert d.new_points > d.old_points
    assert d.coverage_after > 0.99


def test_huge_osm_bbox_deferred_to_manual_review():
    # Đà Nẵng-style: OSM administrative bbox far exceeds the review threshold.
    d = rr.decide(
        row("UNDER", osm_w=194000, osm_h=153000, sug_lat=16.06, sug_lon=108.2),
        cur(16.06, 108.21, 20000, 20000),
    )
    assert d.action == "DEFER"
    assert "threshold" in d.reason
    assert d.new_width_m is None  # geometry untouched


def test_threshold_boundary_exactly_at_cap_is_resized():
    d = rr.decide(
        row("UNDER", osm_w=30000, osm_h=30000, sug_lat=1.0, sug_lon=2.0),
        cur(1.0, 2.0, 5000, 5000),
        threshold_m=30000,
    )
    assert d.action == "RESIZE"


def test_wrong_place_deferred():
    d = rr.decide(row("WRONG_PLACE"), cur(1.0, 2.0, 5000, 5000))
    assert d.action == "DEFER" and d.new_width_m is None


@pytest.mark.parametrize("verdict", ["NO_POLYGON", "NOT_FOUND", "OVER", "BBOX_SUSPECT"])
def test_other_verdicts_deferred(verdict):
    assert rr.decide(row(verdict), cur(1.0, 2.0, 5000, 5000)).action == "DEFER"


def test_ok_verdict_skipped():
    assert rr.decide(row("OK"), cur(1.0, 2.0, 5000, 5000)).action == "SKIP"


def test_under_with_missing_osm_bbox_deferred():
    assert rr.decide(row("UNDER"), cur(1.0, 2.0, 5000, 5000)).action == "DEFER"


def test_idempotent_after_applying_target():
    r = row("UNDER", osm_w=17600, osm_h=14226, sug_lat=41.6, sug_lon=-87.34)
    first = rr.decide(r, cur(41.6, -87.34, 8, 11))
    # Feed back the post-execute geometry: same center, grown dimensions.
    applied = cur(first.new_center_lat, first.new_center_lon, first.new_width_m, first.new_height_m)
    assert rr.decide(r, applied).action == "NOCHANGE"


# --- issue #459: a hand-fitted grid is never grown back unattended ------------

# Kuna, Idaho's shape (#457): a 4.9 x 5.7 km grid fitted to its streets inside
# a boundary bbox several times that size, well under the 30 km threshold.
_FITTED_ROW = dict(osm_w=12000, osm_h=11000, sug_lat=43.49, sug_lon=-116.42)


def test_a_manually_resized_grid_is_deferred_not_grown_back():
    r = row("UNDER", **_FITTED_ROW)
    # The premise: without the note this exact city IS resized unattended.
    assert rr.decide(r, cur(43.49753, -116.42638, 4900, 5700)).action == "RESIZE"

    d = rr.decide(r, cur(43.49753, -116.42638, 4900, 5700, manually_resized=True))
    assert d.action == "DEFER"
    assert d.reason == rr.MANUAL_RESIZE_REASON
    # Geometry untouched.
    assert d.new_width_m is None and d.new_height_m is None
    assert (d.old_width_m, d.old_height_m) == (4900, 5700)
    # The review page shows rec_basis in place of the reason and offers the
    # recommended rectangle as a one-click choice unless it equals the current
    # one. So the recommendation must BE the current grid and say why, not
    # recommend()'s generic "grow to full OSM bbox" (PR #466 review).
    assert (d.rec_center_lat, d.rec_center_lon) == (43.49753, -116.42638)
    assert (d.rec_width_m, d.rec_height_m) == (4900, 5700)
    assert "fitted by hand" in d.rec_basis and "grow to full" not in d.rec_basis
    generic = rr.recommend(r, cur(43.49753, -116.42638, 4900, 5700))
    assert generic["rec_width_m"] == 12000 and "grow to full" in generic["rec_basis"]  # premise


def test_the_manual_resize_guard_diverts_only_a_would_be_resize():
    # Already at the audit's target: nothing would change, so nothing to review.
    at_target = cur(43.49, -116.42, 12000, 11000, manually_resized=True)
    assert rr.decide(row("UNDER", **_FITTED_ROW), at_target).action == "NOCHANGE"
    fitted = cur(43.49753, -116.42638, 4900, 5700, manually_resized=True)
    assert rr.decide(row("OK"), fitted).action == "SKIP"
    over_threshold = rr.decide(
        row("UNDER", osm_w=194000, osm_h=153000, sug_lat=43.49, sug_lon=-116.42), fitted
    )
    assert over_threshold.action == "DEFER" and "threshold" in over_threshold.reason


def test_load_current_geometry_reads_the_resize_note_resize_city_writes(tmp_path, monkeypatch):
    """End to end across the two scripts: the note resize_city.py really writes
    is the note reregister_boundaries.py really reads, among other notes."""
    from streetscape_metadata_tracker import db

    catalog = str(tmp_path / "catalog.db")
    conn = db.connect(catalog)
    ids = {}
    for name in ("Kuna", "Boise", "Magna"):
        ids[name] = db.register_city(
            conn,
            city_name=name,
            state_name="Idaho",
            state_code="ID",
            country_name="United States",
            country_code="US",
            center_lat=43.5,
            center_lon=-116.4,
            grid_width_m=12000,
            grid_height_m=11000,
            step_m=20,
            notes="tranche 3" if name == "Magna" else None,
        )
    conn.close()

    resize = _load_script("resize_city")
    for name in ("Kuna", "Magna"):
        argv = ["resize_city.py", ids[name], "--width", "4900", "--height", "5700"]
        monkeypatch.setattr(sys, "argv", [*argv, "--db-path", catalog, "--execute"])
        assert resize.main() == 0

    conn = db.connect(catalog)
    current = rr.load_current_geometry(conn)
    conn.close()
    assert current[ids["Kuna"]].manually_resized is True
    assert current[ids["Magna"]].manually_resized is True  # appended after another note
    assert current[ids["Boise"]].manually_resized is False  # NULL notes
    assert current[ids["Kuna"]].width_m == 4900


def test_unknown_city_skipped():
    assert (
        rr.decide(row("UNDER", osm_w=5000, osm_h=5000, sug_lat=1, sug_lon=2), None).action == "SKIP"
    )


# --- recommendations attached to deferred (manual-review) cities ---------------


def _row_full(verdict, **kw):
    """Report row including the extra columns recommend() reads."""
    base = {"over_area_ratio": "", "osm_polygon_area_km2": "", "center_dist_km": ""}
    base.update(kw)
    return {
        **row(
            verdict,
            osm_w=kw.get("osm_w"),
            osm_h=kw.get("osm_h"),
            sug_lat=kw.get("sug_lat"),
            sug_lon=kw.get("sug_lon"),
        ),
        **{
            k: str(v)
            for k, v in base.items()
            if k in ("over_area_ratio", "osm_polygon_area_km2", "center_dist_km")
        },
    }


def test_giant_offset_recommends_recenter_only():
    # OSM bbox huge but the grid already matches it (over_area_ratio ~1): the
    # recommendation is a recenter, keeping the current (large) size.
    r = _row_full(
        "UNDER",
        osm_w=106000,
        osm_h=84000,
        sug_lat=61.2,
        sug_lon=-149.9,
        over_area_ratio=1.0,
        center_dist_km=0.1,
    )
    d = rr.decide(r, cur(61.15, -149.8, 106000, 84000))
    assert d.action == "DEFER"
    assert d.rec_width_m == 106000 and d.rec_height_m == 84000
    assert (d.rec_center_lat, d.rec_center_lon) == (61.2, -149.9)
    assert "recenter only" in d.rec_basis


def test_undersized_giant_recommends_full_bbox_capped_at_ceiling():
    # Đà Nẵng's 194x153 km municipality: grow toward full bbox but clamp at the
    # 80 km practical ceiling (bias bigger, but not absurd).
    r = _row_full(
        "UNDER",
        osm_w=194000,
        osm_h=153000,
        sug_lat=16.06,
        sug_lon=108.2,
        over_area_ratio=0.01,
        center_dist_km=1.6,
        osm_polygon_area_km2=15962,
    )
    d = rr.decide(r, cur(16.06, 108.21, 20000, 20000))
    assert d.action == "DEFER"
    assert d.rec_width_m == rr.REC_CEILING_M and d.rec_height_m == rr.REC_CEILING_M
    assert "capped" in d.rec_basis


def test_undersized_giant_under_ceiling_recommends_full_bbox():
    # São Paulo 47x72 km is under the ceiling: recommend the full bbox, uncapped.
    r = _row_full(
        "UNDER",
        osm_w=47000,
        osm_h=72000,
        sug_lat=-23.6,
        sug_lon=-46.6,
        over_area_ratio=0.06,
        center_dist_km=0.0,
    )
    d = rr.decide(r, cur(-23.6, -46.6, 15000, 15000))
    assert d.rec_width_m == 47000 and d.rec_height_m == 72000
    assert "capped" not in d.rec_basis


def test_over_recommends_keeping_big_not_shrinking():
    # Bias bigger: an oversized grid is safe (clip to polygon later) — keep it.
    r = _row_full("OVER", osm_w=800, osm_h=500, sug_lat=33.9, sug_lon=35.5)
    d = rr.decide(r, cur(33.9, 35.5, 20000, 20000))
    assert d.action == "DEFER"
    assert d.rec_width_m == 20000 and d.rec_height_m == 20000  # unchanged, not shrunk
    assert "do not shrink" in d.rec_basis


def test_wrong_place_recommendation_says_verify():
    d = rr.decide(
        _row_full("WRONG_PLACE", osm_w=2000, osm_h=2000, sug_lat=33.1, sug_lon=-93.6),
        cur(40.0, -80.0, 39000, 60000),
    )
    assert d.action == "DEFER" and "verify geocode" in d.rec_basis
