#!/usr/bin/env python3
"""Classify matched-pano changes between each city's last two GSV drive walks (#442).

Joins samples on the collector's own coordinate key (``road_sampling.quantize_coord``,
9 decimals, the key ``compute_streetwalk_coverage`` joins on), so walks whose row order
or count differ still pair and two samples a metre apart are never merged into one.
A changed pano is split by whether its capture date moved: a newer date is a new
capture, the same date is most likely a re-issued pano id (with month-precision dates a
neighbouring frame of the same drive, or a second drive in the same month, reads the
same), an older date is Google changing which capture it serves. Reads local files
only; no network.

``--as-of DATE`` ignores walks dated after DATE, so the record can be re-made from a
catalog that has kept collecting. ``scripts/gsv_history_sizing_analyze.py`` assembles
this script's output into ``docs/experiments/gsv-history-sizing_metrics.json``.

Usage:
    python scripts/gsv_history_walk_pair_changes.py <repo_dir> <out_json> [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)
sys.path.insert(0, os.path.dirname(_SCRIPTS_DIR))

from experiment_stats import percentile  # noqa: E402

from streetscape_street_analyzer.road_sampling import quantize_coord  # noqa: E402

GOOGLE = "© Google"
PRESENT = {"OK", "NO_DATE"}
COLS = ["query_lat", "query_lon", "pano_id", "capture_date", "copyright_info", "status"]
SCRIPT = "scripts/gsv_history_walk_pair_changes.py"
MATCH_FRAC_MIN = 0.95

MATCH_FRAC_PCTILES = (0, 10, 50)
DAYS_PCTILES = (10, 50, 90)
NEW_FRAC_PCTILES = (25, 50, 75, 90, 99)


def pctiles(values, qs) -> dict:
    """``{str(q): percentile}`` through the shared study implementation; ``{}`` when empty."""
    vals = [float(v) for v in values]
    if not vals:
        return {}
    return {str(q): percentile(vals, q) for q in qs}


def load(path: Path) -> tuple[pd.DataFrame, int]:
    """The walk's rows keyed by ``quantize_coord``, and how many rows that key merged.

    The collector already dedups samples on this key, so the second number is 0 for
    any walk it wrote; it is recorded rather than assumed because a merge here
    would pair one walk's sample with a different sample of the other.
    """
    df = pd.read_csv(path, usecols=COLS, dtype=str)
    df["g"] = df["status"].isin(PRESENT) & (df["copyright_info"] == GOOGLE) & df["pano_id"].notna()
    # Keys from Python floats of the text (the scorer's own reading, #425), never
    # from numpy's round(), which can land a half-way value on the other side.
    df["k"] = [
        quantize_coord(float(la), float(lo))
        for la, lo in zip(df["query_lat"], df["query_lon"], strict=True)
    ]
    deduped = df.drop_duplicates("k")
    return deduped, int(len(df) - len(deduped))


def generated_by(repo: Path, out: Path, *, as_of: str | None = None) -> str:
    """The command that reproduces ``out``, as it was given (run from the checkout, ``.``)."""
    cmd = f"python {SCRIPT} {repo} {out}"
    if as_of is not None:
        cmd += f" --as-of {as_of}"
    return cmd


def measure(repo: Path, out: Path, *, as_of: str | None = None) -> dict:
    con = sqlite3.connect(f"file:{repo / 'data/streetscape_tracker.db'}?mode=ro", uri=True)
    clause, params = (" AND run_date <= ?", (as_of,)) if as_of is not None else ("", ())
    rows = con.execute(
        "SELECT city_id, run_date, csv_filename FROM street_walks "
        f"WHERE provider='gsv' AND network_type='drive'{clause} ORDER BY city_id, run_date DESC",
        params,
    ).fetchall()
    walks: dict[str, list[tuple[str, str]]] = {}
    for c, d, f in rows:
        walks.setdefault(c, []).append((d, f))
    recs, missing = [], []
    for city, ws in sorted(walks.items()):
        if len(ws) < 2:
            continue
        (d1, f1), (d0, f0) = ws[0], ws[1]
        paths = [repo / "data" / f for f in (f1, f0)]
        absent = [str(p.name) for p in paths if not p.exists()]
        if absent:
            missing.extend(absent)
            continue
        (a, dups_curr), (b, dups_prev) = load(paths[0]), load(paths[1])
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
                key_merged_curr=dups_curr,
                key_merged_prev=dups_prev,
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
    columns = [
        "city_id",
        "days",
        "samples_curr",
        "samples_prev",
        "key_merged_curr",
        "key_merged_prev",
        "matched",
        "google_both",
        "changed",
        "changed_newer_date",
        "changed_same_date",
        "changed_older_date",
        "gained",
        "lost",
        "new_capture_distinct_panos",
    ]
    df = pd.DataFrame(recs, columns=columns)
    df["match_frac"] = df["matched"] / df[["samples_curr", "samples_prev"]].max(axis=1)
    df["new_frac"] = (df["changed_newer_date"] + df["gained"]) / df["google_both"].clip(lower=1)
    good = df[df["match_frac"] >= MATCH_FRAC_MIN]
    s = {
        "generated_by": generated_by(repo, out, as_of=as_of),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "as_of": as_of,
        "pairs": int(len(df)),
        "missing_files": len(missing),
        "pairs_match_ge_95pct": int(len(good)),
        "key_merged_rows": int(df["key_merged_curr"].sum() + df["key_merged_prev"].sum()),
        "match_frac_pctiles": pctiles(df["match_frac"], MATCH_FRAC_PCTILES),
        "days_pctiles": pctiles(good["days"], DAYS_PCTILES),
        "google_both": int(good["google_both"].sum()),
        "changed": int(good["changed"].sum()),
        "changed_newer_date": int(good["changed_newer_date"].sum()),
        "changed_same_date": int(good["changed_same_date"].sum()),
        "changed_older_date": int(good["changed_older_date"].sum()),
        "gained": int(good["gained"].sum()),
        "lost": int(good["lost"].sum()),
        "new_capture_distinct_panos": int(good["new_capture_distinct_panos"].sum()),
        "new_frac_pctiles": pctiles(good["new_frac"], NEW_FRAC_PCTILES),
    }
    exposure = float((good["days"] * good["google_both"]).sum())
    s["new_capture_panos_per_day_per_1M_google_samples"] = (
        float(good["new_capture_distinct_panos"].sum() / exposure * 1e6) if exposure else None
    )
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
    p.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        help="ignore walks dated after this (re-makes a record from a catalog that kept collecting)",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.as_of is not None:
        # Normalized to YYYY-MM-DD (see size_gsv_history_walks.main).
        args.as_of = date.fromisoformat(args.as_of).isoformat()
    print(json.dumps(measure(args.repo_dir, args.out_json, as_of=args.as_of), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
