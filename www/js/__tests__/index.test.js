// Offline unit tests for the overview map's pure helpers (index.js). Run with
// `npm test` (Node's built-in test runner) — no network, no jsdom.
//
// index.js is a page script, not a module: it builds a Leaflet map and reads
// the URL at load time. So unlike grid.js/streets.js — which only touch the
// DOM behind a `typeof document` guard — the browser globals it uses have to
// be stubbed BEFORE the require below, not inside the tests. Everything
// stubbed here is the minimum that load needs (L.map/L.tileLayer, a window
// with a location, a document that can register a listener); the tests then
// install richer fakes per case.
//
// The shared helpers come from the real streetscape-utils.js rather than
// stubs, because two of these tests are about how index.js and
// adaptCityRecord agree on what the aggregate actually contains — a stubbed
// adapter could not disagree with them.

const test = require("node:test");
const assert = require("node:assert/strict");

// index.js reads these as browser globals; the tests also name two of them
// directly, so keep one binding of each rather than two spellings that could
// drift apart.
const streetscapeUtils = require("../streetscape-utils.js");
Object.assign(global, streetscapeUtils);
const { PROVIDERS, adaptCityRecord } = streetscapeUtils;

/** Minimal Leaflet: index.js builds a map and one tile layer at load. */
const mapStub = {
  on() {},
  closePopup() {},
  fitBounds() {},
  attributionControl: { addAttribution() {}, removeAttribution() {} },
};
global.L = {
  map: () => ({ setView: () => mapStub }),
  tileLayer: () => ({ addTo() {} }),
};
global.window = { location: new URL("https://example.test/index.html") };
global.history = { replaceState() {} };
global.document = { addEventListener() {} };
global.Chart = class { destroy() {} };

const {
  createTooltip,
  providerToggleHtml,
  initProviderToggle,
} = require("../index.js");

// ── DOM fakes ───────────────────────────────────────────────────────────────
//
// createTooltip builds its content as an innerHTML string on a container and
// then appends two child elements, so the assertions read that string. The
// element fake carries only what the function touches.

/** @returns {Object} A stand-in for a DOM element. */
function fakeElement(tag) {
  return {
    tag,
    style: {},
    children: [],
    innerHTML: "",
    className: "",
    textContent: "",
    setAttribute() {},
    appendChild(child) {
      this.children.push(child);
    },
  };
}

/** Install a document that can create elements; returns a restore function. */
function withFakeDocument(extra = {}) {
  const previous = global.document;
  global.document = {
    addEventListener() {},
    createElement: (tag) => fakeElement(tag),
    ...extra,
  };
  return () => {
    global.document = previous;
  };
}

// ── Fixtures ────────────────────────────────────────────────────────────────
//
// Built as aggregate v3 records and run through the REAL adaptCityRecord, so
// the popup is asked exactly what the published file provides. In particular
// `unique_google_panos` is left off the blocks that do not publish it, which
// is how production cities.json.gz stores it: the key is ABSENT, not null.

/** @returns {Object} A v3 aggregate record with one provider block. */
function v3Record(provider, latest) {
  return {
    city_id: "bend--or",
    city: {
      name: "Bend",
      state: { name: "Oregon" },
      country: { name: "United States" },
      center: { lat: 44, lon: -121 },
      bounds: { min_lat: 43.9, max_lat: 44.1, min_lon: -121.4, max_lon: -121.2 },
    },
    providers: {
      [provider]: {
        latest: {
          run_date: "2026-07-05",
          search_area_km2: 25,
          coverage_rate_percent: 60,
          all_panos_age_stats: { median_pano_age_years: 4 },
          histogram_of_capture_dates_by_year: { all_panos: { counts: { 2024: 3 } } },
          data_file: { filename: "bend--or_width_5000_height_5000_step_20_2026-07-05.csv.gz" },
          json_file: { filename: "bend--or.json.gz" },
          ...latest,
        },
        runs: [],
        change: null,
      },
    },
  };
}

