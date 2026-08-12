/**
 * table-utils.js — shared machinery for the sortable data-table pages
 * (grid.html and streets.html).
 *
 * Extracted from streets.js when the Grid page was added so the two tables
 * share one sorter/renderer instead of drifting copies. The CSS class names
 * these helpers emit (.coverage-cell, .streets-view-link, …) predate the
 * extraction and are shared by both pages via data-table.css.
 *
 * Column descriptors are the single source of truth for BOTH the header and
 * the body (issue #188). A descriptor carries its `label`/`title` (so the
 * header can be rendered from JS once presets make the visible set dynamic)
 * and a `cell(row)` function (so a column that is not in the header cannot
 * emit a body cell). Before #188 the `<thead>` was hand-authored in each HTML
 * file and the row renderers emitted a matching, hand-maintained sequence of
 * `<td>`s — two parallel lists that a column change had to update in step.
 *
 * Depends on globals from streetscape-utils.js (loaded first): coverageColor.
 */

/**
 * Build a "City, State, Country" label from an adapted city record.
 * The canonical copy — index.js and streets.js both alias it.
 *
 * @param {Object} city - Adapted city record.
 * @returns {string}
 */
function cityDisplayLabel(city) {
  const name = city.city || city.state?.name || city.country?.name || "Unknown";
  const parts = [name];
  if (city.state?.name && city.state.name !== name) parts.push(city.state.name);
  if (city.country?.name) parts.push(city.country.name);
  return parts.join(", ");
}

/**
 * Sort row models by one column. Nulls always sink to the bottom regardless of
 * direction (a missing number is not "small" — it is absent), and the tie key
 * (city_id) keeps the order stable across reloads and re-sorts.
 *
 * Text compares with `sensitivity: "base"` so the worldwide frame's accented
 * and non-Latin city names order the way a reader expects rather than by code
 * point ("Ávila" next to "Avila", not after "Zurich").
 *
 * @param {Object[]} columns - Column descriptors ({key, type, initial}); an
 *   unknown `key` falls back to the first column rather than throwing.
 * @param {Object[]} rows - Flat row models.
 * @param {string} key - A column key.
 * @param {"asc"|"desc"} [dir]
 * @param {string} [tieKey] - Row field used as the stable tiebreaker.
 * @returns {Object[]} A new sorted array.
 */
function sortRowsBy(columns, rows, key, dir = "desc", tieKey = "cityId") {
  const column = columns.find((c) => c.key === key) ?? columns[0];
  const sign = dir === "asc" ? 1 : -1;
  const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
  return [...rows].sort((a, b) => {
    const av = a[column.key];
    const bv = b[column.key];
    if (av == null && bv == null) return collator.compare(a[tieKey], b[tieKey]);
    if (av == null) return 1;
    if (bv == null) return -1;
    const cmp = column.type === "number" ? av - bv : collator.compare(String(av), String(bv));
    return cmp !== 0 ? cmp * sign : collator.compare(a[tieKey], b[tieKey]);
  });
}

