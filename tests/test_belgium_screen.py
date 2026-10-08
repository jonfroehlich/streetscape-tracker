"""
The 2026-10-08 Belgium screen's committed record (docs/experiments/belgium-screen.md).

* belgium-screen_metrics.json must be exactly what
  scripts/belgium_screen_analyze.py derives from the committed per-place CSV,
  so a hand-edited number in the JSON fails;
* the collect script's per-place arithmetic is pinned on a synthetic input,
  since the real tiles are gitignored (the full replay reproduced the
  research script's CSV byte for byte on 2026-10-08; that is a provenance
  claim in the writeup, not something this suite can re-run).
"""

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from scripts import belgium_screen_analyze as analyze
from scripts import belgium_screen_collect as collect

DOCS = Path(__file__).resolve().parents[1] / "docs" / "experiments"


def test_metrics_json_is_the_analysis_of_the_committed_csv():
    committed = json.loads((DOCS / analyze.METRICS_JSON).read_text(encoding="utf-8"))
    derived = analyze.summarize(analyze.load(DOCS / analyze.PLACES_CSV))
    committed.pop("_about")
    assert committed == json.loads(json.dumps(derived))


def test_every_pinned_brussels_capital_place_is_in_the_record():
    rows = analyze.load(DOCS / analyze.PLACES_CSV)
    names = {r["place"] for r in rows}
    assert len(analyze.BRUSSELS_CAPITAL) == 18
    assert set(analyze.BRUSSELS_CAPITAL) <= names


def test_place_rows_counts_only_what_lies_inside_each_radius():
    lat0, lon0 = 51.0, 4.0
    dlon_km = 111.32 * math.cos(math.radians(lat0))
    seg = pd.DataFrame(
        {
            "seq": ["a", "a", "b", "c"],
            "lon": [lon0, lon0 + 1.9 / dlon_km, lon0, lon0 + 2.1 / dlon_km],
            "lat": [lat0, lat0, lat0 + 1.0 / 110.57, lat0],
            "km": [1.0, 2.0, 1.0, 5.0],  # "c" is 2.1 km out, so excluded
            "creator": [7, 7, 8, 9],
        }
    )
    cells = pd.DataFrame(
        {
            "lon": [lon0, lon0 + 2.9 / dlon_km, lon0 + 3.1 / dlon_km],
            "lat": [lat0, lat0, lat0],
            "pics": [10, 20, 400],  # the third is 3.1 km out
            "pano": [5, 20, 400],
        }
    )
    places = pd.DataFrame({"name": ["P"], "lat": [lat0], "lon": [lon0], "pop": [12_345]})

    out = collect.place_rows(seg, cells, places).iloc[0]

    assert out["mly_recent360_km"] == 4.0
    assert out["mly_km_per_km2"] == round(4.0 / (math.pi * 4), 2)
    assert out["mly_top_creator_share"] == 0.75
    assert (out["pnx_hexes"], out["pnx_pics_ub"], out["pnx_360_ub"]) == (2, 30, 25)


def test_collect_refuses_a_tile_that_is_not_cached(tmp_path):
    with pytest.raises(SystemExit, match="not cached"):
        collect.panoramax_cells(tmp_path, [(32, 21)])
