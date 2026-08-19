"""
Issue #225: audit KartaView capture dates against their own upload timestamps.

    python scripts/kartaview_shotdate_audit.py --docs-dir docs/experiments

WHAT THIS MEASURES. A photo cannot be captured after it was uploaded, so
`shotDate < dateAdded` is an invariant every honest record must satisfy. Sampling
KartaView shows a population that violates it: for one upload batch the v1
endpoint reports `shot_date: null` while the v2 endpoint reports a `shotDate`
equal to, or minutes later than, that photo's own `dateAdded` -- i.e. the ingest
time wearing a capture-date label.

WHY `>=` AND NOT `>`. The violating records are not all "later"; some are equal to
the second (Langkawi sequence 11616157 reads
`shotDate == dateAdded == 2025-11-19 11:18:29`). A strict `>` test misses those,
which is exactly the sort of near-miss guard that lets bad data through, so the
predicate here is `shotDate >= dateAdded`.

WHY IT MATTERS TO US. A null is honest and can be handled; a plausible-looking
capture date that is really an ingest timestamp is silently wrong, and this
project's entire subject is when imagery was captured. Any KartaView collector
must apply this predicate and record which date field it used. Same species as
`plan_match.plausible_capture_date`, which exists because #213 found corrupt
third-party EXIF poisoning GSV capture statistics.

COST. One nearby-photos call per probe point (radius ladder), then two calls per
distinct sequence (`/2.0/sequence/{id}` for the authoritative photo count and
device, `/2.0/photo/{id}` for a photo-level shotDate). Paced by the same
documented limits as kartaview_probe. Refuses to run on a makelab* host.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kartaview_probe import (  # noqa: E402
    RADIUS_LADDER_M,
    REQUESTS_PER_HOUR_ANON,
    REQUESTS_PER_HOUR_AUTH,
    HourlyRateLimiter,
    ProbeError,
    _post_nearby,
    refuse_on_collection_host,
)

logger = logging.getLogger("kartaview_shotdate_audit")

SEQUENCE_URL = "https://api.openstreetcam.org/2.0/sequence/{}"
PHOTO_URL = "https://api.openstreetcam.org/2.0/photo/{}"

# Several points per release city, because one point samples one drive (see the
# paging finding in docs/experiments/kartaview-feasibility.md) and the question
# here is how WIDESPREAD the defect is, not what one street looks like.
# Non-release Grab markets are included to test whether the bad ingest reached
# beyond the open release; Seattle is a pure-community control.
POINTS: list[tuple[str, str, float, float]] = [
    ("Krabi", "release", 8.0634637, 98.9162345),
    ("Krabi", "release", 8.0700, 98.9200),
    ("Krabi", "release", 8.0550, 98.9100),
    ("Yogyakarta", "release", -7.7956, 110.3695),
    ("Yogyakarta", "release", -7.8033342, 110.37552685),
    ("Yogyakarta", "release", -7.7800, 110.3700),
    ("Langkawi", "release", 6.3200, 99.8500),
    ("Langkawi", "release", 6.2900, 99.7300),
    ("Langkawi", "release", 6.3350, 99.7280),
    ("Singapore", "grab-market", 1.2830, 103.8600),
    ("Bangkok", "grab-market", 13.7563, 100.5018),
    ("Ho Chi Minh City", "grab-market", 10.7769, 106.7009),
    ("Seattle", "community-control", 47.6097, -122.3331),
]

DOCS_METRICS_NAME = "kartaview-shotdate-audit_metrics.json"
MAX_SEQUENCES_PER_POINT = 8


def _get_json(session, limiter, url: str, token: str | None) -> dict[str, Any]:
    limiter.acquire()
    params = {"access_token": token} if token else None
    r = session.get(url, params=params, timeout=60)
    body = r.json()
    data = (body.get("result") or {}).get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def classify(v1_shot: str | None, v2_shot: str | None, v2_added: str | None) -> str:
    """
    One of: 'ok' | 'invalid' | 'unknown'.

    'invalid' means shotDate >= dateAdded -- capture at or after upload, which is
    impossible and marks the value as an ingest timestamp rather than a capture
    time. Deliberately NOT `>`: see the module docstring.
    """
    if not v2_shot or not v2_added:
        return "unknown"
    return "invalid" if v2_shot[:19] >= v2_added[:19] else "ok"


def audit(session, limiter, token: str | None) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}

    for city, band, lat, lng in POINTS:
        items: list[dict] = []
        used_radius = None
        for radius in RADIUS_LADDER_M:
            try:
                items, _ = _post_nearby(
                    session, limiter, lat, lng, radius, ipp=200, access_token=token
                )
                used_radius = radius
                break
            except ProbeError as e:
                logger.debug(f"{city} r={radius}: {e}")
        if not items:
            logger.warning(f"{city} @ {lat},{lng}: no rung returned data")
            continue

        by_seq = collections.defaultdict(list)
        for it in items:
            if it.get("sequence_id"):
                by_seq[it["sequence_id"]].append(it)
        logger.info(
            f"{city} @ {lat},{lng} r={used_radius}: {len(items)} photos, {len(by_seq)} sequences"
        )

        ranked = sorted(by_seq.items(), key=lambda kv: -len(kv[1]))[:MAX_SEQUENCES_PER_POINT]
        for seq_id, seq_items in ranked:
            if seq_id in seen:
                # Already audited from another probe point -- record the extra
                # city sighting but do not pay for the lookups twice.
                seen[seq_id]["seen_at"].append(f"{city}@{lat},{lng}")
                continue
            sample = seq_items[0]
            try:
                sq = _get_json(session, limiter, SEQUENCE_URL.format(seq_id), token)
                ph = _get_json(session, limiter, PHOTO_URL.format(sample["id"]), token)
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"seq {seq_id}: lookup failed ({type(e).__name__})")
                continue

            v2_shot, v2_added = ph.get("shotDate"), ph.get("dateAdded")
            rec = {
                "sequence_id": seq_id,
                "city": city,
                "band": band,
                "seen_at": [f"{city}@{lat},{lng}"],
                "device": sq.get("deviceName"),
                "user_id": sq.get("userId"),
                "username": sample.get("username"),
                "projection": sample.get("projection"),
                "count_active_photos": _as_int(sq.get("countActivePhotos")),
                "sequence_date_added": sq.get("dateAdded"),
                "sampled_photo_id": sample.get("id"),
                "v1_shot_date": sample.get("shot_date"),
                "v2_shot_date": v2_shot,
                "v2_date_added": v2_added,
                "verdict": classify(sample.get("shot_date"), v2_shot, v2_added),
            }
            seen[seq_id] = rec
            logger.info(
                f"  seq {seq_id:>10s} {rec['verdict']:>7s} n={rec['count_active_photos']} "
                f"{rec['device']} added={rec['sequence_date_added']}"
            )
    return list(seen.values())


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate by verdict, city and device. Photo counts are the AUTHORITATIVE
    countActivePhotos from the sequence endpoint, not the sampled page."""

    def tally(key):
        out = collections.defaultdict(lambda: {"sequences": 0, "photos": 0})
        for r in rows:
            b = out[r.get(key)]
            b["sequences"] += 1
            b["photos"] += r.get("count_active_photos") or 0
        return {str(k): v for k, v in sorted(out.items(), key=lambda kv: -kv[1]["photos"])}

    invalid = [r for r in rows if r["verdict"] == "invalid"]
    return {
        "sequences_audited": len(rows),
        "photos_audited": sum(r.get("count_active_photos") or 0 for r in rows),
        "sequences_invalid": len(invalid),
        "photos_invalid": sum(r.get("count_active_photos") or 0 for r in invalid),
        "by_verdict": tally("verdict"),
        "by_city": tally("city"),
        "by_device": tally("device"),
        "invalid_upload_dates": sorted(
            {(r.get("sequence_date_added") or "")[:10] for r in invalid}
        ),
        "invalid_devices": sorted({str(r.get("device")) for r in invalid}),
        "ok_upload_dates": sorted(
            {(r.get("sequence_date_added") or "")[:10] for r in rows if r["verdict"] == "ok"}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Audit KartaView shotDate vs dateAdded (issue #225).")
    p.add_argument("--docs-dir", default=None, help="write the metrics record here")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    refuse_on_collection_host()
    load_dotenv(find_dotenv(usecwd=True))
    token = os.environ.get("KARTAVIEW_ACCESS_TOKEN") or None
    limiter = HourlyRateLimiter(REQUESTS_PER_HOUR_AUTH if token else REQUESTS_PER_HOUR_ANON)
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "streetscape-tracker audit (github.com/jonfroehlich/streetscape-tracker)"}
    )

    rows = audit(session, limiter, token)
    summary = summarize(rows)

    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))
    print(f"\n{'sequence':>11s} {'verdict':>8s} {'photos':>7s} {'device':<20s} {'added':<20s} city")
    for r in sorted(rows, key=lambda r: (r["verdict"] != "invalid", r["city"])):
        print(
            f"{r['sequence_id']:>11s} {r['verdict']:>8s} {str(r['count_active_photos']):>7s} "
            f"{str(r['device']):<20s} {str(r['sequence_date_added'])[:19]:<20s} {r['city']}"
        )

    if args.docs_dir:
        os.makedirs(args.docs_dir, exist_ok=True)
        path = os.path.join(args.docs_dir, DOCS_METRICS_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "_about": {
                        "experiment": "kartaview-shotdate-audit",
                        "writeup": "docs/experiments/kartaview-feasibility.md",
                        "generated_by": (
                            f"scripts/kartaview_shotdate_audit.py --docs-dir {args.docs_dir}"
                        ),
                        "issue": 225,
                        "probed_at_utc": datetime.now(UTC).isoformat(),
                        "authenticated": token is not None,
                        "invariant": "shotDate < dateAdded; shotDate >= dateAdded is invalid",
                        "note": (
                            "Photo counts are countActivePhotos from /2.0/sequence/{id} -- the "
                            "sequence's FULL size, not the sampled page -- so photos_invalid is "
                            "the real number of affected photos in the sequences reached, and "
                            "still a LOWER BOUND on the batch as a whole."
                        ),
                    },
                    "summary": summary,
                    "sequences": rows,
                },
                f,
                indent=2,
            )
            f.write("\n")
        print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
