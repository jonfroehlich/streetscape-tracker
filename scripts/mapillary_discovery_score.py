#!/usr/bin/env python3
"""
Scoring half of the Mapillary discovery screen for ANY scanned region. Zero requests.

    python scripts/mapillary_discovery_score.py \\
        --raw-dir experiments/mapillary-discovery-europe \\
        --places experiments/mapillary-discovery-383/cities500.txt \\
        --prod-snapshot experiments/mapillary-discovery-europe/prod_snapshot.csv \\
        --scan-date 2026-11-25 --out-dir experiments/mapillary-discovery-europe/score

It reads the tile cache a ``mapillary_discovery_collect.py fetch`` (or ``scan``)
left in ``--raw-dir`` ONE TILE AT A TIME, keeps only the 360-degree sequences as
compact sample arrays, and scores every GeoNames place and every catalog city in
the scanned tiles with the #441 re-score's features (``place_features`` in
``mapillary_discovery_common.py``; V4 is the score the tranche-3 advance
prediction was made on and held for, 20 of 25).

Why not ``mapillary_discovery_analyze.py`` or ``mapillary_discovery_rescore/``:
both are the committed record of one scan (2026-10-02, US and southern Canada)
and are hardcoded to its directory, date, bounding box and countries. They stay
as they ran. This script takes all four as arguments.

Outputs, in ``--out-dir`` (a gitignored working directory):

    places_ranked.csv    uncatalogued places passing every filter, best V4 first
    place_features.parquet / catalog_features.parquet    every scored point
    metrics.json         funnel counts, score-vs-walk agreement, parameters

and, beside the tiles in ``--raw-dir``: ``samples.npz`` (the sample cache, so a
re-score does not decode the tiles again) and ``creator_images.csv`` (each
uploader's newest 360 image, which ``collect resolve-creators`` needs to look a
username up).

Everything here is a SCREENING SIGNAL, never coverage: a road walk is the only
measurement of street coverage, and this only decides which places to walk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mapillary_discovery_analyze import (  # noqa: E402
    in_tiles,
    load_scan_manifest,
    scan_tiles,
    spearman,
    strict_json,
)
from scripts.mapillary_discovery_common import (  # noqa: E402
    FEATURE_STEP_KM,
    YEAR_MS,
    PanoSamples,
    decode_sequences,
    place_features,
    sample_polyline,
)

# The strict bar a walk is judged against (#441): at least half the street-km
# covered by 360-degree imagery, at a median age of at most two years.
STRICT_COVERAGE_PCT = 50.0
STRICT_AGE_YR = 2.0
# An uploader "dominates" a walked city, for its record, at this length share.
RECORD_TOP_SHARE = 0.5
# Sequence vertices are rounded as `collect scan` stores them, so a score from
# the tile cache equals one from that scan's sequences.parquet.
PTS_DECIMALS = 5
PLACE_COLS = {0: "geonameid", 2: "name", 4: "lat", 5: "lon", 8: "cc", 10: "admin1", 14: "pop"}


def tile_samples(raw: bytes, x: int, y: int, zoom: int):
    """
    One tile's 360-degree samples as arrays (lon, lat, km, captured_ms,
    creator) plus the on-foot flags, and ``{creator: (captured_ms, image_id)}``
    for each uploader's newest 360 sequence in the tile. A sequence with no
    capture time keeps NaN; one with no uploader gets -1.
    """
    lon, lat, km, cap, cre, foot = [], [], [], [], [], []
    newest: dict[int, tuple[float, int]] = {}
    for s in decode_sequences(raw, x, y, zoom):
        if not s.get("is_pano"):
            continue
        pts = [(round(a, PTS_DECIMALS), round(b, PTS_DECIMALS)) for a, b in s["_pts"]]
        slon, slat, skm = sample_polyline(pts)
        if len(skm) == 0:
            continue
        captured = s.get("captured_at")
        captured = float(captured) if captured is not None else np.nan
        creator = int(s["creator_id"]) if s.get("creator_id") is not None else -1
        lon.append(slon)
        lat.append(slat)
        km.append(skm)
        cap.append(np.full(len(skm), captured))
        cre.append(np.full(len(skm), creator, dtype=np.int64))
        foot.append(np.full(len(skm), bool(s.get("foot")), dtype=bool))
        image = s.get("image_id")
        if creator != -1 and image is not None and not np.isnan(captured):
            if creator not in newest or captured > newest[creator][0]:
                newest[creator] = (captured, int(image))
    if not km:
        e = np.empty(0)
        return e, e, e, e, np.empty(0, dtype=np.int64), np.empty(0, dtype=bool), newest
    return (
        np.concatenate(lon),
        np.concatenate(lat),
        np.concatenate(km),
        np.concatenate(cap),
        np.concatenate(cre),
        np.concatenate(foot),
        newest,
    )


def build_sample_cache(raw_dir: Path, tiles: list[tuple[int, int]], zoom: int) -> dict:
    """
    Decode every cached tile in ``tiles`` and return the sample arrays. A tile
    the manifest names that is not in the cache is refused: read as empty, its
    ground would score 0 without ever having been looked at.
    """
    parts: list[tuple] = []
    newest: dict[int, tuple[float, int]] = {}
    t0 = time.time()
    for i, (x, y) in enumerate(tiles, 1):
        path = raw_dir / "tiles" / f"{zoom}_{x}_{y}.mvt"
        if not path.exists():
            raise SystemExit(f"{path} is in the scan manifest but not in the tile cache")
        *arrays, tile_newest = tile_samples(path.read_bytes(), x, y, zoom)
        parts.append(tuple(arrays))
        for creator, seen in tile_newest.items():
            if creator not in newest or seen[0] > newest[creator][0]:
                newest[creator] = seen
        print(
            f"{i}/{len(tiles)} {x},{y} samples={len(arrays[0])} ({time.time() - t0:.0f} s)",
            flush=True,
        )
    lon, lat, km, cap, cre, foot = (np.concatenate(c) for c in zip(*parts, strict=True))
    return {
        "tile_bytes": tile_sizes(raw_dir, tiles, zoom),
        "step_km": np.array(FEATURE_STEP_KM),
        "lon": lon,
        "lat": lat,
        "km": km.astype(np.float32),
        "captured_ms": cap,
        "creator": cre,
        "foot": foot,
        "tiles": np.array(tiles, dtype=np.int64).reshape(-1, 2),
        "newest_creator": np.array(list(newest), dtype=np.int64),
        "newest_captured_ms": np.array([v[0] for v in newest.values()]),
        "newest_image": np.array([v[1] for v in newest.values()], dtype=np.int64),
    }


def tile_sizes(raw_dir: Path, tiles: list[tuple[int, int]], zoom: int) -> np.ndarray:
    """Each cached tile's size in bytes, -1 for one that is missing."""
    paths = [raw_dir / "tiles" / f"{zoom}_{x}_{y}.mvt" for x, y in tiles]
    return np.array([p.stat().st_size if p.exists() else -1 for p in paths], dtype=np.int64)


