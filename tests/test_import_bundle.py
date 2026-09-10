"""
`scheduler import-bundle` — landing a laptop investigation in this catalog (issue #330).

What these pin, and why each one exists, is in docs/testing.md. The short version:
the importer writes into the production catalog and the published data directory
from a file its operator copied in, so every check that stands between those two
facts gets a test, and the refusals are pinned INDIVIDUALLY — a bundle that is
refused for the wrong reason is a bundle whose real problem went unlooked-at.
"""

import gzip
import json
import os
import sqlite3
from datetime import date

import pytest

from streetscape_metadata_tracker import analysis, bundle_import, db, fileutils
from streetscape_metadata_tracker.naming import (
    generate_run_filename,
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_metadata_tracker.scheduler import (
    ProviderConfig,
    SchedulerConfig,
    cmd_import_bundle,
)
from tests.conftest import make_mapillary_city_df, write_city_csv_gz

RUN_DATE = date(2026, 9, 10)
CITY = dict(
    city_name="San Luis Obispo",
    state_name="California",
    state_code="CA",
    country_name="United States",
    country_code="us",
    center_lat=35.2766,
    center_lon=-120.6704,
    grid_width_m=10400,
    grid_height_m=10400,
    step_m=20,
)
CITY_ID = "san-luis-obispo--california--united-states"
SAMPLE_POINTS = 12


def _walk_names(provider="gsv", network_type="drive", spacing_m=15):
    stem = generate_streetwalk_filename(
        CITY_ID,
        CITY["grid_width_m"],
        CITY["grid_height_m"],
        CITY["step_m"],
        spacing_m,
        RUN_DATE,
        provider=provider,
        network_type=network_type,
    )
    csv_name = stem + ".csv.gz"
    return csv_name, streetwalk_coverage_filename(csv_name)


def _run_names(provider="mapillary"):
    stem = generate_run_filename(
        CITY_ID,
        CITY["grid_width_m"],
        CITY["grid_height_m"],
        CITY["step_m"],
        RUN_DATE,
        provider=provider,
    )
    return stem + ".csv.gz", stem + ".json.gz"


def _write_walk_artifacts(root, csv_name, coverage_name, *, sample_points=SAMPLE_POINTS):
    """A walk CSV with one row per sample point, and the coverage GeoJSON beside it."""
    with gzip.open(os.path.join(root, csv_name), "wt", encoding="utf-8") as fh:
        fh.write("query_lat,query_lon,status\n")
        for i in range(sample_points):
            fh.write(f"35.{i:04d},-120.6,OK\n")
    geojson = {
        "type": "FeatureCollection",
        "features": [],
        "properties": {
            "metadata": {
                "schema_version": 1,
                "kind": "streetwalk_coverage",
                "spacing_m": 15,
                "match_dist_m": 25.0,
                "totals": {"edges": 40, "coverage_pct_by_length": 92.8, "length_km": 375.1},
            }
        },
    }
    with gzip.open(os.path.join(root, coverage_name), "wt", encoding="utf-8") as fh:
        json.dump(geojson, fh)


@pytest.fixture
def bundle_dir(tmp_path):
    """
    A complete laptop bundle: one city, one Mapillary grid run, one GSV walk,
    the frozen network, and the spend ledger the laptop wrote.

    Deliberately built with the real ``db`` writers rather than hand-rolled SQL,
    so the fixture cannot drift from what a laptop run actually produces.
    """
    root = tmp_path / "bundle"
    (root / bundle_import.OSM_CACHE_DIRNAME).mkdir(parents=True)
    conn = db.connect(str(root / bundle_import.CATALOG_NAME))
    db.register_city(conn, **CITY)

    run_csv, run_json = _run_names()
    df = make_mapillary_city_df([("p1", "2024-05-01"), ("p2", "2023-07-01")], run_date=RUN_DATE)
    write_city_csv_gz(df, str(root / run_csv))
    (root / run_json).write_bytes(gzip.compress(b'{"metadata": {"schema_version": 2}}'))
    stats = analysis.calculate_run_stats(
        fileutils.load_city_csv_file(str(root / run_csv)), RUN_DATE, provider="mapillary"
    )
    db.register_run(
        conn,
        city_id=CITY_ID,
        run_date=RUN_DATE,
        csv_filename=run_csv,
        provider="mapillary",
        json_filename=run_json,
        api_requests=36,
        num_flat_images=0,
        census_fetched_by="mapillary",
        **stats,
    )

    walk_csv, walk_cov = _walk_names()
    _write_walk_artifacts(root, walk_csv, walk_cov)
    db.register_street_walk(
        conn,
        city_id=CITY_ID,
        run_date=RUN_DATE,
        csv_filename=walk_csv,
        provider="gsv",
        coverage_filename=walk_cov,
        network_type="drive",
        spacing_m=15,
        match_dist_m=25.0,
        sample_points=SAMPLE_POINTS,
        edges_total=40,
        coverage_pct_by_length=92.8,
        length_km=375.1,
        api_requests=SAMPLE_POINTS,
    )

    graphml = f"{CITY_ID}_streets_network.graphml"
    (root / bundle_import.OSM_CACHE_DIRNAME / graphml).write_text("<graphml/>")
    db.register_street_network(
        conn, city_id=CITY_ID, graphml_filename=graphml, network_type="drive", edge_count=40
    )

    db.add_api_usage(conn, RUN_DATE, SAMPLE_POINTS, provider="gsv_streets")
    db.add_api_usage(conn, RUN_DATE, 36, provider="mapillary_streets")
    conn.commit()
    conn.close()
    return root


@pytest.fixture
def cfg(tmp_path):
    """A scheduler config whose catalog and data dir are this host's, and publishing off."""
    data_dir = tmp_path / "prod"
    data_dir.mkdir()
    return SchedulerConfig(
        data_dir=str(data_dir),
        db_path=str(data_dir / "streetscape_tracker.db"),
        log_dir=str(tmp_path / "logs"),
        publish_enabled=False,
        providers={
            "gsv": ProviderConfig(),
            "gsv_streets": ProviderConfig(),
            "mapillary": ProviderConfig(),
            "mapillary_streets": ProviderConfig(),
        },
    )


@pytest.fixture(autouse=True)
def _no_batch_in_flight(monkeypatch):
    """
    Neutralize the `ps` scan suite-wide.

    Without this the suite's own pytest command line, or a real nightly batch on
    the machine running the tests, decides whether these tests pass.
    """
    monkeypatch.setattr("streetscape_metadata_tracker.scheduler._run_due_in_flight", lambda: None)


def _prod(cfg):
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── the happy path ────────────────────────────────────────────────────────


def test_dry_run_writes_nothing_to_either_the_catalog_or_the_data_dir(cfg, bundle_dir):
    assert cmd_import_bundle(cfg, str(bundle_dir)) == 0
    # Both halves matter: an importer that copied files and then declined to
    # catalog them would leave artifacts the aggregate never sees. The catalog
    # FILE is allowed to appear — db.connect creates the schema in order to ask
    # it anything — so the assertion is on rows, and on the data dir holding no
    # artifact.
    assert _prod(cfg).execute("SELECT COUNT(*) FROM cities").fetchone()[0] == 0
    assert [f for f in os.listdir(cfg.data_dir) if not f.startswith("streetscape_tracker.db")] == []


def test_execute_lands_rows_files_and_the_network(cfg, bundle_dir):
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, enable=True) == 0
    conn = _prod(cfg)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM street_networks").fetchone()[0] == 1
    run_csv, _ = _run_names()
    walk_csv, walk_cov = _walk_names()
    for name in (run_csv, walk_csv, walk_cov):
        assert os.path.exists(os.path.join(cfg.data_dir, name)), name
    assert os.path.exists(
        os.path.join(
            cfg.data_dir, bundle_import.OSM_CACHE_DIRNAME, f"{CITY_ID}_streets_network.graphml"
        )
    )


