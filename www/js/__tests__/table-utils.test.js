// Offline unit tests for the shared sortable-table machinery in
// table-utils.js (extracted from streets.js when the Grid page was added).
// Run with `npm test` (Node's built-in test runner) — no network, no jsdom.
//
// In the browser these helpers read shared globals from streetscape-utils.js;
// here we stub the one they touch (coverageColor).

const test = require("node:test");
const assert = require("node:assert/strict");

global.coverageColor = (pct) => `coverage(${pct})`;

const {
  cityDisplayLabel,
  sortRowsBy,
  formatCellNumber,
  coverageCellHtml,
  createSortableTable,
} = require("../table-utils.js");

// --- cityDisplayLabel (canonical copy; streets.js's cityLabel aliases it) ---

test("cityDisplayLabel: joins city, state, and country", () => {
  assert.equal(
    cityDisplayLabel({
      city: "Seattle",
      state: { name: "Washington" },
      country: { name: "United States" },
    }),
    "Seattle, Washington, United States"
  );
});

test("cityDisplayLabel: city-states don't repeat, empty records fall to Unknown", () => {
  assert.equal(
    cityDisplayLabel({ city: "Singapore", state: { name: "Singapore" }, country: { name: "Singapore" } }),
    "Singapore, Singapore"
  );
  assert.equal(cityDisplayLabel({}), "Unknown");
});

// --- sortRowsBy -------------------------------------------------------------

const COLUMNS = [
  { key: "label", label: "City", type: "text", initial: "asc" },
  { key: "pct", label: "Coverage", type: "number", initial: "desc" },
];

const ROWS = [
  { cityId: "c", label: "Cee", pct: 50 },
  { cityId: "a", label: "Aye", pct: null },
  { cityId: "b", label: "Bee", pct: 98.4 },
  { cityId: "d", label: "Dee", pct: 50 },
];

test("sortRowsBy: numeric desc puts the best first, nulls last in both directions", () => {
  assert.deepEqual(sortRowsBy(COLUMNS, ROWS, "pct", "desc").map((r) => r.cityId), ["b", "c", "d", "a"]);
  assert.deepEqual(sortRowsBy(COLUMNS, ROWS, "pct", "asc").map((r) => r.cityId), ["c", "d", "b", "a"]);
});

test("sortRowsBy: ties break on the tie key, so re-sorting is stable", () => {
  assert.deepEqual(sortRowsBy(COLUMNS, ROWS, "pct", "desc").slice(1, 3).map((r) => r.cityId), ["c", "d"]);
  assert.deepEqual(sortRowsBy(COLUMNS, ROWS, "pct", "asc").slice(0, 2).map((r) => r.cityId), ["c", "d"]);
});

test("sortRowsBy: an unknown key falls back to the first column; input not mutated", () => {
  const before = ROWS.map((r) => r.cityId);
  assert.equal(sortRowsBy(COLUMNS, ROWS, "nope", "asc").length, ROWS.length);
  assert.deepEqual(ROWS.map((r) => r.cityId), before);
});

test("sortRowsBy: a custom tie key is honored", () => {
  const rows = [
    { id: "z", v: 1 },
    { id: "a", v: 1 },
  ];
  const cols = [{ key: "v", type: "number", initial: "desc" }];
  assert.deepEqual(sortRowsBy(cols, rows, "v", "desc", "id").map((r) => r.id), ["a", "z"]);
});

// --- formatCellNumber / coverageCellHtml ------------------------------------

test("formatCellNumber: em dash for null/undefined, locale digits otherwise", () => {
  assert.equal(formatCellNumber(null), "—");
  assert.equal(formatCellNumber(undefined), "—");
  assert.equal(formatCellNumber(0), "0");
  assert.equal(formatCellNumber(12.345, 1), "12.3");
});

test("coverageCellHtml: bar clamped to 0–100%, dash cell for null", () => {
  assert.match(coverageCellHtml(137), /width:100%/);
  assert.match(coverageCellHtml(50), /coverage\(50\)/);
  assert.equal(coverageCellHtml(null), `<td class="coverage-cell">—</td>`);
});

// --- createSortableTable (stub-DOM, same approach as streets.test.js) --------

/** A minimal Element stand-in for the pieces createSortableTable touches. */
function stubTh(key) {
  const listeners = [];
  const arrow = { textContent: "" };
  const th = {
    dataset: { key },
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    querySelector(sel) {
      if (sel === "button") {
        return { addEventListener: (_type, fn) => listeners.push(fn) };
      }
      if (sel === ".sort-arrow") return arrow;
      return null;
    },
    click: () => listeners.forEach((fn) => fn()),
    arrow,
  };
  return th;
}

function stubTable(columns) {
  const ths = columns.map((c) => stubTh(c.key));
  const wrapEl = { querySelectorAll: () => ths };
  const tbodyEl = { innerHTML: "" };
  return { ths, wrapEl, tbodyEl };
}

test("createSortableTable: renders sorted rows and keeps aria-sort in step", () => {
  const { ths, wrapEl, tbodyEl } = stubTable(COLUMNS);
  const table = createSortableTable({
    columns: COLUMNS,
    defaultSort: { key: "pct", dir: "desc" },
    wrapEl,
    tbodyEl,
    rowHtml: (r) => `<tr><td>${r.cityId}</td></tr>`,
  });
  table.setRows(ROWS);
  assert.match(tbodyEl.innerHTML, /^<tr><td>b<\/td><\/tr>/);
  assert.equal(ths[1].attrs["aria-sort"], "descending");
  assert.equal(ths[0].attrs["aria-sort"], "none");
  assert.equal(ths[1].arrow.textContent, "▼");
});

test("createSortableTable: header click sorts a new column at its natural direction, re-click reverses", () => {
  const { ths, tbodyEl, wrapEl } = stubTable(COLUMNS);
  const table = createSortableTable({
    columns: COLUMNS,
    defaultSort: { key: "pct", dir: "desc" },
    wrapEl,
    tbodyEl,
    rowHtml: (r) => `[${r.cityId}]`,
  });
  table.setRows(ROWS);

  ths[0].click(); // label column: initial asc
  assert.equal(tbodyEl.innerHTML, "[a][b][c][d]");
  assert.equal(ths[0].attrs["aria-sort"], "ascending");
  assert.equal(ths[0].arrow.textContent, "▲");

  ths[0].click(); // same column: reverses
  assert.equal(tbodyEl.innerHTML, "[d][c][b][a]");
  assert.equal(ths[0].attrs["aria-sort"], "descending");
});

test("createSortableTable: setSort with an unknown key is a no-op", () => {
  const { tbodyEl, wrapEl } = stubTable(COLUMNS);
  const table = createSortableTable({
    columns: COLUMNS,
    defaultSort: { key: "pct", dir: "desc" },
    wrapEl,
    tbodyEl,
    rowHtml: (r) => `[${r.cityId}]`,
  });
  table.setRows(ROWS);
  const before = tbodyEl.innerHTML;
  table.setSort("nope");
  assert.equal(tbodyEl.innerHTML, before);
});
