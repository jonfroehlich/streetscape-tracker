"""
Registration geometry facts for the tranche-3 manifest (#441), from committed
files plus a read-only production snapshot. No network.

    python scripts/mapillary_discovery_rescore/overlaps.py SNAPSHOT_CSV OUT_JSON

For each manifest row, on its registration grid (the vetted grid, or the
street-fitted one for the three in GRID_FITS):

* ``catalog_overlaps``: the share of the row's grid lying inside each catalog
  grid it intersects (the same flat-rectangle formula fit_grids.py uses);
* ``nearest_catalog_km``: register_frame.py's haversine from the row's
  GeoNames point (what ``find_overlap`` measures from) to the nearest catalog
  centre;
* ``nearest_earlier_row_km``: the same, to the GEOCODED (vetted) centre of the
  nearest EARLIER manifest row. register_frame.py appends each newly
  registered city's geocoded centre to the cities it checks later rows
  against, so within one batch this is the reuse margin ``--overlap-km`` sees,
  and the dry run (which registers nothing) cannot see it;
* ``gsv_lattice_points``: the GSV grid-point count of the registration grid
  under the scheduler's lattice formula, (int(W/20)+1) * (int(H/20)+1).
"""

import csv
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.build_mapillary_discovery_tranche3 import GRID_FITS  # noqa: E402
from scripts.register_frame import _haversine_km  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXP = os.path.join(ROOT, "docs", "experiments", "mapillary-discovery-screen_tranche3_")
STEP_M = 20


def rect(lat, lon, w, h):
    cx = 111.32 * math.cos(math.radians(lat))
    return (
        lon - w / 2000 / cx,
        lat - h / 2000 / 110.57,
        lon + w / 2000 / cx,
        lat + h / 2000 / 110.57,
    )


def overlap_share(a, b):
    """Share of rectangle a's area lying inside rectangle b (lon/lat degrees)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return (w * h) / ((a[2] - a[0]) * (a[3] - a[1]))


def main(snapshot, out):
    with open(os.path.join(ROOT, "mapillary_discovery_cities_tranche3.csv"), encoding="utf-8") as f:
        manifest = list(csv.DictReader(f))
    vetted = {}
    for name in ("vet", "vet_retry"):
        with open(EXP + name + ".csv", encoding="utf-8") as f:
            vetted.update({r["geonameid"]: r for r in csv.DictReader(f) if r["width_m"]})
    with open(snapshot, encoding="utf-8") as f:
        catalog = [r for r in csv.DictReader(f) if r["table"] == "cities"]

    rows, earlier = [], []
    for m in manifest:
        gid, v = m["geonameid"], vetted[m["geonameid"]]
        if gid in GRID_FITS:
            clat, clon, w, h = GRID_FITS[gid]
        else:
            clat, clon = float(v["center_lat"]), float(v["center_lon"])
            w, h = int(v["width_m"]), int(v["height_m"])
        lat, lon = float(m["lat"]), float(m["lon"])
        mine = rect(clat, clon, w, h)
        overlaps = {}
        for c in catalog:
            share = overlap_share(
                mine,
                rect(
                    float(c["lat"]),
                    float(c["lon"]),
                    float(c["grid_width_m"]),
                    float(c["grid_height_m"]),
                ),
            )
            if share >= 0.001:
                overlaps[c["city_id"]] = round(share, 3)
        near_cat = min(
            catalog, key=lambda c: _haversine_km(lat, lon, float(c["lat"]), float(c["lon"]))
        )
        row = dict(
            geonameid=gid,
            city=m["city"],
            grid=[round(clat, 6), round(clon, 6), w, h],
            catalog_overlaps=dict(sorted(overlaps.items(), key=lambda kv: -kv[1])),
            nearest_catalog=near_cat["city_id"],
            nearest_catalog_km=round(
                _haversine_km(lat, lon, float(near_cat["lat"]), float(near_cat["lon"])), 3
            ),
            gsv_lattice_points=(int(w / STEP_M) + 1) * (int(h / STEP_M) + 1),
        )
        if earlier:
            name, km = min(
                ((e[0], _haversine_km(lat, lon, e[1], e[2])) for e in earlier), key=lambda x: x[1]
            )
            row.update(nearest_earlier_row=name, nearest_earlier_row_km=round(km, 3))
        earlier.append((m["city"], float(v["center_lat"]), float(v["center_lon"])))
        rows.append(row)

    out_doc = {
        "_about": {
            "generated_by": "scripts/mapillary_discovery_rescore/overlaps.py",
            "catalog_snapshot": "production, exported read-only 2026-11-05",
        },
        "rows": rows,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
