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
  headerCellHtml,
  rowHtmlFromColumns,
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
  {
    key: "label",
    label: "City",
    type: "text",
    initial: "asc",
    always: true,
    cell: (r) => `<th scope="row">${r.label}</th>`,
  },
  {
    key: "pct",
    label: "Coverage",
    type: "number",
    initial: "desc",
    cell: (r) => `<td>${r.pct}</td>`,
  },
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

// --- headerCellHtml / rowHtmlFromColumns ------------------------------------

test("headerCellHtml: marks the active column and reserves the arrow", () => {
  const active = headerCellHtml(COLUMNS[1], { key: "pct", dir: "desc" });
  assert.match(active, /data-key="pct"/);
  assert.match(active, /aria-sort="descending"/);
  assert.match(active, /▼/);

  const idle = headerCellHtml(COLUMNS[0], { key: "pct", dir: "desc" });
  assert.match(idle, /aria-sort="none"/);
  assert.doesNotMatch(idle, /[▲▼]/);
});

test("headerCellHtml: a non-sortable column gets a label-less header, no data-key", () => {
  // The trailing link column: no sort affordance, but it still needs a <th> or
  // every body row would have one more cell than the header.
  const html = headerCellHtml(
    { key: "actions", sortable: false, srLabel: "Link to city map" },
    { key: "pct", dir: "desc" }
  );
  assert.doesNotMatch(html, /data-key/);
  assert.doesNotMatch(html, /<button/);
  assert.match(html, /Link to city map/);
});

test("rowHtmlFromColumns: renders exactly the columns it is given", () => {
  const html = rowHtmlFromColumns(COLUMNS, ROWS[0]);
  assert.equal(html, "<tr><th scope=\"row\">Cee</th><td>50</td></tr>");
  // One column in, one cell out — this is the invariant that replaced the old
  // hand-maintained thead/tbody pairing.
  assert.equal(rowHtmlFromColumns([COLUMNS[1]], ROWS[0]), "<tr><td>50</td></tr>");
});

// --- createSortableTable (stub-DOM, same approach as streets.test.js) --------

/**
 * A minimal <thead> stand-in. The controller replaces the element's innerHTML
 * and delegates clicks to it, so the stub records the markup and can replay a
 * click for a given column key — asserting along the way that the key is
 * actually present in the rendered header.
 */
function stubThead() {
  const listeners = [];
  return {
    innerHTML: "",
    addEventListener(type, fn) {
      if (type === "click") listeners.push(fn);
    },
    clickKey(key) {
      assert.ok(
        this.innerHTML.includes(`data-key="${key}"`),
        `header has no sortable column ${key}`
      );
      const target = {
        closest: (sel) => (sel === "th[data-key]" ? { dataset: { key } } : null),
      };
      for (const fn of listeners) fn({ target });
    },
  };
}

function stubTable() {
  return { theadEl: stubThead(), tbodyEl: { innerHTML: "" } };
}

function makeTable(overrides = {}) {
  const { theadEl, tbodyEl } = stubTable();
  const table = createSortableTable({
    columns: COLUMNS,
    defaultSort: { key: "pct", dir: "desc" },
    theadEl,
    tbodyEl,
    ...overrides,
  });
  return { table, theadEl, tbodyEl };
}

test("createSortableTable: renders sorted rows and keeps aria-sort in step", () => {
  const { table, theadEl, tbodyEl } = makeTable();
  table.setRows(ROWS);
  assert.match(tbodyEl.innerHTML, /^<tr><th scope="row">Bee<\/th>/);
  assert.match(theadEl.innerHTML, /data-key="pct" aria-sort="descending"/);
  assert.match(theadEl.innerHTML, /data-key="label" aria-sort="none"/);
});

test("createSortableTable: header click sorts a new column at its natural direction, re-click reverses", () => {
  const { table, theadEl, tbodyEl } = makeTable();
  table.setRows(ROWS);

  theadEl.clickKey("label"); // label column: initial asc
  assert.match(tbodyEl.innerHTML, /^<tr><th scope="row">Aye<\/th>/);
  assert.match(theadEl.innerHTML, /data-key="label" aria-sort="ascending"/);

  theadEl.clickKey("label"); // same column: reverses
  assert.match(tbodyEl.innerHTML, /^<tr><th scope="row">Dee<\/th>/);
  assert.match(theadEl.innerHTML, /data-key="label" aria-sort="descending"/);
});

test("createSortableTable: sorting still works after a column change re-renders the header", () => {
  // The regression the delegated listener exists for. Listeners bound to each
  // <th>'s button at construction die the first time setColumns replaces the
  // thead's innerHTML, leaving a table whose headers look clickable and are not.
  const { table, theadEl, tbodyEl } = makeTable();
  table.setRows(ROWS);
  table.setColumns(COLUMNS); // re-render, same columns
  theadEl.clickKey("label");
  assert.match(tbodyEl.innerHTML, /^<tr><th scope="row">Aye<\/th>/);
  assert.equal(table.getSort().key, "label");
});

test("createSortableTable: dropping the sorted column falls back to a visible one", () => {
  // Otherwise the table stays ordered by a column the reader can no longer see.
  const { table, tbodyEl } = makeTable();
  table.setRows(ROWS);
  assert.equal(table.getSort().key, "pct");
  table.setColumns([COLUMNS[0]]);
  assert.equal(table.getSort().key, "label");
  assert.match(tbodyEl.innerHTML, /^<tr><th scope="row">Aye<\/th>/);
});

test("createSortableTable: setSortTo restores a direction instead of toggling it", () => {
  // A "?sort=pct&dir=desc" link must land descending even though the page
  // already opens on that column — click semantics would reverse it.
  const { table } = makeTable();
  table.setRows(ROWS);
  table.setSortTo("pct", "desc");
  assert.deepEqual(table.getSort(), { key: "pct", dir: "desc" });
  table.setSortTo("label", "asc");
  assert.deepEqual(table.getSort(), { key: "label", dir: "asc" });
});

test("createSortableTable: onSortChange fires for header clicks, not for setSortTo", () => {
  // table-controls.js listens here to repaint the strip and rewrite the URL;
  // echoing a restore straight back into the URL would be circular.
  const { table, theadEl } = makeTable();
  const seen = [];
  table.onSortChange((sort) => seen.push(sort.key));
  table.setRows(ROWS);
  theadEl.clickKey("label");
  assert.deepEqual(seen, ["label"]);
  table.setSortTo("pct", "desc");
  assert.deepEqual(seen, ["label"]);
});

test("createSortableTable: setSort with an unknown key is a no-op", () => {
  const { table, tbodyEl } = makeTable();
  table.setRows(ROWS);
  const before = tbodyEl.innerHTML;
  table.setSort("nope");
  assert.equal(tbodyEl.innerHTML, before);
});
