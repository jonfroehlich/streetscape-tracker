#!/usr/bin/env python3
"""
How often does pandas' default C float parser misread a run CSV's coordinates,
and what did that cost the road-walk scorer? (issue #425)

`fileutils.load_city_csv_file` read with pandas' default C parser, which is not
correctly rounded. The CSV text is exact -- it is `repr` of the float the writer
had -- so the right value of a coordinate is Python's `float()` of its text, and
`float_precision="round_trip"` is the pandas option that reads exactly that.
This script measures the difference out of files already on disk, with no
network, and writes the committed record beside
docs/experiments/csv-float-parse.md:

    python scripts/csv_float_parse_analyze.py --data-dir data --docs-dir docs/experiments

THREE MEASUREMENTS
------------------
  walks    Every road-walk CSV the catalog names: each coordinate read three
           ways (default parser, round-trip, and `float()` of the text), how
           many come back off, by how much, and how many 9-decimal sample keys
           (`road_sampling.quantize_coord`, the key compute_streetwalk_coverage
           joins on) the default parse moves.
  scoring  The same walks re-scored by `recompute_streetwalk_stats.recompute_walk`
           against their frozen GraphML, once through the loader with the default
           parser and once with round-trip: the stat columns a walk would publish
           under each. Both use the CURRENT coverage definition, so the difference
           is #425's alone.
  grid     The largest dated grid runs per provider, loaded through the real
           loader under both parsers: whether any `calculate_run_stats` value,
           query-radius status, grid key or per-run JSON center/bounds moves.

The keys are built from Python floats (`.tolist()`), as the scorer's own `zip`
over a Series yields them. `round()` on an `np.float64` takes numpy's path,
which can land a half-way value on the other side, and would miscount.

Files are named by the catalog (`street_walks`, `runs`), never by globbing
data/. The catalog is opened READ-ONLY, so a dev copy at an older schema is
measured as it is and never migrated.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS_DIR)
sys.path.insert(0, os.path.dirname(_SCRIPTS_DIR))

from experiment_stats import describe  # noqa: E402

from streetscape_metadata_tracker import diff as diff_module  # noqa: E402
from streetscape_metadata_tracker.analysis import (  # noqa: E402
    calculate_run_stats,
    count_grid_points,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_street_analyzer.road_sampling import quantize_coord  # noqa: E402

DOCS_METRICS_NAME = "csv-float-parse_metrics.json"
DOCS_GENERATED_BY = "scripts/csv_float_parse_analyze.py --data-dir data --docs-dir docs/experiments"
COORD_COLUMNS = ["query_lat", "query_lon"]
# The scorer columns compared between the two parses (recompute_streetwalk_stats.STAT_COLUMNS
# minus coverage_by_highway, which is compared as a whole below).
SCORING_COLUMNS = (
    "edges_total",
    "edges_fully_covered",
    "mean_edge_coverage",
    "coverage_pct_by_length",
    "coverage_pct_by_length_any",
    "length_km",
    "length_km_covered",
    "length_km_covered_any",
    "median_covered_age_years",
)
# recompute_run_stats._equalish's tolerance: a stat "moves" only past it.
STAT_TOLERANCE = 1e-9
# Loads faster than this are timer noise, not a slowdown measurement.
SLOWDOWN_MIN_S = 0.1


@contextlib.contextmanager
def loader_parser(float_precision: str | None):
    """Run the REAL loader with ``pd.read_csv``'s float_precision forced.

    ``None`` is pandas' default parser -- what the loader used before #425.
    Patching the one call is what keeps every other step of the loader (dtypes,
    date parsing, the GSV query-radius gate) identical between the two reads.
    """
    real = pd.read_csv

    def read(*args, **kwargs):
        kwargs["float_precision"] = float_precision
        return real(*args, **kwargs)

    pd.read_csv = read
    try:
        yield
    finally:
        pd.read_csv = real


def _py_keys(lats, lons) -> set:
    """quantize_coord keys from Python floats, as the scorer's zip yields them."""
    return {quantize_coord(la, lo) for la, lo in zip(lats.tolist(), lons.tolist(), strict=True)}


