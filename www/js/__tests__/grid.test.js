// Offline unit tests for the pure helpers in grid.js — the Grid Coverage
// table page, pivoted to one row per city in issue #250. Run with `npm test`
// (Node's built-in test runner) — no network, no jsdom. `document` is left
// undefined on purpose: grid.js only registers its DOMContentLoaded listener
// when one exists.

const test = require("node:test");
const assert = require("node:assert/strict");

// A THIRD provider the page has never heard of: every per-provider column,
// filter option and row key is generated from this registry, so a hardcoded
// pair fails here rather than the day one is really registered (issue #225).
// It deliberately carries no shortLabel/panoCountingModel, which is what the
// caller-side fallbacks are for.
//
// The capability flags are NOT optional decoration here. Both tooltip
// branches are guarded by one (`hasFlatImagery`, `hasCopyrightFilter`), so a
// stub that omits them leaves the true branch unevaluated and the
// cross-provider sweep below green against a hardcoded provider name —
// mutation-verified, and the gap the #296 review found in the first version
// of that sweep.
global.PROVIDERS = {
  gsv: {
    label: "Google Street View",
    shortLabel: "GSV",
    panoCountingModel: "sample",
    hasCopyrightFilter: true,
    hasFlatImagery: false,
  },
  mapillary: {
    label: "Mapillary",
    shortLabel: "Mapillary",
    panoCountingModel: "census",
    hasCopyrightFilter: false,
    hasFlatImagery: true,
  },
  thirdparty: { label: "Third Party" },
};
global.coverageColor = (pct) => `coverage(${pct})`;
global.escapeHtml = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// grid.js reads the shared table machinery as browser globals — mirror that
// here (must precede the grid.js require). PROVIDERS above must already be
// set: table-utils.js's provider-column helpers read it.
const tableUtils = require("../table-utils.js");
Object.assign(global, tableUtils);
const { deltaCellHtml } = tableUtils;

// The real adapter is exercised by streetscape-utils.test.js; here a stub
// keeps the fixtures readable and, crucially, reproduces the ONE behaviour the
// pivot depends on: a city with no runs for the provider being adapted is
// simply absent from that pass.
global.adaptCitiesPayload = (raw, provider) => ({
  meta: { generatedAt: raw?.generated_at ?? null },
  cities: (raw?.cities ?? []).filter((c) => c.provider === provider).map((c) => ({ ...c })),
});

const {
  pivotGridRows,
  gridRowHtml,
  gridDeltaPair,
  buildGridColumns,
  buildGridPresets,
  buildGridFilters,
  GRID_COLUMNS,
  GRID_PRESETS,
  GRID_DEFAULT_SORT,
  GRID_FILTERS,
} = require("../grid.js");

const SEATTLE_GSV = {
  provider: "gsv",
  city_id: "seattle--wa",
  city: "Seattle",
  state: { name: "Washington" },
  country: { name: "United States" },
  coverage_rate_percent: 51.2,
  any_imagery_coverage_rate_percent: 51.2,
  pano_age_stats: { median_pano_age_years: 3.4 },
  pano_count: 41234,
  total_search_points: 1681,
  search_area_km2: 25.0,
  grid: { width_meters: 5000, height_meters: 5000, step_length_meters: 20 },
  latest_run_date: "2026-07-05",
  runs: [{ run_date: "2025-01-17" }, { run_date: "2026-07-05" }],
  data_file: { filename: "seattle--wa_width_5000_height_5000_step_20_2026-07-05.csv.gz" },
};

const SEATTLE_MAPILLARY = {
  provider: "mapillary",
  city_id: "seattle--wa",
  city: "Seattle",
  state: { name: "Washington" },
  country: { name: "United States" },
  coverage_rate_percent: 61.5,
  any_imagery_coverage_rate_percent: 74.8,
  pano_age_stats: { median_pano_age_years: 2.1 },
  pano_count: 987654,
  latest_run_date: "2026-07-05",
  runs: [{ run_date: "2026-07-05" }],
  data_file: {
    filename: "seattle--wa_width_5000_height_5000_step_20_mapillary_2026-07-05.csv.gz",
  },
};

const BEND_GSV = { ...SEATTLE_GSV, city_id: "bend--or", city: "Bend", state: { name: "Oregon" } };

function payload(...cities) {
  return { generated_at: "2026-07-06T00:00:00Z", cities };
}

function rowFor(raw, cityId) {
  return pivotGridRows(raw).rows.find((r) => r.cityId === cityId);
}

/**
 * What the page would actually render for a payload: the row, plus the
 * columns and filters built from the providers that payload CONTAINS.
 *
 * The module-level GRID_COLUMNS/GRID_FILTERS are the full-registry build —
 * every provider the site knows about, collected or not — so asserting a
 * payload's row model against them asks for keys the pivot deliberately does
 * not build (issue #250 review).
 */
