// Offline unit tests for the exploration chassis in table-controls.js
// (issue #188): search, structured filters, column presets, URL round-trip,
// and the distribution strip's bucketing.
//
// Run with `npm test` (Node's built-in test runner) — no network, no jsdom.
// In the browser this module reads formatCellNumber from table-utils.js; here
// we stub it. The DOM wiring in createTableControls is covered by the browser
// e2e smoke test instead (tests/e2e/test_smoke.py).

const test = require("node:test");
const assert = require("node:assert/strict");

global.formatCellNumber = (v, digits = 0) =>
  v == null ? "—" : v.toFixed(digits);

const {
  foldForSearch,
  matchesSearch,
  isFilterUnset,
  rowPassesFilter,
  applyFilters,
  resolveVisibleColumns,
  parseTableState,
  serializeTableState,
  bucketCountFor,
  histogramBuckets,
  medianOf,
  formatStripSummary,
  controlsHtml,
} = require("../table-controls.js");

// --- search -----------------------------------------------------------------

test("foldForSearch: strips accents and case so the worldwide frame is searchable", () => {
  // Issue #115 put non-ASCII city names in the table; "avila" must find "Ávila".
  assert.equal(foldForSearch("Ávila"), "avila");
  assert.equal(foldForSearch("Córdoba"), "cordoba");
  assert.equal(foldForSearch(null), "");
  assert.equal(foldForSearch(42), "42");
});

const SEARCH_FIELDS = ["label", "providerLabel"];

test("matchesSearch: every term must match, but any field may supply it", () => {
  const row = { label: "Seattle, Washington", providerLabel: "Mapillary" };
  assert.ok(matchesSearch(row, SEARCH_FIELDS, "seattle mapillary"));
  assert.ok(matchesSearch(row, SEARCH_FIELDS, "SEATTLE"));
  assert.ok(!matchesSearch(row, SEARCH_FIELDS, "seattle google"));
});

test("matchesSearch: a blank or whitespace query matches everything", () => {
  const row = { label: "Seattle", providerLabel: "Mapillary" };
  assert.ok(matchesSearch(row, SEARCH_FIELDS, ""));
  assert.ok(matchesSearch(row, SEARCH_FIELDS, "   "));
});

// --- filters ----------------------------------------------------------------

const FILTERS = [
  {
    key: "provider",
    label: "Provider",
    type: "select",
    options: [
      { value: "gsv", label: "GSV" },
      { value: "mapillary", label: "Mapillary" },
    ],
    test: (row, value) => row.provider === value,
  },
  { key: "cov", label: "Coverage", type: "range", field: "pct", min: 0, max: 100 },
  { key: "changed", label: "Has Δ", type: "boolean", test: (row) => row.delta != null },
];

const ROWS = [
  { cityId: "a", provider: "gsv", pct: 90, delta: 1.2 },
  { cityId: "b", provider: "mapillary", pct: 10, delta: null },
  { cityId: "c", provider: "gsv", pct: null, delta: null },
];

test("isFilterUnset: blanks, unchecked boxes and empty ranges narrow nothing", () => {
  assert.ok(isFilterUnset(FILTERS[0], ""));
  assert.ok(isFilterUnset(FILTERS[0], null));
  assert.ok(isFilterUnset(FILTERS[2], false));
  assert.ok(isFilterUnset(FILTERS[1], { min: null, max: null }));
  assert.ok(!isFilterUnset(FILTERS[1], { min: 10, max: null }));
});

test("rowPassesFilter: a range EXCLUDES rows with no measured value", () => {
  // Same posture as sortRowsBy sinking nulls in both directions: an unmeasured
  // city is absent, not zero. "Coverage 0–50" must not sweep in the row whose
  // coverage was never recorded.
  const range = FILTERS[1];
  assert.ok(!rowPassesFilter(range, ROWS[2], { min: 0, max: 50 }));
  assert.ok(rowPassesFilter(range, ROWS[1], { min: 0, max: 50 }));
  // ...but an UNSET range leaves it alone.
  assert.ok(rowPassesFilter(range, ROWS[2], { min: null, max: null }));
});

test("rowPassesFilter: range bounds are inclusive and one-sided bounds work", () => {
  const range = FILTERS[1];
  assert.ok(rowPassesFilter(range, { pct: 10 }, { min: 10, max: 90 }));
  assert.ok(rowPassesFilter(range, { pct: 90 }, { min: 10, max: 90 }));
  assert.ok(rowPassesFilter(range, { pct: 95 }, { min: 90, max: null }));
  assert.ok(!rowPassesFilter(range, { pct: 95 }, { min: null, max: 90 }));
});

test("applyFilters: search and filters compose, and the input is untouched", () => {
  const before = ROWS.map((r) => r.cityId);
  const out = applyFilters(ROWS, {
    filters: FILTERS,
    values: { provider: "gsv" },
    query: "",
    searchFields: [],
  });
  assert.deepEqual(out.map((r) => r.cityId), ["a", "c"]);
  assert.deepEqual(ROWS.map((r) => r.cityId), before);
});

