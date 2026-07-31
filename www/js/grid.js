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
 * escapeHtml — and from table-utils.js: cityDisplayLabel, sortRowsBy,
 * formatCellNumber, coverageCellHtml, createSortableTable.
 */

/**
 * The sortable columns, in table order. `key` is the row-model field, `type`
 * picks the comparator, and `initial` is the direction a first click applies
 * (numbers read best-first, text reads A–Z).
 */
const GRID_COLUMNS = [
  { key: "label", label: "City", type: "text", initial: "asc" },
  { key: "providerLabel", label: "Provider", type: "text", initial: "asc" },
  { key: "pct", label: "Grid coverage", type: "number", initial: "desc" },
  { key: "pctAny", label: "Any imagery", type: "number", initial: "desc" },
  { key: "medianAge", label: "Median age", type: "number", initial: "asc" },
  { key: "panos", label: "Panoramas", type: "number", initial: "desc" },
  { key: "collected", label: "Last collected", type: "text", initial: "desc" },
  { key: "snapshots", label: "Snapshots", type: "number", initial: "desc" },
];

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
    collected: city.latest_run_date ?? null,
    snapshots: (city.runs ?? []).length || null,
    filename: city.data_file?.filename ?? null,
  };
}

/**
 * Build one table row from a row model.
 *
 * @param {Object} row - From gridRowModel.
 * @returns {string} HTML for one <tr>.
 */
function gridRowHtml(row) {
  const link = row.filename
    ? `<a class="streets-view-link"
          href="city.html?file=${encodeURIComponent(row.filename)}">View on map</a>`
    : `<span class="streets-no-link" title="This city has no published run to link to">—</span>`;

  // City/state/country names come from OSM/Nominatim (publicly editable
  // third-party data) — escape everything data-derived entering innerHTML.
  return `
    <tr>
      <th scope="row">${escapeHtml(row.label)}</th>
      <td>${escapeHtml(row.providerLabel)}</td>
      ${coverageCellHtml(row.pct)}
      ${coverageCellHtml(row.pctAny)}
      <td>${row.medianAge == null ? "—" : `${formatCellNumber(row.medianAge, 1)} yrs`}</td>
      <td>${formatCellNumber(row.panos)}</td>
      <td>${escapeHtml(row.collected ?? "—")}</td>
      <td>${formatCellNumber(row.snapshots)}</td>
      <td>${link}</td>
    </tr>`;
}

// The table controller (created on first render so a header click can
// re-sort without refetching or re-adapting).
let gridTable = null;

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

  if (rows.length === 0) {
    statusEl.textContent = "No city collections have been published yet.";
    return;
  }

  gridTable ??= createSortableTable({
    columns: GRID_COLUMNS,
    defaultSort: GRID_DEFAULT_SORT,
    wrapEl,
    tbodyEl: document.getElementById("grid-tbody"),
    rowHtml: gridRowHtml,
  });
  gridTable.setRows(rows);

  document.getElementById("grid-caption").textContent =
    `${rows.length} city grid-run series` +
    (generatedAt ? ` · updated ${new Date(generatedAt).toLocaleString()}` : "");

  statusEl.hidden = true;
  wrapEl.hidden = false;
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
    renderGridRuns,
    GRID_COLUMNS,
    GRID_DEFAULT_SORT,
  };
}
