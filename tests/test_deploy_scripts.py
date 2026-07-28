"""Guard tests for deploy_makelab1.sh.

The website deploy runs ``rsync --delete`` against the public docroot, which
sits right next to the ~15 GB (irreplaceable) ``data/`` directory. A wrong
filter could wipe it. These tests shell out to the REAL script in --dry-run mode
(so they can never drift from the actual filter list) and assert that data/ is
protected, legacy repo junk is swept, and dev tooling stays off the web server.

Skipped automatically where bash/rsync aren't available.
"""

import os
import shutil
import subprocess

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_PROJECT_ROOT, "deploy_makelab1.sh")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("rsync") is None,
    reason="deploy script test needs bash + rsync",
)


def test_deploy_dryrun_protects_data_sweeps_junk_excludes_devtooling(tmp_path):
    docroot = tmp_path / "docroot"
    # Protected content (must survive --delete):
    (docroot / "data").mkdir(parents=True)
    (docroot / "data" / "seattle.csv.gz").write_text("PRECIOUS 15GB")
    (docroot / "poster").mkdir()
    (docroot / "poster" / "p.png").write_text("x")
    # Legacy full-repo junk that a flatten should sweep:
    (docroot / "scripts").mkdir()
    (docroot / "scripts" / "old.py").write_text("junk")
    (docroot / "CLAUDE.md").write_text("junk")
    (docroot / "www").mkdir()  # legacy pre-flatten subdir
    (docroot / "www" / "stale.html").write_text("old")

    env = {**os.environ, "STREETSCAPE_DOCROOT": str(docroot)}
    # --dry-run skips git pull and applies nothing; runs against the repo's real www/.
    r = subprocess.run(
        ["bash", _SCRIPT, "--dry-run"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    out = r.stdout

    # data/ and poster/ are protected — never touched, never mentioned.
    assert "seattle.csv.gz" not in out
    assert "p.png" not in out

    # Legacy repo junk is swept by --delete.
    assert "scripts/old.py" in out
    assert "CLAUDE.md" in out
    assert "stale.html" in out  # the old docroot/www/ subdir

    # Dev tooling never reaches the web server.
    assert "node_modules" not in out

    # The site is flattened to the docroot root.
    assert "index.html" in out
    assert "city.html" in out


def _fake_systemctl(tmp_path, state):
    """A stub `systemctl` earlier on PATH than the real one, reporting the given
    is-active state. Lets the guard be tested on any host, including one with no
    systemd at all."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text(f"#!/usr/bin/env bash\nexit {0 if state == 'active' else 3}\n")
    stub.chmod(0o755)
    return str(bindir)


def test_deploy_refuses_to_pull_while_the_nightly_collector_is_running(tmp_path):
    """A fast-forward under a running collector kills it in its catalog tail —
    on 2026-07-28 that stranded a finished 611k-request Berlin road walk. The
    deploy must refuse before it pulls or rsyncs anything."""
    docroot = tmp_path / "docroot"
    (docroot / "data").mkdir(parents=True)
    (docroot / "data" / "seattle.csv.gz").write_text("PRECIOUS 15GB")

    env = {
        **os.environ,
        "STREETSCAPE_DOCROOT": str(docroot),
        "PATH": _fake_systemctl(tmp_path, "active") + os.pathsep + os.environ["PATH"],
    }
    r = subprocess.run(
        ["bash", _SCRIPT, "--yes"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert "refusing to pull code under it" in r.stdout
    # Refused BEFORE doing anything: no pull, no rsync.
    assert "git pull" not in r.stdout
    assert "index.html" not in r.stdout


def test_deploy_guard_allows_a_site_only_republish_during_a_run(tmp_path):
    """--skip-pull lands no new code, so it is safe (and often needed) while the
    nightly is mid-collection — the guard must not block it."""
    docroot = tmp_path / "docroot"
    (docroot / "data").mkdir(parents=True)

    env = {
        **os.environ,
        "STREETSCAPE_DOCROOT": str(docroot),
        "PATH": _fake_systemctl(tmp_path, "active") + os.pathsep + os.environ["PATH"],
    }
    r = subprocess.run(
        ["bash", _SCRIPT, "--skip-pull", "--dry-run"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    assert "refusing to pull" not in r.stdout
    assert "index.html" in r.stdout
