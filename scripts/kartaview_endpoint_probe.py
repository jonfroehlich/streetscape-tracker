"""
Did KartaView's nearby-photos endpoint move, and is the new host safe to sweep?

    python scripts/kartaview_endpoint_probe.py --docs-dir docs/experiments

WHAT THIS MEASURES. On the 2026-10-22 night every KartaView collection failed:
``POST https://kartaview.org/1.0/list/nearby-photos/`` -- the collector's
``NEARBY_PHOTOS_URL`` -- answered ``404 page not found`` (text/plain), while the
same path on the ``api.`` hosts still answered. Before pointing the collector at
a new host, three questions have to be answered by measurement, not assumed:

  1. HOSTS. Which hosts answer the same request, and with the same photos? The
     identical form body goes to the current host and to both ``api.`` hosts.
  2. CREDENTIAL. Is our token validated there? The pacer assumes the documented
     1,000/h AUTHENTICATED tier. A host that answers a bogus and a garbage token
     exactly as it answers the real one is not checking the token, so nothing a
     client can observe says which tier it is being metered at.
  3. TOTAL. Does page 1 still report the circle's total? The sweep sizes its
     paging from ``totalFilteredItems`` (``pages_for_total``); the 2026-08-18
     measurement found it exhaustive and exact (Seattle r=400 ipp=200, union ==
     total, page 7 empty). This re-asks that circle's pages 1, 2 and 7. If the
     total no longer counts the circle, a one-line host change would silently
     read one page of every circle -- a truncated census, published as a real
     coverage number.

THE TOKEN IS NEVER RECORDED. Rows name the token's KIND (none, real, bogus,
garbage); the bogus one is zeros of the real token's length, so a length check
on the server cannot tell them apart. The token travels as a query parameter,
and urllib3 logs full request URLs at DEBUG, so the ``urllib3`` logger is held
at INFO whatever ``--log-level`` says.

COST. 12 requests, paced at the ANONYMOUS 100/hour rather than the 1,000 a
token is documented to buy -- question 2 is whether the token buys anything,
so this probe does not spend on the assumption it is testing. About 7 minutes.
Refuses to run on a makelab* host, as every KartaView probe here does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kartaview_probe import (  # noqa: E402
    REQUESTS_PER_HOUR_ANON,
    HourlyRateLimiter,
    refuse_on_collection_host,
)

from streetscape_metadata_tracker import config as cfg  # noqa: E402

logger = logging.getLogger("kartaview_endpoint_probe")

DOCS_METRICS_NAME = "kartaview-endpoint-move_metrics.json"

PATH = "/1.0/list/nearby-photos/"
HOST_CURRENT = "https://kartaview.org"
HOST_API = "https://api.kartaview.org"
HOST_OSC = "https://api.openstreetcam.org"

# The 2026-08-18 pagination measurement's circle (download_kartaview's module
# docstring, fact 1), so a changed answer is a change in the API, not the place.
SEATTLE = {"lat": 47.6062, "lng": -122.3321}

# (label, host, token kind, radius, page, ipp)
HOST_CASES = [
    ("current host, real token", HOST_CURRENT, "real", 500, 1, 50),
    ("api.kartaview, no token", HOST_API, "none", 500, 1, 50),
    ("api.kartaview, real token", HOST_API, "real", 500, 1, 50),
    ("api.kartaview, bogus token", HOST_API, "bogus", 500, 1, 50),
    ("api.kartaview, garbage token", HOST_API, "garbage", 500, 1, 50),
    ("api.openstreetcam, no token", HOST_OSC, "none", 500, 1, 50),
    ("api.openstreetcam, real token", HOST_OSC, "real", 500, 1, 50),
    ("api.openstreetcam, bogus token", HOST_OSC, "bogus", 500, 1, 50),
]
PAGE_CASES = [
    (f"api.kartaview r=400 ipp=200 page {p}", HOST_API, "real", 400, p, 200) for p in (1, 2, 7)
] + [("api.openstreetcam r=400 ipp=200 page 1", HOST_OSC, "real", 400, 1, 200)]


def token_for(kind: str, real: str | None) -> str | None:
    """The credential a case sends; ``bogus`` matches the real token's length."""
    if kind == "none" or not real:
        return None
    return {"real": real, "bogus": "0" * len(real), "garbage": "notatoken"}[kind]


