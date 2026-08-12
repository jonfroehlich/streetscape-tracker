"""Browser end-to-end smoke test for the static frontend (issue #124).

Automates the manual 2026-07-10 browser verification (#92 task 2): serves
``www/`` locally, intercepts the frontend's data fetches with the committed
synthetic fixture (``tests/e2e/fixture``, built by ``build_fixture.py``),
and drives real headless Chromium across the full render path.

This is a SEPARATE, non-blocking job — marked ``e2e`` and excluded from the fast
``pytest`` run (see pyproject ``addopts``); the e2e CI job opts back in with
``-m e2e``. It needs ``pytest-playwright`` + a Chromium install, so we skip the
whole module cleanly when the plugin isn't present (e.g. the fast job).

Assertions (seeded from the manual run):
  * overview draws rectangles; no ``Infinity``/``NaN``/epoch in any popup (B1–B4)
  * provider toggle works and persists via ``?provider=``
  * city page: Chart.js canvas renders; snapshot ``<select>`` on a multi-run city
  * 0-pano city shows ``—``, not the Unix epoch date (#122 / #69)
  * road-walk street coverage renders from the sidecar manifest, opening on the
    fractional ramp; a city with no walk renders none, silently (#155)
  * street coverage is discoverable from the site root: the shared header, the
    streets.html listing, the "Street coverage" overview metric, and the
    always-on popup line
  * console/page-error clean on both pages and providers
"""

import functools
import http.server
import os
import socketserver
import threading

import pytest

pytest.importorskip("pytest_playwright")
from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = pytest.mark.e2e

HERE = os.path.dirname(os.path.abspath(__file__))
WWW_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "www")
FIXTURE_DIR = os.path.join(HERE, "fixture")

# Latest-run csv.gz filenames the fixture emits (see build_fixture.py).
ALPHA_LATEST = "alpha-city--alphastate--testland_width_100_height_100_step_20_2026-04-15.csv.gz"
ZERO_CITY = "zero-city--zerostate--testland_width_100_height_100_step_20_2026-04-15.csv.gz"
# The published diff detail between Alpha City's two runs (real compute_run_diff
# output: one pano_added row), fetched by the city page's change overlay.
ALPHA_DIFF = "alpha-city--alphastate--testland_diff_2026-01-15_to_2026-04-15.csv.gz"

# Substrings of expected third-party console noise to ignore (analytics/CDN),
# so "console clean" tracks OUR code, not the network environment. The
# grid-attribution streets artifact is optional by design (issue #24) — the
# fixture has none, so the browser logs a 404 console.error for the
# "_streets.json.gz" fetch that street-coverage.js handles as a silent no-op.
# The streetwalk manifest is NOT in this list: it is rebuilt with the aggregate
# in production, so it always exists and must never 404 (the fixture ships one).
_IGNORABLE_CONSOLE = (
    "favicon",
    "googletagmanager",
    "gtag",
    "google-analytics",
    "doubleclick",
    "_streets.json.gz",
)