def test_city_is_registered_disabled_unless_enable_is_passed(cfg, bundle_dir):
    # The whole point of the default: a city arriving through an investigation
    # has a boundary nobody vetted, and a fresh city sorts to the head of the
    # next night's stalest-first queue.
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    assert _prod(cfg).execute("SELECT enabled FROM cities").fetchone()[0] == 0


def test_enable_turns_on_a_city_that_is_already_registered_and_off(cfg, bundle_dir):
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    db.set_city_enabled(db.connect(cfg.db_path), CITY_ID, True)
    assert _prod(cfg).execute("SELECT enabled FROM cities").fetchone()[0] == 1


# ── what the import must NOT do ───────────────────────────────────────────


def test_the_bundles_one_city_aggregate_is_never_copied_over_this_hosts(cfg, bundle_dir):
    """
    The footgun this importer exists next to.

    A bundle's data/ also holds cities.json.gz, streetwalks.json.gz and
    driving_plan.json.gz that the laptop generated over a ONE-CITY catalog. A
    directory sync would replace this host's published index with them — a
    site-wide outage produced by a successful import — so the copy step is
    file-by-file over the names catalog rows carry, and nothing else.
    """
    (bundle_dir / "cities.json.gz").write_bytes(gzip.compress(b'{"cities": ["only-me"]}'))
    bundle = bundle_import.read_bundle(bundle_dir)
    assert "cities.json.gz" not in bundle_import._artifact_sources(bundle)

    conn = db.connect(cfg.db_path)
    existing, problems = bundle_import.check_bundle(bundle, conn, cfg.data_dir)
    assert problems == []
    result = bundle_import.apply_bundle(
        bundle, conn, data_dir=cfg.data_dir, existing=existing, enable=False
    )
    assert not os.path.exists(os.path.join(cfg.data_dir, "cities.json.gz"))
    assert "cities.json.gz" not in result.files


