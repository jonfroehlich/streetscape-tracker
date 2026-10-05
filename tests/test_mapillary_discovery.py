"""Sampling invariants for the Mapillary discovery screen (#383).

The screen's numbers are only as good as four mappings: tile-local coordinates
to the globe (a flipped y puts a town's sweep in the next state), simplified
geometry to length (the score is a length), length to a place (which disc a
sample lands in), and a place to "already tracked" (distance to a centre is not
grid membership). These tests pin those, plus the sampling frame itself.
"""

import json
import math
import random
import sqlite3

import mapbox_vector_tile
import pandas as pd
import pytest

from scripts import mapillary_discovery_analyze as mda
from scripts import mapillary_discovery_collect as mdcol
from scripts import mapillary_discovery_common as mdc
from streetscape_metadata_tracker.download_common import lonlat_to_tile_frac, tile_frac_to_lonlat


def test_the_2026_10_02_frame_is_97_z6_tiles():
    # The committed numbers came from exactly this tile set; a change to
    # REGIONS or the tile math silently changes the population.
    assert len(mdc.region_tiles(list(mdc.REGIONS))) == 97
    assert len(mdc.region_tiles(list(mdc.REGIONS))) == len(set(mdc.region_tiles(list(mdc.REGIONS))))


def test_decode_places_tile_local_points_y_up():
    x, y, z = 15, 23, 6
    # mapbox_vector_tile's default is y-up: (0, 4096) is the tile's NW corner
    raw = mapbox_vector_tile.encode(
        [
            {
                "name": "sequence",
                "features": [
                    {
                        "geometry": "LINESTRING (0 4096, 2048 2048)",
                        "properties": {"id": "s1", "is_pano": True, "creator_id": 7},
                    }
                ],
            }
        ]
    )
    (seq,) = mdc.decode_sequences(raw, x, y, z)
    nw = tile_frac_to_lonlat(x, y, z)
    mid = tile_frac_to_lonlat(x + 0.5, y + 0.5, z)
    assert seq["_pts"][0] == pytest.approx(nw, abs=1e-6)
    assert seq["_pts"][1] == pytest.approx(mid, abs=1e-6)
    assert seq["id"] == "s1" and seq["creator_id"] == 7


def test_decode_tolerates_an_empty_or_layerless_tile():
    assert mdc.decode_sequences(b"", 0, 0, 6) == []
    raw = mapbox_vector_tile.encode([{"name": "overview", "features": []}])
    assert mdc.decode_sequences(raw, 0, 0, 6) == []


def test_split_samples_conserve_length_and_bound_piece_size():
    pts = [
        (-94.85, 42.84),
        (-94.80, 42.84),
        (-94.80, 42.86),
        (-94.80, 42.86),
    ]  # last is a zero-length repeat
    total = sum(mdc.local_km(*a, *b) for a, b in zip(pts, pts[1:], strict=False))
    samples = list(mdc.split_samples(pts))
    assert sum(km for _, _, km in samples) == pytest.approx(total, rel=1e-12)
    assert max(km for _, _, km in samples) <= mdc.SAMPLE_KM + 1e-12
    # a long segment is spread along its length, not dumped at one end
    lons = [lon for lon, lat, _ in samples if lat == pytest.approx(42.84)]
    assert min(lons) > -94.85 and max(lons) < -94.80 and len(lons) >= 8


def _samples(rows):
    return pd.DataFrame(rows, columns=["lon", "lat", "km", "creator", "captured", "foot"])


def test_place_score_counts_only_its_own_disc():
    lat, lon = 42.8468, -94.8515
    km_per_deg_lon = 111.32 * math.cos(math.radians(lat))
    inside = lon + 1.9 / km_per_deg_lon  # 1.9 km east
    outside = lon + 2.1 / km_per_deg_lon  # 2.1 km east
    t = pd.Timestamp("2025-11-04").value // 10**6
    s = _samples(
        [
            (inside, lat, 3.0, 1, t, False),
            (lon, lat, 1.0, 2, t - 10**10, True),
            (outside, lat, 100.0, 3, t, False),
        ]
    )
    places = pd.DataFrame({"name": ["Laurens"], "lat": [lat], "lon": [lon]})
    (row,) = mdc.place_scores(s, places, r_km=2.0, min_km=0).to_dict("records")
    assert row["km_in_disc"] == pytest.approx(4.0)
    assert row["km_per_km2"] == pytest.approx(4.0 / (math.pi * 4), abs=1e-3)
    assert row["top_creator"] == 1 and row["top_share"] == pytest.approx(0.75)
    assert row["foot_share"] == pytest.approx(0.25)
    # length-weighted median: 3 of 4 km were captured on 2025-11-04
    assert row["median_captured"] == "2025-11-04"


