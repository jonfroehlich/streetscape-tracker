"""The published Mapillary quality_score distribution (issue #321).

`compute_mapillary_meta` gains a `quality` block (percentiles, tail shares, a
sequence-weighted cut and the on-foot split), and `_build_provider_summary`
carries the whole `mapillary_meta` into the aggregate. These tests pin the
definitions against the study that measured them, the absent-never-zero rule,
and the carry, plus the backfill handle in `scripts/recompute_run_stats.py`.
"""

import gzip
import json
import os
import re
import subprocess
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

from scripts import mapillary_image_quality_collect as mqc
from scripts import recompute_run_stats as rrs
from streetscape_metadata_tracker import db, mapillary_quality, naming, scheduler
from streetscape_metadata_tracker.config import MAPILLARY_METADATA_DTYPES, METADATA_DTYPES
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.json_summarizer import (
    _build_provider_summary,
    _foot_codes,
    compute_mapillary_meta,
    generate_city_metadata_summary_as_json,
)
from tests.conftest import COLUMNS, MAPILLARY_COLUMNS, make_mapillary_city_df, write_city_csv_gz

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

QUALITY_KEYS = {
    "n_scored",
    "p10",
    "p25",
    "p50",
    "p75",
    "p90",
    "pct_ge_good",
    "pct_lt_poor",
    "n_sequences",
    "seq_p25",
    "seq_p50",
    "seq_p75",
    "p50_on_foot",
    "p50_vehicle",
    "n_on_foot",
    "n_foot_known",
}


def _census(rows):
    """A typed Mapillary census frame from (status, quality, on_foot, seq) tuples."""
    out = []
    for i, (status, quality, foot, seq) in enumerate(rows):
        d = dict.fromkeys(MAPILLARY_COLUMNS)
        d.update(
            query_lat=47.6 + i * 1e-4,
            query_lon=-122.3,
            query_timestamp="2026-09-01T00:00:00+00:00",
            pano_id=f"p{i}",
            status=status,
            organization_id=None,
            on_foot=foot,
            quality_score=quality,
            sequence_id=seq,
            is_pano=status in ("OK", "NO_DATE"),
        )
        out.append(d)
    return pd.DataFrame(out, columns=MAPILLARY_COLUMNS).astype(MAPILLARY_METADATA_DTYPES)


# ── the thresholds live once ──────────────────────────────────────────────────


def test_the_study_script_reads_the_packages_thresholds():
    """The study and the published shares must cut at the same place. Identity,
    not equality: a literal 0.90 re-declared in the script would pass an
    equality check today and drift silently the day either one moves."""
    assert mqc.GOOD_THRESHOLD is mapillary_quality.GOOD_THRESHOLD
    assert mqc.POOR_THRESHOLD is mapillary_quality.POOR_THRESHOLD
    assert (mapillary_quality.GOOD_THRESHOLD, mapillary_quality.POOR_THRESHOLD) == (0.90, 0.60)


def test_the_grid_page_labels_the_cuts_the_shares_were_taken_at():
    """grid.js prints "≥ 0.9" and "< 0.6" in its headers. The numbers under
    them were cut here, so the two constants are read out of the JS source and
    compared -- across the language boundary, the way the run-filename regex
    is (test_the_js_run_filename_regex_agrees_with_python)."""
    with open(os.path.join(_PROJECT_ROOT, "www", "js", "grid.js"), encoding="utf-8") as fh:
        source = fh.read()
    for name, value in (
        ("QUALITY_GOOD_THRESHOLD", mapillary_quality.GOOD_THRESHOLD),
        ("QUALITY_POOR_THRESHOLD", mapillary_quality.POOR_THRESHOLD),
    ):
        match = re.search(rf"^const {name} = ([0-9.]+);$", source, re.MULTILINE)
        assert match, f"{name} not found in grid.js"
        assert float(match.group(1)) == value, name


# ── the block's definitions ───────────────────────────────────────────────────


def test_the_block_reports_the_distribution_over_pano_rows_only():
    """Ten scored panos 0.05..0.95 plus non-pano rows carrying scores that would
    move every number if they counted."""
    qs = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]
    rows = [("OK", q, False, "s1") for q in qs]
    rows += [("FLAT_ONLY", 0.01, True, "s9"), ("ZERO_RESULTS", None, None, None)]
    block = compute_mapillary_meta(_census(rows))["quality"]

    assert set(block) == QUALITY_KEYS
    assert block["n_scored"] == 10
    expected = np.percentile(qs, (10, 25, 50, 75, 90))
    for key, value in zip(("p10", "p25", "p50", "p75", "p90"), expected, strict=True):
        assert block[key] == round(float(value), 3)
    assert block["pct_ge_good"] == 10.0  # 0.95 only
    assert block["pct_lt_poor"] == 60.0  # 0.05..0.55


