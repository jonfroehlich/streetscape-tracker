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
import numpy as np
import pandas as pd
import pytest

from scripts import mapillary_discovery_analyze as mda
from scripts import mapillary_discovery_collect as mdcol
from scripts import mapillary_discovery_common as mdc
from scripts import mapillary_discovery_score as mds
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


def test_a_failed_connection_never_carries_the_token_out(tmp_path, monkeypatch):
    import requests

    class _Dead:
        def get(self, url, **kwargs):
            # requests' own message quotes the URL, query string included
            raise requests.ConnectionError(f"{url}?access_token={kwargs['params']['access_token']}")

    f = _fetcher(tmp_path, monkeypatch, [])
    f.session = _Dead()
    with pytest.raises(mdcol.StopProbe) as e:
        f.get(6, 1, 1)
    assert "ConnectionError" in str(e.value) and "test-token" not in str(e.value)
    # `from None`: nothing chained, so a printed traceback has no token either
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert f.requests == 1 and not (tmp_path / "tiles" / "6_1_1.mvt").exists()


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


# ── A second region, the fetch-only path and the region-agnostic scorer ──


def test_europe_is_an_extra_region_and_never_part_of_the_default_frame():
    # REGIONS is the pinned 2026-10-02 frame; a region added to it would change
    # the committed population and `scan`'s default at once.
    assert "europe" in mdc.EXTRA_REGIONS and "europe" not in mdc.REGIONS
    assert len(mdc.region_tiles(["europe"])) == 104
    assert mdcol.parse_args(["scan", "--out-dir", "x"]).regions == list(mdc.REGIONS)
    with pytest.raises(SystemExit):
        mdcol.parse_args(["fetch", "--out-dir", "x"])  # fetch has no default region


@pytest.mark.parametrize(
    ("env", "file_lines", "expected"),
    [
        ({"MAPILLARY_DISCOVERY_ACCESS_TOKEN": "d", "MAPILLARY_ACCESS_TOKEN": "p"}, [], ("d", 0)),
        ({"MAPILLARY_ACCESS_TOKEN": "p"}, [], ("p", 1)),
        # the discovery token in .env outranks production's in the environment
        ({"MAPILLARY_ACCESS_TOKEN": "p"}, ["MAPILLARY_DISCOVERY_ACCESS_TOKEN='d'"], ("d", 0)),
        ({}, ["MAPILLARY_STREETS_ACCESS_TOKEN=s", "MAPILLARY_ACCESS_TOKEN=p"], ("p", 1)),
        # the environment outranks .env for the same variable
        (
            {"MAPILLARY_DISCOVERY_ACCESS_TOKEN": "env"},
            ["MAPILLARY_DISCOVERY_ACCESS_TOKEN=file"],
            ("env", 0),
        ),
    ],
)
def test_the_discovery_token_outranks_the_production_one(
    tmp_path, monkeypatch, env, file_lines, expected
):
    for var in mdcol.TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    (tmp_path / ".env").write_text("\n".join(file_lines) + "\n")
    monkeypatch.setattr(mdcol, "ENV_FILE", tmp_path / ".env")
    token, var = mdcol.load_token_named()
    assert (token, var) == (expected[0], mdcol.TOKEN_ENV_VARS[expected[1]])


def test_no_token_anywhere_is_refused_by_name(tmp_path, monkeypatch):
    for var in mdcol.TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mdcol, "ENV_FILE", tmp_path / "absent.env")
    with pytest.raises(SystemExit, match="MAPILLARY_DISCOVERY_ACCESS_TOKEN"):
        mdcol.load_token_named()


