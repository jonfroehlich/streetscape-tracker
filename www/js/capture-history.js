/**
 * capture-history.js
 * The city page's "Capture history" legend section (issue #109).
 *
 * Renders a city's harvested GSV capture history -- every official Google
 * panorama an out-of-band harvest (issue #2) found, by capture year -- from
 * the PRE-AGGREGATED summary JSON the aggregate points at
 * (`providers.<p>.capture_history.json_file`). The history CSV itself is never
 * streamed here; a per-pano map layer is a later slice of #109.
 *
 * Three rules this file holds:
 *   - ONE dataset. The harvest is a census and the run beside it a grid
 *     sample, so their counts are not comparable and are never overlaid.
 *   - The caveat is the summary's own `source.caveat`, shown VERBATIM -- the
 *     wording lives once, in Python (json_summarizer.CAPTURE_HISTORY_CAVEAT).
 *   - Dates are shown as the ISO strings the JSON carries. Nothing is parsed
 *     with `new Date()`, which reads a bare date as UTC midnight and shifts it
 *     a day west of Greenwich (the #226 class).
 *
 * Depends on globals from streetscape-utils.js: escapeHtml, getColor,
 * buildFilledHistogram; and the vendored Chart.js (`Chart`).
 * city.js only wires it; the pure helpers are unit-tested under Node
 * (js/__tests__/capture-history.test.js) and the chart by the e2e suite.
 *
 * @module capture-history
 */

/** DOM id of the section's chart canvas. */
const CAPTURE_HISTORY_CANVAS_ID = "capture-history-chart";

/**
 * The years with imagery in a capture-history summary, ascending.
 *
 * @param {?Object} summary - A capture-history summary JSON (schema v1).
 * @returns {number[]} Years present in `histogram_of_capture_dates_by_year`,
 *   ascending; `[]` when the summary or its histogram is missing or empty.
 *
 * @example
 *   captureHistoryYears({ histogram_of_capture_dates_by_year: { "2024": 1, "2009": 2 } });
 *   // [2009, 2024]
 */
function captureHistoryYears(summary) {
  const histogram = summary?.histogram_of_capture_dates_by_year;
  if (!histogram) return [];
  return Object.keys(histogram)
    .map(Number)
    .filter((y) => Number.isFinite(y))
    .sort((a, b) => a - b);
}

/**
 * The last year the chart's gap-fill may run through: the HARVEST's year.
 *
 * A harvest is a one-time census as of its harvest date, so a year after it
 * was never observed -- a zero bar there would claim "Google did not drive
 * here" about a year nobody looked at. The viewer's clock is therefore NOT
 * the end of the range (it is still what bars are aged against).
 * Falls back to `currentYear` only when the summary carries no readable
 * harvest date, and never ends before the newest year with imagery, so a bar
 * the summary does hold is never cut off.
 *
 * @param {?Object} summary - A capture-history summary JSON.
 * @param {number} currentYear - The viewer's year; the fallback end.
 * @returns {number} The inclusive last year of the filled range.
 *
 * @example
 *   captureHistoryEndYear({ harvest: { harvest_date: "2026-04-10" },
 *     histogram_of_capture_dates_by_year: { 2009: 1 } }, 2028);
 *   // 2026
 */
function captureHistoryEndYear(summary, currentYear) {
  const harvestYear = Number(String(summary?.harvest?.harvest_date ?? "").slice(0, 4));
  const end = Number.isInteger(harvestYear) && harvestYear > 0 ? harvestYear : currentYear;
  const years = captureHistoryYears(summary);
  return years.length > 0 ? Math.max(end, years[years.length - 1]) : end;
}

/**
 * Bar-chart series for the year histogram, gap-filled through the HARVEST year.
 *
 * A year with no imagery renders as a zero bar rather than being skipped: a
 * year Google did not drive the city is information, not missing data.
 * The fill stops at the harvest's year (captureHistoryEndYear), never the
 * viewer's: a year after the harvest was not observed, so it gets no bar.
 * Each bar is coloured by its AGE (currentYear - year) on the provider's
 * ramp, the same encoding the map's markers use.
 *
 * @param {?Object} summary - A capture-history summary JSON.
 * @param {string} provider - Provider key, for the colour ramp.
 * @param {number} currentYear - The viewer's year: what bars are aged
 *   against, and the fill's end only when the harvest date is unreadable.
 * @returns {{labels: string[], counts: number[], colors: string[]}}
 *   Parallel arrays, empty when the summary has no years.
 */
function captureHistoryBars(summary, provider, currentYear) {
  if (captureHistoryYears(summary).length === 0) {
    return { labels: [], counts: [], colors: [] };
  }
  const filled = buildFilledHistogram(
    summary.histogram_of_capture_dates_by_year,
    captureHistoryEndYear(summary, currentYear)
  );
  const years = Object.keys(filled)
    .map(Number)
    .sort((a, b) => a - b);
  return {
    labels: years.map(String),
    counts: years.map((y) => filled[y]),
    colors: years.map((y) => getColor(currentYear - y, provider)),
  };
}