def measure_walk_csv(path: str) -> dict:
    """Read one walk CSV's coordinates three ways and compare them.

    Raises AssertionError if the round-trip read is not ``float()`` of the
    text: that is the premise every other number here rests on.
    """
    t0 = time.perf_counter()
    default = pd.read_csv(path, usecols=COORD_COLUMNS)
    t1 = time.perf_counter()
    round_trip = pd.read_csv(path, usecols=COORD_COLUMNS, float_precision="round_trip")
    t2 = time.perf_counter()
    text = pd.read_csv(path, usecols=COORD_COLUMNS, dtype=str)
    out = {"rows": len(default)}
    for col, short in (("query_lat", "lat"), ("query_lon", "lon")):
        exact = np.array([float(t) for t in text[col]], dtype=float)
        if not np.array_equal(round_trip[col].to_numpy(), exact):
            raise AssertionError(f"{path}: round-trip {col} is not float() of its text")
        got = default[col].to_numpy()
        off = got != exact
        ulps = np.abs((got[off] - exact[off]) / np.spacing(exact[off])) if off.any() else []
        out[f"{short}_off"] = int(off.sum())
        out[f"{short}_off_share"] = round(float(off.mean()), 4) if len(off) else 0.0
        out[f"max_{short}_err_deg"] = float(np.abs(got - exact).max()) if len(got) else 0.0
        out[f"max_{short}_err_ulp"] = float(max(ulps, default=0.0))
    exact_keys = _py_keys(round_trip["query_lat"], round_trip["query_lon"])
    default_keys = _py_keys(default["query_lat"], default["query_lon"])
    out["keys_shifted"] = len(exact_keys - default_keys)
    out["parse_s_default"] = round(t1 - t0, 3)
    out["parse_s_round_trip"] = round(t2 - t1, 3)
    return out


def _stats_moved(a: dict, b: dict) -> list[str]:
    moved = []
    for key in a:
        x, y = a[key], b[key]
        if x is None or y is None:
            same = x is y or x == y
        elif isinstance(x, float) or isinstance(y, float):
            fx, fy = float(x), float(y)
            same = (math.isnan(fx) and math.isnan(fy)) or math.isclose(
                fx, fy, rel_tol=0, abs_tol=STAT_TOLERANCE
            )
        else:
            same = x == y
        if not same:
            moved.append(key)
    return moved


def measure_walk_scoring(row: dict, data_dir: str) -> dict:
    """Score one walk under each parser with the recompute's own code path.

    ``tolerance_only_matches`` is the recompute's ``n_noise``: samples whose
    exact 9-decimal key matched no CSV location, which the scorer's key join
    therefore scored uncovered.
    """
    import recompute_streetwalk_stats as rws

    csv_path = os.path.join(data_dir, row["csv_filename"])
    try:
        edges = rws.load_frozen_edges(row["city_id"], data_dir, row["network_type"])
        samples = rws.generate_samples(edges, rws._spacing_arg(row["spacing_m"]))
        stats, noise = {}, {}
        for name, precision in (("default", None), ("round_trip", "round_trip")):
            with loader_parser(precision):
                stats[name] = rws.recompute_walk(row, edges, data_dir, rws.Report()).stats
                noise[name] = rws.match_frame(samples, load_city_csv_file(csv_path))[0]
    except rws.WalkRefused as exc:
        return {"refused": exc.reason, "detail": str(exc)}
    default = {c: stats["default"][c] for c in SCORING_COLUMNS}
    round_trip = {c: stats["round_trip"][c] for c in SCORING_COLUMNS}
    moved = _stats_moved(default, round_trip)
    if json.loads(stats["default"]["coverage_by_highway"]) != json.loads(
        stats["round_trip"]["coverage_by_highway"]
    ):
        moved.append("coverage_by_highway")
    return {
        "default": default,
        "round_trip": round_trip,
        "moved": moved,
        "tolerance_only_matches": noise,
    }


