import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from streetscape_metadata_tracker.download_common import lonlat_to_tile_frac, tiles_for_bbox

S = sys.argv[1]
c = pd.read_parquet(S + "/catalog_features.parquet")
man = json.load(open("experiments/mapillary-discovery-383/scan_manifest.json"))
tiles = {t for b in man["regions"].values() for t in tiles_for_bbox(*b, man["zoom"])}
inn = [
    (lambda f: (int(f[0]), int(f[1])) in tiles)(lonlat_to_tile_frac(r.lon, r.lat, 6))
    for r in c.itertuples()
]
c = c[np.array(inn)]
old = pd.read_csv("docs/experiments/mapillary-discovery-screen_validation.csv")
print("old cols", list(old.columns)[:8])
m = c.merge(old[["city_id", "km_per_km2"]], on="city_id", validate="one_to_one")
print(
    "reproduce committed score: n",
    len(m),
    "spearman",
    round(m.v0.rank().corr(m.km_per_km2.rank()), 4),
    "pearson",
    round(m.v0.corr(m.km_per_km2), 4),
    "median abs diff",
    round((m.v0 - m.km_per_km2).abs().median(), 4),
)
w = c[c.coverage_pct_by_length.notna()].copy()
w["tranche"] = w.notes.fillna("").str.contains("discovery screen")
w["good2"] = (w.coverage_pct_by_length >= 50) & (w.median_covered_age_years <= 2)
w["good3"] = (w.coverage_pct_by_length >= 50) & (w.median_covered_age_years <= 3)
w["any50"] = w.coverage_pct_by_length >= 50
print(
    "walked in tiles",
    len(w),
    "tranche",
    w.tranche.sum(),
    "latest run",
    w.run_date.max(),
    "walks older than scan",
    (w.run_date < "2026-10-02").sum(),
)
b = w[~w.tranche].copy()
t = w[w.tranche].copy()
print(
    "non-tranche: good2",
    b.good2.sum(),
    "good3",
    b.good3.sum(),
    "any50",
    b.any50.sum(),
    "| tranche good2",
    t.good2.sum(),
    "good3",
    t.good3.sum(),
    "any50",
    t.any50.sum(),
    "of",
    len(t),
)
b["v3max"] = b[["occ1_aw", "occ2_aw", "occ3_aw"]].max(axis=1)
t["v3max"] = t[["occ1_aw", "occ2_aw", "occ3_aw"]].max(axis=1)


# LOO uploader record over ALL walked (share>=.5): shrunk hit rate
def add_prior(df, ref, tgt):
    base = ref[tgt].mean()
    a = 5 * base
    bb = 5
    g = ref[ref.top_share >= 0.5].groupby("top")[tgt].agg(["sum", "count"])
    sm = df.top.map(g["sum"]).fillna(0).astype(float)
    n = df.top.map(g["count"]).fillna(0).astype(float)
    self_in = (df.index.isin(ref.index)) & (df.top_share >= 0.5)
    sm = sm - (df[tgt].astype(float) * self_in)
    n = n - self_in.astype(float)
    return (sm + a) / (n + bb)


V = {
    "V0 committed: recent length, 2 km": "v0",
    "V1 age-weighted length, 2 km": "v1",
    "V2 recent cells occupied, 2 km": "occ2_recent",
    "V3 age-weighted cells, 2 km": "occ2_aw",
    "V4 age-weighted cells, best of 1/2/3 km": "v3max",
}


def ap(y, s):
    o = np.argsort(-s, kind="stable")
    y = y[o]
    return (np.cumsum(y) / np.arange(1, len(y) + 1))[y == 1].mean()


def rc(y, s, k):
    o = np.argsort(-s, kind="stable")[:k]
    return int(y[o].sum())


