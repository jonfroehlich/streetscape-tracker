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
  isRangeType,
  isFilterUnset,
  rowPassesFilter,
  applyFilters,
  rowsExceptFilter,
  resolveVisibleColumns,
  defaultFilterValues,
  parseTableState,
  serializeTableState,
  bucketCountFor,
  histogramBuckets,
  medianOf,
  formatStripSummary,
  renderDistributionStrip,
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

// --- renderDistributionStrip (clickable bars) --------------------------------

const STRIP_EL = () => ({ innerHTML: "" });
const STRIP_COLUMN = { key: "pct", label: "Coverage", type: "number", unit: "%", digits: 1 };

test("renderDistributionStrip: plain, non-interactive spans when the caller passes no filter", () => {
  const el = STRIP_EL();
  renderDistributionStrip(el, STRIP_COLUMN, [10, 50, 90]);
  assert.match(el.innerHTML, /<div class="strip-bars" aria-hidden="true">/);
  assert.match(el.innerHTML, /<span class="strip-bar"/);
  assert.doesNotMatch(el.innerHTML, /<button/);
});

test("renderDistributionStrip: clickable buttons carrying rounded bounds when a matching filter exists", () => {
  const el = STRIP_EL();
  renderDistributionStrip(el, STRIP_COLUMN, [10, 50, 90], true);
  // No aria-hidden on the container once its bars are real, focusable buttons.
  assert.doesNotMatch(el.innerHTML, /aria-hidden="true"/);
  assert.match(el.innerHTML, /<button type="button" class="strip-bar" data-from="[\d.]+" data-to="[\d.]+"/);
  assert.match(el.innerHTML, /click to filter to this range/);
});

