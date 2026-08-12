/**
 * table-controls.js — the exploration chassis for the data-table pages
 * (grid.html and streets.html), issue #188.
 *
 * The tables outgrew "sortable list": grid.html renders 1,501 rows and
 * streets.html 283, and the questions being asked of them are comparative
 * ("where does Mapillary beat GSV?", "which cities have imagery where people
 * actually walk?") rather than lookup-by-name. This module adds the machinery
 * that makes a large table explorable — text search, structured filters,
 * column presets, and a distribution strip for the sorted column — and keeps
 * the whole view in the URL so a finding can be linked to.
 *
 * Deliberately NOT an export tool. `cities.json.gz` and `streetwalks.json.gz`
 * are already published as public gzipped JSON at fixed URLs, so a CSV of the
 * on-screen view would be a lossier copy of a path that already exists; real
 * analysis pulls from the catalog instead.
 *
 * Page-agnostic: everything specific to a page arrives as descriptors
 * (`columns`, `presets`, `filters`) from grid.js / streets.js. The pure
 * functions here are exported for the offline unit tests; only
 * `createTableControls` touches the DOM.
 *
 * Depends on globals from table-utils.js (loaded first): formatCellNumber.
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
  const terms = foldForSearch(query).split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const haystack = fields.map((f) => foldForSearch(row[f])).join(" ");
  return terms.every((term) => haystack.includes(term));
}

// ── Structured filters ────────────────────────────────────────

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
  if (filter.type === "range") return value.min == null && value.max == null;
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
  if (filter.type === "range") {
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
 * @param {Object[]} rows - All row models.
 * @param {Object} cfg
 * @param {Object[]} cfg.filters - Filter descriptors.
 * @param {Object} cfg.values - {filterKey: value}.
 * @param {string} cfg.query - Free-text query.
 * @param {string[]} cfg.searchFields - Row fields the query searches.
 * @returns {Object[]} A new filtered array (input untouched).
 */
function applyFilters(rows, { filters, values, query, searchFields }) {
  return rows.filter(
    (row) =>
      matchesSearch(row, searchFields, query ?? "") &&
      filters.every((filter) => rowPassesFilter(filter, row, values[filter.key]))
  );
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
    if (raw == null || raw === "") continue;
    if (filter.type === "range") {
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
    if (filter.type === "range") {
      params.set(filter.key, `${value.min ?? ""}~${value.max ?? ""}`);
    } else if (filter.type === "boolean") {
      params.set(filter.key, "1");
    } else {
      params.set(filter.key, value);
    }
  }
  return params.toString();
}

// ── Distribution strip ────────────────────────────────────────

/**
 * How many buckets to cut a set of N values into.
 *
 * The square-root rule, clamped. A fixed bucket count is wrong at both ends of
 * this table's range: at N=2 (a heavily filtered view) 24 buckets strand two
 * lone bars at opposite edges of an otherwise empty strip, and past ~600 the
 * bars are thinner than the gaps between them. Production sits at 283 walks
 * and 1,501 grid series, both of which land on the 24 cap.
 *
 * @param {number} n - Count of measurable values.
 * @returns {number}
 */
function bucketCountFor(n) {
  return Math.min(24, Math.max(1, Math.ceil(Math.sqrt(n))));
}

/**
 * Bucket numeric values into a histogram.
 *
 * Nulls are dropped rather than bucketed as zero (see rowPassesFilter). A
 * single distinct value yields one full-width bucket instead of a degenerate
 * zero-width range.
 *
 * @param {Array<?number>} values
 * @param {number} [bucketCount] - Defaults to bucketCountFor(N).
 * @returns {{buckets: {from: number, to: number, count: number}[],
 *            min: number, max: number, count: number}|null}
 *   Null when nothing is measurable.
 */
function histogramBuckets(values, bucketCount) {
  const nums = values.filter((v) => typeof v === "number" && Number.isFinite(v));
  if (nums.length === 0) return null;
  bucketCount = bucketCount ?? bucketCountFor(nums.length);
  const min = Math.min(...nums);
  const max = Math.max(...nums);
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
    // The maximum lands in the last bucket rather than one past the end.
    const index = Math.min(bucketCount - 1, Math.floor((value - min) / width));
    buckets[index].count += 1;
  }
  return { buckets, min, max, count: nums.length };
}

/** Median of a numeric array (the strip's text summary). */
function medianOf(values) {
  const nums = values
    .filter((v) => typeof v === "number" && Number.isFinite(v))
    .sort((a, b) => a - b);
  if (nums.length === 0) return null;
  const mid = Math.floor(nums.length / 2);
  return nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2;
}

