/**
 * street-coverage.js
 * OSM street-coverage overlay for the per-city detail view (issue #24).
 *
 * Given a run's data filename, fetches the sibling
 * `{run_stem}_streets.json.gz` GeoJSON (written by
 * streetscape_street_analyzer.analyze) and draws each OSM street segment on
 * the map. Three view modes restyle the same layer in place:
 *
 *   - "age"      (default) covered segments use the provider age scale
 *                (matching the pano dots); uncovered are gray and dashed.
 *   - "coverage" binary covered (green) vs uncovered (gray, dashed).
 *   - "type"     each segment colored by OSM highway class; uncovered
 *                segments are faded + dashed so coverage still reads.
 *
 * The panel also renders a "coverage by street type" stacked bar chart.
 * Clicking a chart segment spotlights the matching street segments on the
 * map (all others dim); hovering previews the same. A "Show pano dots"
 * toggle hides the pano markers so the streets can be read on their own.
 *
 * The artifact is optional: most cities have no streets file yet, so a missing
 * file is a silent no-op (the rest of the page is unaffected).
 *
 * Color choices are the dataviz skill's validated dark palette (see
 * validate_palette.js): a green/slate presence-absence pair for coverage and
 * the 8-slot categorical theme for street type. The panel is dark so both the
 * covered green and the uncovered slate clear 3:1 against it — fixing the old
 * green-vs-gray and gray-on-gray legibility problems.
 *
 * Relies on globals from streetscape-utils.js: STREETSCAPE_DATA_BASE_URL,
 * PROVIDERS, getColor, fetchGzippedJson. Loaded before city.js, which calls
 * renderStreetCoverage().
 *
 * @module street-coverage
 */

/** Covered segments with no capture date (age unknown) — solid green. */
const STREET_COVERED_NODATE_COLOR = "#2fb974";
/** Binary "covered" color for the coverage view mode. */
const STREET_COVERED_COLOR = "#2fb974";
/** Segments with no imagery coverage — slate, drawn dashed. Clears 3:1 on the
 *  dark panel and still reads on the light map basemap (CVD ΔE ~30 vs green). */
const STREET_UNCOVERED_COLOR = "#767c85";

/** Pale end of the fractional-coverage ramp (a barely-driven edge). The full
 *  end is STREET_COVERED_COLOR, so a fully-covered edge matches the binary
 *  green exactly — the ramp is continuous with the other modes. Only the
 *  road-walk (streetwalk) artifact carries per-edge `coverage_fraction`; the
 *  grid-attribution artifact is binary and never uses this. */
const STREET_PARTIAL_LOW_COLOR = "#bfe8d4";

/** Panel surface color; also used as the inter-segment gap color in the chart. */
const STREET_PANEL_BG = "#1b1f24";

/**
 * Parse a #rrggbb hex to an [r, g, b] triple.
 * @param {string} hex
 * @returns {[number, number, number]}
 */
function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/**
 * Sequential green ramp for a fractional per-edge coverage value: pale green at
 * ~0 (mostly uncovered) → the full covered green at 1 (driven end to end).
 * @param {number} frac - Coverage fraction in [0, 1].
 * @returns {string} An rgb() color.
 */
function fractionColor(frac) {
  const f = Math.max(0, Math.min(1, Number(frac) || 0));
  const lo = hexToRgb(STREET_PARTIAL_LOW_COLOR);
  const hi = hexToRgb(STREET_COVERED_COLOR);
  const mix = (a, b) => Math.round(a + (b - a) * f);
  return `rgb(${mix(lo[0], hi[0])}, ${mix(lo[1], hi[1])}, ${mix(lo[2], hi[2])})`;
}

/**
 * Street-type categorical palette: the dataviz dark categorical slots assigned
 * to OSM highway classes in importance order. Exactly eight hues, and that is a
 * ceiling, not an accident — the dataviz rule is that a 9th category is never a
 * generated hue.
 *
 * The Python side's `_HIGHWAY_BUCKETS` (street_coverage.py) is much wider: a
 * broad-network walk (`--network-type all_public`) emits alleys, footways,
 * paths, pedestrian streets, cycleways, steps, tracks and bridleways too. Do
 * NOT "sync" the two by adding hues here. Extra classes are encoded without
 * new color instead:
 *
 *   - Service subtypes (alley, driveway, parking_aisle) are all `highway=service`
 *     and inherit the service hue via STREET_TYPE_FAMILY — a family mapping, not
 *     a new slot.
 *   - Non-motorized classes fold into the neutral "minor" gray, and render
 *     thinner in type mode (they are physically narrower ways). Thickness is
 *     the only free channel left: dash already means "uncovered" and opacity
 *     already means "not spotlighted".
 *   - To isolate one specific class, click its bar in the breakdown panel — the
 *     existing spotlight filters the map by {highway, covered}, which scales to
 *     any number of classes without a single new hue.
 */
