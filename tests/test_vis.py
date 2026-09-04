"""
Tests for map-visualization edge cases (streetscape_metadata_tracker/vis.py).

Pure-logic / no-network: these build a tiny in-memory metadata DataFrame and
call create_visualization_map, asserting it produces a folium.Map without
raising. The focus is the degenerate-geometry guard from issue #69.
"""

import pathlib

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


def test_kartaview_map_url_needs_only_a_position():
    """
    Issue #312: the map fallback exists because KartaView's own /details backend
    answers `osv: null` — for every sequence measured, their own documented
    example included — so the exact-photo link lands on an error page. It is
    keyed on the PANO's position and on nothing else: a row with no sequence at
    all, which can build no viewer link, must still build this one.
    """
    map_url = vis.PROVIDER_DISPLAY["kartaview"]["map_url"]

    unlinkable = pd.Series({"pano_lat": 8.061405, "pano_lon": 98.917865, "sequence_id": pd.NA})
    assert vis.PROVIDER_DISPLAY["kartaview"]["viewer_url"]("2627370567", unlinkable) is None
    assert map_url(unlinkable) == "https://kartaview.org/map/@8.061405,98.917865,19z"

    for missing in (
        pd.Series({"pano_lat": pd.NA, "pano_lon": 98.917865}),
        pd.Series({"pano_lat": 8.061405, "pano_lon": pd.NA}),
        pd.Series({"pano_lat": "", "pano_lon": ""}),
        pd.Series({"other": "column"}),
    ):
        assert map_url(missing) is None


def test_only_kartaview_declares_a_map_fallback():
    """
    Every provider declares the keys — a fan-out over the registry must not have
    to know which providers have one — but only the provider whose viewer was
    measured broken carries a URL builder. A second entry appearing here means
    either a working viewer was given a fallback it does not need, or this one
    was copied rather than read.
    """
    with_fallback = {p for p, d in vis.PROVIDER_DISPLAY.items() if d["map_url"]}
    assert with_fallback == {"kartaview"}
    assert all("map_label" in d and "viewer_label" in d for d in vis.PROVIDER_DISPLAY.values())


def test_kartaview_popup_puts_the_working_link_first():
    """
    Order is the whole point of the fallback: the link that works has to be the
    one a reader reaches first, or the popup still sends them to KartaView's
    error page. Pins the rendered popup rather than the two URL builders — both
    builders were correct before this change too, and the popup still offered
    only the broken one.
    """
    df = _frame([_row("p1", 47.60, -122.33)])
    df["copyright_info"] = "© KartaView contributor someone"
    df["sequence_id"] = pd.Series(["11616154"], dtype="string")
    df["sequence_index"] = pd.Series([1], dtype="Int64")

    m = vis.create_visualization_map(df, "Krabi, Thailand", provider="kartaview")
    html = m.get_root().render()
    assert "kartaview.org/map/@47.6,-122.33,19z" in html
    assert "kartaview.org/details/11616154/1" in html
    assert html.index("kartaview.org/map/@") < html.index("kartaview.org/details/")


def test_gsv_popup_still_renders_exactly_one_link():
    """A provider with no fallback is unchanged by #312 — one link, as before."""
    m = vis.create_visualization_map(_frame([_row("p1", 47.60, -122.33)]), "Seattle, WA")
    html = m.get_root().render()
    assert html.count("map_action=pano") == 1
    assert "kartaview.org/map/@" not in html


def test_the_js_registry_builds_the_same_kartaview_urls():
    """
    www/js/streetscape-utils.js and PROVIDER_DISPLAY are two hand-maintained
    copies of the same two deep-links, and only the JS one is what a visitor
    clicks. Read the JS the way tests/test_build_boundary_review.py already does
    and pin the parts that must agree: the map form, its z-level, and the two
    columns it reads. Divergence is otherwise invisible to the fast suite — the
    Python copy is exercised by tests and the JS copy by nobody.
    """
    js_path = pathlib.Path(__file__).resolve().parent.parent / "www" / "js" / "streetscape-utils.js"
    js = js_path.read_text(encoding="utf-8")
    assert "https://kartaview.org/map/@" in js
    assert ",19z" in js
    for column in ("pano_lat", "pano_lon"):
        assert f"row?.{column}" in js
    assert "https://kartaview.org/details/" in js
