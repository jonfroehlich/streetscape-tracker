/**
 * index.js
 * Overview-map logic for Streetscape City Explorer.
 *
 * Depends on globals from streetscape-utils.js (PROVIDERS, METRICS, getColor,
 * fetchGzippedJson, adaptCitiesPayload, STREETSCAPE_DATA_BASE_URL) and the
 * Leaflet / Chart.js libraries.
 *
 * View state, all persisted in the URL:
 *   ?provider= — which imagery provider's data to show (gsv / mapillary)
 *   ?metric=   — which scalar colors the view (age / coverage); the map
 *                rectangles, legend buckets, and scatter-plot y-axes all
 *                follow the active metric
 *   ?filter=   — inclusive MIN-MAX bucket range of the active metric
 *                (legend range slider); out-of-range cities are dimmed
 */

// ── Global state ──────────────────────────────────────────────
const map = L.map("map").setView([0, 0], 2);
const charts = { pano: null, area: null };
const mapRectangles = [];
let allCityBounds = null;
let rawCitiesData = null; // the fetched cities.json.gz payload (all providers)

// The streetwalks.json.gz sidecar (issue #155), or null when absent/unreadable
// — road-walk street coverage is optional and most cities have none, so every
// read of this is null-tolerant. Merged onto the adapted city records each
// render by mergeStreetwalkStats(); `walkedCityCount` is what the stats banner
// reports so a near-empty streets view explains itself.
let streetwalkManifest = null;
let walkedCityCount = 0;

// The one live popup histogram. Popups are content-functions (built on
// open), so at most one Chart exists at a time — destroyed on close,
// otherwise each open leaks a Chart instance + ResizeObserver.
let activePopupChart = null;
map.on("popupclose", () => {
  activePopupChart?.destroy();
  activePopupChart = null;
});

// Active provider and color-by metric, persisted in the URL
// (?provider=mapillary&metric=coverage)
const overviewUrlParams = new URLSearchParams(window.location.search);
const providerParam = overviewUrlParams.get("provider");
let currentProvider = isKnownProvider(providerParam) ? providerParam : "gsv";
const metricParam = overviewUrlParams.get("metric");
let currentMetric = isKnownMetric(metricParam) ? metricParam : "age";

// Active metric filter: an inclusive bucket-id range {min, max} set by the
// legend's range slider or a legend-row click, or null (no filter). Reset
// whenever the legend is rebuilt (provider/metric switch) because the
// bucket space changes with it. A URL-supplied ?filter= seeds the FIRST
// legend build only, then is consumed.
let filterRange = null;
let filterBucketSpan = { min: 0, max: 0 }; // slider bounds of the active legend
let pendingFilterParam = overviewUrlParams.get("filter");
let legendFilterEls = null; // slider DOM refs, rebuilt with each legend

// Fill color for cities with no value for the active metric (e.g. 0 dated
// panos → null median age). Previously they fell through getColor(null) →
// 0 years → newest-yellow, indistinguishable from genuinely fresh coverage.
const NO_DATA_COLOR = "#666666";

/**
 * Baseline fill opacity for a city rectangle under the active metric.
 *
 * Normally 0.6 for everything. The exception is street coverage: cities are
 * road-walked over a collection cycle, so until one completes, painting the
 * not-yet-walked ones at full opacity buries the walked ones — they fade back
 * instead. Used both at render time and by applyDefaultStyles(), so a hover
 * can't restore the wrong baseline.
 *
 * @param {Object} city - Adapted city record.
 * @returns {number} fillOpacity in [0, 1].
 */
function baseFillOpacity(city) {
  const value = METRICS[currentMetric].valueOf(city);
  return value == null && currentMetric === "streets" ? 0.2 : 0.6;
}

addBasemapLayer(map);

// ── Popup histogram ───────────────────────────────────────────

/**
 * Create a bar-chart canvas showing panorama counts by capture year.
 *
 * @param {Object<number, number>} histogramData - Year → count mapping.
 * @param {number} currentYear - Current calendar year (for age coloring).
 * @returns {HTMLCanvasElement}
 */
function createPopupHistogram(histogramData, currentYear) {
  const canvas = document.createElement("canvas");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label",
    `Bar chart of ${PROVIDERS[currentProvider].panoNoun} by capture year`);
  const years = Object.keys(histogramData).map(Number).sort((a, b) => a - b);
  const counts = years.map((y) => histogramData[y]);
  const ages = years.map((y) => currentYear - y);

  activePopupChart = new Chart(canvas, {
    type: "bar",
    data: {
      labels: years,
      datasets: [{
        data: counts,
        backgroundColor: ages.map((a) => getColor(a, currentProvider)),
        borderColor: "rgba(0,0,0,0.2)",
        borderWidth: 1,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        title: { display: true,
                 text: `${PROVIDERS[currentProvider].panoNoun} by Capture Year` },
      },
      scales: {
        y: { beginAtZero: true, title: { display: true, text: "Panoramas" } },
        x: { title: { display: true, text: "Capture Year" } },
      },
    },
  });

  return canvas;
}

// ── Popup tooltip ─────────────────────────────────────────────

/**
 * Build a DOM element used as a Leaflet popup for a city rectangle.
 *
 * @param {Object} city - City record from cities.json.
 * @returns {HTMLElement}
 */
