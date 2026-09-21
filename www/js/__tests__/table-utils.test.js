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
  sortRowsBy,
  formatCellNumber,
  coverageCellHtml,
  headerCellHtml,
  theadHtml,
  rowHtmlFromColumns,
  createSortableTable,
  providerColumnGroup,
  anyImageryLeafTitle,
  withPresetTitle,
  presetTitle,
} = require("../table-utils.js");

// cityDisplayLabel's tests moved to streetscape-utils.test.js with the
// function itself.

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

test("headerCellHtml: a grouped leaf's sort button gets a SELF-CONTAINED accessible name", () => {
  // A pivoted page's leaf labels are bare provider names repeated under every
  // metric group, so its header exposes eight sort buttons carrying three
  // distinct accessible names, in one tab order and one rotor list. Reading
  // the table is fine (AT associates the colgroup cell during table
  // navigation); a controls list gets the button's name and nothing else, and
  // the disambiguating text was in a hover-only title. pickerLabel already
  // computes exactly the right string for the column picker's flat list.
  const leaf = {
    key: "pct_mapillary",
    label: "Mapillary",
    pickerLabel: "Grid coverage (%) — Mapillary",
    title: "Share of the city's grid sample points with a 360° panorama",
  };
  const html = headerCellHtml(leaf, { key: "label", dir: "asc" });
  assert.match(html, /aria-label="Grid coverage \(%\) — Mapillary"/);
  // The VISIBLE label stays short — three of them have to fit one measure.
  assert.match(html, />Mapillary <span class="sort-arrow"/);
  // The title is unchanged and still there; aria-label does not replace it.
  assert.match(html, /title="Share of the city/);
});

test("headerCellHtml: a column with no pickerLabel emits no aria-label at all", () => {
  // driving.html's descriptors carry none, and its header markup must not
  // move — the same guarantee theadHtml's group-free branch makes.
  const html = headerCellHtml(COLUMNS[1], { key: "pct", dir: "desc" });
  assert.doesNotMatch(html, /aria-label/);
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

// --- theadHtml (issue #250: grouped two-row headers) ------------------------

const GROUPED = [
  { key: "label", label: "City", type: "text", initial: "asc", always: true, cell: () => "" },
  {
    key: "pct_gsv",
    label: "GSV",
    type: "number",
    initial: "desc",
    group: { id: "cov", label: "Grid coverage (%)", title: "Share of sample points" },
    cell: () => "",
  },
  {
    key: "pct_mapillary",
    label: "Mapillary",
    type: "number",
    initial: "desc",
    group: { id: "cov", label: "Grid coverage (%)", title: "Share of sample points" },
    cell: () => "",
  },
  {
    key: "deltaPct",
    label: "Δ",
    type: "number",
    initial: "desc",
    group: { id: "cov", label: "Grid coverage (%)", title: "Share of sample points" },
    cell: () => "",
  },
  { key: "areaKm2", label: "Grid area", type: "number", initial: "desc", cell: () => "" },
];

test("theadHtml: a group-free column set emits exactly today's single row", () => {
  // The driving.html guarantee. That page's descriptors carry no `group`, so
  // its header markup must be byte-identical to the pre-#250 output — which is
  // precisely what this expression used to be, inlined in createSortableTable.
  const activeSort = { key: "pct", dir: "desc" };
  const expected = `<tr>${COLUMNS.map((c) => headerCellHtml(c, activeSort)).join("")}</tr>`;
  assert.equal(theadHtml(COLUMNS, activeSort), expected);
  assert.equal((theadHtml(COLUMNS, activeSort).match(/<tr>/g) || []).length, 1);
});

test("theadHtml: grouped columns collapse into a colgroup cell over their leaves", () => {
  const html = theadHtml(GROUPED, { key: "pct_gsv", dir: "desc" });
  assert.equal((html.match(/<tr>/g) || []).length, 2);
  assert.match(html, /<th scope="colgroup" class="th-group" colspan="3" title="Share of sample points">Grid coverage \(%\)<\/th>/);
  // Ungrouped columns live in row 1 and span both rows, or row 2 would be
  // short by exactly the number of ungrouped columns.
  assert.match(html, /<th scope="col" rowspan="2" data-key="label"/);
  assert.match(html, /<th scope="col" rowspan="2" data-key="areaKm2"/);
  // ...and the leaves are in row 2, WITHOUT a rowspan.
  const [row1, row2] = html.split("</tr><tr>");
  assert.doesNotMatch(row1, /data-key="pct_gsv"/);
  assert.match(row2, /data-key="pct_gsv"/);
  assert.match(row2, /data-key="pct_mapillary"/);
  assert.match(row2, /data-key="deltaPct"/);
  assert.doesNotMatch(row2, /rowspan/);
});

test("theadHtml: only leaves carry data-key and aria-sort, so a group cell is inert", () => {
  // createSortableTable delegates on `closest("th[data-key]")`; a group cell
  // that carried one would sort by a column key that does not exist.
  const html = theadHtml(GROUPED, { key: "pct_mapillary", dir: "asc" });
  const groupCell = html.slice(html.indexOf('<th scope="colgroup"'));
  const groupCellOnly = groupCell.slice(0, groupCell.indexOf("</th>"));
  assert.doesNotMatch(groupCellOnly, /data-key/);
  assert.doesNotMatch(groupCellOnly, /aria-sort/);
  assert.doesNotMatch(groupCellOnly, /<button/);
  // The active leaf is still marked.
  assert.match(html, /data-key="pct_mapillary" aria-sort="ascending"/);
});

test("theadHtml: the first VISIBLE member names the group", () => {
  // A preset or the column picker can drop a group's first column; the group
  // must still be named rather than rendering an empty header.
  const withoutFirstLeaf = GROUPED.filter((c) => c.key !== "pct_gsv");
  const html = theadHtml(withoutFirstLeaf, { key: "label", dir: "asc" });
  assert.match(html, /colspan="2"[^>]*>Grid coverage \(%\)</);
});

test("theadHtml: two adjacent groups stay separate cells", () => {
  const cols = [
    GROUPED[0],
    GROUPED[1],
    { ...GROUPED[1], key: "age_gsv", group: { id: "age", label: "Median age (yrs)" } },
  ];
  const html = theadHtml(cols, { key: "label", dir: "asc" });
  assert.equal((html.match(/scope="colgroup"/g) || []).length, 2);
  assert.match(html, /colspan="1"[^>]*>Grid coverage \(%\)</);
  assert.match(html, /colspan="1">Median age \(yrs\)</);
});

test("createSortableTable: a grouped header still sorts on a leaf click", () => {
  const { theadEl, tbodyEl } = stubTable();
  const table = createSortableTable({
    columns: GROUPED,
    defaultSort: { key: "label", dir: "asc" },
    theadEl,
    tbodyEl,
  });
  table.setRows([
    { cityId: "a", label: "Aye", pct_gsv: 10, pct_mapillary: 20, deltaPct: 10, areaKm2: 1 },
    { cityId: "b", label: "Bee", pct_gsv: 90, pct_mapillary: 5, deltaPct: -85, areaKm2: 2 },
  ]);
  theadEl.clickKey("pct_gsv");
  assert.equal(table.getSort().key, "pct_gsv");
  assert.match(theadEl.innerHTML, /data-key="pct_gsv" aria-sort="descending"/);
});

test("coverageCellHtml: the compact variant adds a class and nothing else", () => {
  assert.match(coverageCellHtml(50, { compact: true }), /class="coverage-cell coverage-cell--compact"/);
  assert.equal(
    coverageCellHtml(null, { compact: true }),
    `<td class="coverage-cell coverage-cell--compact">—</td>`
  );
  // The default is unchanged, so driving.html's cells do not move.
  assert.match(coverageCellHtml(50), /class="coverage-cell"/);
});

// --- providerColumnGroup: leaf tooltips (#295) ---

// PROVIDERS is read by providerShortLabel; two entries are enough, and the
// second deliberately carries no shortLabel so the `?? label` fallback shows.
global.PROVIDERS = {
  gsv: { label: "Google Street View", shortLabel: "GSV" },
  other: { label: "Other Provider" },
};

/** Build the group and return its leaves (the Δ, if any, is not one). */
function leavesOf(extra = {}) {
  return providerColumnGroup({
    id: "cov",
    groupLabel: "Coverage",
    groupTitle: "The group's own tooltip",
    providers: ["gsv", "other"],
    keyFor: (p) => `pct_${p}`,
    cellFor: () => () => ({ html: "x" }),
    initial: "desc",
    ...extra,
  });
}

test("providerColumnGroup: leaves default to the group title", () => {
  for (const col of leavesOf()) {
    assert.equal(col.title, "The group's own tooltip");
  }
});

test("providerColumnGroup: leafTitle overrides per leaf, and the GROUP keeps its own", () => {
  // The pass-through, not just the default: `title: groupTitle` on every leaf
  // is what attached "flat imagery (Mapillary)" to KartaView's column (#295),
  // so the test mutates the hook rather than asserting the shipped string.
  const leaves = leavesOf({ leafTitle: (p) => `tooltip for ${p}` });
  assert.deepEqual(
    leaves.map((c) => c.title),
    ["tooltip for gsv", "tooltip for other"]
  );
  // Every leaf still points at ONE shared group object, whose title is
  // untouched — the group header and the leaf headers say different things.
  const groups = new Set(leaves.map((c) => c.group));
  assert.equal(groups.size, 1);
  assert.equal(leaves[0].group.title, "The group's own tooltip");
});

test("providerColumnGroup: a leafTitle returning nothing falls back to the group title", () => {
  // A hook that only knows some providers must not blank the others' tooltip.
  const leaves = leavesOf({ leafTitle: (p) => (p === "gsv" ? "only gsv" : undefined) });
  assert.equal(leaves[0].title, "only gsv");
  assert.equal(leaves[1].title, "The group's own tooltip");
});

test("anyImageryLeafTitle: the branch is the registry flag, the clause is the caller's", () => {
  // Lives here rather than in either page because BOTH call it: grid.js's
  // any-imagery column and streets.js's two. Copying the branch is how the
  // misattribution spread in the first place (#295/#296 review), so the test
  // for it is shared too.
  const restore = global.PROVIDERS.gsv.hasFlatImagery;
  try {
    global.PROVIDERS.gsv.hasFlatImagery = true;
    assert.equal(
      anyImageryLeafTitle("gsv", "Equals grid coverage"),
      "Includes Google Street View's flat/perspective imagery as well as its 360° panoramas"
    );
    global.PROVIDERS.gsv.hasFlatImagery = false;
    assert.equal(
      anyImageryLeafTitle("gsv", "Equals grid coverage"),
      "Equals grid coverage: Google Street View publishes 360° panoramas only"
    );
    // The equivalence clause is the caller's half — the two pages divide by
    // different denominators (grid points vs street-km), which is why it is a
    // parameter rather than a constant inside the helper.
    assert.match(
      anyImageryLeafTitle("gsv", "Equals the 360° street-km number"),
      /^Equals the 360° street-km number: /
    );
  } finally {
    global.PROVIDERS.gsv.hasFlatImagery = restore;
  }
  // An unregistered provider names itself by key rather than rendering
  // "undefined publishes 360° panoramas only".
  assert.match(anyImageryLeafTitle("nosuch", "Equals grid coverage"), /: nosuch publishes/);
});

// --- withPresetTitle: a default preset's assembled title ------------------
//
// This replaced `fitDefaultPreset` in #350. That function ALSO trimmed a
// default preset's columns to a leaf budget so the table fit the page measure,
// and its whole suite went with it -- deliberately: at production's four
// providers the trim was dropping a DATE group from each pivoted page, and
// there is no longer a width to trim toward, since the tables scroll.

const _cols = (spec, deltas = []) =>
  spec.flatMap(([group, keys]) =>
    keys.map((key) => ({
      key,
      group: group ? { id: group } : undefined,
      // What providerColumnGroup stamps on the one leaf that is a pairwise
      // comparison rather than a provider's own value.
      ...(deltas.includes(key) ? { isGroupDelta: true } : {}),
    }))
  );

test("withPresetTitle: a preset that spells no titleParts is returned untouched", () => {
  // Identity, not a copy: the caller keeps every other field, and an
  // unnecessary rebuild is how a preset silently loses one.
  const columns = _cols([["cov", ["a", "b"]], [null, ["z"]]]);
  const preset = { id: "overview", label: "Overview", columns: ["a", "b", "z"] };
  assert.equal(withPresetTitle(preset, columns), preset);
});

test("withPresetTitle: a preset that spells a plain title keeps it verbatim", () => {
  const columns = _cols([["cov", ["c1", "c2"]], ["age", ["a1", "a2"]]]);
  const preset = { id: "compare", title: "fixed", columns: columns.map((c) => c.key) };
  assert.equal(withPresetTitle(preset, columns).title, "fixed");
});

test("withPresetTitle: every clause survives, at every provider count", () => {
  // The regression #350 guards. The title used to be assembled from the
  // clauses whose columns SURVIVED A TRIM, so at four providers grid's
  // Overview read "how much imagery a city has and how fresh it is" and
  // showed no collection date at all. Nothing trims now, so no count drops a
  // clause -- and a count-swept assertion is what fails if a budget returns.
  const parts = { cov: "how much", age: "how fresh", collected: "when collected" };
  for (const n of [1, 2, 3, 4, 5]) {
    const names = (g) => Array.from({ length: n }, (_, i) => `${g}${i}`);
    const columns = _cols([
      ["cov", names("c")],
      ["age", names("a")],
      ["collected", names("t")],
    ]);
    const preset = {
      titleLead: "The headline read:",
      titleParts: parts,
      columns: columns.map((c) => c.key),
    };
    assert.equal(
      withPresetTitle(preset, columns).title,
      "The headline read: how much, how fresh, and when collected",
      `${n} providers`
    );
  }
});

test("withPresetTitle: a clause whose group has no leaves is not promised", () => {
  // The one filter worth keeping from the trimming version: a group with no
  // COLLECTED providers builds no leaves, and naming it would promise a
  // column that is not on the page.
  const columns = _cols([["cov", ["c1", "c2"]], ["age", ["a1", "a2"]]]);
  const preset = {
    titleLead: "The headline read:",
    titleParts: { cov: "how much", age: "how fresh", collected: "when collected" },
    columns: columns.map((c) => c.key),
  };
  assert.equal(withPresetTitle(preset, columns).title, "The headline read: how much and how fresh");
});

test("presetTitle: the clause list is Oxford-joined, and an absent one is absent", () => {
  // Spelled directly because the join is prose and the three shapes read
  // differently: "a", "a and b", "a, b, and c".
  const groupOf = (k) => k;
  const isDelta = () => false;
  const withParts = (parts) => ({ titleLead: "Lead:", titleParts: parts });
  const t = (parts, chosen) => presetTitle(withParts(parts), chosen, groupOf, isDelta);

  assert.equal(t({ a: "one", b: "two", c: "three" }, ["a", "b", "c"]), "Lead: one, two, and three");
  assert.equal(t({ a: "one", b: "two", c: "three" }, ["a", "b"]), "Lead: one and two");
  assert.equal(t({ a: "one", b: "two", c: "three" }, ["b"]), "Lead: two");
  // No lead is legal: the clauses are the whole title.
  assert.equal(
    presetTitle({ titleParts: { a: "one", b: "two" } }, ["a", "b"], groupOf, isDelta),
    "one and two"
  );
  // A preset with neither is untouched, undefined included — that is what lets
  // withPresetTitle return an untitled preset by identity.
  assert.equal(presetTitle({ columns: [] }, [], groupOf, isDelta), undefined);
  assert.equal(presetTitle({ title: "fixed" }, [], groupOf, isDelta), "fixed");
});
