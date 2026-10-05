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

# Pacing, as the production Mapillary channels run (#292): mean 40/min, gaps
# drawn as mean * ((1 - j) + j * Exp(1)) so the mean rate is unchanged and
# there is a floor but no ceiling.
MEAN_GAP_S = 60 / 40
JITTER = 0.6

SAMPLE_KM = 0.5  # a simplified segment is split into pieces no longer than this
R_KM = 2.0  # radius of the disc a place is scored over


def jittered_gap(rng: random.Random, mean_s: float = MEAN_GAP_S, jitter: float = JITTER) -> float:
    """One inter-request gap, in seconds."""
    return mean_s * ((1 - jitter) + jitter * rng.expovariate(1.0))


def region_tiles(names: Iterable[str], zoom: int = SCREEN_ZOOM) -> list[tuple[int, int]]:
    """The sorted, de-duplicated (x, y) tiles covering the named REGIONS."""
    return sorted({t for n in names for t in tiles_for_bbox(*REGIONS[n], zoom)})


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
