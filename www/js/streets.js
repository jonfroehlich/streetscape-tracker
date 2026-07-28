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
 * adaptCitiesPayload, coverageColor, escapeHtml.
 */

// ── Display helpers ───────────────────────────────────────────

/**
 * Build a "City, State, Country" label from an adapted city record.
 * Mirrors index.js's getCityLabel (kept local rather than shared: it is three
 * lines and the two pages have no other overlap).
 *
 * @param {Object} city - Adapted city record.
 * @returns {string}
 */
function cityLabel(city) {
  const name = city.city || city.state?.name || city.country?.name || "Unknown";
  const parts = [name];
  if (city.state?.name && city.state.name !== name) parts.push(city.state.name);
  if (city.country?.name) parts.push(city.country.name);
  return parts.join(", ");
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

/**
 * The sortable columns, in table order. `key` is the row-model field, `type`
 * picks the comparator, and `initial` is the direction a first click applies
 * (numbers read best-first, text reads A–Z).
 */
const STREET_COLUMNS = [
  { key: "label", label: "City", type: "text", initial: "asc" },
  { key: "providerLabel", label: "Provider", type: "text", initial: "asc" },
  // Which OSM network was walked. Without this column two rows for one city
  // would look like duplicates, when in fact their coverage percentages divide
  // by different street-km denominators and are not comparable.
  { key: "networkLabel", label: "Network", type: "text", initial: "asc" },
  { key: "runDate", label: "Walked", type: "text", initial: "desc" },
  { key: "spacing", label: "Sample spacing", type: "number", initial: "asc" },
  { key: "pct", label: "Street-km covered", type: "number", initial: "desc" },
  { key: "pctAny", label: "Any imagery", type: "number", initial: "desc" },
  { key: "edges", label: "Streets", type: "number", initial: "desc" },
  { key: "fullyCovered", label: "Fully covered", type: "number", initial: "desc" },
];

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
    edges: walk.edges ?? null,
    fullyCovered: walk.edges_fully_covered ?? null,
    filename: city?.data_file?.filename ?? null,
  };
}

/**
 * Sort row models by one column. Nulls always sink to the bottom regardless of
 * direction (a missing number is not "small" — it is absent), and city_id is
 * the tiebreaker so the order is stable across reloads and re-sorts.
 *
 * Text compares with `sensitivity: "base"` so the worldwide frame's accented
 * and non-Latin city names order the way a reader expects rather than by code
 * point ("Ávila" next to "Avila", not after "Zurich").
 *
 * @param {Object[]} rows - Row models from toRowModel.
 * @param {string} key - A STREET_COLUMNS key.
 * @param {"asc"|"desc"} dir
 * @returns {Object[]} A new sorted array.
 */
function sortRows(rows, key, dir = "desc") {
  const column = STREET_COLUMNS.find((c) => c.key === key) ?? STREET_COLUMNS[0];
  const sign = dir === "asc" ? 1 : -1;
  const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
  return [...rows].sort((a, b) => {
    const av = a[column.key];
    const bv = b[column.key];
    if (av == null && bv == null) return collator.compare(a.cityId, b.cityId);
    if (av == null) return 1;
    if (bv == null) return -1;
    const cmp = column.type === "number" ? av - bv : collator.compare(String(av), String(bv));
    return cmp !== 0 ? cmp * sign : collator.compare(a.cityId, b.cityId);
  });
}

