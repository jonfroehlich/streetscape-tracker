"""
Check, and re-arm, the scheduler's systemd USER timers (issue #369).

Why this exists: after the 2026-09-23 makelab2 hang and reboot, all four user
timers came back ``enabled`` but ``inactive``. Nothing collected, nothing
published, and nothing alerted — the only watchdog, ``backup-status --alert``
(issue #193), is itself run by one of those timers, so it went down with them.
The leading (unproven) cause is a boot-time race: the user manager
(``user@<uid>.service``, started by linger) scanned ``~/.config/systemd/user/``
five seconds before ``autofs`` mounted the NFS home, found no unit files, and
never looked again. The ordering is root's to fix, not ours.

So the check runs from somewhere that survives that race — a user crontab,
which lives in ``/var/spool/cron`` on local disk (``deploy/cron/``) — and it is
deliberately cause-agnostic: whatever left a timer enabled-but-inactive, the
repair is the one the operator did by hand on 2026-09-23 (``daemon-reload``,
then ``start`` each timer), and the alert fires either way.

Everything that talks to ``systemctl`` goes through a ``Runner`` — a callable
taking the arguments AFTER ``systemctl --user`` — so the tests drive the whole
classification and re-arm sequence against a pure-Python fake without a
systemd anywhere. ``run_systemctl`` is the real one.

Every run that reaches the check writes a heartbeat file
(``HEARTBEAT_FILENAME`` under the log directory), which ``backup-status`` gates
on (``[schedule].timer_watchdog_max_age_h``). The two watchdogs cover each
other: cron catches dead timers, the backup-check timer catches a dead cron.
Both dead at once is silence — the honest gap, documented in
``docs/scheduler.md``.

Usage (what the crontab runs)::

    python -m streetscape_metadata_tracker.scheduler \\
        --config config/scheduler.makelab1.toml timer-status --rearm --alert
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HEARTBEAT_FILENAME = "timer_watchdog_status.json"
# The shipped units are the definition of "our timers": a fifth timer added to
# deploy/systemd/ is covered the moment it ships, never via a second list.
DEFAULT_UNIT_DIR = Path(__file__).resolve().parent.parent / "deploy" / "systemd"
# Where the installed copies live. Expanded at call time: under cron, HOME is
# the passwd home, which is the NFS home the race is about.
USER_UNIT_DIR = "~/.config/systemd/user"
# The unit whose ConditionHost= names the active host. Read, never restated.
HOST_UNIT = "streetscape-tracker.service"
# A DISABLED or MASKED timer is an operator's deliberate pause and is left alone.
# This is why the documented pause is `disable --now`, not `stop`: a stopped but
# still-enabled timer is indistinguishable from the #369 reboot shape.
PAUSED_STATES = frozenset({"disabled", "masked", "masked-runtime"})
# What `systemctl --user is-system-running` prints for a manager that exists and
# answers. `offline`/`unknown` (or nothing, when the bus is unreachable) are not.
LIVE_MANAGER_STATES = frozenset({"initializing", "starting", "running", "degraded", "maintenance"})
DEFAULT_POLL_S = 15.0
SYSTEMCTL_TIMEOUT_S = 60.0

# Verdict words, in the precedence the alert subject uses.
VERDICT_MANAGER_UNREACHABLE = "USER MANAGER UNREACHABLE"
VERDICT_FILES_UNREACHABLE = "UNIT FILES UNREACHABLE"
VERDICT_NOT_INSTALLED = "NOT INSTALLED"
VERDICT_INACTIVE = "INACTIVE"
VERDICT_REARMED = "REARMED"
VERDICT_OK = "ok"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


@dataclass(frozen=True)
class TimerState:
    """One timer as ``systemctl --user show`` reported it."""

    name: str
    load_state: str
    active_state: str
    unit_file_state: str
    next_elapse: str = ""

    @property
    def paused(self) -> bool:
        return self.unit_file_state in PAUSED_STATES or self.load_state == "masked"

    @property
    def loaded(self) -> bool:
        return self.load_state == "loaded"

    @property
    def active(self) -> bool:
        return self.active_state == "active"

    @property
    def healthy(self) -> bool:
        return self.paused or (self.loaded and self.active)

    @property
    def word(self) -> str:
        """The one-word state the report prints."""
        if self.paused:
            return "paused"
        if not self.loaded:
            return "NOT INSTALLED"
        return "active" if self.active else "INACTIVE"


@dataclass
class WatchdogResult:
    """What one check found, and what it did about it."""

    reachable: bool = True
    files_visible: bool = True
    waited_s: float = 0.0
    wait_budget_s: float = 0.0
    states: list[TimerState] = field(default_factory=list)
    rearmed: list[str] = field(default_factory=list)
    inactive: list[str] = field(default_factory=list)
    not_installed: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return (
            self.reachable and self.files_visible and not self.inactive and not self.not_installed
        )

    @property
    def verdict(self) -> str:
        """The alert subject's verdict fragment, in precedence order."""
        if not self.reachable:
            return f"{VERDICT_MANAGER_UNREACHABLE} after {self.wait_budget_s:g} s"
        if not self.files_visible:
            return f"{VERDICT_FILES_UNREACHABLE} after {self.wait_budget_s:g} s"
        if self.not_installed:
            return f"{VERDICT_NOT_INSTALLED}: {', '.join(self.not_installed)}"
        if self.inactive:
            return f"{VERDICT_INACTIVE}: {', '.join(self.inactive)}"
        if self.rearmed:
            return f"{VERDICT_REARMED}: {', '.join(self.rearmed)}"
        return VERDICT_OK

    def to_json(self) -> dict:
        return {
            "healthy": self.healthy,
            "verdict": self.verdict,
            "rearmed": list(self.rearmed),
            "inactive": list(self.inactive),
            "paused": list(self.paused),
            "not_installed": list(self.not_installed),
            "waited_s": round(self.waited_s, 1),
        }


