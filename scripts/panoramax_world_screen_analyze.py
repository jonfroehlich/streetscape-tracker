"""
Issue #406: derive the committed record of the 2026-10-01 candidate-city screen.

    python scripts/panoramax_world_screen_analyze.py --docs-dir docs/experiments

Offline, no network. Reads the gitignored raw outputs of the research pass and
writes the two committed artifacts `docs/experiments/panoramax-world-screen.md`
quotes from:

- `panoramax-world-screen_metrics.json` -- the request-log summary, the hexagon
  distribution, the place and cluster counts, the ranked new and already-tracked
  clusters, and the Mapillary half (its two Graph probes and its web-research
  table's catalog distances);
- `panoramax-world-screen_clusters.csv` -- every screened cluster, ranked.

Inputs (all under `--raw-dir`, default the 2026-10-01 pass):

    panoramax/hexes.csv           res-6 hexagons from panoramax_world_screen_collect.py
    panoramax/requests.log        one JSON line per request (235 tiles + 4 searches)
    prod_catalog_cities.csv       a production catalog snapshot (city_id, lat, lon, enabled)
    mapillary/probe_results.jsonl the Graph API probes
    mapillary/candidates.csv      the Mapillary web-research table

plus `data_sources/cities15000.txt` and `admin1CodesASCII.txt` (GeoNames, vendored).

THE DERIVATION, which reproduces the research pass's `places_ranked.csv` and
`candidates.csv` row for row (checked when this was written):

1. A PLACE's bound is the sum of `nb_360_pictures` over hexagons whose centre is
   within `PLACE_RADIUS_KM` of its GeoNames point. Places under
   `MIN_PLACE_360` are dropped.
2. Places are clustered greedily in descending bound order: a place within
   `CLUSTER_RADIUS_KM` of an existing cluster's ANCHOR joins it. A cluster
   reports the anchor's bound but the name and point of its most POPULOUS member
   (GeoNames population picks the name and is never itself reported).
3. A cluster is "tracked" when that named point is within `CATALOG_RADIUS_KM`
   of any catalog city (the #298 reuse radius), enabled or not.
4. A new cluster is a CANDIDATE at `CANDIDATE_MIN_360`, or at `CANDIDATE_MIN_360_NA`
   in the US and Canada (the deployment priority).

Every bound is an UPPER BOUND, never coverage: a res-6 hexagon is ~36 km^2, and
neighbouring places share hexagons, so their bounds double-count (the metrics
file quantifies it).
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import math
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from experiment_stats import describe  # noqa: E402

TOPIC = "panoramax-world-screen"
PLACE_RADIUS_KM = 10.0
MIN_PLACE_360 = 2000
CLUSTER_RADIUS_KM = 20.0
CATALOG_RADIUS_KM = 25.0
CANDIDATE_MIN_360 = 5000
CANDIDATE_MIN_360_NA = 2000
NORTH_AMERICA = ("US", "CA")
TOP_N = 40
SCREEN_DAY = "2026-10-01"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RAW_DIR = os.path.join(REPO, "experiments", "candidate-360-cities-2026-10-01")
GEONAMES = os.path.join(REPO, "data_sources", "cities15000.txt")
ADMIN1 = os.path.join(REPO, "data_sources", "admin1CodesASCII.txt")

CLUSTER_FIELDS = [
    "rank",
    "name",
    "admin1",
    "cc",
    "lat",
    "lon",
    "ub_360_10km",
    "ub_all_10km",
    "share_360",
    "max_hex_360",
    "newest_hex_date",
    "n_members",
    "in_catalog",
    "candidate",
    "catalog_city_id",
    "catalog_enabled",
    "catalog_km",
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (the research pass's exact formula, Earth diameter 12,742 km)."""
    p = math.pi / 180
    a = (
        0.5
        - math.cos((lat2 - lat1) * p) / 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    )
    return 12742 * math.asin(math.sqrt(a))