def measure_grid_run(path: str, provider: str, run_date: str) -> dict:
    """Load one grid run through the real loader under both parsers and compare
    every number a grid consumer derives from its coordinates."""
    day = pd.Timestamp(run_date).date()
    derived = {}
    for name, precision in (("default", None), ("round_trip", "round_trip")):
        t0 = time.perf_counter()
        with loader_parser(precision):
            df = load_city_csv_file(path)
        elapsed = time.perf_counter() - t0
        derived[name] = {
            "rows": len(df),
            "parse_s": round(elapsed, 2),
            "stats": calculate_run_stats(df, day, provider=provider),
            "grid_points": count_grid_points(df),
            "grid_keys": diff_module._grid_keys(df),
            "status": df["status"].to_numpy(),
            "distance": (
                df["query_distance_m"].to_numpy(dtype=float)
                if "query_distance_m" in df.columns
                else None
            ),
            "center": (float(df["query_lat"].mean()), float(df["query_lon"].mean())),
            "bounds": tuple(
                float(v)
                for v in (
                    df["query_lat"].min(),
                    df["query_lat"].max(),
                    df["query_lon"].min(),
                    df["query_lon"].max(),
                )
            ),
        }
        del df  # one 16M-row frame at a time
    d, r = derived["default"], derived["round_trip"]
    out = {
        "provider": provider,
        "run_date": run_date,
        "rows": d["rows"],
        "stats_moved": _stats_moved(d["stats"], r["stats"]),
        "status_flips": int((d["status"] != r["status"]).sum()),
        "grid_points_equal": d["grid_points"] == r["grid_points"],
        "grid_keys_equal": bool(d["grid_keys"].equals(r["grid_keys"])),
        "center_equal": d["center"] == r["center"],
        "bounds_equal": d["bounds"] == r["bounds"],
        "parse_s_default": d["parse_s"],
        "parse_s_round_trip": r["parse_s"],
    }
    if d["distance"] is not None:
        out["max_query_distance_delta_m"] = float(np.nanmax(np.abs(d["distance"] - r["distance"])))
    return out


def _connect_read_only(data_dir: str) -> sqlite3.Connection:
    path = os.path.abspath(os.path.join(data_dir, "streetscape_tracker.db"))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect(data_dir: str, grid_sample: int) -> dict:
    conn = _connect_read_only(data_dir)
    walk_rows = [dict(r) for r in conn.execute("SELECT * FROM street_walks ORDER BY walk_id")]
    walks = []
    for row in walk_rows:
        path = os.path.join(data_dir, row["csv_filename"])
        if not os.path.isfile(path):
            walks.append({"csv_filename": row["csv_filename"], "missing": True})
            continue
        entry = {
            "csv_filename": row["csv_filename"],
            "city_id": row["city_id"],
            "provider": row["provider"],
            "network_type": row["network_type"],
            "run_date": row["run_date"],
            **measure_walk_csv(path),
        }
        scoring = measure_walk_scoring(row, data_dir)
        entry["scoring"] = scoring
        walks.append(entry)
        print(
            f"walk {row['city_id']} [{row['provider']}/{row['network_type']}] {row['run_date']}: "
            f"{entry['rows']} rows, lat off {entry['lat_off']}, lon off {entry['lon_off']}, "
            f"keys shifted {entry['keys_shifted']}, moved {scoring.get('moved', scoring)}"
        )
    grid = []
    if grid_sample:
        providers = [r[0] for r in conn.execute("SELECT DISTINCT provider FROM runs ORDER BY 1")]
        for provider in providers:
            runs = conn.execute(
                "SELECT csv_filename, run_date FROM runs WHERE provider = ? AND is_baseline = 0 "
                "ORDER BY total_points DESC LIMIT ?",
                (provider, grid_sample),
            ).fetchall()
            for fn, run_date in runs:
                path = os.path.join(data_dir, fn)
                if not os.path.isfile(path):
                    grid.append({"csv_filename": fn, "missing": True})
                    continue
                entry = {"csv_filename": fn, **measure_grid_run(path, provider, run_date)}
                grid.append(entry)
                print(
                    f"grid {fn}: {entry['rows']} rows, stats moved {entry['stats_moved']}, "
                    f"flips {entry['status_flips']}, {entry['parse_s_default']} -> "
                    f"{entry['parse_s_round_trip']} s"
                )
    conn.close()
    return {"walks": walks, "grid": grid}


