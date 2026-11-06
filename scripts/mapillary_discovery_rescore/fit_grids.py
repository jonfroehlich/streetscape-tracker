"""Fit a town rectangle to where its streets are, inside its municipal polygon. The uploader's sweep is a CHECK, never an input."""

import json
import math
import sys
from collections import deque

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import contains_xy
from shapely.geometry import box, shape

K = sys.argv[1]
SNAP = sys.argv[2]
CELL = 0.2
TH = 0.3
KEEP = 0.02
BUF = 0.25
sys.path.insert(0, ".")
from streetscape_metadata_tracker.download_common import tiles_for_bbox

T = {
    "kuna": ("16001", 43.492, -116.420, 16932, 14704),
    "magna": ("49035", 40.709, -112.102, 16651, 14607),
    "carmel": ("06053", 36.555, -121.923, 9550, 4032),
}
vet = pd.read_csv(K + "/../tranche3_vet.csv")
vn = {"kuna": "Kuna", "magna": "Magna", "carmel": "Carmel-by-the-Sea"}
cat = pd.read_csv(SNAP, low_memory=False)
cat = cat[cat.table == "cities"]
# sweep check samples
q = pd.read_parquet(
    "experiments/mapillary-discovery-383/sequences.parquet",
    columns=["creator_id", "captured_at", "is_pano", "foot", "pts"],
)
q = q[q.is_pano]
SCAN = pd.Timestamp("2026-10-02").value // 10**6
res = {}
for k, (fp, gla, glo, _, _) in T.items():
    j = json.load(open(f"{K}/{k}_poly.json"))[0]
    poly = shape(j["geojson"])
    v = vet[vet.city == vn[k]].iloc[0]
    cla, clo, W, H = v.center_lat, v.center_lon, v.width_m, v.height_m
    w, s, e, n = poly.bounds
    m = 0.02
    r = gpd.read_file(
        f"{K}/tiger/tl_2024_{fp}_roads.zip", bbox=(w - m, s - m, e + m, n + m)
    ).to_crs(4326)
    r = r[r.MTFCC.isin(["S1100", "S1200", "S1400", "S1640"])]
    la0 = (s + n) / 2
    cx = 111.32 * math.cos(math.radians(la0))
    cy = 110.57
    X = []
    Y = []
    L = []
    loc = []
    for g, mt in zip(r.geometry, r.MTFCC):
        for ln in g.geoms if g.geom_type == "MultiLineString" else [g]:
            a = np.asarray(ln.coords)[:, :2]
            d = np.hypot(np.diff(a[:, 0]) * cx, np.diff(a[:, 1]) * cy)
            for i, dd in enumerate(d):
                nn = max(1, math.ceil(dd / 0.025))
                f = (np.arange(nn) + 0.5) / nn
                X.append(a[i, 0] + (a[i + 1, 0] - a[i, 0]) * f)
                Y.append(a[i, 1] + (a[i + 1, 1] - a[i, 1]) * f)
                L.append(np.full(nn, dd / nn))
                loc.append(np.full(nn, mt == "S1400"))
    X, Y, L, loc = map(np.concatenate, (X, Y, L, loc))
    inp = contains_xy(poly, X, Y)
    ix = np.floor((X - w) * cx / CELL).astype(int)
    iy = np.floor((Y - s) * cy / CELL).astype(int)
    df = pd.DataFrame({"ix": ix, "iy": iy, "L": L})[inp & loc]
    c = df.groupby(["ix", "iy"]).L.sum()
    built = set(c[c >= TH].index)
    # connected components, allowing a one-cell gap
    seen = set()
    comps = []
    for b in built:
        if b in seen:
            continue
        Q = deque([b])
        seen.add(b)
        cc = [b]
        while Q:
            a = Q.popleft()
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    t = (a[0] + dx, a[1] + dy)
                    if t in built and t not in seen:
                        seen.add(t)
                        Q.append(t)
                        cc.append(t)
        comps.append(cc)
    tot = sum(c[x] for x in built)
    keep = [max(comps, key=lambda cc: sum(c[x] for x in cc))]
    cells = [x for cc in keep for x in cc]
    # rectangle: cell bounds of kept built cells + buffer
    xs = np.array([x for x, _ in cells])
    ys = np.array([y for _, y in cells])
    x0 = w + (xs.min() * CELL - BUF) / cx
    x1 = w + ((xs.max() + 1) * CELL + BUF) / cx
    y0 = s + (ys.min() * CELL - BUF) / cy
    y1 = s + ((ys.max() + 1) * CELL + BUF) / cy
    PW = (x1 - x0) * cx * 1000
    PH = (y1 - y0) * cy * 1000
    PLA = (y0 + y1) / 2
    PLO = (x0 + x1) / 2
    dW, dH = W / 2000 / cx, H / 2000 / cy
    oldb = (clo - dW, cla - dH, clo + dW, cla + dH)

    def share(b, mask):
        ii = (X >= b[0]) & (X <= b[2]) & (Y >= b[1]) & (Y <= b[3])
        return L[ii & mask].sum() / L[mask].sum()

    newb = (x0, y0, x1, y1)
    pl = inp & loc
    # catalog overlap
    ov = []
    for t in cat.itertuples():
        a, b, gw, gh = float(t.lat), float(t.lon), float(t.grid_width_m), float(t.grid_height_m)
        ccx = 111.32 * math.cos(math.radians(a))
        bb = box(b - gw / 2000 / ccx, a - gh / 2000 / cy, b + gw / 2000 / ccx, a + gh / 2000 / cy)
        rect = box(*newb)
        if rect.intersects(bb):
            ov.append((t.city_id, round(rect.intersection(bb).area / rect.area, 3)))
    # sweep check
    bb = (
        min(x0, oldb[0]) - 0.02,
        min(y0, oldb[1]) - 0.02,
        max(x1, oldb[2]) + 0.02,
        max(y1, oldb[3]) + 0.02,
    )
    sw = []
    for p, ca, cr in zip(q.pts.values, q.captured_at.values, q.creator_id.astype(str).values):
        p0 = p[0]
        a = np.vstack(p)
        if (
            a[:, 0].max() < bb[0]
            or a[:, 0].min() > bb[2]
            or a[:, 1].max() < bb[1]
            or a[:, 1].min() > bb[3]
        ):
            continue
        mx = (a[:-1] + a[1:]) / 2
        d = np.hypot(np.diff(a[:, 0]) * cx, np.diff(a[:, 1]) * cy)
        sw.append(np.c_[mx, d, np.full(len(d), (SCAN - ca) / (365.25 * 86400e3))])
    sw = np.vstack(sw)
    rec = sw[sw[:, 3] <= 2]
    ins = lambda A, b: (A[:, 0] >= b[0]) & (A[:, 0] <= b[2]) & (A[:, 1] >= b[1]) & (A[:, 1] <= b[3])
    # street-km density inside the rectangles
    km_new = L[loc & (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)].sum()
    km_old = L[loc & (X >= oldb[0]) & (X <= oldb[2]) & (Y >= oldb[1]) & (Y <= oldb[3])].sum()
    t_new = len(tiles_for_bbox(x0, y0, x1, y1, 14))
    t_old = len(tiles_for_bbox(*oldb, 14))
    res[k] = dict(
        poly_km2=None,
        old_wh=[int(W), int(H)],
        new_wh=[round(PW), round(PH)],
        new_center=[round(PLA, 5), round(PLO, 5)],
        old_center=[cla, clo],
        comps=len(comps),
        kept=len(keep),
        built_cells=len(built),
        poly_local_km=round(L[pl].sum()),
        local_km_in_old=round(km_old),
        local_km_in_new=round(km_new),
        poly_streets_in_old=round(share(oldb, pl), 3),
        poly_streets_in_new=round(share(newb, pl), 3),
        gsv_old=int(W * H / 400),
        gsv_new=int(PW * PH / 400),
        tiles_old=t_old,
        tiles_new=t_new,
        area_old=round(W * H / 1e6, 1),
        area_new=round(PW * PH / 1e6, 1),
        sweep_recent_km_old=round(rec[ins(rec, oldb), 2].sum()),
        sweep_recent_km_new=round(rec[ins(rec, newb), 2].sum()),
        overlaps=ov,
        geo_pt_in_new=bool(x0 <= glo <= x1 and y0 <= gla <= y1),
    )
    print(k, json.dumps(res[k]))
    json.dump(
        {
            "poly": j["geojson"],
            "old": oldb,
            "new": newb,
            "geo": [glo, gla],
            "rec": rec[:, :2].round(5).tolist(),
            "road_x": X[loc][::4].round(5).tolist(),
            "road_y": Y[loc][::4].round(5).tolist(),
        },
        open(f"{K}/{k}_fit.json", "w"),
    )
json.dump(res, open(K + "/fit_results.json", "w"), indent=1)