def test_the_request_log_names_the_token_variable_and_never_its_value(tmp_path, monkeypatch):
    monkeypatch.setattr(mdcol, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setenv("MAPILLARY_DISCOVERY_ACCESS_TOKEN", "MLY|secret-value")
    f = _fetcher(tmp_path, monkeypatch, [_FakeResponse(200)])
    f.get(6, 1, 1)
    assert f.session.calls[0][1]["params"] == {"access_token": "MLY|secret-value"}
    assert _log(tmp_path)[0]["token_var"] == "MAPILLARY_DISCOVERY_ACCESS_TOKEN"
    assert "secret-value" not in (tmp_path / "request_log.jsonl").read_text()


def _tile_local(lon, lat, x, y, zoom=6, extent=4096):
    """(lon, lat) as the y-up tile-local integer pair `mapbox_vector_tile.encode` takes."""
    fx, fy = lonlat_to_tile_frac(lon, lat, zoom)
    return round((fx - x) * extent), round((1 - (fy - y)) * extent)


def _sequence_tile(sequences, x, y):
    """A tile whose `sequence` layer holds (lonlat_points, properties) features."""
    features = []
    for pts, props in sequences:
        local = ", ".join("{} {}".format(*_tile_local(lon, lat, x, y)) for lon, lat in pts)
        features.append({"geometry": f"LINESTRING ({local})", "properties": props})
    return mapbox_vector_tile.encode([{"name": "sequence", "features": features}])


_HONOLULU = (21.30, -157.85)  # in z6 tile (4, 28), one of hawaii's four
_HAWAII_TILES = mdc.region_tiles(["hawaii"])


def _fetch_session(monkeypatch, responses):
    session = _FakeSession(responses)
    session.headers = {}
    monkeypatch.setattr(mdcol.requests, "Session", lambda: session)
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(mdcol.time, "sleep", lambda s: None)
    return session


def test_fetch_caches_tiles_and_writes_a_manifest_without_the_parquet(tmp_path, monkeypatch):
    lat, lon = _HONOLULU
    tile = _sequence_tile(
        [([(lon, lat), (lon + 0.01, lat)], {"id": "s1", "is_pano": True, "creator_id": 7})], 4, 28
    )
    assert (4, 28) in _HAWAII_TILES and len(_HAWAII_TILES) == 4
    session = _fetch_session(
        monkeypatch,
        [
            _FakeResponse(200, content=tile) if t == (4, 28) else _FakeResponse(204, "", b"")
            for t in _HAWAII_TILES
        ],
    )
    rc = mdcol.main(["fetch", "--out-dir", str(tmp_path), "--regions", "hawaii", "--seed", "0"])
    assert rc == 0 and len(session.calls) == 4
    manifest = json.loads((tmp_path / "scan_manifest.json").read_text())
    assert manifest["stopped"] is None
    assert (manifest["tiles"], manifest["requests"], manifest["rows"]) == (4, 4, 1)
    assert manifest["regions"] == {"hawaii": list(mdc.REGIONS["hawaii"])}
    assert manifest["token_var"] == "MAPILLARY_ACCESS_TOKEN"
    assert not (tmp_path / "sequences.parquet").exists()
    assert sorted(p.name for p in (tmp_path / "tiles").iterdir()) == sorted(
        f"6_{x}_{y}.mvt" for x, y in _HAWAII_TILES
    )
    # the analyzer's manifest reader accepts it, and names the same tiles
    assert mda.scan_tiles(mda.load_scan_manifest(tmp_path)) == set(_HAWAII_TILES)
    # a second run is free: every tile is a cache hit
    session.responses = []
    assert mdcol.main(["fetch", "--out-dir", str(tmp_path), "--regions", "hawaii"]) == 0
    assert len(session.calls) == 4


def test_fetch_dry_run_makes_no_request_and_writes_nothing(tmp_path, monkeypatch, capsys):
    session = _fetch_session(monkeypatch, [])
    (tmp_path / "tiles").mkdir()
    x, y = _HAWAII_TILES[0]
    (tmp_path / "tiles" / f"6_{x}_{y}.mvt").write_bytes(b"")
    rc = mdcol.main(["fetch", "--out-dir", str(tmp_path), "--regions", "hawaii", "--dry-run"])
    assert rc == 0 and session.calls == []
    assert "4 z6 tiles over hawaii; 1 cached, 3 to request" in capsys.readouterr().out
    assert not (tmp_path / "scan_manifest.json").exists()
    assert not (tmp_path / "request_log.jsonl").exists()


@pytest.mark.parametrize(
    ("responses", "argv", "calls", "stopped"),
    [
        ([_FakeResponse(204, "", b""), _FakeResponse(302, "text/html")], [], 2, "302"),
        ([_FakeResponse(204, "", b"")] * 4, ["--max-requests", "2"], 2, "max-requests 2"),
    ],
    ids=["refusal", "request-cap"],
)
def test_fetch_records_a_stop_and_the_scorer_refuses_it(
    tmp_path, monkeypatch, responses, argv, calls, stopped
):
    session = _fetch_session(monkeypatch, responses)
    rc = mdcol.main(["fetch", "--out-dir", str(tmp_path), "--regions", "hawaii", *argv])
    assert rc != 0 and len(session.calls) == calls
    assert stopped in json.loads((tmp_path / "scan_manifest.json").read_text())["stopped"]
    with pytest.raises(SystemExit, match="partial scan"):
        mds.main(
            [
                "--raw-dir",
                str(tmp_path),
                "--places",
                "x",
                "--prod-snapshot",
                "x",
                "--scan-date",
                "2026-11-25",
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )


def test_age_weight_steps_at_two_three_and_five_years():
    ages = [0.0, 2.0, 2.001, 3.0, 3.001, 5.0, 5.001, float("nan")]
    assert mdc.age_weight(ages).tolist() == [1.0, 1.0, 0.75, 0.75, 0.25, 0.25, 0.0, 0.0]


def test_sample_polyline_agrees_with_split_samples_and_conserves_length():
    pts = [(-94.85, 42.84), (-94.80, 42.84), (-94.80, 42.84), (-94.80, 42.86), (-94.7995, 42.86)]
    lon, lat, km = mdc.sample_polyline(pts, 0.2)
    want = list(mdc.split_samples(pts, 0.2))
    assert len(km) == len(want) > 20
    assert lon.tolist() == pytest.approx([w[0] for w in want])
    assert lat.tolist() == pytest.approx([w[1] for w in want])
    assert km.tolist() == pytest.approx([w[2] for w in want])
    assert km.max() <= 0.2 + 1e-9
    assert all(len(a) == 0 for a in mdc.sample_polyline([(1.0, 2.0)]))
    assert all(len(a) == 0 for a in mdc.sample_polyline([(1.0, 2.0), (1.0, 2.0)]))


_P_LAT, _P_LON = 45.0, 10.0


def _at(east_km, north_km):
    """(lon, lat) a given offset from the test place, in place_features' own projection."""
    return (
        _P_LON + east_km / (111.32 * math.cos(math.radians(_P_LAT))),
        _P_LAT + north_km / 110.57,
    )


def _pano(rows):
    """PanoSamples from (east_km, north_km, km, age_yr, creator, foot) rows."""
    lon, lat = zip(*[_at(r[0], r[1]) for r in rows], strict=True)
    return mdc.PanoSamples(lon, lat, *zip(*[r[2:] for r in rows], strict=True))


def test_place_features_count_cells_by_their_newest_imagery():
    cells_in_1km = math.pi / 0.25**2
    s = _pano(
        [
            (0.10, 0.10, 0.2, 1.0, 7, False),  # a cell with new imagery ...
            (0.12, 0.12, 0.2, 6.0, 8, False),  # ... and old imagery too: counted by the newest
            (0.40, 0.10, 0.2, 4.0, 7, False),  # a cell holding only 4-year-old imagery
            (0.10, 0.60, 0.2, 2.5, 7, False),  # only 2.5-year-old imagery
            (0.60, 0.60, 0.2, 9.0, 7, False),  # only imagery past 5 years: weighs nothing
            (0.85, 0.10, 0.2, float("nan"), 7, False),  # undated: weighs nothing
        ]
    )
    f = mdc.place_features(s, _P_LAT, _P_LON)
    assert f["occ1_recent"] == pytest.approx(1 / cells_in_1km)
    assert f["occ1_aw"] == pytest.approx((1 + 0.25 + 0.75) / cells_in_1km)
    # the same cells against a larger disc's area: the score falls with radius,
    # and V4 is the best disc
    assert f["occ2_aw"] == pytest.approx(f["occ1_aw"] / 4)
    assert f["v4"] == f["occ1_aw"] > f["occ2_aw"] > f["occ3_aw"] > 0
    # V0 is recent LENGTH per km^2 of the 2 km disc, not cells
    assert f["v0"] == pytest.approx(0.2 / (math.pi * 4))
    assert f["newest_age"] == 1.0 and f["km2"] == pytest.approx(1.2)


def test_place_features_read_each_disc_only():
    s = _pano(
        [
            (0.5, 0.0, 1.0, 1.0, 7, False),
            (1.5, 0.0, 1.0, 1.0, 7, True),
            (2.5, 0.0, 1.0, 1.0, 8, False),  # outside the 2 km disc: no V0, share or foot
            (3.5, 0.0, 9.0, 1.0, 9, True),  # outside every disc
        ]
    )
    f = mdc.place_features(s, _P_LAT, _P_LON)
    cell = 0.25**2
    assert f["occ1_recent"] == pytest.approx(1 * cell / math.pi)
    assert f["occ2_recent"] == pytest.approx(2 * cell / (math.pi * 4))
    assert f["occ3_recent"] == pytest.approx(3 * cell / (math.pi * 9))
    assert f["v0"] == pytest.approx(2.0 / (math.pi * 4))
    assert (f["top"], f["top_share"], f["n_cre"]) == (7, 1.0, 1)
    assert f["foot_share"] == pytest.approx(0.5)
    # nothing within reach: zeros and NaNs, never an exception
    far = mdc.place_features(s, _P_LAT + 1.0, _P_LON)
    assert far["v4"] == 0.0 and far["top"] == -1 and math.isnan(far["top_share"])


def test_place_features_top_uploader_is_by_age_weighted_length_and_median_by_length():
    s = _pano(
        [
            (0.1, 0.1, 3.0, 8.0, 1, False),  # the longest, but 8 years old: weight floor 0.01
            (0.4, 0.1, 1.0, 1.0, 2, False),
            (0.1, 0.4, 0.5, 2.5, 3, False),
        ]
    )
    f = mdc.place_features(s, _P_LAT, _P_LON)
    weighted = {1: 3.0 * 0.01, 2: 1.0 * 1.0, 3: 0.5 * 0.75}
    assert f["top"] == 2
    assert f["top_share"] == pytest.approx(weighted[2] / sum(weighted.values()))
    assert f["n_cre"] == 3
    # 4.5 km in all; the half-length point (2.25 km) falls in the 8-year-old 3 km
    assert f["med_age"] == 8.0 and f["newest_age"] == 1.0


def test_tile_samples_keep_only_panos_and_each_uploaders_newest_image():
    lat, lon = _HONOLULU
    line = [(lon, lat), (lon + 0.02, lat)]
    t = pd.Timestamp("2026-01-15").value // 10**6
    raw = _sequence_tile(
        [
            (line, {"id": "a", "is_pano": True, "creator_id": 7, "captured_at": t, "image_id": 1}),
            (
                line,
                {"id": "b", "is_pano": True, "creator_id": 7, "captured_at": t + 5, "image_id": 2},
            ),
            (line, {"id": "c", "is_pano": False, "creator_id": 9, "captured_at": t, "image_id": 3}),
            (line, {"id": "d", "is_pano": True, "foot": True}),  # no uploader, no date
        ],
        4,
        28,
    )
    slon, slat, km, cap, cre, foot, newest = mds.tile_samples(raw, 4, 28, 6)
    assert sorted(set(cre.tolist())) == [-1, 7]  # the flat sequence's uploader never appears
    per_sequence = km[cre == -1].sum()
    # a tile-local unit is ~150 m at z6, so the encoded 2 km line is exact only to that grid
    assert per_sequence == pytest.approx(mdc.local_km(lon, lat, lon + 0.02, lat), rel=0.1)
    assert km.sum() == pytest.approx(3 * per_sequence)
    assert np.isnan(cap[cre == -1]).all() and foot[cre == -1].all() and not foot[cre == 7].any()
    assert newest == {7: (float(t + 5), 2)}
    assert abs(slat.mean() - lat) < 0.01 and abs(slon.mean() - (lon + 0.01)) < 0.01


def test_catalog_relation_is_grid_membership_and_nearest_centre():
    cat = pd.DataFrame(
        {"lat": [45.0, 50.0], "lon": [10.0, 10.0], "grid_width_m": [20000, 1000],
         "grid_height_m": [4000, 1000]}
    )  # fmt: skip
    places = pd.DataFrame(
        {"lat": [_at(8, 0)[1], _at(0, 3)[1], _at(8, 3)[1]],
         "lon": [_at(8, 0)[0], _at(0, 3)[0], _at(8, 3)[0]]}
    )  # fmt: skip
    rel = mds.catalog_relation(places, cat)
    # 8 km east is inside the 20 km-wide grid; 3 km north is outside its 4 km height
    assert rel.in_grid.tolist() == [True, False, False]
    assert rel.catalog_km.tolist() == pytest.approx([8.0, 3.0, math.hypot(8, 3)], rel=1e-3)


_RULES = {
    "v4_min": 0.7,
    "top_share_min": 0.6,
    "foot_share_max": 0.5,
    "med_age_max": 2.0,
    "catalog_km_min": 5.0,
    "thin_km": 5.0,
    "pop_max": None,
    "countries": None,
}


def _place(**overrides):
    row = {
        "geonameid": 1,
        "lat": 45.0,
        "lon": 10.0,
        "cc": "IT",
        "pop": 5_000_000,
        "v4": 0.9,
        "top_share": 0.8,
        "foot_share": 0.1,
        "med_age": 1.0,
        "in_grid": False,
        "catalog_km": 50.0,
    }
    return {**row, **overrides}


@pytest.mark.parametrize(
    ("override", "rules"),
    [
        ({"v4": 0.69}, {}),
        ({"top_share": 0.59}, {}),
        ({"foot_share": 0.5}, {}),
        ({"med_age": 2.01}, {}),
        ({"in_grid": True}, {}),
        ({"catalog_km": 5.0}, {}),
        ({}, {"pop_max": 60000}),
        ({}, {"countries": ["US", "CA"]}),
    ],
)
def test_rank_places_applies_every_rule(override, rules):
    # the base row passes with the defaults -- a five-million-person city in
    # Italy included, since neither a population cap nor a country list is set
    base = pd.DataFrame([_place()])
    assert len(mds.rank_places(base, _RULES)[0]) == 1
    ranked, funnel = mds.rank_places(pd.DataFrame([_place(**override)]), {**_RULES, **rules})
    assert len(ranked) == 0 and funnel["after_thinning"] == 0


def test_rank_places_orders_by_v4_thins_and_reports_the_funnel():
    lon_3km = _at(3, 0)[0]
    places = pd.DataFrame(
        [
            _place(geonameid=1, v4=0.8),
            _place(geonameid=2, v4=1.1, lon=lon_3km),  # 3 km away and better: it survives
            _place(geonameid=3, v4=0.75, lat=46.0),
            _place(geonameid=4, v4=0.95, lat=47.0, in_grid=True),
            _place(geonameid=5, v4=0.2, lat=48.0),
        ]
    )
    ranked, funnel = mds.rank_places(places, _RULES)
    assert ranked.geonameid.tolist() == [2, 3]
    assert funnel == {
        "places_scored": 5,
        "places_v4_at_least_min": 4,
        "of_which_inside_a_catalog_grid": 1,
        "outside_grids_and_beyond_catalog_km_min": 3,
        "pass_all_rules": 3,
        "after_thinning": 2,
    }


def test_the_sample_cache_refuses_a_manifest_tile_missing_from_the_cache(tmp_path):
    (tmp_path / "tiles").mkdir()
    (tmp_path / "tiles" / "6_4_28.mvt").write_bytes(b"")
    with pytest.raises(SystemExit, match="not in the tile cache"):
        mds.build_sample_cache(tmp_path, [(4, 28), (4, 29)], 6)


def _score_inputs(tmp_path):
    """A fetched hawaii directory holding one dense 360 sweep around Honolulu, and its inputs."""
    lat, lon = _HONOLULU
    raw = tmp_path / "raw"
    (raw / "tiles").mkdir(parents=True)
    t = pd.Timestamp("2026-06-01").value // 10**6
    # a serpentine over +-1.5 km: rows 300 m apart, so most 250 m cells are occupied
    rows = [lat + (i * 0.3 - 1.5) / 110.57 for i in range(11)]
    half = 1.5 / (111.32 * math.cos(math.radians(lat)))
    sweep = []
    for i, r in enumerate(rows):
        ends = (lon - half, lon + half) if i % 2 == 0 else (lon + half, lon - half)
        sweep += [(e, r) for e in ends]
    props = {"id": "s", "is_pano": True, "creator_id": 7, "captured_at": t, "image_id": 11}
    for x, y in _HAWAII_TILES:
        tile = _sequence_tile([(sweep, props)], x, y) if (x, y) == (4, 28) else b""
        (raw / "tiles" / f"6_{x}_{y}.mvt").write_bytes(tile)
    (raw / "scan_manifest.json").write_text(
        json.dumps({"regions": {"hawaii": mdc.REGIONS["hawaii"]}, "zoom": 6, "tiles": 4, "rows": 1})
    )

    def place(gid, name, plat, plon, cc):
        row = [str(gid), name, name, "", str(plat), str(plon), "P", "PPL", cc, "", "HI"]
        return "\t".join(row + ["", "", "", "900000", "", "", "", ""])

    (tmp_path / "places.txt").write_text(
        "\n".join(
            [
                place(1, "Sweptville", lat, lon, "US"),
                place(2, "Emptyton", lat + 0.5, lon, "NA"),  # Namibia's code is not a NaN
                place(3, "Elsewhere", 48.85, 2.35, "FR"),  # in no scanned tile
            ]
        )
        + "\n"
    )
    snap = pd.DataFrame(
        [
            {"table": "cities", "city_id": "walked", "lat": lat + 0.2, "lon": lon, "enabled": 1,
             "grid_width_m": 1000, "grid_height_m": 1000},
            {"table": "walk", "city_id": "walked", "run_date": "2026-09-01",
             "coverage_pct_by_length": 3.0, "median_covered_age_years": 0.5, "length_km": 10.0},
        ]
    )  # fmt: skip
    snap.to_csv(tmp_path / "snap.csv", index=False)
    return raw


def _score(tmp_path, raw, *extra):
    out = tmp_path / "out"
    rc = mds.main(
        [
            "--raw-dir",
            str(raw),
            "--places",
            str(tmp_path / "places.txt"),
            "--prod-snapshot",
            str(tmp_path / "snap.csv"),
            "--scan-date",
            "2026-11-25",
            "--out-dir",
            str(out),
            *extra,
        ]
    )
    assert rc == 0
    return out


def test_scorer_end_to_end_ranks_the_swept_place_from_the_tile_cache(tmp_path):
    raw = _score_inputs(tmp_path)
    out = _score(tmp_path, raw)
    ranked = pd.read_csv(out / "places_ranked.csv", keep_default_na=False)
    # a 900,000-person place is ranked: no population cap by default
    assert ranked.name.tolist() == ["Sweptville"]
    row = ranked.iloc[0]
    assert row.v4 >= 0.7 and str(row.user) == "7" and row.top_share == 1.0
    assert 0.4 < row.med_age < 0.6  # 2026-06-01 seen from the 2026-11-25 scan date
    features = pd.read_parquet(out / "place_features.parquet").set_index("name")
    assert sorted(features.index) == ["Emptyton", "Sweptville"]  # Elsewhere is in no scanned tile
    assert features.cc["Emptyton"] == "NA" and features.v4["Emptyton"] == 0.0

    def refuse(const):
        raise AssertionError(f"non-strict JSON constant {const}")

    metrics = json.loads((out / "metrics.json").read_text(), parse_constant=refuse)
    assert metrics["funnel"]["places_scored"] == 2 and metrics["funnel"]["after_thinning"] == 1
    assert metrics["validation"]["walked_catalog_cities_in_scanned_tiles"] == 1
    assert metrics["_about"]["requests"] == 0
    # resolve-creators' input: each uploader's newest 360 image
    assert pd.read_csv(raw / "creator_images.csv").to_dict("records") == [
        {"creator_id": 7, "image_id": 11, "captured_at": pd.Timestamp("2026-06-01").value / 1e6}
    ]


def test_scorer_population_cap_and_scan_date_are_arguments(tmp_path):
    raw = _score_inputs(tmp_path)
    assert len(pd.read_csv(_score(tmp_path, raw, "--pop-max", "60000") / "places_ranked.csv")) == 0
    # the same tiles read from a scan date three years on: the sweep has aged out
    late = _score(tmp_path, raw, "--scan-date", "2029-11-25")
    assert len(pd.read_csv(late / "places_ranked.csv")) == 0
    features = pd.read_parquet(late / "place_features.parquet").set_index("name")
    assert 3.4 < features.med_age["Sweptville"] < 3.6


def test_a_stale_sample_cache_is_rebuilt_not_trusted(tmp_path):
    raw = _score_inputs(tmp_path)
    _score(tmp_path, raw)
    z = dict(np.load(raw / "samples.npz"))
    assert len(z["km"]) > 0
    # a cache built over other tiles than the manifest's must not be scored
    z["tiles"] = z["tiles"][:1]
    for k in ("lon", "lat", "km", "captured_ms", "creator", "foot"):
        z[k] = z[k][:0]
    np.savez(raw / "samples.npz", **z)
    out = _score(tmp_path, raw)
    assert pd.read_csv(out / "places_ranked.csv").name.tolist() == ["Sweptville"]


def test_a_valid_sample_cache_is_reused_and_a_refetched_tile_rebuilds_it(tmp_path, monkeypatch):
    raw = _score_inputs(tmp_path)
    _score(tmp_path, raw)
    built = []
    real = mds.build_sample_cache
    monkeypatch.setattr(mds, "build_sample_cache", lambda *a: built.append(1) or real(*a))
    _score(tmp_path, raw)
    assert built == []  # same tiles, same sizes: no decode
    # a re-scan into the same directory: same tile list, other bytes
    lat, lon = _HONOLULU
    props = {"id": "s", "is_pano": True, "creator_id": 7, "captured_at": 0, "image_id": 11}
    (raw / "tiles" / "6_4_28.mvt").write_bytes(
        _sequence_tile([([(lon, lat), (lon + 0.01, lat)], props)], 4, 28)
    )
    out = _score(tmp_path, raw)
    assert built == [1]
    assert pd.read_csv(out / "places_ranked.csv").empty  # the old sweep is no longer scored


def test_scorer_runs_where_no_catalog_city_is_in_the_scanned_tiles(tmp_path):
    raw = _score_inputs(tmp_path)
    snap = pd.read_csv(tmp_path / "snap.csv")
    snap.loc[snap.table == "cities", ["lat", "lon"]] = [48.85, 2.35]
    snap.to_csv(tmp_path / "snap.csv", index=False)
    out = _score(tmp_path, raw)
    v = json.loads((out / "metrics.json").read_text())["validation"]
    assert v["catalog_cities_in_scanned_tiles"] == 0 and v["strict_hits"] == 0
    assert pd.read_csv(out / "places_ranked.csv").name.tolist() == ["Sweptville"]


def test_scorer_reports_what_the_collection_cost_and_its_calibration(tmp_path):
    raw = _score_inputs(tmp_path)
    log = [
        {"host": "tiles", "status": 200, "bytes": 10, "token_var": "MAPILLARY_DISCOVERY_ACCESS_TOKEN"},
        {"host": "tiles", "status": 204, "bytes": 0, "token_var": "MAPILLARY_DISCOVERY_ACCESS_TOKEN"},
        {"status": 200, "bytes": 30},  # a pre-`host` entry is a tile request
        {"host": "graph", "status": 200, "creator_id": "7"},
    ]  # fmt: skip
    (raw / "request_log.jsonl").write_text("\n".join(json.dumps(e) for e in log) + "\n")
    town = {
        "by_zoom": [
            {"zoom": 6, "pano_recent": 9, "top_creator": 7},
            {"zoom": 10, "pano_recent": 10, "top_creator": 8},
        ]
    }
    empty = {"by_zoom": [{"zoom": z, "pano_recent": 0, "top_creator": None} for z in (6, 10)]}
    (raw / "calibration.json").write_text(
        json.dumps({"r_km": 3.0, "recent_since": "2024-11-25", "towns": {"a": town, "b": empty}})
    )
    out = _score(tmp_path, raw)
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["collection"] == {
        "by_host_and_status": {"graph:200": 1, "tiles:200": 2, "tiles:204": 1},
        "tile_bytes": 40,
        "largest_tile_bytes": 30,
        "token_vars": ["MAPILLARY_DISCOVERY_ACCESS_TOKEN"],
    }
    assert metrics["calibration"]["towns"] == {
        "a": {"z6_recent_pano": 9, "z10_recent_pano": 10, "ratio": 0.9, "same_top_creator": False},
        "b": {"z6_recent_pano": 0, "z10_recent_pano": 0, "ratio": None, "same_top_creator": True},
    }
    validation = pd.read_csv(out / "validation.csv")
    assert validation.city_id.tolist() == ["walked"] and not validation.strict[0]
