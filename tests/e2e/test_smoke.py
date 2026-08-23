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
import re
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
# Alpha City's Mapillary series — the fixture's one two-provider city, which
# the pivoted grid/streets tables (#250) need in order to render a Δ at all.
ALPHA_MAPILLARY_LATEST = (
    "alpha-city--alphastate--testland_width_100_height_100_step_20_mapillary_2026-04-15.csv.gz"
)
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
    # The two Mapillary cities remain; Zero City (gsv only) drops out. The
    # provider sets OVERLAP but are not equal, which is what makes the toggle
    # observable at all — Alpha City is collected by both.
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)

    # The choice survives a reload (persisted in the URL, re-read on load).
    page.reload()
    expect(page.locator('input[name="provider"][value="mapillary"]')).to_be_checked()
    expect(page.locator("path.leaflet-interactive")).to_have_count(2)


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

    # The Driving nav item reaches the plan-vs-observed join (issue #176).
    page.locator('.site-nav a[href="driving.html"]').click()
    page.wait_for_url("**/driving.html")
    expect(page.locator("h1")).to_contain_text("Driving plan")

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

    Since issue #250 a row is a (city, NETWORK) pair with each provider as a
    sub-column — so the fixture's four walks collapse to two 'drive' rows, and
    the 'all_public' walk is behind the network selector rather than beside
    them (a different street-km denominator must never share a column).
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    rows = page.locator("#streets-tbody tr")
    expect(rows).to_have_count(2)  # Alpha City and Map Ville, on Roads

    # Default sort is best GSV 360° coverage first, so Alpha City (85.1%)
    # leads Map Ville, which has no GSV walk at all.
    alpha_row = rows.first
    expect(alpha_row).to_contain_text("Alpha City")
    expect(alpha_row).to_contain_text("85.1%")
    expect(alpha_row).to_contain_text("2026-04-15")

    # Alpha City is walked by BOTH providers: two populated 360° cells and a
    # real Δ. Map Ville is Mapillary-only, so its GSV cell reads absent.
    expect(alpha_row.locator("td.coverage-cell").nth(0)).to_have_text("85.1%")  # GSV
    expect(alpha_row.locator("td.coverage-cell").nth(1)).to_have_text("0.0%")  # Mapillary
    expect(alpha_row.locator("td.delta-cell").first).to_have_text("-85.1 pp")
    expect(rows.nth(1).locator("td.coverage-cell").nth(0)).to_have_text("—")

    # The link target comes from the aggregate, not the manifest — plus this
    # row's own network type, so the city page draws the walk the row advertises
    # rather than defaulting to 'drive'.
    link = alpha_row.locator("th a.streets-view-link")
    expect(link).to_have_attribute("href", f"city.html?file={ALPHA_LATEST}&network=drive")
    # The link lives on the row's own name cell now (issue #188 follow-up: the
    # separate "View on map" column was folded in) — a whole trailing column
    # existed only to carry the one link every row already has a natural home
    # for. There is no longer a dedicated actions/link header.
    expect(link).to_have_text("Alpha City, Alphastate, Testland")
    expect(page.locator("thead").get_by_text("Link to city map")).to_have_count(0)
    expect(page.locator("#streets-caption")).to_contain_text("2 cities walked on Roads")

    # And it actually lands on the city page with the overlay.
    link.click()
    page.wait_for_url(f"**/city.html?file={ALPHA_LATEST}&network=drive")
    expect(page.locator("#street-coverage-container")).to_be_visible()

    assert errors == []


def test_streets_provider_cells_open_that_providers_own_walk(page: Page, base_url):
    """city.html derives its provider from the run filename, so a per-city row
    needs a per-provider way in, and EVERY per-provider cell is one. The
    filename must come from the provider-keyed aggregate entry, never from the
    bare-city_id NAME fallback — that fallback exists so a city walked by a
    provider it has no grid run for still gets a label, and following it would
    open a different provider's series."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    alpha = page.locator("#streets-tbody tr", has_text="Alpha City")
    links = alpha.locator("td a.provider-cell-link")
    expect(links).to_have_count(6)  # 3 groups x 2 providers; the Δ is not one
    hrefs = links.evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert set(hrefs) == {
        f"city.html?file={ALPHA_LATEST}&network=drive",
        f"city.html?file={ALPHA_MAPILLARY_LATEST}&network=drive",
    }, hrefs
    expect(alpha.locator("td.delta-cell a")).to_have_count(0)

    # The broad walk's links carry ITS network, not the default.
    page.locator('select[data-filter="network"]').select_option("all_public")
    broad = page.locator("#streets-tbody tr", has_text="Alpha City")
    expect(broad.locator("td a.provider-cell-link").first).to_have_attribute(
        "href", f"city.html?file={ALPHA_LATEST}&network=all_public"
    )

    assert errors == []


def test_streets_page_separates_360_and_any_imagery_coverage(page: Page, base_url):
    """Alpha City's Mapillary walk is flat-imagery-only: 0% by 360° pano, 85.1%
    counting any imagery. Pivoted, that split is now one provider's two cells
    in two different groups — and the group headers are what keep them from
    reading as one number."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html?preset=kilometres")

    alpha = page.locator("#streets-tbody tr", has_text="Alpha City")
    # Kilometres shows the 360° group only, so switch to the compare preset,
    # which carries both coverage groups.
    page.goto(f"{base_url}/streets.html")
    page.locator(".col-picker summary").click()
    page.locator('input[data-column="pctAny_mapillary"]').check()

    alpha = page.locator("#streets-tbody tr", has_text="Alpha City")
    cells = alpha.locator("td.coverage-cell")
    # GSV 360°, Mapillary 360°, then the Mapillary any-imagery cell just added.
    expect(cells.nth(1)).to_have_text("0.0%")  # Mapillary, 360° only
    expect(cells.last).to_have_text("85.1%")  # Mapillary, including flat imagery

    assert errors == []


