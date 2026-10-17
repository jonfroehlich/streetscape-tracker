#!/usr/bin/env python3
"""
Summarize a GSV capture-history pilot crawl for its docs/experiments writeup (#442).

Reads the crawl's parsed files for one work list (pass 1, and pass 2 when
present) plus the documented-API spot-check metrics, and writes ONE metrics
JSON the writeup quotes from. Offline; no requests. Runs wherever the result
files are (the crawler host), and the JSON is what gets committed.

Per pass: requests, HTTP and response-code counts, how often the walk's
anchor pano appears in its own history, dated panos per sample (mean and
percentiles), zero-history samples, third-party rows, distinct official
panos, the capture-year histogram, and the achieved pace.
Across passes: per EDGE, how many distinct official panos pass 1's midpoint
found against pass 1 + 2 together -- the number that decides whether the
backfill is one query per edge or one per sample.

Usage:
    python scripts/gsv_history_pilot_analyze.py --results-dir /srv/crawler/results --worklist W.worklist.csv.gz \
        --spot-check spot.json --out gsv-history-pilot_metrics.json
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker.download_gsv_history import load_worklist  # noqa: E402

PCTILES = (10, 25, 50, 75, 90)


def _pct(series: pd.Series) -> dict[str, float]:
    return {str(q): float(series.quantile(q / 100)) for q in PCTILES} if len(series) else {}


def summarize_pass(queries: pd.DataFrame, history: pd.DataFrame, worklist_rows: int) -> dict:
    anchored = queries["anchor_in_history"].notna()
    in_hist = queries["anchor_in_history"].fillna(False).astype(bool)
    official = history[~history["third_party"].astype(bool)]
    fetched = pd.to_datetime(queries["fetched_at"], format="ISO8601", utc=True)
    elapsed_min = (fetched.max() - fetched.min()).total_seconds() / 60 if len(fetched) > 1 else None
    years = official["capture_date"].astype(str).str[:4]
    return {
        "worklist_rows": int(worklist_rows),
        "requests_answered": int(len(queries)),
        "http_status": {str(k): int(v) for k, v in queries["http_status"].value_counts().items()},
        "response_code": {
            str(k): int(v) for k, v in queries["response_code"].value_counts(dropna=False).items()
        },
        "anchored_samples": int(anchored.sum()),
        "anchor_in_history": int((anchored & in_hist).sum()),
        "anchor_in_history_share": round(float((anchored & in_hist).sum() / anchored.sum()), 4)
        if anchored.sum()
        else None,
        "dated_panos_per_sample_mean": round(float(queries["panos_dated"].mean()), 3),
        "dated_panos_per_sample_pctiles": _pct(queries["panos_dated"]),
        "zero_history_samples": int((queries["panos_dated"] == 0).sum()),
        "history_rows": int(len(history)),
        "third_party_rows": int(len(history) - len(official)),
        "distinct_official_panos": int(official["pano_id"].nunique()),
        "official_captures_per_sample_mean": round(float(len(official) / len(queries)), 3)
        if len(queries)
        else None,
        "capture_year_histogram": {
            str(k): int(v) for k, v in years.value_counts().sort_index().items()
        },
        "first_fetched_at": fetched.min().isoformat() if len(fetched) else None,
        "last_fetched_at": fetched.max().isoformat() if len(fetched) else None,
        "elapsed_min": round(elapsed_min, 2) if elapsed_min is not None else None,
        "achieved_rate_per_min": round(len(queries) / elapsed_min, 2) if elapsed_min else None,
    }


def summarize_edges(h1: pd.DataFrame, h2: pd.DataFrame) -> dict:
    """Distinct official panos per edge: pass 1's midpoint alone vs both passes."""
    off1 = h1[~h1["third_party"].astype(bool)]
    off2 = h2[~h2["third_party"].astype(bool)]
    p1 = off1.groupby("edge_id")["pano_id"].nunique()
    both = pd.concat([off1, off2]).groupby("edge_id")["pano_id"].nunique()
    edges = both.index.union(p1.index)
    p1 = p1.reindex(edges, fill_value=0)
    both = both.reindex(edges, fill_value=0)
    gain = both - p1
    ratio = both.sum() / p1.sum() if p1.sum() else None
    return {
        "edges": int(len(edges)),
        "distinct_official_panos_pass1_sum": int(p1.sum()),
        "distinct_official_panos_both_sum": int(both.sum()),
        "ratio_both_to_pass1": round(float(ratio), 4) if ratio else None,
        "edges_where_pass2_added_panos": int((gain > 0).sum()),
        "pass2_added_per_edge_pctiles": _pct(gain),
        "distinct_official_panos_global_pass1": int(off1["pano_id"].nunique()),
        "distinct_official_panos_global_both": int(pd.concat([off1, off2])["pano_id"].nunique()),
    }


def _read(path):
    return pd.read_csv(path, dtype={"pano_id": str, "edge_id": str, "anchor_pano_id": str})


def analyze(results_dir: str, worklist_path: str, spot_check_path: str | None) -> dict:
    worklist = load_worklist(worklist_path)
    stem = os.path.basename(worklist_path).removesuffix(".worklist.csv.gz")
    out: dict = {
        "generated_by": "scripts/gsv_history_pilot_analyze.py",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "worklist": os.path.basename(worklist_path),
        "city_id": str(worklist["city_id"].iloc[0]) if len(worklist) else None,
        "passes": {},
    }
    histories = {}
    for pass_no in (1, 2):
        q = glob.glob(os.path.join(results_dir, f"{stem}.pass{pass_no}.queries.csv.gz"))
        h = glob.glob(os.path.join(results_dir, f"{stem}.pass{pass_no}.history.csv.gz"))
        if not q:
            continue
        queries = _read(q[0])
        history = (
            _read(h[0])
            if h
            else pd.DataFrame(columns=["pano_id", "edge_id", "third_party", "capture_date"])
        )
        out["passes"][str(pass_no)] = summarize_pass(
            queries, history, int((worklist["pass"] == pass_no).sum())
        )
        histories[pass_no] = history
    if 1 in histories and 2 in histories:
        out["edges"] = summarize_edges(histories[1], histories[2])
    if spot_check_path and os.path.exists(spot_check_path):
        with open(spot_check_path, encoding="utf-8") as f:
            sc = json.load(f)
        out["spot_check"] = {
            k: sc.get(k)
            for k in (
                "requests",
                "statuses",
                "month_match",
                "month_mismatch",
                "pano_id_differs",
                "unknown",
                "error",
                "month_match_share_of_answered",
                "seed",
            )
        }
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--results-dir", required=True)
    p.add_argument("--worklist", required=True)
    p.add_argument("--spot-check", default=None, help="spot_check_gsv_history.py's metrics JSON")
    p.add_argument("--out", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    metrics = analyze(args.results_dir, args.worklist, args.spot_check)
    if not metrics["passes"]:
        print(f"no pass files for this work list under {args.results_dir}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
