#!/usr/bin/env python3
"""
Collection half of the Mapillary discovery screen (issue #383).

    # z6 sequence tiles over the regions -> <out>/sequences.parquet
    python scripts/mapillary_discovery_collect.py scan --out-dir experiments/mapillary-discovery-383

    # recall of z6 against finer zooms around known towns -> <out>/calibration.json
    python scripts/mapillary_discovery_collect.py calibrate --out-dir experiments/mapillary-discovery-383 \\
        --zooms 6,10 --town laurens--iowa=42.8468,-94.8515 ...

    # read-only catalog export, run where the catalog lives (production)
    python scripts/mapillary_discovery_collect.py catalog-snapshot --db data/streetscape_tracker.db --out prod_snapshot.csv

    # creator_id -> username, one Graph API image lookup per creator -> <out>/creators.json
    python scripts/mapillary_discovery_collect.py resolve-creators --out-dir ... --ids-from <csv> ...

Network discipline, decided after reading the Mapillary docs and forum
(docs/provider-access.md): tiles.mapillary.com's binding limit is an
undocumented per-IP throttle that answers HTTP 302 -> login, so

  * every tile is cached under <out>/tiles/{z}_{x}_{y}.mvt and a cached tile
    costs nothing -- re-running any subcommand over the same tiles makes 0
    requests, which is how the committed numbers are regenerated;
  * requests are serial, paced at the production channels' jittered 40/min,
    under the machine-wide Mapillary tile host lock;
  * the FIRST response that is not 200/204 stops the run (no retry), and
    every request is appended to <out>/request_log.jsonl;
  * ``--max-requests`` bounds a run.

Graph API lookups (resolve-creators) are 3 s apart, same stop rule.
The access token comes from MAPILLARY_ACCESS_TOKEN (or .env) and is never
written to any output.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mapillary_discovery_common import (  # noqa: E402
    REGIONS,
    SCREEN_ZOOM,
    TILE_URL_TEMPLATE,
    decode_sequences,
    jittered_gap,
    local_km,
    region_tiles,
)
from streetscape_metadata_tracker.download_common import (  # noqa: E402
    HOST_MAPILLARY_TILES,
    lonlat_to_tile_frac,
)
from streetscape_metadata_tracker.host_lock import host_lock  # noqa: E402

USER_AGENT = "streetscape-tracker research (UW Makeability Lab; Mapillary discovery screen #383)"
GRAPH_URL = "https://graph.mapillary.com/{image_id}"
GRAPH_GAP_S = 3.0


class StopProbe(RuntimeError):
    """A response that ends the run: a block, an auth failure, or the request cap."""


def load_token() -> str:
    tok = os.environ.get("MAPILLARY_ACCESS_TOKEN")
    if tok:
        return tok
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("MAPILLARY_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("MAPILLARY_ACCESS_TOKEN is not set (environment or .env)")


class TileFetcher:
    """Cached, paced, logged, stop-on-first-refusal tile fetcher."""

    def __init__(self, out_dir: Path, max_requests: int, seed: int | None = None):
        self.tile_dir = out_dir / "tiles"
        self.tile_dir.mkdir(parents=True, exist_ok=True)
        self.log = out_dir / "request_log.jsonl"
        self.max_requests = max_requests
        self.requests = 0
        self.cache_hits = 0
        self.rng = random.Random(seed)
        self._last = 0.0
        self._token: str | None = None
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def get(self, z: int, x: int, y: int) -> bytes:
        path = self.tile_dir / f"{z}_{x}_{y}.mvt"
        if path.exists():
            self.cache_hits += 1
            return path.read_bytes()
        if self.requests >= self.max_requests:
            raise StopProbe(f"--max-requests {self.max_requests} reached")
        if self._token is None:
            self._token = load_token()
        wait = self._last + jittered_gap(self.rng) - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = t0 = time.time()
        r = self.session.get(
            TILE_URL_TEMPLATE.format(z=z, x=x, y=y),
            params={"access_token": self._token},
            allow_redirects=False,  # a 302 -> login IS the per-IP block (#199)
            timeout=120,
        )
        self.requests += 1
        ctype = r.headers.get("Content-Type", "")
        entry = {
            "t": round(t0, 3),
            "host": "tiles",
            "z": z,
            "x": x,
            "y": y,
            "status": r.status_code,
            "bytes": len(r.content),
            "ctype": ctype,
            "elapsed_s": round(time.time() - t0, 3),
        }
        with self.log.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        if r.status_code not in (200, 204) or "html" in ctype or "json" in ctype:
            raise StopProbe(f"STOP at {z}/{x}/{y}: HTTP {r.status_code} ({ctype})")
        data = r.content if r.status_code == 200 else b""
        path.write_bytes(data)
        return data


def cmd_scan(args) -> int:
    out = Path(args.out_dir)
    tiles = region_tiles(args.regions, SCREEN_ZOOM)
    print(f"{len(tiles)} z{SCREEN_ZOOM} tiles over {', '.join(args.regions)}", flush=True)
    fetcher = TileFetcher(out, args.max_requests, args.seed)
    rows = []
    stopped = None
    with host_lock(HOST_MAPILLARY_TILES):
        for i, (x, y) in enumerate(tiles, 1):
            try:
                seqs = decode_sequences(fetcher.get(SCREEN_ZOOM, x, y), x, y, SCREEN_ZOOM)
            except StopProbe as e:
                stopped = str(e)
                print(stopped, flush=True)
                break
            for s in seqs:
                rows.append(
                    {
                        "seq": s.get("id"),
                        "tile": f"{x}_{y}",
                        "creator_id": s.get("creator_id"),
                        "organization_id": s.get("organization_id"),
                        "captured_at": s.get("captured_at"),
                        "is_pano": bool(s.get("is_pano")),
                        "foot": s.get("foot"),
                        "image_id": s.get("image_id"),
                        "quality_score": s.get("quality_score"),
                        "pts": [(round(a, 5), round(b, 5)) for a, b in s["_pts"]],
                    }
                )
            print(f"{i}/{len(tiles)} {x},{y} sequences={len(seqs)}", flush=True)
    pd.DataFrame(rows).to_parquet(out / "sequences.parquet")
    manifest = {
        "regions": {n: REGIONS[n] for n in args.regions},
        "zoom": SCREEN_ZOOM,
        "tiles": len(tiles),
        "requests": fetcher.requests,
        "cache_hits": fetcher.cache_hits,
        "stopped": stopped,
        "rows": len(rows),
        "finished_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
    }
    (out / "scan_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(json.dumps(manifest))
    return 1 if stopped else 0


def calibrate_town(fetcher, lat, lon, zoom, r_km, cutoff_ms) -> dict:
    """Sequences touching a disc around (lat, lon), from the one tile holding it."""
    fx, fy = lonlat_to_tile_frac(lon, lat, zoom)
    x, y = int(fx), int(fy)
    seqs = decode_sequences(fetcher.get(zoom, x, y), x, y, zoom)
    hit = [s for s in seqs if any(local_km(a, b, lon, lat) <= r_km for a, b in s["_pts"])]
    pano = [s for s in hit if s.get("is_pano")]
    recent = [s for s in pano if (s.get("captured_at") or 0) >= cutoff_ms]
    by: dict[int, int] = {}
    for s in recent:
        by[s.get("creator_id")] = by.get(s.get("creator_id"), 0) + 1
    newest = max((s["captured_at"] for s in pano), default=None)
    return {
        "zoom": zoom,
        "tile": [x, y],
        "sequences": len(hit),
        "pano": len(pano),
        "pano_recent": len(recent),
        "top_creator": max(by, key=by.get) if by else None,
        "top_creator_sequences": max(by.values()) if by else 0,
        "newest_pano": (pd.Timestamp(newest, unit="ms").date().isoformat() if newest else None),
    }


def cmd_calibrate(args) -> int:
    out = Path(args.out_dir)
    fetcher = TileFetcher(out, args.max_requests, args.seed)
    cutoff_ms = pd.Timestamp(args.recent_since).value // 10**6
    zooms = [int(z) for z in args.zooms.split(",")]
    result = {"r_km": args.r_km, "recent_since": args.recent_since, "towns": {}}
    with host_lock(HOST_MAPILLARY_TILES):
        for spec in args.town:
            name, ll = spec.split("=")
            lat, lon = (float(v) for v in ll.split(","))
            result["towns"][name] = {
                "lat": lat,
                "lon": lon,
                "by_zoom": [
                    calibrate_town(fetcher, lat, lon, z, args.r_km, cutoff_ms) for z in zooms
                ],
            }
    result["requests"] = fetcher.requests
    result["cache_hits"] = fetcher.cache_hits
    (out / "calibration.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0


def cmd_resolve_creators(args) -> int:
    """creator_id -> username via ``GET /{image_id}?fields=creator`` on one of its images."""
    out = Path(args.out_dir)
    cache = out / "creators.json"
    known = json.loads(cache.read_text()) if cache.exists() else {}
    want: list[str] = []
    for spec in args.ids_from:
        path, col, n = spec.split(":")
        ids = pd.read_csv(path)[col].dropna().astype("int64").astype(str).tolist()
        want += ids[: int(n)]
    want = list(dict.fromkeys(want))
    seqs = pd.read_parquet(
        out / "sequences.parquet", columns=["creator_id", "image_id", "is_pano", "captured_at"]
    )
    seqs = (
        seqs[seqs.is_pano].sort_values("captured_at", ascending=False).drop_duplicates("creator_id")
    )
    sample_image = dict(
        zip(seqs.creator_id.astype("int64").astype(str), seqs.image_id, strict=True)
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Authorization": f"OAuth {load_token()}"})
    made = 0
    for cid in want:
        if cid in known or cid not in sample_image:
            continue
        if made >= args.max_requests:
            print(f"--max-requests {args.max_requests} reached")
            break
        time.sleep(GRAPH_GAP_S)
        r = session.get(
            GRAPH_URL.format(image_id=int(sample_image[cid])),
            params={"fields": "creator"},
            timeout=30,
        )
        made += 1
        with (out / "request_log.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {
                        "t": round(time.time(), 3),
                        "host": "graph",
                        "creator_id": cid,
                        "status": r.status_code,
                    }
                )
                + "\n"
            )
        if r.status_code != 200:
            print(f"STOP: HTTP {r.status_code} {r.text[:200]}")
            break
        known[cid] = (r.json().get("creator") or {}).get("username")
        print(cid, known[cid], flush=True)
    cache.write_text(json.dumps(known, indent=1, sort_keys=True) + "\n")
    print(f"{made} Graph API requests; {len(known)} creators known")
    return 0


CATALOG_CITIES_SQL = (
    "select city_id, center_lat, center_lon, enabled, grid_width_m, grid_height_m from cities"
)
# each city's LATEST Mapillary drive walk
CATALOG_WALKS_SQL = """
select city_id, run_date, coverage_pct_by_length, median_covered_age_years, length_km
from street_walks s where provider = 'mapillary' and network_type = 'drive'
and run_date = (select max(run_date) from street_walks t where t.city_id = s.city_id
                and t.provider = 'mapillary' and t.network_type = 'drive')
