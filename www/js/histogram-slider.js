/**
 * histogram-slider.js — a mini histogram with a dual-handle range brush, used
 * as the numeric filter control on the pivoted data-table pages (issue #250).
 *
 * WHY this replaces the min/max number inputs as the primary control, and why
 * it replaces the distribution strip rather than joining it:
 *
 *  * A bare pair of number inputs asks the reader to guess a window before
 *    they know the shape of the data. The bars answer "where are the rows?"
 *    and the handles then act on that answer directly.
 *  * The strip it replaces on these pages visualized the ACTIVE SORT COLUMN
 *    over the FILTERED rows, which made it change its own meaning twice over:
 *    re-sorting silently swapped its metric, and clicking a bar filtered the
 *    rows the strip was drawn from, so the picture collapsed under the very
 *    interaction it invited. Here each filter owns one histogram, on one
 *    metric, drawn over the rows every OTHER control has selected
 *    (`rowsExceptFilter` in table-controls.js) and on a FIXED axis. Brushing
 *    shrinks the bars; it never moves the axis out from under the handles.
 *
 * The number inputs stay, to the right of the slider: they are the precision
 * path ("exactly 50%", not "wherever the thumb landed"), they carry the
 * `data-filter`/`data-bound` hooks the rest of the chassis already reads, and
 * they remain the fully labeled form controls. The two <input type="range">
 * handles deliberately carry NO `data-filter` — `syncControlsToState` and
 * `handleControlChange` locate a range filter's bounds with
 * `querySelectorAll('[data-filter=KEY]')` and expect exactly two elements
 * back.
 *
 * The interaction mechanics (two native range inputs overlaid on one track,
 * thumbs clamped against each other, a draggable selection window, and the
 * z-index hack for two thumbs pinned at the top of the domain) are lifted from
 * the overview map's legend slider in index.js, generalized from integer
 * bucket indices to continuous data values. Native <input type="range"> is
 * what buys keyboard operation, focus handling and AT semantics for free.
 *
 * Depends on globals from table-utils.js (loaded first): formatCellNumber.
 */

/** How many bars a histogram-slider draws. Matches the strip's cap. */
const HISTOGRAM_SLIDER_BUCKETS = 24;

/**
 * A keyboard step for a continuous domain: roughly 100 arrow-key presses to
 * cross it, snapped to a 1/2/5 x power-of-ten ladder so the values a reader
 * lands on are readable ones (1, 5, 0.05) rather than 0.8374.
 *
 * NEVER returns 0, and never `step="any"`: a zero step makes the arrow keys
 * dead, and `"any"` makes every keypress move by 1/100 of the range in
 * un-rounded floating point, so the value in the URL becomes noise like
 * `18.442000000000004`.
 *
 * @param {number} min - Domain floor.
 * @param {number} max - Domain ceiling.
 * @returns {number} A positive step.
 */
function sliderStepFor(min, max) {
  const span = Math.abs(max - min);
  if (!Number.isFinite(span) || span <= 0) return 1;
  const raw = span / 100;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalized = raw / magnitude;
  const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return nice * magnitude;
}

/**
 * Put a proposed {min, max} into the canonical value shape.
 *
 * Three jobs, all of which the raw DOM can produce on its own:
 *   * a missing/unparseable bound means "open at that end" and becomes the
 *     domain edge;
 *   * crossed handles (min above max — reachable by typing into the number
 *     inputs) are swapped rather than yielding an empty table;
 *   * a bound at or beyond the domain edge is NOT a filter, so it round-trips
 *     as `null` — which is what keeps a full-span brush out of the URL and
 *     keeps `isFilterUnset` agreeing with what the reader sees.
 *
 * @param {{min: ?number, max: ?number}} value
 * @param {{min: number, max: number}} domain
 * @returns {{min: ?number, max: ?number}} Nulls mean "open at that end".
 */
function normalizeSliderRange(value, domain) {
  const fallback = (v, edge) => (typeof v === "number" && Number.isFinite(v) ? v : edge);
  let lo = fallback(value?.min, domain.min);
  let hi = fallback(value?.max, domain.max);
  if (lo > hi) [lo, hi] = [hi, lo];
  const clamp = (v) => Math.min(Math.max(v, domain.min), domain.max);
  lo = clamp(lo);
  hi = clamp(hi);
  return {
    min: lo <= domain.min ? null : lo,
    max: hi >= domain.max ? null : hi,
  };
}

/**
 * Which buckets does a selection touch? Used for dimming, so the rule is
 * OVERLAP rather than containment: a bucket straddling a handle still holds
 * rows that pass the filter, and dimming it would claim otherwise.
 *
 * An open end matches everything on that side.
 *
 * @param {{from: number, to: number}[]} buckets
 * @param {{min: ?number, max: ?number}} range
 * @returns {boolean[]} True where the bucket is inside the selection.
 */
