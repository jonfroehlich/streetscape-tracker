"""
Catalog backup mechanism and the restore drill (issue #145).

The restore drill is the point of this file. Until now the restore path was
*documented* (deploy/README.md) but had never been executed — and PR #187 said
so outright. A restore nobody has run is a restore nobody knows works, so
``test_restore_drill_*`` genuinely destroys a populated catalog and rebuilds it
from a dated backup.
"""

import json
import os
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from streetscape_metadata_tracker import catalog_backup, db


def _populate(conn, n_cities=3, offset=0):
    """A catalog with rows in the tables a restore has to bring back.

    ``offset`` shifts the generated names so a test can add *more* distinct
    cities to an already-populated catalog.
    """
    city_ids = []
    for i in range(offset, offset + n_cities):
        cid = db.register_city(
            conn,
            city_name=f"Testville {i}",
            state_name="Oregon",
            state_code="OR",
            country_name="United States",
            country_code="US",
            center_lat=44.0 + i,
            center_lon=-121.0 - i,
            grid_width_m=2000,
            grid_height_m=2000,
            step_m=20,
        )
        city_ids.append(cid)
        db.register_run(
            conn,
            city_id=cid,
            run_date=date(2026, 7, 1),
            csv_filename=f"{cid}_width_2000_height_2000_step_20_2026-07-01.csv.gz",
            provider="gsv",
            total_points=101,
            status_ok=90,
            coverage_rate_pct=89.1,
        )
        db.register_street_walk(
            conn,
            city_id=cid,
            run_date=date(2026, 7, 1),
            csv_filename=f"{cid}_width_2000_height_2000_step_20_streetwalk_sp15_2026-07-01.csv.gz",
            provider="gsv",
            network_type="drive",
            spacing_m=15,
            sample_points=1234,
            edges_total=50,
            coverage_pct_by_length=77.5,
        )
    conn.commit()
    return city_ids


def _counts(conn):
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("cities", "runs", "street_walks")
    }


# ─────────────────────────── the restore drill ───────────────────────────


def test_restore_drill_rebuilds_a_destroyed_catalog(conn, tmp_path, data_dir):
    """
    The drill #187 said had never been run: back up, DESTROY the original
    (database plus its WAL/SHM sidecars, which is what a real loss looks like
    for a WAL-mode catalog), restore, and prove the result is both intact and
    complete.
    """
    _populate(conn)
    before = _counts(conn)
    live_path = os.path.join(data_dir, "streetscape_tracker.db")

    backup_dir = str(tmp_path / "backups")
    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 7, 1))
    assert result.ok and result.integrity == "ok"

    # Destroy the original the way a media/filesystem loss would.
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(live_path + suffix):
            os.unlink(live_path + suffix)
    assert not os.path.exists(live_path)

    catalog_backup.restore_backup(result.path, live_path)

    restored = db.connect(live_path)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert _counts(restored) == before
        # Not just row counts — the frozen geometry a city's whole run series
        # depends on has to survive verbatim.
        row = restored.execute(
            "SELECT grid_width_m, grid_height_m, step_m, center_lat FROM cities "
            "ORDER BY city_id LIMIT 1"
        ).fetchone()
        assert (row[0], row[1], row[2]) == (2000, 2000, 20)
        # And the schema version, so the restored catalog isn't re-migrated.
        assert restored.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        restored.close()