rng = np.random.default_rng(0)
out = {}
for tgt in ["good2", "good3", "any50"]:
    print(f"\n== target {tgt}: non-tranche n={len(b)} positives={b[tgt].sum()}")
    for name, col in list(V.items()):
        s = b[col].values
        y = b[tgt].values.astype(int)
        rho = b[col].rank().corr(b.coverage_pct_by_length.rank())
        print(
            f"{name:42s} rho {rho:.3f}  AP {ap(y, s):.3f}  hits@40 {rc(y, s, 40):3d} @100 {rc(y, s, 100):3d}"
        )
        out.setdefault(tgt, {"n": len(b), "positives": int(y.sum()), "scores": {}})["scores"][
            name
        ] = dict(
            spearman_rho_vs_walk_coverage=round(rho, 3),
            average_precision=round(ap(y, s), 3),
            hits_at_40=rc(y, s, 40),
            hits_at_100=rc(y, s, 100),
        )
    pr = add_prior(b, w, tgt)
    s = (b.v3max * pr).values
    y = b[tgt].values.astype(int)
    rho = pd.Series(s, index=b.index).rank().corr(b.coverage_pct_by_length.rank())
    print(
        f"{'V5 V4 x LOO uploader record':42s} rho {rho:.3f}  AP {ap(y, s):.3f}  hits@40 {rc(y, s, 40):3d} @100 {rc(y, s, 100):3d}"
    )
    out[tgt]["scores"]["V5 V4 x LOO uploader record"] = dict(
        spearman_rho_vs_walk_coverage=round(rho, 3),
        average_precision=round(ap(y, s), 3),
        hits_at_40=rc(y, s, 40),
        hits_at_100=rc(y, s, 100),
    )
    # paired bootstrap vs V0 for hits@100
    n = len(b)
    idx = rng.integers(0, n, (2000, n))
    base = b.v0.values
    for nm, sc in [("V4", b.v3max.values), ("V5", s)]:
        di = []
        for i in idx:
            yy = y[i]
            di.append(ap(yy, sc[i]) - ap(yy, base[i]) if yy.sum() else 0)
        print(
            f"   AP diff {nm}-V0 mean {np.mean(di):+.3f} CI [{np.percentile(di, 2.5):+.3f},{np.percentile(di, 97.5):+.3f}]"
        )
        out[tgt][f"ap_diff_{nm}_minus_V0_city_bootstrap"] = dict(
            mean=round(float(np.mean(di)), 3),
            ci95=[round(float(np.percentile(di, q)), 3) for q in (2.5, 97.5)],
        )


def auc(y, s):
    r = pd.Series(s).rank().values
    n1 = y.sum()
    n0 = len(y) - n1
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else np.nan


print(
    "\n== Within the screened tranches (selection already on V0): does a variant separate hits from misses?",
    len(t),
)
for tgt in ["good2", "good3", "any50"]:
    y = t[tgt].values.astype(int)
    print(
        tgt,
        y.sum(),
        {k.split()[0]: round(auc(y, t[col].values), 3) for k, col in V.items()},
        "pop-free med_age",
        round(auc(y, -t.med_age.values), 3),
        "top_share",
        round(auc(y, t.top_share.values), 3),
    )
print(
    t[
        [
            "city_id",
            "v0",
            "occ2_aw",
            "v3max",
            "med_age",
            "top_share",
            "coverage_pct_by_length",
            "median_covered_age_years",
            "good2",
        ]
    ]
    .sort_values("coverage_pct_by_length")
    .round(2)
    .head(20)
    .to_string()
)
# named misses
for k in [
    "juneau--alaska",
    "laurens--iowa",
    "grand-marais",
    "wasta",
    "richmond--virginia",
    "waterbury",
    "honolulu",
    "meridian--idaho",
]:
    r = w[w.city_id.str.startswith(k)]
    for x in r.itertuples():
        print(
            x.city_id[:28],
            "v0",
            round(x.v0, 2),
            "pct",
            round((w.v0 < x.v0).mean(), 3),
            "v1",
            round(x.v1, 2),
            "v4",
            round(x.v3max, 3) if hasattr(x, "v3max") else "",
            "cov",
            x.coverage_pct_by_length,
            "age",
            x.median_covered_age_years,
        )
json.dump(
    dict(
        reproduce_committed_score_spearman=round(m.v0.rank().corr(m.km_per_km2.rank()), 4),
        walked_in_tiles=len(w),
        screened_tranche_walked=int(w.tranche.sum()),
        by_target=out,
    ),
    open(S + "/evaluate_metrics.json", "w"),
    indent=1,
)
b.to_parquet(S + "/val_b.parquet")
t.to_parquet(S + "/val_t.parquet")
