"""
What capture-date formats are actually on disk, and what a strict reader cost
us (issue #226).

Reads data already collected — no network, no provider credentials, no new
requests. That makes this the smallest an experiment gets, and it is written up
anyway (docs/experiments/capture-date-precision.md), because the numbers behind
issue #226's fix decide two things that outlive the fix: whether
``format="ISO8601"`` is sufficient for the corpus, and how large the
``recompute_run_stats.py --regenerate-json`` repair pass actually is.

Three measurements, each independently selectable with --measure:

  sweep      Classify every ``capture_date`` value in every run CSV by ISO
             shape (day / month / year / absent / other). Answers "is any file
             on disk MIXING precisions?" — which is the case format-free
             ``pd.to_datetime`` silently mangles, and therefore the reason the
             format is pinned rather than inferred. Column-only read, so it is
             cheap relative to the corpus size.

  parse      Time the three parse strategies over a synthetic 3M-row column,
             and demonstrate the inference loss in BOTH orderings. Retires the
             "#157 said the census is memory/CPU-bound, so is ISO8601 slower?"
             objection with a number instead of an opinion.

  asymmetry  Recompute each catalogued gsv run's capture-date columns under
             BOTH readers (strict '%Y-%m-%d' and 'ISO8601') and compare each to
             the value stored in the catalog. The point is the DIRECTION: under
             the strict reader a repair pass can only ever clear a date column,
             never restore one, which is the tell that sent us looking for
             #226 in the first place. Full-corpus read; the slow one.

Usage:

    # everything, writing docs/experiments/capture-date-precision_metrics.json
    python scripts/capture_date_precision_analyze.py --measure all \
        --docs-dir docs/experiments

    # just the cheap halves
    python scripts/capture_date_precision_analyze.py --measure sweep,parse

Raw per-file rows are NOT written to data/ (the publisher rsyncs it to a public
web server) and are not written at all: the derived metrics JSON is small
enough to be the committed record, per the docs/experiments convention.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db, naming  # noqa: E402
from streetscape_metadata_tracker.analysis import calculate_run_stats  # noqa: E402
from streetscape_metadata_tracker.config import METADATA_DTYPES  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

TOPIC = "capture-date-precision"
MEASUREMENTS = ("sweep", "parse", "asymmetry")


def docs_generated_by(docs_dir: str) -> str:
    """The exact command that reproduces the committed metrics file.

    A constant rather than a literal in the JSON, so a test can assert the
    stamp names a run the repo can actually make — not a string any run could
    copy onto any file (the grid-density precedent).
    """
    return f"python scripts/capture_date_precision_analyze.py --measure all --docs-dir {docs_dir}"


DOCS_DIR_DEFAULT = "docs/experiments"
DOCS_GENERATED_BY = docs_generated_by(DOCS_DIR_DEFAULT)

# The ISO date shapes a run CSV can carry. Deliberately anchored and
# deliberately NOT a validity test: "2022-13" is shape-valid and calendar-
# invalid, and the question this measurement asks is which SHAPES a reader has
# to accept, not which values are real. Folding the calendar-invalid ones into
# "other" would understate the affected population by exactly the corrupt rows,
# which are the rows most worth knowing about. What the PARSERS make of a value
# is a separate question, answered by the asymmetry pass rather than here.
_SHAPE_PATTERNS = (
    ("day", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("month", re.compile(r"^\d{4}-\d{2}$")),
    ("year", re.compile(r"^\d{4}$")),
)
# The precisions that a strict '%Y-%m-%d' reader silently turns into NaT.
REDUCED_PRECISION_SHAPES = ("month", "year")


def classify_shape(value) -> str:
    """One capture_date cell -> 'day' | 'month' | 'year' | 'absent' | 'other'."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA:
        return "absent"
    text = str(value)
    # pandas' default NA list already turns the literal "None" a legacy writer
    # emitted into a real NA, but a CSV read as raw str elsewhere may not, so
    # the two spellings are folded here rather than counted as "other".
    if text == "" or text.lower() in {"none", "nan", "nat"}:
        return "absent"
    for name, pattern in _SHAPE_PATTERNS:
        if pattern.match(text):
            return name
    return "other"


