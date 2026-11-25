#!/usr/bin/env python3
"""
Score the #441 tranche-3 advance prediction against the walks. Zero requests.

    python scripts/mapillary_discovery_tranche3_outcome.py \\
        --prod-snapshot experiments/mapillary-discovery-europe/prod_snapshot.csv \\
        --snapshot-label makelab2-prod-9cd70ec-2026-11-25 \\
        --walk-counts 1 \\
        --out docs/experiments/mapillary-discovery-screen_tranche3_outcome.json

The prediction (``advance_prediction_tranche3`` in the tranche-3 metrics, written
before any of the 25 towns was walked) is falsified below a stated number of
strict hits on each town's FIRST Mapillary drive walk, strict meaning at least
50% of street-km covered by 360-degree imagery at a median age of at most two
years. ``collect catalog-snapshot`` exports each city's LATEST walk, so the
caller states with ``--walk-counts`` how many Mapillary drive walks each town
had when the snapshot was taken (a read-only count on the catalog host); the
outcome is refused unless that is 1, when latest and first are the same walk.

Ages are compared as stored, unrounded: 2.03 years is a miss.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mapillary_discovery_analyze import strict_json  # noqa: E402
from scripts.mapillary_discovery_score import (  # noqa: E402
    STRICT_AGE_YR,
    STRICT_COVERAGE_PCT,
    load_catalog,
)
from scripts.register_frame import frame_identity  # noqa: E402
from streetscape_metadata_tracker import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "mapillary_discovery_cities_tranche3.csv"
METRICS = REPO_ROOT / "docs/experiments/mapillary-discovery-screen_tranche3_metrics.json"


def outcome(manifest_rows: list[dict], walks: pd.DataFrame, falsified_below: int) -> dict:
    """
    The per-town verdicts and the tally. A town with no walk is ``unwalked``,
    and the prediction is ``undecided`` while any is, never scored on the rest.
    """
    by_city = walks.set_index("city_id")
    towns = []
    for row in manifest_rows:
        city_id = db.derive_city_id(*frame_identity(row))
        if city_id not in by_city.index:
            towns.append({"city_id": city_id, "walked": False})
            continue
        w = by_city.loc[city_id]
        coverage, age = float(w.coverage_pct_by_length), float(w.median_covered_age_years)
        towns.append(
            {
                "city_id": city_id,
                "walked": True,
                "run_date": w.run_date,
                "coverage_pct_by_length": round(coverage, 2),
                "median_covered_age_years": round(age, 3),
                "length_km": round(float(w.length_km), 1),
                "meets_coverage": coverage >= STRICT_COVERAGE_PCT,
                "meets_age": age <= STRICT_AGE_YR,
                "strict": coverage >= STRICT_COVERAGE_PCT and age <= STRICT_AGE_YR,
            }
        )
    walked = [t for t in towns if t["walked"]]
    hits = sum(t["strict"] for t in walked)
    if len(walked) < len(towns):
        verdict = "undecided"
    else:
        verdict = "falsified" if hits < falsified_below else "held"
    return {
        "n": len(towns),
        "walked": len(walked),
        "strict_hits": hits,
        "coverage_at_least_50pct": sum(t["meets_coverage"] for t in walked),
        "missed_on_coverage": sorted(t["city_id"] for t in walked if not t["meets_coverage"]),
        "missed_on_age_only": sorted(
            t["city_id"] for t in walked if t["meets_coverage"] and not t["meets_age"]
        ),
        "falsified_below": falsified_below,
        "verdict": verdict,
        "towns": towns,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--prod-snapshot", required=True, help="collect catalog-snapshot output")
    p.add_argument("--snapshot-label", required=True, help="which catalog, at which commit and day")
    p.add_argument(
        "--walk-counts",
        type=int,
        required=True,
        help="the largest number of Mapillary drive walks any tranche-3 town had at the snapshot",
    )
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    if args.walk_counts != 1:
        raise SystemExit(
            f"--walk-counts {args.walk_counts}: the snapshot holds each town's LATEST walk and "
            "the prediction is about its FIRST; they are the same walk only when every town has one"
        )
    with MANIFEST.open(newline="") as f:
        rows = list(csv.DictReader(f))
    prediction = json.loads(METRICS.read_text())["advance_prediction_tranche3"]
    _, walks = load_catalog(args.prod_snapshot)
    result = outcome(rows, walks, prediction["falsified_below"])
    result = {
        "_about": {
            "generated_by": "scripts/mapillary_discovery_tranche3_outcome.py "
            + " ".join(sys.argv[1:] if argv is None else argv),
            "catalog_snapshot": args.snapshot_label,
            "prediction": prediction,
            "strict_bar": {
                "coverage_pct_at_least": STRICT_COVERAGE_PCT,
                "age_yr_at_most": STRICT_AGE_YR,
            },
            "first_walk_basis": "stated by the caller (--walk-counts 1) from a count on the catalog "
            "host; the snapshot holds only each town's latest walk, so it cannot check this",
        },
        **result,
    }
    Path(args.out).write_text(strict_json(result) + "\n")
    print({k: result[k] for k in ("n", "walked", "strict_hits", "verdict")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