function createTooltip(city) {
  const container = document.createElement("div");
  container.style.minWidth = "250px";

  const panoStats = city.panorama_counts;
  const ageStats = city.pano_age_stats;

  // Break out the official-Google share of all found panoramas wherever there
  // IS one. The DATA decides that, not the provider name: a `latest` block
  // whose provider publishes no copyright field simply has no
  // `unique_google_panos` key — measured on the production cities.json.gz
  // (1,144 cities), the key is ABSENT rather than null — so `!= null` already
  // answers the capability question the old `city.provider === "gsv"` was
  // asking by identity.
  //
  // Do NOT "finish the job" by swapping that data test for
  // PROVIDERS[...].hasCopyrightFilter. Copyright availability varies per RUN
  // *within* GSV, not per provider: nine GSV cities (the #93 archival imports
  // — berkeley, denison, point-roberts, …) carry copyright_info_available:
  // false and no unique_google_panos at all, so a flag-driven branch would
  // reach .toLocaleString() on undefined and throw out of createTooltip,
  // taking the whole popup with it. The flag is the right question for a
  // LABEL (see the "(360°)" suffix below, which has no value to test); a
  // value that may or may not be present is still a data test.
  let panoLinesHtml = `<li>Total Panoramas: ${panoStats.unique_panos.toLocaleString()}</li>`;
  if (panoStats.unique_google_panos != null) {
    // googleSharePercent guards the 0-pano divide-by-zero "Infinity%" (#69).
    const googlePct = googleSharePercent(
      panoStats.unique_google_panos, panoStats.unique_panos);
    panoLinesHtml += `<li>Google Panoramas: ${panoStats.unique_google_panos.toLocaleString()} (${googlePct}%)</li>`;
  }

  // Any-imagery coverage line (issue #116): shown when flat imagery actually
  // widens the footprint beyond the 360° panos — otherwise it would just
  // repeat the Grid Coverage number. The widening IS the condition, so no
  // provider test is needed: adaptCityRecord falls the any-imagery rate back
  // to the 360° rate for providers that emit no flat imagery, which makes the
  // difference exactly zero for them.
  let anyImageryHtml = "";
  const anyRate = city.any_imagery_coverage_rate_percent;
  if (
    anyRate != null &&
    city.coverage_rate_percent != null &&
    anyRate - city.coverage_rate_percent > 0.05
  ) {
    anyImageryHtml = `<li>Any Imagery: ${anyRate.toFixed(1)}% (incl. flat)</li>`;
  }

  // Road-walk street coverage (issue #99/#155), when this city has been
  // walked. Shown in EVERY metric mode, not just the streets view — the
  // popup is where most people will discover the modality exists at all.
  // A different denominator from Grid Coverage above (street-km driven vs.
  // grid points with imagery), so it's labeled to keep the two apart.
  let streetCoverageHtml = "";
  const walk = city.street_walk;
  if (walk && walk.coverage_pct_by_length != null) {
    const spacing = walk.spacing_m != null ? `${walk.spacing_m} m spacing, ` : "";
    streetCoverageHtml = `
      <li>Street Coverage: ${walk.coverage_pct_by_length.toFixed(1)}% of street-km
        <span style="color:#666">(road-walk, ${spacing}${escapeHtml(walk.run_date ?? "")})</span></li>`;
  }

  // Snapshot history line (schema v2): "3 snapshots since 2025-01-17"
  let snapshotsHtml = "";
  if (city.runs && city.runs.length > 0) {
    const n = city.runs.length;
    snapshotsHtml = `<li>Snapshots: ${n} (since ${escapeHtml(city.runs[0].run_date)})</li>`;
  }

  // Change-since-last-run line (schema v2), colored by direction
  let changeHtml = "";
  const change = formatChangeSummary(city.change);
  if (change) {
    changeHtml = `
      <div style="margin-top:12px"><strong>Since ${escapeHtml(change.from)}:</strong></div>
      <ul class="popup-stats-list">
        <li><span class="change-added">${change.added}</span> /
            <span class="change-removed">${change.removed}</span> panoramas</li>
        ${change.redated ? `<li>${change.redated}</li>` : ""}
        ${change.coverage ? `<li>Coverage: ${change.coverage}</li>` : ""}
      </ul>`;
  }

  // City/state/country names come from OSM/Nominatim (publicly editable
  // third-party data) — escape everything data-derived entering innerHTML.
  container.innerHTML = `
    <h3>${escapeHtml(getCityLabel(city))}</h3>
    <p class="popup-collected">Collected
      <strong>${escapeHtml(city.latest_run_date) || (city.collection_info?.end_time ? new Date(city.collection_info.end_time).toLocaleDateString() : "Unknown")}</strong></p>
    <strong>Coverage Statistics:</strong>
    <ul class="popup-stats-list">
      ${snapshotsHtml}
      <li>Area: ${city.search_area_km2.toFixed(1)} km²</li>
      <li>Grid Coverage: ${city.coverage_rate_percent != null
        ? `${city.coverage_rate_percent.toFixed(1)}% of search points${PROVIDERS[city.provider]?.hasFlatImagery ? " (360°)" : ""}`
        : "No data"}</li>
      ${anyImageryHtml}
      ${streetCoverageHtml}
      ${panoLinesHtml}
    </ul>
    <div style="margin-top:12px"><strong>Age Statistics:</strong></div>
    <ul class="popup-stats-list">
      <li>Median Age: ${ageStats.median_pano_age_years != null ? ageStats.median_pano_age_years.toFixed(1) + " years" : "No data"}</li>
      <li>Average Age: ${ageStats.avg_pano_age_years != null ? ageStats.avg_pano_age_years.toFixed(1) + " years" : "No data"}
        ${ageStats.stdev_pano_age_years != null ? ` (SD=${ageStats.stdev_pano_age_years.toFixed(1)})` : ""}</li>
      <li>Newest: ${panoDateOrNull(ageStats.newest_pano_date)?.toLocaleDateString() ?? "No data"}</li>
      <li>Oldest: ${panoDateOrNull(ageStats.oldest_pano_date)?.toLocaleDateString() ?? "No data"}</li>
    </ul>
    ${changeHtml}
  `;

  // Histogram chart
  const chartContainer = document.createElement("div");
  chartContainer.className = "popup-chart-container";

  const currentYear = new Date().getFullYear();
  // capture_year_histogram may be a {counts: {...}} wrapper, a bare year→count
  // map, or missing entirely for an empty run — buildFilledHistogram tolerates
  // all three (and the Math.min([]) === Infinity empty-run case, #69).
  const rawHistogram =
    city.capture_year_histogram?.counts || city.capture_year_histogram;
  const filledHistogram = buildFilledHistogram(rawHistogram, currentYear);

  chartContainer.appendChild(createPopupHistogram(filledHistogram, currentYear));
  container.appendChild(chartContainer);

  // "View Detailed Analysis" button
  const btnWrap = document.createElement("div");
  btnWrap.style.textAlign = "right";
  btnWrap.style.marginTop = "12px";

  const link = document.createElement("a");
  link.href = `city.html?file=${encodeURIComponent(city.data_file.filename)}`;
  link.target = "_blank";
  link.rel = "noopener";
  link.className = "view-details-btn";
  link.textContent = "View Detailed Analysis";
  btnWrap.appendChild(link);
  container.appendChild(btnWrap);

  return container;
}

