"""
Tests for map-visualization edge cases (streetscape_metadata_tracker/vis.py).

Pure-logic / no-network: these build a tiny in-memory metadata DataFrame and
call create_visualization_map, asserting it produces a folium.Map without
raising. The focus is the degenerate-geometry guard from issue #69.
"""

import folium
import pandas as pd

from streetscape_metadata_tracker import vis
from streetscape_metadata_tracker.config import METADATA_DTYPES


def _row(pano_id, lat, lon):
    """One valid, official-Google GSV metadata row (config.METADATA_DTYPES)."""
    return {
        "query_lat": lat,
        "query_lon": lon,
        "query_timestamp": "2026-07-01T00:00:00+00:00",
        "pano_lat": lat,
        "pano_lon": lon,
        "pano_id": pano_id,
        "capture_date": "2024-08-01",
        "copyright_info": "© Google",
        "status": "OK",
    }


def _frame(rows):
    df = pd.DataFrame(rows, columns=list(METADATA_DTYPES.keys()))
    return df.astype({"pano_id": "string", "copyright_info": "string"})


def test_single_pano_city_does_not_crash():
    """
    A city with exactly one valid pano yields a 0 x 0 bounding box, so the
    coverage-density division would raise ZeroDivisionError without the guard
    (issue #69 — e.g. Eastsound, WA / Kodiak, AK). It must return a map instead.
    """
    result = vis.create_visualization_map(_frame([_row("p1", 47.62, -122.35)]), "Eastsound, WA")
    assert isinstance(result, folium.Map)


def test_no_valid_panos_returns_empty_map():
    """Zero valid rows is already guarded and returns an empty map, not a crash."""
    row = _row("p1", 47.62, -122.35)
    row["status"] = "ZERO_RESULTS"  # filtered out -> no valid rows
    result = vis.create_visualization_map(_frame([row]), "Nowhere, WA")
    assert isinstance(result, folium.Map)


def test_multi_pano_city_still_builds():
    """A normal multi-pano city (non-zero area) is unaffected by the guard."""
    rows = [_row("p1", 47.60, -122.33), _row("p2", 47.62, -122.35), _row("p3", 47.64, -122.31)]
    result = vis.create_visualization_map(_frame(rows), "Seattle, WA")
    assert isinstance(result, folium.Map)


def test_impossible_capture_dates_are_excluded(caplog):
    """Issue #213: a pano dated 2611 would set the age color scale and the
    temporal histogram's range for the whole city, squeezing every real capture
    into one bin. It is dropped — and said aloud, since a plot that silently
    omits data is its own trap."""
    rows = [_row("p1", 47.60, -122.33), _row("p2", 47.62, -122.35)]
    corrupt = _row("bad", 47.64, -122.31)
    corrupt["capture_date"] = "2611-09-01"
    ancient = _row("old", 47.66, -122.29)
    ancient["capture_date"] = "1970-08-01"

    kept = vis._plottable_dated_rows(_frame([*rows, corrupt, ancient]))
    assert sorted(kept["pano_id"]) == ["p1", "p2"]

    with caplog.at_level("WARNING"):
        result = vis.create_visualization_map(_frame([*rows, corrupt, ancient]), "Seattle, WA")
    assert isinstance(result, folium.Map)
    assert "2 pano(s) whose capture date cannot be true" in caplog.text


def test_plottable_rows_keep_duplicate_pano_references():
    """The plot helper narrows dates only. It must NOT adopt
    dated_unique_panos' pano_id dedup: these histograms have always counted
    pano references (one per grid point that saw the pano), so deduping here
    would quietly redefine every existing plot."""
    same_pano_twice = [_row("p1", 47.60, -122.33), _row("p1", 47.601, -122.331)]
    assert len(vis._plottable_dated_rows(_frame(same_pano_twice))) == 2


def test_every_known_provider_has_a_display_entry():
    """
    PROVIDER_DISPLAY must cover naming.KNOWN_PROVIDERS exactly. The map is
    generated AFTER the run is registered, so a missing entry fails a fully
    successful collection at its very last step and reports the sweep as
    FAILED (PR #251 review) — invisible to the CLI tests, which pass
    --no-visual. Set equality, following
    test_every_scheduled_channel_declares_its_per_ip_hosts.
    """
    from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS

    assert set(vis.PROVIDER_DISPLAY) == set(KNOWN_PROVIDERS)


def test_kartaview_run_builds_a_map():
    """A kartaview run must render (the KeyError regression), link included."""
    rows = [_row("p1", 47.60, -122.33), _row("p2", 47.62, -122.35)]
    df = _frame(rows)
    df["copyright_info"] = "© KartaView contributor someone"
    df["sequence_id"] = pd.Series(["11616154", pd.NA], dtype="string")
    df["sequence_index"] = pd.Series([1, pd.NA], dtype="Int64")
    result = vis.create_visualization_map(df, "Krabi, Thailand", provider="kartaview")
    assert isinstance(result, folium.Map)


def test_kartaview_viewer_url_needs_sequence_and_index():
    """
    The viewer is addressed by (sequence, index), not photo id — and a row can
    legitimately lack a sequence, which must yield NO link rather than a link
    to nowhere (mirrors PROVIDERS.kartaview.viewerUrl in streetscape-utils.js).
    """
    linked = pd.Series({"sequence_id": "11616154", "sequence_index": 1})
    assert vis.PROVIDER_DISPLAY["kartaview"]["viewer_url"]("2627370567", linked) == (
        "https://kartaview.org/details/11616154/1"
    )
    for missing in (
        pd.Series({"sequence_id": pd.NA, "sequence_index": 1}),
        pd.Series({"sequence_id": "11616154", "sequence_index": pd.NA}),
        pd.Series({"other": "column"}),
    ):
        assert vis.PROVIDER_DISPLAY["kartaview"]["viewer_url"]("2627370567", missing) is None
