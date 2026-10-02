"""Sampling invariants for the Mapillary discovery screen (#383).

The screen's numbers are only as good as four mappings: tile-local coordinates
to the globe (a flipped y puts a town's sweep in the next state), simplified
geometry to length (the score is a length), length to a place (which disc a
sample lands in), and a place to "already tracked" (distance to a centre is not
grid membership). These tests pin those, plus the sampling frame itself.
"""

import math
import random

import mapbox_vector_tile
import pandas as pd
import pytest

from scripts import mapillary_discovery_analyze as mda
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