def test_streets_network_selector_switches_series_and_round_trips(page: Page, base_url):
    """Two networks are two different street-km denominators, so they are rows
    behind a page-level selector rather than columns. There is deliberately no
    "all networks" option — that would stack incomparable numbers — which is
    why the parameter's ABSENCE means 'drive' rather than "no filter"."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    network = page.locator('select[data-filter="network"]')
    expect(network).to_have_value("drive")
    # No blank "any" option: every option is a real network.
    expect(network.locator("option")).to_have_count(2)
    expect(page.locator("#streets-caption")).to_contain_text("walked on Roads")
    # The default is not written into a clean URL.
    assert "network=" not in page.url

    network.select_option("all_public")
    rows = page.locator("#streets-tbody tr")
    expect(rows).to_have_count(1)  # only Alpha City has a broad walk
    expect(rows.first).to_contain_text("Alpha City")
    expect(page.locator("#streets-caption")).to_contain_text("1 city walked on Roads + paths")
    assert "network=all_public" in page.url

    # The broad row's link carries its own network, or the city page would draw
    # the drive walk instead — a different metric under the same name.
    expect(rows.first.locator("th a.streets-view-link")).to_have_attribute(
        "href", f"city.html?file={ALPHA_LATEST}&network=all_public"
    )

    # A cold reload of that URL reproduces the view rather than snapping back.
    page.goto(f"{base_url}/streets.html?network=all_public")
    expect(page.locator('select[data-filter="network"]')).to_have_value("all_public")
    expect(page.locator("#streets-tbody tr")).to_have_count(1)

    assert errors == []


def test_streets_table_sorts_on_header_click(page: Page, base_url):
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")
    rows = page.locator("#streets-tbody tr")
    expect(rows).to_have_count(2)

    # Opens on best-GSV-360°-coverage-first. pctBest has no column of its own,
    # so sorting by it would order the table by something invisible.
    expect(page.locator('th[data-key="pct_gsv"]')).to_have_attribute("aria-sort", "descending")
    expect(rows.first).to_contain_text("Alpha City")

    # Sorting by city name ascending puts Alpha first, descending flips it.
    page.locator('th[data-key="label"] button').click()
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "ascending")
    expect(page.locator('th[data-key="pct_gsv"]')).to_have_attribute("aria-sort", "none")
    expect(rows.first).to_contain_text("Alpha City")

    page.locator('th[data-key="label"] button').click()
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "descending")
    expect(rows.first).to_contain_text("Map Ville")

    # A Δ header sorts too — the head-to-head question the pivot exists for.
    page.locator('th[data-key="deltaPct"] button').click()
    expect(page.locator('th[data-key="deltaPct"]')).to_have_attribute("aria-sort", "descending")
    # Map Ville has no GSV walk, so its Δ is absent and sinks in both
    # directions: a missing comparison is not a small one.
    expect(rows.first).to_contain_text("Alpha City")
    page.locator('th[data-key="deltaPct"] button').click()
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


def test_grid_page_lists_one_row_per_city(page: Page, base_url):
    """
    grid.html is the tabular counterpart of the overview map. Since issue #250
    a row is a CITY, with each provider as a sub-column under a grouped header
    — the pivot that makes "does Mapillary beat GSV here?" readable off one
    line instead of scattered across two rows a metric sort pulls apart.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html")

    rows = page.locator("#grid-tbody tr")
    expect(rows).to_have_count(3)  # three CITIES, not four (city, provider) series

    # Default sort is alphabetical (a browsable index).
    expect(page.locator('th[data-key="label"]')).to_have_attribute("aria-sort", "ascending")
    expect(rows.first).to_contain_text("Alpha City")
    expect(rows.nth(1)).to_contain_text("Map Ville")
    expect(rows.nth(2)).to_contain_text("Zero City")

    # The header is two rows now: a colgroup cell per metric over its
    # per-provider leaves. Only the leaves are sortable.
    expect(page.locator("#grid-thead tr")).to_have_count(2)
    group = page.locator("#grid-thead th.th-group", has_text="Grid coverage")
    expect(group).to_have_count(1)
    expect(group).to_have_attribute("colspan", "3")  # GSV + Mapillary + Δ
    assert group.evaluate("el => el.hasAttribute('data-key')") is False

    # Alpha City is the two-provider city: both leaves populated, and a Δ that
    # is a real signed number rather than an em-dash.
    alpha = rows.first
    expect(alpha.locator("td.coverage-cell").nth(0)).to_have_text("75.0%")  # GSV
    expect(alpha.locator("td.coverage-cell").nth(1)).to_have_text("66.7%")  # Mapillary
    expect(alpha.locator("td.delta-cell").first).to_have_text("-8.3 pp")

    # Map Ville has no GSV run at all: the union pivot keeps the row, the
    # missing provider reads as absent, and the Δ has nothing to compare.
    map_ville = rows.nth(1)
    expect(map_ville.locator("td.coverage-cell").nth(0)).to_have_text("—")
    expect(map_ville.locator("td.coverage-cell").nth(1)).to_have_text("66.7%")
    expect(map_ville.locator("td.delta-cell").first).to_have_text("—")

    # Rows link to the city page via a run filename, from the row's own name
    # cell — there is no separate "View on map" column (issue #188 follow-up:
    # folded into City, the one place every row already has).
    expect(alpha.locator("th a.streets-view-link")).to_have_attribute(
        "href", f"city.html?file={ALPHA_LATEST}"
    )
    expect(page.locator("thead").get_by_text("Link to city map")).to_have_count(0)
    expect(page.locator("#grid-caption")).to_contain_text("3 cities (4 provider series)")

    # A grouped LEAF header sorts (GSV grid coverage, best first: the 0-pano
    # city sinks, and Map Ville sinks below it because it has no GSV value at
    # all — absent is not small).
    page.locator('th[data-key="pct_gsv"] button').click()
    expect(page.locator('th[data-key="pct_gsv"]')).to_have_attribute("aria-sort", "descending")
    expect(page.locator("#grid-tbody tr").first).to_contain_text("Alpha City")
    expect(page.locator("#grid-tbody tr").last).to_contain_text("Map Ville")

    # ...and so does a Δ header, which is the whole point of the pivot.
    page.locator('th[data-key="deltaPct"] button').click()
    expect(page.locator('th[data-key="deltaPct"]')).to_have_attribute("aria-sort", "descending")
    expect(page.locator("#grid-tbody tr").first).to_contain_text("Alpha City")
    assert "sort=deltaPct" in page.url

    # And it actually lands on the city page.
    page.locator("#grid-tbody tr", has_text="Alpha City").locator("th a.streets-view-link").click()
    page.wait_for_url(f"**/city.html?file={ALPHA_LATEST}")
    expect(page.locator("table.legend-stats")).to_be_visible()

    assert errors == []


