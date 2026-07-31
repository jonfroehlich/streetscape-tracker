/**
 * table-utils.js — shared machinery for the sortable data-table pages
 * (grid.html and streets.html).
 *
 * Extracted from streets.js when the Grid page was added so the two tables
 * share one sorter/renderer instead of drifting copies. The CSS class names
 * these helpers emit (.coverage-cell, .streets-view-link, …) predate the
 * extraction and are shared by both pages via data-table.css.
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
 * Wire a sortable table: paints the tbody for the active sort, keeps
 * `aria-sort` and the ▲/▼ glyph in step on the headers, and re-sorts on
 * header-button clicks. A new column starts at its natural direction
 * (numbers best-first, text A–Z); clicking the active column reverses it.
 *
 * `aria-sort` on the <th> is what a screen reader announces; the ▲/▼ glyph is
 * decorative and hidden from the accessibility tree so the column is not read
 * as "City ▲".
 *
 * @param {{columns: Object[], defaultSort: {key: string, dir: string},
 *          wrapEl: Element, tbodyEl: Element,
 *          rowHtml: (row: Object) => string}} cfg
 * @returns {{setRows: (rows: Object[]) => void, setSort: (key: string) => void,
 *            render: () => void}}
 */
function createSortableTable({ columns, defaultSort, wrapEl, tbodyEl, rowHtml }) {
  let rows = [];
  let activeSort = { ...defaultSort };

  function render() {
    tbodyEl.innerHTML = sortRowsBy(columns, rows, activeSort.key, activeSort.dir)
      .map(rowHtml)
      .join("");

    for (const th of wrapEl.querySelectorAll("th[data-key]")) {
      const isActive = th.dataset.key === activeSort.key;
      th.setAttribute(
        "aria-sort",
        isActive ? (activeSort.dir === "asc" ? "ascending" : "descending") : "none"
      );
      const arrow = th.querySelector(".sort-arrow");
      if (arrow) arrow.textContent = isActive ? (activeSort.dir === "asc" ? "▲" : "▼") : "";
    }
  }

  function setSort(key) {
    const column = columns.find((c) => c.key === key);
    if (!column) return;
    activeSort =
      activeSort.key === key
        ? { key, dir: activeSort.dir === "asc" ? "desc" : "asc" }
        : { key, dir: column.initial };
    render();
  }

  function setRows(next) {
    rows = next;
    render();
  }

  for (const th of wrapEl.querySelectorAll("th[data-key]")) {
    th.querySelector("button")?.addEventListener("click", () => setSort(th.dataset.key));
  }

  return { setRows, setSort, render };
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cityDisplayLabel,
    sortRowsBy,
    formatCellNumber,
    coverageCellHtml,
    createSortableTable,
  };
}
