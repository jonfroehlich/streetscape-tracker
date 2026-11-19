"""
Pin the city map's pano cells to the slippy-map pyramid from the Python side (issue #77).

The city page indexes its panoramas by Web Mercator cell (``www/js/pano-overview.js``,
``mercatorCell``), and those cells must be the tiles Leaflet draws, or anything
later built on them (an overview layer, a pipeline-emitted artifact) sits beside the
dots instead of under them.
The committed golden ``www/js/__tests__/pano-cell-golden.json`` maps ``(lat, lon)`` to
the cell at each of several levels.
Here it is held to the repo's own Python tile math (``download_common.lonlat_to_tile_frac``,
the lattice both tile censuses fetch on); ``pano-overview.test.js`` holds the JS function
to the same file, so the two languages are pinned to one table rather than to each other's
source.

Regenerate (after changing the point list below, never to make a failure go away):

    PYTHONPATH=. python tests/test_pano_cell_golden.py
"""

import json
import math
import random
import sys
from pathlib import Path

from streetscape_metadata_tracker.download_common import lonlat_to_tile_frac

GOLDEN = (
    Path(__file__).resolve().parent.parent / "www" / "js" / "__tests__" / "pano-cell-golden.json"
)

LEVELS = (0, 1, 2, 4, 8, 10, 12, 14, 15, 16, 18, 20)

# A point closer than this (in cells) to a cell edge at any level is left out:
# the JS and Python projections are algebraically equal but not bit-equal, and
# a point ON an edge would make the golden a coin toss rather than a contract.
EDGE_MARGIN_CELLS = 1e-6

# Named cities across hemispheres and latitudes. Two of their cells (Seattle at
# level 10, Sydney at level 12) the plan derived by hand and the review recomputed
# independently; test_the_hand_derived_cells_are_in_the_golden asserts those.
NAMED = [
    ("seattle", 47.6062, -122.3321),
    ("sydney", -33.8688, 151.2093),
    ("reykjavik", 64.1466, -21.9426),
    ("ushuaia", -54.8019, -68.303),
    ("nairobi", -1.2921, 36.8219),
    ("tokyo", 35.6762, 139.6503),
]

# A 3x3 cluster around a zoom-8 tile CORNER (x 42|43, y 90|91), so the golden
# holds points on all four sides of a coarse boundary.
Z8_CORNER_LON = 43 / 256 * 360 - 180  # -119.53125
Z8_CORNER_LAT = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * 91 / 256))))


def _points() -> list[tuple[str, float, float]]:
    pts = list(NAMED)
    for dy in (-0.01, 0.0001, 0.01):
        for dx in (-0.01, 0.0001, 0.01):
            pts.append(
                (
                    f"z8-corner{dy:+}{dx:+}",
                    round(Z8_CORNER_LAT + dy, 7),
                    round(Z8_CORNER_LON + dx, 7),
                )
            )
    rng = random.Random(77)
    for i in range(40):
        pts.append((f"seeded{i}", round(rng.uniform(-80, 80), 6), round(rng.uniform(-179, 179), 6)))
    return pts


def _cells(lat: float, lon: float) -> dict[str, list[int]] | None:
    """The cell at every level, or None when the point sits on an edge at one."""
    out = {}
    for level in LEVELS:
        fx, fy = lonlat_to_tile_frac(lon, lat, level)
        for f in (fx, fy):
            if abs(f - round(f)) < EDGE_MARGIN_CELLS and level > 0:
                return None
        out[str(level)] = [int(math.floor(fx)), int(math.floor(fy))]
    return out


def build_golden() -> dict:
    entries = []
    for name, lat, lon in _points():
        cells = _cells(lat, lon)
        if cells is not None:
            entries.append({"name": name, "lat": lat, "lon": lon, "cells": cells})
    return {
        "generated_by": (
            "PYTHONPATH=. python tests/test_pano_cell_golden.py"
            " (download_common.lonlat_to_tile_frac, floored)"
        ),
        "levels": list(LEVELS),
        "entries": entries,
    }


def test_the_golden_is_the_python_tile_math():
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == build_golden()


def test_the_golden_covers_both_sides_of_a_zoom_8_corner():
    entries = json.loads(GOLDEN.read_text(encoding="utf-8"))["entries"]
    z8 = {tuple(e["cells"]["8"]) for e in entries if e["name"].startswith("z8-corner")}
    assert z8 == {(42, 90), (43, 90), (42, 91), (43, 91)}


def test_the_hand_derived_cells_are_in_the_golden():
    by_name = {e["name"]: e for e in json.loads(GOLDEN.read_text(encoding="utf-8"))["entries"]}
    assert by_name["seattle"]["cells"]["10"] == [164, 357]
    assert by_name["sydney"]["cells"]["12"] == [3768, 2457]


if __name__ == "__main__":
    golden = build_golden()
    # One entry per line, so a diff of a regenerated golden names the point that moved.
    lines = ",\n  ".join(json.dumps(e, separators=(",", ":")) for e in golden["entries"])
    GOLDEN.write_text(
        "{\n"
        f' "generated_by": {json.dumps(golden["generated_by"])},\n'
        f' "levels": {json.dumps(golden["levels"])},\n'
        f' "entries": [\n  {lines}\n ]\n'
        "}\n",
        encoding="utf-8",
    )
    sys.exit(0)