@pytest.fixture(scope="session")
def base_url():
    """Serve www/ over http on an ephemeral port for the whole session."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WWW_DIR)

    class QuietServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = QuietServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def route_fixture_data(page: Page):
    """Fulfill every data fetch from the committed fixture.

    The frontend hardcodes the production data host (STREETSCAPE_DATA_BASE_URL); rather
    than change prod code, we intercept ``**/streetscape-tracker/data/**`` and serve the
    fixture bytes RAW (no Content-Encoding: gzip) so the page's own pako /
    DecompressionStream("gzip") does the decompression, exactly as in prod.
    """

    def handler(route):
        filename = route.request.url.split("/data/")[-1].split("?")[0]
        path = os.path.join(FIXTURE_DIR, filename)
        if not os.path.isfile(path):
            route.fulfill(status=404, body=b"not in fixture")
            return
        with open(path, "rb") as f:
            body = f.read()
        route.fulfill(
            status=200,
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
                "Access-Control-Allow-Origin": "*",
            },
        )

    page.route("**/streetscape-tracker/data/**", handler)
    yield


def _is_ignorable_console(msg) -> bool:
    """Match ignorables against the message text AND its source URL: network
    failures log a generic "Failed to load resource: ... 404" text, so the
    offending URL (e.g. the optional _streets.json.gz fetch) only appears in
    msg.location."""
    url = (msg.location or {}).get("url", "")
    return any(s in msg.text or s in url for s in _IGNORABLE_CONSOLE)


def _capture_errors(page: Page):
    """Attach console-error + pageerror listeners; return the collected list."""
    errors = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: (
            errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" and not _is_ignorable_console(msg)
            else None
        ),
    )
    return errors


# Counts non-transparent pixels on a named Leaflet pane's canvas. city.js
# creates the map with `preferCanvas`, so both the street overlay and the diff
# overlay draw onto their pane's own canvas instead of SVG paths — "did it
# render?" has to be asked in pixels. Yields 0 when the pane or its canvas is
# absent.
def _pane_ink_js(pane: str) -> str:
    return f"""
      const c = document.querySelector('.leaflet-pane.leaflet-{pane}-pane canvas');
      if (!c || !c.width || !c.height) return 0;
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let n = 0;
      for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
      return n;
    """


_STREET_INK_JS = _pane_ink_js("streetCoverage")


def _street_pane_ink(page: Page) -> int:
    """Non-transparent pixel count on the street-coverage pane canvas."""
    return page.evaluate("() => {" + _STREET_INK_JS + "}")


def _expect_pane_ink(page: Page, pane: str, *, drawn: bool):
    """Wait for a pane's canvas to be drawn / cleared (Leaflet's canvas
    renderer clears on the next animation frame, so poll)."""
    page.wait_for_function(
        "(want) => { const ink = (() => {" + _pane_ink_js(pane) + "})(); "
        "return want ? ink > 0 : ink === 0; }",
        arg=drawn,
    )


def _expect_street_ink(page: Page, *, drawn: bool):
    """Wait for the street overlay to be drawn / cleared.

    Leaflet's canvas renderer clears on the next animation frame, so toggling a
    layer off is not synchronous with the click — poll instead of sampling once.
    """
    page.wait_for_function(
        "(want) => { const ink = (() => {"
        + _STREET_INK_JS
        + "})(); return want ? ink > 0 : ink === 0; }",
        arg=drawn,
    )


def test_overview_renders_without_infinity_or_nan(page: Page, base_url):
    errors = _capture_errors(page)
    page.goto(f"{base_url}/index.html")

    # Both GSV cities (Alpha + the 0-pano Zero City) draw a rectangle. A B1–B4
    # crash on the 0-pano city would abort rendering before this succeeds.
    rects = page.locator("path.leaflet-interactive")
    expect(rects).to_have_count(2)

    # Every popup must be free of the Infinity%/NaN/epoch-date bugs. Open one
    # at a time (close between) so the popup locator stays unambiguous.
    popup = page.locator(".leaflet-popup-content")
    for i in range(rects.count()):
        rects.nth(i).click(force=True)
        expect(popup).to_have_count(1)
        text = popup.inner_text()
        for bad in ("Infinity", "NaN", "1969", "1970"):
            assert bad not in text, f"popup {i} contained {bad!r}:\n{text}"
        page.keyboard.press("Escape")
        expect(popup).to_have_count(0)

    assert errors == []


def test_provider_toggle_persists_via_query_param(page: Page, base_url):
    page.goto(f"{base_url}/index.html")
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)  # gsv default

    page.locator('input[name="provider"][value="mapillary"]').check()
    page.wait_for_url("**provider=mapillary**")
    # Only the single Mapillary city remains after the toggle.
    expect(page.locator("path.leaflet-interactive")).to_have_count(1)

    # The choice survives a reload (persisted in the URL, re-read on load).
    page.reload()
    expect(page.locator('input[name="provider"][value="mapillary"]')).to_be_checked()
    expect(page.locator("path.leaflet-interactive")).to_have_count(1)


def test_metric_toggle_recolors_and_persists_via_query_param(page: Page, base_url):
    errors = _capture_errors(page)
    page.goto(f"{base_url}/index.html")
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)
    expect(page.locator("#legend h4")).to_have_text("Median Age (years)")  # default

    page.locator('input[name="metric"][value="coverage"]').check()
    page.wait_for_url("**metric=coverage**")
    # Same cities, recolored: legend switches to coverage deciles, and the
    # popup's always-on coverage line renders a percentage (no NaN/Infinity).
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)
    expect(page.locator("#legend h4")).to_have_text("Grid Coverage (%)")
    page.locator("path.leaflet-interactive").first.click(force=True)
    popup = page.locator(".leaflet-popup-content")
    expect(popup).to_have_count(1)
    assert "Grid Coverage:" in popup.inner_text()
    page.keyboard.press("Escape")

    # The choice survives a reload (persisted in the URL, re-read on load).
    page.reload()
    expect(page.locator('input[name="metric"][value="coverage"]')).to_be_checked()
    expect(page.locator("#legend h4")).to_have_text("Grid Coverage (%)")

    assert errors == []


def test_legend_range_filter_slider(page: Page, base_url):
    """The legend's min-max range slider filters (dims) out-of-range buckets,
    persists via ?filter=, and legend rows snap the range to one bucket."""
    errors = _capture_errors(page)
    # Coverage metric: the slider span is always the full 10 deciles.
    page.goto(f"{base_url}/index.html?metric=coverage")
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)

    # Keyboard on the max thumb: 9 -> 8 activates a 0-8 decile filter.
    hi = page.locator("#legend-slider-hi")
    expect(hi).to_be_visible()
    hi.focus()
    page.keyboard.press("ArrowLeft")
    page.wait_for_url("**filter=0-8**")
    expect(page.locator("#legend-filter-label")).to_have_text("0–90%")
    # Exactly the 90-100% row falls outside the range and dims.
    expect(page.locator("button.legend-item.dimmed")).to_have_count(1)

    # A row click snaps the filter to that single bucket...
    page.locator('button.legend-item[data-bucket="0"]').click()
    page.wait_for_url("**filter=0-0**")
    expect(page.locator("#legend-filter-label")).to_have_text("0–10%")
    expect(page.locator("button.legend-item.dimmed")).to_have_count(9)
    # ...and a second click on the sole-selected row clears the filter.
    page.locator('button.legend-item[data-bucket="0"]').click()
    expect(page.locator("button.legend-item.dimmed")).to_have_count(0)
    assert "filter=" not in page.url
    expect(page.locator("#legend-filter-label")).to_have_text("all cities")

    # A URL-supplied filter is pre-applied on load.
    page.goto(f"{base_url}/index.html?metric=coverage&filter=3-7")
    expect(page.locator("#legend-filter-label")).to_have_text("30–80%")
    expect(page.locator("button.legend-item.dimmed")).to_have_count(5)

    # Dragging the filled window itself slides it, width preserved:
    # 3-7 dragged two deciles right becomes 5-9.
    slider_box = page.locator("#legend .legend-slider").bounding_box()
    fill_box = page.locator("#legend-slider-fill").bounding_box()
    per_decile = slider_box["width"] / 9
    x = fill_box["x"] + fill_box["width"] / 2
    y = fill_box["y"] + fill_box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 2 * per_decile, y, steps=8)
    page.mouse.up()
    page.wait_for_url("**filter=5-9**")
    expect(page.locator("#legend-filter-label")).to_have_text("50–100%")
    expect(page.locator("button.legend-item.dimmed")).to_have_count(5)

    assert errors == []


def test_city_page_multirun_gsv_renders_chart_and_snapshot_select(page: Page, base_url):
    errors = _capture_errors(page)
    page.goto(f"{base_url}/city.html?file={ALPHA_LATEST}")

    # Chart.js canvas is present and laid out (non-zero size).
    canvas = page.locator("#temporal-plot")
    expect(canvas).to_be_visible()
    box = canvas.bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0

    # Multi-run city → snapshot <select>, one <option> per run (2), filtered to
    # the active (GSV) provider.
    select = page.locator("#run-select")
    expect(select).to_be_visible()
    expect(select.locator("option")).to_have_count(2)

    assert errors == []


def test_zero_pano_city_shows_dash_not_epoch(page: Page, base_url):
    errors = _capture_errors(page)
    page.goto(f"{base_url}/city.html?file={ZERO_CITY}")

    stats = page.locator("table.legend-stats")
    expect(stats).to_be_visible()
    text = stats.inner_text()
    # The #122 / #69 regression: null oldest/newest dates must render "—", never
    # the Unix epoch from new Date(null).
    assert "—" in text
    for bad in ("1969", "1970"):
        assert bad not in text, f"legend showed epoch date {bad!r}:\n{text}"

    assert errors == []


def test_city_page_renders_the_road_walk_street_overlay(page: Page, base_url):
    """
    The road-walk (streetwalk) coverage artifact renders on the city page
    (#155). Alpha City is the only fixture city with a walk, so this also
    proves the manifest lookup found it — the artifact filename is NOT
    derivable from the run filename, which is the whole reason the manifest
    exists.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/city.html?file={ALPHA_LATEST}")

    panel = page.locator("#street-coverage-container")
    expect(panel).to_be_visible()

    # Headline reads the streetwalk totals through the key aliasing
    # (edges/edges_any_coverage → segments/covered): 14.9% uncovered by length,
    # 2 of 2 edges with some coverage.
    headline = page.locator("#street-coverage-headline")
    expect(headline).to_contain_text("14.9%")
    expect(headline).to_contain_text("2 of 2 segments covered")

    # A fractional artifact opens on the graduated Coverage ramp, not Age.
    expect(page.locator('.street-mode-btn[data-mode="coverage"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator('.street-mode-btn[data-mode="age"]')).to_have_attribute(
        "aria-pressed", "false"
    )
    expect(page.locator("#street-legend")).to_contain_text("partial → full")

    # The edges are drawn into the dedicated pane, which sits BELOW the pano
    # markers (city.js uses preferCanvas, so the lines land on that pane's own
    # canvas rather than as SVG paths — assert pixels, not elements). The exact
    # per-edge ramp colors are unit-tested in www/js/__tests__.
    _expect_street_ink(page, drawn=True)  # pane canvas must not be blank

    # The by-highway breakdown chart renders (residential + service).
    chart = page.locator("#street-coverage-chart")
    expect(chart).to_be_visible()
    box = chart.bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0

    # Switching to Age keeps the overlay alive (the median-age alias path).
    page.locator('.street-mode-btn[data-mode="age"]').click()
    expect(page.locator('.street-mode-btn[data-mode="age"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    _expect_street_ink(page, drawn=True)

    # The layer toggle removes the overlay and puts it back.
    page.locator("#street-layer-toggle").uncheck()
    _expect_street_ink(page, drawn=False)
    page.locator("#street-layer-toggle").check()
    _expect_street_ink(page, drawn=True)

    # "Highlight gaps" restyles in place (uncovered → red, covered faded) —
    # the overlay must stay drawn through the toggle, both ways.
    gaps = page.locator("#street-gaps-toggle")
    expect(gaps).not_to_be_checked()
    gaps.check()
    _expect_street_ink(page, drawn=True)
    gaps.uncheck()
    _expect_street_ink(page, drawn=True)

    assert errors == []


def test_site_header_navigates_and_clears_the_floating_panels(page: Page, base_url):
    """
    The shared header is the site's only navigation — before it, street
    coverage had no path from the site root at all. It floats over a
    full-bleed map, so it must also not sit on top of the panels it shares
    the viewport with.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/index.html")

    header = page.locator("header.site-header")
    expect(header).to_be_visible()
    header_box = header.bounding_box()
    assert header_box and header_box["height"] > 0

    # Panels start below the header rather than under it.
    for selector in ("#stats", ".city-search", "#legend"):
        box = page.locator(selector).bounding_box()
        assert box, f"{selector} has no bounding box"
        assert box["y"] >= header_box["y"] + header_box["height"], (
            f"{selector} overlaps the site header (y={box['y']})"
        )

    # The Streets nav item is the entry point to the road-walk listing.
    page.locator('.site-nav a[href="streets.html"]').click()
    page.wait_for_url("**/streets.html")
    expect(page.locator("h1")).to_contain_text("Street-level coverage")

    # The Grid nav item is the tabular counterpart of the overview map.
    page.locator('.site-nav a[href="grid.html"]').click()
    page.wait_for_url("**/grid.html")
    expect(page.locator("h1")).to_contain_text("Grid coverage")

    # The city page carries the same header, and its "Map" item is the way
    # back (it replaced the old standalone #back-link).
    page.goto(f"{base_url}/city.html?file={ALPHA_LATEST}")
    expect(page.locator("header.site-header")).to_be_visible()
    page.locator('.site-nav a[href="index.html"]').click()
    page.wait_for_url("**/index.html")

    assert errors == []


