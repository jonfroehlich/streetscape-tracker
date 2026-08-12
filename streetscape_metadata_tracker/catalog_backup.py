"""
Dated, integrity-verified backups of the SQLite catalog (issue #145).

Why this exists as its own mechanism rather than trusting the filesystem:
UW CSE IT confirmed on 2026-08-05 that ``/projects/makeabilitylab`` on makelab2
was **not being snapshotted or backed up at all** — the second such
misconfiguration found in two months. That is now fixed (ZFS snapshots, nightly
sync to the CSE backup servers, off-site sync to UW's lolo), but two facts
survive the fix:

1. A ZFS snapshot of a **live** WAL-mode SQLite file may be torn. CSE IT would
   not vouch for the in-flight-write behaviour, and their recommendation was the
   standard one: have the database write its own consistent copy on a schedule,
   into the same directory they snapshot, so every snapshot automatically
   carries a known-good restore point. ``sqlite3.Connection.backup()`` is
   exactly that — the online backup API takes a transactionally consistent
   snapshot of a live database, so no ``VACUUM INTO`` is needed.
2. A backup nobody can observe is indistinguishable from no backup — which is
   precisely how #145 happened. So every attempt records its outcome to
   ``backup_status.json``, **including failures**, where an operator (or
   ``scheduler backup-status``) can see it.

Design notes worth keeping:

- **Dated files, not one rolling copy.** A single in-place copy means a night
  that backs up a corrupted catalog destroys the last good one, and it leaves
  local point-in-time recovery entirely dependent on ZFS retention that CSE IT
  never stated a number for.
- **Atomic promotion.** The copy is written to a ``.tmp`` sibling, verified with
  ``PRAGMA integrity_check``, and only then ``os.replace()``'d into place. A
  filesystem snapshot therefore can never catch a half-written backup, and a
  failed verification leaves the previous good backup untouched.
- **Never prune the newest file**, whatever its age. Retention that ignores this
  turns a long-running backup failure into data loss: the window slides past the
  last good copy and prunes it. (Same guard as the Makeability Lab website's
  ``pg_dump`` retention, makeabilitylabwebsite#1444.)

Stdlib only, deliberately: this runs in the nightly scheduler's critical path
and must not drag in pandas or the geo stack.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

logger = logging.getLogger(__name__)

# Retention for dated catalog copies. Matches the Makeability Lab website's
# pg_dump retention (makeabilitylabwebsite#1444) so one number covers both
# repos. The catalog is small (single-digit MB locally), so this is cheap.
KEEP_DAYS = 14

_BASENAME = "streetscape_tracker.db"
_SUFFIX = ".backup"
STATUS_FILENAME = "backup_status.json"

# streetscape_tracker.db.2026-08-07.backup
_DATED_RE = re.compile(rf"^{re.escape(_BASENAME)}\.(\d{{4}}-\d{{2}}-\d{{2}}){re.escape(_SUFFIX)}$")

# Tables whose row counts go into the status file. Provenance, not validation:
# it is what tells an operator holding a backup file which catalog it came from
# and roughly how complete it is. A backup of a *test fixture* catalog has been
# mistaken for the real one before, which is what motivated recording this.
_COUNTED_TABLES = (
    "cities",
    "runs",
    "run_diffs",
    "street_networks",
    "street_walks",
    "street_walk_diffs",
    "history_harvests",
    "driving_plan_snapshots",
)


@dataclass
class BackupResult:
    """Outcome of one backup attempt. ``ok`` is the only thing callers must check."""

    ok: bool
    path: str | None = None
    bytes_written: int = 0
    integrity: str = ""
    row_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    pruned: list[str] = field(default_factory=list)


@dataclass
class BackupStatus:
    """What ``backup-status`` reports about the dated-copy directory."""

    backup_dir: str
    exists: bool
    newest_path: str | None = None
    newest_date: str | None = None
    age_hours: float | None = None
    file_count: int = 0
    total_bytes: int = 0
    last_attempt: dict | None = None


@dataclass
class AssetInventory:
    """Counts for a directory whose contents exist nowhere else."""

    label: str
    path: str
    exists: bool
    file_count: int = 0
    total_bytes: int = 0
    newest_mtime: str | None = None


def backup_filename(when: date) -> str:
    """Dated backup basename for ``when``."""
    return f"{_BASENAME}.{when.isoformat()}{_SUFFIX}"


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for the provenance record; missing tables are skipped."""
    counts = {}
    for table in _COUNTED_TABLES:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            # An older catalog legitimately lacks newer tables.
            continue
    return counts


