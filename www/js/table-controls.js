/**
 * table-controls.js — the exploration chassis for the data-table pages
 * (grid.html, streets.html and driving.html), issue #188.
 *
 * The tables outgrew "sortable list": driving.html renders ~3,800 rows,
 * grid.html ~1,190 and streets.html 283, and the questions being asked of them
 * are comparative ("where does Mapillary beat GSV?", "which cities have
 * imagery where people actually walk?") rather than lookup-by-name. This
 * module adds the machinery that makes a large table explorable — text search,
 * structured filters, column presets and per-filter histograms, rendered into
 * a sidebar beside the table — and keeps the whole view in the URL so a
 * finding can be linked to.
 *
 * Deliberately NOT an export tool. `cities.json.gz` and `streetwalks.json.gz`
 * are already published as public gzipped JSON at fixed URLs, so a CSV of the
 * on-screen view would be a lossier copy of a path that already exists; real
 * analysis pulls from the catalog instead.
 *
 * Page-agnostic: everything specific to a page arrives as descriptors
 * (`columns`, `presets`, `filters`) from grid.js / streets.js / driving.js.
 * The pure functions here are exported for the offline unit tests; only
 * `createTableControls` touches the DOM.
 *
 * Depends on globals from histogram-slider.js (loaded first):
 * createHistogramSlider, HISTOGRAM_SLIDER_BUCKETS.
 */

// ── Text search ───────────────────────────────────────────────

/**
 * Fold a value for accent- and case-insensitive substring matching.
 *
 * `Intl.Collator` (which sortRowsBy uses for ordering) has no substring
 * operation, so search normalizes instead: NFD splits an accented character
 * into base + combining mark and the mark is dropped. That is what lets
 * "avila" match "Ávila" — the worldwide frame (issue #115) means most city
 * names in the table are not plain ASCII.
 *
 * @param {*} value
 * @returns {string}
 */
