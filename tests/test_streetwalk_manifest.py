"""
Tests for the streetwalk sidecar manifest (issue #155).

`generate_streetwalk_manifest` publishes `streetwalks.json.gz` — a small index
of the latest road-walk coverage artifact per (city, provider), read by the
city page to locate an artifact it cannot derive from the grid run filename.
"""

import gzip
import json
import os
from datetime import date

import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.json_summarizer import generate_streetwalk_manifest


def _register_city(conn, name, state, code):
    return db.register_city(
        conn,
        city_name=name,
        state_name=state,
        state_code=code,
        country_name="United States",
        country_code="US",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=800,
        grid_height_m=800,
        step_m=20,
    )


def _read_manifest(data_dir):
    with gzip.open(os.path.join(data_dir, "streetwalks.json.gz")) as f:
        return json.load(f)


def test_manifest_lists_one_walk_per_city_provider(conn, data_dir):
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 17),
        csv_filename="adrian_streetwalk_sp15_2026-07-17.csv.gz",
        coverage_filename="adrian_streetwalk_sp15_2026-07-17_coverage.json.gz",
        spacing_m=15.0,
        match_dist_m=25.0,
        edges_total=41,
        edges_fully_covered=36,
        mean_edge_coverage=0.9362,
        coverage_pct_by_length=95.6,
    )

    manifest = generate_streetwalk_manifest(conn, data_dir)

    assert manifest["schema_version"] == 1
    assert "generated_at" in manifest
    assert manifest == _read_manifest(data_dir)  # file matches return value

    assert len(manifest["walks"]) == 1
    w = manifest["walks"][0]
    assert w["city_id"] == city_id
    assert w["provider"] == "gsv"
    assert w["coverage_filename"] == "adrian_streetwalk_sp15_2026-07-17_coverage.json.gz"
    assert w["run_date"] == "2026-07-17"
    assert w["coverage_pct_by_length"] == 95.6
    # uncovered is derived (no such column on street_walks).
    assert w["uncovered_pct_by_length"] == pytest.approx(4.4)
    assert w["edges"] == 41
    assert w["edges_fully_covered"] == 36


def test_manifest_keeps_only_the_latest_run_per_city(conn, data_dir):
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    for d, pct in [(date(2026, 6, 1), 90.0), (date(2026, 7, 17), 95.6)]:
        db.register_street_walk(
            conn,
            city_id=city_id,
            run_date=d,
            csv_filename=f"adrian_streetwalk_sp15_{d.isoformat()}.csv.gz",
            coverage_filename=f"adrian_streetwalk_sp15_{d.isoformat()}_coverage.json.gz",
            spacing_m=15.0,
            match_dist_m=25.0,
            coverage_pct_by_length=pct,
        )

    walks = generate_streetwalk_manifest(conn, data_dir)["walks"]

    assert len(walks) == 1
    assert walks[0]["run_date"] == "2026-07-17"
    assert walks[0]["coverage_pct_by_length"] == 95.6


def test_manifest_is_written_even_when_empty(conn, data_dir):
    manifest = generate_streetwalk_manifest(conn, data_dir)
    assert manifest["walks"] == []
    # The file must still exist so the frontend fetch 200s (empty → no overlays).
    assert _read_manifest(data_dir)["walks"] == []