def test_grid_provider_cells_open_that_providers_own_run(page: Page, base_url):
    """city.html derives its provider from the run filename, so a per-city row
    needs a per-provider way in — otherwise the Mapillary series of a city that
    also has GSV is unreachable from this page. EVERY per-provider cell is that
    way in, not just the date one."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html")

    alpha = page.locator("#grid-tbody tr", has_text="Alpha City")
    # Overview shows two metric groups plus Last collected, so Alpha City's two
    # providers contribute six linked cells; the Δ cells are not links.
    links = alpha.locator("td a.provider-cell-link")
    expect(links).to_have_count(6)
    hrefs = links.evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert set(hrefs) == {
        f"city.html?file={ALPHA_LATEST}",
        f"city.html?file={ALPHA_MAPILLARY_LATEST}",
    }, hrefs
    expect(alpha.locator("td.delta-cell a")).to_have_count(0)

    # Map Ville has no GSV run: its GSV cells are plain, not links to nowhere.
    map_ville = page.locator("#grid-tbody tr", has_text="Map Ville")
    expect(map_ville.locator("td a.provider-cell-link")).to_have_count(3)

    # Clicking a Mapillary cell lands on the Mapillary series.
    alpha.locator("td.coverage-cell").nth(1).locator("a").click()
    page.wait_for_url(f"**/city.html?file={ALPHA_MAPILLARY_LATEST}")
    expect(page.locator("table.legend-stats")).to_be_visible()

    assert errors == []


def test_grid_collected_by_filter_replaces_the_multi_provider_checkbox(page: Page, base_url):
    """The old "Multiple providers" checkbox existed only to FIND comparable
    cities, because the un-pivoted layout could not SHOW the comparison. It is
    now an option on the same select the per-provider values live on, so an old
    ?provider= link keeps working and "2+ providers" is one click away."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html?provider=mapillary")

    rows = page.locator("#grid-tbody tr")
    expect(rows).to_have_count(2)  # Alpha City and Map Ville
    expect(page.locator('select[data-filter="provider"]')).to_have_value("mapillary")

    page.locator('select[data-filter="provider"]').select_option("multi")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Alpha City")
    expect(page.locator("#grid-caption")).to_contain_text("1 of 3 cities (2 provider series)")

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

    # Clearing restores every row and the unqualified caption — and puts the
    # network selector back on its DEFAULT rather than blanking it, since a
    # blank network would stack two street-km denominators in one column.
    page.locator(".controls-clear").click()
    expect(rows).to_have_count(2)
    expect(page.locator("#streets-caption")).to_contain_text("2 cities walked on Roads")
    expect(page.locator('select[data-filter="network"]')).to_have_value("drive")

    # A structured filter narrows the same way. Only Alpha City was walked by
    # Google Street View.
    page.locator('select[data-filter="provider"]').select_option("gsv")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Alpha City")

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

    # Kilometres keeps the sorted column (pct_gsv), so the sort carries across
    # the re-render rather than being reset by the drop-the-sorted-column
    # fallback.
    page.locator("#table-preset").select_option("kilometres")
    expect(page.locator('th[data-key="pct_gsv"]')).to_have_attribute("aria-sort", "descending")

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
    expect(page.locator('th[data-key="pct_gsv"]')).to_have_attribute("aria-sort", "descending")

    # Network drops the whole coverage group.
    page.locator("#table-preset").select_option("network")
    expect(page.locator('th[data-key="pct_gsv"]')).to_have_count(0)
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

    # A grouped LEAF is offered under a self-contained name, not the bare
    # provider label its header shows — "GSV" appears under four different
    # metrics and would be four identical checkboxes.
    expect(page.locator(".col-picker")).to_contain_text(
        "Panoramas (per provider — not comparable) — GSV"
    )
    page.locator('input[data-column="panos_gsv"]').check()
    expect(page.locator('th[data-key="panos_gsv"]')).to_have_count(1)
    # Checking ONE leaf of a group still renders the group header over it.
    expect(page.locator("#grid-thead th.th-group", has_text="Panoramas")).to_have_attribute(
        "colspan", "1"
    )

    page.locator(".col-reset").click()
    expect(page.locator('th[data-key="searchPoints"]')).to_have_count(0)
    expect(page.locator('th[data-key="panos_gsv"]')).to_have_count(0)

    assert errors == []