function foldForSearch(value) {
  if (value == null) return "";
  return String(value)
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

/**
 * Fold a raw query into its search terms: whitespace-separated, blanks
 * dropped, each folded by foldForSearch.
 *
 * Split out so applyFilters can fold the query ONCE per pass instead of once
 * per row. It matters because of how many passes there are: every `apply()`
 * runs applyFilters for the table plus one `rowsExceptFilter` per histogram
 * (three on driving.html, four on grid.html), so driving.html's ~3,800 rows
 * meant ~19,000 NFD folds of the same three-character query per keystroke.
 *
 * @param {string} query - Raw query text.
 * @returns {string[]} Folded terms; empty for a blank query.
 */
function foldSearchTerms(query) {
  return foldForSearch(query).split(/\s+/).filter(Boolean);
}

/**
 * Each row's searched fields, folded and joined — computed once per row.
 *
 * Keyed on the row OBJECT, so the cache lives exactly as long as the row
 * models do and a reload drops it with them. It assumes the SEARCHED fields
 * are immutable once a row model is built, which is true of all three pages
 * (driving.js's mergeStreetCoverage does mutate rows, but it writes observed
 * coverage, not any of DRIVING_SEARCH_FIELDS, and it runs before the rows
 * reach the chassis at all).
 *
 * @type {WeakMap<Object, {fields: string, text: string}>}
 */
const searchHaystacks = new WeakMap();

/**
 * @param {Object} row - A row model.
 * @param {string[]} fields - Row fields to search.
 * @returns {string} The folded, joined haystack for that row.
 */
function searchHaystackFor(row, fields) {
  const fieldKey = fields.join(",");
  const cached = searchHaystacks.get(row);
  if (cached !== undefined && cached.fields === fieldKey) return cached.text;
  const text = fields.map((f) => foldForSearch(row[f])).join(" ");
  searchHaystacks.set(row, { fields: fieldKey, text });
  return text;
}

/**
 * Does a row match a free-text query?
 *
 * Every whitespace-separated term must appear in at least one of the row's
 * search fields (AND across terms, OR across fields), so "seattle mapillary"
 * narrows to one series rather than matching either word.
 *
 * @param {Object} row - A row model.
 * @param {string[]} fields - Row fields to search.
 * @param {string} query - Raw query text; blank matches everything.
 * @returns {boolean}
 */
function matchesSearch(row, fields, query) {
  return matchesSearchTerms(row, fields, foldSearchTerms(query));
}

/**
 * matchesSearch with the query already folded — the hot path.
 *
 * @param {Object} row - A row model.
 * @param {string[]} fields - Row fields to search.
 * @param {string[]} terms - Folded terms from foldSearchTerms; empty matches
 *   everything.
 * @returns {boolean}
 */
function matchesSearchTerms(row, fields, terms) {
  if (terms.length === 0) return true;
  const haystack = searchHaystackFor(row, fields);
  return terms.every((term) => haystack.includes(term));
}

// ── Structured filters ────────────────────────────────────────

/**
 * Is this a numeric-window filter — a `{min, max}` value, either nullable,
 * serialized as `"min~max"`?
 *
 * There were TWO flavours once (issue #188 follow-up): a bar-less `range` (the two
 * number inputs) and `histogram-range` (issue #250: a mini histogram plus a
 * dual-handle slider, keeping those same inputs for precision and keyboard/AT
 * parity). They differed only in what they RENDERED, so this predicate existed
 * to keep every value-shaped call site accepting both.
 *
 * `range` is gone, along with its render branch, its `.control-range` CSS and
 * the twinned tests. driving.html was its last caller and moved to
 * `histogram-range` here; keeping a second flavour warm for a hypothetical
 * caller would have kept warm a code path nothing renders, and the "parity"
 * tests that appeared to protect it proved nothing — every one of them
 * dispatched through THIS predicate first, so `f(range) === f(histogram)` was
 * comparing a branch against itself.
 *
 * The predicate itself stays, rather than inlining `type === "histogram-range"`
 * at nine call sites: it names WHY those sites are grouped (they reason about
 * the value, not the widget), and it is where the next numeric widget would be
 * admitted.
 *
 * @param {Object} filter - Filter descriptor.
 * @returns {boolean}
 */
function isRangeType(filter) {
  return filter.type === "histogram-range";
}

/**
 * Is a filter's value "unset" (and therefore not narrowing anything)?
 *
 * @param {Object} filter - Filter descriptor.
 * @param {*} value - Current value for that filter.
 * @returns {boolean}
 */
function isFilterUnset(filter, value) {
  if (value == null || value === "") return true;
  if (filter.type === "boolean") return value !== true;
  if (isRangeType(filter)) return value.min == null && value.max == null;
  return false;
}

/**
 * Apply one filter descriptor to a row.
 *
 * Range filters EXCLUDE rows whose value is null. A numeric window is a
 * question about a measured quantity, and an unmeasured row is not a small
 * one — the same posture sortRowsBy takes when it sinks nulls in both
 * directions rather than treating them as zero. (A city with no median age
 * recorded is not "0 years old".)
 *
 * @param {Object} filter - Filter descriptor.
 * @param {Object} row - A row model.
 * @param {*} value - Current value for that filter.
 * @returns {boolean}
 */
function rowPassesFilter(filter, row, value) {
  if (isFilterUnset(filter, value)) return true;
  if (isRangeType(filter)) {
    const v = row[filter.field];
    if (v == null) return false;
    if (value.min != null && v < value.min) return false;
    if (value.max != null && v > value.max) return false;
    return true;
  }
  if (filter.type === "boolean") return filter.test(row);
  // select
  return filter.test ? filter.test(row, value) : row[filter.field] === value;
}

/**
 * Narrow rows by the free-text query and every set filter.
 *
 * The query is folded ONCE here, not once per row, and each row's haystack is
 * folded once for the life of the row model (searchHaystackFor) — worth doing
 * because this function runs 4-5 times per keystroke on the largest table (the
 * table itself plus one `rowsExceptFilter` per histogram) behind a 150 ms
 * debounce.
 *
 * @param {Object[]} rows - All row models.
 * @param {Object} cfg
 * @param {Object[]} cfg.filters - Filter descriptors.
 * @param {Object} cfg.values - {filterKey: value}.
 * @param {string} cfg.query - Free-text query.
 * @param {string[]} cfg.searchFields - Row fields the query searches.
 * @returns {Object[]} A new filtered array (input untouched).
 */
function applyFilters(rows, { filters, values, query, searchFields }) {
  const terms = foldSearchTerms(query ?? "");
  return rows.filter(
    (row) =>
      matchesSearchTerms(row, searchFields, terms) &&
      filters.every((filter) => rowPassesFilter(filter, row, values[filter.key]))
  );
}

/**
 * Resolve the filters that depend on ANOTHER filter's value (issue #250
 * follow-up: the provider scope).
 *
 * A pivoted row holds one value per provider, so a numeric filter has to be
 * told WHOSE number it is asking about — and the answer is whatever the
 * "Collected by" select currently says. Before this, the two did not compose
 * at all: the sliders always read a best-across-providers field, so
 * "Collected by Mapillary" + "coverage >= 80%" returned 56 cities on the live
 * catalog and **none** of them had Mapillary coverage >= 80 — every one was
 * matched on GSV's number, and no city anywhere reaches 80 on Mapillary, so
 * the honest answer was zero rows.
 *
 * A descriptor opts in with `fieldFor(values)` / `labelFor(values)` /
 * `testFor(values)`; everything else passes through untouched, which is what
 * keeps the unpivoted page (driving.html, whose rows are places rather than
 * cities-with-a-column-per-provider) unaware of any of this. The result is a
 * SHALLOW COPY, so the originals stay the page's static descriptors and
 * nothing accumulates state across renders.
 *
 * @param {Object[]} filters - Filter descriptors.
 * @param {Object} values - Current {filterKey: value}.
 * @returns {Object[]} Filters with `field`/`label`/`test` resolved.
 */
function resolveFilters(filters, values) {
  return filters.map((filter) => {
    if (!filter.fieldFor && !filter.labelFor && !filter.testFor) return filter;
    return {
      ...filter,
      field: filter.fieldFor ? filter.fieldFor(values) : filter.field,
      label: filter.labelFor ? filter.labelFor(values) : filter.label,
      test: filter.testFor ? filter.testFor(values) : filter.test,
    };
  });
}

/**
 * Narrow rows by everything EXCEPT one filter (issue #250).
 *
 * The crossfilter rule for a histogram-slider: the bars a slider draws must be
 * computed over the rows every OTHER control has selected, never over its own
 * selection. Feeding a filter its own output makes the histogram change under
 * its own interaction — drag a handle in, the bars outside the window vanish,
 * so the next drag is measured against a different picture (and dragging back
 * out cannot restore what is no longer drawn). The other controls still count,
 * which is the point: searching "Oregon" really should redraw the coverage
 * bars.
 *
 * @param {Object[]} rows - All row models.
 * @param {Object} cfg - Same shape applyFilters takes.
 * @param {string} selfKey - The filter to leave out.
 * @returns {Object[]} A new filtered array (input untouched).
 */
function rowsExceptFilter(rows, cfg, selfKey) {
  return applyFilters(rows, {
    ...cfg,
    filters: cfg.filters.filter((filter) => filter.key !== selfKey),
  });
}

// ── Column presets ────────────────────────────────────────────

/**
 * Resolve the visible column descriptors for a preset or an explicit key list.
 *
 * Explicit keys win whenever the picker has been touched at all — including
 * down to zero optional columns. That is `explicitKeys` being a non-null
 * array (possibly empty), which is distinct from `null` ("no picker
 * deviation, use the preset"). Testing `.length > 0` instead would treat
 * "every box unchecked" as "no override" and silently snap back to the
 * preset's columns while the checkboxes still read unchecked.
 * Order always follows the canonical `columns` order rather than the order the
 * keys arrived in, so the table's column sequence is stable no matter how the
 * URL was assembled. Unknown keys are ignored rather than throwing: a stale
 * link from before a column was renamed should degrade, not break the page.
 *
 * Structural columns — currently just City, which also carries the row's
 * link out to the city page — are `always: true` and appear in every preset,
 * including when the picker has zeroed out every optional column.
 *
 * @param {Object[]} columns - All column descriptors.
 * @param {Object[]} presets - Preset descriptors ({id, label, columns}).
 * @param {?string} presetId
 * @param {?string[]} explicitKeys - `null` means "no picker override"; an
 *   array (including `[]`) is an explicit selection.
 * @returns {Object[]} Visible descriptors, in canonical order.
 */
function resolveVisibleColumns(columns, presets, presetId, explicitKeys) {
  let wanted;
  if (explicitKeys != null) {
    wanted = new Set(explicitKeys);
  } else {
    const preset = presets.find((p) => p.id === presetId) ?? presets[0];
    wanted = new Set(preset.columns);
  }
  return columns.filter((column) => column.always === true || wanted.has(column.key));
}

// ── URL round-trip ────────────────────────────────────────────

/**
 * Parse a query string into control state.
 *
 * Every unrecognized or malformed value falls back to the default rather than
 * throwing — a URL is user-editable text, and a bad `?pct=` should not blank
 * the page.
 *
 * A `select` filter may declare a `defaultValue` (issue #250, streets.html's
 * network selector). That makes ABSENCE of the parameter mean the default
 * rather than "no filter": the page has no "all networks" reading, because
 * road-km coverage under two different network denominators must never sit in
 * one comparable column. An unknown value falls back to the same default, for
 * the same reason — dropping the filter entirely would double every city's
 * rows on a hand-edited URL.
 *
 * @param {string} search - `location.search`.
 * @param {{filters: Object[]}} cfg
 * @returns {{query: string, preset: ?string, cols: ?string[],
 *            sort: ?{key: string, dir: string}, values: Object}}
 */
function parseTableState(search, { filters }) {
  const params = new URLSearchParams(search || "");
  const values = {};
  for (const filter of filters) {
    const raw = params.get(filter.key);
    if (raw == null || raw === "") {
      if (filter.defaultValue != null) values[filter.key] = filter.defaultValue;
      continue;
    }
    if (isRangeType(filter)) {
      // "10~90", "10~" (min only), "~90" (max only). Bare "~" is unset.
      const [minRaw, maxRaw] = raw.split("~");
      const min = Number.parseFloat(minRaw);
      const max = Number.parseFloat(maxRaw);
      const parsed = {
        min: Number.isFinite(min) ? min : null,
        max: Number.isFinite(max) ? max : null,
      };
      if (parsed.min != null || parsed.max != null) values[filter.key] = parsed;
    } else if (filter.type === "boolean") {
      if (raw === "1") values[filter.key] = true;
    } else if (filter.options.some((o) => o.value === raw)) {
      values[filter.key] = raw;
    } else if (filter.defaultValue != null) {
      values[filter.key] = filter.defaultValue;
    }
  }

  const dir = params.get("dir");
  const sortKey = params.get("sort");
  // `?cols=` (present, empty) is an explicit "picker zeroed out every optional
  // column" and must round-trip as `[]`, not fall back to `null` ("no picker
  // override, use the preset") the way a plain falsy check on the raw string
  // would. Presence of the param is what carries that distinction, not its
  // content.
  return {
    query: params.get("q") ?? "",
    preset: params.get("preset"),
    cols: params.has("cols") ? params.get("cols").split(",").filter(Boolean) : null,
    sort: sortKey ? { key: sortKey, dir: dir === "asc" ? "asc" : "desc" } : null,
    values,
  };
}

/**
 * Serialize control state back into a query string.
 *
 * Only non-default state is written, so an untouched page keeps a clean URL
 * and a shared link carries exactly the deviations that produced the view.
 *
 * @param {Object} state - {query, preset, cols, sort, values}.
 * @param {{filters: Object[], defaultPreset: string}} cfg
 * @returns {string} A query string WITHOUT the leading "?" (may be empty).
 */
function serializeTableState(state, { filters, defaultPreset }) {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.preset && state.preset !== defaultPreset) params.set("preset", state.preset);
  // `state.cols` is `null` for "no picker override" and an array (possibly
  // `[]`, when every optional column has been unchecked) for an explicit
  // selection — both must be distinguishable after a round trip, so an empty
  // selection still writes `cols=` rather than being dropped like `null` is.
  if (state.cols) params.set("cols", state.cols.join(","));
  if (state.sort) {
    params.set("sort", state.sort.key);
    params.set("dir", state.sort.dir);
  }
  for (const filter of filters) {
    const value = state.values[filter.key];
    if (isFilterUnset(filter, value)) continue;
    // A select sitting on its declared default is the page's opening view, so
    // it is omitted for the same reason the default preset is: only deviations
    // belong in a shared link.
    if (filter.defaultValue != null && value === filter.defaultValue) continue;
    if (isRangeType(filter)) {
      params.set(filter.key, `${value.min ?? ""}~${value.max ?? ""}`);
    } else if (filter.type === "boolean") {
      params.set(filter.key, "1");
    } else {
      params.set(filter.key, value);
    }
  }
  return params.toString();
}