def test_streets_page_lists_published_road_walks(page: Page, base_url):
    """
    streets.html joins the manifest against the aggregate: the manifest has no
    display name and no run filename, so a row can only name Alpha City and
    link to its map if that join worked.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    rows = page.locator("#streets-tbody tr")
    expect(rows).to_have_count(2)  # one GSV walk, one Mapillary walk

    # Default sort is best 360° coverage first, so Alpha City (85.1%) leads
    # Map Ville (0.0% by pano, flat imagery only).
    alpha_row = rows.first
    expect(alpha_row).to_contain_text("Alpha City")
    expect(alpha_row).to_contain_text("Google Street View")
    expect(alpha_row).to_contain_text("85.1%")
    expect(alpha_row).to_contain_text("2026-04-15")
    expect(rows.nth(1)).to_contain_text("Mapillary")

    # The link target comes from the aggregate, not the manifest — plus this
    # row's own network type, so the city page draws the walk the row advertises
    # rather than defaulting to 'drive' (these fixtures ARE drive walks, hence
    # the explicit token even though it matches the default).
    expect(alpha_row.locator("a.streets-view-link")).to_have_attribute(
        "href", f"city.html?file={ALPHA_LATEST}&network=drive"
    )
    expect(page.locator("#streets-caption")).to_contain_text("2 published road-walk collections")

    # And it actually lands on the city page with the overlay.
    alpha_row.locator("a.streets-view-link").click()
    page.wait_for_url(f"**/city.html?file={ALPHA_LATEST}&network=drive")
    expect(page.locator("#street-coverage-container")).to_be_visible()

    assert errors == []


def test_streets_page_separates_360_and_any_imagery_coverage(page: Page, base_url):
    """The Mapillary fixture walk is flat-imagery-only: 0% by 360° pano, 85.1%
    counting any imagery. The two columns must show that difference rather
    than one silently standing in for the other."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    mapillary_row = page.locator("#streets-tbody tr", has_text="Mapillary")
    cells = mapillary_row.locator("td.coverage-cell")
    expect(cells).to_have_count(2)
    expect(cells.nth(0)).to_have_text("0.0%")  # 360° only
    expect(cells.nth(1)).to_have_text("85.1%")  # including flat imagery

    assert errors == []


