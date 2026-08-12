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
from datetime import date, timedelta

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
    # No temp file survives a successful promotion.
    assert not any(p.endswith(".tmp") for p in os.listdir(backup_dir))


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