// ── Legend ─────────────────────────────────────────────────────

/**
 * Populate the legend panel: a min–max range-filter slider over the active
 * metric's buckets, one row per bucket (integer years for age, deciles for
 * coverage), plus a non-interactive "No data" row when any city lacks a
 * value. Rows double as filter shortcuts — clicking one snaps the range to
 * that single bucket.
 *
 * @param {Object[]} cities - Array of city records.
 */
function createLegend(cities) {
  const metric = METRICS[currentMetric];
  const legend = document.getElementById("legend");
  legend.setAttribute("aria-label", `${metric.legendTitle} legend`);

  const values = [];
  let noDataCount = 0;
  const bucketCounts = new Map();
  cities.forEach((city) => {
    const value = metric.valueOf(city);
    if (value == null) {
      noDataCount++;
      return;
    }
    values.push(value);
    const bucket = metric.bucketOf(value);
    bucketCounts.set(bucket, (bucketCounts.get(bucket) || 0) + 1);
  });

  const buckets = metric.legendBuckets(values);
  filterBucketSpan = { min: Math.min(...buckets), max: Math.max(...buckets) };

  // A new legend means a new bucket space — drop any previous filter. The
  // URL-supplied ?filter= (validated against the real span) seeds the very
  // first build only; anything invalid is dropped from the URL rather than
  // half-applied.
  filterRange = null;
  if (pendingFilterParam != null) {
    filterRange = parseFilterParam(
      pendingFilterParam, filterBucketSpan.min, filterBucketSpan.max);
    pendingFilterParam = null;
  }
  updateFilterUrl();

  let html = `<h4>${metric.legendTitle}</h4>`;

  // Range-filter slider: two overlaid native range inputs (keyboard
  // accessible for free) with a filled track segment marking the selected
  // span. Skipped in the degenerate one-bucket case.
  const hasSlider = filterBucketSpan.max > filterBucketSpan.min;
  if (hasSlider) {
    html += `
      <div class="legend-filter">
        <div class="legend-filter-readout">Filter:
          <span id="legend-filter-label" aria-live="polite"></span></div>
        <div class="legend-slider">
          <div class="legend-slider-track" aria-hidden="true">
            <div class="legend-slider-fill" id="legend-slider-fill"></div>
          </div>
          <input type="range" id="legend-slider-lo"
                 min="${filterBucketSpan.min}" max="${filterBucketSpan.max}" step="1"
                 aria-label="Minimum ${metric.sliderLabel}">
          <input type="range" id="legend-slider-hi"
                 min="${filterBucketSpan.min}" max="${filterBucketSpan.max}" step="1"
                 aria-label="Maximum ${metric.sliderLabel}">
        </div>
        <div class="legend-hint">Drag handles or slide the bar &middot; click a row to filter</div>
      </div>`;
  }

  buckets.forEach((bucket) => {
    const color = metric.bucketColor(bucket, currentProvider);
    const n = bucketCounts.get(bucket) || 0;
    const label = n > 0 ? `(${n} ${n === 1 ? "city" : "cities"})` : "(no cities)";

    // Real <button>s: native Enter/Space activation and focus handling,
    // with aria-pressed carrying the toggle state.
    html += `
      <button type="button" class="legend-item" data-bucket="${bucket}"
              aria-pressed="false"
              aria-label="Filter to cities with ${metric.label.toLowerCase()} ${metric.bucketLabel(bucket)} ${label}">
        <span class="legend-color" style="background:${color}" aria-hidden="true"></span>
        ${metric.bucketLabel(bucket)} ${label}
      </button>`;
  });
  if (noDataCount > 0) {
    html += `
      <div class="legend-item">
        <span class="legend-color" style="background:${NO_DATA_COLOR}" aria-hidden="true"></span>
        No data (${noDataCount})
      </div>`;
  }
  legend.innerHTML = html;

  legendFilterEls = hasSlider ? {
    lo: legend.querySelector("#legend-slider-lo"),
    hi: legend.querySelector("#legend-slider-hi"),
    fill: legend.querySelector("#legend-slider-fill"),
    label: legend.querySelector("#legend-filter-label"),
  } : null;

  if (legendFilterEls) {
    const { lo, hi } = legendFilterEls;
    // Each thumb clamps against the other, so lo ≤ hi always holds
    lo.addEventListener("input", () => {
      const hiV = parseInt(hi.value, 10);
      setFilterRange({ min: Math.min(parseInt(lo.value, 10), hiV), max: hiV });
    });
    hi.addEventListener("input", () => {
      const loV = parseInt(lo.value, 10);
      setFilterRange({ min: loV, max: Math.max(parseInt(hi.value, 10), loV) });
    });

    // Dragging the selected window itself (the band between the thumbs)
    // slides min and max together, width preserved. The thumbs' native
    // pointer handling is untouched — their pointerdowns target the range
    // INPUTs and are excluded here.
    const sliderEl = legend.querySelector(".legend-slider");
    const span = filterBucketSpan.max - filterBucketSpan.min;
    let windowDrag = null; // {startX, startMin, width, pxPerBucket}

    sliderEl.addEventListener("pointerdown", (e) => {
      if (!filterRange || e.target.tagName === "INPUT") return;
      const rect = sliderEl.getBoundingClientRect();
      const bucketAt = filterBucketSpan.min +
        ((e.clientX - rect.left) / rect.width) * span;
      // Only grabs inside the window (±half a bucket of slack) start a drag
      if (bucketAt < filterRange.min - 0.5 || bucketAt > filterRange.max + 0.5) return;
      windowDrag = {
        startX: e.clientX,
        startMin: filterRange.min,
        width: filterRange.max - filterRange.min,
        pxPerBucket: rect.width / span,
      };
      sliderEl.setPointerCapture(e.pointerId);
      sliderEl.classList.add("dragging");
      e.preventDefault();
    });
    sliderEl.addEventListener("pointermove", (e) => {
      if (!windowDrag || !filterRange) return;
      const delta = Math.round((e.clientX - windowDrag.startX) / windowDrag.pxPerBucket);
      const min = Math.max(filterBucketSpan.min,
        Math.min(windowDrag.startMin + delta,
          filterBucketSpan.max - windowDrag.width));
      if (min !== filterRange.min) {
        setFilterRange({ min, max: min + windowDrag.width });
      }
    });
    const endWindowDrag = () => {
      windowDrag = null;
      sliderEl.classList.remove("dragging");
    };
    sliderEl.addEventListener("pointerup", endWindowDrag);
    sliderEl.addEventListener("pointercancel", endWindowDrag);
  }

  // Row clicks snap the filter to that single bucket, or clear it when the
  // row is already the sole selection. (The "No data" row is a plain div
  // and stays non-interactive; buttons handle keyboard activation natively.)
  legend.querySelectorAll("button.legend-item").forEach((item) => {
    item.addEventListener("click", () => {
      const bucket = parseInt(item.dataset.bucket, 10);
      const isSoleSelection = filterRange != null &&
        filterRange.min === bucket && filterRange.max === bucket;
      setFilterRange(isSoleSelection ? null : { min: bucket, max: bucket });
    });
  });

  updateLegendFilterUI();
}

