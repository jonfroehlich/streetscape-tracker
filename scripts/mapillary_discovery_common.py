"""
Shared pieces of the Mapillary discovery screen (issue #383): find places with
fresh, systematic 360-degree capture whether or not they are in the catalog.

The instrument is Mapillary's coverage vector tiles, ``sequence`` layer, at
z6 -- one LineString per capture sequence carrying ``captured_at``,
``creator_id``, ``is_pano``, ``foot``, ``image_id``, ``quality_score`` and
(on 20.8% of sequences in the 2026-10-02 scan) ``organization_id``. About 100 z6 tiles cover North
America; one dense tile is 5-10 MB and up to ~190,000 sequences.

Everything here is a SCREENING SIGNAL, never coverage. z6 geometry is
simplified, so lengths are approximate, and a sequence's length says nothing
about which streets it covered. A road walk is the only measurement of street
coverage; this screen only decides which places are worth walking.

Pipeline:
    collect  tiles -> one row per sequence (vertices kept)  [mapillary_discovery_collect.py]
    analyze  recent 360 sequences -> ~0.5 km samples -> per-place score
             [mapillary_discovery_analyze.py]

The per-place score is ``recent-360 km within R_KM of the place's GeoNames
point, per km^2 of that disc``. Clusters of connected cells were tried first
and rejected: one statewide sweep merged all of Connecticut into a single
10,687 km cluster, so towns have to be scored from their own point.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable

import mapbox_vector_tile
import numpy as np
import pandas as pd

from streetscape_metadata_tracker.download_common import tile_frac_to_lonlat, tiles_for_bbox

TILE_URL_TEMPLATE = "https://tiles.mapillary.com/maps/vtp/mly1_computed_public/2/{z}/{x}/{y}"
SEQUENCE_LAYER = "sequence"
SCREEN_ZOOM = 6  # the sequence layer's coarsest zoom (docs: z6-14)

# (min_lon, min_lat, max_lon, max_lat). The 2026-10-02 run used the first four.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "conus_scanada": (-125.0, 24.5, -66.5, 50.5),
    "se_alaska": (-136.0, 55.0, -130.0, 59.5),
    "anchorage_fairbanks": (-151.5, 60.5, -146.5, 65.5),
    "hawaii": (-160.5, 18.8, -154.7, 22.4),
}
# Regions added after the 2026-10-02 run. They are NOT part of ``REGIONS`` on
# purpose: that dict is the pinned 97-tile frame the committed US/Canada numbers
# trace to, and it is ``scan``'s default. Iceland, the Canaries, Cyprus and
# everything east of 32 E are outside "europe" and left for a later pass.
EXTRA_REGIONS: dict[str, tuple[float, float, float, float]] = {
    "europe": (-11.0, 34.5, 32.0, 71.5),
}
ALL_REGIONS: dict[str, tuple[float, float, float, float]] = {**REGIONS, **EXTRA_REGIONS}

# Pacing, as the production Mapillary channels run (#292): mean 40/min, gaps
# drawn as mean * ((1 - j) + j * Exp(1)) so the mean rate is unchanged and
# there is a floor but no ceiling.
MEAN_GAP_S = 60 / 40
JITTER = 0.6

SAMPLE_KM = 0.5  # a simplified segment is split into pieces no longer than this
R_KM = 2.0  # radius of the disc a place is scored over

# The #441 re-score ("V4"), as scripts/mapillary_discovery_rescore/features.py
# ran it on 2026-11-05. Those scripts are a frozen record; these constants and
# ``place_features`` are the same maths, importable and tested, with ONE
# difference: an undated sample. The record's ``np.minimum.at`` lets a NaN age
# poison its cell (a cell holding dated and undated imagery scored 0) and its
# newest age; here a NaN never wins (``np.fmin``), so the cell scores by its
# dated imagery. Neither scan holds an undated 360 sample, so no recorded
# number depends on it.
FEATURE_STEP_KM = 0.2  # sample spacing along a sequence
FEATURE_CELL_KM = 0.25  # side of the cells whose occupancy is the score
FEATURE_DISCS_KM = (1, 2, 3)  # V4 is the best of these discs
FEATURE_REACH_KM = 3.0  # latitude window searched around a place (the largest disc)
YEAR_MS = 365.25 * 86400e3


def jittered_gap(rng: random.Random, mean_s: float = MEAN_GAP_S, jitter: float = JITTER) -> float:
    """One inter-request gap, in seconds."""
    return mean_s * ((1 - jitter) + jitter * rng.expovariate(1.0))


def region_tiles(names: Iterable[str], zoom: int = SCREEN_ZOOM) -> list[tuple[int, int]]:
    """The sorted, de-duplicated (x, y) tiles covering the named regions."""
    return sorted({t for n in names for t in tiles_for_bbox(*ALL_REGIONS[n], zoom)})


def local_km(lon0: float, lat0: float, lon1: float, lat1: float) -> float:
    """Equirectangular distance in km -- accurate at the sub-10 km scale used here."""
    c = math.cos(math.radians((lat0 + lat1) / 2))
    return math.hypot((lon1 - lon0) * 111.32 * c, (lat1 - lat0) * 110.57)


def decode_sequences(raw: bytes, x: int, y: int, zoom: int) -> list[dict]:
    """
    Decode one tile's ``sequence`` layer into dicts of the tile properties plus
    ``_pts``, a list of (lon, lat) vertices.

    A MultiLineString's parts are concatenated. That can bridge a gap between
    parts with one spurious segment; at z6 the parts are tile-clipped pieces of
    one sequence, so the error is a few segments in millions and is accepted.
    """
    if not raw:
        return []
    layer = mapbox_vector_tile.decode(raw).get(SEQUENCE_LAYER)
    if not layer:
        return []
    extent = layer.get("extent", 4096)
    out = []
    for feature in layer["features"]:
        geom = feature["geometry"]
        if geom["type"] == "LineString":
            lines = [geom["coordinates"]]
        elif geom["type"] == "MultiLineString":
            lines = geom["coordinates"]
        else:
            continue
        # decode() returns y-up tile-local coordinates, as in download_mapillary
        pts = [
            tile_frac_to_lonlat(x + px / extent, y + (1 - py / extent), zoom)
            for line in lines
            for px, py in line
        ]
        out.append({**feature.get("properties", {}), "_pts": pts})
    return out


def split_samples(pts: list[tuple[float, float]], sample_km: float = SAMPLE_KM):
    """
    Yield (lon, lat, km) samples along a polyline: each segment is cut into
    ``ceil(len / sample_km)`` equal pieces, placed at the piece midpoints and
    weighted by piece length. The weights sum to the polyline's length exactly,
    which is the invariant a place score depends on.
    """
    for (a0, b0), (a1, b1) in zip(pts, pts[1:], strict=False):
        d = local_km(a0, b0, a1, b1)
        if d == 0:
            continue
        n = max(1, math.ceil(d / sample_km))
        for j in range(n):
            f = (j + 0.5) / n
            yield a0 + (a1 - a0) * f, b0 + (b1 - b0) * f, d / n


def place_scores(
    samples: pd.DataFrame,
    places: pd.DataFrame,
    r_km: float = R_KM,
    min_km: float = 5.0,
) -> pd.DataFrame:
    """
    Score each place by the recent-360 sample length within ``r_km`` of it.

    ``samples`` needs lon, lat, km, creator, captured (epoch ms), foot;
    ``places`` needs name, lat, lon plus any columns to carry through.
    Returns one row per place holding at least ``min_km``, with the score
    ``km_per_km2`` = km / (pi r^2), the top creator and its length share, the
    length-weighted median capture date, and the on-foot length share.
    """
    s = samples.sort_values("lat").reset_index(drop=True)
    slat, slon, km = s.lat.to_numpy(), s.lon.to_numpy(), s.km.to_numpy()
    cre, cap, foot = s.creator.to_numpy(), s.captured.to_numpy(), s.foot.to_numpy()
    dlat = r_km / 110.57
    area = math.pi * r_km**2
    rows = []
    for p in places.itertuples(index=False):
        a, b = np.searchsorted(slat, [p.lat - dlat, p.lat + dlat])
        if a == b:
            continue
        c = math.cos(math.radians(p.lat))
        d = np.hypot((slon[a:b] - p.lon) * 111.32 * c, (slat[a:b] - p.lat) * 110.57)
        h = np.nonzero(d <= r_km)[0] + a
        tot = float(km[h].sum())
        if len(h) == 0 or tot < min_km:
            continue
        by = pd.Series(km[h]).groupby(cre[h]).sum().sort_values(ascending=False)
        order = np.argsort(cap[h], kind="stable")
        cum = np.cumsum(km[h][order])
        median_ms = int(cap[h][order][np.searchsorted(cum, tot / 2)])
        rows.append(
            {
                **p._asdict(),
                "km_in_disc": round(tot, 2),
                "km_per_km2": round(tot / area, 3),
                "top_creator": int(by.index[0]),
                "top_share": round(float(by.iloc[0]) / tot, 3),
                "n_creators": int(len(by)),
                "foot_share": round(float(km[h][foot[h] == True].sum()) / tot, 3),  # noqa: E712
                "median_captured": pd.Timestamp(median_ms, unit="ms").date().isoformat(),
                "newest_captured": pd.Timestamp(int(cap[h].max()), unit="ms").date().isoformat(),
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values("km_per_km2", ascending=False).reset_index(drop=True) if rows else out


def nearest_km(lat: float, lon: float, lats: np.ndarray, lons: np.ndarray) -> tuple[int, float]:
    """Index and great-circle km of the nearest of (lats, lons) to (lat, lon)."""
    la, lo = math.radians(lat), math.radians(lon)
    rl, ro = np.radians(lats), np.radians(lons)
    cosd = np.sin(la) * np.sin(rl) + np.cos(la) * np.cos(rl) * np.cos(ro - lo)
    d = 6371.0 * np.arccos(np.clip(cosd, -1.0, 1.0))
    i = int(d.argmin())
    return i, float(d[i])


def thin_by_distance(df: pd.DataFrame, min_km: float) -> pd.DataFrame:
    """
    Keep rows in order, dropping any within ``min_km`` of an already-kept row.
    Neighbouring GeoNames points (a town and its CDPs) share one sweep's
    samples, so without this one sweep fills several slots of a ranked list.
    """
    kept: list[int] = []
    for i, r in enumerate(df.itertuples(index=False)):
        if all(local_km(r.lon, r.lat, df.lon.iat[k], df.lat.iat[k]) >= min_km for k in kept):
            kept.append(i)
    return df.iloc[kept].reset_index(drop=True)


def age_weight(age_yr):
    """
    The V4 weight of imagery ``age_yr`` years old at the scan: 1 up to 2 years,
    0.75 up to 3, 0.25 up to 5, 0 beyond (and 0 for an unknown age).

    >>> age_weight(np.array([0.5, 2.0, 2.5, 4.0, 6.0])).tolist()
    [1.0, 1.0, 0.75, 0.25, 0.0]
    """
    a = np.asarray(age_yr, dtype=float)
    return np.select([a <= 2, a <= 3, a <= 5], [1.0, 0.75, 0.25], 0.0)


def sample_polyline(pts, step_km: float = FEATURE_STEP_KM):
    """
    Vectorized ``split_samples``: cut each segment of a (lon, lat) polyline
    into ``ceil(len / step_km)`` equal pieces and return three arrays, the
    piece midpoints' lon and lat and each piece's length in km. The lengths
    sum to the polyline's length; zero-length segments yield nothing.
    """
    a = np.asarray(pts, dtype=float).reshape(-1, 2)
    empty = np.empty(0)
    if len(a) < 2:
        return empty, empty, empty
    d = a[1:] - a[:-1]
    c = np.cos(np.radians((a[1:, 1] + a[:-1, 1]) / 2))
    length = np.hypot(d[:, 0] * 111.32 * c, d[:, 1] * 110.57)
    m = length > 0
    if not m.any():
        return empty, empty, empty
    n = np.maximum(1, np.ceil(length[m] / step_km)).astype(int)
    seg = np.repeat(np.arange(int(m.sum())), n)
    j = np.concatenate([np.arange(k) for k in n])
    f = (j + 0.5) / n[seg]
    a0, dm = a[:-1][m], d[m]
    return a0[seg, 0] + dm[seg, 0] * f, a0[seg, 1] + dm[seg, 1] * f, length[m][seg] / n[seg]


class PanoSamples:
    """
    Every 360-degree sample of a scan, sorted by latitude so a place's
    neighbourhood is one ``searchsorted`` slice. ``age`` is in years before the
    scan date (NaN when the sequence carries no capture time), ``creator`` is
    the uploader id (-1 when absent) and ``foot`` the on-foot flag.
    """

    def __init__(self, lon, lat, km, age, creator, foot):
        o = np.argsort(lat, kind="stable")
        self.lon = np.asarray(lon, dtype=float)[o]
        self.lat = np.asarray(lat, dtype=float)[o]
        self.km = np.asarray(km, dtype=float)[o]
        self.age = np.asarray(age, dtype=float)[o]
        self.creator = np.asarray(creator, dtype=np.int64)[o]
        self.foot = np.asarray(foot, dtype=bool)[o]

    def __len__(self) -> int:
        return len(self.lat)


def place_features(s: PanoSamples, plat: float, plon: float) -> dict:
    """
    The #441 re-score's features for one point, from the 360-degree samples
    around it. Keys:

    - ``v0``: km of imagery at most 2 years old within 2 km, per km^2 (the
      original #383 score); ``v1``: the same length, age-weighted.
    - ``occ{r}_recent`` / ``occ{r}_aw`` for r in 1, 2, 3 km: of the 250 m cells
      a disc of radius r could hold, the share whose NEWEST imagery is at most
      2 years old, and that share with each cell weighted by ``age_weight``.
      Not a true fraction: cells are counted against the disc's area, so it can
      exceed 1.
    - ``v4``: the largest ``occ{r}_aw``, the score the tranche-3 prediction used.
    - within 2 km: ``top`` (the uploader with the most age-weighted length, -1
      if none), ``top_share``, ``n_cre``, ``foot_share``, ``med_age`` (the
      length-weighted median age), ``newest_age`` and ``km2`` (total length).
    """
    out = {"v0": 0.0, "v1": 0.0}
    for r in FEATURE_DISCS_KM:
        out[f"occ{r}_recent"] = 0.0
        out[f"occ{r}_aw"] = 0.0
    out.update(
        v4=0.0,
        top=-1,
        top_share=np.nan,
        foot_share=np.nan,
        med_age=np.nan,
        newest_age=np.nan,
        km2=0.0,
        n_cre=0,
    )
    dl = FEATURE_REACH_KM / 110.57
    a, b = np.searchsorted(s.lat, [plat - dl, plat + dl])
    if a == b:
        return out
    c = math.cos(math.radians(plat))
    x = (s.lon[a:b] - plon) * 111.32 * c
    y = (s.lat[a:b] - plat) * 110.57
    d = np.hypot(x, y)
    age, km = s.age[a:b], s.km[a:b]
    w = age_weight(age)
    m2 = d <= 2
    disc2 = math.pi * 4
    out["v0"] = float(km[m2 & (age <= 2)].sum() / disc2)
    out["v1"] = float((km[m2] * w[m2]).sum() / disc2)
    cell = np.floor(x / FEATURE_CELL_KM).astype(np.int64) * 100000 + np.floor(
        y / FEATURE_CELL_KM
    ).astype(np.int64)
    for r in FEATURE_DISCS_KM:
        m = d <= r
        if not m.any():
            continue
        cells_in_disc = math.pi * r * r / (FEATURE_CELL_KM * FEATURE_CELL_KM)
        uniq, inv = np.unique(cell[m], return_inverse=True)
        newest = np.full(len(uniq), 99.0)
        # NaN ages never win a minimum, so a cell holding only undated imagery
        # keeps the 99-year sentinel and weighs 0
        np.fmin.at(newest, inv, age[m])
        out[f"occ{r}_recent"] = float((newest <= 2).sum() / cells_in_disc)
        out[f"occ{r}_aw"] = float(age_weight(newest).sum() / cells_in_disc)
    out["v4"] = max(out[f"occ{r}_aw"] for r in FEATURE_DISCS_KM)
    if m2.any():
        tot = float(km[m2].sum())
        out["km2"] = tot
        by = (
            pd.Series(km[m2] * np.maximum(w[m2], 0.01))
            .groupby(s.creator[a:b][m2])
            .sum()
            .sort_values(ascending=False)
        )
        out["top"] = int(by.index[0])
        out["top_share"] = float(by.iloc[0] / by.sum())
        out["n_cre"] = int(len(by))
        out["foot_share"] = float(km[m2][s.foot[a:b][m2]].sum() / tot)
        dated = m2 & ~np.isnan(age)
        if dated.any():
            order = np.argsort(age[dated], kind="stable")
            cum = np.cumsum(km[dated][order])
            out["med_age"] = float(
                age[dated][order][min(np.searchsorted(cum, cum[-1] / 2), len(cum) - 1)]
            )
            out["newest_age"] = float(age[dated].min())
    return out