function classifyBuckets(buckets, range) {
  const lo = range?.min ?? -Infinity;
  const hi = range?.max ?? Infinity;
  return buckets.map((bucket) => bucket.to >= lo && bucket.from <= hi);
}

/**
 * What a screen reader announces for a thumb. `aria-valuenow` would read the
 * bare number ("58.7"), which loses the unit that makes it a percentage, a
 * year count or a percentage-point difference.
 *
 * @param {number} value
 * @param {{unit?: string, digits?: number}} [filter] - Filter descriptor.
 * @returns {string}
 */
function sliderValuetext(value, filter = {}) {
  return `${formatCellNumber(value, filter.digits ?? 0)}${filter.unit ?? ""}`;
}

/**
 * Strip float noise from a value that came off a stepped range input.
 *
 * `min + k * step` accumulates error (0.30000000000000004), and these values
 * are written into the URL and into a number input, both of which a person
 * reads. Six decimals is well past any step this control produces and well
 * short of where the noise lives.
 *
 * @param {number} value
 * @returns {number}
 */
function roundSliderValue(value) {
  return Math.round(value * 1e6) / 1e6;
}

/**
 * Wire one histogram-slider onto the shell `controlsHtml` emitted for a
 * `histogram-range` filter.
 *
 * The component owns only its own presentation: bars, thumbs, fill, dimming
 * and `aria-valuetext`, all updated synchronously so dragging feels direct.
 * It never filters anything — it reports a data-space `{min, max}` through
 * `onInput` and the chassis debounces that into a re-filter.
 *
 * @param {Object} cfg
 * @param {Element} cfg.rootEl - The `.control-histogram` element.
 * @param {Object} cfg.filter - The filter descriptor (label/unit/digits).
 * @param {(range: {min: ?number, max: ?number}) => void} [cfg.onInput]
 * @returns {{setDomain: Function, setHistogram: Function, setValue: Function,
 *            getValue: Function, destroy: Function}}
 */
