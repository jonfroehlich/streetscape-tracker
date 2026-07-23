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