def test_restore_refuses_when_orphaned_wal_sidecars_remain(conn, tmp_path):
    """
    The drill above deletes the sidecars; a real incident often does not. The
    catalog goes bad, the operator moves ``streetscape_tracker.db`` aside, and
    the ``-wal``/``-shm`` of the process that died stay behind.

    Nothing binds a WAL file to a particular database: SQLite replays whatever
    valid frames sit next to the main file. Restoring into that leaves you with
    the pre-restore state — silently, with integrity_check reporting "ok" — so
    the restore must refuse rather than produce a plausible-looking wrong
    catalog. Deleting them for the operator is not the answer either: an
    orphaned WAL can hold the only copy of the last committed writes.
    """
    live = str(tmp_path / "live.db")
    live_conn = sqlite3.connect(live)
    live_conn.execute("PRAGMA journal_mode=WAL")
    live_conn.execute("CREATE TABLE t (x TEXT)")
    live_conn.execute("INSERT INTO t VALUES ('in-the-backup')")
    live_conn.commit()

    backup_dir = str(tmp_path / "backups")
    result = catalog_backup.write_backup(live_conn, backup_dir, date(2026, 8, 7))
    assert result.ok

    # Writes that land in the WAL and are never checkpointed, then a kill.
    for i in range(300):
        live_conn.execute("INSERT INTO t VALUES (?)", (f"after-the-backup-{i}",))
    live_conn.commit()
    assert os.path.getsize(live + "-wal") > 0
    # Deliberately NOT closed: a clean close checkpoints and removes the
    # sidecars, and the case being modelled is a process that was killed.
    os.rename(live, live + ".corrupt-aside")
    assert os.path.exists(live + "-wal")

    with pytest.raises(FileExistsError) as excinfo:
        catalog_backup.restore_backup(result.path, live)
    assert "-wal" in str(excinfo.value)
    assert not os.path.exists(live), "a refused restore must leave no file behind"

    # Once the operator moves them aside, the restore is the backup's contents —
    # NOT the post-backup rows the stale WAL was carrying.
    for suffix in ("-wal", "-shm"):
        if os.path.exists(live + suffix):
            os.rename(live + suffix, live + suffix + ".aside")
    catalog_backup.restore_backup(result.path, live)
    restored = sqlite3.connect(live)
    try:
        rows = [r[0] for r in restored.execute("SELECT x FROM t")]
    finally:
        restored.close()
    assert rows == ["in-the-backup"]


def test_restore_refuses_to_clobber_an_existing_catalog(conn, tmp_path, data_dir):
    """Restoring onto a live catalog is an operator decision, not a silent
    overwrite — the wrong default here loses the very data you're recovering."""
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 7, 1))

    with pytest.raises(FileExistsError):
        catalog_backup.restore_backup(result.path, os.path.join(data_dir, "streetscape_tracker.db"))


def test_restore_refuses_a_corrupt_backup(conn, tmp_path):
    """A corrupt backup must fail loudly rather than produce a half-restored
    catalog that looks like a successful recovery."""
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 7, 1))

    with open(result.path, "r+b") as f:
        f.seek(2048)
        f.write(b"\xde\xad\xbe\xef" * 512)

    dest = str(tmp_path / "restored.db")
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        catalog_backup.restore_backup(result.path, dest)
    assert not os.path.exists(dest), "a refused restore must leave no partial file"


# ─────────────────────────── write + verify ───────────────────────────


def test_backup_is_dated_verified_and_atomically_promoted(conn, tmp_path):
    _populate(conn)
    backup_dir = str(tmp_path / "backups")
    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    assert result.ok
    assert os.path.basename(result.path) == "streetscape_tracker.db.2026-08-07.backup"
    assert result.integrity == "ok"
    assert result.bytes_written > 0
    assert result.row_counts["cities"] == 3
    # Nothing but the copy and its status survives a successful promotion — an
    # endswith(".tmp") check would miss orphaned .tmp-wal/.tmp-shm sidecars.
    assert sorted(os.listdir(backup_dir)) == [
        catalog_backup.STATUS_FILENAME,
        "streetscape_tracker.db.2026-08-07.backup",
    ]


