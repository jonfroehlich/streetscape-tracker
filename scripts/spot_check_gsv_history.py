#!/usr/bin/env python3
"""
Spot-check harvested capture dates against the DOCUMENTED metadata API (#442).

The history crawl reads an undocumented endpoint. The Street View Static
metadata request is documented, key-metered and answers for a pano id you
already know (`pano=<id>` -> `date: YYYY-MM`), so a random sample of
harvested ids checked through it says whether the two agree on the month.
It is the pilot's second check, and the one that keys a Google project, so
it runs from a host that holds the streets key (makelab2), never from the
crawler host, which holds no credential on purpose.

Reads one `<stem>.pass<N>.history.csv.gz`, samples `--limit` distinct
official pano ids (third-party ones only with --include-third-party), asks
the metadata API for each at `--rate-per-minute`, and writes a metrics JSON:
how many agree on the month, disagree (with examples), echo a different
pano id, or are unknown to the documented API (ZERO_RESULTS: a retired
pano). Spend is recorded in the catalog ledger when --db-path is given.

The catalog is opened BEFORE any request, through the orphan sweep's
read-only opener, which refuses a schema mismatch in either direction and
never creates or migrates a file -- db.connect runs init_schema, and on
2026-10-17 a newer checkout's spot check migrated the PROD catalog that way
and crashed the next nightly. The one write, the spend row, goes through the
same validated connection with query_only lifted for it; so a refusal costs
zero requests, and spend never reaches a catalog this code did not check.

Spends by default (there is nothing to execute but the requests); --dry-run
samples and reports without one.

Usage:
    python scripts/spot_check_gsv_history.py --history results/X.pass1.history.csv.gz --limit 200 --out spot.json
    python scripts/spot_check_gsv_history.py --history ... --limit 200 --out spot.json --db-path data/streetscape_tracker.db
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.sweep_orphan_diff_details import (  # noqa: E402
    CatalogRefused,
    open_catalog_readonly,
)
from streetscape_metadata_tracker import config, db  # noqa: E402
from streetscape_metadata_tracker.download_common import (  # noqa: E402
    positive_int,
    redact_credentials,
)

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
PROVIDER = "gsv_streets"
Fetch = Callable[[str], tuple[int, dict[str, Any]]]


def sample_ids(
    history: pd.DataFrame, limit: int, seed: int, *, include_third_party: bool = False
) -> pd.DataFrame:
    """``limit`` distinct pano ids (with our month) drawn uniformly, seeded."""
    df = history if include_third_party else history[~history["third_party"].astype(bool)]
    distinct = df.drop_duplicates("pano_id")[
        ["pano_id", "capture_date", "third_party", "is_current"]
    ]
    if len(distinct) > limit:
        distinct = distinct.sample(n=limit, random_state=seed)
    return distinct.reset_index(drop=True)


def _requests_fetch(api_key: str, timeout: float = 30) -> Fetch:
    def fetch(pano_id: str) -> tuple[int, dict[str, Any]]:
        resp = requests.get(METADATA_URL, params={"pano": pano_id, "key": api_key}, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"status": "UNPARSEABLE", "text": redact_credentials(resp.text[:200])}
        return resp.status_code, payload

    return fetch


def spot_check(
    sample: pd.DataFrame, fetch: Fetch, *, rate_per_minute: float = 300, sleep=time.sleep
) -> dict[str, Any]:
    """Query each sampled id; classify the answer against our month."""
    gap = 60.0 / rate_per_minute if rate_per_minute > 0 else 0.0
    counts = {"month_match": 0, "month_mismatch": 0, "pano_id_differs": 0, "unknown": 0, "error": 0}
    by_year: dict[str, dict[str, int]] = {}
    mismatches: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    statuses: dict[str, int] = {}
    n = 0
    for row in sample.itertuples(index=False):
        if n and gap:
            sleep(gap)
        n += 1
        http, payload = fetch(row.pano_id)
        status = str(payload.get("status", f"HTTP {http}"))
        statuses[status] = statuses.get(status, 0) + 1
        year = str(row.capture_date)[:4]
        by_year.setdefault(year, {"n": 0, "month_match": 0})
        by_year[year]["n"] += 1
        if http != 200 or status not in ("OK", "ZERO_RESULTS"):
            counts["error"] += 1
            errors.append({"pano_id": row.pano_id, "http": http, "status": status})
            continue
        if status == "ZERO_RESULTS":
            counts["unknown"] += 1
            continue
        if payload.get("pano_id") not in (None, row.pano_id):
            counts["pano_id_differs"] += 1
            mismatches.append(
                {
                    "pano_id": row.pano_id,
                    "returned_pano_id": payload.get("pano_id"),
                    "kind": "pano_id",
                }
            )
            continue
        theirs = str(payload.get("date", ""))[:7]
        ours = str(row.capture_date)[:7]
        if theirs == ours:
            counts["month_match"] += 1
            by_year[year]["month_match"] += 1
        else:
            counts["month_mismatch"] += 1
            mismatches.append(
                {"pano_id": row.pano_id, "ours": ours, "theirs": theirs or None, "kind": "month"}
            )
    answered = counts["month_match"] + counts["month_mismatch"]
    return {
        "generated_by": "scripts/spot_check_gsv_history.py",
        "generated_at": datetime.now(UTC).isoformat(),
        "requests": n,
        "statuses": statuses,
        **counts,
        "month_match_share_of_answered": round(counts["month_match"] / answered, 4)
        if answered
        else None,
        "by_year": by_year,
        "mismatches": mismatches,
        "errors": errors[:20],
    }


def open_ledger(db_path: str):
    """
    The catalog, validated and opened for the ONE write this script makes.

    ``open_catalog_readonly`` is what checks it: the file must exist, be a
    catalog at exactly this code's schema version, and hold runs or walks;
    nothing is created or migrated. ``query_only`` is then lifted on that same
    connection so ``db.add_api_usage`` can insert the spend row (an INSERT
    into tables the version check proved exist).
    """
    conn = open_catalog_readonly(db_path)
    conn.execute("PRAGMA query_only = OFF")
    return conn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--history", required=True, help="<stem>.pass<N>.history.csv.gz")
    p.add_argument("--out", required=True, help="metrics JSON to write")
    p.add_argument("--limit", type=positive_int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--include-third-party", action="store_true")
    p.add_argument("--rate-per-minute", type=float, default=300.0)
    p.add_argument("--db-path", default=None, help="record the spend in this catalog's ledger")
    p.add_argument(
        "--env-file",
        default=None,
        help="the .env holding GMAPS_STREETS_API_KEY (default: the nearest .env up from the cwd)",
    )
    p.add_argument("--dry-run", action="store_true", help="sample and report, no requests")
    return p


def main(argv=None, fetch: Fetch | None = None) -> int:
    args = build_parser().parse_args(argv)
    history = pd.read_csv(args.history, dtype={"pano_id": str, "capture_date": str})
    sample = sample_ids(
        history, args.limit, args.seed, include_third_party=args.include_third_party
    )
    print(
        f"{len(sample)} distinct pano ids sampled from {len(history)} history rows (seed {args.seed})"
    )
    conn = None
    if args.db_path:
        # Before the first request: a catalog this code cannot write without
        # migrating it is refused here, at a cost of zero requests.
        try:
            conn = open_ledger(args.db_path)
        except CatalogRefused as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.dry_run:
        return 0
    if fetch is None:
        from dotenv import find_dotenv, load_dotenv

        # The same loader the CLI uses, from the cwd rather than this file's
        # directory, so a worktree checkout can read the production .env.
        load_dotenv(args.env_file or find_dotenv(usecwd=True))
        fetch = _requests_fetch(config.load_config(PROVIDER)["api_key"])
    metrics = spot_check(sample, fetch, rate_per_minute=args.rate_per_minute)
    if conn is not None:
        db.add_api_usage(conn, datetime.now(UTC).date(), metrics["requests"], PROVIDER)
        conn.commit()
        conn.close()
    metrics["history_file"] = os.path.basename(args.history)
    metrics["seed"] = args.seed
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(
        f"{metrics['requests']} requests: {metrics['month_match']} month match, "
        f"{metrics['month_mismatch']} mismatch, {metrics['pano_id_differs']} pano id differs, "
        f"{metrics['unknown']} unknown to the documented API, {metrics['error']} errors "
        f"-> {args.out}"
    )
    return 0 if metrics["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