test("renderDistributionStrip: a non-numeric or empty column never renders buttons, even if asked", () => {
  // clickable=true from a stale sort state must not produce a strip with
  // nothing behind it to filter.
  const el = STRIP_EL();
  renderDistributionStrip(el, { key: "label", label: "City", type: "text" }, [], true);
  assert.doesNotMatch(el.innerHTML, /<button/);
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

// --- histogram-range parity (issue #250) ------------------------------------
//
// `histogram-range` renders differently and behaves identically: same value
// shape, same URL wire format. Every place that reasons about the VALUE has to
// treat the two as one, so each is asserted against its `range` twin rather
// than against a hand-copied expectation.

const HIST_FILTERS = [
  { key: "cov", label: "Coverage", type: "histogram-range", field: "pct", min: 0, max: 100 },
  { key: "age", label: "Median age", type: "histogram-range", field: "age", min: 0 },
];

test("isRangeType: both numeric-window flavours, nothing else", () => {
  assert.ok(isRangeType({ type: "range" }));
  assert.ok(isRangeType({ type: "histogram-range" }));
  assert.ok(!isRangeType({ type: "select" }));
  assert.ok(!isRangeType({ type: "boolean" }));
});

test("histogram-range is unset/pass-tested exactly like range", () => {
  const hist = HIST_FILTERS[0];
  const plain = FILTERS[1];
  for (const value of [{ min: null, max: null }, { min: 10, max: null }, null, ""]) {
    assert.equal(
      isFilterUnset(hist, value),
      isFilterUnset(plain, value),
      `unset disagreed on ${JSON.stringify(value)}`
    );
  }
  for (const row of [{ pct: 90 }, { pct: 10 }, { pct: null }]) {
    assert.equal(
      rowPassesFilter(hist, row, { min: 50, max: 100 }),
      rowPassesFilter(plain, row, { min: 50, max: 100 }),
      `pass disagreed on ${JSON.stringify(row)}`
    );
  }
});

test("histogram-range round-trips through the URL in the plain range's format", () => {
  const state = {
    query: "",
    preset: "overview",
    cols: null,
    sort: null,
    values: { cov: { min: 10, max: 90 } },
  };
  const qs = serializeTableState(state, { filters: HIST_FILTERS, defaultPreset: "overview" });
  // Byte-for-byte what the plain `range` twin writes (URLSearchParams
  // percent-encodes the "~", which is why this is compared rather than typed).
  assert.equal(
    qs,
    serializeTableState(state, { filters: [FILTERS[1]], defaultPreset: "overview" })
  );
  assert.deepEqual(parseTableState(qs, { filters: HIST_FILTERS }).values.cov, { min: 10, max: 90 });

  // One-sided and malformed degrade the same way a plain range does.
  assert.deepEqual(parseTableState("cov=50~", { filters: HIST_FILTERS }).values.cov, {
    min: 50,
    max: null,
  });
  assert.deepEqual(parseTableState("cov=~50", { filters: HIST_FILTERS }).values.cov, {
    min: null,
    max: 50,
  });
  assert.deepEqual(parseTableState("cov=abc~xyz", { filters: HIST_FILTERS }).values, {});
  assert.deepEqual(parseTableState("cov=~", { filters: HIST_FILTERS }).values, {});
});

// --- rowsExceptFilter (the crossfilter rule) --------------------------------

test("rowsExceptFilter: ignores its own filter but honors every other one", () => {
  // The bars a slider draws must not be computed over its own selection, or
  // the histogram changes under the reader's hand. Everything else still
  // narrows them, which is the point of computing them at all.
  const cfg = {
    filters: FILTERS,
    values: { provider: "gsv", cov: { min: 80, max: 100 } },
    query: "",
    searchFields: [],
  };
  // With every filter applied only "a" survives...
  assert.deepEqual(applyFilters(ROWS, cfg).map((r) => r.cityId), ["a"]);
  // ...but the coverage histogram is drawn over both GSV rows, including the
  // one its own window currently excludes.
  assert.deepEqual(rowsExceptFilter(ROWS, cfg, "cov").map((r) => r.cityId), ["a", "c"]);
  // Dropping the provider filter instead leaves cov doing the narrowing.
  assert.deepEqual(rowsExceptFilter(ROWS, cfg, "provider").map((r) => r.cityId), ["a"]);
  // An unknown self-key drops nothing.
  assert.deepEqual(rowsExceptFilter(ROWS, cfg, "nope").map((r) => r.cityId), ["a"]);
});

test("rowsExceptFilter: the free-text query still narrows the bars", () => {
  const rows = [
    { cityId: "a", label: "Seattle", pct: 90 },
    { cityId: "b", label: "Bend", pct: 10 },
  ];
  const out = rowsExceptFilter(
    rows,
    { filters: FILTERS, values: { cov: { min: 50, max: 100 } }, query: "bend", searchFields: ["label"] },
    "cov"
  );
  assert.deepEqual(out.map((r) => r.cityId), ["b"]);
});

// --- histogramBuckets: fixed domain -----------------------------------------

test("histogramBuckets: a domain override fixes the axis instead of tracking the values", () => {
  // The slider's axis must not rescale under a brush — bars shrink, the axis
  // stays. Without the override these three values would span 10–30.
  const stats = histogramBuckets([10, 20, 30], 4, { min: 0, max: 100 });
  assert.equal(stats.min, 0);
  assert.equal(stats.max, 100);
  // Four 25-wide buckets over 0-100: 10 and 20 in the first, 30 in the second.
  assert.deepEqual(stats.buckets.map((b) => b.count), [2, 1, 0, 0]);
  assert.equal(stats.buckets[0].from, 0);
  assert.equal(stats.buckets[3].to, 100);
  // Without the override the same values would span 10-30 instead.
  assert.equal(histogramBuckets([10, 20, 30], 4).max, 30);
});

test("histogramBuckets: values outside a fixed domain clamp into the end buckets", () => {
  // A domain is computed once from the unfiltered rows; a value beyond it can
  // only come from a stale domain, and losing the row silently would be worse
  // than piling it on an end bar.
  const stats = histogramBuckets([-5, 50, 105], 2, { min: 0, max: 100 });
  // -5 clamps up into the first bucket, 105 clamps down into the last; 50 sits
  // on the boundary and belongs to the upper bucket, as it always has.
  assert.deepEqual(stats.buckets.map((b) => b.count), [1, 2]);
  assert.equal(stats.count, 3);
});

test("histogramBuckets: without a domain, nothing about the existing behaviour moves", () => {
  const stats = histogramBuckets([0, 50, 100], 4);
  assert.equal(stats.min, 0);
  assert.equal(stats.max, 100);
  assert.equal(histogramBuckets([], 4, { min: 0, max: 100 }), null);
});

// --- select defaultValue (issue #250) ---------------------------------------

const NETWORK_FILTER = {
  key: "network",
  label: "Network",
  type: "select",
  defaultValue: "drive",
  options: [
    { value: "drive", label: "Roads" },
    { value: "all_public", label: "Roads + paths" },
  ],
  test: (row, value) => row.networkType === value,
};

test("parseTableState: an absent param means the DEFAULT, not 'no filter'", () => {
  // streets.html has no "all networks" reading: two networks are two different
  // street-km denominators, which must never share a comparable column.
  assert.deepEqual(parseTableState("", { filters: [NETWORK_FILTER] }).values, {
    network: "drive",
  });
  assert.deepEqual(parseTableState("network=all_public", { filters: [NETWORK_FILTER] }).values, {
    network: "all_public",
  });
});

test("parseTableState: an unknown value falls back to the default, not to unset", () => {
  // Dropping the filter would double every city's rows on a hand-edited URL.
  assert.deepEqual(parseTableState("network=bogus", { filters: [NETWORK_FILTER] }).values, {
    network: "drive",
  });
  assert.deepEqual(parseTableState("network=", { filters: [NETWORK_FILTER] }).values, {
    network: "drive",
  });
});

test("serializeTableState: the default is omitted, a deviation is written", () => {
  const cfg = { filters: [NETWORK_FILTER], defaultPreset: "overview" };
  const base = { query: "", preset: "overview", cols: null, sort: null };
  assert.equal(serializeTableState({ ...base, values: { network: "drive" } }, cfg), "");
  assert.equal(
    serializeTableState({ ...base, values: { network: "all_public" } }, cfg),
    "network=all_public"
  );
});

test("defaultFilterValues: only defaulted selects contribute — this is what Clear all resets to", () => {
  assert.deepEqual(defaultFilterValues([NETWORK_FILTER, ...FILTERS]), { network: "drive" });
  assert.deepEqual(defaultFilterValues(FILTERS), {});
});

test("controlsHtml: a defaulted select drops the blank 'any' option", () => {
  const html = controlsHtml({ filters: [NETWORK_FILTER], presets: PRESETS, columns: COLUMNS });
  assert.doesNotMatch(html, /<option value="">/);
  assert.match(html, /<option value="drive">Roads<\/option>/);
  // ...while an ordinary select keeps it.
  assert.match(
    controlsHtml({ filters: FILTERS, presets: PRESETS, columns: COLUMNS }),
    /<option value="">All<\/option>/
  );
});

// --- strip opt-out + pickerLabel --------------------------------------------

test("controlsHtml: showDistributionStrip=false omits the strip container entirely", () => {
  // The pivoted pages replace it with per-filter histograms; leaving an empty
  // container would still paint a card-shaped box under the controls.
  const off = controlsHtml({
    filters: FILTERS,
    presets: PRESETS,
    columns: COLUMNS,
    showDistributionStrip: false,
  });
  assert.doesNotMatch(off, /distribution-strip/);
  // driving.html passes nothing and keeps it.
  assert.match(controlsHtml({ filters: FILTERS, presets: PRESETS, columns: COLUMNS }), /id="distribution-strip"/);
});

test("controlsHtml: the picker prefers pickerLabel over the leaf label", () => {
  // A pivot's leaf label is a provider name repeated under every metric group
  // ("GSV" four times); unambiguous in the header, useless in a flat list.
  const columns = [
    { key: "label", label: "City", always: true },
    { key: "pct_gsv", label: "GSV", pickerLabel: "Grid coverage — GSV" },
    { key: "km", label: "Street km" },
  ];
  const html = controlsHtml({ filters: [], presets: PRESETS, columns });
  assert.match(html, /data-column="pct_gsv"> Grid coverage — GSV/);
  assert.match(html, /data-column="km"> Street km/);
});