def test_failed_integrity_check_keeps_the_previous_good_backup(conn, tmp_path, monkeypatch):
    """
    The reason for verify-then-promote. A night that produces a bad copy must
    not destroy the last good one — with a single rolling in-place file, it did.
    """
    _populate(conn)
    backup_dir = str(tmp_path / "backups")
    good = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))
    good_bytes = open(good.path, "rb").read()

    monkeypatch.setattr(
        catalog_backup,
        "_verify",
        lambda dest: "*** in database main: page 3 is never used",
    )
    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    assert not result.ok
    assert "integrity_check failed" in result.error
    assert open(good.path, "rb").read() == good_bytes, "the good backup was overwritten"
    assert not any(p.endswith(".tmp") for p in os.listdir(backup_dir))


def test_same_day_rebackup_replaces_in_place(conn, tmp_path):
    """The scheduler backs up twice a night — once before the city loop, once in
    the tail once runs are registered. The second must replace the first, not
    accumulate a second file for the same date."""
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    first = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))
    first_rows = first.row_counts["runs"]

    # The night collects more.
    _populate(conn, n_cities=2, offset=1)
    second = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    assert second.path == first.path
    assert len(catalog_backup.list_backups(backup_dir)) == 1
    assert second.row_counts["runs"] > first_rows, "the tail copy must see the night's runs"


def test_the_staging_name_identifies_the_writing_process(tmp_path, monkeypatch):
    """
    Two processes must never derive the same staging path for one date, and
    neither staging file may be mistakable for a backup.
    """
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    final = os.path.join(backup_dir, catalog_backup.backup_filename(date(2026, 8, 7)))

    monkeypatch.setattr(catalog_backup.os, "getpid", lambda: 111)
    a = catalog_backup._staging_path(final)
    monkeypatch.setattr(catalog_backup.os, "getpid", lambda: 222)
    b = catalog_backup._staging_path(final)

    assert a != b
    for path in (a, b):
        assert path.startswith(final) and path.endswith(".tmp")
        open(path, "wb").close()
    # list_backups' regex requires the name to END at .backup, so a staging file
    # is invisible rather than reported as a restore point.
    assert catalog_backup.list_backups(backup_dir) == []


def test_a_concurrent_writer_cannot_have_its_staging_file_stolen(conn, tmp_path, monkeypatch):
    """
    Two ``run-due`` runs can overlap — the nightly timer and an operator's
    on-demand catch-up (issue #214) are both supported and nothing serializes
    them — and both stage the SAME date's copy. With one shared ``.tmp`` name that
    interleaving silently tore the day's backup: B's "clear any leftover staging
    file" unlinked the file A was mid-copy into, A went on writing to the unlinked
    inode, A verified *its own* copy, and A's ``os.replace`` then promoted whatever
    now sat at that path — B's half-written file — as the day's verified backup.

    Stand in for process A with a staging file it is still copying into, and
    assert the writer running here neither removes nor promotes it. A's path is
    derived THROUGH the module under a different pid, deliberately: revert the
    staging name to a shared one and both writers resolve to the same file, so
    this fails rather than passing on a name the test invented.
    """
    _populate(conn)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    final = os.path.join(backup_dir, catalog_backup.backup_filename(date(2026, 8, 7)))
    real_pid = os.getpid()
    with monkeypatch.context() as m:
        m.setattr(os, "getpid", lambda: real_pid + 1)
        in_flight = catalog_backup._staging_path(final)
    with open(in_flight, "wb") as f:
        f.write(b"process A is still copying into this")

    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    assert result.ok
    assert open(in_flight, "rb").read() == b"process A is still copying into this", (
        "a live concurrent copy's staging file was disturbed"
    )
    # The promoted file is this process's verified copy, not the other writer's.
    assert result.row_counts["cities"] == 3
    assert os.path.getsize(result.path) > len(b"process A is still copying into this")


