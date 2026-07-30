// Offline unit tests for the pure helpers in streets.js — the street-level
// coverage page (issues #99/#155). Run with `npm test` (Node's built-in test
// runner) — no network, no jsdom.
//
// In the browser these helpers read shared globals from streetscape-utils.js;
// here we stub the three they touch. `document` is left undefined on purpose:
// streets.js only registers its DOMContentLoaded listener when one exists.

const test = require("node:test");
const assert = require("node:assert/strict");

global.PROVIDERS = {
  gsv: { label: "Google Street View" },
  mapillary: { label: "Mapillary" },
};
global.DEFAULT_STREET_NETWORK_TYPE = "drive";
global.STREET_NETWORK_LABELS = { drive: "Roads", all_public: "Roads + paths" };
global.streetNetworkLabel = (networkType) => {
  const key = networkType ?? global.DEFAULT_STREET_NETWORK_TYPE;
  return global.STREET_NETWORK_LABELS[key] ?? key;
};
global.coverageColor = (pct) => `coverage(${pct})`;
global.escapeHtml = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
global.adaptCitiesPayload = (raw, provider) => ({
  cities: (raw?.cities ?? [])
    .filter((c) => c.provider === provider)
    .map((c) => ({ ...c })),
});

const {
  cityLabel,
  indexCitiesByProvider,
  toRowModel,
  sortRows,
  num,
  walkRowHtml,
  STREET_COLUMNS,
  DEFAULT_SORT,
} = require("../streets.js");

// --- cityLabel -------------------------------------------------------------

test("cityLabel: joins city, state, and country", () => {
  assert.equal(
    cityLabel({
      city: "Seattle",
      state: { name: "Washington" },
      country: { name: "United States" },
    }),
    "Seattle, Washington, United States"
  );
});

test("cityLabel: does not repeat a state that equals the city name", () => {
  // City-states (Singapore, Luxembourg) geocode with name === state.
  assert.equal(
    cityLabel({ city: "Singapore", state: { name: "Singapore" }, country: { name: "Singapore" } }),
    "Singapore, Singapore"
  );
});

test("cityLabel: falls back through state/country to Unknown", () => {
  assert.equal(cityLabel({}), "Unknown");
  assert.equal(cityLabel({ state: { name: "Bavaria" } }), "Bavaria");
});

// --- indexCitiesByProvider -------------------------------------------------

const RAW_CITIES = {
  cities: [
    { provider: "gsv", city_id: "seattle--wa", city: "Seattle" },
    { provider: "gsv", city_id: "bend--or", city: "Bend" },
    { provider: "mapillary", city_id: "seattle--wa", city: "Seattle" },
  ],
};

test("indexCitiesByProvider: keys by provider + city_id, so providers don't collide", () => {
  const index = indexCitiesByProvider(RAW_CITIES, ["gsv", "mapillary"]);
  assert.equal(index.get("gsv|seattle--wa").city, "Seattle");
  assert.equal(index.get("mapillary|seattle--wa").city, "Seattle");
  assert.equal(index.get("gsv|bend--or").city, "Bend");
  // 3 provider-keyed entries + 2 bare city_id entries (the name fallback;
  // seattle--wa appears under both providers but is indexed bare only once)
  assert.equal(index.size, 5);
});

test("indexCitiesByProvider: only indexes the providers asked for", () => {
  const index = indexCitiesByProvider(RAW_CITIES, ["gsv"]);
  assert.equal(index.get("mapillary|seattle--wa"), undefined);
  // 2 provider-keyed entries + 2 bare city_id entries (the name fallback)
  assert.equal(index.size, 4);
});

test("indexCitiesByProvider: also indexes by bare city_id for the name fallback", () => {
  const index = indexCitiesByProvider(RAW_CITIES, ["gsv"]);
  assert.equal(index.get("seattle--wa").city, "Seattle");
});

test("toRowModel: uses the label fallback but never its link", () => {
  // A city walked by a provider it has no grid run for: name it properly,
  // but do NOT link to another provider's run — city.html derives its
  // provider from the filename and would open the wrong series.
  const row = toRowModel(
    { city_id: "adrian--or", provider: "mapillary" },
    null,
    { city: "Adrian", state: { name: "Oregon" }, data_file: { filename: "adrian_gsv.csv.gz" } }
  );
  assert.equal(row.label, "Adrian, Oregon");
  assert.equal(row.filename, null);
});

