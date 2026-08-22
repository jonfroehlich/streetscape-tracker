// Offline unit tests for the pure helpers in histogram-slider.js — the
// numeric filter control on the pivoted data-table pages (issue #250). Run
// with `npm test` (Node's built-in test runner) — no network, no jsdom.
//
// The DOM half (createHistogramSlider) is covered by the browser e2e smoke
// test instead (tests/e2e/test_smoke.py), the same split table-controls.js
// takes. In the browser these helpers read formatCellNumber from
// table-utils.js; here we take the real one.

const test = require("node:test");
const assert = require("node:assert/strict");

Object.assign(global, require("../table-utils.js"));

const {
  HISTOGRAM_SLIDER_BUCKETS,
  sliderStepFor,
  normalizeSliderRange,
  classifyBuckets,
  sliderValuetext,
  roundSliderValue,
} = require("../histogram-slider.js");

// --- sliderStepFor ----------------------------------------------------------

test("sliderStepFor: ~100 keyboard presses across the domain, on a 1/2/5 ladder", () => {
  assert.equal(sliderStepFor(0, 100), 1); // coverage %
  assert.equal(sliderStepFor(0, 1000), 10);
  assert.equal(sliderStepFor(0, 20), 0.2); // median age, years
  assert.equal(sliderStepFor(-60, 40), 1); // a Δ column spanning zero
  for (const [min, max] of [[0, 100], [0, 20], [0, 3], [-60, 40], [0, 8734.2]]) {
    const steps = (max - min) / sliderStepFor(min, max);
    assert.ok(steps >= 20 && steps <= 200, `${min}-${max} needs ${steps} presses`);
  }
});

test("sliderStepFor: NEVER zero, and never NaN — a zero step makes the arrows dead", () => {
  // Degenerate domains are real: one row, or every row at the same value.
  assert.equal(sliderStepFor(5, 5), 1);
  assert.equal(sliderStepFor(0, 0), 1);
  assert.equal(sliderStepFor(0, NaN), 1);
  assert.equal(sliderStepFor(0, Infinity), 1);
  for (const [min, max] of [[0, 0.0001], [0, 1e9], [-1, 1]]) {
    const step = sliderStepFor(min, max);
    assert.ok(Number.isFinite(step) && step > 0, `${min}-${max} yielded ${step}`);
  }
});

// --- normalizeSliderRange ---------------------------------------------------

const DOMAIN = { min: 0, max: 100 };

test("normalizeSliderRange: a full-span brush is NOT a filter and reads as nulls", () => {
  // This is what keeps an untouched slider out of the URL and keeps
  // isFilterUnset agreeing with what the reader sees.
  assert.deepEqual(normalizeSliderRange({ min: 0, max: 100 }, DOMAIN), { min: null, max: null });
  assert.deepEqual(normalizeSliderRange({ min: null, max: null }, DOMAIN), {
    min: null,
    max: null,
  });
  // One-sided brushes keep the bound they set and null the open end.
  assert.deepEqual(normalizeSliderRange({ min: 50, max: 100 }, DOMAIN), { min: 50, max: null });
  assert.deepEqual(normalizeSliderRange({ min: 0, max: 50 }, DOMAIN), { min: null, max: 50 });
});

test("normalizeSliderRange: crossed handles are swapped, not left empty", () => {
  // Typing 90 into the min box while max reads 10 must not blank the table.
  assert.deepEqual(normalizeSliderRange({ min: 90, max: 10 }, DOMAIN), { min: 10, max: 90 });
});

test("normalizeSliderRange: bounds outside the domain clamp to its edges", () => {
  assert.deepEqual(normalizeSliderRange({ min: -20, max: 140 }, DOMAIN), { min: null, max: null });
  assert.deepEqual(normalizeSliderRange({ min: 140, max: 160 }, DOMAIN), { min: 100, max: null });
});

test("normalizeSliderRange: unparseable bounds mean 'open at that end'", () => {
  assert.deepEqual(normalizeSliderRange({ min: NaN, max: 50 }, DOMAIN), { min: null, max: 50 });
  assert.deepEqual(normalizeSliderRange({ min: "60", max: undefined }, DOMAIN), {
    min: null,
    max: null,
  });
  assert.deepEqual(normalizeSliderRange(null, DOMAIN), { min: null, max: null });
});

test("normalizeSliderRange: a domain that does not start at zero still nulls at ITS edges", () => {
  // A Δ column's domain can be negative on both ends; "no filter" is the
  // domain edge, never the number 0.
  const domain = { min: -60, max: 40 };
  assert.deepEqual(normalizeSliderRange({ min: -60, max: 40 }, domain), { min: null, max: null });
  assert.deepEqual(normalizeSliderRange({ min: 0, max: 40 }, domain), { min: 0, max: null });
});

// --- classifyBuckets --------------------------------------------------------

const BUCKETS = [
  { from: 0, to: 25 },
  { from: 25, to: 50 },
  { from: 50, to: 75 },
  { from: 75, to: 100 },
];

test("classifyBuckets: OVERLAP, not containment — a straddled bucket still holds matches", () => {
  // Dimming a bucket claims "no selected rows here". A bucket cut by a handle
  // still contains rows that pass, so it stays lit.
  assert.deepEqual(classifyBuckets(BUCKETS, { min: 40, max: 60 }), [false, true, true, false]);
  assert.deepEqual(classifyBuckets(BUCKETS, { min: 25, max: 25 }), [true, true, false, false]);
});

test("classifyBuckets: an open end matches everything on that side", () => {
  assert.deepEqual(classifyBuckets(BUCKETS, { min: 60, max: null }), [false, false, true, true]);
  assert.deepEqual(classifyBuckets(BUCKETS, { min: null, max: 30 }), [true, true, false, false]);
  assert.deepEqual(classifyBuckets(BUCKETS, { min: null, max: null }), [true, true, true, true]);
  assert.deepEqual(classifyBuckets(BUCKETS, null), [true, true, true, true]);
});

test("classifyBuckets: an empty bucket list is not an error", () => {
  assert.deepEqual(classifyBuckets([], { min: 10, max: 20 }), []);
});

// --- sliderValuetext / roundSliderValue -------------------------------------

test("sliderValuetext: carries the unit, which is what makes the number mean something", () => {
  // aria-valuenow would announce a bare "58.7".
  assert.equal(sliderValuetext(58.74, { unit: "%", digits: 1 }), "58.7%");
  assert.equal(sliderValuetext(3, { unit: " yrs", digits: 1 }), "3.0 yrs");
  assert.equal(sliderValuetext(-12.5, { unit: " pp", digits: 1 }), "-12.5 pp");
  assert.equal(sliderValuetext(7), "7");
});

test("roundSliderValue: kills the float noise a stepped range input accumulates", () => {
  assert.equal(roundSliderValue(0.1 + 0.2), 0.3);
  assert.equal(roundSliderValue(18.442000000000004), 18.442);
  // ...without rounding away a real figure.
  assert.equal(roundSliderValue(85.1), 85.1);
  assert.equal(roundSliderValue(-0.0001), -0.0001);
});

test("HISTOGRAM_SLIDER_BUCKETS is the strip's cap, so the two read at one resolution", () => {
  assert.equal(HISTOGRAM_SLIDER_BUCKETS, 24);
});