def test_a_score_exactly_at_the_good_threshold_counts_as_good():
    """`>=` at the good cut and `<` at the poor cut -- the study's definition.
    A score of exactly 0.90 is good and exactly 0.60 is not poor; swapping
    either comparison moves these shares by a whole image."""
    good, poor = mapillary_quality.GOOD_THRESHOLD, mapillary_quality.POOR_THRESHOLD
    rows = [("OK", good, False, "s1"), ("OK", poor, False, "s1"), ("OK", 0.75, False, "s1")]
    block = compute_mapillary_meta(_census(rows))["quality"]
    assert block["pct_ge_good"] == pytest.approx(33.333)
    assert block["pct_lt_poor"] == 0.0


def test_sequence_medians_weight_drives_and_drop_sequence_less_images_from_that_cut_only():
    """One long poor drive (5 images at 0.2) and two short good ones, plus
    three sequence-less images at 0.1. The image-weighted median sees the
    sequence-less images; the sequence-weighted cut does not, and gives each
    drive one vote."""
    rows = [("OK", 0.2, False, "long") for _ in range(5)]
    rows += [("OK", 0.9, False, "a"), ("OK", 0.8, False, "a")]
    rows += [("OK", 0.95, False, "b")]
    rows += [("OK", 0.1, False, None) for _ in range(3)]
    block = compute_mapillary_meta(_census(rows))["quality"]

    assert block["n_scored"] == 11, "sequence-less images still count as scored imagery"
    assert block["p50"] == 0.2
    assert block["n_sequences"] == 3
    # Drive medians: long 0.2, a 0.85, b 0.95.
    assert block["seq_p50"] == 0.85
    assert block["seq_p25"] == round(float(np.percentile([0.2, 0.85, 0.95], 25)), 3)
    assert block["seq_p75"] == round(float(np.percentile([0.2, 0.85, 0.95], 75)), 3)


def test_the_on_foot_split_is_reported_beside_the_score():
    """The caveat that has to travel with the number: on-foot and vehicle
    medians, and the counts the on-foot share is built from. An unknown on_foot
    is neither class, and the counts are over every pano (scored or not)."""
    rows = [("OK", 0.3, True, "w"), ("OK", 0.4, True, "w"), ("NO_DATE", None, True, "w")]
    rows += [("OK", 0.9, False, "v"), ("OK", 0.8, False, "v")]
    rows += [("OK", 0.5, None, "u")]
    block = compute_mapillary_meta(_census(rows))["quality"]
    assert block["p50_on_foot"] == 0.35
    assert block["p50_vehicle"] == 0.85
    assert block["n_on_foot"] == 3
    assert block["n_foot_known"] == 5


def test_the_existing_keys_are_unchanged():
    """Additive: every key the block carried before #321 keeps its value."""
    rows = [("OK", 0.9, True, "s"), ("OK", 0.7, False, "s"), ("NO_DATE", 0.5, True, "s")]
    meta = compute_mapillary_meta(_census(rows))
    assert meta["n_images"] == 3
    assert meta["median_quality_score"] == 0.7
    assert meta["pct_on_foot"] == pytest.approx(66.7)


def test_on_foot_read_as_text_is_coded_like_the_typed_column():
    """A CSV read WITHOUT the Mapillary dtypes hands on_foot back as text, in
    every spelling pandas and the writer produce; the typed (nullable bool)
    column takes a numeric path. Both must code alike: 1 on foot, 0 vehicle,
    -1 unknown."""
    typed = pd.Series([True, False, None, True, False], dtype="boolean")
    expected = [1, 0, -1, 1, 0]
    assert _foot_codes(typed).tolist() == expected
    assert _foot_codes(pd.Series([True, False], dtype=bool)).tolist() == [1, 0]
    for spelling in (
        ["True", "False", None, "true", "FALSE"],
        ["1", "0", "", "1", "0"],
        ["1.0", "0.0", None, "1.0", "0.0"],
    ):
        assert _foot_codes(pd.Series(spelling, dtype=object)).tolist() == expected, spelling


