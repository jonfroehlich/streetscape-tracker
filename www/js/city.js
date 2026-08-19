/* exported switchRun, toggleYear, setGsvMode, toggleFlatOnly, setRenderAll, toggleDiffOverlay */
// (switchRun/toggleYear/setGsvMode/toggleFlatOnly/setRenderAll/toggleDiffOverlay
// are invoked from onchange/onclick attributes in the HTML this file
// generates, so ESLint can't see those string references.)
/**
 * city.js
 * Per-city detail-view logic for Streetscape City Explorer.
 *
 * Depends on globals from streetscape-utils.js: PROVIDERS, getColor,
 * getProviderFromFilename, fetchGzippedJson, adaptCitiesPayload,
 * STREETSCAPE_DATA_BASE_URL. The imagery provider (GSV vs Mapillary) is derived
 * from the data filename's provider token.
 *
 * Third-party libraries (loaded via CDN in city.html):
 *   Leaflet, Chart.js, moment, chartjs-adapter-moment, PapaParse, pako
 *
 * Streaming architecture:
 *   The CSV data files for large cities can exceed 200 MB uncompressed.
 *   We use the native browser DecompressionStream + TextDecoderStream to
 *   inflate on the fly, then manually drive a read loop that feeds string
 *   chunks to PapaParse. This avoids both the pako RangeError (single
 *   giant string) and PapaParse's unsupported WHATWG ReadableStream input.
 *
 * @module city
 */

// ── Map setup ──────────────────────────────────────────────────
// preferCanvas: pano markers render on a shared <canvas> instead of one
// SVG DOM node each — the difference between usable and frozen for large
// cities (10⁵+ markers).
const map = L.map("map", { zoomControl: false, preferCanvas: true }).setView([0, 0], 13);
L.control.zoom({ position: "bottomleft" }).addTo(map);

L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: "© OpenStreetMap contributors © CARTO",
  maxZoom: 19,
}).addTo(map);

// ── Module-level state ─────────────────────────────────────────
let markersByYear = {};
let activeYears = new Set();
let selectedDate = null;
let cityNameGlobal = "";
let stateNameGlobal = "";
let totalPanosGlobal = 0;
let collectionDateGlobal = "";
let statsGlobal = null;
let panoStatsGlobal = null; // provider pano block: google_panos (gsv) or all_panos
let copyrightAvailableGlobal = true; // false for archival GSV runs with no copyright data
let providerGlobal = "gsv"; // derived from the data filename
let oldestDateGlobal = null;
let newestDateGlobal = null;
let cityIdGlobal = null; // canonical city_id from the aggregate record (for the streetwalk lookup)

// GSV imagery display mode. Every run streams BOTH official-Google and
// contributor (UGC) panos into markers; `showAllGsv` is a pure display
// switch: false (default) hides the contributor markers, true reveals them.
// Never set for non-GSV providers (their rows are all provider imagery) or
// archival GSV runs that recorded no copyright (Google vs UGC unknown).
let showAllGsv = false;

// Flat-only imagery layer (issue #116, Mapillary only). A FLAT_ONLY CSV row
// marks a grid point covered by flat/perspective imagery but no 360° pano;
// these render as distinct muted markers, hidden by default (showFlatOnly),
// so the map can reveal phone-camera coverage that the pano layer omits.
let flatOnlyMarkers = [];
let showFlatOnly = false;
const FLAT_ONLY_COLOR = "#9aa0a6"; // muted gray — visibly not a dated pano

// ── Render-cap / visibility model (issues #77, #58) ─────────────
// `markersByYear` holds every streamed pano marker (both GSV modes). What is
// actually *drawn* is reconciled through a single desired→onMap delta so no
// interaction re-touches all N markers, and so dense cities can draw only a
// spatial subsample. Reported stats/counts/plot always use the full set.
let onMap = new Set();          // markers currently added to the Leaflet map
let desiredShown = new Set();   // markers that SHOULD be drawn right now
let renderAllOverride = false;  // user opted out of the cap ("Render all")
let panoDotsHidden = false;     // streets overlay hid the pano layer

// Mode-scoped caches, rebuilt by rebuildModeCaches() on load and on setGsvMode.
// "In mode" = markerInMode(): Google-only (default) vs all-GSV; for non-GSV and
// archival runs every marker is in mode.
let inModeAll = [];             // all in-mode markers
let inModeByYear = new Map();   // year -> in-mode markers[]
let inModeCountByYear = new Map(); // year -> count (legend, avoids O(N) per row)
let inModeByDateStr = new Map();   // captureDate.toDateString() -> in-mode markers[]
let totalInMode = 0;
let globalCapped = [];          // spatial-stride subsample of inModeAll (<= RENDER_CAP)
let cappedByYear = new Map();   // year -> stride subsample (lazy)
let allMarkerCount = 0;         // both modes; fixed per run (legend "All GSV" count)
let googleMarkerCount = 0;      // official-Google markers; fixed per run

// Temporal-plot handles, kept at module scope so setGsvMode() can rebuild the
// plot in place (updating the existing chart/handlers rather than re-creating
// them and duplicating the keyboard listeners / live region).
let temporalChartGlobal = null;
let temporalDataGlobal = [];
let temporalKeyboardIdx = -1;

// panoDateOrNull(), isGoogleCopyright() and isPlausibleCaptureDate() are shared
// helpers from streetscape-utils.js (loaded first), reused here for the
// info-panel dates, the © Google filter, and the #213 impossible-date guard.
let runsGlobal = [];        // this city's run history from the aggregate
let currentFileGlobal = ""; // csv.gz filename of the run being displayed
let changeGlobal = null;    // change_from_previous_run block of this run

// "Changes since previous run" overlay (diff-overlay.js renders it; the diff
// detail CSV is fetched lazily on first toggle and the layer cached after).
let diffOverlay = { shown: false, loading: false, layer: null, counts: null, drawn: null, error: false };

// Run-history mini-chart handle: updateLegend repaints via innerHTML (which
// destroys the canvas), so the chart is destroyed/recreated alongside it.
let runHistoryChart = null;

// ── Map interaction ────────────────────────────────────────────

// Reset selection when clicking the map background.
map.on("click", (e) => {
  if (e.originalEvent.target !== map._container) return;
  selectedDate = null;
  activeYears.clear();
  applyDesired(); // reconcile drawn set + reset any date dimming (over onMap only)
  updateLegend(Object.keys(markersByYear).map(Number));
});

/**
 * Whether a marker belongs in the current GSV display mode. Contributor
 * (non-official-Google) GSV markers are shown only in "All GSV" mode; every
 * Google marker — and every non-GSV/archival marker, which is tagged
 * isGoogle:true since the distinction doesn't apply — is always eligible.
 * Year- and date-filters compose on top of this base visibility.
 *
 * @param {L.CircleMarker} m
 * @returns {boolean}
 */
function markerInMode(m) {
  return m.options.isGoogle || showAllGsv;
}

// ── Reconcile model (issues #77, #58) ──────────────────────────
//
// One path decides what is drawn: compute the desired set (mode ∧ year ∧ cap,
// plus the honesty union for a selected date), then add/remove only the delta
// against what is already on the map. Every interaction routes through
// applyDesired(), so no click ever re-touches all N markers.

/**
 * Spatially-strided subsample of a marker list, or the list itself when it is
 * already within the cap. Deterministic (see spatialStrideSample) so the drawn
 * dots stay spread across the city rather than clumping.
 *
 * @param {L.CircleMarker[]} markers
 * @returns {L.CircleMarker[]} At most RENDER_CAP markers.
 */
function strideSubset(markers) {
  if (markers.length <= RENDER_CAP) return markers;
  const pts = markers.map((m) => {
    const ll = m.getLatLng();
    return [ll.lat, ll.lng];
  });
  return spatialStrideSample(pts, RENDER_CAP).map((i) => markers[i]);
}

/**
 * Rebuild the in-mode caches after the marker set's mode changes (initial load
 * and setGsvMode). Everything downstream reads these instead of re-filtering
 * all markers on every interaction.
 */
function rebuildModeCaches() {
  inModeAll = [];
  inModeByYear = new Map();
  inModeCountByYear = new Map();
  inModeByDateStr = new Map();
  for (const [y, markers] of Object.entries(markersByYear)) {
    const year = Number(y);
    for (const m of markers) {
      if (!markerInMode(m)) continue;
      inModeAll.push(m);
      if (!inModeByYear.has(year)) inModeByYear.set(year, []);
      inModeByYear.get(year).push(m);
      inModeCountByYear.set(year, (inModeCountByYear.get(year) || 0) + 1);
      const dk = m.options.captureDate.toDateString();
      if (!inModeByDateStr.has(dk)) inModeByDateStr.set(dk, []);
      inModeByDateStr.get(dk).push(m);
    }
  }
  totalInMode = inModeAll.length;
  globalCapped = strideSubset(inModeAll);
  cappedByYear = new Map();
}