/** Format a nullable number for a table cell. */
function num(value, digits = 0) {
  return value == null ? "—" : value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

// ── Rendering ─────────────────────────────────────────────────

/** A coverage cell: a proportional bar (decorative) behind the number. */
function coverageCellHtml(pct) {
  if (pct == null) return `<td class="coverage-cell">—</td>`;
  const bar = `
    <span class="coverage-bar" aria-hidden="true"
          style="width:${Math.max(0, Math.min(100, pct))}%;
                 background:${coverageColor(pct)}"></span>`;
  return `<td class="coverage-cell">${bar}<span class="coverage-value">${pct.toFixed(
    1
  )}%</span></td>`;
}

/**
 * Build one table row from a row model.
 *
 * @param {Object} row - From toRowModel.
 * @returns {string} HTML for one <tr>.
 */
function walkRowHtml(row) {
  // The link carries THIS row's network type: city.html selects the walk to
  // draw by network type and defaults to 'drive', so without it a "Roads +
  // paths" row would open the city's drive walk instead — or, for a city walked
  // only broadly, fall all the way back to the grid-attribution artifact, a
  // different metric entirely. Advertising a row the link cannot reach is worse
  // than not listing it.
  const link = row.filename
    ? `<a class="streets-view-link"
          href="city.html?file=${encodeURIComponent(row.filename)}&network=${encodeURIComponent(
        row.networkType
      )}">View on map</a>`
    : `<span class="streets-no-link" title="This city has no published run to link to">—</span>`;

  // City/state/country names come from OSM/Nominatim (publicly editable
  // third-party data) — escape everything data-derived entering innerHTML.
  return `
    <tr>
      <th scope="row">${escapeHtml(row.label)}</th>
      <td>${escapeHtml(row.providerLabel)}</td>
      <td>${escapeHtml(row.networkLabel)}</td>
      <td>${escapeHtml(row.runDate ?? "—")}</td>
      <td>${row.spacing == null ? "—" : `${num(row.spacing)} m`}</td>
      ${coverageCellHtml(row.pct)}
      ${coverageCellHtml(row.pctAny)}
      <td>${num(row.edges)}</td>
      <td>${num(row.fullyCovered)}</td>
      <td>${link}</td>
    </tr>`;
}

// The rendered rows and the active sort, kept so a header click can re-sort
// without refetching or re-joining.
let streetRows = [];
let activeSort = { ...DEFAULT_SORT };

/**
 * Paint the tbody for the active sort and reflect it on the headers.
 *
 * `aria-sort` on the <th> is what a screen reader announces; the ▲/▼ glyph is
 * decorative and hidden from the accessibility tree so the column is not read
 * as "City ▲".
 */
function renderRows() {
  document.getElementById("streets-tbody").innerHTML = sortRows(
    streetRows,
    activeSort.key,
    activeSort.dir
  )
    .map(walkRowHtml)
    .join("");

  for (const th of document.querySelectorAll("#streets-table-wrap th[data-key]")) {
    const isActive = th.dataset.key === activeSort.key;
    th.setAttribute("aria-sort", isActive ? (activeSort.dir === "asc" ? "ascending" : "descending") : "none");
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) arrow.textContent = isActive ? (activeSort.dir === "asc" ? "▲" : "▼") : "";
  }
}

/**
 * Sort by `key`: a new column starts at its natural direction (numbers
 * best-first, text A–Z); clicking the active column reverses it.
 *
 * @param {string} key - A STREET_COLUMNS key.
 */
function setSort(key) {
  const column = STREET_COLUMNS.find((c) => c.key === key);
  if (!column) return;
  activeSort =
    activeSort.key === key
      ? { key, dir: activeSort.dir === "asc" ? "desc" : "asc" }
      : { key, dir: column.initial };
  renderRows();
}

/** Wire each sortable header's button once. */
function initSorting() {
  for (const th of document.querySelectorAll("#streets-table-wrap th[data-key]")) {
    th.querySelector("button")?.addEventListener("click", () => setSort(th.dataset.key));
  }
}

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
  streetRows = walks.map((walk) =>
    toRowModel(
      walk,
      index.get(`${walk.provider}|${walk.city_id}`),
      index.get(walk.city_id)
    )
  );

  initSorting();
  renderRows();

  document.getElementById("streets-caption").textContent =
    `${walks.length} published road-walk collection${walks.length === 1 ? "" : "s"}` +
    (manifest.generated_at
      ? ` · manifest updated ${new Date(manifest.generated_at).toLocaleString()}`
      : "");

  statusEl.hidden = true;
  wrapEl.hidden = false;
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
    walkRowHtml,
    renderStreetWalks,
    STREET_COLUMNS,
    DEFAULT_SORT,
  };
}