def test_place_score_skips_empty_and_thin_discs():
    s = _samples([(0.0, 0.0, 1.0, 1, 0, False)])
    places = pd.DataFrame({"name": ["far", "thin"], "lat": [10.0, 0.0], "lon": [10.0, 0.0]})
    assert mdc.place_scores(s, places, min_km=0).name.tolist() == ["thin"]
    assert mdc.place_scores(s, places, min_km=5).empty


def test_thin_by_distance_keeps_the_first_of_a_cluster():
    df = pd.DataFrame(
        {"name": ["a", "b", "c"], "lat": [40.0, 40.01, 40.2], "lon": [-100.0, -100.0, -100.0]}
    )
    assert mdc.thin_by_distance(df, 5.0).name.tolist() == ["a", "c"]


def test_inside_grid_is_rectangle_membership_not_centre_distance():
    grids = pd.DataFrame(
        {
            "city_id": ["big", "small"],
            "lat": [21.33, 40.0],
            "lon": [-157.83, -100.0],
            "grid_width_m": [40_000.0, 1_000.0],
            "grid_height_m": [20_000.0, 1_000.0],
        }
    )
    # 15 km west of a 40 km-wide grid's centre is inside it
    assert (
        mda.inside_grid(21.33, -157.83 - 15 / (111.32 * math.cos(math.radians(21.33))), grids)
        == "big"
    )
    # 12 km north of it is outside (half-height is 10 km)
    assert mda.inside_grid(21.33 + 12 / 110.57, -157.83, grids) == ""


def test_manifest_strips_okina_and_applies_geocode_overrides():
    tranche = pd.DataFrame(
        {
            "name": ["Waipi'o Acres", "Fond du Lac"],
            "admin_name": ["Hawaii", "Wisconsin"],
            "cc": ["US", "US"],
            "pop": [5531, 42933],
            "geonameid": [5854718, 5253352],
            "lat": [21.46, 43.77],
            "lon": [-158.0, -88.4],
        }
    )
    rows = mda.manifest_rows(tranche).to_dict("records")
    assert rows[0]["city"] == "Waipio Acres"
    assert rows[0]["query_string"] == "Waipio Acres, Hawaii, United States"
    assert rows[1]["query_string"] == mda.GEOCODE_OVERRIDES[5253352]
    assert rows[1]["city"] == "Fond du Lac"


def test_jittered_gap_keeps_the_mean_and_a_floor():
    rng = random.Random(0)
    gaps = [mdc.jittered_gap(rng) for _ in range(20_000)]
    assert sum(gaps) / len(gaps) == pytest.approx(mdc.MEAN_GAP_S, rel=0.03)
    assert min(gaps) >= (1 - mdc.JITTER) * mdc.MEAN_GAP_S


def test_round_trip_tile_math_matches_the_calibration_tile():
    # Laurens' z6 tile was (15, 23) in the calibration run
    fx, fy = lonlat_to_tile_frac(-94.8515, 42.8468, 6)
    assert (int(fx), int(fy)) == (15, 23)


# ── The fetcher's stop rules (the template for a standing screen-provider mapillary) ──


class _FakeResponse:
    def __init__(self, status, ctype="application/x-protobuf", content=b"tile"):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = content