def test_manifest_keeps_both_providers_of_the_same_city(conn, data_dir):
    """
    The frontend looks up (city_id, provider), so a city walked under two
    providers must yield two entries — the "latest" collapse groups by BOTH
    keys. Regression guard for a GROUP BY that drops the provider dimension:
    with only-city grouping, the mapillary walk (older date) would vanish.
    """
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    for provider, d, pct in [
        ("gsv", date(2026, 7, 17), 95.6),
        ("mapillary", date(2026, 5, 2), 41.2),
    ]:
        db.register_street_walk(
            conn,
            city_id=city_id,
            provider=provider,
            run_date=d,
            csv_filename=f"adrian_{provider}_streetwalk_sp15_{d.isoformat()}.csv.gz",
            coverage_filename=f"adrian_{provider}_sp15_{d.isoformat()}_coverage.json.gz",
            coverage_pct_by_length=pct,
        )

    walks = generate_streetwalk_manifest(conn, data_dir)["walks"]

    by_provider = {w["provider"]: w for w in walks}
    assert set(by_provider) == {"gsv", "mapillary"}
    assert by_provider["gsv"]["coverage_pct_by_length"] == 95.6
    assert by_provider["mapillary"]["coverage_pct_by_length"] == 41.2
    # ...and each provider's own latest, independently of the other's dates.
    assert by_provider["mapillary"]["run_date"] == "2026-05-02"


def test_manifest_latest_is_per_provider_not_per_city(conn, data_dir):
    """Two dates per provider: each provider collapses to its own newest."""
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    for provider, d in [
        ("gsv", date(2026, 3, 1)),
        ("gsv", date(2026, 7, 17)),
        ("mapillary", date(2026, 4, 9)),
        ("mapillary", date(2026, 6, 30)),
    ]:
        db.register_street_walk(
            conn,
            city_id=city_id,
            provider=provider,
            run_date=d,
            csv_filename=f"adrian_{provider}_{d.isoformat()}.csv.gz",
            coverage_filename=f"adrian_{provider}_{d.isoformat()}_coverage.json.gz",
        )

    walks = generate_streetwalk_manifest(conn, data_dir)["walks"]

    assert {(w["provider"], w["run_date"]) for w in walks} == {
        ("gsv", "2026-07-17"),
        ("mapillary", "2026-06-30"),
    }
    # A walk with no explicit network type is a drive walk.
    assert {w["network_type"] for w in walks} == {"drive"}


def test_manifest_latest_is_per_network_type_too(conn, data_dir):
    """A city's drive and all_public walks are separate series, so the manifest
    must advertise both — collapsing them would make the city page render
    whichever the JOIN happened to pick, silently switching denominators."""
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    for network_type, d, pct in [
        ("drive", date(2026, 3, 1), 90.0),
        ("drive", date(2026, 7, 17), 95.6),
        ("all_public", date(2026, 7, 17), 61.2),
    ]:
        db.register_street_walk(
            conn,
            city_id=city_id,
            run_date=d,
            network_type=network_type,
            csv_filename=f"adrian_{network_type}_{d.isoformat()}.csv.gz",
            coverage_filename=f"adrian_{network_type}_{d.isoformat()}_coverage.json.gz",
            coverage_pct_by_length=pct,
        )

    walks = generate_streetwalk_manifest(conn, data_dir)["walks"]

    assert {(w["network_type"], w["run_date"], w["coverage_pct_by_length"]) for w in walks} == {
        ("drive", "2026-07-17", 95.6),
        ("all_public", "2026-07-17", 61.2),
    }
    # Each entry points at its own artifact, never a shared one.
    assert len({w["coverage_filename"] for w in walks}) == 2


def test_manifest_lists_every_city_ordered_by_city_then_provider(conn, data_dir):
    """Multiple cities all appear; the order is the query's stable city→provider."""
    for name, code in [("Zanesville", "OH"), ("Adrian", "OR")]:
        city_id = _register_city(conn, name, "State", code)
        for provider in ("mapillary", "gsv"):  # inserted out of order on purpose
            db.register_street_walk(
                conn,
                city_id=city_id,
                provider=provider,
                run_date=date(2026, 7, 17),
                csv_filename=f"{city_id}_{provider}.csv.gz",
                coverage_filename=f"{city_id}_{provider}_coverage.json.gz",
            )

    walks = generate_streetwalk_manifest(conn, data_dir)["walks"]

    assert len(walks) == 4
    assert [(w["city_id"], w["provider"]) for w in walks] == sorted(
        (w["city_id"], w["provider"]) for w in walks
    )