// ── Histogram bucketing ───────────────────────────────────────

/**
 * Bucket numeric values into a histogram, over an axis the CALLER fixes.
 *
 * Nulls are dropped rather than bucketed as zero (see rowPassesFilter). A
 * single distinct value yields one full-width bucket instead of a degenerate
 * zero-width range.
 *
 * The axis is never taken from the values themselves. A histogram-slider's
 * axis has to stay put while the reader brushes it, or the handles' meaning
 * moves out from under their hand — and the bars have to be bucketed over the
 * SNAPPED domain the component hands back, not the raw extent, or the two are
 * painted across the same 100% width on two different scales (measured at
 * 1.05% of the track on a 0–85.1 coverage axis). The sorted-column
 * distribution strip did scale itself to the rows in view, which is why this
 * carried a self-scaling default until that strip was retired; it does not,
 * now, because nothing left on the site wants one.
 *
 * @param {Array<?number>} values
 * @param {number} bucketCount
 * @param {{min: number, max: number}} domain - The fixed axis. Values outside
 *   it are clamped into the end buckets rather than dropped, so a stale domain
 *   under-draws rather than losing rows.
 * @returns {{buckets: {from: number, to: number, count: number}[],
 *            min: number, max: number, count: number}|null}
 *   Null when nothing is measurable.
 */
