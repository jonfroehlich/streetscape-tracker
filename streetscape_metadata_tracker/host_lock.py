"""Cross-process mutual exclusion for third-party hosts that meter us by IP
(issue #208).

`AsyncRateLimiter` paces requests within ONE process. A per-IP limit is a
property of the whole machine, so two politely-paced processes present double
the configured rate to a host that cannot tell them apart. That is not
hypothetical: on 2026-08-14 a detached catch-up script running alongside the
nightly scheduler earned makelab2 per-IP bans from BOTH Mapillary's tile CDN
and overpass-api.de in one night, with 60/min tile pacing already deployed.

CLAUDE.md previously stated the mitigation as a rule for humans ("do not run
them in parallel"). The processes that most need to obey it — a detached
backfill loop launched two days ago, a manual CLI run, a second agent session —
are exactly the ones that cannot read it. Hence a lock.

Design notes, each of which is load-bearing:

* **The child holds the lock, never the scheduler parent.** `flock` is scoped
  to an open file description and is NOT inherited across ``subprocess.run``,
  so a parent holding the lock would make every child's ``timeout=0`` acquire
  fail deterministically. ``tests/test_host_lock.py`` pins this by asserting
  ``scheduler.py`` never imports this module.
* **``timeout=0``, fail fast.** Matching the run-level lock in
  ``download_gsv.py``. Queueing would either be too short to help or would let
  one manual run eat the nightly batch's 10 h deadline; failing fast plus the
  scheduler's breaker makes the collision *visible* instead.
* **A stale lock file cannot wedge a night.** The kernel releases ``flock``
  when the holding fd closes — including on SIGKILL and OOM — so a leftover
  ``.lock`` file on disk is not a held lock. Only the ``.owner`` sidecar can go
  stale, and the busy message says so.

Where the lock file lives is the subtle part; see :func:`lock_dir`.
"""

import contextlib
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

from filelock import FileLock, Timeout

from .download_common import HOST_LABELS, HostBusyError, redact_credentials
from .paths import get_project_root

# Operator/systemd override. Read at CALL time, not import time, so tests can
# point it at a per-test tmp dir and the systemd unit can set it explicitly.
LOCK_DIR_ENV = "STREETSCAPE_LOCK_DIR"


def lock_dir() -> str:
    """
    Directory holding the per-host lock files.

    Three places this must NOT be, each of which would make the lock a silent
    no-op or a hazard:

    1. **Not ``/tmp``.** ``deploy/systemd/streetscape-tracker.service`` sets
       ``PrivateTmp=true``, so the scheduler and its children get a private
       ``/tmp`` namespace while a detached operator shell sees the real one —
       invisible between exactly the two processes this lock exists to
       serialize.
    2. **Not the unresolved checkout path.** The systemd unit runs from
       ``%h/streetscape-tracker``, a symlink into ``/projects/makeabilitylab``,
       and ``get_project_root()`` uses ``os.path.abspath``, which does not
       resolve symlinks. Two processes reaching the same tree by different
       paths would take two different locks, so this resolves the realpath.
    3. **Not under ``data/``**, which ``sync_data_to_server.sh`` rsyncs to a
       public web server.

    Landing beside ``logs/``, ``backups/`` and ``archive/`` satisfies all three
    and keeps the lock on the host's local disk (``flock`` over NFS is not
    something to rely on).
    """
    override = os.environ.get(LOCK_DIR_ENV)
    if override:
        return override
    return os.path.join(os.path.realpath(get_project_root()), "locks")


def lock_path(host: str) -> str:
    """Path of the lock file for ``host`` (one of the ``HOST_*`` constants)."""
    return os.path.join(lock_dir(), f"{host}.lock")


def _owner_path(host: str) -> str:
    return f"{lock_path(host)}.owner"


def _write_owner(host: str) -> None:
    """
    Record who holds the lock, for the benefit of the *next* process's error
    message. Best effort in every sense: this is diagnostics, and must never be
    what fails a collection.
    """
    try:
        with open(_owner_path(host), "w", encoding="utf-8") as fh:
            fh.write(f"pid {os.getpid()}\n")
            fh.write(f"since {datetime.now(UTC).isoformat()}\n")
            fh.write(f"argv {redact_credentials(' '.join(sys.argv))}\n")
    except OSError:
        pass


def _read_owner(host: str) -> str:
    try:
        with open(_owner_path(host), encoding="utf-8") as fh:
            return " | ".join(line.strip() for line in fh if line.strip())
    except OSError:
        return "(owner unknown)"


def _clear_owner(host: str) -> None:
    try:
        os.remove(_owner_path(host))
    except OSError:
        pass


@contextlib.contextmanager
def host_lock(host: str) -> Iterator[None]:
    """
    Hold the machine-wide lock for ``host`` for the duration of the block.

    Args:
        host: one of the ``HOST_*`` constants in ``download_common``.

    Raises:
        HostBusyError: another process on this machine holds the lock. Raised
            immediately (``timeout=0``) rather than queueing, so the caller
            fails fast with a message naming the holder.
    """
    path = lock_path(host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    label = HOST_LABELS.get(host, host)

    lock = FileLock(path, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise HostBusyError(
            f"Another process on this machine is already talking to {label} "
            f"[{_read_owner(host)}]; refusing to run concurrently. Its rate limit "
            f"is per-IP, so two processes present double the paced rate to it and "
            f"risk a block on the whole host (issue #208). Wait for that process "
            f"to finish, or collect a provider that does not use this host. "
            f"(The lock is the flock on {path}, not the file's existence — if "
            f"that pid is gone the lock is already free.)",
            host=host,
        ) from None

    _write_owner(host)
    try:
        yield
    finally:
        _clear_owner(host)
        lock.release()