/**
 * The set of markers that should currently be drawn. Base membership is the
 * in-mode set, narrowed to an active year and capped (unless the user chose
 * "Render all"); a selected date additionally unions in EVERY in-mode marker of
 * that date at full opacity, so the map never under-shows a date relative to the
 * temporal plot's count (the render-cap honesty trap).
 *
 * @returns {Set<L.CircleMarker>}
 */
function computeDesiredShown() {
  let base;
  if (activeYears.size) {
    const y = [...activeYears][0];
    const yearMarkers = inModeByYear.get(y) ?? [];
    if (renderAllOverride) {
      base = yearMarkers;
    } else {
      if (!cappedByYear.has(y)) cappedByYear.set(y, strideSubset(yearMarkers));
      base = cappedByYear.get(y);
    }
  } else {
    base = renderAllOverride ? inModeAll : globalCapped;
  }
  const set = new Set(base);
  if (selectedDate) {
    for (const m of (inModeByDateStr.get(selectedDate.toDateString()) ?? [])) set.add(m);
  }
  return set;
}

/**
 * Reconcile the map to `desiredShown` (or nothing, when the streets overlay has
 * hidden the pano layer) by adding/removing only the delta. Bounded by the
 * symmetric difference, i.e. ≤ RENDER_CAP work, never O(N).
 */
function reconcile() {
  const target = panoDotsHidden ? new Set() : desiredShown;
  const { toAdd, toRemove } = computeVisibilityDelta(onMap, target);
  for (const m of toRemove) { m.remove(); onMap.delete(m); }
  for (const m of toAdd) { m.addTo(map); onMap.add(m); }
}

/**
 * Style every on-map marker for the current date filter (default when no date
 * is selected). Iterates only what is drawn (≤ cap), so it stays cheap; a
 * re-added marker that carried a stale dimmed style is corrected here.
 */
function restyleOnMap() {
  const selKey = selectedDate ? selectedDate.toDateString() : null;
  for (const m of onMap) {
    const s = markerDateStyle(m.options.captureDate.toDateString(), selKey);
    m.setStyle({ fillOpacity: s.fillOpacity });
    m.setRadius(s.radius);
  }
}

/**
 * The single entry point every interaction uses: recompute the desired set,
 * reconcile the map to it, and restyle what is drawn.
 */
function applyDesired() {
  desiredShown = computeDesiredShown();
  reconcile();
  restyleOnMap();
}

/**
 * Toggle the render cap off ("Render all") or back on ("Show subset").
 * Shown only for cities above the cap; see updateLegend's cap notice.
 *
 * @param {boolean} on - true = draw all in-mode panos; false = re-apply the cap.
 */
function setRenderAll(on) {
  if (on === renderAllOverride) return;
  renderAllOverride = on;
  applyDesired();
  updateLegend(Object.keys(markersByYear).map(Number));
}

// ── Legend (Leaflet control) ───────────────────────────────────
const legendControl = L.control({ position: "topright" });
legendControl.onAdd = () => {
  const div = L.DomUtil.create("div", "legend");
  // Keep legend interaction local: without these, clicking a year toggle
  // also fires the map's click handler and scrolling the legend zooms
  // the map underneath it.
  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  return div;
};
legendControl.addTo(map);

/**
 * Rebuild the legend HTML to reflect the current marker state.
 * Renders two sections: a data overview table and an interactive year filter.
 *
 * @param {Iterable<number>} years - Set or array of years present in data.
 */