def load_samples(raw_dir: Path, manifest: dict, rebuild: bool = False) -> dict:
    """
    The sample cache for this manifest's tiles, built (and saved) if absent or
    stale. Stale means: other tiles, a tile whose size on disk differs from the
    one the cache was built from (a re-scan into the same directory), or another
    sample spacing. A cache from before those were recorded is rebuilt.
    """
    tiles = sorted(scan_tiles(manifest))
    cache = raw_dir / "samples.npz"
    if cache.exists() and not rebuild:
        z = dict(np.load(cache))
        if (
            z["tiles"].tolist() == [list(t) for t in tiles]
            and "tile_bytes" in z
            and z["tile_bytes"].tolist() == tile_sizes(raw_dir, tiles, manifest["zoom"]).tolist()
            and float(z.get("step_km", np.nan)) == FEATURE_STEP_KM
        ):
            return z
        print("samples.npz was built from other tiles or settings; rebuilding", flush=True)
    z = build_sample_cache(raw_dir, tiles, manifest["zoom"])
    np.savez(cache, **z)
    pd.DataFrame(
        {
            "creator_id": z["newest_creator"],
            "image_id": z["newest_image"],
            "captured_at": z["newest_captured_ms"],
        }
    ).to_csv(raw_dir / "creator_images.csv", index=False)
    return z


def pano_samples(z: dict, scan_date: str) -> PanoSamples:
    scan_ms = pd.Timestamp(scan_date).value // 10**6
    return PanoSamples(
        z["lon"],
        z["lat"],
        z["km"],
        (scan_ms - z["captured_ms"]) / YEAR_MS,
        z["creator"],
        z["foot"],
    )


