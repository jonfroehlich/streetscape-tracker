// Offline unit tests for the pure helpers in diff-overlay.js — the
// "changes since previous run" overlay. Run with `npm test` (Node's built-in
// test runner) — no network, no jsdom, no Leaflet (renderDiffOverlay needs
// both and is covered by the e2e suite instead).

const test = require("node:test");
const assert = require("node:assert/strict");

global.STREETSCAPE_DATA_BASE_URL = "https://example.test/data/";
global.RENDER_CAP = 40000;
global.escapeHtml = (s) =>
  s == null ? "" : String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
global.spatialStrideSample = (points, cap) =>
  points.slice(0, cap).map((_, i) => i);
// The REAL date helpers, not stubs: the popup's capture-date guard is the same
// issue #213 rule the map applies, and a stub here would let the two drift.
const utils = require("../streetscape-utils.js");
global.panoDateOrNull = utils.panoDateOrNull;
global.isPlausibleCaptureDate = utils.isPlausibleCaptureDate;

const {
  DIFF_COLORS,
  DIFF_RENDER_CAP,
  partitionDiffRows,
  diffMarkerStyle,
  diffPopupHtml,
} = require("../diff-overlay.js");

// --- partitionDiffRows ------------------------------------------------------

test("partitionDiffRows: splits the three change types", () => {
  const parts = partitionDiffRows([
    { change_type: "pano_added", pano_lat: 1, pano_lon: 2 },
    { change_type: "pano_removed", pano_lat: 3, pano_lon: 4 },
    { change_type: "capture_date_changed", pano_lat: 5, pano_lon: 6 },
    { change_type: "pano_added", pano_lat: 7, pano_lon: 8 },
  ]);
  assert.equal(parts.added.length, 2);
  assert.equal(parts.removed.length, 1);
  assert.equal(parts.redated.length, 1);
});

test("partitionDiffRows: drops null coordinates but keeps 0.0 (equator/meridian)", () => {
  const parts = partitionDiffRows([
    { change_type: "pano_added", pano_lat: null, pano_lon: 2 },
    { change_type: "pano_added", pano_lat: 1, pano_lon: null },
    { change_type: "pano_added", pano_lat: 0, pano_lon: 0 },
  ]);
  assert.equal(parts.added.length, 1);
  assert.equal(parts.added[0].pano_lat, 0);
});

test("partitionDiffRows: unknown change types and empty/absent input are ignored", () => {
  const parts = partitionDiffRows([
    { change_type: "something_new", pano_lat: 1, pano_lon: 2 },
  ]);
  assert.equal(parts.added.length + parts.removed.length + parts.redated.length, 0);
  assert.deepEqual(partitionDiffRows(null), { added: [], removed: [], redated: [] });
});

test("partitionDiffRows: pano_id passes through untouched as a string", () => {
  // Mapillary ids exceed 2^53 — the parse options type only lat/lon, and the
  // partitioner must not coerce.
  const bigId = "9007199254740993123";
  const parts = partitionDiffRows([
    { change_type: "pano_added", pano_lat: 1, pano_lon: 2, pano_id: bigId },
  ]);
  assert.equal(parts.added[0].pano_id, bigId);
});

// --- diffMarkerStyle --------------------------------------------------------

test("diffMarkerStyle: one color per change type, ringed to pop over pano dots", () => {
  assert.equal(diffMarkerStyle("pano_added").fillColor, DIFF_COLORS.pano_added);
  assert.equal(diffMarkerStyle("pano_removed").fillColor, DIFF_COLORS.pano_removed);
  assert.equal(
    diffMarkerStyle("capture_date_changed").fillColor,
    DIFF_COLORS.capture_date_changed
  );
  const s = diffMarkerStyle("pano_added");
  assert.ok(s.weight >= 1); // the ring
  assert.ok(s.radius >= 4); // bigger than the radius-3 pano dots
  assert.equal(new Set(Object.values(DIFF_COLORS)).size, 3);
});

// --- diffPopupHtml ----------------------------------------------------------

test("diffPopupHtml: re-dated rows show old → new; content is escaped", () => {
  const html = diffPopupHtml({
    change_type: "capture_date_changed",
    pano_id: "<img src=x>",
    old_capture_date: "2020-06-01",
    new_capture_date: "2024-03-01",
  });
  assert.match(html, /2020-06-01/);
  assert.match(html, /2024-03-01/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
});

test("diffPopupHtml: an impossible capture date reads 'unknown', not 2611", () => {
  // The diff detail CSV is a RAW date stream like the run CSV — diff.py records
  // what the provider said — so issue #213's guard has to be repeated here or a
  // re-dated photosphere renders "2611-09-01 → 2612-01-01".
  const html = diffPopupHtml({
    change_type: "capture_date_changed",
    pano_id: "abc",
    old_capture_date: "2611-09-01",
    new_capture_date: "2024-03-01",
  }, "gsv");
  assert.doesNotMatch(html, /2611/);
  assert.match(html, /unknown → 2024-03-01/);

  // Both sides can be bad, and the removed/added variants use the same guard.
  assert.doesNotMatch(
    diffPopupHtml({ change_type: "pano_removed", pano_id: "a", old_capture_date: "1970-08-01" },
      "gsv"),
    /1970/);
  assert.match(
    diffPopupHtml({ change_type: "pano_added", pano_id: "a", new_capture_date: null }, "gsv"),
    /unknown/);
});

test("diffPopupHtml: the plausibility floor follows the provider", () => {
  // 2005 predates Street View but is ordinary Mapillary imagery, so the same
  // row must survive on one provider and be suppressed on the other.
  const row = { change_type: "pano_added", pano_id: "a", new_capture_date: "2005-06-01" };
  assert.doesNotMatch(diffPopupHtml(row, "gsv"), /2005/);
  assert.match(diffPopupHtml(row, "mapillary"), /2005-06-01/);
});

test("DIFF_RENDER_CAP: a fraction of the pano render cap, not the whole budget", () => {
  assert.ok(DIFF_RENDER_CAP > 0 && DIFF_RENDER_CAP < global.RENDER_CAP);
});