def unwrap_total(raw: Any) -> int | None:
    """``totalFilteredItems`` arrives as ``['737']``; mirror the collector's unwrap."""
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def ask(
    session: requests.Session,
    limiter: HourlyRateLimiter,
    case: tuple,
    real_token: str | None,
) -> tuple[dict[str, Any], set[int]]:
    """One POST; returns the recorded row and the ids it served."""
    label, host, kind, radius, page, ipp = case
    limiter.acquire()
    token = token_for(kind, real_token)
    row: dict[str, Any] = {
        "case": label,
        "url": host + PATH,
        "token": kind,
        "radius_m": radius,
        "page": page,
        "ipp": ipp,
    }
    t0 = time.monotonic()
    try:
        resp = session.post(
            host + PATH,
            data={**SEATTLE, "radius": radius, "page": page, "ipp": ipp},
            params={"access_token": token} if token else None,
            allow_redirects=False,
            timeout=90,
        )
    except requests.RequestException as e:
        row.update(error=type(e).__name__, seconds=round(time.monotonic() - t0, 1))
        return row, set()
    row["seconds"] = round(time.monotonic() - t0, 1)
    row["http_status"] = resp.status_code
    row["content_type"] = resp.headers.get("Content-Type", "")
    row["rate_limit_headers"] = sorted(
        h for h in resp.headers if "ratelimit" in h.lower() or h.lower() == "retry-after"
    )
    if "json" not in row["content_type"]:
        row["body_head"] = resp.text[:80]
        return row, set()
    body = resp.json()
    status = body.get("status") or {}
    items = body.get("currentPageItems") or []
    ids = {int(i["id"]) for i in items if "id" in i}
    row.update(
        api_code=status.get("apiCode"),
        total_raw=body.get("totalFilteredItems"),
        total=unwrap_total(body.get("totalFilteredItems")),
        items=len(items),
        ids_digest=hashlib.sha256(",".join(map(str, sorted(ids))).encode()).hexdigest()[:12],
    )
    return row, ids


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Probe KartaView's nearby-photos hosts.")
    p.add_argument("--docs-dir", default=None, help="write the metrics record here")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    # urllib3 logs the full request URL at DEBUG, access_token included.
    logging.getLogger("urllib3").setLevel(
        max(logging.INFO, logging.getLogger("urllib3").getEffectiveLevel())
    )

    refuse_on_collection_host()
    load_dotenv()
    cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    real_token = os.environ.get("KARTAVIEW_ACCESS_TOKEN") or None
    if not real_token:
        logger.error("KARTAVIEW_ACCESS_TOKEN is unset; the credential question needs it")
        return 64
    limiter = HourlyRateLimiter(REQUESTS_PER_HOUR_ANON)
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "streetscape-tracker probe (github.com/jonfroehlich/streetscape-tracker)"}
    )

    host_rows: list[dict[str, Any]] = []
    page_rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    complete = False
    try:
        for case in HOST_CASES:
            row, _ = ask(session, limiter, case, real_token)
            host_rows.append(row)
            logger.info(f"{row['case']}: {row.get('http_status', row.get('error'))}")
        for case in PAGE_CASES:
            row, ids = ask(session, limiter, case, real_token)
            if case[1] == HOST_API:
                row["ids_already_seen"] = len(ids & seen)
                seen |= ids
            page_rows.append(row)
            logger.info(f"{row['case']}: total={row.get('total_raw')} items={row.get('items')}")
        complete = True
    except (KeyboardInterrupt, Exception) as e:
        logger.error(f"probe interrupted: {type(e).__name__}: {e}; writing what was collected")

    answered = [r for r in host_rows if r.get("http_status") == 200 and "ids_digest" in r]
    api_pages = [r for r in page_rows if r["url"].startswith(HOST_API)]
    summary = {
        "current_host_status": next(
            (r.get("http_status") for r in host_rows if r["url"].startswith(HOST_CURRENT)), None
        ),
        "answering_cases": len(answered),
        "distinct_answers_among_answering_cases": len({r["ids_digest"] for r in answered}),
        "invalid_token_rejected": any(
            r.get("http_status") in (401, 403)
            for r in host_rows
            if r["token"] in ("bogus", "garbage")
        ),
        "rate_limit_headers_seen": sorted(
            {h for r in host_rows + page_rows for h in r.get("rate_limit_headers", [])}
        ),
        "page_totals_reported": [r.get("total") for r in api_pages],
        "distinct_ids_served_pages_1_2_7": len(seen),
        "total_counts_the_circle": all(
            r.get("total") is not None and r["total"] >= len(seen) for r in api_pages
        ),
    }
    logger.info(json.dumps(summary))

    if args.docs_dir:
        os.makedirs(args.docs_dir, exist_ok=True)
        name = (
            DOCS_METRICS_NAME if complete else DOCS_METRICS_NAME.replace(".json", ".partial.json")
        )
        with open(os.path.join(args.docs_dir, name), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "_about": {
                        "experiment": "kartaview-endpoint-move",
                        "writeup": "docs/experiments/kartaview-endpoint-move.md",
                        "generated_by": f"scripts/kartaview_endpoint_probe.py --docs-dir {args.docs_dir}",
                        "probed_at_utc": datetime.now(UTC).isoformat(),
                        "complete": complete,
                        "rate_limit_used_per_hour": REQUESTS_PER_HOUR_ANON,
                        "note": "Token values are never recorded, only their kind.",
                    },
                    "summary": summary,
                    "host_cases": host_rows,
                    "page_cases": page_rows,
                },
                f,
                indent=1,
            )
            f.write("\n")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