const STREET_TYPE_COLORS = {
  motorway: "#3987e5",
  trunk: "#199e70",
  primary: "#c98500",
  secondary: "#008300",
  tertiary: "#9085e9",
  residential: "#e66767",
  unclassified: "#d55181",
  service: "#d95926",
};

/**
 * Classes that borrow another class's hue because they are a subtype of it.
 * `highway=service` + `service=alley|driveway|parking_aisle` are all service
 * roads; the analyzer splits them because an alley is a real back street while
 * a driveway is not, but on the map they belong to one visual family.
 */
const STREET_TYPE_FAMILY = {
  alley: "service",
  driveway: "service",
  parking_aisle: "service",
};

/**
 * Non-motorized OSM classes, present only in broad-network walks. They share
 * the minor gray and are drawn thinner — see STREET_TYPE_COLORS on why they get
 * no hue of their own.
 */
const STREET_TYPE_NON_MOTORIZED = new Set([
  "pedestrian",
  "footway",
  "path",
  "cycleway",
  "steps",
  "track",
  "bridleway",
]);

/** Fallback for highway classes outside STREET_TYPE_COLORS. */
const STREET_TYPE_MINOR_COLOR = "#8a8f97";

/**
 * Categorical color for an OSM highway bucket.
 * @param {string} highway - The segment's `highway` bucket.
 * @returns {string} A CSS color; the neutral "minor" gray for unlisted classes.
 */
function streetTypeColor(highway) {
  const key = STREET_TYPE_FAMILY[highway] || highway;
  return Object.prototype.hasOwnProperty.call(STREET_TYPE_COLORS, key)
    ? STREET_TYPE_COLORS[key]
    : STREET_TYPE_MINOR_COLOR;
}

/**
 * Whether a bucket is a non-motorized way (footpath, park trail, steps, ...).
 * @param {string} highway - The segment's `highway` bucket.
 * @returns {boolean}
 */
function isNonMotorizedType(highway) {
  return STREET_TYPE_NON_MOTORIZED.has(highway);
}

/**
 * Derive the streets artifact URL from a run's CSV filename.
 * Mirrors naming.streets_filename_for_run on the Python side — keep in sync,
 * including its suffix validation: a name that isn't a run csv.gz has no
 * sibling artifact, so throw rather than silently return a bogus URL (the
 * regex replace would be a no-op and we'd fetch the CSV itself).
 * @param {string} dataFile - e.g. "city_..._2026-07-06.csv.gz"
 * @returns {string} Full URL to the sibling "_streets.json.gz".
 * @throws {Error} If dataFile does not end in ".csv.gz".
 */
function streetsUrlForDataFile(dataFile) {
  if (!/\.csv\.gz$/.test(dataFile)) {
    throw new Error(`Not a run csv.gz filename: ${dataFile}`);
  }
  return STREETSCAPE_DATA_BASE_URL + dataFile.replace(/\.csv\.gz$/, "_streets.json.gz");
}

/**
 * Leaflet style for the "age" view mode: covered segments use the provider
 * age color (like the pano dots); uncovered segments are gray and dashed.
 * @param {Object} feature - GeoJSON feature with coverage properties.
 * @param {string} provider - Provider key for the age color scale.
 * @returns {Object} Leaflet path style.
 */
function styleStreetFeature(feature, provider) {
  const p = feature.properties || {};
  if (!p.covered) {
    return { color: STREET_UNCOVERED_COLOR, weight: 2, opacity: 0.75, dashArray: "4 4" };
  }
  const age = p.nearest_pano_age_years;
  const color = age == null ? STREET_COVERED_NODATE_COLOR : getColor(age, provider);
  return { color, weight: 3, opacity: 0.9 };
}

