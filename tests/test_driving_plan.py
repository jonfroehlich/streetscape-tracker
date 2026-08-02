"""Driving-plan feed snapshot tests (issue #176) — no network.

The fetch primitive is either bypassed (raw= injection, the same seam the
--from-file backfill uses) or monkeypatched; ingest is exercised for real
against the conn fixture's catalog and a tmp_path archive dir.
"""

import gzip
import json
import urllib.request
from datetime import date

import pytest

from streetscape_metadata_tracker import db, driving_plan


def _record(**overrides):
    base = {
        "country": "United States",
        "code": "US",
        "svspc": "SV",
        "region": "Kentucky",
        "districts": "Jefferson, Bullitt, Nelson",
        "publish": "Yes",
        "datestart": "2026-02-02T08:00:00.000Z",
        "dateend": "2026-12-31T08:00:00.000Z",
    }
    base.update(overrides)
    return base


def _raw(records):
    return json.dumps(records).encode("utf-8")


# ── Parsing ────────────────────────────────────────────────────────────────


def test_parse_feed_date_handles_clean_and_dirty_values():
    assert driving_plan.parse_feed_date("2026-02-02T08:00:00.000Z") == "2026-02-02"
    # The ~58 known-dirty shapes must map to None, never raise.
    assert driving_plan.parse_feed_date("13/1/19") is None
    assert driving_plan.parse_feed_date("Septemb") is None
    assert driving_plan.parse_feed_date("") is None
    assert driving_plan.parse_feed_date(None) is None
    assert driving_plan.parse_feed_date(12345) is None


def test_parse_feed_rejects_non_array_payloads():
    with pytest.raises(ValueError):
        driving_plan.parse_feed(b'{"oops": "an object"}')
    with pytest.raises(ValueError):
        driving_plan.parse_feed(b'["just", "strings"]')


def test_explode_records_splits_districts_and_drops_nothing():
    records = [
        _record(),
        # Free-form non-US districts split the same way; the raw feed is
        # preserved in the artifact, so a wrong split there loses nothing.
        _record(country="Andorra", code="AD", region=None, districts="No Driving, All Areas"),
        # Empty districts -> one presence row with a NULL district.
        _record(country="Iceland", code="IS", districts=""),
        _record(country="Norway", code="NO", districts=None),
    ]
    entries = driving_plan.explode_records(records, snapshot_id=7)

    districts = [(e[1], e[5]) for e in entries]  # (country, district)
    assert ("United States", "Jefferson") in districts
    assert ("United States", "Bullitt") in districts
    assert ("United States", "Nelson") in districts
    assert ("Andorra", "No Driving") in districts
    assert ("Andorra", "All Areas") in districts
    assert ("Iceland", None) in districts
    assert ("Norway", None) in districts
    assert len(entries) == 3 + 2 + 1 + 1
    assert all(e[0] == 7 for e in entries)


def test_dirty_date_row_survives_with_raw_string_and_null_parse(conn, tmp_path):
    raw = _raw([_record(datestart="13/1/19")])
    driving_plan.ingest(conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1), raw=raw)

    rows = conn.execute("SELECT * FROM driving_plan_entries").fetchall()
    assert len(rows) == 3, "a dirty date must never drop the record"
    assert all(r["date_start"] is None for r in rows)
    assert all(r["date_start_raw"] == "13/1/19" for r in rows)
    assert all(r["date_end"] == "2026-12-31" for r in rows)


# ── Ingest: archive + catalog ──────────────────────────────────────────────


def test_first_ingest_archives_and_explodes(conn, tmp_path):
    raw = _raw([_record()])
    result = driving_plan.ingest(
        conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1), raw=raw
    )

    assert not result.skipped and result.changed
    assert result.record_count == 1 and result.entry_count == 3

    row = conn.execute("SELECT * FROM driving_plan_snapshots").fetchone()
    assert row["fetch_date"] == "2026-08-01" and row["changed"] == 1
    assert row["artifact_filename"] == "gsv_driving_plan_2026-08-01.json.gz"
    assert row["sha256"] and row["record_count"] == 1

    artifact = tmp_path / "gsv_driving_plan_2026-08-01.json.gz"
    assert artifact.exists()
    assert gzip.decompress(artifact.read_bytes()) == raw, "the archive is the raw feed, verbatim"
    assert not (tmp_path / "gsv_driving_plan_2026-08-01.json.gz.tmp").exists()


def test_unchanged_feed_gets_a_snapshot_row_but_no_artifact_or_entries(conn, tmp_path):
    raw = _raw([_record()])
    driving_plan.ingest(conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1), raw=raw)
    result = driving_plan.ingest(
        conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 2), raw=raw
    )

    assert not result.changed and result.entry_count == 0
    day2 = conn.execute(
        "SELECT * FROM driving_plan_snapshots WHERE fetch_date = '2026-08-02'"
    ).fetchone()
    assert day2["changed"] == 0, "the row IS the observation 'we looked, it was unchanged'"
    assert day2["artifact_filename"] is None
    assert not (tmp_path / "gsv_driving_plan_2026-08-02.json.gz").exists()
    n_entries = conn.execute(
        "SELECT COUNT(*) FROM driving_plan_entries WHERE snapshot_id = ?",
        (day2["snapshot_id"],),
    ).fetchone()[0]
    assert n_entries == 0


