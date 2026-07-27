"""
One-shot boundary audit of every city's frozen search rectangle
(issue #91, step 1).

Grid geometry is frozen at registration, so a rectangle inferred from a bad
Nominatim geocode (an admin region, a township, a neighborhood — e.g.
"Bancroft, SD" registered as Le Sueur Township, MN) is locked into every
future run and can only be fixed at the cost of diff continuity. This audit
re-geocodes each city with a structured query + polygon GeoJSON, compares
the OSM boundary to the frozen rectangle, and writes a CSV for human review
**before** the scheduler starts accumulating quarterly history. It never
writes the catalog; re-registering flagged cities is a separate, reviewed
step.

Raw Nominatim responses are cached in an append-only JSONL (one line per
city, last record wins, flushed per line) so the ~21-minute full fetch
(1143 cities at Nominatim's 1 req/sec) is Ctrl-C-safe and resumable, and so
issue #91 step 2 (store polygons in the catalog) can reuse them without
re-fetching. Artifacts live in the gitignored audit/ directory — data/ is
rsynced to a public server and polygon GeoJSON for 1143 cities is large.

Usage:
    python scripts/audit_city_boundaries.py --limit 5      # smoke test
    python scripts/audit_city_boundaries.py                # full audit
    python scripts/audit_city_boundaries.py --no-fetch     # re-classify from
                                                           # cache (offline)
    python scripts/audit_city_boundaries.py --city seattle--washington--united-states
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import asdict, fields
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geopy.exc import GeocoderServiceError  # noqa: E402

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.boundary_audit import (  # noqa: E402
    AuditResult,
    OsmBoundary,
    Thresholds,
    classify,
    parse_osm_result,
)
from streetscape_metadata_tracker.geoutils import geocode_boundary_raw  # noqa: E402
from streetscape_metadata_tracker.paths import (  # noqa: E402
    get_default_data_dir,
    get_project_root,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Report verdicts in review-priority order
VERDICT_ORDER = [
    "WRONG_PLACE",
    "UNDER",
    "OVER",
    "BBOX_SUSPECT",
    "NO_POLYGON",
    "NOT_FOUND",
    "NOT_FETCHED",
    "OK",
]


def load_cache(path: str) -> dict[str, OsmBoundary | None]:
    """
    Stream the JSONL cache into {city_id: OsmBoundary | None}.

    Parsed immediately and the raw geojson discarded so a multi-hundred-MB
    cache never fully materializes in memory. None means Nominatim found
    nothing (a stable answer, distinct from "not yet fetched").
    """
    cache: dict[str, OsmBoundary | None] = {}
    if not os.path.exists(path):
        return cache
    with open(path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                cache[rec["city_id"]] = parse_osm_result(rec["raw"]) if rec.get("found") else None
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.warning(f"Skipping bad cache line {line_num}: {e}")
    return cache


def fetch_missing(cities, cache, cache_path: str, limit: int | None) -> int:
    """
    Geocode cities absent from the cache, appending one flushed JSONL line
    per result. Transient geocoder errors are logged and NOT cached, so a
    re-run retries them. Returns the number of fetch errors.
    """
    todo = [c for c in cities if c.city_id not in cache]
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        print("Fetch: everything already cached")
        return 0

    print(
        f"Fetch: {len(todo)} cities to geocode "
        f"(~{len(todo) * 1.1 / 60:.0f} min at 1 req/sec; resumable)"
    )
    n_errors = 0
    start = time.monotonic()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "a", encoding="utf-8") as f:
        for i, city in enumerate(todo, 1):
            try:
                raw = geocode_boundary_raw(city.city_name, city.state_name, city.country_name)
            except GeocoderServiceError as e:
                n_errors += 1
                logger.warning(f"[FETCH_ERROR] {city.city_id}: {e}")
                continue
            f.write(
                json.dumps(
                    {
                        "city_id": city.city_id,
                        "query": {
                            "city": city.city_name,
                            "state": city.state_name,
                            "country": city.country_name,
                        },
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "found": raw is not None,
                        "raw": raw,
                    }
                )
                + "\n"
            )
            f.flush()
            cache[city.city_id] = parse_osm_result(raw) if raw is not None else None
            if i % 25 == 0 or i == len(todo):
                rate = (time.monotonic() - start) / i
                eta_min = rate * (len(todo) - i) / 60
                print(f"  {i}/{len(todo)} fetched, ETA {eta_min:.0f} min")
    return n_errors


def write_report(rows, report_path: str) -> None:
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    result_fields = [f.name for f in fields(AuditResult)]
    header = (
        [
            "city_id",
            "display_name",
            "frozen_center_lat",
            "frozen_center_lon",
            "frozen_width_m",
            "frozen_height_m",
            "frozen_area_km2",
        ]
        + result_fields
        + ["notes"]
    )
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for city, res in rows:
            r = asdict(res)
            writer.writerow(
                [
                    city.city_id,
                    city.display_name,
                    city.center_lat,
                    city.center_lon,
                    city.grid_width_m,
                    city.grid_height_m,
                    city.grid_width_m * city.grid_height_m / 1e6,
                ]
                + [r[name] for name in result_fields]
                + [city.notes]
            )


def print_summary(rows, top: int) -> None:
    by_verdict = {v: [] for v in VERDICT_ORDER}
    for city, res in rows:
        by_verdict.setdefault(res.verdict, []).append((city, res))

    def flagged_line(city, res):
        frozen = f"{city.grid_width_m}x{city.grid_height_m}m"
        osm = (
            f"{res.osm_bbox_width_m:.0f}x{res.osm_bbox_height_m:.0f}m"
            if res.osm_bbox_width_m is not None
            else "?"
        )
        extra = ""
        if res.verdict == "WRONG_PLACE" and res.center_dist_km is not None:
            extra = f" center {res.center_dist_km:.0f} km away"
        elif res.verdict == "UNDER" and res.bbox_coverage_frac is not None:
            extra = f" coverage={res.bbox_coverage_frac:.2f}"
        elif res.verdict == "OVER" and res.over_area_ratio is not None:
            extra = f" area ratio={res.over_area_ratio:.1f}x"
        name = res.osm_display_name or ""
        return (
            f'  [{res.verdict}] {city.city_id}: frozen {frozen}, OSM bbox {osm}{extra} — "{name}"'
        )

    for verdict in VERDICT_ORDER:
        group = by_verdict.get(verdict, [])
        if not group or verdict == "OK":
            continue
        # Sort worst-first within each verdict
        if verdict == "UNDER":
            group.sort(key=lambda cr: cr[1].bbox_coverage_frac or 0)
        elif verdict == "OVER":
            group.sort(key=lambda cr: -(cr[1].over_area_ratio or 0))
        elif verdict == "WRONG_PLACE":
            group.sort(key=lambda cr: -(cr[1].center_dist_km or 0))
        print(f"\n{verdict} ({len(group)}):")
        for city, res in group[:top]:
            print(flagged_line(city, res))
        if len(group) > top:
            print(f"  ... and {len(group) - top} more (see CSV)")

    print("\nSummary:")
    for verdict in VERDICT_ORDER:
        n = len(by_verdict.get(verdict, []))
        if n:
            print(f"  {verdict:13s} {n}")


def main() -> int:
    audit_dir = os.path.join(get_project_root(), "audit")
    parser = argparse.ArgumentParser(
        description="Audit frozen search rectangles against OSM boundaries "
        "(issue #91, step 1). Read-only on the catalog.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--cache", default=os.path.join(audit_dir, "nominatim_boundary_cache.jsonl")
    )
    parser.add_argument("--report", default=os.path.join(audit_dir, "boundary_audit_report.csv"))
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Classify from cache only (no network) — for "
        "iterating on thresholds after a full fetch",
    )
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="Re-query cached cities too (appends; the newest record wins)",
    )
    parser.add_argument(
        "--city", action="append", default=None, help="Restrict to this city_id (repeatable)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Fetch at most N uncached cities (smoke test)"
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=0.75,
        help="UNDER when the frozen rectangle covers less "
        "than this fraction of the OSM bbox "
        "(default 0.75)",
    )
    parser.add_argument(
        "--over-ratio",
        type=float,
        default=4.0,
        help="OVER when frozen area exceeds this multiple of the OSM bbox area (default 4.0)",
    )
    parser.add_argument(
        "--top", type=int, default=15, help="Worst offenders printed per verdict (default 15)"
    )
    args = parser.parse_args()

    conn = db.connect(db.get_default_db_path(args.data_dir))
    # Disabled cities included on purpose: sampling-frame cities register with
    # enabled=0 precisely so this audit can vet them before collection (#110).
    cities = db.get_all_cities(conn)
    if args.city:
        wanted = set(args.city)
        cities = [c for c in cities if c.city_id in wanted]
        missing = wanted - {c.city_id for c in cities}
        if missing:
            print(f"Unknown city_ids: {sorted(missing)}")
            return 1
    print(f"Auditing {len(cities)} cities")

    cache = load_cache(args.cache)
    if args.refetch:
        for c in cities:
            cache.pop(c.city_id, None)
    n_errors = 0
    if not args.no_fetch:
        n_errors = fetch_missing(cities, cache, args.cache, args.limit)

    thresholds = Thresholds(min_coverage=args.min_coverage, over_area_ratio=args.over_ratio)
    rows = []
    for city in cities:
        if city.city_id in cache:
            rows.append((city, classify(city, cache[city.city_id], thresholds)))
        else:
            rows.append((city, AuditResult(verdict="NOT_FETCHED")))

    write_report(rows, args.report)
    print_summary(rows, args.top)
    print(f"\nReport: {args.report}")
    print(f"Cache:  {args.cache}")
    if n_errors:
        print(f"{n_errors} fetch errors (not cached — re-run to retry)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
