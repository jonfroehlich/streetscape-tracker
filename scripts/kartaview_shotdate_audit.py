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
    is_complete_sample,
    refuse_on_collection_host,
)

from streetscape_metadata_tracker import config as cfg  # noqa: E402

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
    """
    One v2 GET, returning ``result.data``.

    Raises ProbeError on a refusal rather than returning ``{}``. A refused lookup
    and a genuinely empty one are different facts (the same rule ``_post_nearby``
    states): swallowing the first writes a row with a null photo count and verdict
    'unknown', which deflates photos_audited silently and is indistinguishable in
    the record from imagery that really carries no date.
    """
    limiter.acquire()
    params = {"access_token": token} if token else None
    r = session.get(url, params=params, timeout=60)
    try:
        body = r.json()
    except ValueError as e:
        ctype = r.headers.get("Content-Type", "?")
        raise ProbeError(f"non-JSON body (HTTP {r.status_code}, {ctype}) from {url}") from e

    api_code = (body.get("status") or {}).get("apiCode")
    if r.status_code >= 400 or api_code in (408, 690):
        raise ProbeError(f"HTTP {r.status_code}, apiCode {api_code} from {url}")

    data = (body.get("result") or {}).get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _parse_ts(value: str | None):
    """
    Parse one KartaView timestamp, tolerating the renderings both APIs emit.

    Compared as DATETIMES rather than as truncated strings. A lexical compare
    happens to work only while both fields render identically: v1 already emits
    milliseconds where v2 does not, and if `shotDate` ever arrived ISO-8601 with a
    'T' separator while `dateAdded` kept the space form, `'T' (0x54) > ' ' (0x20)`
    would flip EVERY sequence to invalid at once. A MySQL zero date -- plausible
    here, given the collation error leaking out of findNearbyPhotos -- is not a
    time and returns None so the verdict is 'unknown' rather than 'ok'.
    """
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    if text.startswith("0000-00-00"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(text)], fmt)
        except ValueError:
            continue
    # Trailing zone/offset the formats above don't cover: retry on the bare
    # 19-char prefix before giving up, so a suffix alone can't read as unknown.
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def classify(v2_shot: str | None, v2_added: str | None) -> str:
    """
    One of: 'ok' | 'invalid' | 'unknown'.

    'invalid' means shotDate >= dateAdded -- capture at or after upload, which is
    impossible and marks the value as an ingest timestamp rather than a capture
    time. Deliberately NOT `>`: see the module docstring.

    Compares only v2's own two fields. It took a v1 date as a first argument and
    never read it, which made it look like a two-endpoint cross-check; that
    correspondence is a separate finding, asserted over the finished record.
    """
    shot, added = _parse_ts(v2_shot), _parse_ts(v2_added)
    if shot is None or added is None:
        return "unknown"
    return "invalid" if shot >= added else "ok"