function buildFor(raw, cityId) {
  const { rows, providers } = pivotGridRows(raw);
  return {
    providers,
    row: rows.find((r) => r.cityId === cityId),
    columns: buildGridColumns(providers),
    filters: buildGridFilters(providers),
  };
}

/** A payload carrying a run for every registered provider. */
function fullRegistryPayload() {
  return payload(
    SEATTLE_GSV,
    SEATTLE_MAPILLARY,
    { ...SEATTLE_MAPILLARY, provider: "thirdparty", coverage_rate_percent: 99 }
  );
}

// --- pivotGridRows ----------------------------------------------------------

test("pivotGridRows: one row per city, providers folded into sub-fields", () => {
  const { rows, generatedAt } = pivotGridRows(payload(SEATTLE_GSV, SEATTLE_MAPILLARY, BEND_GSV));
  assert.equal(rows.length, 2);
  assert.equal(generatedAt, "2026-07-06T00:00:00Z");

  const seattle = rows.find((r) => r.cityId === "seattle--wa");
  assert.equal(seattle.label, "Seattle, Washington, United States");
  assert.deepEqual(seattle.providers, ["gsv", "mapillary"]);
  assert.equal(seattle.providerCount, 2);
  assert.equal(seattle.providersLabel, "GSV, Mapillary");
  assert.equal(seattle.pct_gsv, 51.2);
  assert.equal(seattle.pct_mapillary, 61.5);
  assert.equal(seattle.pctAny_mapillary, 74.8);
  assert.equal(seattle.medianAge_gsv, 3.4);
  assert.equal(seattle.panos_mapillary, 987654);
  assert.equal(seattle.collected_mapillary, "2026-07-05");
  assert.equal(seattle.snapshots_gsv, 2);
  assert.equal(seattle.snapshots_mapillary, 1);
});

test("pivotGridRows: the city set is the UNION across providers, never the intersection", () => {
  // adaptCitiesPayload drops a city that has no runs for the provider it is
  // adapting for, so intersecting would hide every single-provider city —
  // which is most of them.
  const onlyMapillary = { ...SEATTLE_MAPILLARY, city_id: "lisboa--pt", city: "Lisboa" };
  const { rows } = pivotGridRows(payload(BEND_GSV, onlyMapillary));
  assert.deepEqual(rows.map((r) => r.cityId).sort(), ["bend--or", "lisboa--pt"]);

  const lisboa = rows.find((r) => r.cityId === "lisboa--pt");
  assert.deepEqual(lisboa.providers, ["mapillary"]);
  assert.equal(lisboa.pct_gsv, null, "a provider that never collected must read null");
  assert.equal(lisboa.pct_mapillary, 61.5);
  assert.equal(lisboa.collected_gsv, null);
  assert.equal(lisboa.filename_gsv, null);
});

test("pivotGridRows: shared frozen-grid facts collapse to one field", () => {
  // Grid geometry is a CITY property shared by every provider — that is what
  // makes their coverage rates comparable — so it is one column, not one per
  // provider. The Mapillary record here carries none of it, and must not
  // overwrite the GSV record's values with nulls.
  const seattle = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  assert.equal(seattle.searchPoints, 1681);
  assert.equal(seattle.gridWidthM, 5000);
  assert.equal(seattle.gridStepM, 20);
  assert.equal(seattle.areaKm2, 25.0);
  assert.equal(seattle.gridSpanLabel, "5.0 × 5.0 km");

  // ...and in the other order too, so "first non-null wins" is not "first
  // provider wins".
  const flipped = rowFor(payload(SEATTLE_MAPILLARY, SEATTLE_GSV), "seattle--wa");
  assert.equal(flipped.searchPoints, 1681);
  assert.equal(flipped.gridSpanLabel, "5.0 × 5.0 km");
});

test("pivotGridRows: the City link prefers the first REGISTERED provider with a run", () => {
  // The row is a city now, so it has no provider of its own; each provider's
  // "Last collected" sub-cell carries the link to that provider's series.
  const both = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  assert.equal(both.filename, SEATTLE_GSV.data_file.filename);

  const mapillaryOnly = rowFor(payload(SEATTLE_MAPILLARY), "seattle--wa");
  assert.equal(mapillaryOnly.filename, SEATTLE_MAPILLARY.data_file.filename);

  const none = rowFor(payload({ provider: "gsv", city_id: "x--y", city: "X" }), "x--y");
  assert.equal(none.filename, null);
});