/**
 * Sync the legend's filter UI — slider thumbs, filled track, readout, and
 * row selected/dimmed states — to the current filterRange.
 */
function updateLegendFilterUI() {
  const metric = METRICS[currentMetric];
  const legend = document.getElementById("legend");
  const range = filterRange ?? filterBucketSpan;

  if (legendFilterEls) {
    const { lo, hi, fill, label } = legendFilterEls;
    lo.value = String(range.min);
    hi.value = String(range.max);
    lo.setAttribute("aria-valuetext", metric.bucketLabel(range.min));
    hi.setAttribute("aria-valuetext", metric.bucketLabel(range.max));

    // hi paints on top (later in the DOM). If both thumbs sit together at
    // the span's top, only lo can still move — raise it so it's grabbable.
    lo.style.zIndex =
      range.min === range.max && range.max === filterBucketSpan.max ? "1" : "";
    label.textContent = filterRange
      ? metric.rangeLabel(range.min, range.max)
      : "all cities";

    // The window is only draggable while a filter is active (a full-span
    // fill has nowhere to slide) — the class carries the grab cursor
    fill.classList.toggle("draggable", filterRange != null);

    const span = filterBucketSpan.max - filterBucketSpan.min;
    const loPct = ((range.min - filterBucketSpan.min) / span) * 100;
    const hiPct = ((range.max - filterBucketSpan.min) / span) * 100;
    fill.style.left = `${loPct}%`;
    fill.style.width = `${hiPct - loPct}%`;
  }

  legend.querySelectorAll("button.legend-item").forEach((item) => {
    const bucket = parseInt(item.dataset.bucket, 10);
    const inRange = filterRange != null &&
      bucket >= filterRange.min && bucket <= filterRange.max;
    item.classList.toggle("selected", inRange);
    item.classList.toggle("dimmed", filterRange != null && !inRange);
    item.setAttribute("aria-pressed", String(inRange));
  });
}

/** Reflect filterRange in the URL (?filter=MIN-MAX), mirroring setProvider. */
function updateFilterUrl() {
  const url = new URL(window.location);
  if (filterRange) {
    url.searchParams.set("filter", `${filterRange.min}-${filterRange.max}`);
  } else {
    url.searchParams.delete("filter");
  }
  history.replaceState(null, "", url);
}

/**
 * Set (or clear, with null) the active metric filter and update every
 * dependent surface: URL, legend UI, map rectangles, and scatter plots.
 *
 * @param {?{min: number, max: number}} range - Inclusive bucket range.
 */
function setFilterRange(range) {
  // Selecting the full span means "no filter"
  if (range != null &&
      range.min <= filterBucketSpan.min && range.max >= filterBucketSpan.max) {
    range = null;
  }
  filterRange = range;
  updateFilterUrl();
  updateLegendFilterUI();
  if (filterRange) {
    lastHighlightedCity = FILTER_HIGHLIGHT;
    applyFilterStyles();
  } else {
    lastHighlightedCity = null;
    applyDefaultStyles();
  }
}

// ── Highlighting helpers ──────────────────────────────────────

/**
 * Restyle the map and both scatter plots for the active filterRange: cities
 * inside the range go opaque with a ring, everything else fades. Callers
 * own lastHighlightedCity bookkeeping.
 */
function applyFilterStyles() {
  const metric = METRICS[currentMetric];
  const { min, max } = filterRange;

  // Null value (no data) never falls inside a range
  const inRange = (city) => {
    const value = metric.valueOf(city);
    if (value == null) return false;
    const bucket = metric.bucketOf(value);
    return bucket >= min && bucket <= max;
  };

  // Charts are null while a provider with no cities is shown
  [charts.pano, charts.area].forEach((chart) => {
    if (!chart) return;
    const ds = chart.data.datasets[0];
    ds.pointBackgroundColor = ds.data.map((pt) =>
      // Selected points go fully opaque (base points sit at 0.8)
      inRange(pt.city) ? withAlpha(pt.backgroundColor, 1) : withAlpha(pt.backgroundColor, 0.3)
    );
    ds.pointRadius = ds.data.map((pt) => (inRange(pt.city) ? 6 : 3));
    ds.borderWidth = ds.data.map((pt) => (inRange(pt.city) ? 2 : 0));
    ds.borderColor = ds.data.map((pt) =>
      inRange(pt.city) ? "rgba(0,0,0,0.8)" : "rgba(0,0,0,0)"
    );
    chart.update();
  });

  mapRectangles.forEach((rect) => {
    if (inRange(rect.city)) {
      // Selected state
      rect.setStyle({
        fillOpacity: 0.8,
        weight: 2,
        opacity: 1
      });
      rect.bringToFront();
    } else {
      // Unselected state: significantly more "faded"
      rect.setStyle({
        fillOpacity: 0.1, // Very faint fill
        weight: 0.1,       // Thin borders
        opacity: 0.2       // Faded borders
      });
    }
  });
}

