"""Catalog tests: registration, aliases, runs, diffs, budget, scheduling."""

import json
import os
import sqlite3
from datetime import date

import pytest

from streetscape_metadata_tracker import db


@pytest.fixture
def city(conn):
    return db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )


def test_register_city_derives_canonical_id(conn, city):
    assert city == "bend--oregon--united-states"
    row = db.resolve_city(conn, "Bend, Oregon, United States")
    assert row.grid_width_m == 5000 and row.enabled


def test_register_city_is_idempotent_and_freezes_geometry(conn, city):
    again = db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=99.9,
        center_lon=99.9,  # different geometry...
        grid_width_m=1,
        grid_height_m=1,
        step_m=1,
    )
    assert again == city
    row = db.resolve_city(conn, city)
    assert row.center_lat == 44.05 and row.grid_width_m == 5000  # ...must not overwrite


def test_alias_resolution(conn, city):
    db.add_alias(conn, "bend--or", city)
    assert db.resolve_city(conn, "Bend, OR").city_id == city
    assert db.resolve_city(conn, "Nowhere, KS") is None


def test_register_city_disabled_with_notes(conn):
    cid = db.register_city(
        conn,
        city_name="Vetville",
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code=None,
        center_lat=1.0,
        center_lon=2.0,
        grid_width_m=1000,
        grid_height_m=1000,
        step_m=20,
        enabled=False,
        notes="worldwide frame; pending boundary vetting",
    )
    row = db.resolve_city(conn, cid)
    assert row.enabled is False
    assert row.notes == "worldwide frame; pending boundary vetting"
    # disabled cities stay out of scheduler-facing queries
    assert cid not in {c.city_id for c in db.get_all_cities(conn, enabled_only=True)}


def test_update_city_geometry_overwrites_and_appends_note(conn, city):
    db.update_city_geometry(
        conn,
        city_id=city,
        center_lat=44.10,
        center_lon=-121.40,
        grid_width_m=18000,
        grid_height_m=20000,
        notes="regeom #91",
    )
    row = db.resolve_city(conn, city)
    assert (row.center_lat, row.center_lon) == (44.10, -121.40)
    assert row.grid_width_m == 18000 and row.grid_height_m == 20000
    assert row.step_m == 20  # step is untouched
    assert row.notes == "regeom #91"
    # A second correction appends to (does not clobber) the audit-trail note.
    db.update_city_geometry(
        conn,
        city_id=city,
        center_lat=44.11,
        center_lon=-121.41,
        grid_width_m=18000,
        grid_height_m=20000,
        notes="regeom #91 again",
    )
    assert db.resolve_city(conn, city).notes == "regeom #91\nregeom #91 again"


def test_update_city_geometry_unknown_city_raises(conn):
    with pytest.raises(KeyError):
        db.update_city_geometry(
            conn,
            city_id="nope",
            center_lat=1.0,
            center_lon=2.0,
            grid_width_m=100,
            grid_height_m=100,
        )


def test_runs_ordering_and_uniqueness(conn, city):
    r1 = db.register_run(conn, city_id=city, run_date=date(2026, 4, 1), csv_filename="a.csv.gz")
    r2 = db.register_run(conn, city_id=city, run_date=date(2026, 7, 1), csv_filename="b.csv.gz")
    assert db.get_latest_run(conn, city).run_id == r2
    assert db.get_previous_run(conn, city, date(2026, 7, 1)).run_id == r1
    assert [r.run_id for r in db.get_runs_for_city(conn, city)] == [r1, r2]
    with pytest.raises(sqlite3.IntegrityError):  # same city+date rejected
        db.register_run(conn, city_id=city, run_date=date(2026, 7, 1), csv_filename="c.csv.gz")


def test_register_run_round_trips_status_no_date(conn, city):
    rid = db.register_run(
        conn,
        city_id=city,
        run_date=date(2026, 4, 1),
        csv_filename="a.csv.gz",
        total_points=10,
        status_ok=6,
        status_no_date=2,
        status_zero_results=2,
        status_other=0,
        unique_panos=8,
        unique_google_panos=8,
        coverage_rate_pct=80.0,
    )
    run = db.get_latest_run(conn, city)
    assert run.run_id == rid
    assert run.status_no_date == 2  # new v4 column persists
    assert run.status_ok == 6 and run.unique_panos == 8
    assert abs(run.coverage_rate_pct - 80.0) < 1e-9


def test_register_run_status_no_date_defaults_null(conn, city):
    # Callers that omit status_no_date (e.g. legacy paths) store NULL, not 0.
    db.register_run(conn, city_id=city, run_date=date(2026, 4, 1), csv_filename="a.csv.gz")
    assert db.get_latest_run(conn, city).status_no_date is None


def test_diff_storage(conn, city):
    r1 = db.register_run(conn, city_id=city, run_date=date(2026, 4, 1), csv_filename="a.csv.gz")
    r2 = db.register_run(conn, city_id=city, run_date=date(2026, 7, 1), csv_filename="b.csv.gz")
    db.record_diff(
        conn,
        city_id=city,
        from_run_id=r1,
        to_run_id=r2,
        grid_aligned=True,
        panos_added=5,
        panos_removed=2,
        panos_persisted=93,
        capture_date_changed=1,
        points_gained_coverage=3,
        points_lost_coverage=1,
        coverage_delta_pct=0.5,
        detail_filename="d.csv.gz",
    )
    row = db.get_diff_for_run(conn, r2)
    assert row["panos_added"] == 5 and row["grid_aligned"] == 1


def _diff(conn, city, from_run_id, to_run_id, **overrides):
    kwargs = dict(
        city_id=city,
        from_run_id=from_run_id,
        to_run_id=to_run_id,
        grid_aligned=True,
        panos_added=0,
        panos_removed=0,
        panos_persisted=0,
        capture_date_changed=0,
        points_gained_coverage=0,
        points_lost_coverage=0,
        coverage_delta_pct=0.0,
        detail_filename=None,
    )
    kwargs.update(overrides)
    return db.record_diff(conn, **kwargs)


