#!/usr/bin/env python3
"""Size a GSV capture-history backfill and its ongoing load from existing road walks (#442).

Reads the catalog and walk CSVs only; makes no network requests.

  1. Backfill size: distinct matched pano IDs across every city's latest GSV drive walk
     (all, and official '© Google' only), per city and globally deduplicated.
  2. Ongoing load: for cities with two or more walks, how many sample points changed
     matched pano between the last two walks, normalized per day. A pair is used only
     when both walks hold the identical sample list in the identical order
     (``aligned``); ``gsv_history_walk_pair_changes.py`` relaxes that by joining on
     coordinates and is the one to quote for change rates.
  3. Catalog totals for the same latest walks: edges, street-km and covered street-km,
     the sizes of a one-query-per-edge pass (``--catalog-totals-only`` writes just this
     block, sub-second, so it can be re-read without re-scanning 78 M walk rows).

This is the script that produced ``docs/experiments/gsv-history-sizing_metrics.json``
(``backfill``, ``walk_totals``); the argparse front end and the ``walk_totals`` block were
added after the 2026-10-09 run, whose as-run copy is recorded there by md5.

Usage:
    python scripts/size_gsv_history_walks.py <repo_dir> <out_json>
    python scripts/size_gsv_history_walks.py <repo_dir> <out_json> --catalog-totals-only
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PRESENT = {"OK", "NO_DATE"}
GOOGLE = "© Google"
COLS = ["query_lat", "query_lon", "pano_id", "copyright_info", "status"]

LATEST_WALKS_SQL = (
    "SELECT city_id, run_date, csv_filename FROM street_walks "
    "WHERE provider='gsv' AND network_type='drive' ORDER BY city_id, run_date DESC"
)


def h64(ids):
    """Stable 64-bit hash per pano id, so ~80 M ids fit in under 1 GB for a global unique."""
    return np.fromiter(
        (
            int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "little")
            for s in ids
        ),
        dtype=np.uint64,
        count=len(ids),
    )


def load(data_dir, name):
    df = pd.read_csv(
        data_dir / name, usecols=COLS, dtype={"pano_id": str, "copyright_info": str, "status": str}
    )
    df["present"] = df["status"].isin(PRESENT) & df["pano_id"].notna()
    df["google"] = df["present"] & (df["copyright_info"] == GOOGLE)
    return df


def open_catalog(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{repo / 'data/streetscape_tracker.db'}?mode=ro", uri=True)


def walks_by_city(con: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """city_id -> [(run_date, csv_filename), ...] newest first, GSV drive walks only."""
    walks: dict[str, list[tuple[str, str]]] = {}
    for city, d, f in con.execute(LATEST_WALKS_SQL).fetchall():
        walks.setdefault(city, []).append((d, f))
    return walks


def catalog_totals(con: sqlite3.Connection) -> dict:
    """Edge and street-km totals over each city's latest GSV drive walk.

    ``edges_total`` is the number of frozen-network edges the walk sampled (one
    query per edge is the issue's pass 1), ``length_km`` the street-km walked and
    ``length_km_covered`` the covered share. A walk collected before a column
    existed holds NULL there and is counted in ``walks_missing_<column>`` rather
    than silently as 0.
    """
    rows = con.execute(
        "SELECT w.city_id, w.edges_total, w.length_km, w.length_km_covered "
        "FROM street_walks w JOIN ("
        "  SELECT city_id, MAX(run_date) AS run_date FROM street_walks "
        "  WHERE provider='gsv' AND network_type='drive' GROUP BY city_id"
        ") latest ON latest.city_id = w.city_id AND latest.run_date = w.run_date "
        "WHERE w.provider='gsv' AND w.network_type='drive'"
    ).fetchall()
    df = pd.DataFrame(rows, columns=["city_id", "edges_total", "length_km", "length_km_covered"])
    out = {"cities": int(len(df))}
    for col in ("edges_total", "length_km", "length_km_covered"):
        present = df[col].dropna()
        out[col] = float(present.sum()) if col != "edges_total" else int(present.sum())
        out[f"walks_missing_{col}"] = int(df[col].isna().sum())
    out["edges_per_city_pctiles"] = {
        q: float(df["edges_total"].dropna().quantile(q / 100)) for q in (10, 50, 90, 99, 100)
    }
    return out


def measure(repo: Path, out: Path, *, totals_only: bool = False) -> dict:
    data_dir = repo / "data"
    con = open_catalog(repo)
    totals = catalog_totals(con)
    if totals_only:
        summary = {
            "generated_by": "size_gsv_history_walks.py --catalog-totals-only",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "walk_totals": totals,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2))
        return summary
    walks = walks_by_city(con)

    all_hashes, google_hashes = [], []
    per_city, pairs, missing = [], [], []
    t0 = time.time()
    for n, (city, ws) in enumerate(sorted(walks.items()), 1):
        d1, f1 = ws[0]
        if not (data_dir / f1).exists():
            missing.append(f1)
            continue
        a = load(data_dir, f1)
        ids_all = a.loc[a["present"], "pano_id"].unique()
        ids_g = a.loc[a["google"], "pano_id"].unique()
        all_hashes.append(h64(ids_all))
        google_hashes.append(h64(ids_g))
        rec = {
            "city_id": city,
            "samples": len(a),
            "present": int(a["present"].sum()),
            "google": int(a["google"].sum()),
            "distinct_all": len(ids_all),
            "distinct_google": len(ids_g),
        }
        if len(ws) > 1 and (data_dir / ws[1][1]).exists():
            d0, f0 = ws[1]
            b = load(data_dir, f0)
            days = (date.fromisoformat(d1) - date.fromisoformat(d0)).days
            aligned = (
                len(a) == len(b)
                and np.allclose(a["query_lat"], b["query_lat"])
                and np.allclose(a["query_lon"], b["query_lon"])
            )
            p = {"city_id": city, "prev": d0, "curr": d1, "days": days, "aligned": bool(aligned)}
            if aligned:
                both = a["google"].values & b["google"].values
                changed = both & (a["pano_id"].values != b["pano_id"].values)
                gained = a["google"].values & ~b["google"].values
                lost = ~a["google"].values & b["google"].values
                p.update(
                    samples=len(a),
                    changed=int(changed.sum()),
                    gained=int(gained.sum()),
                    lost=int(lost.sum()),
                    changed_distinct_new_panos=int(a.loc[changed | gained, "pano_id"].nunique()),
                )
            pairs.append(p)
        per_city.append(rec)
        if n % 100 == 0:
            print(f"{n}/{len(walks)} cities, {time.time() - t0:.0f}s", flush=True)

    ga = np.unique(np.concatenate(all_hashes)) if all_hashes else np.array([])
    gg = np.unique(np.concatenate(google_hashes)) if google_hashes else np.array([])
    pc = pd.DataFrame(per_city)
    pr = pd.DataFrame([p for p in pairs if p.get("aligned")])
    summary = {
        "generated_by": "size_gsv_history_walks.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cities": len(pc),
        "missing_files": len(missing),
        "samples": int(pc["samples"].sum()),
        "present_samples": int(pc["present"].sum()),
        "google_samples": int(pc["google"].sum()),
        "distinct_all_sum_per_city": int(pc["distinct_all"].sum()),
        "distinct_google_sum_per_city": int(pc["distinct_google"].sum()),
        "distinct_all_global": int(len(ga)),
        "distinct_google_global": int(len(gg)),
        "distinct_google_per_city_pctiles": {
            q: float(pc["distinct_google"].quantile(q / 100)) for q in (10, 25, 50, 75, 90, 99, 100)
        },
        "pairs_total": len(pairs),
        "pairs_aligned": len(pr),
        "walk_totals": totals,
    }
    if len(pr):
        pr["changed_frac"] = pr["changed"] / pr["samples"]
        pr["new_panos_per_day"] = pr["changed_distinct_new_panos"] / pr["days"].clip(lower=1)
        summary.update(
            pairs_days_pctiles={q: float(pr["days"].quantile(q / 100)) for q in (10, 50, 90)},
            pairs_samples=int(pr["samples"].sum()),
            pairs_changed=int(pr["changed"].sum()),
            pairs_gained=int(pr["gained"].sum()),
            pairs_lost=int(pr["lost"].sum()),
            pairs_changed_distinct_new_panos=int(pr["changed_distinct_new_panos"].sum()),
            pairs_changed_frac_pctiles={
                q: float(pr["changed_frac"].quantile(q / 100)) for q in (10, 25, 50, 75, 90, 99)
            },
            pairs_new_panos_per_day_sum=float(pr["new_panos_per_day"].sum()),
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    pc.to_csv(out.with_name("per_city.csv"), index=False)
    pd.DataFrame(pairs).to_csv(out.with_name("pairs.csv"), index=False)
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "repo_dir", type=Path, help="checkout holding data/streetscape_tracker.db and the walk CSVs"
    )
    p.add_argument(
        "out_json", type=Path, help="summary JSON; per_city.csv and pairs.csv land beside it"
    )
    p.add_argument(
        "--catalog-totals-only",
        action="store_true",
        help="write only the walk_totals block (catalog query, no CSV scan)",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    summary = measure(args.repo_dir, args.out_json, totals_only=args.catalog_totals_only)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