// The current highlight state: null (defaults), a city record (hover), or
// FILTER_HIGHLIGHT (range filter baseline). Hover events fire per mousemove;
// restyling ~1,100 rectangles and updating two charts on every one froze
// the map, so highlightCity/resetHighlights no-op when nothing changed.
const FILTER_HIGHLIGHT = Symbol("metric-filter");
let lastHighlightedCity = null;

/**
 * Highlight a single city across both scatter charts and the map.
 *
 * @param {Object} city - The city record to highlight.
 */
function highlightCity(city) {
  if (city === lastHighlightedCity) return;
  lastHighlightedCity = city;

  [charts.pano, charts.area].forEach((chart) => {
    const ds = chart.data.datasets[0];
    ds.pointBackgroundColor = ds.data.map((pt) =>
      // Hovered city goes fully opaque (base points sit at 0.8)
      pt.city === city ? withAlpha(pt.backgroundColor, 1) : withAlpha(pt.backgroundColor, 0.3)
    );
    ds.pointRadius = ds.data.map((pt) => (pt.city === city ? 6 : 3));
    ds.borderWidth = ds.data.map((pt) => (pt.city === city ? 2 : 0));
    ds.borderColor = ds.data.map((pt) =>
      pt.city === city ? "rgba(0,0,0,0.8)" : "rgba(0,0,0,0)"
    );
    chart.update();
  });

  mapRectangles.forEach((rect) => {
    rect.setStyle(rect.city === city
      ? { fillOpacity: 0.8, weight: 2 }
      : { fillOpacity: 0.2, weight: 1 });
  });
}

/** Restyle the map and both scatter plots to their unfiltered defaults. */
function applyDefaultStyles() {
  // Charts are null while a provider with no cities is shown
  [charts.pano, charts.area].forEach((chart) => {
    if (!chart) return;
    const ds = chart.data.datasets[0];
    ds.pointBackgroundColor = ds.data.map((pt) => pt.backgroundColor);
    ds.pointRadius = ds.data.map(() => 3);
    ds.borderWidth = ds.data.map(() => 0);
    ds.borderColor = ds.data.map(() => "rgba(0,0,0,0)");
    chart.update();
  });

  mapRectangles.forEach((rect) => {
    rect.setStyle({ fillOpacity: baseFillOpacity(rect.city), weight: 1 });
  });
}

/**
 * Return chart and map highlights to the baseline view: the filtered state
 * while a range filter is active (so a hover can't silently wipe the
 * filter dimming), otherwise the defaults.
 */
function resetHighlights() {
  if (filterRange) {
    if (lastHighlightedCity === FILTER_HIGHLIGHT) return;
    lastHighlightedCity = FILTER_HIGHLIGHT;
    applyFilterStyles();
  } else {
    if (lastHighlightedCity === null) return;
    lastHighlightedCity = null;
    applyDefaultStyles();
  }
}

// ── City search ──────────────────────────────────────────────

/**
 * Build the display label for a city record.
 * @param {Object} city
 * @returns {string}  e.g. "Seattle, Washington, United States"
 */
function getCityLabel(city) {
  const name = city.city || city.state?.name || city.country?.name || "Unknown";
  const parts = [name];
  if (city.state?.name && city.state.name !== name) parts.push(city.state.name);
  if (city.country?.name) parts.push(city.country.name);
  return parts.join(", ");
}

/**
 * Select a city: highlight it on map & charts, zoom to it, and open
 * its popup.
 * @param {Object} city
 */
function selectCity(city) {
  // Highlight across all visualizations
  highlightCity(city);

  // Animated zoom to the city rectangle with some padding
  const b = city.bounds;
  const latSpan = b.max_lat - b.min_lat;
  const lonSpan = b.max_lon - b.min_lon;
  const pad = Math.max(latSpan, lonSpan) * 3;
  map.flyToBounds([
    [b.min_lat - pad, b.min_lon - pad],
    [b.max_lat + pad, b.max_lon + pad],
  ], { duration: 1.2 });

  // Open the popup after the animation finishes
  const rect = mapRectangles.find((r) => r.city === city);
  if (rect) {
    map.once("moveend", () => rect.openPopup());
  }
}

// Search state shared across provider re-renders: the entry list is
// swapped per provider, but DOM listeners are attached exactly once.
let searchEntries = [];
let searchInitialized = false;

/**
 * Initialise (or, on provider switch, re-populate) the city search
 * autocomplete. Safe to call repeatedly.
 * @param {Object[]} cities - Array of city records.
 */
