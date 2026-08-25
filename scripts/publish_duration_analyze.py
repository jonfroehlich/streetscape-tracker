#!/usr/bin/env python3
"""
How long does the nightly publish actually take? (issues #218, #230)

The scheduler's publish step — ``sync_data_to_server.sh``, an rsync of data/ to
the web docroot — was the only unbounded child in scheduler.py. Bounding it
needs a number, and CLAUDE.md's rule is that a number like that comes from a
measured distribution with a date on it, not from a guess. This script reads the
scheduler's own rotating logs and emits that distribution, plus the committed
record beside docs/experiments/publish-duration.md.

    python scripts/publish_duration_analyze.py --logs-dir logs \
        --time-tree-walk --docs-dir docs/experiments

THREE POPULATIONS, NEVER POOLED
-------------------------------
The logs carry three different things, and mixing any two of them produces a
number that is wrong in a direction nobody would notice:

  exact   `Published in N.N s`. The real measurement, added by #229/#206.
  bound   `Publishing via …` -> the timestamp of the NEXT scheduler-log line.
          An UPPER bound: that interval also contains whatever ran between the
          publish returning and the next thing being logged, which on the nights
          that have a successor line is the alert's SMTP send. This is the only
          population available before #229 is deployed, which is why it exists at
          all — and precisely why it must stay labelled.
  failed  `Publish script failed (…) after N.N s`. NOT a publish duration. A
          failure is sub-second BY CONSTRUCTION whenever the script refuses
          before transferring (a bad --local/SSH mode, an immediate rsync exit),
          so pooled into the healthy set it becomes the new minimum of the
          distribution the timeout is sized from and drags p25 down with it.
          Each failure therefore records `successor_delta_s` — how long after
          `Publishing via` the failure line was logged — as a deliberately
          NON-duration field, so that claim is checkable against the record
          instead of resting on prose. `seconds` stays null on the pre-#229
          line: counted, never timed.

A further trap the `bound` population has all to itself: a healthy pre-#229 night
logged NOTHING after `Publishing via`, so its successor line — if the file has
one at all — belongs to a LATER invocation hours away. Those are recognised
(a run-start or a per-city line, or a delta over --bound-cutoff-s) and reported
as `excluded`, with their count, rather than dropped silently. A sampling frame
that quietly discards what it cannot measure reads as coverage it never had.

Note the resulting selection bias, which is stated in the writeup and cannot be
removed from this side: pre-#229, only nights that ALERTED have a successor line,
so the `bound` population is drawn from nights that had a failed collection. That
is #218's complaint restated — the log gives no way to see a healthy publish.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from experiment_stats import describe as _describe  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The committed record beside the writeup. write_docs_record is its ONLY
# producer — CLAUDE.md requires the JSON a writeup cites to be written by
# committed code, and `generated_by` must stay a command the repo can run.
DOCS_METRICS_NAME = "publish-duration_metrics.json"
DOCS_GENERATED_BY = (
    "scripts/publish_duration_analyze.py --logs-dir logs --time-tree-walk "
    "--docs-dir docs/experiments"
)

# Above any plausible gap between the publish returning and the next line of the
# same invocation (the alert's SMTP send). A successor further out than this is
# a later invocation even when it is not one of the run-start shapes below.
DEFAULT_BOUND_CUTOFF_S = 300.0

_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
_LINE_RE = re.compile(rf"^{_TS} - ")
_PUBLISHING_RE = re.compile(rf"^{_TS} - .* - INFO - Publishing via (?P<cmd>.*)$")
_PUBLISHED_RE = re.compile(rf"^{_TS} - .* - INFO - Published in (?P<secs>[\d.]+) s")
_FAILED_RE = re.compile(
    rf"^{_TS} - .* - ERROR - Publish script failed \((?P<why>[^)]*)\) after (?P<secs>[\d.]+) s"
)
# Pre-#229 the failure line carried no elapsed at all, and it is the trap in this
# whole parser: it is the very next line after `Publishing via`, so the bound
# fallback below would score it as a HEALTHY publish of 0.05 s. Three such lines
# exist in the prod history, and pooling them drops p25 by more than half. Matched
# explicitly and filed under `failed` with a null duration — counted, never timed.
_FAILED_BARE_RE = re.compile(rf"^{_TS} - .* - ERROR - Publish script failed\s*$")
# A successor line that belongs to a DIFFERENT invocation of the scheduler. The
# first is a run-start banner; the second is the city loop, which cannot run
# after the tail's publish within one invocation.
_NEW_INVOCATION_RE = re.compile(r" - INFO - (?:\d[\d,]* cities due on |Collecting )")

_TS_FORMAT = "%Y-%m-%d %H:%M:%S,%f"


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _TS_FORMAT)


def publish_mode(cmd: str) -> str:
    """`local` when the publish targets an NFS-mounted docroot, else `ssh`.

    Worth splitting on: prod moved to --local in #215, so most of the history is
    a transport prod no longer uses, and the two need not have the same shape.
    """
    return "local" if "--local" in cmd else "ssh"


def percentiles(values: list[float]) -> dict:
    """n plus min/p25/p50/p75/p90/p95/max — never a bare median.

    CLAUDE.md: the shape is usually the finding, and a single headline number
    hides it. The formula is shared (see scripts/experiment_stats.py) so two
    writeups cannot quote p90s computed by two implementations; 3 decimals is
    this study's own resolution — seconds of publish wall-clock.
    """
    return _describe(values, digits=3)


def parse_log(text: str, bound_cutoff_s: float = DEFAULT_BOUND_CUTOFF_S) -> dict:
    """Extract the three populations plus the excluded bounds from one log file.

    Returns {"exact": [...], "bound": [...], "failed": [...], "excluded": [...]}
    with each observation a dict carrying at least `seconds`, `mode` and `at`.
    """
    lines = text.splitlines()
    out: dict[str, list[dict]] = {"exact": [], "bound": [], "failed": [], "excluded": []}

    for i, line in enumerate(lines):
        m = _PUBLISHING_RE.match(line)
        if not m:
            continue
        started_at, mode = m.group(1), publish_mode(m.group("cmd"))

        # The exact and failed forms are logged by _publish itself, so they are
        # the NEXT line of the same invocation when they exist at all. Scanning
        # forward rather than assuming adjacency keeps a stray line (a library
        # warning between the two) from demoting an exact reading to a bound.
        resolved = None
        for j in range(i + 1, min(i + 6, len(lines))):
            exact = _PUBLISHED_RE.match(lines[j])
            if exact:
                resolved = ("exact", float(exact.group("secs")), None, j)
                break
            failed = _FAILED_RE.match(lines[j])
            if failed:
                resolved = ("failed", float(failed.group("secs")), failed.group("why"), j)
                break
            if _FAILED_BARE_RE.match(lines[j]):
                resolved = ("failed", None, "pre-#229 line, elapsed not recorded", j)
                break
            if _PUBLISHING_RE.match(lines[j]):
                break
        if resolved:
            kind, secs, why, j = resolved
            obs = {
                "seconds": None if secs is None else round(secs, 3),
                "mode": mode,
                "at": started_at,
            }
            if why:
                obs["why"] = why
            if kind == "failed":
                # How long after `Publishing via` the failure was logged. NOT a
                # duration and deliberately not named like one — a refused
                # publish is sub-second, and pooling that into the healthy set
                # is this parser's sharpest hazard. Recording it is what makes
                # "it would be the new minimum" a checkable claim rather than a
                # sentence in a writeup, which is the same single-copy failure
                # CLAUDE.md's traceability rule exists to prevent. The
                # `excluded` rows below carry the identically-named field.
                obs["successor_delta_s"] = round(
                    (
                        _parse_ts(_LINE_RE.match(lines[j]).group(1)) - _parse_ts(started_at)
                    ).total_seconds(),
                    3,
                )
            out[kind].append(obs)
            continue

        # No outcome line: pre-#229 logs. Fall back to the interval to the next
        # log line of ANY kind, and say so in the record.
        successor = next(
            (lines[j] for j in range(i + 1, len(lines)) if _LINE_RE.match(lines[j])), None
        )
        if successor is None:
            out["excluded"].append(
                {"mode": mode, "at": started_at, "why": "no successor line (log ends here)"}
            )
            continue
        delta = (
            _parse_ts(_LINE_RE.match(successor).group(1)) - _parse_ts(started_at)
        ).total_seconds()
        if _NEW_INVOCATION_RE.search(successor):
            why = "successor belongs to a later invocation"
        elif delta > bound_cutoff_s:
            why = f"successor {delta:.0f}s away, over the {bound_cutoff_s:.0f}s cutoff"
        else:
            out["bound"].append(
                {"seconds": round(delta, 3), "mode": mode, "at": started_at, "upper_bound": True}
            )
            continue
        out["excluded"].append(
            {"mode": mode, "at": started_at, "why": why, "successor_delta_s": round(delta, 1)}
        )
    return out


def collect(paths: list[str], bound_cutoff_s: float = DEFAULT_BOUND_CUTOFF_S) -> dict:
    """Merge parse_log over every scheduler log, sorted by observation time."""
    merged: dict[str, list[dict]] = {"exact": [], "bound": [], "failed": [], "excluded": []}
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for key, rows in parse_log(fh.read(), bound_cutoff_s).items():
                merged[key].extend(rows)
    for rows in merged.values():
        rows.sort(key=lambda r: r["at"])
    return merged


def summarize(observations: dict) -> dict:
    """Per-population stats, and per-mode within the two timed populations."""
    summary = {}
    for key in ("exact", "bound", "failed"):
        rows = observations[key]
        # `seconds` is null on the pre-#229 bare failure line. Counted in `rows`,
        # excluded from every percentile — an untimed observation is not a zero.
        timed = [r for r in rows if r["seconds"] is not None]
        entry = {"overall": percentiles([r["seconds"] for r in timed])}
        if len(timed) != len(rows):
            entry["untimed"] = len(rows) - len(timed)
        # Summarized for `failed` only, and never merged into `overall`: it is
        # how fast the failure was LOGGED, not how long a publish took. It exists
        # so the writeup's "a refused publish would be the new minimum" is a
        # figure in the record rather than a claim in prose.
        deltas = [r["successor_delta_s"] for r in rows if r.get("successor_delta_s") is not None]
        if deltas:
            entry["successor_delta_s"] = percentiles(deltas)
        for mode in ("local", "ssh"):
            in_mode = [r["seconds"] for r in timed if r["mode"] == mode]
            if in_mode:
                entry[mode] = percentiles(in_mode)
        summary[key] = entry
    summary["excluded"] = {"n": len(observations["excluded"])}
    return summary


def published_volume(data_dir: str | None = None) -> tuple[int, int]:
    """(files, bytes) of what the publish would actually transfer.

    The walk time above says scanning is cheap; this says what the transfer is
    made of, and it is the number the ONE case the bound does not cover depends
    on — a cold publish into an empty docroot has to move all of it.

    The two patterns mirror sync_data_to_server.sh's FILTER_FLAGS whitelist
    (`*.csv.gz`, `*.json.gz`). Duplicated rather than parsed out of the shell,
    because a regex over someone else's argument array is the more fragile of
    the two couplings and this one is checked by the file COUNT landing beside
    rsync's own `N files to consider` in the same record.
    """
    root = (
        data_dir or os.environ.get("STREETSCAPE_LOCAL_DATA_DIR") or os.path.join(_REPO_ROOT, "data")
    )
    files = total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith((".csv.gz", ".json.gz")):
                files += 1
                total += os.path.getsize(os.path.join(dirpath, name))
    return files, total


# Repeats of the dry run, because ONE number would misrepresent it: the walk is
# an NFS stat() sweep, so it is dominated by the client's dentry cache. Measured
# on makelab2 2026-08-20, three passes ran 2.303 / 0.139 / 0.138 s — a 17x spread
# that a single figure would silently pick a side of. The first pass is reported
# as `as_found` (whatever cache state the host was in) and the rest as warm; the
# writeup quotes the range, not a point. Those are the figures in
# publish-duration_metrics.json; do not restate them from memory here.
_TREE_WALK_PASSES = 3


def time_tree_walk(publish_script: str | None = None, passes: int = _TREE_WALK_PASSES) -> dict:
    """Time the rsync's tree walk alone, via the publish script's own dry run.

    Read-only against the docroot, and it is the floor the transfer sits on top
    of: knowing the walk is at most a couple of seconds is what says the observed
    3-26 s is transfer rather than scanning, and therefore that the bound has to
    grow with the published VOLUME rather than with the file count.
    """
    script = publish_script or os.path.join(_REPO_ROOT, "sync_data_to_server.sh")
    seconds, files_considered = [], None
    for _ in range(passes):
        started = time.monotonic()
        proc = subprocess.run(
            ["bash", script, "--local", "--dry-run"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            return {"error": f"dry run exited {proc.returncode}", "seconds": round(elapsed, 3)}
        files = re.search(r"(\d[\d,]*) files to consider", proc.stdout)
        if files:
            files_considered = int(files.group(1).replace(",", ""))
        seconds.append(round(elapsed, 3))
    published_files, published_bytes = published_volume()
    return {
        "as_found_seconds": seconds[0],
        "warm_seconds": seconds[1:],
        "files_considered": files_considered,
        "published_files": published_files,
        "published_gb": round(published_bytes / 1024**3, 2),
        # Both stamped because this one measurement is host- and date-specific in
        # a way the log parsing is not: it walks an NFS-mounted docroot, so it
        # says nothing about a laptop and little about last month.
        "host": socket.gethostname(),
        "measured_at": datetime.now().date().isoformat(),
    }


def docs_generated_by(logs_dir: str, docs_dir: str, tree_walk: bool) -> str:
    """The command that actually produced the record, for `_about.generated_by`.

    A fixed constant would let `--docs-dir /tmp/scratch` write a file claiming it
    came from the canonical invocation — a provenance claim true of no run in
    particular. The canonical run renders exactly DOCS_GENERATED_BY. Mirrors
    pano_spacing_analyze.docs_generated_by.
    """
    walk = " --time-tree-walk" if tree_walk else ""
    return f"scripts/publish_duration_analyze.py --logs-dir {logs_dir}{walk} --docs-dir {docs_dir}"


def build_record(observations: dict, generated_by: str, tree_walk: dict | None = None) -> dict:
    """The committed record. Raw observations are included, not just the stats:
    there are ~20 of them, so every number the writeup quotes stays traceable to
    a line in a log rather than to a percentile someone has to trust."""
    record = {
        "_about": {
            "experiment": "publish-duration",
            "writeup": "docs/experiments/publish-duration.md",
            "generated_by": generated_by,
            "note": (
                "How long the nightly publish (sync_data_to_server.sh) takes, read out of "
                "logs/streetscape_scheduler.log*. Sizes scheduler.PUBLISH_TIMEOUT_S (issue "
                "#230). `exact` is the `Published in N.N s` line; `bound` is the pre-#229 "
                "fallback (`Publishing via` -> next log line) and is an UPPER bound that also "
                "contains the alert's SMTP send; `failed` is not a publish duration at all. "
                "The three are never pooled. The source logs stay on the collecting host."
            ),
        },
        "summary": summarize(observations),
        "observations": observations,
    }
    if tree_walk:
        record["tree_walk"] = {
            "measured_by": "bash sync_data_to_server.sh --local --dry-run",
            "note": (
                "The rsync file-list build alone, with nothing transferred — the floor the "
                "observed durations sit on top of."
            ),
            **tree_walk,
        }
    return record


def write_docs_record(record: dict, docs_dir: str) -> str:
    os.makedirs(docs_dir, exist_ok=True)
    path = os.path.join(docs_dir, DOCS_METRICS_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    return path


def _print_summary(summary: dict) -> None:
    for key in ("exact", "bound", "failed"):
        stats = summary[key]["overall"]
        label = {"exact": "exact", "bound": "UPPER BOUND", "failed": "failed (not a duration)"}[key]
        # Say how many observations exist but carry no duration, or a `failed
        # n=0` line reads as "no failures" when three are sitting in the record.
        untimed = summary[key].get("untimed", 0)
        suffix = f"  (+{untimed} untimed)" if untimed else ""
        if not stats["n"]:
            print(f"{label:24s} n=0{suffix}")
            continue
        print(
            f"{label:24s} n={stats['n']:<3d} min={stats['min']:.2f}  p25={stats['p25']:.2f}  "
            f"p50={stats['p50']:.2f}  p75={stats['p75']:.2f}  p90={stats['p90']:.2f}  "
            f"p95={stats['p95']:.2f}  max={stats['max']:.2f}{suffix}"
        )
    # Never a silent cap: what could not be measured is reported as a count.
    print(f"{'excluded':24s} n={summary['excluded']['n']} (successor from another invocation)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--logs-dir", default="logs", help="directory holding streetscape_scheduler.log*"
    )
    ap.add_argument("--docs-dir", help="write the committed metrics JSON here")
    ap.add_argument(
        "--time-tree-walk",
        action="store_true",
        help="also time `sync_data_to_server.sh --local --dry-run` (read-only; needs the docroot)",
    )
    ap.add_argument("--bound-cutoff-s", type=float, default=DEFAULT_BOUND_CUTOFF_S)
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.logs_dir, "streetscape_scheduler.log*")))
    if not paths:
        print(f"no scheduler logs under {args.logs_dir}", file=sys.stderr)
        return 1
    print(f"reading {len(paths)} log file(s) from {args.logs_dir}")

    observations = collect(paths, args.bound_cutoff_s)
    summary = summarize(observations)
    _print_summary(summary)

    tree_walk = time_tree_walk() if args.time_tree_walk else None
    if tree_walk:
        print(
            f"{'tree walk (dry run)':24s} as-found {tree_walk.get('as_found_seconds')} s, "
            f"warm {tree_walk.get('warm_seconds')} s over "
            f"{tree_walk.get('files_considered')} candidate files; "
            f"{tree_walk.get('published_files')} published files / "
            f"{tree_walk.get('published_gb')} GB"
        )

    if args.docs_dir:
        generated_by = docs_generated_by(args.logs_dir, args.docs_dir, args.time_tree_walk)
        path = write_docs_record(build_record(observations, generated_by, tree_walk), args.docs_dir)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
