import json
import sys

import numpy as np
import pandas as pd

S = sys.argv[1]
b = pd.read_parquet(S + "/val_b.parquet")
t = pd.read_parquet(S + "/val_t.parquet")
w = pd.concat([b, t])
names = json.load(open("experiments/mapillary-discovery-383/creators_resolved.json"))
for extra in [
    "1361362982257515:GIS_ISG",
    "1006984308465014:UAS_ISG",
    "104273298482293:rking",
    "713819173436997:marker_geo1",
]:
    i, n = extra.split(":")
    names.setdefault(i, n)
TGT = "good2"
base = w[TGT].mean()


def record(df, ref):
    g = ref[ref.top_share >= 0.5].groupby("top")[TGT].agg(["sum", "count"])
    sm = df.top.map(g["sum"]).fillna(0).astype(float)
    n = df.top.map(g["count"]).fillna(0).astype(float)
    # Leave out EVERY copy of the row's own city: a cluster bootstrap draws an
    # uploader's cities k times, and subtracting one copy leaked the other k-1
    # copies of the city's own outcome into its score (fixed 2026-11-06, PR #457
    # review; the 11-05 run subtracted one copy).
    own = ref[ref.top_share >= 0.5].city_id.value_counts()
    k = df.city_id.map(own).fillna(0).astype(float) * (df.top_share >= 0.5)
    return (sm - df[TGT].astype(float) * k + 5 * base) / (n - k + 5)


def ap(y, s):
    o = np.argsort(-s, kind="stable")
    y = y[o]
    return (np.cumsum(y) / np.arange(1, len(y) + 1))[y == 1].mean() if y.sum() else np.nan


# Uploader-clustered bootstrap: resample uploaders, not cities; record recomputed inside each draw (LOO)
rng = np.random.default_rng(0)
b = b.copy()
b["ckey"] = b.top.where(b.top != "", b.city_id)
gs = {k: g for k, g in b.groupby("ckey")}
keys = list(gs)
res = {"V4": [], "V5": []}
tp = t.copy()
for _ in range(1000):
    pick = rng.integers(0, len(keys), len(keys))
    d = pd.concat([gs[keys[i]] for i in pick], ignore_index=True)
    if d[TGT].sum() == 0:
        continue
    ref = pd.concat([d, t], ignore_index=True)
    d2 = ref.iloc[: len(d)]
    r = record(d2, ref)
    y = d2[TGT].values.astype(int)
    a0 = ap(y, d2.v0.values)
    res["V4"].append(ap(y, d2.v3max.values) - a0)
    res["V5"].append(ap(y, (d2.v3max * r).values) - a0)
M = {"ap_diff_vs_V0_uploader_clustered": {}}
for k, v in res.items():
    M["ap_diff_vs_V0_uploader_clustered"][k] = dict(
        mean=round(float(np.mean(v)), 3),
        ci95=[round(float(x), 3) for x in np.percentile(v, [2.5, 97.5])],
        draws=len(v),
    )
    print(
        k,
        "AP diff vs V0, uploader-clustered: mean %+.3f CI [%+.3f,%+.3f] n=%d"
        % (np.mean(v), *np.percentile(v, [2.5, 97.5]), len(v)),
    )
# sensitivity: V5 record from tranche walks excluded
r2 = record(b, b)
print(
    "V5 with non-tranche record only: AP %.3f (V0 %.3f, V4 %.3f)"
    % (
        ap(b[TGT].values.astype(int), (b.v3max * r2).values),
        ap(b[TGT].values.astype(int), b.v0.values),
        ap(b[TGT].values.astype(int), b.v3max.values),
    )
)
# Threshold check, both populations
for th in (0.0, 0.5, 0.6, 0.7, 0.8, 1.0):
    for nm, d in (("tranche", t), ("catalog", b)):
        h = d[d.v3max >= th]
        M.setdefault("v4_threshold", []).append(
            dict(
                v4_at_least=th,
                population=nm,
                n=len(h),
                good2=int(h.good2.sum()),
                good3=int(h.good3.sum()),
                any50=int(h.any50.sum()),
            )
        )
        print(
            f"V4>={th} {nm:8s} n={len(h):3d} good2={int(h.good2.sum()):3d} good3={int(h.good3.sum()):3d} any50={int(h.any50.sum()):3d}",
            end=" | ",
        )
    print()
