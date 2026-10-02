#!/usr/bin/env python3
"""
Analysis half of the Mapillary discovery screen (issue #383). Makes NO network
requests: it reads what ``mapillary_discovery_collect.py`` cached.

    python scripts/mapillary_discovery_analyze.py \\
        --raw-dir experiments/mapillary-discovery-383 \\
        --places experiments/mapillary-discovery-383/cities500.txt \\
        --prod-snapshot experiments/mapillary-discovery-383/prod/prod_snapshot.csv \\
        --catalog-label makelab2-prod --docs-dir docs/experiments --manifest-out mapillary_discovery_cities.csv

Inputs (all under the gitignored raw dir except the vendored admin-1 names):
  sequences.parquet   one row per (sequence, tile) from ``collect scan``
  calibration.json    from ``collect calibrate``
  creators.json       from ``collect resolve-creators`` (optional)
  cities500.txt       GeoNames places >= 500 people (CC BY 4.0), downloaded
                      from download.geonames.org/export/dump/cities500.zip
  prod snapshot       read-only export of the production catalog: every
                      city's centre, and its latest Mapillary drive walk

Outputs:
  <docs>/mapillary-discovery-screen_metrics.json      every number the writeup quotes
  <docs>/mapillary-discovery-screen_candidates.csv    ranked uncatalogued places
  <docs>/mapillary-discovery-screen_validation.csv    catalog cities: score vs walk
  <manifest-out>                                      the first registration tranche,
                                                      in register_frame.py's format

A sequence crossing a tile edge appears once per tile, each copy clipped to
its tile, so rows are NOT de-duplicated by sequence id for length (that would
drop the other tiles' pieces); they are for sequence COUNTS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_worldwide_frame import effective_admin  # noqa: E402
from scripts.mapillary_discovery_common import (  # noqa: E402
    R_KM,
    SAMPLE_KM,
    nearest_km,
    place_scores,
    split_samples,
    thin_by_distance,
)

EXPERIMENT = "mapillary-discovery-screen"
RECENT_SINCE = "2024-10-02"  # two years before the 2026-10-02 scan
SCAN_BOX = (-161.0, 18.5, -66.0, 66.0)  # places outside every scanned region are not scored

# Candidate list: not in the catalog, recent, dense, dominated by one sweep
CANDIDATE_RULES = {
    "catalog_km_gt": 10.0,
    "median_captured_ge": "2025-01-01",
    "km_per_km2_ge": 3.0,
    "top_share_ge": 0.6,
    "thin_km": 5.0,
}
# The first registration tranche: Laurens-like towns -- small, very recent, one uploader,
# vehicle-borne -- at most three per uploader so no single rig decides the test
TRANCHE_RULES = {
    "catalog_km_gt": 10.0,
    "median_captured_ge": "2025-03-01",
    "km_per_km2_ge": 3.5,
    "top_share_ge": 0.75,
    "pop_le": 60000,
    "foot_share_lt": 0.5,
    "thin_km": 8.0,
    "per_creator_max": 3,
    "size": 25,
}
SCORE_BINS = [0, 0.5, 1.5, 3.0, 6.0, np.inf]
# Geocode-query overrides, by geonameid, found by computing each grid BEFORE
# registering (CLAUDE.md): the plain query matched a different feature.
GEOCODE_OVERRIDES = {
    # plain query matched Fond du Lac COUNTY (58.5 x 44.0 km, 7.7 km off)
    5253352: "Fond du Lac, Fond du Lac County, Wisconsin, United States",
}
# Okina/apostrophes are dropped from names: they reach city_ids and filenames,
# and the apostrophe form of "Waipi'o Acres" does not geocode at all.
NAME_STRIP = str.maketrans("", "", "'\u02bb\u2018\u2019")


def pct(series: pd.Series, qs=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict:
    s = series.dropna()
    return {"n": int(len(s)), **{f"p{int(q * 100)}": round(float(s.quantile(q)), 3) for q in qs}}


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman's rho as the Pearson correlation of average ranks (no scipy)."""
    m = a.notna() & b.notna()
    return round(float(a[m].rank().corr(b[m].rank())), 3)


def build_samples(seq: pd.DataFrame, since_ms: int) -> pd.DataFrame:
    rec = seq[seq.is_pano & (seq.captured_at >= since_ms)]
    rows = []
    for r in rec.itertuples(index=False):
        for lon, lat, km in split_samples(r.pts):
            rows.append((lon, lat, km, r.creator_id, r.captured_at, r.foot))
    return pd.DataFrame(rows, columns=["lon", "lat", "km", "creator", "captured", "foot"])