function histogramBuckets(values, bucketCount, domain) {
  const nums = values.filter((v) => typeof v === "number" && Number.isFinite(v));
  if (nums.length === 0) return null;
  const { min, max } = domain;
  if (min === max) {
    return { buckets: [{ from: min, to: max, count: nums.length }], min, max, count: nums.length };
  }
  const width = (max - min) / bucketCount;
  const buckets = Array.from({ length: bucketCount }, (_, i) => ({
    from: min + i * width,
    to: min + (i + 1) * width,
    count: 0,
  }));
  for (const value of nums) {
    // The maximum lands in the last bucket rather than one past the end; with
    // a fixed domain, so does anything beyond it (and anything below the floor
    // lands in the first) — clamped at both ends, not discarded.
    const index = Math.min(bucketCount - 1, Math.max(0, Math.floor((value - min) / width)));
    buckets[index].count += 1;
  }
  return { buckets, min, max, count: nums.length };
}

// ── DOM wiring ────────────────────────────────────────────────

/**
 * Build the controls markup for a page.
 *
 * Real labeled form controls throughout: the search box is a `<label>`ed
 * `<input type="search">`, filters are `<select>`/number inputs/checkboxes,
 * and the column picker is a `<details>` disclosure wrapping a checkbox
 * `<fieldset>` — so the whole region is keyboard-reachable and announced
 * without any ARIA of its own.
 *
 * One section order, for the one place these controls are rendered: a ~280px
 * sidebar beside the table. Search, selects, the column controls, numeric
 * windows, booleans, Clear all (issue #250). In a column that narrow the
 * reading order IS the layout, so the cheap categorical narrowings come first
 * and the tall histogram brushes sit below the column controls rather than
 * pushing them off the bottom.
 *
 * There was a second, horizontal order for driving.html until that page moved
 * to the sidebar too; it is gone rather than kept warm, since an alternative
 * layout nothing renders is one nothing tests either.
 *
 * The order partitions by filter TYPE, so it also carries an "everything else"
 * bucket: a filter type added later must still be rendered somewhere rather
 * than silently vanishing.
 *
 * @param {Object} cfg - {filters, presets, columns, searchPlaceholder}.
 * @returns {string} HTML.
 */
function controlsHtml({ filters, presets, columns, searchPlaceholder }) {
  const renderFilter = (filter) => {
      if (filter.type === "select") {
        // A select with a declared default has no "any" reading (issue #250:
        // streets.html's network selector — two networks means two different
        // street-km denominators, which must never share a column), so the
        // blank option is omitted rather than offered and then ignored.
        const options = (filter.defaultValue != null
          ? []
          : [`<option value="">${filter.anyLabel ?? "All"}</option>`]
        )
          .concat(filter.options.map((o) => `<option value="${o.value}">${o.label}</option>`))
          .join("");
        return `
          <div class="control">
            <label for="f-${filter.key}">${filter.label}</label>
            <select id="f-${filter.key}" data-filter="${filter.key}">${options}</select>
          </div>`;
      }
      // The number inputs are the PRECISION path under the brush, and they are
      // what syncControlsToState, handleControlChange and the e2e selectors
      // read (`data-filter`/`data-bound`). They were shared verbatim with a
      // second, bar-less `range` flavour until that flavour lost its last
      // caller; see isRangeType for why it is gone rather than kept warm.
      const boundInputs = () => `<input type="number" data-filter="${filter.key}" data-bound="min"
                   aria-label="Minimum ${filter.label}" placeholder="min"
                   ${filter.min != null ? `min="${filter.min}"` : ""}
                   ${filter.max != null ? `max="${filter.max}"` : ""}>
            <span class="control-dash" aria-hidden="true">–</span>
            <input type="number" data-filter="${filter.key}" data-bound="max"
                   aria-label="Maximum ${filter.label}" placeholder="max"
                   ${filter.min != null ? `min="${filter.min}"` : ""}
                   ${filter.max != null ? `max="${filter.max}"` : ""}>`;
      if (filter.type === "histogram-range") {
        // The bars are decorative (aria-hidden): the two range thumbs carry a
        // live aria-valuetext and the number inputs carry the exact figures,
        // so nothing here is announced twice or only visually.
        return `
          <div class="control control-histogram" data-histogram="${filter.key}"
               role="group" aria-labelledby="f-${filter.key}-legend">
            <span class="control-legend" id="f-${filter.key}-legend">${filter.label}</span>
            <div class="hist-slider">
              <div class="hist-bars" aria-hidden="true"></div>
              <div class="hist-track" aria-hidden="true"><div class="hist-fill"></div></div>
              <input type="range" class="hist-lo" aria-label="Minimum ${filter.label}">
              <input type="range" class="hist-hi" aria-label="Maximum ${filter.label}">
            </div>
            <div class="hist-bounds">
              ${boundInputs()}
            </div>
          </div>`;
      }
      return `
        <div class="control control-check">
          <input type="checkbox" id="f-${filter.key}" data-filter="${filter.key}">
          <label for="f-${filter.key}"${
            filter.title ? ` title="${filter.title}"` : ""
          }>${filter.label}</label>
        </div>`;
  };

  const presetOptions = presets
    .map((p) => `<option value="${p.id}"${p.title ? ` title="${p.title}"` : ""}>${p.label}</option>`)
    .join("");

  // `pickerLabel` overrides `label` here because a pivoted page's leaf labels
  // are provider names ("GSV", "Mapillary") repeated across every metric group
  // — unambiguous under their group header, meaningless in a flat checkbox
  // list. Pages that don't set one are unchanged.
  const pickerBoxes = columns
    .filter((c) => c.always !== true)
    .map(
      (c) => `
        <label class="col-toggle">
          <input type="checkbox" data-column="${c.key}"> ${c.pickerLabel ?? c.label}
        </label>`
    )
    .join("");

  const searchControl = `
      <div class="control control-search">
        <label for="table-search">Search</label>
        <input type="search" id="table-search"
               placeholder="${searchPlaceholder ?? "City, provider…"}"
               autocomplete="off" spellcheck="false">
      </div>`;

  const columnControls = `
      <div class="control">
        <label for="table-preset">Columns</label>
        <select id="table-preset">${presetOptions}</select>
      </div>
      <details class="col-picker">
        <summary>Customize</summary>
        <div class="col-panel">
          <fieldset>
            <legend class="visually-hidden">Columns to show</legend>
            ${pickerBoxes}
          </fieldset>
          <button type="button" class="col-reset">Reset to preset</button>
        </div>
      </details>`;

  const clearControl = `
      <button type="button" class="controls-clear">Clear all</button>`;

  // Partition, don't hand-list: `rest` catches any filter type not named here,
  // so a type added later renders in the wrong PLACE rather than not at all.
  // The order is search -> selects -> columns -> numeric windows -> booleans
  // -> clear.
  const selects = filters.filter((f) => f.type === "select");
  const ranges = filters.filter(isRangeType);
  const booleans = filters.filter((f) => f.type === "boolean");
  const placed = new Set([...selects, ...ranges, ...booleans]);
  const rest = filters.filter((f) => !placed.has(f));
  const render = (list) => list.map(renderFilter).join("");
  const body =
    searchControl +
    render(selects) +
    columnControls +
    render(ranges) +
    render(booleans) +
    render(rest) +
    clearControl;

  return `
    <div class="table-controls">${body}
    </div>`;
}

