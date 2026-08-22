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
 * Is this a numeric-window filter?
 *
 * `range` (two number inputs) and `histogram-range` (issue #250: a mini
 * histogram plus a dual-handle slider, WITH the same two number inputs kept
 * for precision and keyboard/AT parity) differ only in what they render. The
 * value shape is identical — `{min, max}`, either nullable — and so is the URL
 * wire format, `"min~max"`. Every place that reasons about the VALUE therefore
 * has to accept both, which is what this predicate is for: a missed site would
 * make a histogram filter silently unserializable or un-parseable rather than
 * failing loudly.
 *
 * @param {Object} filter - Filter descriptor.
 * @returns {boolean}
 */
function isRangeType(filter) {
  return filter.type === "range" || filter.type === "histogram-range";
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
 * keeps driving.html and the plain `range` filters unaware of any of this. The
 * result is a SHALLOW COPY, so the originals stay the page's static
 * descriptors and nothing accumulates state across renders.
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
 * @param {?{min: number, max: number}} [domain] - Fixed axis (issue #250). By
 *   default the extent is taken from `values`, which is right for the
 *   distribution strip (it describes the rows in view) and WRONG for a
 *   histogram-slider, whose axis must stay put while the reader brushes it:
 *   a self-scaling axis would move the handles' meaning out from under them.
 *   Values outside the domain are clamped into the end buckets rather than
 *   dropped, so a stale domain under-draws rather than losing rows.
 * @returns {{buckets: {from: number, to: number, count: number}[],
 *            min: number, max: number, count: number}|null}
 *   Null when nothing is measurable.
 */
function histogramBuckets(values, bucketCount, domain = null) {
  const nums = values.filter((v) => typeof v === "number" && Number.isFinite(v));
  if (nums.length === 0) return null;
  bucketCount = bucketCount ?? bucketCountFor(nums.length);
  const min = domain ? domain.min : Math.min(...nums);
  const max = domain ? domain.max : Math.max(...nums);
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
 * Two section orders, picked by `layout`:
 *
 *  * `"inline"` (default) — search, then every filter in descriptor order,
 *    then the column controls, then Clear all. This is the horizontal strip
 *    driving.html has always rendered, and it is emitted byte-identically.
 *  * `"sidebar"` — search, selects, the column controls, numeric windows, then
 *    booleans, then Clear all (issue #250). In a 280px column the reading
 *    order IS the layout, so the cheap categorical narrowings come first and
 *    the tall histogram brushes sit below the column controls rather than
 *    pushing them off the bottom.
 *
 * The sidebar order partitions by filter TYPE, so it also carries an
 * "everything else" bucket: a filter type added later must still be rendered
 * somewhere rather than silently vanishing from one of the two layouts.
 *
 * @param {Object} cfg - {filters, presets, columns, searchPlaceholder,
 *   showDistributionStrip, layout}.
 * @returns {string} HTML.
 */
function controlsHtml({
  filters,
  presets,
  columns,
  searchPlaceholder,
  showDistributionStrip = true,
  layout = "inline",
}) {
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
      // The two numeric-window flavours share their number inputs verbatim —
      // same `data-filter`/`data-bound` hooks, same aria-labels — because
      // those are what syncControlsToState, handleControlChange and the e2e
      // selectors read. `histogram-range` only ADDS the bars + brush above
      // them (issue #250); the precision path is unchanged, which is also what
      // keeps the control fully usable by keyboard and by AT.
      const boundInputs = () => `<input type="number" data-filter="${filter.key}" data-bound="min"
                   aria-label="Minimum ${filter.label}" placeholder="min"
                   ${filter.min != null ? `min="${filter.min}"` : ""}
                   ${filter.max != null ? `max="${filter.max}"` : ""}>
            <span class="control-dash" aria-hidden="true">–</span>
            <input type="number" data-filter="${filter.key}" data-bound="max"
                   aria-label="Maximum ${filter.label}" placeholder="max"
                   ${filter.min != null ? `min="${filter.min}"` : ""}
                   ${filter.max != null ? `max="${filter.max}"` : ""}>`;
      if (filter.type === "range") {
        return `
          <div class="control control-range" role="group" aria-labelledby="f-${filter.key}-legend">
            <span class="control-legend" id="f-${filter.key}-legend">${filter.label}</span>
            ${boundInputs()}
          </div>`;
      }
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

  let body;
  if (layout === "sidebar") {
    // Partition, don't hand-list: `rest` catches any filter type not named
    // here, so a type added later renders in the wrong PLACE rather than not
    // at all. The order is search -> selects -> columns -> numeric windows ->
    // booleans -> clear.
    const selects = filters.filter((f) => f.type === "select");
    const ranges = filters.filter(isRangeType);
    const booleans = filters.filter((f) => f.type === "boolean");
    const placed = new Set([...selects, ...ranges, ...booleans]);
    const rest = filters.filter((f) => !placed.has(f));
    const render = (list) => list.map(renderFilter).join("");
    body =
      searchControl +
      render(selects) +
      columnControls +
      render(ranges) +
      render(booleans) +
      render(rest) +
      clearControl;
  } else {
    // The literal indent the old `${filterControls}` interpolation sat on. It
    // is here so this branch stays BYTE-identical to the pre-#250 markup —
    // driving.html renders through it, and a test compares the two strings
    // rather than trusting the eye.
    body =
      searchControl + "\n      " + filters.map(renderFilter).join("") + columnControls + clearControl;
  }

  return `
    <div class="table-controls">${body}
    </div>
    ${showDistributionStrip ? `<div class="distribution-strip" id="distribution-strip"></div>` : ""}`;
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
 * @param {Function} [cfg.onChange] - Called with the filtered rows, every row,
 *   and a snapshot of the control state, after every change — for the page's
 *   caption/result count. The third argument is what lets a caption name a
 *   filter's active value (streets.html's network) without reaching into the
 *   DOM for it.
 * @param {"inline"|"sidebar"} [cfg.layout="inline"] - Control section order;
 *   see controlsHtml. The pivoted pages pass "sidebar"; driving.js does not,
 *   and its markup is unchanged.
 * @param {boolean} [cfg.showDistributionStrip=true] - Render the sorted-column
 *   distribution strip. The pivoted pages (issue #250) turn it off: their
 *   numeric filters are histogram-sliders, which draw a PER-FILTER histogram
 *   on a fixed axis, so a second histogram of whichever column happens to be
 *   sorted — one that silently swapped its metric on every header click — is
 *   two conflicting answers to one question. driving.html keeps it.
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

function createTableControls({
  rootEl,
  table,
  columns,
  presets,
  filters,
  searchFields,
  searchPlaceholder,
  onChange,
  showDistributionStrip = true,
  layout = "inline",
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
    values: defaultFilterValues(filters),
  };

  rootEl.innerHTML = controlsHtml({
    filters,
    presets,
    columns,
    searchPlaceholder,
    showDistributionStrip,
    layout,
  });
  const searchEl = rootEl.querySelector("#table-search");
  const presetEl = rootEl.querySelector("#table-preset");
  // Null when the page opted out — every strip site below guards on the
  // element rather than re-reading the flag, so there is one condition to get
  // right instead of five.
  const stripEl = rootEl.querySelector("#distribution-strip");

  // ── Histogram-sliders (issue #250) ──
  // One per `histogram-range` filter, each with a FIXED axis seeded once from
  // the full row set. The domain lives here rather than inside the component
  // because repaintHistograms has to hand the same domain to histogramBuckets;
  // keeping one copy is what stops the bars and the handles from ever
  // describing different scales.
  // The filters as they read RIGHT NOW: a scoped descriptor's field, label and
  // test all depend on another control's value, so everything that reasons
  // about a filter reads this rather than the static `filters`. Re-derived
  // whenever the values change (see refreshResolved).
  let resolved = resolveFilters(filters, state.values);
  const filterFor = (key) => resolved.find((f) => f.key === key);

  /**
   * Write a numeric window into its two precision inputs.
   *
   * THE single writer, because there are three paths that change a window — a
   * typed bound, a dragged handle, and a scope change clearing it — and every
   * one of them has to leave the inputs agreeing with `state.values`. The
   * scope-clear path is the one that got this wrong: the filter was genuinely
   * cleared and the table re-filtered, while the min box went on reading "80".
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
        // pointer move, and on grid.html that is 1,501 rows re-filtered and
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
   */
  function syncHistogramDomains(clearOnChange = true) {
    for (const entry of histograms.values()) {
      const filter = filterFor(entry.key);
      if (entry.field === filter.field) continue;
      const changed = entry.field !== null;
      entry.field = filter.field;
      const values = allRows
        .map((row) => row[filter.field])
        .filter((v) => typeof v === "number" && Number.isFinite(v));
      let min = values.length ? Math.min(...values) : (filter.min ?? 0);
      let max = values.length ? Math.max(...values) : (filter.max ?? 1);
      if (filter.min != null) min = Math.max(min, filter.min);
      if (filter.max != null) max = Math.min(max, filter.max);
      entry.domain = { min, max: max > min ? max : min + 1 };
      if (changed && clearOnChange) {
        delete state.values[entry.key];
        writeRangeInputs(entry.key, null);
      }
      entry.slider.setDomain(entry.domain);
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
   * histogram — three passes over ~1,500 rows on the widest page.
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
  function refreshResolved({ clearOnScopeChange = true } = {}) {
    resolved = resolveFilters(filters, state.values);
    syncHistogramDomains(clearOnScopeChange);
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
    // Adopt WITHOUT reporting: this runs when the URL, "Clear all" or a strip
    // click is the source of the value, and echoing it back through onInput
    // would be circular.
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

  /** The range filter a click on the strip's bars would set, if any. */
  function rangeFilterFor(column) {
    return filters.find((f) => isRangeType(f) && f.field === column.key);
  }

  function repaintStrip() {
    if (!stripEl) return;
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
    repaintStrip();
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
  stripEl?.addEventListener("click", (event) => {
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
    state.values = defaultFilterValues(filters);
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
      // Not a scope CHANGE: the field and its window arrived together from the
      // URL, so clearing here would discard a shared link's own filter.
      refreshResolved({ clearOnScopeChange: false });
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
 * Wire syncSidebarDisclosure to a media query. A no-op on a page with no
 * sidebar (driving.html) and in Node, so it is safe to call unconditionally.
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

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => wireSidebarDisclosure());
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    foldForSearch,
    matchesSearch,
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
    bucketCountFor,
    histogramBuckets,
    medianOf,
    formatStripSummary,
    renderDistributionStrip,
    controlsHtml,
    createTableControls,
    syncSidebarDisclosure,
    wireSidebarDisclosure,
  };
}