test("pivotGridRows: pctBest is the MAX and medianAgeBest is the MIN (freshest)", () => {
  const seattle = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  assert.equal(seattle.pctBest, 61.5);
  assert.equal(seattle.medianAgeBest, 2.1, "best age is the SMALL number");

  const gsvOnly = rowFor(payload(BEND_GSV), "bend--or");
  assert.equal(gsvOnly.pctBest, 51.2);
  assert.equal(gsvOnly.medianAgeBest, 3.4);

  const nothing = rowFor(payload({ provider: "gsv", city_id: "x--y", city: "X" }), "x--y");
  assert.equal(nothing.pctBest, null);
  assert.equal(nothing.medianAgeBest, null);
});

test("pivotGridRows: Δ is null unless BOTH operands are present, and signed Mapillary − GSV", () => {
  // Treating a missing operand as zero would turn "no Mapillary run here" into
  // "Mapillary is 51 points behind" — a made-up comparison that would then
  // sort as one.
  const both = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  assert.equal(Math.round(both.deltaPct * 10) / 10, 10.3); // 61.5 − 51.2
  assert.equal(Math.round(both.deltaPctAny * 10) / 10, 23.6); // 74.8 − 51.2
  assert.equal(Math.round(both.deltaMedianAge * 10) / 10, -1.3); // 2.1 − 3.4

  for (const key of ["deltaPct", "deltaPctAny", "deltaMedianAge"]) {
    assert.equal(rowFor(payload(BEND_GSV), "bend--or")[key], null, `${key} on a GSV-only city`);
    assert.equal(
      rowFor(payload(SEATTLE_MAPILLARY), "seattle--wa")[key],
      null,
      `${key} on a Mapillary-only city`
    );
  }
});

test("pivotGridRows: a third registered provider gets keys and is counted, but no Δ", () => {
  const third = { ...SEATTLE_MAPILLARY, provider: "thirdparty", coverage_rate_percent: 99 };
  const seattle = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY, third), "seattle--wa");
  assert.equal(seattle.pct_thirdparty, 99);
  assert.equal(seattle.providerCount, 3);
  // shortLabel is absent from this registry entry — the fallback is `label`.
  assert.match(seattle.providersLabel, /Third Party/);
  // pctBest spans EVERY provider, not just the Δ pair.
  assert.equal(seattle.pctBest, 99);
  // ...but the Δ still compares exactly the declared pair.
  assert.deepEqual(gridDeltaPair(), ["mapillary", "gsv"]);
  assert.equal(Math.round(seattle.deltaPct * 10) / 10, 10.3);
  assert.equal(seattle.deltaPct_thirdparty, undefined);
});

test("pivotGridRows: missing stats become nulls, never NaN or 'undefined'", () => {
  const row = rowFor(payload({ provider: "mapillary", city_id: "x--y", city: "X" }), "x--y");
  assert.equal(row.pct_mapillary, null);
  assert.equal(row.pctAny_mapillary, null);
  assert.equal(row.medianAge_mapillary, null);
  assert.equal(row.panos_mapillary, null);
  assert.equal(row.collected_mapillary, null);
  assert.equal(row.snapshots_mapillary, null); // zero runs reads as absent, not "0"
  assert.equal(row.gridSpanLabel, null);
});

test("pivotGridRows: an empty or missing payload yields no rows rather than throwing", () => {
  assert.deepEqual(pivotGridRows({ cities: [] }).rows, []);
});

test("pivotGridRows: records with no city_id stay DISTINCT rows rather than merging", () => {
  // Folding is keyed on city_id, so records without one cannot be folded with
  // each other either — sharing a "" key merged unrelated cities into a single
  // "Unknown" row and made the catalog look smaller than it is. Latent (the
  // published v3 aggregate always carries an id), which is why the pivot also
  // warns rather than absorbing it silently.
  const warnings = [];
  const realWarn = console.warn;
  console.warn = (...args) => warnings.push(args[0]);
  try {
    const { rows } = pivotGridRows(
      payload(
        { provider: "gsv", city: "A", coverage_rate_percent: 10 },
        { provider: "gsv", city: "B", coverage_rate_percent: 90 },
        SEATTLE_GSV
      )
    );
    assert.equal(rows.length, 3);
    const unknown = rows.filter((r) => r.label === "Unknown");
    assert.equal(unknown.length, 2);
    assert.deepEqual(
      unknown.map((r) => r.pct_gsv).sort((a, b) => a - b),
      [10, 90],
      "the two id-less records collapsed into one row"
    );
    assert.equal(warnings.length, 2, "the pivot absorbed it silently");
  } finally {
    console.warn = realWarn;
  }
});

// --- columns / presets / invariants -----------------------------------------

