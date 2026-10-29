"""The #442 crawler's deployment: deploy/crawler/ (issue #442).

The harvester runs on a dedicated host the repository never names, through a
queue runner and a system unit. These tests run the runner FOR REAL against a
stub interpreter, so the exit-code policy in its header (which harvester exits
end the queue, which it rides past, what it exits with) is what the script
does rather than what a comment says; read the exits the unit must not restart
on out of the harvester's own constants, so a renumbering fails here; pin the
unit to the checkout's runner, the crawler user and the sandbox; and refuse any
host identity in this directory and the harvester's docs, which is the one
rule a reviewer cannot see from the diff alone.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE  # noqa: E402
from streetscape_metadata_tracker.download_gsv_history import (  # noqa: E402
    HISTORY_BLOCKED_EXIT_CODE,
    HISTORY_BUSY_EXIT_CODE,
    LATCH_FILENAME,
    LOCK_FILENAME,
)

ROOT = Path(__file__).resolve().parent.parent
CRAWLER_DIR = ROOT / "deploy" / "crawler"
RUNNER = CRAWLER_DIR / "run_gsv_history_queue.sh"
UNIT = CRAWLER_DIR / "gsv-history-queue@.service"
README = CRAWLER_DIR / "README.md"

# How the unit and the runner spell the host's layout.
HOST_REPO = "/srv/streetscape-tracker"
HOST_ROOT = "/srv/crawler"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """{section: {key: [values, in order]}}; comments are not directives."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def _one(unit, section, key):
    values = unit.get(section, {}).get(key, [])
    assert len(values) == 1, f"expected exactly one {key}= in [{section}], got {values}"
    return values[0]


def _seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+)\s*(s|sec|min|m|h|d)?", value)
    assert m, f"unrecognized time span {value!r}"
    return (
        int(m.group(1))
        * {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}[m.group(2)]
    )


# ── The runner, run for real ───────────────────────────────────────────────

STUB = r"""#!/usr/bin/env bash
# Stands in for .venv/bin/python running scripts/harvest_gsv_history.py.
# Records the call, drains stdin (a runner that handed it the queue file would
# lose the rest of the queue), and exits with the code filed for its work list.
cat > /dev/null
echo "cwd=$PWD argv=$*" >> "$CRAWLER_ROOT/calls.log"
echo "harvester output for $*"
wl=""; while [ $# -gt 0 ]; do [ "$1" = "--worklist" ] && wl=$2; shift; done
stem=$(basename "$wl" .worklist.csv.gz)
rc_file="$CRAWLER_ROOT/rc/$stem"
if [ -f "$rc_file" ]; then exit "$(cat "$rc_file")"; fi
exit 0
"""


class Host:
    """A fake crawler host: a repo whose interpreter is the stub, a crawler root
    with work lists, and a queue file."""

    def __init__(self, tmp_path: Path, stems: list[str], rcs: dict[str, int] | None = None):
        self.repo = tmp_path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        python = self.repo / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(STUB)
        python.chmod(0o755)
        self.root = tmp_path / "crawler"
        (self.root / "worklists").mkdir(parents=True)
        (self.root / "rc").mkdir()
        self.worklists = {}
        for stem in stems:
            wl = self.root / "worklists" / f"{stem}.worklist.csv.gz"
            wl.write_bytes(b"not read by the stub")
            self.worklists[stem] = wl
        for stem, rc in (rcs or {}).items():
            (self.root / "rc" / stem).write_text(str(rc))
        self.queue = self.root / "worklists" / "queue-pass1.txt"
        self.queue.write_text("".join(f"{self.worklists[s]}\n" for s in stems))

    def run(self, pass_no="1", queue=None, extra_argv=()):
        env = {**os.environ, "CRAWLER_REPO": str(self.repo), "CRAWLER_ROOT": str(self.root)}
        argv = ["bash", str(RUNNER), str(pass_no), str(queue or self.queue), *extra_argv]
        return subprocess.run(argv, env=env, capture_output=True, text=True, cwd=str(self.root))

    def calls(self) -> list[str]:
        path = self.root / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def called_stems(self) -> list[str]:
        return [
            re.search(r"--worklist (\S+)", c).group(1).split("/")[-1].split(".")[0]
            for c in self.calls()
        ]

    def queue_log(self, pass_no=1) -> str:
        path = self.root / f"queue-pass{pass_no}.log"
        return path.read_text() if path.exists() else ""


