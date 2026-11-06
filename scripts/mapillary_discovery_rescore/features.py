"""Offline: per-place features from the cached 2026-10-02 z6 scan. Zero requests."""

import math
import sys
import time

import numpy as np
import pandas as pd

S = sys.argv[1]
D = "experiments/mapillary-discovery-383/"
SCAN_MS = pd.Timestamp("2026-10-02").value // 10**6
YR = 365.25 * 86400e3
STEP = 0.2
CELL = 0.25
t = time.time()
q = pd.read_parquet(
    D + "sequences.parquet", columns=["creator_id", "captured_at", "is_pano", "foot", "pts"]
)
q = q[q.is_pano]
print("pano rows", len(q), flush=True)
lon = []
lat = []
km = []
ci = []
for i, p in enumerate(q.pts.values):
    a = np.vstack(p)
    d = a[1:] - a[:-1]
    c = np.cos(np.radians((a[1:, 1] + a[:-1, 1]) / 2))
    L = np.hypot(d[:, 0] * 111.32 * c, d[:, 1] * 110.57)
    m = L > 0
    if not m.any():
        continue
    n = np.maximum(1, np.ceil(L[m] / STEP)).astype(int)
    seg = np.repeat(np.arange(m.sum()), n)
    j = np.concatenate([np.arange(k) for k in n])
    f = (j + 0.5) / n[seg]
    a0 = a[:-1][m]
    dm = d[m]
    lon.append(a0[seg, 0] + dm[seg, 0] * f)
    lat.append(a0[seg, 1] + dm[seg, 1] * f)
    km.append(L[m][seg] / n[seg])
    ci.append(np.full(len(seg), i, dtype=np.int32))
lon, lat, km, ci = map(np.concatenate, (lon, lat, km, ci))
cap = q.captured_at.values[ci]
cre = q.creator_id.astype(str).values[ci]
ft = q.foot.fillna(False).astype(bool).values[ci]
age = (SCAN_MS - cap) / YR
o = np.argsort(lat)
lon, lat, km, age, cre, ft = lon[o], lat[o], km[o], age[o], cre[o], ft[o]
print("samples", len(lat), "total km", round(km.sum()), round(time.time() - t), flush=True)


def w_age(a):
    return np.select([a <= 2, a <= 3, a <= 5], [1, 0.75, 0.25], 0.0)


def feats(plat, plon):
    out = {}
    c = math.cos(math.radians(plat))
    dl = 3.0 / 110.57
    a, b = np.searchsorted(lat, [plat - dl, plat + dl])
    for k in ["v0", "v1"]:
        out[k] = 0.0
    for r in (1, 2, 3):
        out[f"occ{r}_recent"] = 0.0
        out[f"occ{r}_aw"] = 0.0
    out.update(
        top="",
        top_share=np.nan,
        foot_share=np.nan,
        med_age=np.nan,
        newest_age=np.nan,
        km2=0.0,
        n_cre=0,
    )
    if a == b:
        return out
    x = (lon[a:b] - plon) * 111.32 * c
    y = (lat[a:b] - plat) * 110.57
    d = np.hypot(x, y)
    A = age[a:b]
    K = km[a:b]
    W = w_age(A)
    m2 = d <= 2
    # V0: the committed score: recent length in 2 km (foot included)
    out["v0"] = K[m2 & (A <= 2)].sum() / (math.pi * 4)  # scan-relative <=2.0y ~= since 2024-10-02
    out["v1"] = (K[m2] * W[m2]).sum() / (math.pi * 4)
    cx = np.floor(x / CELL).astype(np.int64)
    cy = np.floor(y / CELL).astype(np.int64)
    cid = cx * 100000 + cy
    for r in (1, 2, 3):
        m = d <= r
        area = math.pi * r * r / (CELL * CELL)
        if not m.any():
            continue
        uc, inv = np.unique(cid[m], return_inverse=True)
        best = np.full(len(uc), 99.0)
        np.minimum.at(best, inv, A[m])  # newest imagery per cell
        out[f"occ{r}_recent"] = (best <= 2).sum() / area
        out[f"occ{r}_aw"] = w_age(best).sum() / area
    if m2.any():
        tot = K[m2].sum()
        out["km2"] = tot
        wk = K[m2] * np.maximum(W[m2], 0.01)
        s = pd.Series(wk).groupby(cre[a:b][m2]).sum().sort_values(ascending=False)
        out["top"] = s.index[0]
        out["top_share"] = s.iloc[0] / s.sum()
        out["n_cre"] = len(s)
        out["foot_share"] = K[m2][ft[a:b][m2]].sum() / tot
        od = np.argsort(A[m2])
        cum = np.cumsum(K[m2][od])
        out["med_age"] = A[m2][od][np.searchsorted(cum, tot / 2)]
        out["newest_age"] = A[m2].min()
    return out


snap = pd.read_csv(sys.argv[2], low_memory=False)
cat = snap[snap.table == "cities"][
    ["city_id", "lat", "lon", "enabled", "grid_width_m", "grid_height_m", "notes"]
].reset_index(drop=True)
assert cat.city_id.is_unique
walk = snap[snap.table == "walk"][
    ["city_id", "run_date", "coverage_pct_by_length", "median_covered_age_years", "length_km"]
]
c = pd.DataFrame([feats(r.lat, r.lon) for r in cat.itertuples()])
c = pd.concat([cat, c], axis=1).merge(walk, on="city_id", how="left", validate="one_to_one")
c.to_parquet(S + "/catalog_features.parquet")
print("catalog", len(c), round(time.time() - t), flush=True)
cols = {0: "geonameid", 2: "name", 4: "lat", 5: "lon", 8: "cc", 10: "admin1", 14: "pop"}
pl = pd.read_csv(
    D + "cities500.txt", sep="\t", header=None, usecols=list(cols), dtype={10: str}
).rename(columns=cols)
pl = pl[pl.lon.between(-161, -66) & pl.lat.between(18.5, 66)].reset_index(drop=True)
print("places", len(pl), flush=True)
assert pl.geonameid.is_unique
p = pd.DataFrame([feats(r.lat, r.lon) for r in pl.itertuples()])
p = pd.concat([pl, p], axis=1)
p.to_parquet(S + "/place_features.parquet")
print("done", round(time.time() - t), flush=True)
