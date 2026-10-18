#!/usr/bin/env python3
"""
Write the GSV capture-history crawler's work list for a city's latest road walk (#442).

Runs on the catalog host, offline: it reads the catalog, the walk's CSV and the
FROZEN network GraphML, and makes no request. A city whose GraphML is missing
is refused rather than fetched -- this is a place that could reach Overpass,
and it must not. The work list is what gets copied to the dedicated crawler
host, which holds no catalog (see docs/capture-dates.md).

Output, under --out-dir (default: the experiments/ sibling of data/, which is
gitignored and never published):
    <walk csv stem>.worklist.csv.gz   one row per sample point (WORKLIST_COLUMNS)
    <walk csv stem>.worklist.json     provenance and the pass split

Usage:
    python scripts/build_gsv_history_worklist.py --city "Seattle, WA"
    python scripts/build_gsv_history_worklist.py --city "Seattle, WA" --limit 200   # pilot: first 200 edges
    python scripts/build_gsv_history_worklist.py --all
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

import osmnx as ox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.download_common import positive_int  # noqa: E402
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.naming import network_cache_path  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_street_analyzer.download_street_network import graph_to_edges  # noqa: E402
from streetscape_street_analyzer.history_worklist import build_worklist  # noqa: E402

PROVIDER = "gsv"


def default_out_dir(data_dir: str) -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(data_dir)), "experiments", "gsv-history", "worklists"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--city", action="append", help="city query or slug (repeatable)")
    who.add_argument("--all", action="store_true", help="every city with a GSV walk")
    p.add_argument("--data-dir", default=get_default_data_dir())
    p.add_argument("--db-path", default=None, help="default: <data-dir>/streetscape_tracker.db")
    p.add_argument(
        "--out-dir", default=None, help="default: <data-dir>/../experiments/gsv-history/worklists"
    )
    p.add_argument("--network-type", default="drive")
    p.add_argument(
        "--limit", type=positive_int, default=None, help="keep only the first N edges (whole)"
    )
    return p


def build_city(conn, city_id: str, args, out_dir: str) -> int:
    """Write one city's work list; returns 0, or 1 when it was refused."""
    walk = db.get_latest_street_walk(conn, city_id, PROVIDER, args.network_type)
    if walk is None:
        print(f"{city_id}: no {PROVIDER} {args.network_type} walk in the catalog; skipped")
        return 1
    if walk["spacing_m"] is None:
        print(f"{city_id}: walk {walk['csv_filename']} has no spacing_m; refused", file=sys.stderr)
        return 1
    graphml = network_cache_path(city_id, args.data_dir, args.network_type)
    if not os.path.isfile(graphml):
        print(
            f"{city_id}: no frozen {args.network_type} network at {graphml}; refusing rather "
            "than fetching it from Overpass",
            file=sys.stderr,
        )
        return 1
    csv_path = os.path.join(args.data_dir, walk["csv_filename"])
    if not os.path.isfile(csv_path):
        print(f"{city_id}: walk file {csv_path} is missing; refused", file=sys.stderr)
        return 1

    edges = graph_to_edges(ox.load_graphml(graphml))
    collected = load_city_csv_file(csv_path)
    worklist, stats = build_worklist(
        edges,
        collected,
        city_id=city_id,
        walk_csv_filename=walk["csv_filename"],
        network_type=args.network_type,
        spacing_m=float(walk["spacing_m"]),
        limit=args.limit,
    )
    stem = walk["csv_filename"].removesuffix(".csv.gz")
    os.makedirs(out_dir, exist_ok=True)
    csv_out = os.path.join(out_dir, f"{stem}.worklist.csv.gz")
    worklist.to_csv(csv_out, index=False, compression="gzip")
    sidecar = {
        "city_id": city_id,
        "provider": PROVIDER,
        "network_type": args.network_type,
        "walk_run_date": walk["run_date"],
        "walk_csv_filename": walk["csv_filename"],
        "spacing_m": float(walk["spacing_m"]),
        "limit": args.limit,
        "built_at": datetime.now(UTC).isoformat(),
        "worklist": os.path.basename(csv_out),
        **stats,
    }
    with open(os.path.join(out_dir, f"{stem}.worklist.json"), "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2)
    print(
        f"{city_id}: {stats['edges']} edges, {stats['samples']} samples "
        f"(pass 1: {stats['pass1_rows']}, pass 2: {stats['pass2_rows']}), "
        f"{stats['anchored']} anchored ({stats['anchored_share']}), "
        f"{stats['coincident_dropped']} coincident dropped, "
        f"{stats['unmatched_samples']} without a collected row -> {csv_out}"
    )
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    if not os.path.isfile(db_path):
        print(f"no catalog at {db_path}", file=sys.stderr)
        return 2
    out_dir = args.out_dir or default_out_dir(args.data_dir)
    conn = db.connect(db_path)
    if args.all:
        city_ids = [
            r["city_id"]
            for r in db.get_latest_street_walks_all(conn)
            if r["provider"] == PROVIDER and r["network_type"] == args.network_type
        ]
    else:
        city_ids = []
        for query in args.city:
            city = db.resolve_city(conn, query)
            if city is None:
                print(f"{query!r} is not a registered city", file=sys.stderr)
                return 2
            city_ids.append(city.city_id)
    failures = sum(build_city(conn, cid, args, out_dir) for cid in city_ids)
    print(f"{len(city_ids) - failures} of {len(city_ids)} work lists written")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
