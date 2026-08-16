"""Guard tests for sync_data_to_server.sh's publish filter.

data/ holds private working files (the SQLite catalog, logs, *.rejected
quarantine dumps, *.harvesting checkpoints) right next to the published
*.csv.gz / *.json.gz artifacts, and everything transferred lands on a
public web URL. rsync's default for a file matching NO filter rule is to
TRANSFER it, so the filter must be default-deny (trailing ``--exclude '*'``).
These tests shell out to the REAL script in --local --dry-run mode against a
synthetic data dir (so they can never drift from the actual filter list).

Skipped automatically where bash/rsync aren't available.
"""

import os
import shutil
import subprocess

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_PROJECT_ROOT, "sync_data_to_server.sh")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("rsync") is None,
    reason="sync script test needs bash + rsync",
)

# Everything the pipeline can leave in data/, and whether it may be published.
PUBLISHED = [
    "seattle--wa_width_20_height_20_step_1_2026-01-05.csv.gz",
    "seattle--wa_width_20_height_20_step_1_2026-01-05.json.gz",
    "seattle--wa_width_20_height_20_step_1_mapillary_2026-01-05.csv.gz",
    "seattle--wa_diff_2025-10-01_to_2026-01-05.csv.gz",
    "seattle--wa_width_20_height_20_step_1_gsv_history_2026-01-05.csv.gz",
    "cities.json.gz",
    "streetwalks.json.gz",
    # The DERIVED driving-plan join (issue #176). Its raw counterpart lives in
    # archive/gsv_driving_plan/ — outside data/ entirely, so the rsync never
    # walks it — while this join of Google's plan against our own observations
    # is published on purpose. See driving_plan.py's module docstring.
    "driving_plan.json.gz",
]
PRIVATE = [
    # Rejected runs (raw REQUEST_DENIED/quota dumps; cli.py renames to *.rejected)
    "bend--or_width_20_height_20_step_1_2026-01-05.csv.gz.rejected",
    # History-harvester resumable checkpoint (+ its atomic-write temp)
    "bend--or_width_20_height_20_step_1_gsv_history_2026-01-05.csv.gz.harvesting",
    "bend--or_width_20_height_20_step_1_gsv_history_2026-01-05.csv.gz.harvesting.tmp",
    # Catalog + sidecars (local-only by contract)
    "streetscape_tracker.db",
    "streetscape_tracker.db-wal",
    "streetscape_tracker.db-shm",
    "streetscape_tracker.db.backup",
    # Bare (uncompressed) artifacts and working files
    "bend--or_width_20_height_20_step_1_2026-01-05.csv",
    "bend--or_summary.json",
    "bend--or_width_20_height_20_step_1_2026-01-05.csv.downloading",
    "bend--or_failed_points.csv",
    "bend--or.lock",
    "stray.log",
    "stray.tmp",
    "preview.html",
    # A file type the filter list has never heard of: default-deny means it
    # must NOT transfer. Regression guard for the missing trailing
    # '--exclude *' that let *.rejected / *.harvesting reach the server.
    "unanticipated.futureformat",
]


def _run_dry_sync(tmp_path, names):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in names:
        (data_dir / name).write_text("x")
    docroot = tmp_path / "docroot"

    env = {
        **os.environ,
        "STREETSCAPE_LOCAL_DATA_DIR": str(data_dir),
        "STREETSCAPE_REMOTE_DATA_DIR": str(docroot),
        "STREETSCAPE_PUBLISH_LOCAL": "1",
    }
    r = subprocess.run(
        ["bash", _SCRIPT, "--dry-run", "--verbose"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    return r.stdout


def test_publish_filter_is_default_deny(tmp_path):
    out = _run_dry_sync(tmp_path, PUBLISHED + PRIVATE)
    # rsync lists one path per line; compare whole lines because private
    # names can contain published names as substrings ('x.csv.gz.rejected'
    # contains 'x.csv.gz').
    lines = {ln.strip() for ln in out.splitlines()}
    for name in PUBLISHED:
        assert name in lines, f"expected {name} to be published"
    for name in PRIVATE:
        assert name not in lines, f"PRIVATE file {name} would be published"


def test_nested_published_files_survive_whitelist(tmp_path):
    # The '*/' include must let rsync recurse into subdirectories while the
    # catch-all exclude still drops private files inside them.
    data_dir = tmp_path / "data"
    (data_dir / "archive").mkdir(parents=True)
    (data_dir / "archive" / "old_run.csv.gz").write_text("x")
    (data_dir / "archive" / "old_run.csv").write_text("x")
    docroot = tmp_path / "docroot"

    env = {
        **os.environ,
        "STREETSCAPE_LOCAL_DATA_DIR": str(data_dir),
        "STREETSCAPE_REMOTE_DATA_DIR": str(docroot),
        "STREETSCAPE_PUBLISH_LOCAL": "1",
    }
    r = subprocess.run(
        ["bash", _SCRIPT, "--dry-run", "--verbose"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}"
    lines = {ln.strip() for ln in r.stdout.splitlines()}
    assert "archive/old_run.csv.gz" in lines
    assert "archive/old_run.csv" not in lines