def test_streets_table_sorts_on_header_click(page: Page, base_url):
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")
    rows = page.locator("#streets-tbody tr")
    expect(rows).to_have_count(2)

    # Opens on best-360°-coverage-first.
    expect(page.locator('th[data-key="pct"]')).to_have_attribute("aria-sort", "descending")
    expect(rows.first).to_contain_text("Alpha City")

    # Sorting by city name ascending puts Alpha first, descending flips it.
    page.locator('th[data-key="label"] button').click()
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "ascending")
    expect(page.locator('th[data-key="pct"]')).to_have_attribute("aria-sort", "none")
    expect(rows.first).to_contain_text("Alpha City")

    page.locator('th[data-key="label"] button').click()
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "descending")
    expect(rows.first).to_contain_text("Map Ville")

    # Any-imagery column: both rows are 85.1%, so the city_id tiebreak keeps
    # the order stable rather than shuffling on every click.
    page.locator('th[data-key="pctAny"] button').click()
    expect(rows.first).to_contain_text("Alpha City")

    assert errors == []


def test_streets_metric_colors_only_the_walked_cities(page: Page, base_url):
    """
    The "Street coverage" metric reads values merged from the sidecar manifest
    (they are not in cities.json.gz — that is #102). Unwalked cities must fall
    to "No data" rather than borrowing their grid coverage rate.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/index.html?metric=streets")

    expect(page.locator('input[name="metric"][value="streets"]')).to_be_checked()
    expect(page.locator("#legend h4")).to_have_text("Street Coverage (% of street-km)")

    # Both GSV cities still draw; only one of them has a value.
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)
    expect(page.locator("#legend")).to_contain_text("No data (1)")

    # The banner states the sparse denominator outright.
    expect(page.locator("#stats")).to_contain_text("1 of 2 Google Street View cities walked")

    assert errors == []


def test_overview_popup_shows_street_coverage_in_the_default_metric(page: Page, base_url):
    """
    The popup line is the main discovery surface — it must appear without the
    visitor first switching to the streets metric, and only for walked cities.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/index.html")  # default age mode

    rects = page.locator("path.leaflet-interactive")
    expect(rects).to_have_count(2)

    popup = page.locator(".leaflet-popup-content")
    with_line = []
    for i in range(rects.count()):
        rects.nth(i).click(force=True)
        expect(popup).to_have_count(1)
        text = popup.inner_text()
        if "Street Coverage" in text:
            with_line.append(text)
        page.keyboard.press("Escape")
        expect(popup).to_have_count(0)

    assert len(with_line) == 1, "exactly one fixture city has a road-walk"
    assert "85.1% of street-km" in with_line[0]
    assert "road-walk" in with_line[0]

    assert errors == []