def audit(session, limiter, token: str | None, ipp: int = 2000) -> tuple[list, list]:
    """
    Returns ``(sequence_rows, point_rows)``.

    Point rows exist so the record can be checked for the sampling bias below: a
    point that was dropped, or reached at a radius that paged most of its drives
    away, is otherwise invisible and reads as a clean result.
    """
    seen: dict[str, dict[str, Any]] = {}
    points: list[dict[str, Any]] = []

    for city, band, lat, lng in POINTS:
        # Walk the ladder DOWN to the smallest COMPLETE rung rather than stopping
        # at the first (largest) success. The companion probe shows this endpoint
        # fills a page by sequence, so a large circle returns one long drive: at
        # r=500 Seattle yields 1 sequence out of 2,030 photos, at r=100 it yields
        # 12. Taking the first success is why the previous pass audited 2 Seattle
        # sequences and 2 Singapore ones, and then rested "the controls are clean"
        # on them. Sequence DIVERSITY is the whole point of this sampling step.
        best: tuple[int, list[dict]] | None = None
        errors: list[str] = []
        for radius in RADIUS_LADDER_M:
            try:
                items, total = _post_nearby(
                    session, limiter, lat, lng, radius, ipp=ipp, access_token=token
                )
            except ProbeError as e:
                logger.info(f"{city} @ {lat},{lng} r={radius}m FAILED - {e}")
                errors.append(f"r={radius}: {e}")
                continue
            if items:
                best = (radius, items)
            if is_complete_sample(len(items), total) and items:
                break

        if best is None:
            logger.warning(
                f"{city} @ {lat},{lng}: DROPPED - no rung returned photos "
                f"({len(errors)} refusals). This is NOT evidence of absent imagery."
            )
            points.append(
                {
                    "city": city,
                    "band": band,
                    "lat": lat,
                    "lng": lng,
                    "reached": False,
                    "errors": errors,
                }
            )
            continue

        used_radius, items = best
        by_seq = collections.defaultdict(list)
        for it in items:
            if it.get("sequence_id"):
                by_seq[it["sequence_id"]].append(it)
        logger.info(
            f"{city} @ {lat},{lng} r={used_radius}: {len(items)} photos, {len(by_seq)} sequences"
        )

        ranked = sorted(by_seq.items(), key=lambda kv: -len(kv[1]))[:MAX_SEQUENCES_PER_POINT]
        # The top-N cap is a real bound on coverage, so it is recorded rather than
        # left for a reader to infer from a total: "48 sequences audited" reads as
        # "all of them" unless the record says how many were on the page.
        points.append(
            {
                "city": city,
                "band": band,
                "lat": lat,
                "lng": lng,
                "reached": True,
                "radius_used_m": used_radius,
                "photos_on_page": len(items),
                "sequences_found": len(by_seq),
                "sequences_selected": len(ranked),
                "capped_by_max_sequences_per_point": len(by_seq) > MAX_SEQUENCES_PER_POINT,
                "errors": errors,
            }
        )
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
            except (requests.RequestException, ProbeError) as e:
                logger.warning(f"seq {seq_id}: lookup failed ({type(e).__name__}: {e})")
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
                "verdict": classify(v2_shot, v2_added),
            }
            seen[seq_id] = rec
            logger.info(
                f"  seq {seq_id:>10s} {rec['verdict']:>7s} n={rec['count_active_photos']} "
                f"{rec['device']} added={rec['sequence_date_added']}"
            )
    return list(seen.values()), points


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
    p.add_argument(
        "--ipp",
        type=int,
        default=2000,
        help=(
            "items per page (server cap 2000). The default is the CAP, not 200: a "
            "bigger page is the same one request and is what lets a point reach a "
            "complete sample, which is what gives it sequence diversity."
        ),
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    refuse_on_collection_host()
    # See kartaview_probe.main: bare load_dotenv() to LOAD (its search walks up
    # from this module's directory, so it works from any cwd), find_dotenv only
    # for the permission warning. The reverse silently drops the token.
    load_dotenv()
    cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    token = os.environ.get("KARTAVIEW_ACCESS_TOKEN") or None
    limiter = HourlyRateLimiter(REQUESTS_PER_HOUR_AUTH if token else REQUESTS_PER_HOUR_ANON)
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "streetscape-tracker audit (github.com/jonfroehlich/streetscape-tracker)"}
    )

    rows, points = audit(session, limiter, token, ipp=args.ipp)
    summary = summarize(rows)
    summary["points_reached"] = sum(1 for p in points if p["reached"])
    summary["points_dropped"] = sum(1 for p in points if not p["reached"])
    summary["points_capped"] = sum(1 for p in points if p.get("capped_by_max_sequences_per_point"))

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
                            "scripts/kartaview_shotdate_audit.py"
                            + (f" --ipp {args.ipp}" if args.ipp != 2000 else "")
                            + f" --docs-dir {args.docs_dir}"
                        ),
                        "issue": 225,
                        "probed_at_utc": datetime.now(UTC).isoformat(),
                        "authenticated": token is not None,
                        "ipp": args.ipp,
                        "max_sequences_per_point": MAX_SEQUENCES_PER_POINT,
                        "points_configured": len(POINTS),
                        "invariant": "shotDate < dateAdded; shotDate >= dateAdded is invalid",
                        "note": (
                            "Photo counts are countActivePhotos from /2.0/sequence/{id} -- the "
                            "sequence's FULL size, not the sampled page -- so photos_invalid is "
                            "the count for the sequences reached, EXTRAPOLATED from one sampled "
                            "photo per sequence on the strength of the per-sequence datedness "
                            "finding (see per_sequence[] in the feasibility record), and still a "
                            "LOWER BOUND on the batch as a whole. points[] carries the radius "
                            "each point was reached at and whether its sequence list was capped "
                            "at max_sequences_per_point, because both bound coverage."
                        ),
                    },
                    "summary": summary,
                    "points": points,
                    "sequences": rows,
                },
                f,
                indent=2,
            )
            f.write("\n")
        # Written before the console table: the record is the deliverable and the
        # table is a convenience, so a formatting error must not discard an
        # hour-long paced audit that has already spent its requests.
        print(f"\nWrote {path}")

    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))
    print(f"\n{'sequence':>11s} {'verdict':>8s} {'photos':>7s} {'device':<20s} {'added':<20s} city")
    for r in sorted(rows, key=lambda r: (r["verdict"] != "invalid", r["city"])):
        print(
            f"{str(r['sequence_id']):>11s} {str(r['verdict']):>8s} "
            f"{str(r['count_active_photos']):>7s} "
            f"{str(r['device']):<20s} {str(r['sequence_date_added'])[:19]:<20s} {r['city']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