test("every sortable column key exists on a row model", () => {
  // Guards the column/model seam: a sortable column with no matching model
  // field would silently sort every row as null. Asserted against the build
  // for the payload's OWN providers, and again against a payload carrying
  // every registered one, so the seam holds narrowed and wide.
  for (const raw of [payload(SEATTLE_GSV, SEATTLE_MAPILLARY), fullRegistryPayload()]) {
    const { row, columns } = buildFor(raw, "seattle--wa");
    for (const col of columns.filter((c) => c.sortable !== false)) {
      assert.ok(col.key in row, `row model is missing ${col.key}`);
    }
  }
  assert.ok(GRID_COLUMNS.some((c) => c.key === GRID_DEFAULT_SORT.key));
});

test("every column can render a cell, including from a fully null row model", () => {
  // The header and the body are both generated from GRID_COLUMNS now, so a
  // column that throws on a sparse record takes the whole table down rather
  // than rendering one bad cell.
  const sparse = rowFor(payload({ provider: "gsv", city_id: "x--y", city: "X" }), "x--y");
  for (const col of GRID_COLUMNS) {
    assert.equal(typeof col.cell, "function", `${col.key} has no cell renderer`);
    assert.match(col.cell(sparse), /^<t[hd][\s>]/, `${col.key} did not render a cell`);
  }
});

test("every preset names only real columns", () => {
  const keys = new Set(GRID_COLUMNS.map((c) => c.key));
  for (const preset of GRID_PRESETS) {
    for (const key of preset.columns) {
      assert.ok(keys.has(key), `preset ${preset.id} names unknown column ${key}`);
    }
  }
});

test("grouped columns carry a group on EVERY member, and only leaves are sortable keys", () => {
  // theadHtml collapses a run of same-group columns into one colgroup cell and
  // takes the label from the first VISIBLE member, which only works if every
  // member repeats it.
  const groups = new Map();
  for (const col of GRID_COLUMNS) {
    if (!col.group) continue;
    assert.ok(col.group.id && col.group.label, `${col.key} has an incomplete group`);
    const seen = groups.get(col.group.id);
    if (seen) assert.equal(seen, col.group.label, `${col.group.id} members disagree on the label`);
    else groups.set(col.group.id, col.group.label);
  }
  // The registry has three providers, so each per-provider group has three
  // leaves (plus a Δ where one is declared).
  const cov = GRID_COLUMNS.filter((c) => c.group?.id === "cov");
  assert.deepEqual(cov.map((c) => c.key), [
    "pct_gsv",
    "pct_mapillary",
    "pct_thirdparty",
    "deltaPct",
  ]);
  // Panorama counts are census-vs-sample and get NO Δ, ever.
  const panos = GRID_COLUMNS.filter((c) => c.group?.id === "panos");
  assert.equal(panos.length, 3);
  assert.ok(!panos.some((c) => c.key.startsWith("delta")));
  assert.match(panos[0].label, /GSV \(sample\)/);
  assert.match(panos[1].label, /Mapillary \(census\)/);
  // ...and a provider with no declared counting model just reads its name.
  assert.equal(panos[2].label, "Third Party");
});

test("grouped leaves carry a self-contained pickerLabel", () => {
  // A leaf's own label is a provider name repeated under every metric group —
  // unambiguous in the header, meaningless in the picker's flat list.
  const leaf = GRID_COLUMNS.find((c) => c.key === "pct_mapillary");
  assert.equal(leaf.label, "Mapillary");
  assert.equal(leaf.pickerLabel, "Grid coverage (%) — Mapillary");
  const delta = GRID_COLUMNS.find((c) => c.key === "deltaPct");
  assert.equal(delta.pickerLabel, "Grid coverage (%) — Δ");
});

test("the age Δ column says which sign means fresher", () => {
  // The one place the sign is counter-intuitive: a NEGATIVE age difference is
  // Mapillary being newer. The column title is where that is stated.
  const delta = GRID_COLUMNS.find((c) => c.key === "deltaMedianAge");
  assert.match(delta.title, /NEGATIVE means Mapillary is fresher/);
});

// --- cells ------------------------------------------------------------------

test("deltaCellHtml: signs positives, em-dashes nulls, marks an exact tie", () => {
  assert.match(deltaCellHtml(10.3, { unit: " pp" }), />\+10\.3 pp</);
  assert.match(deltaCellHtml(10.3, { unit: " pp" }), /delta-pos/);
  assert.match(deltaCellHtml(-1.3, { unit: " yrs" }), />-1\.3 yrs</);
  assert.match(deltaCellHtml(-1.3), /delta-neg/);
  assert.match(deltaCellHtml(0), /delta-zero/);
  assert.match(deltaCellHtml(0), />0\.0</);
  assert.equal(deltaCellHtml(null), `<td class="delta-cell">—</td>`);
});

