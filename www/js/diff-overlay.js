/**
 * diff-overlay.js
 * "What changed since the previous run" overlay for the per-city detail view.
 *
 * The pipeline publishes a diff detail CSV per consecutive run pair
 * ({city_id}_diff_[provider_]{FROM}_to_{TO}.csv.gz, written by
 * streetscape_metadata_tracker/diff.py): one row per changed pano —
 * pano_added, pano_removed, or capture_date_changed, with coordinates. This
 * module fetches that file and renders the churn as a dot layer over the map:
 * green = added, red = removed, amber = re-dated.
 *
 * The filename is resolved by city.js (from the run's own stats JSON, or
 * constructed from the run history) and validated with isValidDiffFilename
 * BEFORE it reaches renderDiffOverlay — never taken from the URL.
 *
 * Relies on globals from streetscape-utils.js: STREETSCAPE_DATA_BASE_URL,
 * fetchGzippedText, escapeHtml, spatialStrideSample, RENDER_CAP,
 * panoDateOrNull, isPlausibleCaptureDate — plus Leaflet (L) and PapaParse
 * (Papa). Loaded before city.js, which calls renderDiffOverlay().
 *
 * @module diff-overlay
 */

/** Dot colors per change type: presence green / absence red / changed amber. */
const DIFF_COLORS = {
  pano_added: "#2fb974",
  pano_removed: "#ef5350",
  capture_date_changed: "#f5a623",
};

/** Human labels per change type (popups + the legend counts line). */
const DIFF_LABELS = {
  pano_added: "Added",
  pano_removed: "Removed",
  capture_date_changed: "Re-dated",
};

/** Per-change-type cap on drawn diff dots. A big city's churn can reach 10⁵
 *  rows; like the pano layer, the drawn set is an honest spatial subsample
 *  and the reported counts always describe the full set. */
const DIFF_RENDER_CAP = Math.floor(RENDER_CAP / 4);

/**
 * Partition parsed diff rows by change type, dropping rows without usable
 * coordinates. Pure and DOM/Leaflet-free (node-testable).
 *
 * @param {Object[]} rows - PapaParse output rows ({change_type, pano_id,
 *   pano_lat, pano_lon, old_capture_date, new_capture_date}; lat/lon numeric
 *   via per-column dynamicTyping — pano_id stays a string, Mapillary ids
 *   exceed 2^53).
 * @returns {{added: Object[], removed: Object[], redated: Object[]}}
 */
function partitionDiffRows(rows) {
  const parts = { added: [], removed: [], redated: [] };
  for (const row of rows || []) {
    // == null, not falsy: 0.0 is a valid coordinate (equator/meridian)
    if (row.pano_lat == null || row.pano_lon == null) continue;
    if (row.change_type === "pano_added") parts.added.push(row);
    else if (row.change_type === "pano_removed") parts.removed.push(row);
    else if (row.change_type === "capture_date_changed") parts.redated.push(row);
  }
  return parts;
}

/**
 * Leaflet circleMarker style for a diff dot. A thin near-white ring pops the
 * dot off both the dark basemap and the age-colored pano dots underneath.
 * Pure (node-testable).
 *
 * @param {string} changeType - "pano_added" | "pano_removed" | "capture_date_changed".
 * @returns {Object} Leaflet circleMarker options (minus pane/position).
 */
function diffMarkerStyle(changeType) {
  return {
    radius: 4,
    fillColor: DIFF_COLORS[changeType] ?? DIFF_COLORS.capture_date_changed,
    color: "#f5f5f5",
    weight: 1,
    opacity: 0.9,
    fillOpacity: 0.9,
  };
}

/**
 * A diff row's capture date as popup text: the date itself, or "unknown" when
 * it is absent or cannot be true.
 *
 * The diff detail CSV is a RAW date stream, exactly like the run CSV city.js
 * streams — diff.py records what the provider said on both sides of the
 * comparison, and the published summaries are the only place issue #213's
 * narrowing is applied. So the guard is repeated here too, or a contributor
 * photosphere whose corrupt EXIF changed between two runs renders as
 * "2611-09-01 → 2612-01-01" in the popup.
 *
 * @param {?string} raw - old_capture_date or new_capture_date, as in the CSV.
 * @param {?string} provider - Provider key (see PROVIDERS).
 * @returns {string} Escaped date text, or "unknown".
 */