def test_latest_runs_emits_one_row_per_city_provider_even_when_re_diffed(conn, city):
    # run_diffs is UNIQUE on (from_run_id, to_run_id), NOT on to_run_id alone,
    # so a run diffed against two different predecessors — what happens when an
    # earlier run is purged and the diff recomputed — matches the join twice. A
    # bare LEFT JOIN emitted that (city, provider) twice, and the caller's
    # last-write-wins dict then advertised whichever baseline SQLite happened
    # to return last.
    r1 = db.register_run(conn, city_id=city, run_date=date(2026, 1, 1), csv_filename="a.csv.gz")
    r2 = db.register_run(conn, city_id=city, run_date=date(2026, 4, 1), csv_filename="b.csv.gz")
    r3 = db.register_run(conn, city_id=city, run_date=date(2026, 7, 1), csv_filename="c.csv.gz")
    _diff(conn, city, r1, r3, capture_date_changed=11)
    _diff(conn, city, r2, r3, capture_date_changed=22)

    rows = db.get_latest_runs_all(conn)

    assert len(rows) == 1, "one row per (city, provider), regardless of how many diffs point at it"
    # And the choice is explicit rather than arbitrary: the most recently
    # computed comparison (highest diff_id) wins.
    assert rows[0]["capture_date_changed"] == 22


def test_latest_runs_still_returns_a_run_that_was_never_diffed(conn, city):
    # The join must stay a LEFT one — a first run has no diff, and dropping it
    # from the aggregate would erase every newly-collected city.
    db.register_run(conn, city_id=city, run_date=date(2026, 7, 1), csv_filename="only.csv.gz")
    rows = db.get_latest_runs_all(conn)
    assert len(rows) == 1
    assert rows[0]["capture_date_changed"] is None


def test_api_usage_ledger(conn):
    d = date(2026, 7, 1)
    assert db.get_api_usage(conn, d) == 0
    db.add_api_usage(conn, d, 100)
    db.add_api_usage(conn, d, 50)
    assert db.get_api_usage(conn, d) == 150


def test_due_selection_lifecycle(conn, city):
    kw = dict(today=date(2026, 7, 2), cycle_days=90, grace_days=7, max_consecutive_failures=5)
    db.assign_schedule(conn, 90)
    assert [c.city_id for c in db.get_due_cities(conn, **kw)] == [city]  # never run

    db.record_attempt(conn, city, success=True)
    assert db.get_due_cities(conn, **kw) == []  # fresh

    # Failure cap: repeated failures eventually remove the city from `due`
    for _ in range(5):
        db.record_attempt(conn, city, success=False, error="boom")
    row = conn.execute(
        "SELECT consecutive_failures, last_error FROM schedule_state WHERE city_id = ?", (city,)
    ).fetchone()
    assert row["consecutive_failures"] == 5 and row["last_error"] == "boom"


def test_stagger_is_stable_and_spread():
    days = [db.compute_day_of_cycle(f"city-{i}", 90) for i in range(900)]
    assert days == [db.compute_day_of_cycle(f"city-{i}", 90) for i in range(900)]
    assert len(set(days)) == 90  # every day of the cycle gets cities


# ── Provider dimension (schema v2) ─────────────────────────────────────────


def test_runs_per_provider_series(conn, city):
    g1 = db.register_run(conn, city_id=city, run_date=date(2026, 4, 1), csv_filename="g1.csv.gz")
    m1 = db.register_run(
        conn,
        city_id=city,
        run_date=date(2026, 4, 1),
        csv_filename="m1.csv.gz",
        provider="mapillary",
    )
    m2 = db.register_run(
        conn,
        city_id=city,
        run_date=date(2026, 7, 1),
        csv_filename="m2.csv.gz",
        provider="mapillary",
    )
    # Same city+date is fine across providers, rejected within one
    with pytest.raises(sqlite3.IntegrityError):
        db.register_run(
            conn,
            city_id=city,
            run_date=date(2026, 4, 1),
            csv_filename="m1b.csv.gz",
            provider="mapillary",
        )
    # Lookups are per-provider series
    assert db.get_latest_run(conn, city).run_id == g1
    assert db.get_latest_run(conn, city, provider="mapillary").run_id == m2
    assert db.get_previous_run(conn, city, date(2026, 7, 1), provider="mapillary").run_id == m1
    # gsv series is independent: nothing before its own first run
    assert db.get_previous_run(conn, city, date(2026, 4, 1)) is None
    assert [r.run_id for r in db.get_runs_for_city(conn, city)] == [g1]
    assert [r.run_id for r in db.get_runs_for_city(conn, city, provider="mapillary")] == [m1, m2]
    assert [r.run_id for r in db.get_runs_for_city(conn, city, provider=None)] == [g1, m1, m2]


def test_api_usage_ledger_per_provider(conn):
    d = date(2026, 7, 1)
    db.add_api_usage(conn, d, 100)
    db.add_api_usage(conn, d, 30, provider="mapillary")
    db.add_api_usage(conn, d, 30, provider="mapillary")
    assert db.get_api_usage(conn, d) == 100
    assert db.get_api_usage(conn, d, provider="mapillary") == 60


def test_schedule_state_per_provider(conn, city):
    kw = dict(today=date(2026, 7, 2), cycle_days=90, grace_days=7, max_consecutive_failures=5)
    db.assign_schedule(conn, 90, providers=("gsv", "mapillary"))
    # Both providers land on the same cycle day (paired snapshots)
    days = conn.execute(
        "SELECT DISTINCT day_of_cycle FROM schedule_state WHERE city_id = ?", (city,)
    ).fetchall()
    assert len(days) == 1

    # A gsv success leaves the city due for mapillary, and vice versa
    db.record_attempt(conn, city, success=True)
    assert db.get_due_cities(conn, **kw) == []
    assert [c.city_id for c in db.get_due_cities(conn, provider="mapillary", **kw)] == [city]

    # Failures accrue per provider
    for _ in range(5):
        db.record_attempt(conn, city, success=False, error="boom", provider="mapillary")
    assert db.get_due_cities(conn, provider="mapillary", **kw) == []
    row = conn.execute(
        "SELECT consecutive_failures FROM schedule_state WHERE city_id = ? AND provider = 'gsv'",
        (city,),
    ).fetchone()
    assert row["consecutive_failures"] == 0