function updateLegend(years) {
  const div = document.querySelector(".legend");
  const currentYear = new Date().getFullYear();
  const sortedYears = Array.from(years).sort((a, b) => b - a);

  // ── Section 1: Data overview ───────────────────────────────
  // City/state names originate in OSM/Nominatim (publicly editable) via
  // the run's JSON metadata — escape before injecting.
  let html = `<h4>${escapeHtml(cityNameGlobal)}${stateNameGlobal ? `, ${escapeHtml(stateNameGlobal)}` : ""}</h4>`;

  if (statsGlobal) {
    const s = statsGlobal;
    const fmt = (v) => (v != null ? v.toFixed(1) : "—");
    const oldest = oldestDateGlobal?.toLocaleDateString() ?? "—";
    const newest = newestDateGlobal?.toLocaleDateString() ?? "—";
    const median = fmt(panoStatsGlobal.age_stats.median_pano_age_years);
    const avg    = fmt(panoStatsGlobal.age_stats.avg_pano_age_years);
    const sd     = fmt(panoStatsGlobal.age_stats.stdev_pano_age_years);

    html += `
      <table class="legend-stats" aria-label="Dataset statistics">
        <tbody>
          <tr>
            <td>Collected</td>
            <td>${collectionDateGlobal || "Unknown"}</td>
          </tr>
          <tr>
            <td>Dated panoramas</td>
            <td>${totalPanosGlobal.toLocaleString()}</td>
          </tr>
          <tr>
            <td>Grid area</td>
            <td>${s.search_grid.area_km2.toFixed(1)} km²</td>
          </tr>
          <tr>
            <td>Search points</td>
            <td>${s.search_grid.total_search_points.toLocaleString()}</td>
          </tr>
          <tr>
            <td>Step size</td>
            <td>${s.search_grid.step_length_meters} m</td>
          </tr>
          <tr class="legend-stats-divider">
            <td>Oldest pano</td>
            <td>${oldest}</td>
          </tr>
          <tr>
            <td>Newest pano</td>
            <td>${newest}</td>
          </tr>
          <tr>
            <td>Median age</td>
            <td>${median} yrs</td>
          </tr>
          <tr>
            <td>Avg age</td>
            <td>${avg} yrs <span class="legend-stats-sd">(±${sd})</span></td>
          </tr>
        </tbody>
      </table>`;
  } else {
    html += `<p class="legend-meta">Dated panos: ${totalPanosGlobal.toLocaleString()}</p>`;
  }

  // Archival imports carry no copyright column, so the Google-vs-contributor
  // split below is unavailable and the counts above include every pano. Said
  // here because this is now the only place that caveat appears.
  if (!copyrightAvailableGlobal) {
    html += `<p class="legend-meta"><em>Copyright not recorded (archival import);
      counts include all panoramas</em></p>`;
  }

  // ── Cap notice (issues #77/#58) ───────────────────────────
  // Only for cities above the render cap. The stat table above always shows the
  // full count; this line is the one place the *drawn* count is exposed, plus
  // the opt-in override. A year filter usually drops below the cap (full detail
  // for that year), so the notice is framed around the whole-run picture.
  if (totalInMode > RENDER_CAP) {
    const shown = renderAllOverride ? totalInMode : Math.min(RENDER_CAP, totalInMode);
    html += `
      <div class="legend-divider"></div>
      <div class="legend-cap-notice">
        <p class="legend-meta">Drawing ${shown.toLocaleString()} of
          ${totalInMode.toLocaleString()} panos${renderAllOverride ? "" : "; filter or zoom for detail"}.</p>
        <button type="button" class="gsv-mode-btn"
                title="${renderAllOverride
                  ? "Return to the capped spatial subsample (faster on dense cities)"
                  : "Draw every panorama dot — can be slow on dense cities"}"
                onclick="setRenderAll(${!renderAllOverride})">
          ${renderAllOverride ? `Show ~${RENDER_CAP.toLocaleString()}` : "Render all"}
        </button>
      </div>`;
  }

  // ── Section 2: Snapshot history (v2 temporal data) ────────
  if (runsGlobal.length > 1) {
    const options = runsGlobal
      .slice()
      .reverse() // newest first
      .map((r) => {
        const selected = r.data_file === currentFileGlobal ? " selected" : "";
        return `<option value="${escapeHtml(r.data_file)}"${selected}>${escapeHtml(r.run_date)}${r.is_baseline ? " (baseline)" : ""}</option>`;
      })
      .join("");
    html += `
      <div class="legend-divider"></div>
      <div class="legend-year-header">
        <label for="run-select">Snapshot (${runsGlobal.length} runs)</label>
      </div>
      <select id="run-select" aria-label="Select collection snapshot"
              title="View an earlier collection snapshot of this city"
              style="width:100%;margin-top:4px"
              onchange="switchRun(this.value)">${options}</select>
      <div id="run-history-wrap">
        <canvas id="run-history-chart" role="img"
                aria-label="Panorama count across ${runsGlobal.length} collection runs; click a point to open that snapshot"></canvas>
      </div>`;
  }

  // ── Section 2b: change since previous run + diff overlay ──
  // The counts come from the run's own JSON (no fetch); the "Show changes on
  // map" button lazily fetches the published diff detail CSV and overlays
  // added/removed/re-dated dots (diff-overlay.js).
  const change = formatChangeSummary(changeGlobal);
  const diffFile = currentDiffFilename();
  if (change || diffFile) {
    const from = change?.from || previousRunDate() || "";
    html += `
      <div class="legend-divider"></div>
      <div class="legend-year-header">Since ${escapeHtml(from)}</div>`;
    if (change) {
      html += `
      <p class="legend-meta" style="margin:4px 0 0">
        <span class="change-added">${change.added}</span> /
        <span class="change-removed">${change.removed}</span>
        ${change.redated ? `<br>${change.redated}` : ""}
        ${change.coverage ? `<br>Coverage ${change.coverage}` : ""}
      </p>`;
    }
    if (diffFile) {
      const btnLabel = diffOverlay.loading
        ? "Loading…"
        : diffOverlay.shown ? "Hide changes on map" : "Show changes on map";
      html += `
      <button type="button" class="gsv-mode-btn" id="diff-overlay-btn"
              style="width:100%;margin-top:6px"
              aria-pressed="${diffOverlay.shown}"${diffOverlay.loading ? " disabled" : ""}
              title="Overlay the panoramas added (green), removed (red), or re-dated (amber) since the previous snapshot"
              onclick="toggleDiffOverlay()">${btnLabel}</button>`;
      const status = diffStatusHtml();
      if (status) {
        html += `<p class="legend-meta" id="diff-overlay-status" style="margin:4px 0 0">${status}</p>`;
      }
    }
  }

  // ── Section 3: GSV imagery mode toggle ────────────────────
  // Only for GSV runs that recorded copyright (so Google vs contributor is
  // known). Both marker sets are already in memory — this flips which are
  // drawn without any refetch. Other providers' rows are all provider
  // imagery, so there is nothing to toggle.
  if (providerGlobal === "gsv" && copyrightAvailableGlobal) {
    // Counts are fixed per run (computed once at finalize), not re-derived from
    // all N markers on every legend rebuild.
    const allCount = allMarkerCount;
    const googleCount = googleMarkerCount;
    html += `
      <div class="legend-divider"></div>
      <div class="legend-year-header">Imagery</div>
      <div class="gsv-mode-toggle" role="radiogroup" aria-label="Google Street View imagery filter">
        <button type="button" class="gsv-mode-btn ${!showAllGsv ? "active" : ""}"
                role="radio" aria-checked="${!showAllGsv}"
                title="Only official Google panoramas (copyright exactly '© Google') — the imagery Google's own cars collected"
                onclick="setGsvMode(false)">
          Google only <span class="year-count">(${googleCount.toLocaleString()})</span>
        </button>
        <button type="button" class="gsv-mode-btn ${showAllGsv ? "active" : ""}"
                role="radio" aria-checked="${showAllGsv}"
                title="All panoramas served by Street View, including contributor-uploaded (user-generated) imagery"
                onclick="setGsvMode(true)">
          All GSV <span class="year-count">(${allCount.toLocaleString()})</span>
        </button>
      </div>`;
  }

  // ── Section 3b: Flat-only imagery toggle (issue #116) ─────
  // A census run can cover a grid point with flat imagery but no 360° pano
  // (a FLAT_ONLY row). Those markers are off by default; this reveals them so
  // any-imagery coverage is visible alongside the pano layer. Shown only when
  // the run actually has flat-only points -- which is the whole condition. The
  // provider test that used to sit beside it was redundant (only a provider
  // emitting FLAT_ONLY rows can have any) AND wrong for a third one: the
  // markers were parsed and built, and then had no way to be shown.
  if (flatOnlyMarkers.length > 0) {
    html += `
      <div class="legend-divider"></div>
      <div class="legend-year-header">Any imagery</div>
      <button type="button" class="year-item ${showFlatOnly ? "active-item" : ""}"
              aria-pressed="${showFlatOnly}"
              aria-label="Toggle flat-only imagery markers, ${flatOnlyMarkers.length.toLocaleString()} points"
              title="Grid points covered only by flat/perspective imagery (no 360° panorama) — undated, so they sit outside the age coloring"
              onclick="toggleFlatOnly()">
        <i style="background:${FLAT_ONLY_COLOR}" class="${showFlatOnly ? "active" : ""}"
           aria-hidden="true"></i>
        Flat-only points <span class="year-count">(${flatOnlyMarkers.length.toLocaleString()})</span>
      </button>`;
  }

  // ── Section 4: Interactive year filter ────────────────────
  if (sortedYears.length > 0) {
    html += `
      <div class="legend-divider"></div>
      <div class="legend-year-header">Filter by Year</div>`;

    sortedYears.forEach((year) => {
      const age = currentYear - year;
      const color = getColor(age, providerGlobal);
      const isActive = activeYears.has(year);
      // Count only markers visible in the current mode, so the year rows
      // never advertise contributor panos that are hidden in Google-only. Uses
      // the per-year cache (O(years), not O(N) per legend rebuild).
      const count = inModeCountByYear.get(year)
        ?? (markersByYear[year] || []).filter(markerInMode).length;
      if (count === 0) return; // a year that is all-contributor drops out in Google-only mode

      // Real <button>s (native Enter/Space + focus) with aria-pressed
      // toggle state; the color swatch is decorative.
      html += `
        <button type="button" class="year-item ${isActive ? "active-item" : ""}"
                data-year="${year}" aria-pressed="${isActive}"
                aria-label="Filter to year ${year}, ${count.toLocaleString()} panoramas"
                onclick="toggleYear(${year})">
          <i style="background:${color}" class="${isActive ? "active" : ""}"
             aria-hidden="true"></i>
          ${year} <span class="year-count">(${count.toLocaleString()})</span>
        </button>`;
    });
  }

  // Replacing innerHTML destroys the focused element; if focus was on a
  // year button, put it back on the same year so keyboard users don't get
  // dropped to <body> on every toggle.
  const focusedYear = div.contains(document.activeElement)
    ? document.activeElement.dataset?.year
    : null;
  div.innerHTML = html;
  if (focusedYear != null) {
    div.querySelector(`.year-item[data-year="${focusedYear}"]`)?.focus();
  }

  // The innerHTML swap above destroyed any previous chart canvas — rebuild.
  rebuildRunHistoryChart();
}

/**
 * Show or hide the flat-only imagery layer (issue #116, Mapillary only).
 * These points carry no capture date, so they sit outside the year/pano
 * machinery — a simple add/remove of the marker set.
 */
function toggleFlatOnly() {
  showFlatOnly = !showFlatOnly;
  flatOnlyMarkers.forEach((m) => (showFlatOnly ? m.addTo(map) : m.remove()));
  updateLegend(Object.keys(markersByYear).map(Number));
}

/**
 * Navigate to another snapshot of the same city (full reload keeps the
 * streaming pipeline simple — each run is its own csv.gz/json.gz pair).
 *
 * Carries ?network= forward: it selects which road walk the street panel draws,
 * and that choice belongs to the city, not to the snapshot being viewed.
 *
 * @param {string} dataFile - csv.gz filename of the selected run.
 */
function switchRun(dataFile) {
  if (dataFile && dataFile !== currentFileGlobal) {
    const network =
      streetNetworkType === DEFAULT_STREET_NETWORK_TYPE
        ? ""
        : `&network=${encodeURIComponent(streetNetworkType)}`;
    window.location.href = `city.html?file=${encodeURIComponent(dataFile)}${network}`;
  }
}

// ── Change-since-previous-run overlay (diff-overlay.js renders it) ──

/** Index of the displayed run in runsGlobal, or -1. */
function currentRunIndex() {
  return runsGlobal.findIndex((r) => r.data_file === currentFileGlobal);
}

/** The previous run's date ("YYYY-MM-DD"), or null on a first/unknown run. */
function previousRunDate() {
  const i = currentRunIndex();
  return i > 0 ? runsGlobal[i - 1].run_date : null;
}

/**
 * The diff detail filename for the CURRENT run, validated, or null.
 *
 * The run's own stats JSON is authoritative when present: its
 * change_from_previous_run.diff_file is null exactly when no detail was
 * published (no changes, or grids didn't align). Only when the whole change
 * block is absent — a first run, or a run whose diff was never recorded — is
 * the name constructed from the run history, in which case the fetch itself
 * discovers whether the pair predates diff publishing. A rebuilt JSON
 * (json_summarizer.regenerate_run_json) replays its block from the run_diffs
 * row precisely so it does NOT land in that fallback.
 *
 * NEVER read from the URL; both sources still pass isValidDiffFilename before
 * any fetch (see the security note on that validator).
 *
 * @returns {?string}
 */
function currentDiffFilename() {
  if (changeGlobal) {
    return isValidDiffFilename(changeGlobal.diff_file) ? changeGlobal.diff_file : null;
  }
  const i = currentRunIndex();
  if (i < 1 || !cityIdGlobal) return null;
  const name = diffFilenameFor(
    cityIdGlobal, providerGlobal, runsGlobal[i - 1].run_date, runsGlobal[i].run_date);
  return isValidDiffFilename(name) ? name : null;
}

