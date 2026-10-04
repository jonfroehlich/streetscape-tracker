#!/usr/bin/env python3
"""
Is there recent Mapillary 360-degree imagery at each candidate city's centre?
One small Graph API request per candidate, paced, dry-run by default.

    python scripts/mapillary_candidate_probe.py candidates.csv                  # the plan; no request
    python scripts/mapillary_candidate_probe.py candidates.csv --execute --out probe.csv

An OPERATOR tool for a laptop (issue #406, acceptance item 4): the re-run of
the 2026-10-01 research pass's Graph API probe over the Mapillary candidates it
never reached. It is never run by the scheduler, and it refuses a ``makelab*``
host unless ``--allow-collection-host`` (a per-IP refusal there takes out the
nightly batch; docs/provider-access.md).

INPUT
-----
A CSV with at least ``name``, ``lat`` and ``lon`` columns (other columns are
ignored), one candidate per row, probed in file order.

THE REQUEST
-----------
One request per candidate, never paginated::

    GET https://graph.mapillary.com/images
        ?bbox=minLon,minLat,maxLon,maxLat     (2 x 2 km around the point)
        &fields=id,captured_at,creator_id,is_pano&limit=200
    Authorization: OAuth <MAPILLARY_ACCESS_TOKEN>

``limit`` is at most 200. The research pass asked for 2,000 with six fields and
got HTTP 500 "Please reduce the amount of data you're asking for" on its
second request (docs/experiments/panoramax-world-screen.md, finding 6), so the
documented page cap is not a safe request size. A candidate whose answer fills
the limit is reported as ``capped``: its counts are a FLOOR.

The client, the pacer, the redirect handling and the token-in-a-header rule
are ``scripts/mapillary_user_activity.py``'s, imported rather than copied. The
tile CDN (``tiles.mapillary.com``, the nightly census's per-IP-blocked host) is
never touched.

PACING AND STOPPING
-------------------
Single-threaded, at least ``--min-interval`` seconds (floor and default 3.0)
between request starts, jitter only ever lengthening a gap. NO retries: the
first response that is not a 200 JSON body stops the run, whatever it is --
a 3xx or an HTML page (how Mapillary presents a per-IP block) exits 75, any
other status or a transport error exits 1. Candidates already answered are
still written.

OUTPUT
------
``--out`` gets one CSV row per candidate answered (``RESULT_COLUMNS``), and
``--request-log`` (default ``<out>.requests.jsonl``) one JSON line per request
sent, flushed as it is written: UTC start time, candidate, status, bytes,
latency, images returned. ``--out`` must not exist, so a run never overwrites
an earlier one.

EXIT STATUS: 0 every candidate answered; 1 a non-200 or transport error;
64 usage error or missing token; 75 the provider refused this IP.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from scripts.mapillary_user_activity import (  # noqa: E402
    BLOCKED_EXIT,
    EXIT_ERROR,
    EXIT_OK,
    GRAPH_HOST,
    GRAPH_IMAGES_URL,
    KM_PER_DEG_LAT,
    USAGE_EXIT,
    Fetch,
    Pacer,
    TransientError,
    _api_error_message,
    _UsageParser,
    make_requests_fetch,
    refuse_on_collection_host,
)

logger = logging.getLogger("mapillary_candidate_probe")

FIELDS = "id,captured_at,creator_id,is_pano"
MAX_LIMIT = 200  # never more: 2,000 drew an HTTP 500 for payload size (#406)
BOX_KM = 2.0
MIN_INTERVAL_FLOOR_S = 3.0

RESULT_COLUMNS = [
    "name",
    "lat",
    "lon",
    "status",
    "images",
    "capped",
    "panos",
    "pano_share",
    "creators",
    "top_creator_id",
    "top_creator_share",
    "oldest_captured_utc",
    "newest_captured_utc",
]


class ProbeStop(Exception):
    """The run must stop here; ``exit_code`` says how."""

    def __init__(self, message: str, exit_code: int, status: int | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.status = status


def bbox_around(lat: float, lon: float, box_km: float = BOX_KM) -> str:
    """``minLon,minLat,maxLon,maxLat`` of a ``box_km`` square centred on the point."""
    half_lat = (box_km / 2) / KM_PER_DEG_LAT
    half_lon = (box_km / 2) / (KM_PER_DEG_LAT * math.cos(math.radians(lat)))
    return f"{lon - half_lon:.6f},{lat - half_lat:.6f},{lon + half_lon:.6f},{lat + half_lat:.6f}"


def request_params(lat: float, lon: float, limit: int) -> dict[str, Any]:
    return {"bbox": bbox_around(lat, lon), "fields": FIELDS, "limit": limit}


def load_candidates(path: str) -> list[dict[str, Any]]:
    """The candidate rows, validated: a bad row is a usage error, not a request."""
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"name", "lat", "lon"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing column(s) {sorted(missing)}")
        rows = []
        for n, row in enumerate(reader, 2):
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
            except (TypeError, ValueError):
                raise ValueError(f"{path}:{n}: lat/lon not numbers") from None
            if not (-85 <= lat <= 85 and -180 <= lon <= 180):
                raise ValueError(f"{path}:{n}: lat/lon out of range")
            rows.append({"name": row["name"], "lat": lat, "lon": lon})
    if not rows:
        raise ValueError(f"{path}: no candidates")
    return rows


def _iso_date(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def summarize(candidate: dict[str, Any], images: list[dict[str, Any]], limit: int) -> dict:
    """One result row from one answered request."""
    panos = sum(1 for img in images if img.get("is_pano") is True)
    creators = Counter(str(img.get("creator_id")) for img in images if img.get("creator_id"))
    top_id, top_n = creators.most_common(1)[0] if creators else ("", 0)
    dates = sorted(d for d in (_iso_date(img.get("captured_at")) for img in images) if d)
    return {
        "name": candidate["name"],
        "lat": candidate["lat"],
        "lon": candidate["lon"],
        "status": 200,
        "images": len(images),
        "capped": len(images) >= limit,
        "panos": panos,
        "pano_share": round(panos / len(images), 3) if images else "",
        "creators": len(creators),
        "top_creator_id": top_id,
        "top_creator_share": round(top_n / len(images), 3) if images else "",
        "oldest_captured_utc": dates[0] if dates else "",
        "newest_captured_utc": dates[-1] if dates else "",
    }


def probe_one(
    candidate: dict[str, Any],
    *,
    fetch: Fetch,
    pacer: Pacer,
    limit: int,
    log: Callable[[dict[str, Any]], None],
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """
    Exactly one request for one candidate. Returns its result row, or raises
    ProbeStop for ANY answer that is not a 200 JSON body — never a retry.
    """
    pacer.wait()
    started = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = clock()
    entry: dict[str, Any] = {"utc": started, "name": candidate["name"]}
    try:
        res = fetch(GRAPH_IMAGES_URL, request_params(candidate["lat"], candidate["lon"], limit))
    except TransientError as exc:
        log({**entry, "status": None, "error": str(exc), "latency_s": round(clock() - t0, 3)})
        raise ProbeStop(f"transport error ({exc}); stopping, no retry", EXIT_ERROR) from None
    entry.update(status=res.status, bytes=len(res.body), latency_s=round(clock() - t0, 3))

    if 300 <= res.status < 400 or (res.status == 200 and "html" in res.content_type.lower()):
        log(entry)
        raise ProbeStop(
            f"HTTP {res.status} ({res.content_type or 'no content type'}) from {GRAPH_HOST} -- "
            f"how Mapillary presents a per-IP block. Stop, and do not re-run from this IP "
            f"for several hours.",
            BLOCKED_EXIT,
            res.status,
        )
    if res.status != 200:
        log(entry)
        raise ProbeStop(
            f"HTTP {res.status} from {GRAPH_HOST}: {_api_error_message(res.body)}",
            EXIT_ERROR,
            res.status,
        )
    try:
        images = json.loads(res.body).get("data") or []
    except (ValueError, AttributeError):
        log(entry)
        raise ProbeStop(f"unparseable JSON from {GRAPH_HOST}", EXIT_ERROR, res.status) from None
    entry["images"] = len(images)
    log(entry)
    return summarize(candidate, images, limit)


def run(
    candidates: list[dict[str, Any]],
    *,
    fetch: Fetch,
    pacer: Pacer,
    limit: int,
    out_path: str,
    log_path: str,
) -> int:
    """Probe every candidate in order, stopping at the first non-200."""
    results: list[dict[str, Any]] = []
    status = EXIT_OK
    with open(log_path, "a", encoding="utf-8") as log_fh:

        def log(entry: dict[str, Any]) -> None:
            log_fh.write(json.dumps(entry) + "\n")
            log_fh.flush()

        for i, candidate in enumerate(candidates, 1):
            try:
                results.append(probe_one(candidate, fetch=fetch, pacer=pacer, limit=limit, log=log))
            except ProbeStop as stop:
                label = "BLOCKED" if stop.exit_code == BLOCKED_EXIT else "STOPPED"
                print(
                    f"{label} at candidate {i}/{len(candidates)} ({candidate['name']}): {stop}",
                    file=sys.stderr,
                )
                status = stop.exit_code
                break
            r = results[-1]
            print(
                f"[{i}/{len(candidates)}] {candidate['name']}: {r['images']} images"
                f"{' (capped: a floor)' if r['capped'] else ''}, {r['panos']} panos, "
                f"newest {r['newest_captured_utc'] or '-'}",
                file=sys.stderr,
            )

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(results)
    print(f"{len(results)} of {len(candidates)} candidates answered -> {out_path}", file=sys.stderr)
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = _UsageParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("candidates", help="CSV with name, lat, lon columns")
    p.add_argument(
        "--execute", action="store_true", help="send the requests (default: print the plan only)"
    )
    p.add_argument(
        "--out", default=None, help="results CSV (required with --execute; must not exist)"
    )
    p.add_argument(
        "--request-log", default=None, help="JSONL request log (default: <out>.requests.jsonl)"
    )
    p.add_argument(
        "--limit",
        type=int,
        default=MAX_LIMIT,
        help=f"images per request, at most {MAX_LIMIT} (default: %(default)s)",
    )
    p.add_argument(
        "--min-interval",
        type=float,
        default=MIN_INTERVAL_FLOOR_S,
        help=f"seconds between request starts, at least {MIN_INTERVAL_FLOOR_S} "
        "(default: %(default)s)",
    )
    p.add_argument(
        "--allow-collection-host",
        action="store_true",
        help="run even on a makelab* host (the nightly batch's IP); think twice",
    )
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv)
    if not 1 <= args.limit <= MAX_LIMIT:
        p.error(f"--limit must be between 1 and {MAX_LIMIT}")
    if not (args.min_interval >= MIN_INTERVAL_FLOOR_S and math.isfinite(args.min_interval)):
        p.error(f"--min-interval must be at least {MIN_INTERVAL_FLOOR_S} s")
    if args.execute and not args.out:
        p.error("--execute needs --out")
    if args.out and os.path.exists(args.out):
        p.error(f"--out {args.out} already exists; a probe never overwrites an earlier one")
    if args.request_log is None and args.out:
        args.request_log = f"{args.out}.requests.jsonl"
    return args


def print_plan(candidates: list[dict[str, Any]], args: argparse.Namespace) -> None:
    print(
        f"DRY RUN: {len(candidates)} request(s) to {GRAPH_IMAGES_URL}, one per candidate, "
        f"limit={args.limit}, fields={FIELDS}, >= {args.min_interval:g} s apart "
        f"(>= {len(candidates) * args.min_interval / 60:.1f} min). Nothing sent; "
        f"pass --execute --out PATH."
    )
    for i, c in enumerate(candidates, 1):
        print(f"  [{i}] {c['name']}: bbox={bbox_around(c['lat'], c['lon'])}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    try:
        candidates = load_candidates(args.candidates)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return USAGE_EXIT
    if not args.execute:
        print_plan(candidates, args)
        return EXIT_OK
    refuse_on_collection_host(args.allow_collection_host)

    # The repo convention, as mapillary_user_activity.main loads it.
    from dotenv import find_dotenv, load_dotenv

    from streetscape_metadata_tracker import config as cfg

    load_dotenv()
    cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    try:
        token = cfg.load_config("mapillary")["access_token"]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_EXIT
    return run(
        candidates,
        fetch=make_requests_fetch(token),
        pacer=Pacer(args.min_interval),
        limit=args.limit,
        out_path=args.out,
        log_path=args.request_log,
    )


if __name__ == "__main__":
    sys.exit(main())