def test_the_runner_walks_the_queue_in_order_one_harvester_at_a_time(tmp_path):
    host = Host(tmp_path, ["aaa", "bbb", "ccc"])
    # A blank line is skipped, not a work list named "".
    host.queue.write_text(
        host.queue.read_text().replace(f"{host.worklists['bbb']}\n", f"{host.worklists['bbb']}\n\n")
    )
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert host.called_stems() == ["aaa", "bbb", "ccc"]
    for call, stem in zip(host.calls(), ["aaa", "bbb", "ccc"], strict=True):
        # From the checkout, so scripts/... resolves; the out-dir is per work
        # list and the latch dir is the shared results root.
        assert call.startswith(f"cwd={host.repo} argv=")
        argv = shlex.split(call.split("argv=", 1)[1])
        assert argv[0] == "scripts/harvest_gsv_history.py"
        opts = dict(zip(argv[1::2], argv[2::2], strict=True))
        assert opts == {
            "--worklist": str(host.worklists[stem]),
            "--pass": "1",
            "--out-dir": str(host.root / "results" / stem),
            "--latch-dir": str(host.root / "results"),
        }
        # The harvester's own output lands beside its files, not in the journal.
        assert "harvester output for" in (host.root / "results" / stem / "pass1.log").read_text()
    log = host.queue_log()
    assert re.findall(r"(START|END) +pass=1 (\w+)", log) == [
        ("START", "aaa"),
        ("END", "aaa"),
        ("START", "bbb"),
        ("END", "bbb"),
        ("START", "ccc"),
        ("END", "ccc"),
    ]
    assert "QUEUE DONE pass=1 incomplete=0" in log


def test_a_final_queue_line_without_a_newline_is_still_a_work_list(tmp_path):
    """`read` returns false on an unterminated last line; a runner that stopped
    there would skip the last city, exit 0 and log a complete queue."""
    host = Host(tmp_path, ["aaa", "bbb"])
    host.queue.write_text(host.queue.read_text().rstrip("\n"))
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert host.called_stems() == ["aaa", "bbb"]
    # The pre-flight sees it too: missing, it refuses the queue.
    host.worklists["bbb"].unlink()
    assert host.run().returncode == 2


def test_an_incomplete_work_list_is_ridden_past_and_the_queue_ends_83(tmp_path):
    host = Host(tmp_path, ["aaa", "bbb", "ccc"], rcs={"bbb": SWEEP_INCOMPLETE_EXIT_CODE})
    r = host.run()
    assert r.returncode == SWEEP_INCOMPLETE_EXIT_CODE
    assert host.called_stems() == ["aaa", "bbb", "ccc"]
    log = host.queue_log()
    assert f"END   pass=1 bbb rc={SWEEP_INCOMPLETE_EXIT_CODE}" in log
    assert "QUEUE DONE pass=1 incomplete=1" in log
    assert "QUEUE STOPPED" not in log


@pytest.mark.parametrize(
    "rc",
    [HISTORY_BLOCKED_EXIT_CODE, HISTORY_BUSY_EXIT_CODE, 2, 1, 137],
    ids=["refused-or-latched", "lock-held", "our-own-refusal", "crash", "killed"],
)
def test_a_refusal_a_held_lock_a_bad_argument_or_a_crash_stops_the_queue_with_that_code(
    tmp_path, rc
):
    host = Host(tmp_path, ["aaa", "bbb", "ccc"], rcs={"bbb": rc})
    r = host.run()
    assert r.returncode == rc
    # ccc is never started: a latch is the address's, a lock is the host's, and
    # a refusal of our own argv is deterministic.
    assert host.called_stems() == ["aaa", "bbb"]
    log = host.queue_log()
    assert f"QUEUE STOPPED pass=1 rc={rc} (bbb)" in log
    assert "QUEUE DONE" not in log


def test_a_missing_work_list_refuses_the_whole_queue_before_the_first_request(tmp_path):
    host = Host(tmp_path, ["aaa", "bbb"])
    host.worklists["bbb"].unlink()
    r = host.run()
    assert r.returncode == 2
    assert "no such work list" in r.stderr and "nothing started" in r.stderr
    assert host.calls() == []
    assert host.queue_log() == ""