def systemctl_env(environ: Mapping[str, str], uid: int) -> dict[str, str]:
    """
    The environment ``systemctl --user`` needs, filling only what cron lacks.

    Cron's environment has neither variable, and without them every call fails
    ``Failed to connect to bus``. An interactive shell has both, and a value
    already set is never overridden.
    """
    env = dict(environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    return env


def run_systemctl(
    args: Sequence[str], *, timeout_s: float = SYSTEMCTL_TIMEOUT_S
) -> subprocess.CompletedProcess:
    """The real ``Runner``: ``systemctl --user *args``, never raising.

    A hung call comes back as returncode 124 and a missing binary as 127, so
    the check reports "unreachable" rather than dying with a traceback that
    lands only in a log nobody reads.
    """
    argv = ["systemctl", "--user", *args]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=systemctl_env(os.environ, os.getuid()),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout_s:g} s")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def shipped_timers(unit_dir: Path | None = None) -> list[str]:
    """Sorted basenames of every ``*.timer`` shipped in ``deploy/systemd``.

    Raises ``FileNotFoundError`` when there are none: an empty list would
    otherwise read as "every timer is healthy", which is the silence this
    module exists to end.
    """
    unit_dir = Path(unit_dir if unit_dir is not None else DEFAULT_UNIT_DIR)
    names = sorted(p.name for p in unit_dir.glob("*.timer"))
    if not names:
        raise FileNotFoundError(f"no shipped timers found under {unit_dir}")
    return names


def active_host_glob(unit_dir: Path | None = None) -> str:
    """The ``ConditionHost=`` glob of the shipped collection unit."""
    unit_dir = Path(unit_dir if unit_dir is not None else DEFAULT_UNIT_DIR)
    for raw in (unit_dir / HOST_UNIT).read_text().splitlines():
        line = raw.strip()
        if line.startswith("ConditionHost="):
            return line.partition("=")[2].strip()
    raise ValueError(f"{unit_dir / HOST_UNIT} has no ConditionHost=")


def is_active_host(hostname: str, glob: str) -> bool:
    """Whether ``hostname`` matches the ``ConditionHost=`` glob, as systemd does."""
    return fnmatch.fnmatchcase(hostname, glob)


def manager_reachable(run: Runner) -> bool:
    """Whether the user manager exists and answers on its bus."""
    return run(["is-system-running"]).stdout.strip() in LIVE_MANAGER_STATES