def test_unchecking_every_optional_column_actually_empties_the_table(page: Page, base_url):
    """Regression: resolveVisibleColumns used to treat an explicit "every box
    unchecked" selection the same as "no picker override" and silently kept
    rendering the preset's columns while the checkboxes read unchecked. Only
    the always-on City column (which also carries the row's link, since the
    separate "View on map" column was folded into it) should survive
    unchecking everything, and a reload of the resulting link must reproduce
    that."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html")

    page.locator(".col-picker summary").click()
    boxes = page.locator("input[data-column]")
    count = boxes.count()
    for i in range(count):
        boxes.nth(i).uncheck()

    # Only the one always-on column remains: City. With no grouped column left
    # visible, the header collapses back to a SINGLE row — the shape
    # driving.html renders permanently, reached here from the other direction.
    expect(page.locator("#grid-thead th")).to_have_count(1)
    expect(page.locator("#grid-thead tr")).to_have_count(1)
    expect(page.locator("#grid-thead th.th-group")).to_have_count(0)
    expect(page.locator('th[data-key="pct_gsv"]')).to_have_count(0)
    for i in range(count):
        expect(boxes.nth(i)).not_to_be_checked()

    # The URL carries the explicit empty selection...
    assert "cols=" in page.url
    reload_url = page.url

    # ...and reloading it cold reproduces the same empty-but-not-default view,
    # rather than snapping back to the preset's columns.
    page.goto(reload_url)
    expect(page.locator("#grid-thead th")).to_have_count(1)
    expect(page.locator("#grid-thead tr")).to_have_count(1)
    page.locator(".col-picker summary").click()
    for i in range(page.locator("input[data-column]").count()):
        expect(page.locator("input[data-column]").nth(i)).not_to_be_checked()

    assert errors == []


@pytest.mark.parametrize("path", ["grid.html", "streets.html"])
def test_the_pivoted_pages_replaced_the_strip_with_per_filter_histograms(
    page: Page, base_url, path
):
    """Issue #250. The sorted-column strip is gone from these two pages: it
    visualized whichever column happened to be sorted, over the FILTERED rows,
    so it silently swapped its metric on a header click and collapsed under the
    very bar-click it invited. Each numeric filter now owns one histogram, on
    one metric, on a fixed axis."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/{path}")
    expect(page.locator("tbody tr").first).to_be_visible()

    expect(page.locator("#distribution-strip")).to_have_count(0)
    cov = page.locator('.control-histogram[data-histogram="cov"]')
    expect(cov).to_have_count(1)
    expect(cov.locator(".hist-bar").first).to_be_visible()
    # The bars are decorative; the thumbs carry the announced value and the
    # number inputs carry the exact figures.
    expect(cov.locator(".hist-bars")).to_have_attribute("aria-hidden", "true")
    expect(cov.locator(".hist-lo")).to_have_attribute("aria-valuetext", re.compile(r"."))
    expect(cov.locator('input[data-bound="min"]')).to_have_count(1)

    assert errors == []


