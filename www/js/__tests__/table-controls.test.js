// Offline unit tests for the exploration chassis in table-controls.js
// (issue #188): search, structured filters, column presets and URL round-trip.
//
// Run with `npm test` (Node's built-in test runner) — no network, no jsdom.
// The DOM wiring in createTableControls is covered by the browser e2e smoke
// test instead (tests/e2e/test_smoke.py). No global stubs are needed: the
// module's only cross-file dependency is histogram-slider.js, reached solely
// through createTableControls' DOM path, which nothing here exercises.

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  foldForSearch,
  foldSearchTerms,
  matchesSearch,
  matchesSearchTerms,
  isRangeType,
  isFilterUnset,
  rowPassesFilter,
  applyFilters,
  rowsExceptFilter,
  resolveVisibleColumns,
  resolveFilters,
  defaultFilterValues,
  parseTableState,
  serializeTableState,
  histogramBuckets,
  controlsHtml,
  syncSidebarDisclosure,
  wireSidebarDisclosure,
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

test("foldSearchTerms: the query is folded to terms once, for the whole pass", () => {
  // applyFilters calls this once and hands the terms to matchesSearchTerms per
  // row, rather than re-folding the query ~19,000 times per keystroke on
  // driving.html. The two spellings must agree, or the split would be a
  // behaviour change dressed as an optimization.
  assert.deepEqual(foldSearchTerms("  SÃO   paulo "), ["sao", "paulo"]);
  assert.deepEqual(foldSearchTerms("   "), []);
  const row = { label: "São Paulo, Brazil", providerLabel: "Mapillary" };
  for (const query of ["sao paulo", "SÃO", "  ", "sao google"]) {
    assert.equal(
      matchesSearchTerms(row, SEARCH_FIELDS, foldSearchTerms(query)),
      matchesSearch(row, SEARCH_FIELDS, query),
      `disagreed on ${JSON.stringify(query)}`
    );
  }
});