test("applyFilters: a boolean filter keeps only rows its test accepts", () => {
  const out = applyFilters(ROWS, {
    filters: FILTERS,
    values: { changed: true },
    query: "",
    searchFields: [],
  });
  assert.deepEqual(out.map((r) => r.cityId), ["a"]);
});

// --- column presets ---------------------------------------------------------

const COLUMNS = [
  { key: "label", label: "City", always: true },
  { key: "provider", label: "Provider" },
  { key: "pct", label: "Coverage" },
  { key: "km", label: "Street km" },
  { key: "actions", label: "", sortable: false, always: true },
];

const PRESETS = [
  { id: "overview", label: "Overview", columns: ["provider", "pct"] },
  { id: "km", label: "Kilometres", columns: ["pct", "km"] },
];

test("resolveVisibleColumns: a preset yields its columns plus the always-on ones", () => {
  const keys = resolveVisibleColumns(COLUMNS, PRESETS, "overview", null).map((c) => c.key);
  assert.deepEqual(keys, ["label", "provider", "pct", "actions"]);
});

test("resolveVisibleColumns: explicit picker keys win over the preset", () => {
  const keys = resolveVisibleColumns(COLUMNS, PRESETS, "overview", ["km"]).map((c) => c.key);
  assert.deepEqual(keys, ["label", "km", "actions"]);
});

test("resolveVisibleColumns: order follows the canonical list, not the URL", () => {
  // ?cols=km,provider must still render Provider before Street km.
  const keys = resolveVisibleColumns(COLUMNS, PRESETS, null, ["km", "provider"]).map((c) => c.key);
  assert.deepEqual(keys, ["label", "provider", "km", "actions"]);
});

test("resolveVisibleColumns: unknown preset or column degrades instead of throwing", () => {
  // A stale link from before a column was renamed should still open a page.
  assert.deepEqual(
    resolveVisibleColumns(COLUMNS, PRESETS, "nope", null).map((c) => c.key),
    ["label", "provider", "pct", "actions"]
  );
  assert.deepEqual(
    resolveVisibleColumns(COLUMNS, PRESETS, null, ["gone"]).map((c) => c.key),
    ["label", "actions"]
  );
});

test("resolveVisibleColumns: an explicit empty selection shows only the always-on columns", () => {
  // `[]` is the picker having every optional box unchecked — a real, distinct
  // selection, not "no override, fall back to the preset". Confusing this
  // with `null` would silently re-show the preset's columns while the
  // checkboxes still read unchecked.
  const keys = resolveVisibleColumns(COLUMNS, PRESETS, "overview", []).map((c) => c.key);
  assert.deepEqual(keys, ["label", "actions"]);
});

// --- URL round-trip ---------------------------------------------------------

test("parseTableState/serializeTableState: a full view round-trips", () => {
  const state = {
    query: "seattle",
    preset: "km",
    cols: ["pct", "km"],
    sort: { key: "pct", dir: "asc" },
    values: { provider: "gsv", cov: { min: 10, max: 90 }, changed: true },
  };
  const qs = serializeTableState(state, { filters: FILTERS, defaultPreset: "overview" });
  const parsed = parseTableState(qs, { filters: FILTERS });
  assert.equal(parsed.query, "seattle");
  assert.equal(parsed.preset, "km");
  assert.deepEqual(parsed.cols, ["pct", "km"]);
  assert.deepEqual(parsed.sort, { key: "pct", dir: "asc" });
  assert.deepEqual(parsed.values, state.values);
});

test("serializeTableState: an untouched view writes nothing but the sort", () => {
  // Default state must not litter the address bar with redundant parameters.
  const qs = serializeTableState(
    { query: "", preset: "overview", cols: null, sort: null, values: {} },
    { filters: FILTERS, defaultPreset: "overview" }
  );
  assert.equal(qs, "");
});

test("parseTableState/serializeTableState: an explicit empty column selection round-trips as [], not null", () => {
  // The picker zeroed out to no optional columns is a real selection, distinct
  // from "no picker override" (cols: null) — a shared link for that view must
  // reopen with zero optional columns, not silently regain the preset's.
  const qs = serializeTableState(
    { query: "", preset: "overview", cols: [], sort: null, values: {} },
    { filters: FILTERS, defaultPreset: "overview" }
  );
  assert.equal(qs, "cols=");
  assert.deepEqual(parseTableState(qs, { filters: FILTERS }).cols, []);

  // A URL with no `cols` param at all is still "no override".
  assert.equal(parseTableState("", { filters: FILTERS }).cols, null);
});

test("parseTableState: one-sided and malformed ranges degrade rather than throw", () => {
  const parsed = parseTableState("cov=10~", { filters: FILTERS });
  assert.deepEqual(parsed.values.cov, { min: 10, max: null });

  const openMin = parseTableState("cov=~90", { filters: FILTERS });
  assert.deepEqual(openMin.values.cov, { min: null, max: 90 });

  // Fully unparseable: the filter is simply not set.
  assert.deepEqual(parseTableState("cov=abc~xyz", { filters: FILTERS }).values, {});
});

