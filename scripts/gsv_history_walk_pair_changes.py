#!/usr/bin/env python3
"""Classify matched-pano changes between each city's last two GSV drive walks (#442).

Joins samples on rounded query coordinates (so walks whose row order or count differ still
pair), and splits a changed pano by whether its capture date moved: a newer date is a new
capture, the same date is most likely a re-issued pano id, an older date is Google changing
which capture it serves. Reads local files only; no network.

This is the script that produced the ``change_rate`` block of
``docs/experiments/gsv-history-sizing_metrics.json``; the argparse front end was added after
the 2026-10-09 run, whose as-run copy is recorded there by md5.

Usage:
    python scripts/gsv_history_walk_pair_changes.py <repo_dir> <out_json>
"""

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

GOOGLE = "© Google"
PRESENT = {"OK", "NO_DATE"}
COLS = ["query_lat", "query_lon", "pano_id", "capture_date", "copyright_info", "status"]


def load(path):
    df = pd.read_csv(path, usecols=COLS, dtype=str)
    df["g"] = df["status"].isin(PRESENT) & (df["copyright_info"] == GOOGLE) & df["pano_id"].notna()
    # ~1 m keys; sample points are deterministic from the frozen network, so equal points round equal.
    df["k"] = (
        df["query_lat"].astype(float).round(5).astype(str)
        + ","
        + df["query_lon"].astype(float).round(5).astype(str)
    )
    return df.drop_duplicates("k")


def measure(repo: Path, out: Path) -> dict:
    con = sqlite3.connect(f"file:{repo / 'data/streetscape_tracker.db'}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT city_id, run_date, csv_filename FROM street_walks "
        "WHERE provider='gsv' AND network_type='drive' ORDER BY city_id, run_date DESC"
    ).fetchall()
    walks = {}
    for c, d, f in rows:
        walks.setdefault(c, []).append((d, f))
    recs = []
    for city, ws in sorted(walks.items()):
        if len(ws) < 2:
            continue
        (d1, f1), (d0, f0) = ws[0], ws[1]
        a, b = load(repo / "data" / f1), load(repo / "data" / f0)
        m = a.merge(b, on="k", suffixes=("", "_p"), how="inner", validate="one_to_one")
        both = m["g"] & m["g_p"]
        ch = both & (m["pano_id"] != m["pano_id_p"])
        newer = ch & (m["capture_date"] > m["capture_date_p"])
        same = ch & (m["capture_date"] == m["capture_date_p"])
        older = ch & (m["capture_date"] < m["capture_date_p"])
        gained = m["g"] & ~m["g_p"]
        recs.append(
            dict(
                city_id=city,
                days=(date.fromisoformat(d1) - date.fromisoformat(d0)).days,
                samples_curr=len(a),
                samples_prev=len(b),
                matched=len(m),
                google_both=int(both.sum()),
                changed=int(ch.sum()),
                changed_newer_date=int(newer.sum()),
                changed_same_date=int(same.sum()),
                changed_older_date=int(older.sum()),
                gained=int(gained.sum()),
                lost=int((~m["g"] & m["g_p"]).sum()),
                new_capture_distinct_panos=int(
                    m.loc[newer | (gained & m["capture_date"].notna()), "pano_id"].nunique()
                ),
            )
        )
    df = pd.DataFrame(recs)
    df["match_frac"] = df["matched"] / df[["samples_curr", "samples_prev"]].max(axis=1)
    df["new_frac"] = (df["changed_newer_date"] + df["gained"]) / df["google_both"].clip(lower=1)
    good = df[df["match_frac"] >= 0.95]
    s = {
        "generated_by": "gsv_history_walk_pair_changes.py",
        "pairs": len(df),
        "pairs_match_ge_95pct": len(good),
        "match_frac_pctiles": {q: float(df["match_frac"].quantile(q / 100)) for q in (0, 10, 50)},
        "days_pctiles": {q: float(good["days"].quantile(q / 100)) for q in (10, 50, 90)},
        "google_both": int(good["google_both"].sum()),
        "changed": int(good["changed"].sum()),
        "changed_newer_date": int(good["changed_newer_date"].sum()),
        "changed_same_date": int(good["changed_same_date"].sum()),
        "changed_older_date": int(good["changed_older_date"].sum()),
        "gained": int(good["gained"].sum()),
        "lost": int(good["lost"].sum()),
        "new_capture_distinct_panos": int(good["new_capture_distinct_panos"].sum()),
        "new_frac_pctiles": {
            q: float(good["new_frac"].quantile(q / 100)) for q in (25, 50, 75, 90, 99)
        },
        "new_capture_panos_per_day_per_1M_google_samples": float(
            good["new_capture_distinct_panos"].sum()
            / (good["days"] * good["google_both"]).sum()
            * 1e6
        ),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(s, indent=2))
    df.to_csv(out.with_name("pair_changes.csv"), index=False)
    return s


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "repo_dir", type=Path, help="checkout holding data/streetscape_tracker.db and the walk CSVs"
    )
    p.add_argument("out_json", type=Path, help="summary JSON; pair_changes.csv lands beside it")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(measure(args.repo_dir, args.out_json), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
