#!/usr/bin/env python3
"""
Registration geometry facts for the first European tranche's manifest, from
committed files plus a read-only production snapshot. No network.

    python scripts/mapillary_discovery_europe1_overlaps.py SNAPSHOT_CSV SNAPSHOT_LABEL OUT_JSON

Per manifest row, on its vetted grid (the ``_vet`` CSV, a ``_vet_retry`` row
replacing its first attempt): the share of that grid lying inside each catalog
grid it overlaps, the nearest catalog centre to its GeoNames point, the nearest
EARLIER manifest row's vetted centre (what ``register_frame.py --overlap-km``
compares a later row against once the earlier one is registered), and the GSV
lattice size.

The catalog is the snapshot ``collect catalog-snapshot`` exported, disabled
cities included. A city registered after it is not in this record; the prod dry
run of ``register_frame.py`` is the authoritative check, and this record is what
the runbook's ``--overlap-km`` was chosen from.
"""

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mapillary_discovery_rescore.overlaps import (  # noqa: E402
    STEP_M,
    overlap_share,
    rect,
)
from scripts.register_frame import _haversine_km  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXP = os.path.join(ROOT, "docs", "experiments", "mapillary-europe-screen_tranche1_")
MANIFEST = os.path.join(ROOT, "mapillary_discovery_cities_europe1.csv")


def load_catalog(snapshot):
    """(city_id, lat, lon, width_m, height_m) for every city in the snapshot."""
    with open(snapshot, encoding="utf-8") as f:
        return [
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


def load_vetted():
    """geonameid -> vetted row, a retry replacing the first attempt."""
    vetted = {}
    for name in ("vet", "vet_retry"):
        with open(EXP + name + ".csv", encoding="utf-8") as f:
            vetted.update({r["geonameid"]: r for r in csv.DictReader(f) if r["width_m"]})
    return vetted


def main(snapshot, label, out):
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = list(csv.DictReader(f))
    vetted = load_vetted()
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
            "generated_by": "scripts/mapillary_discovery_europe1_overlaps.py",
            "catalog_snapshot": label,
            "catalog_cities": len(catalog),
        },
        "rows": rows,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, indent=1, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