def test_histogram_slider_narrows_the_table_by_keyboard(page: Page, base_url):
    """The slider must be operable without a pointer, and one keypress has to
    move all three of the things that carry the filter: the rendered rows, the
    precision input beside it, and the URL."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")
    rows = page.locator("#streets-tbody tr")
    expect(rows).to_have_count(2)

    lo = page.locator('.control-histogram[data-histogram="cov"] .hist-lo')
    # The step is derived from the data's own extent (sliderStepFor), so read
    # it rather than hardcoding what today's fixture happens to span.
    step = lo.get_attribute("step")
    lo.press("ArrowRight")

    # Map Ville's walk is flat-imagery-only (0.0% by 360° pano), so any
    # non-zero floor drops it and leaves Alpha City.
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Alpha City")
    expect(page.locator('input[data-filter="cov"][data-bound="min"]')).to_have_value(step)
    # URLSearchParams percent-encodes the "~" separator; the wire format itself
    # is unchanged from the plain `range` filter's.
    assert f"cov={step}%7E" in page.url, f"expected cov={step}~ in {page.url}"

    # Full-span is not a filter: arrowing back to the floor clears it from the
    # URL rather than writing an inert "cov=0~".
    lo.press("ArrowLeft")
    expect(rows).to_have_count(2)
    assert "cov=" not in page.url

    assert errors == []


def test_histogram_bars_track_other_controls_but_not_their_own_selection(page: Page, base_url):
    """The crossfilter rule. A histogram is computed over the rows every OTHER
    control has selected: a search query must redraw it, while its own brush
    must not — otherwise the picture collapses under the hand that drew it and
    dragging back out cannot restore bars that are no longer there."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/streets.html")

    def lit_bars():
        """Bars with a non-zero height, i.e. buckets holding rows."""
        return page.evaluate(
            """() => [...document.querySelectorAll(
                 '.control-histogram[data-histogram=cov] .hist-bar')]
                 .filter((b) => parseFloat(b.style.height) > 0).length"""
        )

    both = lit_bars()
    assert both == 2, f"two walks at opposite ends of the range: {both} lit bars"

    # Its OWN brush leaves the bars alone — the excluded buckets are dimmed,
    # not removed, and the axis does not rescale. (Typed through the precision
    # input rather than the thumb, which also pins that the two halves of the
    # control stay in step: a bound typed on the right must move the handles
    # and the dimming on the left.)
    page.locator('input[data-filter="cov"][data-bound="min"]').fill("50")
    expect(page.locator("#streets-tbody tr")).to_have_count(1)
    assert lit_bars() == both, "a slider's own selection must not redraw its bars"
    dimmed = page.locator('.control-histogram[data-histogram="cov"] .hist-bar.dimmed')
    expect(dimmed.first).to_be_attached()
    # The bucket holding the surviving row is NOT dimmed, so dimming reads as
    # "outside the window" rather than "everything but the last bar".
    assert dimmed.count() < 24, "the whole histogram was dimmed"

    # Another control DOES redraw them: the search box is not this filter, so
    # its narrowing is part of the cross-selection the bars are computed over.
    page.locator("#table-search").fill("alpha")
    expect(page.locator("#streets-tbody tr")).to_have_count(1)
    page.wait_for_function(
        """() => [...document.querySelectorAll(
             '.control-histogram[data-histogram=cov] .hist-bar')]
             .filter((b) => parseFloat(b.style.height) > 0).length === 1"""
    )

    assert errors == []