const GSV_CITY = () =>
  adaptCityRecord(
    v3Record("gsv", {
      panorama_counts: { unique_panos: 100, unique_google_panos: 80 },
      google_panos_age_stats: { median_pano_age_years: 3 },
      histogram_of_capture_dates_by_year: {
        google_panos: { counts: { 2024: 3 } },
        all_panos: { counts: { 2024: 4 } },
      },
    }),
    "gsv"
  );

// The #93 archival imports: a GSV run that recorded no copyright at all, so
// there is no Google subset to publish and no unique_google_panos key.
const ARCHIVAL_GSV_CITY = () =>
  adaptCityRecord(
    v3Record("gsv", {
      panorama_counts: { unique_panos: 100 },
      copyright_info_available: false,
    }),
    "gsv"
  );

const MAPILLARY_CITY = () =>
  adaptCityRecord(
    v3Record("mapillary", {
      panorama_counts: { unique_panos: 250 },
      any_imagery_coverage_rate_percent: 74.5,
    }),
    "mapillary"
  );

// --- createTooltip: the Google-panos line is a DATA test -------------------

test("createTooltip: a provider that publishes a Google subset gets the Google line", () => {
  const restore = withFakeDocument();
  try {
    const html = createTooltip(GSV_CITY()).innerHTML;
    assert.match(html, /Total Panoramas: 100/);
    assert.match(html, /Google Panoramas: 80 \(80\.0%\)/);
  } finally {
    restore();
  }
});

test("createTooltip: a census provider publishes no Google subset, so no Google line", () => {
  const restore = withFakeDocument();
  try {
    const html = createTooltip(MAPILLARY_CITY()).innerHTML;
    assert.match(html, /Total Panoramas: 250/);
    assert.doesNotMatch(html, /Google Panoramas/);
  } finally {
    restore();
  }
});

test("createTooltip: an archival GSV run has no Google subset either — and must not throw", () => {
  // The regression guard for "finish the job by reading hasCopyrightFilter
  // here". Copyright availability varies per RUN within GSV: nine production
  // cities (the #93 archival imports) carry copyright_info_available: false
  // and no unique_google_panos, so a flag-driven branch would reach
  // .toLocaleString() on undefined and take the whole popup down. The
  // provider IS copyright-filtered; this record is not.
  const city = ARCHIVAL_GSV_CITY();
  assert.equal(PROVIDERS[city.provider].hasCopyrightFilter, true);
  assert.equal(city.copyright_info_available, false);
  assert.equal(city.panorama_counts.unique_google_panos, undefined);

  const restore = withFakeDocument();
  try {
    const html = createTooltip(city).innerHTML;
    assert.match(html, /Total Panoramas: 100/);
    assert.doesNotMatch(html, /Google Panoramas/);
  } finally {
    restore();
  }
});

// --- createTooltip: any-imagery and the "(360°)" suffix --------------------

test("createTooltip: the any-imagery line follows the widening, not the provider name", () => {
  const restore = withFakeDocument();
  try {
    // Mapillary: flat imagery widens the footprint, so the line appears.
    assert.match(createTooltip(MAPILLARY_CITY()).innerHTML, /Any Imagery: 74\.5% \(incl\. flat\)/);
    // GSV: adaptCityRecord falls the any-imagery rate back to the 360° rate,
    // so the difference is exactly zero and the line would only repeat Grid
    // Coverage.
    assert.doesNotMatch(createTooltip(GSV_CITY()).innerHTML, /Any Imagery/);
  } finally {
    restore();
  }
});

test("createTooltip: the (360°) coverage suffix is driven by hasFlatImagery", () => {
  const restore = withFakeDocument();
  try {
    // The one label with no value to test: it says what the number EXCLUDES,
    // which is a property of the provider rather than of this record.
    assert.match(createTooltip(MAPILLARY_CITY()).innerHTML, /60\.0% of search points \(360°\)/);
    assert.match(createTooltip(GSV_CITY()).innerHTML, /60\.0% of search points</);
    assert.doesNotMatch(createTooltip(GSV_CITY()).innerHTML, /\(360°\)/);
  } finally {
    restore();
  }
});

// --- providerToggleHtml: the radio group is the registry -------------------