def load_places(path: str, admin1_path: str) -> pd.DataFrame:
    cols = {0: "geonameid", 2: "name", 4: "lat", 5: "lon", 8: "cc", 10: "admin1", 14: "pop"}
    pl = pd.read_csv(path, sep="\t", header=None, usecols=list(cols), names=None, dtype={10: str})
    pl = pl.rename(columns=cols)
    adm = pd.read_csv(
        admin1_path, sep="\t", header=None, names=["code", "admin_name", "ascii", "gid"]
    )
    pl["admin_name"] = (pl.cc + "." + pl.admin1.fillna("")).map(
        dict(zip(adm.code, adm.ascii, strict=True))
    )
    x0, y0, x1, y1 = SCAN_BOX
    return pl[pl.lon.between(x0, x1) & pl.lat.between(y0, y1)].reset_index(drop=True)


def inside_grid(lat: float, lon: float, grids: pd.DataFrame) -> str:
    """The first catalog city whose frozen grid rectangle contains (lat, lon), else ''."""
    dy = (grids.lat.to_numpy() - lat) * 110_570.0
    dx = (grids.lon.to_numpy() - lon) * 111_320.0 * np.cos(np.radians(lat))
    hit = (np.abs(dx) <= grids.grid_width_m.to_numpy() / 2) & (
        np.abs(dy) <= grids.grid_height_m.to_numpy() / 2
    )
    return str(grids.city_id.to_numpy()[hit][0]) if hit.any() else ""


def apply_rules(df: pd.DataFrame, rules: dict) -> pd.DataFrame:
    m = (
        (df.inside_catalog_grid == "")
        & (df.catalog_km > rules["catalog_km_gt"])
        & (df.median_captured >= rules["median_captured_ge"])
        & (df.km_per_km2 >= rules["km_per_km2_ge"])
        & (df.top_share >= rules["top_share_ge"])
    )
    if "pop_le" in rules:
        m &= df["pop"] <= rules["pop_le"]
    if "foot_share_lt" in rules:
        m &= df.foot_share < rules["foot_share_lt"]
    out = thin_by_distance(df[m].reset_index(drop=True), rules["thin_km"])
    if "per_creator_max" in rules:
        out = out.groupby("top_creator", sort=False).head(rules["per_creator_max"])
    if "size" in rules:
        out = out.head(rules["size"])
    return out.reset_index(drop=True)


