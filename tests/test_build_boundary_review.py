"""
Boundary-review payload builder (scripts/build_boundary_review.py, issue #91).

Pure, network-/DB-free tests: build_city_payload / build_payloads take already
parsed rows and produce the viewer's per-city records. Rectangle bounds are
delegated to boundary_audit.frozen_rect_bounds, so we assert the two agree.
"""

import importlib.util
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "build_boundary_review", os.path.join(PROJECT_ROOT, "scripts", "build_boundary_review.py")
)
bbr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bbr)

from streetscape_metadata_tracker.boundary_audit import frozen_rect_bounds  # noqa: E402

CURRENT = {"center_lat": 42.35, "center_lon": -71.06, "grid_width_m": 20000, "grid_height_m": 20000}


def _expected_bounds(lat, lon, w, h):
    s, n, we, e = frozen_rect_bounds(lat, lon, w, h)
    return [[s, we], [n, e]]


def manual_row(**kw):
    base = {
        "city_id": "boston",
        "display_name": "Boston",
        "verdict": "UNDER",
        "reason": "recenter only",
        "rec_center_lat": "42.31",
        "rec_center_lon": "-71.00",
        "rec_width_m": "31885",
        "rec_height_m": "18779",
        "rec_basis": "recenter only — grid ≈ OSM bbox",
        "coverage_before": "0.62",
    }
    base.update(kw)
    return base


def plan_row(**kw):
    base = {
        "city_id": "boston",
        "display_name": "Boston",
        "verdict": "UNDER",
        "reason": "recenter on OSM midpoint + grow to OSM bbox",
        "old_center_lat": "42.35",
        "old_center_lon": "-71.06",
        "old_width_m": "20000",
        "old_height_m": "20000",
        "new_center_lat": "42.31",
        "new_center_lon": "-71.00",
        "new_width_m": "31885",
        "new_height_m": "18779",
        "coverage_before": "0.62",
    }
    base.update(kw)
    return base


def report_row(**kw):
    base = {
        "city_id": "boston",
        "display_name": "Boston",
        "verdict": "UNDER",
        # pre-fix frozen snapshot — differs from CURRENT so a resize is detected
        "frozen_center_lat": "42.20",
        "frozen_center_lon": "-71.20",
        "frozen_width_m": "20000",
        "frozen_height_m": "20000",
        "suggested_center_lat": "42.31",
        "suggested_center_lon": "-71.00",
        "osm_bbox_width_m": "31885",
        "osm_bbox_height_m": "18779",
        "center_dist_km": "5.2",
        "osm_polygon_area_km2": "125",
    }
    base.update(kw)
    return base


def cache_rec(geojson=None, lat="42.30", lon="-71.05"):
    raw = {"lat": lat, "lon": lon}
    if geojson is not None:
        raw["geojson"] = geojson
    return {"city_id": "boston", "raw": raw}


# --- frozen rectangle: always from live catalog geometry ---


def test_frozen_bounds_match_audit_math():
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=None,
    )
    assert p["frozen_bounds"] == _expected_bounds(42.35, -71.06, 20000, 20000)
    assert p["frozen_geom"]["width_m"] == 20000


def test_no_current_geometry_yields_null_frozen():
    p = bbr.build_city_payload(
        "ghost",
        group="manual",
        current=None,
        report=report_row(),
        rec_source=manual_row(),
        cache=None,
    )
    assert p["frozen_bounds"] is None
    assert p["frozen_geom"] is None


# --- OSM bbox from the report ---


def test_osm_bbox_bounds_from_report():
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=None,
    )
    assert p["osm_bbox_bounds"] == _expected_bounds(42.31, -71.00, 31885, 18779)


def test_osm_bbox_null_when_report_missing_fields():
    r = report_row(osm_bbox_width_m="", suggested_center_lat="")
    p = bbr.build_city_payload(
        "boston", group="manual", current=CURRENT, report=r, rec_source=manual_row(), cache=None
    )
    assert p["osm_bbox_bounds"] is None
    assert p["osm_bbox_geom"] is None