# Uploader scorecard
g = (
    w[w.top_share >= 0.5]
    .groupby("top")
    .agg(
        walked=("city_id", "count"),
        good2=("good2", "sum"),
        good3=("good3", "sum"),
        any50=("any50", "sum"),
        med_cov=("coverage_pct_by_length", "median"),
    )
)
# Places
p = pd.read_parquet(S + "/place_features.parquet")
p["v4"] = p[["occ1_aw", "occ2_aw", "occ3_aw"]].max(axis=1)
sn = pd.read_csv(S + "/prod_snapshot.csv", low_memory=False)
cat = sn[sn.table == "cities"]
clat, clon, gw, gh = [
    cat[c].astype(float).values for c in ["lat", "lon", "grid_width_m", "grid_height_m"]
]


def blocked(r):
    c = np.cos(np.radians(r.lat))
    dx = np.abs(clon - r.lon) * 111.32 * c
    dy = np.abs(clat - r.lat) * 110.57
    ingrid = (dx <= gw / 2000) & (dy <= gh / 2000)
    d = np.hypot(dx, dy)
    return bool(ingrid.any()), float(d.min())


x = [blocked(r) for r in p.itertuples()]
p["in_grid"] = [a for a, _ in x]
p["catalog_km"] = [b_ for _, b_ in x]
print(
    "\nplaces with v4>=.7:",
    (p.v4 >= 0.7).sum(),
    "not in grid & >5km:",
    ((p.v4 >= 0.7) & ~p.in_grid & (p.catalog_km > 5)).sum(),
)
ref = w[w.top_share >= 0.5].groupby("top")[TGT].agg(["sum", "count"])
p["rec"] = (p.top.map(ref["sum"]).fillna(0) + 5 * base) / (p.top.map(ref["count"]).fillna(0) + 5)
p["user"] = p.top.map(lambda i: names.get(str(i), str(i)))
p["v5"] = p.v4 * p.rec
c = p[
    (~p.in_grid)
    & (p.catalog_km > 5)
    & (p.v4 >= 0.7)
    & (p.top_share >= 0.6)
    & (p.foot_share < 0.5)
    & (p.med_age <= 2)
    & (p["pop"] <= 60000)
    & (p.cc.isin(["US", "CA"]))
].sort_values("v5", ascending=False)
print("pass all rules:", len(c))
M["funnel"] = dict(
    places_v4_at_least_0_7=int((p.v4 >= 0.7).sum()),
    outside_grids_and_over_5km=int(((p.v4 >= 0.7) & ~p.in_grid & (p.catalog_km > 5)).sum()),
    pass_all_rules=len(c),
)
kept = []
for i, r in c.iterrows():
    if all(
        np.hypot(
            (r.lat - c.at[k, "lat"]) * 110.57,
            (r.lon - c.at[k, "lon"]) * 111.32 * np.cos(np.radians(r.lat)),
        )
        >= 5
        for k in kept
    ):
        kept.append(i)