def test_collected_by_scopes_the_numeric_filters(page: Page, base_url):
    """The "Collected by" select is a SCOPE, not merely a row filter (#250).

    A pivoted row holds one number per provider, so "coverage over 10%" is not
    a complete question until you say WHOSE coverage — and before this the two
    controls did not compose at all: the sliders always read a best-across
    field, so on the live catalog "Mapillary + >= 80%" returned 56 cities and
    not one of them had Mapillary coverage over 80. Every one matched on GSV's
    number. This pins the whole interaction in a browser: the field the slider
    reads, the axis it re-seeds to, and the wording that says whose numbers
    these are.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html")
    rows = page.locator("#grid-tbody tr")
    legend = page.locator("#f-cov-legend")
    cov_min = page.locator('input[data-filter="cov"][data-bound="min"]')
    lo = page.locator('.control-histogram[data-histogram="cov"] .hist-lo')

    # Unscoped it keeps the exists-semantics, with the quantifier spelled out
    # instead of a bare "best" that never said across what.
    expect(legend).to_have_text("Grid coverage % — any provider reaches")
    assert float(lo.get_attribute("max")) >= 75, lo.get_attribute("max")

    # Alpha City qualifies on GSV's 75.0%, Map Ville on Mapillary's 66.7%:
    # different providers, and either one counts.
    cov_min.fill("10")
    expect(rows).to_have_count(2)

    page.locator('select[data-filter="provider"]').select_option("mapillary")

    # A scope change CLEARS the window rather than carrying it into a domain
    # where it means something else. All three carriers of the filter have to
    # agree it is gone — the rendered rows, the URL, and the precision input,
    # which is the one that used to go on reading "10" after the table had
    # already re-filtered without it.
    expect(cov_min).to_have_value("")
    assert "cov=" not in page.url, page.url
    expect(rows).to_have_count(2)  # both Mapillary cities, unfiltered

    # The axis re-seeds to the scoped provider's own range (a deliberate
    # loosening of the fixed-axis rule: a scope change is a different gesture
    # from brushing), and every label moves with it.
    expect(legend).to_have_text("Grid coverage % — Mapillary")
    assert float(lo.get_attribute("min")) > 60, lo.get_attribute("min")
    expect(cov_min).to_have_attribute("aria-label", "Minimum Grid coverage % — Mapillary")
    expect(lo).to_have_attribute("aria-label", "Minimum Grid coverage % — Mapillary")

    # And the slider now reads Mapillary's column: neither city reaches 67% on
    # Mapillary (both sit at 66.7), so the honest answer is no rows. Reading
    # best-across here would keep Alpha City on GSV's 75 — a row whose own
    # Mapillary number contradicts the filter that returned it.
    cov_min.fill("67")
    expect(rows).to_have_count(0)

    assert errors == []


def test_a_scoped_window_survives_a_url_restore(page: Page, base_url):
    """Clearing on a scope CHANGE must not clear on a scope ARRIVAL. A shared
    link carries the field and the window together, so the pair is coherent on
    the way in and dropping it would discard the very thing being shared."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/grid.html?provider=mapillary&age=3~")

    rows = page.locator("#grid-tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Map Ville")  # 4.0 yrs; Alpha City 2.0
    expect(page.locator('input[data-filter="age"][data-bound="min"]')).to_have_value("3")
    expect(page.locator("#f-age-legend")).to_have_text("Median age (yrs) — Mapillary")

    assert errors == []


