/**
 * streets.js — the Street-Level Coverage page (streets.html).
 *
 * Lists every published road-walk collection (issue #99) from the
 * `streetwalks.json.gz` sidecar manifest (issue #155), joined against the
 * `cities.json.gz` aggregate for display names and the run filename each row
 * links to.
 *
 * Why the join: the manifest is keyed by canonical `city_id` and carries no
 * human label and no grid-run filename, while `city.html` is addressed by run
 * filename (`city.html?file=…`). Only the aggregate has both, so a row can
 * only become a link if the city also has a published run. Rows whose city is
 * missing from the aggregate still render — they just don't link out.
 *
 * Deliberately not a map: with a handful of walked cities, a second Leaflet
 * view would duplicate the overview's search/legend/scatter machinery for no
 * gain. Each row links to the city page, which already prefers the road-walk
 * artifact over the grid one via the same manifest.
 *
 * Depends on globals from streetscape-utils.js (loaded first): PROVIDERS,
 * STREETSCAPE_DATA_BASE_URL, fetchGzippedJson, fetchStreetwalkManifest,
 * adaptCitiesPayload, escapeHtml — from table-utils.js: cityDisplayLabel,
 * sortRowsBy, formatCellNumber, coverageCellHtml, createSortableTable — and
 * from table-controls.js: createTableControls.
 */

// ── Display helpers ───────────────────────────────────────────

/**
 * Build a "City, State, Country" label from an adapted city record.
 * Alias kept for this page's tests/callers; the canonical copy lives in
 * table-utils.js since the grid page needs it too.
 *
 * @param {Object} city - Adapted city record.
 * @returns {string}
 */
function cityLabel(city) {
  return cityDisplayLabel(city);
}

/**
 * Index the aggregate by `city_id` for every provider that appears in the
 * walks, so each row can be joined in one lookup.
 *
 * Adapting is per-provider (a v3 record holds an independent run series per
 * provider), so this adapts once per distinct provider rather than once per
 * row — today that is a single pass for "gsv".
 *
 * @param {?Object} rawCities - The parsed cities.json.gz, or null.
 * @param {string[]} providers - Distinct provider keys to index.
 * @returns {Map<string, Object>} Keyed "provider|city_id".
 */
function indexCitiesByProvider(rawCities, providers) {
  const index = new Map();
  if (!rawCities) return index;
  for (const provider of providers) {
    const { cities } = adaptCitiesPayload(rawCities, provider);
    for (const city of cities) {
      if (!city.city_id) continue;
      index.set(`${provider}|${city.city_id}`, city);
      // Also index by city_id alone, for the display NAME only. A city can be
      // walked by a provider it has no grid run for (Mapillary street coverage
      // costs a handful of tiles, so it lands before a full census does), and
      // a city's name is provider-independent — falling back to it beats
      // showing a raw slug. The link is NOT taken from this entry: city.html
      // derives its provider from the run filename, so a cross-provider link
      // would open the wrong series.
      if (!index.has(city.city_id)) index.set(city.city_id, city);
    }
  }
  return index;
}

/** Format a nullable number for a table cell (table-utils alias). */
function num(value, digits = 0) {
  return formatCellNumber(value, digits);
}

/**
 * Cell for the "since last walk" coverage delta: an em-dash for a first walk
 * (no change block in the manifest), else a signed percentage-point figure
 * whose title carries the comparison date and the edge churn behind it.
 *
 * @param {Object} row - From toRowModel.
 * @returns {string} HTML for one <td>.
 */
