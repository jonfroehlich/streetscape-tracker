"""The crontab that runs the user-timer watchdog (issue #369).

The watchdog exists because the user timers cannot watch themselves: after the
2026-09-23 reboot they came back enabled but inactive, the #193 backup check
among them. What runs it is a user crontab (`deploy/cron/`), and these tests pin
what makes that file do its job rather than merely exist: both lines parse under
the scheduler's own parser with the prod config, the checkout and interpreter are
the ones the units use, the @reboot line waits a bounded time, the daily slot
precedes the backup-check timer and leaves room for a catch-up night, cron's own
mail is off, and the deploy README installs it idempotently.
"""

import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from streetscape_metadata_tracker import scheduler  # noqa: E402
from streetscape_metadata_tracker.scheduler import load_scheduler_config  # noqa: E402
from tests.test_prefreeze_unit import _daily_pacific, _one, _parse_unit, _span_minutes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CRONTAB = ROOT / "deploy" / "cron" / "streetscape-tracker.crontab"
MARKER = "streetscape-tracker timer watchdog"
LOG_REDIRECT = ">> logs/timer_watchdog.log 2>&1"


def _lines() -> list[str]:
    return [
        ln.strip()
        for ln in CRONTAB.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _env_lines() -> dict[str, str]:
    return dict(
        ln.split("=", 1)
        for ln in _lines()
        if re.fullmatch(r"[A-Z_]+=.*", ln) and not ln.startswith(("@", "*"))
    )


def _jobs() -> dict[str, str]:
    """{'@reboot' | 'daily': command}."""
    jobs: dict[str, str] = {}
    for ln in _lines():
        if ln.startswith("@reboot "):
            jobs["@reboot"] = ln[len("@reboot ") :]
        elif re.match(r"^[\d*]", ln):
            fields = ln.split(None, 5)
            jobs["daily"] = fields[5]
            jobs["daily_fields"] = " ".join(fields[:5])
    return jobs


def _parse(command: str):
    """(cd dir, argv after the interpreter parsed by build_parser, redirect)."""
    assert command.endswith(LOG_REDIRECT), command
    body = command[: -len(LOG_REDIRECT)].strip()
    cd_part, _, run_part = body.partition(" && ")
    cd_argv = shlex.split(cd_part)
    assert cd_argv[0] == "cd" and len(cd_argv) == 2
    argv = shlex.split(run_part)
    return cd_argv[1], argv, LOG_REDIRECT


@pytest.fixture(scope="module")
def units():
    return {
        "collect": _parse_unit("streetscape-tracker.service"),
        "nightly": _parse_unit("streetscape-tracker.timer"),
        "backup": _parse_unit("streetscape-backup-check.timer"),
    }


def test_the_crontab_has_exactly_a_reboot_line_and_a_daily_line():
    lines = _lines()
    assert len(lines) == 4, lines
    assert set(_env_lines()) == {"MAILTO", "CRON_TZ"}
    assert sum(ln.startswith("@reboot ") for ln in lines) == 1
    jobs = _jobs()
    assert set(jobs) == {"@reboot", "daily", "daily_fields"}
    assert re.fullmatch(r"\d+ \d+ \* \* \*", jobs["daily_fields"]), jobs["daily_fields"]


@pytest.mark.parametrize("job", ["@reboot", "daily"])
def test_both_commands_run_timer_status_with_rearm_and_alert_under_the_prod_config(units, job):
    cd_dir, argv, _ = _parse(_jobs()[job])
    # The REAL checkout (the units' sandbox root), never the %h symlink on NFS.
    assert cd_dir == units["collect"]["Service"]["ReadWritePaths"][0]
    assert argv[0] == ".venv-makelab2/bin/python"
    assert argv[1:3] == ["-m", "streetscape_metadata_tracker.scheduler"]
    args = scheduler.build_parser().parse_args(argv[3:])
    assert args.command == "timer-status"
    assert args.rearm and args.alert
    assert args.config == "config/scheduler.makelab1.toml"
    assert (ROOT / args.config).is_file()
    # The same interpreter the units run.
    exec_start = shlex.split(_one(units["collect"], "Service", "ExecStart"))
    assert exec_start[0] == "%h/streetscape-tracker/" + argv[0]


def test_the_reboot_line_waits_and_the_daily_line_does_not():
    reboot = scheduler.build_parser().parse_args(_parse(_jobs()["@reboot"])[1][3:])
    daily = scheduler.build_parser().parse_args(_parse(_jobs()["daily"])[1][3:])
    assert 600 <= reboot.wait_s <= 3600
    assert daily.wait_s == 0


def test_the_daily_slot_precedes_the_backup_check_and_lets_a_catch_up_night_finish(units):
    """08:30 must (1) precede the noon backup-check timer, so a re-armed check
    still fires that day, and (2) leave a Persistent catch-up night the re-arm
    starts time to end before the next 02:00 — bounded as in the prefreeze
    tests, by delay + max_batch_hours + the collection unit's TimeoutStopSec."""
    minute, hour = (int(x) for x in _jobs()["daily_fields"].split()[:2])
    ours = hour * 60 + minute
    night_start, tz = _daily_pacific(units["nightly"])
    backup_start, backup_tz = _daily_pacific(units["backup"])
    assert _env_lines()["CRON_TZ"] == tz == backup_tz == "America/Los_Angeles"
    assert ours < backup_start
    prod = load_scheduler_config(str(ROOT / "config" / "scheduler.makelab1.toml"))
    catch_up_end = (
        ours
        + _span_minutes(_one(units["nightly"], "Timer", "RandomizedDelaySec"))
        + prod.max_batch_hours * 60
        + _span_minutes(_one(units["collect"], "Service", "TimeoutStopSec"))
    )
    assert catch_up_end <= 24 * 60 + night_start


def test_cron_mail_is_off_and_the_log_is_the_trail():
    assert _env_lines()["MAILTO"] == '""'
    for job in ("@reboot", "daily"):
        assert _jobs()[job].endswith(LOG_REDIRECT)


def test_the_deploy_readme_installs_the_crontab_idempotently():
    readme = (ROOT / "deploy" / "README.md").read_text()
    assert MARKER in CRONTAB.read_text().splitlines()[0]
    install = re.search(
        rf"grep -q '{re.escape(MARKER)}'.*?\n?.*?cat deploy/cron/streetscape-tracker\.crontab\) \| crontab -",
        readme,
    )
    assert install, "deploy/README.md must append the crontab guarded by its marker line"


def test_no_doc_pauses_a_timer_with_stop():
    """The daily re-arm undoes `stop` within a day; `disable --now` is the pause."""
    pattern = re.compile(r"systemctl --user stop streetscape-[a-z-]+\.timer")
    docs = [
        ROOT / "CLAUDE.md",
        ROOT / "deploy" / "README.md",
        *sorted((ROOT / "docs").glob("*.md")),
    ]
    offenders = [str(p.relative_to(ROOT)) for p in docs if pattern.search(p.read_text())]
    assert offenders == []