def test_stale_staging_files_are_swept_but_live_ones_survive(conn, tmp_path):
    """
    The old code cleared exactly one ``.tmp`` because the name was deterministic.
    Now that it names the writer, an abandoned copy would otherwise accumulate
    forever in the directory an operator inspects during an incident — so age is
    the test, with a threshold no live copy can fall under (every copy is bounded
    by BACKUP_TIMEOUT_S). A stranded sidecar goes with the file it belonged to.
    """
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    final = os.path.join(backup_dir, catalog_backup.backup_filename(date(2026, 8, 7)))
    stale, fresh = f"{final}.deadhost.1.tmp", f"{final}.livehost.2.tmp"
    for path in (stale, fresh, stale + "-wal"):
        open(path, "wb").close()
    old = time.time() - catalog_backup._STALE_TMP_AFTER_S - 60
    for path in (stale, stale + "-wal"):
        os.utime(path, (old, old))

    assert catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7)).ok

    assert not os.path.exists(stale)
    assert not os.path.exists(stale + "-wal")
    assert os.path.exists(fresh), "a live concurrent copy must never be swept"


def test_row_counts_describe_the_copy_not_the_live_catalog(conn, tmp_path):
    """
    Provenance has to describe the file an operator is holding. Counting the
    source would report whatever the live catalog says at that moment, which is
    not necessarily what the copy contains.
    """
    _populate(conn, n_cities=2)
    backup_dir = str(tmp_path / "backups")
    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    copy = sqlite3.connect(result.path)
    try:
        in_file = {
            t: copy.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in result.row_counts
        }
    finally:
        copy.close()
    catalog_backup._remove_sidecars(result.path)

    assert result.row_counts == in_file


def test_an_open_source_transaction_fails_fast_instead_of_hanging(conn, tmp_path):
    """
    sqlite3's backup API retries SQLITE_BUSY forever, and an uncommitted write
    on the SOURCE connection is a busy source. In the pre-flight position that
    would stall the whole night into a systemd SIGKILL, so name it immediately.
    """
    _populate(conn, n_cities=1)
    conn.execute("UPDATE cities SET grid_width_m = grid_width_m")  # uncommitted write
    assert conn.in_transaction

    result = catalog_backup.write_backup(conn, str(tmp_path / "backups"), date(2026, 8, 7))

    assert not result.ok
    assert "open transaction" in result.error
    conn.rollback()


def test_a_busy_source_times_out_instead_of_hanging(tmp_path, monkeypatch):
    """The general case of the above: a lock held by somebody else. The copy is
    bounded by BACKUP_TIMEOUT_S via the progress callback, which sqlite3 invokes
    on busy retries too — without it there is no timeout at any layer."""
    src_path = str(tmp_path / "busy.db")
    # Rollback-journal mode (not WAL), so a competing writer genuinely blocks
    # the backup's reader instead of being invisible to it.
    source = sqlite3.connect(src_path)
    source.execute("CREATE TABLE t (x)")
    source.commit()
    blocker = sqlite3.connect(src_path, timeout=0.1)
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.execute("INSERT INTO t VALUES (1)")

    monkeypatch.setattr(catalog_backup, "BACKUP_TIMEOUT_S", 1.0)
    started = time.monotonic()
    result = catalog_backup.write_backup(source, str(tmp_path / "backups"), date(2026, 8, 7))
    elapsed = time.monotonic() - started
    blocker.rollback()

    assert not result.ok
    assert "exceeded" in result.error
    assert elapsed < 30, "the copy must be bounded, not merely eventually interrupted"


def test_backup_never_raises_on_a_broken_destination(conn, tmp_path):
    """A backup problem must be reported, never allowed to abort a collection
    night — the whole tail (aggregate, publish) still has to run."""
    _populate(conn, n_cities=1)
    # A file where the directory should be makes makedirs/connect fail.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    result = catalog_backup.write_backup(conn, str(blocked), date(2026, 8, 7))
    assert not result.ok and result.error


# ─────────────────────────── retention ───────────────────────────


def test_prune_keeps_the_retention_window(conn, tmp_path):
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    today = date(2026, 8, 7)
    for age in (0, 3, 13, 14, 20, 60):
        catalog_backup.write_backup(conn, backup_dir, today - timedelta(days=age))

    catalog_backup.prune_backups(backup_dir, today)
    kept = {d.isoformat() for d, _ in catalog_backup.list_backups(backup_dir)}

    assert (today - timedelta(days=13)).isoformat() in kept
    assert (today - timedelta(days=20)).isoformat() not in kept
    assert (today - timedelta(days=60)).isoformat() not in kept