def feature_table(samples: PanoSamples, points: pd.DataFrame) -> pd.DataFrame:
    """``points`` (lat, lon, any other columns) with ``place_features`` joined on, row for row."""
    feats = pd.DataFrame([place_features(samples, r.lat, r.lon) for r in points.itertuples()])
    if not len(points):  # no rows still needs the columns (a region with no catalog city)
        feats = pd.DataFrame(columns=list(place_features(samples, 0.0, 0.0)))
    return pd.concat([points.reset_index(drop=True), feats], axis=1)


def load_places(path: str, tiles: set[tuple[int, int]], zoom: int) -> pd.DataFrame:
    """GeoNames places (a cities500-format file) whose point lies in a scanned tile."""
    pl = pd.read_csv(
        path,
        sep="\t",
        header=None,
        usecols=list(PLACE_COLS),
        dtype={10: str},
        keep_default_na=False,  # the country code "NA" (Namibia) is not a missing value
        na_values=[""],
    ).rename(columns=PLACE_COLS)
    assert pl.geonameid.is_unique
    return pl[in_tiles(pl.lat, pl.lon, tiles, zoom)].reset_index(drop=True)


def load_catalog(snapshot: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The snapshot's cities (centre and frozen grid) and each one's latest Mapillary drive walk."""
    snap = pd.read_csv(snapshot, low_memory=False)
    cat = snap[snap.table == "cities"][
        ["city_id", "lat", "lon", "enabled", "grid_width_m", "grid_height_m"]
    ].reset_index(drop=True)
    walks = snap[snap.table == "walk"][
        ["city_id", "run_date", "coverage_pct_by_length", "median_covered_age_years", "length_km"]
    ].reset_index(drop=True)
    assert cat.city_id.is_unique and walks.city_id.is_unique
    return cat, walks


def catalog_relation(places: pd.DataFrame, cat: pd.DataFrame) -> pd.DataFrame:
    """
    Two columns per place: ``in_grid`` (its point lies inside some catalog
    city's frozen grid rectangle) and ``catalog_km`` (distance to the nearest
    catalog centre). A place inside a grid is already being collected, however
    far it sits from that grid's centre.
    """
    clat, clon = cat.lat.to_numpy(float), cat.lon.to_numpy(float)
    half_w, half_h = (
        cat.grid_width_m.to_numpy(float) / 2000,
        cat.grid_height_m.to_numpy(float) / 2000,
    )
    in_grid, nearest = [], []
    for r in places.itertuples():
        dx = np.abs(clon - r.lon) * 111.32 * np.cos(np.radians(r.lat))
        dy = np.abs(clat - r.lat) * 110.57
        in_grid.append(bool(((dx <= half_w) & (dy <= half_h)).any()))
        nearest.append(float(np.hypot(dx, dy).min()) if len(clat) else np.inf)
    return pd.DataFrame({"in_grid": in_grid, "catalog_km": nearest})


def thin(df: pd.DataFrame, km: float) -> pd.DataFrame:
    """Greedy thinning in the frame's order: drop a row within ``km`` of one already kept."""
    kept: list[int] = []
    lat, lon = df.lat.to_numpy(float), df.lon.to_numpy(float)
    for i in range(len(df)):
        d = np.hypot(
            (lat[i] - lat[kept]) * 110.57,
            (lon[i] - lon[kept]) * 111.32 * np.cos(np.radians(lat[i])),
        )
        if (d >= km).all():
            kept.append(i)
    return df.iloc[kept]


def rank_places(places: pd.DataFrame, rules: dict) -> tuple[pd.DataFrame, dict]:
    """
    The uncatalogued places passing every rule, best V4 first, thinned; and the
    funnel that got there. ``rules`` keys: v4_min, top_share_min,
    foot_share_max, med_age_max, catalog_km_min, thin_km, and optionally
    pop_max and countries (a list of ISO codes).
    """
    hot = places.v4 >= rules["v4_min"]
    open_ = hot & ~places.in_grid & (places.catalog_km > rules["catalog_km_min"])
    ok = (
        open_
        & (places.top_share >= rules["top_share_min"])
        & (places.foot_share < rules["foot_share_max"])
        & (places.med_age <= rules["med_age_max"])
    )
    if rules.get("pop_max") is not None:
        ok &= places["pop"] <= rules["pop_max"]
    if rules.get("countries"):
        ok &= places.cc.isin(rules["countries"])
    passing = places[ok].sort_values(["v4", "geonameid"], ascending=[False, True])
    ranked = thin(passing, rules["thin_km"])
    funnel = {
        "places_scored": int(len(places)),
        "places_v4_at_least_min": int(hot.sum()),
        "of_which_inside_a_catalog_grid": int((hot & places.in_grid).sum()),
        "outside_grids_and_beyond_catalog_km_min": int(open_.sum()),
        "pass_all_rules": int(len(passing)),
        "after_thinning": int(len(ranked)),
    }
    return ranked, funnel


def validation(cat_features: pd.DataFrame, walks: pd.DataFrame, v4_min: float) -> tuple:
    """
    Catalog cities in the scanned tiles joined to their walks, and how the
    scores agree with them. ``strict`` is the #441 bar.
    """
    val = cat_features.merge(walks, on="city_id", how="inner", validate="one_to_one")
    val["strict"] = (val.coverage_pct_by_length >= STRICT_COVERAGE_PCT) & (
        val.median_covered_age_years <= STRICT_AGE_YR
    )
    hot = val.v4 >= v4_min
    summary = {
        "walked_catalog_cities_in_scanned_tiles": int(len(val)),
        "strict_hits": int(val.strict.sum()),
        "spearman_v4_vs_walk_coverage": spearman(val.v4, val.coverage_pct_by_length)
        if len(val) > 2
        else None,
        "spearman_v0_vs_walk_coverage": spearman(val.v0, val.coverage_pct_by_length)
        if len(val) > 2
        else None,
        "at_or_above_v4_min": {"n": int(hot.sum()), "strict_hits": int(val.strict[hot].sum())},
        "below_v4_min": {"n": int((~hot).sum()), "strict_hits": int(val.strict[~hot].sum())},
    }
    return val, summary


def uploader_record(val: pd.DataFrame) -> pd.DataFrame:
    """Per uploader: walked catalog cities it dominates, and how many met the strict bar."""
    own = val[(val.top_share >= RECORD_TOP_SHARE) & (val.top != -1)]
    return own.groupby("top").strict.agg(uploader_walked="count", uploader_strict="sum")


def request_summary(raw_dir: Path) -> dict:
    """What the collection cost, from its own ``request_log.jsonl``: requests by host and status."""
    log = raw_dir / "request_log.jsonl"
    if not log.exists():
        return {}
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    by: dict[str, int] = {}
    for e in entries:
        key = f"{e.get('host', 'tiles')}:{e['status']}"
        by[key] = by.get(key, 0) + 1
    tiles = [e for e in entries if e.get("host", "tiles") == "tiles"]
    return {
        "by_host_and_status": dict(sorted(by.items())),
        "tile_bytes": int(sum(e.get("bytes", 0) for e in tiles)),
        "largest_tile_bytes": int(max((e.get("bytes", 0) for e in tiles), default=0)),
        "token_vars": sorted({e["token_var"] for e in entries if e.get("token_var")}),
    }


def calibration_summary(raw_dir: Path) -> dict:
    """
    ``collect calibrate``'s result, reduced to what it is for: per town, the
    recent 360 sequences the screening zoom shows against the finer zoom's.
    The two tiles cover different ground (the finer one is smaller than the
    disc), so a ratio above 1 is expected and only a LOW one is a finding.
    """
    path = raw_dir / "calibration.json"
    if not path.exists():
        return {}
    cal = json.loads(path.read_text())
    towns = {}
    for name, t in cal.get("towns", {}).items():
        coarse, fine = t["by_zoom"][0], t["by_zoom"][-1]
        towns[name] = {
            f"z{coarse['zoom']}_recent_pano": coarse["pano_recent"],
            f"z{fine['zoom']}_recent_pano": fine["pano_recent"],
            "ratio": round(coarse["pano_recent"] / fine["pano_recent"], 3)
            if fine["pano_recent"]
            else None,
            "same_top_creator": coarse["top_creator"] == fine["top_creator"],
        }
    return {"r_km": cal.get("r_km"), "recent_since": cal.get("recent_since"), "towns": towns}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--raw-dir", required=True, help="holds tiles/ and scan_manifest.json")
    p.add_argument("--places", required=True, help="a GeoNames cities500-format file")
    p.add_argument("--prod-snapshot", required=True, help="collect catalog-snapshot output")
    p.add_argument("--scan-date", required=True, help="the day the tiles were fetched (ages)")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--creators", help="creator_id -> username JSON (collect resolve-creators)")
    p.add_argument("--rebuild-samples", action="store_true")
    p.add_argument("--v4-min", type=float, default=0.7)
    p.add_argument("--top-share-min", type=float, default=0.6)
    p.add_argument("--foot-share-max", type=float, default=0.5)
    p.add_argument("--med-age-max", type=float, default=2.0)
    p.add_argument("--catalog-km-min", type=float, default=5.0)
    p.add_argument("--thin-km", type=float, default=5.0)
    p.add_argument("--pop-max", type=int, default=None, help="default: no population cap")
    p.add_argument("--countries", nargs="+", default=None, help="ISO codes; default: all")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    raw, out = Path(args.raw_dir), Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_scan_manifest(raw)
    tiles, zoom = scan_tiles(manifest), manifest["zoom"]
    z = load_samples(raw, manifest, args.rebuild_samples)
    samples = pano_samples(z, args.scan_date)
    print(f"{len(samples):,} 360 samples, {samples.km.sum():,.0f} km", flush=True)

    cat, walks = load_catalog(args.prod_snapshot)
    cat_in = cat[in_tiles(cat.lat, cat.lon, tiles, zoom)].reset_index(drop=True)
    cat_features = feature_table(samples, cat_in)
    cat_features.to_parquet(out / "catalog_features.parquet")
    val, val_summary = validation(cat_features, walks, args.v4_min)
    print(f"catalog cities in scanned tiles: {len(cat_in)}, walked: {len(val)}", flush=True)
    names = json.loads(Path(args.creators).read_text()) if args.creators else {}

    def username(i) -> str:
        return names.get(str(i)) or ("" if i == -1 else str(i))

    val["user"] = val.top.map(username)
    val_cols = ["city_id", "v0", "v4", "user", "top_share", "foot_share", "med_age"]
    val_cols += ["coverage_pct_by_length", "median_covered_age_years", "length_km", "strict"]
    val.sort_values("v4", ascending=False)[val_cols].round(4).to_csv(
        out / "validation.csv", index=False
    )

    places = load_places(args.places, tiles, zoom)
    print(f"{len(places):,} places in scanned tiles", flush=True)
    places = feature_table(samples, places)
    places = pd.concat([places, catalog_relation(places, cat)], axis=1)
    record = uploader_record(val)
    places = places.join(record, on="top")
    places[["uploader_walked", "uploader_strict"]] = (
        places[["uploader_walked", "uploader_strict"]].fillna(0).astype(int)
    )
    places["user"] = places.top.map(username)
    places.to_parquet(out / "place_features.parquet")

    rules = {
        "v4_min": args.v4_min,
        "top_share_min": args.top_share_min,
        "foot_share_max": args.foot_share_max,
        "med_age_max": args.med_age_max,
        "catalog_km_min": args.catalog_km_min,
        "thin_km": args.thin_km,
        "pop_max": args.pop_max,
        "countries": args.countries,
    }
    ranked, funnel = rank_places(places, rules)
    cols = [
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
        "catalog_km",
        "uploader_walked",
        "uploader_strict",
    ]
    ranked[cols].round(4).to_csv(out / "places_ranked.csv", index=False)

    metrics = {
        "_about": {
            "generated_by": "scripts/mapillary_discovery_score.py " + " ".join(sys.argv[1:]),
            "scan_date": args.scan_date,
            "requests": 0,
        },
        "scan": {k: manifest.get(k) for k in ("regions", "zoom", "tiles", "rows", "finished_at")},
        "samples": {"n": int(len(samples)), "km": round(float(samples.km.sum()), 1)},
        "collection": request_summary(raw),
        "calibration": calibration_summary(raw),
        "v4_of_scored_places": {
            f"p{q}": round(float(places.v4.quantile(q / 100)), 4) for q in (50, 90, 99, 99.9)
        },
        "rules": rules,
        "funnel": funnel,
        "validation": {"catalog_cities_in_scanned_tiles": int(len(cat_in)), **val_summary},
        # the walked catalog cities each ranked uploader dominates, and how they walked
        "ranked_uploader_record": {
            user: {
                "ranked_places": int(len(g)),
                "walked_cities": int(g.uploader_walked.iloc[0]),
                "strict_hits": int(g.uploader_strict.iloc[0]),
            }
            for user, g in ranked[ranked.uploader_walked > 0].groupby("user")
        },
        "ranked_by_country": ranked.cc.value_counts().to_dict(),
        "ranked_by_uploader": ranked.user.value_counts().head(25).to_dict(),
        "ranked_population": {
            f"p{q}": float(ranked["pop"].quantile(q / 100)) for q in (10, 25, 50, 75, 90)
        }
        if len(ranked)
        else {},
    }
    (out / "metrics.json").write_text(strict_json(metrics) + "\n")
    print(strict_json({k: metrics[k] for k in ("funnel", "validation")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