def test_osm_bbox_geom_is_selectable_center_size():
    # The OSM bbox is exposed as a center+size geom so a reviewer can pick it.
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=None,
    )
    assert p["osm_bbox_geom"] == {
        "center_lat": 42.31,
        "center_lon": -71.00,
        "width_m": 31885,
        "height_m": 18779,
    }


# --- recommendation sourcing differs by group ---


def test_manual_rec_from_rec_fields():
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=None,
    )
    assert p["rec_bounds"] == _expected_bounds(42.31, -71.00, 31885, 18779)
    assert p["rec_geom"] == {
        "center_lat": 42.31,
        "center_lon": -71.00,
        "width_m": 31885,
        "height_m": 18779,
    }
    assert "recenter only" in p["rec_basis"]
    assert p["old_bounds"] is None  # manual cities are untouched — no before/after


def test_resize_shows_old_from_report_frozen():
    # An already-applied resize verifies OLD (report frozen_*) vs current; no rec.
    p = bbr.build_city_payload(
        "boston",
        group="resize",
        current=CURRENT,
        report=report_row(),
        rec_source=plan_row(),
        cache=None,
    )
    assert p["old_bounds"] == _expected_bounds(42.20, -71.20, 20000, 20000)
    assert p["old_geom"] == {
        "center_lat": 42.20,
        "center_lon": -71.20,
        "width_m": 20000,
        "height_m": 20000,
    }
    assert p["rec_bounds"] is None and p["rec_geom"] is None
    assert p["frozen_bounds"] == _expected_bounds(42.35, -71.06, 20000, 20000)
    assert "grow to OSM bbox" in p["rec_basis"]


# --- polygon + representative point from the cache ---


def test_polygon_passthrough_and_rounding():
    poly = {
        "type": "Polygon",
        "coordinates": [[[-71.123456789, 42.987654321], [-71.1, 42.9], [-71.2, 42.8]]],
    }
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=cache_rec(geojson=poly),
    )
    assert p["osm_polygon"]["type"] == "Polygon"
    assert p["osm_polygon"]["coordinates"][0][0] == [-71.12346, 42.98765]
    assert p["osm_center"] == [42.30, -71.05]


def test_multipolygon_supported():
    poly = {
        "type": "MultiPolygon",
        "coordinates": [[[[-71.1, 42.9], [-71.0, 42.9], [-71.0, 42.8]]]],
    }
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=cache_rec(geojson=poly),
    )
    assert p["osm_polygon"]["type"] == "MultiPolygon"


def test_missing_cache_yields_null_polygon_and_center():
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=None,
    )
    assert p["osm_polygon"] is None
    assert p["osm_center"] is None


def test_non_polygon_geojson_ignored():
    pt = {"type": "Point", "coordinates": [-71.05, 42.30]}
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=cache_rec(geojson=pt),
    )
    assert p["osm_polygon"] is None


# --- info panel metrics ---


def test_info_metrics_populated():
    p = bbr.build_city_payload(
        "boston",
        group="manual",
        current=CURRENT,
        report=report_row(),
        rec_source=manual_row(),
        cache=None,
    )
    assert p["info"]["center_dist_km"] == 5.2
    assert p["info"]["coverage_before"] == 0.62
    assert p["info"]["osm_polygon_area_km2"] == 125.0


# --- assembling the full list ---


def test_build_payloads_groups_and_orders():
    payloads, skipped = bbr.build_payloads(
        current_by_id={"boston": CURRENT, "seattle": CURRENT},
        report_by_id={"boston": report_row(), "seattle": report_row(city_id="seattle")},
        manual_by_id={"boston": manual_row()},
        plan_by_id={"seattle": plan_row(city_id="seattle")},
        cache_by_id={},
    )
    groups = {p["city_id"]: p["group"] for p in payloads}
    assert groups == {"boston": "manual", "seattle": "resize"}
    assert skipped == 0