def unit_files_visible(names: Sequence[str], user_unit_dir: str | None = None) -> bool:
    """Whether every installed timer file is visible.

    The ``isfile`` is also what triggers the autofs mount of the NFS home.
    """
    base = os.path.expanduser(user_unit_dir if user_unit_dir is not None else USER_UNIT_DIR)
    return all(os.path.isfile(os.path.join(base, n)) for n in names)


def timer_state(run: Runner, name: str) -> TimerState:
    """Parse one ``show`` call; a missing key reads as the empty string."""
    out = run(
        [
            "show",
            name,
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "UnitFileState",
            "-p",
            "NextElapseUSecRealtime",
        ]
    ).stdout
    props: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return TimerState(
        name=name,
        load_state=props.get("LoadState", ""),
        active_state=props.get("ActiveState", ""),
        unit_file_state=props.get("UnitFileState", ""),
        next_elapse=props.get("NextElapseUSecRealtime", ""),
    )


def wait_for_manager(
    run: Runner,
    names: Sequence[str],
    *,
    wait_s: float,
    poll_s: float = DEFAULT_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    user_unit_dir: str | None = None,
) -> tuple[bool, bool, float]:
    """Poll until the manager answers AND the unit files are visible.

    Returns ``(reachable, files_visible, waited_s)`` from the last poll.
    ``wait_s = 0`` is exactly one check; otherwise the loop ends once both hold
    or once another poll would pass the budget — never unbounded.
    """
    start = clock()
    while True:
        reachable = manager_reachable(run)
        visible = unit_files_visible(names, user_unit_dir)
        waited = clock() - start
        if reachable and visible:
            return reachable, visible, waited
        if waited + poll_s > wait_s:
            return reachable, visible, waited
        sleep(poll_s)


def check_and_rearm(run: Runner, names: Sequence[str], *, rearm: bool) -> WatchdogResult:
    """Classify every timer and, with ``rearm``, restart the dead ones.

    The call sequence is pinned by the tests: ``show`` for every timer; then,
    only when some timer is INACTIVE or NOT INSTALLED and ``rearm`` is set, ONE
    ``daemon-reload`` (the manager may hold the empty unit map of a scan that
    raced the mount); ``start`` for each such timer in order (``daemon-reload``
    starts nothing, and ``timers.target`` is already active so its wants are not
    re-pulled); and a re-``show`` of each of those only. A ``start`` that
    returned 0 is not trusted — only the re-read state counts.
    """
    result = WatchdogResult()
    states = {n: timer_state(run, n) for n in names}
    broken = [n for n in names if not states[n].healthy]

    if rearm and broken:
        run(["daemon-reload"])
        for n in broken:
            run(["start", n])
        for n in broken:
            after = timer_state(run, n)
            if after.healthy and not after.paused:
                result.rearmed.append(n)
            states[n] = after

    result.states = [states[n] for n in names]
    for s in result.states:
        if s.paused:
            result.paused.append(s.name)
        elif not s.loaded:
            result.not_installed.append(s.name)
        elif not s.active:
            result.inactive.append(s.name)
    return result


def heartbeat_path(log_dir: str) -> str:
    return os.path.join(log_dir, HEARTBEAT_FILENAME)


def write_heartbeat(log_dir: str, result: WatchdogResult, *, host: str, now: datetime) -> str:
    """Write the heartbeat atomically (``.tmp`` then ``os.replace``)."""
    os.makedirs(log_dir, exist_ok=True)
    path = heartbeat_path(log_dir)
    payload = {"checked_at": now.astimezone(UTC).isoformat(), "host": host, **result.to_json()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return path


def read_heartbeat(log_dir: str) -> dict | None:
    """The last heartbeat, or None when missing, truncated or not an object.

    Tolerant on purpose: ``backup-status`` reads this, and a half-written file
    must make it report "never ran", not take the backup check down with it.
    """
    try:
        with open(heartbeat_path(log_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_age_hours(log_dir: str, now: datetime) -> float | None:
    """Hours since the last heartbeat's ``checked_at``, or None if unreadable."""
    data = read_heartbeat(log_dir)
    if data is None:
        return None
    try:
        checked = datetime.fromisoformat(str(data["checked_at"]))
    except (KeyError, ValueError):
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return (now - checked).total_seconds() / 3600.0