/**
 * Leaflet style for the "coverage" view mode. Binary covered green vs uncovered
 * slate (dashed) for the grid-attribution artifact; when the feature carries a
 * per-edge `coverage_fraction` (the road-walk artifact), covered edges instead
 * graduate along the sequential green ramp so a street driven end-to-end reads
 * differently from one glimpsed at a single intersection — the whole point of
 * the fractional modality.
 * @param {Object} feature - GeoJSON feature with coverage properties.
 * @returns {Object} Leaflet path style.
 */
function styleStreetByCoverage(feature) {
  const p = feature.properties || {};
  if (!p.covered) {
    return { color: STREET_UNCOVERED_COLOR, weight: 2, opacity: 0.75, dashArray: "4 4" };
  }
  if (p.coverage_fraction != null) {
    return { color: fractionColor(p.coverage_fraction), weight: 3, opacity: 0.95 };
  }
  return { color: STREET_COVERED_COLOR, weight: 3, opacity: 0.9 };
}

/**
 * Leaflet style for the "type" view mode: colored by highway class. Uncovered
 * segments keep their type color but are faded and dashed so coverage still
 * reads at a glance.
 *
 * Non-motorized ways (footpaths, park trails, steps — only present in a
 * broad-network walk) are drawn a step thinner. They share the minor gray with
 * living_street/other, so thickness is what separates "a narrow way for people"
 * from "a road class we don't give a hue to"; see STREET_TYPE_COLORS.
 *
 * @param {Object} feature - GeoJSON feature with coverage properties.
 * @returns {Object} Leaflet path style.
 */
function styleStreetByType(feature) {
  const p = feature.properties || {};
  const color = streetTypeColor(p.highway);
  const thin = isNonMotorizedType(p.highway) ? 1 : 0;
  if (!p.covered) {
    return { color, weight: 2 - thin * 0.5, opacity: 0.5, dashArray: "4 4" };
  }
  return { color, weight: 3 - thin, opacity: 0.9 };
}

/**
 * Dispatch to the per-mode base styler.
 * @param {Object} feature - GeoJSON feature.
 * @param {string} mode - "age" | "coverage" | "type".
 * @param {string} provider - Provider key (age mode only).
 * @returns {Object} Leaflet path style.
 */
function styleForMode(feature, mode, provider) {
  if (mode === "coverage") return styleStreetByCoverage(feature);
  if (mode === "type") return styleStreetByType(feature);
  return styleStreetFeature(feature, provider);
}

/**
 * Restyle the whole street layer for the current mode, applying a spotlight
 * when a selection is active: segments matching {highway, covered} keep full
 * opacity and thicken; everything else dims.
 * @param {L.GeoJSON} layer - The street layer.
 * @param {string} mode - Current view mode.
 * @param {string} provider - Provider key.
 * @param {?{type: string, covered: boolean}} selection - Active spotlight, or null.
 */
function applyStreetStyles(layer, mode, provider, selection) {
  layer.setStyle((feature) => {
    const base = styleForMode(feature, mode, provider);
    if (!selection) return base;
    const p = feature.properties;
    const match = p.highway === selection.type && Boolean(p.covered) === selection.covered;
    if (match) return { ...base, opacity: 1, weight: base.weight + 2 };
    return { ...base, opacity: 0.12 };
  });
}

// NOTE: the streetwalk-manifest helpers (streetwalkManifestUrl,
// fetchStreetwalkManifest, lookupStreetwalk) used to live here, but the
// overview map and streets.html need them too — they now live in
// streetscape-utils.js and reach this file as globals, like getColor.

/**
 * Normalize either street-coverage artifact into the single internal shape the
 * styling + panel code consumes, so one renderer serves both:
 *
 *   - grid-attribution `_streets.json.gz` (from `analyze`) — already in shape;
 *     binary per-edge `covered`, no fractional signal.
 *   - road-walk `_streetwalk_..._coverage.json.gz` (from `collect`, #99) — uses
 *     `median_covered_age_years` for the age color and `edges`/`edges_any_coverage`
 *     in its totals, and adds per-edge `coverage_fraction`.
 *
 * Mutates the fetched object in place (it's ours) and returns it plus the
 * derived `hasFractional` flag. Idempotent for the grid artifact (its keys are
 * already canonical), so `kind` only gates the aliasing that the grid data
 * doesn't need.
 *
 * @param {Object} fc - The parsed GeoJSON FeatureCollection.
 * @param {"grid"|"streetwalk"} kind - Which artifact this is.
 * @returns {{fc: Object, meta: ?Object, hasFractional: boolean}}
 */
