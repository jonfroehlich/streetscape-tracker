// Offline unit tests for the pure helpers in streets.js — the street-level
// coverage page (issues #99/#155), pivoted to one row per (city, network) in
// issue #250. Run with `npm test` (Node's built-in test runner) — no network,
// no jsdom.
//
// In the browser these helpers read shared globals from streetscape-utils.js;
// here we stub the ones they touch. `document` is left undefined on purpose:
// streets.js only registers its DOMContentLoaded listener when one exists.

const test = require("node:test");
const assert = require("node:assert/strict");

// A THIRD provider the page has never heard of: every per-provider column,
// filter option and row key is generated from this registry, so a hardcoded
// pair fails here rather than the day one is really registered (issue #225).
global.PROVIDERS = {
  gsv: { label: "Google Street View", shortLabel: "GSV" },
  mapillary: { label: "Mapillary", shortLabel: "Mapillary" },
  thirdparty: { label: "Third Party" },
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

// streets.js delegates to the shared table machinery, which it reads as
// browser globals — mirror that here (must precede the streets.js require, and
// must follow PROVIDERS, which the provider-column helpers read).
Object.assign(global, require("../table-utils.js"));

const {
  cityLabel,
  indexCitiesByProvider,
  pivotStreetWalks,
  sortRows,
  num,
  walkChangeCellHtml,
  walkRowHtml,
  streetDeltaPair,
  walkProvidersIn,
  buildStreetColumns,
  buildStreetPresets,
  buildStreetFilters,
  updateStreetsCaption,
  STREET_COLUMNS,
  STREET_PRESETS,
  STREET_FILTERS,
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
    {
      provider: "gsv",
      city_id: "seattle--wa",
      city: "Seattle",
      data_file: { filename: "seattle_gsv.csv.gz" },
    },
    { provider: "gsv", city_id: "bend--or", city: "Bend" },
    {
      provider: "mapillary",
      city_id: "seattle--wa",
      city: "Seattle",
      data_file: { filename: "seattle_mapillary.csv.gz" },
    },
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
  assert.equal(index.size, 4);
});

test("indexCitiesByProvider: also indexes by bare city_id for the name fallback", () => {
  assert.equal(indexCitiesByProvider(RAW_CITIES, ["gsv"]).get("seattle--wa").city, "Seattle");
});

test("indexCitiesByProvider: a missing aggregate yields an empty index, not a throw", () => {
  // The page still renders its table (unlinked) when cities.json.gz fails.
  assert.equal(indexCitiesByProvider(null, ["gsv"]).size, 0);
});

// --- pivotStreetWalks ------------------------------------------------------

const SEATTLE_GSV_WALK = {
  city_id: "seattle--wa",
  provider: "gsv",
  network_type: "drive",
  run_date: "2026-07-26",
  spacing_m: 15,
  coverage_pct_by_length: 98.4,
  coverage_pct_by_length_any: 98.4,
  length_km: 873.2,
  length_km_covered: 859.2,
  length_km_covered_any: 859.2,
  median_covered_age_years: 2.3,
  edges: 33597,
  edges_fully_covered: 32391,
};

const SEATTLE_MAPILLARY_WALK = {
  ...SEATTLE_GSV_WALK,
  provider: "mapillary",
  coverage_pct_by_length: 61.2,
  coverage_pct_by_length_any: 74.8,
  length_km: 873.4, // each walk re-derives it from the frozen graph
  length_km_covered: 534.5,
  length_km_covered_any: 653.2,
  median_covered_age_years: 1.1,
  edges_fully_covered: 20100,
};

const SEATTLE_BROAD_WALK = {
  ...SEATTLE_GSV_WALK,
  network_type: "all_public",
  coverage_pct_by_length: 71.0,
  length_km: 1402.9,
};

const INDEX = indexCitiesByProvider(RAW_CITIES, ["gsv", "mapillary"]);

function rowsFor(...walks) {
  return pivotStreetWalks(walks, INDEX);
}

/**
 * What the page would actually render for a manifest: the rows, plus the
 * columns and filters built from the providers those walks CONTAIN.
 *
 * The module-level STREET_COLUMNS/STREET_FILTERS are the full-registry build —
 * every provider the site knows about, walked or not — so asserting a
 * manifest's row model against them asks for keys the pivot deliberately does
 * not build (issue #250 review).
 */
