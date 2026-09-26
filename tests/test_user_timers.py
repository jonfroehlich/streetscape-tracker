"""The user-timer watchdog (issue #369).

After the 2026-09-23 makelab2 reboot every user timer came back enabled but
INACTIVE and nothing alerted. `timer-status` is the check a user crontab runs to
find that state, re-arm it, and say so. Every test here drives the command
against `FakeSystemctl`, a pure-Python stand-in for `systemctl --user`, so the
exact call sequence — not just the verdict — is what is pinned.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subprocess  # noqa: E402

import pytest  # noqa: E402

from streetscape_metadata_tracker import scheduler as sched  # noqa: E402
from streetscape_metadata_tracker import user_timers as ut  # noqa: E402
from streetscape_metadata_tracker.scheduler import SchedulerConfig  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TIMERS = [
    "streetscape-backup-check.timer",
    "streetscape-prefreeze.timer",
    "streetscape-screen-provider.timer",
    "streetscape-tracker.timer",
]
PROD_HOST = "makelab2.cs.washington.edu"
SHOW_PROPS = [
    "-p",
    "LoadState",
    "-p",
    "ActiveState",
    "-p",
    "UnitFileState",
    "-p",
    "NextElapseUSecRealtime",
]


def _show(name):
    return ["show", name, *SHOW_PROPS]


class FakeSystemctl:
    """A `Runner`: records every call and answers from `state`."""

    def __init__(self, *, manager="running", start_flips=True, reload_installs=False):
        self.state = {
            n: {
                "LoadState": "loaded",
                "ActiveState": "active",
                "UnitFileState": "enabled",
                "NextElapseUSecRealtime": "Sat 2026-09-26 02:07:00 PDT",
            }
            for n in TIMERS
        }
        self.manager = manager
        self.start_flips = start_flips
        self.reload_installs = reload_installs
        self.calls: list[list[str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        out = ""
        verb = args[0]
        if verb == "is-system-running":
            out = self.manager() if callable(self.manager) else self.manager
        elif verb == "show":
            out = "\n".join(f"{k}={v}" for k, v in self.state[args[1]].items())
        elif verb == "daemon-reload":
            if self.reload_installs:
                for st in self.state.values():
                    if st["LoadState"] == "not-found":
                        st["LoadState"] = "loaded"
                        st["UnitFileState"] = "enabled"
        elif verb == "start":
            st = self.state[args[1]]
            if self.start_flips and st["LoadState"] == "loaded":
                st["ActiveState"] = "active"
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 0, out + "\n", "")

    def set(self, name, **kv):
        self.state[name].update(kv)

    def verbs(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def unit_files(tmp_path, monkeypatch):
    """The four installed timer files, where the fake home keeps them."""
    user = tmp_path / "user"
    user.mkdir()
    for n in TIMERS:
        (user / n).write_text("")
    monkeypatch.setattr(ut, "USER_UNIT_DIR", str(user))
    return user


@pytest.fixture
def cfg(tmp_path):
    return SchedulerConfig(log_dir=str(tmp_path / "logs"))


@pytest.fixture
def sent(monkeypatch):
    mails: list[tuple[str, str]] = []
    monkeypatch.setattr(sched, "send_alert", lambda a, s, b: mails.append((s, b)) or True)
    return mails


def _run(cfg, fake, **kw):
    kw.setdefault("hostname", PROD_HOST)
    return sched.cmd_timer_status(cfg, run=fake, **kw)


# --- the timer set and the host ------------------------------------------------


def test_shipped_timers_are_exactly_the_four_installed_units():
    # A fifth timer fails here, which is the prompt to confirm the watchdog
    # (and its heartbeat) should cover it.
    assert ut.shipped_timers() == TIMERS


def test_no_shipped_timers_is_an_error_not_an_empty_success(
    tmp_path, monkeypatch, cfg, unit_files, sent, capsys
):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "streetscape-tracker.service").write_text("[Unit]\nConditionHost=makelab2*\n")
    with pytest.raises(FileNotFoundError):
        ut.shipped_timers(empty)
    monkeypatch.setattr(ut, "DEFAULT_UNIT_DIR", empty)
    fake = FakeSystemctl()
    assert _run(cfg, fake) == 1
    assert str(empty) in capsys.readouterr().out
    assert fake.calls == []


def test_systemctl_env_fills_only_what_cron_lacks():
    env = ut.systemctl_env({}, 29497)
    assert env["XDG_RUNTIME_DIR"] == "/run/user/29497"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/29497/bus"
    preset = {"XDG_RUNTIME_DIR": "/x", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/y"}
    assert ut.systemctl_env(preset, 29497) == preset


def test_active_host_glob_is_read_from_the_shipped_service(tmp_path):
    assert ut.active_host_glob() == "makelab2*"
    (tmp_path / "streetscape-tracker.service").write_text(
        "[Unit]\n# ConditionHost=commented*\nConditionHost=elsewhere*\n"
    )
    assert ut.active_host_glob(tmp_path) == "elsewhere*"
    assert ut.is_active_host(PROD_HOST, "makelab2*")
    assert not ut.is_active_host("makelab1.cs.washington.edu", "makelab2*")


def test_a_host_that_is_not_the_active_one_does_nothing(cfg, unit_files, sent):
    """makelab1 shares the NFS home but has no linger: a crontab copied there
    would otherwise mail USER MANAGER UNREACHABLE every morning."""
    fake = FakeSystemctl()
    assert _run(cfg, fake, hostname="makelab1.cs.washington.edu", rearm=True, alert=True) == 0
    assert fake.calls == []
    assert sent == []
    assert not os.path.exists(ut.heartbeat_path(cfg.log_dir))


# --- classification and re-arm -------------------------------------------------


def test_enabled_but_inactive_timers_exit_nonzero_and_are_not_started_without_rearm(
    cfg, unit_files, sent, capsys
):
    fake = FakeSystemctl()
    for n in TIMERS:
        fake.set(n, ActiveState="inactive")
    assert _run(cfg, fake) == 1
    out = capsys.readouterr().out
    assert all(f"{n}" in out for n in TIMERS)
    assert "Verdict: INACTIVE: " + ", ".join(TIMERS) in out
    assert fake.calls == [["is-system-running"]] + [_show(n) for n in TIMERS]


def test_rearm_reloads_once_then_starts_only_the_inactive_timers(cfg, unit_files, sent):
    fake = FakeSystemctl()
    a, b = TIMERS[1], TIMERS[3]
    fake.set(a, ActiveState="inactive")
    fake.set(b, ActiveState="inactive")
    assert _run(cfg, fake, rearm=True) == 0
    assert fake.calls == [
        ["is-system-running"],
        *[_show(n) for n in TIMERS],
        ["daemon-reload"],
        ["start", a],
        ["start", b],
        _show(a),
        _show(b),
    ]
    beat = ut.read_heartbeat(cfg.log_dir)
    assert beat["rearmed"] == [a, b]
    assert beat["healthy"] is True


def test_a_timer_that_stays_inactive_after_start_is_a_failure(cfg, unit_files, sent):
    fake = FakeSystemctl(start_flips=False)
    fake.set(TIMERS[0], ActiveState="inactive")
    assert _run(cfg, fake, rearm=True, alert=True) == 1
    beat = ut.read_heartbeat(cfg.log_dir)
    assert beat["inactive"] == [TIMERS[0]]
    assert beat["rearmed"] == []
    assert len(sent) == 1 and "INACTIVE" in sent[0][0]


@pytest.mark.parametrize(
    "load,unit_file",
    [("loaded", "disabled"), ("masked", "masked")],
    ids=["disabled", "masked"],
)
def test_a_disabled_timer_is_a_pause_not_a_failure(cfg, unit_files, sent, capsys, load, unit_file):
    fake = FakeSystemctl()
    name = TIMERS[2]
    fake.set(name, LoadState=load, ActiveState="inactive", UnitFileState=unit_file)
    assert _run(cfg, fake, rearm=True, alert=True) == 0
    assert "start" not in fake.verbs() and "daemon-reload" not in fake.verbs()
    assert "paused" in capsys.readouterr().out
    assert ut.read_heartbeat(cfg.log_dir)["paused"] == [name]
    assert sent == []


def test_a_not_found_timer_is_unhealthy_and_a_reload_can_recover_it(cfg, unit_files, sent):
    """`not-found` is exactly the shape the NFS mount race leaves: the manager
    scanned an empty directory. daemon-reload re-scans; start then arms it."""
    name = TIMERS[3]

    def fake_for(reload_installs):
        f = FakeSystemctl(reload_installs=reload_installs)
        f.set(name, LoadState="not-found", ActiveState="inactive", UnitFileState="")
        return f

    fake = fake_for(True)
    assert _run(cfg, fake, alert=True) == 1
    assert "NOT INSTALLED" in sent[-1][0]
    assert "daemon-reload" not in fake.verbs()

    fake = fake_for(True)
    assert _run(cfg, fake, rearm=True, alert=True) == 0
    assert fake.calls[-3:] == [["daemon-reload"], ["start", name], _show(name)]
    assert f"REARMED: {name}" in sent[-1][0]

    fake = fake_for(False)
    assert _run(cfg, fake, rearm=True, alert=True) == 1
    assert "NOT INSTALLED" in sent[-1][0]


# --- waiting for the manager and the files ----------------------------------


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_wait_polls_until_the_manager_and_the_unit_files_are_both_visible(cfg, unit_files, sent):
    answers = iter(["offline", "offline"])
    fake = FakeSystemctl(manager=lambda: next(answers, "running"))
    files = sorted(unit_files.iterdir())
    for f in files:
        f.unlink()
    fc = FakeClock()

    def sleep(s):
        fc.sleep(s)
        if len(fc.sleeps) == 3:
            for f in files:
                f.write_text("")

    rc = _run(cfg, fake, wait_s=120, poll_s=10, sleep=sleep, clock=fc.clock)
    assert rc == 0
    assert fake.verbs().count("is-system-running") == 4
    assert fc.sleeps == [10, 10, 10]
    assert fake.verbs()[4:] == ["show"] * 4


def test_wait_gives_up_after_the_budget_and_still_writes_the_heartbeat(cfg, unit_files, sent):
    fake = FakeSystemctl(manager="offline")
    fc = FakeClock()
    rc = _run(
        cfg, fake, alert=True, rearm=True, wait_s=30, poll_s=10, sleep=fc.sleep, clock=fc.clock
    )
    assert rc == 1
    assert fc.t <= 30
    assert set(fake.verbs()) == {"is-system-running"}
    assert len(sent) == 1 and "USER MANAGER UNREACHABLE" in sent[0][0]
    beat = ut.read_heartbeat(cfg.log_dir)
    assert beat["healthy"] is False
    assert beat["verdict"].startswith("USER MANAGER UNREACHABLE")


def test_wait_gives_up_on_unit_files_that_never_appear(cfg, unit_files, sent):
    for f in unit_files.iterdir():
        f.unlink()
    fake = FakeSystemctl()
    fc = FakeClock()
    assert _run(cfg, fake, alert=True, wait_s=20, poll_s=10, sleep=fc.sleep, clock=fc.clock) == 1
    assert "UNIT FILES UNREACHABLE" in sent[0][0]
    assert "show" not in fake.verbs()


def test_wait_zero_is_a_single_check(cfg, unit_files, sent):
    fake = FakeSystemctl(manager="offline")
    fc = FakeClock()
    assert _run(cfg, fake, sleep=fc.sleep, clock=fc.clock) == 1
    assert fake.calls == [["is-system-running"]]
    assert fc.sleeps == []


def test_bad_wait_or_poll_values_exit_usage(cfg, unit_files, sent):
    for kw in ({"wait_s": -1}, {"poll_s": 0}, {"poll_s": -5}):
        fake = FakeSystemctl()
        assert _run(cfg, fake, **kw) == sched.USAGE_EXIT_CODE
        assert fake.calls == []


# --- alerting and the heartbeat --------------------------------------------


def test_alert_fires_on_rearm_and_on_failure_and_never_on_a_quiet_day(cfg, unit_files, sent):
    assert _run(cfg, FakeSystemctl(), rearm=True, alert=True) == 0
    assert sent == []

    fake = FakeSystemctl()
    fake.set(TIMERS[0], ActiveState="inactive")
    assert _run(cfg, fake, rearm=True, alert=True) == 0
    assert len(sent) == 1 and f"REARMED: {TIMERS[0]}" in sent[0][0]
    # The body says what to check next: the night before the reboot may be unpublished.
    assert "regenerate-aggregate --publish" in sent[0][1]

    fake = FakeSystemctl(start_flips=False)
    fake.set(TIMERS[0], ActiveState="inactive")
    assert _run(cfg, fake, rearm=True, alert=True) == 1
    assert len(sent) == 2 and "INACTIVE" in sent[1][0]


def test_alert_does_not_change_the_exit_status(cfg, unit_files, sent):
    for alert in (False, True):
        fake = FakeSystemctl()
        fake.set(TIMERS[0], ActiveState="inactive")
        assert _run(cfg, fake, alert=alert) == 1
    fake = FakeSystemctl()
    fake.set(TIMERS[0], ActiveState="inactive")
    assert _run(cfg, fake, rearm=True, alert=True) == 0


def test_the_heartbeat_records_the_verdict_atomically(cfg, unit_files, sent):
    fake = FakeSystemctl()
    fake.set(TIMERS[1], ActiveState="inactive")
    fake.set(TIMERS[2], UnitFileState="disabled", ActiveState="inactive")
    now = datetime(2026, 9, 26, 15, 30, tzinfo=UTC)
    assert _run(cfg, fake, rearm=True, now=now) == 0
    beat = ut.read_heartbeat(cfg.log_dir)
    assert datetime.fromisoformat(beat["checked_at"]) == now
    assert beat["host"] == PROD_HOST
    assert beat["healthy"] is True
    assert beat["verdict"] == f"REARMED: {TIMERS[1]}"
    assert beat["rearmed"] == [TIMERS[1]]
    assert beat["paused"] == [TIMERS[2]]
    assert beat["inactive"] == [] and beat["not_installed"] == []
    assert os.listdir(cfg.log_dir) == [ut.HEARTBEAT_FILENAME]
    assert ut.heartbeat_age_hours(cfg.log_dir, now + timedelta(hours=3)) == pytest.approx(3.0)


def test_read_heartbeat_tolerates_a_truncated_file(tmp_path):
    (tmp_path / ut.HEARTBEAT_FILENAME).write_text('{"checked_at": "2026-09-26T')
    assert ut.read_heartbeat(str(tmp_path)) is None
    assert ut.heartbeat_age_hours(str(tmp_path), datetime.now(UTC)) is None
    (tmp_path / ut.HEARTBEAT_FILENAME).write_text(json.dumps(["not", "an", "object"]))
    assert ut.read_heartbeat(str(tmp_path)) is None
    assert ut.heartbeat_age_hours(str(tmp_path / "missing"), datetime.now(UTC)) is None


def test_the_subcommand_is_wired():
    args = sched.build_parser().parse_args(
        ["timer-status", "--rearm", "--alert", "--wait-s", "1800", "--poll-s", "5"]
    )
    assert (args.command, args.rearm, args.alert, args.wait_s, args.poll_s) == (
        "timer-status",
        True,
        True,
        1800.0,
        5.0,
    )
    args = sched.build_parser().parse_args(["timer-status"])
    assert (args.rearm, args.alert, args.wait_s) == (False, False, 0.0)