test("gridRowHtml: one cell per column, with the city linking to city.html", () => {
  const html = gridRowHtml(rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa"));
  const cells = (html.match(/<t[hd][\s>]/g) || []).length;
  assert.equal(cells, GRID_COLUMNS.length);
  assert.match(
    html,
    /<th scope="row" title="Seattle, Washington, United States"><a class="streets-view-link" href="city\.html\?file=seattle--wa_width_5000_height_5000_step_20_2026-07-05\.csv\.gz"/
  );
  assert.match(html, /51\.2%/);
  assert.match(html, /61\.5%/);
  assert.match(html, />\+10\.3 pp</);
});

test("EVERY per-provider cell opens THAT provider's series", () => {
  // city.html derives its provider from the run filename, so these cells are
  // the only place a reader can open a specific series — the City cell can
  // only ever open one of them. Asserted across the whole group set rather
  // than on one column, since a group added later must not quietly opt out.
  const row = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  const perProvider = GRID_COLUMNS.filter((c) => c.group && !c.key.startsWith("delta"));
  assert.ok(perProvider.length >= 12, "expected several per-provider groups");

  for (const col of perProvider) {
    const provider = col.key.slice(col.key.lastIndexOf("_") + 1);
    const cell = col.cell(row);
    if (provider === "thirdparty") {
      // Never collected this city: a plain cell, not a link to nowhere.
      assert.doesNotMatch(cell, /href=/, `${col.key} linked with no run behind it`);
      continue;
    }
    const expected =
      provider === "gsv"
        ? "seattle--wa_width_5000_height_5000_step_20_2026-07-05.csv.gz"
        : "seattle--wa_width_5000_height_5000_step_20_mapillary_2026-07-05.csv.gz";
    assert.match(
      cell,
      new RegExp(`href="city\\.html\\?file=${expected.replace(/\./g, "\\.")}"`),
      `${col.key} does not open ${provider}'s run`
    );
    assert.match(cell, /class="provider-cell-link"/, `${col.key} is not the whole-cell link`);
  }
});

test("a Δ cell is never a link — it belongs to no one provider", () => {
  const row = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  for (const col of GRID_COLUMNS.filter((c) => c.key.startsWith("delta"))) {
    assert.doesNotMatch(col.cell(row), /href=/, `${col.key} should not be a link`);
  }
});

test("a per-provider cell renders unlinked when that provider has no published run", () => {
  const row = rowFor(
    payload({ ...SEATTLE_GSV, data_file: null, latest_run_date: "2026-07-05" }),
    "seattle--wa"
  );
  const cell = GRID_COLUMNS.find((c) => c.key === "collected_gsv").cell(row);
  assert.match(cell, />2026-07-05</);
  assert.doesNotMatch(cell, /href=/);
});

test("the per-provider link names the provider and its date, for hover and AT", () => {
  const row = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  const cell = GRID_COLUMNS.find((c) => c.key === "pct_mapillary").cell(row);
  assert.match(cell, /title="Open Mapillary · 2026-07-05 for this city"/);
});

test("gridRowHtml: null stats render em dashes and no link", () => {
  const html = gridRowHtml(rowFor(payload({ provider: "gsv", city_id: "x--y", city: "X" }), "x--y"));
  assert.match(html, /—/);
  assert.doesNotMatch(html, /href="city\.html/);
  assert.doesNotMatch(html, /coverage-bar/);
});

test("gridRowHtml: city names are HTML-escaped (OSM data is publicly editable)", () => {
  const html = gridRowHtml(
    rowFor(payload({ provider: "gsv", city_id: "x--y", city: "<script>alert(1)</script>" }), "x--y")
  );
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

// --- GRID_FILTERS -----------------------------------------------------------

test("GRID_FILTERS: 'Collected by' offers every COLLECTED provider plus the arity option", () => {
  // The arity option replaced the old "Multiple providers" checkbox: with the
  // pivot, "collected by 2+ providers" is exactly "this row's Δ columns are
  // populated", which is what the checkbox was really asking.
  const provider = GRID_FILTERS.find((f) => f.key === "provider");
  assert.equal(provider.label, "Collected by");
  // The full-registry build offers all three...
  assert.deepEqual(
    provider.options.map((o) => o.value),
    ["gsv", "mapillary", "thirdparty", "multi"]
  );
  // ...but a payload that carries no thirdparty run does not, since choosing
  // it would match no rows AND scope every slider onto an all-null field.
  const { filters } = buildFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  assert.deepEqual(
    filters.find((f) => f.key === "provider").options.map((o) => o.value),
    ["gsv", "mapillary", "multi"]
  );

  const both = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  const one = rowFor(payload(BEND_GSV), "bend--or");
  assert.ok(provider.test(both, "gsv"));
  assert.ok(provider.test(both, "mapillary"));
  assert.ok(!provider.test(one, "mapillary"));
  assert.ok(provider.test(both, "multi"));
  assert.ok(!provider.test(one, "multi"));
});

test("a REGISTERED but uncollected provider gets no columns, presets or scope option", () => {
  // The registry is not the payload (issue #250 review). KartaView is
  // registered (#225/#251) and, since #248, an OPT-IN scheduler channel, so the
  // published aggregate carries no KartaView city until one is enrolled —
  // "registered" and "collected" stay different sets either way. Fanning the leaves
  // out over the registry put six em-dash columns on this page, three of them
  // in the default preset, plus a "Collected by → KartaView" option matching
  // zero rows. That select is also the numeric SCOPE, so choosing it pointed
  // every slider at an all-null field, whose empty domain then falls back to
  // the descriptor's min/max — an arbitrary 0–1 axis on the age filter.
  const { columns, filters, providers } = buildFor(
    payload(SEATTLE_GSV, SEATTLE_MAPILLARY),
    "seattle--wa"
  );
  assert.deepEqual(providers, ["gsv", "mapillary"]);

  assert.deepEqual(
    columns.filter((c) => c.key.endsWith("_thirdparty")),
    [],
    "uncollected provider still has leaf columns"
  );
  for (const preset of buildGridPresets(columns)) {
    for (const key of preset.columns) {
      assert.ok(!key.endsWith("_thirdparty"), `preset ${preset.id} names ${key}`);
    }
  }
  assert.ok(
    !filters.find((f) => f.key === "provider").options.some((o) => o.value === "thirdparty"),
    "uncollected provider is still offered as a scope"
  );

  // The fan-out itself is intact — collect that provider and the columns are
  // there, with no edit here. That is the half this narrowing must not break.
  assert.ok(GRID_COLUMNS.some((c) => c.key === "pct_thirdparty"));
  const wide = buildFor(fullRegistryPayload(), "seattle--wa");
  assert.deepEqual(wide.providers, ["gsv", "mapillary", "thirdparty"]);
  assert.ok(wide.columns.some((c) => c.key === "pct_thirdparty"));
  assert.ok(
    wide.filters.find((f) => f.key === "provider").options.some((o) => o.value === "thirdparty")
  );
});

test("the Panoramas leaf says HOW each provider counts, and the group title names nobody", () => {
  // The parenthetical is the whole reason this group is safe to show: it is
  // the only thing telling a reader that a census count and a sampled count
  // are not subtractable, which is also why the group has no Δ. KartaView
  // shipped without a panoCountingModel (#295) and so rendered a bare label
  // beside the largest number in its row.
  const cols = buildGridColumns(["gsv", "mapillary", "thirdparty"]);
  const byKey = Object.fromEntries(cols.map((c) => [c.key, c]));
  assert.equal(byKey.panos_gsv.label, "GSV (sample)");
  assert.equal(byKey.panos_mapillary.label, "Mapillary (census)");
  // The registry stub omits the field, and the fallback is the bare label —
  // this is the shape the fix removes from the REAL registry, kept here so the
  // fallback itself stays covered.
  assert.equal(byKey.panos_thirdparty.label, "Third Party");

  // The group header states the rule instead of enumerating who is in it, so
  // it cannot go stale when a provider is added.
  const groupTitle = byKey.panos_gsv.group.title;
  for (const name of ["GSV", "Mapillary", "Google", "KartaView"]) {
    assert.doesNotMatch(groupTitle, new RegExp(name), `group title should not name ${name}`);
  }
  assert.match(groupTitle, /NOT comparable/);
});

test("the Panoramas leaf discloses the copyright filter, driven by the flag", () => {
  // The two numbers in a GSV row have DIFFERENT copyright denominators:
  // `panos_gsv` is adaptCityRecord's `unique_google_panos` (the official-fleet
  // subset), while `pct_gsv` counts every PRESENT grid point regardless of who
  // shot it. The group title used to disclose that ("official © Google panos
  // for GSV") and lost it when the enumeration was rewritten into a rule
  // (#296 review), leaving the page silent about a filter it applies.
  //
  // Sets the flag rather than reading whatever the registry declares, so the
  // clause is pinned to the CONDITION and not to gsv happening to be the only
  // filtered provider today.
  const titleFor = (hasCopyrightFilter) => {
    const restore = global.PROVIDERS.mapillary.hasCopyrightFilter;
    try {
      global.PROVIDERS.mapillary.hasCopyrightFilter = hasCopyrightFilter;
      return buildGridColumns(["mapillary"]).find((c) => c.key === "panos_mapillary").title;
    } finally {
      global.PROVIDERS.mapillary.hasCopyrightFilter = restore;
    }
  };
  assert.match(titleFor(true), /official-fleet/i);
  assert.match(titleFor(true), /different denominators/i);
  assert.doesNotMatch(titleFor(false), /official-fleet/i);
  // The counting-model half survives the clause rather than being replaced by it.
  assert.match(titleFor(true), /every 360° panorama found in the search area/);

  // And it is live on the shipped registry, where gsv is the filtered one.
  assert.match(
    buildGridColumns(["gsv"]).find((c) => c.key === "panos_gsv").title,
    /official-fleet/i
  );
});

test("no per-provider leaf tooltip names a DIFFERENT provider, on ANY capability branch", () => {
  // The defect this pins (#295): `title: groupTitle` gave every leaf one
  // shared string, so "flat/perspective imagery (Mapillary)" was attached to
  // KartaView's any-imagery column — whose flat imagery is in fact the larger
  // half of its data. Checked across the whole registry so adding a provider
  // cannot reintroduce it.
  //
  // Swept over the CAPABILITY FLAGS as well as the providers, which is what
  // makes it a guard rather than a spot check (#296 review). Every tooltip
  // that could name a provider is behind one of these two flags, so a sweep
  // reading the registry's own values evaluates one branch per provider and
  // leaves the other free to hardcode a name — mutation-verified: with the
  // real flags, planting "(Mapillary)" in the flat-imagery branch survives,
  // because the only provider reaching that branch IS Mapillary. Forcing the
  // flags means every provider is tested through every branch.
  const providers = ["gsv", "mapillary", "thirdparty"];
  const names = { gsv: /GSV|Google Street View/, mapillary: /Mapillary/, thirdparty: /Third Party/ };
  const saved = providers.map((p) => ({ ...global.PROVIDERS[p] }));
  try {
    for (const hasFlatImagery of [false, true]) {
      for (const hasCopyrightFilter of [false, true]) {
        for (const p of providers) {
          Object.assign(global.PROVIDERS[p], { hasFlatImagery, hasCopyrightFilter });
        }
        const where = `hasFlatImagery=${hasFlatImagery} hasCopyrightFilter=${hasCopyrightFilter}`;
        for (const col of buildGridColumns(providers)) {
          const owner = providers.find((q) => col.key.endsWith(`_${q}`));
          if (!owner) continue; // Δ and the shared grid-geometry columns own no provider.
          for (const [other, pattern] of Object.entries(names)) {
            if (other === owner) continue;
            assert.doesNotMatch(
              col.title ?? "",
              pattern,
              `[${where}] ${col.key} tooltip names ${other}: ${col.title}`
            );
          }
        }
      }
    }
  } finally {
    providers.forEach((p, i) => {
      global.PROVIDERS[p] = saved[i];
    });
  }
});

test("the Any-imagery tooltip is derived from hasFlatImagery, not from a provider name", () => {
  // The flag drives the sentence, so the test SETS it rather than leaning on
  // whatever the registry happens to declare — this file's stub omits it
  // entirely, and a test that read the shipped value would pass on the
  // fallback while proving nothing about the branch.
  const titleFor = (hasFlatImagery) => {
    const restore = global.PROVIDERS.gsv.hasFlatImagery;
    try {
      global.PROVIDERS.gsv.hasFlatImagery = hasFlatImagery;
      return buildGridColumns(["gsv"]).find((c) => c.key === "pctAny_gsv").title;
    } finally {
      global.PROVIDERS.gsv.hasFlatImagery = restore;
    }
  };
  // A 360°-only provider's any-imagery column IS its grid coverage, and the
  // tooltip has to say so or an identical pair of numbers looks broken.
  assert.match(titleFor(false), /[Ee]quals grid coverage/);
  assert.match(titleFor(true), /flat\/perspective/);
  // Each branch names its OWN provider — the misattribution this replaces.
  assert.match(titleFor(true), /Google Street View/);
});

test("the default preset's width tracks the COLLECTED providers, not the registry", () => {
  // Why the narrowing is a layout fact and not just a tidiness one: the default
  // view has to fit the page's content measure (1500px − the 280px sidebar)
  // without scrolling sideways, and it carries three grouped metrics, so each
  // extra provider is three more ~90px leaves.
  const widthFor = (providers) => buildGridPresets(buildGridColumns(providers))[0].columns.length;
  assert.equal(widthFor(["gsv", "mapillary", "thirdparty"]) - widthFor(["gsv", "mapillary"]), 3);
});

test("the Δ columns and the Δ filter go away when only one of the pair is collected", () => {
  // Already true for an unregistered provider; the point here is that a
  // REGISTERED one with nothing in this payload behaves the same way, rather
  // than producing a column of em-dashes and a slider over an empty domain.
  const { row, columns, filters } = buildFor(payload(BEND_GSV), "bend--or");
  assert.equal(gridDeltaPair(["gsv"]), null);
  assert.deepEqual(
    columns.filter((c) => c.key.startsWith("delta")),
    []
  );
  assert.ok(!filters.some((f) => f.key === "dcov"));
  // Null, not missing: "no Δ here" is the same answer whether the pair is
  // half-collected in this city or absent from the whole payload.
  assert.equal(row.deltaPct, null);
  assert.equal(row.deltaPctAny, null);
  assert.equal(row.deltaMedianAge, null);
});

test("GRID_FILTERS: the old ?provider=gsv links still select the same rows", () => {
  // The value vocabulary is unchanged apart from the addition, so a link made
  // before the pivot keeps working — it now selects the CITY rather than the
  // series, which is the same set of cities.
  const provider = GRID_FILTERS.find((f) => f.key === "provider");
  assert.ok(provider.options.some((o) => o.value === "gsv"));
  assert.ok(provider.test(rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa"), "gsv"));
});

test("GRID_FILTERS: numeric filters read best-across-providers, and are histogram sliders", () => {
  const byKey = Object.fromEntries(GRID_FILTERS.map((f) => [f.key, f]));
  assert.equal(byKey.cov.field, "pctBest");
  assert.equal(byKey.age.field, "medianAgeBest");
  assert.equal(byKey.dcov.field, "deltaPct");
  for (const key of ["cov", "age", "dcov"]) {
    assert.equal(byKey[key].type, "histogram-range", `${key} is not a histogram filter`);
  }
  // The Δ filter has no declared bounds: a difference is signed and its extent
  // is a property of the data, not of the metric.
  assert.equal(byKey.dcov.min, undefined);
  assert.equal(byKey.dcov.max, undefined);
});

test("GRID_FILTERS: every filter's field exists on a row model", () => {
  const row = rowFor(payload(SEATTLE_GSV, SEATTLE_MAPILLARY), "seattle--wa");
  for (const filter of GRID_FILTERS) {
    if (!filter.field) continue;
    assert.ok(filter.field in row, `filter ${filter.key} reads a missing field ${filter.field}`);
  }
});

// --- the provider scope (issue #250 follow-up) ------------------------------

test("GRID_FILTERS: the numeric filters follow the Collected by scope", () => {
  const byKey = Object.fromEntries(GRID_FILTERS.map((f) => [f.key, f]));

  // Unscoped: best across a city's providers, and the label names the
  // quantifier rather than leaving "best" to be guessed at.
  assert.equal(byKey.cov.fieldFor({}), "pctBest");
  assert.equal(byKey.cov.labelFor({}), "Grid coverage % — any provider reaches");
  assert.equal(byKey.age.fieldFor({}), "medianAgeBest");
  // Unscoped "best" age is the MINIMUM, which "best" alone would not convey.
  assert.equal(byKey.age.labelFor({}), "Median age (yrs) — freshest of any");

  // Scoped: that provider's own column.
  assert.equal(byKey.cov.fieldFor({ provider: "mapillary" }), "pct_mapillary");
  assert.equal(byKey.cov.labelFor({ provider: "mapillary" }), "Grid coverage % — Mapillary");
  assert.equal(byKey.age.fieldFor({ provider: "gsv" }), "medianAge_gsv");

  // "2+ providers" names no single provider, so it stays best-across.
  assert.equal(byKey.cov.fieldFor({ provider: "multi" }), "pctBest");
  // ...as does a scope naming a provider that is not registered.
  assert.equal(byKey.cov.fieldFor({ provider: "nope" }), "pctBest");
});

test("GRID_FILTERS: a third registered provider is scopable with no edit here", () => {
  const cov = GRID_FILTERS.find((f) => f.key === "cov");
  assert.equal(cov.fieldFor({ provider: "thirdparty" }), "pct_thirdparty");
  // shortLabel is absent from that registry entry — the fallback is `label`.
  assert.equal(cov.labelFor({ provider: "thirdparty" }), "Grid coverage % — Third Party");
});

test("GRID_FILTERS: the Δ filter is NOT scoped — it is a question about the pair", () => {
  const dcov = GRID_FILTERS.find((f) => f.key === "dcov");
  assert.equal(dcov.fieldFor, undefined);
  assert.equal(dcov.field, "deltaPct");
});

test("every scoped field a filter can resolve to exists on a row model", () => {
  // The seam the scope introduces: a fieldFor that names a key the pivot does
  // not build would filter every row out with nothing to see. The scopes worth
  // asserting are the ones the select can actually OFFER — narrowed to the
  // payload's providers — plus the two that name no single provider.
  for (const raw of [payload(SEATTLE_GSV, SEATTLE_MAPILLARY), fullRegistryPayload()]) {
    const { row, filters, providers } = buildFor(raw, "seattle--wa");
    const scopes = [{}, { provider: "multi" }, ...providers.map((p) => ({ provider: p }))];
    for (const filter of filters) {
      if (!filter.fieldFor) continue;
      for (const values of scopes) {
        const field = filter.fieldFor(values);
        assert.ok(
          field in row,
          `${filter.key} under ${JSON.stringify(values)} reads missing ${field}`
        );
      }
    }
  }
});