function buildFor(...walks) {
  const providers = walkProvidersIn(walks);
  return {
    providers,
    rows: pivotStreetWalks(walks, INDEX),
    columns: buildStreetColumns(providers),
    filters: buildStreetFilters(providers),
  };
}

/** A walk for every registered provider, so the full-registry build applies. */
function fullRegistryWalks() {
  return [
    SEATTLE_GSV_WALK,
    SEATTLE_MAPILLARY_WALK,
    { ...SEATTLE_MAPILLARY_WALK, provider: "thirdparty" },
  ];
}

test("pivotStreetWalks: providers fold into one row; networks do NOT", () => {
  // Two providers walk the SAME sample points on the same frozen network, so
  // their numbers are comparable and belong side by side. Two NETWORKS divide
  // by different street-km denominators, so they stay separate rows.
  const rows = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK, SEATTLE_BROAD_WALK);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r) => r.rowKey), ["seattle--wa|drive", "seattle--wa|all_public"]);

  const drive = rows[0];
  assert.equal(drive.networkLabel, "Roads");
  assert.deepEqual(drive.providers, ["gsv", "mapillary"]);
  assert.equal(drive.providersLabel, "GSV, Mapillary");
  assert.equal(drive.pct_gsv, 98.4);
  assert.equal(drive.pct_mapillary, 61.2);
  assert.equal(drive.pctAny_mapillary, 74.8);
  assert.equal(drive.medianAge_mapillary, 1.1);
  assert.equal(drive.lengthKmCovered_gsv, 859.2);
  assert.equal(drive.lengthKmCoveredAny_mapillary, 653.2);
  assert.equal(drive.fullyCovered_mapillary, 20100);
  assert.equal(drive.spacing_gsv, 15);

  const broad = rows[1];
  assert.equal(broad.networkLabel, "Roads + paths");
  assert.deepEqual(broad.providers, ["gsv"]);
  assert.equal(broad.pct_gsv, 71.0);
  assert.equal(broad.pct_mapillary, null);
});

test("pivotStreetWalks: rowKey is what makes a city's two networks distinct rows", () => {
  // city_id alone no longer identifies a row, which is why the table is built
  // with tieKey: "rowKey" — ties would otherwise break arbitrarily between a
  // city's own two networks.
  const rows = rowsFor(SEATTLE_GSV_WALK, SEATTLE_BROAD_WALK);
  assert.equal(new Set(rows.map((r) => r.cityId)).size, 1);
  assert.equal(new Set(rows.map((r) => r.rowKey)).size, 2);
});

test("pivotStreetWalks: a walk with no network_type defaults to the drive series", () => {
  // Walks published before network types existed carry no field at all, and
  // must land on the scheduled series rather than in a row of their own.
  const legacy = { ...SEATTLE_GSV_WALK };
  delete legacy.network_type;
  const rows = rowsFor(legacy, SEATTLE_MAPILLARY_WALK);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].networkType, "drive");
  assert.equal(rows[0].networkLabel, "Roads");
});

test("pivotStreetWalks: Δ is null unless BOTH providers walked this (city, network)", () => {
  const both = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  assert.equal(Math.round(both.deltaPct * 10) / 10, -37.2); // 61.2 − 98.4
  assert.equal(Math.round(both.deltaPctAny * 10) / 10, -23.6); // 74.8 − 98.4
  assert.deepEqual(streetDeltaPair(), ["mapillary", "gsv"]);

  const gsvOnly = rowsFor(SEATTLE_GSV_WALK)[0];
  assert.equal(gsvOnly.deltaPct, null);
  assert.equal(gsvOnly.deltaPctAny, null);
});

test("pivotStreetWalks: pctBest is the max across providers", () => {
  const row = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  assert.equal(row.pctBest, 98.4);
  assert.equal(rowsFor({ city_id: "x", provider: "gsv" })[0].pctBest, null);
});