/**
 * Text equivalent of the distribution strip.
 *
 * The bars are decorative (`aria-hidden`); this sentence carries the same
 * information for a screen reader and doubles as the visible caption.
 *
 * @param {Object} column - The active sort column descriptor.
 * @param {Array<?number>} values
 * @returns {string}
 */
function formatStripSummary(column, values) {
  const stats = histogramBuckets(values);
  if (!stats) return `No ${column.label.toLowerCase()} values in the current view.`;
  const unit = column.unit ?? "";
  const fmt = (v) => `${formatCellNumber(v, column.digits ?? 1)}${unit}`;
  return (
    `${column.label} across ${formatCellNumber(stats.count)} rows: ` +
    `min ${fmt(stats.min)}, median ${fmt(medianOf(values))}, max ${fmt(stats.max)}.`
  );
}

// ── DOM wiring ────────────────────────────────────────────────

/**
 * Render the distribution strip into a container.
 *
 * A single-series magnitude chart, so: one flat hue for every bar (bar colour
 * carries no second meaning), no legend, no axis furniture. The bars are
 * deliberately NOT painted with `coverageColor` — these encode row counts, and
 * reusing the coverage ramp here would imply the height meant coverage.
 *
 * When the active sort column has a matching range filter, each bucket
 * becomes a real `<button>` carrying its bounds in `data-from`/`data-to` —
 * `createTableControls` delegates a click listener to the container (bars are
 * rebuilt on every repaint, so a per-bar listener would need re-binding every
 * time; delegation on the container survives the innerHTML replacement, the
 * same reason the header's sort click is delegated to the `<thead>`). Without
 * a matching filter the bars stay plain, `aria-hidden` `<span>`s exactly as
 * before — there is nothing a click on them could narrow.
 *
 * @param {Element} el - Container.
 * @param {Object} column - Active sort column descriptor.
 * @param {Array<?number>} values
 * @param {boolean} [clickable] - Whether the active sort column has a range
 *   filter a bar click can set.
 */
function renderDistributionStrip(el, column, values, clickable = false) {
  const stats = histogramBuckets(values);
  const summary = formatStripSummary(column, values);
  if (!stats || column.type !== "number") {
    el.innerHTML = `<p class="strip-summary">${
      column.type === "number" ? summary : "Sort by a numeric column to see its distribution."
    }</p>`;
    return;
  }
  const tallest = Math.max(...stats.buckets.map((b) => b.count));
  const unit = column.unit ?? "";
  const digits = column.digits ?? 1;
  // Bucket bounds are rounded to the column's own display precision before
  // being used anywhere — as the label text AND as the value a click writes
  // into the filter — so a bar's tooltip and the range it actually selects
  // always agree (rather than a label reading "58.7%" while the click quietly
  // filters to the unrounded 58.6987…%).
  const roundToDigits = (v) => Math.round(v * 10 ** digits) / 10 ** digits;
  const bars = stats.buckets
    .map((bucket) => {
      const height = tallest === 0 ? 0 : Math.round((bucket.count / tallest) * 100);
      const from = roundToDigits(bucket.from);
      const to = roundToDigits(bucket.to);
      const label =
        `${formatCellNumber(from, digits)}${unit}–${formatCellNumber(to, digits)}${unit}: ` +
        `${formatCellNumber(bucket.count)} row${bucket.count === 1 ? "" : "s"}` +
        (clickable ? " — click to filter to this range" : "");
      // min-height keeps a non-empty bucket visible instead of rounding it away.
      const heightPct = bucket.count > 0 ? Math.max(height, 2) : 0;
      return clickable
        ? `<button type="button" class="strip-bar" data-from="${from}" data-to="${to}"
                   title="${label}" aria-label="${label}" style="height:${heightPct}%"></button>`
        : `<span class="strip-bar" title="${label}" style="height:${heightPct}%"></span>`;
    })
    .join("");
  el.innerHTML =
    `<div class="strip-bars"${clickable ? "" : ' aria-hidden="true"'}>${bars}</div>` +
    `<p class="strip-summary">${summary}</p>`;
}

/**
 * Build the controls markup for a page.
 *
 * Real labeled form controls throughout: the search box is a `<label>`ed
 * `<input type="search">`, filters are `<select>`/number inputs/checkboxes,
 * and the column picker is a `<details>` disclosure wrapping a checkbox
 * `<fieldset>` — so the whole region is keyboard-reachable and announced
 * without any ARIA of its own.
 *
 * @param {Object} cfg - {filters, presets, columns}.
 * @returns {string} HTML.
 */