test("indexCitiesByProvider: a missing aggregate yields an empty index, not a throw", () => {
  // The page still renders its table (unlinked) when cities.json.gz fails.
  assert.equal(indexCitiesByProvider(null, ["gsv"]).size, 0);
});

// --- toRowModel ------------------------------------------------------------

test("toRowModel: flattens walk + joined city into the shape the sorter reads", () => {
  const row = toRowModel(
    {
      city_id: "seattle--wa",
      provider: "gsv",
      run_date: "2026-07-26",
      spacing_m: 15,
      coverage_pct_by_length: 98.4,
      edges: 100,
      edges_fully_covered: 90,
    },
    { city: "Seattle", data_file: { filename: "seattle.csv.gz" } }
  );
  assert.equal(row.label, "Seattle");
  assert.equal(row.providerLabel, "Google Street View");
  assert.equal(row.pct, 98.4);
  assert.equal(row.filename, "seattle.csv.gz");
});

test("toRowModel: falls back to city_id and nulls when the join missed", () => {
  const row = toRowModel({ city_id: "ghost--xx", provider: "gsv" }, null);
  assert.equal(row.label, "ghost--xx");
  assert.equal(row.filename, null);
  assert.equal(row.pct, null);
  assert.equal(row.pctAny, null);
});

test("toRowModel: any-imagery coverage is null on walks predating the field", () => {
  // Pre-existing manifests carry no coverage_pct_by_length_any; the column
  // must read "no data", never silently mirror the 360° number.
  const row = toRowModel(
    { city_id: "x", provider: "gsv", coverage_pct_by_length: 80 },
    null
  );
  assert.equal(row.pctAny, null);
});

// --- sortRows --------------------------------------------------------------

const ROWS = [
  { cityId: "c", label: "Cee", pct: 50, edges: 5, runDate: "2026-01-01" },
  { cityId: "a", label: "Aye", pct: null, edges: 90, runDate: null },
  { cityId: "b", label: "Bee", pct: 98.4, edges: 10, runDate: "2026-05-05" },
  { cityId: "d", label: "Dee", pct: 50, edges: 1, runDate: "2026-03-03" },
];

test("sortRows: numeric desc puts the best first, nulls last", () => {
  assert.deepEqual(sortRows(ROWS, "pct", "desc").map((r) => r.cityId), ["b", "c", "d", "a"]);
});

test("sortRows: nulls stay last when the direction flips (absent is not small)", () => {
  const ids = sortRows(ROWS, "pct", "asc").map((r) => r.cityId);
  assert.equal(ids[ids.length - 1], "a");
  assert.deepEqual(ids, ["c", "d", "b", "a"]);
});

test("sortRows: ties break on city_id, so re-sorting is stable", () => {
  // "c" and "d" both sit at 50% — they must keep the same relative order
  // in both directions rather than swapping on every re-sort.
  assert.deepEqual(sortRows(ROWS, "pct", "desc").slice(1, 3).map((r) => r.cityId), ["c", "d"]);
  assert.deepEqual(sortRows(ROWS, "pct", "asc").slice(0, 2).map((r) => r.cityId), ["c", "d"]);
});

test("sortRows: text columns sort lexically in both directions", () => {
  assert.deepEqual(sortRows(ROWS, "label", "asc").map((r) => r.cityId), ["a", "b", "c", "d"]);
  assert.deepEqual(sortRows(ROWS, "label", "desc").map((r) => r.cityId), ["d", "c", "b", "a"]);
});

test("sortRows: an unknown key falls back to the first column, never throws", () => {
  assert.equal(sortRows(ROWS, "nope", "asc").length, ROWS.length);
});

test("sortRows: does not mutate its input", () => {
  const before = ROWS.map((r) => r.cityId);
  sortRows(ROWS, "pct", "asc");
  assert.deepEqual(ROWS.map((r) => r.cityId), before);
});

test("every sortable column key exists on a row model", () => {
  // Guards the html/js seam: a th[data-key] with no matching model field
  // would silently sort every row as null.
  const row = toRowModel(
    { city_id: "x", provider: "gsv", run_date: "2026-01-01", spacing_m: 15 },
    { city: "X" }
  );
  for (const col of STREET_COLUMNS) {
    assert.ok(col.key in row, `row model is missing ${col.key}`);
  }
  assert.ok(STREET_COLUMNS.some((c) => c.key === DEFAULT_SORT.key));
});

