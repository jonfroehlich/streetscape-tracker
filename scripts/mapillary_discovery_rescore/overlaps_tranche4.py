"""
Registration geometry facts for the tranche-4 manifest, from committed files
plus a read-only production snapshot. No network.

    python scripts/mapillary_discovery_rescore/overlaps_tranche4.py SNAPSHOT_CSV OUT_JSON

The same four facts per row as ``overlaps.py`` writes for tranche 3 (see its
docstring), on each row's vetted grid. The catalog is the 2026-11-05 snapshot
PLUS tranche 3's 25 registration grids from the committed
``mapillary-discovery-screen_tranche3_overlaps.json``, because that snapshot
was exported before tranche 3 was registered and two tranche-3 towns (Delavan,
Pell Lake) are the nearest neighbours of this tranche's Walworth County rows.

Not in that catalog: any city registered after 2026-11-05 other than tranche 3
(two were on 2026-11-19, neither in Wisconsin or Minnesota). The prod dry run
of ``register_frame.py`` is the authoritative check; this record is what the
runbook's ``--overlap-km`` was chosen from.
"""

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.mapillary_discovery_rescore.overlaps import (  # noqa: E402
    STEP_M,
    overlap_share,
    rect,
)
from scripts.register_frame import _haversine_km  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXP = os.path.join(ROOT, "docs", "experiments", "mapillary-discovery-screen_")


def load_catalog(snapshot):
    """(city_id, lat, lon, width_m, height_m) for the snapshot plus tranche 3."""
    with open(snapshot, encoding="utf-8") as f:
        catalog = [
            (
                r["city_id"],
                float(r["lat"]),
                float(r["lon"]),
                float(r["grid_width_m"]),
                float(r["grid_height_m"]),
            )
            for r in csv.DictReader(f)
            if r["table"] == "cities"
        ]
    with open(EXP + "tranche3_overlaps.json", encoding="utf-8") as f:
        for r in json.load(f)["rows"]:
            catalog.append((f"tranche3:{r['city']}", *r["grid"]))
    return catalog


def main(snapshot, out):
    with open(os.path.join(ROOT, "mapillary_discovery_cities_tranche4.csv"), encoding="utf-8") as f:
        manifest = list(csv.DictReader(f))
    vetted = {}
    for name in ("vet", "vet_retry"):
        with open(EXP + "tranche4_" + name + ".csv", encoding="utf-8") as f:
            vetted.update({r["geonameid"]: r for r in csv.DictReader(f) if r["width_m"]})
    catalog = load_catalog(snapshot)

    rows, earlier = [], []
    for m in manifest:
        v = vetted[m["geonameid"]]
        clat, clon = float(v["center_lat"]), float(v["center_lon"])
        w, h = int(v["width_m"]), int(v["height_m"])
        lat, lon = float(m["lat"]), float(m["lon"])
        mine = rect(clat, clon, w, h)
        overlaps = {}
        for cid, *grid in catalog:
            share = overlap_share(mine, rect(*grid))
            if share >= 0.001:
                overlaps[cid] = round(share, 3)
        near = min(catalog, key=lambda c: _haversine_km(lat, lon, c[1], c[2]))
        row = dict(
            geonameid=m["geonameid"],
            city=m["city"],
            grid=[round(clat, 6), round(clon, 6), w, h],
            catalog_overlaps=dict(sorted(overlaps.items(), key=lambda kv: -kv[1])),
            nearest_catalog=near[0],
            nearest_catalog_km=round(_haversine_km(lat, lon, near[1], near[2]), 3),
            gsv_lattice_points=(int(w / STEP_M) + 1) * (int(h / STEP_M) + 1),
        )
        if earlier:
            name, km = min(
                ((e[0], _haversine_km(lat, lon, e[1], e[2])) for e in earlier), key=lambda x: x[1]
            )
            row.update(nearest_earlier_row=name, nearest_earlier_row_km=round(km, 3))
        rows.append(row)
        earlier.append((m["city"], clat, clon))

    out_doc = {
        "_about": {
            "generated_by": "scripts/mapillary_discovery_rescore/overlaps_tranche4.py",
            "catalog_snapshot": (
                "production, exported read-only 2026-11-05, plus tranche 3's "
                "registration grids from mapillary-discovery-screen_tranche3_overlaps.json"
            ),
        },
        "rows": rows,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