function initCitySearch(cities) {
  // Pre-compute labels and sort alphabetically
  searchEntries = cities
    .map((city) => ({ city, label: getCityLabel(city) }))
    .sort((a, b) => a.label.localeCompare(b.label));

  if (searchInitialized) return;
  searchInitialized = true;

  const input = document.getElementById("city-search-input");
  const list = document.getElementById("city-search-results");
  let activeIdx = -1;
  let matches = [];

  /** Show or hide the dropdown. */
  function showDropdown(show) {
    list.classList.toggle("visible", show);
    input.setAttribute("aria-expanded", String(show));
  }

  /** Render the current matches into the dropdown list. */
  function renderMatches() {
    list.innerHTML = "";
    activeIdx = -1;

    if (matches.length === 0) {
      showDropdown(false);
      return;
    }

    matches.forEach((entry, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("tabindex", "-1");
      li.id = `city-option-${i}`;
      li.textContent = entry.label;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault(); // keep focus on input
        pickMatch(i);
      });
      li.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          pickMatch(i);
        } else if (e.key === "ArrowDown") {
          e.preventDefault();
          const next = Math.min(i + 1, matches.length - 1);
          setActive(next);
          list.children[next].focus();
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          if (i === 0) {
            input.focus();
          } else {
            setActive(i - 1);
            list.children[i - 1].focus();
          }
        } else if (e.key === "Escape") {
          showDropdown(false);
          input.focus();
        }
      });
      list.appendChild(li);
    });

    showDropdown(true);
  }

  /** Commit a selection by index. */
  function pickMatch(idx) {
    if (idx < 0 || idx >= matches.length) return;
    const entry = matches[idx];
    input.value = entry.label;
    showDropdown(false);
    selectCity(entry.city);
  }

  /** Set the visual active state for keyboard navigation. */
  function setActive(idx) {
    const items = list.querySelectorAll("li");
    items.forEach((li) => li.classList.remove("active"));
    activeIdx = idx;
    if (idx >= 0 && idx < items.length) {
      items[idx].classList.add("active");
      items[idx].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", items[idx].id);
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    if (query.length === 0) {
      matches = [];
      renderMatches();
      resetHighlights();
      return;
    }

    // Filter: prefer starts-with, then contains
    const startsWith = [];
    const contains = [];
    for (const entry of searchEntries) {
      const lower = entry.label.toLowerCase();
      if (lower.startsWith(query)) startsWith.push(entry);
      else if (lower.includes(query)) contains.push(entry);
    }
    matches = startsWith.concat(contains).slice(0, 15);
    renderMatches();
  });

  input.addEventListener("keydown", (e) => {
    if (!list.classList.contains("visible")) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(activeIdx + 1, matches.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(activeIdx - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (activeIdx >= 0) {
        pickMatch(activeIdx);
      } else if (matches.length > 0) {
        pickMatch(0);
      }
    } else if (e.key === "Escape") {
      showDropdown(false);
    } else if (e.key === "Tab" && list.classList.contains("visible")) {
      e.preventDefault();
      const target = activeIdx >= 0 ? activeIdx : 0;
      setActive(target);
      list.children[target].focus();
    }
  });

  // Close dropdown when clicking elsewhere
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#city-search")) {
      showDropdown(false);
    }
  });

  // Reset button: clear search AND the metric filter, reset highlights,
  // zoom to all cities
  document.getElementById("city-search-reset").addEventListener("click", () => {
    input.value = "";
    matches = [];
    showDropdown(false);
    setFilterRange(null);
    resetHighlights();
    map.closePopup();
    if (allCityBounds) map.flyToBounds(allCityBounds, { duration: 1.2 });
  });
}

// ── Scatter plots ─────────────────────────────────────────────

/**
 * Create the two bottom-right scatter plots: pano count vs. the active
 * metric and city area vs. the active metric (y-axis and point colors
 * both follow the "color by" toggle).
 *
 * @param {Object[]} cities - Array of city records.
 */
function createScatterPlots(cities) {
  const metric = METRICS[currentMetric];

  // Cities without a metric value have no y value to plot — they stay on
  // the map (greyed) but are omitted from the scatters.
  const valuedCities = cities.filter((c) => metric.valueOf(c) != null);

  // 80%-opaque points: with ~1,100 overlapping dots, slight translucency
  // shows density instead of a solid mass (hover/legend selection bumps the
  // highlighted points back to full opacity).
  const panoData = valuedCities.map((city) => ({
    x: city.pano_count,
    y: metric.valueOf(city),
    city,
    backgroundColor: withAlpha(metric.color(metric.valueOf(city), currentProvider), 0.8),
  }));

  const areaData = valuedCities.map((city) => ({
    x: city.search_area_km2,
    y: metric.valueOf(city),
    city,
    backgroundColor: withAlpha(metric.color(metric.valueOf(city), currentProvider), 0.8),
  }));

  // Canvas aria-labels track the active metric (the static HTML defaults
  // describe the default age view)
  document.getElementById("panoScatter").setAttribute("aria-label",
    `Scatter plot of total panorama count versus ${metric.label.toLowerCase()} for each city`);
  document.getElementById("areaScatter").setAttribute("aria-label",
    `Scatter plot of city area versus ${metric.label.toLowerCase()} for each city`);

  const sharedOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      tooltip: {
        callbacks: {
          label: (ctx) => [
            getCityLabel(ctx.raw.city),
            `${metric.label}: ${metric.formatValue(ctx.raw.y)}`,
          ],
        },
      },
      // Wheel/pinch zoom + drag pan (chartjs-plugin-zoom; double-click
      // resets — wired in initChartZoomReset). ~1,100 overlapping dots need
      // zoom to disambiguate. `limits: original` stops panning/zooming out
      // beyond the data extent.
      zoom: {
        zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: "xy" },
        pan: { enabled: true, mode: "xy" },
        limits: {
          x: { min: "original", max: "original" },
          y: { min: "original", max: "original" },
        },
      },
    },
    scales: {
      y: {
        beginAtZero: true,
        // Coverage is a percentage — pin the top at 100 so a 40%-max
        // provider view doesn't stretch to look like full coverage
        max: metric.yMax ?? undefined,
        title: { display: true, text: metric.axisTitle },
      },
    },
    onHover: (_event, elements) => {
      if (elements.length > 0) {
        highlightCity(elements[0].element.$context.raw.city);
      } else {
        resetHighlights();
      }
    },
    onClick: (_event, elements) => {
      if (elements.length === 0) return;
      const c = elements[0].element.$context.raw.city;
      const bounds = [
        [c.bounds.min_lat, c.bounds.min_lon],
        [c.bounds.max_lat, c.bounds.max_lon],
      ];
      const latSpan = c.bounds.max_lat - c.bounds.min_lat;
      const lonSpan = c.bounds.max_lon - c.bounds.min_lon;
      const padding = Math.max(latSpan, lonSpan) * 5.5;
      map.fitBounds([
        [bounds[0][0] - padding, bounds[0][1] - padding],
        [bounds[1][0] + padding, bounds[1][1] + padding],
      ]);
    },
  };

  charts.pano = new Chart(document.getElementById("panoScatter"), {
    type: "scatter",
    data: {
      datasets: [{
        data: panoData,
        backgroundColor: panoData.map((d) => d.backgroundColor),
        pointRadius: 3,
        pointHoverRadius: 6,
        borderWidth: 0, // the black ring appears only on hover/bucket highlight
        // Let marks at the scale extremes (100% coverage, rightmost pano
        // counts) draw their full circle instead of being cut by the plot
        // edge; 8px covers pointHoverRadius + highlight border.
        clip: 8,
      }],
    },
    options: {
      ...sharedOptions,
      plugins: {
        ...sharedOptions.plugins,
        legend: { display: false },
        title: { display: true, text: `Pano Count vs ${metric.titleNoun}` },
      },
      scales: {
        ...sharedOptions.scales,
        x: {
          // Auto-ranged: a fixed min of 100 hid every city with fewer
          // than 100 panos (small towns, sparse Mapillary coverage)
          type: "logarithmic",
          title: { display: true, text: "Total Panos (log scale)" },
        },
      },
    },
  });

  charts.area = new Chart(document.getElementById("areaScatter"), {
    type: "scatter",
    data: {
      datasets: [{
        data: areaData,
        backgroundColor: areaData.map((d) => d.backgroundColor),
        pointRadius: 3,
        pointHoverRadius: 6,
        borderWidth: 0, // ring only on hover/bucket highlight
        clip: 8, // same edge-overflow allowance as the pano scatter
      }],
    },
    options: {
      ...sharedOptions,
      plugins: {
        ...sharedOptions.plugins,
        legend: { display: false },
        title: { display: true, text: `City Size (km²) vs ${metric.titleNoun}` },
      },
      scales: {
        ...sharedOptions.scales,
        x: {
          // Auto-ranged (a fixed min of 1 hid sub-km² villages)
          type: "logarithmic",
          title: { display: true, text: "Area (km², log scale)" },
        },
      },
    },
  });
}