def test_a_census_without_sequence_ids_still_gets_its_block():
    """sequence_id is not one of the two absence conditions: without it the
    image-weighted numbers and the on-foot split still stand, and only the
    sequence-weighted cut is empty (null, with zero drives)."""
    rows = [("OK", 0.9, True, "s"), ("OK", 0.5, False, "s"), ("NO_DATE", 0.7, False, "s")]
    meta = compute_mapillary_meta(_census(rows).drop(columns=["sequence_id"]))
    block = meta["quality"]
    assert block["n_scored"] == 3 and block["p50"] == 0.7
    assert block["n_on_foot"] == 1 and block["n_foot_known"] == 3
    assert block["n_sequences"] == 0
    assert (block["seq_p25"], block["seq_p50"], block["seq_p75"]) == (None, None, None)


def test_per_sequence_medians_match_a_groupby():
    """The columnar lexsort is the groupby median, without the frame."""
    rng = np.random.default_rng(321)
    q = rng.random(500)
    codes = rng.integers(0, 37, 500)
    expected = pd.Series(q).groupby(codes).median().to_numpy()
    np.testing.assert_allclose(mapillary_quality.per_sequence_medians(q, codes), expected)


# ── absent, never zero ────────────────────────────────────────────────────────


def test_a_legacy_frame_has_no_quality_block():
    """Pre-2026-07-24 censuses carry no quality_score column. The whole meta is
    absent for them (no organization_id either), and so is the block."""
    df = pd.DataFrame(
        [dict.fromkeys(METADATA_DTYPES, None) | {"status": "OK"}],
        columns=list(METADATA_DTYPES),
    )
    assert compute_mapillary_meta(df) is None


def test_a_frame_without_the_quality_column_keeps_its_meta_but_has_no_block():
    """Defensive: a frame carrying organization_id but no quality_score loses
    the score rather than raising and taking the per-run JSON down with it."""
    meta = compute_mapillary_meta(
        _census([("OK", 0.5, False, "s")]).drop(columns=["quality_score"])
    )
    assert meta["n_images"] == 1
    assert meta["median_quality_score"] is None
    assert "quality" not in meta


def test_an_all_unscored_census_has_no_quality_block():
    """A census whose pano rows all lack a score has no distribution to
    publish. The block is ABSENT -- not zeros, not a block of nulls -- so key
    presence keeps meaning "measured"."""
    meta = compute_mapillary_meta(_census([("OK", None, True, "s"), ("NO_DATE", None, False, "s")]))
    assert meta is not None
    assert "quality" not in meta
    assert meta["median_quality_score"] is None


# ── the published block reproduces the study ──────────────────────────────────


def test_the_published_block_reproduces_the_study_row(tmp_path):
    """The point of reusing the study's definitions: the same census, read the
    study's way (chunked, five columns) and the pipeline's way (the loader, the
    summarizer), gives the same numbers up to the two rounding precisions."""
    rng = np.random.default_rng(7)
    n = 400
    statuses = rng.choice(["OK", "NO_DATE", "FLAT_ONLY", "ZERO_RESULTS"], n, p=[0.6, 0.1, 0.2, 0.1])
    quality = np.where(rng.random(n) < 0.1, np.nan, rng.random(n))
    foot = rng.choice([True, False, None], n, p=[0.3, 0.6, 0.1])
    seqs = rng.choice([f"seq{i}" for i in range(15)] + [None], n)
    df = _census(
        [
            (s, None if np.isnan(q) else float(q), f, sq)
            for s, q, f, sq in zip(statuses, quality, foot, seqs, strict=True)
        ]
    )
    path = os.path.join(tmp_path, "c_width_100_height_100_step_20_mapillary_2026-09-01.csv.gz")
    write_city_csv_gz(df, path)

    study = mqc.measure_run(path, "c", "2026-09-01", os.path.basename(path))
    block = compute_mapillary_meta(load_city_csv_file(path))["quality"]

    pairs = {
        "n_scored": "n_quality",
        "p10": "q_p10",
        "p25": "q_p25",
        "p50": "q_p50",
        "p75": "q_p75",
        "p90": "q_p90",
        "pct_ge_good": "pct_ge_good",
        "pct_lt_poor": "pct_lt_poor",
        "n_sequences": "n_sequences",
        "seq_p25": "seq_q_p25",
        "seq_p50": "seq_q_p50",
        "seq_p75": "seq_q_p75",
        "p50_on_foot": "q_p50_on_foot",
        "p50_vehicle": "q_p50_vehicle",
        "n_on_foot": "n_panos_on_foot",
        "n_foot_known": "n_foot_known",
    }
    assert set(pairs) == QUALITY_KEYS
    for ours, theirs in pairs.items():
        assert block[ours] == pytest.approx(study[theirs], abs=6e-4), ours