# The v1 schema verbatim (pre-provider), for migration testing.
_V1_SCHEMA = """
CREATE TABLE cities (
    city_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    city_name      TEXT NOT NULL,
    state_name     TEXT,
    state_code     TEXT,
    country_name   TEXT,
    country_code   TEXT,
    center_lat     REAL NOT NULL,
    center_lon     REAL NOT NULL,
    grid_width_m   INTEGER NOT NULL,
    grid_height_m  INTEGER NOT NULL,
    step_m         INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    notes          TEXT
);
CREATE TABLE city_aliases (
    alias_slug     TEXT PRIMARY KEY,
    city_id        TEXT NOT NULL REFERENCES cities(city_id)
);
CREATE TABLE runs (
    run_id              INTEGER PRIMARY KEY,
    city_id             TEXT NOT NULL REFERENCES cities(city_id),
    run_date            TEXT NOT NULL,
    csv_filename        TEXT NOT NULL UNIQUE,
    json_filename       TEXT,
    is_baseline         INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT,
    finished_at         TEXT,
    duration_seconds    REAL,
    total_points        INTEGER,
    status_ok           INTEGER,
    status_zero_results INTEGER,
    status_other        INTEGER,
    unique_panos        INTEGER,
    unique_google_panos INTEGER,
    coverage_rate_pct   REAL,
    oldest_capture_date TEXT,
    newest_capture_date TEXT,
    median_pano_age_years REAL,
    api_requests        INTEGER,
    UNIQUE (city_id, run_date)
);
CREATE INDEX idx_runs_city_date ON runs(city_id, run_date DESC);
CREATE TABLE run_diffs (
    diff_id                INTEGER PRIMARY KEY,
    city_id                TEXT NOT NULL REFERENCES cities(city_id),
    from_run_id            INTEGER NOT NULL REFERENCES runs(run_id),
    to_run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    grid_aligned           INTEGER NOT NULL,
    panos_added            INTEGER,
    panos_removed          INTEGER,
    panos_persisted        INTEGER,
    capture_date_changed   INTEGER,
    points_gained_coverage INTEGER,
    points_lost_coverage   INTEGER,
    coverage_delta_pct     REAL,
    detail_filename        TEXT,
    computed_at            TEXT NOT NULL,
    UNIQUE (from_run_id, to_run_id)
);
CREATE TABLE api_usage (
    usage_date  TEXT PRIMARY KEY,
    requests    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE schedule_state (
    city_id              TEXT PRIMARY KEY REFERENCES cities(city_id),
    day_of_cycle         INTEGER NOT NULL,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);
"""


def test_migrate_v1_to_v2(tmp_path):
    db_path = str(tmp_path / "v1.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(_V1_SCHEMA)
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute(
        """INSERT INTO runs (run_id, city_id, run_date, csv_filename,
           unique_panos, unique_google_panos)
           VALUES (7, 'bend--or', '2026-04-01', 'a.csv.gz', 100, 90)"""
    )
    raw.execute(
        """INSERT INTO run_diffs (city_id, from_run_id, to_run_id,
           grid_aligned, computed_at)
           VALUES ('bend--or', 7, 7, 1, '2026-04-01T00:00:00+00:00')"""
    )
    raw.execute("INSERT INTO api_usage VALUES ('2026-04-01', 12345)")
    raw.execute(
        """INSERT INTO schedule_state (city_id, day_of_cycle, last_success_at)
           VALUES ('bend--or', 42, '2026-04-01T00:00:00+00:00')"""
    )
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)  # triggers the migration (v1 -> current)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # v4 added status_no_date; a migrated legacy run has it as NULL (unknown)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "status_no_date" in cols
    # v5 added the street_networks table (empty on a migrated catalog)
    assert conn.execute("SELECT COUNT(*) FROM street_networks").fetchone()[0] == 0

    run = db.get_latest_run(conn, "bend--or")
    assert run.run_id == 7 and run.provider == "gsv"
    assert run.unique_panos == 100 and run.unique_google_panos == 90
    assert run.status_no_date is None
    assert db.get_api_usage(conn, date(2026, 4, 1)) == 12345
    row = conn.execute("SELECT provider, day_of_cycle FROM schedule_state").fetchone()
    assert (row["provider"], row["day_of_cycle"]) == ("gsv", 42)
    assert db.get_diff_for_run(conn, 7)["grid_aligned"] == 1

    # Idempotent: reopening must not migrate again or lose anything
    conn.close()
    conn2 = db.connect(db_path)
    assert db.get_latest_run(conn2, "bend--or").run_id == 7
    conn2.close()


def test_connect_migrates_legacy_gsv_tracker_db(tmp_path):
    """A pre-rename catalog named gsv_tracker.db is transparently renamed to the
    new streetscape_tracker.db on first connect (GSV Tracker -> Streetscape
    Tracker back-compat), preserving its contents."""
    data_dir = str(tmp_path)
    legacy_path = os.path.join(data_dir, "gsv_tracker.db")

    # Create and populate a legacy-named catalog, then fully close it.
    conn = db.connect(legacy_path)
    db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )
    conn.close()

    # Opening the NEW default path in the same dir migrates the legacy file.
    new_path = db.get_default_db_path(data_dir)
    assert new_path.endswith("streetscape_tracker.db")
    conn2 = db.connect(new_path)
    assert not os.path.exists(legacy_path)
    assert os.path.exists(new_path)
    assert (
        db.resolve_city(conn2, "Bend, Oregon, United States").city_id
        == "bend--oregon--united-states"
    )
    conn2.close()


