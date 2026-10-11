"""
The 2026-10-08 Belgium screen's committed record (docs/experiments/belgium-screen.md).

* belgium-screen_metrics.json must be exactly what
  scripts/belgium_screen_analyze.py derives from the committed per-place CSV,
  so a hand-edited number in the JSON fails;
* the collect script's per-place arithmetic, its segment filter (360° only,
  captured at or after the cutoff, one row per duplicated segment), its
  cached-tile filenames and its population filter are pinned on synthetic
  inputs, since the real tiles are gitignored (the full replay reproduced the
  research script's CSV byte for byte on 2026-10-08; that is a provenance
  claim in the writeup, not something this suite can re-run);
* both scripts run by path from outside the repo, as their documented
  `python scripts/...` commands do.
"""

import json
import math
import subprocess
import sys
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


def _fake_tiles(tmp_path, monkeypatch, by_tile):
    """
    Write one cached Mapillary tile per key of ``by_tile`` under the name the
    collect script reads (``6_{x}_{y}.mvt``), and make ``decode_sequences``
    return that tile's sequences, so a filename change or a filter change in
    ``mapillary_segments`` is visible without a real MVT.
    """
    for x, y in by_tile:
        (tmp_path / f"6_{x}_{y}.mvt").write_bytes(f"{x},{y}".encode())

    def decode(raw, x, y, zoom):
        assert raw == f"{x},{y}".encode() and zoom == collect.ZOOM
        return by_tile[(x, y)]

    monkeypatch.setattr(collect, "decode_sequences", decode)


def test_mapillary_segments_keeps_only_recent_panos_once(tmp_path, monkeypatch):
    cutoff_ms = pd.Timestamp(collect.CUTOFF).value // 10**6
    line = [(4.0, 51.0), (4.01, 51.0)]
    dup = [(5.0, 50.0), (5.01, 50.0)]

    def seq(sid, *, pano=True, at=cutoff_ms, pts=line):
        return {"id": sid, "is_pano": pano, "captured_at": at, "creator_id": 1, "_pts": pts}

    _fake_tiles(
        tmp_path,
        monkeypatch,
        {
            (32, 21): [
                seq("at-cutoff"),
                seq("one-ms-early", at=cutoff_ms - 1),
                seq("flat", pano=False),
                seq("undated", at=None),
                seq("dup", pts=dup),
            ],
            # The same segment again, as when one sequence spans both tiles.
            (33, 21): [seq("dup", pts=dup)],
        },
    )

    seg = collect.mapillary_segments(tmp_path, [(32, 21), (33, 21)], cutoff_ms)

    assert sorted(seg.seq) == ["at-cutoff", "dup"]
    row = seg[seg.seq == "at-cutoff"].iloc[0]
    assert (row.lon, row.lat) == (4.005, 51.0)
    assert row.km == pytest.approx(collect.local_km(4.0, 51.0, 4.01, 51.0))


def test_the_cutoff_is_two_years_before_the_screen():
    """Pinned as a literal: the writeup quotes 2024-10-08 as the recent cutoff."""
    assert collect.CUTOFF == "2024-10-08"


def test_mapillary_segments_refuses_a_tile_that_is_not_cached(tmp_path, monkeypatch):
    _fake_tiles(tmp_path, monkeypatch, {(32, 21): []})
    with pytest.raises(SystemExit, match="not cached"):
        collect.mapillary_segments(tmp_path, [(32, 21), (33, 21)], 0)


def test_load_places_keeps_belgian_places_of_at_least_ten_thousand(tmp_path):
    """A cities500.txt row: 19 tab-separated GeoNames fields."""

    def row(name, cc, pop):
        f = [""] * 19
        f[1], f[4], f[5], f[8], f[10], f[14] = name, "51.0", "4.0", cc, "VLG", str(pop)
        return "\t".join(f)

    path = tmp_path / "cities500.txt"
    rows = [row("Below", "BE", 9_999), row("AtFloor", "BE", 10_000), row("Abroad", "NL", 50_000)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    assert list(collect.load_places(path).name) == ["AtFloor"]


@pytest.mark.parametrize("script", ["belgium_screen_analyze.py", "belgium_screen_collect.py"])
def test_script_runs_by_path_from_outside_the_repo(tmp_path, script):
    """The writeup's generated_by/collected_by are `python scripts/...` commands."""
    path = Path(__file__).resolve().parents[1] / "scripts" / script
    result = subprocess.run(
        [sys.executable, str(path), "--help"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