test("pivotStreetWalks: network properties collapse to one field, first walk wins", () => {
  // Street km and edge count describe the OSM network, not a provider — but
  // each walk re-derives them from the frozen graph and can differ slightly,
  // which is why the column's title says so instead of pretending otherwise.
  const row = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  assert.equal(row.lengthKm, 873.2);
  assert.equal(row.edges, 33597);

  // A first walk carrying no length must not shadow a later one that does
  // (pre-v12 walks are NULL, "not measured", never zero).
  const noLength = { ...SEATTLE_GSV_WALK, length_km: null, edges: null };
  const salvaged = rowsFor(noLength, SEATTLE_MAPILLARY_WALK)[0];
  assert.equal(salvaged.lengthKm, 873.4);
});

test("pivotStreetWalks: the label may come from any provider's record; the LINK may not", () => {
  // A city can be walked by a provider it has no grid run for (a Mapillary
  // walk costs a handful of tiles and lands before a full census does). Name
  // it properly, but never link to another provider's run: city.html derives
  // its provider from the filename and would open the wrong series.
  const row = pivotStreetWalks(
    [{ city_id: "seattle--wa", provider: "thirdparty", coverage_pct_by_length: 5 }],
    INDEX
  )[0];
  assert.equal(row.label, "Seattle");
  assert.equal(row.filename_thirdparty, null, "the bare-city_id fallback must not supply a link");
  assert.equal(row.filename, null);
});

test("pivotStreetWalks: the City link is the first REGISTERED provider with a run", () => {
  const both = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  assert.equal(both.filename_gsv, "seattle_gsv.csv.gz");
  assert.equal(both.filename_mapillary, "seattle_mapillary.csv.gz");
  assert.equal(both.filename, "seattle_gsv.csv.gz");

  const mapillaryOnly = rowsFor(SEATTLE_MAPILLARY_WALK)[0];
  assert.equal(mapillaryOnly.filename, "seattle_mapillary.csv.gz");
});

test("pivotStreetWalks: falls back to city_id and nulls when the join missed", () => {
  const row = rowsFor({ city_id: "ghost--xx", provider: "gsv" })[0];
  assert.equal(row.label, "ghost--xx");
  assert.equal(row.filename, null);
  assert.equal(row.pct_gsv, null);
  assert.equal(row.pctAny_gsv, null);
});

test("pivotStreetWalks: any-imagery coverage is null on walks predating the field", () => {
  // Pre-existing manifests carry no coverage_pct_by_length_any; the column
  // must read "no data", never silently mirror the 360° number.
  const row = rowsFor({ city_id: "x", provider: "gsv", coverage_pct_by_length: 80 })[0];
  assert.equal(row.pctAny_gsv, null);
});

// --- sortRows --------------------------------------------------------------

const SORT_ROWS = rowsFor(
  { ...SEATTLE_GSV_WALK, city_id: "c", coverage_pct_by_length: 50, run_date: "2026-01-01" },
  { ...SEATTLE_GSV_WALK, city_id: "a", coverage_pct_by_length: null, run_date: null },
  { ...SEATTLE_GSV_WALK, city_id: "b", coverage_pct_by_length: 98.4, run_date: "2026-05-05" },
  { ...SEATTLE_GSV_WALK, city_id: "d", coverage_pct_by_length: 50, run_date: "2026-03-03" }
);

test("sortRows: numeric desc puts the best first, nulls last in both directions", () => {
  assert.deepEqual(sortRows(SORT_ROWS, "pct_gsv", "desc").map((r) => r.cityId), ["b", "c", "d", "a"]);
  const asc = sortRows(SORT_ROWS, "pct_gsv", "asc").map((r) => r.cityId);
  assert.equal(asc[asc.length - 1], "a", "absent is not small");
  assert.deepEqual(asc, ["c", "d", "b", "a"]);
});

test("sortRows: ties break on rowKey, so re-sorting is stable", () => {
  assert.deepEqual(sortRows(SORT_ROWS, "pct_gsv", "desc").slice(1, 3).map((r) => r.cityId), ["c", "d"]);
  assert.deepEqual(sortRows(SORT_ROWS, "pct_gsv", "asc").slice(0, 2).map((r) => r.cityId), ["c", "d"]);
});

