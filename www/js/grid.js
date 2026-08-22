/**
 * grid.js — the Grid Coverage page (grid.html).
 *
 * Lists every (city, provider) grid-run series from the cities.json.gz
 * aggregate as a sortable table — the tabular counterpart of the overview
 * map, the way streets.html is the tabular view of the road walks. One row
 * per provider series: a city collected by both GSV and Mapillary appears
 * twice, because the two are independent run series on the same frozen grid
 * and their pano counts are census-vs-sample (not comparable).
 *
 * Depends on globals from streetscape-utils.js (loaded first): PROVIDERS,
 * STREETSCAPE_DATA_BASE_URL, fetchGzippedJson, adaptCitiesPayload,
 * escapeHtml — from table-utils.js: cityDisplayLabel, sortRowsBy,
 * formatCellNumber, coverageCellHtml, rowHtmlFromColumns,
 * createSortableTable — and from table-controls.js: createTableControls.
 */

/**
 * Cell for the "City" column: the label, hyperlinked to the city page when
 * this row has a published run to link to.
 *
 * Absorbs the "View on map" link — a follow-up to issue #188. A whole extra
 * trailing column existed only to carry one link per row, when every row
 * already has exactly one natural place for it: its own name. A row with
 * nothing to link to (no published run) still renders, just as plain text —
 * the same degrade-not-disappear posture the old placeholder cell had.
 *
 * `title` carries the full, untruncated label either way — the cell itself is
 * ellipsis-truncated in CSS (data-table.css) because OSM/Nominatim labels are
 * unbounded and a long one alone can push the table past its measure.
 *
 * @param {Object} row - From gridRowModel.
 * @returns {string} HTML for one <th scope="row">.
 */
function gridLabelCellHtml(row) {
  const label = escapeHtml(row.label);
  const content = row.filename
    ? `<a class="streets-view-link" href="city.html?file=${encodeURIComponent(row.filename)}">${label}</a>`
    : label;
  return `<th scope="row" title="${label}">${content}</th>`;
}

/**
 * The columns, in table order.
 *
 * `key` is the row-model field, `type` picks the comparator, `initial` is the
 * direction a first click applies (numbers read best-first, text reads A–Z),
 * and `cell(row)` renders that column's own cell — the header and the body are
 * both generated from this list so they cannot drift (see table-utils.js).
 *
 * `unit`/`digits` are read by the distribution strip; `title` becomes the
 * header tooltip (these lived in grid.html before the header became
 * JS-rendered).
 *
 * City/state/country names come from OSM/Nominatim (publicly editable
 * third-party data) — escape everything data-derived entering innerHTML.
 */
const GRID_COLUMNS = [
  {
    key: "label",
    label: "City",
    type: "text",
    initial: "asc",
    always: true,
    cell: gridLabelCellHtml,
  },
  {
    key: "providerLabel",
    label: "Provider",
    type: "text",
    initial: "asc",
    title:
      "Imagery provider. Each provider is an independent run series on the same frozen grid.",
    cell: (r) => `<td>${escapeHtml(r.providerLabel)}</td>`,
  },
  {
    key: "pct",
    label: "Grid coverage",
    type: "number",
    initial: "desc",
    unit: "%",
    title: "Share of the city's grid sample points with a 360° panorama",
    cell: (r) => coverageCellHtml(r.pct),
  },
  {
    key: "pctAny",
    label: "Any imagery",
    type: "number",
    initial: "desc",
    unit: "%",
    title:
      "Including flat/perspective imagery (Mapillary); equals Grid coverage for Google Street View",
    cell: (r) => coverageCellHtml(r.pctAny),
  },
  {
    key: "medianAge",
    label: "Median age",
    type: "number",
    initial: "asc",
    unit: " yrs",
    digits: 1,
    title: "Median age of the city's panoramas at its latest snapshot",
    cell: (r) => `<td>${r.medianAge == null ? "—" : `${formatCellNumber(r.medianAge, 1)} yrs`}</td>`,
  },
  {
    key: "panos",
    label: "Panoramas",
    type: "number",
    initial: "desc",
    title:
      "Unique panoramas in the latest snapshot: official © Google panos for GSV, all 360° " +
      "panos for Mapillary. Not comparable across providers (sample vs census).",
    cell: (r) => `<td>${formatCellNumber(r.panos)}</td>`,
  },
  // The denominator of Grid coverage (aggregate schema v3 additive, issue
  // #189). Without it a reader cannot tell a 40% built from 1,681 points from
  // a 40% built from two million — the difference between a village and a
  // metro.
  {
    key: "searchPoints",
    label: "Grid points",
    type: "number",
    initial: "desc",
    title: "Sample points in the frozen grid — the denominator Grid coverage is a share of",
    cell: (r) => `<td>${formatCellNumber(r.searchPoints)}</td>`,
  },
  {
    key: "gridWidthM",
    label: "Grid size",
    type: "number",
    initial: "desc",
    unit: " m",
    title:
      "Extent of the frozen sampling grid, width × height. Sorts by width. Oversized city " +
      "grids are capped at 40 km per side (issue #166).",
    cell: (r) => `<td>${r.gridSpanLabel ?? "—"}</td>`,
  },
  {
    key: "gridStepM",
    label: "Grid step",
    type: "number",
    initial: "asc",
    unit: " m",
    title: "Spacing between grid sample points",
    cell: (r) => `<td>${r.gridStepM == null ? "—" : `${formatCellNumber(r.gridStepM)} m`}</td>`,
  },
  {
    key: "areaKm2",
    label: "Grid area",
    type: "number",
    initial: "desc",
    unit: " km²",
    digits: 1,
    title: "Area covered by the frozen sampling grid",
    cell: (r) => `<td>${r.areaKm2 == null ? "—" : `${formatCellNumber(r.areaKm2, 1)} km²`}</td>`,
  },
  {
    key: "collected",
    label: "Last collected",
    type: "text",
    initial: "desc",
    title: "Date of the latest collection run — how fresh this row's data is",
    cell: (r) => `<td>${escapeHtml(r.collected ?? "—")}</td>`,
  },
  {
    key: "snapshots",
    label: "Snapshots",
    type: "number",
    initial: "desc",
    title: "Number of dated collection runs; repeat runs enable change tracking over time",
    cell: (r) => `<td>${formatCellNumber(r.snapshots)}</td>`,
  },
];