function controlsHtml({ filters, presets, columns }) {
  const filterControls = filters
    .map((filter) => {
      if (filter.type === "select") {
        const options = [`<option value="">${filter.anyLabel ?? "All"}</option>`]
          .concat(filter.options.map((o) => `<option value="${o.value}">${o.label}</option>`))
          .join("");
        return `
          <div class="control">
            <label for="f-${filter.key}">${filter.label}</label>
            <select id="f-${filter.key}" data-filter="${filter.key}">${options}</select>
          </div>`;
      }
      if (filter.type === "range") {
        return `
          <div class="control control-range" role="group" aria-labelledby="f-${filter.key}-legend">
            <span class="control-legend" id="f-${filter.key}-legend">${filter.label}</span>
            <input type="number" data-filter="${filter.key}" data-bound="min"
                   aria-label="Minimum ${filter.label}" placeholder="min"
                   ${filter.min != null ? `min="${filter.min}"` : ""}
                   ${filter.max != null ? `max="${filter.max}"` : ""}>
            <span class="control-dash" aria-hidden="true">–</span>
            <input type="number" data-filter="${filter.key}" data-bound="max"
                   aria-label="Maximum ${filter.label}" placeholder="max"
                   ${filter.min != null ? `min="${filter.min}"` : ""}
                   ${filter.max != null ? `max="${filter.max}"` : ""}>
          </div>`;
      }
      return `
        <div class="control control-check">
          <input type="checkbox" id="f-${filter.key}" data-filter="${filter.key}">
          <label for="f-${filter.key}"${
            filter.title ? ` title="${filter.title}"` : ""
          }>${filter.label}</label>
        </div>`;
    })
    .join("");

  const presetOptions = presets
    .map((p) => `<option value="${p.id}"${p.title ? ` title="${p.title}"` : ""}>${p.label}</option>`)
    .join("");

  const pickerBoxes = columns
    .filter((c) => c.always !== true)
    .map(
      (c) => `
        <label class="col-toggle">
          <input type="checkbox" data-column="${c.key}"> ${c.label}
        </label>`
    )
    .join("");

  return `
    <div class="table-controls">
      <div class="control control-search">
        <label for="table-search">Search</label>
        <input type="search" id="table-search" placeholder="City, provider…"
               autocomplete="off" spellcheck="false">
      </div>
      ${filterControls}
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
      </details>
      <button type="button" class="controls-clear">Clear all</button>
    </div>
    <div class="distribution-strip" id="distribution-strip"></div>`;
}

/**
 * Wire the controls region to a sortable table.
 *
 * Owns the filter/search/preset state, pushes the narrowed rows into the
 * table, keeps the URL in step, and repaints the distribution strip whenever
 * the active sort column or the filtered set changes.
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
 * @param {Function} [cfg.onChange] - Called with the filtered rows after every
 *   change, for the page's caption/result count.
 * @returns {{setRows: Function, getFilteredRows: Function}}
 */