def sweep_run_files(data_dir: str) -> dict:
    """Classify capture_date shapes across every run CSV in data_dir.

    Only files whose name parses as a RUN (naming.parse_filename) are read:
    diff, streetwalk and history artifacts live in the same directory and carry
    different schemas, and counting them would inflate the denominator the
    writeup quotes.
    """
    paths = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.csv.gz"))):
        try:
            provider = naming.parse_filename(os.path.basename(path)).provider
        except ValueError:
            continue
        paths.append((path, provider))

    row_shapes = Counter()
    files_by_shape_set = Counter()
    reduced_precision_files = []
    mixed_precision_files = []
    unreadable = []
    # Per provider, because the answer to "is this corpus really day precision?"
    # is not the same for a sample provider and a census one -- see day_on_first.
    by_provider = {}

    for i, (path, provider) in enumerate(paths, 1):
        name = os.path.basename(path)
        try:
            column = pd.read_csv(path, usecols=["capture_date"], dtype={"capture_date": str})[
                "capture_date"
            ]
        except Exception as exc:  # a truncated or schema-less file is data, not a crash
            unreadable.append({"file": name, "error": f"{type(exc).__name__}: {exc}"})
            continue

        values = column.to_numpy()
        counts = Counter(classify_shape(v) for v in values)
        row_shapes.update(counts)
        present = {s for s in counts if s != "absent" and counts[s]}
        files_by_shape_set["|".join(sorted(present)) if present else "absent-only"] += 1

        # How many DAY-shaped values land on the 1st. The question this answers
        # is whether the corpus's day precision is real: GSV publishes month
        # precision "most commonly" (download_gsv) and standardize_capture_date
        # pins it to the 1st at write time, so a day-shaped GSV corpus may be
        # month-precision data wearing a day-shaped spelling -- which decides
        # whether the legacy files are an exotic minority format or simply the
        # same data, unnormalized.
        day_on_first = sum(
            1 for v in values if isinstance(v, str) and len(v) == 10 and v.endswith("-01")
        )
        bucket = by_provider.setdefault(
            provider, {"files": 0, "rows_by_shape": Counter(), "day_rows_on_first": 0}
        )
        bucket["files"] += 1
        bucket["rows_by_shape"].update(counts)
        bucket["day_rows_on_first"] += day_on_first

        reduced = {s: counts[s] for s in REDUCED_PRECISION_SHAPES if counts.get(s)}
        if reduced:
            reduced_precision_files.append(
                {
                    "file": name,
                    "rows": int(len(column)),
                    "shape_counts": {k: int(v) for k, v in sorted(counts.items())},
                }
            )
        # "Mixing" is the specific hazard format-free inference mangles: two or
        # more DATED precisions in one column. Absent values are not a
        # precision and never confuse the inference.
        if len(present) > 1:
            mixed_precision_files.append({"file": name, "shapes": sorted(present)})

        if i % 100 == 0:
            print(f"  ... {i}/{len(paths)} files", flush=True)

    return {
        "files_scanned": len(paths),
        "files_unreadable": unreadable,
        "by_provider": {
            k: {
                "files": v["files"],
                "rows_by_shape": {s: int(n) for s, n in sorted(v["rows_by_shape"].items())},
                "day_rows_on_first": int(v["day_rows_on_first"]),
            }
            for k, v in sorted(by_provider.items())
        },
        "rows_total": int(sum(row_shapes.values())),
        "rows_by_shape": {k: int(v) for k, v in sorted(row_shapes.items())},
        "files_by_shape_set": dict(sorted(files_by_shape_set.items())),
        "files_with_reduced_precision": sorted(reduced_precision_files, key=lambda r: r["file"]),
        "files_mixing_precisions": mixed_precision_files,
    }