/**
 * Column presets. The first is the default and must fit the page's 1200px
 * measure without horizontal scrolling — that is what these exist for.
 */
const GRID_PRESETS = [
  {
    id: "overview",
    label: "Overview",
    title: "The headline read: how much imagery a city has, and how fresh it is",
    columns: ["providerLabel", "pct", "pctAny", "medianAge", "panos", "collected"],
  },
  {
    id: "grid",
    label: "Grid geometry",
    title: "What the percentage is a percentage OF (aggregate schema v3, issue #189)",
    columns: ["providerLabel", "pct", "searchPoints", "gridWidthM", "gridStepM", "areaKm2"],
  },
  {
    id: "provenance",
    label: "Provenance",
    title: "When each series was collected and how many times",
    columns: ["providerLabel", "collected", "snapshots", "medianAge", "panos"],
  },
];

/** Filters offered above the table. */
const GRID_FILTERS = [
  {
    key: "provider",
    label: "Provider",
    type: "select",
    anyLabel: "All providers",
    // One option per REGISTERED provider (issue #225). renderGridRuns already
    // builds its rows by iterating the registry, so a hardcoded pair meant a
    // third provider's rows appeared in the table with no filter able to
    // isolate them.
    options: Object.entries(PROVIDERS).map(([value, p]) => ({ value, label: p.label })),
    test: (row, value) => row.provider === value,
  },
  // Histogram-sliders (issue #250): the bars show where the rows actually
  // sit before a window is chosen, on a fixed axis, computed over the rows the
  // OTHER controls have selected. The min/max number inputs are still there
  // for precision.
  {
    key: "cov",
    label: "Grid coverage %",
    type: "histogram-range",
    field: "pct",
    min: 0,
    max: 100,
    unit: "%",
    digits: 1,
  },
  {
    key: "age",
    label: "Median age (yrs)",
    type: "histogram-range",
    field: "medianAge",
    min: 0,
    unit: " yrs",
    digits: 1,
  },
  {
    key: "both",
    // "Both" was arity-wrong the moment a third provider could be registered;
    // the test below has always been `size > 1`, not "these two".
    label: "Multiple providers",
    type: "boolean",
    title:
      "Only cities collected by more than one provider, where the series are " +
      "directly comparable on the same frozen grid",
    test: (row) => row.hasBothProviders === true,
  },
];

/** Row fields the free-text search box looks at. */
const GRID_SEARCH_FIELDS = ["label", "cityId", "providerLabel"];

/** Default sort: alphabetical, so the page opens as a browsable index. */
const GRID_DEFAULT_SORT = { key: "label", dir: "asc" };

/**
 * Flatten an adapted city record (one provider's series) into the one shape
 * the sorter and the row renderer both read.
 *
 * @param {Object} city - Adapted city record from adaptCitiesPayload.
 * @returns {Object} Row model.
 */
function gridRowModel(city) {
  // Grid geometry, published from aggregate v3 onward and null on older
  // records (and on v1/v2, which will never gain it).
  const width = city.grid?.width_meters ?? null;
  const height = city.grid?.height_meters ?? null;
  return {
    cityId: city.city_id ?? "",
    label: city.city_id ? cityDisplayLabel(city) : "Unknown",
    provider: city.provider,
    providerLabel: PROVIDERS[city.provider]?.label ?? city.provider,
    pct: city.coverage_rate_percent ?? null,
    // Any-imagery coverage (issue #116): Mapillary's full footprint including
    // flat imagery; the adapter falls back to the 360° rate for GSV/pre-v7.
    pctAny: city.any_imagery_coverage_rate_percent ?? null,
    medianAge: city.pano_age_stats?.median_pano_age_years ?? null,
    panos: city.pano_count ?? null,
    searchPoints: city.total_search_points ?? null,
    gridWidthM: width,
    gridStepM: city.grid?.step_length_meters ?? null,
    // Rendered form of the extent; the column sorts on gridWidthM because a
    // "40 × 40 km" string sorts lexically, which is meaningless.
    gridSpanLabel:
      width == null || height == null
        ? null
        : `${formatCellNumber(width / 1000, 1)} × ${formatCellNumber(height / 1000, 1)} km`,
    areaKm2: city.search_area_km2 ?? null,
    collected: city.latest_run_date ?? null,
    snapshots: (city.runs ?? []).length || null,
    filename: city.data_file?.filename ?? null,
    // Filled in by renderGridRuns, which is the only place that can see every
    // provider's rows at once.
    hasBothProviders: false,
  };
}