function normalizeStreetArtifact(fc, kind) {
  let hasFractional = false;
  for (const feat of fc.features || []) {
    const p = feat.properties || (feat.properties = {});
    if (kind === "streetwalk" && p.nearest_pano_age_years == null) {
      // The road-walk age signal is the median age of an edge's covered
      // samples; the styler colors edges by `nearest_pano_age_years`.
      p.nearest_pano_age_years = p.median_covered_age_years ?? null;
    }
    if (p.coverage_fraction != null) hasFractional = true;
  }

  const meta = fc.properties && fc.properties.metadata;
  if (meta && meta.totals && kind === "streetwalk") {
    const t = meta.totals;
    // The panel headline reads `segments`/`covered`; the road-walk totals name
    // these `edges`/`edges_any_coverage` (an edge with ≥1 covered sample).
    if (t.segments == null) t.segments = t.edges;
    if (t.covered == null) t.covered = t.edges_any_coverage;
  }

  return { fc, meta, hasFractional };
}

/**
 * Fetch and render the street-coverage overlay and breakdown panel.
 * Silently returns if no streets artifact exists for this run.
 *
 * Prefers the road-walk (streetwalk) artifact when the caller supplies its
 * filename (via `options.streetwalkFile`, from the manifest); otherwise falls
 * back to deriving the grid-attribution `_streets.json.gz` from the run file.
 *
 * @param {L.Map} map - The Leaflet map (pano markers already added).
 * @param {string} dataFile - The active run's CSV filename.
 * @param {string} provider - Provider key ("gsv" | "mapillary").
 * @param {Object} [options] - Optional hooks from the caller.
 * @param {string} [options.streetwalkFile] - Road-walk coverage artifact filename
 *   (from the streetwalk manifest); when set, render it instead of the grid file.
 * @param {function(boolean):void} [options.setPanoDotsVisible] - Show/hide the
 *   pano dot markers (owned by city.js); enables the "Show pano dots" toggle.
 * @returns {Promise<void>}
 */
async function renderStreetCoverage(map, dataFile, provider, options = {}) {
  const kind = options.streetwalkFile ? "streetwalk" : "grid";
  let fc;
  try {
    const url = options.streetwalkFile
      ? STREETSCAPE_DATA_BASE_URL + options.streetwalkFile
      : streetsUrlForDataFile(dataFile);
    fc = await fetchGzippedJson(url);
  } catch (e) {
    console.info("No street-coverage artifact for this run (skipping):", e.message);
    return;
  }
  if (!fc || !fc.features || !fc.features.length) return;

  const { meta, hasFractional } = normalizeStreetArtifact(fc, kind);

  // Guard against a malformed artifact (partial upload, schema mismatch): the
  // panel is driven entirely by properties.metadata.{totals,coverage_by_highway},
  // so bail out of the whole overlay if that block is absent rather than throw
  // an unhandled rejection from this un-awaited call.
  if (!meta || !meta.totals || !meta.coverage_by_highway) {
    console.warn("Street-coverage artifact missing its metadata block (skipping overlay).");
    return;
  }

  // For the fractional (road-walk) artifact the coverage ramp is the payoff, so
  // open on it; the binary grid artifact opens on the age scale (matches the
  // pano dots).
  const initialMode = hasFractional ? "coverage" : "age";

  // Dedicated pane below the pano markers (overlayPane, z-index 400) so the
  // dots always draw on top of the street lines.
  if (!map.getPane("streetCoverage")) {
    map.createPane("streetCoverage");
    map.getPane("streetCoverage").style.zIndex = 250;
  }

  const layer = L.geoJSON(fc, {
    pane: "streetCoverage",
    style: (feature) => styleForMode(feature, initialMode, provider),
    onEachFeature: (feature, lyr) => {
      const p = feature.properties || {};
      const frac =
        p.coverage_fraction != null ? ` ${Math.round(p.coverage_fraction * 100)}%` : "";
      const status = p.covered
        ? `covered${frac}${p.nearest_pano_date ? ` · ${p.nearest_pano_date}` : ""}`
        : "no coverage";
      lyr.bindTooltip(`${p.highway} · ${status}`, { sticky: true });
    },
  }).addTo(map);

  buildStreetCoveragePanel(map, layer, meta, provider, {
    ...options,
    hasFractional,
    initialMode,
  });
}