/**
 * Pluralize a count with its noun.
 *
 * @param {number} n
 * @param {string} singular
 * @param {string} plural
 * @returns {string} e.g. "1 year", "4 years" (localized number).
 */
function pluralCount(n, singular, plural) {
  return `${Number(n).toLocaleString()} ${n === 1 ? singular : plural}`;
}

/**
 * The legend section's HTML, or "" when there is nothing to show.
 *
 * Gated on the summary alone: city.js passes null for a provider or city with
 * no harvest, so this never decides by provider name (#334).
 * The chart canvas carries a role/label for assistive technology, and a
 * `<details>` table of the years WITH imagery is the screen-reader path to
 * the same numbers.
 *
 * @param {?Object} summary - A capture-history summary JSON, or null.
 * @param {string} provider - Provider key (unused for gating; kept so the
 *   signature matches the chart builder's).
 * @returns {string} HTML for the section, starting with its divider.
 */
function captureHistoryLegendHtml(summary, provider) {
  const years = captureHistoryYears(summary);
  if (years.length === 0) return "";
  const panos = summary.panos || {};
  const histogram = summary.histogram_of_capture_dates_by_year;
  const oldest = escapeHtml(panos.oldest_capture_date ?? String(years[0]));
  const newest = escapeHtml(panos.newest_capture_date ?? String(years[years.length - 1]));
  const harvested = escapeHtml(summary.harvest?.harvest_date ?? "");
  const count = panos.plausibly_dated_panos ?? years.reduce((n, y) => n + histogram[y], 0);
  const yearsText = pluralCount(years.length, "year", "years");

  const rows = years
    .map((y) => `<tr><td>${y}</td><td>${Number(histogram[y]).toLocaleString()}</td></tr>`)
    .join("");

  let html = `
      <div class="legend-divider"></div>
      <div class="legend-year-header">Capture history <span class="legend-meta">(harvested)</span></div>
      <p class="legend-meta" style="margin:4px 0 0">${escapeHtml(pluralCount(count, "official panorama", "official panoramas"))} across ${escapeHtml(yearsText)} (${oldest} – ${newest}), harvested ${harvested}.</p>
      <div id="capture-history-wrap">
        <canvas id="${CAPTURE_HISTORY_CANVAS_ID}" role="img"
                aria-label="Official Google panoramas by capture year, ${oldest} to ${newest}, ${escapeHtml(yearsText)} with imagery"></canvas>
      </div>
      <details class="capture-history-table">
        <summary>Counts by year</summary>
        <table class="capture-history-counts" aria-label="Harvested panoramas by capture year">
          <thead><tr><th scope="col">Year</th><th scope="col">Panoramas</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </details>
      <p class="legend-meta capture-history-caveat" style="margin:4px 0 0">${escapeHtml(summary.source?.caveat ?? "")}</p>`;
  const dropped = panos.implausible_dates_dropped ?? 0;
  if (dropped > 0) {
    html += `
      <p class="legend-meta" style="margin:4px 0 0">${escapeHtml(pluralCount(dropped, "capture date", "capture dates"))} left out as implausible or unreadable (before such imagery existed, after the harvest, or not a valid date).</p>`;
  }
  return html;
}

/**
 * (Re)build the section's bar chart after a legend repaint.
 *
 * updateLegend swaps the legend's innerHTML, which destroys the old canvas,
 * so the previous Chart must be destroyed FIRST or Chart.js keeps a handle to
 * a detached canvas (the same ordering rebuildRunHistoryChart relies on).
 *
 * @param {?Object} summary - A capture-history summary JSON, or null.
 * @param {string} provider - Provider key, for the colour ramp.
 * @param {?Object} previousChart - The Chart this replaces, or null.
 * @returns {?Object} The new Chart, or null when the section is not rendered.
 */
function rebuildCaptureHistoryChart(summary, provider, previousChart) {
  previousChart?.destroy();
  const canvas = document.getElementById(CAPTURE_HISTORY_CANVAS_ID);
  if (!canvas || !summary) return null;
  const { labels, counts, colors } = captureHistoryBars(summary, provider, new Date().getFullYear());
  if (labels.length === 0) return null;
  return new Chart(canvas, {
    type: "bar",
    data: { labels, datasets: [{ data: counts, backgroundColor: colors }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) =>
              `${pluralCount(ctx.parsed.y, "panorama", "panoramas")} captured in ${ctx.label}`,
          },
        },
      },
      scales: {
        x: { ticks: { font: { size: 9 }, maxRotation: 0, autoSkip: true }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { font: { size: 9 }, precision: 0 } },
      },
    },
  });
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    CAPTURE_HISTORY_CANVAS_ID,
    captureHistoryYears,
    captureHistoryEndYear,
    captureHistoryBars,
    captureHistoryLegendHtml,
    rebuildCaptureHistoryChart,
  };
}