/**
 * Build one table row from a row model.
 *
 * @param {Object} row - From gridRowModel.
 * @param {Object[]} [columns] - Visible columns; defaults to all of them.
 * @returns {string} HTML for one <tr>.
 */
function gridRowHtml(row, columns = GRID_COLUMNS) {
  return rowHtmlFromColumns(columns, row);
}

/**
 * Mark the rows whose city was collected by more than one provider.
 *
 * The head-to-head question ("does Mapillary ever beat GSV?") is only askable
 * on cities where both series exist, and that is a property of the row SET,
 * not of any single row — so it is computed here rather than in gridRowModel.
 *
 * @param {Object[]} rows - All row models; mutated in place.
 * @returns {Object[]} The same array, for chaining.
 */
function markBothProviders(rows) {
  const providersByCity = new Map();
  for (const row of rows) {
    if (!providersByCity.has(row.cityId)) providersByCity.set(row.cityId, new Set());
    providersByCity.get(row.cityId).add(row.provider);
  }
  for (const row of rows) {
    row.hasBothProviders = providersByCity.get(row.cityId).size > 1;
  }
  return rows;
}

// The table + controls controllers (created on first render so a header click
// or a filter change can repaint without refetching or re-adapting).
let gridTable = null;
let gridControls = null;

/**
 * Render the table (or the empty state) from the aggregate payload.
 *
 * @param {?Object} rawCities - Parsed cities.json.gz, or null.
 */
function renderGridRuns(rawCities) {
  const statusEl = document.getElementById("grid-status");
  const wrapEl = document.getElementById("grid-table-wrap");

  // One adaptation pass per provider: a v3 record holds an independent run
  // series per provider, so each pass yields that provider's cities only.
  const rows = [];
  let generatedAt = null;
  for (const provider of Object.keys(PROVIDERS)) {
    const { meta, cities } = adaptCitiesPayload(rawCities, provider);
    generatedAt ??= meta.generatedAt;
    rows.push(...cities.map(gridRowModel));
  }
  markBothProviders(rows);

  if (rows.length === 0) {
    statusEl.textContent = "No city collections have been published yet.";
    return;
  }

  gridTable ??= createSortableTable({
    columns: GRID_COLUMNS,
    defaultSort: GRID_DEFAULT_SORT,
    theadEl: document.getElementById("grid-thead"),
    tbodyEl: document.getElementById("grid-tbody"),
  });
  gridControls ??= createTableControls({
    rootEl: document.getElementById("grid-controls"),
    table: gridTable,
    columns: GRID_COLUMNS,
    presets: GRID_PRESETS,
    filters: GRID_FILTERS,
    searchFields: GRID_SEARCH_FIELDS,
    // The sidebar carries per-filter histograms on fixed axes; a second
    // histogram of whichever column happens to be sorted would be a different
    // answer to the same question, changing its metric on every header click.
    layout: "sidebar",
    showDistributionStrip: false,
    onChange: (shown, all) => updateGridCaption(shown, all, generatedAt),
  });
  gridControls.setRows(rows);

  statusEl.hidden = true;
  wrapEl.hidden = false;
}

/**
 * Keep the caption reporting what is actually on screen.
 *
 * @param {Object[]} shown - Filtered rows.
 * @param {Object[]} all - Every row.
 * @param {?string} generatedAt - Aggregate timestamp.
 */
function updateGridCaption(shown, all, generatedAt) {
  const counts =
    shown.length === all.length
      ? `${all.length} city grid-run series`
      : `${shown.length} of ${all.length} city grid-run series`;
  document.getElementById("grid-caption").textContent =
    counts + (generatedAt ? ` · updated ${new Date(generatedAt).toLocaleString()}` : "");
}

/** Fetch the aggregate, then render. */
async function loadGridRuns() {
  try {
    const rawCities = await fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "cities.json.gz");
    renderGridRuns(rawCities);
  } catch (error) {
    console.error("Error loading grid coverage:", error);
    document.getElementById("grid-status").textContent =
      "Error loading grid coverage. Please check the console for details.";
  }
}

// Guarded so `require`ing this file in the Node unit tests (which have no
// document) exercises the pure helpers without trying to load anything.
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", loadGridRuns);
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    gridRowModel,
    gridRowHtml,
    markBothProviders,
    renderGridRuns,
    updateGridCaption,
    GRID_COLUMNS,
    GRID_PRESETS,
    GRID_FILTERS,
    GRID_SEARCH_FIELDS,
    GRID_DEFAULT_SORT,
  };
}
