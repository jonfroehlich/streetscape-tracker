"""
Shared logic for the issue #106 grid-density experiment (20 m vs 10 m vs 5 m vs
road-clipped 5 m GSV sampling).

The experiment queries ONLY a 5 m lattice per area, aligned so the production 20 m
grid is a bit-identical index subset; the coarser variants and the road-clipped
variant are derived offline from that single snapshot. See
scripts/grid_density_collect.py and scripts/grid_density_analyze.py.

Alignment invariant (the whole experiment rests on this):

    generate_grid_points (download_common.py) computes each production point as
        destination(destination(origin, 0deg, i*step), 90deg, j*step)
    and (4*i)*5.0 == i*20.0 exactly in floats, so the 5 m lattice point at
    index (4i, 4j) is bit-identical to the production 20 m point at (i, j) --
    PROVIDED the 5 m index range is derived from the production 20 m range.
    A naive int(dim/5) sizing drops the most-negative row/col whenever
    int(dim/20) is odd (floor-division asymmetry of range(-n//2, n//2+1)).

Experiment artifacts live under experiments/grid-density/ (gitignored, never under data/ --
the rsync publisher would pick up any *.csv.gz there).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import geopy
import geopy.distance
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker.db import CityRow  # noqa: E402

FINE_STEP_M = 5.0
CLIP_DIST_M_DEFAULT = 15.0
DEFAULT_OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments", "grid-density"
)

# Same quantum as download_gsv.resume_point_key: the engine's own .downloading
# resume already proves a 9-decimal key survives the CSV round-trip.
_COORD_QUANT_DECIMALS = 9


@dataclass(frozen=True)
class StudyArea:
    """One experiment study area: a full city grid or a centered sub-tile of it."""

    key: str
    city_id: str
    # None = full city; t = tile with i20, j20 in [-t, t] around the frozen
    # center (a contiguous subrange of the full-city 20 m index space, so tile
    # points coincide exactly with rows in existing production runs).
    tile_halfwidth_i20: int | None
    label: str


STUDY_AREAS: dict[str, StudyArea] = {
    "adrian": StudyArea(
        "adrian", "adrian--oregon--united-states", None, "Adrian, OR (rural, full city)"
    ),
    "corvallis": StudyArea(
        "corvallis",
        "corvallis--oregon--united-states",
        50,
        "Corvallis, OR (college town, 2x2 km tile)",
    ),
    "seattle": StudyArea(
        "seattle",
        "seattle--washington--united-states",
        50,
        "Seattle, WA (dense urban, 2x2 km tile)",
    ),
}


def index_ranges_20m(city: CityRow, area: StudyArea) -> tuple[range, range]:
    """
    The area's (i, j) index ranges in the production 20 m grid.

    Full city reproduces download_gsv.py's sizing exactly:
        n = int(dim / step); indices in range(-(n // 2), n // 2 + 1)
    A tile is the centered subrange [-t, t] of that space (asserted to fit).
    """
    h20 = int(city.grid_height_m / city.step_m)
    w20 = int(city.grid_width_m / city.step_m)
    # NB: -n // 2 (floor of -n/2), NOT -(n // 2) — for odd n production's
    # range(-n // 2, n // 2 + 1) is asymmetric, e.g. n=41 -> -21..20.
    i20 = range(-h20 // 2, h20 // 2 + 1)
    j20 = range(-w20 // 2, w20 // 2 + 1)
    t = area.tile_halfwidth_i20
    if t is not None:
        if not (-t >= i20[0] and t <= i20[-1] and -t >= j20[0] and t <= j20[-1]):
            raise ValueError(
                f"Tile halfwidth {t} exceeds {city.city_id} grid "
                f"(i {i20[0]}..{i20[-1]}, j {j20[0]}..{j20[-1]})"
            )
        i20 = range(-t, t + 1)
        j20 = range(-t, t + 1)
    return i20, j20


def fine_index_ranges(i20: range, j20: range, substep: int) -> tuple[range, range]:
    """
    Fine-lattice index ranges derived FROM the 20 m ranges (never int(dim/5)),
    so every 20 m index i has its fine twin at substep*i. substep 4 = 5 m.
    """
    return (
        range(substep * i20[0], substep * i20[-1] + 1),
        range(substep * j20[0], substep * j20[-1] + 1),
    )


def generate_lattice(
    origin: geopy.Point, i_range: range, j_range: range, step_m: float
) -> list[tuple[float, float, int, int]]:
    """
    Generate (lat, lon, i, j) lattice points over explicit index ranges.

    Local twin of download_common.generate_grid_points, which cannot be reused
    directly because it only takes step COUNTS and always derives its own
    (possibly asymmetric) range. The two geopy destination() calls are kept in
    the identical order/bearings so that, for i = 4*i20 and step 5.0, the
    result is bit-identical to the production 20 m point (meters=i*step_m is
    the only geometry input, and (4*i20)*5.0 == i20*20.0 exactly).
    """
    points: list[tuple[float, float, int, int]] = []
    for i in i_range:
        north_point = geopy.distance.distance(meters=i * step_m).destination(origin, 0)
        for j in j_range:
            point = geopy.distance.distance(meters=j * step_m).destination(north_point, 90)
            points.append((point.latitude, point.longitude, i, j))
    return points


def lattice_frame(points: list[tuple[float, float, int, int]]) -> pd.DataFrame:
    """Lattice points as a DataFrame with a clean RangeIndex (lat, lon, i, j)."""
    return pd.DataFrame(points, columns=["lat", "lon", "i", "j"])


def quant_key(lat: float, lon: float) -> tuple[float, float]:
    """Quantized coordinate key; matches download_gsv.resume_point_key's quantum."""
    return (round(float(lat), _COORD_QUANT_DECIMALS), round(float(lon), _COORD_QUANT_DECIMALS))


def attach_indices(df: pd.DataFrame, lattice: pd.DataFrame) -> pd.DataFrame:
    """
    Join collected snapshot rows back to their lattice (i, j) indices.

    The engine's CSV has no index columns, only query_lat/query_lon; the join
    key is the 9-decimal quantized coordinate pair (distinct 5 m points are
    ~5e-5 deg apart, 4+ orders above the quantum, so keys cannot collide).

    Rows whose exact key misses (the engine's CSV write can shave a trailing
    ULP, which flips the 9th decimal when the value sits on a rounding
    boundary — observed ~1 in 10^5 rows) fall back to the nearest lattice
    point, accepted only within 1e-7 deg (~1 cm; grid spacing is ~5e-5 deg,
    so the assignment is unambiguous by 3 orders of magnitude).

    Raises:
        ValueError: if any snapshot row is farther than the fallback tolerance
            from every lattice point, any lattice point has no snapshot row
            (partial/aborted collection), or one matched more than one row.
    """
    key_to_ij = {quant_key(r.lat, r.lon): (r.i, r.j) for r in lattice.itertuples(index=False)}
    keys = [quant_key(lat, lon) for lat, lon in zip(df["query_lat"], df["query_lon"], strict=True)]
    lat_arr = lattice["lat"].to_numpy()
    lon_arr = lattice["lon"].to_numpy()
    ij: list[tuple] = []
    matched_keys: set = set()
    for k, (lat, lon) in zip(keys, zip(df["query_lat"], df["query_lon"], strict=True), strict=True):
        hit = key_to_ij.get(k)
        if hit is None:
            # ULP-shaved straggler: nearest lattice point within tolerance.
            pos = int(np.argmin(np.abs(lat_arr - lat) + np.abs(lon_arr - lon)))
            if max(abs(lat_arr[pos] - lat), abs(lon_arr[pos] - lon)) > 1e-7:
                raise ValueError(
                    f"Snapshot row ({lat!r}, {lon!r}) has no lattice match — "
                    "snapshot and lattice definition disagree (wrong area/city?)"
                )
            hit = (int(lattice["i"].iloc[pos]), int(lattice["j"].iloc[pos]))
            k = quant_key(lat_arr[pos], lon_arr[pos])
        if k in matched_keys:
            raise ValueError("Snapshot contains duplicate query points")
        matched_keys.add(k)
        ij.append(hit)
    missing = len(key_to_ij) - len(ij)
    if missing:
        raise ValueError(
            f"{missing} lattice points missing from the snapshot — collection "
            "incomplete? Rerun grid_density_collect.py (it resumes automatically)."
        )
    out = df.copy()
    out["i"] = [p[0] for p in ij]
    out["j"] = [p[1] for p in ij]
    return out


def variant_masks(i: pd.Series, j: pd.Series, substep: int = 4) -> dict[str, np.ndarray]:
    """
    Boolean masks selecting each derived grid variant from 5 m-lattice indices.

    step20 ⊂ step10 ⊂ step5 by construction. The derived step10 grid can be
    ≤1 row/col smaller than a native int(dim/10) grid (report caveat).
    """
    i = np.asarray(i)
    j = np.asarray(j)
    half = substep // 2
    return {
        "step20": (i % substep == 0) & (j % substep == 0),
        "step10": (i % half == 0) & (j % half == 0),
        "step5": np.ones(len(i), dtype=bool),
    }


def road_clip_mask(
    lattice: pd.DataFrame, edges, clip_dist_m: float = CLIP_DIST_M_DEFAULT
) -> np.ndarray:
    """
    Mask of lattice points within clip_dist_m of any OSM street edge.

    Mirrors street_coverage.py's sjoin_nearest idiom (metric UTM CRS, no
    buffer polygons); sjoin_nearest emits duplicate rows on distance ties, so
    the result is deduped on the left index.

    Args:
        lattice: lattice_frame() output (RangeIndex, lat/lon columns)
        edges: GeoDataFrame from fetch_street_edges (EPSG:4326)
    """
    import geopandas as gpd  # heavy geo stack: keep out of module import path

    if not isinstance(lattice.index, pd.RangeIndex) or lattice.index.start != 0:
        raise ValueError("lattice must have a clean RangeIndex (use lattice_frame())")
    metric_crs = edges.estimate_utm_crs()
    points_m = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(lattice["lon"], lattice["lat"]), crs="EPSG:4326"
    ).to_crs(metric_crs)
    edges_m = edges[["geometry"]].to_crs(metric_crs)
    joined = gpd.sjoin_nearest(points_m, edges_m, how="inner", max_distance=clip_dist_m)
    mask = np.zeros(len(lattice), dtype=bool)
    mask[joined.index.unique()] = True
    return mask


def area_tag(area: StudyArea, step_m: int = 20) -> str:
    """Filename tag: 'full' or e.g. 'tile2000m' (tile edge length in metres)."""
    t = area.tile_halfwidth_i20
    return "full" if t is None else f"tile{2 * t * step_m}m"


def snapshot_csv_path(out_dir: str, area: StudyArea) -> str:
    """The area's 5 m snapshot path (must end .csv.gz — the engine asserts)."""
    return os.path.join(out_dir, f"grid_density_{area.city_id}_{area_tag(area)}_step5.csv.gz")
