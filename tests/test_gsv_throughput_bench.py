"""Invariants of the GSV throughput bench (issue #304).

`scripts/gsv_throughput_bench.py` writes `docs/experiments/gsv-throughput_metrics.json`.
Its measurement needs a local TLS server and ~100 minutes, so these pin what
decides the committed numbers without running it: every cell is summarized by
the shared `experiment_stats.describe` (so this study's percentiles are the same
ruler as every other writeup's), the "where the limiter binds" selection is a
written rule rather than a hand-picked set, and neither a dirty tree nor an
unpushed HEAD can produce a record that names a commit nobody can check out.
"""

import argparse
import subprocess

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


def test_a_cell_whose_old_pacer_arm_has_no_steady_rate_is_not_selected():
    """#304 review: an old-pacer cell with an empty steady field ({"n": 0}) has
    no p50, and selecting it raised KeyError at the end of a 100-minute sweep.
    The fixed arm alone sitting on the rate is not enough to select a cell."""
    records = [
        _rec("s", 0.03, "depth4", 48001),
        _rec("s", 0.03, "depth4-oldpacer", 38107),
        _rec("s", 0.25, "depth4", 48002),
        _rec("s", 0.25, "depth4-oldpacer", None),
    ]
    cells = bench.summarize_cells(records)
    assert next(c for c in cells if c["coverage"] == 0.25 and c["arm"] == "depth4-oldpacer")[
        "steady_req_per_min"
    ] == {"n": 0}

    summary = bench.limiter_bound_summary(cells)

    assert summary["cells"] == [{"scenario": "s", "coverage": 0.03}]
    assert summary["oldpacer_steady_p50"] == {"min": 38107, "max": 38107}


def _sweep_until_the_guard(monkeypatch, tmp_path, state, **flags):
    monkeypatch.setattr(bench, "RAW_DIR", tmp_path)
    monkeypatch.setattr(bench, "tree_state", lambda repo=bench.REPO: state)
    monkeypatch.setattr(
        bench, "_extract_baseline", lambda ref: pytest.fail("ran past the provenance guard")
    )
    (tmp_path / "cert.pem").write_text("")  # skip the openssl call
    args = argparse.Namespace(allow_dirty=False, allow_unpushed=False)
    for k, v in flags.items():
        setattr(args, k, v)
    bench._sweep(args)


def test_a_dirty_tree_is_refused_before_anything_runs(monkeypatch, tmp_path):
    """A dirty tree measures code that no commit holds, so engine_head would
    name something other than what ran."""
    with pytest.raises(SystemExit, match="dirty tree"):
        _sweep_until_the_guard(monkeypatch, tmp_path, ("abc123", True, ["origin/b"]))


def test_an_unpushed_head_is_refused_before_anything_runs(monkeypatch, tmp_path):
    """The failure that actually happened: this study's first record named
    bd6ea3f, a real, CLEAN commit that was later amended away and never
    pushed. A dirty-tree check cannot see that; only "does any remote-tracking
    ref contain HEAD" can."""
    with pytest.raises(SystemExit, match="unpushed HEAD"):
        _sweep_until_the_guard(monkeypatch, tmp_path, ("abc123", False, []))


@pytest.mark.parametrize(
    "state, flags",
    [
        (("abc123", True, ["origin/b"]), {"allow_dirty": True}),
        (("abc123", False, []), {"allow_unpushed": True}),
    ],
)
def test_each_override_lets_its_own_state_through(state, flags):
    """--allow-dirty and --allow-unpushed are honored, each for its own state
    only: a guard that ignored its override would refuse every hand run."""
    bench.check_provenance(
        state[1],
        state[2],
        flags.get("allow_dirty", False),
        flags.get("allow_unpushed", False),
    )
    # ... and neither override covers the other state.
    with pytest.raises(SystemExit):
        bench.check_provenance(True, [], **{"allow_dirty": False, "allow_unpushed": True})
    with pytest.raises(SystemExit):
        bench.check_provenance(True, [], **{"allow_dirty": True, "allow_unpushed": False})


def _git(repo, *argv):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_tree_state_against_a_real_repository(tmp_path):
    """The real ``tree_state``, not a monkeypatch: an untracked file is not
    dirty (it cannot change what the engine imports), a modified tracked file
    is, and HEAD counts as pushed only once a remote-tracking ref contains it."""
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "engine.py").write_text("x = 1\n")
    _git(repo, "add", "engine.py")
    _git(repo, "commit", "-q", "-m", "one")
    (repo / "scratch.txt").write_text("untracked\n")

    head, dirty, remotes = bench.tree_state(repo)
    assert head == _git(repo, "rev-parse", "HEAD") and len(head) == 40
    assert dirty is False  # untracked only
    assert remotes == []  # never pushed

    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    assert bench.tree_state(repo)[2] == ["origin/main"]

    _git(repo, "commit", "-q", "--amend", "-m", "amended")  # the bd6ea3f shape
    assert bench.tree_state(repo)[1:] == (False, [])

    (repo / "engine.py").write_text("x = 2\n")
    assert bench.tree_state(repo)[1] is True