def test_build_payloads_skips_unchanged_resizes():
    # If the current geometry still equals the pre-fix frozen snapshot, no resize
    # was actually applied — nothing to verify, so the city is dropped but counted.
    unchanged = report_row(
        city_id="nc",
        frozen_center_lat="42.35",
        frozen_center_lon="-71.06",
        frozen_width_m="20000",
        frozen_height_m="20000",
    )
    payloads, skipped = bbr.build_payloads(
        current_by_id={"nc": CURRENT},
        report_by_id={"nc": unchanged},
        manual_by_id={},
        plan_by_id={"nc": plan_row(city_id="nc")},
        cache_by_id={},
    )
    assert payloads == []
    assert skipped == 1


def test_resize_changed():
    assert bbr._resize_changed(report_row(), CURRENT) is True  # frozen 42.20 != 42.35
    same = report_row(
        frozen_center_lat="42.35",
        frozen_center_lon="-71.06",
        frozen_width_m="20000",
        frozen_height_m="20000",
    )
    assert bbr._resize_changed(same, CURRENT) is False
    assert bbr._resize_changed(None, CURRENT) is False
    assert bbr._resize_changed(report_row(), None) is False


# --- HTML injection ---


def test_render_html_injects_and_escapes():
    payloads = [
        bbr.build_city_payload(
            "boston",
            group="manual",
            current=CURRENT,
            report=report_row(),
            rec_source=manual_row(),
            cache=None,
        )
    ]
    html = bbr.render_html(payloads)
    assert bbr.DATA_PLACEHOLDER not in html
    assert '"city_id":"boston"' in html


def test_render_html_neutralizes_close_tag():
    row = manual_row(rec_basis="danger </script> tag")
    payloads = [
        bbr.build_city_payload(
            "boston",
            group="manual",
            current=CURRENT,
            report=report_row(),
            rec_source=row,
            cache=None,
        )
    ]
    html = bbr.render_html(payloads)
    assert "danger <\\/script> tag" in html


# ── CARTO basemap key ───────────────────────────────────────────────────────
#
# The viewer is generated, standalone HTML: it cannot load
# www/js/streetscape-utils.js, so its basemap key is substituted in here. That
# is worth pinning because the failure is invisible -- CARTO answers a
# missing, stale or placeholder key with HTTP 200 and "API KEY REQUIRED"
# printed across the tile, and on a tool whose whole job is judging a city
# boundary by eye, a watermark over every tile is a working-looking page that
# is harder to review.


def test_render_html_substitutes_the_frontend_carto_key():
    payloads = [
        bbr.build_city_payload(
            "boston",
            group="manual",
            current=CURRENT,
            report=report_row(),
            rec_source=manual_row(),
            cache=None,
        )
    ]
    html = bbr.render_html(payloads)
    assert bbr.CARTO_KEY_PLACEHOLDER not in html
    assert f"?key={bbr.carto_basemap_key()}" in html


def test_carto_key_comes_from_the_frontend_module_not_a_second_copy():
    """One key, one home. A second literal is how the two drift apart."""
    js = os.path.join(PROJECT_ROOT, "www", "js", "streetscape-utils.js")
    with open(js, encoding="utf-8") as f:
        js_source = f.read()
    assert f'CARTO_BASEMAP_KEY = "{bbr.carto_basemap_key()}"' in js_source

    # The template must carry the placeholder, never the key itself.
    with open(bbr.TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    assert bbr.CARTO_KEY_PLACEHOLDER in template
    assert bbr.carto_basemap_key() not in template


def test_carto_key_read_raises_rather_than_degrading(tmp_path):
    """A renamed const must fail the build, not ship a placeholder as the key.

    Returning "" or the placeholder would render a page that looks fine until
    a reviewer notices the watermark, which is exactly the failure mode this
    whole change exists to remove.
    """
    empty = tmp_path / "no-key.js"
    empty.write_text("const SOMETHING_ELSE = 1;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CARTO_BASEMAP_KEY"):
        bbr.carto_basemap_key(str(empty))


def test_render_html_raises_when_the_template_loses_the_key_placeholder(tmp_path):
    with open(bbr.TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    stripped = tmp_path / "template.html"
    stripped.write_text(template.replace("?key=" + bbr.CARTO_KEY_PLACEHOLDER, ""), encoding="utf-8")
    with pytest.raises(ValueError, match=bbr.CARTO_KEY_PLACEHOLDER):
        bbr.render_html([], template_path=str(stripped))