/** Format a nullable number for a table cell. */
function formatCellNumber(value, digits = 0) {
  return value == null ? "—" : value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

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
 * Header cell for one column descriptor.
 *
 * `aria-sort` on the <th> is what a screen reader announces; the ▲/▼ glyph is
 * decorative and hidden from the accessibility tree so the column is not read
 * as "City ▲". The <button> is what carries the click and the keyboard focus —
 * a click handler on a bare <th> is unreachable by keyboard.
 *
 * Labels and titles are code constants, not data, so they are interpolated
 * unescaped — unlike anything row-derived, which is OSM/Nominatim content and
 * is escaped at the point it enters innerHTML.
 *
 * @param {Object} column - Column descriptor.
 * @param {{key: string, dir: string}} activeSort
 * @returns {string} HTML for one <th>.
 */
function headerCellHtml(column, activeSort) {
  // A non-sortable column (none currently defined, but the mechanism stays
  // general) still needs a header cell so the column count matches the body
  // rows, even with nothing to sort and no visible label.
  if (column.sortable === false) {
    return `<th scope="col"><span class="visually-hidden">${column.srLabel ?? ""}</span></th>`;
  }
  const isActive = column.key === activeSort.key;
  const ariaSort = isActive ? (activeSort.dir === "asc" ? "ascending" : "descending") : "none";
  const arrow = isActive ? (activeSort.dir === "asc" ? "▲" : "▼") : "";
  const title = column.title ? ` title="${column.title}"` : "";
  return `
    <th scope="col" data-key="${column.key}" aria-sort="${ariaSort}">
      <button type="button"${title}>${column.label} <span class="sort-arrow" aria-hidden="true">${arrow}</span></button>
    </th>`;
}

/**
 * Build one body row from the visible columns.
 *
 * Each descriptor's `cell(row)` returns its own complete cell — normally a
 * <td>, but the first column returns a <th scope="row"> so the row has a
 * header. Rendering from the same list the header came from is what keeps the
 * two in step when a preset changes the visible set.
 *
 * @param {Object[]} columns - Visible column descriptors.
 * @param {Object} row - A row model.
 * @returns {string} HTML for one <tr>.
 */
function rowHtmlFromColumns(columns, row) {
  return `<tr>${columns.map((column) => column.cell(row)).join("")}</tr>`;
}

/**
 * Wire a sortable table: renders the header and body for the active sort and
 * the active column set, and re-sorts on header clicks.
 *
 * The click listener is DELEGATED to the <thead> element rather than bound to
 * each <th>'s button. The thead's innerHTML is replaced whenever the visible
 * columns change, which destroys any per-button listeners bound at
 * construction — delegation survives because the thead element itself is never
 * replaced.
 *
 * @param {{columns: Object[], defaultSort: {key: string, dir: string},
 *          theadEl: Element, tbodyEl: Element, tieKey?: string}} cfg
 *   `columns` is the initially visible set; change it later with setColumns.
 * @returns {{setRows: Function, setSort: Function, setColumns: Function,
 *            getSort: Function, getColumns: Function, render: Function}}
 */
function createSortableTable({ columns, defaultSort, theadEl, tbodyEl, tieKey = "cityId" }) {
  let rows = [];
  let visible = [...columns];
  let activeSort = { ...defaultSort };
  const sortListeners = [];

  function render() {
    theadEl.innerHTML = `<tr>${visible
      .map((column) => headerCellHtml(column, activeSort))
      .join("")}</tr>`;
    tbodyEl.innerHTML = sortRowsBy(visible, rows, activeSort.key, activeSort.dir, tieKey)
      .map((row) => rowHtmlFromColumns(visible, row))
      .join("");
  }

  function setSort(key) {
    const column = visible.find((c) => c.key === key);
    if (!column) return;
    activeSort =
      activeSort.key === key
        ? { key, dir: activeSort.dir === "asc" ? "desc" : "asc" }
        : { key, dir: column.initial };
    render();
    for (const fn of sortListeners) fn(getSort());
  }

  /**
   * Set the sort explicitly, without the click semantics (which reverse the
   * active column). This is how a `?sort=&dir=` URL is restored: a link that
   * says "descending" must land descending, not toggle into ascending because
   * the page happened to open on that column.
   *
   * Does not notify sort listeners — the caller is the one restoring state, so
   * echoing it straight back into the URL would be circular.
   */
  function setSortTo(key, dir) {
    if (!visible.some((c) => c.key === key)) return;
    activeSort = { key, dir: dir === "asc" ? "asc" : "desc" };
    render();
  }

  function setRows(next) {
    rows = next;
    render();
  }

  /**
   * Swap the visible column set (a preset change, or the column picker).
   *
   * If the active sort column is dropped from the view, sorting falls back to
   * the first sortable column rather than silently sorting by a column the
   * reader can no longer see.
   */
  function setColumns(next) {
    visible = [...next];
    if (!visible.some((c) => c.key === activeSort.key)) {
      const fallback = visible.find((c) => c.sortable !== false);
      if (fallback) activeSort = { key: fallback.key, dir: fallback.initial };
    }
    render();
  }

  function getSort() {
    return { ...activeSort };
  }

  /**
   * Subscribe to sort changes made through the header.
   *
   * The table controller deliberately knows nothing about the URL or the
   * distribution strip; table-controls.js registers here to keep both in step
   * with a header click.
   *
   * @param {(sort: {key: string, dir: string}) => void} fn
   */
  function onSortChange(fn) {
    sortListeners.push(fn);
  }

  theadEl.addEventListener("click", (event) => {
    const th = event.target.closest?.("th[data-key]");
    if (th?.dataset?.key) setSort(th.dataset.key);
  });

  return {
    setRows,
    setSort,
    setSortTo,
    setColumns,
    render,
    getSort,
    onSortChange,
    getColumns: () => [...visible],
  };
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cityDisplayLabel,
    sortRowsBy,
    formatCellNumber,
    coverageCellHtml,
    headerCellHtml,
    rowHtmlFromColumns,
    createSortableTable,
  };
}