// ── Provider toggle & rendering ───────────────────────────────

/**
 * Render everything (banner, legend, rectangles, scatter plots, search)
 * for the current provider and color-by metric from the already-fetched
 * payload.
 *
 * @param {boolean} [fitMap=false] - Fit the viewport to all cities
 *   (first render only; provider/metric switches keep the current view).
 */
function renderProvider(fitMap = false) {
  const providerInfo = PROVIDERS[currentProvider];
  const metric = METRICS[currentMetric];
  const { meta, cities } = adaptCitiesPayload(rawCitiesData, currentProvider);

  // Attach road-walk coverage from the sidecar manifest (issue #155). It is
  // NOT in the aggregate — folding it in is #102 — so METRICS.streets reads
  // what this merge writes onto each record.
  walkedCityCount = mergeStreetwalkStats(cities, streetwalkManifest);

  // Clear previous provider's view
  map.closePopup();
  mapRectangles.forEach((rect) => rect.remove());
  mapRectangles.length = 0;
  [charts.pano, charts.area].forEach((chart) => chart?.destroy());
  charts.pano = charts.area = null;

  // Provider attribution (Mapillary's terms require visible attribution)
  Object.values(PROVIDERS).forEach((p) =>
    map.attributionControl.removeAttribution(p.attribution));
  map.attributionControl.addAttribution(providerInfo.attribution);

  // Stats banner. In streets mode much of the map is "no data": the street
  // channels are scheduled like the grid ones, so cities fill in over a
  // collection cycle rather than all at once. Say so outright rather than let
  // a sparse render read as a broken one.
  const streetsNote = currentMetric === "streets"
    ? `<br><span class="stats-note">Road-walk street coverage: ${walkedCityCount}
       of ${cities.length} ${providerInfo.label} cities walked
       (<a href="streets.html">see all</a>)</span>`
    : "";
  document.getElementById("stats").innerHTML = `
    <strong>${providerInfo.label} City Coverage Analysis</strong><br>
    ${cities.length} cities analyzed | Updated: ${new Date(meta.generatedAt).toLocaleString()}
    ${streetsNote}
  `;

  if (cities.length === 0) {
    // No legend → no filter (clear any range left over from the previous
    // provider, and its URL param)
    filterRange = null;
    legendFilterEls = null;
    lastHighlightedCity = null;
    updateFilterUrl();
    document.getElementById("legend").innerHTML =
      `<h4>No ${providerInfo.label} data yet</h4>`;
    return;
  }

  // Legend
  createLegend(cities);

  // Map rectangles
  cities.forEach((city) => {
    const bounds = [
      [city.bounds.min_lat, city.bounds.min_lon],
      [city.bounds.max_lat, city.bounds.max_lon],
    ];

    const value = metric.valueOf(city);
    const rect = L.rectangle(bounds, {
      color: value != null ? metric.color(value, currentProvider) : NO_DATA_COLOR,
      weight: 1,
      fillOpacity: baseFillOpacity(city),
    }).addTo(map);

    rect.city = city;
    // Content function: the popup DOM (including its Chart.js histogram)
    // is built on OPEN, not eagerly for all ~1,100 cities at render time —
    // and rebuilt each open, so a provider toggle can't leak stale charts.
    //
    // The popup lives inside the map pane's stacking context (the pane's
    // translate3d transform creates one), so NO z-index can lift it above
    // the fixed header/search/rail chrome — instead, autoPan padding makes
    // Leaflet pan the map until the popup sits clear of all of it: 390px
    // left covers the search column (left:60 + 320 wide), 130px top covers
    // the 44px header plus the #stats banner, and the right padding keeps
    // it out from under the right rail's legend.
    rect.bindPopup(() => createTooltip(city), {
      maxWidth: 340,
      autoPanPaddingTopLeft: L.point(390, 130),
      autoPanPaddingBottomRight: L.point(260, 40),
    });
    mapRectangles.push(rect);

    rect.on("mouseover", () => highlightCity(city));
    rect.on("mouseout", () => resetHighlights());
  });

  createScatterPlots(cities);

  // A filter seeded from the URL (first render only — createLegend clears
  // it otherwise) dims the freshly built rectangles and charts
  if (filterRange) {
    lastHighlightedCity = FILTER_HIGHLIGHT;
    applyFilterStyles();
  } else {
    lastHighlightedCity = null;
  }

  initCitySearch(cities);

  allCityBounds = cities.map((c) => [
    [c.bounds.min_lat, c.bounds.min_lon],
    [c.bounds.max_lat, c.bounds.max_lon],
  ]);
  if (fitMap) map.fitBounds(allCityBounds);
}

/**
 * Switch the active imagery provider, re-render from the cached payload,
 * and persist the choice in the URL.
 *
 * @param {string} provider - Provider key (see PROVIDERS).
 */
