#!/usr/bin/env python3
"""
How much imagery carries no usable capture date, per provider? (issue #257)

Issue #257 made a ``NO_DATE`` pano count as road-walk coverage, matching the
grid. Reviewing it surfaced the question this script answers: the docstrings
and docs said that population is "large by construction for KartaView, small
but real for Mapillary, and empty in practice for GSV" — three claims, none of
them a number, all of them load-bearing. They decide whether the one-time
phantom coverage delta the fix produces is invisible or is the largest change
in a city's walk series, and whether an age median taken over the dated subset
alone can still be read as the age of the imagery.

    python scripts/undated_imagery_share_analyze.py --docs-dir docs/experiments

TWO SAMPLING FRAMES, NEVER POOLED
---------------------------------
The catalog and the KartaView audit measure different things, and averaging
them would be meaningless:

  catalog   Our own dated grid snapshots, per (provider, run), from the
            ``runs`` table's ``status_ok``/``status_no_date`` counters. This is
            a census of what WE collected — 1,000+ GSV runs — and a thin one
            for KartaView: ``kartaview`` became a scheduler channel in #248,
            but an opt-in one, so it collects only enrolled cities, and few
            of those runs hold any imagery.
  audit     ``kartaview-shotdate-audit_metrics.json``, already committed beside
            ``kartaview-feasibility.md``: 48 sequences sampled from KartaView's
            API and tested against the ``shot_date >= date_added`` invariant.
            A sample of a provider, not a census of our data, and its own
            writeup calls it Grab-heavy and a lower bound.

So the cross-provider comparison is an order-of-magnitude one and is reported
as such. What it cannot be is a controlled comparison, and the writeup says so.

TWO DENOMINATORS, BOTH REPORTED
-------------------------------
They answer different questions and differ by the coverage rate:

  of_present   ``no_date / (ok + no_date)`` — of the imagery that EXISTS at a
               queried point, what share is undated. The provider-honesty
               number, and the one comparable to the KartaView audit's
               photos_invalid / photos_audited.
  of_queried   ``no_date / total_grid_points`` — the percentage-POINT shift a
               coverage rate takes when undated panos start counting. The
               phantom-delta number: ``coverage_pct_by_length`` is published to
               one decimal, so anything under 0.05 here rounds away entirely.

of_queried's denominator is the GRID size, ``runs.total_grid_points`` (issue
#289), never ``runs.total_points``. ``total_points`` and the status buckets are
ROW counts, and a census provider writes one row per IMAGE plus one per empty
point, so ``no_date / total_points`` divided images by a mixture of images and
points. That is what the first version of this script did, and it is why the
Mapillary ``pct_of_queried`` figures it committed (preserved at ``d974fd4``)
were wrong by construction; the committed file now carries the regeneration.

The denominator is fixed; the NUMERATOR is still the catalog's ``status_no_date``
and so still counts rows. For an ordinary gsv run a row is a point, so
of_queried is the exact shift -- but not for a legacy resumed gsv run that holds
several rows for one point, which is why ``of_queried_kind`` is decided from the
runs (``runs_with_more_rows_than_grid_points``), not from the provider alone.
For a census provider (``checkpointing.CENSUS_PROVIDERS``) the shift is
the share of grid points whose ONLY present imagery is undated, which is at
most the share holding any undated image, which is at most undated images /
grid points -- so the census figure is an UPPER BOUND on the shift, labelled
``of_queried_kind: "upper_bound"`` in the metrics, never the shift itself. The
exact census number needs the per-point join, i.e. the CSV, which the catalog
does not hold. A bound under 0.05 still settles "is it invisible?"; a bound
above it settles nothing.

A run whose ``total_grid_points`` is NULL (cataloged before v20 and not yet
backfilled by ``scripts/recompute_run_stats.py``) is NOT measured: it is left
out of every of_queried figure and counted in ``runs_without_grid_points``,
never read as zero and never replaced by ``total_points``.

WHAT THIS IS NOT
----------------
These are GRID runs. The fix was to the ROAD WALK, and walks record no undated
counter at all — which is exactly the gap #257 also closed by adding
``dated_covered_samples``/``covered_samples_dated`` to the walk artifact. Until
walks collected under that column accumulate, a walk's undated share has to be
inferred from the same provider's grid runs, and this script measures that
proxy rather than the thing itself. The two frames differ: a walk samples only
on-street points, where imagery is denser and, plausibly, better dated. Treat
the grid number as an estimate of the right ORDER, not as the walk's value.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.checkpointing import CENSUS_PROVIDERS  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

TOPIC = "undated-imagery-share"
DOCS_DIR_DEFAULT = "docs/experiments"
# The KartaView half is not re-measured here: it is already a committed
# measurement with its own writeup, and re-deriving it from a second API pass
# would produce a second number for one question.
KARTAVIEW_AUDIT = "kartaview-shotdate-audit_metrics.json"


def docs_generated_by(docs_dir: str, label: str) -> str:
    """The exact command that reproduces the committed metrics file.

    A function rather than a literal in the JSON, so
    ``tests/test_undated_imagery_share_analyze.py`` can assert the committed
    stamp names a run the repo can actually make (the grid-density precedent).

    `--catalog-label` is part of it because WHICH catalog was read is the
    single biggest determinant of these numbers and cannot be recovered from
    them afterwards: a dev laptop holds a handful of Mapillary runs against
    production's 1,200 at the first pass, which is the difference between
    "Mapillary never emits NO_DATE" and "Mapillary emits 12-17x GSV's rate"
    (17x at the first production pass, 12x at the regeneration). A label rather than a path,
    so no machine's directory layout lands in a committed file.
    """
    return (
        "python scripts/undated_imagery_share_analyze.py "
        f"--docs-dir {docs_dir} --catalog-label {label}"
    )


DEFAULT_LABEL = "unspecified"
DOCS_GENERATED_BY = docs_generated_by(DOCS_DIR_DEFAULT, DEFAULT_LABEL)


def percentiles(values: list[float]) -> dict:
    """n + the distribution, never a bare headline (CLAUDE.md's rule).

    Nearest-rank on a sorted list, no interpolation: the population here is
    per-run shares, and an interpolated p95 would be a value no run actually
    took.
    """
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def at(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(p / 100.0 * (len(ordered) - 1)))))
        return round(ordered[idx], 6)

    return {
        "n": len(ordered),
        "min": at(0),
        "p25": at(25),
        "p50": at(50),
        "p75": at(75),
        "p90": at(90),
        "p95": at(95),
        "max": at(100),
    }


def of_queried_kind(provider: str, runs_with_more_rows_than_points: int = 0) -> str:
    """Whether ``no_date / total_grid_points`` is the shift or a bound on it.

    "exact" only where one row is one grid point, so the NO_DATE row count IS
    the count of points covered only by undated imagery. That holds for a
    sample provider's ordinary run, but it is a property of each run's CSV, not
    of the provider: a legacy ``is_baseline=1`` gsv run that was resumed can
    hold several rows for one grid point (measured on a development catalog in
    the PR #422 review), and there a duplicated NO_DATE row is counted twice. So
    a provider is "exact" only when no measured run has more rows than grid
    points (``runs_with_more_rows_than_points``, counted by measure_catalog).

    "upper_bound" otherwise, and always for a census provider, whose NO_DATE
    rows are images: several can share one point, and a point holding an
    undated image may also hold a dated one (see the module docstring). In
    every case the NO_DATE row count is at least the number of points covered
    only by undated imagery, so the ratio can only overstate the shift.
    """
    if provider in CENSUS_PROVIDERS or runs_with_more_rows_than_points:
        return "upper_bound"
    return "exact"


def _argmax_run(values: list[tuple[float, str, str]]) -> dict | None:
    """The run behind a per-run maximum, so a writeup can name it from the JSON.

    ``values`` holds ``(pct, city_id, run_date)``; ties go to the first run in
    catalog order. None when there are no values.
    """
    if not values:
        return None
    pct, city, run_date = max(values, key=lambda v: v[0])
    return {"city_id": city, "run_date": run_date, "pct": round(pct, 6)}


def measure_catalog(conn: sqlite3.Connection) -> dict:
    """Per-provider undated share, pooled and as a per-run distribution."""
    out: dict = {}
    providers = [r[0] for r in conn.execute("SELECT DISTINCT provider FROM runs ORDER BY provider")]
    for provider in providers:
        # total_grid_points is NOT coalesced: NULL is "not measured" (issue
        # #289), and a 0 would silently drop the run as though it had queried
        # nothing rather than say it was never backfilled. total_points is read
        # only to compare rows with points (see of_queried_kind), never divided by.
        rows = conn.execute(
            "SELECT COALESCE(status_ok, 0), COALESCE(status_no_date, 0), "
            "total_grid_points, city_id, run_date, total_points FROM runs WHERE provider = ?",
            (provider,),
        ).fetchall()
        ok_total = sum(r[0] for r in rows)
        nd_total = sum(r[1] for r in rows)
        measured = [r for r in rows if r[2] is not None]
        queried_total = sum(r[2] for r in measured)
        # The pooled of_queried numerator is restricted to the SAME runs as its
        # denominator; pooling every run's NO_DATE over only the backfilled
        # runs' grid would overstate it.
        nd_measured = sum(r[1] for r in measured)
        present_total = ok_total + nd_total
        more_rows_than_points = sum(1 for r in measured if r[5] is not None and r[5] > r[2])

        of_present, of_queried = [], []
        for ok, nd, queried, city, run_date, _rows in rows:
            if ok + nd > 0:
                of_present.append((100.0 * nd / (ok + nd), city, run_date))
            if queried:
                of_queried.append((100.0 * nd / queried, city, run_date))

        out[provider] = {
            "runs": len(rows),
            "panos_present": present_total,
            "panos_no_date": nd_total,
            "points_queried": queried_total,
            "runs_without_grid_points": len(rows) - len(measured),
            "runs_with_more_rows_than_grid_points": more_rows_than_points,
            "of_queried_kind": of_queried_kind(provider, more_rows_than_points),
            "runs_with_any_no_date": sum(1 for r in rows if r[1] > 0),
            "pooled_pct_of_present": round(100.0 * nd_total / present_total, 6)
            if present_total
            else None,
            "pooled_pct_of_queried": round(100.0 * nd_measured / queried_total, 6)
            if queried_total
            else None,
            "per_run_pct_of_present": percentiles([v[0] for v in of_present]),
            "per_run_pct_of_queried": percentiles([v[0] for v in of_queried]),
            # The run behind each maximum, so a writeup that names the worst
            # run traces that name to this file rather than to a side query.
            "max_run_pct_of_present": _argmax_run(of_present),
            "max_run_pct_of_queried": _argmax_run(of_queried),
            # The pooled share is carried by a handful of runs, so the top
            # contributors ARE the finding rather than colour: a mean over a
            # distribution this concentrated describes no run in it. Same
            # reason the largest run is named -- one city's census can be most
            # of a provider's whole present-pano corpus.
            "top_no_date_runs": [
                {"city_id": city, "run_date": run_date, "no_date": nd}
                for _ok, nd, _q, city, run_date, _rows in sorted(rows, key=lambda r: -r[1])[:5]
                if nd > 0
            ],
            "largest_run_by_present_panos": max(
                (
                    {"city_id": city, "run_date": run_date, "panos_present": ok + nd}
                    for ok, nd, _q, city, run_date, _rows in rows
                ),
                key=lambda r: r["panos_present"],
                default=None,
            ),
        }
    return out


def read_kartaview_audit(docs_dir: str) -> dict:
    """The committed KartaView audit, restated in this measurement's units.

    Deliberately a READ of an existing metrics file rather than a fresh probe:
    the number already exists, has a writeup, and carries caveats this script
    has no standing to restate. Missing file -> an explicit null rather than a
    silent omission, so a reader can tell "not measured" from "measured zero".
    """
    path = os.path.join(docs_dir, KARTAVIEW_AUDIT)
    if not os.path.exists(path):
        return {"available": False, "source": KARTAVIEW_AUDIT}
    with open(path, encoding="utf-8") as fh:
        audit = json.load(fh)
    summary = audit.get("summary", {})
    photos = summary.get("photos_audited")
    invalid = summary.get("photos_invalid")
    return {
        "available": True,
        "source": KARTAVIEW_AUDIT,
        "frame": "API sample of 48 sequences, NOT our own snapshots",
        "sequences_audited": summary.get("sequences_audited"),
        "sequences_invalid": summary.get("sequences_invalid"),
        "photos_audited": photos,
        "photos_invalid": invalid,
        "pct_of_present": round(100.0 * invalid / photos, 6) if photos else None,
        "invalid_upload_dates": summary.get("invalid_upload_dates"),
        "note": (
            "Photos whose shot_date >= date_added, which the KartaView collector nulls to "
            "NO_DATE. Every violating sequence is one 2025-11-19 bulk ingest, so this "
            "population is the provider's NEWEST imagery, not a random sample of it -- "
            "dropping it from an age median biases the median OLD. The feasibility "
            "writeup calls this sample Grab-heavy and the count a lower bound."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--data-dir", default=None, help="data dir holding the catalog")
    parser.add_argument(
        "--docs-dir",
        default=None,
        help=f"write {TOPIC}_metrics.json here (default: print only)",
    )
    parser.add_argument(
        "--catalog-label",
        default=DEFAULT_LABEL,
        help="which catalog this read, e.g. 'makelab2-prod' (recorded, not a path)",
    )
    args = parser.parse_args()

    data_dir = args.data_dir or get_default_data_dir()
    conn = db.connect(db.get_default_db_path(data_dir))
    try:
        catalog = measure_catalog(conn)
    finally:
        conn.close()

    docs_dir = args.docs_dir or DOCS_DIR_DEFAULT
    metrics = {
        "_about": {
            "experiment": TOPIC,
            "writeup": f"docs/experiments/{TOPIC}.md",
            "generated_by": docs_generated_by(
                args.docs_dir or DOCS_DIR_DEFAULT, args.catalog_label
            ),
            "issue": 257,
            "catalog_label": args.catalog_label,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "note": (
                "Share of present imagery carrying no usable capture date, per provider. "
                "`catalog` is a census of our own GRID runs; `kartaview_audit` is a sample "
                "of KartaView's API, read from its own committed metrics file. The two are "
                "different sampling frames and are never pooled. Both are proxies for the "
                "ROAD-WALK undated share, which no walk recorded until #257 added "
                "dated_covered_samples. No data_dir is recorded: an absolute path would "
                "put one machine's layout into a committed file -- `catalog_label` names "
                "which catalog was read instead, since a dev catalog and production give "
                "materially different answers for the same provider."
            ),
        },
        "catalog": catalog,
        "kartaview_audit": read_kartaview_audit(docs_dir),
    }

    for provider, block in catalog.items():
        print(
            f"{provider}: {block['runs']} runs, {block['panos_present']:,} present panos, "
            f"{block['panos_no_date']:,} NO_DATE "
            f"({block['pooled_pct_of_present']}% of present, "
            f"{block['pooled_pct_of_queried']}% of queried, {block['of_queried_kind']}); "
            f"{block['runs_with_any_no_date']} runs carry any; "
            f"{block['runs_without_grid_points']} runs not yet backfilled with total_grid_points"
        )
    kv = metrics["kartaview_audit"]
    if kv["available"]:
        print(
            f"kartaview (API audit): {kv['photos_invalid']:,}/{kv['photos_audited']:,} photos "
            f"= {kv['pct_of_present']}% -- different frame, not comparable as a census"
        )

    if args.docs_dir:
        os.makedirs(args.docs_dir, exist_ok=True)
        out = os.path.join(args.docs_dir, f"{TOPIC}_metrics.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"Wrote {out}")
    else:
        print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
