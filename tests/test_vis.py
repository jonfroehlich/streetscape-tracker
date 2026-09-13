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


def test_the_js_registry_covers_every_known_provider_too():
    """
    The same coverage pin for the browser-side registry, and the hole that
    made issue #334 possible: Python had this check and JS had none, so
    panoramax could be — and was, for three PRs — a COLLECTED provider that
    was not REGISTERED. CLAUDE.md states the inverse rule ("a registered
    provider is not a collected one"), and every frontend fan-out already
    gates on presence in the payload, so nothing failed: ``grid.js`` and
    ``streets.js`` simply skipped its rows, ``index.js`` rewrote
    ``?provider=panoramax`` to gsv, and ``city.js`` rendered 135,389 Panoramax
    pictures under Google's attribution, colour ramp and 2007 floor.

    Read out of the source the way
    ``test_the_js_registry_builds_the_same_kartaview_urls`` does — there is no
    Node in the fast suite — anchored on the ``const PROVIDERS = {`` block so
    a key elsewhere in the file cannot satisfy it.
    """
    import re

    from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS

    js_path = pathlib.Path(__file__).resolve().parent.parent / "www" / "js" / "streetscape-utils.js"
    js = js_path.read_text(encoding="utf-8")

    block = re.search(r"^const PROVIDERS = \{$(.*?)^\};$", js, re.MULTILINE | re.DOTALL)
    assert block, "www/js/streetscape-utils.js no longer spells `const PROVIDERS = {`"
    keys = set(re.findall(r"^  ([a-z_]+): \{$", block.group(1), re.MULTILINE))

    assert keys == set(KNOWN_PROVIDERS), (
        "www/js/streetscape-utils.js PROVIDERS and naming.KNOWN_PROVIDERS disagree: "
        f"only in JS {sorted(keys - set(KNOWN_PROVIDERS))}, "
        f"only in Python {sorted(set(KNOWN_PROVIDERS) - keys)}"
    )


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
    and pin what must agree. Divergence is otherwise invisible to the fast suite
    — the Python copy is exercised by tests and the JS copy by nobody.

    Compares the URLs the PYTHON builders actually produce against the JS
    source, rather than grepping the JS for strings it obviously contains: an
    earlier version of this test asserted only ``",19z" in js`` and would have
    stayed green through a Python-side z-level change, which is the exact
    divergence it exists to catch.
    """
    js_path = pathlib.Path(__file__).resolve().parent.parent / "www" / "js" / "streetscape-utils.js"
    js = js_path.read_text(encoding="utf-8")

    row = pd.Series(
        {
            "pano_lat": 8.061405,
            "pano_lon": 98.917865,
            "sequence_id": "8313353",
            "sequence_index": 936,
        }
    )
    map_url = vis.PROVIDER_DISPLAY["kartaview"]["map_url"](row)
    viewer_url = vis.PROVIDER_DISPLAY["kartaview"]["viewer_url"]("1855176953", row)

    # Every literal the Python builders emit around their row values has to
    # appear in the JS, z-level included, and the JS has to read the same two
    # columns for the map link and the same two for the photo link.
    for literal in ("https://kartaview.org/map/@", ",19z"):
        assert literal in js, f"JS registry does not build {map_url!r}"
    for literal in ("https://kartaview.org/details/",):
        assert literal in js, f"JS registry does not build {viewer_url!r}"
    assert map_url.startswith("https://kartaview.org/map/@") and map_url.endswith(",19z")
    assert viewer_url.startswith("https://kartaview.org/details/")
    for column in ("pano_lat", "pano_lon", "sequence_id", "sequence_index"):
        assert f"row?.{column}" in js

    # Both copies reject the same unlinkable rows. The guards drifted once
    # already: the JS rejected an empty-string sequence_index and the Python
    # copy fell through to int("") and raised, aborting a whole run's map over
    # one row that should just have lost a link.
    for missing in (
        pd.Series({"sequence_id": "8313353", "sequence_index": ""}),
        pd.Series({"sequence_id": "", "sequence_index": 936}),
    ):
        assert vis.PROVIDER_DISPLAY["kartaview"]["viewer_url"]("1855176953", missing) is None
    assert 'index === ""' in js
    assert 'lat === ""' in js and 'lng === ""' in js


def test_the_js_registry_builds_the_same_panoramax_url():
    """
    The Panoramax half of the hand-maintained-pair problem, and the one that
    just moved: both copies linked the raw JPEG until #334 measured the
    federation viewer, and only the JS copy is what a visitor clicks.

    Compares the URL the PYTHON builder actually produces against the JS
    source rather than grepping for a string the JS obviously contains — the
    same standard the KartaView parity test above was raised to. A
    Python-side change of `focus`, of the parameter name, or back to
    `/api/pictures/{id}/sd.jpg` fails here.
    """
    js_path = pathlib.Path(__file__).resolve().parent.parent / "www" / "js" / "streetscape-utils.js"
    js = js_path.read_text(encoding="utf-8")

    pano_id = "599c8ad1-3a21-4311-9179-82e31ed23d32"  # a real row from the Ames run
    url = vis.PROVIDER_DISPLAY["panoramax"]["viewer_url"](pano_id, pd.Series({}))

    prefix = "https://api.panoramax.xyz/?focus=pic&pic="
    assert url == prefix + pano_id
    assert prefix in js, f"JS registry does not build {url!r}"

    # And the link that was there before is gone from both sides, so a reader
    # cannot find two answers to "what does a Panoramax popup open?".
    assert "sd.jpg" not in js
    assert "sd.jpg" not in pathlib.Path(vis.__file__).read_text(encoding="utf-8")

    # Both copies spell the same label, which is the half a URL test misses:
    # #312's rule is that the label must describe what the link opens.
    assert vis.PROVIDER_DISPLAY["panoramax"]["viewer_label"] == "View in Panoramax"
    assert 'viewerLabel: "View in Panoramax"' in js


def test_kartaview_urls_percent_encode_their_row_values():
    """
    Both builders percent-encode everything they take from a row, so a value
    carrying a URL delimiter cannot reshape the link — the same contract
    ``viewerLinksHtml`` states in JS, where the href is interpolated into
    popup markup with no further escaping. `sequence_id` is a nullable STRING
    column, so a hostile or corrupt value is a decode away, not a schema
    violation.
    """
    hostile = pd.Series(
        {"sequence_id": '1/../x?a=b&c=d"', "sequence_index": 1, "pano_lat": 8.0, "pano_lon": 98.0}
    )
    viewer_url = vis.PROVIDER_DISPLAY["kartaview"]["viewer_url"]("p", hostile)
    assert viewer_url == "https://kartaview.org/details/1%2F..%2Fx%3Fa%3Db%26c%3Dd%22/1"
    for bad in ("/../", "?", "&", '"'):
        assert bad not in viewer_url.removeprefix("https://kartaview.org/details/")


def test_no_display_entry_builds_a_link_it_has_no_address_for():
    """
    The Python half of the JS registry sweep (#312, PR #326), and the reason it
    is a sweep: the browser-side guard shipped naming gsv and mapillary, and
    the three PROVIDER_DISPLAY entries that mirror it kept building
    ``...?pKey=``, ``...&pano=`` and ``...&pic=`` from an empty id
    — truthy strings, so the popup rendered a link to nowhere rather than no
    link.

    Reachability here is narrow (``create_visualization_map`` plots only
    ``status == "OK"`` rows, so FLAT_ONLY never arrives), which is exactly why
    it needs a test rather than a reader: nothing about the rendered map would
    have shown the drift, and the entry that will next be copied into the JS
    registry is one of the three that was wrong.

    KartaView passes on its own guard, not on the shared one — it is addressed
    by (sequence_id, sequence_index) — which is the rule being pinned: no link
    without something to address it with, not no link without an id.
    """
    empty_row = pd.Series({"sequence_id": None, "sequence_index": None})
    for provider, display in vis.PROVIDER_DISPLAY.items():
        for missing in ("", None, pd.NA):
            assert display["viewer_url"](missing, empty_row) is None, (
                f"{provider} built a link from {missing!r}"
            )


def test_id_addressed_display_entries_percent_encode_the_id():
    """
    The contract ``test_kartaview_urls_percent_encode_their_row_values`` states
    for the row-addressed builder, held for the id-addressed ones too. It did
    not hold before PR #326: gsv and mapillary interpolated ``pano_id`` raw
    while kartaview and panoramax encoded it, and ``pano_id`` is a nullable
    STRING column, so a hostile or corrupt value is a decode away rather than a
    schema violation.

    The failure this closes is concrete — an id of ``a" onmouseover=...``
    terminated the href in the folium popup and turned the remainder into a
    live attribute, inside the very anchor whose ``rel="noopener"`` was being
    added at the time.
    """
    hostile = 'a" onmouseover=alert(1) x='
    row = pd.Series({"sequence_id": "8313353", "sequence_index": 936})
    for provider in ("gsv", "mapillary", "panoramax"):
        url = vis.PROVIDER_DISPLAY[provider]["viewer_url"](hostile, row)
        for bad in ('"', " ", "<", ">"):
            assert bad not in url, f"{provider} left {bad!r} unencoded in {url!r}"


def test_a_photographer_credit_cannot_inject_markup_into_a_popup():
    """
    ``copyright_info`` is arbitrary third-party content — contributor names
    from Mapillary and KartaView, archival GSV credits — and reached the popup
    template unescaped while city.js escaped the same field with a comment
    saying why. The run map is a local artifact an operator opens, never
    published (the publish rsync walks ``data/`` only), so the blast radius is
    one browser; it is still the operator's.

    Rendered as MAPILLARY, not gsv: a gsv map keeps only ``is_google_copyright``
    rows, so a hostile credit is filtered out before it can reach a popup and
    the test would pass on an empty map. The providers whose credits are
    arbitrary are precisely the ones with no copyright filter.
    """
    df = _frame([_row("p1", 47.60, -122.33)])
    df["copyright_info"] = "<img src=x onerror=alert(1)>"
    rendered = (
        vis.create_visualization_map(df, "Seattle, WA", provider="mapillary").get_root().render()
    )
    assert "<img src=x onerror=alert(1)>" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered


def test_the_js_registry_guards_its_id_addressed_viewers_too():
    """
    Read the JS the way ``test_the_js_registry_builds_the_same_kartaview_urls``
    does, and pin the guard rather than the URL. Both copies rejecting the same
    unlinkable rows is the whole value of maintaining two, and this pair
    drifted the moment one side was fixed: PR #326 guarded the JS entries and
    left all three Python ones building the dead link.
    """
    js_path = pathlib.Path(__file__).resolve().parent.parent / "www" / "js" / "streetscape-utils.js"
    js = js_path.read_text(encoding="utf-8")

    # The JS sweep asserts this for every registered provider; assert here that
    # the sweep exists, so deleting it on that side is a failure on this one.
    assert "no registered provider builds a link it has no address for" in (
        (js_path.parent / "__tests__" / "streetscape-utils.test.js").read_text(encoding="utf-8")
    )
    # And that the two id-addressed entries actually carry a conditional.
    assert "panoId\n        ? `https://www.google.com/maps/@" in js
    assert "panoId ? `https://www.mapillary.com/app/?pKey=" in js
    assert "panoId\n        ? `https://api.panoramax.xyz/?focus=pic&pic=" in js