function setProvider(provider) {
  if (!isKnownProvider(provider) || provider === currentProvider) return;
  currentProvider = provider;

  const url = new URL(window.location);
  if (provider === "gsv") url.searchParams.delete("provider");
  else url.searchParams.set("provider", provider);
  history.replaceState(null, "", url);

  // Before the payload arrives, just record the choice — the initial
  // renderProvider(true) in loadData() picks it up. (Previously a click
  // during the fetch was silently reverted.)
  if (rawCitiesData) renderProvider();
}

/**
 * Markup for the provider radio group's options — one <label> per registered
 * provider, exactly one of them checked.
 *
 * Generated rather than hardcoded in index.html (issue #225). `?provider=` is
 * validated against the registry (isKnownProvider), so the day a third
 * provider is registered a two-radio group would leave `currentProvider` with
 * no radio at all and NOTHING checked — a keyboard user tabbing in lands on
 * the first option and their first arrow-press silently switches provider,
 * which is the a11y failure a radio group's checked state exists to prevent.
 * The fieldset and its visually-hidden <legend> (the group's accessible name)
 * stay in the markup; only the options are rendered.
 *
 * @param {Object} providers - Provider registry (PROVIDERS; a subset in tests).
 * @param {string} current - Active provider key — the one radio marked checked.
 * @returns {string} HTML for the inside of the #provider-toggle fieldset.
 */
function providerToggleHtml(providers, current) {
  return Object.entries(providers).map(([key, p]) => {
    // Registry values are code rather than fetched data, but they land in an
    // attribute, so they go through escapeHtml like every other interpolation
    // on this page. An entry with no description gets no tooltip at all rather
    // than an empty one, which some screen readers announce.
    const title = p.description ? ` title="${escapeHtml(p.description)}"` : "";
    return `
    <label>
      <input type="radio" name="provider" value="${escapeHtml(key)}"${key === current ? " checked" : ""}>
      <span${title}>${escapeHtml(p.label)}</span>
    </label>`;
  }).join("");
}

/** Render the provider radio group from the registry and wire up its events. */
function initProviderToggle() {
  const fieldset = document.getElementById("provider-toggle");
  // insertAdjacentHTML rather than innerHTML: the <legend> already in the
  // markup is the group's accessible name and must survive.
  if (fieldset) {
    fieldset.insertAdjacentHTML("beforeend", providerToggleHtml(PROVIDERS, currentProvider));
  }
  document.querySelectorAll('input[name="provider"]').forEach((radio) => {
    radio.checked = radio.value === currentProvider;
    radio.addEventListener("change", () => {
      if (radio.checked) setProvider(radio.value);
    });
  });
}

/**
 * Switch the active color-by metric, re-render from the cached payload,
 * and persist the choice in the URL. Mirrors setProvider.
 *
 * @param {string} metric - Metric key (see METRICS).
 */
function setMetric(metric) {
  if (!isKnownMetric(metric) || metric === currentMetric) return;
  currentMetric = metric;

  const url = new URL(window.location);
  if (metric === "age") url.searchParams.delete("metric");
  else url.searchParams.set("metric", metric);
  history.replaceState(null, "", url);

  // Before the payload arrives, just record the choice — the initial
  // renderProvider(true) in loadData() picks it up.
  if (rawCitiesData) renderProvider();
}

/** Wire up the color-by radio group and reflect the initial state. */
function initMetricToggle() {
  document.querySelectorAll('input[name="metric"]').forEach((radio) => {
    radio.checked = radio.value === currentMetric;
    radio.addEventListener("change", () => {
      if (radio.checked) setMetric(radio.value);
    });
  });
}

// ── Chart drawer & zoom reset ─────────────────────────────────

/**
 * Wire the scatter-plot drawer's minimize/expand toggle. Collapsed state
 * persists across visits; on expand the charts are re-measured (Chart.js
 * can't size a canvas inside display:none).
 */
function initChartDrawer() {
  const container = document.querySelector(".chart-container");
  const toggle = document.getElementById("chart-drawer-toggle");
  const KEY = "streetscape-overview-charts-collapsed";

  function setCollapsed(collapsed) {
    container.classList.toggle("collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Expand charts" : "Minimize charts");
    toggle.textContent = collapsed ? "+" : "–";
    try {
      localStorage.setItem(KEY, String(collapsed));
    } catch { /* private browsing: collapse still works, just not persisted */ }
    if (!collapsed) {
      charts.pano?.resize();
      charts.area?.resize();
    }
  }

  toggle.addEventListener("click", () =>
    setCollapsed(!container.classList.contains("collapsed")));

  let stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch { /* ignore */ }
  if (stored === "true") setCollapsed(true);
}

/**
 * Double-click on a scatter canvas resets its zoom/pan. Wired once on the
 * (static) canvases; routed through the live `charts.*` handles because the
 * Chart instances are destroyed and rebuilt on every provider/metric switch.
 */
function initChartZoomReset() {
  document.getElementById("panoScatter").addEventListener("dblclick", () =>
    charts.pano?.resetZoom());
  document.getElementById("areaScatter").addEventListener("dblclick", () =>
    charts.area?.resetZoom());
}

// ── Data loading ──────────────────────────────────────────────

/** Fetch cities.json.gz + the streetwalk manifest, then render the view. */
async function loadData() {
  // Wire the toggles BEFORE the fetch so a click during loading is
  // recorded (setProvider/setMetric defer the render until data arrives).
  initProviderToggle();
  initMetricToggle();
  initChartDrawer();
  initChartZoomReset();
  try {
    // The manifest is small (a few hundred bytes) and optional — fetch it
    // alongside the aggregate rather than serially, and let its own error
    // handling resolve it to null so a missing one never blocks the map.
    const [cities, manifest] = await Promise.all([
      fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "cities.json.gz"),
      fetchStreetwalkManifest(),
    ]);
    rawCitiesData = cities;
    streetwalkManifest = manifest;
    document.getElementById("loading").style.display = "none";
    renderProvider(true);
  } catch (error) {
    console.error("Error loading data:", error);
    document.getElementById("loading").textContent =
      "Error loading city data. Please check the console for details.";
  }
}

// Guarded so `require`ing this file under Node's test runner (which stubs the
// browser globals it touches at load) exercises the pure helpers without
// trying to load anything — the same shape as streets.js/grid.js.
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", loadData);
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    createTooltip,
    providerToggleHtml,
    initProviderToggle,
  };
}