c = c.loc[kept]
print("after 5 km thinning:", len(c))
M["funnel"]["after_5km_thinning"] = len(c)
c["rank_in_user"] = c.groupby("top").cumcount() + 1
out = c[
    [
        "geonameid",
        "name",
        "admin1",
        "cc",
        "lat",
        "lon",
        "pop",
        "user",
        "top_share",
        "n_cre",
        "foot_share",
        "med_age",
        "newest_age",
        "v0",
        "v1",
        "v4",
        "rec",
        "v5",
        "catalog_km",
        "rank_in_user",
    ]
].round(3)
out.to_csv("experiments/mapillary-discovery-383/tranche3_ranked_2026-11-05.csv", index=False)
M["uploader_scorecard"] = [
    dict(
        user=names.get(str(i), str(i)),
        walked=int(r.walked),
        good2=int(r.good2),
        good3=int(r.good3),
        any50=int(r.any50),
        median_walk_coverage=round(float(r.med_cov), 1),
    )
    for i, r in g[g.walked >= 3].sort_values("walked", ascending=False).iterrows()
]
sc = g.sort_values("walked", ascending=False).copy()
sc["user"] = [names.get(str(i), str(i)) for i in sc.index]
sc["open_towns"] = sc.index.map(c.groupby("top").size()).fillna(0).astype(int)
print(
    sc[sc.walked >= 3][["user", "walked", "good2", "good3", "any50", "med_cov", "open_towns"]]
    .round(1)
    .to_string()
)
print("\nopen by uploader (top):", c.user.value_counts().head(15).to_dict())
print("\nTop 30 capped at 5 per uploader")
pr = out[out.rank_in_user <= 5].head(30)
print(
    pr[
        ["name", "admin1", "pop", "user", "top_share", "med_age", "v0", "v4", "rec", "catalog_km"]
    ].to_string()
)
print("\nNamed:")
q = p[
    p.name.isin(
        [
            "Elko",
            "Ammon",
            "Yukon",
            "De Soto",
            "Beaver Dam",
            "New London",
            "Brillion",
            "Arnold",
            "Lake Geneva",
            "Delavan",
            "Williams Bay",
            "Baldwin",
            "Horizon West",
            "Alafaya",
        ]
    )
    & p.admin1.isin(["NV", "ID", "OK", "KS", "WI", "MN", "FL"])
    & (p["pop"] > 1500)
]
print(
    q[
        [
            "name",
            "admin1",
            "pop",
            "user",
            "top_share",
            "med_age",
            "v0",
            "v4",
            "in_grid",
            "catalog_km",
        ]
    ]
    .round(2)
    .sort_values("v4", ascending=False)
    .to_string()
)
# Unresolved names
print("\nunresolved uploader ids in top 30:", [u for u in pr.user.unique() if u.isdigit()])
# Metrics for the writeup, with the evaluation's numbers merged in and the
# advance prediction for tranche 3 derived from the screened tranches' V4 >= 0.7 rate.
if len(sys.argv) > 2:
    from math import comb

    ev = json.load(open(S + "/evaluate_metrics.json"))
    t7 = [r for r in M["v4_threshold"] if r["v4_at_least"] == 0.7 and r["population"] == "tranche"][
        0
    ]
    p7 = t7["good2"] / t7["n"]
    n = 25
    cdf = lambda k: sum(comb(n, i) * p7**i * (1 - p7) ** (n - i) for i in range(k + 1))
    floor = max(
        k for k in range(n + 1) if cdf(k - 1) <= 0.05
    )  # P(X < floor) <= 5% at the observed rate
    M["advance_prediction_tranche3"] = dict(
        n=n,
        basis=f"screened tranches 1-2, V4 >= 0.7: {t7['good2']}/{t7['n']} strict hits",
        rate=round(p7, 3),
        expected_strict_hits=round(n * p7, 1),
        falsified_below=floor,
        p_below_floor_at_rate=round(cdf(floor - 1), 3),
    )
    M["evaluation"] = ev
    M["generated_by"] = [
        "scripts/mapillary_discovery_rescore/features.py",
        "scripts/mapillary_discovery_rescore/evaluate.py",
        "scripts/mapillary_discovery_rescore/tier3.py",
    ]
    json.dump(M, open(sys.argv[2], "w"), indent=1)
    print("wrote", sys.argv[2])
