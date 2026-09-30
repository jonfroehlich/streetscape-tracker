"""Invariants of the GSV throughput bench (issue #304).

`scripts/gsv_throughput_bench.py` writes `docs/experiments/gsv-throughput_metrics.json`.
Its measurement needs a local TLS server and ~100 minutes, so these pin what
decides the committed numbers without running it: every cell is summarized by
the shared `experiment_stats.describe` (so this study's percentiles are the same
ruler as every other writeup's), the "where the limiter binds" selection is a
written rule rather than a hand-picked set, and a dirty tree cannot produce a
record that names a commit it did not measure.
"""

import argparse

import pytest

from scripts import experiment_stats
from scripts import gsv_throughput_bench as bench


def _rec(scenario, cov, arm, steady, rep=0, **extra):
    base = {
        "scenario": scenario,
        "coverage": cov,
        "arm": arm,
        "rep": rep,
        "zero_ms": 15.0,
        "ok_ms": 45.0,
        "steady_req_per_min": steady,
        "whole_run_req_per_min": steady,
        "cpu_ms_per_request": 0.35,
        "client_connects": 50,
        "peak_rss_mb": 208.0,
    }
    base.update(extra)
    return base


def test_cells_are_summarized_by_the_shared_describe():
    records = [_rec("fit", 0.5, "depth4", v, rep=i) for i, v in enumerate([47990, 48010, 48000])]
    records.append(_rec("cpu-ceiling", 0.5, "depth4", None))
    cells = bench.summarize_cells(records)

    fit = next(c for c in cells if c["scenario"] == "fit")
    assert fit["steady_req_per_min"] == experiment_stats.describe([47990, 48010, 48000], digits=0)
    assert fit["steady_req_per_min"]["p50"] == 48000
    assert fit["steady_req_per_min"]["n"] == 3
    # A run shorter than the warmup window has no steady rate: an empty
    # describe, never a fabricated number or a crash.
    ceiling = next(c for c in cells if c["scenario"] == "cpu-ceiling")
    assert ceiling["steady_req_per_min"] == {"n": 0}


def test_the_limiter_bound_selection_is_the_written_rule():
    """Only cells where the fixed depth-4 engine sits ON the configured rate
    count: a refill burst above it (51k) and a socket-bound cell below it (38k)
    are not "where the limiter binds", and must not widen the old bucket's
    quoted range."""
    records = []
    for cov, fixed, old in [(0.03, 48001, 38107), (0.25, 51216, 44304), (0.75, 37917, 38016)]:
        records.append(_rec("s", cov, "depth4", fixed))
        records.append(_rec("s", cov, "depth4-oldpacer", old))
    summary = bench.limiter_bound_summary(bench.summarize_cells(records))

    assert summary["cells"] == [{"scenario": "s", "coverage": 0.03}]
    assert summary["oldpacer_steady_p50"] == {"min": 38107, "max": 38107}
    assert summary["oldpacer_fraction_of_configured"]["min"] == round(38107 / 48000, 4)
    assert "0.5%" in summary["rule"]


def test_a_dirty_tree_is_refused_before_anything_runs(monkeypatch, tmp_path):
    """The first record of this study named an engine sha that was later
    amended away, because nothing stopped a measurement of uncommitted code."""
    monkeypatch.setattr(bench, "RAW_DIR", tmp_path)
    monkeypatch.setattr(bench, "tree_state", lambda repo=bench.REPO: ("abc123", True))
    monkeypatch.setattr(
        bench, "_extract_baseline", lambda ref: pytest.fail("ran past the dirty-tree guard")
    )
    (tmp_path / "cert.pem").write_text("")  # skip the openssl call
    args = argparse.Namespace(allow_dirty=False)

    with pytest.raises(SystemExit, match="dirty tree"):
        bench._sweep(args)