# ── Frozen OSM street networks (issue #103, schema v5) ──────────────────────


def test_migrate_v4_to_v5(tmp_path):
    """A v4 catalog gains the street_networks table on connect.

    A v4 catalog is exactly the current schema minus street_networks, so the
    fixture is built from db._SCHEMA with that table dropped (keeping the
    fixture in sync with the code) and stamped user_version = 4.
    """
    db_path = str(tmp_path / "v4.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    # v4 predates both street_networks (v5) and street_walks (v6).
    raw.execute("DROP TABLE street_networks")
    raw.execute("DROP TABLE street_walks")
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM street_networks").fetchone()[0] == 0
    # Existing data is untouched by the additive migration.
    assert db.resolve_city(conn, "bend--or").city_id == "bend--or"

    # Idempotent: reopening must not error or re-migrate.
    conn.close()
    conn2 = db.connect(db_path)
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn2.close()


def test_migrate_v5_to_v6(tmp_path):
    """A v5 catalog gains the street_walks table on connect (additive).

    A v5 catalog is the current schema minus street_walks, stamped
    user_version = 5; connecting must create the table and stamp v6.
    """
    db_path = str(tmp_path / "v5.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    raw.execute("DROP TABLE street_walks")
    raw.execute("PRAGMA user_version = 5")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 0
    conn.close()


def test_migrate_v6_to_v7(tmp_path):
    """A v6 catalog gains the issue-#116 imagery-type columns on connect.

    A v6 runs table is the current one minus status_flat_only /
    any_imagery_coverage_rate_pct / num_flat_images. Build the fixture from
    db._SCHEMA and DROP those columns (keeping it in sync with the code),
    seed a pre-v7 run, stamp user_version = 6, then connect.
    """
    db_path = str(tmp_path / "v6.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    for col in ("status_flat_only", "any_imagery_coverage_rate_pct", "num_flat_images"):
        raw.execute(f"ALTER TABLE runs DROP COLUMN {col}")
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute(
        """INSERT INTO runs (city_id, provider, run_date, csv_filename, coverage_rate_pct)
           VALUES ('bend--or', 'mapillary', '2026-05-01', 'old.csv.gz', 42.0)"""
    )
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert {"status_flat_only", "any_imagery_coverage_rate_pct", "num_flat_images"} <= cols
    # Pre-v7 run keeps its data; new columns are NULL (unknown, not zero).
    run = db.get_latest_run(conn, "bend--or", provider="mapillary")
    assert run.coverage_rate_pct == 42.0
    assert run.status_flat_only is None
    assert run.any_imagery_coverage_rate_pct is None
    assert run.num_flat_images is None

    # Idempotent: reopening must not error or re-migrate.
    conn.close()
    conn2 = db.connect(db_path)
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn2.close()


def test_migrate_v7_to_v8(tmp_path):
    """A v7 catalog gains street_walks.coverage_pct_by_length_any on connect.

    Built from db._SCHEMA minus that one column (so the fixture tracks the
    code), with a pre-v8 walk seeded to prove the existing row survives and
    the new column reads NULL — "not measured", never a copy of the 360°
    number, which would claim the flat footprint had been scored.
    """
    db_path = str(tmp_path / "v7.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    raw.execute("ALTER TABLE street_walks DROP COLUMN coverage_pct_by_length_any")
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute(
        """INSERT INTO street_walks (city_id, provider, run_date, csv_filename,
           coverage_pct_by_length)
           VALUES ('bend--or', 'gsv', '2026-05-01', 'old_streetwalk.csv.gz', 98.4)"""
    )
    raw.execute("PRAGMA user_version = 7")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(street_walks)").fetchall()}
    assert "coverage_pct_by_length_any" in cols

    walk = db.get_latest_street_walk(conn, "bend--or")
    assert walk["coverage_pct_by_length"] == 98.4
    assert walk["coverage_pct_by_length_any"] is None

    # Idempotent: reopening must not error or re-migrate.
    conn.close()
    conn2 = db.connect(db_path)
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn2.close()


def _unique_key_columns(conn, table, must_contain="run_date"):
    """Columns of the UNIQUE index over `table` that includes `must_contain`."""
    for (name,) in conn.execute(
        f"SELECT name FROM pragma_index_list('{table}') WHERE \"unique\" = 1"
    ).fetchall():
        cols = [r[0] for r in conn.execute("SELECT name FROM pragma_index_info(?)", (name,))]
        if must_contain in cols:
            return set(cols)
    return set()


def _build_v8_catalog(db_path):
    """A real v8 catalog on disk: the narrower street_walks key plus one walk.

    Rebuilds street_walks with the v8 (narrower) key rather than stamping the
    current schema with an old version, so the migration under test has
    something to actually migrate.
    """
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    # Rebuild street_walks with the v8 (narrower) key, so the fixture is a real
    # v8 catalog rather than the current schema wearing an old version stamp.
    raw.execute("DROP TABLE street_walks")
    raw.executescript(
        """
        CREATE TABLE street_walks (
            walk_id                INTEGER PRIMARY KEY,
            city_id                TEXT NOT NULL REFERENCES cities(city_id),
            provider               TEXT NOT NULL DEFAULT 'gsv',
            run_date               TEXT NOT NULL,
            csv_filename           TEXT NOT NULL UNIQUE,
            coverage_filename      TEXT,
            network_type           TEXT NOT NULL DEFAULT 'drive',
            spacing_m              REAL,
            match_dist_m           REAL,
            sample_points          INTEGER,
            edges_total            INTEGER,
            edges_fully_covered    INTEGER,
            mean_edge_coverage     REAL,
            coverage_pct_by_length REAL,
            coverage_pct_by_length_any REAL,
            api_requests           INTEGER,
            started_at             TEXT,
            finished_at            TEXT,
            UNIQUE (city_id, provider, run_date)
        );
        """
    )
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute(
        """INSERT INTO street_walks (city_id, provider, run_date, csv_filename,
           coverage_filename, spacing_m, edges_total, coverage_pct_by_length)
           VALUES ('bend--or', 'gsv', '2026-05-01', 'old_streetwalk.csv.gz',
                   'old_streetwalk_coverage.json.gz', 15.0, 41, 95.6)"""
    )
    raw.execute("PRAGMA user_version = 8")
    raw.commit()
    raw.close()


def test_migrate_v8_to_v9(tmp_path):
    """A v8 catalog's street_walks UNIQUE gains network_type on connect.

    v8 keyed a walk on (city_id, provider, run_date), which collapses a city's
    'drive' and 'all_public' walks onto one row — the second silently
    overwrites the first. This is a table rebuild, so the test also proves the
    pre-existing row survives it intact.
    """
    db_path = str(tmp_path / "v8.db")
    _build_v8_catalog(db_path)

    pre = sqlite3.connect(db_path)
    assert _unique_key_columns(pre, "street_walks") == {"city_id", "provider", "run_date"}
    pre.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert _unique_key_columns(conn, "street_walks") == {
        "city_id",
        "provider",
        "network_type",
        "run_date",
    }

    # The rebuild preserved the row, and a pre-v9 walk is a drive walk.
    walk = db.get_latest_street_walk(conn, "bend--or")
    assert walk["csv_filename"] == "old_streetwalk.csv.gz"
    assert walk["coverage_filename"] == "old_streetwalk_coverage.json.gz"
    assert walk["coverage_pct_by_length"] == 95.6
    assert walk["edges_total"] == 41
    assert walk["network_type"] == "drive"

    # Idempotent: reopening must not error or re-run the rebuild.
    conn.close()
    conn3 = db.connect(db_path)
    assert conn3.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn3.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 1
    conn3.close()


def test_migrate_v8_to_v9_is_atomic(tmp_path):
    """An interrupted v8 -> v9 rebuild must leave the v8 catalog untouched.

    The rebuild drops street_walks and renames a scratch table over it. Without
    a transaction those run in autocommit, so dying in between leaves NO
    street_walks at all — and the next connect() would take the "absent table"
    early exit, let _SCHEMA create it empty, and stamp user_version = 9, losing
    every walk row silently. Here the script is run with its COMMIT stripped and
    the connection abandoned, which is exactly what a crash mid-rebuild does.
    """
    db_path = str(tmp_path / "v8.db")
    _build_v8_catalog(db_path)

    crashed = sqlite3.connect(db_path)
    crashed.execute("PRAGMA foreign_keys=OFF")
    crashed.executescript(db._MIGRATE_V8_TO_V9.replace("COMMIT;", ""))
    crashed.close()  # no COMMIT reached → SQLite rolls the whole rebuild back

    after = sqlite3.connect(db_path)
    assert after.execute("PRAGMA user_version").fetchone()[0] == 8
    assert _unique_key_columns(after, "street_walks") == {"city_id", "provider", "run_date"}
    assert after.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 1
    after.close()

    # And the retry — the next ordinary connect() — completes it.
    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_latest_street_walk(conn, "bend--or")["csv_filename"] == "old_streetwalk.csv.gz"
    conn.close()


def test_migrate_v8_to_v9_clears_a_stale_scratch_table(tmp_path):
    """A street_walks_v9 left by a pre-transaction build must not brick connect.

    CREATE TABLE street_walks_v9 has no IF NOT EXISTS, so a leftover scratch
    table from an interrupted older run would fail every subsequent connect —
    an unopenable catalog, not a degraded one.
    """
    db_path = str(tmp_path / "v8.db")
    _build_v8_catalog(db_path)

    stale = sqlite3.connect(db_path)
    stale.execute("CREATE TABLE street_walks_v9 (walk_id INTEGER PRIMARY KEY)")
    stale.commit()
    stale.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_latest_street_walk(conn, "bend--or")["csv_filename"] == "old_streetwalk.csv.gz"
    conn.close()


def test_migrate_v9_to_v10(tmp_path):
    """A v9 catalog gains the driving-plan tables on connect (issue #176).

    A v9 catalog is exactly the current schema minus the two driving_plan
    tables, so the fixture is built from db._SCHEMA with them dropped (keeping
    the fixture in sync with the code) and stamped user_version = 9.
    """
    db_path = str(tmp_path / "v9.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    raw.execute("DROP TABLE driving_plan_entries")
    raw.execute("DROP TABLE driving_plan_snapshots")
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute("PRAGMA user_version = 9")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM driving_plan_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM driving_plan_entries").fetchone()[0] == 0
    # Existing data is untouched by the additive migration.
    assert db.resolve_city(conn, "bend--or").city_id == "bend--or"

    # Idempotent: reopening must not error or re-migrate.
    conn.close()
    conn2 = db.connect(db_path)
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn2.close()


def test_migrate_v10_to_v11(tmp_path):
    """A v10 catalog gains street_walks.coverage_by_highway and the
    street_walk_diffs table on connect (issue #101).

    Built from db._SCHEMA minus that column and table (so the fixture tracks
    the code), with a pre-v11 walk seeded to prove the existing row survives
    and the new column reads NULL — "not captured", awaiting backfill.
    """
    db_path = str(tmp_path / "v10.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    raw.execute("DROP TABLE street_walk_diffs")
    raw.execute("ALTER TABLE street_walks DROP COLUMN coverage_by_highway")
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute(
        """INSERT INTO street_walks (city_id, provider, run_date, csv_filename,
           coverage_pct_by_length)
           VALUES ('bend--or', 'gsv', '2026-05-01', 'old_streetwalk.csv.gz', 98.4)"""
    )
    raw.execute("PRAGMA user_version = 10")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(street_walks)").fetchall()}
    assert "coverage_by_highway" in cols
    assert conn.execute("SELECT COUNT(*) FROM street_walk_diffs").fetchone()[0] == 0

    walk = db.get_latest_street_walk(conn, "bend--or")
    assert walk["coverage_pct_by_length"] == 98.4
    assert walk["coverage_by_highway"] is None

    # Idempotent: reopening must not error or re-migrate.
    conn.close()
    conn2 = db.connect(db_path)
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn2.close()


def test_migrate_v11_to_v12(tmp_path):
    """A v11 catalog gains the street_walks absolute-length columns on connect.

    Built from db._SCHEMA minus those columns (so the fixture tracks the code),
    with a pre-v12 walk seeded to prove the existing row survives and the new
    columns read NULL — "not captured", awaiting
    scripts/backfill_streetwalk_length.py.
    """
    db_path = str(tmp_path / "v11.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    for column in db._V12_STREET_WALK_COLUMNS:
        raw.execute(f"ALTER TABLE street_walks DROP COLUMN {column}")
    raw.execute(
        """INSERT INTO cities (city_id, display_name, city_name, center_lat,
           center_lon, grid_width_m, grid_height_m, step_m, created_at)
           VALUES ('bend--or', 'Bend, OR', 'Bend', 44.05, -121.31,
                   5000, 5000, 20, '2026-01-01T00:00:00+00:00')"""
    )
    raw.execute(
        """INSERT INTO street_walks (city_id, provider, run_date, csv_filename,
           coverage_pct_by_length)
           VALUES ('bend--or', 'gsv', '2026-05-01', 'old_streetwalk.csv.gz', 98.4)"""
    )
    raw.execute("PRAGMA user_version = 11")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(street_walks)").fetchall()}
    assert set(db._V12_STREET_WALK_COLUMNS) <= cols

    walk = db.get_latest_street_walk(conn, "bend--or")
    assert walk["coverage_pct_by_length"] == 98.4  # the pre-v12 row survived
    for column in db._V12_STREET_WALK_COLUMNS:
        assert walk[column] is None

    # Idempotent: reopening must not error or re-migrate.
    conn.close()
    conn2 = db.connect(db_path)
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn2.close()


def test_migrate_v11_to_v12_resumes_after_a_partial_migration(tmp_path):
    """
    A catalog interrupted midway through the v12 ADD COLUMNs must complete on
    the next connect. The columns are added one statement at a time, so a crash
    (or a kill -9 mid-night) can leave some present and some not — a guard that
    checked only the FIRST column would declare the migration done and leave
    the rest permanently missing.
    """
    db_path = str(tmp_path / "partial.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(db._SCHEMA)
    # Drop only the last two: simulates a migration that got halfway.
    for column in db._V12_STREET_WALK_COLUMNS[2:]:
        raw.execute(f"ALTER TABLE street_walks DROP COLUMN {column}")
    raw.execute("PRAGMA user_version = 11")
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(street_walks)").fetchall()}
    assert set(db._V12_STREET_WALK_COLUMNS) <= cols
    conn.close()


def test_register_street_walk_round_trips_the_v12_lengths(tmp_path):
    """The lengths reach the row, and re-collecting the same day replaces them
    rather than keeping the first walk's figures (the upsert covers v12 too)."""
    conn = db.connect(str(tmp_path / "c.db"))
    db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )
    cid = "bend--oregon--united-states"
    for csv_name, length_km, covered in [("a.csv.gz", 100.0, 40.0), ("b.csv.gz", 101.5, 90.0)]:
        db.register_street_walk(
            conn,
            city_id=cid,
            run_date=date(2026, 5, 1),
            csv_filename=csv_name,
            length_km=length_km,
            length_km_covered=covered,
            length_km_covered_any=covered,
            median_covered_age_years=2.5,
        )

    walk = db.get_latest_street_walk(conn, cid)
    assert walk["length_km"] == 101.5
    assert walk["length_km_covered"] == 90.0
    assert walk["median_covered_age_years"] == 2.5
    conn.close()


def test_migrate_v8_catalog_reaches_current_schema_in_one_connect(tmp_path):
    """A v8 catalog must flow through every terminal ladder step in ONE connect.

    Regression test for the ladder's reassignment bug: the v8 -> v9 step
    originally did not set user_version = 9, so any step gated on
    `user_version == 9` (or later) would silently never fire for a catalog
    arriving at v8 — it would get the rebuild, the version stamp, and NONE of
    the newer columns. Asserts the v9 rebuild, the v10 tables, and the v11
    column all land together.
    """
    db_path = str(tmp_path / "v8.db")
    _build_v8_catalog(db_path)

    conn = db.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # v9: the widened UNIQUE key.
    assert _unique_key_columns(conn, "street_walks") == {
        "city_id",
        "provider",
        "network_type",
        "run_date",
    }
    # v10: the driving-plan tables.
    assert conn.execute("SELECT COUNT(*) FROM driving_plan_snapshots").fetchone()[0] == 0
    # v11: the per-highway column and the walk-diff table.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(street_walks)").fetchall()}
    assert "coverage_by_highway" in cols
    assert conn.execute("SELECT COUNT(*) FROM street_walk_diffs").fetchone()[0] == 0
    # v12: the absolute-length columns.
    assert set(db._V12_STREET_WALK_COLUMNS) <= cols
    # And the seeded v8 walk survived the whole ladder.
    walk = db.get_latest_street_walk(conn, "bend--or")
    assert walk["csv_filename"] == "old_streetwalk.csv.gz"
    assert walk["coverage_pct_by_length"] == 95.6
    assert walk["coverage_by_highway"] is None
    conn.close()


def test_two_network_types_coexist_on_one_date(tmp_path):
    """The widened key is what lets both walks of a city survive the same night."""
    conn = db.connect(str(tmp_path / "c.db"))
    db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )
    cid = "bend--oregon--united-states"
    run_date = date(2026, 7, 8)
    for network_type, pct in (("drive", 98.4), ("all_public", 61.2)):
        db.register_street_walk(
            conn,
            city_id=cid,
            run_date=run_date,
            csv_filename=f"walk_{network_type}.csv.gz",
            provider="gsv",
            network_type=network_type,
            coverage_pct_by_length=pct,
        )
    assert conn.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 2
    assert db.get_latest_street_walk(conn, cid)["coverage_pct_by_length"] == 98.4
    assert (
        db.get_latest_street_walk(conn, cid, "gsv", "all_public")["coverage_pct_by_length"] == 61.2
    )

    # Re-collecting ONE network type on the same date replaces only its own row.
    db.register_street_walk(
        conn,
        city_id=cid,
        run_date=run_date,
        csv_filename="walk_drive.csv.gz",
        provider="gsv",
        network_type="drive",
        coverage_pct_by_length=97.0,
    )
    assert conn.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 2
    assert db.get_latest_street_walk(conn, cid)["coverage_pct_by_length"] == 97.0
    assert (
        db.get_latest_street_walk(conn, cid, "gsv", "all_public")["coverage_pct_by_length"] == 61.2
    )

    # And the manifest source must advertise BOTH, not collapse to one.
    latest = db.get_latest_street_walks_all(conn)
    assert sorted(r["network_type"] for r in latest) == ["all_public", "drive"]
    conn.close()


def test_register_street_walk_round_trips_any_imagery_coverage(conn, city):
    """Both street-coverage numbers persist; a GSV walk's are equal by
    construction (GSV emits no flat imagery), a Mapillary walk's can differ."""
    db.register_street_walk(
        conn,
        city_id=city,
        run_date=date(2026, 7, 8),
        csv_filename="w_mly.csv.gz",
        provider="mapillary",
        coverage_pct_by_length=1.9,
        coverage_pct_by_length_any=9.9,
    )
    walk = db.get_latest_street_walk(conn, city, provider="mapillary")
    assert walk["coverage_pct_by_length"] == 1.9
    assert walk["coverage_pct_by_length_any"] == 9.9


def test_register_run_round_trips_flat_only_columns(conn, city):
    rid = db.register_run(
        conn,
        city_id=city,
        provider="mapillary",
        run_date=date(2026, 4, 1),
        csv_filename="a.csv.gz",
        total_points=10,
        status_ok=4,
        status_no_date=1,
        status_zero_results=2,
        status_flat_only=3,
        status_other=0,
        unique_panos=5,
        coverage_rate_pct=50.0,
        any_imagery_coverage_rate_pct=80.0,
        num_flat_images=17,
    )
    run = db.get_latest_run(conn, city, provider="mapillary")
    assert run.run_id == rid
    assert run.status_flat_only == 3
    assert abs(run.any_imagery_coverage_rate_pct - 80.0) < 1e-9
    assert run.num_flat_images == 17


def test_register_run_flat_only_columns_default_null(conn, city):
    # GSV/legacy callers that omit the issue-#116 kwargs store NULL, not 0.
    db.register_run(conn, city_id=city, run_date=date(2026, 4, 1), csv_filename="a.csv.gz")
    run = db.get_latest_run(conn, city)
    assert run.status_flat_only is None
    assert run.any_imagery_coverage_rate_pct is None
    assert run.num_flat_images is None


def test_street_walk_register_and_get(conn, city):
    walk_id = db.register_street_walk(
        conn,
        city_id=city,
        run_date=date(2026, 7, 8),
        csv_filename=f"{city}_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz",
        coverage_filename=f"{city}_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08_coverage.json.gz",
        spacing_m=15.0,
        match_dist_m=25.0,
        sample_points=1000,
        edges_total=200,
        edges_fully_covered=150,
        mean_edge_coverage=0.82,
        coverage_pct_by_length=79.5,
        api_requests=1000,
    )
    row = db.get_latest_street_walk(conn, city)
    assert row["walk_id"] == walk_id
    assert row["provider"] == "gsv"
    assert row["spacing_m"] == 15.0
    assert row["edges_total"] == 200

    # Idempotent on (city, provider, run_date): a re-collect replaces the row.
    walk_id2 = db.register_street_walk(
        conn,
        city_id=city,
        run_date=date(2026, 7, 8),
        csv_filename=f"{city}_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz",
        spacing_m=15.0,
        sample_points=1100,
    )
    assert walk_id2 == walk_id
    assert db.get_latest_street_walk(conn, city)["sample_points"] == 1100


def test_register_street_walk_persists_coverage_by_highway(conn, city):
    """The per-bucket JSON round-trips, and a same-day re-register replaces it
    (ON CONFLICT covers the new column like every other stat)."""
    breakdown = json.dumps({"residential": {"edges": 100, "coverage_pct_by_length": 80.1}})
    db.register_street_walk(
        conn,
        city_id=city,
        run_date=date(2026, 7, 8),
        csv_filename=f"{city}_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz",
        coverage_by_highway=breakdown,
    )
    row = db.get_latest_street_walk(conn, city)
    assert json.loads(row["coverage_by_highway"])["residential"]["edges"] == 100

    replacement = json.dumps({"residential": {"edges": 101, "coverage_pct_by_length": 81.0}})
    db.register_street_walk(
        conn,
        city_id=city,
        run_date=date(2026, 7, 8),
        csv_filename=f"{city}_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz",
        coverage_by_highway=replacement,
    )
    assert json.loads(db.get_latest_street_walk(conn, city)["coverage_by_highway"]) == {
        "residential": {"edges": 101, "coverage_pct_by_length": 81.0}
    }


def test_get_previous_street_walk_filters_series_and_date(conn, city):
    """The predecessor lookup is per (city, provider, network_type) and
    strictly before the date — never the walk itself, never another series."""

    def _walk(run_date, provider="gsv", network_type="drive"):
        from streetscape_metadata_tracker.naming import generate_streetwalk_filename

        stem = generate_streetwalk_filename(
            city, 5000, 5000, 20, 15, run_date, provider=provider, network_type=network_type
        )
        return db.register_street_walk(
            conn,
            city_id=city,
            run_date=run_date,
            csv_filename=stem + ".csv.gz",
            provider=provider,
            network_type=network_type,
        )

    first_id = _walk(date(2026, 4, 1))
    _walk(date(2026, 5, 1), provider="mapillary")
    _walk(date(2026, 6, 1), network_type="all_public")
    _walk(date(2026, 7, 1))

    prev = db.get_previous_street_walk(conn, city, date(2026, 7, 1))
    assert prev["walk_id"] == first_id  # skips the other series entirely
    assert prev["run_date"] == "2026-04-01"

    # Strictly before: the first walk of a series has no predecessor.
    assert db.get_previous_street_walk(conn, city, date(2026, 4, 1)) is None
    assert db.get_previous_street_walk(conn, city, date(2026, 5, 1), provider="mapillary") is None


def test_record_and_get_walk_diff(conn, city):
    walk_a = db.register_street_walk(
        conn,
        city_id=city,
        run_date=date(2026, 4, 1),
        csv_filename="a_streetwalk.csv.gz",
    )
    walk_b = db.register_street_walk(
        conn,
        city_id=city,
        run_date=date(2026, 7, 1),
        csv_filename="b_streetwalk.csv.gz",
    )
    db.record_street_walk_diff(
        conn,
        city_id=city,
        from_walk_id=walk_a,
        to_walk_id=walk_b,
        edges_aligned=200,
        edges_added=0,
        edges_removed=0,
        edges_gained_coverage=12,
        edges_lost_coverage=3,
        coverage_fraction_changed=40,
        nearest_pano_date_changed=25,
        edges_fully_covered_delta=9,
        coverage_pct_by_length_delta=4.2,
        coverage_pct_by_length_any_delta=None,
        detail_filename="diff.csv.gz",
    )
    row = db.get_walk_diff_for_walk(conn, walk_b)
    assert row["edges_gained_coverage"] == 12
    assert row["coverage_pct_by_length_delta"] == 4.2
    assert row["coverage_pct_by_length_any_delta"] is None
    assert row["from_run_date"] == "2026-04-01"
    assert row["computed_at"]

    # Idempotent on the pair: a re-diff replaces, never duplicates.
    db.record_street_walk_diff(
        conn,
        city_id=city,
        from_walk_id=walk_a,
        to_walk_id=walk_b,
        edges_aligned=200,
        edges_added=0,
        edges_removed=0,
        edges_gained_coverage=13,
        edges_lost_coverage=3,
        coverage_fraction_changed=41,
        nearest_pano_date_changed=25,
        edges_fully_covered_delta=9,
        coverage_pct_by_length_delta=4.3,
        coverage_pct_by_length_any_delta=None,
        detail_filename="diff.csv.gz",
    )
    assert conn.execute("SELECT COUNT(*) FROM street_walk_diffs").fetchone()[0] == 1
    assert db.get_walk_diff_for_walk(conn, walk_b)["edges_gained_coverage"] == 13

    # No diff recorded for the 'from' walk.
    assert db.get_walk_diff_for_walk(conn, walk_a) is None

    # Deleting by 'to' walk drops the row; a second delete is a no-op.
    db.delete_walk_diff_for_walk(conn, walk_b)
    assert db.get_walk_diff_for_walk(conn, walk_b) is None
    db.delete_walk_diff_for_walk(conn, walk_b)
    assert conn.execute("SELECT COUNT(*) FROM street_walk_diffs").fetchone()[0] == 0


def test_street_network_register_and_get(conn, city):
    network_id = db.register_street_network(
        conn,
        city_id=city,
        graphml_filename=f"{city}_streets_network.graphml",
        node_count=1200,
        edge_count=3400,
        osmnx_version="2.1.0",
    )
    row = db.get_street_network(conn, city)
    assert row["network_id"] == network_id
    assert row["network_type"] == "drive"
    assert row["graphml_filename"] == f"{city}_streets_network.graphml"
    assert (row["node_count"], row["edge_count"]) == (1200, 3400)
    assert row["osmnx_version"] == "2.1.0"
    assert row["fetched_at"]  # stamped by register

    assert db.get_street_network(conn, city, network_type="walk") is None
    assert db.get_street_network(conn, "nowhere--xx") is None


def test_street_network_refresh_replaces_row(conn, city):
    first_id = db.register_street_network(
        conn, city_id=city, graphml_filename=f"{city}_streets_network.graphml", node_count=10
    )
    first = db.get_street_network(conn, city)
    # A --refresh re-fetch upserts: same (city, network_type) row, new stats.
    second_id = db.register_street_network(
        conn, city_id=city, graphml_filename=f"{city}_streets_network.graphml", node_count=11
    )
    second = db.get_street_network(conn, city)
    assert second_id == first_id
    assert second["node_count"] == 11
    assert second["fetched_at"] >= first["fetched_at"]
    assert conn.execute("SELECT COUNT(*) FROM street_networks").fetchone()[0] == 1


def test_street_network_types_coexist(conn, city):
    db.register_street_network(
        conn, city_id=city, graphml_filename=f"{city}_streets_network.graphml"
    )
    db.register_street_network(
        conn,
        city_id=city,
        graphml_filename=f"{city}_streets_network_walk.graphml",
        network_type="walk",
    )
    assert conn.execute("SELECT COUNT(*) FROM street_networks").fetchone()[0] == 2
    assert db.get_street_network(conn, city, network_type="walk")["network_type"] == "walk"


def test_street_network_unknown_city_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.register_street_network(conn, city_id="nowhere--xx", graphml_filename="nowhere.graphml")
