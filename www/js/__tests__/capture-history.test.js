// Offline unit tests for the pure helpers in capture-history.js -- the city
// page's harvested capture-history section (issue #109). Run with
// `node --test "js/__tests__/**/*.test.js"` -- no network, no jsdom, no
// Chart.js (rebuildCaptureHistoryChart needs a canvas and is covered by the
// e2e suite instead).

const test = require("node:test");
const assert = require("node:assert/strict");

global.escapeHtml = (s) =>
  s == null
    ? ""
    : String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
// A recognizable stub, so a test can see WHICH age and provider each bar was
// coloured by.
global.getColor = (age, provider) => `color(${age},${provider})`;
// The REAL gap-filler, not a stub: the section's zero bars are its behaviour.
global.buildFilledHistogram = require("../streetscape-utils.js").buildFilledHistogram;

const {
  CAPTURE_HISTORY_CANVAS_ID,
  captureHistoryYears,
  captureHistoryBars,
  captureHistoryLegendHtml,
} = require("../capture-history.js");

const CAVEAT =
  "Harvested from an unpublished Google endpoint; there is no guarantee it keeps working.";

/** The committed e2e fixture's summary, in shape and numbers. */
function fixtureSummary(overrides = {}) {
  return {
    schema_version: 1,
    artifact: "capture_history",
    provider: "gsv",
    harvest: { harvest_date: "2026-04-10" },
    source: { endpoint: "x", caveat: CAVEAT },
    panos: {
      unique_panos: 6,
      plausibly_dated_panos: 5,
      implausible_dates_dropped: 1,
      oldest_capture_date: "2009-06-01",
      newest_capture_date: "2024-06-01",
      years_with_imagery: 4,
    },
    histogram_of_capture_dates_by_year: { 2009: 1, 2012: 2, 2018: 1, 2024: 1 },
    ...overrides,
  };
}

// --- captureHistoryYears ------------------------------------------------------

test("captureHistoryYears: ascending numbers from string keys", () => {
  // Insertion order deliberately reversed: integer-like keys happen to
  // enumerate ascending in JS, so also feed a non-sorted array-free check.
  const years = captureHistoryYears({
    histogram_of_capture_dates_by_year: { "2024": 1, "2009": 3, "2012": 2 },
  });
  assert.deepEqual(years, [2009, 2012, 2024]);
  assert.ok(years.every((y) => typeof y === "number"));
});

test("captureHistoryYears: [] for a missing summary or an empty histogram", () => {
  assert.deepEqual(captureHistoryYears(null), []);
  assert.deepEqual(captureHistoryYears({}), []);
  assert.deepEqual(captureHistoryYears({ histogram_of_capture_dates_by_year: {} }), []);
});

// --- captureHistoryBars ---------------------------------------------------------

test("captureHistoryBars: gap years render as zero bars through currentYear", () => {
  const bars = captureHistoryBars(fixtureSummary(), "gsv", 2026);
  assert.equal(bars.labels.length, 2026 - 2009 + 1);
  assert.equal(bars.labels[0], "2009");
  assert.equal(bars.labels.at(-1), "2026");
  assert.equal(bars.counts[bars.labels.indexOf("2010")], 0);
  assert.equal(bars.counts[bars.labels.indexOf("2012")], 2);
  assert.equal(bars.counts.reduce((a, b) => a + b, 0), 5);
});

test("captureHistoryBars: each bar is coloured by its AGE on the provider's ramp", () => {
  const bars = captureHistoryBars(fixtureSummary(), "gsv", 2026);
  assert.equal(bars.colors[bars.labels.indexOf("2009")], "color(17,gsv)");
  assert.equal(bars.colors[bars.labels.indexOf("2026")], "color(0,gsv)");
});

test("captureHistoryBars: empty series when there are no years", () => {
  assert.deepEqual(captureHistoryBars(null, "gsv", 2026), { labels: [], counts: [], colors: [] });
});

// --- captureHistoryLegendHtml -----------------------------------------------------

test("captureHistoryLegendHtml: nothing at all without a harvest", () => {
  assert.equal(captureHistoryLegendHtml(null, "gsv"), "");
  assert.equal(
    captureHistoryLegendHtml(fixtureSummary({ histogram_of_capture_dates_by_year: {} }), "gsv"),
    ""
  );
});

test("captureHistoryLegendHtml: an accessible chart, a table of years WITH imagery", () => {
  const html = captureHistoryLegendHtml(fixtureSummary(), "gsv");
  assert.ok(html.includes(`id="${CAPTURE_HISTORY_CANVAS_ID}"`));
  assert.ok(html.includes('role="img"'));
  const label = /aria-label="([^"]*)"/.exec(html)[1];
  assert.ok(label.includes("2009-06-01") && label.includes("2024-06-01"), label);
  // One row per year that HAS imagery -- 4, not the 18 gap-filled bars.
  const tbody = /<tbody>(.*)<\/tbody>/s.exec(html)[1];
  assert.equal((tbody.match(/<tr>/g) || []).length, 4);
  assert.ok(html.includes("5 official panoramas across 4 years"));
  assert.ok(html.includes("harvested 2026-04-10"));
});

test("captureHistoryLegendHtml: the caveat is the summary's own, verbatim", () => {
  const html = captureHistoryLegendHtml(fixtureSummary(), "gsv");
  assert.ok(html.includes(CAVEAT), "the wording lives once, in Python");
});

test("captureHistoryLegendHtml: the dropped-dates line appears only when dates were dropped", () => {
  assert.match(captureHistoryLegendHtml(fixtureSummary(), "gsv"), /1 capture date left out/);
  const clean = fixtureSummary();
  clean.panos = { ...clean.panos, implausible_dates_dropped: 0 };
  assert.doesNotMatch(captureHistoryLegendHtml(clean, "gsv"), /left out/);
});

test("captureHistoryLegendHtml: escapes the published strings", () => {
  const hostile = fixtureSummary({
    source: { caveat: "<script>alert(1)</script>" },
    harvest: { harvest_date: '"><img src=x>' },
  });
  const html = captureHistoryLegendHtml(hostile, "gsv");
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;script&gt;"));
});