def test_city_without_a_walk_falls_back_and_stays_clean(page: Page, base_url):
    """
    A city absent from the manifest must not render a road-walk overlay, and
    the lookup miss must be silent — the manifest is fetched on every city page
    load, so a noisy miss would spam the console for most cities.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/city.html?file={ZERO_CITY}")

    expect(page.locator("table.legend-stats")).to_be_visible()  # page finished
    expect(page.locator("#street-coverage-container")).to_be_hidden()
    assert _street_pane_ink(page) == 0

    assert errors == []


def test_grid_page_lists_city_runs(page: Page, base_url):
    """
    grid.html is the tabular counterpart of the overview map: one row per
    (city, provider) run series from the aggregate, linking into city.html.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html")

    rows = page.locator("#grid-tbody tr")
    expect(rows).to_have_count(3)  # Alpha (gsv), Zero (gsv), Map Ville (mapillary)

    # Default sort is alphabetical (a browsable index).
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "ascending")
    expect(rows.first).to_contain_text("Alpha City")
    expect(rows.nth(1)).to_contain_text("Map Ville")
    expect(rows.nth(2)).to_contain_text("Zero City")

    # Rows link to the city page via the latest run filename.
    expect(rows.first.locator("a.streets-view-link")).to_have_attribute(
        "href", f"city.html?file={ALPHA_LATEST}"
    )
    expect(page.locator("#grid-caption")).to_contain_text("3 city grid-run series")

    # A header click re-sorts (grid coverage, best first: the 0-pano city sinks).
    page.locator('th[data-key="pct"] button').click()
    expect(page.locator('th[data-key="pct"]')).to_have_attribute("aria-sort", "descending")
    expect(page.locator("#grid-tbody tr").last).to_contain_text("Zero City")

    # And it actually lands on the city page.
    page.locator("#grid-tbody tr", has_text="Alpha City").locator("a.streets-view-link").click()
    page.wait_for_url(f"**/city.html?file={ALPHA_LATEST}")
    expect(page.locator("table.legend-stats")).to_be_visible()

    assert errors == []


