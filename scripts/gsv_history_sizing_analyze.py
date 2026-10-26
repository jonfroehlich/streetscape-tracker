#!/usr/bin/env python3
"""
Assemble the committed #442 sizing record from the two measurement scripts' outputs.

``scripts/size_gsv_history_walks.py`` and ``scripts/gsv_history_walk_pair_changes.py``
write their raw results into the gitignored ``experiments/gsv-history-sizing/out/``
(``summary.json`` + ``per_city.csv`` + ``pairs.csv``, ``pair_changes.json`` +
``pair_changes.csv``, ``walk_totals.json``). This script merges the three JSON blocks
into ``docs/experiments/gsv-history-sizing_metrics.json``, copies the three tables
beside it under the ``gsv-history-sizing_`` prefix byte for byte, and computes every
share, ratio and duration the writeup quotes under ``derived``, so no number in the
writeup rests on a merge done by hand (docs/experiments/README.md, "Numbers cited in a
writeup must be traceable to committed code"):

    python scripts/gsv_history_sizing_analyze.py --raw-dir experiments/gsv-history-sizing/out \\
        --docs-dir docs/experiments --catalog-label makelab2-prod

Reads local files only; no network, no catalog.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

import pandas as pd

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)

from experiment_stats import percentile  # noqa: E402

TOPIC = "gsv-history-sizing"
RAW_DIR_DEFAULT = "experiments/gsv-history-sizing/out"
DOCS_DIR_DEFAULT = "docs/experiments"
METRICS_NAME = f"{TOPIC}_metrics.json"
# raw name -> committed name
TABLES = {
    "per_city.csv": f"{TOPIC}_per_city.csv",
    "pairs.csv": f"{TOPIC}_pairs.csv",
    "pair_changes.csv": f"{TOPIC}_pair_changes.csv",
}
INPUT_JSON = ("summary.json", "pair_changes.json", "walk_totals.json")
# Provenance keys each measurement script stamps on its own output; lifted into
# ``inputs`` rather than left inside the copied blocks.
PROVENANCE_KEYS = ("generated_by", "generated_at", "as_of")
# The paced rates the writeup prices a pass at: the pilot's 60/min, and double it.
REQUESTS_PER_MINUTE = (60, 120)
GOOGLE_SHARE_PCTILES = (10, 50, 90)
TOP_CONTRIBUTORS = 5


def docs_generated_by(raw_dir: str, docs_dir: str, label: str) -> str:
    """The exact command that reproduces the committed metrics file."""
    return (
        f"python scripts/gsv_history_sizing_analyze.py --raw-dir {raw_dir} "
        f"--docs-dir {docs_dir} --catalog-label {label}"
    )


def read_raw(raw_dir: str) -> tuple[dict, dict]:
    """The three JSON blocks and the three tables, checked against each other."""
    blocks = {}
    for name in INPUT_JSON:
        with open(os.path.join(raw_dir, name), encoding="utf-8") as f:
            blocks[name] = json.load(f)
    tables = {name: pd.read_csv(os.path.join(raw_dir, name)) for name in TABLES}
    summary, pair_changes, totals = (blocks[n] for n in INPUT_JSON)
    checks = {
        "per_city rows vs summary.cities": (len(tables["per_city.csv"]), summary["cities"]),
        "pair_changes rows vs pair_changes.pairs": (
            len(tables["pair_changes.csv"]),
            pair_changes["pairs"],
        ),
        "pairs rows vs summary.pairs_total": (len(tables["pairs.csv"]), summary["pairs_total"]),
        "summary.cities vs walk_totals.cities": (
            summary["cities"],
            totals["walk_totals"]["cities"],
        ),
        "summary.as_of vs pair_changes.as_of": (summary.get("as_of"), pair_changes.get("as_of")),
        "summary.as_of vs walk_totals.as_of": (summary.get("as_of"), totals.get("as_of")),
    }
    bad = {k: v for k, v in checks.items() if v[0] != v[1]}
    if bad:
        raise SystemExit(f"raw outputs disagree, not one measurement: {bad}")
    return blocks, tables


def strip_provenance(block: dict) -> tuple[dict, dict]:
    """Split a measurement script's output into its provenance and its numbers."""
    prov = {k: block[k] for k in PROVENANCE_KEYS if k in block}
    rest = {k: v for k, v in block.items() if k not in PROVENANCE_KEYS}
    return prov, rest