def summarize(observations: dict) -> dict:
    walks = [w for w in observations["walks"] if not w.get("missing")]
    grid = [g for g in observations["grid"] if not g.get("missing")]
    # Round-trip over default loader wall-clock, for runs whose default load
    # took at least SLOWDOWN_MIN_S: below that the timer's resolution dominates.
    slowdown = [
        g["parse_s_round_trip"] / g["parse_s_default"]
        for g in grid
        if g["parse_s_default"] >= SLOWDOWN_MIN_S
    ]
    return {
        "walk_files": len(walks),
        "walk_rows": sum(w["rows"] for w in walks),
        "lat_off_share": describe([w["lat_off_share"] for w in walks], digits=4),
        "lon_off_share": describe([w["lon_off_share"] for w in walks], digits=4),
        "pooled_coord_off_share": round(
            sum(w["lat_off"] + w["lon_off"] for w in walks) / (2 * sum(w["rows"] for w in walks)), 4
        )
        if walks
        else None,
        "max_err_ulp": max(
            [max(w["max_lat_err_ulp"], w["max_lon_err_ulp"]) for w in walks], default=0.0
        ),
        "keys_shifted_total": sum(w["keys_shifted"] for w in walks),
        "walks_whose_scoring_moved": sum(1 for w in walks if w["scoring"].get("moved")),
        "grid_runs": len(grid),
        "grid_rows": sum(g["rows"] for g in grid),
        "grid_runs_with_stats_moved": sum(1 for g in grid if g["stats_moved"]),
        "grid_status_flips": sum(g["status_flips"] for g in grid),
        "grid_runs_with_grid_keys_changed": sum(1 for g in grid if not g["grid_keys_equal"]),
        "grid_runs_with_grid_points_changed": sum(1 for g in grid if not g["grid_points_equal"]),
        "grid_runs_with_center_changed": sum(1 for g in grid if not g["center_equal"]),
        "grid_runs_with_bounds_changed": sum(1 for g in grid if not g["bounds_equal"]),
        "grid_load_slowdown": describe(slowdown, digits=2),
    }


def docs_generated_by(data_dir: str, docs_dir: str, grid_sample: int) -> str:
    """The command that actually produced the record, for `_about.generated_by`.

    A fixed constant would let a scratch run claim the canonical provenance.
    The canonical run renders exactly DOCS_GENERATED_BY.
    """
    sample = "" if grid_sample == 4 else f" --grid-sample {grid_sample}"
    return f"scripts/csv_float_parse_analyze.py --data-dir {data_dir}{sample} --docs-dir {docs_dir}"


def build_record(observations: dict, generated_by: str) -> dict:
    return {
        "_about": {
            "experiment": "csv-float-parse",
            "writeup": "docs/experiments/csv-float-parse.md",
            "generated_by": generated_by,
            "pandas": pd.__version__,
            "note": (
                "Measured on the DEV catalog (a laptop copy), not production: the production "
                "re-measure is the walk recompute's dry run after deploy. `default` is pandas' "
                "default C float parser (the loader before #425); `round_trip` is "
                "float_precision='round_trip', which equals float() of the CSV text. Parse "
                "timings are wall-clock on the measuring machine."
            ),
        },
        "summary": summarize(observations),
        "observations": observations,
    }


def write_docs_record(record: dict, docs_dir: str) -> str:
    os.makedirs(docs_dir, exist_ok=True)
    path = os.path.join(docs_dir, DOCS_METRICS_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data-dir", default=None, help="data directory (default: the repo's)")
    ap.add_argument("--docs-dir", help="write the committed metrics JSON here")
    ap.add_argument(
        "--grid-sample",
        type=int,
        default=4,
        help="the N largest dated grid runs per provider to check (0 skips the grid check)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or get_default_data_dir()
    observations = collect(data_dir, args.grid_sample)
    summary = summarize(observations)
    print(json.dumps(summary, indent=2))
    if args.docs_dir:
        generated_by = docs_generated_by(args.data_dir or "data", args.docs_dir, args.grid_sample)
        path = write_docs_record(build_record(observations, generated_by), args.docs_dir)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