def test_distribution_strip_survives_on_the_driving_page(page: Page, base_url):
    """The strip was removed from the two pivoted pages, NOT from the chassis.
    driving.html keeps it, and keeps the behaviour it always had: a histogram
    of the ACTIVE SORT COLUMN over the CURRENTLY FILTERED rows, with the
    summary sentence as its accessible equivalent."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/driving.html")

    summary = page.locator("#distribution-strip .strip-summary")
    expect(summary).to_be_visible()

    # Re-sorting repoints the strip at the newly sorted column (coveragePct is
    # in driving.html's default Overview preset, so it is on screen).
    page.locator('th[data-key="coveragePct"] button').click()
    # Case-insensitive: the summary lowercases the column label in its
    # "No <label> values" form, which is what a fixture with few tracked
    # cities in view actually produces.
    expect(summary).to_contain_text(re.compile(r"grid coverage", re.I))

    # ...and driving.html renders none of the pivot's furniture.
    expect(page.locator(".control-histogram")).to_have_count(0)
    expect(page.locator(".table-sidebar")).to_have_count(0)
    expect(page.locator("thead th.th-group")).to_have_count(0)
    expect(page.locator("#driving-thead tr")).to_have_count(1)

    assert errors == []


@pytest.mark.parametrize("path", ["grid.html", "streets.html"])
def test_the_table_and_its_filters_are_on_the_first_screen(page: Page, base_url, path):
    """These two pages are instruments, not articles. A screen of preamble
    pushed both the table and the filter sidebar below the fold, so the lead is
    now one sentence and the rest lives in a closed disclosure. The assertion is
    the outcome, not the word count: the table's first row and the search box
    both have to be visible without scrolling."""
    errors = _capture_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/{path}")
    expect(page.locator("tbody tr").first).to_be_visible()

    top = page.evaluate(
        """() => ({
             table: document.querySelector('.streets-table-wrap').getBoundingClientRect().top,
             search: document.querySelector('#table-search').getBoundingClientRect().top,
             viewport: window.innerHeight,
           })"""
    )
    assert top["table"] < top["viewport"] / 2, f"table starts at {top['table']}px"
    assert top["search"] < top["viewport"] / 2, f"search starts at {top['search']}px"

    # The long explanation is still there, just closed.
    about = page.locator("details.page-about")
    expect(about).to_have_count(1)
    assert about.evaluate("el => el.open") is False
    expect(about.locator(".page-about-body")).to_be_hidden()
    about.locator("summary").click()
    expect(about.locator(".page-about-body")).to_be_visible()

    assert errors == []


@pytest.mark.parametrize("path", ["grid.html", "streets.html"])
def test_the_filter_sidebar_spans_the_full_viewport_height(page: Page, base_url, path):
    """ "Always there" means it does not scroll away, and "full extent" means it
    is a column rather than a short card with grey below it. Both come from the
    sidebar itself being the sticky, full-height panel — a flex chain through
    the <details> does not survive Chromium's ::details-content box."""
    errors = _capture_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/{path}")
    expect(page.locator("tbody tr").first).to_be_visible()

    box = page.evaluate(
        """() => {
             const a = document.querySelector('.table-sidebar');
             const c = getComputedStyle(a);
             return {h: a.getBoundingClientRect().height, viewport: window.innerHeight,
                     position: c.position, background: c.backgroundColor};
           }"""
    )
    assert box["position"] == "sticky"
    assert box["h"] > box["viewport"] * 0.85, f"sidebar is only {box['h']}px of {box['viewport']}px"
    # The container is the panel, so the height is visible rather than notional.
    assert box["background"] not in ("rgba(0, 0, 0, 0)", "transparent"), box["background"]

    # ...and it stays put when the table scrolls past it.
    page.mouse.wheel(0, 1200)
    page.wait_for_timeout(200)
    assert (
        page.evaluate("() => document.querySelector('.table-sidebar').getBoundingClientRect().top")
        < 100
    ), "sidebar scrolled away instead of sticking"

    assert errors == []


@pytest.mark.parametrize("path", ["grid.html", "streets.html"])
def test_filter_sidebar_sits_beside_the_table_and_collapses_on_narrow_screens(
    page: Page, base_url, path
):
    """Issue #250's layout: a ~280px filter column beside the table at desktop
    width, collapsing to a "Filters" disclosure below 900px. The one state that
    must be unreachable is "collapsed, then widened" — the summary is hidden at
    desktop width, so a panel left closed would strand filters that are in the
    URL and cannot be seen or changed."""
    errors = _capture_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/{path}")
    expect(page.locator("tbody tr").first).to_be_visible()

    aside = page.locator(".table-sidebar")
    table = page.locator(".streets-table-wrap")
    expect(aside).to_be_visible()
    # Beside, not above: the sidebar's right edge is left of the table's left.
    boxes = page.evaluate(
        """() => {
             const a = document.querySelector('.table-sidebar').getBoundingClientRect();
             const t = document.querySelector('.streets-table-wrap').getBoundingClientRect();
             return {aRight: a.right, tLeft: t.left, aTop: a.top, tTop: t.top};
           }"""
    )
    assert boxes["aRight"] <= boxes["tLeft"] + 1, "sidebar overlaps the table"
    assert abs(boxes["aTop"] - boxes["tTop"]) < 40, "sidebar is not on the table's row"
    # At this width the disclosure is a plain panel, with no toggle to find.
    expect(page.locator(".sidebar-disclosure > summary")).to_be_hidden()
    expect(table).to_be_visible()

    # Narrow: the summary becomes the toggle and closing it hides the filters.
    page.set_viewport_size({"width": 600, "height": 900})
    summary = page.locator(".sidebar-disclosure > summary")
    expect(summary).to_be_visible()
    expect(summary).to_have_text("Filters")
    expect(page.locator("#table-search")).to_be_visible()
    summary.click()
    expect(page.locator("#table-search")).to_be_hidden()

    # Widening re-opens it, rather than leaving a hidden panel with no toggle.
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.locator("#table-search")).to_be_visible()
    expect(page.locator(".sidebar-disclosure > summary")).to_be_hidden()

    assert errors == []