def test_freshness_metric_recolors_by_collection_recency(page: Page, base_url):
    """
    The Freshness metric colors cities by how recently they were collected —
    the direct answer to "which cities were just scraped?". Bucket membership
    shifts as the fixture ages, so assert the fixed bucket set rather than
    which bucket the fixture cities land in today.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/index.html?metric=freshness")

    expect(page.locator('input[name="metric"][value="freshness"]')).to_be_checked()
    expect(page.locator("#legend h4")).to_have_text("Data Freshness (last collected)")
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)

    # All five recency buckets always render (fixed set, freshest first).
    legend_rows = page.locator("button.legend-item")
    expect(legend_rows).to_have_count(5)
    expect(legend_rows.first).to_contain_text("Last 3 months")
    expect(legend_rows.last).to_contain_text("Over 1.5 years")

    # The popup leads with the collected date.
    page.locator("path.leaflet-interactive").first.click(force=True)
    popup = page.locator(".leaflet-popup-content")
    expect(popup).to_have_count(1)
    expect(popup.locator(".popup-collected")).to_contain_text("Collected")

    # The choice survives a reload (persisted in the URL, re-read on load).
    page.keyboard.press("Escape")
    page.reload()
    expect(page.locator('input[name="metric"][value="freshness"]')).to_be_checked()
    expect(page.locator("#legend h4")).to_have_text("Data Freshness (last collected)")

    assert errors == []


def test_overview_chart_drawer_collapses_and_persists(page: Page, base_url):
    """The scatter plots live in a collapsible drawer at the bottom of the
    right rail (the rail is what stops the legend/chart overlap); the
    collapsed choice persists in localStorage."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/index.html")
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)

    body = page.locator("#chart-drawer-body")
    expect(body).to_be_visible()

    page.locator("#chart-drawer-toggle").click()
    expect(body).to_be_hidden()
    # With the drawer collapsed the legend still sits clear of the header.
    header_box = page.locator("header.site-header").bounding_box()
    legend_box = page.locator("#legend").bounding_box()
    assert legend_box["y"] >= header_box["y"] + header_box["height"]

    page.reload()
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)
    expect(page.locator("#chart-drawer-body")).to_be_hidden()

    page.locator("#chart-drawer-toggle").click()
    expect(page.locator("#chart-drawer-body")).to_be_visible()

    assert errors == []


