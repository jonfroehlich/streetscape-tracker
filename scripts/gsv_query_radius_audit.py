"""
How far from its query point is the pano Google returns? (issue #367)

Offline and read-only: no API calls, and the catalog is opened with SQLite's
``mode=ro`` rather than ``db.connect`` (which would MIGRATE it). The file list
comes from the catalog's ``runs.csv_filename`` for provider gsv -- never from a
glob of ``data/``, which holds thousands of files -- and each sampled run CSV
is read through ``fileutils.load_city_csv_file(..., raw=True)``, i.e. as Google
answered, before the query-radius rule this measurement justified.

For every row with a pano (status OK or NO_DATE, both pano coordinates
present) it takes the haversine distance to its query point
(``geoutils.haversine_m``, the same function the rule uses) and writes
``docs/experiments/gsv-query-radius_metrics.json``: files and rows measured,
the count and share beyond 50 / 100 / 1,000 m, how many files hold any row
beyond each, the per-city distribution of the >50 m share, each file's worst
case, and how many >50 m rows carry a non-Google copyright.

Usage:
    python scripts/gsv_query_radius_audit.py                         # 60 files, seed 0
    python scripts/gsv_query_radius_audit.py --sample 0              # every gsv run on disk
    python scripts/gsv_query_radius_audit.py --data-dir /path/to/data --sample 60 --seed 0
"""

import argparse
import json
import os
import random
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker.analysis import (  # noqa: E402
    GSV_QUERY_RADIUS_M,
    PRESENT_STATUSES,
    is_google_copyright,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.geoutils import haversine_m  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir, get_project_root  # noqa: E402

THRESHOLDS_M = (50, 100, 1000)
DEFAULT_OUT = os.path.join(
    get_project_root(), "docs", "experiments", "gsv-query-radius_metrics.json"
)


def _gsv_run_files(data_dir: str) -> list[tuple[str, str]]:
    """(city_id, csv_filename) for every cataloged gsv run whose CSV is on disk, sorted."""
    db_path = os.path.join(data_dir, "streetscape_tracker.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT city_id, csv_filename FROM runs WHERE provider = 'gsv' ORDER BY csv_filename"
        ).fetchall()
    finally:
        conn.close()
    return [(c, f) for c, f in rows if os.path.exists(os.path.join(data_dir, f))]


def _measure(path: str) -> dict:
    """Distances for one run CSV, read raw."""
    df = load_city_csv_file(path, raw=True)
    panos = df[
        df["status"].isin(PRESENT_STATUSES) & df["pano_lat"].notna() & df["pano_lon"].notna()
    ]
    d = np.asarray(
        haversine_m(panos["query_lat"], panos["query_lon"], panos["pano_lat"], panos["pano_lon"])
    )
    beyond = {t: int((d > t).sum()) for t in THRESHOLDS_M}
    far = panos[d > GSV_QUERY_RADIUS_M]
    copyright_known = far["copyright_info"].notna().to_numpy(dtype=bool)
    worst = None
    if len(d):
        i = int(np.argmax(d))
        row = panos.iloc[i]
        worst = {
            "distance_m": round(float(d[i]), 1),
            "query": [float(row["query_lat"]), float(row["query_lon"])],
            "pano": [float(row["pano_lat"]), float(row["pano_lon"])],
            "copyright_info": None
            if pd.isna(row["copyright_info"])
            else str(row["copyright_info"]),
        }
    return {
        "pano_rows": int(len(d)),
        "beyond": beyond,
        "far_non_google": int(
            (
                copyright_known
                & ~is_google_copyright(far["copyright_info"]).fillna(False).to_numpy(dtype=bool)
            ).sum()
        ),
        "far_copyright_unknown": int((~copyright_known).sum()),
        "worst": worst,
    }


def _pct(values, q):
    return round(float(np.percentile(values, q)), 3) if len(values) else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--sample", type=int, default=60, help="Files to sample (0 = every gsv run on disk)"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    candidates = _gsv_run_files(args.data_dir)
    if not candidates:
        print(f"No cataloged gsv run CSVs on disk under {args.data_dir}", file=sys.stderr)
        return 1
    if args.sample and args.sample < len(candidates):
        chosen = sorted(random.Random(args.seed).sample(candidates, args.sample))
    else:
        chosen = candidates

    per_file = []
    for i, (city_id, name) in enumerate(chosen, 1):
        print(f"[{i}/{len(chosen)}] {name}", file=sys.stderr)
        m = _measure(os.path.join(args.data_dir, name))
        per_file.append({"city_id": city_id, "csv_filename": name, **m})

    n_rows = sum(f["pano_rows"] for f in per_file)
    beyond = {t: sum(f["beyond"][t] for f in per_file) for t in THRESHOLDS_M}
    share_50 = [f["beyond"][50] / f["pano_rows"] * 100 for f in per_file if f["pano_rows"]]
    far_total = beyond[50]
    metrics = {
        "_about": {
            "experiment": "gsv-query-radius",
            "writeup": "docs/experiments/gsv-query-radius.md",
            "generated_by": (
                f"scripts/gsv_query_radius_audit.py --sample {args.sample} --seed {args.seed}"
            ),
            "note": (
                "Haversine distance from (query_lat, query_lon) to (pano_lat, pano_lon) over "
                "every row with a pano (status OK or NO_DATE, both pano coordinates present) "
                "in a seeded sample of the cataloged gsv run CSVs, read RAW (before the "
                "issue #367 query-radius rule). Shares are of rows with a pano; GSV holds one "
                "row per grid point, so they are also shares of covered grid points. The "
                "sampled files come from the catalog's runs.csv_filename, never a data/ glob."
            ),
        },
        "query_radius_m": GSV_QUERY_RADIUS_M,
        "candidate_files": len(candidates),
        "files_measured": len(per_file),
        "pano_rows": n_rows,
        "beyond_m": {
            str(t): {
                "rows": beyond[t],
                "share_pct": round(beyond[t] / n_rows * 100, 3) if n_rows else None,
                "files_with_any": sum(1 for f in per_file if f["beyond"][t]),
            }
            for t in THRESHOLDS_M
        },
        "per_file_share_beyond_50m_pct": {
            "n": len(share_50),
            "min": _pct(share_50, 0),
            "p10": _pct(share_50, 10),
            "p25": _pct(share_50, 25),
            "p50": _pct(share_50, 50),
            "p75": _pct(share_50, 75),
            "p90": _pct(share_50, 90),
            "max": _pct(share_50, 100),
        },
        "beyond_50m_non_google_copyright": {
            "rows": sum(f["far_non_google"] for f in per_file),
            "share_pct": round(sum(f["far_non_google"] for f in per_file) / far_total * 100, 3)
            if far_total
            else None,
            "copyright_unknown_rows": sum(f["far_copyright_unknown"] for f in per_file),
        },
        "worst_case_per_file": sorted(
            (
                {"city_id": f["city_id"], "csv_filename": f["csv_filename"], **f["worst"]}
                for f in per_file
                if f["worst"]
            ),
            key=lambda w: -w["distance_m"],
        ),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
