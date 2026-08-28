#!/usr/bin/env python3
"""
Build the boundary-review visualization (issue #91, part 2 review aid).

The frozen-rectangle boundary audit flagged cities whose search grid may be
mis-centered or mis-sized against their OSM boundary. Judging those cases from
CSV columns is impractical, so this tool renders each one on a map: the frozen
grid rectangle, the OSM bounding box, the real OSM boundary polygon, and both
center points, with the audit's recommendation. A human records a decision
(accept / keep-current / skip) that a later step can apply to the catalog.

It reads (all under ``audit/`` by default, joined on ``city_id``):
  * the catalog ``cities`` table    -> current frozen geometry (read-only)
  * boundary_audit_report.csv       -> OSM bbox, suggested center, detail metrics
  * manual_review.csv               -> per-city recommendation (rec_* + rec_basis)
  * reregister_plan.csv             -> planned recenter+grow geometry (new_*)
  * nominatim_boundary_cache.jsonl  -> OSM boundary polygon + representative point

and emits a single self-contained ``audit/boundary_review.html`` (data embedded
inline — no server, no file:// CORS) built from
``scripts/boundary_review.template.html``.

Two review groups are shown:
  * ``manual`` — the manual_review.csv cities that need a human decision.
  * ``resize`` — the reregister_plan.csv cities queued for automatic
    recenter+grow, included so the auto-fix can be spot-checked before it runs.

Read-only on the catalog. Makes ZERO network / provider API calls.

Usage:
    python scripts/build_boundary_review.py
    python scripts/build_boundary_review.py --out /tmp/review.html
    python scripts/build_boundary_review.py --audit-dir audit --db-path PATH
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.boundary_audit import (  # noqa: E402
    frozen_rect_bounds,
    rect_polygon_coverage,
)
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("build_boundary_review")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_AUDIT_DIR = os.path.join(_PROJECT_ROOT, "audit")
TEMPLATE_PATH = os.path.join(_PROJECT_ROOT, "scripts", "boundary_review.template.html")
DATA_PLACEHOLDER = "/*__DATA__*/"
META_PLACEHOLDER = "/*__META__*/"
CARTO_KEY_PLACEHOLDER = "__CARTO_KEY__"

# The CARTO basemap key lives in exactly one place, the frontend module that
# also serves it to the public site. This viewer is generated HTML that
# cannot load that module, so the key is read out of it here rather than
# pasted into the template -- a second copy is how one map silently keeps
# rendering CARTO's "API KEY REQUIRED" watermark after the other is fixed.
BASEMAP_KEY_SOURCE = os.path.join(_PROJECT_ROOT, "www", "js", "streetscape-utils.js")
_CARTO_KEY_RE = re.compile(r'CARTO_BASEMAP_KEY\s*=\s*"([^"]+)"')

# Coordinate precision for embedded polygons: ~1 m at the equator, small file.
_COORD_DECIMALS = 5


def _num(row: dict, key: str) -> float | None:
    """Parse a possibly-blank/NaN CSV cell to float, or None."""
    val = row.get(key, "")
    if val is None or val == "":
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN filter without importing math


def _rect_bounds(center_lat, center_lon, width_m, height_m):
    """
    Leaflet ``[[south, west], [north, east]]`` for a frozen rectangle, or None
    if any input is missing. Delegates the meters->degrees math to
    ``boundary_audit.frozen_rect_bounds`` so the drawn box matches the audit.
    """
    if None in (center_lat, center_lon, width_m, height_m):
        return None
    s, n, w, e = frozen_rect_bounds(center_lat, center_lon, width_m, height_m)
    return [[s, w], [n, e]]


def _round_coords(obj):
    """Recursively round every float in a nested GeoJSON coordinate array."""
    if isinstance(obj, (int, float)):
        return round(obj, _COORD_DECIMALS)
    if isinstance(obj, list):
        return [_round_coords(x) for x in obj]
    return obj


def build_city_payload(
    city_id: str,
    *,
    group: str,
    current: dict | None,
    report: dict | None,
    rec_source: dict,
    cache: dict | None,
) -> dict:
    """
    Pure transform: assemble one city's viewer record from its already-parsed
    inputs. Network- and DB-free (all lookups are passed in), so it is directly
    unit-testable.

    Args:
        city_id: canonical catalog id (the join key).
        group: "manual" or "resize".
        current: current frozen geometry dict with keys center_lat, center_lon,
            grid_width_m, grid_height_m (from the catalog), or None.
        report: the boundary_audit_report.csv row for this city, or None.
        rec_source: the recommendation row — a manual_review.csv row (rec_* +
            rec_basis) for the manual group, or a reregister_plan.csv row
            (new_* geometry) for the resize group.
        cache: the parsed nominatim_boundary_cache.jsonl record, or None.

    Returns:
        A JSON-serializable dict with precomputed Leaflet bounds and overlays.
    """
    report = report or {}
    display_name = rec_source.get("display_name") or report.get("display_name") or city_id
    verdict = rec_source.get("verdict") or report.get("verdict") or ""

    # Frozen (current) grid rectangle — always from live catalog geometry.
    frozen_bounds = None
    frozen_geom = None
    if current is not None:
        frozen_bounds = _rect_bounds(
            current["center_lat"],
            current["center_lon"],
            current["grid_width_m"],
            current["grid_height_m"],
        )
        frozen_geom = {
            "center_lat": current["center_lat"],
            "center_lon": current["center_lon"],
            "width_m": current["grid_width_m"],
            "height_m": current["grid_height_m"],
        }

    # OSM bounding box — from the audit report's suggested center + bbox size.
    ob_lat, ob_lon = _num(report, "suggested_center_lat"), _num(report, "suggested_center_lon")
    ob_w, ob_h = _num(report, "osm_bbox_width_m"), _num(report, "osm_bbox_height_m")
    osm_bbox_bounds = _rect_bounds(ob_lat, ob_lon, ob_w, ob_h)
    # ...and as a selectable center+size geometry (the reviewer can pick the OSM
    # bbox as the new grid), mirroring frozen_geom/rec_geom.
    osm_bbox_geom = (
        None
        if None in (ob_lat, ob_lon, ob_w, ob_h)
        else {
            "center_lat": ob_lat,
            "center_lon": ob_lon,
            "width_m": int(ob_w),
            "height_m": int(ob_h),
        }
    )

    # The resize group verifies an ALREADY-APPLIED fix: show the OLD (pre-fix)
    # rectangle from the audit report's frozen_* snapshot vs the current
    # (corrected) frozen rectangle above. The manual group instead shows the
    # still-pending recommendation (rec_*).
    old_bounds = old_geom = None
    rec_bounds = rec_geom = None
    if group == "resize":
        o_lat, o_lon = _num(report, "frozen_center_lat"), _num(report, "frozen_center_lon")
        o_w, o_h = _num(report, "frozen_width_m"), _num(report, "frozen_height_m")
        old_bounds = _rect_bounds(o_lat, o_lon, o_w, o_h)
        old_geom = (
            None
            if None in (o_lat, o_lon, o_w, o_h)
            else {
                "center_lat": o_lat,
                "center_lon": o_lon,
                "width_m": int(o_w),
                "height_m": int(o_h),
            }
        )
        rec_basis = rec_source.get("reason", "")
    else:
        rc_lat, rc_lon = _num(rec_source, "rec_center_lat"), _num(rec_source, "rec_center_lon")
        rc_w, rc_h = _num(rec_source, "rec_width_m"), _num(rec_source, "rec_height_m")
        rec_bounds = _rect_bounds(rc_lat, rc_lon, rc_w, rc_h)
        rec_geom = (
            None
            if None in (rc_lat, rc_lon, rc_w, rc_h)
            else {
                "center_lat": rc_lat,
                "center_lon": rc_lon,
                "width_m": int(rc_w),
                "height_m": int(rc_h),
            }
        )
        rec_basis = rec_source.get("rec_basis", "")

    # OSM boundary polygon + representative point, from the Nominatim cache.
    osm_polygon = None
    osm_center = None
    raw = (cache or {}).get("raw") or {}
    geojson = raw.get("geojson")
    if isinstance(geojson, dict) and geojson.get("type") in ("Polygon", "MultiPolygon"):
        osm_polygon = {
            "type": geojson["type"],
            "coordinates": _round_coords(geojson.get("coordinates", [])),
        }
    r_lat, r_lon = raw.get("lat"), raw.get("lon")
    if r_lat is not None and r_lon is not None:
        try:
            osm_center = [float(r_lat), float(r_lon)]
        except (TypeError, ValueError):
            osm_center = None

    # How much of the true OSM boundary polygon each grid actually covers —
    # the metric the audit's bbox ratios only proxy. Lets the reviewer see
    # whether the auto-fix improved real coverage (resize: before -> current).
    def _coverage(geom):
        if not geom or not isinstance(geojson, dict):
            return None
        return rect_polygon_coverage(
            geojson,
            frozen_rect_bounds(
                geom["center_lat"], geom["center_lon"], geom["width_m"], geom["height_m"]
            ),
        )

    coverage_current = _coverage(frozen_geom)
    coverage_before = _coverage(old_geom) if group == "resize" else None

    return {
        "city_id": city_id,
        "display_name": display_name,
        "verdict": verdict,
        "group": group,
        "reason": rec_source.get("reason") or report.get("notes") or "",
        "frozen_bounds": frozen_bounds,
        "frozen_geom": frozen_geom,
        "old_bounds": old_bounds,
        "old_geom": old_geom,
        "poly_coverage_current": coverage_current,
        "poly_coverage_before": coverage_before,
        "osm_bbox_bounds": osm_bbox_bounds,
        "osm_bbox_geom": osm_bbox_geom,
        "osm_polygon": osm_polygon,
        "osm_center": osm_center,
        "rec_bounds": rec_bounds,
        "rec_geom": rec_geom,
        "rec_basis": rec_basis,
        "info": {
            "center_dist_km": _num(report, "center_dist_km"),
            "coverage_before": _num(rec_source, "coverage_before"),
            "osm_polygon_area_km2": _num(report, "osm_polygon_area_km2"),
            "osm_bbox_width_m": _num(report, "osm_bbox_width_m"),
            "osm_bbox_height_m": _num(report, "osm_bbox_height_m"),
        },
    }


def _load_current_geometry(conn) -> dict:
    """Map city_id -> current frozen geometry dict, read live from the catalog."""
    rows = conn.execute(
        "SELECT city_id, center_lat, center_lon, grid_width_m, grid_height_m, step_m FROM cities"
    ).fetchall()
    return {
        r["city_id"]: {
            "center_lat": r["center_lat"],
            "center_lon": r["center_lon"],
            "grid_width_m": r["grid_width_m"],
            "grid_height_m": r["grid_height_m"],
            "step_m": r["step_m"] or 20,
        }
        for r in rows
    }


def _estimate_points(geom: dict) -> int:
    """Grid points (= one GSV metadata request each) for a geometry dict."""
    s = geom.get("step_m") or 20
    return int((geom["grid_width_m"] // s + 1) * (geom["grid_height_m"] // s + 1))


def _load_csv_by_id(path: str) -> dict:
    """Read a CSV into {city_id: row}. Missing file -> empty dict."""
    if not os.path.exists(path):
        logger.warning("missing CSV: %s", path)
        return {}
    with open(path, newline="") as f:
        return {row["city_id"]: row for row in csv.DictReader(f)}


def _load_cache(path: str) -> dict:
    """Read the Nominatim cache JSONL into {city_id: record}."""
    out = {}
    if not os.path.exists(path):
        logger.warning("missing cache: %s", path)
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = rec.get("city_id")
            if cid:
                out[cid] = rec  # last write wins (append-only cache may repeat)
    return out


# Tolerances for "geometry actually changed" (center in degrees, size in meters).
_CENTER_EPS_DEG = 1e-7
_SIZE_EPS_M = 1.0


def _resize_changed(report: dict | None, current: dict | None) -> bool:
    """
    True when a city's current (post-fix) geometry differs from the audit
    report's pre-fix ``frozen_*`` snapshot — i.e. the resize was actually
    applied and is worth spot-checking. A city already at target when the
    resize ran (no change) needs no before/after verification.
    """
    if report is None or current is None:
        return False
    o_lat, o_lon = _num(report, "frozen_center_lat"), _num(report, "frozen_center_lon")
    o_w, o_h = _num(report, "frozen_width_m"), _num(report, "frozen_height_m")
    if None in (o_lat, o_lon, o_w, o_h):
        return False
    return (
        abs(o_lat - current["center_lat"]) > _CENTER_EPS_DEG
        or abs(o_lon - current["center_lon"]) > _CENTER_EPS_DEG
        or abs(o_w - current["grid_width_m"]) > _SIZE_EPS_M
        or abs(o_h - current["grid_height_m"]) > _SIZE_EPS_M
    )


def build_payloads(
    current_by_id: dict,
    report_by_id: dict,
    manual_by_id: dict,
    plan_by_id: dict,
    cache_by_id: dict,
    include_largest: int = 0,
):
    """
    Assemble the ordered list of viewer records for all reviewed cities.

    ``include_largest`` adds that many cities as a "large" group — the biggest
    by grid-point count that aren't already flagged (manual/resize) — so the
    reviewer can sanity-check whether the very largest frozen rectangles are
    justified by the city or just over-generous, independent of the audit.

    Returns ``(payloads, skipped_resize)`` where ``skipped_resize`` is the count
    of reregister-plan cities dropped because their current geometry still
    equals the pre-fix snapshot (either legitimately already-at-target, or a
    resize that was never ``--execute``-d). Surfacing the count keeps a shorter
    list from being read as "everything passed".
    """
    payloads = []
    for city_id, row in manual_by_id.items():
        payloads.append(
            build_city_payload(
                city_id,
                group="manual",
                current=current_by_id.get(city_id),
                report=report_by_id.get(city_id),
                rec_source=row,
                cache=cache_by_id.get(city_id),
            )
        )
    skipped_resize = 0
    for city_id, row in plan_by_id.items():
        report = report_by_id.get(city_id)
        current = current_by_id.get(city_id)
        if not _resize_changed(report, current):
            skipped_resize += 1
            continue
        payloads.append(
            build_city_payload(
                city_id,
                group="resize",
                current=current,
                report=report,
                rec_source=row,
                cache=cache_by_id.get(city_id),
            )
        )

    if include_largest > 0:
        seen = {p["city_id"] for p in payloads}
        ranked = sorted(
            ((cid, g) for cid, g in current_by_id.items() if cid not in seen),
            key=lambda kv: _estimate_points(kv[1]),
            reverse=True,
        )
        for city_id, geom in ranked[:include_largest]:
            p = build_city_payload(
                city_id,
                group="large",
                current=geom,
                report=report_by_id.get(city_id),
                rec_source=report_by_id.get(city_id) or {},
                cache=cache_by_id.get(city_id),
            )
            p["points"] = _estimate_points(geom)
            payloads.append(p)
    return payloads, skipped_resize


def _json_for_script(obj) -> str:
    """Compact JSON safe to inline in a <script> (no premature tag close)."""
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")


def carto_basemap_key(source_path: str = BASEMAP_KEY_SOURCE) -> str:
    """Read the CARTO basemap key out of the frontend module that owns it.

    Raises rather than degrading: a missing or renamed const would otherwise
    leave the viewer requesting tiles with a literal placeholder for a key,
    and CARTO answers a bad key with HTTP 200 and a watermarked tile, so the
    failure would reach a reviewer as a cosmetic oddity instead of an error.
    """
    with open(source_path, encoding="utf-8") as f:
        match = _CARTO_KEY_RE.search(f.read())
    if not match:
        raise ValueError(f"no CARTO_BASEMAP_KEY const found in {source_path}")
    return match.group(1)


def render_html(
    payloads: list, template_path: str = TEMPLATE_PATH, meta: dict | None = None
) -> str:
    """Inject the payload JSON (and optional meta block) into the viewer template."""
    with open(template_path) as f:
        template = f.read()
    if DATA_PLACEHOLDER not in template:
        raise ValueError(f"template missing {DATA_PLACEHOLDER!r} placeholder")
    if CARTO_KEY_PLACEHOLDER not in template:
        raise ValueError(f"template missing {CARTO_KEY_PLACEHOLDER!r} placeholder")
    html = template.replace(DATA_PLACEHOLDER, _json_for_script(payloads))
    html = html.replace(CARTO_KEY_PLACEHOLDER, quote(carto_basemap_key(), safe=""))
    if META_PLACEHOLDER in html:
        html = html.replace(META_PLACEHOLDER, _json_for_script(meta or {}))
    return html


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--audit-dir",
        default=DEFAULT_AUDIT_DIR,
        help="directory holding the audit CSVs + cache (default: audit/)",
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--db-path", default=None, help="default: {data-dir}/streetscape_tracker.db"
    )
    parser.add_argument("--template", default=TEMPLATE_PATH)
    parser.add_argument(
        "--out", default=None, help="output HTML (default: {audit-dir}/boundary_review.html)"
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    parser.add_argument(
        "--include-largest",
        type=int,
        default=0,
        metavar="N",
        help="also review the N largest-by-grid-points cities not "
        "already flagged (a 'LARGE' group), to sanity-check "
        "whether the biggest rectangles are justified",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    out_path = args.out or os.path.join(args.audit_dir, "boundary_review.html")
    report_path = os.path.join(args.audit_dir, "boundary_audit_report.csv")
    manual_path = os.path.join(args.audit_dir, "manual_review.csv")
    plan_path = os.path.join(args.audit_dir, "reregister_plan.csv")
    cache_path = os.path.join(args.audit_dir, "nominatim_boundary_cache.jsonl")

    print("=== Build boundary-review visualization ===")
    print(f"Catalog: {db_path}")
    print(f"Audit:   {args.audit_dir}\n")

    conn = db.connect(db_path)
    current_by_id = _load_current_geometry(conn)
    conn.close()

    payloads, skipped_resize = build_payloads(
        current_by_id,
        _load_csv_by_id(report_path),
        _load_csv_by_id(manual_path),
        _load_csv_by_id(plan_path),
        _load_cache(cache_path),
        include_largest=args.include_largest,
    )

    n_manual = sum(1 for p in payloads if p["group"] == "manual")
    n_resize = sum(1 for p in payloads if p["group"] == "resize")
    n_large = sum(1 for p in payloads if p["group"] == "large")
    n_poly = sum(1 for p in payloads if p["osm_polygon"])

    html = render_html(payloads, args.template, meta={"skipped_resize": skipped_resize})
    with open(out_path, "w") as f:
        f.write(html)

    print(f"Cities: {len(payloads)}  (manual={n_manual}, resize={n_resize}, large={n_large})")
    print(f"Resize cities skipped (unchanged geometry): {skipped_resize}")
    print(f"With OSM polygon: {n_poly}")
    print(f"\nWrote {out_path} ({len(html) / 1024:.0f} KB) — open it in a browser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