class _FakeSession:
    """Answers queued responses in order and records every request made."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _fetcher(tmp_path, monkeypatch, responses, max_requests=10):
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(mdcol.time, "sleep", lambda s: None)
    f = mdcol.TileFetcher(tmp_path, max_requests, seed=0)
    f.session = _FakeSession(responses)
    return f


def _log(tmp_path):
    return [json.loads(line) for line in (tmp_path / "request_log.jsonl").read_text().splitlines()]


@pytest.mark.parametrize(
    "response",
    [_FakeResponse(302, "text/html"), _FakeResponse(200, "text/html"), _FakeResponse(403, "")],
    ids=["302-login-redirect", "200-but-html", "403"],
)
def test_fetcher_stops_at_the_first_refusal_and_logs_it(tmp_path, monkeypatch, response):
    f = _fetcher(tmp_path, monkeypatch, [_FakeResponse(200), response, _FakeResponse(200)])
    assert f.get(6, 1, 1) == b"tile"
    with pytest.raises(mdcol.StopProbe):
        f.get(6, 1, 2)
    # the refusal is logged, never cached, and a 302 is not followed
    entries = _log(tmp_path)
    assert [e["status"] for e in entries] == [200, response.status_code]
    assert entries[1]["host"] == "tiles" and (entries[1]["z"], entries[1]["y"]) == (6, 2)
    assert not (tmp_path / "tiles" / "6_1_2.mvt").exists()
    assert f.session.calls[1][1]["allow_redirects"] is False
    assert len(f.session.calls) == 2


def test_fetcher_caches_a_204_as_empty_and_serves_cache_for_free(tmp_path, monkeypatch):
    f = _fetcher(tmp_path, monkeypatch, [_FakeResponse(204, "", b"")])
    assert f.get(6, 3, 4) == b""
    assert f.get(6, 3, 4) == b""
    assert (f.requests, f.cache_hits, len(f.session.calls)) == (1, 1, 1)


def test_fetcher_never_exceeds_max_requests(tmp_path, monkeypatch):
    f = _fetcher(tmp_path, monkeypatch, [_FakeResponse(200)] * 5, max_requests=2)
    f.get(6, 0, 0)
    f.get(6, 0, 1)
    with pytest.raises(mdcol.StopProbe, match="max-requests"):
        f.get(6, 0, 2)
    assert len(f.session.calls) == 2
    # a cached tile still costs nothing past the cap
    assert f.get(6, 0, 0) == b"tile"


def test_catalog_snapshot_exports_each_citys_latest_drive_walk(tmp_path):
    db = tmp_path / "catalog.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table cities (city_id, center_lat, center_lon, enabled, grid_width_m, grid_height_m)"
    )
    conn.execute(
        "create table street_walks (city_id, provider, network_type, run_date, "
        "coverage_pct_by_length, median_covered_age_years, length_km)"
    )
    conn.execute("insert into cities values ('a', 1.0, 2.0, 1, 1000, 1000)")
    conn.executemany(
        "insert into street_walks values (?, ?, ?, ?, ?, ?, ?)",
        [
            ("a", "mapillary", "drive", "2026-01-01", 10.0, 1.0, 5.0),
            ("a", "mapillary", "drive", "2026-06-01", 60.0, 0.5, 5.0),
            ("a", "mapillary", "all_public", "2026-09-01", 99.0, 0.1, 9.0),
            ("a", "gsv", "drive", "2026-09-01", 98.0, 0.1, 5.0),
        ],
    )
    conn.commit()
    conn.close()
    out = tmp_path / "snap.csv"
    mdcol.main(["catalog-snapshot", "--db", str(db), "--out", str(out)])
    snap = pd.read_csv(out)
    walks = snap[snap.table == "walk"]
    assert walks.run_date.tolist() == ["2026-06-01"]
    assert walks.coverage_pct_by_length.tolist() == [60.0]


# ── The analyzer's selection rules, where they are applied ──


def _scored(**overrides):
    """One place that passes every rule; override columns per row."""
    base = {
        "name": "pass",
        "geonameid": 1,
        "lat": 40.0,
        "lon": -100.0,
        "pop": 5000,
        "km_per_km2": 5.0,
        "top_creator": 1,
        "top_share": 0.9,
        "foot_share": 0.0,
        "median_captured": "2025-06-01",
        "catalog_km": 50.0,
        "inside_catalog_grid": "",
    }
    return {**base, **overrides}


def test_apply_rules_applies_every_candidate_rule():
    rows = [
        _scored(name="pass", lat=40.0, km_per_km2=9.0),
        _scored(name="in_grid", lat=41.0, inside_catalog_grid="some-city"),
        _scored(name="near_centre", lat=42.0, catalog_km=9.9),
        _scored(name="old", lat=43.0, median_captured="2024-12-31"),
        _scored(name="sparse", lat=44.0, km_per_km2=2.99),
        _scored(name="mixed", lat=45.0, top_share=0.59),
        _scored(name="thinned", lat=40.01, km_per_km2=8.0),  # ~1.1 km from "pass"
    ]
    out = mda.apply_rules(pd.DataFrame(rows), mda.CANDIDATE_RULES)
    assert out.name.tolist() == ["pass"]


def test_apply_rules_tranche_caps_per_uploader_population_foot_and_size():
    rows = [_scored(name=f"c1-{i}", lat=30.0 + i, top_creator=1) for i in range(5)]
    rows += [
        _scored(name="big", lat=20.0, top_creator=2, pop=60_001),
        _scored(name="on_foot", lat=21.0, top_creator=3, foot_share=0.5),
        _scored(name="c4", lat=22.0, top_creator=4),
    ]
    out = mda.apply_rules(pd.DataFrame(rows), mda.TRANCHE_RULES)
    assert out.name.tolist() == ["c1-0", "c1-1", "c1-2", "c4"]
    small = mda.apply_rules(pd.DataFrame(rows), {**mda.TRANCHE_RULES, "size": 2})
    assert small.name.tolist() == ["c1-0", "c1-1"]


def test_grid_rule_effect_counts_removals_not_the_net_change():
    # "a" is inside a grid and suppresses its neighbour "b" by thinning;
    # removing "a" readmits "b", so the net change (0) hides one removal.
    rows = [
        _scored(name="a", geonameid=1, lat=40.0, km_per_km2=9.0, inside_catalog_grid="g"),
        _scored(name="b", geonameid=2, lat=40.01, km_per_km2=8.0),
        _scored(name="c", geonameid=3, lat=45.0, km_per_km2=7.0),
    ]
    assert mda.grid_rule_effect(pd.DataFrame(rows), mda.CANDIDATE_RULES) == {
        "thinned_list_without_the_rule": 2,
        "of_which_inside_a_catalog_grid": 1,
        "readmitted_once_those_are_removed": 1,
        "cascade_dropped_once_those_are_removed": 0,
        "thinned_list_with_the_rule": 2,
    }


def test_grid_rule_effect_counts_a_cascade_rather_than_netting_it_out():
    # G is in a grid; B is 4 km east of G, A 8 km east of G and so 4 km from B.
    # Without the rule thinning keeps G and A; with it, B is readmitted and
    # then thins A away. The net change (2 - 1 + 0 = 1) would hide both.
    d = 4 / (111.32 * math.cos(math.radians(40.0)))
    rows = [
        _scored(name="G", geonameid=1, lon=-100.0, km_per_km2=9.0, inside_catalog_grid="g"),
        _scored(name="B", geonameid=2, lon=-100.0 + d, km_per_km2=8.0),
        _scored(name="A", geonameid=3, lon=-100.0 + 2 * d, km_per_km2=7.0),
    ]
    assert mda.grid_rule_effect(pd.DataFrame(rows), mda.CANDIDATE_RULES) == {
        "thinned_list_without_the_rule": 2,
        "of_which_inside_a_catalog_grid": 1,
        "readmitted_once_those_are_removed": 1,
        "cascade_dropped_once_those_are_removed": 1,
        "thinned_list_with_the_rule": 1,
    }


def test_grid_rule_effect_refuses_duplicate_geonameids():
    rows = [_scored(name="x", lat=40.0), _scored(name="y", lat=45.0)]  # both geonameid 1
    with pytest.raises(ValueError, match="geonameid"):
        mda.grid_rule_effect(pd.DataFrame(rows), mda.CANDIDATE_RULES)


def test_build_samples_keeps_only_recent_panos():
    since = pd.Timestamp("2024-10-02").value // 10**6
    line = [(-100.0, 40.0), (-100.0, 40.001)]
    seq = pd.DataFrame(
        {
            "is_pano": [True, True, False],
            "captured_at": [since, since - 1, since + 1],
            "creator_id": [1, 2, 3],
            "foot": [False, False, False],
            "pts": [line, line, line],
        }
    )
    assert mda.build_samples(seq, since).creator.tolist() == [1]


def test_spearman_is_rank_correlation_not_pearson():
    a = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, None])
    b = pd.Series([1.0, 2.0, 3.0, 4.0, 1000.0, 7.0])
    assert mda.spearman(a, b) == 1.0


def test_nearest_km_picks_the_nearest():
    i, d = mdc.nearest_km(40.0, -100.0, pd.Series([41.0, 40.1, 50.0]), pd.Series([-100.0] * 3))
    assert i == 1 and d == pytest.approx(11.1, abs=0.1)


def test_inside_grid_bounds_the_width_at_half():
    grids = pd.DataFrame(
        {
            "city_id": ["big"],
            "lat": [21.33],
            "lon": [-157.83],
            "grid_width_m": [40_000.0],
            "grid_height_m": [20_000.0],
        }
    )
    # 21 km west of a 40 km-wide grid's centre is outside it
    assert (
        mda.inside_grid(21.33, -157.83 - 21 / (111.32 * math.cos(math.radians(21.33))), grids) == ""
    )


def test_place_score_median_is_length_weighted_toward_older_imagery():
    t_new = pd.Timestamp("2025-11-04").value // 10**6
    t_old = pd.Timestamp("2025-01-15").value // 10**6
    s = _samples([(0.0, 0.0, 3.0, 1, t_old, False), (0.0, 0.0, 1.0, 1, t_new, False)])
    places = pd.DataFrame({"name": ["p"], "lat": [0.0], "lon": [0.0]})
    (row,) = mdc.place_scores(s, places, min_km=0).to_dict("records")
    assert row["median_captured"] == "2025-01-15"
    assert row["newest_captured"] == "2025-11-04"


def test_manifest_admin_is_empty_not_nan_when_geonames_has_none():
    tranche = pd.DataFrame(
        {
            "name": ["Nowhere"],
            "admin_name": [float("nan")],
            "cc": ["US"],
            "pop": [600],
            "geonameid": [1],
            "lat": [40.0],
            "lon": [-100.0],
        }
    )
    (row,) = mda.manifest_rows(tranche).to_dict("records")
    assert row["admin"] == ""
    assert row["query_string"] == "Nowhere, United States"


# ── The analyzer's population: only what was actually scanned ──

_MANIFEST_2026_10_02 = {"regions": dict(mdc.REGIONS), "zoom": mdc.SCREEN_ZOOM}


def test_scan_tiles_are_the_manifests_own_regions():
    assert mda.scan_tiles(_MANIFEST_2026_10_02) == set(mdc.region_tiles(list(mdc.REGIONS)))


def test_validation_drops_a_catalog_city_in_no_scanned_tile():
    # Mexico City and Kodiak are inside SCAN_BOX but in no scanned z6 tile; scored,
    # they would enter the validation as 0 -- an absence nobody observed.
    cat = pd.DataFrame(
        {
            "city_id": ["laurens", "mexico-city", "kodiak"],
            "lat": [42.8468, 19.4326, 57.79],
            "lon": [-94.8515, -99.1332, -152.41],
        }
    )
    walks = pd.DataFrame(
        {
            "city_id": ["laurens", "mexico-city", "kodiak"],
            "coverage_pct_by_length": [91.6, 5.0, 42.4],
            "median_covered_age_years": [0.81, 3.0, 4.0],
        }
    )
    samples = _samples([(-94.8515, 42.8468, 3.0, 1, 0, False)])
    val = mda.build_validation(
        samples, cat, walks, mda.scan_tiles(_MANIFEST_2026_10_02), mdc.SCREEN_ZOOM
    )
    assert val.city_id.tolist() == ["laurens"]
    assert val.km_per_km2.iloc[0] > 0


def test_analyzer_refuses_a_scan_that_stopped(tmp_path):
    (tmp_path / "scan_manifest.json").write_text(
        json.dumps({**_MANIFEST_2026_10_02, "stopped": "STOP at 6/15/23: HTTP 302 (text/html)"})
    )
    with pytest.raises(SystemExit, match="partial scan"):
        mda.main(
            [
                "--raw-dir",
                str(tmp_path),
                "--places",
                "unused",
                "--prod-snapshot",
                "unused",
                "--catalog-label",
                "t",
                "--docs-dir",
                str(tmp_path),
                "--manifest-out",
                str(tmp_path / "m.csv"),
            ]
        )
    assert not (tmp_path / "m.csv").exists()


def test_validation_keeps_scanned_zero_sample_cities_and_tile_edges_and_only_walked_ones():
    # One scanned tile, (15, 23) at z6. Points are placed by tile FRACTION:
    # 15.9 is inside it (int) but rounds into 16; 14.6 is outside but rounds into 15.
    tiles = {(15, 23)}

    def at(fx, fy):
        lon, lat = tile_frac_to_lonlat(fx, fy, mdc.SCREEN_ZOOM)
        return lat, lon

    places = {
        "laurens": (42.8468, -94.8515),
        "no_samples": at(15.5, 23.2),
        "edge_in": at(15.9, 23.5),
        "edge_out": at(14.6, 23.5),
        "unwalked": at(15.3, 23.8),
        "thin_walk": at(15.7, 23.3),
        "stale_walk": at(15.4, 23.4),
    }
    cat = pd.DataFrame(
        {
            "city_id": list(places),
            "lat": [ll[0] for ll in places.values()],
            "lon": [ll[1] for ll in places.values()],
        }
    )
    walked = ["laurens", "no_samples", "edge_in", "edge_out", "thin_walk", "stale_walk"]
    cov = {"thin_walk": 49.9, "stale_walk": 60.0}
    age = {"stale_walk": 2.01}
    walks = pd.DataFrame(
        {
            "city_id": walked,
            "coverage_pct_by_length": [cov.get(c, 91.6) for c in walked],
            "median_covered_age_years": [age.get(c, 0.81) for c in walked],
        }
    )
    samples = _samples([(-94.8515, 42.8468, 3.0, 1, 0, False)])
    val = mda.build_validation(samples, cat, walks, tiles, mdc.SCREEN_ZOOM).set_index("city_id")
    # edge_out is in no scanned tile, unwalked has no walk to validate against
    assert sorted(val.index) == sorted(
        ["laurens", "no_samples", "edge_in", "thin_walk", "stale_walk"]
    )
    # a scanned city with no samples scored 0 -- it was observed, and holds nothing
    assert val.km_per_km2["no_samples"] == 0.0
    assert val.km_per_km2["laurens"] > 0
    assert val.good.to_dict() == {
        "laurens": True,
        "no_samples": True,
        "edge_in": True,
        "thin_walk": False,
        "stale_walk": False,
    }


def test_scan_records_a_stop_exits_nonzero_and_the_analyzer_refuses_it(tmp_path, monkeypatch):
    # The producer half of the stopped-scan contract: collect scan still writes
    # sequences.parquet after a stop, so the manifest's `stopped` is all that
    # tells the analyzer an unscanned tile is not an empty one.
    session = _FakeSession([_FakeResponse(204, "", b""), _FakeResponse(302, "text/html")])
    session.headers = {}
    monkeypatch.setattr(mdcol.requests, "Session", lambda: session)
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(mdcol.time, "sleep", lambda s: None)
    rc = mdcol.main(["scan", "--out-dir", str(tmp_path), "--regions", "hawaii", "--seed", "0"])
    assert rc != 0
    assert len(session.calls) == 2  # stopped at the refusal; hawaii is 4 tiles
    manifest = json.loads((tmp_path / "scan_manifest.json").read_text())
    assert manifest["stopped"].startswith("STOP at 6/")
    assert "302" in manifest["stopped"]
    with pytest.raises(SystemExit, match="partial scan"):
        mda.load_scan_manifest(tmp_path)


def test_metrics_json_is_strict_with_nan_as_null():
    import numpy as np

    text = mda.strict_json({"a": float("nan"), "b": [np.float64("nan"), 1.5], "c": "x"})

    def refuse(const):
        raise AssertionError(f"non-strict JSON constant {const}")

    assert json.loads(text, parse_constant=refuse) == {"a": None, "b": [None, 1.5], "c": "x"}
    with pytest.raises(ValueError):
        mda.strict_json({"inf": float("inf")})


def test_grid_rule_effect_counts_every_readmission():
    # two in-grid places, each suppressing its own neighbour: 2 readmitted
    rows = [
        _scored(name="a", geonameid=1, lat=40.0, km_per_km2=9.0, inside_catalog_grid="g"),
        _scored(name="b", geonameid=2, lat=40.01, km_per_km2=8.0),
        _scored(name="c", geonameid=3, lat=45.0, km_per_km2=7.0, inside_catalog_grid="h"),
        _scored(name="d", geonameid=4, lat=45.01, km_per_km2=6.0),
    ]
    effect = mda.grid_rule_effect(pd.DataFrame(rows), mda.CANDIDATE_RULES)
    assert effect["of_which_inside_a_catalog_grid"] == 2
    assert effect["readmitted_once_those_are_removed"] == 2
    assert effect["thinned_list_with_the_rule"] == 2


def test_analyzer_writes_strict_metrics_when_a_reference_town_has_no_score(tmp_path):
    # End to end over synthetic inputs. Wasta is a reference town in a scanned
    # tile with no samples near it, so its top_creator/top_share/median_captured
    # are NaN -- which main() must write as null, never as a bare NaN.
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "scan_manifest.json").write_text(
        json.dumps({"regions": {"hawaii": mdc.REGIONS["hawaii"]}, "zoom": 6, "tiles": 4, "rows": 1})
    )
    lat, lon = 21.30, -157.85  # Honolulu
    t = pd.Timestamp("2026-01-15").value // 10**6
    zigzag = [(lon + (0.015 if i % 2 else 0.0), lat) for i in range(7)]  # ~9 km in a 2 km disc
    pd.DataFrame(
        [
            {
                "seq": "s1",
                "tile": "4_28",
                "creator_id": 7,
                "organization_id": None,
                "captured_at": t,
                "is_pano": True,
                "foot": False,
                "image_id": 1,
                "quality_score": 0.5,
                "pts": zigzag,
            }
        ]
    ).to_parquet(raw / "sequences.parquet")
    (raw / "calibration.json").write_text("{}")
    (raw / "request_log.jsonl").write_text(json.dumps({"host": "tiles", "status": 200}) + "\n")
    place = ["1", "Testville", "", "", str(lat), str(lon + 0.0075), "P", "PPL", "US", "", "HI"]
    place += ["", "", "", "1000", "", "", "", ""]
    (tmp_path / "places.txt").write_text("\t".join(place) + "\n")
    (tmp_path / "admin1.txt").write_text("US.HI\tHawaii\tHawaii\t1\n")
    snap = pd.DataFrame(
        [
            {
                "table": "cities",
                "city_id": "honolulu--hawaii--united-states",
                "lat": lat,
                "lon": lon + 0.0075,
                "enabled": 1,
                "grid_width_m": 1000,
                "grid_height_m": 1000,
            },
            {
                "table": "cities",
                "city_id": "wasta--south-dakota--united-states",
                "lat": 19.7,
                "lon": -155.1,
                "enabled": 1,
                "grid_width_m": 1000,
                "grid_height_m": 1000,
            },
            {
                "table": "walk",
                "city_id": "honolulu--hawaii--united-states",
                "run_date": "2026-09-01",
                "coverage_pct_by_length": 90.0,
                "median_covered_age_years": 0.5,
                "length_km": 10.0,
            },
            {
                "table": "walk",
                "city_id": "wasta--south-dakota--united-states",
                "run_date": "2026-09-26",
                "coverage_pct_by_length": 56.3,
                "median_covered_age_years": 6.49,
                "length_km": 5.0,
            },
        ]
    )
    snap.to_csv(tmp_path / "snap.csv", index=False)
    docs = tmp_path / "docs"
    docs.mkdir()
    rc = mda.main(
        [
            "--raw-dir",
            str(raw),
            "--places",
            str(tmp_path / "places.txt"),
            "--admin1",
            str(tmp_path / "admin1.txt"),
            "--prod-snapshot",
            str(tmp_path / "snap.csv"),
            "--catalog-label",
            "t",
            "--docs-dir",
            str(docs),
            "--manifest-out",
            str(tmp_path / "m.csv"),
        ]
    )
    assert rc == 0

    def refuse(const):
        raise AssertionError(f"non-strict JSON constant {const}")

    metrics = json.loads(
        (docs / "mapillary-discovery-screen_metrics.json").read_text(), parse_constant=refuse
    )
    refs = {r["city_id"]: r for r in metrics["validation"]["reference_towns"]}
    wasta = refs["wasta--south-dakota--united-states"]
    assert (wasta["top_creator"], wasta["top_share"], wasta["median_captured"]) == (
        None,
        None,
        None,
    )
    assert wasta["km_per_km2"] == 0.0
    assert refs["honolulu--hawaii--united-states"]["top_creator"] == 7