test("providerToggleHtml: one radio per registered provider, exactly one checked", () => {
  const html = providerToggleHtml(PROVIDERS, "gsv");
  for (const [key, p] of Object.entries(PROVIDERS)) {
    assert.match(html, new RegExp(`value="${key}"`), key);
    assert.match(html, new RegExp(escapeRegExp(p.label)), key);
  }
  assert.equal((html.match(/<input /g) || []).length, Object.keys(PROVIDERS).length);
  assert.equal((html.match(/ checked/g) || []).length, 1);
  assert.match(html, /value="gsv" checked/);
});

test("providerToggleHtml: a third provider is checkable — the defect a hardcoded pair had", () => {
  // With two hardcoded radios, ?provider=thirdparty (isKnownProvider says yes,
  // so currentProvider becomes it) left the group with NOTHING checked: a
  // keyboard user tabbing in lands on the first option and their first
  // arrow-press silently switches provider.
  const registry = {
    ...PROVIDERS,
    thirdparty: { label: "Third Party", description: "A third census provider" },
  };
  const html = providerToggleHtml(registry, "thirdparty");
  assert.equal((html.match(/<input /g) || []).length, 3);
  assert.equal((html.match(/ checked/g) || []).length, 1);
  assert.match(html, /value="thirdparty" checked/);
  assert.match(html, /title="A third census provider"/);
});

test("providerToggleHtml: labels and descriptions are escaped, and an absent description emits no title", () => {
  const html = providerToggleHtml(
    { evil: { label: '<script>alert(1)</script>', description: 'a "quoted" one' }, bare: { label: "Bare" } },
    "bare"
  );
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /title="a &quot;quoted&quot; one"/);
  // The bare entry gets no tooltip at all rather than title="".
  assert.doesNotMatch(html, /title=""/);
  assert.match(html, /<span>Bare<\/span>/);
});

test("providerToggleHtml: every registered provider carries the description the toggle renders", () => {
  // A provider registered without one still gets a radio (above), but the
  // tooltip explaining what its data IS is the toggle's only affordance.
  for (const [key, p] of Object.entries(PROVIDERS)) {
    assert.equal(typeof p.description, "string", key);
    assert.ok(p.description.length > 0, key);
  }
});

// --- initProviderToggle: renders into the fieldset, keeps the legend -------

test("initProviderToggle: appends the options after the legend and wires change events", () => {
  const inserted = [];
  const radios = [
    { value: "gsv", checked: false, addEventListener(ev, fn) { this[ev] = fn; } },
    { value: "mapillary", checked: false, addEventListener(ev, fn) { this[ev] = fn; } },
  ];
  const restore = withFakeDocument({
    getElementById: (id) =>
      id === "provider-toggle"
        ? { insertAdjacentHTML: (pos, html) => inserted.push([pos, html]) }
        : null,
    querySelectorAll: () => radios,
  });
  try {
    initProviderToggle();
    // "beforeend" so the visually-hidden <legend> — the group's accessible
    // name — is not replaced by the render.
    assert.equal(inserted.length, 1);
    assert.equal(inserted[0][0], "beforeend");
    assert.match(inserted[0][1], /name="provider"/);
    // The default provider (no ?provider=) is reflected on the live radios.
    assert.equal(radios[0].checked, true);
    assert.equal(radios[1].checked, false);
    // And each radio can drive a provider switch.
    assert.equal(typeof radios[0].change, "function");
    assert.equal(typeof radios[1].change, "function");
  } finally {
    restore();
  }
});

test("initProviderToggle: a page with no fieldset still wires whatever radios exist", () => {
  // The renderer is additive, not load-bearing: getElementById returning null
  // must not throw before the listeners are attached.
  const radios = [{ value: "gsv", checked: false, addEventListener() {} }];
  const restore = withFakeDocument({
    getElementById: () => null,
    querySelectorAll: () => radios,
  });
  try {
    initProviderToggle();
    assert.equal(radios[0].checked, true);
  } finally {
    restore();
  }
});

/** Escape a literal string for use inside a RegExp. */
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