test("sortRows: text columns sort lexically in both directions", () => {
  assert.deepEqual(sortRows(SORT_ROWS, "label", "asc").map((r) => r.cityId), ["a", "b", "c", "d"]);
  assert.deepEqual(sortRows(SORT_ROWS, "label", "desc").map((r) => r.cityId), ["d", "c", "b", "a"]);
});

test("sortRows: an unknown key falls back to the first column, never throws", () => {
  assert.equal(sortRows(SORT_ROWS, "nope", "asc").length, SORT_ROWS.length);
});

test("sortRows: does not mutate its input", () => {
  const before = SORT_ROWS.map((r) => r.cityId);
  sortRows(SORT_ROWS, "pct_gsv", "asc");
  assert.deepEqual(SORT_ROWS.map((r) => r.cityId), before);
});

// --- columns / presets / invariants ----------------------------------------

test("every sortable column key exists on a row model", () => {
  // Asserted against the build for the manifest's OWN providers, and again
  // against a manifest carrying every registered one, so the column/model
  // seam holds narrowed and wide.
  for (const walks of [[SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK], fullRegistryWalks()]) {
    const { rows, columns } = buildFor(...walks);
    for (const col of columns.filter((c) => c.sortable !== false)) {
      assert.ok(col.key in rows[0], `row model is missing ${col.key}`);
    }
  }
  assert.ok(STREET_COLUMNS.some((c) => c.key === DEFAULT_SORT.key));
});

test("the default sort is a VISIBLE column of the default preset", () => {
  // pctBest has no column of its own — it is a filter field — so sorting by it
  // would order the table by something the reader cannot see, which is exactly
  // what createSortableTable's fallback exists to prevent. The GSV leaf is the
  // deliberate, slightly asymmetric alternative.
  assert.equal(DEFAULT_SORT.key, "pct_gsv");
  assert.ok(STREET_PRESETS[0].columns.includes(DEFAULT_SORT.key));
});

test("every column can render a cell, including from a fully null row model", () => {
  const sparse = rowsFor({ city_id: "x", provider: "gsv" })[0];
  for (const col of STREET_COLUMNS) {
    assert.equal(typeof col.cell, "function", `${col.key} has no cell renderer`);
    assert.match(col.cell(sparse), /^<t[hd][\s>]/, `${col.key} did not render a cell`);
  }
});

test("every preset names only real columns", () => {
  const keys = new Set(STREET_COLUMNS.map((c) => c.key));
  for (const preset of STREET_PRESETS) {
    for (const key of preset.columns) {
      assert.ok(keys.has(key), `preset ${preset.id} names unknown column ${key}`);
    }
  }
});

test("a REGISTERED but unwalked provider gets no columns, presets or scope option", () => {
  // The registry is not the manifest (issue #250 review). KartaView is
  // registered but has no road walk at ALL — `build_streetwalk_rows` is
  // Mapillary-specific in three separate ways — so fanning the leaves out over
  // the registry put nine em-dash columns on this page plus a scope option
  // matching zero rows, which then pointed the sliders at an all-null field.
  const { columns, filters, providers } = buildFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK);
  assert.deepEqual(providers, ["gsv", "mapillary"]);

  assert.deepEqual(
    columns.filter((c) => c.key.endsWith("_thirdparty")),
    [],
    "unwalked provider still has leaf columns"
  );
  for (const preset of buildStreetPresets(columns)) {
    for (const key of preset.columns) {
      assert.ok(!key.endsWith("_thirdparty"), `preset ${preset.id} names ${key}`);
    }
  }
  assert.ok(
    !filters.find((f) => f.key === "provider").options.some((o) => o.value === "thirdparty"),
    "unwalked provider is still offered as a scope"
  );

  // The fan-out itself is intact — walk with that provider and the columns
  // appear, with no edit here.
  const wide = buildFor(...fullRegistryWalks());
  assert.deepEqual(wide.providers, ["gsv", "mapillary", "thirdparty"]);
  assert.ok(wide.columns.some((c) => c.key === "pct_thirdparty"));
  assert.ok(
    wide.filters.find((f) => f.key === "provider").options.some((o) => o.value === "thirdparty")
  );
});