# ── the aggregate carries it ──────────────────────────────────────────────────


def _register_mapillary_run(conn, data_dir, meta):
    """One cataloged Mapillary run whose per-run JSON is `latest_json`."""
    cid = db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )
    csv_name = (
        "bend--oregon--united-states_width_100_height_100_step_20_mapillary_2026-09-01.csv.gz"
    )
    with gzip.open(os.path.join(data_dir, csv_name), "wt") as fh:
        fh.write("status\n")
    db.register_run(
        conn,
        city_id=cid,
        run_date=date(2026, 9, 1),
        csv_filename=csv_name,
        provider="mapillary",
        total_points=1,
        status_ok=1,
        status_no_date=0,
        status_zero_results=0,
        status_other=0,
        unique_panos=1,
        unique_google_panos=0,
        coverage_rate_pct=100.0,
    )
    runs = db.get_runs_for_city(conn, cid, provider="mapillary")
    latest_json = {
        "all_panos": {
            "duplicate_stats": {"total_unique_panos": 1},
            "histogram_of_capture_dates_by_year": {},
            "age_stats": {},
        },
        "search_grid": {
            "area_km2": 0.01,
            "total_search_points": 25,
            "width_meters": 100,
            "height_meters": 100,
            "step_length_meters": 20,
        },
        "download": {},
    }
    if meta is not None:
        latest_json["mapillary_meta"] = meta
    return runs, latest_json


def test_the_aggregate_carries_mapillary_meta_when_present(conn, data_dir):
    meta = {"n_images": 1, "quality": {"p50": 0.8, "n_on_foot": 0, "n_foot_known": 1}}
    runs, latest_json = _register_mapillary_run(conn, data_dir, meta)
    block = _build_provider_summary(runs, latest_json, data_dir, conn, frozenset())
    assert block["latest"]["mapillary_meta"] == meta


def test_the_aggregate_carries_a_meta_that_has_no_quality_block(conn, data_dir):
    """The carry is keyed on the META, not on its block: a run summarized
    before #321 deployed publishes its meta as it stands (the frontend gates on
    the block), rather than nothing until it is backfilled."""
    meta = {"n_images": 1, "median_quality_score": 0.7}
    runs, latest_json = _register_mapillary_run(conn, data_dir, meta)
    block = _build_provider_summary(runs, latest_json, data_dir, conn, frozenset())
    assert block["latest"]["mapillary_meta"] == meta


def test_a_run_without_mapillary_meta_is_byte_identical_to_before(conn, data_dir):
    """Absent stays absent: no key, not a null -- so every GSV, KartaView and
    Panoramax block, and every legacy Mapillary one, is unchanged by #321."""
    runs, latest_json = _register_mapillary_run(conn, data_dir, None)
    block = _build_provider_summary(runs, latest_json, data_dir, conn, frozenset())
    assert "mapillary_meta" not in block["latest"]


# ── the backfill handle ───────────────────────────────────────────────────────


# One run per selection outcome.
#   legacy   -- CSV predates the enriched schema: no mapillary_meta (stays absent)
#   nopano   -- the column, but zero pano rows: no mapillary_meta (stays absent)
#   unscored -- the meta, every score missing: median null (stays absent)
#   done     -- JSON already carries the block (left alone)
#   stale    -- the meta with a scored median, no block (the block is spliced in)
#   nojson   -- the column, no JSON at all (rebuilt whole)
BACKFILL_RUNS = ("legacy", "nopano", "unscored", "done", "stale", "nojson")


