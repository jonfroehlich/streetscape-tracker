"""Laptop only: municipal polygon (Nominatim) + drive streets (Overpass) for three towns. Raw JSON cached; a cached town makes no request."""

import json
import os
import sys
import time

import requests

K = sys.argv[1]
sys.path.insert(0, ".")
from streetscape_metadata_tracker.download_common import streetscape_user_agent

UA = streetscape_user_agent("requests/" + requests.__version__)
T = {
    "kuna": ("Kuna, Idaho, United States", 43.492, -116.420),
    "magna": ("Magna, Utah, United States", 40.709, -112.102),
    "carmel": ("Carmel-by-the-Sea, California, United States", 36.555, -121.923),
}
for k, (q, la, lo) in T.items():
    pf = f"{K}/{k}_poly.json"
    if not os.path.exists(pf):
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "polygon_geojson": 1, "limit": 1},
            headers={"User-Agent": UA},
            timeout=60,
        )
        r.raise_for_status()
        j = r.json()
        json.dump(j, open(pf, "w"))
        time.sleep(1.2)
    j = json.load(open(pf))[0]
    print(
        k,
        j["category"],
        j["type"],
        j["osm_type"],
        j["osm_id"],
        j["display_name"][:70],
        j["boundingbox"],
        j["geojson"]["type"],
    )
    s, n, w, e = [float(x) for x in j["boundingbox"]]
    m = 0.02
    of = f"{K}/{k}_osm.json"
    if not os.path.exists(of):
        q2 = f'[out:json][timeout:90];way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"]({s - m},{w - m},{n + m},{e + m});out tags geom;'
        r = requests.post(
            "https://overpass.kumi.systems/api/interpreter",
            data={"data": q2},
            headers={"User-Agent": UA},
            timeout=150,
        )
        print(" overpass", r.status_code, len(r.content))
        if r.status_code != 200:
            sys.exit(f"stop at first non-200: {r.status_code}")
        open(of, "wb").write(r.content)
        time.sleep(35)
    print(" ways", len(json.load(open(of))["elements"]))