def manifest_rows(tranche: pd.DataFrame) -> pd.DataFrame:
    """register_frame.py's manifest format (as in mapillary_360_cities.csv)."""
    rows = []
    for r in tranche.itertuples(index=False):
        country = {"US": "United States", "CA": "Canada"}.get(r.cc, r.cc)
        name = r.name.translate(NAME_STRIP).strip()
        admin = effective_admin(name, r.admin_name)
        query = GEOCODE_OVERRIDES.get(int(r.geonameid)) or ", ".join(
            p for p in (name, admin, country) if p
        )
        rows.append(
            {
                "query_string": query,
                "city": name,
                "admin": r.admin_name or "",
                "iso2": r.cc,
                "country": country,
                "continent": "NA" if r.cc in ("US", "CA", "MX") else "",
                "size_band": "",
                "population": int(r.pop),
                "coverage_regime": "",
                "geonameid": int(r.geonameid),
                "lat": r.lat,
                "lon": r.lon,
            }
        )
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--raw-dir", required=True)
    p.add_argument("--places", required=True)
    p.add_argument("--admin1", default="data_sources/admin1CodesASCII.txt")
    p.add_argument("--prod-snapshot", required=True)
    p.add_argument("--catalog-label", required=True)
    p.add_argument("--docs-dir", default="docs/experiments")
    p.add_argument("--manifest-out", default="mapillary_discovery_cities.csv")
    args = p.parse_args(argv)
    raw = Path(args.raw_dir)
    docs = Path(args.docs_dir)

    seq = pd.read_parquet(raw / "sequences.parquet")
    since_ms = pd.Timestamp(RECENT_SINCE).value // 10**6
    samples = build_samples(seq, since_ms)
    uniq = seq.drop_duplicates("seq")

    snap = pd.read_csv(args.prod_snapshot)
    cat = snap[snap.table == "cities"][
        ["city_id", "lat", "lon", "enabled", "grid_width_m", "grid_height_m"]
    ].reset_index(drop=True)
    walks = snap[snap.table == "walk"][
        ["city_id", "run_date", "coverage_pct_by_length", "median_covered_age_years", "length_km"]
    ]

    # ── Validation: score each catalog centre and join its measured walk ──
    cat_places = cat.rename(columns={"city_id": "name"})
    val = place_scores(samples, cat_places[["name", "lat", "lon"]], min_km=0.0)
    val = cat_places[["name", "lat", "lon"]].merge(
        val.drop(columns=["lat", "lon"]), on="name", how="left", validate="one_to_one"
    )
    val["km_per_km2"] = val.km_per_km2.fillna(0.0)
    x0, y0, x1, y1 = SCAN_BOX
    val = val[val.lon.between(x0, x1) & val.lat.between(y0, y1)]
    val = val.rename(columns={"name": "city_id"}).merge(
        walks, on="city_id", how="inner", validate="one_to_one"
    )
    val["good"] = (val.coverage_pct_by_length >= 50) & (val.median_covered_age_years <= 2)
    val["score_bin"] = pd.cut(val.km_per_km2, SCORE_BINS, right=False)
    bins = []
    for b, g in val.groupby("score_bin", observed=True):
        bins.append(
            {
                "km_per_km2": f"[{b.left:g}, {b.right:g})",
                "walked_cities": int(len(g)),
                "median_walk_coverage_pct": round(float(g.coverage_pct_by_length.median()), 1),
                "share_ge50pct_and_le2yr": round(float(g.good.mean()), 3),
                "good": int(g.good.sum()),
            }
        )
    ref_ids = [
        "laurens--iowa--united-states",
        "trotwood--ohio--united-states",
        "meridian--idaho--united-states",
        "honolulu--hawaii--united-states",
        "juneau--alaska--united-states",
        "grand-marais--minnesota--united-states",
        "waterville--maine--united-states",
        "waterbury--connecticut--united-states",
        "richmond--virginia--united-states",
        "wasta--south-dakota--united-states",
    ]
    refs = val[val.city_id.isin(ref_ids)][
        [
            "city_id",
            "km_per_km2",
            "top_creator",
            "top_share",
            "median_captured",
            "coverage_pct_by_length",
            "median_covered_age_years",
            "run_date",
        ]
    ].sort_values("km_per_km2", ascending=False)
    val.drop(columns="score_bin").to_csv(docs / f"{EXPERIMENT}_validation.csv", index=False)

    # ── Candidates: every GeoNames place, minus the catalog ──
    places = load_places(args.places, args.admin1)
    scored = place_scores(samples, places)
    near = [
        nearest_km(r.lat, r.lon, cat.lat.to_numpy(), cat.lon.to_numpy())
        for r in scored.itertuples()
    ]
    scored["catalog_city"] = [cat.city_id.iat[i] for i, _ in near]
    scored["catalog_km"] = [round(d, 1) for _, d in near]
    # distance to a centre is not membership: a frozen grid can be 40 km across
    scored["inside_catalog_grid"] = [inside_grid(r.lat, r.lon, cat) for r in scored.itertuples()]
    names_path = raw / "creators.json"
    names = json.loads(names_path.read_text()) if names_path.exists() else {}
    scored["top_creator_username"] = scored.top_creator.astype(str).map(names)
    cand = apply_rules(scored, CANDIDATE_RULES)
    cand.to_csv(docs / f"{EXPERIMENT}_candidates.csv", index=False)
    tranche = apply_rules(scored, TRANCHE_RULES)
    manifest_rows(tranche).to_csv(args.manifest_out, index=False)

    # ── Creators ──
    by_creator = samples.groupby("creator").agg(
        recent_pano_km=("km", "sum"), newest=("captured", "max")
    )
    by_creator["candidate_places"] = cand.groupby("top_creator").size()
    by_creator = by_creator.fillna({"candidate_places": 0}).sort_values(
        "recent_pano_km", ascending=False
    )
    top_creators = [
        {
            "creator_id": int(cid),
            "username": names.get(str(int(cid))),
            "recent_pano_km": round(float(r.recent_pano_km), 1),
            "candidate_places": int(r.candidate_places),
            "newest": pd.Timestamp(int(r.newest), unit="ms").date().isoformat(),
        }
        for cid, r in by_creator.head(30).iterrows()
    ]

    calib = json.loads((raw / "calibration.json").read_text())
    scan_manifest = json.loads((raw / "scan_manifest.json").read_text())
    log = [json.loads(line) for line in (raw / "request_log.jsonl").read_text().splitlines()]
    statuses: dict[str, dict[str, int]] = {}
    for e in log:
        h = statuses.setdefault(e.get("host", "tiles"), {})
        h[str(e["status"])] = h.get(str(e["status"]), 0) + 1

    metrics = {
        "_about": {
            "experiment": EXPERIMENT,
            "writeup": f"docs/experiments/{EXPERIMENT}.md",
            "generated_by": "python scripts/mapillary_discovery_analyze.py "
            + " ".join(sys.argv[1:]),
            "collected_by": "python scripts/mapillary_discovery_collect.py {scan,calibrate,resolve-creators} --out-dir "
            + args.raw_dir,
            "catalog_label": args.catalog_label,
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
            "note": "Screening signals from z6 vector tiles, never coverage. 'recent' means captured on or after "
            f"{RECENT_SINCE}. A place's score is recent-360 km within {R_KM} km of its point per km^2 of that disc, "
            f"from samples <= {SAMPLE_KM} km along simplified z6 geometry.",
        },
        "requests": {"by_host_and_status": statuses, "total": len(log)},
        "scan": {
            **{k: scan_manifest[k] for k in ("regions", "zoom", "tiles", "rows")},
            "unique_sequences": int(len(uniq)),
            "unique_pano_sequences": int(uniq.is_pano.sum()),
            "unique_recent_pano_sequences": int(
                (uniq.is_pano & (uniq.captured_at >= since_ms)).sum()
            ),
            "organization_id_share_of_sequences": round(
                float(uniq.organization_id.notna().mean()), 4
            ),
            "recent_pano_km": round(float(samples.km.sum()), 1),
            "creators_with_recent_pano": int(samples.creator.nunique()),
        },
        "calibration_zoom_recall": calib,
        "validation": {
            "population": "catalog cities with a Mapillary drive walk, centre inside the scanned regions; "
            "the score is taken at the catalog centre, the walk covers the whole frozen grid",
            "n": int(len(val)),
            "spearman_score_vs_walk_coverage": spearman(val.km_per_km2, val.coverage_pct_by_length),
            "good_definition": "walk coverage_pct_by_length >= 50 and median_covered_age_years <= 2",
            "by_score_bin": bins,
            "at_candidate_threshold": {
                "threshold_km_per_km2": CANDIDATE_RULES["km_per_km2_ge"],
                "good_total": int(val.good.sum()),
                "good_at_or_above": int(
                    val[val.km_per_km2 >= CANDIDATE_RULES["km_per_km2_ge"]].good.sum()
                ),
                "cities_at_or_above": int(
                    (val.km_per_km2 >= CANDIDATE_RULES["km_per_km2_ge"]).sum()
                ),
                "base_rate": round(float(val.good.mean()), 4),
            },
            "reference_towns": refs.to_dict(orient="records"),
        },
        "places": {
            "scored_places": int(len(scored)),
            "score_distribution": pct(scored.km_per_km2),
            "candidate_rules": CANDIDATE_RULES,
            "candidates": int(len(cand)),
            "excluded_inside_a_catalog_grid": int(
                len(apply_rules(scored.assign(inside_catalog_grid=""), {**CANDIDATE_RULES}))
                - len(cand)
            ),
            "candidates_by_top_creator": {
                str(names.get(str(k), k)): int(v)
                for k, v in cand.groupby("top_creator")
                .size()
                .sort_values(ascending=False)
                .head(15)
                .items()
            },
            "tranche_rules": TRANCHE_RULES,
            "geocode_overrides": {str(k): v for k, v in GEOCODE_OVERRIDES.items()},
            "tranche": tranche[
                [
                    "name",
                    "admin_name",
                    "pop",
                    "km_per_km2",
                    "top_creator_username",
                    "top_share",
                    "median_captured",
                    "catalog_city",
                    "catalog_km",
                ]
            ].to_dict(orient="records"),
        },
        "top_creators_by_recent_pano_km": top_creators,
    }
    out = docs / f"{EXPERIMENT}_metrics.json"
    out.write_text(json.dumps(metrics, indent=1, default=str) + "\n")
    print(f"wrote {out}, {len(cand)} candidates, tranche of {len(tranche)} -> {args.manifest_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