function createTableControls({
  rootEl,
  table,
  columns,
  presets,
  filters,
  searchFields,
  onChange,
}) {
  const defaultPreset = presets[0].id;
  let allRows = [];
  let filtered = [];
  // No `sort` field here: the sort is owned entirely by `table` (the
  // createSortableTable controller), read via `table.getSort()` wherever it's
  // needed (updateUrl, repaintStrip). Duplicating it into this state object
  // would just be a second, driftable copy of the same fact.
  const state = {
    query: "",
    preset: defaultPreset,
    cols: null,
    values: {},
  };

  rootEl.innerHTML = controlsHtml({ filters, presets, columns });
  const searchEl = rootEl.querySelector("#table-search");
  const presetEl = rootEl.querySelector("#table-preset");
  const stripEl = rootEl.querySelector("#distribution-strip");

  // ── state → DOM ──
  function syncControlsToState() {
    searchEl.value = state.query;
    presetEl.value = state.preset;
    for (const filter of filters) {
      const value = state.values[filter.key];
      if (filter.type === "range") {
        const [minEl, maxEl] = rootEl.querySelectorAll(`[data-filter="${filter.key}"]`);
        minEl.value = value?.min ?? "";
        maxEl.value = value?.max ?? "";
      } else {
        const el = rootEl.querySelector(`[data-filter="${filter.key}"]`);
        if (filter.type === "boolean") el.checked = value === true;
        else el.value = value ?? "";
      }
    }
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

  /** The range filter a click on the strip's bars would set, if any. */
  function rangeFilterFor(column) {
    return filters.find((f) => f.type === "range" && f.field === column.key);
  }

  function repaintStrip() {
    const sort = table.getSort();
    const column = columns.find((c) => c.key === sort.key);
    if (column) {
      renderDistributionStrip(
        stripEl,
        column,
        filtered.map((r) => r[column.key]),
        Boolean(rangeFilterFor(column))
      );
    }
  }

  /** Re-filter, repaint the table, the strip, and the URL. */
  function apply() {
    filtered = applyFilters(allRows, {
      filters,
      values: state.values,
      query: state.query,
      searchFields,
    });
    table.setRows(filtered);
    repaintStrip();
    updateUrl();
    onChange?.(filtered, allRows);
  }

  function applyColumns() {
    table.setColumns(resolveVisibleColumns(columns, presets, state.preset, state.cols));
  }

  // ── DOM → state ──
  let searchTimer = null;
  searchEl.addEventListener("input", () => {
    // Debounced: 1,501 rows re-filter and re-render on every keystroke.
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
    repaintStrip();
    updateUrl();
  });

  /** Fold one control's current DOM value into `state` and repaint. */
  function handleControlChange(target) {
    if (target.dataset?.column) {
      state.cols = [...rootEl.querySelectorAll("[data-column]")]
        .filter((b) => b.checked)
        .map((b) => b.dataset.column);
      applyColumns();
      repaintStrip();
      updateUrl();
      return;
    }
    const key = target.dataset?.filter;
    if (!key) return;
    const filter = filters.find((f) => f.key === key);
    if (filter.type === "range") {
      const [minEl, maxEl] = rootEl.querySelectorAll(`[data-filter="${key}"]`);
      const min = Number.parseFloat(minEl.value);
      const max = Number.parseFloat(maxEl.value);
      state.values[key] = {
        min: Number.isFinite(min) ? min : null,
        max: Number.isFinite(max) ? max : null,
      };
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
    if (event.target.dataset?.bound) return;
    handleControlChange(event.target);
  });
  let rangeTimer = null;
  rootEl.addEventListener("input", (event) => {
    if (!event.target.dataset?.bound) return;
    // Debounced for the same reason the search box is: a range bound applies
    // on every keystroke (see above), and on grid.html that is up to 1,501
    // rows re-filtered and re-rendered per digit typed. handleControlChange
    // re-reads the input's live value when the timer fires, so only the
    // settled figure is ever applied.
    clearTimeout(rangeTimer);
    rangeTimer = setTimeout(() => handleControlChange(event.target), 150);
  });

  // Distribution strip: a bar click sets the range filter matching the
  // ACTIVE SORT COLUMN to that bucket's bounds (renderDistributionStrip only
  // emits <button>s, rather than the plain aria-hidden <span>s, when such a
  // filter exists — so a stray click elsewhere in the strip's whitespace is
  // simply not on a `.strip-bar[data-from]` and no-ops here). Delegated on
  // the container rather than bound per-bar: repaintStrip replaces the strip's
  // innerHTML on every sort/filter change, which would silently drop a
  // per-button listener the same way an un-delegated header click would (see
  // createSortableTable's own listener for that exact regression).
  stripEl.addEventListener("click", (event) => {
    const bar = event.target.closest?.(".strip-bar[data-from]");
    if (!bar) return;
    const sort = table.getSort();
    const column = columns.find((c) => c.key === sort.key);
    const filter = column && rangeFilterFor(column);
    if (!filter) return;
    state.values[filter.key] = {
      min: Number.parseFloat(bar.dataset.from),
      max: Number.parseFloat(bar.dataset.to),
    };
    syncControlsToState();
    apply();
  });

  rootEl.querySelector(".col-reset").addEventListener("click", () => {
    state.cols = null;
    applyColumns();
    syncControlsToState();
    repaintStrip();
    updateUrl();
  });

  rootEl.querySelector(".controls-clear").addEventListener("click", () => {
    state.query = "";
    state.values = {};
    state.preset = defaultPreset;
    state.cols = null;
    applyColumns();
    syncControlsToState();
    apply();
  });

  // A header click re-sorts inside the table controller, which knows nothing
  // about the URL or the strip — so listen on the same thead and follow up.
  table.onSortChange?.(() => {
    repaintStrip();
    updateUrl();
  });

  return {
    /** Seed or replace the full row set (called once the data has loaded). */
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
      applyColumns();
      if (parsed.sort) table.setSortTo(parsed.sort.key, parsed.sort.dir);
      syncControlsToState();
      apply();
    },
    getFilteredRows: () => filtered,
  };
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    foldForSearch,
    matchesSearch,
    isFilterUnset,
    rowPassesFilter,
    applyFilters,
    resolveVisibleColumns,
    parseTableState,
    serializeTableState,
    bucketCountFor,
    histogramBuckets,
    medianOf,
    formatStripSummary,
    renderDistributionStrip,
    controlsHtml,
    createTableControls,
  };
}