/**
 * Build the top-left panel: headline, view-mode control, layer toggles, a
 * per-mode legend, and a stacked covered-vs-uncovered length bar chart by
 * street type (click a segment to spotlight it on the map).
 *
 * @param {L.Map} map - The map (for the show/hide toggles).
 * @param {L.GeoJSON} layer - The street layer to restyle/toggle.
 * @param {Object} meta - The artifact's `properties.metadata` block.
 * @param {string} provider - Provider key (for the human label).
 * @param {Object} options - Caller hooks (see renderStreetCoverage).
 */
function buildStreetCoveragePanel(map, layer, meta, provider, options) {
  const container = document.getElementById("street-coverage-container");
  if (!container) return;
  const providerLabel = PROVIDERS[provider]?.label ?? provider;
  const totals = meta.totals;
  const byType = meta.coverage_by_highway;

  // Types sorted by total length desc — the chart's row order.
  const types = Object.keys(byType).sort(
    (a, b) => byType[b].length_km - byType[a].length_km
  );

  // Shared view state, mutated by the controls below. The road-walk artifact
  // opens on the fractional coverage ramp; the grid artifact on the age scale.
  const hasFractional = Boolean(options.hasFractional);
  let mode = options.initialMode || "age";
  let selection = null; // {type, covered} spotlight, or null

  const uncoveredPct = totals.uncovered_pct_by_length;
  container.innerHTML = `
    <div id="street-coverage-header">
      <strong>Street coverage</strong>
      <label class="street-toggle">
        <input type="checkbox" id="street-layer-toggle" checked> streets
      </label>
    </div>
    <p id="street-coverage-headline">
      <span class="street-headline-pct">${uncoveredPct}%</span> of street-km have
      no ${providerLabel} imagery
      <span class="street-headline-sub">
        (${totals.covered.toLocaleString()} of
        ${totals.segments.toLocaleString()} segments covered)
      </span>
    </p>
    <div class="street-controls">
      <div class="street-mode" role="group" aria-label="Color streets by">
        <span class="street-mode-label">Color by</span>
        <button type="button" class="street-mode-btn is-active" data-mode="age" aria-pressed="true">Age</button>
        <button type="button" class="street-mode-btn" data-mode="coverage" aria-pressed="false">Coverage</button>
        <button type="button" class="street-mode-btn" data-mode="type" aria-pressed="false">Type</button>
      </div>
      <label class="street-toggle street-dots-toggle">
        <input type="checkbox" id="street-dots-toggle" checked> Show pano dots
      </label>
    </div>
    <div class="street-legend" id="street-legend"></div>
    <div id="street-chart-head">
      <span id="street-chart-title">Coverage by street type (km)</span>
      <button type="button" id="street-clear-selection" hidden>✕ clear</button>
    </div>
    <div id="street-chart-wrap"><canvas id="street-coverage-chart"></canvas></div>
  `;
  container.style.display = "block";

  const legendEl = document.getElementById("street-legend");

  // ── Legend (rebuilt per mode) ──────────────────────────────────
  const swatch = (color, label, dashed = false) =>
    `<span><i class="${dashed ? "dashed" : ""}" style="background:${color}"></i>${label}</span>`;

  function renderLegend() {
    if (mode === "coverage") {
      if (hasFractional) {
        // Fractional (road-walk): a pale→full green ramp chip plus uncovered.
        const stops = [STREET_PARTIAL_LOW_COLOR, STREET_COVERED_COLOR].join(",");
        legendEl.innerHTML =
          `<span><i style="background:linear-gradient(90deg,${stops})"></i>partial → full</span>` +
          swatch(STREET_UNCOVERED_COLOR, "no coverage", true);
      } else {
        legendEl.innerHTML =
          swatch(STREET_COVERED_COLOR, "covered") +
          swatch(STREET_UNCOVERED_COLOR, "no coverage", true);
      }
    } else if (mode === "type") {
      // Only the types actually present, in importance order, plus uncovered.
      legendEl.innerHTML =
        types
          .slice()
          .sort((a, b) => streetTypeOrder(a) - streetTypeOrder(b))
          .map((t) => swatch(streetTypeColor(t), t))
          .join("") + swatch(STREET_UNCOVERED_COLOR, "no coverage", true);
    } else {
      // Age: a small newest→oldest gradient chip plus the uncovered swatch.
      const stops = [0, 3, 6, 10].map((a) => getColor(a, provider)).join(",");
      legendEl.innerHTML =
        `<span><i style="background:linear-gradient(90deg,${stops})"></i>newer → older</span>` +
        swatch(STREET_UNCOVERED_COLOR, "no coverage", true);
    }
  }

  // ── View-mode buttons ──────────────────────────────────────────
  const modeButtons = Array.from(container.querySelectorAll(".street-mode-btn"));
  // Sync the active button to the initial mode (the HTML hardcodes Age active).
  modeButtons.forEach((b) => {
    const active = b.dataset.mode === mode;
    b.classList.toggle("is-active", active);
    b.setAttribute("aria-pressed", String(active));
  });
  modeButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      mode = btn.dataset.mode;
      modeButtons.forEach((b) => {
        const active = b === btn;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-pressed", String(active));
      });
      renderLegend();
      applyStreetStyles(layer, mode, provider, selection);
    });
  });

  // ── Layer + pano-dot toggles ───────────────────────────────────
  document.getElementById("street-layer-toggle").addEventListener("change", (e) => {
    if (e.target.checked) layer.addTo(map);
    else map.removeLayer(layer);
  });

  const dotsToggle = document.getElementById("street-dots-toggle");
  if (typeof options.setPanoDotsVisible === "function") {
    dotsToggle.addEventListener("change", (e) => {
      options.setPanoDotsVisible(e.target.checked);
    });
  } else {
    // No hook wired up (e.g. tests): hide the control rather than dangle it.
    dotsToggle.closest(".street-dots-toggle").style.display = "none";
  }

  // ── Selection plumbing ─────────────────────────────────────────
  const clearBtn = document.getElementById("street-clear-selection");
  function setSelection(next) {
    selection = next;
    clearBtn.hidden = next == null;
    applyStreetStyles(layer, mode, provider, selection);
  }
  clearBtn.addEventListener("click", () => {
    setSelection(null);
    paintChartSelection(chart, null);
  });

  renderLegend();

  // ── Stacked bar chart ──────────────────────────────────────────
  const coveredKm = types.map((t) => byType[t].length_km_covered);
  const uncoveredKm = types.map((t) => byType[t].length_km - byType[t].length_km_covered);

  // Size the chart to fit one labeled row per street type (Chart.js otherwise
  // auto-skips y labels once rows get short).
  document.getElementById("street-chart-wrap").style.height =
    `${Math.max(120, types.length * 30 + 44)}px`;

  const chart = new Chart(document.getElementById("street-coverage-chart"), {
    type: "bar",
    data: {
      labels: types,
      datasets: [
        {
          label: "Covered",
          data: coveredKm,
          backgroundColor: types.map(() => STREET_COVERED_COLOR),
          borderColor: STREET_PANEL_BG,
          borderWidth: 2,
          borderRadius: 2,
          borderSkipped: false,
        },
        {
          label: "No coverage",
          data: uncoveredKm,
          backgroundColor: types.map(() => STREET_UNCOVERED_COLOR),
          borderColor: STREET_PANEL_BG,
          borderWidth: 2,
          borderRadius: 2,
          borderSkipped: false,
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? "pointer" : "default";
        // Preview only when nothing is pinned by a click.
        if (selection) return;
        const hovered = elements.length ? selectionFor(elements[0]) : null;
        applyStreetStyles(layer, mode, provider, hovered);
      },
      onClick: (evt, elements) => {
        if (!elements.length) return;
        const clicked = selectionFor(elements[0]);
        if (!clicked) return; // zero-length segment
        const same =
          selection && selection.type === clicked.type && selection.covered === clicked.covered;
        setSelection(same ? null : clicked);
        paintChartSelection(chart, selection);
      },
      scales: {
        x: {
          stacked: true,
          title: { display: true, text: "Street length (km)", color: "#c3c2b7" },
          ticks: { color: "#a9a9a4" },
          grid: { color: "rgba(255,255,255,0.10)" },
        },
        y: {
          stacked: true,
          ticks: { color: "#e6e6e6", autoSkip: false },
          grid: { display: false },
        },
      },
      plugins: {
        legend: { labels: { color: "#e6e6e6", boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const t = byType[ctx.label];
              return ctx.datasetIndex === 0
                ? `Covered: ${t.length_km_covered.toFixed(1)} km (${t.coverage_pct_by_length}%)`
                : `No coverage: ${(t.length_km - t.length_km_covered).toFixed(1)} km`;
            },
          },
        },
      },
    },
    plugins: [coveragePctLabelPlugin],
  });

  // Reset the map spotlight when the pointer leaves the chart (unless pinned).
  document.getElementById("street-chart-wrap").addEventListener("mouseleave", () => {
    if (!selection) applyStreetStyles(layer, mode, provider, null);
  });

  /**
   * Map a clicked/hovered Chart.js element to a spotlight selection.
   * @param {{datasetIndex: number, index: number}} el
   * @returns {?{type: string, covered: boolean}} null if the segment is empty.
   */
  function selectionFor(el) {
    const type = types[el.index];
    const covered = el.datasetIndex === 0;
    const value = (covered ? coveredKm : uncoveredKm)[el.index];
    if (!value) return null;
    return { type, covered };
  }
}