def test_city_page_change_overlay_draws_the_published_diff(page: Page, base_url):
    """
    "Show changes on map" fetches the published diff detail CSV (Alpha City's
    two fixture runs differ by one added pano) and draws it as a dot layer;
    the run-history mini-chart renders alongside the snapshot selector.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/city.html?file={ALPHA_LATEST}")
    expect(page.locator("table.legend-stats")).to_be_visible()

    # Multi-run city: the run-history mini-chart accompanies the selector.
    expect(page.locator("#run-history-chart")).to_be_visible()

    # The change section names the predecessor run.
    expect(page.locator(".legend")).to_contain_text("Since 2026-01-15")

    btn = page.locator("#diff-overlay-btn")
    expect(btn).to_have_text("Show changes on map")
    btn.click()

    # The fetch resolves, counts appear, and the dots land on their own pane.
    status = page.locator("#diff-overlay-status")
    expect(status).to_contain_text("1 added")
    expect(status).to_contain_text("0 removed")
    _expect_pane_ink(page, "diffOverlay", drawn=True)
    expect(page.locator("#diff-overlay-btn")).to_have_text("Hide changes on map")

    # Toggling off clears the pane; back on restores it from the cached layer.
    page.locator("#diff-overlay-btn").click()
    _expect_pane_ink(page, "diffOverlay", drawn=False)
    page.locator("#diff-overlay-btn").click()
    _expect_pane_ink(page, "diffOverlay", drawn=True)

    assert errors == []


# ── Exploration chassis (issue #188) ──────────────────────────────────────────
# The pure logic (filter predicates, URL round-trip, bucketing, preset
# resolution) is unit-tested offline in www/js/__tests__/table-controls.test.js.
# What can only be checked in a real browser is the wiring: that a control
# actually narrows the rendered table, that reloading the URL the page wrote
# reproduces the view, and that the default column set fits without pushing the
# page sideways.


def test_table_search_and_filters_narrow_the_rendered_rows(page: Page, base_url):
    """Typing in the search box and setting a filter must change what is in the
    tbody — and the caption must say "N of M" rather than continuing to report
    the full dataset."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")
    rows = page.locator("#streets-tbody tr")
    expect(rows).to_have_count(2)

    # Free-text search is debounced, so assert on the settled state.
    page.locator("#table-search").fill("map ville")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Map Ville")
    expect(page.locator("#streets-caption")).to_contain_text("1 of 2")

    # Clearing restores every row and the unqualified caption.
    page.locator(".controls-clear").click()
    expect(rows).to_have_count(2)
    expect(page.locator("#streets-caption")).to_contain_text("2 published road-walk collections")

    # A structured filter narrows the same way.
    page.locator('select[data-filter="provider"]').select_option("mapillary")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Mapillary")

    assert errors == []


def test_range_filter_excludes_rows_with_no_measured_value(page: Page, base_url):
    """A numeric window is a question about a measured quantity: the Mapillary
    fixture walk is 0.0% by 360° pano, so a 50–100 window must drop it rather
    than treating "no imagery" as somewhere in range."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")
    rows = page.locator("#streets-tbody tr")

    page.locator('input[data-filter="cov"][data-bound="min"]').fill("50")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Alpha City")

    assert errors == []


def test_the_view_round_trips_through_the_url(page: Page, base_url):
    """The address bar carries the whole view, so a finding can be linked. The
    page writes it with replaceState; reloading that exact URL must reproduce
    the filtered, sorted, preset-selected table."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")
    rows = page.locator("#streets-tbody tr")

    page.locator('select[data-filter="provider"]').select_option("gsv")
    page.locator("#table-preset").select_option("kilometres")
    page.locator('th[data-key="label"] button').click()
    expect(rows).to_have_count(1)

    shared_url = page.url
    assert "provider=gsv" in shared_url
    assert "preset=kilometres" in shared_url
    assert "sort=label" in shared_url

    # Reload the link cold: the controls, the column set and the sort all come
    # back, not just the row filter.
    page.goto(shared_url)
    expect(page.locator("#streets-tbody tr")).to_have_count(1)
    expect(page.locator('select[data-filter="provider"]')).to_have_value("gsv")
    expect(page.locator("#table-preset")).to_have_value("kilometres")
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "ascending")
    # The Kilometres preset shows street length and hides the Walked date.
    expect(page.locator('th[data-key="lengthKm"]')).to_have_count(1)
    expect(page.locator('th[data-key="runDate"]')).to_have_count(0)

    assert errors == []