def derive(backfill: dict, change: dict, totals: dict, tables: dict) -> dict:
    """Every share, ratio and duration the writeup quotes, from the blocks and tables."""
    pc = tables["per_city.csv"]
    pcs = tables["pair_changes.csv"]
    good = pcs[pcs["match_frac"] >= 0.95]
    google = backfill["google_samples"]
    samples = backfill["samples"]
    within = 1 - backfill["distinct_google_sum_per_city"] / google
    global_ = 1 - backfill["distinct_google_global"] / google
    units = {
        "walk_edge": totals["edges_total"],
        "distinct_google_pano": backfill["distinct_google_global"],
        "walk_sample": samples,
    }
    top_decile = pc["distinct_google"].sort_values(ascending=False).head(len(pc) // 10)
    top = good.sort_values("new_capture_distinct_panos", ascending=False).head(TOP_CONTRIBUTORS)
    google_share = (pc["google"] / pc["samples"]).tolist()
    new_per_day_per_1m = change["new_capture_panos_per_day_per_1M_google_samples"]
    scaled_per_day = new_per_day_per_1m * google / 1e6
    # The ongoing design re-queries every sample whose pano changed or that gained
    # imagery (a walk diff is the trigger, never the answer), so its load is the
    # candidate set, not the new captures the candidates turn out to hold.
    candidates = change["changed"] + change["gained"]
    candidates_per_new = candidates / change["new_capture_distinct_panos"]
    candidates_per_day = scaled_per_day * candidates_per_new
    return {
        "present_share_of_samples": backfill["present_samples"] / samples,
        "google_share_of_samples": google / samples,
        "unanchored_samples": samples - google,
        "unanchored_share_of_google_samples": (samples - google) / google,
        "dedup_within_city_share": within,
        "dedup_global_share": global_,
        "dedup_global_extra_percentage_points": (global_ - within) * 100,
        "top_decile_cities_share_of_distinct_google": float(
            top_decile.sum() / backfill["distinct_google_sum_per_city"]
        ),
        "google_share_per_city_pctiles": {
            str(q): percentile(google_share, q) for q in GOOGLE_SHARE_PCTILES
        },
        "distinct_google_per_walk_edge": backfill["distinct_google_global"] / totals["edges_total"],
        "requests_per_unit": units,
        "days_at_requests_per_minute": {
            str(rpm): {unit: n / (rpm * 60 * 24) for unit, n in units.items()}
            for rpm in REQUESTS_PER_MINUTE
        },
        "google_both_share_of_google_samples": change["google_both"] / google,
        "changed_share_of_google_both": change["changed"] / change["google_both"],
        "changed_newer_share": change["changed_newer_date"] / change["changed"],
        "changed_same_share": change["changed_same_date"] / change["changed"],
        "changed_older_share": change["changed_older_date"] / change["changed"],
        "changed_undated": change["changed"]
        - change["changed_newer_date"]
        - change["changed_same_date"]
        - change["changed_older_date"],
        "pairs_with_no_new_capture": int(
            ((good["changed_newer_date"] + good["gained"]) == 0).sum()
        ),
        "top_contributors": top["city_id"].tolist(),
        "top_contributors_share_of_new_captures": float(
            top["new_capture_distinct_panos"].sum() / change["new_capture_distinct_panos"]
        ),
        "new_panos_per_day_scaled_to_all_google_samples": scaled_per_day,
        "requests_per_second_if_each_new_pano_requeried": scaled_per_day / 86400,
        "requery_candidates": candidates,
        "requery_candidates_per_new_capture_pano": candidates_per_new,
        "requery_candidates_per_day_scaled_to_all_google_samples": candidates_per_day,
        "requests_per_second_if_each_candidate_requeried": candidates_per_day / 86400,
    }


def build_record(blocks: dict, tables: dict, generated_by: str, label: str) -> dict:
    summary, pair_changes, totals_block = (blocks[n] for n in INPUT_JSON)
    inputs = {}
    inputs["summary.json"], backfill = strip_provenance(summary)
    inputs["pair_changes.json"], change = strip_provenance(pair_changes)
    inputs["walk_totals.json"], totals_rest = strip_provenance(totals_block)
    backfill.pop("walk_totals", None)
    totals = totals_rest["walk_totals"]
    return {
        "generated_by": generated_by,
        "catalog_label": label,
        "inputs": inputs,
        "backfill": backfill,
        "change_rate": change,
        "walk_totals": totals,
        "derived": derive(backfill, change, totals, tables),
    }


def write_docs(record: dict, raw_dir: str, docs_dir: str) -> str:
    os.makedirs(docs_dir, exist_ok=True)
    path = os.path.join(docs_dir, METRICS_NAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    for raw_name, docs_name in TABLES.items():
        shutil.copyfile(os.path.join(raw_dir, raw_name), os.path.join(docs_dir, docs_name))
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--raw-dir", default=RAW_DIR_DEFAULT, help="the two scripts' output dir")
    p.add_argument("--docs-dir", default=DOCS_DIR_DEFAULT, help="where the record is committed")
    p.add_argument(
        "--catalog-label",
        default="unspecified",
        help="which catalog was read (a label, never a path), recorded in the file",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    blocks, tables = read_raw(args.raw_dir)
    stamp = docs_generated_by(args.raw_dir, args.docs_dir, args.catalog_label)
    record = build_record(blocks, tables, stamp, args.catalog_label)
    path = write_docs(record, args.raw_dir, args.docs_dir)
    print(json.dumps(record["derived"], indent=2))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
