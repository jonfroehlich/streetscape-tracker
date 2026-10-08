"""
The 2026-10-08 Belgium screen: per-place Mapillary recent-360° km and
Panoramax picture upper bounds for every Belgian GeoNames place of >= 10,000
people, replayed OFFLINE from the cached tiles.

    python scripts/belgium_screen_collect.py \\
        --mapillary-tiles experiments/mapillary-discovery-383/tiles \\
        --panoramax-tiles experiments/belgium-screen-2026-10-08 \\
        --places experiments/mapillary-discovery-383/cities500.txt \\
        --out docs/experiments/belgium-screen_places.csv

This file makes NO network request. It reads two Mapillary z6 sequence tiles
(``6_{x}_{y}.mvt``) and two Panoramax z6 ``grid`` tiles (``pnx_6_{x}_{y}.mvt``)
that must already be on disk, and refuses if any is missing.

PROVENANCE, stated so nobody mistakes this for the code that ran. The
2026-10-08 pass was a research script kept beside its raw output
(``experiments/belgium-screen-2026-10-08/screen.py``, gitignored with it). It
fetched the Mapillary tiles through the #383 screen's research fetcher (the
uncommitted ancestor of ``mapillary_discovery_collect.TileFetcher``: cached,
paced at a mean 40/min with CV 0.6, stop on the first refusal) and the two
Panoramax tiles with one plain GET each, 2 s apart. Both tiles of each provider
were then in a cache, so this replay reads exactly the bytes that pass read.
It is that script with its decoding moved onto the committed helpers
(``mapillary_discovery_common.decode_sequences`` and ``local_km``,
``panoramax_screen.hexes_from_tile`` and ``merge_hexes``), and on the 2026-10-08
cache it reproduces the research script's ``belgium_places.csv`` byte for byte.

THE SIGNALS are screening signals, never coverage:

* Mapillary: the length of every 360° sequence captured on or after
  ``CUTOFF`` within ``MLY_R_KM`` of the place's GeoNames point, per km² of
  that disc, plus the share of that length from its largest creator. z6
  geometry is simplified, so lengths are approximate.
* Panoramax: the summed picture counts of the H3 cells (measured resolution 7,
  ~5.2 km²) whose centres lie within ``PNX_R_KM``. A cell is counted whole, so
  these are UPPER BOUNDS on what lies near the place, and neighbouring places
  share cells.

The research script's segmentation is kept, not ``split_samples``': each
simplified segment is ONE sample at its midpoint, weighted by its full length.
That is coarser than the #383 analysis (0.5 km pieces) and is what the record
measured, so it is what this replay computes.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.mapillary_discovery_common import decode_sequences, local_km
from streetscape_metadata_tracker import panoramax_screen as ps
from streetscape_metadata_tracker.download_common import tiles_for_bbox

ZOOM = 6
# (min_lon, min_lat, max_lon, max_lat): all of Belgium. Two z6 tiles.
BBOX = (2.5, 49.45, 6.45, 51.55)
CUTOFF = "2024-10-08"  # "recent": captured within two years of the screen
MLY_R_KM = 2.0
PNX_R_KM = 3.0  # cell centres within 3 km ~ cells touching the 2 km disc
COUNTRY = "BE"
MIN_POPULATION = 10_000

PLACES_COLUMNS = [
    "place",
    "pop",
    "lat",
    "lon",
    "mly_recent360_km",
    "mly_km_per_km2",
    "mly_top_creator_share",
    "pnx_hexes",
    "pnx_pics_ub",
    "pnx_360_ub",
]


def _require(path: Path) -> bytes:
    if not path.exists():
        raise SystemExit(f"{path} is not cached; this replay makes no network request")
    return path.read_bytes()


def mapillary_segments(tile_dir: Path, tiles, cutoff_ms: int) -> pd.DataFrame:
    """One row per simplified segment of a recent 360° sequence, at its midpoint."""
    seg = []
    for x, y in tiles:
        raw = _require(tile_dir / f"{ZOOM}_{x}_{y}.mvt")
        for s in decode_sequences(raw, x, y, ZOOM):
            if not s.get("is_pano") or (s.get("captured_at") or 0) < cutoff_ms:
                continue
            p = s["_pts"]
            for k in range(len(p) - 1):
                (a0, b0), (a1, b1) = p[k], p[k + 1]
                seg.append(
                    (
                        s.get("id"),
                        (a0 + a1) / 2,
                        (b0 + b1) / 2,
                        local_km(a0, b0, a1, b1),
                        s.get("creator_id"),
                    )
                )
    df = pd.DataFrame(seg, columns=["seq", "lon", "lat", "km", "creator"])
    return df.drop_duplicates(["seq", "lon", "lat"])


def panoramax_cells(tile_dir: Path, tiles) -> pd.DataFrame:
    """One row per merged H3 cell of the cached z6 grid tiles."""
    hexes: dict = {}
    for x, y in tiles:
        raw = _require(tile_dir / f"pnx_{ZOOM}_{x}_{y}.mvt")
        hexes = ps.merge_hexes(hexes, ps.hexes_from_tile(raw, x, y, ZOOM))
    return pd.DataFrame(
        [
            {
                "lon": (h["min_lon"] + h["max_lon"]) / 2,
                "lat": (h["min_lat"] + h["max_lat"]) / 2,
                "pics": h["nb_pictures"],
                "pano": h["nb_360_pictures"],
            }
            for h in hexes.values()
        ]
    )


def load_places(path: Path) -> pd.DataFrame:
    """Belgian GeoNames places of >= MIN_POPULATION, from a cities500.txt dump."""
    pl = pd.read_csv(
        path,
        sep="\t",
        header=None,
        usecols=[1, 4, 5, 8, 10, 14],
        names=["name", "lat", "lon", "cc", "admin1", "pop"],
    )
    return pl[(pl.cc == COUNTRY) & (pl["pop"] >= MIN_POPULATION)]


def _near(df: pd.DataFrame, p, r_km: float) -> pd.DataFrame:
    d = np.hypot(
        (df.lon - p.lon) * 111.32 * math.cos(math.radians(p.lat)), (df.lat - p.lat) * 110.57
    )
    return df[d <= r_km]


def place_rows(seg: pd.DataFrame, cells: pd.DataFrame, places: pd.DataFrame) -> pd.DataFrame:
    """The per-place record, sorted by descending Mapillary density."""
    rows = []
    for p in places.itertuples():
        s = _near(seg, p, MLY_R_KM)
        h = _near(cells, p, PNX_R_KM)
        top = s.groupby("creator").km.sum().sort_values(ascending=False)
        rows.append(
            {
                "place": p.name,
                "pop": p.pop,
                "lat": p.lat,
                "lon": p.lon,
                "mly_recent360_km": round(s.km.sum(), 1),
                "mly_km_per_km2": round(s.km.sum() / (math.pi * MLY_R_KM**2), 2),
                "mly_top_creator_share": round(top.iloc[0] / top.sum(), 2) if len(top) else None,
                "pnx_hexes": len(h),
                "pnx_pics_ub": int(h.pics.sum()),
                "pnx_360_ub": int(h.pano.sum()),
            }
        )
    return pd.DataFrame(rows, columns=PLACES_COLUMNS).sort_values("mly_km_per_km2", ascending=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mapillary-tiles", type=Path, required=True)
    ap.add_argument("--panoramax-tiles", type=Path, required=True)
    ap.add_argument("--places", type=Path, required=True, help="GeoNames cities500.txt")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    tiles = tiles_for_bbox(*BBOX, ZOOM)
    cutoff_ms = pd.Timestamp(CUTOFF).value // 10**6
    seg = mapillary_segments(args.mapillary_tiles, tiles, cutoff_ms)
    cells = panoramax_cells(args.panoramax_tiles, tiles)
    out = place_rows(seg, cells, load_places(args.places))
    out.to_csv(args.out, index=False)
    print(
        f"z{ZOOM} tiles {tiles}; recent-360 km {seg.km.sum():.0f}; "
        f"Panoramax cells {len(cells)}; {len(out)} places -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