/**
 * Wire the controls region to a sortable table.
 *
 * Owns the filter/search/preset state, pushes the narrowed rows into the
 * table, keeps the URL in step, and redraws each filter's histogram whenever
 * the filtered set changes.
 *
 * The URL is written with `replaceState`, not `pushState`: exploring a table is
 * a continuous adjustment, and one history entry per keystroke would make the
 * Back button useless. The current view is still linkable, which is the point.
 *
 * @param {Object} cfg
 * @param {Element} cfg.rootEl - Container the controls render into.
 * @param {Object} cfg.table - Controller from createSortableTable.
 * @param {Object[]} cfg.columns - All column descriptors.
 * @param {Object[]} cfg.presets - Preset descriptors.
 * @param {Object[]} cfg.filters - Filter descriptors.
 * @param {string[]} cfg.searchFields - Row fields the text query searches.
 * @param {Function} [cfg.onChange] - Called with the filtered rows, every row,
 *   and a snapshot of the control state, after every change — for the page's
 *   caption/result count. The third argument is what lets a caption name a
 *   filter's active value (streets.html's network) without reaching into the
 *   DOM for it.
 * @returns {{setRows: Function, getFilteredRows: Function}}
 */
/**
 * The filter values an untouched page opens on.
 *
 * Only `defaultValue` selects contribute: everything else's "unset" is the
 * absence of a key. This is what "Clear all" resets TO — blanking a defaulted
 * select instead would leave streets.html showing both network types at once,
 * i.e. two incomparable street-km denominators stacked in one column.
 *
 * @param {Object[]} filters - Filter descriptors.
 * @returns {Object} {filterKey: value}.
 */
function defaultFilterValues(filters) {
  const values = {};
  for (const filter of filters) {
    if (filter.defaultValue != null) values[filter.key] = filter.defaultValue;
  }
  return values;
}

/**
 * Wrap the controls container in the sidebar chrome, and reveal the layout.
 *
 * The `<aside>` landmark, the collapsible `<details>` and its "Filters"
 * summary used to be hand-copied into all three page HTMLs. That made a
 * wrapper change — the summary text, the `aria-label`, the `open` default — a
 * three-file edit whose failure mode was silent: miss one page and
 * `wireSidebarDisclosure` simply returns null there, so its filters stay
 * collapsed after a widen with no toggle to reopen them.
 *
 * Revealing the layout is the same argument from the other side. The pages
 * author `.table-layout` as `hidden`, and every empty-state and error path
 * returns BEFORE createTableControls — so a deployment with nothing published
 * (driving.html before its first `fetch-driving-plan`, streets.html before the
 * first road walk) used to render a viewport-tall empty white sidebar beside
 * its status line, and an empty landmark to assistive tech. Clearing the
 * attribute here rather than per page means the reveal cannot be forgotten by
 * whichever page is added next.
 *
 * @param {Element} rootEl - The `#<page>-controls` container authored in HTML.
 * @returns {?Element} The layout element, if there was one.
 */
function mountSidebar(rootEl) {
  if (typeof document === "undefined") return null;
  const layoutEl = rootEl.closest(".table-layout");
  if (rootEl.closest(".table-sidebar")) {
    // Already wrapped — a page that calls createTableControls twice.
    layoutEl?.removeAttribute("hidden");
    return layoutEl;
  }
  const asideEl = document.createElement("aside");
  asideEl.className = "table-sidebar";
  asideEl.setAttribute("aria-label", "Search and filters");
  const detailsEl = document.createElement("details");
  detailsEl.className = "sidebar-disclosure";
  detailsEl.open = true;
  const summaryEl = document.createElement("summary");
  summaryEl.textContent = "Filters";
  detailsEl.append(summaryEl);
  asideEl.append(detailsEl);
  // Put the aside exactly where the container was, then adopt the container
  // into it: `rootEl` keeps its identity, so everything downstream that reads
  // or listens on it is unaffected.
  rootEl.replaceWith(asideEl);
  detailsEl.append(rootEl);
  layoutEl?.removeAttribute("hidden");
  return layoutEl;
}