def test_prune_never_deletes_the_only_remaining_backup(conn, tmp_path):
    """
    Load-bearing, not tidiness. If backups have been failing for longer than the
    retention window, a naive prune slides past the last good copy and deletes
    it — converting a silent reporting failure into actual data loss.
    """
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    stale = date(2026, 1, 1)
    catalog_backup.write_backup(conn, backup_dir, stale)

    # Six months later, nothing has succeeded since.
    pruned = catalog_backup.prune_backups(backup_dir, date(2026, 8, 7))

    assert pruned == []
    assert len(catalog_backup.list_backups(backup_dir)) == 1


def test_prune_keeps_newest_even_when_every_file_is_stale(conn, tmp_path):
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    for d in (date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)):
        catalog_backup.write_backup(conn, backup_dir, d)

    catalog_backup.prune_backups(backup_dir, date(2026, 8, 7))
    kept = [d for d, _ in catalog_backup.list_backups(backup_dir)]

    assert kept == [date(2026, 3, 1)], "the newest survives; the rest age out"


def test_promotion_clears_stale_sidecars_beside_the_backup(conn, tmp_path):
    """
    The backup inherits WAL format from the source, so *any* read of a dated
    copy — including a read-only one, which cannot clean up after itself —
    leaves a -wal/-shm pair beside it. The tail then replaces the file under
    them, and on the next open SQLite would replay frames belonging to the copy
    that is no longer there.
    """
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    first = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    # Reading the pre-flight copy the way an operator (or restore_backup) would.
    ro = sqlite3.connect(f"file:{first.path}?mode=ro", uri=True)
    ro.execute("PRAGMA integrity_check").fetchone()
    ro.close()
    assert any(os.path.exists(p) for p in catalog_backup.sidecar_paths(first.path))

    # The tail's second copy of the night.
    catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    assert not any(os.path.exists(p) for p in catalog_backup.sidecar_paths(first.path))
    assert sorted(os.listdir(backup_dir)) == [
        catalog_backup.STATUS_FILENAME,
        "streetscape_tracker.db.2026-08-07.backup",
    ]


def test_prune_takes_sidecars_with_the_file(conn, tmp_path):
    """An orphaned sidecar is invisible to list_backups/backup_status and
    replayable into a future file of the same name, so it must not outlive the
    backup it belonged to."""
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    # Newest first, so the stale copy is written without being pruned on the way
    # in (write_backup prunes relative to the date it is given).
    catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))
    old = catalog_backup.write_backup(conn, backup_dir, date(2026, 1, 1))
    for path in catalog_backup.sidecar_paths(old.path):
        with open(path, "wb") as f:
            f.write(b"stale")

    pruned = catalog_backup.prune_backups(backup_dir, date(2026, 8, 7))

    assert pruned == [old.path]
    assert not any(os.path.exists(p) for p in catalog_backup.sidecar_paths(old.path))


def test_list_backups_ignores_unrelated_files(conn, tmp_path):
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))
    # The legacy single rolling copy, and a junk file.
    (tmp_path / "backups" / "streetscape_tracker.db.backup").write_text("legacy")
    (tmp_path / "backups" / "notes.txt").write_text("hi")

    assert [d for d, _ in catalog_backup.list_backups(backup_dir)] == [date(2026, 8, 7)]


# ─────────────────────────── status reporting ───────────────────────────