test("the Δ columns go away when only one of the pair has walked", () => {
  const { rows, columns } = buildFor(SEATTLE_GSV_WALK);
  assert.equal(streetDeltaPair(["gsv"]), null);
  assert.deepEqual(
    columns.filter((c) => c.key.startsWith("delta")),
    []
  );
  // Null, not missing — same contract as the grid page.
  assert.equal(rows[0].deltaPct, null);
  assert.equal(rows[0].deltaPctAny, null);
});

test("the walk-to-walk change group has one column per provider and NO cross-provider Δ", () => {
  // "GSV improved 4 points and Mapillary improved 1" is two facts about two
  // series; their difference is not a third.
  const change = STREET_COLUMNS.filter((c) => c.group?.id === "change");
  assert.deepEqual(change.map((c) => c.key), [
    "changeDelta_gsv",
    "changeDelta_mapillary",
    "changeDelta_thirdparty",
  ]);
  assert.match(change[0].title, /Never a cross-provider comparison/);
  // ...while the coverage group DOES get one.
  assert.ok(STREET_COLUMNS.some((c) => c.key === "deltaPct" && c.group?.id === "cov"));
});

test("a row renders one cell per column", () => {
  const html = walkRowHtml(rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0]);
  const cells = (html.match(/<t[hd][\s>]/g) || []).length;
  assert.equal(cells, STREET_COLUMNS.length);
});

// --- num -------------------------------------------------------------------

test("num: renders an em dash for null/undefined rather than 'null'", () => {
  assert.equal(num(null), "—");
  assert.equal(num(undefined), "—");
  assert.equal(num(0), "0");
});

// --- cells -----------------------------------------------------------------