function createTableControls({
  rootEl,
  table,
  columns,
  presets,
  filters,
  searchFields,
  searchPlaceholder,
  onChange,
}) {
  const defaultPreset = presets[0].id;
  let allRows = [];
  let filtered = [];
  // No `sort` field here: the sort is owned entirely by `table` (the
  // createSortableTable controller), read via `table.getSort()` wherever it's
  // needed (updateUrl). Duplicating it into this state object would just be a
  // second, driftable copy of the same fact.
  const state = {
    query: "",
    preset: defaultPreset,
    cols: null,
    values: defaultFilterValues(filters),
  };

  const layoutEl = mountSidebar(rootEl);
  rootEl.innerHTML = controlsHtml({ filters, presets, columns, searchPlaceholder });
  // Wired here rather than on DOMContentLoaded: the disclosure it needs does
  // not exist until mountSidebar has run.
  wireSidebarDisclosure(layoutEl ?? undefined);
  const searchEl = rootEl.querySelector("#table-search");
  const presetEl = rootEl.querySelector("#table-preset");

  // ── Histogram-sliders (issue #250) ──
  // One per `histogram-range` filter, each with a FIXED axis seeded once from
  // the full row set. `entry.domain` is whatever `setDomain` HANDED BACK, not
  // the raw extent passed in: the component snaps the ends outward to whole
  // steps, and the thumbs and `.hist-fill` are positioned over that snapped
  // axis while `repaintHistograms` draws the bars across the same 100% width.
  // Keeping the pre-snap copy here is what would let the bars and the handles
  // describe different scales (measured 1.05% of the track at the data max on
  // a 0-85.1 coverage axis).
  // The filters as they read RIGHT NOW: a scoped descriptor's field, label and
  // test all depend on another control's value, so everything that reasons
  // about a filter reads this rather than the static `filters`. Re-derived
  // whenever the values change (see refreshResolved).
  let resolved = resolveFilters(filters, state.values);
  const filterFor = (key) => resolved.find((f) => f.key === key);

  /**
   * Write a numeric window into its two precision inputs.
   *
   * THE single writer, because there are FOUR paths that change a window — a
   * typed bound, that bound being NORMALIZED (swapped, nulled at an edge, or
   * clamped past one), a dragged handle, and a scope change clearing it — and
   * every one of them has to leave the inputs agreeing with `state.values`.
   * The scope-clear path is the one that got this wrong first: the filter was
   * genuinely cleared and the table re-filtered, while the min box went on
   * reading "80". The normalization path is the same shape and was missed in
   * the first pass — see handleControlChange.
   */
  function writeRangeInputs(key, value) {
    const [minEl, maxEl] = rootEl.querySelectorAll(`[data-filter="${key}"]`);
    if (!minEl || !maxEl) return;
    minEl.value = value?.min ?? "";
    maxEl.value = value?.max ?? "";
  }

  const histograms = new Map(); // filter key -> {slider, domain, field}
  for (const filter of filters) {
    if (filter.type !== "histogram-range") continue;
    const el = rootEl.querySelector(`.control-histogram[data-histogram="${filter.key}"]`);
    if (!el || typeof createHistogramSlider !== "function") continue;
    const entry = { key: filter.key, domain: null, slider: null, field: null };
    entry.slider = createHistogramSlider({
      rootEl: el,
      filter,
      onInput: (range) => {
        state.values[filter.key] = range;
        // Keep the precision inputs showing what the handles say, so a drag
        // and a typed bound can never disagree about the current window.
        writeRangeInputs(filter.key, range);
        // Debounced on the SAME timer as a typed bound: a drag emits on every
        // pointer move, and on driving.html that is ~3,800 rows re-filtered and
        // re-rendered per frame.
        clearTimeout(rangeTimer);
        rangeTimer = setTimeout(apply, 150);
      },
    });
    histograms.set(filter.key, entry);
  }

  /**
   * Fix each histogram's axis from the FULL row set, clamped by whatever the
   * descriptor declares.
   *
   * An axis recomputed under a BRUSH would move the handles' meaning out from
   * under the reader's hand, so it is seeded once per field and then left
   * alone. A SCOPE change is the one thing that legitimately moves it — the
   * slider is now asking about a different population, and a Mapillary-scoped
   * coverage axis genuinely should not span GSV's range. That is a different
   * gesture from brushing, and it is the only case this re-seeds on.
   *
   * The scoped filter's window is CLEARED when its field changes, rather than
   * carried across and clamped into the new domain. Clamping would silently
   * rewrite the question: ">= 80%" against a 0-47.6% Mapillary axis would
   * become ">= 47.6%" and return a row, where the truthful answer is none.
   *
   * @param {boolean} [clearOnChange] - Clear a filter whose field moved. False
   *   on the initial seed, where the field has not "changed" — it arrived that
   *   way from the URL, together with the window that belongs to it.
   * @param {Object} [opts]
   * @param {boolean} [opts.force] - Re-seed even where the field has not
   *   moved. `setRows` passes this, because the ROWS are what an axis is
   *   derived from: gating on the field alone made a second `setRows` keep the
   *   first row set's axis, which is not what a method called "set the rows"
   *   should do. Not reachable while both pages render once, which is exactly
   *   why it is worth closing now.
   */
  function syncHistogramDomains(clearOnChange = true, { force = false } = {}) {
    for (const entry of histograms.values()) {
      const filter = filterFor(entry.key);
      if (!force && entry.field === filter.field) continue;
      const changed = entry.field !== null;
      entry.field = filter.field;
      const values = allRows
        .map((row) => row[filter.field])
        .filter((v) => typeof v === "number" && Number.isFinite(v));
      let min = values.length ? Math.min(...values) : (filter.min ?? 0);
      let max = values.length ? Math.max(...values) : (filter.max ?? 1);
      if (filter.min != null) min = Math.max(min, filter.min);
      if (filter.max != null) max = Math.min(max, filter.max);
      if (changed && clearOnChange) {
        delete state.values[entry.key];
        writeRangeInputs(entry.key, null);
      }
      // The SNAPPED axis, which is the one histogramBuckets has to bucket over.
      entry.domain = entry.slider.setDomain({ min, max: max > min ? max : min + 1 });
      entry.slider.setValue(state.values[entry.key]);
      entry.slider.setLabel(filter.label);
    }
  }

  /**
   * Redraw every histogram over the rows the OTHER controls have selected.
   *
   * Never over `filtered`: feeding a slider its own output makes the picture
   * collapse under the brush that drew it (and dragging back out cannot
   * restore bars that are no longer there). Costs one extra filter pass per
   * histogram: four extra passes over grid.html's ~1,190 rows, three over
   * driving.html's ~3,800.
   */
  function repaintHistograms() {
    for (const [key, entry] of histograms) {
      const rows = rowsExceptFilter(
        allRows,
        { filters: resolved, values: state.values, query: state.query, searchFields },
        key
      );
      entry.slider.setHistogram(
        histogramBuckets(
          rows.map((row) => row[entry.field]),
          HISTOGRAM_SLIDER_BUCKETS,
          entry.domain
        )
      );
    }
  }

  /**
   * Re-derive the resolved filters and everything downstream of a scope
   * change: each histogram's axis, its window, and the wording that says whose
   * numbers it is asking about.
   */
  function refreshResolved({ clearOnScopeChange = true, reseedDomains = false } = {}) {
    resolved = resolveFilters(filters, state.values);
    syncHistogramDomains(clearOnScopeChange, { force: reseedDomains });
    for (const filter of resolved) if (filter.labelFor) relabelControl(filter);
  }

  /**
   * Write a resolved label back onto its control.
   *
   * A scoped filter's wording is the ONLY thing that says whose numbers it is
   * asking about, so it has to move with the scope — leaving it static is the
   * invisible-semantics problem this whole change exists to fix. Two shapes to
   * cover: a numeric window labels itself with a `.control-legend` span and two
   * aria-labelled bounds, everything else with a plain `<label for>`.
   */
  function relabelControl(filter) {
    const legend = rootEl.querySelector(`#f-${filter.key}-legend`);
    if (legend) {
      legend.textContent = filter.label;
      const [minEl, maxEl] = rootEl.querySelectorAll(`[data-filter="${filter.key}"]`);
      minEl?.setAttribute("aria-label", `Minimum ${filter.label}`);
      maxEl?.setAttribute("aria-label", `Maximum ${filter.label}`);
      return;
    }
    const label = rootEl.querySelector(`label[for="f-${filter.key}"]`);
    if (label) label.textContent = filter.label;
  }

  // ── state → DOM ──
  function syncControlsToState() {
    searchEl.value = state.query;
    presetEl.value = state.preset;
    for (const filter of filters) {
      const value = state.values[filter.key];
      if (isRangeType(filter)) {
        // Exactly the two number inputs: a histogram-range's <input type=range>
        // handles deliberately carry no `data-filter`, so writeRangeInputs
        // finds the same [min, max] pair for both range flavours.
        writeRangeInputs(filter.key, value);
      } else {
        const el = rootEl.querySelector(`[data-filter="${filter.key}"]`);
        if (filter.type === "boolean") el.checked = value === true;
        else el.value = value ?? "";
      }
    }
    // Adopt WITHOUT reporting: this runs when the URL or "Clear all" is the
    // source of the value, and echoing it back through onInput would be
    // circular.
    for (const [key, entry] of histograms) entry.slider.setValue(state.values[key]);
    for (const filter of resolved) if (filter.labelFor) relabelControl(filter);
    const visibleKeys = new Set(
      resolveVisibleColumns(columns, presets, state.preset, state.cols).map((c) => c.key)
    );
    for (const box of rootEl.querySelectorAll("[data-column]")) {
      box.checked = visibleKeys.has(box.dataset.column);
    }
  }

  function updateUrl() {
    if (typeof history === "undefined" || !history.replaceState) return;
    const qs = serializeTableState({ ...state, sort: table.getSort() }, { filters, defaultPreset });
    history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
  }

  /** Re-filter, repaint the table, the histograms, and the URL. */
  function apply() {
    // A scope change lands here through the same path as any other control, so
    // this is where the resolved filters catch up before anything reads them.
    refreshResolved();
    filtered = applyFilters(allRows, {
      filters: resolved,
      values: state.values,
      query: state.query,
      searchFields,
    });
    table.setRows(filtered);
    repaintHistograms();
    updateUrl();
    onChange?.(filtered, allRows, { values: { ...state.values }, preset: state.preset });
  }

  function applyColumns() {
    table.setColumns(resolveVisibleColumns(columns, presets, state.preset, state.cols));
  }

  // ── DOM → state ──
  let searchTimer = null;
  searchEl.addEventListener("input", () => {
    // Debounced: ~3,800 rows re-filter and re-render on every keystroke.
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = searchEl.value;
      apply();
    }, 150);
  });

  presetEl.addEventListener("change", () => {
    state.preset = presetEl.value;
    state.cols = null; // an explicit preset choice discards a picker deviation
    applyColumns();
    syncControlsToState();
    updateUrl();
  });

  /** Fold one control's current DOM value into `state` and repaint. */
  /**
   * Apply one control's current DOM state.
   *
   * @param {Element} target
   * @param {Object} [opts]
   * @param {boolean} [opts.commit] - The reader has FINISHED with this control
   *   (blur or Enter on a number input), so a normalized window may be written
   *   back into the two precision boxes. Deliberately not done on every
   *   debounced keystroke: a bound half-way to "95" reads as "9", which
   *   normalizes to the domain edge or to null, and rewriting the box would
   *   wipe the digit that was about to follow.
   */
  function handleControlChange(target, { commit = false } = {}) {
    if (target.dataset?.column) {
      state.cols = [...rootEl.querySelectorAll("[data-column]")]
        .filter((b) => b.checked)
        .map((b) => b.dataset.column);
      applyColumns();
      updateUrl();
      return;
    }
    const key = target.dataset?.filter;
    if (!key) return;
    const filter = filters.find((f) => f.key === key);
    if (isRangeType(filter)) {
      const [minEl, maxEl] = rootEl.querySelectorAll(`[data-filter="${key}"]`);
      const min = Number.parseFloat(minEl.value);
      const max = Number.parseFloat(maxEl.value);
      state.values[key] = {
        min: Number.isFinite(min) ? min : null,
        max: Number.isFinite(max) ? max : null,
      };
      // A typed bound has to move the handles too, or the slider would keep
      // showing the previous window while the table showed a different one.
      // The component's normalized value is then read BACK as the state, so
      // there is one answer rather than two: a bound at the domain edge is not
      // a filter (and must not litter the URL), and a bound outside it is
      // clamped to the edge — same rows either way, since nothing lies beyond.
      const entry = histograms.get(key);
      if (entry) {
        entry.slider.setValue(state.values[key]);
        state.values[key] = entry.slider.getValue();
        // The fourth path (issue #250 review). normalizeSliderRange swaps
        // crossed handles, nulls a bound sitting at a domain edge and clamps
        // one beyond it — so without this the boxes can end up reading 90 and
        // 10 while the table, the thumbs and the URL all say 10-90. Same
        // two-halves-disagree shape the single writer was introduced for.
        if (commit) writeRangeInputs(key, state.values[key]);
      }
    } else if (filter.type === "boolean") {
      state.values[key] = target.checked;
    } else {
      state.values[key] = target.value;
    }
    apply();
  }

  // Selects and checkboxes settle on "change"; a number input does not fire it
  // until blur, which would leave a typed range bound doing nothing until the
  // reader clicked elsewhere. Range bounds therefore apply on "input" instead,
  // and the later "change" for the same element is skipped rather than
  // re-running the same filter pass.
  rootEl.addEventListener("change", (event) => {
    if (event.target.dataset?.bound) {
      // A number input fires "change" on blur or Enter — the moment the reader
      // is DONE typing, and the only safe moment to write a normalized window
      // back into the boxes. The pending debounce is dropped rather than left
      // to re-run the same filter pass a beat later.
      clearTimeout(rangeTimer);
      handleControlChange(event.target, { commit: true });
      return;
    }
    handleControlChange(event.target);
  });
  let rangeTimer = null;
  rootEl.addEventListener("input", (event) => {
    if (!event.target.dataset?.bound) return;
    // Debounced for the same reason the search box is: a range bound applies
    // on every keystroke (see above), and on driving.html that is up to ~3,800
    // rows re-filtered and re-rendered per digit typed. handleControlChange
    // re-reads the input's live value when the timer fires, so only the
    // settled figure is ever applied.
    clearTimeout(rangeTimer);
    rangeTimer = setTimeout(() => handleControlChange(event.target), 150);
  });

  rootEl.querySelector(".col-reset").addEventListener("click", () => {
    state.cols = null;
    applyColumns();
    syncControlsToState();
    updateUrl();
  });

  rootEl.querySelector(".controls-clear").addEventListener("click", () => {
    state.query = "";
    state.values = defaultFilterValues(filters);
    state.preset = defaultPreset;
    state.cols = null;
    applyColumns();
    syncControlsToState();
    apply();
  });

  // A header click re-sorts inside the table controller, which knows nothing
  // about the URL — so listen on the same thead and follow up. The histograms
  // are deliberately NOT redrawn here: which column is sorted does not change
  // which rows are selected, and a picture that repainted on a sort would be
  // claiming otherwise.
  table.onSortChange?.(() => {
    updateUrl();
  });

  return {
    /**
     * Seed or replace the full row set (called once the data has loaded).
     *
     * Idempotent in the way the name implies: calling it again with a
     * different row set re-derives the filter axes from those rows rather
     * than keeping the first set's.
     */
    setRows(rows) {
      allRows = rows;
      const parsed = parseTableState(
        typeof location !== "undefined" ? location.search : "",
        { filters }
      );
      state.query = parsed.query;
      state.preset = presets.some((p) => p.id === parsed.preset) ? parsed.preset : defaultPreset;
      state.cols = parsed.cols;
      state.values = parsed.values;
      // Not a scope CHANGE: the field and its window arrived together from the
      // URL, so clearing here would discard a shared link's own filter. The
      // axes DO re-seed though — these are new rows, and an axis is derived
      // from them — while the window that came with the URL is kept and
      // re-normalized against whatever domain the new rows produce.
      refreshResolved({ clearOnScopeChange: false, reseedDomains: true });
      applyColumns();
      if (parsed.sort) table.setSortTo(parsed.sort.key, parsed.sort.dir);
      syncControlsToState();
      apply();
    },
    getFilteredRows: () => filtered,
  };
}