def test_driving_page_joins_the_plan_against_observed_imagery(page: Page, base_url):
    """
    The Driving page's whole reason to exist is the contradiction: Google's
    feed says Alphastate's campaign closed in 2019, while Alpha City's imagery
    was captured in 2024. If that row ever renders as a bare "campaign closed",
    the page is actively misleading — a closed plan entry would read as
    evidence the area was not driven, which is exactly the inference the real
    Israel data disproves.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/driving.html")

    expect(page.locator("#driving-table-wrap")).to_be_visible()
    row = page.locator("tbody tr", has=page.locator("th", has_text="Alpha City"))
    expect(row).to_contain_text("Driven, unplanned")

    # The archive's own thinness is stated, not left for the reader to assume.
    expect(page.locator("#driving-provenance")).to_contain_text("2026-04-20")
    expect(page.locator("#driving-provenance")).to_contain_text("driving distance")

    # A city Google has an open window for reads as such.
    zero = page.locator("tbody tr", has=page.locator("th", has_text="Zero City"))
    expect(zero).to_contain_text("Driving now")

    assert errors == []


def test_driving_page_lists_untracked_plan_areas_as_rows(page: Page, base_url):
    """
    The "Tracked?" column only carries information if untracked places are rows
    too — otherwise it reads "yes" on every row and answers nothing. Chubut is
    in Google's plan and has no city in the fixture, so it must appear as a row
    marked not tracked, with its observed columns empty rather than zeroed.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/driving.html")
    expect(page.locator("#driving-table-wrap")).to_be_visible()

    row = page.locator("tbody tr", has=page.locator("th", has_text="Chubut"))
    expect(row).to_contain_text("Not tracked")
    expect(row).to_contain_text("Driving now")
    # No run to open, so no link out — degrade, don't fabricate.
    expect(row.locator("th a")).to_have_count(0)

    # And the filter can isolate either universe.
    page.select_option("[data-filter='scope']", "area")
    expect(page.locator("tbody tr")).to_have_count(1)

    assert errors == []


def test_driving_page_shows_capture_history_and_plan_revisions(page: Page, base_url):
    """
    The two halves of "the past". The sparkline is the observable drive history
    (Alpha City was captured in 2018, 2020 and 2024 — three drives a single
    median age would hide), and the revision log is the archive's own reason to
    exist: Google overwrites the feed in place, so a Yes->No flip is
    unobservable to anyone who was not watching at the time.
    """
    errors = _capture_errors(page)
    page.goto(f"{base_url}/driving.html")
    expect(page.locator("#driving-table-wrap")).to_be_visible()

    row = page.locator("tbody tr", has=page.locator("th", has_text="Alpha City"))
    spark = row.locator(".spark-bar")
    expect(spark.first).to_be_attached()
    # 2018..2024 inclusive is seven year slots, four of them empty.
    assert spark.count() == 7

    revisions = page.locator("#driving-revisions")
    expect(revisions).to_be_visible()
    expect(revisions).to_contain_text("2026-04-10")
    expect(revisions).to_contain_text("1 campaign closed")
    expect(revisions).to_contain_text("Alphastate")

    assert errors == []


def test_driving_page_row_links_to_the_city_page(page: Page, base_url):
    """The artifact carries csv_filename precisely so a row can link out —
    city.html is addressed by run filename, not city_id."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/driving.html")
    expect(page.locator("#driving-table-wrap")).to_be_visible()

    page.locator("tbody th a").first.click()
    page.wait_for_url("**/city.html?file=**")

    assert errors == []


@pytest.mark.parametrize("path", ["grid.html", "streets.html", "driving.html"])
def test_city_name_cell_is_truncated_not_left_to_overflow(page: Page, base_url, path):
    """The committed fixture's city names are short ("Alpha City"), so this
    can't observe an actual ellipsis firing — it instead confirms the CSS rule
    that handles the real worldwide-frame names (60+ chars, issue #115) is
    actually wired onto the live cell, not just sitting unused in the
    stylesheet. Regression: this column used to have no width limit at all, so
    a single long production label could push the whole table into horizontal
    scroll even though the short-named fixture never triggered it."""
    errors = _capture_errors(page)
    page.goto(f"{base_url}/{path}")
    cell = page.locator("tbody th[scope='row']").first
    expect(cell).to_be_visible()
    style = cell.evaluate(
        "el => { const s = getComputedStyle(el); "
        "return {maxWidth: s.maxWidth, overflow: s.overflow, textOverflow: s.textOverflow}; }"
    )
    assert style["maxWidth"] not in ("none", ""), "city-name cell has no max-width set"
    assert style["overflow"] == "hidden"
    assert style["textOverflow"] == "ellipsis"

    assert errors == []


@pytest.mark.parametrize("path", ["grid.html", "streets.html", "driving.html"])
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
