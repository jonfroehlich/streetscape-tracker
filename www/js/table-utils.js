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
 * A descriptor may additionally carry `group: {id, label, title}`, repeated on
 * every member of the group, which turns the header into two rows (see
 * theadHtml) — that is how the pivoted pages (issue #250) put one sub-column
 * per provider under one metric heading. A grouped leaf's `label` is then just
 * a provider name, so `pickerLabel` supplies the self-contained wording the
 * column picker needs (read in table-controls.js).
 *
 * Depends on globals from streetscape-utils.js (loaded first): coverageColor,
 * PROVIDERS.
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

/**
 * A coverage cell: a proportional bar (decorative) behind the number.
 *
 * `compact` narrows the cell (90px rather than 128px) for the pivoted pages,
 * where one metric occupies one sub-column PER PROVIDER under a grouped
 * header: at the full width a three-provider coverage group alone would push
 * the table past its measure. The bar still reads as a proportion — it is the
 * surrounding whitespace that goes, not the bar.
 *
 * @param {?number} pct
 * @param {{compact?: boolean}} [options]
 * @returns {string} HTML for one <td>.
 */
function coverageCellHtml(pct, options) {
  const { html, className } = coverageCellParts(pct, options);
  return `<td class="${className}">${html}</td>`;
}

/**
 * The INNER content and class of a coverage cell, without its `<td>`.
 *
 * Split out so `providerColumnGroup` can wrap the content in a link without
 * doing string surgery on an assembled `<td>` (issue #250 follow-up: every
 * per-provider cell opens that provider's series).
 *
 * @param {?number} pct
 * @param {{compact?: boolean}} [options]
 * @returns {{html: string, className: string}}
 */
function coverageCellParts(pct, { compact = false } = {}) {
  const className = compact ? "coverage-cell coverage-cell--compact" : "coverage-cell";
  if (pct == null) return { html: "—", className };
  const bar = `
    <span class="coverage-bar" aria-hidden="true"
          style="width:${Math.max(0, Math.min(100, pct))}%;
                 background:${coverageColor(pct)}"></span>`;
  return { html: `${bar}<span class="coverage-value">${pct.toFixed(1)}%</span>`, className };
}

/**
 * Short, column-header form of a provider's name, falling back to its full
 * label and then to its key.
 *
 * The pivoted pages (issue #250) repeat a provider name under every metric
 * group, so "Google Street View" three times over would cost more width than
 * the numbers underneath. A provider registered without a `shortLabel` still
 * renders.
 *
 * @param {string} provider - A PROVIDERS key.
 * @returns {string}
 */
function providerShortLabel(provider) {
  const entry = PROVIDERS[provider];
  return entry?.shortLabel ?? entry?.label ?? provider;
}

/**
 * Leaf tooltip for an "any imagery" column, derived from the registry rather
 * than from a provider name.
 *
 * Shared by grid.js and streets.js because the branch is the same on both
 * pages and only the equivalence clause differs — one place to fix, rather
 * than a fix that lands on one page and leaves the other reading the old
 * sentence, which is exactly the state the #296 review found.
 *
 * The defect it replaces (#295): a group title naming ONE provider
 * ("Including flat/perspective imagery (Mapillary)") is hung verbatim on
 * every leaf, so KartaView's column — whose flat imagery is the LARGER half
 * of its data (Yogyakarta: 1,071,155 flat images against 16,913 panos) — was
 * credited to Mapillary, and GSV's, which publishes no flat imagery at all,
 * carried the same sentence.
 *
 * @param {string} provider - A PROVIDERS key.
 * @param {string} sameAs - What the number equals for a 360°-only provider,
 *   as a sentence opener: the two pages have different denominators
 *   ("Equals grid coverage" vs "Equals the 360° street-km number"), so the
 *   caller owns that half and this owns the branch.
 * @returns {string}
 *
 * @example
 *   anyImageryLeafTitle("gsv", "Equals grid coverage");
 *   // "Equals grid coverage: Google Street View publishes 360° panoramas only"
 */
function anyImageryLeafTitle(provider, sameAs) {
  const label = PROVIDERS[provider]?.label ?? provider;
  return PROVIDERS[provider]?.hasFlatImagery
    ? `Includes ${label}'s flat/perspective imagery as well as its 360° panoramas`
    : `${sameAs}: ${label} publishes 360° panoramas only`;
}

/**
 * A signed difference cell, for the pivoted pages' cross-provider Δ columns.
 *
 * Deliberately NOT colored green/red by sign. In a coverage group a positive Δ
 * means Mapillary has more imagery; in an age group a NEGATIVE Δ means
 * Mapillary is FRESHER — so one sign-to-color rule would have to lie in one of
 * the two places. The sign character carries the direction and the column's
 * `title` says what it means; `delta-pos`/`delta-neg` are emitted as styling
 * hooks. Only an exact zero is styled, because "the two providers agree
 * exactly" is a genuinely different fact from "they are close".
 *
 * @param {?number} value
 * @param {{digits?: number, unit?: string}} [options]
 * @returns {string} HTML for one <td>.
 */
function deltaCellHtml(value, { digits = 1, unit = "" } = {}) {
  if (value == null) return `<td class="delta-cell">—</td>`;
  const tone = value > 0 ? "delta-pos" : value < 0 ? "delta-neg" : "delta-zero";
  const sign = value > 0 ? "+" : "";
  return `<td class="delta-cell ${tone}">${sign}${formatCellNumber(value, digits)}${unit}</td>`;
}

/**
 * Build one grouped metric for a pivoted page: a leaf column per registered
 * provider, plus an optional Δ leaf for a head-to-head pair.
 *
 * `group` is repeated on every member — theadHtml collapses the run into one
 * `<th scope="colgroup">` and takes the label from the first VISIBLE member,
 * so dropping a leaf via a preset or the column picker still leaves the group
 * named. `pickerLabel` exists because a leaf's own label is just a provider
 * name, repeated under every metric: unambiguous in the header, useless in the
 * picker's flat checkbox list.
 *
 * Shared by grid.js and streets.js rather than copied: the two pages pivot the
 * same registry the same way, and a copy would let their headers drift apart
 * while both looked right in isolation.
 *
 * @param {Object} spec
 * @param {string} spec.id - Group id; every member repeats it.
 * @param {string} spec.groupLabel
 * @param {string} spec.groupTitle
 * @param {string[]} [spec.providers] - Which providers get a leaf, in order.
 *   Defaults to the whole registry. Callers pass the providers actually
 *   PRESENT IN THE PAYLOAD instead: a registered provider is not a collected
 *   one, and a leaf for a provider with no rows is a column of em-dashes.
 * Every per-provider LEAF cell is a link into that provider's own series when
 * `linkFor` supplies one: a row is a city now, so its per-provider numbers are
 * the only place a reader can ask for one specific series, and making them
 * click through is what stops the City cell's single link from being the whole
 * way in. The Δ leaf is deliberately never linked — it belongs to no one
 * provider. The link inherits the cell's colour and only shows an underline on
 * hover/focus, so a table of numbers does not turn into a table of blue text.
 *
 * @param {(provider: string) => string} spec.keyFor - Row-model key per provider.
 * @param {(provider: string) => Function} spec.cellFor - Cell renderer factory.
 *   The renderer returns `{html, className?, title?}` — the cell's INNER
 *   content, not an assembled `<td>`, so the link wrapper can go inside it.
 * @param {(provider: string) => Function} [spec.linkFor] - Link factory
 *   returning `{href, title}` or null for "this provider has nothing here".
 * @param {(provider: string) => string} [spec.leafLabel] - Overrides the
 *   default short provider label.
 * @param {(provider: string) => string} [spec.leafTitle] - Per-provider
 *   tooltip for the LEAF column, defaulting to `groupTitle`. Exists because a
 *   group title that enumerates providers ("flat imagery (Mapillary)") is
 *   attached verbatim to every leaf, so it misattributes the moment a third
 *   provider joins the group -- KartaView's flat-imagery column read
 *   "(Mapillary)" while carrying the largest flat count in the payload
 *   (#295). The group HEADER keeps `groupTitle`, which is why that string
 *   should say what the group measures rather than who is in it.
 * @param {string} [spec.type="number"]
 * @param {string} spec.initial - First-click sort direction.
 * @param {string} [spec.unit]
 * @param {number} [spec.digits]
 * @param {?{key: string, unit?: string, title: string}} [spec.delta] - Falsy
 *   for a group that must never have one (per-provider pano counts are
 *   census-vs-sample, so their difference answers nothing).
 * @returns {Object[]} Column descriptors, in leaf order.
 */
function providerColumnGroup({
  id,
  groupLabel,
  groupTitle,
  providers = Object.keys(PROVIDERS),
  keyFor,
  cellFor,
  linkFor,
  leafLabel,
  leafTitle,
  type = "number",
  initial,
  unit,
  digits,
  delta,
}) {
  const group = { id, label: groupLabel, title: groupTitle };
  const columns = providers.map((provider) => {
    const render = cellFor(provider);
    const link = linkFor?.(provider);
    return {
      key: keyFor(provider),
      label: leafLabel ? leafLabel(provider) : providerShortLabel(provider),
      pickerLabel: `${groupLabel} — ${providerShortLabel(provider)}`,
      type,
      initial,
      unit,
      digits,
      title: leafTitle?.(provider) ?? groupTitle,
      group,
      cell: (row) => providerCellHtml(render(row), link?.(row)),
    };
  });
  if (delta) {
    columns.push({
      key: delta.key,
      label: "Δ",
      pickerLabel: `${groupLabel} — Δ`,
      type: "number",
      initial: "desc",
      unit: delta.unit,
      digits: 1,
      title: delta.title,
      group,
      cell: (row) => deltaCellHtml(row[delta.key], { unit: delta.unit }),
    });
  }
  return columns;
}

/**
 * The "Collected by" option meaning "more than one provider", rather than a
 * provider key. It scopes to nothing in particular, so it reads as unscoped.
 */
const SCOPE_MULTI = "multi";

/**
 * Which provider the "Collected by" select is currently scoped to, or null for
 * "any provider" (including the 2+ option, which names no single one).
 *
 * @param {Object} values - Current filter values.
 * @returns {?string} A PROVIDERS key.
 */
function scopedProvider(values) {
  const scope = values?.provider;
  return scope && scope !== SCOPE_MULTI && PROVIDERS[scope] ? scope : null;
}

/**
 * The `fieldFor`/`labelFor` half of a numeric filter that follows the provider
 * scope (issue #250 follow-up).
 *
 * A pivoted row holds one value per provider, so "coverage over 80%" is
 * incomplete until you say whose coverage. The scope select answers it: pick a
 * provider and the slider reads that provider's column and says so; leave it
 * on "any" and it reads the best-across field with a label that spells the
 * quantifier out. Without this the two controls did not compose at all — see
 * resolveFilters in table-controls.js for what that cost.
 *
 * @param {Object} spec
 * @param {string} spec.base - Row-key prefix, e.g. "pct" -> `pct_gsv`.
 * @param {string} spec.bestField - The unscoped row key, e.g. "pctBest".
 * @param {string} spec.label - The metric's name, without the scope.
 * @param {string} spec.anyLabel - How the unscoped quantifier reads.
 * @returns {{fieldFor: Function, labelFor: Function}}
 */
function scopedNumericFilter({ base, bestField, label, anyLabel }) {
  return {
    fieldFor: (values) => {
      const provider = scopedProvider(values);
      return provider ? `${base}_${provider}` : bestField;
    },
    labelFor: (values) => {
      const provider = scopedProvider(values);
      return `${label} — ${provider ? providerShortLabel(provider) : anyLabel}`;
    },
  };
}

/**
 * Assemble one per-provider `<td>` from its parts, wrapping the content in a
 * link when the row has that provider's series to open.
 *
 * The cell's own `title` wins over the link's when both exist — a cell that
 * has something specific to say (the walk-to-walk churn behind a Δ) is saying
 * more than "opens this series".
 *
 * @param {{html: string, className?: string, title?: string}} parts
 * @param {?{href: string, title?: string}} link
 * @returns {string} HTML for one <td>.
 */
function providerCellHtml(parts, link) {
  const { html, className, title } = parts;
  const attrs =
    (className ? ` class="${className}"` : "") + (title ? ` title="${title}"` : "");
  if (!link) return `<td${attrs}>${html}</td>`;
  const linkTitle = !title && link.title ? ` title="${link.title}"` : "";
  return (
    `<td${attrs}><a class="provider-cell-link"${linkTitle} ` +
    `href="${link.href}">${html}</a></td>`
  );
}

/**
 * Header cell for one column descriptor.
 *
 * `aria-sort` on the <th> is what a screen reader announces; the ▲/▼ glyph is
 * decorative and hidden from the accessibility tree so the column is not read
 * as "City ▲". The <button> is what carries the click and the keyboard focus —
 * a click handler on a bare <th> is unreachable by keyboard.
 *
 * A grouped leaf's visible label is only a provider name, repeated under every
 * metric group, so the buttons of a pivoted page expose three distinct
 * accessible names across eight controls ("GSV", "Mapillary", "Delta", "GSV",
 * ...) in ONE tab order and one rotor list. Reading the table is fine — AT
 * associates the `scope="colgroup"` cell with the body cells during table
 * navigation — but a controls list gets the button's accessible name and
 * nothing else, and the disambiguating text was in a hover-only `title`.
 * `aria-label` therefore carries `pickerLabel` ("Grid coverage (%) - Mapillary"),
 * which the column picker already computes for exactly this reason. It is set
 * only where a descriptor supplies one, so driving.html's markup is unchanged.
 *
 * Labels and titles are code constants, not data, so they are interpolated
 * unescaped — unlike anything row-derived, which is OSM/Nominatim content and
 * is escaped at the point it enters innerHTML.
 *
 * @param {Object} column - Column descriptor.
 * @param {{key: string, dir: string}} activeSort
 * @param {{rowspan?: number}} [options] - `rowspan` is emitted only when a
 *   two-row grouped header is in play (see theadHtml): an UNgrouped column has
 *   no second-row leaf, so its single cell has to span both rows or the header
 *   would be one cell short on row 2. Omitting the option leaves the markup
 *   byte-identical to the single-row form.
 * @returns {string} HTML for one <th>.
 */
function headerCellHtml(column, activeSort, { rowspan } = {}) {
  const span = rowspan ? ` rowspan="${rowspan}"` : "";
  // A non-sortable column (none currently defined, but the mechanism stays
  // general) still needs a header cell so the column count matches the body
  // rows, even with nothing to sort and no visible label.
  if (column.sortable === false) {
    return `<th scope="col"${span}><span class="visually-hidden">${column.srLabel ?? ""}</span></th>`;
  }
  const isActive = column.key === activeSort.key;
  const ariaSort = isActive ? (activeSort.dir === "asc" ? "ascending" : "descending") : "none";
  const arrow = isActive ? (activeSort.dir === "asc" ? "▲" : "▼") : "";
  const title = column.title ? ` title="${column.title}"` : "";
  const ariaLabel = column.pickerLabel ? ` aria-label="${column.pickerLabel}"` : "";
  return `
    <th scope="col"${span} data-key="${column.key}" aria-sort="${ariaSort}">
      <button type="button"${ariaLabel}${title}>${column.label} <span class="sort-arrow" aria-hidden="true">${arrow}</span></button>
    </th>`;
}

/**
 * Build the whole <thead> content for a visible column set (issue #250).
 *
 * TWO shapes, and which one you get is decided by the data, not by a flag:
 *
 *  * No visible column carries a `group` → ONE `<tr>`, byte-identical to what
 *    this file emitted before grouped headers existed. That is the
 *    driving.html guarantee: that page's descriptors have no groups, so its
 *    header markup cannot move. A test pins the identity.
 *  * Otherwise TWO `<tr>`s. Row 1 collapses each run of columns sharing a
 *    `group.id` into one `<th scope="colgroup" colspan=N>`; an ungrouped
 *    column emits its normal header cell with `rowspan="2"`. Row 2 carries the
 *    leaf cells of the grouped columns only.
 *
 * Runs are contiguous because `resolveVisibleColumns` already orders the
 * visible set canonically — a group's members are adjacent in the page's
 * column list, so they are adjacent here. A group whose members were somehow
 * split would render as two separate group cells rather than misaligning the
 * body, which is the safe failure.
 *
 * `data-key`, the sort <button> and `aria-sort` live ONLY on leaf `<th>`s, so
 * createSortableTable's delegated `closest("th[data-key]")` click keeps
 * working unchanged and a click on a group cell is a no-op.
 *
 * The first VISIBLE member of a group supplies the group's label and title —
 * the descriptor repeats them on every member so that dropping the first one
 * from the view (a preset, or the column picker) still leaves the group named.
 *
 * @param {Object[]} visible - Visible column descriptors, in canonical order.
 * @param {{key: string, dir: string}} activeSort
 * @returns {string} The <thead>'s inner HTML.
 */
function theadHtml(visible, activeSort) {
  if (!visible.some((column) => column.group)) {
    return `<tr>${visible.map((column) => headerCellHtml(column, activeSort)).join("")}</tr>`;
  }
  const groupRow = [];
  const leafRow = [];
  for (let i = 0; i < visible.length; ) {
    const column = visible[i];
    if (!column.group) {
      groupRow.push(headerCellHtml(column, activeSort, { rowspan: 2 }));
      i += 1;
      continue;
    }
    let end = i;
    while (end < visible.length && visible[end].group?.id === column.group.id) end += 1;
    const members = visible.slice(i, end);
    const { label, title } = members[0].group;
    groupRow.push(
      `<th scope="colgroup" class="th-group" colspan="${members.length}"${
        title ? ` title="${title}"` : ""
      }>${label}</th>`
    );
    for (const member of members) leafRow.push(headerCellHtml(member, activeSort));
    i = end;
  }
  return `<tr>${groupRow.join("")}</tr><tr>${leafRow.join("")}</tr>`;
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
    theadEl.innerHTML = theadHtml(visible, activeSort);
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
    coverageCellParts,
    providerShortLabel,
    anyImageryLeafTitle,
    SCOPE_MULTI,
    scopedProvider,
    scopedNumericFilter,
    deltaCellHtml,
    providerCellHtml,
    providerColumnGroup,
    headerCellHtml,
    theadHtml,
    rowHtmlFromColumns,
    createSortableTable,
  };
}
