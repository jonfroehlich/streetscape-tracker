// Offline unit tests for the pure helpers in grid.js — the Grid Coverage
// table page. Run with `npm test` (Node's built-in test runner) — no network,
// no jsdom. `document` is left undefined on purpose: grid.js only registers
// its DOMContentLoaded listener when one exists.

const test = require("node:test");
const assert = require("node:assert/strict");

global.PROVIDERS = {
  gsv: { label: "Google Street View" },
  mapillary: { label: "Mapillary" },
};
global.coverageColor = (pct) => `coverage(${pct})`;
global.escapeHtml = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// grid.js reads the shared table machinery as browser globals — mirror that
// here (must precede the grid.js require).
Object.assign(global, require("../table-utils.js"));

const {
  gridRowModel,
  gridRowHtml,
  GRID_COLUMNS,
  GRID_DEFAULT_SORT,
} = require("../grid.js");

const SEATTLE = {
  provider: "gsv",
  city_id: "seattle--wa",
  city: "Seattle",
  state: { name: "Washington" },
  country: { name: "United States" },
  coverage_rate_percent: 51.2,
  any_imagery_coverage_rate_percent: 51.2,
  pano_age_stats: { median_pano_age_years: 3.4 },
  pano_count: 41234,
  latest_run_date: "2026-07-05",
  runs: [{ run_date: "2025-01-17" }, { run_date: "2026-07-05" }],
  data_file: { filename: "seattle--wa_width_5000_height_5000_step_20_2026-07-05.csv.gz" },
};

// --- gridRowModel -----------------------------------------------------------

test("gridRowModel: flattens an adapted record into the sorter's shape", () => {
  const row = gridRowModel(SEATTLE);
  assert.equal(row.cityId, "seattle--wa");
  assert.equal(row.label, "Seattle, Washington, United States");
  assert.equal(row.providerLabel, "Google Street View");
  assert.equal(row.pct, 51.2);
  assert.equal(row.medianAge, 3.4);
  assert.equal(row.panos, 41234);
  assert.equal(row.collected, "2026-07-05");
  assert.equal(row.snapshots, 2);
  assert.equal(row.filename, SEATTLE.data_file.filename);
});

test("gridRowModel: missing stats become nulls, never NaN or 'undefined'", () => {
  const row = gridRowModel({ provider: "mapillary", city_id: "x--y", city: "X" });
  assert.equal(row.pct, null);
  assert.equal(row.pctAny, null);
  assert.equal(row.medianAge, null);
  assert.equal(row.panos, null);
  assert.equal(row.collected, null);
  assert.equal(row.snapshots, null); // zero runs reads as absent, not "0"
  assert.equal(row.filename, null);
});

test("every sortable column key exists on a row model", () => {
  // Guards the html/js seam: a th[data-key] with no matching model field
  // would silently sort every row as null.
  const row = gridRowModel(SEATTLE);
  for (const col of GRID_COLUMNS) {
    assert.ok(col.key in row, `row model is missing ${col.key}`);
  }
  assert.ok(GRID_COLUMNS.some((c) => c.key === GRID_DEFAULT_SORT.key));
});

// --- gridRowHtml ------------------------------------------------------------

test("gridRowHtml: one cell per column plus the link cell, linking to city.html", () => {
  const html = gridRowHtml(gridRowModel(SEATTLE));
  const cells = (html.match(/<t[hd][\s>]/g) || []).length;
  assert.equal(cells, GRID_COLUMNS.length + 1);
  assert.match(
    html,
    /href="city\.html\?file=seattle--wa_width_5000_height_5000_step_20_2026-07-05\.csv\.gz"/
  );
  assert.match(html, /51\.2%/);
  assert.match(html, /3\.4 yrs/);
});

test("gridRowHtml: null stats render em dashes and no link", () => {
  const html = gridRowHtml(gridRowModel({ provider: "gsv", city_id: "x--y", city: "X" }));
  assert.match(html, /—/);
  assert.doesNotMatch(html, /href="city\.html/);
  assert.doesNotMatch(html, /coverage-bar/);
});

test("gridRowHtml: city names are HTML-escaped (OSM data is publicly editable)", () => {
  const html = gridRowHtml(
    gridRowModel({ provider: "gsv", city_id: "x--y", city: "<script>alert(1)</script>" })
  );
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});