def _backfill_catalog(conn, data_dir):
    """Six Mapillary runs, one per BACKFILL_RUNS outcome.

    Returns {name: (run_id, json_path)}.
    """
    out = {}
    for name in BACKFILL_RUNS:
        cid = db.register_city(
            conn,
            city_name=name.title(),
            state_name="Oregon",
            state_code="OR",
            country_name="United States",
            country_code="US",
            center_lat=44.0,
            center_lon=-121.0,
            grid_width_m=100,
            grid_height_m=100,
            step_m=20,
        )
        run_date = date(2026, 9, 1)
        csv_name = (
            naming.generate_run_filename(cid, 100, 100, 20, run_date, provider="mapillary")
            + ".csv.gz"
        )
        csv_path = os.path.join(data_dir, csv_name)
        panos = [] if name == "nopano" else [("a", "2025-01-01"), ("b", "2025-06-01")]
        df = make_mapillary_city_df(panos, run_date=run_date, n_empty=2)
        if name == "legacy":
            df = df[COLUMNS]
        if name == "unscored":
            df["quality_score"] = np.nan
        write_city_csv_gz(df, csv_path)
        json_path = generate_city_metadata_summary_as_json(
            csv_path,
            load_city_csv_file(csv_path),
            name.title(),
            "Oregon",
            "United States",
            100,
            100,
            20,
            force_recreate_file=True,
            run_date=run_date,
            provider="mapillary",
        )
        with gzip.open(json_path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        if name in ("stale", "nojson"):
            # A JSON summarized before #321 deployed: the meta, without its block.
            del payload["mapillary_meta"]["quality"]
            with gzip.open(json_path, "wt", encoding="utf-8") as fh:
                json.dump(payload, fh)
        assert ("quality" in payload.get("mapillary_meta", {})) == (name == "done")
        assert ("mapillary_meta" in payload) == (name not in ("legacy", "nopano"))
        if name == "nojson":
            os.remove(json_path)
        run_id = db.register_run(
            conn,
            city_id=cid,
            run_date=run_date,
            csv_filename=csv_name,
            json_filename=None if name == "nojson" else os.path.basename(json_path),
            provider="mapillary",
        )
        out[name] = (run_id, json_path)
    return out


def _bytes(path):
    """The file's bytes AND its identity.

    Bytes alone cannot see a rewrite: regenerating a legacy run's JSON
    reproduces it byte-for-byte within the same second (gzip's mtime field has
    one-second resolution), so "left alone" is asserted on the inode and
    nanosecond mtime too -- the atomic writer's os.replace changes both.
    """
    with open(path, "rb") as fh:
        stat = os.stat(path)
        return fh.read(), stat.st_ino, stat.st_mtime_ns


def _payload(json_path):
    with gzip.open(json_path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _has_block(json_path):
    return "quality" in _payload(json_path).get("mapillary_meta", {})


def _existing(runs):
    """Snapshot every per-run JSON that exists on disk."""
    return {name: _bytes(path) for name, (_, path) in runs.items() if os.path.exists(path)}


def _listed(out):
    """The city slugs' first token of every run the backfill listed."""
    return sorted(
        line.split()[0].split("--")[0] for line in out.splitlines() if "[mapillary]" in line
    )


@pytest.fixture
def no_batch(monkeypatch):
    """No run-due in flight, whatever this machine is doing."""
    monkeypatch.setattr(scheduler, "_run_due_in_flight", lambda: None)


def test_the_backfill_dry_run_selects_only_runs_that_can_gain_a_block(
    conn, data_dir, no_batch, capsys
):
    runs = _backfill_catalog(conn, data_dir)
    before = _existing(runs)

    assert rrs.backfill_mapillary_quality(data_dir, execute=False, publish=True) == 0

    assert _listed(capsys.readouterr().out) == ["nojson", "stale"]
    assert _existing(runs) == before
    assert not os.path.exists(runs["nojson"][1])
    assert not os.path.exists(os.path.join(data_dir, "cities.json.gz"))


def test_the_backfill_report_counts_each_reason(conn, data_dir, no_batch, capsys):
    _backfill_catalog(conn, data_dir)
    rrs.backfill_mapillary_quality(data_dir, execute=False, publish=True)
    out = capsys.readouterr().out
    assert "6 Mapillary runs scanned" in out
    assert "1 already carry the quality block" in out
    assert "2 predate the enriched schema or hold no pano and stay absent" in out
    assert "1 have the quality_score column but no scored pano and stay absent" in out
    assert "0 skipped (missing CSV)" in out
    assert "1 would gain the block" in out
    assert "1 would be rebuilt whole" in out


def test_the_backfill_counts_a_missing_csv_as_missing_not_absent(conn, data_dir, no_batch, capsys):
    """A run whose CSV is gone is SKIPPED and says so -- it is not one of the
    runs that predate the column, and the operator needs to tell them apart."""
    runs = _backfill_catalog(conn, data_dir)
    row = conn.execute(
        "SELECT csv_filename FROM runs WHERE run_id = ?", (runs["stale"][0],)
    ).fetchone()
    os.remove(os.path.join(data_dir, row["csv_filename"]))
    rrs.backfill_mapillary_quality(data_dir, execute=False, publish=True)
    out = capsys.readouterr().out
    assert "1 skipped (missing CSV)" in out
    assert "2 predate the enriched schema or hold no pano and stay absent" in out
    assert _listed(out) == ["nojson"]


def test_the_backfill_execute_gives_exactly_the_selected_runs_the_block(conn, data_dir, no_batch):
    runs = _backfill_catalog(conn, data_dir)
    untouched = {n: _bytes(runs[n][1]) for n in ("legacy", "nopano", "unscored", "done")}

    assert rrs.backfill_mapillary_quality(data_dir, execute=True, publish=False) == 0

    assert _has_block(runs["stale"][1])
    assert _has_block(runs["nojson"][1]), "a missing JSON is rebuilt whole"
    for name in ("legacy", "nopano", "unscored"):
        assert not _has_block(runs[name][1]), f"{name} stays ABSENT, never zero"
    assert {n: _bytes(runs[n][1]) for n in untouched} == untouched
    # publish=False means no aggregate.
    assert not os.path.exists(os.path.join(data_dir, "cities.json.gz"))


def test_a_second_backfill_pass_selects_nothing(conn, data_dir, no_batch, capsys):
    """Idempotent: a run with the column but no scored pano can never gain a
    block, so a selection that keeps choosing it re-reads its census forever."""
    runs = _backfill_catalog(conn, data_dir)
    rrs.backfill_mapillary_quality(data_dir, execute=True, publish=True)
    capsys.readouterr()
    after_first = _existing(runs)

    rrs.backfill_mapillary_quality(data_dir, execute=True, publish=True)
    out = capsys.readouterr().out
    assert _listed(out) == []
    assert "0 will gain the block" in out and "0 will be rebuilt whole" in out
    assert "3 already carry the quality block" in out
    assert _existing(runs) == after_first


def test_the_backfill_splices_the_block_and_rewrites_nothing_else(conn, data_dir, no_batch):
    """The spliced run's JSON is its old JSON plus one block: no other key is
    re-derived under today's definitions, the catalog row is not written, and
    the block equals the one the live summarizer builds from the full census."""
    runs = _backfill_catalog(conn, data_dir)
    run_id, json_path = runs["stale"]
    # Stand in for an older summarizer: a key and a value today's would not
    # write. A whole-JSON rebuild drops both; a splice must keep them.
    before = _payload(json_path)
    before["written_by_an_older_summarizer"] = True
    before["mapillary_meta"]["n_images"] = 999
    with gzip.open(json_path, "wt", encoding="utf-8") as fh:
        json.dump(before, fh)
    before_row = dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    rrs.backfill_mapillary_quality(data_dir, execute=True, publish=False)

    after = _payload(json_path)
    block = after["mapillary_meta"].pop("quality")
    assert after == before
    assert dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()) == (
        before_row
    )
    csv_path = os.path.join(data_dir, before_row["csv_filename"])
    assert block == compute_mapillary_meta(load_city_csv_file(csv_path))["quality"]


def test_the_backfill_refuses_execute_while_run_due_is_in_flight(conn, data_dir, monkeypatch):
    runs = _backfill_catalog(conn, data_dir)
    before = _bytes(runs["stale"][1])
    monkeypatch.setattr(scheduler, "_run_due_in_flight", lambda: "4242 run-due")

    status = rrs.backfill_mapillary_quality(data_dir, execute=True, publish=True)
    assert status == scheduler.USAGE_EXIT_CODE
    assert _bytes(runs["stale"][1]) == before
    assert not os.path.exists(runs["nojson"][1])


def test_the_backfill_flag_requires_the_mapillary_provider(data_dir):
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(_PROJECT_ROOT, "scripts", "recompute_run_stats.py"),
            "--data-dir",
            data_dir,
            "--regenerate-json-mapillary-meta",
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
    assert "requires --provider mapillary" in result.stderr


@pytest.mark.parametrize(
    ("other", "named"),
    [
        (["--only", "total_grid_points"], "--only"),
        (["--regenerate-json"], "--regenerate-json"),
    ],
)
def test_the_backfill_flag_refuses_the_other_modes(data_dir, other, named):
    # The quality backfill returns before the stats pass, so either companion
    # flag would be silently dropped rather than combined: refuse instead.
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(_PROJECT_ROOT, "scripts", "recompute_run_stats.py"),
            "--data-dir",
            data_dir,
            "--provider",
            "mapillary",
            "--regenerate-json-mapillary-meta",
            *other,
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
    assert f"its own mode; run {named} separately" in result.stderr