def test_sorting_survives_a_column_preset_change(page: Page, base_url):
    """The regression the delegated header listener exists for: switching
    presets re-renders the <thead>, which would destroy click handlers bound to
    each button at construction — leaving headers that look sortable and are
    not."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    # Kilometres keeps the sorted column (pct), so the sort carries across the
    # re-render rather than being reset by the drop-the-sorted-column fallback.
    page.locator("#table-preset").select_option("kilometres")
    expect(page.locator('th[data-key="pct"]')).to_have_attribute("aria-sort", "descending")

    page.locator('th[data-key="label"] button').click()
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "ascending")
    expect(page.locator("#streets-tbody tr").first).to_contain_text("Alpha City")

    page.locator('th[data-key="label"] button').click()
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "descending")
    expect(page.locator("#streets-tbody tr").first).to_contain_text("Map Ville")

    assert errors == []


def test_dropping_the_sorted_column_falls_back_to_a_visible_one(page: Page, base_url):
    """A preset that omits the active sort column must not leave the table
    ordered by something the reader can no longer see."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")
    expect(page.locator('th[data-key="pct"]')).to_have_attribute("aria-sort", "descending")

    # Network drops the coverage column entirely.
    page.locator("#table-preset").select_option("network")
    expect(page.locator('th[data-key="pct"]')).to_have_count(0)
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "ascending")

    assert errors == []


def test_column_picker_adds_and_drops_columns(page: Page, base_url):
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html")

    # "Grid points" is not in the default Overview preset.
    expect(page.locator('th[data-key="searchPoints"]')).to_have_count(0)
    page.locator(".col-picker summary").click()
    page.locator('input[data-column="searchPoints"]').check()
    expect(page.locator('th[data-key="searchPoints"]')).to_have_count(1)
    # The fixture publishes total_search_points = 4 for Alpha City.
    expect(page.locator("#grid-tbody tr", has_text="Alpha City")).to_contain_text("4")

    page.locator(".col-reset").click()
    expect(page.locator('th[data-key="searchPoints"]')).to_have_count(0)

    assert errors == []


def test_distribution_strip_describes_the_sorted_column(page: Page, base_url):
    """The strip is a histogram of the ACTIVE SORT COLUMN over the CURRENTLY
    FILTERED rows. Its bars are decorative; the summary sentence beside them is
    the accessible equivalent and must track both."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    summary = page.locator("#distribution-strip .strip-summary")
    expect(summary).to_contain_text("360° street-km across 2 rows")

    # Re-sorting repoints the strip at the newly sorted column.
    page.locator('th[data-key="medianAge"] button').click()
    expect(summary).to_contain_text("Median age")

    # Filtering repoints it at the narrowed set.
    page.locator('select[data-filter="provider"]').select_option("gsv")
    expect(summary).to_contain_text("across 1 rows")

    assert errors == []


@pytest.mark.parametrize("path", ["grid.html", "streets.html"])
def test_default_columns_fit_without_scrolling_the_page_sideways(page: Page, base_url, path):
    """Both tables scrolled horizontally before #188, which is treated as a bug
    rather than a layout choice. The default preset exists to fit the page's
    1200px measure at desktop width."""
    errors = _capture_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/{path}")
    expect(page.locator("tbody tr").first).to_be_visible()

    # The document itself must not scroll sideways...
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"{path} scrolls sideways by {overflow}px at 1440px wide"

    # ...nor may the table overflow its own scroll container, which is the
    # narrow-viewport safety net rather than the desktop layout.
    table_overflow = page.evaluate(
        """() => {
             const wrap = document.querySelector('.streets-table-wrap');
             return wrap.scrollWidth - wrap.clientWidth;
           }"""
    )
    assert table_overflow <= 0, f"{path} table overflows its container by {table_overflow}px"

    assert errors == []