function walkChangeCellHtml(row) {
  if (row.changeDelta == null) return `<td>—</td>`;
  const sign = row.changeDelta >= 0 ? "+" : "";
  const title =
    `Since ${row.change.from}: ${row.change.edges_gained_coverage ?? 0} streets gained ` +
    `coverage, ${row.change.edges_lost_coverage ?? 0} lost it`;
  return `<td title="${escapeHtml(title)}">${sign}${row.changeDelta.toFixed(1)} pp</td>`;
}

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
 * The link carries THIS row's network type: city.html selects the walk to draw
 * by network type and defaults to 'drive', so without it a "Roads + paths" row
 * would open the city's drive walk instead — or, for a city walked only
 * broadly, fall all the way back to the grid-attribution artifact, a different
 * metric entirely. Advertising a row the link cannot reach is worse than not
 * listing it.
 *
 * `title` carries the full, untruncated label either way — the cell itself is
 * ellipsis-truncated in CSS (data-table.css) because OSM/Nominatim labels are
 * unbounded and a long one alone can push the table past its measure.
 *
 * @param {Object} row - From toRowModel.
 * @returns {string} HTML for one <th scope="row">.
 */
function walkLabelCellHtml(row) {
  const label = escapeHtml(row.label);
  const content = row.filename
    ? `<a class="streets-view-link"
          href="city.html?file=${encodeURIComponent(row.filename)}&network=${encodeURIComponent(
        row.networkType
      )}">${label}</a>`
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
 * header tooltip (these lived in streets.html before the header became
 * JS-rendered).
 *
 * City/state/country names come from OSM/Nominatim (publicly editable
 * third-party data) — escape everything data-derived entering innerHTML.
 */
const STREET_COLUMNS = [
  {
    key: "label",
    label: "City",
    type: "text",
    initial: "asc",
    always: true,
    cell: walkLabelCellHtml,
  },
  {
    key: "providerLabel",
    label: "Provider",
    type: "text",
    initial: "asc",
    cell: (r) => `<td>${escapeHtml(r.providerLabel)}</td>`,
  },
  // Which OSM network was walked. Without this column two rows for one city
  // would look like duplicates, when in fact their coverage percentages divide
  // by different street-km denominators and are not comparable.
  {
    key: "networkLabel",
    label: "Network",
    type: "text",
    initial: "asc",
    title:
      "Which OSM network was walked. &quot;Roads&quot; is motorized public roads only; " +
      "&quot;Roads + paths&quot; also covers alleys, footpaths, park trails, cycleways and " +
      "steps. Coverage percentages are only comparable within the same network.",
    cell: (r) => `<td>${escapeHtml(r.networkLabel)}</td>`,
  },
  {
    key: "runDate",
    label: "Walked",
    type: "text",
    initial: "desc",
    cell: (r) => `<td>${escapeHtml(r.runDate ?? "—")}</td>`,
  },
  {
    key: "spacing",
    label: "Sample spacing",
    type: "number",
    initial: "asc",
    unit: " m",
    cell: (r) => `<td>${r.spacing == null ? "—" : `${num(r.spacing)} m`}</td>`,
  },
  {
    key: "pct",
    label: "360° street-km",
    type: "number",
    initial: "desc",
    unit: "%",
    title: "Share of street-km covered by 360° imagery",
    cell: (r) => coverageCellHtml(r.pct),
  },
  {
    key: "pctAny",
    label: "Any imagery",
    type: "number",
    initial: "desc",
    unit: "%",
    title:
      "Including flat/perspective imagery; equals the 360° number for Google Street View",
    cell: (r) => coverageCellHtml(r.pctAny),
  },
  // "Since last walk" coverage delta (issue #101), from the manifest's
  // optional change block. Null for first walks — most cities, until their
  // second walk lands — which the number comparator sorts to the end.
  {
    key: "changeDelta",
    label: "Δ coverage",
    type: "number",
    initial: "desc",
    unit: " pp",
    title:
      "Change in 360° street-km coverage since the previous walk of this city, in " +
      "percentage points. Blank for first walks.",
    cell: walkChangeCellHtml,
  },
  // Absolute street length (schema v12, issue #189). A share cannot be turned
  // back into kilometres without its denominator — 74.5% of Corvallis is 873 km
  // and the same 74.5% of a village is 5 km — and deployment estimates are
  // quoted in km, not percent.
  {
    key: "lengthKm",
    label: "Street km",
    type: "number",
    initial: "desc",
    unit: " km",
    digits: 1,
    title: "Total length of the walked network, in kilometres",
    cell: (r) => `<td>${r.lengthKm == null ? "—" : `${num(r.lengthKm, 1)} km`}</td>`,
  },
  {
    key: "lengthKmCovered",
    label: "Covered km",
    type: "number",
    initial: "desc",
    unit: " km",
    digits: 1,
    title: "Kilometres of street covered by 360° imagery",
    cell: (r) => `<td>${r.lengthKmCovered == null ? "—" : `${num(r.lengthKmCovered, 1)} km`}</td>`,
  },
  {
    key: "lengthKmCoveredAny",
    label: "Covered km (any)",
    type: "number",
    initial: "desc",
    unit: " km",
    digits: 1,
    title: "Kilometres of street covered by any imagery, including flat/perspective",
    cell: (r) =>
      `<td>${r.lengthKmCoveredAny == null ? "—" : `${num(r.lengthKmCoveredAny, 1)} km`}</td>`,
  },
  {
    key: "medianAge",
    label: "Median age",
    type: "number",
    initial: "asc",
    unit: " yrs",
    digits: 1,
    title:
      "Median age of the imagery covering this walk's streets. Stored rather than " +
      "derived — a median of the per-class medians is not the median.",
    cell: (r) => `<td>${r.medianAge == null ? "—" : `${num(r.medianAge, 1)} yrs`}</td>`,
  },
  {
    key: "edges",
    label: "Streets",
    type: "number",
    initial: "desc",
    cell: (r) => `<td>${num(r.edges)}</td>`,
  },
  {
    key: "fullyCovered",
    label: "Fully covered",
    type: "number",
    initial: "desc",
    cell: (r) => `<td>${num(r.fullyCovered)}</td>`,
  },
];

/**
 * Column presets. The first is the default and must fit the page's 1200px
 * measure without horizontal scrolling — that is what these exist for.
 */
const STREET_PRESETS = [
  {
    id: "overview",
    label: "Overview",
    title: "The headline read: who walked what, how much of it, and how fresh",
    // pctAny stays in the default view: the 360°-vs-any-imagery split (issue
    // #116) is the page's headline distinction for Mapillary, not a detail to
    // be discovered behind a preset.
    columns: [
      "providerLabel",
      "networkLabel",
      "runDate",
      "pct",
      "pctAny",
      "lengthKm",
      "medianAge",
    ],
  },
  {
    id: "kilometres",
    label: "Kilometres",
    title: "Absolute street length rather than shares (schema v12)",
    columns: ["providerLabel", "pct", "pctAny", "lengthKm", "lengthKmCovered", "lengthKmCoveredAny"],
  },
  {
    id: "change",
    label: "Change",
    title: "Walk-to-walk movement (issue #101); blank until a city's second walk lands",
    columns: ["providerLabel", "networkLabel", "runDate", "pct", "changeDelta", "lengthKm"],
  },
  {
    id: "network",
    label: "Network",
    title: "The shape of the walked network itself",
    columns: ["providerLabel", "networkLabel", "spacing", "edges", "fullyCovered", "lengthKm"],
  },
];

/** Filters offered above the table. */
const STREET_FILTERS = [
  {
    key: "provider",
    label: "Provider",
    type: "select",
    anyLabel: "All providers",
    // One option per REGISTERED provider (issue #225). Rows come from the
    // streetwalk manifest, which carries whichever providers actually walked
    // the city, so a hardcoded pair meant a third provider's walks were
    // listed with no filter able to isolate them.
    options: Object.entries(PROVIDERS).map(([value, p]) => ({ value, label: p.label })),
    test: (row, value) => row.provider === value,
  },
  {
    key: "network",
    label: "Network",
    type: "select",
    anyLabel: "All networks",
    options: [
      { value: "drive", label: "Roads" },
      { value: "all_public", label: "Roads + paths" },
    ],
    test: (row, value) => row.networkType === value,
  },
  {
    key: "cov",
    label: "360° street-km %",
    type: "range",
    field: "pct",
    min: 0,
    max: 100,
  },
  {
    key: "km",
    label: "Street km",
    type: "range",
    field: "lengthKm",
    min: 0,
  },
  {
    key: "changed",
    label: "Has Δ coverage",
    type: "boolean",
    title: "Only cities walked at least twice, so a change could be computed (issue #101)",
    test: (row) => row.changeDelta != null,
  },
];

/** Row fields the free-text search box looks at. */
const STREET_SEARCH_FIELDS = ["label", "cityId", "providerLabel", "networkLabel"];

/** Default sort: best 360° coverage first (what the page opens on). */
const DEFAULT_SORT = { key: "pct", dir: "desc" };

/**
 * Flatten a manifest walk + its joined aggregate record into the one shape the
 * sorter and the row renderer both read.
 *
 * Doing this once up front (rather than reaching into the walk record from
 * each comparator) is what lets the table sort on *derived* columns like the
 * display label, which exists only after the aggregate join.
 *
 * @param {Object} walk - A manifest walk record.
 * @param {?Object} city - The joined aggregate record for this walk's exact
 *   (provider, city_id), or null/undefined. Supplies both the label and the
 *   `city.html?file=` link target.
 * @param {?Object} [labelSource] - Fallback record matched on city_id alone,
 *   used for the display NAME only when the city has no run in this walk's
 *   provider series. Never used for the link: city.html derives its provider
 *   from the run filename, so a cross-provider link opens the wrong series.
 * @returns {Object} Row model.
 */
function toRowModel(walk, city, labelSource = null) {
  const named = city ?? labelSource;
  return {
    cityId: walk.city_id,
    label: named ? cityLabel(named) : walk.city_id,
    provider: walk.provider,
    providerLabel: PROVIDERS[walk.provider]?.label ?? walk.provider,
    networkType: walk.network_type ?? DEFAULT_STREET_NETWORK_TYPE,
    networkLabel: streetNetworkLabel(walk.network_type),
    runDate: walk.run_date ?? null,
    spacing: walk.spacing_m ?? null,
    pct: walk.coverage_pct_by_length ?? null,
    // Any-imagery street coverage: Mapillary only (flat/perspective imagery
    // counts as covered too). For GSV it equals the 360° number, and it is
    // null for walks collected before the field existed.
    pctAny: walk.coverage_pct_by_length_any ?? null,
    // "Since last walk" change block (issue #101). Absent from the manifest
    // for first walks, so both stay null and the cell renders an em-dash.
    change: walk.change ?? null,
    changeDelta: walk.change?.coverage_pct_by_length_delta ?? null,
    // Absolute lengths and median covered age (schema v12). NULL on walks
    // cataloged before v12 and not yet backfilled.
    lengthKm: walk.length_km ?? null,
    lengthKmCovered: walk.length_km_covered ?? null,
    lengthKmCoveredAny: walk.length_km_covered_any ?? null,
    medianAge: walk.median_covered_age_years ?? null,
    // Per-highway-class breakdown, absent-not-null in the manifest. Carried on
    // the row model for the class-level filter now and the row expansion in
    // the follow-up PR.
    coverageByHighway: walk.coverage_by_highway ?? null,
    edges: walk.edges ?? null,
    fullyCovered: walk.edges_fully_covered ?? null,
    filename: city?.data_file?.filename ?? null,
  };
}

/**
 * Sort row models by one column (table-utils.sortRowsBy over this page's
 * columns). Alias kept for this page's tests/callers.
 *
 * @param {Object[]} rows - Row models from toRowModel.
 * @param {string} key - A STREET_COLUMNS key.
 * @param {"asc"|"desc"} dir
 * @returns {Object[]} A new sorted array.
 */
function sortRows(rows, key, dir = "desc") {
  return sortRowsBy(STREET_COLUMNS, rows, key, dir);
}

// ── Rendering ─────────────────────────────────────────────────

/**
 * Build one table row from a row model.
 *
 * @param {Object} row - From toRowModel.
 * @param {Object[]} [columns] - Visible columns; defaults to all of them.
 * @returns {string} HTML for one <tr>.
 */
function walkRowHtml(row, columns = STREET_COLUMNS) {
  return rowHtmlFromColumns(columns, row);
}

// The table + controls controllers (created on first render so a header click
// or a filter change can repaint without refetching or re-joining).
let streetsTable = null;
let streetsControls = null;

/**
 * Render the table (or the empty state) from a manifest + aggregate.
 *
 * @param {?Object} manifest - Parsed streetwalks.json.gz, or null.
 * @param {?Object} rawCities - Parsed cities.json.gz, or null.
 */
function renderStreetWalks(manifest, rawCities) {
  const statusEl = document.getElementById("streets-status");
  const wrapEl = document.getElementById("streets-table-wrap");
  const walks = Array.isArray(manifest?.walks) ? manifest.walks : [];

  if (walks.length === 0) {
    // A real code path, not a defensive branch: a deployment that has never
    // run the collector publishes no manifest at all.
    statusEl.textContent =
      "No road-walk collections have been published yet. Street-level coverage is " +
      "collected on the same schedule as the grid, city by city; check back soon.";
    return;
  }

  const providers = [...new Set(walks.map((w) => w.provider))];
  const index = indexCitiesByProvider(rawCities, providers);
  const rows = walks.map((walk) =>
    toRowModel(
      walk,
      index.get(`${walk.provider}|${walk.city_id}`),
      index.get(walk.city_id)
    )
  );

  streetsTable ??= createSortableTable({
    columns: STREET_COLUMNS,
    defaultSort: DEFAULT_SORT,
    theadEl: document.getElementById("streets-thead"),
    tbodyEl: document.getElementById("streets-tbody"),
  });
  streetsControls ??= createTableControls({
    rootEl: document.getElementById("streets-controls"),
    table: streetsTable,
    columns: STREET_COLUMNS,
    presets: STREET_PRESETS,
    filters: STREET_FILTERS,
    searchFields: STREET_SEARCH_FIELDS,
    onChange: (shown, all) => updateStreetsCaption(shown, all, manifest),
  });
  streetsControls.setRows(rows);

  statusEl.hidden = true;
  wrapEl.hidden = false;
}

/**
 * Keep the caption reporting what is actually on screen.
 *
 * The count is the live result count for the current filters, so it must say
 * how many of how many — a bare "12 collections" after a filter reads as the
 * whole dataset.
 *
 * @param {Object[]} shown - Filtered rows.
 * @param {Object[]} all - Every row.
 * @param {?Object} manifest - For the generated-at stamp.
 */
function updateStreetsCaption(shown, all, manifest) {
  const noun = `road-walk collection${all.length === 1 ? "" : "s"}`;
  const counts =
    shown.length === all.length
      ? `${all.length} published ${noun}`
      : `${shown.length} of ${all.length} published ${noun}`;
  document.getElementById("streets-caption").textContent =
    counts +
    (manifest?.generated_at
      ? ` · manifest updated ${new Date(manifest.generated_at).toLocaleString()}`
      : "");
}

// ── Data loading ──────────────────────────────────────────────

/** Fetch the manifest + aggregate, then render. */
async function loadStreetWalks() {
  try {
    // The aggregate is only needed for labels and links, so a failure there
    // still yields a useful (if unlinked) table — but a manifest failure
    // means there is nothing to list at all.
    const [manifest, rawCities] = await Promise.all([
      fetchStreetwalkManifest(),
      fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "cities.json.gz").catch((e) => {
        console.warn("Could not load cities.json.gz; rows will not link out:", e.message);
        return null;
      }),
    ]);
    renderStreetWalks(manifest, rawCities);
  } catch (error) {
    console.error("Error loading street-level coverage:", error);
    document.getElementById("streets-status").textContent =
      "Error loading street-level coverage. Please check the console for details.";
  }
}

// Guarded so `require`ing this file in the Node unit tests (which have no
// document) exercises the pure helpers without trying to load anything.
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", loadStreetWalks);
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cityLabel,
    indexCitiesByProvider,
    toRowModel,
    sortRows,
    num,
    walkChangeCellHtml,
    walkRowHtml,
    renderStreetWalks,
    updateStreetsCaption,
    STREET_COLUMNS,
    STREET_PRESETS,
    STREET_FILTERS,
    STREET_SEARCH_FIELDS,
    DEFAULT_SORT,
  };
}