/**
 * Importance rank of a highway bucket (for legend ordering); unlisted classes
 * sort last.
 *
 * Ordered in three runs so a broad-network walk reads top-to-bottom as a
 * hierarchy: motorized road classes by descending importance, then the
 * service-road family (alley is a real back street, driveway and parking aisle
 * are not), then non-motorized ways. `living_street` and `other` are
 * deliberately absent and sort last, as before.
 *
 * @param {string} highway
 * @returns {number}
 */
function streetTypeOrder(highway) {
  const order = [
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "residential",
    "unclassified",
    "service",
    "alley",
    "driveway",
    "parking_aisle",
    "pedestrian",
    "footway",
    "path",
    "cycleway",
    "steps",
    "track",
    "bridleway",
  ];
  const i = order.indexOf(highway);
  return i === -1 ? order.length : i;
}

/**
 * Fade the chart's non-selected segments to echo the map spotlight; pass null
 * to restore full color. Mutates per-bar backgroundColor arrays in place.
 * @param {Chart} chart
 * @param {?{type: string, covered: boolean}} selection
 */
function paintChartSelection(chart, selection) {
  const labels = chart.data.labels;
  const base = [STREET_COVERED_COLOR, STREET_UNCOVERED_COLOR];
  chart.data.datasets.forEach((ds, di) => {
    ds.backgroundColor = labels.map((label, li) => {
      if (!selection) return base[di];
      const match = label === selection.type && di === (selection.covered ? 0 : 1);
      return match ? base[di] : withStreetAlpha(base[di], 0.22);
    });
  });
  chart.update("none");
}