test("a row renders one cell per column, plus the link cell", () => {
  // The <thead> lives in streets.html and the <tr> is built here, so adding a
  // column to one and not the other misaligns every row — invisible until the
  // page is opened in a browser, which no test does.
  const html = walkRowHtml(
    toRowModel(
      { city_id: "x", provider: "gsv", network_type: "all_public", run_date: "2026-01-01" },
      { city: "X" }
    )
  );
  const cells = (html.match(/<t[hd][\s>]/g) || []).length;
  assert.equal(cells, STREET_COLUMNS.length + 1);
});

test("toRowModel: labels the network, defaulting a tokenless walk to roads", () => {
  const broad = toRowModel(
    { city_id: "x", provider: "gsv", network_type: "all_public" },
    { city: "X" }
  );
  assert.equal(broad.networkType, "all_public");
  assert.equal(broad.networkLabel, "Roads + paths");

  // A walk published before network types existed carries no field at all.
  const legacy = toRowModel({ city_id: "x", provider: "gsv" }, { city: "X" });
  assert.equal(legacy.networkType, "drive");
  assert.equal(legacy.networkLabel, "Roads");
});

// --- num -------------------------------------------------------------------

test("num: renders an em dash for null/undefined rather than 'null'", () => {
  assert.equal(num(null), "—");
  assert.equal(num(undefined), "—");
  assert.equal(num(0), "0");
});

// --- walkRowHtml -----------------------------------------------------------

const SEATTLE_WALK = {
  city_id: "seattle--washington--united-states",
  provider: "gsv",
  run_date: "2026-07-26",
  spacing_m: 15,
  coverage_pct_by_length: 98.4,
  edges: 33597,
  edges_fully_covered: 32391,
};

test("walkRowHtml: links to the city page using the aggregate's run filename", () => {
  const html = walkRowHtml(
    toRowModel(SEATTLE_WALK, {
      city: "Seattle",
      state: { name: "Washington" },
      country: { name: "United States" },
      data_file: { filename: "seattle--wa_width_1_height_1_step_20.csv.gz" },
    })
  );
  assert.match(html, /Seattle, Washington, United States/);
  assert.match(html, /Google Street View/);
  assert.match(html, /98\.4%/);
  assert.match(
    html,
    /href="city\.html\?file=seattle--wa_width_1_height_1_step_20\.csv\.gz&network=drive"/
  );
});

test("walkRowHtml: the link carries this row's network type, not the default", () => {
  // city.html selects the walk to draw by network type and defaults to 'drive',
  // so a broad row whose link omits ?network= opens a DIFFERENT walk — or, for a
  // city walked only broadly, falls back to the grid-attribution artifact.
  const html = walkRowHtml(
    toRowModel(
      { ...SEATTLE_WALK, network_type: "all_public" },
      { city: "Seattle", data_file: { filename: "seattle--wa_width_1_height_1_step_20.csv.gz" } }
    )
  );
  assert.match(html, /&network=all_public"/);
});

test("walkRowHtml: a city missing from the aggregate still renders, without a link", () => {
  // The manifest is keyed by city_id and can name a city whose runs are not
  // published — the row must degrade, not disappear or throw.
  const html = walkRowHtml(toRowModel(SEATTLE_WALK, null));
  assert.match(html, /seattle--washington--united-states/);
  assert.doesNotMatch(html, /href="city\.html/);
});

test("walkRowHtml: null stats render em dashes, and no coverage bar", () => {
  const html = walkRowHtml(
    toRowModel(
      { city_id: "x", provider: "gsv", coverage_pct_by_length: null, spacing_m: null },
      null
    )
  );
  assert.match(html, /—/);
  assert.doesNotMatch(html, /coverage-bar/);
});

test("walkRowHtml: renders both the 360° and any-imagery coverage cells", () => {
  const html = walkRowHtml(
    toRowModel({ ...SEATTLE_WALK, provider: "mapillary", coverage_pct_by_length: 61.2, coverage_pct_by_length_any: 74.8 }, null)
  );
  assert.match(html, /61\.2%/);
  assert.match(html, /74\.8%/);
  assert.match(html, /Mapillary/);
});

test("walkRowHtml: the coverage bar width is clamped to 0–100%", () => {
  const html = walkRowHtml(toRowModel({ ...SEATTLE_WALK, coverage_pct_by_length: 137 }, null));
  assert.match(html, /width:100%/);
});

test("walkRowHtml: city names are HTML-escaped (OSM data is publicly editable)", () => {
  const html = walkRowHtml(toRowModel(SEATTLE_WALK, { city: "<script>alert(1)</script>" }));
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});