def _verify(dest: sqlite3.Connection) -> str:
    """``PRAGMA integrity_check`` on a backup copy; "ok" when sound.

    A named function rather than an inline call so tests can inject damage —
    the promote-only-if-verified branch is the thing that protects the previous
    good backup, and it has to be exercised without corrupting a real file.
    """
    return dest.execute("PRAGMA integrity_check").fetchone()[0]


def write_backup(
    conn: sqlite3.Connection,
    backup_dir: str,
    when: date,
    *,
    source_db: str | None = None,
) -> BackupResult:
    """
    Write a verified, dated backup of ``conn`` into ``backup_dir``.

    Uses SQLite's online backup API, so the source may be live and in WAL mode.
    The copy is verified with ``PRAGMA integrity_check`` **before** it replaces
    any existing file for the same date, so a corrupt or truncated copy can
    never overwrite a good one.

    Re-running for the same date is intentional and safe: the scheduler backs up
    once before the city loop (so the night's copy survives a mid-loop SIGKILL)
    and again in the tail (so the retained copy reflects the runs the night
    actually registered). The second write atomically replaces the first.

    Never raises — returns ``ok=False`` with ``error`` set, because a backup
    problem must be *reported*, not allowed to abort a collection night.
    """
    final_path = os.path.join(backup_dir, backup_filename(when))
    tmp_path = final_path + ".tmp"

    try:
        # Inside the try: makedirs can fail (a stale file in the directory's
        # place, a read-only mount, a full disk), and this function's contract is
        # that it reports failure rather than raising into a collection night.
        os.makedirs(backup_dir, exist_ok=True)

        # A leftover .tmp from a previous crash would otherwise be opened as an
        # existing database and backed up *into*, which is harmless but muddles
        # the failure mode; start clean.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

        dest = sqlite3.connect(tmp_path, timeout=10)
        try:
            conn.backup(dest)
            integrity = _verify(dest)
        finally:
            dest.close()

        if integrity != "ok":
            # Leave any previous good backup for this date in place.
            os.unlink(tmp_path)
            result = BackupResult(
                ok=False,
                integrity=integrity,
                error=f"integrity_check failed: {integrity}",
            )
            _record(backup_dir, result, source_db, when)
            logger.error(f"Catalog backup FAILED integrity check: {integrity}")
            return result

        os.replace(tmp_path, final_path)
        result = BackupResult(
            ok=True,
            path=final_path,
            bytes_written=os.path.getsize(final_path),
            integrity=integrity,
            row_counts=_row_counts(conn),
        )
    except Exception as e:  # noqa: BLE001 — a backup failure must never abort a night
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        result = BackupResult(ok=False, error=str(e))
        _record(backup_dir, result, source_db, when)
        logger.error(f"Catalog backup failed: {e}")
        return result

    result.pruned = prune_backups(backup_dir, when)
    _record(backup_dir, result, source_db, when)
    logger.info(
        f"Catalog backed up to {final_path} "
        f"({result.bytes_written:,} bytes, integrity {integrity}"
        + (f", pruned {len(result.pruned)} old" if result.pruned else "")
        + ")"
    )
    return result


def list_backups(backup_dir: str) -> list[tuple[date, str]]:
    """Dated backups present, oldest first. Parsed from filenames, not mtimes."""
    found = []
    for path in glob.glob(os.path.join(backup_dir, f"{_BASENAME}.*{_SUFFIX}")):
        m = _DATED_RE.match(os.path.basename(path))
        if not m:
            continue
        try:
            found.append((date.fromisoformat(m.group(1)), path))
        except ValueError:
            continue
    return sorted(found)


def prune_backups(backup_dir: str, today: date, keep_days: int = KEEP_DAYS) -> list[str]:
    """
    Delete dated backups older than ``keep_days``, **never the newest one**.

    The exception is load-bearing rather than tidy: if backups have been failing
    for longer than the retention window, a naive prune would slide past the
    last good copy and delete it — turning a reporting problem into data loss.
    """
    backups = list_backups(backup_dir)
    if len(backups) <= 1:
        return []

    cutoff = today - timedelta(days=keep_days)
    newest_path = backups[-1][1]
    pruned = []
    for when, path in backups:
        if path == newest_path or when >= cutoff:
            continue
        try:
            os.unlink(path)
            pruned.append(path)
        except OSError as e:
            logger.warning(f"Could not prune old backup {path}: {e}")
    return pruned