"""
SNAPSHOT_HEADER = [
    "table",
    "city_id",
    "lat",
    "lon",
    "enabled",
    "grid_width_m",
    "grid_height_m",
    "run_date",
    "coverage_pct_by_length",
    "median_covered_age_years",
    "length_km",
]


def cmd_catalog_snapshot(args) -> int:
    """
    Read-only export of a catalog (run it where the catalog lives): every
    city's centre and frozen grid, and its latest Mapillary drive walk. The
    analysis uses the first to decide "already tracked" and the second to
    validate the score. No network.
    """
    import csv
    import sqlite3

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SNAPSHOT_HEADER)
        for r in conn.execute(CATALOG_CITIES_SQL):
            w.writerow(["cities", *r, "", "", "", ""])
        for r in conn.execute(CATALOG_WALKS_SQL):
            w.writerow(["walk", r[0], "", "", "", "", "", *r[1:]])
    conn.close()
    print(f"wrote {args.out}")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("catalog-snapshot")
    snap.add_argument("--db", required=True, help="catalog path, opened read-only")
    snap.add_argument("--out", required=True)
    for name in ("scan", "calibrate", "resolve-creators"):
        s = sub.add_parser(name)
        s.add_argument(
            "--out-dir",
            required=True,
            help="gitignored raw-output dir, e.g. experiments/mapillary-discovery-383",
        )
        s.add_argument("--max-requests", type=int, default=300)
        s.add_argument("--seed", type=int, default=None, help="pacing RNG seed")
        if name == "scan":
            s.add_argument("--regions", nargs="+", default=list(REGIONS), choices=list(REGIONS))
        if name == "calibrate":
            s.add_argument("--zooms", default="6,10")
            s.add_argument(
                "--town", action="append", required=True, help="name=lat,lon (repeatable)"
            )
            s.add_argument("--r-km", type=float, default=3.0)
            s.add_argument("--recent-since", default="2024-04-01")
        if name == "resolve-creators":
            s.add_argument(
                "--ids-from",
                action="append",
                required=True,
                help="csv_path:column:first_n (repeatable)",
            )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return {
        "scan": cmd_scan,
        "calibrate": cmd_calibrate,
        "resolve-creators": cmd_resolve_creators,
        "catalog-snapshot": cmd_catalog_snapshot,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