test("parseTableState: a select value outside the option list is ignored", () => {
  // A hand-edited URL should not filter the table down to nothing on a value
  // no control could ever produce.
  assert.deepEqual(parseTableState("provider=bogus", { filters: FILTERS }).values, {});
  assert.deepEqual(parseTableState("provider=gsv", { filters: FILTERS }).values, {
    provider: "gsv",
  });
});

test("parseTableState: an empty query string yields defaults, not undefined", () => {
  const parsed = parseTableState("", { filters: FILTERS });
  assert.deepEqual(parsed, { query: "", preset: null, cols: null, sort: null, values: {} });
});

// --- distribution strip -----------------------------------------------------

test("bucketCountFor: scales with N and clamps at both ends", () => {
  // A heavily filtered view must not render two lone bars across 24 empty
  // buckets, and a full table must not render bars thinner than their gaps.
  assert.equal(bucketCountFor(1), 1);
  assert.equal(bucketCountFor(2), 2);
  assert.equal(bucketCountFor(283), 17); // production's walk count
  assert.equal(bucketCountFor(1501), 24); // production's grid-series count, capped
});

test("histogramBuckets: bucket count adapts to N when not given one", () => {
  assert.equal(histogramBuckets([1, 2]).buckets.length, 2);
  assert.equal(histogramBuckets(Array.from({ length: 100 }, (_, i) => i)).buckets.length, 10);
});

test("histogramBuckets: drops nulls and puts the maximum in the last bucket", () => {
  const stats = histogramBuckets([0, 50, 100, null, undefined], 4);
  assert.equal(stats.count, 3);
  assert.equal(stats.min, 0);
  assert.equal(stats.max, 100);
  assert.equal(stats.buckets.length, 4);
  // 100 belongs in the final bucket, not one past the end.
  assert.equal(stats.buckets[3].count, 1);
  assert.equal(stats.buckets.reduce((n, b) => n + b.count, 0), 3);
});

test("histogramBuckets: a single distinct value yields one bucket, not a zero-width range", () => {
  const stats = histogramBuckets([7, 7, 7], 10);
  assert.equal(stats.buckets.length, 1);
  assert.deepEqual(stats.buckets[0], { from: 7, to: 7, count: 3 });
});

test("histogramBuckets: nothing measurable yields null, not an empty chart", () => {
  assert.equal(histogramBuckets([]), null);
  assert.equal(histogramBuckets([null, undefined]), null);
  // NaN/Infinity are not measurements either.
  assert.equal(histogramBuckets([NaN, Infinity]), null);
});

test("medianOf: averages the middle pair on an even count, ignores nulls", () => {
  assert.equal(medianOf([1, 2, 3]), 2);
  assert.equal(medianOf([1, 2, 3, 4]), 2.5);
  assert.equal(medianOf([3, null, 1]), 2);
  assert.equal(medianOf([]), null);
});

test("formatStripSummary: carries min/median/max as text for screen readers", () => {
  const column = { key: "pct", label: "Grid coverage", type: "number", unit: "%", digits: 1 };
  const summary = formatStripSummary(column, [10, 20, 90]);
  assert.match(summary, /Grid coverage across 3 rows/);
  assert.match(summary, /min 10\.0%/);
  assert.match(summary, /median 20\.0%/);
  assert.match(summary, /max 90\.0%/);
});

test("formatStripSummary: says so plainly when the filtered view has no values", () => {
  const column = { key: "pct", label: "Grid coverage", type: "number" };
  assert.match(formatStripSummary(column, [null, null]), /No grid coverage values/);
});

// --- controls markup --------------------------------------------------------

test("controlsHtml: every filter becomes a labeled control", () => {
  const html = controlsHtml({ filters: FILTERS, presets: PRESETS, columns: COLUMNS });
  // Select: a real <select> tied to its <label> by id.
  assert.match(html, /<label for="f-provider">Provider<\/label>/);
  assert.match(html, /<select id="f-provider" data-filter="provider">/);
  // Range: two number inputs, each individually labeled for a screen reader
  // (a shared visible legend cannot distinguish min from max).
  assert.match(html, /aria-label="Minimum Coverage"/);
  assert.match(html, /aria-label="Maximum Coverage"/);
  // Boolean: a checkbox, not a select.
  assert.match(html, /type="checkbox" id="f-changed"/);
});

test("controlsHtml: the picker offers every column except the always-on ones", () => {
  const html = controlsHtml({ filters: FILTERS, presets: PRESETS, columns: COLUMNS });
  for (const key of ["provider", "pct", "km"]) {
    assert.match(html, new RegExp(`data-column="${key}"`));
  }
  // City and the link column are structural — they are never toggleable.
  assert.doesNotMatch(html, /data-column="label"/);
  assert.doesNotMatch(html, /data-column="actions"/);
});