def test_the_runner_takes_a_pass_of_1_or_2_and_a_queue_file_only(tmp_path):
    host = Host(tmp_path, ["aaa"])
    assert host.run(pass_no="3").returncode == 2
    assert host.run(queue=host.root / "no-such-queue.txt").returncode == 2
    assert host.run(extra_argv=["surplus"]).returncode == 2
    assert host.calls() == []
    r = host.run(pass_no="2")
    assert r.returncode == 0
    assert "--pass 2" in host.calls()[0]
    assert "QUEUE DONE pass=2" in host.queue_log(2)
    assert (host.root / "results" / "aaa" / "pass2.log").exists()


def test_the_runner_is_executable_in_git():
    """The unit execs the checkout's copy, so the committed mode is what runs."""
    if shutil.which("git") is None:
        pytest.skip("needs git")
    r = subprocess.run(
        ["git", "ls-files", "-s", str(RUNNER.relative_to(ROOT))],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("not a git checkout")
    assert r.stdout.split()[0] == "100755", r.stdout


# ── The unit ───────────────────────────────────────────────────────────────


def test_the_unit_runs_the_checkouts_runner_as_crawler_for_the_pass_it_names():
    unit = _parse_unit(UNIT.read_text())
    argv = shlex.split(_one(unit, "Service", "ExecStart"))
    assert argv == [
        f"{HOST_REPO}/deploy/crawler/run_gsv_history_queue.sh",
        "%i",
        f"{HOST_ROOT}/worklists/queue-pass%i.txt",
    ]
    assert (ROOT / Path(argv[0]).relative_to(HOST_REPO)) == RUNNER
    assert _one(unit, "Service", "User") == "crawler"
    assert _one(unit, "Service", "WorkingDirectory") == HOST_REPO
    assert _one(unit, "Service", "Type") == "simple"
    assert _one(unit, "Install", "WantedBy") == "multi-user.target"
    # The runner's defaults are the unit's paths: a relocation on either side
    # (or the unit setting neither environment variable) must move both.
    runner = RUNNER.read_text()
    assert f"${{CRAWLER_REPO:-{HOST_REPO}}}" in runner
    assert f"${{CRAWLER_ROOT:-{HOST_ROOT}}}" in runner
    assert "Environment" not in unit["Service"]


def test_the_exits_the_unit_never_restarts_on_are_the_harvesters_own():
    """Restart=on-failure is for a crash. A latch (86) restarted every RestartSec
    would be refused each time for a day; a held lock (88) and our own refusal
    (2) repeat deterministically; an incomplete queue (83) is an operator's
    re-run after reading which work lists, not a loop."""
    unit = _parse_unit(UNIT.read_text())
    assert _one(unit, "Service", "Restart") == "on-failure"
    prevented = {int(c) for c in _one(unit, "Service", "RestartPreventExitStatus").split()}
    assert prevented == {
        2,
        SWEEP_INCOMPLETE_EXIT_CODE,
        HISTORY_BLOCKED_EXIT_CODE,
        HISTORY_BUSY_EXIT_CODE,
    }
    # A crash that repeats is bounded: a long RestartSec and a start limit.
    assert _seconds(_one(unit, "Service", "RestartSec")) >= 5 * 60
    assert _seconds(_one(unit, "Unit", "StartLimitIntervalSec")) >= 3600
    assert int(_one(unit, "Unit", "StartLimitBurst")) <= 5


def test_a_stop_lets_the_crawl_flush_its_member():
    """SIGTERM ends Python without its `finally`, cutting the gzip member in
    progress; SIGINT raises KeyboardInterrupt and the crawl's final flush runs.
    The stop timeout must outlast one in-flight request plus that flush."""
    unit = _parse_unit(UNIT.read_text())
    assert _one(unit, "Service", "KillSignal") == "SIGINT"
    assert _seconds(_one(unit, "Service", "TimeoutStopSec")) >= 60
    assert "KillMode" not in unit["Service"], "the default control-group signals the harvester too"


def test_the_unit_writes_only_under_the_crawler_root():
    """The checkout is read-only; everything the runner writes -- out-dirs, the
    latch, the lock, the queue log -- is under the crawler root, and the unit
    runs on the host's own boot target, not a user manager."""
    unit = _parse_unit(UNIT.read_text())
    assert _one(unit, "Service", "ProtectSystem") == "strict"
    assert unit["Service"]["ReadWritePaths"] == [HOST_ROOT]
    assert _one(unit, "Service", "NoNewPrivileges") == "true"
    assert _one(unit, "Service", "PrivateTmp") == "true"
    runner = RUNNER.read_text()
    for written in ("RESULTS=$ROOT/results", "LOG=$ROOT/queue-pass$PASS.log"):
        assert written in runner
    # The README's layout table agrees on the shared latch and lock.
    readme = README.read_text()
    assert f"`{HOST_ROOT}/results/{LATCH_FILENAME}`, `{LOCK_FILENAME}`" in readme


def test_the_readme_installs_the_unit_it_ships():
    readme = README.read_text()
    assert f"cp {HOST_REPO}/deploy/crawler/{UNIT.name} /etc/systemd/system/" in readme
    assert "systemctl enable --now gsv-history-queue@1" in readme
    assert "systemctl enable --now gsv-history-queue@2" in readme
    assert "systemctl disable --now gsv-history-queue@1" in readme


# ── The privacy rule ───────────────────────────────────────────────────────

# Anything that would identify the crawler host: a cloud provider, a region, an
# instance type or id, a security-group id, a literal address, the remote-shell
# service used to reach it. The pilot writeup carried several of these until the
# review of PR #446 scrubbed it; this keeps them out. IPv6 literals are not
# matched (too many false positives); none has ever been written here.
HOST_IDENTITY = re.compile(
    r"\b(aws|ec2|amazon|azure|gce|gcp|hetzner|linode|lightsail|droplet|ssm|elastic ip"
    # regions: eu-central-1, ap-southeast-2, us-central1, europe-west4
    r"|(us|eu|ap|sa|ca|me|af|il|europe|asia|australia|northamerica|southamerica)"
    r"-(east|west|central|north|south|southeast|northeast|southwest|northwest)-?\d"
    # instance types: m5.2xlarge, c6gn.large, e2-micro, n2-standard-4, Standard_D2s
    r"|[a-z]\d[a-z]*\.(nano|micro|small|medium|\d*x?large|metal)"
    r"|[a-z]\d[a-z]?-(micro|small|medium|standard|highmem|highcpu)(-\d+)?"
    r"|standard_[a-z]\d+[a-z]*"
    # ids and the hostnames a provider derives from an address
    r"|i-[0-9a-f]{8,}|sg-[0-9a-f]{8,}|ip-\d+-\d+-\d+-\d+)\b"
    # a literal IPv4 address
    r"|\b\d{1,3}(\.\d{1,3}){3}\b",
    re.IGNORECASE,
)


def test_nothing_in_the_crawler_deploy_or_the_harvesters_docs_names_the_host():
    files = [
        *sorted(CRAWLER_DIR.iterdir()),
        ROOT / "docs" / "capture-dates.md",
        *sorted((ROOT / "docs" / "experiments").glob("gsv-history-*.md")),
        ROOT / "scripts" / "harvest_gsv_history.py",
        ROOT / "streetscape_metadata_tracker" / "download_gsv_history.py",
    ]
    assert len(files) >= 7
    offenders = [
        f"{p.relative_to(ROOT)}: {m.group(0)!r}"
        for p in files
        for m in HOST_IDENTITY.finditer(p.read_text())
    ]
    assert offenders == []


def test_the_privacy_regex_catches_each_kind_of_host_identity():
    """Synthetic examples of each kind, never the host's own values: this file
    is public too, and the regex is the rule, not a list of secrets."""
    for leak in (
        "an EC2 instance",
        "on AWS",
        "a GCE VM",
        "via SSM",
        "eu-central-1",
        "ap-southeast-2",
        "us-central1",
        "europe-west4",
        "an m5.large",
        "a c6gn.2xlarge box",
        "an e2-micro",
        "n2-standard-4",
        "Standard_D2s",
        "i-0123456789abcdef0",
        "sg-0123456789abcdef0",
        "ip-10-0-0-1",
        "192.0.2.10",
    ):
        assert HOST_IDENTITY.search(leak), leak
    for fine in (
        "Google Street View",
        "pass 2 of 2",
        "v3/api/cities",
        "2026-10-26",
        "a small virtual machine",
        "the e2e suite",
        "a t-test",
        "schema v22",
        "Python 3.12",
        "at 60/min, 1,500,869 edges",
    ):
        assert not HOST_IDENTITY.search(fine), fine