def test_spend_is_ledgered_only_for_credentials_this_host_shares(cfg, bundle_dir):
    """
    The ABSENCE is the assertion.

    Mapillary meters by IP, so a laptop's tile spend was drawn against a
    different allowance; charging it here would tighten a budget gate against
    requests this host never made — on the very channel whose budget is the
    instrument in an open block investigation.
    """
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    charged = dict(_prod(cfg).execute("SELECT provider, requests FROM api_usage").fetchall())
    assert charged == {"gsv_streets": SAMPLE_POINTS}
    assert "mapillary_streets" not in charged


def test_no_cadence_row_for_a_channel_the_scheduler_cannot_run(cfg, bundle_dir):
    """
    A success on an unwired channel would suppress its FIRST real collection.

    The bundle's Mapillary GRID run has a channel, so it gets a row. Panoramax
    is in UNWIRED_CHANNELS, so it must not — even though it is a perfectly good
    provider token with a perfectly good run.
    """
    conn = db.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE runs SET provider = 'panoramax'")
    run_csv, run_json = _run_names(provider="panoramax")
    old_csv, old_json = _run_names(provider="mapillary")
    conn.execute("UPDATE runs SET csv_filename = ?, json_filename = ?", (run_csv, run_json))
    conn.commit()
    conn.close()
    os.rename(bundle_dir / old_csv, bundle_dir / run_csv)
    os.rename(bundle_dir / old_json, bundle_dir / run_json)

    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, enable=True) == 0
    channels = {
        r[0]
        for r in _prod(cfg).execute(
            "SELECT provider FROM schedule_state WHERE last_success_at IS NOT NULL"
        )
    }
    assert "panoramax" not in channels
    assert "gsv_streets" in channels  # the walk still recorded one


# ── stats ─────────────────────────────────────────────────────────────────


def test_grid_stats_are_recomputed_from_the_csv_not_carried(cfg, bundle_dir):
    """
    Pinned by MUTATING the bundle's stored value, not by reading a default.

    A test that only checked the imported number equalled the recomputed one
    would stay green if the importer carried the bundle's row instead — the two
    agree whenever both checkouts are in step, which is exactly when the bug is
    invisible.
    """
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE runs SET coverage_rate_pct = 99.0, total_points = 1")
    conn.commit()
    conn.close()

    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    row = _prod(cfg).execute("SELECT coverage_rate_pct, total_points FROM runs").fetchone()
    assert row["coverage_rate_pct"] != 99.0
    assert row["total_points"] != 1