test("walkRowHtml: the City cell links with THIS row's network type", () => {
  // city.html selects the walk to draw by network type and defaults to
  // 'drive', so a broad row whose link omits ?network= opens a DIFFERENT walk
  // — or, for a city walked only broadly, falls back to the grid-attribution
  // artifact, a different metric entirely.
  const [drive, broad] = rowsFor(SEATTLE_GSV_WALK, SEATTLE_BROAD_WALK);
  assert.match(walkRowHtml(drive), /href="city\.html\?file=seattle_gsv\.csv\.gz&network=drive"/);
  assert.match(walkRowHtml(broad), /&network=all_public"/);
});

test("EVERY per-provider cell opens THAT provider's walk of THIS row's network", () => {
  // Asserted across the whole group set rather than on one column, since a
  // group added later must not quietly opt out of being a way in.
  const row = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  const perProvider = STREET_COLUMNS.filter((c) => c.group && !c.key.startsWith("delta"));
  assert.ok(perProvider.length >= 15, "expected several per-provider groups");

  for (const col of perProvider) {
    const provider = col.key.slice(col.key.lastIndexOf("_") + 1);
    const cell = col.cell(row);
    if (provider === "thirdparty") {
      assert.doesNotMatch(cell, /href=/, `${col.key} linked with no walk behind it`);
      continue;
    }
    assert.match(
      cell,
      new RegExp(`file=seattle_${provider}\\.csv\\.gz&network=drive`),
      `${col.key} does not open ${provider}'s walk of this network`
    );
    assert.match(cell, /class="provider-cell-link"/, `${col.key} is not the whole-cell link`);
  }
});

test("the per-provider link carries THIS row's network, not the default", () => {
  // city.html defaults to 'drive', so a broad row whose link omits ?network=
  // opens a different walk — or falls back to the grid-attribution artifact.
  const broad = rowsFor(SEATTLE_BROAD_WALK)[0];
  const cell = STREET_COLUMNS.find((c) => c.key === "pct_gsv").cell(broad);
  assert.match(cell, /&network=all_public"/);
  assert.match(cell, /title="Open GSV · 2026-07-26 · Roads \+ paths"/);
});

test("a Δ cell is never a link — it belongs to no one provider", () => {
  const row = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  for (const col of STREET_COLUMNS.filter((c) => c.key.startsWith("delta"))) {
    assert.doesNotMatch(col.cell(row), /href=/, `${col.key} should not be a link`);
  }
});

test("a per-provider cell is unlinked when that city has no run for that provider", () => {
  // The name fallback supplies a label, never a link (see indexCitiesByProvider):
  // city.html derives its provider from the filename, so following it would
  // open a different provider's series.
  const row = pivotStreetWalks(
    [{ city_id: "bend--or", provider: "mapillary", run_date: "2026-07-26" }],
    INDEX
  )[0];
  assert.equal(row.label, "Bend");
  const cell = STREET_COLUMNS.find((c) => c.key === "runDate_mapillary").cell(row);
  assert.match(cell, />2026-07-26</);
  assert.doesNotMatch(cell, /href=/);
});

test("walkRowHtml: a city missing from the aggregate still renders, without a link", () => {
  const html = walkRowHtml(rowsFor({ city_id: "ghost--xx", provider: "gsv" })[0]);
  assert.match(html, /ghost--xx/);
  assert.doesNotMatch(html, /href="city\.html/);
});

test("walkRowHtml: null stats render em dashes, and no coverage bar", () => {
  const html = walkRowHtml(
    rowsFor({ city_id: "x", provider: "gsv", coverage_pct_by_length: null, spacing_m: null })[0]
  );
  assert.match(html, /—/);
  assert.doesNotMatch(html, /coverage-bar/);
});

test("walkRowHtml: renders both providers' 360° and any-imagery cells", () => {
  const html = walkRowHtml(rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0]);
  assert.match(html, /98\.4%/);
  assert.match(html, /61\.2%/);
  assert.match(html, /74\.8%/);
});

test("walkRowHtml: the coverage bar width is clamped to 0–100%", () => {
  const html = walkRowHtml(rowsFor({ ...SEATTLE_GSV_WALK, coverage_pct_by_length: 137 })[0]);
  assert.match(html, /width:100%/);
});

test("walkRowHtml: city names are HTML-escaped (OSM data is publicly editable)", () => {
  const raw = { cities: [{ provider: "gsv", city_id: "x", city: "<script>alert(1)</script>" }] };
  const html = walkRowHtml(
    pivotStreetWalks([{ city_id: "x", provider: "gsv" }], indexCitiesByProvider(raw, ["gsv"]))[0]
  );
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

test("walkRowHtml: the label cell carries its full text as a title, for ellipsis truncation", () => {
  // Worldwide-frame labels (issue #115) run 60+ chars; the CSS truncates the
  // cell with an ellipsis, so the untruncated name needs to survive on hover.
  const html = walkRowHtml(rowsFor(SEATTLE_GSV_WALK)[0]);
  assert.match(html, /<th scope="row" title="Seattle">/);
});

// --- walkChangeCellHtml (issue #101) ---------------------------------------

const CHANGE_BLOCK = {
  from: "2026-04-01",
  to: "2026-07-26",
  edges_gained_coverage: 12,
  edges_lost_coverage: 3,
  coverage_pct_by_length_delta: 4.2,
  coverage_pct_by_length_any_delta: null,
  nearest_pano_date_changed: 40,
  diff_file: "seattle_streetwalkdiff_2026-04-01_to_2026-07-26.csv.gz",
};

test("pivotStreetWalks: the manifest change block lands on the walking provider", () => {
  const row = rowsFor({ ...SEATTLE_GSV_WALK, change: CHANGE_BLOCK }, SEATTLE_MAPILLARY_WALK)[0];
  assert.equal(row.changeDelta_gsv, 4.2);
  assert.equal(row.change_gsv.from, "2026-04-01");
  // ...and NOT on the other provider, whose own walk was a first walk.
  assert.equal(row.changeDelta_mapillary, null);
  assert.equal(row.change_mapillary, null);
});

test("walkChangeCellHtml: em dash for a first walk, signed pp figure with a churn title", () => {
  const first = rowsFor(SEATTLE_GSV_WALK)[0];
  assert.deepEqual(walkChangeCellHtml(first, "gsv"), { html: "—" });

  const changed = rowsFor({ ...SEATTLE_GSV_WALK, change: CHANGE_BLOCK })[0];
  const parts = walkChangeCellHtml(changed, "gsv");
  assert.equal(parts.html, "+4.2 pp");
  assert.match(parts.title, /Since 2026-04-01/);
  assert.match(parts.title, /12 streets gained/);
  assert.match(parts.title, /3 lost/);

  const negative = rowsFor({
    ...SEATTLE_GSV_WALK,
    change: { ...CHANGE_BLOCK, coverage_pct_by_length_delta: -0.3 },
  })[0];
  assert.equal(walkChangeCellHtml(negative, "gsv").html, "-0.3 pp");
});

test("walkChangeCellHtml: a zero delta still renders (imagery churned, net flat)", () => {
  const zero = rowsFor({
    ...SEATTLE_GSV_WALK,
    change: { ...CHANGE_BLOCK, coverage_pct_by_length_delta: 0 },
  })[0];
  assert.equal(walkChangeCellHtml(zero, "gsv").html, "+0.0 pp");
});

test("the change cell keeps its OWN title over the link's — it says more", () => {
  // "Since 2026-04-01: 12 streets gained coverage, 3 lost it" is specific;
  // "Open GSV · …" is what every other cell already says.
  const changed = rowsFor({ ...SEATTLE_GSV_WALK, change: CHANGE_BLOCK })[0];
  const cell = STREET_COLUMNS.find((c) => c.key === "changeDelta_gsv").cell(changed);
  assert.match(cell, /<td title="Since 2026-04-01/);
  assert.doesNotMatch(cell, /title="Open GSV/);
  assert.match(cell, /href=/, "...while still being a link");
});

test("sortRows: changeDelta sorts numerically with first walks (null) last", () => {
  const rows = rowsFor(
    { ...SEATTLE_GSV_WALK, city_id: "a" },
    {
      ...SEATTLE_GSV_WALK,
      city_id: "b",
      change: { ...CHANGE_BLOCK, coverage_pct_by_length_delta: -0.3 },
    },
    {
      ...SEATTLE_GSV_WALK,
      city_id: "c",
      change: { ...CHANGE_BLOCK, coverage_pct_by_length_delta: 4.2 },
    }
  );
  assert.deepEqual(sortRows(rows, "changeDelta_gsv", "desc").map((r) => r.cityId), ["c", "b", "a"]);
});

// --- STREET_FILTERS ---------------------------------------------------------

test("STREET_FILTERS: Network is first, defaulted, and has no 'any' reading", () => {
  // Two networks are two different street-km denominators; an "all networks"
  // option would stack incomparable numbers in one column.
  assert.equal(STREET_FILTERS[0].key, "network");
  assert.equal(STREET_FILTERS[0].defaultValue, "drive");
  assert.equal(STREET_FILTERS[0].anyLabel, undefined);
  const [drive, broad] = rowsFor(SEATTLE_GSV_WALK, SEATTLE_BROAD_WALK);
  assert.ok(STREET_FILTERS[0].test(drive, "drive"));
  assert.ok(!STREET_FILTERS[0].test(broad, "drive"));
  assert.ok(STREET_FILTERS[0].test(broad, "all_public"));
});

test("STREET_FILTERS: 'Collected by' offers every registered provider plus the arity option", () => {
  const provider = STREET_FILTERS.find((f) => f.key === "provider");
  assert.deepEqual(
    provider.options.map((o) => o.value),
    ["gsv", "mapillary", "thirdparty", "multi"]
  );
  const both = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  const one = rowsFor(SEATTLE_GSV_WALK)[0];
  assert.ok(provider.test(both, "multi"));
  assert.ok(!provider.test(one, "multi"));
  assert.ok(provider.test(one, "gsv"));
  assert.ok(!provider.test(one, "thirdparty"));
});

test("STREET_FILTERS: 'Has Δ' asks whether ANY provider walked this row twice", () => {
  const none = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  const some = rowsFor({ ...SEATTLE_GSV_WALK, change: CHANGE_BLOCK }, SEATTLE_MAPILLARY_WALK)[0];
  const changed = STREET_FILTERS.find((f) => f.key === "changed");
  assert.ok(!changed.test(none));
  assert.ok(changed.test(some));
});

test("STREET_FILTERS: numeric filters are histogram sliders over real row fields", () => {
  const row = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK)[0];
  for (const filter of STREET_FILTERS) {
    if (!filter.field) continue;
    assert.equal(filter.type, "histogram-range", `${filter.key} is not a histogram filter`);
    assert.ok(filter.field in row, `filter ${filter.key} reads a missing field ${filter.field}`);
  }
  assert.equal(STREET_FILTERS.find((f) => f.key === "cov").field, "pctBest");
  assert.equal(STREET_FILTERS.find((f) => f.key === "km").field, "lengthKm");
});

// --- updateStreetsCaption ---------------------------------------------------

test("updateStreetsCaption: names the active network and counts within it", () => {
  // `all` holds BOTH networks, so "1 of 3" would compare the visible roads
  // rows against a total that includes rows the selector deliberately excluded.
  const captions = [];
  global.document = { getElementById: () => ({ set textContent(v) { captions.push(v); } }) };
  const all = rowsFor(SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK, SEATTLE_BROAD_WALK);
  const drive = all.filter((r) => r.networkType === "drive");

  updateStreetsCaption(drive, all, null, { values: { network: "drive" } });
  assert.equal(captions.pop(), "1 city walked on Roads");

  updateStreetsCaption([], all, null, { values: { network: "drive" } });
  assert.equal(captions.pop(), "0 of 1 city walked on Roads");

  updateStreetsCaption(
    all.filter((r) => r.networkType === "all_public"),
    all,
    null,
    { values: { network: "all_public" } }
  );
  assert.equal(captions.pop(), "1 city walked on Roads + paths");

  delete global.document;
});

// --- the provider scope (issue #250 follow-up) ------------------------------

test("STREET_FILTERS: coverage follows the scope; street km cannot", () => {
  const byKey = Object.fromEntries(STREET_FILTERS.map((f) => [f.key, f]));
  assert.equal(byKey.cov.fieldFor({}), "pctBest");
  assert.equal(byKey.cov.labelFor({}), "360° street-km % — any provider reaches");
  assert.equal(byKey.cov.fieldFor({ provider: "mapillary" }), "pct_mapillary");
  assert.equal(byKey.cov.labelFor({ provider: "gsv" }), "360° street-km % — GSV");

  // Street length is a property of the OSM network, not of a provider's walk
  // of it, so there is no per-provider column it could read.
  assert.equal(byKey.km.fieldFor, undefined);
  assert.equal(byKey.km.field, "lengthKm");
});

test("STREET_FILTERS: 'Has Δ' follows the scope too — 'walked twice' needs a whom", () => {
  const changed = STREET_FILTERS.find((f) => f.key === "changed");
  const gsvOnly = rowsFor(
    { ...SEATTLE_GSV_WALK, change: CHANGE_BLOCK },
    SEATTLE_MAPILLARY_WALK
  )[0];

  // Unscoped: any provider having walked twice is enough.
  assert.ok(changed.testFor({})(gsvOnly));
  assert.equal(changed.labelFor({}), "Has Δ since last walk — any provider");

  // Scoped to the provider that DID walk twice.
  assert.ok(changed.testFor({ provider: "gsv" })(gsvOnly));
  assert.equal(changed.labelFor({ provider: "gsv" }), "Has Δ since last walk — GSV");

  // Scoped to the one that did not: the honest answer is no.
  assert.ok(!changed.testFor({ provider: "mapillary" })(gsvOnly));
});

test("every scoped field a filter can resolve to exists on a row model", () => {
  // The scopes worth asserting are the ones the select can actually OFFER —
  // narrowed to the manifest's providers — plus the two that name no single
  // provider.
  for (const walks of [[SEATTLE_GSV_WALK, SEATTLE_MAPILLARY_WALK], fullRegistryWalks()]) {
    const { rows, filters, providers } = buildFor(...walks);
    const scopes = [{}, { provider: "multi" }, ...providers.map((p) => ({ provider: p }))];
    for (const filter of filters) {
      if (!filter.fieldFor) continue;
      for (const values of scopes) {
        const field = filter.fieldFor(values);
        assert.ok(
          field in rows[0],
          `${filter.key} under ${JSON.stringify(values)} reads missing ${field}`
        );
      }
    }
  }
});
