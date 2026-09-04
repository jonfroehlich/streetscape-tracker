"""
Issue #312: does KartaView's own viewer still load the photos we link to?

    python scripts/kartaview_details_probe.py --csv <run.csv.gz> --docs-dir docs/experiments

WHAT THIS MEASURES. Every KartaView pano popup on the published site deep-links
to ``kartaview.org/details/{sequence_id}/{sequence_index}``. That page loads its
sequence through one call -- ``POST https://api.kartaview.org/details`` with
``id=<sequenceId>&platform=web`` -- and then reads ``response.osv.photos``. When
the endpoint answers ``osv: null`` the page renders "Ups! Sequence cannot be
loaded..." and the browser console shows
``Cannot read properties of null (reading 'photos')``. This probe asks that
endpoint, for every sequence one of our runs actually links to, whether it
answers with an ``osv`` at all.

WHY THE CONTROLS MATTER MORE THAN THE SEQUENCES. "Their site is broken" and "our
links are wrong" produce the same error page, so the probe carries two controls
that separate them:

  1. KartaView's OWN documented example sequence (``--control-sequence``, from
     their in-app API docs) goes through the identical call. If their example
     fails too, no property of our data can be the cause.
  2. The v2 endpoints for the same sequence (``/2.0/sequence/{id}``,
     ``/2.0/sequence/{id}/photos``, ``/2.0/photo/``) are queried once. They are
     what tells a total outage apart from one broken endpoint, and they are also
     the stack behind the map view we fall back to -- so this is the check that
     the fallback still has something to stand on.

A third confound is worth ruling out by construction rather than by argument:
the endpoint could be refusing a probe rather than a viewer. So the request is
byte-identical to the SPA's (same URL, method, form body and content type), the
run is paced to the documented per-hour limit, and ``--token-sample`` re-asks a
few sequences WITH our credential -- a difference between the two would mean the
answer depends on who is asking rather than on the sequence.

WHAT IT CANNOT SEE. The page, only the call it depends on. A sequence whose
``/details`` answers could still fail to render for some other reason, so a
clean run here is necessary and not sufficient -- confirm in a browser before
concluding the viewer recovered.

COST. One request per sequence, plus one per token-sample re-ask, plus three
control requests. Paced by kartaview_probe's documented-limit pacer (1,000/hr
with a token, 100/hr without), so ~40 sequences is ~2.5 minutes authenticated
and ~24 minutes anonymous. Refuses to run on a makelab* host: this is a probe,
and both per-IP bans this project has taken landed on a collection host.
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

import pandas as pd
import requests
from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kartaview_probe import (  # noqa: E402
    REQUESTS_PER_HOUR_ANON,
    REQUESTS_PER_HOUR_AUTH,
    HourlyRateLimiter,
    refuse_on_collection_host,
)

from streetscape_metadata_tracker import config as cfg  # noqa: E402
from streetscape_metadata_tracker.download_common import redact_credentials  # noqa: E402

logger = logging.getLogger("kartaview_details_probe")

DOCS_METRICS_NAME = "kartaview-viewer-deeplink_metrics.json"

# The exact call kartaview.org's sequence page makes, read out of their bundle
# (`sequenceService.get` -> `this._apiService.post("/details", {id, platform})`,
# with `_baseUrl` = the environment's `apiHostName`).
DETAILS_URL = "https://api.kartaview.org/details"

# KartaView's own documented example sequence, from the API docs rendered inside
# their SPA ("Example: https://api.openstreetcam.org/2.0/sequence/6187609").
# The control that makes this a statement about their service and not our data.
CONTROL_SEQUENCE = "6187609"

# Read once, for the sequence the run's first row names. Not a sample of
# anything -- the question they answer is binary: is the rest of the host up?
V2_CONTROL_URLS = [
    "https://api.kartaview.org/2.0/sequence/{seq}",
    "https://api.kartaview.org/2.0/sequence/{seq}/photos",
    "https://api.kartaview.org/2.0/photo/?sequenceId={seq}&sequenceIndex=0",
]


def sequences_from_run(csv_path: str) -> list[str]:
    """
    The distinct sequence ids a run's popups link to, most-linked first.

    Takes a local path or a URL (pandas reads either), because the file that
    prompts this question is usually the published one rather than a local copy:
    the catalog and the run CSVs live on the collection host, and this script
    refuses to run there.
    """
    df = pd.read_csv(csv_path, usecols=["sequence_id"], dtype={"sequence_id": "string"})
    counts = df["sequence_id"].dropna().value_counts()
    return [str(s) for s in counts.index]


def ask_details(
    session: requests.Session,
    limiter: HourlyRateLimiter,
    sequence: str,
    access_token: str | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """
    One `/details` call, recorded as the viewer would experience it.

    Never raises: a transport failure, an HTML error page and a JSON body with
    `osv: null` are three different ways for the viewer to break, and flattening
    any of them into "failed" would lose the distinction this probe exists to
    draw. `loads` is the verdict the writeup counts: True only when the response
    carries the `osv.photos` the page dereferences.
    """
    limiter.acquire()
    payload = {"id": sequence, "platform": "web"}
    if access_token:
        payload["access_token"] = access_token
    try:
        resp = session.post(
            DETAILS_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=timeout_s,
        )
    except requests.RequestException as e:
        return {
            "sequence_id": sequence,
            "http_status": None,
            "loads": False,
            "failure": redact_credentials(f"{type(e).__name__}: {e}"),
        }

    row: dict[str, Any] = {"sequence_id": sequence, "http_status": resp.status_code}
    try:
        body = resp.json()
    except ValueError:
        row["loads"] = False
        row["failure"] = f"non-JSON body ({resp.headers.get('Content-Type', '?')})"
        return row

    status = body.get("status") or {}
    osv = body.get("osv")
    row["api_code"] = str(status.get("apiCode")) if status.get("apiCode") is not None else None
    row["api_message"] = (status.get("apiMessage") or "").strip() or None
    # Some failures answer outside the v1 envelope entirely (a gateway 500 with
    # its own {code, message} shape), so fall back to that rather than recording
    # a failure with no reason attached.
    if row["api_message"] is None and body.get("message"):
        row["api_message"] = str(body["message"]).strip()
    row["loads"] = bool(osv and osv.get("photos"))
    row["photos"] = len((osv or {}).get("photos") or [])
    return row


def probe_v2_controls(
    session: requests.Session, limiter: HourlyRateLimiter, sequence: str
) -> list[dict[str, Any]]:
    """Is the rest of the host up, and does the map fallback have a backend?"""
    out = []
    for template in V2_CONTROL_URLS:
        url = template.format(seq=sequence)
        limiter.acquire()
        try:
            resp = session.get(url, timeout=60)
            body = (
                resp.json()
                if resp.headers.get("Content-Type", "").startswith("application/json")
                else {}
            )
            code = (body.get("status") or {}).get("apiCode")
            out.append(
                {
                    "url": url,
                    "http_status": resp.status_code,
                    "api_code": str(code) if code is not None else None,
                    "ok": resp.status_code == 200 and str(code) == "600",
                }
            )
        except requests.RequestException as e:
            out.append(
                {
                    "url": url,
                    "http_status": None,
                    "ok": False,
                    "failure": redact_credentials(f"{type(e).__name__}: {e}"),
                }
            )
    return out


def summarize(rows: list[dict[str, Any]], token_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    The distribution, which is the finding: how many sequences the viewer can
    load, and what the ones it cannot say for themselves.
    """
    by_outcome: collections.Counter[str] = collections.Counter()
    for r in rows:
        if r["loads"]:
            by_outcome["loads"] += 1
        elif r.get("api_code"):
            by_outcome[f"api_code {r['api_code']}"] += 1
        else:
            by_outcome[f"http {r.get('http_status')}"] += 1

    # A token that changes the verdict would mean the endpoint is refusing
    # anonymous callers rather than failing, which is a different bug with a
    # different fix -- so the comparison is recorded even when it is a null result.
    anon = {r["sequence_id"]: r["loads"] for r in rows}
    disagreements = [
        t["sequence_id"] for t in token_rows if anon.get(t["sequence_id"]) != t["loads"]
    ]

    return {
        "sequences_probed": len(rows),
        "sequences_loading": sum(1 for r in rows if r["loads"]),
        "by_outcome": dict(by_outcome.most_common()),
        "api_messages": sorted({r["api_message"] for r in rows if r.get("api_message")}),
        "token_sample_size": len(token_rows),
        "token_changes_verdict_for": disagreements,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Probe KartaView's /details endpoint (issue #312).")
    p.add_argument(
        "--csv",
        required=True,
        help="a KartaView run CSV (local path or URL); its distinct sequences are the sample",
    )
    p.add_argument("--docs-dir", default=None, help="write the metrics record here")
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="probe at most this many of the run's sequences (0 = all of them)",
    )
    p.add_argument(
        "--control-sequence",
        default=CONTROL_SEQUENCE,
        help="KartaView's own documented example sequence, probed identically",
    )
    p.add_argument(
        "--token-sample",
        type=int,
        default=4,
        help="re-ask this many sequences WITH the credential, to test who-is-asking",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    refuse_on_collection_host()
    # Bare load_dotenv() to LOAD, find_dotenv only for the permission warning --
    # see kartaview_probe.main; the reverse silently drops the token.
    load_dotenv()
    cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    token = os.environ.get("KARTAVIEW_ACCESS_TOKEN") or None
    limiter = HourlyRateLimiter(REQUESTS_PER_HOUR_AUTH if token else REQUESTS_PER_HOUR_ANON)
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "streetscape-tracker probe (github.com/jonfroehlich/streetscape-tracker)"}
    )

    sequences = sequences_from_run(args.csv)
    if args.limit:
        sequences = sequences[: args.limit]
    logger.info(f"{len(sequences)} distinct sequence(s) from {args.csv}")

    rows = []
    for i, sequence in enumerate(sequences, start=1):
        row = ask_details(session, limiter, sequence)
        row["source"] = "run"
        rows.append(row)
        logger.info(f"[{i}/{len(sequences)}] {sequence}: {'loads' if row['loads'] else 'FAILS'}")

    control = ask_details(session, limiter, args.control_sequence)
    control["source"] = "kartaview_documented_example"
    rows.append(control)
    logger.info(
        f"control sequence {args.control_sequence}: {'loads' if control['loads'] else 'FAILS'}"
    )

    token_rows = []
    if token and args.token_sample:
        for sequence in sequences[: args.token_sample]:
            t = ask_details(session, limiter, sequence, access_token=token)
            t["source"] = "run (authenticated)"
            token_rows.append(t)

    v2 = probe_v2_controls(session, limiter, sequences[0]) if sequences else []
    summary = summarize(rows, token_rows)
    summary["v2_endpoints_ok"] = sum(1 for c in v2 if c["ok"])
    summary["v2_endpoints_probed"] = len(v2)

    logger.info(
        f"{summary['sequences_loading']}/{summary['sequences_probed']} sequences load; "
        f"v2 controls {summary['v2_endpoints_ok']}/{summary['v2_endpoints_probed']} healthy"
    )

    if args.docs_dir:
        os.makedirs(args.docs_dir, exist_ok=True)
        path = os.path.join(args.docs_dir, DOCS_METRICS_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "_about": {
                        "experiment": "kartaview-viewer-deeplink",
                        "writeup": "docs/experiments/kartaview-viewer-deeplink.md",
                        "generated_by": (
                            f"scripts/kartaview_details_probe.py --csv {args.csv}"
                            + (f" --limit {args.limit}" if args.limit else "")
                            + f" --docs-dir {args.docs_dir}"
                        ),
                        "issue": 312,
                        "probed_at_utc": datetime.now(UTC).isoformat(),
                        "authenticated_pacing": token is not None,
                        "rate_limit_used_per_hour": (
                            REQUESTS_PER_HOUR_AUTH if token else REQUESTS_PER_HOUR_ANON
                        ),
                        "endpoint": DETAILS_URL,
                        "verdict_definition": (
                            "loads = the response carries osv.photos, which is what the viewer "
                            "dereferences; anything else renders 'Sequence cannot be loaded...'"
                        ),
                        "note": (
                            "The run sequences and the control sequence go through the IDENTICAL "
                            "call, so a control that fails rules out our data as the cause. The "
                            "v2 rows are one-shot health checks of the same host, and are also "
                            "the stack behind the map-view fallback."
                        ),
                    },
                    "summary": summary,
                    "sequences": rows,
                    "authenticated_sample": token_rows,
                    "v2_controls": v2,
                },
                f,
                indent=2,
            )
        logger.info(f"wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