def test_status_records_provenance_on_success(conn, tmp_path):
    """
    Provenance exists because a backup of a *test fixture* catalog was once
    mistaken for the production one — the file alone can't tell you which
    database it came from or how complete it is.
    """
    _populate(conn)
    backup_dir = str(tmp_path / "backups")
    catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7), source_db="/data/real.db")

    status = json.loads((tmp_path / "backups" / "backup_status.json").read_text())
    assert status["ok"] is True
    assert status["source_db"] == "/data/real.db"
    assert status["source_host"]
    assert status["row_counts"]["cities"] == 3
    assert status["retention_days"] == catalog_backup.KEEP_DAYS
    assert status["backups_present"] == ["streetscape_tracker.db.2026-08-07.backup"]


def test_status_is_written_on_failure_too(conn, tmp_path):
    """
    A failed backup that wrote nothing is otherwise indistinguishable from one
    that never ran — the ambiguity that let #145 go unnoticed for months.
    """
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    # Make the destination unwritable by putting a directory in its place.
    os.makedirs(os.path.join(backup_dir, catalog_backup.backup_filename(date(2026, 8, 7))))

    result = catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))
    assert not result.ok

    status = catalog_backup.read_status(backup_dir)
    assert status is not None
    assert status["ok"] is False
    assert status["error"]


def test_backup_status_reports_age_and_totals(conn, tmp_path):
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 6))
    catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    st = catalog_backup.backup_status(backup_dir)
    assert st.exists and st.file_count == 2
    assert st.newest_date == "2026-08-07"
    assert st.total_bytes > 0
    assert st.age_hours is not None and st.age_hours >= 0
    assert st.last_attempt["ok"] is True


def test_backup_status_flags_a_stale_newest_copy(conn, tmp_path):
    """
    "The last attempt succeeded" stays true forever once nothing is running —
    a masked timer, a disabled unit, a ConditionHost that stopped matching after
    a host cutover. Since the newest copy is deliberately never pruned, that
    state otherwise presents as one file plus an ok status, which is #145's
    exact shape. Age is the signal that survives it.
    """
    _populate(conn, n_cities=1)
    backup_dir = str(tmp_path / "backups")
    catalog_backup.write_backup(conn, backup_dir, date(2026, 8, 7))

    fresh = catalog_backup.backup_status(backup_dir)
    assert fresh.stale is False

    # Same directory, same successful status file — only the clock moved on.
    later = datetime.now(UTC) + timedelta(hours=catalog_backup.STALE_AFTER_HOURS + 1)
    stale = catalog_backup.backup_status(backup_dir, now=later)
    assert stale.stale is True
    assert stale.last_attempt["ok"] is True, "the outcome alone would still say healthy"


def test_backup_status_on_a_missing_directory(tmp_path):
    st = catalog_backup.backup_status(str(tmp_path / "nope"))
    assert st.exists is False and st.file_count == 0 and st.newest_path is None


def test_read_status_tolerates_a_truncated_file(tmp_path):
    """Status reading feeds an operator command; malformed JSON must degrade to
    'unknown', not crash the report."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / catalog_backup.STATUS_FILENAME).write_text('{"ok": tr')
    assert catalog_backup.read_status(str(backup_dir)) is None


# ─────────────────────── single-copy inventory ───────────────────────


def test_inventory_counts_single_copy_assets(tmp_path):
    archive = tmp_path / "archive" / "gsv_driving_plan"
    archive.mkdir(parents=True)
    (archive / "gsv_driving_plan_2026-08-01.json.gz").write_bytes(b"x" * 100)
    (archive / "gsv_driving_plan_2026-08-05.json.gz").write_bytes(b"y" * 250)

    inv = catalog_backup.inventory_single_copy(
        {"driving-plan archive": str(archive), "osm cache": str(tmp_path / "absent")}
    )
    by_label = {a.label: a for a in inv}

    assert by_label["driving-plan archive"].exists
    assert by_label["driving-plan archive"].file_count == 2
    assert by_label["driving-plan archive"].total_bytes == 350
    assert by_label["driving-plan archive"].newest_mtime
    assert by_label["osm cache"].exists is False
    assert by_label["osm cache"].file_count == 0