/** Status line under the diff button: error text or colored counts. */
function diffStatusHtml() {
  if (diffOverlay.error) return "Change detail not published for this run.";
  if (!diffOverlay.counts) return "";
  const c = diffOverlay.counts;
  const d = diffOverlay.drawn ?? c;
  const total = c.added + c.removed + c.redated;
  const totalDrawn = d.added + d.removed + d.redated;
  const capNote = totalDrawn < total
    ? `<br>Drawing ${totalDrawn.toLocaleString()} of ${total.toLocaleString()} changes`
    : "";
  return `<span class="change-added">●&nbsp;${c.added.toLocaleString()} added</span> ·
          <span class="change-removed">●&nbsp;${c.removed.toLocaleString()} removed</span> ·
          <span class="change-redated">●&nbsp;${c.redated.toLocaleString()} re-dated</span>${capNote}`;
}

/**
 * Show/hide the "changes since previous run" overlay. The diff detail CSV is
 * fetched once (on first show) and the built layer cached; later toggles just
 * add/remove it. A missing file — older pairs predate diff publishing — turns
 * into a status message, never a page error.
 */
async function toggleDiffOverlay() {
  const refreshLegend = () => updateLegend(Object.keys(markersByYear).map(Number));
  if (diffOverlay.loading) return;

  if (diffOverlay.layer) {
    diffOverlay.shown = !diffOverlay.shown;
    if (diffOverlay.shown) diffOverlay.layer.addTo(map);
    else map.removeLayer(diffOverlay.layer);
    refreshLegend();
    return;
  }

  const diffFile = currentDiffFilename();
  if (!diffFile) return;
  diffOverlay.loading = true;
  refreshLegend();
  try {
    const { layer, counts, drawn } = await renderDiffOverlay(map, diffFile, providerGlobal);
    Object.assign(diffOverlay, { layer, counts, drawn, shown: true });
  } catch (e) {
    console.info("Change detail unavailable for this run:", e.message);
    diffOverlay.error = true;
  }
  diffOverlay.loading = false;
  refreshLegend();
}

// ── Run-history mini-chart ─────────────────────────────────────

/**
 * (Re)build the pano-count-per-snapshot line chart under the snapshot
 * selector. Destroys the previous instance first — updateLegend repaints the
 * legend via innerHTML, which orphans the old canvas. Cheap: one point per
 * run, and cities have at most a few dozen runs. Clicking a point navigates
 * to that snapshot; coverage/median-age ride along in the tooltip (one axis —
 * two scales on one plot is a chart-crime).
 */
function rebuildRunHistoryChart() {
  runHistoryChart?.destroy();
  runHistoryChart = null;
  const canvas = document.getElementById("run-history-chart");
  if (!canvas) return;
  const idx = currentRunIndex();
  runHistoryChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: runsGlobal.map((r) => r.run_date),
      datasets: [{
        data: runsGlobal.map((r) => r.unique_panos ?? null),
        borderColor: "#1a73e8",
        backgroundColor: "#1a73e8",
        borderWidth: 2,
        pointRadius: runsGlobal.map((_, i) => (i === idx ? 5 : 3)),
        pointBackgroundColor: runsGlobal.map((_, i) => (i === idx ? "#1a73e8" : "#8ab0ea")),
        spanGaps: true, // old runs can carry null counts — bridge, don't break
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onClick: (evt, elements) => {
        if (elements.length) switchRun(runsGlobal[elements[0].index]?.data_file);
      },
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? "pointer" : "default";
      },
      scales: {
        x: { ticks: { font: { size: 9 }, maxRotation: 0, autoSkip: true }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { font: { size: 9 } }, grid: { color: "rgba(0,0,0,0.08)" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const r = runsGlobal[ctx.dataIndex];
              const lines = [`${(r.unique_panos ?? 0).toLocaleString()} panos`];
              if (r.coverage_rate_percent != null) {
                lines.push(`Coverage: ${r.coverage_rate_percent.toFixed(1)}%`);
              }
              if (r.median_pano_age_years != null) {
                lines.push(`Median age: ${r.median_pano_age_years.toFixed(1)} yrs`);
              }
              lines.push("Click to open this snapshot");
              return lines;
            },
          },
        },
      },
    },
  });
}

/**
 * Toggle visibility of markers for a single year. Re-clicking the
 * active year restores all years.
 *
 * @param {number} year
 */
function toggleYear(year) {
  const wasActive = activeYears.has(year);
  activeYears.clear();
  if (!wasActive) activeYears.add(year);
  applyDesired();
  updateLegend(Object.keys(markersByYear).map(Number));
}

/**
 * Switch the GSV imagery display mode between official-Google-only (default)
 * and all GSV (Google + contributor/UGC panos).
 *
 * A pure display switch: every contributor marker is already streamed into
 * memory, so this only shows/hides them and recomputes the derived views
 * (overview stats, dated-pano count, year filter, temporal plot) from the
 * matching JSON block and the in-memory markers — no network request. Any
 * active year/date filter is cleared, since its marker set is mode-specific.
 * No-op for non-GSV providers and archival GSV runs (no toggle is shown).
 *
 * @param {boolean} showAll - true = All GSV, false = Google only.
 */
function setGsvMode(showAll) {
  if (providerGlobal !== "gsv" || !copyrightAvailableGlobal) return;
  if (showAll === showAllGsv) return;
  showAllGsv = showAll;

  // Swap the stats block that drives the legend overview (age stats,
  // oldest/newest). Both blocks are present in the run JSON for GSV.
  panoStatsGlobal = showAllGsv
    ? statsGlobal.all_panos
    : (statsGlobal.google_panos ?? statsGlobal.all_panos);
  oldestDateGlobal = panoDateOrNull(panoStatsGlobal.age_stats.oldest_pano_date);
  newestDateGlobal = panoDateOrNull(panoStatsGlobal.age_stats.newest_pano_date);

  // Clear the year/date filters, reset the "Render all" override (a new mode is
  // a fresh honesty budget), rebuild the in-mode caches for the new marker set,
  // and reconcile the map. applyDesired restyles the drawn set (undoing any date
  // dimming); refreshTemporalPlot rebuilds the plot with fresh colors.
  activeYears.clear();
  selectedDate = null;
  renderAllOverride = false;
  rebuildModeCaches();

  totalPanosGlobal = totalInMode;
  applyDesired();
  refreshTemporalPlot();
  updateLegend(Object.keys(markersByYear).map(Number));
}

// ── URL parameters ─────────────────────────────────────────────
const urlParams = new URLSearchParams(window.location.search);
// ?file= is untrusted input concatenated onto the data base URL, so it is
// validated against the filename contract (isValidRunFilename): anything
// else — path traversal, non-run artifacts — is treated as absent.
const rawCsvFileParam = urlParams.get("file");
const csvFile = isValidRunFilename(rawCsvFileParam) ? rawCsvFileParam : null;
if (rawCsvFileParam && !csvFile) {
  console.warn("Ignoring invalid ?file= parameter:", rawCsvFileParam);
}
// ?network= selects WHICH road walk to draw, since a city can have one per OSM
// network type and their coverage numbers divide by different street-km
// denominators. Untrusted like ?file=, so an unknown value falls back to the
// default series ('drive') rather than silently rendering nothing. streets.html
// sets it so each listed walk links to itself.
const rawNetworkParam = urlParams.get("network");
const streetNetworkType = isKnownStreetNetworkType(rawNetworkParam)
  ? rawNetworkParam
  : DEFAULT_STREET_NETWORK_TYPE;
if (rawNetworkParam && rawNetworkParam !== streetNetworkType) {
  console.warn("Ignoring unknown ?network= parameter:", rawNetworkParam);
}
const cityQuery = urlParams.get("city");
const decodedCityQuery = cityQuery
  ? decodeURIComponent(cityQuery).replace(/^"(.*)"$/, "$1")
  : null;

// ── Fuzzy-matching helpers ─────────────────────────────────────

/**
 * Compute the Levenshtein edit distance between two strings.
 *
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
function levenshteinDistance(a, b) {
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;

  const matrix = [];
  for (let i = 0; i <= b.length; i++) matrix[i] = [i];
  for (let j = 0; j <= a.length; j++) matrix[0][j] = j;

  for (let i = 1; i <= b.length; i++) {
    for (let j = 1; j <= a.length; j++) {
      matrix[i][j] = b[i - 1] === a[j - 1]
        ? matrix[i - 1][j - 1]
        : Math.min(matrix[i - 1][j - 1], matrix[i][j - 1], matrix[i - 1][j]) + 1;
    }
  }
  return matrix[b.length][a.length];
}

/**
 * Parse a user-supplied location query string into structured components.
 *
 * @param {string} query - e.g. "Seattle, WA" or "Paris, France".
 * @param {Object} citiesData - Full cities.json payload (used to
 *   disambiguate whether the second token is a state or country).
 * @returns {{city: string, state: ?string, country: ?string}|null}
 */