def test_publish_yes_to_no_transition_is_queryable_across_changed_snapshots(conn, tmp_path):
    """The Yes -> No flip is the campaign-closed signal — the reason ingest
    never filters on publish."""
    driving_plan.ingest(
        conn,
        archive_dir=str(tmp_path),
        fetch_date=date(2026, 8, 1),
        raw=_raw([_record(publish="Yes")]),
    )
    result = driving_plan.ingest(
        conn,
        archive_dir=str(tmp_path),
        fetch_date=date(2026, 9, 1),
        raw=_raw([_record(publish="No")]),
    )
    assert result.changed
    assert (tmp_path / "gsv_driving_plan_2026-09-01.json.gz").exists()

    flips = conn.execute(
        """SELECT s.fetch_date, e.publish FROM driving_plan_entries e
           JOIN driving_plan_snapshots s ON s.snapshot_id = e.snapshot_id
           WHERE e.region = 'Kentucky' AND e.district = 'Jefferson'
           ORDER BY s.fetch_date"""
    ).fetchall()
    assert [(r["fetch_date"], r["publish"]) for r in flips] == [
        ("2026-08-01", "Yes"),
        ("2026-09-01", "No"),
    ]
    # And the read helper reflects the newest picture.
    active = db.get_active_driving_plans(conn)
    assert {r["publish"] for r in active} == {"No"}


def test_same_day_reingest_skips_without_touching_the_network(conn, tmp_path, monkeypatch):
    raw = _raw([_record()])
    driving_plan.ingest(conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1), raw=raw)

    def no_network(*a, **k):
        raise AssertionError("politeness gate must short-circuit before any fetch")

    monkeypatch.setattr(driving_plan, "fetch_feed", no_network)
    result = driving_plan.ingest(conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1))

    assert result.skipped and result.record_count == 1


def test_forced_same_day_reingest_replaces_without_duplicating(conn, tmp_path):
    driving_plan.ingest(
        conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1), raw=_raw([_record()])
    )
    two = [_record(), _record(region="Ohio", districts="Hamilton")]
    result = driving_plan.ingest(
        conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1), raw=_raw(two), force=True
    )

    assert not result.skipped and result.changed
    assert conn.execute("SELECT COUNT(*) FROM driving_plan_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM driving_plan_entries").fetchone()[0] == 4
    assert conn.execute("SELECT record_count FROM driving_plan_snapshots").fetchone()[0] == 2


def test_forced_reingest_that_becomes_unchanged_clears_stale_entries(conn, tmp_path):
    """Day 1 archives feed A; day 2's first ingest sees feed B (changed, entries
    written); a forced day-2 re-fetch sees A again — changed must flip to 0 and
    the earlier ingest's entries must not linger."""
    feed_a, feed_b = _raw([_record()]), _raw([_record(region="Ohio")])
    driving_plan.ingest(conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 1), raw=feed_a)
    driving_plan.ingest(conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 2), raw=feed_b)
    result = driving_plan.ingest(
        conn, archive_dir=str(tmp_path), fetch_date=date(2026, 8, 2), raw=feed_a, force=True
    )

    assert not result.changed
    day2 = conn.execute(
        "SELECT * FROM driving_plan_snapshots WHERE fetch_date = '2026-08-02'"
    ).fetchone()
    assert day2["changed"] == 0 and day2["artifact_filename"] is None
    n = conn.execute(
        "SELECT COUNT(*) FROM driving_plan_entries WHERE snapshot_id = ?",
        (day2["snapshot_id"],),
    ).fetchone()[0]
    assert n == 0


# ── fetch_feed ─────────────────────────────────────────────────────────────


def test_fetch_feed_retries_then_succeeds_with_browser_ua(monkeypatch):
    calls = []

    class FakeResponse:
        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        if len(calls) < 3:
            raise OSError("transient")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(driving_plan.time, "sleep", lambda s: None)

    assert driving_plan.fetch_feed("https://example.com/feed.json", retries=3) == b"[]"
    assert len(calls) == 3
    assert calls[0].get_header("User-agent", "").startswith("Mozilla/5.0")


def test_fetch_feed_raises_after_exhausting_retries(monkeypatch):
    def always_down(request, timeout=None):
        raise OSError("down")

    monkeypatch.setattr(urllib.request, "urlopen", always_down)
    monkeypatch.setattr(driving_plan.time, "sleep", lambda s: None)

    with pytest.raises(OSError):
        driving_plan.fetch_feed("https://example.com/feed.json", retries=2)