/**
 * Apply an alpha to a #rrggbb hex, returning an rgba() string.
 * @param {string} hex
 * @param {number} alpha
 * @returns {string}
 */
function withStreetAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/**
 * Inline Chart.js plugin: draw each covered segment's coverage % at the right
 * edge of its bar, when the segment is wide enough to fit the text. Keeps the
 * headline number close to the data without an external datalabels dependency.
 */
const coveragePctLabelPlugin = {
  id: "coveragePctLabel",
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    const meta = chart.getDatasetMeta(0); // covered dataset
    if (!meta || meta.hidden) return;
    ctx.save();
    ctx.font = "600 10px system-ui, -apple-system, sans-serif";
    ctx.fillStyle = "#0e1a12";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    meta.data.forEach((bar, i) => {
      const width = Math.abs(bar.x - bar.base);
      if (width < 26) return; // too narrow for a label
      const pct = chart.data.datasets[0].data[i];
      const total = pct + chart.data.datasets[1].data[i];
      if (!total) return;
      const label = `${Math.round((100 * pct) / total)}%`;
      ctx.fillText(label, bar.x - 4, bar.y);
    });
    ctx.restore();
  },
};

// Node/CommonJS export shim for the unit tests (issue #123). This is a no-op
// in the browser, where these symbols are plain globals loaded via <script>.
// renderStreetCoverage/buildStreetCoveragePanel need Leaflet + DOM, so only the
// pure helpers are unit-tested.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    streetsUrlForDataFile,
    styleStreetFeature,
    styleStreetByCoverage,
    styleStreetByType,
    styleForMode,
    streetTypeColor,
    streetTypeOrder,
    isNonMotorizedType,
    withStreetAlpha,
    fractionColor,
    hexToRgb,
    normalizeStreetArtifact,
    renderStreetCoverage,
    STREET_UNCOVERED_COLOR,
    STREET_COVERED_COLOR,
    STREET_COVERED_NODATE_COLOR,
    STREET_PARTIAL_LOW_COLOR,
    STREET_TYPE_COLORS,
    STREET_TYPE_MINOR_COLOR,
    STREET_TYPE_FAMILY,
  };
}