function parseLocationQuery(query, citiesData) {
  if (!query || typeof query !== "string") return null;

  const parts = query.trim().split(",").map((p) => p.trim());

  if (parts.length === 3) {
    return { city: parts[0], state: parts[1], country: parts[2] };
  }

  if (parts.length === 2) {
    const cityPart = parts[0];
    const id = parts[1].toLowerCase();

    const stateIds = new Set();
    const countryIds = new Set();
    citiesData.cities.forEach((c) => {
      if (c.state?.code) stateIds.add(c.state.code.toLowerCase());
      if (c.state?.name) stateIds.add(c.state.name.toLowerCase());
      if (c.country?.code) countryIds.add(c.country.code.toLowerCase());
      if (c.country?.name) countryIds.add(c.country.name.toLowerCase());
    });

    const isState = stateIds.has(id);
    const isCountry = countryIds.has(id);

    if (isCountry && !isState) return { city: cityPart, state: null, country: id };
    return { city: cityPart, state: id, country: null };
  }

  if (parts.length === 1) {
    return { city: parts[0], state: null, country: null };
  }

  return null;
}

/**
 * Find the best fuzzy-matching city record for a parsed location query,
 * using weighted Levenshtein distances.
 *
 * @param {Object} parsedQuery - From {@link parseLocationQuery}.
 * @param {Object} citiesData - Full cities.json payload.
 * @param {number} [maxDistance=3] - Maximum acceptable weighted distance.
 * @returns {{match: Object, distance: number}|{match: null, error: string, suggestions: Object[]}}
 */
function findBestMatchingCity(parsedQuery, citiesData, maxDistance = 3) {
  if (!parsedQuery?.city) {
    return {
      match: null,
      error: "Invalid query format. Please use 'City, State', 'City, Country', or 'City, State, Country' format.",
    };
  }

  let bestMatch = null;
  let bestDistance = Infinity;
  const scored = [];

  citiesData.cities.forEach((cityData) => {
    const cityDist = levenshteinDistance(
      parsedQuery.city.toLowerCase(),
      (cityData.city || "").toLowerCase()
    );
    let total = cityDist * 2;

    if (parsedQuery.state) {
      const codeDist = levenshteinDistance(parsedQuery.state.toLowerCase(), (cityData.state?.code || "").toLowerCase());
      const nameDist = levenshteinDistance(parsedQuery.state.toLowerCase(), (cityData.state?.name || "").toLowerCase());
      total += Math.min(codeDist, nameDist);
    }

    if (parsedQuery.country) {
      const codeDist = levenshteinDistance(parsedQuery.country.toLowerCase(), (cityData.country?.code || "").toLowerCase());
      const nameDist = levenshteinDistance(parsedQuery.country.toLowerCase(), (cityData.country?.name || "").toLowerCase());
      total += Math.min(codeDist, nameDist);
    }

    scored.push({ city: cityData, totalDistance: total });

    if (total < bestDistance) {
      bestDistance = total;
      bestMatch = cityData;
    }
  });

  if (bestDistance > maxDistance) {
    scored.sort((a, b) => a.totalDistance - b.totalDistance);
    const suggestions = scored.slice(0, 3).map((s) => ({
      city: s.city.city,
      state: s.city.state?.code,
      country: s.city.country?.code,
    }));
    return {
      match: null,
      error: suggestions.length
        ? "No close matches found. Did you mean:\n" + suggestions.map((s) => [s.city, s.state, s.country].filter(Boolean).join(", ")).join("\n")
        : "No close matches found.",
      suggestions,
    };
  }

  return { match: bestMatch, distance: bestDistance };
}

// ── Temporal plot ──────────────────────────────────────────────

/**
 * Aggregate the in-mode markers into per-capture-date counts for the
 * temporal scatter plot. Reads the markers already on the map (respecting the
 * GSV Google-only/all mode) rather than a separate pass, so the plot and the
 * map always describe the same pano set. Keyed by each marker's original
 * YYYY-MM-DD capture-date string to avoid the UTC/local shift that would move
 * January/year-precision dates into the previous year.
 *
 * @returns {Array<{date: Date, count: number}>} Sorted oldest-first.
 */