function diffCaptureDateText(raw, provider) {
  if (!raw) return "unknown";
  return isPlausibleCaptureDate(panoDateOrNull(raw), provider) ? escapeHtml(raw) : "unknown";
}

/**
 * Popup HTML for one diff row. Everything data-derived is escaped.
 *
 * @param {Object} row - A diff CSV row.
 * @param {?string} [provider="gsv"] - Provider key, for the date plausibility
 *   bounds (see diffCaptureDateText).
 * @returns {string}
 */
function diffPopupHtml(row, provider) {
  const label = DIFF_LABELS[row.change_type] ?? row.change_type;
  const oldDate = diffCaptureDateText(row.old_capture_date, provider);
  const newDate = diffCaptureDateText(row.new_capture_date, provider);
  const dates =
    row.change_type === "capture_date_changed"
      ? `<br><strong>Capture date:</strong> ${oldDate} → ${newDate}`
      : row.change_type === "pano_removed"
        ? `<br><strong>Was captured:</strong> ${oldDate}`
        : `<br><strong>Captured:</strong> ${newDate}`;
  return `
    <div style="font-family:sans-serif">
      <strong>${escapeHtml(label)}</strong> since the previous run${dates}<br>
      <strong>Pano ID:</strong> ${escapeHtml(row.pano_id)}
    </div>`;
}

/**
 * Fetch a diff detail CSV and build (and add to the map) its dot layer.
 *
 * @param {L.Map} map - The Leaflet map.
 * @param {string} diffFile - A VALIDATED diff detail filename (the caller
 *   runs isValidDiffFilename first).
 * @param {?string} [provider="gsv"] - Provider key, threaded through to the
 *   popups' capture-date plausibility bounds.
 * @returns {Promise<{layer: L.LayerGroup,
 *   counts: {added: number, removed: number, redated: number},
 *   drawn: {added: number, removed: number, redated: number}}>}
 * @throws {Error} On HTTP/decompression failure (e.g. the pair predates diff
 *   publishing) — the caller renders the graceful message.
 */
async function renderDiffOverlay(map, diffFile, provider) {
  // Own pane above the streets overlay (250) and the pano-dot canvas
  // (overlayPane, 400) — change dots are the question being asked, so they
  // draw on top. The map runs preferCanvas, and a marker's `pane` option
  // alone would still land it on the shared overlayPane canvas — a dedicated
  // canvas renderer bound to this pane is what actually lifts the dots.
  if (!map.getPane("diffOverlay")) {
    map.createPane("diffOverlay");
    map.getPane("diffOverlay").style.zIndex = 420;
  }
  const renderer = L.canvas({ pane: "diffOverlay" });

  const text = await fetchGzippedText(STREETSCAPE_DATA_BASE_URL + diffFile);
  // Same per-column typing discipline as the run CSV: pano_id must stay a
  // string (Mapillary ids exceed 2^53 and would silently round).
  const result = Papa.parse(text, {
    header: true,
    dynamicTyping: { pano_lat: true, pano_lon: true },
    skipEmptyLines: true,
  });
  const parts = partitionDiffRows(result.data);

  const layer = L.layerGroup();
  const counts = {};
  const drawn = {};
  for (const key of ["added", "removed", "redated"]) {
    const rows = parts[key];
    counts[key] = rows.length;
    // Honest spatial subsample per class, so a 10⁵-row churn can't freeze
    // the map — mirrors the pano layer's render cap.
    const subset =
      rows.length > DIFF_RENDER_CAP
        ? spatialStrideSample(rows.map((r) => [r.pano_lat, r.pano_lon]), DIFF_RENDER_CAP)
            .map((i) => rows[i])
        : rows;
    drawn[key] = subset.length;
    for (const row of subset) {
      layer.addLayer(
        L.circleMarker([row.pano_lat, row.pano_lon], {
          ...diffMarkerStyle(row.change_type),
          pane: "diffOverlay",
          renderer,
        }).bindPopup(diffPopupHtml(row, provider))
      );
    }
  }

  layer.addTo(map);
  return { layer, counts, drawn };
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>. renderDiffOverlay needs
// Leaflet + fetch, so only the pure helpers are unit-tested.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DIFF_COLORS,
    DIFF_LABELS,
    DIFF_RENDER_CAP,
    partitionDiffRows,
    diffMarkerStyle,
    diffCaptureDateText,
    diffPopupHtml,
    renderDiffOverlay,
  };
}