test("the per-row haystack cache is keyed by the FIELD LIST, not just the row", () => {
  // The cache lives on the row object for the life of the row model. Two
  // callers on one page always search the same fields, but a cache that
  // ignored the field list would serve streets.html's haystack to a caller
  // asking about a different column set — a silent wrong ANSWER, not a slow
  // one, so the key is asserted rather than assumed.
  const row = { label: "Seattle", providerLabel: "Mapillary" };
  assert.ok(matchesSearch(row, ["label", "providerLabel"], "mapillary"));
  assert.ok(!matchesSearch(row, ["label"], "mapillary"));
  assert.ok(matchesSearch(row, ["label", "providerLabel"], "mapillary"));
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
  // `histogram-range` is the ONLY numeric-window type — there was a bar-less
  // `range` twin, and this fixture was it. Every value-shaped assertion below
  // (unset, pass, URL round-trip, section order) now runs against the type the
  // pages actually declare.
  { key: "cov", label: "Coverage", type: "histogram-range", field: "pct", min: 0, max: 100 },
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

// --- histogramBuckets --------------------------------------------------------
//
// The axis is always the CALLER's. This helper fed two consumers until the
// sorted-column distribution strip was retired: the strip scaled itself to the
// rows in view, the histogram-slider must not, and the self-scaling default
// went with the strip rather than being left as an option nothing picks.

test("histogramBuckets: drops nulls and puts the maximum in the last bucket", () => {
  const stats = histogramBuckets([0, 50, 100, null, undefined], 4, { min: 0, max: 100 });
  assert.equal(stats.count, 3);
  assert.equal(stats.min, 0);
  assert.equal(stats.max, 100);
  assert.equal(stats.buckets.length, 4);
  // 100 belongs in the final bucket, not one past the end.
  assert.equal(stats.buckets[3].count, 1);
  assert.equal(stats.buckets.reduce((n, b) => n + b.count, 0), 3);
});

test("histogramBuckets: a single-point domain yields one bucket, not a zero-width range", () => {
  const stats = histogramBuckets([7, 7, 7], 10, { min: 7, max: 7 });
  assert.equal(stats.buckets.length, 1);
  assert.deepEqual(stats.buckets[0], { from: 7, to: 7, count: 3 });
});

test("histogramBuckets: nothing measurable yields null, not an empty chart", () => {
  const domain = { min: 0, max: 100 };
  assert.equal(histogramBuckets([], 4, domain), null);
  assert.equal(histogramBuckets([null, undefined], 4, domain), null);
  // NaN/Infinity are not measurements either.
  assert.equal(histogramBuckets([NaN, Infinity], 4, domain), null);
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
// A second numeric window, for the cases that need two on one page.
const HIST_FILTERS = [
  FILTERS[1],
  { key: "age", label: "Median age", type: "histogram-range", field: "age", min: 0 },
];

test("isRangeType: histogram-range and nothing else", () => {
  // ONE numeric-window type. There was a bar-less `range` twin, and there were
  // "parity" tests asserting f(range) === f(histogram) at every value-shaped
  // call site — which proved nothing, because every one of those sites
  // dispatches through THIS predicate first, so the two arguments took the
  // same branch. The typed assertions the twins wrapped are the real content
  // and live on above, against the histogram-range fixture.
  assert.ok(isRangeType({ type: "histogram-range" }));
  assert.ok(!isRangeType({ type: "range" }));
  assert.ok(!isRangeType({ type: "select" }));
  assert.ok(!isRangeType({ type: "boolean" }));
});

test("a numeric window's URL format is `min~max`, exactly", () => {
  // Typed rather than compared against a twin: this IS the wire format, and a
  // shared link written by an older deployment has to keep parsing.
  const state = {
    query: "",
    preset: "overview",
    cols: null,
    sort: null,
    values: { cov: { min: 10, max: 90 } },
  };
  // URLSearchParams percent-encodes the "~", which is why the expectation
  // reads `%7E` rather than the character the parser splits on.
  assert.equal(
    serializeTableState(state, { filters: HIST_FILTERS, defaultPreset: "overview" }),
    "cov=10%7E90"
  );
  assert.deepEqual(parseTableState("cov=10~90", { filters: HIST_FILTERS }).values.cov, {
    min: 10,
    max: 90,
  });
  // A window on one filter says nothing about the other.
  assert.equal(parseTableState("cov=10~90", { filters: HIST_FILTERS }).values.age, undefined);

  // One-sided keeps the bound it was given; unparseable is simply not set.
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

test("histogramBuckets: the axis is the caller's, never the values' own extent", () => {
  // The slider's axis must not rescale under a brush — bars shrink, the axis
  // stays. Left to themselves these three values would span 10–30, which would
  // move the handles' meaning out from under the reader's hand.
  const stats = histogramBuckets([10, 20, 30], 4, { min: 0, max: 100 });
  assert.equal(stats.min, 0);
  assert.equal(stats.max, 100);
  // Four 25-wide buckets over 0-100: 10 and 20 in the first, 30 in the second.
  assert.deepEqual(stats.buckets.map((b) => b.count), [2, 1, 0, 0]);
  assert.equal(stats.buckets[0].from, 0);
  assert.equal(stats.buckets[3].to, 100);
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

// --- pickerLabel ------------------------------------------------------------

// There WAS a `controlsHtml: renders no sorted-column distribution strip` test
// here, asserting the output matched neither /distribution-strip/ nor
// /strip-bar/. It went with the strip: once those strings appear nowhere in
// table-controls.js, a test for their absence from its output cannot fail, and
// a vacuous test reads like coverage. The strip's real replacement — one
// fixed-axis histogram per numeric filter — is pinned by the histogram-range
// cases above and, in a browser, by
// test_the_table_pages_replaced_the_strip_with_per_filter_histograms.

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

// --- control section order (issue #250) -------------------------------------

const ORDER_FILTERS = [
  FILTERS[1], // range "cov"
  FILTERS[2], // boolean "changed"
  FILTERS[0], // select "provider"
];

/** Index of each landmark in the emitted markup, for order assertions. */
function landmarks(html) {
  return {
    search: html.indexOf('id="table-search"'),
    select: html.indexOf('data-filter="provider"'),
    range: html.indexOf('data-filter="cov"'),
    boolean: html.indexOf('id="f-changed"'),
    columns: html.indexOf('id="table-preset"'),
    clear: html.indexOf("controls-clear"),
  };
}

test("controlsHtml: sections partition by type, columns between selects and ranges", () => {
  // In a 280px column the reading order IS the layout: cheap categorical
  // narrowings first, then the column controls, then the tall histogram
  // brushes, then the checkboxes. Note the descriptors arrive in a DIFFERENT
  // order (range, boolean, select) — the page's declaration order is not the
  // rendering order, which is the whole point of partitioning.
  const at = landmarks(
    controlsHtml({ filters: ORDER_FILTERS, presets: PRESETS, columns: COLUMNS })
  );
  assert.ok(at.search < at.select, "search leads");
  assert.ok(at.select < at.columns, "selects come before the column controls");
  assert.ok(at.columns < at.range, "column controls come before the numeric windows");
  assert.ok(at.range < at.boolean, "numeric windows come before the checkboxes");
  assert.ok(at.boolean < at.clear, "Clear all is last");
});

test("controlsHtml: EVERY filter renders, including an unknown type", () => {
  // The partition is by type, so a type added later must land somewhere — in
  // the wrong PLACE rather than not at all.
  const odd = { key: "future", label: "Future", type: "something-new" };
  const html = controlsHtml({
    filters: [...ORDER_FILTERS, odd],
    presets: PRESETS,
    columns: COLUMNS,
  });
  for (const key of ["provider", "cov", "changed", "future"]) {
    assert.match(html, new RegExp(`data-filter="${key}"`), `${key} is missing from the sidebar`);
  }
});

test("controlsHtml: a histogram-range emits the slider AND keeps the number inputs", () => {
  const html = controlsHtml({ filters: HIST_FILTERS, presets: PRESETS, columns: COLUMNS });
  assert.match(html, /class="control control-histogram" data-histogram="cov"/);
  assert.match(html, /<div class="hist-bars" aria-hidden="true"><\/div>/);
  assert.match(html, /class="hist-lo" aria-label="Minimum Coverage"/);
  assert.match(html, /class="hist-hi" aria-label="Maximum Coverage"/);
  // The chassis and the e2e selectors both locate a range filter's bounds with
  // querySelectorAll('[data-filter=KEY]') and expect exactly two elements — so
  // the range handles must NOT carry one.
  assert.equal((html.match(/data-filter="cov"/g) || []).length, 2);
  assert.match(html, /<input type="number" data-filter="cov" data-bound="min"/);
  assert.match(html, /<input type="number" data-filter="cov" data-bound="max"/);
});

// --- sidebar disclosure -----------------------------------------------------

test("syncSidebarDisclosure: widening re-opens a collapsed sidebar", () => {
  // Otherwise the panel is closed with its only toggle now display:none —
  // filters that exist, are in the URL, and cannot be seen or changed.
  const el = { open: false };
  assert.equal(syncSidebarDisclosure(el, true), true);
  assert.equal(el.open, true);
});

test("syncSidebarDisclosure: narrowing never closes what the reader opened", () => {
  const open = { open: true };
  assert.equal(syncSidebarDisclosure(open, false), true);
  const closed = { open: false };
  assert.equal(syncSidebarDisclosure(closed, false), false);
});

test("wireSidebarDisclosure: a no-op without a sidebar, matchMedia, or a document", () => {
  // Node has no window at all, and any page loading table-controls.js without
  // a sidebar has no .sidebar-disclosure — this is called unconditionally on
  // DOMContentLoaded, so both must be safe.
  assert.equal(wireSidebarDisclosure({ querySelector: () => null }), null);
  assert.equal(wireSidebarDisclosure(), null);
});

// --- resolveFilters: the provider scope -------------------------------------
//
// A pivoted row holds one value per provider, so a numeric filter is
// incomplete until something says WHOSE number it asks about. Before this,
// nothing did: the sliders always read a best-across field while "Collected
// by" only narrowed which cities were listed, so the two controls did not
// compose. Measured on the live catalog, "Collected by Mapillary" + "coverage
// >= 80%" returned 56 cities and NONE of them had Mapillary coverage >= 80.

const SCOPED_FILTERS = [
  {
    key: "provider",
    label: "Collected by",
    type: "select",
    options: [
      { value: "gsv", label: "GSV" },
      { value: "mapillary", label: "Mapillary" },
      { value: "multi", label: "2+ providers" },
    ],
    test: (row, value) =>
      value === "multi" ? row.providers.length > 1 : row.providers.includes(value),
  },
  {
    key: "cov",
    label: "Coverage %",
    type: "histogram-range",
    field: "pctBest",
    fieldFor: (values) =>
      values?.provider && values.provider !== "multi" ? `pct_${values.provider}` : "pctBest",
    labelFor: (values) =>
      values?.provider && values.provider !== "multi"
        ? `Coverage % — ${values.provider}`
        : "Coverage % — any provider reaches",
  },
  { key: "km", label: "Street km", type: "histogram-range", field: "lengthKm" },
];

const SCOPED_ROWS = [
  // The real shape of the bug: GSV is high, Mapillary is zero, best is GSV's.
  { rowKey: "a", providers: ["gsv", "mapillary"], pct_gsv: 97.6, pct_mapillary: 0, pctBest: 97.6 },
  { rowKey: "b", providers: ["mapillary"], pct_gsv: null, pct_mapillary: 44, pctBest: 44 },
  { rowKey: "c", providers: ["gsv"], pct_gsv: 91, pct_mapillary: null, pctBest: 91 },
];

test("resolveFilters: an unscoped view reads the best-across field and says so", () => {
  const [, cov] = resolveFilters(SCOPED_FILTERS, {});
  assert.equal(cov.field, "pctBest");
  assert.equal(cov.label, "Coverage % — any provider reaches");
});

test("resolveFilters: a scoped view reads THAT provider's column and says so", () => {
  const [, cov] = resolveFilters(SCOPED_FILTERS, { provider: "mapillary" });
  assert.equal(cov.field, "pct_mapillary");
  assert.equal(cov.label, "Coverage % — mapillary");
});

test("resolveFilters: the 2+ option scopes to no single provider, so it stays best-across", () => {
  const [, cov] = resolveFilters(SCOPED_FILTERS, { provider: "multi" });
  assert.equal(cov.field, "pctBest");
});

test("resolveFilters: descriptors without a scope hook pass through by IDENTITY", () => {
  // driving.html and the plain `range` filters must be unaware of any of this;
  // returning fresh copies would also churn object identity on every apply.
  const out = resolveFilters(SCOPED_FILTERS, { provider: "gsv" });
  assert.equal(out[0], SCOPED_FILTERS[0], "the select itself is not scoped");
  assert.equal(out[2], SCOPED_FILTERS[2], "an unscoped numeric filter is not copied");
  assert.notEqual(out[1], SCOPED_FILTERS[1]);
  // ...and the originals are never mutated.
  assert.equal(SCOPED_FILTERS[1].field, "pctBest");
  assert.equal(SCOPED_FILTERS[1].label, "Coverage %");
});

test("scope + window compose: the query that used to return 56 wrong rows", () => {
  const values = { provider: "mapillary", cov: { min: 80, max: null } };
  const resolved = resolveFilters(SCOPED_FILTERS, values);
  const out = applyFilters(SCOPED_ROWS, {
    filters: resolved,
    values,
    query: "",
    searchFields: [],
  });
  assert.deepEqual(out.map((r) => r.rowKey), [], "no row has Mapillary coverage >= 80");

  // Under the OLD semantics the same query matched row "a" on GSV's 97.6%.
  const unscoped = applyFilters(SCOPED_ROWS, {
    filters: SCOPED_FILTERS,
    values,
    query: "",
    searchFields: [],
  });
  assert.deepEqual(unscoped.map((r) => r.rowKey), ["a"]);
});

test("scope + window compose: a reachable Mapillary window returns the right row", () => {
  const values = { provider: "mapillary", cov: { min: 40, max: null } };
  const out = applyFilters(SCOPED_ROWS, {
    filters: resolveFilters(SCOPED_FILTERS, values),
    values,
    query: "",
    searchFields: [],
  });
  assert.deepEqual(out.map((r) => r.rowKey), ["b"]);
});

test("resolveFilters: a scoped boolean resolves its TEST, not just its wording", () => {
  const changed = {
    key: "changed",
    label: "Has Δ",
    type: "boolean",
    test: (row) => row.dGsv != null || row.dMap != null,
    testFor: (values) =>
      values?.provider === "gsv" ? (row) => row.dGsv != null : (row) => row.dGsv != null || row.dMap != null,
  };
  const rows = [
    { rowKey: "x", dGsv: 1, dMap: null },
    { rowKey: "y", dGsv: null, dMap: 2 },
  ];
  const anyValues = { changed: true };
  assert.deepEqual(
    applyFilters(rows, { filters: resolveFilters([changed], anyValues), values: anyValues, query: "", searchFields: [] }).map((r) => r.rowKey),
    ["x", "y"]
  );
  const gsvValues = { provider: "gsv", changed: true };
  assert.deepEqual(
    applyFilters(rows, { filters: resolveFilters([changed], gsvValues), values: gsvValues, query: "", searchFields: [] }).map((r) => r.rowKey),
    ["x"]
  );
});