function buildTemporalData() {
  const counts = new Map();
  Object.values(markersByYear).flat().forEach((m) => {
    if (!markerInMode(m)) return;
    const key = m.options.captureDateStr;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  return Array.from(counts.entries())
    .map(([d, c]) => ({ date: panoDateOrNull(d), count: c }))
    .sort((a, b) => a.date - b.date);
}

/**
 * Build the Chart.js dataset for a temporalData array (points colored by
 * capture-year age on the active provider's scale).
 *
 * @param {Array<{date: Date, count: number}>} temporalData
 * @returns {Object} A Chart.js scatter dataset.
 */
function buildTemporalDataset(temporalData) {
  const currentYear = new Date().getFullYear();
  return {
    label: "Panoramas",
    data: temporalData.map((d) => ({ x: d.date, y: d.count, opacity: 1 })),
    backgroundColor: temporalData.map((d) => getColor(currentYear - d.date.getFullYear(), providerGlobal)),
    pointBackgroundColor: temporalData.map((d) => getColor(currentYear - d.date.getFullYear(), providerGlobal)),
    pointRadius: 4,
    pointHoverRadius: 6,
    pointBorderWidth: 0,
    pointStyle: "circle",
  };
}

/**
 * Rebuild the temporal plot from the current in-mode markers, updating the
 * existing chart in place. Used after a GSV mode switch — recreating the
 * chart would duplicate the canvas click/keyboard listeners and the live
 * region, so we only swap the dataset and reset the keyboard cursor.
 */
function refreshTemporalPlot() {
  if (!temporalChartGlobal) return;
  temporalDataGlobal = buildTemporalData();
  temporalChartGlobal.data.datasets[0] = buildTemporalDataset(temporalDataGlobal);
  temporalKeyboardIdx = -1;
  temporalChartGlobal.update();
}

/**
 * Render a temporal scatter plot with vertical-line stems and
 * click-to-select date filtering. Called once per page load; later mode
 * switches go through refreshTemporalPlot(). The chart handle and its plotted
 * data live at module scope (temporalChartGlobal/temporalDataGlobal) so the
 * interaction handlers and refreshTemporalPlot() share them.
 *
 * @param {HTMLCanvasElement} canvas
 */
function createTemporalPlot(canvas) {
  const verticalLinePlugin = {
    id: "verticalLines",
    beforeDraw: (chart) => {
      const ctx = chart.ctx;
      const xAxis = chart.scales.x;
      const yAxis = chart.scales.y;

      chart.data.datasets[0].data.forEach((point, i) => {
        const x = xAxis.getPixelForValue(point.x);
        ctx.save();
        ctx.beginPath();
        ctx.strokeStyle = chart.data.datasets[0].backgroundColor[i];
        ctx.globalAlpha = point.opacity === undefined ? 1 : point.opacity;
        ctx.lineWidth = 3;
        ctx.moveTo(x, yAxis.getPixelForValue(point.y));
        ctx.lineTo(x, yAxis.getPixelForValue(0));
        ctx.stroke();
        ctx.restore();
      });
    },
  };

  temporalDataGlobal = buildTemporalData();

  const chart = new Chart(canvas, {
    type: "scatter",
    data: { datasets: [buildTemporalDataset(temporalDataGlobal)] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        // Tick/title color: #ccc on the rgba(80,80,80,.9) panel ≈ 6:1
        // contrast (#999 was ~2.9:1, below WCAG AA).
        x: {
          type: "time",
          time: { unit: "year", displayFormats: { year: "YYYY" } },
          grid: { display: false, color: "#888" },
          border: { color: "#888" },
          ticks: { color: "#ccc" },
          title: { color: "#ccc", display: true, text: "Capture Date" },
        },
        y: {
          grid: { display: false, color: "#888" },
          ticks: { color: "#ccc" },
          border: { color: "#888" },
          title: { color: "#ccc", display: true, text: "Num of Panoramas" },
          beginAtZero: true,
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          enabled: true,
          mode: "nearest",
          intersect: true,
          callbacks: {
            label: (ctx) =>
              `Date: ${moment(ctx.raw.x).format("MM/DD/YYYY")}, Count: ${ctx.raw.y}`,
          },
        },
      },
    },
    plugins: [verticalLinePlugin],
  });
  temporalChartGlobal = chart;
  setupTemporalToggle(chart);

  /** Apply (or toggle off) the date filter for one capture date. */
  function selectFilterDate(date) {
    if (selectedDate && date && selectedDate.getTime() === date.getTime()) {
      date = null; // re-selecting the active date clears the filter
    }
    selectedDate = date;
    // applyDesired unions in every in-mode marker of the date (so the map can't
    // under-show it under the render cap) and restyles the drawn set — matching
    // dots emphasized, the rest dimmed, or all reset when the filter clears.
    applyDesired();
    if (date) updateChartColorsForDate(chart, date);
    else resetChartColors(chart);
  }

  // Date-selection click handler
  canvas.addEventListener("click", (evt) => {
    const points = chart.getElementsAtEventForMode(evt, "nearest", { intersect: true }, true);
    if (points.length) {
      const data = chart.data.datasets[points[0].datasetIndex].data[points[0].index];
      selectFilterDate(new Date(data.x));
    } else {
      selectFilterDate(null);
    }
  });

  // Keyboard path to the same filter — canvas points can't be tabbed to.
  // Left/Right step through capture dates, Home/End jump, Escape clears.
  // Selections are announced through a visually-hidden live region.
  canvas.setAttribute("tabindex", "0");
  canvas.setAttribute("role", "application");
  canvas.setAttribute("aria-label",
    "Panoramas by capture date. Use Left and Right arrow keys to filter the map by date, Escape to clear the filter.");

  const liveRegion = document.createElement("div");
  liveRegion.className = "visually-hidden";
  liveRegion.setAttribute("aria-live", "polite");
  canvas.parentElement.appendChild(liveRegion);

  // temporalKeyboardIdx / temporalDataGlobal are module-level so a GSV mode
  // switch (which reassigns them via refreshTemporalPlot) is reflected here
  // without re-registering this listener.
  canvas.addEventListener("keydown", (e) => {
    if (temporalDataGlobal.length === 0) return;

    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      const delta = e.key === "ArrowRight" ? 1 : -1;
      temporalKeyboardIdx = temporalKeyboardIdx === -1
        ? (delta === 1 ? 0 : temporalDataGlobal.length - 1)
        : Math.min(Math.max(temporalKeyboardIdx + delta, 0), temporalDataGlobal.length - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      temporalKeyboardIdx = 0;
    } else if (e.key === "End") {
      e.preventDefault();
      temporalKeyboardIdx = temporalDataGlobal.length - 1;
    } else if (e.key === "Escape") {
      e.preventDefault();
      temporalKeyboardIdx = -1;
      selectFilterDate(null);
      liveRegion.textContent = "Date filter cleared";
      return;
    } else {
      return;
    }

    const d = temporalDataGlobal[temporalKeyboardIdx];
    selectedDate = null; // force re-apply even if the same date is revisited
    selectFilterDate(d.date);
    liveRegion.textContent =
      `${d.date.toLocaleDateString()}: ${d.count.toLocaleString()} panoramas highlighted`;
  });
}

/**
 * Wire the temporal panel's minimize/expand button. Collapsing hides the chart
 * body so it stops covering the map; the choice persists across runs/reloads in
 * localStorage. Chart.js is resized on expand because it can't measure a
 * display:none parent.
 *
 * @param {Chart} chart - The temporal Chart.js instance.
 */
function setupTemporalToggle(chart) {
  const container = document.getElementById("temporal-plot-container");
  const toggle = document.getElementById("temporal-plot-toggle");
  if (!container || !toggle) return;

  const KEY = "streetscape-temporal-collapsed";
  const apply = (collapsed) => {
    container.classList.toggle("collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label",
      collapsed ? "Expand capture-date chart" : "Minimize capture-date chart");
    toggle.textContent = collapsed ? "+" : "–";
    if (!collapsed) chart.resize();
  };

  let collapsed = false;
  try { collapsed = localStorage.getItem(KEY) === "1"; } catch { /* storage may be blocked */ }
  apply(collapsed);

  toggle.addEventListener("click", () => {
    collapsed = !collapsed;
    try { localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch { /* storage may be blocked */ }
    apply(collapsed);
  });
}

// ── Marker / chart style helpers ───────────────────────────────
// Map-marker styling for the date filter now lives in restyleOnMap() (which
// iterates only the drawn set, ≤ cap) via the pure markerDateStyle() helper;
// the chart-color helpers below are unchanged.

/**
 * Reset chart point colors and opacities to defaults.
 *
 * @param {Chart} chart - The Chart.js instance.
 */
function resetChartColors(chart) {
  const ds = chart.data.datasets[0];
  ds.data.forEach((pt) => { pt.opacity = 1; });
  ds.backgroundColor = ds.data.map((pt) => {
    const age = new Date().getFullYear() - new Date(pt.x).getFullYear();
    return getColor(age, providerGlobal);
  });
  ds.pointBackgroundColor = ds.backgroundColor;
  chart.update();
}

/**
 * Dim chart points that don't match the selected date.
 *
 * @param {Chart} chart - The Chart.js instance.
 * @param {Date} date - The selected date.
 */
function updateChartColorsForDate(chart, date) {
  const ds = chart.data.datasets[0];
  const dateStr = date.toDateString();

  ds.backgroundColor = ds.data.map((pt) => {
    const ptDate = new Date(pt.x);
    const age = new Date().getFullYear() - ptDate.getFullYear();
    const opacity = ptDate.toDateString() === dateStr ? 1 : 0.3;
    pt.opacity = opacity;
    return withAlpha(getColor(age, providerGlobal), opacity);
  });

  ds.pointBackgroundColor = ds.backgroundColor;
  chart.update();
}

// ── Main data loading ──────────────────────────────────────────

/**
 * Build a popup HTML string for a panorama marker, deep-linking to the
 * active provider's pano viewer.
 *
 * @param {Date} captureDate
 * @param {string} ageFormatted - Human-readable age string.
 * @param {string} panoId
 * @param {string} photographer - e.g. "Google" or a Mapillary contributor.
 * @returns {string} HTML string.
 */
function buildPopupHtml(captureDate, ageFormatted, panoId, photographer) {
  const provider = PROVIDERS[providerGlobal];
  // photographer is third-party content (Mapillary contributor names,
  // archival GSV credits) and pano_id comes straight from the CSV — both
  // must be escaped before entering popup HTML.
  return `
    <div style="font-family:sans-serif">
      <strong>Capture Date:</strong> ${captureDate.toLocaleDateString()}<br>
      <strong>Age:</strong> ${ageFormatted}<br>
      <strong>Photographer:</strong> ${escapeHtml(photographer)}<br>
      <strong>Pano ID:</strong> ${escapeHtml(panoId)}<br><br>
      <a href="${provider.viewerUrl(panoId)}"
         target="_blank" rel="noopener"
         style="color:#2196F3;text-decoration:none">
         ${provider.viewerLabel}
      </a>
    </div>
  `;
}

/**
 * Build a popup HTML string for a flat-only marker (issue #116).
 *
 * A FLAT_ONLY row is a presence marker: the grid point has flat/perspective
 * imagery but no 360° pano. It carries the nearest flat image's id and
 * copyright, but its capture_date is deliberately null — flat timestamps are
 * kept out of every dated-stat path — so this popup shows no date or age.
 * The image itself is still viewable, and `viewerUrl` takes any image key,
 * pano or flat, so the deep-link works unchanged.
 *
 * Falls back to the bare explanation when the row carries no image id (every
 * FLAT_ONLY row written to date does, but a link to nowhere is worse than none).
 *
 * @param {string} panoId - Mapillary image id, possibly empty.
 * @param {string} photographer - Mapillary contributor credit, possibly empty.
 * @returns {string} HTML string.
 */
function buildFlatOnlyPopupHtml(panoId, photographer) {
  const provider = PROVIDERS[providerGlobal];
  const note = "Flat imagery only — no 360° panorama here";
  if (!panoId) return `<div style="font-family:sans-serif">${note}</div>`;
  // photographer and panoId are third-party content straight from the CSV —
  // both must be escaped before entering popup HTML (cf. buildPopupHtml).
  return `
    <div style="font-family:sans-serif">
      ${note}<br><br>
      ${photographer ? `<strong>Photographer:</strong> ${escapeHtml(photographer)}<br>` : ""}
      <strong>Image ID:</strong> ${escapeHtml(panoId)}<br><br>
      <a href="${provider.viewerUrl(panoId)}"
         target="_blank" rel="noopener"
         style="color:#2196F3;text-decoration:none">
         ${provider.viewerLabel}
      </a>
    </div>
  `;
}

/**
 * Load, decompress, and parse the CSV data file for the target city.
 *
 * Uses a native browser streaming pipeline to avoid V8's string-length
 * limit on large files:
 *
 *   fetch → TransformStream (progress) → DecompressionStream → TextDecoderStream
 *                                                                      ↓
 *                                              manual reader.read() loop
 *                                                                      ↓
 *                                          PapaParse (fed string chunks)
 *
 * PapaParse does not support WHATWG ReadableStream as a direct input in
 * browser environments (only Node.js streams). We therefore drive the
 * read loop ourselves and hand PapaParse plain strings, which correctly
 * handles quoted fields (e.g. copyright_info values containing commas).
 *
 * Progress is tracked against the *compressed* byte count reported in
 * the city metadata JSON, giving an accurate download percentage.
 */
/**
 * Show a load error in the progress panel. Replaces the old alert(),
 * which blocked the page and left no trace once dismissed; the panel
 * stays visible next to the "Back to Overview Map" link.
 *
 * @param {string} message
 */
function showLoadError(message) {
  document.getElementById("progress-bar").style.display = "none";
  document.getElementById("progress-container").style.display = "block";
  document.getElementById("progress-text").textContent = message;
}

async function loadData() {
  if (!csvFile && !decodedCityQuery) {
    showLoadError("No city specified — open this page from the overview map, "
      + "or add ?file= or ?city= to the URL.");
    return;
  }

  // The streaming pipeline needs the native DecompressionStream
  // (Safari < 16.4 lacks it). Fail with a clear message instead of a
  // ReferenceError mid-download.
  if (typeof DecompressionStream === "undefined") {
    showLoadError("This browser can't stream the map data (it lacks "
      + "DecompressionStream). Please use a current Chrome, Firefox, Edge, "
      + "or Safari 16.4+.");
    return;
  }

  const progressContainer = document.getElementById("progress-container");
  const progressFill = document.getElementById("progress-fill");
  const progressText = document.getElementById("progress-text");

  try {
    progressContainer.style.display = "block";

    let targetFile = csvFile;

    // Fetch the aggregate: needed to resolve ?city= queries, and to find
    // this city's run history for the snapshot selector.
    let rawCities = null;
    let citiesData = null;
    try {
      progressText.textContent = "Loading city index…";
      rawCities = await fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "cities.json.gz");
      // ?city= queries resolve against the requested provider's view
      // (?provider=mapillary), defaulting to GSV
      const queryProvider = isKnownProvider(urlParams.get("provider"))
        ? urlParams.get("provider") : "gsv";
      citiesData = adaptCitiesPayload(rawCities, queryProvider);
    } catch (e) {
      // The ?file= path can still work without the aggregate
      console.warn("Could not load cities.json.gz:", e);
    }

    // Resolve city query → filename via the aggregate
    if (!csvFile && decodedCityQuery) {
      if (!citiesData) throw new Error("City index unavailable; cannot resolve ?city= query");
      progressText.textContent = `Finding city data for: ${decodedCityQuery}`;
      const parsedQuery = parseLocationQuery(decodedCityQuery, citiesData);
      const result = findBestMatchingCity(parsedQuery, citiesData);
      if (!result.match) {
        showLoadError(result.error);
        return;
      }
      targetFile = result.match.data_file.filename;
    }

    currentFileGlobal = targetFile;
    providerGlobal = getProviderFromFilename(targetFile);
    map.attributionControl.addAttribution(PROVIDERS[providerGlobal].attribution);

    // Locate this city's run history for the snapshot selector — only the
    // active provider's runs, so the <select> never mixes provider series
    if (rawCities) {
      const providerCities = adaptCitiesPayload(rawCities, providerGlobal).cities;
      const record = providerCities.find((c) =>
        c.data_file?.filename === targetFile ||
        (c.runs || []).some((r) => r.data_file === targetFile));
      if (record) {
        runsGlobal = record.runs || [];
        cityIdGlobal = record.city_id ?? null; // for the streetwalk manifest lookup
      }
    }

    // Load city-specific JSON metadata
    progressText.textContent = "Loading city metadata…";
    const metadataUrl = STREETSCAPE_DATA_BASE_URL + targetFile.replace(".csv.gz", ".json.gz");
    const stats = await fetchGzippedJson(metadataUrl);
    changeGlobal = stats.change_from_previous_run || null;

    const totalBytes = stats.data_file.size_bytes;
    const fileSizeMB = (totalBytes / (1024 * 1024)).toFixed(1);
    const cityName = stats.city.name;
    const stateName = stats.city.state?.name ?? null;
    const cityLabel = stateName ? `${cityName}, ${stateName}` : cityName;

    cityNameGlobal = cityName;
    stateNameGlobal = stateName ?? "";
    collectionDateGlobal = stats.download?.end_time
      ? new Date(stats.download.end_time).toLocaleDateString()
      : "";

    // Populate global stats for legend overview (available before streaming
    // starts). For GSV the provider block is the Google-copyright subset;
    // other providers' all_panos rows are already all provider imagery.
    statsGlobal = stats;
    copyrightAvailableGlobal = stats.copyright_info_available !== false;
    panoStatsGlobal = stats.google_panos ?? stats.all_panos;
    oldestDateGlobal = panoDateOrNull(panoStatsGlobal.age_stats.oldest_pano_date);
    newestDateGlobal = panoDateOrNull(panoStatsGlobal.age_stats.newest_pano_date);

    // Draw city bounds outline
    const bounds = stats.city.bounds;
    const regionCoords = [
      [bounds.min_lat, bounds.min_lon],
      [bounds.min_lat, bounds.max_lon],
      [bounds.max_lat, bounds.max_lon],
      [bounds.max_lat, bounds.min_lon],
    ];

    // Pure decoration: no tooltip, and explicitly non-interactive. Under
    // preferCanvas Leaflet hit-tests a polygon's whole interior regardless of
    // `fill: false` (Polygon._containsPoint is a point-in-polygon test), so an
    // interactive region outline would capture every mouseover inside the grid
    // and mask the pano markers' own hovers. The run-level stats it used to
    // show are all in the legend panel (updateLegend, section 1).
    L.polygon(regionCoords, {
      color: "cyan",
      weight: 2,
      opacity: 0.8,
      fill: false,
      interactive: false,
    }).addTo(map);

    // ── Streaming pipeline ─────────────────────────────────────
    //
    // Architecture:
    //   fetch response.body (Uint8Array chunks)
    //     → TransformStream  — counts compressed bytes for progress bar
    //     → DecompressionStream("gzip")  — native browser inflate
    //     → TextDecoderStream  — Uint8Array → UTF-8 string chunks
    //     → reader.read() loop  — feeds string chunks to PapaParse
    //
    // Why not Papa.parse(stream, ...)?
    //   PapaParse's ReadableStream support targets Node.js streams only.
    //   Passing a WHATWG ReadableStream throws a TypeError in _readChunk.

    const response = await fetch(STREETSCAPE_DATA_BASE_URL + targetFile);
    if (!response.ok) throw new Error(`HTTP ${response.status} fetching ${targetFile}`);

    let receivedBytes = 0;
    let lastShownPct = -1;
    const progressStream = new TransformStream({
      transform(chunk, controller) {
        receivedBytes += chunk.length;
        // Only touch the DOM when the whole percent changes — this fires
        // per network chunk (thousands of times on a big city)
        const pct = Math.min(Math.round((receivedBytes / totalBytes) * 100), 99);
        if (pct !== lastShownPct) {
          lastShownPct = pct;
          progressFill.style.width = `${pct}%`;
          progressFill.setAttribute("aria-valuenow", pct);
          progressText.textContent =
            `Downloading ${fileSizeMB} MB for ${cityLabel}… ${pct}%`;
        }
        controller.enqueue(chunk);
      },
    });

    const reader = response.body
      .pipeThrough(progressStream)
      .pipeThrough(new DecompressionStream("gzip"))
      .pipeThrough(new TextDecoderStream())
      .getReader();

    // Per-parse state
    const currentYear = new Date().getFullYear();
    const processedPanos = new Set();
    const validPoints = [];
    // Progressive render: draw in-mode markers as they stream in, but stop once
    // the cap is reached (a dense city's first rows are one geographic band, so
    // the honest spatial subsample is applied once at finalize instead). Small
    // cities never hit the cap and simply fill in live.
    let streamedInMode = 0;
    let streamCapReached = false;

    /**
     * Process a batch of parsed CSV rows, creating a map marker per pano.
     *
     * GSV runs mix official-Google and contributor (UGC) imagery. Both are
     * kept and tagged with `isGoogle` so the "GSV imagery" toggle can reveal
     * or hide the contributor markers with no refetch; only Google markers
     * are drawn initially (Google-only is the default mode). Other providers'
     * rows are all provider imagery, and archival runs recorded no copyright,
     * so those are tagged isGoogle:true (always in-mode). The temporal plot is
     * built later from the in-mode markers (buildTemporalData), so there is no
     * incremental temporal pass here.
     *
     * @param {Object[]} rows - PapaParse output rows.
     */
    function processRows(rows) {
      for (const row of rows) {
        // Flat-only imagery (issue #116): a grid point with flat/perspective
        // Mapillary imagery but no 360° pano. Kept in a separate, off-by-
        // default layer rather than the pano/year machinery (it has no
        // capture date, so it can't be year-bucketed or age-colored).
        if (row.status === "FLAT_ONLY") {
          // == null, not falsy: 0.0 is a valid coordinate (equator/meridian)
          if (row.pano_lat == null || row.pano_lon == null) continue;
          const flatMarker = L.circleMarker([row.pano_lat, row.pano_lon], {
            radius: 2,
            fillColor: FLAT_ONLY_COLOR,
            color: "#000",
            weight: 0,
            fillOpacity: 0.55,
          });
          flatMarker.bindPopup(
            buildFlatOnlyPopupHtml(row.pano_id, row.copyright_info));
          flatOnlyMarkers.push(flatMarker);
          if (showFlatOnly) flatMarker.addTo(map);
          continue;
        }
        if (
          row.status !== "OK" ||
          !row.capture_date ||
          !row.pano_id ||
          // == null, not falsy: 0.0 is a valid coordinate (equator/meridian)
          row.pano_lat == null ||
          row.pano_lon == null ||
          processedPanos.has(row.pano_id)
        ) continue;

        processedPanos.add(row.pano_id);

        // panoDateOrNull parses date-only strings as LOCAL midnight so the
        // year bucket/color matches the calendar date in the CSV (a UTC
        // parse shifted Jan/year-precision dates into the previous year
        // for visitors west of UTC).
        const captureDate = panoDateOrNull(row.capture_date);
        // Unreadable and IMPOSSIBLE dates are treated alike (issue #213): the
        // run CSV keeps whatever the provider said, so a contributor
        // photosphere dated 2611 arrives here intact and would otherwise be
        // drawn at age −585 and open a 2611 bucket in the temporal plot. This
        // map is date-coloured throughout, which is why it already draws no
        // NO_DATE pano at all — a date that cannot be true is no more
        // plottable than one that is missing.
        if (!isPlausibleCaptureDate(captureDate, providerGlobal)) continue;

        const year = captureDate.getFullYear();
        const age = currentYear - year;
        const ageInYears = (Date.now() - captureDate) / (1000 * 60 * 60 * 24 * 365.25);
        const ageFormatted = ageInYears < 1
          ? `${Math.round(ageInYears * 12)} months`
          : `${ageInYears.toFixed(1)} years`;

        // Official-Google iff the copyright matches exactly (see
        // isGoogleCopyright). Non-GSV/archival rows have no such distinction
        // and are treated as always-visible.
        const isGoogle = providerGlobal !== "gsv" || !copyrightAvailableGlobal
          || isGoogleCopyright(row.copyright_info);

        // Map marker. captureDateStr keeps the CSV's own YYYY-MM-DD string so
        // buildTemporalData can re-bucket by local date without a UTC shift.
        const marker = L.circleMarker([row.pano_lat, row.pano_lon], {
          radius: 3,
          fillColor: getColor(age, providerGlobal),
          color: "#000",
          weight: 0,
          opacity: 1,
          fillOpacity: 0.8,
          captureDate,
          captureDateStr: String(row.capture_date),
          isGoogle,
        });
        if (markerInMode(marker)) {
          validPoints.push([row.pano_lat, row.pano_lon]);
          if (!streamCapReached) {
            marker.addTo(map);
            onMap.add(marker);
            if (++streamedInMode >= RENDER_CAP) streamCapReached = true;
          }
        }

        const photographer = providerGlobal !== "gsv"
          ? (row.copyright_info || PROVIDERS[providerGlobal].label)
          : !copyrightAvailableGlobal
            ? (row.copyright_info || "Unknown")
            : isGoogle ? "Google" : (row.copyright_info || "Contributor");
        marker.bindPopup(buildPopupHtml(captureDate, ageFormatted, row.pano_id,
                                        photographer));

        if (!markersByYear[year]) markersByYear[year] = [];
        markersByYear[year].push(marker);
      }
    }

    // Manual read loop — feeds string chunks to PapaParse.
    //
    // Each chunk from TextDecoderStream is a UTF-8 string of arbitrary
    // size that may split mid-row. We accumulate in `buffer` and flush
    // only complete lines (up to the last newline) to PapaParse, keeping
    // the header row prepended so every batch parses correctly.
    //
    // PapaParse handles quoted fields with embedded commas or quotes
    // correctly on each batch because it sees a well-formed CSV fragment
    // with headers on every call. Caveat: a quoted field containing a
    // literal NEWLINE would be split across batches by the lastIndexOf
    // splitter above. That can't occur here — the writers (METADATA_DTYPES
    // schema, standardize_capture_date, pandas to_csv defaults) never emit
    // multi-line field values.

    // Per-column typing: only coordinates are numeric. Blanket
    // dynamicTyping would coerce pano_id to a float — Mapillary IDs are
    // numeric strings that can exceed 2^53 and would silently round,
    // corrupting the dedup set and viewer deep-links.
    const csvParseOptions = {
      header: true,
      dynamicTyping: { query_lat: true, query_lon: true, pano_lat: true, pano_lon: true },
      skipEmptyLines: true,
    };

    let buffer = "";
    let headerLine = null; // first line of the CSV (column names)

    while (true) {
      const { done, value } = await reader.read();

      if (done) {
        // Flush any remaining content after the last newline
        if (buffer.trim() && headerLine !== null) {
          const result = Papa.parse(`${headerLine}\n${buffer}`, csvParseOptions);
          processRows(result.data);
        }
        break;
      }

      buffer += value;

      // Only process complete lines; hold back any partial trailing line
      const lastNewline = buffer.lastIndexOf("\n");
      if (lastNewline === -1) continue; // no complete line yet

      const completeText = buffer.slice(0, lastNewline + 1);
      buffer = buffer.slice(lastNewline + 1);

      if (headerLine === null) {
        // First chunk: extract the header line, parse remainder
        const firstNewline = completeText.indexOf("\n");
        if (firstNewline === -1) {
          // Entire first chunk is still just the header (very unlikely)
          headerLine = completeText.trimEnd();
          continue;
        }
        headerLine = completeText.slice(0, firstNewline);
        const dataText = completeText.slice(firstNewline + 1);
        if (dataText.trim()) {
          const result = Papa.parse(`${headerLine}\n${dataText}`, csvParseOptions);
          processRows(result.data);
        }
      } else {
        // Same options as the first chunk — a blanket dynamicTyping here
        // would coerce pano_id to a float on every later chunk (Mapillary
        // IDs exceed 2^53 and silently round).
        const result = Papa.parse(`${headerLine}\n${completeText}`, csvParseOptions);
        processRows(result.data);
      }
    }

    // ── Finalise ───────────────────────────────────────────────
    progressFill.style.width = "100%";
    progressFill.setAttribute("aria-valuenow", 100);
    progressContainer.style.display = "none";

    // Fixed per-run marker counts (both GSV modes) for the legend's imagery
    // toggle — computed once here, not re-derived on every legend rebuild.
    const flatMarkers = Object.values(markersByYear).flat();
    allMarkerCount = flatMarkers.length;
    googleMarkerCount = flatMarkers.filter((m) => m.options.isGoogle).length;

    // Build the in-mode caches, then reconcile the drawn set to the honest
    // spatial subsample (a no-op for cities under the cap, which already filled
    // in progressively during streaming). Stats/count/plot use the FULL in-mode
    // set, so the cap never changes a reported number.
    rebuildModeCaches();
    totalPanosGlobal = totalInMode;
    applyDesired();

    updateLegend(Object.keys(markersByYear).map(Number));

    createTemporalPlot(document.getElementById("temporal-plot"));

    if (validPoints.length > 0) {
      map.fitBounds(L.latLngBounds(validPoints));
    }

    // Optional street-coverage overlay; a no-op when the city has no artifact.
    // Prefer the road-walk (streetwalk) coverage artifact when the manifest
    // advertises one for this city+provider (fractional per-edge coverage,
    // issue #99/#155); otherwise fall back to the grid-attribution
    // "_streets.json.gz" (issue #24). The streetwalk file is NOT derivable from
    // the run filename, so it comes from the sidecar manifest keyed by city_id.
    // A city can have one walk per OSM network type, chosen by ?network= and
    // defaulting to 'drive' (motorized roads), the scheduled series and what
    // this page has always shown. A broad 'all_public' walk covers a much larger
    // network — different street-km denominator, not a better number for the
    // same thing — so it is selected deliberately, never fallen back to.
    // The setPanoDotsVisible hook lets the panel show/hide the pano markers (a
    // coarse show-all/hide-all) so the streets can be read on their own; it goes
    // through the reconcile model so re-showing respects the mode/year/date/cap.
    const streetwalk = cityIdGlobal
      ? lookupStreetwalk(
          await fetchStreetwalkManifest(),
          cityIdGlobal,
          providerGlobal,
          streetNetworkType
        )
      : null;
    renderStreetCoverage(map, targetFile, providerGlobal, {
      streetwalkFile: streetwalk?.coverage_filename,
      setPanoDotsVisible: (visible) => {
        panoDotsHidden = !visible;
        applyDesired();
      },
    });

  } catch (error) {
    console.error("Error loading or parsing city data:", error);
    showLoadError(`Failed to load city data: ${error.message}`);
  }
}

loadData();