def test_manifest_tolerates_missing_stats(conn, data_dir):
    """
    Every stat column on street_walks is nullable (a row can be cataloged before
    coverage is computed). The manifest must still emit the entry — the
    frontend only strictly needs `coverage_filename` — and must NOT turn a null
    coverage into a bogus 100% uncovered via `100 - None`.
    """
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 17),
        csv_filename="adrian_streetwalk_sp15_2026-07-17.csv.gz",
        coverage_filename="adrian_streetwalk_sp15_2026-07-17_coverage.json.gz",
    )

    w = generate_streetwalk_manifest(conn, data_dir)["walks"][0]

    assert w["coverage_filename"] == "adrian_streetwalk_sp15_2026-07-17_coverage.json.gz"
    assert w["coverage_pct_by_length"] is None
    assert w["uncovered_pct_by_length"] is None
    assert w["edges"] is None and w["mean_edge_coverage"] is None


def test_manifest_survives_a_json_roundtrip_of_every_field(conn, data_dir):
    """
    The manifest is consumed as JSON by the browser, so every value must be
    JSON-native (no sqlite Row objects, no numpy scalars leaking from the
    catalog). Reading the written file back is the real assertion.
    """
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 17),
        csv_filename="adrian_streetwalk_sp15_2026-07-17.csv.gz",
        coverage_filename="adrian_streetwalk_sp15_2026-07-17_coverage.json.gz",
        spacing_m=15.0,
        match_dist_m=25.0,
        sample_points=612,
        edges_total=41,
        edges_fully_covered=36,
        mean_edge_coverage=0.9362,
        coverage_pct_by_length=95.6,
    )

    generate_streetwalk_manifest(conn, data_dir)
    w = _read_manifest(data_dir)["walks"][0]

    assert w["spacing_m"] == 15.0
    assert w["match_dist_m"] == 25.0
    assert w["mean_edge_coverage"] == pytest.approx(0.9362)
    assert w["uncovered_pct_by_length"] == pytest.approx(4.4)
    assert all(not isinstance(v, bytes) for v in w.values())


def test_manifest_reflects_a_recollected_same_day_walk(conn, data_dir):
    """
    Re-collecting a city on the same date upserts the catalog row (a walk is a
    full re-census, not an append). The manifest must advertise the NEW
    filename — a stale one would 404 in the browser.
    """
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    for spacing, pct in [(15.0, 95.6), (10.0, 96.4)]:
        db.register_street_walk(
            conn,
            city_id=city_id,
            run_date=date(2026, 7, 17),
            csv_filename=f"adrian_streetwalk_sp{int(spacing)}_2026-07-17.csv.gz",
            coverage_filename=f"adrian_sp{int(spacing)}_2026-07-17_coverage.json.gz",
            spacing_m=spacing,
            coverage_pct_by_length=pct,
        )

    walks = generate_streetwalk_manifest(conn, data_dir)["walks"]

    assert len(walks) == 1
    assert walks[0]["coverage_filename"] == "adrian_sp10_2026-07-17_coverage.json.gz"
    assert walks[0]["spacing_m"] == 10.0


def test_manifest_overwrites_a_previous_manifest(conn, data_dir):
    """
    The manifest is regenerated in place after every run; a shrinking catalog
    (or a re-registered walk) must not leave stale entries behind from the
    previous write.
    """
    city_id = _register_city(conn, "Adrian", "Oregon", "OR")
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 17),
        csv_filename="adrian_streetwalk_sp15_2026-07-17.csv.gz",
        coverage_filename="stale_coverage.json.gz",
    )
    generate_streetwalk_manifest(conn, data_dir)
    assert _read_manifest(data_dir)["walks"][0]["coverage_filename"] == ("stale_coverage.json.gz")

    conn.execute("DELETE FROM street_walks")
    generate_streetwalk_manifest(conn, data_dir)

    assert _read_manifest(data_dir)["walks"] == []