def _record(
    backup_dir: str,
    result: BackupResult,
    source_db: str | None,
    when: date,
) -> None:
    """
    Write ``backup_status.json``, on failure as well as success.

    A failed backup that wrote no file is otherwise indistinguishable from one
    that never ran — the exact ambiguity that let #145 go unnoticed for months.
    """
    backups = list_backups(backup_dir)
    status = {
        "ok": result.ok,
        "last_attempt_at": datetime.now(UTC).isoformat(),
        "attempt_date": when.isoformat(),
        "source_db": source_db,
        "source_host": socket.gethostname(),
        "path": result.path,
        "bytes": result.bytes_written,
        "integrity": result.integrity,
        "row_counts": result.row_counts,
        "error": result.error,
        "pruned": result.pruned,
        "retention_days": KEEP_DAYS,
        "backups_present": [os.path.basename(p) for _, p in backups],
    }
    try:
        os.makedirs(backup_dir, exist_ok=True)
        status_path = os.path.join(backup_dir, STATUS_FILENAME)
        tmp = status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, sort_keys=True)
        os.replace(tmp, status_path)
    except OSError as e:
        # Reporting is best-effort; never let it turn into the failure it reports.
        logger.warning(f"Could not write backup status: {e}")


def read_status(backup_dir: str) -> dict | None:
    """Last recorded attempt, or None when absent/unreadable."""
    path = os.path.join(backup_dir, STATUS_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def backup_status(backup_dir: str, now: datetime | None = None) -> BackupStatus:
    """Health of the dated-copy directory, for the operator-facing command."""
    if not os.path.isdir(backup_dir):
        return BackupStatus(
            backup_dir=backup_dir, exists=False, last_attempt=read_status(backup_dir)
        )

    backups = list_backups(backup_dir)
    st = BackupStatus(
        backup_dir=backup_dir,
        exists=True,
        file_count=len(backups),
        total_bytes=sum(os.path.getsize(p) for _, p in backups if os.path.exists(p)),
        last_attempt=read_status(backup_dir),
    )
    if backups:
        when, path = backups[-1]
        st.newest_path = path
        st.newest_date = when.isoformat()
        ref = now or datetime.now(UTC)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
        st.age_hours = round((ref - mtime).total_seconds() / 3600.0, 1)
    return st


def inventory_single_copy(paths: dict[str, str]) -> list[AssetInventory]:
    """
    Inventory directories whose contents exist **nowhere but the lab array**.

    Neither of the two is published: the driving-plan archive is deliberately
    outside ``data/`` so the publish rsync cannot republish Google's feed
    content (#176), and the OSM cache is unpublished GraphML. Both are therefore
    covered only by the backup configuration CSE IT fixed on 2026-08-05, which
    is why they are worth *counting* in a report an operator can compare against
    what IT says it is protecting.

    Losing them is not symmetric with losing a run CSV: a driving-plan snapshot
    is unrecoverable by construction (Google overwrites the feed in place), and
    a refetched OSM network yields different edge IDs and sample points, which
    breaks road-walk diff continuity (#101) rather than merely costing a
    download.
    """
    out = []
    for label, path in paths.items():
        if not os.path.isdir(path):
            out.append(AssetInventory(label=label, path=path, exists=False))
            continue
        count = 0
        total = 0
        newest = 0.0
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    stat = os.stat(fp)
                except OSError:
                    continue
                count += 1
                total += stat.st_size
                newest = max(newest, stat.st_mtime)
        out.append(
            AssetInventory(
                label=label,
                path=path,
                exists=True,
                file_count=count,
                total_bytes=total,
                newest_mtime=(
                    datetime.fromtimestamp(newest, tz=UTC).isoformat() if newest else None
                ),
            )
        )
    return out


def restore_backup(backup_path: str, dest_db_path: str) -> str:
    """
    Restore a dated backup to ``dest_db_path``, verifying before promoting.

    Deliberately a real function rather than a documented ``cp``: the restore
    path had never been exercised (recorded in ``deploy/README.md`` under #145),
    and a restore that is only ever described is a restore nobody has tested.
    ``tests/test_catalog_backup.py`` drills it end to end.

    Refuses to clobber an existing database — recovering onto a live catalog is
    a decision an operator should make explicitly (move it aside first), not
    something a helper does silently.
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"No such backup: {backup_path}")
    if os.path.exists(dest_db_path):
        raise FileExistsError(
            f"{dest_db_path} already exists; move it aside before restoring onto it"
        )

    src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True, timeout=10)
    try:
        integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Refusing to restore a corrupt backup: {integrity}")

        os.makedirs(os.path.dirname(os.path.abspath(dest_db_path)), exist_ok=True)
        tmp_path = dest_db_path + ".restoring"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        dest = sqlite3.connect(tmp_path, timeout=10)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()

    os.replace(tmp_path, dest_db_path)
    logger.info(f"Restored {backup_path} -> {dest_db_path}")
    return dest_db_path