class HexIndex:
    """1-degree buckets so a radius query scans 9 buckets, not 261,913 hexagons."""

    def __init__(self, hexes: list[dict[str, Any]]):
        self.buckets: dict[tuple[int, int], list[dict[str, Any]]] = collections.defaultdict(list)
        for h in hexes:
            self.buckets[(int(h["lat"]), int(h["lon"]))].append(h)

    def near(self, lat: float, lon: float, radius_km: float) -> list[dict[str, Any]]:
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for h in self.buckets.get((int(lat) + dy, int(lon) + dx), []):
                    if haversine_km(lat, lon, h["lat"], h["lon"]) <= radius_km:
                        out.append(h)
        return out


def nearest_catalog(
    lat: float, lon: float, catalog: list[dict[str, Any]], radius_km: float = CATALOG_RADIUS_KM
) -> tuple[float, dict[str, Any]] | None:
    """The closest catalog city within `radius_km`, or None (the 25 km reuse rule)."""
    best = None
    for c in catalog:
        if abs(c["lat"] - lat) > 0.4 or abs(c["lon"] - lon) > 0.6:
            continue
        d = haversine_km(lat, lon, c["lat"], c["lon"])
        if d <= radius_km and (best is None or d < best[0]):
            best = (d, c)
    return best


def place_bounds(places: list[dict[str, Any]], index: HexIndex) -> list[dict[str, Any]]:
    """Step 1: each place's 10 km upper bound, dropping those under MIN_PLACE_360, ranked."""
    rows = []
    for p in places:
        hs = index.near(p["lat"], p["lon"], PLACE_RADIUS_KM)
        if not hs:
            continue
        ub_360 = sum(h["nb_360_pictures"] for h in hs)
        if ub_360 < MIN_PLACE_360:
            continue
        rows.append(
            {
                **p,
                "ub_360": ub_360,
                "ub_all": sum(h["nb_pictures"] for h in hs),
                "n_hex": len(hs),
                "newest_hex_date": max((h["date"] or "") for h in hs),
                "max_hex_360": max(h["nb_360_pictures"] for h in hs),
            }
        )
    rows.sort(key=lambda r: (-r["ub_360"], -r["pop"]))
    return rows


def cluster_places(
    ranked: list[dict[str, Any]], radius_km: float = CLUSTER_RADIUS_KM
) -> list[dict[str, Any]]:
    """Step 2: greedy clustering around the highest-bound anchors, in rank order."""
    clusters: list[dict[str, Any]] = []
    for r in ranked:
        for c in clusters:
            if (
                haversine_km(r["lat"], r["lon"], c["anchor"]["lat"], c["anchor"]["lon"])
                <= radius_km
            ):
                c["members"].append(r)
                break
        else:
            clusters.append({"anchor": r, "members": [r]})
    return clusters