def test_walk_row_disagreeing_with_its_coverage_artifact_is_refused(cfg, bundle_dir):
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE street_walks SET edges_total = 4000")
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 0


def test_a_truncated_walk_csv_is_refused(cfg, bundle_dir):
    """An rsync cut short leaves a readable gzip whose row count is simply wrong."""
    walk_csv, walk_cov = _walk_names()
    _write_walk_artifacts(bundle_dir, walk_csv, walk_cov, sample_points=SAMPLE_POINTS - 3)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


# ── the refusals, one at a time ───────────────────────────────────────────


def test_geometry_mismatch_is_refused(cfg, bundle_dir):
    """
    The load-bearing check, and the one nothing in this codebase had.

    Every same_grid_geometry call compares filename to filename; none compares a
    filename to the cities row. A bundle collected on a different rectangle is
    not a later snapshot of the same series.
    """
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **{**CITY, "grid_width_m": 9000})
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_filename_the_generators_would_not_produce_is_refused(cfg, bundle_dir):
    run_csv, _ = _run_names()
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    # Drop the provider token — the exact shape that silently collides two
    # providers sharing a run date.
    conn.execute("UPDATE runs SET csv_filename = ?", (run_csv.replace("_mapillary_", "_"),))
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_an_existing_run_for_the_same_city_provider_and_date_is_refused(cfg, bundle_dir):
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    run_csv, _ = _run_names()
    db.register_run(
        conn, city_id=CITY_ID, run_date=RUN_DATE, csv_filename=run_csv, provider="mapillary"
    )
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_colliding_csv_filename_under_a_DIFFERENT_key_is_refused(cfg, bundle_dir):
    """
    The composite key does not catch this one.

    `runs.csv_filename` carries its own UNIQUE, independent of
    (city_id, provider, run_date) — so a row filed under another city's id can
    hold the name this bundle is about to write, and register_run would raise
    IntegrityError mid-import rather than refuse cleanly.
    """
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    db.register_city(conn, **{**CITY, "city_name": "Elsewhere"})
    run_csv, _ = _run_names()
    db.register_run(
        conn,
        city_id=db.derive_city_id("Elsewhere", "California", "United States"),
        run_date=RUN_DATE,
        csv_filename=run_csv,
        provider="mapillary",
    )
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_row_naming_an_artifact_the_bundle_lacks_is_refused(cfg, bundle_dir):
    run_csv, _ = _run_names()
    os.remove(bundle_dir / run_csv)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_bundle_on_another_schema_version_is_refused(cfg, bundle_dir):
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_live_wal_beside_the_bundle_catalog_is_refused(cfg, bundle_dir):
    """
    A read-only connection cannot replay a WAL, so reading anyway would import a
    bundle quietly missing its most recent rows.
    """
    (bundle_dir / f"{bundle_import.CATALOG_NAME}-wal").write_bytes(b"not empty")
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_directory_with_no_catalog_is_refused(cfg, tmp_path):
    assert cmd_import_bundle(cfg, str(tmp_path / "nothing-here"), execute=True) == 64


def test_an_import_is_refused_while_a_batch_looks_like_it_is_running(cfg, bundle_dir, monkeypatch):
    monkeypatch.setattr(
        "streetscape_metadata_tracker.scheduler._run_due_in_flight",
        lambda: "pid 1234: python -m streetscape_metadata_tracker.scheduler run-due",
    )
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    # ...and --force is the documented way past a false positive.
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, force=True) == 0


def test_re_running_a_landed_import_refuses_rather_than_double_charging(cfg, bundle_dir):
    """
    add_api_usage is ADDITIVE, so this refusal is what makes a retry safe: an
    operator who re-runs after a partial-looking failure must not pay twice.
    """
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    before = _prod(cfg).execute("SELECT requests FROM api_usage").fetchone()[0]
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT requests FROM api_usage").fetchone()[0] == before