function createHistogramSlider({ rootEl, filter, onInput }) {
  const barsEl = rootEl.querySelector(".hist-bars");
  const sliderEl = rootEl.querySelector(".hist-slider");
  const fillEl = rootEl.querySelector(".hist-fill");
  const loEl = rootEl.querySelector(".hist-lo");
  const hiEl = rootEl.querySelector(".hist-hi");
  const abort = new AbortController();
  const on = (el, type, fn) => el.addEventListener(type, fn, { signal: abort.signal });

  // A one-wide domain until setDomain lands: the shell is built before the
  // data is, and a zero-width domain would divide by zero in paint().
  let domain = { min: 0, max: 1 };
  let step = 1;
  let value = { min: null, max: null };
  let buckets = [];

  /** The concrete window the handles show — an open end reads as the edge. */
  function effective() {
    return { min: value.min ?? domain.min, max: value.max ?? domain.max };
  }

  function paint() {
    const span = domain.max - domain.min;
    const { min, max } = effective();
    loEl.value = String(min);
    hiEl.value = String(max);
    loEl.setAttribute("aria-valuetext", sliderValuetext(min, filter));
    hiEl.setAttribute("aria-valuetext", sliderValuetext(max, filter));

    // `hi` paints over `lo` (later in the DOM). With both thumbs together at
    // the top of the domain only `lo` can still move, so raise it or the
    // control becomes unusable exactly when it is fully narrowed.
    loEl.style.zIndex = min === max && max === domain.max ? "1" : "";

    const loPct = ((min - domain.min) / span) * 100;
    const hiPct = ((max - domain.min) / span) * 100;
    fillEl.style.left = `${loPct}%`;
    fillEl.style.width = `${hiPct - loPct}%`;
    // The window is only draggable while it is actually a window; a full-span
    // fill has nowhere to slide, and the class carries the grab cursor.
    fillEl.classList.toggle("draggable", value.min != null || value.max != null);

    const inside = classifyBuckets(buckets, value);
    const bars = barsEl.children;
    for (let i = 0; i < bars.length; i += 1) {
      bars[i].classList.toggle("dimmed", inside[i] === false);
    }
  }

  /** Adopt a proposed window, repaint, and report it. */
  function commit(next) {
    value = normalizeSliderRange(next, domain);
    if (value.min != null) value.min = roundSliderValue(value.min);
    if (value.max != null) value.max = roundSliderValue(value.max);
    paint();
    onInput?.({ ...value });
  }

  // Each thumb clamps against the other, so min <= max always holds.
  on(loEl, "input", () => {
    const hi = Number.parseFloat(hiEl.value);
    commit({ min: Math.min(Number.parseFloat(loEl.value), hi), max: hi });
  });
  on(hiEl, "input", () => {
    const lo = Number.parseFloat(loEl.value);
    commit({ min: lo, max: Math.max(Number.parseFloat(hiEl.value), lo) });
  });

  // Dragging the selected window itself slides both bounds together, width
  // preserved. The thumbs' own native pointer handling is untouched — their
  // pointerdowns target the range INPUTs and are excluded here.
  let windowDrag = null;
  on(sliderEl, "pointerdown", (event) => {
    if (event.target.tagName === "INPUT") return;
    if (value.min == null && value.max == null) return;
    const rect = sliderEl.getBoundingClientRect();
    if (rect.width === 0) return;
    const span = domain.max - domain.min;
    const { min, max } = effective();
    const at = domain.min + ((event.clientX - rect.left) / rect.width) * span;
    // Only grabs inside the window (with half a step of slack) start a drag.
    if (at < min - step / 2 || at > max + step / 2) return;
    windowDrag = {
      startX: event.clientX,
      startMin: min,
      width: max - min,
      pxPerUnit: rect.width / span,
    };
    sliderEl.setPointerCapture(event.pointerId);
    sliderEl.classList.add("dragging");
    event.preventDefault();
  });
  on(sliderEl, "pointermove", (event) => {
    if (!windowDrag) return;
    const raw = (event.clientX - windowDrag.startX) / windowDrag.pxPerUnit;
    const delta = Math.round(raw / step) * step;
    const min = Math.max(
      domain.min,
      Math.min(windowDrag.startMin + delta, domain.max - windowDrag.width)
    );
    if (min !== effective().min) commit({ min, max: min + windowDrag.width });
  });
  const endDrag = () => {
    windowDrag = null;
    sliderEl.classList.remove("dragging");
  };
  on(sliderEl, "pointerup", endDrag);
  on(sliderEl, "pointercancel", endDrag);

  return {
    /**
     * Fix the axis. Called ONCE, from the rows — after that the bars shrink
     * under a brush but the scale never moves.
     *
     * Two adjustments to the raw extent, both load-bearing:
     *
     *  * A degenerate domain (every row at one value, or one row) is widened
     *    rather than left zero-width: two range inputs with min === max are
     *    inert, and the reader gets a control that cannot move and no
     *    explanation.
     *  * The ends are SNAPPED OUTWARD to whole steps. A max that is not a
     *    whole number of steps above min is unreachable — the browser snaps a
     *    range input's value down to the last valid one — so `hi` would rest
     *    just below the top of the data. Full span would then never read as
     *    "no filter", and, worse, the highest-valued rows would silently drop
     *    out of the table the moment the OTHER handle moved (a 0–85.1 axis at
     *    step 1 pins hi to 85, quietly excluding the 85.1% row). The axis
     *    therefore ends up at most one step past the data, which is under 1%
     *    of the span.
     */
    setDomain(next) {
      const rawMin = next.min;
      const rawMax = next.max > next.min ? next.max : next.min + 1;
      step = sliderStepFor(rawMin, rawMax);
      const min = roundSliderValue(Math.floor(rawMin / step) * step);
      const max = roundSliderValue(Math.ceil(rawMax / step) * step);
      domain = { min, max: max > min ? max : min + step };
      for (const el of [loEl, hiEl]) {
        el.min = String(domain.min);
        el.max = String(domain.max);
        el.step = String(step);
      }
      value = normalizeSliderRange(value, domain);
      paint();
    },

    /**
     * Draw the bars from a `histogramBuckets` result (null when nothing in the
     * current cross-selection is measurable — the bars go empty, the axis and
     * the handles stay put).
     */
    setHistogram(stats) {
      buckets = stats?.buckets ?? [];
      const tallest = buckets.reduce((n, b) => Math.max(n, b.count), 0);
      const digits = filter.digits ?? 0;
      const unit = filter.unit ?? "";
      barsEl.innerHTML = buckets
        .map((bucket) => {
          // min-height keeps a non-empty bucket visible rather than rounding
          // it away — a bar for one row is the interesting one.
          const height =
            bucket.count > 0 ? Math.max(Math.round((bucket.count / tallest) * 100), 4) : 0;
          const label =
            `${formatCellNumber(bucket.from, digits)}${unit}–` +
            `${formatCellNumber(bucket.to, digits)}${unit}: ` +
            `${formatCellNumber(bucket.count)} row${bucket.count === 1 ? "" : "s"}`;
          return `<span class="hist-bar" title="${label}" style="height:${height}%"></span>`;
        })
        .join("");
      paint();
    },

    /** Adopt a window WITHOUT reporting it (URL restore, Clear all, …). */
    setValue(next) {
      value = normalizeSliderRange(next ?? { min: null, max: null }, domain);
      paint();
    },

    getValue: () => ({ ...value }),

    destroy() {
      abort.abort();
    },
  };
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    HISTOGRAM_SLIDER_BUCKETS,
    sliderStepFor,
    normalizeSliderRange,
    classifyBuckets,
    sliderValuetext,
    roundSliderValue,
    createHistogramSlider,
  };
}