def summarize_cluster(cluster: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """Steps 3-4 for one cluster: the reported row."""
    anchor = cluster["anchor"]
    named = max(cluster["members"], key=lambda m: m["pop"])
    match = nearest_catalog(named["lat"], named["lon"], catalog)
    na = named["cc"] in NORTH_AMERICA
    threshold = CANDIDATE_MIN_360_NA if na else CANDIDATE_MIN_360
    return {
        "name": named["name"],
        "admin1": named["admin1"],
        "cc": named["cc"],
        "lat": named["lat"],
        "lon": named["lon"],
        "ub_360_10km": anchor["ub_360"],
        "ub_all_10km": anchor["ub_all"],
        "share_360": round(anchor["ub_360"] / anchor["ub_all"], 3) if anchor["ub_all"] else None,
        "max_hex_360": anchor["max_hex_360"],
        "newest_hex_date": max(m["newest_hex_date"] for m in cluster["members"]),
        "n_members": len(cluster["members"]),
        "in_catalog": "yes" if match else "no",
        "candidate": "yes" if (not match and anchor["ub_360"] >= threshold) else "no",
        "catalog_city_id": match[1]["city_id"] if match else "",
        "catalog_enabled": match[1]["enabled"] if match else "",
        "catalog_km": round(match[0], 1) if match else "",
    }


# ── Loaders ────────────────────────────────────────────────────────────────


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_geonames(path: str = GEONAMES, admin_path: str = ADMIN1) -> list[dict[str, Any]]:
    admin = {}
    with open(admin_path, encoding="utf-8") as f:
        for line in f:
            fields = line.rstrip("\n").split("\t")
            admin[fields[0]] = fields[1]
    places = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            fields = line.split("\t")
            places.append(
                {
                    "name": fields[1],
                    "lat": float(fields[4]),
                    "lon": float(fields[5]),
                    "cc": fields[8],
                    "admin1": admin.get(fields[8] + "." + fields[10], fields[10]),
                    "pop": int(fields[14] or 0),
                }
            )
    return places


def load_hexes(path: str) -> list[dict[str, Any]]:
    with open(path, newline="") as f:
        hexes = list(csv.DictReader(f))
    for h in hexes:
        h["lat"], h["lon"] = float(h["lat"]), float(h["lon"])
        for k in ("nb_pictures", "nb_360_pictures", "nb_flat_pictures"):
            h[k] = int(h[k])
    return hexes


def load_catalog(path: str) -> list[dict[str, Any]]:
    with open(path, newline="") as f:
        catalog = list(csv.DictReader(f))
    for c in catalog:
        c["lat"], c["lon"] = float(c["lat"]), float(c["lon"])
    return catalog


def load_jsonl(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ── Summaries ──────────────────────────────────────────────────────────────


def summarize_requests(log: list[dict[str, Any]]) -> dict[str, Any]:
    """Status, region, bytes, latency and pacing of the tile requests, plus the search probes."""
    from datetime import datetime

    tiles = [r for r in log if "x" in r]
    searches = [r for r in log if r.get("endpoint") == "/api/search"]
    stamps = sorted(datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ") for r in tiles)
    span_s = (stamps[-1] - stamps[0]).total_seconds()
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)]
    header_values: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in tiles:
        for k, v in r.get("hdrs", {}).items():
            header_values[k][v] += 1
    statuses = collections.Counter(str(r["status"]) for r in log)
    return {
        "total_requests": len(log),
        "refusals_403_429": statuses.get("403", 0) + statuses.get("429", 0),
        "status_counts_all": dict(sorted(statuses.items())),
        "tiles": {
            "n": len(tiles),
            "status_counts": dict(
                sorted(collections.Counter(str(r["status"]) for r in tiles).items())
            ),
            "by_region": dict(collections.Counter(r["region"] for r in tiles)),
            "bytes_total": sum(r["bytes"] for r in tiles),
            "bytes_per_tile": describe([r["bytes"] for r in tiles], digits=0),
            "latency_s": describe([r["s"] for r in tiles], digits=3),
            "first_ts": stamps[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_ts": stamps[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "span_s": span_s,
            "effective_per_min": round((len(tiles) - 1) / span_s * 60, 2),
            "inter_request_gap_s_1s_resolution": describe(gaps, digits=2),
            "response_header_values": {k: dict(v) for k, v in sorted(header_values.items())},
        },
        "searches": [{k: r[k] for k in ("probe", "status", "bytes", "s", "ts")} for r in searches],
    }


def summarize_hexes(hexes: list[dict[str, Any]]) -> dict[str, Any]:
    """The distribution of what the screen decoded, per res-6 hexagon."""
    with_360 = [h["nb_360_pictures"] for h in hexes if h["nb_360_pictures"] > 0]
    years = collections.Counter((h["date"] or "")[:4] or "none" for h in hexes)
    return {
        "n_hexes": len(hexes),
        "pictures_total": sum(h["nb_pictures"] for h in hexes),
        "pictures_360_total": sum(h["nb_360_pictures"] for h in hexes),
        "pictures_flat_total": sum(h["nb_flat_pictures"] for h in hexes),
        "n_hexes_with_360": len(with_360),
        "nb_pictures_per_hex": describe([h["nb_pictures"] for h in hexes], digits=1),
        "nb_360_per_hex_all": describe([h["nb_360_pictures"] for h in hexes], digits=1),
        "nb_360_per_hex_where_positive": describe(with_360, digits=1),
        "newest_capture_year_per_hex": dict(sorted(years.items())),
        # A hex's `date` is the newest capture the host holds for it, read as
        # served. These cannot be true, and are counted rather than dropped.
        "n_hex_dates_before_2000": sum(bool(h["date"]) and h["date"] < "2000" for h in hexes),
        "n_hex_dates_after_screen_day": sum((h["date"] or "") > SCREEN_DAY for h in hexes),
    }


def summarize_mapillary(
    probes: list[dict[str, Any]], table: list[dict[str, Any]], catalog
) -> dict[str, Any]:
    """The Mapillary half: the two Graph requests sent, and catalog distances for the web table."""
    distances = []
    for row in table:
        match = nearest_catalog(float(row["downtown_lat"]), float(row["downtown_lon"]), catalog)
        distances.append(round(match[0], 1) if match else None)
    labels = collections.Counter(
        "no" if r["already_in_catalog"] == "no" else r["already_in_catalog"].split(":")[0].lower()
        for r in table
    )
    return {
        "graph_probes": probes,
        # The length of the research probe script's target list
        # (mapillary/probe.py, CANDS) -- a constant, not read from any output.
        "graph_probes_planned": 44,
        "graph_probes_sent": len(probes),
        "web_table_rows": len(table),
        "web_table_catalog_label": dict(sorted(labels.items())),
        "web_table_within_25km_recomputed": sum(d is not None for d in distances),
        "web_table_beyond_25km_recomputed": sum(d is None for d in distances),
        "web_table_confidence": dict(
            sorted(collections.Counter(r["confidence"] for r in table).items())
        ),
        "web_table_api_verified": sum(r["api_verification"].startswith("HTTP 200") for r in table),
    }


def build(raw_dir: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    paths = {
        "hexes": os.path.join(raw_dir, "panoramax", "hexes.csv"),
        "requests_log": os.path.join(raw_dir, "panoramax", "requests.log"),
        "catalog": os.path.join(raw_dir, "prod_catalog_cities.csv"),
        "mapillary_probes": os.path.join(raw_dir, "mapillary", "probe_results.jsonl"),
        "mapillary_table": os.path.join(raw_dir, "mapillary", "candidates.csv"),
        "geonames": GEONAMES,
    }
    hexes = load_hexes(paths["hexes"])
    catalog = load_catalog(paths["catalog"])
    places = load_geonames()
    ranked = place_bounds(places, HexIndex(hexes))
    rows = [summarize_cluster(c, catalog) for c in cluster_places(ranked)]
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    new = [r for r in rows if r["in_catalog"] == "no"]
    tracked = [r for r in rows if r["in_catalog"] == "yes"]
    candidates = [r for r in new if r["candidate"] == "yes"]
    with open(paths["mapillary_table"], newline="") as f:
        mapillary_table = list(csv.DictReader(f))

    def brief(r: dict[str, Any]) -> dict[str, Any]:
        keys = ("rank", "name", "admin1", "cc", "ub_360_10km", "share_360", "max_hex_360")
        out = {k: r[k] for k in keys}
        out["newest_hex_date"] = r["newest_hex_date"]
        if r["in_catalog"] == "yes":
            out.update({k: r[k] for k in ("catalog_city_id", "catalog_enabled", "catalog_km")})
        return out

    metrics = {
        "topic": TOPIC,
        "issue": 406,
        "generated_by": "python scripts/panoramax_world_screen_analyze.py --docs-dir docs/experiments",
        "collected_by": (
            "experiments/candidate-360-cities-2026-10-01/panoramax/screen_regions.py --execute "
            "(research script, gitignored; reproduced as scripts/panoramax_world_screen_collect.py)"
        ),
        "raw_dir": "experiments/candidate-360-cities-2026-10-01 (gitignored)",
        "inputs": {
            name: {
                # Relative to the raw dir (or the repo, for GeoNames), never an
                # absolute path: the record must not name one machine's layout.
                "file": os.path.relpath(path, REPO if name == "geonames" else raw_dir),
                "sha256": sha256(path),
            }
            for name, path in paths.items()
        },
        "parameters": {
            "zoom": 6,
            "place_radius_km": PLACE_RADIUS_KM,
            "min_place_360": MIN_PLACE_360,
            "cluster_radius_km": CLUSTER_RADIUS_KM,
            "catalog_radius_km": CATALOG_RADIUS_KM,
            "candidate_min_360": CANDIDATE_MIN_360,
            "candidate_min_360_us_ca": CANDIDATE_MIN_360_NA,
        },
        "catalog_snapshot": {
            "n_cities": len(catalog),
            "enabled": sum(c["enabled"] == "1" for c in catalog),
        },
        "requests": summarize_requests(load_jsonl(paths["requests_log"])),
        "hexes": summarize_hexes(hexes),
        "places": {
            "n_geonames": len(places),
            "n_with_bound_ge_min": len(ranked),
            "n_new": sum(nearest_catalog(p["lat"], p["lon"], catalog) is None for p in ranked),
            # Double counting, measured: the place bounds summed against the
            # hexagons they were summed from. Neighbouring places share hexes.
            "sum_of_place_bounds_360": sum(p["ub_360"] for p in ranked),
            "ratio_to_screen_360_total": round(
                sum(p["ub_360"] for p in ranked) / max(1, sum(h["nb_360_pictures"] for h in hexes)),
                2,
            ),
        },
        "clusters": {
            "n": len(rows),
            "n_new": len(new),
            "n_tracked": len(tracked),
            "n_tracked_disabled": sum(r["catalog_enabled"] == "0" for r in tracked),
            "n_new_candidates": len(candidates),
            "n_new_candidates_us_ca": sum(r["cc"] in NORTH_AMERICA for r in candidates),
            "members_per_cluster": describe([r["n_members"] for r in rows], digits=1),
            "ub_360_new": describe([r["ub_360_10km"] for r in new], digits=0),
            "ub_360_tracked": describe([r["ub_360_10km"] for r in tracked], digits=0),
            "ub_360_new_us_ca": describe(
                [r["ub_360_10km"] for r in new if r["cc"] in NORTH_AMERICA], digits=0
            ),
            "share_360_new": describe(
                [r["share_360"] for r in new if r["share_360"] is not None], digits=3
            ),
            "new_by_country": dict(collections.Counter(r["cc"] for r in candidates).most_common()),
        },
        "top_new": [brief(r) for r in new[:TOP_N]],
        "top_new_us_ca": [brief(r) for r in new if r["cc"] in NORTH_AMERICA][:TOP_N],
        "tracked": [brief(r) for r in tracked],
        "mapillary": summarize_mapillary(
            load_jsonl(paths["mapillary_probes"]), mapillary_table, catalog
        ),
    }
    return metrics, rows


def write(metrics: dict[str, Any], rows: list[dict[str, Any]], docs_dir: str) -> None:
    with open(os.path.join(docs_dir, f"{TOPIC}_metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(docs_dir, f"{TOPIC}_clusters.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CLUSTER_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    parser.add_argument("--docs-dir", default=os.path.join(REPO, "docs", "experiments"))
    args = parser.parse_args(argv)
    metrics, rows = build(args.raw_dir)
    write(metrics, rows, args.docs_dir)
    c = metrics["clusters"]
    print(
        f"{c['n']} clusters: {c['n_new']} new ({c['n_new_candidates']} candidates), {c['n_tracked']} tracked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