def _timed(fn, repeats: int = 3) -> float:
    """Best of `repeats` wall-clock seconds — the least noisy summary here."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return round(best, 3)


def benchmark_parsers(rows: int = 3_000_000) -> dict:
    """Cost of each parse strategy, plus what format-free inference loses.

    The inference case is measured in BOTH orderings on purpose: pandas reads
    ONE format off the first non-null value, so a one-way check passes by luck
    and reports the trap as absent.
    """
    day_only = pd.Series(
        pd.date_range("2015-01-01", periods=rows, freq="min").strftime("%Y-%m-%d"),
        dtype="object",
    )
    seconds = {
        "strict_ymd": _timed(lambda: pd.to_datetime(day_only, format="%Y-%m-%d", errors="coerce")),
        "iso8601": _timed(lambda: pd.to_datetime(day_only, format="ISO8601", errors="coerce")),
        "inferred": _timed(lambda: pd.to_datetime(day_only, errors="coerce")),
    }

    def survivors(values, **kwargs):
        parsed = pd.to_datetime(pd.Series(values, dtype="object"), errors="coerce", **kwargs)
        return [None if pd.isna(v) else v.date().isoformat() for v in parsed]

    orderings = {
        "day_first": ["2022-09-15", "2022-09"],
        "month_first": ["2022-09", "2022-09-15"],
    }
    inference_loss = {
        name: {
            "input": values,
            "inferred": survivors(values),
            "iso8601": survivors(values, format="ISO8601"),
        }
        for name, values in orderings.items()
    }
    return {
        "rows": rows,
        "pandas_version": pd.__version__,
        "seconds_best_of_3": seconds,
        "inference_loss": inference_loss,
    }


_DATE_COLUMNS = ("oldest_capture_date", "newest_capture_date", "median_pano_age_years")
# Columns calculate_run_stats reads: the shared core minus query_timestamp,
# which nothing in the stats path touches and which is the widest string column
# in the schema. Named rather than loading the whole frame because this pass
# opens every gsv run in the catalog -- a ~15 GB corpus -- and a census run's
# provider extras are dead weight here.
_STATS_COLUMNS = [
    "query_lat",
    "query_lon",
    "pano_lat",
    "pano_lon",
    "pano_id",
    "capture_date",
    "copyright_info",
    "status",
]


def _stats_under(
    parse_format: str,
    other_columns: pd.DataFrame,
    raw_dates: pd.Series,
    run_date: date,
    provider: str,
) -> dict:
    """calculate_run_stats over one run, with capture_date parsed one stated way.

    The real stats function is called rather than a re-derivation of its date
    path, so this measurement cannot drift away from what the catalog stores.

    The raw date strings are held apart from the other columns and re-attached
    with `assign` rather than copying the frame per reader: this pass opens
    every gsv run in the catalog, the largest of which is hundreds of MB
    compressed, and a `.copy()` per reader doubles peak memory for nothing.
    """
    parsed = pd.to_datetime(raw_dates, format=parse_format, errors="coerce")
    return calculate_run_stats(
        other_columns.assign(capture_date=parsed), run_date, provider=provider
    )


def _direction(stored, recomputed) -> str:
    """value->NULL, NULL->value, moved, or unchanged, for one column."""
    if stored is None and recomputed is None:
        return "unchanged"
    if stored is not None and recomputed is None:
        return "value_to_null"
    if stored is None and recomputed is not None:
        return "null_to_value"
    if isinstance(stored, float) or isinstance(recomputed, float):
        moved = abs(float(stored) - float(recomputed)) > 1e-9
    else:
        moved = stored != recomputed
    return "moved" if moved else "unchanged"


def measure_asymmetry(data_dir: str, provider: str = "gsv") -> dict:
    """Per-run direction of change under each reader, against the catalog.

    The finding is not the magnitude, it is that one reader's column of
    outcomes has an empty cell: a repair pass that can only ever CLEAR a date
    and never restore one is describing its own blind spot.
    """
    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    rows = conn.execute(
        "SELECT run_id, city_id, provider, run_date, csv_filename, "
        "oldest_capture_date, newest_capture_date, median_pano_age_years "
        "FROM runs WHERE provider = ? ORDER BY city_id, run_date",
        (provider,),
    ).fetchall()

    tallies = {r: Counter() for r in ("strict_ymd", "iso8601")}
    restored = []
    missing = 0
    failed = []
    for i, r in enumerate(rows, 1):
        path = os.path.join(data_dir, r["csv_filename"])
        if not os.path.exists(path):
            missing += 1
            continue
        try:
            raw = pd.read_csv(
                path,
                usecols=_STATS_COLUMNS,
                dtype={c: METADATA_DTYPES[c] for c in _STATS_COLUMNS},
            )
        except Exception as exc:
            failed.append({"file": r["csv_filename"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        raw_dates = raw.pop("capture_date")
        run_date = date.fromisoformat(r["run_date"])
        for reader, fmt in (("strict_ymd", "%Y-%m-%d"), ("iso8601", "ISO8601")):
            stats = _stats_under(fmt, raw, raw_dates, run_date, r["provider"])
            # One verdict per RUN, taken over the three date columns together:
            # they are a min, a max and a median of one population, so counting
            # them separately would report one run three times.
            directions = {_direction(r[c], stats[c]) for c in _DATE_COLUMNS}
            for verdict in ("null_to_value", "value_to_null", "moved"):
                if verdict in directions:
                    tallies[reader][verdict] += 1
                    break
            else:
                tallies[reader]["unchanged"] += 1
            if reader == "iso8601" and "null_to_value" in directions:
                restored.append(
                    {
                        "city_id": r["city_id"],
                        "run_date": r["run_date"],
                        "oldest_capture_date": stats["oldest_capture_date"],
                        "newest_capture_date": stats["newest_capture_date"],
                    }
                )
        if i % 50 == 0:
            print(f"  ... {i}/{len(rows)} runs", flush=True)
    conn.close()

    return {
        "provider": provider,
        "runs_in_catalog": len(rows),
        "runs_missing_csv": missing,
        "runs_unreadable": failed,
        "by_reader": {k: {d: int(n) for d, n in sorted(v.items())} for k, v in tallies.items()},
        "runs_restored_by_iso8601": sorted(restored, key=lambda r: r["city_id"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--measure",
        default="all",
        help="comma-separated: sweep, parse, asymmetry, or all (default: all)",
    )
    parser.add_argument("--data-dir", default=None, help="run CSVs + catalog (default: data/)")
    parser.add_argument(
        "--docs-dir",
        default=None,
        help=f"write {TOPIC}_metrics.json here (default: print only)",
    )
    parser.add_argument("--benchmark-rows", type=int, default=3_000_000)
    args = parser.parse_args()

    data_dir = args.data_dir or get_default_data_dir()
    wanted = {m.strip() for m in args.measure.split(",") if m.strip()}
    if "all" in wanted:
        wanted = {"sweep", "parse", "asymmetry"}
    unknown = wanted - set(MEASUREMENTS)
    if unknown:
        parser.error(f"unknown measurement(s): {', '.join(sorted(unknown))}")
    # A partial run must never overwrite the committed record with a subset:
    # the writeup quotes all three measurements, and a metrics file silently
    # missing one reads as "we never measured that" (grid-density's
    # --docs-dir-refuses-a-partial-area-set guard, same reasoning). Refused by
    # argparse, i.e. before any file is opened or any CSV is read.
    if args.docs_dir and wanted != set(MEASUREMENTS):
        parser.error("--docs-dir writes the committed record, so it requires --measure all")

    metrics = {
        "topic": TOPIC,
        "issue": 226,
        "writeup": f"docs/experiments/{TOPIC}.md",
        # The command that reproduces THIS file, per the docs/experiments rule
        # that every quoted number is traceable to committed code.
        "generated_by": docs_generated_by(args.docs_dir or DOCS_DIR_DEFAULT),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        # The pandas version is provenance, not trivia: the whole finding is
        # about how ONE library parses dates, and both the ISO8601 behaviour and
        # the format-free inference measured here are pandas' own.
        "python": sys.version.split()[0],
        "pandas": pd.__version__,
        # Deliberately no data_dir: the corpus is described by files_scanned /
        # rows_total / runs_in_catalog below, and an absolute path would put one
        # machine's layout into a committed file (grid-density_metrics.json
        # records none).
    }

    if "sweep" in wanted:
        print("Sweeping run CSVs for capture_date shapes ...", flush=True)
        metrics["sweep"] = sweep_run_files(data_dir)
        s = metrics["sweep"]
        print(
            f"  {s['files_scanned']} run files, {s['rows_total']:,} rows; "
            f"shapes: {s['rows_by_shape']}"
        )
        print(
            f"  {len(s['files_with_reduced_precision'])} files carry month/year "
            f"precision; {len(s['files_mixing_precisions'])} mix precisions"
        )

    if "parse" in wanted:
        print("Benchmarking parse strategies ...", flush=True)
        metrics["parse"] = benchmark_parsers(args.benchmark_rows)
        print(f"  seconds: {metrics['parse']['seconds_best_of_3']}")

    if "asymmetry" in wanted:
        print("Recomputing gsv date columns under both readers ...", flush=True)
        metrics["asymmetry"] = measure_asymmetry(data_dir)
        print(f"  {metrics['asymmetry']['by_reader']}")

    if args.docs_dir:
        os.makedirs(args.docs_dir, exist_ok=True)
        out = os.path.join(args.docs_dir, f"{TOPIC}_metrics.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"Wrote {out}")
    else:
        print(json.dumps(metrics, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