// ── Sidebar disclosure (issue #250) ───────────────────────────────────────

/**
 * Keep a collapsible sidebar from becoming unreachable.
 *
 * The filter sidebar is a native `<details>`, which is what gives it keyboard
 * and AT semantics for nothing. Below the breakpoint its `<summary>` is the
 * "Filters" toggle; above it the summary is hidden by CSS and the panel is
 * simply always there. That leaves one bad state: collapse it on a narrow
 * screen, then widen (rotate a tablet, drag a window) and the panel is closed
 * with its only toggle now display:none — filters that exist, are in the URL,
 * and cannot be seen or changed. Widening therefore re-opens it.
 *
 * Deliberately one-way: narrowing does NOT collapse a panel the reader opened.
 *
 * @param {{open: boolean}} detailsEl - The `<details>` element.
 * @param {boolean} isWide - Is the viewport past the breakpoint?
 * @returns {boolean} The resulting open state.
 */
function syncSidebarDisclosure(detailsEl, isWide) {
  if (isWide && !detailsEl.open) detailsEl.open = true;
  return detailsEl.open;
}

/**
 * Wire syncSidebarDisclosure to a media query. A no-op before the sidebar is
 * mounted, on a page that has none, and in Node — so it is safe to call
 * unconditionally. createTableControls calls it once mountSidebar has built
 * the disclosure; there is no DOMContentLoaded hook, because at that point the
 * data has not loaded and the sidebar does not exist yet.
 *
 * @param {Document|Element} [root]
 * @param {string} [query] - Must mirror the `max-width: 900px` breakpoint in
 *   data-table.css; the two are one decision expressed twice.
 * @returns {?Object} The media-query list, for tests.
 */
function wireSidebarDisclosure(root, query = "(min-width: 901px)") {
  const scope = root ?? (typeof document !== "undefined" ? document : null);
  if (!scope || typeof window === "undefined" || !window.matchMedia) return null;
  const detailsEl = scope.querySelector(".sidebar-disclosure");
  if (!detailsEl) return null;
  const mq = window.matchMedia(query);
  const apply = () => syncSidebarDisclosure(detailsEl, mq.matches);
  apply();
  mq.addEventListener("change", apply);
  return mq;
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    foldForSearch,
    foldSearchTerms,
    matchesSearch,
    matchesSearchTerms,
    isRangeType,
    isFilterUnset,
    rowPassesFilter,
    applyFilters,
    rowsExceptFilter,
    resolveVisibleColumns,
    resolveFilters,
    defaultFilterValues,
    parseTableState,
    serializeTableState,
    histogramBuckets,
    controlsHtml,
    createTableControls,
    syncSidebarDisclosure,
    wireSidebarDisclosure,
  };
}
